"""Limite por minuto do Gemini (nível gratuito: RPM 5 e RPD 20 por modelo, AI Studio 12/10/2026).

Regra (decisão do usuário): respeitar o RPM sem esperar. Modelo cheio é pulado sem ser
chamado; 429 por minuto do Google passa ao próximo modelo sem repetir; todos cheios = erro na
hora (a checagem responde no modo "só agências"), antes de gastar a cota diária. Só mocks.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from app.services import gemini
from app.services.gemini import (
    GeminiAnalyzer,
    GeminiRateLimitedError,
    Generated,
    RpmLimiter,
    google_generate,
)
from tests.unit.test_gemini_client import ANSWER, FAKE_KEY, api_error, data, text_response

pytestmark = pytest.mark.anyio

PRIMARY, BACKUP = "gemini-3.8-flash", "gemini-3.7-flash"
PER_MINUTE = [{"violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel"}]}]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Models:
    def __init__(self, scripts: dict[str, list[Any]]) -> None:
        self.scripts = scripts
        self.calls: list[str] = []

    async def generate_content(self, *, model: str, **_: Any) -> Any:
        self.calls.append(model)
        step = self.scripts[model].pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def install(monkeypatch: pytest.MonkeyPatch, scripts: dict[str, list[Any]]) -> Models:
    models = Models(scripts)

    class FakeClient:
        def __init__(self, **_: Any) -> None:
            self.aio = SimpleNamespace(models=models)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("google.genai.Client", FakeClient)
    monkeypatch.setattr(gemini.asyncio, "sleep", no_sleep)
    return models


# ---------- limitador ----------


def test_limiter_allows_rpm_calls_per_rolling_minute() -> None:
    clock = Clock()
    limiter = RpmLimiter(2, clock=clock)

    assert limiter.try_acquire("m") and limiter.try_acquire("m")
    assert not limiter.try_acquire("m")
    assert limiter.try_acquire("outro")  # cada modelo tem o seu limite

    clock.now = 59.9
    assert not limiter.try_acquire("m")
    clock.now = 60.0
    assert limiter.try_acquire("m")


def test_limiter_saturate_blocks_the_model_for_a_minute() -> None:
    clock = Clock()
    limiter = RpmLimiter(5, clock=clock)

    limiter.saturate("m")  # 429 por minuto do Google: o minuto dele acabou

    assert not limiter.has_room("m")
    clock.now = 60.0
    assert limiter.has_room("m")


def test_zero_rpm_means_no_local_limit() -> None:
    limiter = RpmLimiter(0)
    assert all(limiter.try_acquire("m") for _ in range(100))


# ---------- cadeia ----------


async def test_per_minute_429_moves_to_the_next_model_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = install(
        monkeypatch,
        {
            PRIMARY: [api_error(429, "rate", PER_MINUTE)],
            BACKUP: [text_response(ANSWER), text_response(ANSWER)],
        },
    )
    chain = google_generate(FAKE_KEY, [PRIMARY, BACKUP], rpm_per_model=5)

    assert await chain("prompt") == Generated(ANSWER, BACKUP)
    # O minuto do primário acabou: a próxima checagem nem tenta nele.
    assert await chain("prompt") == Generated(ANSWER, BACKUP)
    assert models.calls == [PRIMARY, BACKUP, BACKUP]


async def test_full_model_is_skipped_without_a_call(monkeypatch: pytest.MonkeyPatch) -> None:
    models = install(
        monkeypatch,
        {PRIMARY: [text_response(ANSWER)], BACKUP: [text_response(ANSWER)]},
    )
    chain = google_generate(FAKE_KEY, [PRIMARY, BACKUP], rpm_per_model=1)

    assert await chain("prompt") == Generated(ANSWER, PRIMARY)
    assert await chain("prompt") == Generated(ANSWER, BACKUP)
    with pytest.raises(GeminiRateLimitedError):
        await chain("prompt")  # os dois no limite: erro na hora, sem chamar o Google
    assert models.calls == [PRIMARY, BACKUP]
    assert chain.rate_limited_now()


async def test_single_model_per_minute_429_is_rate_limited_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = install(monkeypatch, {PRIMARY: [api_error(429, "rate", PER_MINUTE)]})

    with pytest.raises(GeminiRateLimitedError):
        await google_generate(FAKE_KEY, [PRIMARY], rpm_per_model=5)("prompt")
    assert models.calls == [PRIMARY]


async def test_analyzer_does_not_spend_the_daily_quota_when_every_model_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, {PRIMARY: [text_response(ANSWER)]})
    chain = google_generate(FAKE_KEY, [PRIMARY], rpm_per_model=1)
    await chain("prompt")
    reserved: list[int] = []

    async def reserve() -> bool:
        reserved.append(1)
        return True

    with pytest.raises(GeminiRateLimitedError):
        await GeminiAnalyzer(chain).analyze(data(), reserve)
    assert reserved == []


def test_rate_limited_is_a_kind_of_unavailable() -> None:
    # Quem só conhece "indisponível" continua funcionando.
    assert issubclass(GeminiRateLimitedError, gemini.GeminiUnavailableError)


def test_default_daily_checks_and_rpm_follow_the_free_tier() -> None:
    from app.config import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    # RPD 20 por modelo, 2 modelos = 40: 36 deixa margem (12/10/2026).
    assert settings.gemini_daily_checks == 36
    assert settings.gemini_rpm_per_model == 5


# ---------- revisão: cota diária devolvida quando nenhum modelo foi chamado ----------


async def test_daily_unit_is_given_back_when_no_model_was_called() -> None:
    # Corrida: o pré-check viu vaga, mas outra checagem ocupou o minuto antes da chamada.
    async def generate(_: str) -> str:
        raise GeminiRateLimitedError("todos no limite", called=False)

    reserved: list[int] = []
    released: list[int] = []

    async def reserve() -> bool:
        reserved.append(1)
        return True

    async def release() -> None:
        released.append(1)

    with pytest.raises(GeminiRateLimitedError):
        await GeminiAnalyzer(generate).analyze(data(), reserve, release)
    assert (reserved, released) == ([1], [1])


async def test_daily_unit_is_kept_when_google_was_called(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, {PRIMARY: [api_error(429, "rate", PER_MINUTE)]})
    chain = google_generate(FAKE_KEY, [PRIMARY], rpm_per_model=5)
    released: list[int] = []

    async def reserve() -> bool:
        return True

    async def release() -> None:
        released.append(1)

    with pytest.raises(GeminiRateLimitedError) as caught:
        await GeminiAnalyzer(chain).analyze(data(), reserve, release)
    assert caught.value.called is True  # o Google recebeu (e contou) a requisição
    assert released == []


async def test_skipped_full_models_are_not_a_call(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, {PRIMARY: [text_response(ANSWER)]})
    chain = google_generate(FAKE_KEY, [PRIMARY], rpm_per_model=1)
    await chain("prompt")

    with pytest.raises(GeminiRateLimitedError) as caught:
        await chain("prompt")
    assert caught.value.called is False
