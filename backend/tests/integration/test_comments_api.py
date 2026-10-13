"""API de comentários, denúncias e fila do admin (etapas 3+4, marco 2) contra Postgres real.

Só o login e a IA de moderação são simulados; o SQL, as regras e os limites são os de verdade.
Regras: plano §9 + decisões de 12/10/2026 (ofensivo/spam sem strike e cooldown; desinformação
publica e vai à fila; Gemini fora = fail-open; denúncia só de conta com mais de 24 h; o autor
apaga o próprio comentário; suspensão sobrevive à exclusão da conta).
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, current_user
from app.config import get_settings
from app.errors import AppError
from app.main import create_app
from app.repositories.checks import NewCheck, SqlCheckRepository, Started
from app.services.bans import ban_hash
from app.services.moderation.pipeline import AiUnavailable, AiVerdict
from tests.integration.conftest import create_auth_user
from tests.integration.test_checks_db import LIMITS, VERSION, link, message

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ADMIN_EMAIL = "admin@exemplo.com"
SALT = "sal-de-teste-0123456789"  # pragma: allowlist secret


@dataclass
class FakeModerator:
    answer: Any = field(default_factory=lambda: AiVerdict("ok", 0.97))
    calls: list[str] = field(default_factory=list)

    async def classify(self, body: str, reserve: Any, model_budget: Any = None) -> Any:
        self.calls.append(body)
        if not await reserve():
            return AiUnavailable("sem_cota")
        return self.answer


@dataclass
class Who:
    user: CurrentUser | None = None


@dataclass
class Api:
    app: Any
    http: httpx.AsyncClient
    who: Who
    moderator: FakeModerator
    session: AsyncSession

    def as_user(self, user_id: UUID, email: str | None = None) -> None:
        self.who.user = CurrentUser(id=user_id, email=email, provider="google")


@pytest.fixture
async def api(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    monkeypatch.setenv("ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("BAN_HASH_SALT", SALT)
    monkeypatch.setenv("COMMENT_MIN_INTERVAL_SECONDS", "0")  # cada teste liga o seu
    get_settings.cache_clear()
    from app.routers import comments as comments_router

    app = create_app()
    who, moderator = Who(), FakeModerator()

    def logged() -> CurrentUser:
        if who.user is None:
            raise AppError(401, "unauthorized", "Você precisa entrar com sua conta Google.")
        return who.user

    app.dependency_overrides[current_user] = logged
    app.dependency_overrides[comments_router.get_moderator] = lambda: moderator
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield Api(app, http, who, moderator, session)
    get_settings.cache_clear()


async def member(api: Api, *, days_old: int = 3, status: str = "active") -> UUID:
    """Usuário com perfil (e identidade Google), criado há `days_old` dias."""
    user_id = await create_auth_user(api.session, google_sub=f"google-{uuid4().hex}")
    async with api.session.begin():
        await api.session.execute(
            text(
                "insert into public.profiles (id, apelido, terms_version, status, "
                "suspended_until, created_at) values (:id, :apelido, 'teste', :status, "
                "case when :status = 'suspended' then now() + interval '1 day' end, "
                "now() - make_interval(days => :days))"
            ),
            {"id": user_id, "apelido": f"u_{user_id.hex[:12]}", "status": status, "days": days_old},
        )
    return user_id


def apelido(user_id: UUID) -> str:
    return f"u_{user_id.hex[:12]}"


async def link_check(api: Api, user_id: UUID, *, text_check: bool = False) -> str:
    repo = SqlCheckRepository(api.session)
    started = await repo.begin_check(user_id, message() if text_check else link(), LIMITS)
    assert isinstance(started, Started)
    saved = await repo.save_result(
        NewCheck(
            prompt_version=VERSION,
            subject=started.subject,
            user_id=user_id,
            veredito="falso",
            confianca=90,
            result={"resumo": "A votação não foi adiada."},
            model="gemini-teste",
            listed=not text_check,
            title=None if text_check else "Título",
            published_at=None if text_check else date(2026, 10, 1),
            claim_token=started.claim_token,
        )
    )
    return saved.public_id


async def scalar(api: Api, sql: str, **params: object) -> Any:
    async with api.session.begin():
        return await api.session.scalar(text(sql), params)


async def post(api: Api, texto: str, **extra: str) -> httpx.Response:
    return await api.http.post("/comments", json={"texto": texto, **extra})


def code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


# ---------- publicar e listar ----------


async def test_comment_on_the_feed_and_on_a_link_check(api: Api) -> None:
    author = await member(api)
    api.as_user(author)
    check_id = await link_check(api, author)

    feed = await post(api, "Alguém sabe o horário de votação em Boa Vista?")
    on_check = await post(api, "A Justiça Eleitoral já desmentiu isso.", checagem=check_id)

    assert feed.status_code == 201 and on_check.status_code == 201
    assert feed.json()["texto"].startswith("Alguém sabe")
    assert feed.json()["apelido"].startswith("u_")
    feed_ids = [c["id"] for c in (await api.http.get("/comments")).json()["items"]]
    check_ids = [
        c["id"]
        for c in (await api.http.get("/comments", params={"checagem": check_id})).json()["items"]
    ]
    assert feed.json()["id"] in feed_ids and on_check.json()["id"] not in feed_ids
    assert check_ids == [on_check.json()["id"]]


async def test_listing_is_public(api: Api) -> None:
    api.as_user(await member(api))
    await post(api, "Comentário público.")
    api.who.user = None
    response = await api.http.get("/comments")
    assert response.status_code == 200 and response.json()["items"]


async def test_no_comments_on_pasted_text_checks(api: Api) -> None:
    author = await member(api)
    api.as_user(author)
    text_check = await link_check(api, author, text_check=True)

    response = await post(api, "Comentando um texto colado.", checagem=text_check)

    assert response.status_code == 422 and code(response) == "comentario_nao_permitido"


async def test_reply_one_level_only(api: Api) -> None:
    api.as_user(await member(api))
    root = (await post(api, "Comentário raiz.")).json()["id"]
    reply = await post(api, "Uma resposta.", resposta_a=root)
    assert reply.status_code == 201

    listed = (await api.http.get("/comments")).json()["items"]
    entry = next(c for c in listed if c["id"] == root)
    assert [r["id"] for r in entry["respostas"]] == [reply.json()["id"]]

    nested = await post(api, "Resposta da resposta.", resposta_a=reply.json()["id"])
    assert nested.status_code == 422 and code(nested) == "resposta_invalida"


async def test_text_limits(api: Api) -> None:
    api.as_user(await member(api))
    assert (await post(api, "   ")).status_code == 422
    assert (await post(api, "x" * 1001)).status_code == 422


# ---------- moderação ----------


async def test_local_filter_block_is_a_strike_and_skips_the_ai(api: Api) -> None:
    author = await member(api)
    api.as_user(author)

    response = await post(api, "que porra de eleição")

    assert response.status_code == 422 and code(response) == "violacao"
    body = response.json()["error"]
    assert body["regra"] and body["explicacao"] and body["strikes"] == 1
    assert api.moderator.calls == []
    count = await scalar(api, "select count(*) from public.comments where user_id = :u", u=author)
    assert count == 0


async def test_ai_strike_with_high_confidence(api: Api) -> None:
    api.as_user(await member(api))
    api.moderator.answer = AiVerdict("incitacao_violencia", 0.95)

    response = await post(api, "Vamos resolver isso na porrada amanhã.")

    assert code(response) == "violacao" and response.json()["error"]["strikes"] == 1


async def test_ai_low_confidence_blocks_and_asks_the_admin(api: Api) -> None:
    author = await member(api)
    api.as_user(author)
    api.moderator.answer = AiVerdict("crime", 0.6)

    response = await post(api, "Texto ambíguo.")

    assert code(response) == "violacao" and response.json()["error"]["strikes"] == 0
    kind = await scalar(
        api, "select kind from public.moderation_queue where user_id = :u", u=author
    )
    assert kind == "strike_confirmation"


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (AiVerdict("desinformacao", 0.9), "desinformacao"),
        (AiUnavailable("indisponivel"), "nao_moderado_ia"),
    ],
)
async def test_published_but_queued(api: Api, answer: Any, reason: str) -> None:
    api.as_user(await member(api))
    api.moderator.answer = answer

    response = await post(api, "Dizem que dá para votar pela internet.")

    assert response.status_code == 201
    queued = await scalar(
        api,
        "select reason from public.moderation_queue q join public.comments c "
        "on c.id = q.comment_id where c.public_id = :p",
        p=response.json()["id"],
    )
    assert queued == reason


async def test_offensive_blocks_pause_comments_for_an_hour(api: Api) -> None:
    api.as_user(await member(api))
    api.moderator.answer = AiVerdict("ofensivo", 0.9)
    for _ in range(5):
        blocked = await post(api, "Você é um idiota.")
        assert code(blocked) == "violacao" and blocked.json()["error"]["strikes"] == 0

    api.moderator.answer = AiVerdict("ok", 0.99)
    paused = await post(api, "Agora um comentário educado.")

    assert paused.status_code == 429 and code(paused) == "comentarios_pausados"
    assert 0 < int(paused.headers["Retry-After"]) <= 3600


async def test_suspended_user_cannot_comment(api: Api) -> None:
    api.as_user(await member(api, status="suspended"))
    response = await post(api, "Oi.")
    assert response.status_code == 403 and code(response) == "conta_suspensa"


# ---------- limites ----------


async def test_one_comment_every_thirty_seconds(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMMENT_MIN_INTERVAL_SECONDS", "30")
    get_settings.cache_clear()
    api.as_user(await member(api))

    assert (await post(api, "Primeiro.")).status_code == 201
    second = await post(api, "Segundo, logo em seguida.")

    assert second.status_code == 429 and code(second) == "limite_comentarios"
    assert 0 < int(second.headers["Retry-After"]) <= 30


async def test_daily_comment_limit(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMMENTS_DAILY_LIMIT", "2")
    get_settings.cache_clear()
    api.as_user(await member(api))

    assert (await post(api, "Um.")).status_code == 201
    assert (await post(api, "Dois.")).status_code == 201
    third = await post(api, "Três.")

    assert third.status_code == 429 and code(third) == "limite_diario_comentarios"


async def test_daily_ai_moderation_limit_per_user(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODERATION_AI_DAILY_PER_USER", "1")
    get_settings.cache_clear()
    api.as_user(await member(api))

    assert (await post(api, "Um.")).status_code == 201
    second = await post(api, "Dois.")

    assert second.status_code == 429 and code(second) == "limite_moderacao"
    assert len(api.moderator.calls) == 1  # o segundo nem chegou à IA


# ---------- denúncias ----------


async def test_three_reports_hide_the_comment_and_queue_it(api: Api) -> None:
    api.as_user(await member(api))
    comment_id = (await post(api, "Comentário polêmico.")).json()["id"]

    for _ in range(3):
        api.as_user(await member(api))
        response = await api.http.post(f"/comments/{comment_id}/denuncia", json={})
        assert response.status_code == 200

    listed = [c["id"] for c in (await api.http.get("/comments")).json()["items"]]
    assert comment_id not in listed
    reason = await scalar(
        api,
        "select reason from public.moderation_queue q join public.comments c "
        "on c.id = q.comment_id where c.public_id = :p",
        p=comment_id,
    )
    assert reason == "hidden_by_reports"


async def test_report_rules(api: Api) -> None:
    api.as_user(await member(api))
    comment_id = (await post(api, "Comentário.")).json()["id"]

    own = await api.http.post(f"/comments/{comment_id}/denuncia", json={})
    assert own.status_code == 422 and code(own) == "denuncia_propria"

    api.as_user(await member(api, days_old=0))
    new_account = await api.http.post(f"/comments/{comment_id}/denuncia", json={})
    assert new_account.status_code == 403 and code(new_account) == "conta_nova"

    api.as_user(await member(api))
    first = await api.http.post(f"/comments/{comment_id}/denuncia", json={})
    again = await api.http.post(f"/comments/{comment_id}/denuncia", json={})
    assert first.json()["status"] == "recebida" and again.json()["status"] == "ja_recebida"
    count = await scalar(
        api, "select report_count from public.comments where public_id = :p", p=comment_id
    )
    assert count == 1

    missing = await api.http.post("/comments/0123456789ab/denuncia", json={})
    assert missing.status_code == 404


# ---------- apagar o próprio ----------


async def test_author_deletes_own_comment_and_replies_stay(api: Api) -> None:
    author = await member(api)
    api.as_user(author)
    root = (await post(api, "Comentário que vou apagar.")).json()["id"]
    api.as_user(await member(api))
    await post(api, "Uma resposta.", resposta_a=root)
    await api.http.post(f"/comments/{root}/denuncia", json={})  # tinha denúncia

    forbidden = await api.http.delete(f"/comments/{root}")
    assert forbidden.status_code == 404  # não é dele

    api.as_user(author)
    assert (await api.http.delete(f"/comments/{root}")).status_code == 204

    entry = next(c for c in (await api.http.get("/comments")).json()["items"] if c["id"] == root)
    assert entry["texto"] is None and entry["removido_pelo_autor"] is True
    assert len(entry["respostas"]) == 1
    snapshot = await scalar(
        api,
        "select content_snapshot from public.moderation_log where user_id = :u "
        "and action = 'deleted_by_author'",
        u=author,
    )
    assert snapshot == "Comentário que vou apagar."  # tinha denúncia: o texto fica no log


# ---------- fila do admin ----------


async def admin(api: Api) -> None:
    api.as_user(await member(api), email=ADMIN_EMAIL)


async def queue(api: Api) -> list[dict[str, Any]]:
    response = await api.http.get("/admin/moderacao/fila")
    assert response.status_code == 200
    return response.json()["items"]


async def test_only_the_admin_sees_the_queue(api: Api) -> None:
    api.as_user(await member(api))
    assert (await api.http.get("/admin/moderacao/fila")).status_code == 403


async def test_admin_approves_a_hidden_comment(api: Api) -> None:
    api.as_user(await member(api))
    comment_id = (await post(api, "Comentário escondido por denúncias.")).json()["id"]
    for _ in range(3):
        api.as_user(await member(api))
        await api.http.post(f"/comments/{comment_id}/denuncia", json={})

    await admin(api)
    item = next(i for i in await queue(api) if i["comentario"] == comment_id)
    assert item["motivo"] == "hidden_by_reports" and item["texto"]
    response = await api.http.post(f"/admin/moderacao/fila/{item['id']}", json={"acao": "aprovar"})

    assert response.status_code == 200
    assert comment_id in [c["id"] for c in (await api.http.get("/comments")).json()["items"]]
    assert all(i["id"] != item["id"] for i in await queue(api))


async def test_admin_removes_and_bans(api: Api) -> None:
    author = await member(api)
    api.as_user(author)
    api.moderator.answer = AiVerdict("desinformacao", 0.9)
    comment_id = (await post(api, "Dá para votar pela internet.")).json()["id"]

    await admin(api)
    item = next(i for i in await queue(api) if i["comentario"] == comment_id)
    response = await api.http.post(f"/admin/moderacao/fila/{item['id']}", json={"acao": "banir"})

    assert response.status_code == 200
    status = await scalar(api, "select status from public.profiles where id = :u", u=author)
    assert status == "banned"
    removed = await scalar(
        api, "select status from public.comments where public_id = :p", p=comment_id
    )
    assert removed == "removed"
    sub = await scalar(api, "select provider_id from auth.identities where user_id = :u", u=author)
    banned = await scalar(
        api,
        "select count(*) from public.banned_identities where sub_hash = :h",
        h=ban_hash(sub, salt=SALT),
    )
    assert banned == 1


async def test_admin_publishes_a_blocked_comment_from_the_log(api: Api) -> None:
    author = await member(api)
    api.as_user(author)
    check_id = await link_check(api, author)
    api.moderator.answer = AiVerdict("crime", 0.5)  # bloqueado, espera o admin
    blocked = await post(api, "Crítica dura, mas legítima.", checagem=check_id)
    assert code(blocked) == "violacao"

    await admin(api)
    item = next(
        i
        for i in await queue(api)
        if i["tipo"] == "strike_confirmation" and i["apelido"] == apelido(author)
    )
    assert item["texto"] == "Crítica dura, mas legítima."
    response = await api.http.post(f"/admin/moderacao/fila/{item['id']}", json={"acao": "aprovar"})

    assert response.status_code == 200
    listed = (await api.http.get("/comments", params={"checagem": check_id})).json()["items"]
    assert [c["texto"] for c in listed] == ["Crítica dura, mas legítima."]


async def test_admin_confirms_the_strike(api: Api) -> None:
    author = await member(api)
    api.as_user(author)
    api.moderator.answer = AiVerdict("crime", 0.5)
    await post(api, "Texto ambíguo.")

    await admin(api)
    item = next(
        i
        for i in await queue(api)
        if i["tipo"] == "strike_confirmation" and i["apelido"] == apelido(author)
    )
    response = await api.http.post(
        f"/admin/moderacao/fila/{item['id']}", json={"acao": "confirmar_strike"}
    )

    assert response.status_code == 200
    strikes = await scalar(api, "select count(*) from public.strikes where user_id = :u", u=author)
    assert strikes == 1


async def test_unknown_queue_item(api: Api) -> None:
    await admin(api)
    response = await api.http.post("/admin/moderacao/fila/999999999", json={"acao": "aprovar"})
    assert response.status_code == 404


# ---------- exclusão de conta suspensa ----------


async def test_suspension_survives_account_deletion(api: Api) -> None:
    from app.legal import CURRENT_TERMS_VERSION
    from app.services.supabase_admin import get_supabase_admin

    sub = f"google-{uuid4().hex}"
    user_id = await create_auth_user(api.session, google_sub=sub)
    async with api.session.begin():
        await api.session.execute(
            text(
                "insert into public.profiles (id, apelido, terms_version, status, "
                "suspended_until) values (:id, :a, 'teste', 'suspended', now() + interval '1 day')"
            ),
            {"id": user_id, "a": f"s_{user_id.hex[:12]}"},
        )

    class FakeAdmin:
        async def delete_user(self, uid: UUID) -> None:
            async with api.session.begin():
                await api.session.execute(
                    text("delete from auth.users where id = :id"), {"id": uid}
                )

    api.app.dependency_overrides[get_supabase_admin] = FakeAdmin
    api.as_user(user_id)
    assert (await api.http.delete("/me")).status_code == 204

    kept = await scalar(
        api,
        "select suspended_until > now() from public.banned_identities where sub_hash = :h",
        h=ban_hash(sub, salt=SALT),
    )
    assert kept is True

    # Volta com o mesmo Google: a conta nasce suspensa até a mesma data.
    new_id = await create_auth_user(api.session, google_sub=sub)
    api.as_user(new_id)
    created = await api.http.post(
        "/me",
        json={
            "apelido": f"n_{new_id.hex[:12]}",
            "aceito_termos": True,
            "terms_version": CURRENT_TERMS_VERSION,
        },
    )
    assert created.status_code == 201
    assert created.json()["status"] == "suspended"


# ---------- revisões do M2 (13/10/2026) ----------


async def test_approving_a_hidden_comment_resets_the_report_count(api: Api) -> None:
    api.as_user(await member(api))
    comment_id = (await post(api, "Comentário que o admin aprova.")).json()["id"]
    for _ in range(3):
        api.as_user(await member(api))
        await api.http.post(f"/comments/{comment_id}/denuncia", json={})
    await admin(api)
    item = next(i for i in await queue(api) if i["comentario"] == comment_id)
    await api.http.post(f"/admin/moderacao/fila/{item['id']}", json={"acao": "aprovar"})

    api.as_user(await member(api))
    await api.http.post(f"/comments/{comment_id}/denuncia", json={})  # uma só denúncia nova

    assert comment_id in [c["id"] for c in (await api.http.get("/comments")).json()["items"]]


async def test_admin_double_click_does_not_publish_twice(api: Api) -> None:
    author = await member(api)
    api.as_user(author)
    check_id = await link_check(api, author)
    api.moderator.answer = AiVerdict("crime", 0.5)
    await post(api, "Publicar uma vez só.", checagem=check_id)

    await admin(api)
    item = next(
        i
        for i in await queue(api)
        if i["tipo"] == "strike_confirmation" and i["apelido"] == apelido(author)
    )
    first = await api.http.post(f"/admin/moderacao/fila/{item['id']}", json={"acao": "aprovar"})
    second = await api.http.post(f"/admin/moderacao/fila/{item['id']}", json={"acao": "aprovar"})

    assert first.status_code == 200 and second.status_code == 404
    listed = (await api.http.get("/comments", params={"checagem": check_id})).json()["items"]
    assert len(listed) == 1


async def test_blocked_attempts_also_wait_the_interval(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMMENT_MIN_INTERVAL_SECONDS", "30")
    get_settings.cache_clear()
    api.as_user(await member(api))
    api.moderator.answer = AiVerdict("ofensivo", 0.9)
    assert code(await post(api, "Primeira tentativa bloqueada.")) == "violacao"

    again = await post(api, "Segunda tentativa logo depois.")

    assert again.status_code == 429 and code(again) == "limite_comentarios"
    assert len(api.moderator.calls) == 1  # a segunda nem chegou à IA


async def test_control_characters_are_rejected(api: Api) -> None:
    api.as_user(await member(api))
    response = await post(api, "texto com" + chr(0) + "nulo")
    assert response.status_code == 422


async def test_only_a_visible_comment_can_be_deleted_by_the_author(api: Api) -> None:
    author = await member(api)
    api.as_user(author)
    comment_id = (await post(api, "Vai ser escondido.")).json()["id"]
    for _ in range(3):
        api.as_user(await member(api))
        await api.http.post(f"/comments/{comment_id}/denuncia", json={})

    api.as_user(author)
    assert (await api.http.delete(f"/comments/{comment_id}")).status_code == 404


async def test_report_without_profile_is_not_found(api: Api) -> None:
    api.as_user(await member(api))
    comment_id = (await post(api, "Comentário.")).json()["id"]
    api.as_user(await create_auth_user(api.session))  # sem perfil
    response = await api.http.post(f"/comments/{comment_id}/denuncia", json={})
    assert response.status_code == 404 and code(response) == "profile_not_found"


async def test_queue_id_out_of_range(api: Api) -> None:
    await admin(api)
    response = await api.http.post(
        "/admin/moderacao/fila/99999999999999999999", json={"acao": "aprovar"}
    )
    assert response.status_code == 422


# ---------- segunda rodada da revisão ----------


async def test_blocked_comment_of_a_banned_author_is_not_published(api: Api) -> None:
    author = await member(api)
    api.as_user(author)
    api.moderator.answer = AiVerdict("crime", 0.5)
    await post(api, "Texto ambíguo de quem depois foi banido.")
    async with api.session.begin():
        await api.session.execute(
            text("update public.profiles set status = 'banned' where id = :u"), {"u": author}
        )

    await admin(api)
    item = next(
        i
        for i in await queue(api)
        if i["tipo"] == "strike_confirmation" and i["apelido"] == apelido(author)
    )
    response = await api.http.post(f"/admin/moderacao/fila/{item['id']}", json={"acao": "aprovar"})

    assert response.status_code == 409
    count = await scalar(api, "select count(*) from public.comments where user_id = :u", u=author)
    assert count == 0


async def test_discard_is_only_for_blocked_items(api: Api) -> None:
    api.as_user(await member(api))
    api.moderator.answer = AiVerdict("desinformacao", 0.9)
    comment_id = (await post(api, "Dá para votar pelo celular.")).json()["id"]

    await admin(api)
    item = next(i for i in await queue(api) if i["comentario"] == comment_id)
    response = await api.http.post(
        f"/admin/moderacao/fila/{item['id']}", json={"acao": "descartar"}
    )

    assert response.status_code == 422 and code(response) == "acao_invalida"


async def test_account_banned_during_moderation_cannot_publish(api: Api) -> None:
    from app.repositories.comments import AccountRestrictedError, SqlCommentRepository

    author = await member(api)
    async with api.session.begin():
        await api.session.execute(
            text("update public.profiles set status = 'banned' where id = :u"), {"u": author}
        )
    repo = SqlCommentRepository(api.session)
    with pytest.raises(AccountRestrictedError):
        await repo.create(
            author,
            link_id=None,
            parent_id=None,
            body="Publicado depois do ban.",
            min_interval_seconds=0,
        )


async def test_author_delete_does_not_override_an_admin_removal(api: Api) -> None:
    from app.repositories.comments import SqlCommentRepository

    author = await member(api)
    api.as_user(author)
    comment_id = (await post(api, "Será removido pelo admin.")).json()["id"]
    async with api.session.begin():
        await api.session.execute(
            text("update public.comments set status = 'removed' where public_id = :p"),
            {"p": comment_id},
        )

    assert await SqlCommentRepository(api.session).delete_own(comment_id, author) is False
    status = await scalar(
        api, "select status from public.comments where public_id = :p", p=comment_id
    )
    assert status == "removed"


# ---------- revisão de segurança do M3 ----------


async def test_invisible_only_text_is_rejected(api: Api) -> None:
    api.as_user(await member(api))
    response = await post(api, "\u200b\u2060\ufeff\u200e")
    assert response.status_code == 422


async def test_invisible_characters_are_removed(api: Api) -> None:
    api.as_user(await member(api))
    response = await post(api, "vo\u200bto\u2060 se\u00adguro")
    assert response.status_code == 201
    assert response.json()["texto"] == "voto seguro"


async def test_hangul_filler_only_text_is_rejected(api: Api) -> None:
    api.as_user(await member(api))
    response = await post(api, chr(0x3164) * 3 + chr(0xFE0F))
    assert response.status_code == 422


async def test_forged_ip_of_someone_else_does_not_block_them(api: Api) -> None:
    from app.services.rate_limit import SlidingWindowLimiter

    api.app.state.rate_limiters.comments_read = SlidingWindowLimiter(limit=1, window_seconds=60)
    victim = {"x-forwarded-for": "198.51.100.7"}
    attacker = {"x-forwarded-for": "198.51.100.7, 203.0.113.9"}
    assert (await api.http.get("/comments", headers=attacker)).status_code == 200
    assert (await api.http.get("/comments", headers=attacker)).status_code == 429
    assert (await api.http.get("/comments", headers=victim)).status_code == 200


async def test_report_reason_rejects_control_characters(api: Api) -> None:
    api.as_user(await member(api))
    comment_id = (await post(api, "Comentário qualquer.")).json()["id"]
    api.as_user(await member(api))
    response = await api.http.post(f"/comments/{comment_id}/denuncia", json={"motivo": "a\x00b"})
    assert response.status_code == 422


async def test_reading_comments_is_rate_limited_per_ip(api: Api) -> None:
    from app.services.rate_limit import SlidingWindowLimiter

    api.app.state.rate_limiters.comments_read = SlidingWindowLimiter(limit=2, window_seconds=60)
    assert (await api.http.get("/comments")).status_code == 200
    assert (await api.http.get("/comments")).status_code == 200
    response = await api.http.get("/comments")
    assert response.status_code == 429 and response.headers["Retry-After"]


async def test_reports_are_rate_limited_per_user(api: Api) -> None:
    from app.services.rate_limit import SlidingWindowLimiter

    api.app.state.rate_limiters.comment_report = SlidingWindowLimiter(limit=1, window_seconds=600)
    ids = []
    for _ in range(2):
        api.as_user(await member(api))
        ids.append((await post(api, "Comentário para denunciar.")).json()["id"])
    api.as_user(await member(api))
    assert (await api.http.post(f"/comments/{ids[0]}/denuncia", json={})).status_code == 200
    assert (await api.http.post(f"/comments/{ids[1]}/denuncia", json={})).status_code == 429
