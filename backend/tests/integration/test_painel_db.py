"""GET /admin/painel (plano mapa-e-painel-admin §B) contra Postgres real: só o admin, só números
agregados por dia (nada por pessoa), tudo numa transação de um comando só.

O banco de teste é compartilhado entre os testes: cada teste mede a diferença (antes/depois).
"""

# A fixture `api` vem de test_comments_api (mesmo login simulado e o mesmo app): os parâmetros
# com esse nome são a fixture, não uma redefinição.
# ruff: noqa: F811

from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from app.repositories.checks import NewCheck, SqlCheckRepository, Started
from app.repositories.painel import BOA_VISTA, SqlPainelRepository
from tests.integration.conftest import create_auth_user
from tests.integration.test_checks_db import LIMITS, VERSION, link
from tests.integration.test_comments_api import (  # noqa: F401 (fixture api)
    ADMIN_EMAIL,
    Api,
    apelido,
    api,
    member,
    post,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def panel(api: Api) -> dict[str, Any]:
    response = await api.http.get("/admin/painel")
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def today(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return rows[-1]


async def as_admin(api: Api) -> UUID:
    admin = await member(api)
    api.as_user(admin, ADMIN_EMAIL)
    return admin


async def check_with_tokens(
    api: Api, user_id: UUID, tokens: dict[str, Any] | None, *, version: str = VERSION
) -> None:
    repo = SqlCheckRepository(api.session)
    started = await repo.begin_check(user_id, link(), LIMITS)
    assert isinstance(started, Started)
    result: dict[str, Any] = {"resumo": "A votação não foi adiada."}
    if tokens is not None:
        result["tokens"] = tokens
    await repo.save_result(
        NewCheck(
            prompt_version=version,
            subject=started.subject,
            user_id=user_id,
            veredito="enganoso",
            confianca=80,
            result=result,
            model="gemini-teste",
            listed=True,
            title="Título",
            published_at=date(2026, 10, 1),
            claim_token=started.claim_token,
        )
    )


async def run(api: Api, sql: str, **params: object) -> None:
    async with api.session.begin():
        await api.session.execute(text(sql), params)


# ---------- acesso ----------


async def test_anonymous_gets_401(api: Api) -> None:
    assert (await api.http.get("/admin/painel")).status_code == 401


async def test_regular_user_gets_403(api: Api) -> None:
    api.as_user(await member(api), "pessoa@exemplo.com")

    response = await api.http.get("/admin/painel")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_admin_gets_the_panel_without_cache(api: Api) -> None:
    await as_admin(api)

    response = await api.http.get("/admin/painel")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert len(body["checagens"]) == 7  # 7 dias, do mais antigo para hoje
    days = [row["dia"] for row in body["checagens"]]
    assert days == sorted(days) and len(set(days)) == 7


# ---------- métricas ----------


async def test_checks_by_verdict_and_tokens_of_today(api: Api) -> None:
    admin = await as_admin(api)
    before = await panel(api)

    await check_with_tokens(api, admin, {"in": 2100, "out": 380, "thoughts": 7})
    await check_with_tokens(api, admin, None, version="agencias")  # sem IA: sem tokens
    await run(
        api,
        "insert into public.check_requests (user_id, cache_hit) values (:u, true), (:u, false)",
        u=admin,
    )
    after = await panel(api)

    old, new = today(before["checagens"]), today(after["checagens"])
    assert new["total"] - old["total"] == 2
    assert new["enganoso"] - old["enganoso"] == 2
    assert new["so_agencias"] - old["so_agencias"] == 1
    old_p, new_p = today(before["pedidos"]), today(after["pedidos"])
    # begin_check também grava um pedido por checagem (2) + os 2 inseridos aqui.
    assert new_p["total"] - old_p["total"] == 4
    assert new_p["do_cache"] - old_p["do_cache"] == 1
    old_t, new_t = today(before["tokens"]), today(after["tokens"])
    assert (new_t["entrada"] - old_t["entrada"], new_t["saida"] - old_t["saida"]) == (2100, 380)
    assert new_t["raciocinio"] - old_t["raciocinio"] == 7
    assert new_t["checagens"] - old_t["checagens"] == 1  # só as que têm tokens


async def test_malformed_tokens_do_not_break_the_panel(api: Api) -> None:
    admin = await as_admin(api)
    before = await panel(api)

    await check_with_tokens(api, admin, {"in": "muitos", "out": None})
    after = await panel(api)

    assert today(after["tokens"])["entrada"] == today(before["tokens"])["entrada"]


async def test_comments_reports_queue_and_users(api: Api) -> None:
    await as_admin(api)
    before = await panel(api)

    author = await member(api)
    api.as_user(author)
    created = await post(api, "Alguém sabe o horário de votação em Boa Vista?")
    assert created.status_code == 201
    comment_id = await _comment_db_id(api, created.json()["id"])
    reporter = await member(api)
    await run(
        api,
        "insert into public.comment_reports (comment_id, reporter_id) values (:c, :r)",
        c=comment_id,
        r=reporter,
    )
    await run(
        api,
        "insert into public.moderation_queue (kind, reason, comment_id) "
        "values ('comment_review', 'hidden_by_reports', :c)",
        c=comment_id,
    )
    await run(
        api, "update public.comments set status = 'hidden_reports' where id = :c", c=comment_id
    )
    api.as_user(author)
    deleted = await post(api, "Vou apagar este comentário.")
    await run(
        api,
        "update public.comments set status = 'removed_by_author' where public_id = :p",
        p=deleted.json()["id"],
    )
    await member(api, status="suspended")
    await member(api, status="banned")
    await as_admin(api)
    after = await panel(api)

    assert today(after["comentarios"])["total"] - today(before["comentarios"])["total"] == 2
    # Escondido por denúncias conta; apagado pelo próprio autor não.
    assert today(after["comentarios"])["ocultos"] - today(before["comentarios"])["ocultos"] == 1
    assert today(after["denuncias"])["total"] - today(before["denuncias"])["total"] == 1
    assert after["fila"]["pendentes"] - before["fila"]["pendentes"] == 1
    assert after["fila"]["revisao"] - before["fila"]["revisao"] == 1
    # Autor, denunciante, suspenso, banido e o 2º admin.
    assert after["usuarios"]["total"] - before["usuarios"]["total"] == 5
    assert after["usuarios"]["suspensos"] - before["usuarios"]["suspensos"] == 1
    assert after["usuarios"]["banidos"] - before["usuarios"]["banidos"] == 1


async def test_new_users_are_counted_on_their_day(api: Api) -> None:
    await as_admin(api)
    before = await panel(api)

    await member(api, days_old=0)
    after = await panel(api)

    assert today(after["usuarios_novos"])["total"] - today(before["usuarios_novos"])["total"] == 1


async def test_gemini_usage_of_the_pacific_day_with_remaining_quota(api: Api) -> None:
    await as_admin(api)
    before = await panel(api)
    await run(
        api,
        "insert into public.gemini_usage (day, purpose, calls) "
        "values ((now() at time zone 'America/Los_Angeles')::date, 'checagem', 3) "
        "on conflict (day, purpose) do update set calls = gemini_usage.calls + 3",
    )
    await run(
        api,
        "insert into public.gemini_model_usage (day, model, calls) "
        "values ((now() at time zone 'America/Los_Angeles')::date, 'gemini-3.1-flash-lite', 2) "
        "on conflict (day, model) do update set calls = gemini_model_usage.calls + 2",
    )
    after = await panel(api)

    def quota(body: dict[str, Any], uso: str) -> dict[str, Any]:
        return next(q for q in body["gemini"]["cota"] if q["uso"] == uso)

    old, new = quota(before, "checagem"), quota(after, "checagem")
    assert new["usadas"] - old["usadas"] == 3
    assert new["restante"] == max(new["limite"] - new["usadas"], 0)

    def model(body: dict[str, Any]) -> dict[str, Any]:
        return next(m for m in body["gemini"]["modelos"] if m["modelo"] == "gemini-3.1-flash-lite")

    assert model(after)["usadas"] - model(before)["usadas"] == 2
    assert model(after)["restante"] == max(model(after)["limite"] - model(after)["usadas"], 0)


async def test_day_boundaries_in_boa_vista_and_the_seven_day_edge(api: Api) -> None:
    """Meia-noite de Boa Vista separa os dias; o 8º dia para trás fica de fora."""
    await as_admin(api)
    before = await panel(api)
    today_bv = datetime.now(BOA_VISTA).date()

    def at(days_ago: int, hour: int, minute: int, second: int) -> datetime:
        return datetime.combine(
            today_bv - timedelta(days=days_ago), time(hour, minute, second), BOA_VISTA
        )

    moments = [
        at(1, 0, 0, 0),  # início de ontem
        at(1, 23, 59, 59),  # fim de ontem
        at(0, 0, 0, 0),  # início de hoje
        at(6, 0, 0, 0),  # primeiro instante da janela de 7 dias
        at(7, 23, 59, 59),  # último instante fora da janela
    ]
    for moment in moments:
        user_id = await create_auth_user(api.session)
        await run(
            api,
            "insert into public.profiles (id, apelido, terms_version, created_at) "
            "values (:id, :apelido, 'teste', :at)",
            id=user_id,
            apelido=f"b_{user_id.hex[:12]}",
            at=moment,
        )
    after = await panel(api)

    deltas = [
        a["total"] - b["total"]
        for a, b in zip(after["usuarios_novos"], before["usuarios_novos"], strict=True)
    ]
    assert after["usuarios_novos"][0]["dia"] == (today_bv - timedelta(days=6)).isoformat()
    assert deltas == [1, 0, 0, 0, 0, 2, 1]


# ---------- privacidade e custo ----------


async def test_nothing_about_any_person(api: Api) -> None:
    admin = await as_admin(api)
    author = await member(api, days_old=0)
    api.as_user(author)
    await post(api, "Comentário de alguém com apelido.")
    api.as_user(admin, ADMIN_EMAIL)

    raw = (await api.http.get("/admin/painel")).text

    for secret in (apelido(admin), apelido(author), str(admin), str(author), ADMIN_EMAIL, "@"):
        assert secret not in raw


async def test_one_transaction_with_a_single_statement(api: Api) -> None:
    calls: list[str] = []
    original = api.session.execute

    async def counting(statement: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(str(statement))
        return await original(statement, *args, **kwargs)

    api.session.execute = counting  # type: ignore[method-assign]
    await SqlPainelRepository(api.session).read()

    assert len(calls) == 1  # ~0,7 s por transação entre Virgínia e São Paulo


async def _comment_db_id(api: Api, public_id: str) -> int:
    async with api.session.begin():
        value = await api.session.scalar(
            text("select id from public.comments where public_id = :p"), {"p": public_id}
        )
    assert value is not None
    return int(value)
