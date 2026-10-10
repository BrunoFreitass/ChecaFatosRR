"""Middleware ASGI puro: request_id, cabeçalhos de segurança, log de acesso e 500 amigável.

ASGI puro (e não @app.middleware("http")) para:
- registrar o log e montar a resposta 500 com o request_id ainda no contexto;
- não relançar a exceção (o uvicorn não duplica o traceback);
- evitar as armadilhas do BaseHTTPMiddleware com contextvars.
"""

import logging
import time
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import INTERNAL_ERROR_MESSAGE, error_response
from app.logging_setup import request_id_var

logger = logging.getLogger("app.request")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    # A API só serve JSON: nada de carregar recursos nem ser emoldurada.
    "Strict-Transport-Security": "max-age=31536000",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}

# Maior corpo aceito. O maior pedido legítimo é um texto colado de 10.000 caracteres (~40 KB em
# UTF-8 no pior caso, com o JSON em volta). Sem teto, o Cloud Run aceita até 32 MiB por pedido.
MAX_BODY_BYTES = 64 * 1024
BODY_TOO_LARGE_MESSAGE = "O conteúdo enviado é grande demais."


class _BodyTooLargeError(Exception):
    pass


class BodySizeLimitMiddleware:
    """Recusa (413) corpos acima de MAX_BODY_BYTES antes de o FastAPI ler e validar tudo.

    Olha o Content-Length e também conta os bytes recebidos (corpo sem Content-Length,
    chunked). Fica DENTRO do RequestContextMiddleware: o 413 leva request_id e cabeçalhos.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                if not value.isdigit():
                    response = error_response(400, "requisicao_invalida", "Requisição inválida.")
                    await response(scope, receive, send)
                    return
                if int(value) > self.max_bytes:
                    await self._too_large(scope, receive, send)
                    return
                break

        received = 0
        too_large = False

        async def limited_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    too_large = True
                    raise _BodyTooLargeError
            return message

        async def guarded_send(message: Message) -> None:
            # O FastAPI captura erros da leitura do corpo e responde 400 por conta própria:
            # passado o limite, a resposta da aplicação é descartada e vai o 413.
            if not too_large:
                await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            # Passado o limite, qualquer erro que a leitura do corpo provoque vira 413.
            if not too_large:
                raise
        if too_large:
            await self._too_large(scope, receive, send)

    @staticmethod
    async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
        response = error_response(413, "corpo_muito_grande", BODY_TOO_LARGE_MESSAGE)
        await response(scope, receive, send)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                for name, value in SECURITY_HEADERS.items():
                    # /docs (só fora de produção) carrega o Swagger de um CDN: sem a CSP da API.
                    if name == "Content-Security-Policy" and scope["path"].startswith("/docs"):
                        continue
                    headers.setdefault(name, value)
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            logger.exception("unhandled_error")
            if response_started:
                # Já foi enviado parte da resposta; não há como trocar por um 500.
                raise
            response = error_response(500, "internal_error", INTERNAL_ERROR_MESSAGE)
            await response(scope, receive, send_with_headers)
        finally:
            logger.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            request_id_var.reset(token)
