"""Cliente HTTP da Google Fact Check Tools API (sem rede: httpx.MockTransport).

Falha da API (rede, 403 de chave, 429 de cota, 5xx, JSON quebrado) não pode virar lista
vazia: "não encontramos" e "a busca falhou" são mensagens diferentes na página pública.
As consultas (até 3) rodam em paralelo; as respostas simuladas são por consulta.
"""

import asyncio
from collections.abc import Callable

import httpx
import pytest

from app.services.factcheck import MAX_RESULTS, FactCheckUnavailableError, HttpFactCheckClient

pytestmark = pytest.mark.anyio

Reply = Callable[[httpx.Request], httpx.Response]


def claim(n: int, agency: str = "Lupa") -> dict[str, object]:
    return {
        "text": f"Alegação {n}",
        "claimReview": [
            {
                "publisher": {"name": agency},
                "url": f"https://{agency.lower().replace(' ', '')}.com.br/checagem/{n}",
                "title": f"Checagem {n}",
                "textualRating": "Falso",
                "reviewDate": "2026-10-20T10:00:00Z",
            }
        ],
    }


def found(*numbers: int, agency: str = "Lupa") -> Reply:
    return lambda _: httpx.Response(200, json={"claims": [claim(n, agency) for n in numbers]})


def empty(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={})


def status(code: int) -> Reply:
    return lambda _: httpx.Response(code, json={"error": {"code": code}})


def network_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("sem rede", request=request)


def broken_json(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"{nao e json")


def not_an_object(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=["fora", "do", "formato"])


def client(by_query: dict[str, Reply], seen: list[str] | None = None) -> HttpFactCheckClient:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if seen is not None:
            seen.append(query)
        return by_query[query](request)

    return HttpFactCheckClient("chave-de-teste", transport=httpx.MockTransport(handler))


async def test_found_checks_are_returned() -> None:
    result = await client({"a": found(1)}).search(["a"])
    assert [c.url for c in result] == ["https://lupa.com.br/checagem/1"]


async def test_search_that_worked_and_found_nothing_is_an_empty_list() -> None:
    assert await client({"a": empty, "b": empty, "c": empty}).search(["a", "b", "c"]) == []


@pytest.mark.parametrize(
    "failure",
    [status(403), status(429), status(500), status(503), network_error, broken_json, not_an_object],
    ids=["403-chave", "429-cota", "500", "503", "rede", "json", "formato"],
)
async def test_api_failure_is_an_error_not_an_empty_list(failure: Reply) -> None:
    with pytest.raises(FactCheckUnavailableError):
        await client({"a": failure, "b": failure}).search(["a", "b"])


async def test_results_from_one_query_win_over_the_failure_of_another() -> None:
    result = await client({"a": status(503), "b": found(1), "c": network_error}).search(
        ["a", "b", "c"]
    )
    assert len(result) == 1


@pytest.mark.parametrize("failed", ["a", "b", "c"])
async def test_incomplete_search_without_results_is_a_failure(failed: str) -> None:
    # Uma das consultas não chegou a responder: não dá para dizer "não encontramos".
    replies: dict[str, Reply] = {"a": empty, "b": empty, "c": empty}
    replies[failed] = status(503)
    with pytest.raises(FactCheckUnavailableError):
        await client(replies).search(["a", "b", "c"])


async def test_results_are_merged_in_query_order_without_repeated_links() -> None:
    result = await client({"a": found(1, 2), "b": found(2, 3), "c": found(4, 1)}).search(
        ["a", "b", "c"]
    )
    assert [c.url.rsplit("/", 1)[1] for c in result] == ["1", "2", "3", "4"]


async def test_at_most_ten_checks_in_total() -> None:
    result = await client(
        {"a": found(*range(0, 8)), "b": found(*range(8, 16)), "c": found(*range(16, 24))}
    ).search(["a", "b", "c"])
    assert len(result) == MAX_RESULTS
    assert [c.url.rsplit("/", 1)[1] for c in result] == [str(n) for n in range(10)]


async def test_at_most_three_queries_are_sent() -> None:
    seen: list[str] = []
    replies = {q: empty for q in "abcde"}
    await client(replies, seen).search(list("abcde"))
    assert sorted(seen) == ["a", "b", "c"]


async def test_queries_run_in_parallel() -> None:
    # Cada resposta só sai quando as 3 consultas já chegaram: em série, travaria.
    arrived: list[str] = []
    all_in = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        arrived.append(request.url.params["query"])
        if len(arrived) == 3:
            all_in.set()
        await asyncio.wait_for(all_in.wait(), timeout=2)
        return httpx.Response(200, json={})

    fc = HttpFactCheckClient("chave-de-teste", transport=httpx.MockTransport(handler))
    assert await fc.search(["a", "b", "c"]) == []
    assert sorted(arrived) == ["a", "b", "c"]


async def test_timeout_of_the_caller_is_not_swallowed() -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    fc = HttpFactCheckClient("chave-de-teste", transport=httpx.MockTransport(slow))
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await fc.search(["a", "b"])


async def test_no_query_at_all_is_not_a_search_that_found_nothing() -> None:
    with pytest.raises(FactCheckUnavailableError):
        await client({}).search([])


async def test_without_an_api_key_the_search_is_recorded_as_not_done() -> None:
    from app.routers.checks import _NoFactCheck

    with pytest.raises(FactCheckUnavailableError):
        await _NoFactCheck().search(["a"])


async def test_the_api_key_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(FactCheckUnavailableError):
        await client({"a": status(429), "b": status(429)}).search(["a", "b"])
    assert "chave-de-teste" not in caplog.text
