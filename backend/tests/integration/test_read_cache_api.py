"""Cache de leitura de /comments (PR cache-leitura) contra Postgres real.

Cada teste aquece o cache com uma leitura ANTES da escrita: a leitura seguinte tem de mostrar a
mudança na hora (invalidação imediata no processo), nunca a lista guardada.
"""

# A fixture `api` vem de test_comments_api (mesmo login simulado e o mesmo app): os parâmetros
# com esse nome são a fixture, não uma redefinição.
# ruff: noqa: F811

import asyncio
from typing import Any

import pytest

from app.repositories.comments import SqlCommentRepository
from app.services.moderation.pipeline import AiVerdict
from tests.integration.test_comments_api import (  # noqa: F401 (fixture api)
    Api,
    _DeletingAdmin,
    admin,
    api,
    link_check,
    member,
    post,
    queue,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def feed(api: Api, **params: str) -> list[dict[str, Any]]:
    response = await api.http.get("/comments", params=params)
    assert response.status_code == 200
    return response.json()["items"]  # type: ignore[no-any-return]


def ids(items: list[dict[str, Any]]) -> list[str]:
    return [c["id"] for c in items]


async def test_author_sees_the_new_comment_at_once(api: Api) -> None:
    author = await member(api)
    api.as_user(author)
    await feed(api)  # cache aquecido

    created = (await post(api, "Comentário novo na Comunidade.")).json()["id"]

    assert created in ids(await feed(api))


async def test_comment_on_a_check_appears_at_once(api: Api) -> None:
    author = await member(api)
    api.as_user(author)
    check_id = await link_check(api, author)
    assert await feed(api, checagem=check_id) == []

    created = (await post(api, "Já foi desmentido.", checagem=check_id)).json()["id"]

    assert ids(await feed(api, checagem=check_id)) == [created]


async def test_deleted_by_the_author_at_once(api: Api) -> None:
    author = await member(api)
    api.as_user(author)
    created = (await post(api, "Vou apagar.")).json()["id"]
    assert created in ids(await feed(api))

    assert (await api.http.delete(f"/comments/{created}")).status_code == 204

    shown = {c["id"]: c for c in await feed(api)}
    assert created not in shown or shown[created]["removido_pelo_autor"] is True


async def test_hidden_by_reports_at_once(api: Api) -> None:
    api.as_user(await member(api))
    created = (await post(api, "Comentário polêmico.")).json()["id"]
    assert created in ids(await feed(api))

    for _ in range(3):
        api.as_user(await member(api))
        assert (await api.http.post(f"/comments/{created}/denuncia", json={})).status_code == 200

    assert created not in ids(await feed(api))


async def test_removed_by_moderation_at_once(api: Api) -> None:
    api.as_user(await member(api))
    api.moderator.answer = AiVerdict("desinformacao", 0.9)  # publica e vai para a fila
    created = (await post(api, "Dá para votar pela internet.")).json()["id"]
    assert created in ids(await feed(api))

    await admin(api)
    item = next(i for i in await queue(api) if i["comentario"] == created)
    response = await api.http.post(f"/admin/moderacao/fila/{item['id']}", json={"acao": "remover"})

    assert response.status_code == 200
    assert created not in ids(await feed(api))


async def test_banned_author_disappears_at_once(api: Api) -> None:
    api.as_user(await member(api))
    api.moderator.answer = AiVerdict("desinformacao", 0.9)
    created = (await post(api, "Urna fraudada, repassem.")).json()["id"]
    assert created in ids(await feed(api))

    await admin(api)
    item = next(i for i in await queue(api) if i["comentario"] == created)
    response = await api.http.post(f"/admin/moderacao/fila/{item['id']}", json={"acao": "banir"})

    assert response.status_code == 200
    assert created not in ids(await feed(api))


async def test_deleted_account_disappears_at_once(api: Api) -> None:
    from app.services.supabase_admin import get_supabase_admin

    author = await member(api)
    api.as_user(author)
    created = (await post(api, "Comentário de quem vai sair.")).json()["id"]
    assert created in ids(await feed(api))

    api.app.dependency_overrides[get_supabase_admin] = lambda: _DeletingAdmin(api)
    assert (await api.http.delete("/me")).status_code == 204

    assert created not in ids(await feed(api))


async def test_concurrent_reads_go_to_the_database_once(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    original = SqlCommentRepository.threads

    async def counting(self: SqlCommentRepository, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(SqlCommentRepository, "threads", counting)
    api.app.state.read_caches.comments.invalidate()

    responses = await asyncio.gather(*(api.http.get("/comments") for _ in range(20)))
    again = await api.http.get("/comments")

    assert all(r.status_code == 200 for r in responses) and again.status_code == 200
    assert len({r.text for r in [*responses, again]}) == 1
    assert calls == 1  # 21 leituras, 1 consulta ao banco


async def test_approved_hidden_comment_reappears_at_once(api: Api) -> None:
    api.as_user(await member(api))
    created = (await post(api, "Comentário denunciado à toa.")).json()["id"]
    for _ in range(3):
        api.as_user(await member(api))
        await api.http.post(f"/comments/{created}/denuncia", json={})
    assert created not in ids(await feed(api))  # oculto (e o cache aquecido sem ele)

    await admin(api)
    item = next(i for i in await queue(api) if i["comentario"] == created)
    response = await api.http.post(f"/admin/moderacao/fila/{item['id']}", json={"acao": "aprovar"})

    assert response.status_code == 200
    assert created in ids(await feed(api))


async def test_shared_read_never_uses_the_request_session(api: Api) -> None:
    """A leitura do cache serve vários pedidos: ela abre a própria sessão. Se usasse a do 1º
    pedido, ela fecharia quando ele desistisse e derrubaria os outros."""
    from app.db import get_session

    async def no_request_session() -> Any:
        raise AssertionError("a leitura do cache usou a sessão da requisição")
        yield  # pragma: no cover

    api.app.dependency_overrides[get_session] = no_request_session
    api.app.state.read_caches.comments.invalidate()
    try:
        response = await api.http.get("/comments")
    finally:
        del api.app.dependency_overrides[get_session]

    assert response.status_code == 200
