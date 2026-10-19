"""Checagens já publicadas por agências (Google Fact Check Tools API, claims:search).

Falha aqui nunca derruba a checagem: sem resposta, o Gemini analisa só o conteúdo e tende a
`nao_verificavel`. A chave vai no cabeçalho X-Goog-Api-Key (nunca na URL).
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

SEARCH_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
MAX_RESULTS = 10
_QUERY_WORDS = 20
_MIN_SENTENCE_WORDS = 8

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
    """Até duas buscas: a frase principal (ou o título) e, se ela não achar nada, as
    palavras mais longas do texto (nomes próprios e termos específicos)."""
    queries: list[str] = []
    main = _first_sentence(title) if title else None
    main = main or _first_sentence(text)
    if main:
        queries.append(main)
    words = _words(_URL.sub(" ", text))
    longest = sorted(dict.fromkeys(w for w in words if len(w) >= 6), key=len, reverse=True)[:8]
    if len(longest) >= 3:
        fallback = " ".join(longest)
        if fallback != main:
            queries.append(fallback)
    return queries


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
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport, trust_env=False
        ) as client:
            failed = False
            for query in queries[:2]:
                try:
                    results = await self._search_one(client, query)
                except FactCheckUnavailableError:
                    failed = True  # tenta a outra consulta; achou algo, vale
                    continue
                if results:
                    return results
        # Nada encontrado: só é "não encontramos" se TODAS as consultas responderam. Com
        # alguma falha, a busca não foi feita por inteiro, e a página diz que ela falhou.
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
