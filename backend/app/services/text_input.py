"""Texto colado pelo usuário (mensagem encaminhada): limpeza, mascaramento e chave do cache.

O texto vira público para quem tiver o link do resultado, então dados pessoais óbvios
(telefone, e-mail, CPF, chave Pix aleatória) são mascarados ANTES de guardar e de enviar
ao Gemini. Nomes de pessoas não são detectáveis com segurança (risco residual, ver plano).
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

from app.services.url_guard import collect_urls

MAX_TEXT_CHARS = 5000
MIN_USEFUL_CHARS = 20
# Corte barato antes de qualquer regex: limpeza (espaços, invisíveis) raramente encolhe mais
# que isso, e um teto baixo limita a CPU gasta no event loop.
_MAX_RAW_CHARS = MAX_TEXT_CHARS * 2

_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
# O NFKC transformaria "2º turno" em "2o turno": estes ficam como o usuário escreveu.
_KEEP_COMPAT = frozenset("ºª²³")
_INLINE_SPACE = re.compile(r"[^\S\n]+")
_MANY_NEWLINES = re.compile(r"\n{3,}")
_FORWARD_MARKERS = {
    "encaminhada", "encaminhada com frequência", "forwarded", "forwarded many times",
}  # fmt: skip

# Ordem importa: padrões mais específicos primeiro.
_MASKS: tuple[tuple[re.Pattern[str], str], ...] = (
    # O lookbehind ancora o início: sem ele, 20 mil letras seguidas sem "@" custavam
    # segundos de CPU (backtracking quadrático) no event loop.
    (re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[e-mail oculto]"),
    (
        re.compile(
            r"(?<![0-9a-f-])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f-])",
            re.IGNORECASE,
        ),
        "[chave Pix oculta]",
    ),
    (re.compile(r"(?<![\d.])\d{3}\.\d{3}\.\d{3}-\d{2}(?!\d)"), "[CPF oculto]"),
    (re.compile(r"(?<![\d.])\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}(?!\d)"), "[CNPJ oculto]"),
    # Com DDD separado (e DDI opcional): (95) 99123-4567 · +55 95 991234567 · 95 3623-1234
    (
        re.compile(
            r"(?<![\w+])(?:\+?55[\s.-]?)?(?:\(\d{2}\)\s?|\d{2}[\s.-])9?\d{4}[\s.-]?\d{4}(?!\d)"
        ),
        "[telefone oculto]",
    ),
    # Tudo junto, só no formato de celular (DDD + 9 + 8 dígitos): 95991234567. Números comuns
    # de 10 ou 11 dígitos (valores, protocolos) não podem sumir do texto checado.
    (re.compile(r"(?<![\w+])(?:\+?55)?[1-9][1-9]9\d{8}(?!\d)"), "[telefone oculto]"),
    # Sem DDD, só com hífen: 3623-1234 · 99123-4567
    # Faixas de anos ("mandato 2023-2026") não são telefone.
    (
        re.compile(r"(?<![\d-])(?!(?:19|20)\d{2}-(?:19|20)\d{2}(?![\d-]))9?\d{4}-\d{4}(?![\d-])"),
        "[telefone oculto]",
    ),
)


# CPF sem pontuação: 11 dígitos seguidos, mascarado só se os dígitos verificadores baterem
# (sem isso, qualquer número de 11 dígitos sumiria do texto).
_BARE_CPF = re.compile(r"(?<![\d.\-/])\d{11}(?![\d.\-/])")
_CPF_MASK = "[CPF oculto]"


def _valid_cpf(digits: str) -> bool:
    if len(set(digits)) == 1:
        return False
    numbers = [int(d) for d in digits]
    for size in (9, 10):
        total = sum(n * w for n, w in zip(numbers[:size], range(size + 1, 1, -1), strict=True))
        check = (total * 10) % 11 % 10
        if check != numbers[size]:
            return False
    return True


def _mask_bare_cpf(match: re.Match[str]) -> str:
    return _CPF_MASK if _valid_cpf(match.group(0)) else match.group(0)


class TextTooShortError(ValueError):
    pass


class TextTooLongError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedText:
    body: str
    text_hash: bytes
    # URLs citadas no texto: viram fontes permitidas para o Gemini, mas NUNCA são baixadas.
    urls: list[str] = field(default_factory=list)


def prepare_text(raw: str) -> PreparedText:
    if len(raw) > _MAX_RAW_CHARS:
        raise TextTooLongError

    text = _INVISIBLE.sub("", _compat_normalize(raw))
    lines = [_INLINE_SPACE.sub(" ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line.casefold() not in _FORWARD_MARKERS]
    text = _MANY_NEWLINES.sub("\n\n", "\n".join(lines)).strip()

    text = _BARE_CPF.sub(_mask_bare_cpf, text)
    for pattern, replacement in _MASKS:
        text = pattern.sub(replacement, text)
    # Depois das máscaras: um wa.me/55959... ou um link com e-mail não vira fonte permitida
    # (e não chega ao Gemini) com o dado pessoal dentro.
    urls = collect_urls(text)

    # Nem o dado pessoal nem o "[telefone oculto]" contam como conteúdo para checar.
    content = text
    for _, replacement in _MASKS:
        content = content.replace(replacement, "")
    if sum(ch.isalnum() for ch in content) < MIN_USEFUL_CHARS:
        raise TextTooShortError
    if len(text) > MAX_TEXT_CHARS:
        raise TextTooLongError

    key = " ".join(text.split()).casefold()
    return PreparedText(
        body=text, text_hash=hashlib.sha256(key.encode("utf-8")).digest(), urls=urls
    )


def _compat_normalize(raw: str) -> str:
    """NFKC (letras "estilizadas" em negrito matemático viram letras normais), exceto
    ordinais e expoentes."""
    composed = unicodedata.normalize("NFC", raw)
    mapped = "".join(
        ch if ch in _KEEP_COMPAT else unicodedata.normalize("NFKC", ch) for ch in composed
    )
    return unicodedata.normalize("NFC", mapped)
