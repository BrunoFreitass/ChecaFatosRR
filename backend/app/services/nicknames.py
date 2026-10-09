"""Regras do apelido: formato, nomes reservados e o mesmo filtro de conteúdo dos comentários."""

import re
from dataclasses import dataclass
from typing import Literal

from app.services.moderation.local_filter import FilterResult, check_text, normalize

NicknameRejection = Literal["formato", "reservado", "conteudo_proibido"]

_FORMAT = re.compile(r"[A-Za-z0-9_]{3,20}")
# Bloqueados em qualquer posição do apelido.
_RESERVED_SUBSTRINGS = ("checafatos", "moderador", "moderacao", "administrador", "suporte")
# Bloqueados no começo do apelido ("AdminRR"), sem pegar "badminton".
_RESERVED_PREFIXES = ("admin", "staff", "root", "oficial")
# Bloqueados como parte separada por "_" ou números ("admin_rr", "tse2026").
_RESERVED_WORDS = frozenset(
    {"admin", "adm", "mod", "tse", "oficial", "staff", "root", "sistema", "ajuda"}
)
_PARTS = re.compile(r"[_0-9]+")


def _is_reserved(apelido: str) -> bool:
    lowered = apelido.lower()
    leet = normalize(apelido)  # "adm1n" → "admin", "checaf4tos" → "checafatos"
    parts = {part for part in _PARTS.split(lowered) if part} | {
        part for part in leet.split("_") if part
    }
    return (
        any(term in variant for term in _RESERVED_SUBSTRINGS for variant in (lowered, leet))
        or any(variant.startswith(_RESERVED_PREFIXES) for variant in (lowered, leet))
        or not parts.isdisjoint(_RESERVED_WORDS)
    )


@dataclass(frozen=True)
class NicknameVerdict:
    ok: bool
    reason: NicknameRejection | None = None
    filter_result: FilterResult | None = None


def validate_nickname(apelido: str) -> NicknameVerdict:
    if not _FORMAT.fullmatch(apelido):
        return NicknameVerdict(ok=False, reason="formato")

    if _is_reserved(apelido):
        return NicknameVerdict(ok=False, reason="reservado")

    # "_" vira espaço para o filtro tratar "p_o_r_r_a" como letras soltas.
    result = check_text(apelido.replace("_", " "), nickname=True)
    if result.blocked:
        return NicknameVerdict(ok=False, reason="conteudo_proibido", filter_result=result)
    return NicknameVerdict(ok=True)
