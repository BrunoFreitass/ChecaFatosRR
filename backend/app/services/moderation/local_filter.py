"""Filtro local de conteúdo (camada 1 da moderação).

Barato e determinístico: roda antes de qualquer chamada à IA e pega as formas disfarçadas
mais comuns (acentos, leetspeak "p0rr4", letras separadas "p.o.r.r.a", letras repetidas,
números grudados "puta1").
"""

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_WORDLIST = Path(__file__).with_name("wordlist_pt.txt")
_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)
_SEPARATORS_KEEP_DIGITS = re.compile(r"[^a-z0-9]+")
_SEPARATORS_LETTERS_ONLY = re.compile(r"[^a-z]+")
_THREE_OR_MORE = re.compile(r"(.)\1{2,}")
_TWO_OR_MORE = re.compile(r"(.)\1+")

# Regra e explicação mostradas no aviso de violação.
RULES: dict[str, tuple[str, str]] = {
    "palavrao": (
        "Linguagem ofensiva",
        "Não são permitidos palavrões, nem disfarçados com números, símbolos ou espaços.",
    ),
    "sexual": (
        "Conteúdo sexual",
        "Não são permitidos termos sexuais ou obscenos.",
    ),
    "discurso_de_odio": (
        "Discurso de ódio",
        "Não são permitidos ataques ou ofensas contra pessoas ou grupos.",
    ),
}


@dataclass(frozen=True)
class FilterResult:
    blocked: bool
    category: str | None = None
    rule: str | None = None
    explanation: str | None = None


CLEAN = FilterResult(blocked=False)


@dataclass(frozen=True)
class _Wordlist:
    substrings: tuple[tuple[str, str], ...]  # (termo, categoria)
    whole_words: dict[str, str]  # termo → categoria
    nickname_only: dict[str, str]  # palavras inteiras bloqueadas só em apelidos


@lru_cache
def _wordlist() -> _Wordlist:
    substrings: list[tuple[str, str]] = []
    whole_words: dict[str, str] = {}
    nickname_only: dict[str, str] = {}
    category: str | None = None
    for raw_line in _WORDLIST.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            category = line[1:-1]
            if category not in RULES:
                raise ValueError(f"categoria desconhecida em {_WORDLIST.name}: {category}")
            continue
        if category is None:
            raise ValueError(f"termo fora de seção em {_WORDLIST.name}: {line}")
        if line.startswith("="):
            whole_words[line[1:]] = category
        elif line.startswith("~"):
            nickname_only[line[1:]] = category
        else:
            substrings.append((line, category))
    return _Wordlist(tuple(substrings), whole_words, nickname_only)


def normalize(text: str) -> str:
    """Minúsculas, sem acentos e com leetspeak traduzido ("Pórr4" → "porra")."""
    return _strip_accents(text).translate(_LEET)


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _split(text: str, separators: re.Pattern[str]) -> list[str]:
    """Palavras; letras soltas em sequência viram uma palavra ("p o r r a" → "porra")."""
    tokens: list[str] = []
    letters: list[str] = []
    for token in separators.split(text):
        if not token:
            continue
        if len(token) == 1:
            letters.append(token)
            continue
        if letters:
            tokens.append("".join(letters))
            letters = []
        tokens.append(token)
    if letters:
        tokens.append("".join(letters))
    return tokens


def _tokens(text: str) -> list[str]:
    # Duas leituras: com leetspeak ("p0rr4" → "porra") e com números como separadores
    # ("puta1" → "puta"), porque o leet transformaria o "1" em letra e esconderia a palavra.
    with_leet = _split(normalize(text), _SEPARATORS_KEEP_DIGITS)
    letters_only = _split(_strip_accents(text), _SEPARATORS_LETTERS_ONLY)
    return with_leet + [token for token in letters_only if token not in with_leet]


def _blocked(category: str) -> FilterResult:
    rule, explanation = RULES[category]
    return FilterResult(blocked=True, category=category, rule=rule, explanation=explanation)


def check_text(text: str, *, nickname: bool = False) -> FilterResult:
    """`nickname=True` também aplica as palavras ambíguas (ex.: "rola") só proibidas em apelidos."""
    wordlist = _wordlist()
    for token in _tokens(text):
        # "porrrraaa" → "porraa" (mantém letras dobradas legítimas, como em "porra").
        reduced = _THREE_OR_MORE.sub(r"\1\1", token)
        # "cuuuzao" → "cuzao", "carallho" → "caralho".
        single = _TWO_OR_MORE.sub(r"\1", token)
        for candidate in (token, reduced, single):
            category = wordlist.whole_words.get(candidate)
            if category is None and nickname:
                category = wordlist.nickname_only.get(candidate)
            if category:
                return _blocked(category)
        for term, category in wordlist.substrings:
            # A forma sem letras repetidas só vale para termos que não têm letra dobrada:
            # "porra" vira "pora", que aparece em "porão".
            if term in reduced or (_TWO_OR_MORE.search(term) is None and term in single):
                return _blocked(category)
    return CLEAN
