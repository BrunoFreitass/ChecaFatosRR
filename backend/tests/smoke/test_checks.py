import base64
import json
import re
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import TokenVerifier, get_token_verifier
from app.repositories.checks import Limits, StoredCheck, SubjectRef, new_public_id
from app.repositories.profiles import Profile
from app.routers.checks import get_check_repository, get_check_settings, get_check_tools
from app.routers.profiles import get_profile_repository
from app.services.check_service import CheckSettings, CheckTools
from app.services.factcheck import AgencyCheck
from app.services.fetcher import PageUnreadableError
from app.services.gemini import GeminiQuotaExhaustedError, GeminiUnavailableError
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
    return CheckSettings(limits=Limits(**values), daily_gemini_checks=daily, model="gemini-teste")


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

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        for hook in self.on_sleep:
            hook()

    def tools(self) -> CheckTools:
        return CheckTools(
            fetcher=self.fetcher, factcheck=self.factcheck, analyzer=self.analyzer, sleep=self.sleep
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
        AgencyCheck("Aos Fatos", "Não houve adiamento", "Falso", "https://aosfatos.org/x")
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


def test_daily_quota_exhausted_does_not_spend_user_limit(api: TestClient, env: Env) -> None:
    env.settings = settings(daily=0)
    _, headers = env.login()
    response = api.post("/checks", json={"url": LINK}, headers=headers)
    assert response.status_code == 503
    assert error_code(response) == "checagem_esgotada_hoje"
    assert env.checks.requests == {}
    assert env.checks.claimed == {}
    assert env.analyzer.inputs == []


def test_gemini_failure_refunds_the_user_limit(api: TestClient, env: Env) -> None:
    env.analyzer.error = GeminiUnavailableError("api 429")
    _, headers = env.login()
    response = api.post("/checks", json={"texto": MSG}, headers=headers)
    assert response.status_code == 503
    assert error_code(response) == "checagem_indisponivel"
    assert env.checks.requests == {}
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
        env.checks.rows.append(_FakeRow(ref, stored, True, uuid4()))

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
    original_claim = env.checks.claim

    async def claim_after_other_finished(subject: SubjectRef) -> datetime | None:
        claimed = await original_claim(subject)
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
        env.checks.rows.append(_FakeRow(subject, stored, True, uuid4()))
        return claimed

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
    assert response.status_code == 503
    assert error_code(response) == "checagem_esgotada_hoje"
    assert env.checks.requests == {}  # não gastou o limite do usuário
