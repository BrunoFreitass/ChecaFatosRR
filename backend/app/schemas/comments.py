import re
import unicodedata
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

# Controle (menos quebra de linha e tab) quebra o Postgres (NUL) ou esconde texto: recusa.
# Invisíveis (formatação Unicode "Cf": largura zero, direção/bidi, BOM, hífen suave, tags; e
# os preenchedores Hangul) disfarçam o que se lê e burlam o filtro de palavras: remove. O ZWJ
# fica, porque compõe emojis, mas ele e os seletores de variação não contam como conteúdo.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_ZWJ = chr(0x200D)
_FILLERS = frozenset(map(chr, (0x115F, 0x1160, 0x3164, 0xFFA0)))
_NOT_CONTENT = frozenset({_ZWJ, *map(chr, range(0xFE00, 0xFE10))})


def _reject_control(value: str) -> str:
    if _CONTROL.search(value):
        raise ValueError("o texto tem caracteres de controle")
    return value


def _strip_invisible(value: str) -> str:
    return "".join(
        c
        for c in _reject_control(value)
        if c == _ZWJ or (unicodedata.category(c) != "Cf" and c not in _FILLERS)
    ).strip()


def _clean_text(value: str) -> str:
    cleaned = _strip_invisible(value)
    if not "".join(c for c in cleaned if c not in _NOT_CONTENT).strip():
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

    motivo: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, max_length=200),
            AfterValidator(_strip_invisible),
        ]
        | None
    ) = None


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
