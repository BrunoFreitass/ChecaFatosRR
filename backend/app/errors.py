"""Erros da API no formato {"error": {"code", "message"}}, sempre com mensagem amigável em pt-BR.

Exceções não tratadas (500) são convertidas em app.middleware.RequestContextMiddleware,
para que a resposta ainda receba request_id, cabeçalhos de segurança e CORS.
"""

from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.db import DatabaseNotConfiguredError

_HTTP_MESSAGES: dict[int, tuple[str, str]] = {
    400: ("bad_request", "Requisição inválida."),
    401: ("unauthorized", "Você precisa entrar com sua conta Google."),
    403: ("forbidden", "Você não tem permissão para isso."),
    404: ("not_found", "Página ou recurso não encontrado."),
    405: ("method_not_allowed", "Operação não permitida."),
    429: ("rate_limited", "Muitas tentativas. Aguarde um pouco e tente de novo."),
}
_DEFAULT_HTTP_MESSAGE = ("http_error", "Não foi possível concluir.")

INTERNAL_ERROR_MESSAGE = "Algo deu errado do nosso lado. Tente de novo."


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        extra: dict[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}
        self.headers = dict(headers) if headers else None


def error_response(
    status_code: int,
    code: str,
    message: str,
    extra: dict[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, **(extra or {})}},
        headers=headers,
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, exc.extra, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message = _HTTP_MESSAGES.get(exc.status_code, _DEFAULT_HTTP_MESSAGE)
        # Preserva WWW-Authenticate, Retry-After, Allow etc.
        return error_response(exc.status_code, code, message, headers=exc.headers)

    @app.exception_handler(DatabaseNotConfiguredError)
    async def _db_not_configured(_: Request, __: DatabaseNotConfiguredError) -> JSONResponse:
        return error_response(503, "db_not_configured", "Serviço indisponível no momento.")

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [".".join(str(part) for part in err["loc"][1:]) for err in exc.errors()]
        return error_response(
            422,
            "validation_error",
            "Alguns dados enviados estão inválidos. Confira e tente de novo.",
            {"fields": [field for field in fields if field]},
        )
