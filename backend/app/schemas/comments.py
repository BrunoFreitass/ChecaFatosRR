import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

# Controle (menos quebra de linha e tab) quebra o Postgres (NUL) ou esconde texto; os
# caracteres de direção (bidi) disfarçam o que se lê. Recusa os primeiros, remove os outros.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_BIDI = re.compile(r"[\u202a-\u202e\u2066-\u2069]")


def _clean_text(value: str) -> str:
    if _CONTROL.search(value):
        raise ValueError("o texto tem caracteres de controle")
    cleaned = _BIDI.sub("", value).strip()
    if not cleaned:
        raise ValueError("o texto está vazio")
    return cleaned


# Id público aleatório (base32 Crockford, 12 caracteres): nunca o bigint interno.
PublicId = Annotated[str, StringConstraints(pattern=r"^[0-9a-hjkmnp-tv-z]{12}$")]
CommentText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
    AfterValidator(_clean_text),
]


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    texto: CommentText
    checagem: PublicId | None = None  # sem checagem = feed geral (Comunidade)
    resposta_a: PublicId | None = None  # resposta (1 nível só)


class CommentOut(BaseModel):
    id: str
    apelido: str
    texto: str | None  # None = removido pelo autor
    criado_em: datetime
    removido_pelo_autor: bool = False
    respostas: list["CommentOut"] = Field(default_factory=list)


class CommentListOut(BaseModel):
    items: list[CommentOut]
    next_cursor: str | None


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    motivo: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


class ReportOut(BaseModel):
    status: Literal["recebida", "ja_recebida"]


AdminAction = Literal["aprovar", "remover", "banir", "confirmar_strike", "descartar"]


class AdminActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acao: AdminAction


class AdminActionOut(BaseModel):
    status: str


class QueueItemOut(BaseModel):
    # Id interno da fila: só o admin vê (rota protegida), nunca em URL pública.
    id: int
    tipo: str
    motivo: str
    categoria: str | None
    confianca: float | None
    criado_em: datetime
    apelido: str | None
    comentario: str | None  # id público do comentário (se publicado)
    status_comentario: str | None
    texto: str | None


class QueueListOut(BaseModel):
    items: list[QueueItemOut]
