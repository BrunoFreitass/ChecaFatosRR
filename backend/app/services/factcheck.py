"""Checagens já publicadas por agências (Google Fact Check Tools API, claims:search).

Falha aqui nunca derruba a checagem: sem resposta, o Gemini analisa só o conteúdo e tende a
`nao_verificavel`. A chave vai no cabeçalho X-Goog-Api-Key (nunca na URL).
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

SEARCH_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
MAX_RESULTS = 10
# Até 3 consultas por checagem, em paralelo (diagnóstico de 19/10/2026: a frase longa só acha
# quando é quase igual ao título da checagem; palavras-chave acham muito mais).
MAX_QUERIES = 3
_QUERY_WORDS = 20
_MIN_SENTENCE_WORDS = 8
_KEYWORDS = 5

# Palavras vazias e frases prontas de checagem/notícia ("é falso que", "veja", "repassem"): não
# ajudam a achar a checagem e atrapalham a busca por palavras.
_STOPWORDS = frozenset(
    """
    a o as os um uma uns umas de da do das dos em no na nos nas num numa por pelo pela pelos pelas
    para pra pro com sem sob sobre entre até ate e ou mas que se não nao sim é e ser foi foram são
    sao era está esta estão estao estava ao aos à às só so quem qual quais como quando onde já ja
    mais menos muito muita muitos muitas isso isto esse essa esses essas este estes aquele aquela
    seu sua seus suas meu minha ele ela eles elas nós nos você voce vocês voces lhe há ha tem têm
    ter agora outro outra outros outras todo toda todos todas também tambem aqui ali vai vão vao
    diz dizem segundo
    falso falsa falsos falsas verdade verdadeiro verdadeira enganoso enganosa boato boatos fake
    checamos checagem veja entenda resultado repassem repasse compartilhe compartilhem urgente
    atenção atencao
    """.split()
)

_URL = re.compile(r"https?://\S+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*")


@dataclass(frozen=True)
class AgencyCheck:
    agencia: str
    titulo: str
    veredito: str
    url: str
    data: str | None = None

    def as_json(self) -> dict[str, str | None]:
        """Forma gravada no resultado da checagem (e devolvida pela API)."""
        return {
            "agencia": self.agencia,
            "titulo": self.titulo,
            "veredito": self.veredito,
            "url": self.url,
            "data": self.data,
        }


class FactCheckClient(Protocol):
    async def search(self, queries: list[str]) -> list[AgencyCheck]: ...


def build_queries(title: str | None, text: str) -> list[str]:
    """Até 3 consultas, sem repetição, nesta ordem:
    1. a frase principal (o título ou a 1ª frase do texto), como antes;
    2. as palavras-chave dela (sem palavras vazias nem frases prontas, na ordem do texto);
    3. as palavras-chave do texto: a 1ª frase do conteúdo (link) ou a frase seguinte (texto colado).
    """
    clean = _URL.sub(" ", text)
    sentences = [s for s in _SENTENCE_END.split(clean) if s.strip()]
    main = (_first_sentence(title) if title else None) or _first_sentence(text)
    if title:
        second, third = keywords(title), keywords(sentences[0] if sentences else None)
    else:
        first = sentences[0] if sentences else None
        rest = " ".join(sentences[1:]) or None
        second, third = keywords(first), keywords(rest)
    queries: list[str] = []
    for query in (main, second, third):
        if query and query.casefold() not in {q.casefold() for q in queries}:
            queries.append(query)
    return queries[:MAX_QUERIES]


def keywords(text: str | None, limit: int = _KEYWORDS) -> str | None:
    """As primeiras `limit` palavras de conteúdo, na ordem do texto: sem palavras vazias nem
    frases prontas, sem repetir; ficam nomes, siglas (TV, TSE) e números. Menos de 2: None."""
    if not text:
        return None
    picked: list[str] = []
    seen: set[str] = set()
    for word in _words(_URL.sub(" ", text)):
        key = word.casefold()
        if key in _STOPWORDS or key in seen:
            continue
        keep = (
            len(word) >= 3 or any(c.isdigit() for c in word) or (len(word) >= 2 and word.isupper())
        )
        if not keep:
            continue
        seen.add(key)
        picked.append(word)
        if len(picked) == limit:
            break
    return " ".join(picked) if len(picked) >= 2 else None


def _first_sentence(text: str | None) -> str | None:
    if not text:
        return None
    clean = _URL.sub(" ", text)
    candidates = [s for s in _SENTENCE_END.split(clean) if s.strip()]
    for sentence in candidates:
        words = _words(sentence)
        if len(words) >= _MIN_SENTENCE_WORDS:
            return " ".join(words[:_QUERY_WORDS])
    words = _words(clean)
    return " ".join(words[:_QUERY_WORDS]) if len(words) >= 3 else None


def _words(text: str) -> list[str]:
    # Só letras e números: emojis e pontuação ficam de fora da busca.
    return _WORD.findall(text)


class FactCheckUnavailableError(Exception):
    """A API não respondeu direito (rede, chave, cota, 5xx, JSON inválido)."""


class HttpFactCheckClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._transport = transport

    async def search(self, queries: list[str]) -> list[AgencyCheck]:
        """As consultas em paralelo (o teto de tempo é o de quem chama). Resultados na ordem das
        consultas, sem repetir link, até MAX_RESULTS. Nada encontrado: só é "não encontramos" se
        TODAS responderam; com alguma falha (ou sem consulta nenhuma), a busca falhou."""
        selected = queries[:MAX_QUERIES]
        if not selected:
            raise FactCheckUnavailableError
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport, trust_env=False
        ) as client:
            outcomes = await asyncio.gather(
                *(self._search_one(client, query) for query in selected), return_exceptions=True
            )
        merged: dict[str, AgencyCheck] = {}
        failed = False
        for outcome in outcomes:
            if isinstance(outcome, FactCheckUnavailableError):
                failed = True
                continue
            if isinstance(outcome, BaseException):
                raise outcome  # cancelamento/timeout de quem chama, ou bug: não engolir
            for check in outcome:
                merged.setdefault(check.url, check)
        if merged:
            return list(merged.values())[:MAX_RESULTS]
        if failed:
            raise FactCheckUnavailableError
        return []

    async def _search_one(self, client: httpx.AsyncClient, query: str) -> list[AgencyCheck]:
        params = {
            "query": query,
            "languageCode": "pt",
            "pageSize": str(MAX_RESULTS),
        }
        try:
            # Chave no cabeçalho, não na URL: URLs acabam em logs e traces.
            response = await client.get(
                SEARCH_URL, params=params, headers={"X-Goog-Api-Key": self._api_key}
            )
        except httpx.HTTPError as exc:
            logger.warning("factcheck_unavailable", extra={"error": type(exc).__name__})
            raise FactCheckUnavailableError from exc
        if response.status_code != 200:
            logger.warning("factcheck_unavailable", extra={"status": response.status_code})
            raise FactCheckUnavailableError
        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning("factcheck_unavailable", extra={"error": "json"})
            raise FactCheckUnavailableError from exc
        if not isinstance(payload, dict):  # 200 fora do formato da API não é "nada encontrado"
            logger.warning("factcheck_unavailable", extra={"error": "formato"})
            raise FactCheckUnavailableError
        return parse_claims(payload)


def parse_claims(payload: Any) -> list[AgencyCheck]:
    """Converte a resposta da API, ignorando qualquer item fora do formato esperado."""
    if not isinstance(payload, dict):
        return []
    found: dict[str, AgencyCheck] = {}
    for claim in payload.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        for review in claim.get("claimReview") or []:
            check = _parse_review(review, claim)
            if check and check.url not in found:
                found[check.url] = check
    return list(found.values())[:MAX_RESULTS]


def _parse_review(review: Any, claim: dict[str, Any]) -> AgencyCheck | None:
    if not isinstance(review, dict):
        return None
    url = review.get("url")
    if not isinstance(url, str) or not url.startswith("https://") or len(url) > 1024:
        return None
    publisher = review.get("publisher") if isinstance(review.get("publisher"), dict) else {}
    agencia = _text(publisher.get("name")) or _text(publisher.get("site"))
    titulo = _text(review.get("title")) or _text(claim.get("text"))
    veredito = _text(review.get("textualRating"))
    if not (agencia and titulo and veredito):
        return None
    return AgencyCheck(
        agencia=agencia[:120],
        titulo=titulo[:300],
        veredito=veredito[:120],
        url=url,
        data=_text(review.get("reviewDate"))[:10] or None,
    )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
