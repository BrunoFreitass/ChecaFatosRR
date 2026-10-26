import base64
import json
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import TokenVerifier, get_token_verifier
from app.repositories.checks import Limits, StoredCheck, SubjectRef, new_public_id
from app.repositories.profiles import Profile
from app.routers.checks import (
    get_check_repos,
    get_check_repository,
    get_check_settings,
    get_check_tools,
)
from app.routers.profiles import get_profile_repository
from app.services.check_service import CheckSettings, CheckTools
from app.services.factcheck import AgencyCheck, FactCheckUnavailableError
from app.services.fetcher import PageUnreadableError
from app.services.gemini import (
    GeminiContentBlockedError,
    GeminiQuotaExhaustedError,
    GeminiRateLimitedError,
    GeminiUnavailableError,
)
from app.services.url_guard import BlockedAddressError
from tests.fakes import (
    TEST_SUPABASE_URL,
    FakeAnalyzer,
    FakeCheckRepository,
    FakeFactCheck,
    FakeFetcher,
    FakeProfileRepository,
    StaticJwks,
    _FakeRow,
    article_html,
    make_es256_keys,
    make_token,
)

PRIVATE_KEY, PUBLIC_KEY = make_es256_keys()
PUBLIC_ID = re.compile(r"^[0-9a-hjkmnp-tv-z]{12}$")
LINK = "https://www.folhabv.com.br/eleicoes/calendario-2-turno"
MSG = (
    "URGENTE!!! O TSE adiou o segundo turno em Roraima para novembro. "
    "Repasse para todos os grupos. Dúvidas: (95) 99123-4567"
)


def settings(**limits: int) -> CheckSettings:
    daily = limits.pop("daily", 200)
    values = {"cache_hours": 24, "checks_per_hour": 10, "lookups_per_hour": 60} | limits
    return CheckSettings(
        limits=Limits(**values, prompt_version="v-teste"),
        daily_gemini_checks=daily,
        model="gemini-teste",
    )


class Env:
    def __init__(self) -> None:
        self.profiles = FakeProfileRepository()
        self.checks = FakeCheckRepository()
        self.fetcher = FakeFetcher()
        self.factcheck = FakeFactCheck()
        self.analyzer = FakeAnalyzer()
        self.settings = settings()
        self.sleeps: list[float] = []
        self.on_sleep: list[Callable[[], None]] = []
        self.now = 0.0  # relógio da requisição (time.monotonic simulado)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        for hook in self.on_sleep:
            hook()

    def tools(self) -> CheckTools:
        return CheckTools(
            fetcher=self.fetcher,
            factcheck=self.factcheck,
            analyzer=self.analyzer,
            sleep=self.sleep,
            clock=lambda: self.now,
        )

    def login(self, *, with_profile: bool = True) -> tuple[UUID, dict[str, str]]:
        user_id = uuid4()
        if with_profile:
            self.profiles.profiles[user_id] = Profile(
                id=user_id,
                apelido=f"u{user_id.hex[:8]}",
                status="active",
                suspended_until=None,
                terms_version="2026-10-08",
                created_at=datetime.now(UTC),
            )
        token = make_token(PRIVATE_KEY, sub=str(user_id))
        return user_id, {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env() -> Env:
    return Env()


@pytest.fixture
def api(app: FastAPI, env: Env) -> Iterator[TestClient]:
    app.dependency_overrides[get_token_verifier] = lambda: TokenVerifier(
        supabase_url=TEST_SUPABASE_URL, jwks=StaticJwks(PUBLIC_KEY)
    )
    app.dependency_overrides[get_profile_repository] = lambda: env.profiles
    app.dependency_overrides[get_check_repository] = lambda: env.checks
    app.dependency_overrides[get_check_repos] = lambda: lambda: nullcontext(env.checks)
    app.dependency_overrides[get_check_tools] = env.tools
    app.dependency_overrides[get_check_settings] = lambda: env.settings
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def error_code(response: object) -> str:
    return response.json()["error"]["code"]  # type: ignore[attr-defined]


# ---------- acesso ----------


def test_requires_login(api: TestClient) -> None:
    assert api.post("/checks", json={"url": LINK}).status_code == 401


def test_requires_profile(api: TestClient, env: Env) -> None:
    _, headers = env.login(with_profile=False)
    response = api.post("/checks", json={"url": LINK}, headers=headers)
    assert response.status_code == 404
    assert error_code(response) == "profile_not_found"


def test_banned_cannot_check(api: TestClient, env: Env) -> None:
    user_id, headers = env.login()
    env.profiles.set_status(user_id, "banned", None)
    response = api.post("/checks", json={"url": LINK}, headers=headers)
    assert response.status_code == 403
    assert error_code(response) == "conta_banida"


def test_suspended_can_check(api: TestClient, env: Env) -> None:
    user_id, headers = env.login()
    env.profiles.set_status(user_id, "suspended", datetime.now(UTC) + timedelta(hours=5))
    assert api.post("/checks", json={"url": LINK}, headers=headers).status_code == 200


# ---------- entrada ----------


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"url": "javascript:alert(1)"}, "url_invalida"),
        ({"url": "g1.globo.com"}, "url_invalida"),
        ({"url": "http://169.254.169.254/latest/meta-data/"}, "url_bloqueada"),
        ({"url": "http://localhost/health"}, "url_bloqueada"),
        ({"url": "http://localhost:8000/health"}, "url_invalida"),  # porta barrada antes
        ({"texto": "isso é verdade?"}, "texto_curto"),
        ({"texto": "palavra " * 700}, "texto_longo"),
        ({"url": LINK, "texto": MSG}, "validation_error"),
        ({}, "validation_error"),
    ],
)
def test_invalid_input(api: TestClient, env: Env, body: dict[str, str], code: str) -> None:
    _, headers = env.login()
    response = api.post("/checks", json=body, headers=headers)
    assert response.status_code == 422
    assert error_code(response) == code
    assert env.fetcher.calls == []
    assert env.analyzer.inputs == []


def test_social_media_suggests_pasting_text_without_downloading(api: TestClient, env: Env) -> None:
    _, headers = env.login()
    response = api.post(
        "/checks", json={"url": "https://www.instagram.com/p/abc123/"}, headers=headers
    )
    assert response.status_code == 422
    assert response.json()["error"]["sugestao"] == "colar_texto"
    assert env.fetcher.calls == []
    assert env.checks.requests == {}  # nem conta no limite


# ---------- checagem ----------


