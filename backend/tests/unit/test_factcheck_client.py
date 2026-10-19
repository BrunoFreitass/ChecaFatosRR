"""Cliente HTTP da Google Fact Check Tools API (sem rede: httpx.MockTransport).

Falha da API (rede, 403 de chave, 429 de cota, 5xx, JSON quebrado) não pode virar lista
vazia: "não encontramos" e "a busca falhou" são mensagens diferentes na página pública.
"""

from collections.abc import Callable

import httpx
import pytest

from app.services.factcheck import FactCheckUnavailableError, HttpFactCheckClient

pytestmark = pytest.mark.anyio

CLAIM = {
    "claims": [
        {
            "text": "A votação foi adiada",
            "claimReview": [
                {
                    "publisher": {"name": "Lupa", "site": "lupa.uol.com.br"},
                    "url": "https://lupa.uol.com.br/jornalismo/2026/10/20/votacao",
                    "title": "É falso que a votação foi adiada",
                    "textualRating": "Falso",
                    "reviewDate": "2026-10-20T10:00:00Z",
                }
            ],
        }
    ]
}


def client(responses: list[Callable[[httpx.Request], httpx.Response]]) -> HttpFactCheckClient:
    calls = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return next(calls)(request)

    return HttpFactCheckClient("chave-de-teste", transport=httpx.MockTransport(handler))


def ok(body: object) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(200, json=body)


def status(code: int) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(code, json={"error": {"code": code}})


def network_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("sem rede", request=request)


def broken_json(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"{nao e json")


async def test_found_checks_are_returned() -> None:
    found = await client([ok(CLAIM)]).search(["votação adiada"])
    assert [c.agencia for c in found] == ["Lupa"]


async def test_search_that_worked_and_found_nothing_is_an_empty_list() -> None:
    assert await client([ok({}), ok({"claims": []})]).search(["a", "b"]) == []


@pytest.mark.parametrize(
    "failure",
    [status(403), status(429), status(500), status(503), network_error, broken_json],
    ids=["403-chave", "429-cota", "500", "503", "rede", "json"],
)
async def test_api_failure_is_an_error_not_an_empty_list(
    failure: Callable[[httpx.Request], httpx.Response],
) -> None:
    with pytest.raises(FactCheckUnavailableError):
        await client([failure, failure]).search(["a", "b"])


async def test_second_query_finds_after_the_first_failed() -> None:
    found = await client([status(503), ok(CLAIM)]).search(["a", "b"])
    assert len(found) == 1


@pytest.mark.parametrize("order", ["falha-depois-vazio", "vazio-depois-falha"])
async def test_incomplete_search_without_results_is_a_failure(order: str) -> None:
    # Uma das consultas não chegou a ser feita: não dá para dizer "não encontramos".
    steps = [status(503), ok({})] if order == "falha-depois-vazio" else [ok({}), status(503)]
    with pytest.raises(FactCheckUnavailableError):
        await client(steps).search(["a", "b"])
