"""Logs estruturados em JSON (formato que o Cloud Logging entende) com request_id."""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Campos passados em logger.info(..., extra={...}) entram primeiro,
        # para nunca sobrescreverem os campos fixos abaixo.
        payload: dict[str, object] = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }
        payload.update(
            time=datetime.fromtimestamp(record.created, UTC).isoformat(),
            severity=record.levelname,
            logger=record.name,
            message=record.getMessage(),
        )
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    # O middleware registra cada requisição; o access log do uvicorn seria duplicado.
    logging.getLogger("uvicorn.access").disabled = True