def test_link_check(api: TestClient, env: Env) -> None:
    env.factcheck.results = [
        AgencyCheck(
            "Aos Fatos",
            "Calendário do 2º turno em Roraima não foi adiado",
            "Falso",
            "https://aosfatos.org/x",
        )
    ]
    _, headers = env.login()

    response = api.post("/checks", json={"url": LINK + "?utm_source=zap"}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert PUBLIC_ID.match(body["id"])
    assert body["tipo"] == "link"
    assert body["url"] == LINK
    assert body["domain"] == "folhabv.com.br"
    assert body["title"] == "Calendário do 2º turno é mantido em Roraima"
    assert body["veredito"] == "falso"
    assert body["cache"] is False
    assert body["checagens_agencias"][0]["agencia"] == "Aos Fatos"
    assert "created_by" not in body and "link_id" not in body
    assert env.fetcher.calls == [LINK]
    assert env.analyzer.inputs[0].kind == "link"
    assert "segundo turno" in env.analyzer.inputs[0].content

    # Pública: qualquer um lê pelo id, e o link aparece em "Últimas".
    assert api.get(f"/checks/{body['id']}").json()["id"] == body["id"]
    latest = api.get("/checks/latest").json()
    assert [item["id"] for item in latest["items"]] == [body["id"]]


def test_link_origin_is_the_downloaded_domain(api: TestClient, env: Env) -> None:
    # Link baixado pelo servidor: a origem é confirmada; texto colado não tem origem.
    _, headers = env.login()
    assert api.post("/checks", json={"url": LINK}, headers=headers).status_code == 200
    assert api.post("/checks", json={"texto": MSG}, headers=headers).status_code == 200
    assert [i.source_domain for i in env.analyzer.inputs] == ["folhabv.com.br", None]


def test_redirect_to_a_url_the_guard_rejects_is_unreadable_not_500(
    api: TestClient, env: Env
) -> None:
    env.fetcher.final_url = "https://exemplo.com.br:9999/x"  # porta fora do permitido
    _, headers = env.login()
    response = api.post("/checks", json={"url": LINK}, headers=headers)
    assert response.status_code == 422
    assert error_code(response) == "pagina_ilegivel"


def test_link_origin_follows_redirects(api: TestClient, env: Env) -> None:
    # O que vale é de onde a página veio de fato, não o link colado.
    env.fetcher.final_url = "https://g1.globo.com/rr/noticia/x.ghtml"
    _, headers = env.login()
    assert api.post("/checks", json={"url": LINK}, headers=headers).status_code == 200
    assert env.analyzer.inputs[0].source_domain == "g1.globo.com"


def test_publication_date_reaches_the_analyzer(api: TestClient, env: Env) -> None:
    # Sem a data, o modelo tratou notícia do dia como "fato futuro" (teste manual 09/10/2026).
    meta = b"<meta property='article:published_time' content='2026-10-09T10:00:00Z'>"
    env.fetcher.html = env.fetcher.html.replace(b"</head>", meta + b"</head>")
    _, headers = env.login()

    assert api.post("/checks", json={"url": LINK}, headers=headers).status_code == 200
    assert env.analyzer.inputs[0].published_at == date(2026, 10, 9)


@pytest.mark.parametrize(
    ("answered", "saved"), [("gemini-3.7-flash", "gemini-3.7-flash"), (None, "gemini-teste")]
)
def test_saves_the_model_that_answered(
    api: TestClient, env: Env, answered: str | None, saved: str
) -> None:
    # Com fallback por cota diária, quem respondeu pode não ser o primeiro da cadeia.
    env.analyzer.model = answered
    _, headers = env.login()

    assert api.post("/checks", json={"texto": MSG}, headers=headers).status_code == 200
    assert [check.model for check in env.checks.saved] == [saved]


def test_tokens_are_saved_in_the_result_but_not_shown(api: TestClient, env: Env) -> None:
    """Painel do admin (plano §B): tokens no checks.result, sem migration; nunca na resposta."""
    env.analyzer.tokens = {"in": 2100, "out": 380, "thoughts": 0}
    _, headers = env.login()

    response = api.post("/checks", json={"texto": MSG}, headers=headers)

    assert response.status_code == 200
    assert env.checks.saved[0].result["tokens"] == {"in": 2100, "out": 380, "thoughts": 0}
    assert "tokens" not in response.text


def test_without_tokens_the_result_has_no_tokens_key(api: TestClient, env: Env) -> None:
    _, headers = env.login()

    assert api.post("/checks", json={"texto": MSG}, headers=headers).status_code == 200
    assert "tokens" not in env.checks.saved[0].result


def _count_gets(env: Env, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    original = env.checks.get

    async def counting(public_id: str) -> Any:
        calls.append(public_id)
        return await original(public_id)

    monkeypatch.setattr(env.checks, "get", counting)
    return calls


def test_public_check_page_is_read_once(
    api: TestClient, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR cache-checagem: link viral = muitas leituras da mesma checagem, uma ida ao banco."""
    _, headers = env.login()
    created = api.post("/checks", json={"url": LINK}, headers=headers).json()
    calls = _count_gets(env, monkeypatch)

    bodies = [api.get(f"/checks/{created['id']}").json() for _ in range(5)]

    assert calls == [created["id"]]
    assert all(b == bodies[0] for b in bodies)
    assert bodies[0]["id"] == created["id"]


def test_a_new_analysis_invalidates_the_check_pages_at_once(
    api: TestClient, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checagem nova (inclusive reanálise do mesmo link) pode mudar o título/data do link que as
    páginas antigas mostram: o cache de páginas de checagem é limpo na hora."""
    _, headers = env.login()
    first = api.post("/checks", json={"url": LINK}, headers=headers).json()["id"]
    calls = _count_gets(env, monkeypatch)
    api.get(f"/checks/{first}")

    api.post("/checks", json={"texto": MSG}, headers=headers)
    api.get(f"/checks/{first}")

    assert calls == [first, first]  # a 2ª leitura foi à fonte de novo


def test_reusing_an_existing_analysis_keeps_the_check_pages(
    api: TestClient, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Num pico, muita gente checa o mesmo link: a análise é reaproveitada e nada muda."""
    _, headers = env.login()
    first = api.post("/checks", json={"url": LINK}, headers=headers).json()["id"]
    calls = _count_gets(env, monkeypatch)
    api.get(f"/checks/{first}")

    again = api.post("/checks", json={"url": LINK}, headers=headers).json()
    api.get(f"/checks/{first}")

    assert again["cache"] is True  # reaproveitou a análise de antes
    assert calls == [first]  # a página continuou saindo da memória


def test_check_page_route_never_reads_the_login() -> None:
    """A página vai para um cache compartilhado: se a rota passar a ler o login (perfil, token),
    a resposta pode virar pessoal. Este teste falha nesse dia."""
    from fastapi.routing import APIRoute

    from app.auth import current_user
    from app.routers.checks import current_profile, router

    route = next(
        r
        for r in router.routes
        if isinstance(r, APIRoute) and r.path == "/checks/{public_id}" and "GET" in r.methods
    )

    def calls(dependant: Any) -> set[Any]:
        found = {dependant.call}
        for sub in dependant.dependencies:
            found |= calls(sub)
        return found

    used = calls(route.dependant)
    assert current_user not in used and current_profile not in used


def test_check_page_is_the_same_for_everyone(api: TestClient, env: Env) -> None:
    """Nada do usuário logado na página: logado ou não, a mesma resposta (vai para o cache)."""
    _, headers = env.login()
    created = api.post("/checks", json={"url": LINK}, headers=headers).json()["id"]

    anonymous = api.get(f"/checks/{created}")
    logged = api.get(f"/checks/{created}", headers=headers)

    assert anonymous.status_code == logged.status_code == 200
    assert anonymous.json() == logged.json()


def test_unknown_check_is_404_and_not_cached(
    api: TestClient, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_gets(env, monkeypatch)

    assert api.get("/checks/abcdefghjkmn").status_code == 404
    assert api.get("/checks/abcdefghjkmn").status_code == 404
    assert api.get("/checks/1").status_code == 404  # formato inválido nem chega ao banco
    assert len(calls) == 2


def test_latest_is_read_once_and_a_new_check_shows_at_once(
    api: TestClient, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR cache-leitura: a home não vai ao banco a cada visita, mas checagem nova aparece já."""
    calls = 0
    original = env.checks.latest

    async def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(env.checks, "latest", counting)
    _, headers = env.login()
    assert api.get("/checks/latest").json()["items"] == []
    assert api.get("/checks/latest").json()["items"] == []
    assert calls == 1  # a 2ª visita saiu da memória

    created = api.post("/checks", json={"url": LINK}, headers=headers).json()["id"]

    assert [i["id"] for i in api.get("/checks/latest").json()["items"]] == [created]
    assert calls == 2


def test_latest_pages_are_cached_separately(api: TestClient, env: Env) -> None:
    assert api.get("/checks/latest", params={"limit": 5}).status_code == 200
    assert api.get("/checks/latest", params={"limit": 10}).status_code == 200
    assert api.get("/checks/latest", params={"cursor": "lixo"}).status_code == 422  # antes do cache


def test_text_check_masks_personal_data_and_stays_unlisted(api: TestClient, env: Env) -> None:
    _, headers = env.login()

    response = api.post("/checks", json={"texto": MSG}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["tipo"] == "texto"
    assert "99123-4567" not in body["texto"]
    assert "[telefone oculto]" in body["texto"]
    assert body["url"] is None
    assert env.fetcher.calls == []
    assert env.analyzer.inputs[0].kind == "texto"
    assert "99123" not in env.analyzer.inputs[0].content  # nem o Gemini recebe o telefone
    assert api.get(f"/checks/{body['id']}").status_code == 200
    assert api.get("/checks/latest").json()["items"] == []


def test_same_text_with_different_spacing_hits_the_cache(api: TestClient, env: Env) -> None:
    _, headers = env.login()
    first = api.post("/checks", json={"texto": MSG}, headers=headers).json()
    variant = "Encaminhada\n" + MSG.upper().replace(" ", "  ")
    again = api.post("/checks", json={"texto": variant}, headers=headers).json()
    assert again["id"] == first["id"]
    assert again["cache"] is True
    assert len(env.analyzer.inputs) == 1


def test_offensive_title_is_not_listed(api: TestClient, env: Env) -> None:
    env.fetcher.html = article_html("Que porra de eleição")
    _, headers = env.login()
    body = api.post("/checks", json={"url": LINK}, headers=headers).json()
    assert api.get(f"/checks/{body['id']}").status_code == 200
    assert api.get("/checks/latest").json()["items"] == []


# ---------- limites e cota ----------


def test_cache_hits_do_not_spend_the_hourly_check_limit(api: TestClient, env: Env) -> None:
    env.settings = settings(checks_per_hour=2)
    _, headers = env.login()
    assert api.post("/checks", json={"url": LINK}, headers=headers).status_code == 200
    for _ in range(5):
        assert api.post("/checks", json={"url": LINK}, headers=headers).json()["cache"] is True
    assert api.post("/checks", json={"url": LINK + "-2"}, headers=headers).status_code == 200

    blocked = api.post("/checks", json={"url": LINK + "-3"}, headers=headers)
    assert blocked.status_code == 429
    assert error_code(blocked) == "limite_checagens"
    assert blocked.headers["Retry-After"] == "1800"


def test_cache_lookups_have_their_own_limit(api: TestClient, env: Env) -> None:
    env.settings = settings(lookups_per_hour=2)
    _, headers = env.login()
    for _ in range(3):  # 1 checagem nova + 2 consultas ao cache
        assert api.post("/checks", json={"url": LINK}, headers=headers).status_code == 200

    blocked = api.post("/checks", json={"url": LINK}, headers=headers)
    assert blocked.status_code == 429
    assert error_code(blocked) == "limite_consultas"


def test_daily_quota_exhausted_answers_with_agencies_only(api: TestClient, env: Env) -> None:
    env.settings = settings(daily=0)
    _, headers = env.login()
    response = api.post("/checks", json={"url": LINK}, headers=headers)
    assert response.status_code == 200
    assert response.json()["modo"] == "agencias"
    # Conta como consulta (limite de consultas), não como checagem com IA.
    assert [hit for _, hit in env.checks.requests.values()] == [True]
    assert env.checks.claimed == {}
    assert env.analyzer.inputs == []


def test_gemini_failure_answers_with_agencies_only(api: TestClient, env: Env) -> None:
    env.analyzer.error = GeminiUnavailableError("modelos lentos")
    _, headers = env.login()
    response = api.post("/checks", json={"texto": MSG}, headers=headers)
    assert response.status_code == 200
    assert response.json()["modo"] == "agencias"
    assert [hit for _, hit in env.checks.requests.values()] == [True]
    assert env.checks.claimed == {}
    assert env.checks.gemini_used == 1  # a unidade da cota diária não volta


def test_unreadable_page_counts_in_the_limit_and_suggests_text(api: TestClient, env: Env) -> None:
    env.fetcher.error = PageUnreadableError("status")
    _, headers = env.login()
    response = api.post("/checks", json={"url": LINK}, headers=headers)
    assert response.status_code == 422
    assert error_code(response) == "pagina_ilegivel"
    assert response.json()["error"]["sugestao"] == "colar_texto"
    assert len(env.checks.requests) == 1
    assert env.checks.claimed == {}
    assert env.checks.gemini_used == 0  # link ilegível não gasta cota


# ---------- single-flight ----------


def test_waits_for_another_check_of_the_same_content(api: TestClient, env: Env) -> None:
    env.checks.busy = True  # outra instância está checando este link

    def other_instance_finishes() -> None:
        if env.checks.rows:
            return
        ref = SubjectRef("link", next(iter(env.checks.link_ids.values())))
        stored = StoredCheck(
            public_id=new_public_id(),
            kind="link",
            url=LINK,
            domain="folhabv.com.br",
            title="Calendário",
            published_at=None,
            texto=None,
            veredito="falso",
            confianca=80,
            result={"resumo": "A votação não foi adiada."},
            created_at=datetime.now(UTC),
        )
        env.checks.rows.append(
            _FakeRow(ref, stored, True, uuid4(), env.settings.limits.prompt_version)
        )

    env.on_sleep.append(other_instance_finishes)
    _, headers = env.login()

    response = api.post("/checks", json={"url": LINK}, headers=headers)

    assert response.status_code == 200
    assert response.json()["cache"] is True
    assert env.analyzer.inputs == []  # não chamou o Gemini de novo
    # Conta como consulta ao cache (limite de consultas), não como checagem nova.
    assert [hit for _, hit in env.checks.requests.values()] == [True]
    assert env.sleeps == [1.5]


def test_gives_up_waiting_with_409(api: TestClient, env: Env) -> None:
    env.checks.busy = True
    _, headers = env.login()
    response = api.post("/checks", json={"url": LINK}, headers=headers)
    assert response.status_code == 409
    assert error_code(response) == "checagem_em_andamento"
    assert env.checks.requests == {}
    assert env.analyzer.inputs == []


# ---------- leitura ----------


@pytest.mark.parametrize("public_id", ["1", "42", "ABCDEFGHJKMN", "0000000000000", "abc"])
def test_malformed_ids_are_404(api: TestClient, public_id: str) -> None:
    response = api.get(f"/checks/{public_id}")
    assert response.status_code == 404
    assert error_code(response) == "checagem_nao_encontrada"


def test_unknown_id_is_404(api: TestClient) -> None:
    assert api.get("/checks/0123456789ab").status_code == 404


def test_latest_paginates_with_cursor(api: TestClient, env: Env) -> None:
    _, headers = env.login()
    ids = [
        api.post("/checks", json={"url": f"{LINK}-{n}"}, headers=headers).json()["id"]
        for n in range(3)
    ]
    page1 = api.get("/checks/latest", params={"limit": 2}).json()
    assert [item["id"] for item in page1["items"]] == [ids[2], ids[1]]
    # Sem checagem de agência: a lista mostra "Ainda sem checagem" (neutro).
    assert [item["total_agencias"] for item in page1["items"]] == [0, 0]
    assert page1["next_cursor"]
    page2 = api.get("/checks/latest", params={"limit": 2, "cursor": page1["next_cursor"]}).json()
    assert [item["id"] for item in page2["items"]] == [ids[0]]
    assert page2["next_cursor"] is None


def test_invalid_cursor(api: TestClient) -> None:
    response = api.get("/checks/latest", params={"cursor": "nao-e-cursor"})
    assert response.status_code == 422
    assert error_code(response) == "cursor_invalido"


def test_naive_cursor_is_rejected(api: TestClient) -> None:
    raw = json.dumps(["2026-10-01T00:00:00", "0123456789ab"]).encode()
    cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    response = api.get("/checks/latest", params={"cursor": cursor})
    assert response.status_code == 422
    assert error_code(response) == "cursor_invalido"


# ---------- correções da revisão ----------


def test_result_finished_between_cache_miss_and_claim_is_reused(api: TestClient, env: Env) -> None:
    # Outra requisição checava (claim ativo) e salvou logo antes do nosso claim.
    env.checks.busy = True
    original_claim = env.checks.claim

    async def claim_after_other_finished(subject: SubjectRef) -> datetime | None:
        env.checks.busy = False
        stored = StoredCheck(
            public_id=new_public_id(),
            kind="link",
            url=LINK,
            domain="folhabv.com.br",
            title="Calendário",
            published_at=None,
            texto=None,
            veredito="falso",
            confianca=80,
            result={"resumo": "Já checado."},
            created_at=datetime.now(UTC),
        )
        env.checks.rows.append(
            _FakeRow(subject, stored, True, uuid4(), env.settings.limits.prompt_version)
        )
        return await original_claim(subject)

    env.checks.claim = claim_after_other_finished  # type: ignore[method-assign]
    _, headers = env.login()

    response = api.post("/checks", json={"url": LINK}, headers=headers)

    assert response.status_code == 200
    assert response.json()["cache"] is True
    assert env.analyzer.inputs == []  # não chamou o Gemini de novo
    assert [hit for _, hit in env.checks.requests.values()] == [True]  # virou consulta
    assert env.checks.claimed == {}  # e soltou o claim


def test_waiter_takes_over_when_the_other_check_fails(api: TestClient, env: Env) -> None:
    env.checks.busy = True

    def other_gives_up() -> None:
        env.checks.busy = False

    env.on_sleep.append(other_gives_up)
    _, headers = env.login()

    response = api.post("/checks", json={"url": LINK}, headers=headers)

    assert response.status_code == 200
    assert response.json()["cache"] is False
    assert len(env.analyzer.inputs) == 1
    assert len(env.checks.requests) == 1  # agora a checagem é dele: conta no limite


def test_waiter_does_not_take_over_without_time_for_a_whole_check(
    api: TestClient, env: Env
) -> None:
    # A outra checagem falhou tarde: assumir agora passaria do corte do Cloudflare (~100 s).
    # Responde "tente de novo" na hora (sem esperar o resto) e devolve o limite.
    env.checks.busy = True

    def other_gives_up_late() -> None:
        if len(env.sleeps) == 3:  # 4,5 s de espera: fora da janela de assumir
            env.checks.busy = False

    env.on_sleep.append(other_gives_up_late)
    _, headers = env.login()

    response = api.post("/checks", json={"url": LINK}, headers=headers)

    assert response.status_code == 409
    assert error_code(response) == "checagem_em_andamento"
    assert len(env.sleeps) == 3  # não ficou esperando até o fim
    assert env.analyzer.inputs == []
    assert env.checks.requests == {}  # limite devolvido
    assert env.checks.claimed == {}  # o claim pego só para conferir foi solto


def test_wait_is_measured_by_the_real_clock(api: TestClient, env: Env) -> None:
    # As consultas ao banco também gastam tempo: a espera conta o relógio, não só as pausas.
    env.checks.busy = True

    def slow_database() -> None:
        env.now += 30.0

    env.on_sleep.append(slow_database)
    _, headers = env.login()

    response = api.post("/checks", json={"url": LINK}, headers=headers)

    assert response.status_code == 409
    assert len(env.sleeps) == 3  # 90 s de relógio, não 55 pausas de 1,5 s


def test_redirect_to_another_site_is_not_listed(api: TestClient, env: Env) -> None:
    env.fetcher.final_url = "https://golpe.example.com/noticia-falsa"
    _, headers = env.login()
    body = api.post("/checks", json={"url": LINK}, headers=headers).json()
    assert body["domain"] == "folhabv.com.br"
    assert api.get("/checks/latest").json()["items"] == []


def test_internal_address_found_while_fetching_looks_like_unreadable_page(
    api: TestClient, env: Env
) -> None:
    env.fetcher.error = BlockedAddressError("dns aponta para rede interna")
    _, headers = env.login()
    response = api.post("/checks", json={"url": LINK}, headers=headers)
    assert response.status_code == 422
    assert error_code(response) == "pagina_ilegivel"  # não revela o que resolve para dentro


# ---------- teto diário por usuário (decisão de 09/10/2026) ----------


def test_daily_limit_blocks_new_checks_but_not_cache(api: TestClient, env: Env) -> None:
    env.settings = settings(checks_per_day=2)
    _, headers = env.login()
    assert api.post("/checks", json={"url": LINK + "-1"}, headers=headers).status_code == 200
    assert api.post("/checks", json={"url": LINK + "-2"}, headers=headers).status_code == 200

    blocked = api.post("/checks", json={"url": LINK + "-3"}, headers=headers)
    assert blocked.status_code == 429
    assert error_code(blocked) == "limite_diario"
    assert int(blocked.headers["Retry-After"]) > 0

    # Resultado que já existe continua abrindo (consulta ao cache não conta no teto).
    again = api.post("/checks", json={"url": LINK + "-1"}, headers=headers)
    assert again.status_code == 200
    assert again.json()["cache"] is True


def test_google_daily_quota_shows_come_back_tomorrow(api: TestClient, env: Env) -> None:
    # O Google respondeu 429 de cota diária durante a chamada (a nossa cota ainda tinha saldo).
    env.analyzer.error = GeminiQuotaExhaustedError()
    _, headers = env.login()
    response = api.post("/checks", json={"texto": MSG}, headers=headers)
    assert response.status_code == 200
    assert response.json()["modo"] == "agencias"
    assert [hit for _, hit in env.checks.requests.values()] == [True]  # consulta


def test_new_prompt_version_does_not_serve_old_results_as_cache(api: TestClient, env: Env) -> None:
    _, headers = env.login()
    old = api.post("/checks", json={"url": LINK}, headers=headers).json()
    assert env.checks.saved[0].prompt_version == "v-teste"

    # Mesma versão: cache.
    again = api.post("/checks", json={"url": LINK}, headers=headers).json()
    assert again["cache"] is True and again["id"] == old["id"]

    # Prompt mudou: análise nova, e a antiga continua no link público.
    env.settings = replace(
        env.settings, limits=replace(env.settings.limits, prompt_version="v-nova")
    )
    fresh = api.post("/checks", json={"url": LINK}, headers=headers).json()
    assert fresh["cache"] is False and fresh["id"] != old["id"]
    assert len(env.analyzer.inputs) == 2
    assert env.checks.saved[1].prompt_version == "v-nova"
    assert api.get(f"/checks/{old['id']}").json()["id"] == old["id"]


def test_router_uses_the_current_prompt_version() -> None:
    from app.config import Settings
    from app.routers.checks import get_check_settings
    from app.services.gemini import PROMPT_VERSION

    assert get_check_settings(Settings()).limits.prompt_version == PROMPT_VERSION


def new_prompt_version(env: Env) -> None:
    env.settings = replace(
        env.settings, limits=replace(env.settings.limits, prompt_version="v-nova")
    )


def test_without_quota_serves_the_previous_prompt_version_analysis(
    api: TestClient, env: Env
) -> None:
    _, headers = env.login()
    old = api.post("/checks", json={"url": LINK}, headers=headers).json()
    assert old["analise_anterior"] is False

    new_prompt_version(env)
    env.checks.gemini_used = env.settings.daily_gemini_checks  # cota do dia acabou
    response = api.post("/checks", json={"url": LINK}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == old["id"]
    assert body["cache"] is True
    assert body["analise_anterior"] is True  # aviso discreto na tela
    assert len(env.analyzer.inputs) == 1  # o Gemini não foi chamado de novo
    # Conta como checagem nova (limites de 10/h e 20/dia): sem isso o fallback viraria
    # consulta ilimitada, já que não passa pelo limite de consultas do begin_check.
    assert [hit for _, hit in env.checks.requests.values()] == [False, False]


def test_without_quota_and_without_previous_analysis_answers_with_agencies(
    api: TestClient, env: Env
) -> None:
    env.settings = settings(daily=0)
    _, headers = env.login()
    response = api.post("/checks", json={"url": LINK}, headers=headers)
    assert response.status_code == 200
    assert response.json()["modo"] == "agencias"


def test_public_link_flags_an_analysis_from_a_previous_prompt(api: TestClient, env: Env) -> None:
    _, headers = env.login()
    old = api.post("/checks", json={"url": LINK}, headers=headers).json()
    assert api.get(f"/checks/{old['id']}").json()["analise_anterior"] is False
    new_prompt_version(env)
    assert api.get(f"/checks/{old['id']}").json()["analise_anterior"] is True


def test_latest_shows_only_the_newest_check_of_each_link(api: TestClient, env: Env) -> None:
    _, headers = env.login()
    old = api.post("/checks", json={"url": LINK}, headers=headers).json()
    new_prompt_version(env)
    new = api.post("/checks", json={"url": LINK}, headers=headers).json()
    assert new["id"] != old["id"]
    items = api.get("/checks/latest").json()["items"]
    assert [item["id"] for item in items] == [new["id"]]


def test_null_prompt_version_is_flagged_as_previous_analysis(api: TestClient, env: Env) -> None:
    # Checagem de antes da coluna (versão desconhecida): mostra o aviso, por segurança.
    _, headers = env.login()
    old = api.post("/checks", json={"url": LINK}, headers=headers).json()
    row = env.checks.rows[0]
    row.check = replace(row.check, prompt_version=None)
    assert api.get(f"/checks/{old['id']}").json()["analise_anterior"] is True


def test_quota_fallback_with_pasted_text(api: TestClient, env: Env) -> None:
    _, headers = env.login()
    old = api.post("/checks", json={"texto": MSG}, headers=headers).json()
    new_prompt_version(env)
    env.checks.gemini_used = env.settings.daily_gemini_checks
    body = api.post("/checks", json={"texto": MSG}, headers=headers).json()
    assert body["id"] == old["id"] and body["analise_anterior"] is True


# ---------- tempo de cada etapa no log (teste de produção: 21,8 s, só 3,3 s no Gemini) ----------


def _timing(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    [record] = [r for r in caplog.records if r.getMessage() == "check_timing"]
    return record


def test_new_check_logs_the_time_of_each_stage(
    api: TestClient, env: Env, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def advancing(seconds: float, call: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            env.now += seconds
            return await call(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(env.fetcher, "fetch", advancing(2.0, env.fetcher.fetch))
    monkeypatch.setattr(env.factcheck, "search", advancing(4.0, env.factcheck.search))
    monkeypatch.setattr(env.analyzer, "analyze", advancing(3.0, env.analyzer.analyze))
    monkeypatch.setattr(env.checks, "save_result", advancing(0.5, env.checks.save_result))
    caplog.set_level(logging.INFO, logger="app.services.check_service")
    _, headers = env.login()

    assert api.post("/checks", json={"url": LINK}, headers=headers).status_code == 200

    record = _timing(caplog)
    assert record.outcome == "nova"
    assert record.fetch_ms == 2000
    assert record.factcheck_ms == 4000
    assert record.gemini_ms == 3000
    assert record.db_ms >= 500
    assert record.extract_ms >= 0
    assert record.total_ms >= 9500


def test_cache_hit_and_errors_also_log_the_timing(
    api: TestClient, env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.services.check_service")
    _, headers = env.login()
    api.post("/checks", json={"url": LINK}, headers=headers)
    caplog.clear()

    api.post("/checks", json={"url": LINK}, headers=headers)
    assert _timing(caplog).outcome == "cache"

    caplog.clear()
    env.analyzer.error = GeminiUnavailableError("fora")
    api.post(
        "/checks",
        json={"texto": "URGENTE: a votação foi adiada para novembro, repasse!"},
        headers=headers,
    )
    assert _timing(caplog).outcome == "agencias"


# ---------- regras de veredito também na leitura (decisões de 11/10/2026) ----------


def _stored_true_without_agency(api: TestClient, env: Env) -> str:
    # Simula uma checagem salva antes da política: "verdadeiro" sem nenhuma agência.
    env.analyzer.veredito = "verdadeiro"
    env.analyzer.confianca = 95
    _, headers = env.login()
    created = api.post("/checks", json={"url": LINK}, headers=headers).json()
    row = env.checks.rows[0]
    row.check = replace(
        row.check,
        veredito="verdadeiro",
        result={**row.check.result, "resumo": "Reportagem legítima.", "checagens_agencias": []},
    )
    return str(created["id"])


def test_public_page_reads_an_old_true_without_agency_as_awaiting_check(
    api: TestClient, env: Env
) -> None:
    check_id = _stored_true_without_agency(api, env)

    body = api.get(f"/checks/{check_id}").json()

    assert body["veredito"] == "nao_verificavel"
    assert body["rebaixado_sem_agencia"] is True
    # Checagem antiga não guardou o domínio final (redirect): sem "origem confirmada".
    assert body["resumo"].startswith("Conteúdo publicado em folhabv.com.br sobre: ")
    assert "origem confirmada" not in body["resumo"]
    assert "legítima" not in body["resumo"]


def test_latest_reads_an_old_true_without_agency_as_awaiting_check(
    api: TestClient, env: Env
) -> None:
    check_id = _stored_true_without_agency(api, env)

    [item] = api.get("/checks/latest").json()["items"]

    assert item["id"] == check_id
    assert item["veredito"] == "nao_verificavel"
    assert item["total_agencias"] == 0


def test_old_true_with_a_corroborating_agency_is_kept(api: TestClient, env: Env) -> None:
    env.analyzer.veredito = "verdadeiro"
    env.factcheck.results = [
        AgencyCheck(
            "Aos Fatos",
            "Confirmado: calendário do 2º turno mantido em Roraima",
            "Verdadeiro",
            "https://aosfatos.org/v",
        )
    ]
    _, headers = env.login()
    created = api.post("/checks", json={"url": LINK}, headers=headers).json()

    assert api.get(f"/checks/{created['id']}").json()["veredito"] == "verdadeiro"
    assert api.get("/checks/latest").json()["items"][0]["veredito"] == "verdadeiro"


OTHER_SUBJECT = AgencyCheck(
    "AFP Checamos",
    "Pausa na divulgação de resultados não comprova fraude eleitoral",
    "Falso",
    "https://checamos.afp.com/pausa",
)


def test_unrelated_check_does_not_back_a_low_confidence_false_when_saving(
    api: TestClient, env: Env
) -> None:
    # Salvaguarda de relevância (19/10): a IA "citou" uma checagem de outro assunto.
    env.analyzer.veredito, env.analyzer.confianca = "falso", 55
    env.factcheck.results = [OTHER_SUBJECT]
    _, headers = env.login()

    body = api.post("/checks", json={"url": LINK}, headers=headers).json()

    assert body["veredito"] == "nao_verificavel"
    assert body["checagens_agencias"] == []
    assert [c["url"] for c in body["checagens_relacionadas"]] == [OTHER_SUBJECT.url]
    assert env.checks.saved[-1].veredito == "nao_verificavel"
    assert env.checks.saved[-1].result["palavras_chave"]
    assert api.get("/checks/latest").json()["items"][0]["veredito"] == "nao_verificavel"


def test_unrelated_confirming_check_does_not_back_a_true_when_saving(
    api: TestClient, env: Env
) -> None:
    env.analyzer.veredito = "verdadeiro"
    env.factcheck.results = [replace(OTHER_SUBJECT, veredito="Verdadeiro")]
    _, headers = env.login()

    body = api.post("/checks", json={"url": LINK}, headers=headers).json()

    assert body["veredito"] == "nao_verificavel"
    assert body["rebaixado_sem_agencia"] is True
    assert body["resumo"] != "A votação não foi adiada."  # texto neutro, não o da IA


def test_high_confidence_false_without_a_relevant_check_stays(api: TestClient, env: Env) -> None:
    env.analyzer.veredito, env.analyzer.confianca = "falso", 85
    env.factcheck.results = [OTHER_SUBJECT]
    _, headers = env.login()

    body = api.post("/checks", json={"url": LINK}, headers=headers).json()

    assert body["veredito"] == "falso"
    assert body["checagens_agencias"] == []


def test_agencies_only_puts_unrelated_checks_in_related_when_saving(
    api: TestClient, env: Env
) -> None:
    env.analyzer.error = GeminiRateLimitedError("rpm")
    env.factcheck.results = [OTHER_SUBJECT]
    _, headers = env.login()

    body = api.post("/checks", json={"url": LINK}, headers=headers).json()

    assert body["modo"] == "agencias"
    assert body["checagens_agencias"] == []
    assert [c["url"] for c in body["checagens_relacionadas"]] == [OTHER_SUBJECT.url]
    assert "não encontramos checagens" in body["resumo"]


def test_related_agency_checks_are_returned_separately(api: TestClient, env: Env) -> None:
    _, headers = env.login()
    created = api.post("/checks", json={"url": LINK}, headers=headers).json()
    related = {
        "agencia": "Lupa",
        "titulo": "Outro",
        "veredito": "Falso",
        "url": "https://lupa.uol.com.br/x",
    }
    row = env.checks.rows[0]
    row.check = replace(row.check, result={**row.check.result, "checagens_relacionadas": [related]})

    body = api.get(f"/checks/{created['id']}").json()

    assert body["checagens_relacionadas"] == [{**related, "data": None}]


def test_old_checks_without_related_field_read_as_empty(api: TestClient, env: Env) -> None:
    _, headers = env.login()
    created = api.post("/checks", json={"url": LINK}, headers=headers).json()
    assert api.get(f"/checks/{created['id']}").json()["checagens_relacionadas"] == []


def test_open_platform_link_reaches_the_analyzer_flagged(api: TestClient, env: Env) -> None:
    _, headers = env.login()
    response = api.post(
        "/checks", json={"url": "https://fulano.blogspot.com/2026/10/post.html"}, headers=headers
    )
    assert response.status_code == 200
    assert env.analyzer.inputs[0].open_platform is True


def test_news_site_link_is_not_an_open_platform(api: TestClient, env: Env) -> None:
    _, headers = env.login()
    api.post("/checks", json={"url": LINK}, headers=headers)
    assert env.analyzer.inputs[0].open_platform is False


# ---------- modo "só agências" (12/10/2026) ----------
# Gemini sem cota, em 429 por minuto ou com todos os modelos fora: responde na hora com o
# resultado da Fact Check API, sem IA, com o selo neutro "Análise automática indisponível".


def agency_check(**extra: str) -> AgencyCheck:
    values = {
        "agencia": "Agência Lupa",
        "titulo": "É falso que o 2º turno em Roraima foi adiado",
        "veredito": "Falso",
        "url": "https://lupa.uol.com.br/jornalismo/2026/10/10/adiamento-falso",
        "data": "2026-10-10",
    }
    return AgencyCheck(**(values | extra))


def test_agencies_only_result_shows_what_the_agencies_found(api: TestClient, env: Env) -> None:
    env.factcheck.results = [agency_check()]
    env.analyzer.error = GeminiRateLimitedError("rpm")
    _, headers = env.login()

    body = api.post("/checks", json={"url": LINK}, headers=headers).json()

    assert body["modo"] == "agencias"
    assert body["veredito"] == "nao_verificavel"  # sem IA, nada de veredito próprio
    assert body["confianca"] == 0
    assert body["analise_anterior"] is False
    assert [c["agencia"] for c in body["checagens_agencias"]] == ["Agência Lupa"]
    assert "indisponível" in body["resumo"]
    assert body["pontos_suspeitos"] == []
    # O link público mostra o mesmo resultado.
    assert api.get(f"/checks/{body['id']}").json()["modo"] == "agencias"
    # E não vai para "Últimas".
    latest = api.get("/checks/latest").json()["items"]
    assert body["id"] not in [item["id"] for item in latest]


def test_agencies_only_without_agency_checks_still_answers(api: TestClient, env: Env) -> None:
    env.analyzer.error = GeminiRateLimitedError("rpm")
    _, headers = env.login()

    body = api.post("/checks", json={"texto": MSG}, headers=headers).json()

    assert body["modo"] == "agencias"
    assert body["checagens_agencias"] == []
    assert "indisponível" in body["resumo"]


def test_agencies_only_is_not_served_as_cache(api: TestClient, env: Env) -> None:
    env.analyzer.error = GeminiRateLimitedError("rpm")
    _, headers = env.login()
    first = api.post("/checks", json={"url": LINK}, headers=headers).json()

    env.analyzer.error = None  # o Gemini voltou
    again = api.post("/checks", json={"url": LINK}, headers=headers).json()

    assert again["modo"] == "ia" and again["cache"] is False and again["id"] != first["id"]
    assert len(env.analyzer.inputs) == 2  # a 1ª falhou (limite por minuto), a 2ª analisou
    assert env.checks.saved[-1].prompt_version == "v-teste"


def test_agencies_only_is_never_the_previous_analysis(api: TestClient, env: Env) -> None:
    env.analyzer.error = GeminiRateLimitedError("rpm")
    _, headers = env.login()
    api.post("/checks", json={"url": LINK}, headers=headers)

    env.analyzer.error = GeminiQuotaExhaustedError()
    body = api.post("/checks", json={"url": LINK}, headers=headers).json()

    # Sem análise de IA anterior: modo agências de novo (não "análise anterior").
    assert body["modo"] == "agencias" and body["analise_anterior"] is False


def test_previous_ai_analysis_wins_over_agencies_only(api: TestClient, env: Env) -> None:
    _, headers = env.login()
    old = api.post("/checks", json={"url": LINK}, headers=headers).json()

    new_prompt_version(env)
    env.analyzer.error = GeminiUnavailableError("modelos lentos")
    body = api.post("/checks", json={"url": LINK}, headers=headers).json()

    assert body["id"] == old["id"] and body["analise_anterior"] is True
    assert body["modo"] == "ia"


def test_agencies_only_does_not_use_the_ai_check_limit(api: TestClient, env: Env) -> None:
    env.settings = settings(checks_per_hour=1)
    env.analyzer.error = GeminiRateLimitedError("rpm")
    _, headers = env.login()

    for index in range(3):
        response = api.post("/checks", json={"url": f"{LINK}-{index}"}, headers=headers)
        assert response.status_code == 200
        assert response.json()["modo"] == "agencias"


# ---------- revisões do modo "só agências" (12/10/2026) ----------


def test_agencies_only_respects_the_lookup_limit(api: TestClient, env: Env) -> None:
    # Sem IA o pedido vira consulta, mas não ilimitada: Gemini fora não abre a porteira.
    env.settings = settings(lookups_per_hour=2)
    env.analyzer.error = GeminiRateLimitedError("rpm")
    _, headers = env.login()

    for index in range(2):
        ok = api.post("/checks", json={"url": f"{LINK}-{index}"}, headers=headers)
        assert ok.status_code == 200 and ok.json()["modo"] == "agencias"

    blocked = api.post("/checks", json={"url": f"{LINK}-9"}, headers=headers)
    assert blocked.status_code == 429
    assert error_code(blocked) == "limite_consultas"
    assert int(blocked.headers["Retry-After"]) > 0
    assert env.checks.claimed == {}
    assert len(env.checks.saved) == 2  # o bloqueado não salvou página pública


def test_content_refused_by_gemini_is_not_an_agencies_page(api: TestClient, env: Env) -> None:
    # Recusa de segurança não passa com "tente de novo": continua 503 e devolve o limite.
    env.analyzer.error = GeminiContentBlockedError("finish_reason=SAFETY")
    _, headers = env.login()

    response = api.post("/checks", json={"texto": MSG}, headers=headers)

    assert response.status_code == 503
    assert error_code(response) == "checagem_indisponivel"
    assert env.checks.requests == {}
    assert env.checks.saved == []


def test_agencies_only_says_when_the_fact_check_api_failed(api: TestClient, env: Env) -> None:
    env.factcheck.error = TimeoutError()
    env.analyzer.error = GeminiRateLimitedError("rpm")
    _, headers = env.login()

    body = api.post("/checks", json={"url": LINK}, headers=headers).json()

    assert body["modo"] == "agencias"
    assert "não conseguimos consultar as agências" in body["resumo"]
    assert "não encontramos" not in body["resumo"]


def test_ai_mode_records_and_shows_that_the_agency_search_failed(api: TestClient, env: Env) -> None:
    # Bug de honestidade (auditoria 19/10): com IA, a busca falhada virava "não encontramos".
    env.factcheck.error = TimeoutError()
    _, headers = env.login()

    body = api.post("/checks", json={"url": LINK}, headers=headers).json()

    assert body["modo"] == "ia"
    assert body["busca_agencias"] == "falhou"
    assert env.checks.saved[-1].result["busca_agencias"] == "falhou"
    page = api.get(f"/checks/{body['id']}").json()
    assert page["busca_agencias"] == "falhou"


def test_quota_429_from_the_real_client_is_recorded_as_a_failed_search(
    api: TestClient, env: Env
) -> None:
    # O cliente HTTP de verdade (rede simulada): 429 de cota não pode virar "não encontramos".
    import httpx

    from app.services.factcheck import HttpFactCheckClient

    transport = httpx.MockTransport(lambda _: httpx.Response(429, json={"error": {"code": 429}}))
    env.factcheck = HttpFactCheckClient("chave-de-teste", transport=transport)  # type: ignore[assignment]
    _, headers = env.login()

    body = api.post("/checks", json={"url": LINK}, headers=headers).json()

    assert body["busca_agencias"] == "falhou"
    assert env.checks.saved[-1].result["busca_agencias"] == "falhou"


def test_ai_mode_records_a_successful_agency_search(api: TestClient, env: Env) -> None:
    _, headers = env.login()

    body = api.post("/checks", json={"url": LINK}, headers=headers).json()

    assert body["busca_agencias"] == "ok"
    assert env.checks.saved[-1].result["busca_agencias"] == "ok"


def test_agencies_only_records_the_failed_search(api: TestClient, env: Env) -> None:
    env.factcheck.error = TimeoutError()
    env.analyzer.error = GeminiRateLimitedError("rpm")
    _, headers = env.login()

    body = api.post("/checks", json={"url": LINK}, headers=headers).json()

    assert body["modo"] == "agencias"
    assert body["busca_agencias"] == "falhou"


def test_failed_search_analysis_is_reused_only_for_a_short_time(api: TestClient, env: Env) -> None:
    # Busca falhada não fica 24 h em cache: depois de 30 min a próxima checagem refaz a busca
    # (antes disso reaproveita, para não gastar a cota do Gemini a cada pedido de link viral).
    env.factcheck.error = TimeoutError()
    _, headers = env.login()
    first = api.post("/checks", json={"url": LINK}, headers=headers).json()

    again = api.post("/checks", json={"url": LINK}, headers=headers).json()
    assert again["id"] == first["id"] and again["cache"] is True

    env.checks.advance(timedelta(minutes=31))
    env.factcheck.error = None
    fresh = api.post("/checks", json={"url": LINK}, headers=headers).json()
    assert fresh["id"] != first["id"]
    assert fresh["busca_agencias"] == "ok"


def test_successful_search_analysis_keeps_the_normal_cache(api: TestClient, env: Env) -> None:
    _, headers = env.login()
    first = api.post("/checks", json={"url": LINK}, headers=headers).json()

    env.checks.advance(timedelta(hours=2))
    again = api.post("/checks", json={"url": LINK}, headers=headers).json()

    assert again["id"] == first["id"] and again["cache"] is True


def test_agencies_only_save_failure_gives_the_limit_back(api: TestClient, env: Env) -> None:
    env.analyzer.error = GeminiRateLimitedError("rpm")

    async def broken_save(_: object) -> object:
        raise ConnectionError("banco fora")

    env.checks.save_result = broken_save  # type: ignore[method-assign]
    _, headers = env.login()

    response = api.post("/checks", json={"url": LINK}, headers=headers)

    assert response.status_code == 500
    assert env.checks.requests == {}  # o pedido não conta em limite nenhum
    assert env.checks.claimed == {}


# ---------- pools de modelos (12/10/2026) ----------


def test_the_model_that_answered_is_recorded_and_logged(
    api: TestClient, env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.services.check_service")
    env.analyzer.model = "gemini-3.6-flash"  # o 3º do pool respondeu
    _, headers = env.login()

    api.post("/checks", json={"url": LINK}, headers=headers)

    assert env.checks.saved[0].model == "gemini-3.6-flash"
    assert env.checks.saved[0].result["model"] == "gemini-3.6-flash"
    assert _timing(caplog).model == "gemini-3.6-flash"


# ---------- link que é a própria checagem de uma agência (23/10/2026) ----------

ESTADAO = "https://www.estadao.com.br/estadao-verifica/frude-transmissao-alteracao-votos-leicoes/"


def _estadao_review(
    alegacao: str | None = "Transmissão dos dados permitiu alterar votos",
) -> AgencyCheck:
    return AgencyCheck(
        agencia="Estadão",
        titulo="É falso que transmissão de dados permitiu alterar votos",
        veredito="Falso",
        url=ESTADAO,
        data="2026-10-22",
        alegacao=alegacao,
    )


@pytest.mark.parametrize(
    "submitted",
    [
        ESTADAO,
        ESTADAO.replace("https://www.", "https://"),
        ESTADAO.rstrip("/"),
        ESTADAO + "?utm_source=whatsapp&utm_medium=social",
        ESTADAO + "#comentarios",
    ],
)
def test_link_that_is_the_agency_check_itself_skips_the_ai(
    api: TestClient, env: Env, submitted: str
) -> None:
    env.factcheck.results = [_estadao_review()]
    _, headers = env.login()

    response = api.post("/checks", json={"url": submitted}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["modo"] == "checagem_de_agencia"
    assert body["checagem_de_agencia"] == {
        "agencia": "Estadão",
        "url": ESTADAO,
        "classificacao": "Falso",
        "alegacao": "Transmissão dos dados permitiu alterar votos",
        "titulo": "É falso que transmissão de dados permitiu alterar votos",
    }
    assert body["checagens_agencias"] == []
    # Gemini não é chamado e a cota não é gasta; o pedido vira consulta (como o modo agências).
    assert env.analyzer.inputs == []
    assert env.checks.gemini_used == 0
    assert [hit for _, hit in env.checks.requests.values()] == [True]
    assert env.checks.claimed == {}
    # Desde 26/10 entra em "Últimas" pela regra de sempre (mesmo site, sem ofensa).
    assert env.checks.saved[0].listed is True
    # A página pública mostra o mesmo.
    page = api.get(f"/checks/{body['id']}").json()
    assert page["modo"] == "checagem_de_agencia"
    assert page["checagem_de_agencia"] == body["checagem_de_agencia"]


def test_agency_page_is_logged_apart_from_gemini_outages(
    api: TestClient, env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.services.check_service")
    env.factcheck.results = [_estadao_review()]
    _, headers = env.login()

    api.post("/checks", json={"url": ESTADAO}, headers=headers)

    assert _timing(caplog).outcome == "checagem_de_agencia"
    assert not [r for r in caplog.records if r.getMessage() == "agencies_only_mode"]


def test_agency_check_without_claim_reviewed_shows_only_the_title(
    api: TestClient, env: Env
) -> None:
    env.factcheck.results = [_estadao_review(alegacao=None)]
    _, headers = env.login()

    body = api.post("/checks", json={"url": ESTADAO}, headers=headers).json()

    assert body["modo"] == "checagem_de_agencia"
    assert body["checagem_de_agencia"]["alegacao"] is None
    assert body["checagem_de_agencia"]["titulo"] == (
        "É falso que transmissão de dados permitiu alterar votos"
    )
    assert env.analyzer.inputs == []


def test_link_different_from_the_agency_check_keeps_the_ai_flow(api: TestClient, env: Env) -> None:
    env.factcheck.results = [_estadao_review()]
    _, headers = env.login()

    # Site comum (desde 26/10, outra página de agência conhecida vira "link de uma agência").
    body = api.post(
        "/checks",
        json={"url": "https://www.estadao.com.br/politica/outra-materia/"},
        headers=headers,
    ).json()

    assert body["modo"] == "ia"
    assert body.get("checagem_de_agencia") is None
    assert len(env.analyzer.inputs) == 1


def test_pasted_text_with_the_agency_link_keeps_the_ai_flow(api: TestClient, env: Env) -> None:
    env.factcheck.results = [_estadao_review()]
    _, headers = env.login()

    body = api.post("/checks", json={"texto": f"{MSG} {ESTADAO}"}, headers=headers).json()

    assert body["modo"] == "ia"
    assert len(env.analyzer.inputs) == 1


# ---------- "Últimas" com checagens de agência (26/10/2026) ----------

ESTADAO_PIX = (
    "https://www.estadao.com.br/estadao-verifica/homem-video-nao-foi-preso-500-8-de-janeiro/"
)
AOS_FATOS_NOVA = "https://www.aosfatos.org/noticias/checagem-recente-fora-do-indice/"


def _seed(
    env: Env,
    *,
    url: str,
    domain: str,
    title: str | None,
    veredito: str,
    result: dict[str, Any],
    listed: bool,
    prompt_version: str,
    minutes_ago: int = 0,
) -> str:
    """Grava uma checagem como ficou no banco (sem passar pelo fluxo de hoje)."""
    public_id = new_public_id()
    stored = StoredCheck(
        public_id=public_id,
        kind="link",
        url=url,
        domain=domain,
        title=title,
        published_at=None,
        texto=None,
        veredito=veredito,
        confianca=60,
        result=result,
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        prompt_version=prompt_version,
    )
    ref = SubjectRef("link", len(env.checks.rows) + 1000)
    env.checks.rows.append(_FakeRow(ref, stored, listed, uuid4(), prompt_version))
    return public_id


def _latest(api: TestClient) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in api.get("/checks/latest").json()["items"]}


def test_latest_lists_a_new_agency_check_with_the_agency_badge(api: TestClient, env: Env) -> None:
    env.factcheck.results = [
        AgencyCheck(
            agencia="Estadão",
            titulo="Homem que aparece em vídeo não foi preso por fazer Pix para 8 de ...",
            veredito="Enganoso",
            url=ESTADAO_PIX,
            alegacao="Idoso que fez pix de R$ 500 é preso a pedido de Alexandre de Moraes",
        )
    ]
    _, headers = env.login()
    body = api.post("/checks", json={"url": ESTADAO_PIX}, headers=headers).json()
    assert body["modo"] == "checagem_de_agencia"

    item = _latest(api)[body["id"]]
    assert item["modo"] == "checagem_de_agencia"
    assert item["agencia"] == "Estadão"
    assert item["classificacao"] == "Enganoso"
    assert env.analyzer.inputs == []


def test_latest_lists_old_agency_checks_saved_unlisted(api: TestClient, env: Env) -> None:
    # Caso real ataxpy4sp8e4 (23/10): gravada pelo caminho sem IA, com listed = false.
    pid = _seed(
        env,
        url=ESTADAO_PIX,
        domain="estadao.com.br",
        title=None,
        veredito="nao_verificavel",
        result={
            "modo": "checagem_de_agencia",
            "checagem_de_agencia": {
                "agencia": "Estadão",
                "titulo": "Homem que aparece em vídeo não foi preso por fazer Pix para 8 de ...",
                "veredito": "Enganoso",
                "url": ESTADAO_PIX,
                "alegacao": "Idoso que fez pix de R$ 500 é preso a pedido de Alexandre de Moraes",
            },
        },
        listed=False,
        prompt_version="agencias",
    )
    item = _latest(api)[pid]
    assert (item["modo"], item["agencia"], item["classificacao"]) == (
        "checagem_de_agencia",
        "Estadão",
        "Enganoso",
    )
    assert item["title"]  # sem título do link: usa o da checagem


def test_latest_recognises_an_old_ai_check_of_an_agency_page(api: TestClient, env: Env) -> None:
    # Caso real sxyv5awc1grr (22/10): IA chamada, "não verificável", listada; a própria checagem
    # citada. A página já mostra a checagem de agência; a lista passa a mostrar o mesmo.
    review = {
        "agencia": "Estadão",
        "titulo": "Justiça Eleitoral tem sistema de segurança que impede fraude na ...",
        "veredito": "Falso",
        "url": ESTADAO,
    }
    pid = _seed(
        env,
        url=ESTADAO.rstrip("/"),
        domain="estadao.com.br",
        title="Justiça Eleitoral tem sistema de segurança | Estadão",
        veredito="nao_verificavel",
        result={"checagens_agencias": [review], "palavras_chave": ["justica", "eleitoral"]},
        listed=True,
        prompt_version=env.settings.limits.prompt_version,
    )
    item = _latest(api)[pid]
    assert item["modo"] == "checagem_de_agencia"
    assert (item["agencia"], item["classificacao"]) == ("Estadão", "Falso")
    assert api.get(f"/checks/{pid}").json()["modo"] == "checagem_de_agencia"  # igual à página


def test_known_agency_link_without_claim_review_is_shown_without_verdict(
    api: TestClient, env: Env
) -> None:
    env.factcheck.results = []  # fora do índice do Google
    _, headers = env.login()

    body = api.post("/checks", json={"url": AOS_FATOS_NOVA}, headers=headers).json()

    assert body["modo"] == "checagem_de_agencia"
    page = body["checagem_de_agencia"]
    assert page["agencia"] == "Aos Fatos"
    assert page["classificacao"] is None  # nada inventado
    assert page["alegacao"] is None
    assert body["resumo"] == (
        "Este link é de uma agência de checagem (Aos Fatos). "
        "Leia a checagem completa no site da agência."
    )
    assert env.analyzer.inputs == []  # sem IA
    assert env.checks.gemini_used == 0
    item = _latest(api)[body["id"]]
    assert (item["modo"], item["agencia"], item["classificacao"]) == (
        "checagem_de_agencia",
        "Aos Fatos",
        None,
    )


def test_known_agency_link_with_claim_review_keeps_the_agency_rating(
    api: TestClient, env: Env
) -> None:
    env.factcheck.results = [
        AgencyCheck(agencia="Aos Fatos", titulo="T", veredito="falso", url=AOS_FATOS_NOVA)
    ]
    _, headers = env.login()
    body = api.post("/checks", json={"url": AOS_FATOS_NOVA}, headers=headers).json()
    assert body["checagem_de_agencia"]["classificacao"] == "falso"
    assert body["resumo"] == "Este link é uma checagem publicada por Aos Fatos."


def test_common_link_keeps_the_ai_flow_and_the_usual_list_item(api: TestClient, env: Env) -> None:
    _, headers = env.login()
    body = api.post("/checks", json={"url": LINK}, headers=headers).json()
    assert body["modo"] == "ia"
    assert len(env.analyzer.inputs) == 1
    item = _latest(api)[body["id"]]
    assert item["modo"] == "ia"
    assert item["agencia"] is None and item["classificacao"] is None
    assert item["veredito"] == body["veredito"]


def test_pasted_text_never_appears_in_latest(api: TestClient, env: Env) -> None:
    _, headers = env.login()
    body = api.post("/checks", json={"texto": MSG}, headers=headers).json()
    assert body["id"] not in _latest(api)


def test_agency_link_with_an_offensive_title_stays_off_latest(api: TestClient, env: Env) -> None:
    env.fetcher.html = article_html("Que porra de eleição")
    env.factcheck.results = []
    _, headers = env.login()
    body = api.post("/checks", json={"url": AOS_FATOS_NOVA}, headers=headers).json()
    assert body["modo"] == "checagem_de_agencia"  # a página continua acessível pelo link
    assert env.checks.saved[0].listed is False
    assert body["id"] not in _latest(api)


def test_old_unlisted_agency_check_with_an_offensive_title_stays_off_latest(
    api: TestClient, env: Env
) -> None:
    pid = _seed(
        env,
        url=ESTADAO_PIX,
        domain="estadao.com.br",
        title=None,
        veredito="nao_verificavel",
        result={
            "modo": "checagem_de_agencia",
            "checagem_de_agencia": {
                "agencia": "Estadão",
                "titulo": "Que porra de eleição",
                "veredito": "Falso",
                "url": ESTADAO_PIX,
            },
        },
        listed=False,
        prompt_version="agencias",
    )
    assert pid not in _latest(api)


def test_agency_link_when_the_fact_check_search_failed_records_the_failure(
    api: TestClient, env: Env
) -> None:
    env.factcheck.error = FactCheckUnavailableError()
    _, headers = env.login()
    body = api.post("/checks", json={"url": AOS_FATOS_NOVA}, headers=headers).json()
    assert body["modo"] == "checagem_de_agencia"
    assert body["checagem_de_agencia"]["classificacao"] is None
    assert body["busca_agencias"] == "falhou"  # não se afirma que não havia checagem
    assert env.analyzer.inputs == []


def test_agency_home_page_is_not_an_agency_check(api: TestClient, env: Env) -> None:
    _, headers = env.login()
    body = api.post("/checks", json={"url": "https://www.aosfatos.org/"}, headers=headers).json()
    assert body["modo"] == "ia"
