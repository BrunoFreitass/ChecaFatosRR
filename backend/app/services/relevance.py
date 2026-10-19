"""Relevância de uma checagem de agência para o conteúdo checado (salvaguarda de 19/10/2026).

Checagem de OUTRO assunto não pode sustentar "Verdadeiro" nem "Falso" abaixo de 70%: só conta
como do mesmo assunto a que tem pelo menos MIN_SHARED palavras-chave em comum com o conteúdo
(sem acento, sem maiúsculas, por prefixo; números só iguais). Sem IA e sem mexer no prompt.
"""

import re
import unicodedata

MIN_SHARED = 2
_PREFIX = 5
_MAX_KEYS = 80  # o começo do conteúdo basta (título + 1º parágrafo); texto longo casaria tudo

_URL = re.compile(r"https?://\S+")
_WORD = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*")

# Palavras vazias, frases prontas de checagem e palavras genéricas, já sem acento. Os nomes dos
# candidatos e "TSE" aparecem em quase toda alegação desta eleição: sozinhos não dizem que duas
# alegações são do mesmo assunto.
_IGNORED = frozenset(
    """
    a o as os um uma uns umas de da do das dos em no na nos nas num numa por pelo pela pelos pelas
    para pra pro com sem sob sobre entre ate e ou mas que se nao sim ser foi foram sao era esta
    estao estava ao aos so quem qual quais como quando onde ja mais menos muito muita muitos
    muitas isso isto esse essa esses essas este estes aquele aquela seu sua seus suas meu minha
    ele ela eles elas nos voce voces lhe ha tem ter agora outro outra outros outras todo toda
    todos todas tambem aqui ali vai vao diz dizem disse afirma afirmou segundo durante nenhum
    nenhuma nem pode podem relacao fala falar
    falso falsa falsos falsas verdade verdadeiro verdadeira enganoso enganosa boato boatos fake
    checamos checagem checa veja entenda resultado repassem repasse compartilhe compartilhem
    urgente atencao video videos foto fotos imagem imagens post posts publicacao mensagem texto
    noticia conteudo oculto
    lula flavio bolsonaro jair janja tse
    """.split()
)


def _plain(word: str) -> str:
    decomposed = unicodedata.normalize("NFKD", word.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def content_keys(*texts: str | None) -> list[str]:
    """Palavras-chave, na ordem, sem repetir: sem acento e em minúsculas; ficam palavras de 3+
    letras, siglas (TV) e números."""
    keys: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for word in _WORD.findall(_URL.sub(" ", text or "")):
            if not (len(word) >= 3 or any(c.isdigit() for c in word) or word.isupper()):
                continue
            key = _plain(word)
            if len(key) < 2 and not key.isdigit():
                continue
            if key in _IGNORED or key in seen:
                continue
            seen.add(key)
            keys.append(key)
            if len(keys) == _MAX_KEYS:
                return keys
    return keys


def _same(a: str, b: str) -> bool:
    if a.isdigit() or b.isdigit():
        return a == b
    a5, b5 = a[:_PREFIX], b[:_PREFIX]
    return a5.startswith(b5) or b5.startswith(a5)


def shared_keys(a: list[str], b: list[str]) -> int:
    """Quantas palavras-chave de `a` aparecem em `b` (por prefixo; números iguais)."""
    return sum(1 for x in a if any(_same(x, y) for y in b))


def is_relevant(check_title: str, keys: list[str]) -> bool:
    """A checagem (pelo título dela) é do mesmo assunto do conteúdo (palavras-chave `keys`)?"""
    return shared_keys(content_keys(check_title), keys) >= MIN_SHARED
