"""Fallback de modelos do Gemini (GEMINI_MODEL_FALLBACKS).

Em 429 de cota DIÁRIA do modelo atual, tenta o próximo da lista; o log diz qual respondeu.
Só mocks: o SDK é simulado e nenhum teste chama o Google.
"""

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.routers import checks as checks_router
from app.services import gemini
from app.services.gemini import (
    SLOW_MODEL_COOLDOWN_SECONDS,
    GeminiAnalyzer,
    GeminiModelSlowError,
    GeminiQuotaExhaustedError,
    GeminiUnavailableError,
    Generated,
    google_generate,
    seconds_until_quota_reset,
    with_model_fallback,
    with_retries,
)
from tests.unit.test_gemini_client import ANSWER, FAKE_KEY, api_error, data, text_response

pytestmark = pytest.mark.anyio

PRIMARY, BACKUP = "gemini-3.8-flash", "gemini-3.7-flash"
DAILY = [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]
PER_MINUTE = [{"violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel"}]}]


def daily_quota() -> Exception:
    return api_error(429, "quota diária", DAILY)


class ScriptedModels:
    """generate_content simulado: um roteiro por modelo e o registro da ordem das chamadas."""

    def __init__(self, scripts: dict[str, list[Any]]) -> None:
        self.scripts = scripts
        self.calls: list[str] = []

    async def generate_content(self, *, model: str, **_: Any) -> Any:
        self.calls.append(model)
        step = self.scripts[model].pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


async def _no_sleep(_: float) -> None:
    return None


def install(
    monkeypatch: pytest.MonkeyPatch, scripts: dict[str, list[Any]]
) -> tuple[ScriptedModels, list[dict[str, Any]]]:
    models = ScriptedModels(scripts)
    clients: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            clients.append(kwargs)
            self.aio = SimpleNamespace(models=models)

    monkeypatch.setattr("google.genai.Client", FakeClient)
    monkeypatch.setattr(gemini.asyncio, "sleep", _no_sleep)
    return models, clients


def models_logged(caplog: pytest.LogCaptureFixture, message: str) -> list[str]:
    return [getattr(r, "model", None) for r in caplog.records if r.getMessage() == message]


# ---------- configuração ----------


def test_default_chain_is_38_then_37() -> None:
    settings = Settings()
    assert settings.gemini_models == (PRIMARY, BACKUP)
    assert not any(
        tag in model for model in settings.gemini_models for tag in ("latest", "preview", "exp")
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" a-1 , b-2,,a-1 ", ("a-1", "b-2")), ("so-um", ("so-um",))],
)
def test_chain_parsing(raw: str, expected: tuple[str, ...]) -> None:
    assert Settings(gemini_model_fallbacks=raw).gemini_models == expected


@pytest.mark.parametrize("raw", ["", " , ", "modelo com espaco", "a;b", "models/../x"])
def test_invalid_chain_is_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        Settings(gemini_model_fallbacks=raw)


def test_chain_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_MODEL_FALLBACKS", "x-1,y-2")
    assert Settings().gemini_models == ("x-1", "y-2")


def test_router_builds_the_chain_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, ...]] = []

    def fake_google_generate(_key: str, models: tuple[str, ...], **_: Any) -> Any:
        seen.append(models)
        return _no_sleep

    monkeypatch.setattr(checks_router, "google_generate", fake_google_generate)
    checks_router._analyzer.cache_clear()
    try:
        settings = Settings(gemini_api_key="k", gemini_model_fallbacks="a-1,b-2")
        checks_router.get_check_tools(settings)
        assert seen == [("a-1", "b-2")]
        # Sem modelo informado pelo analisador, a checagem grava o primeiro da cadeia.
        assert checks_router.get_check_settings(settings).model == "a-1"
    finally:
        checks_router._analyzer.cache_clear()


# ---------- fallback com o SDK simulado ----------


async def test_daily_quota_on_primary_falls_back_to_next_model(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    models, clients = install(
        monkeypatch, {PRIMARY: [daily_quota()], BACKUP: [text_response(ANSWER)]}
    )
    caplog.set_level(logging.INFO, logger=gemini.logger.name)

    result = await google_generate(FAKE_KEY, [PRIMARY, BACKUP])("prompt")

    assert result == Generated(ANSWER, BACKUP)
    assert models.calls == [PRIMARY, BACKUP]
    assert len(clients) == 1  # um cliente do SDK para a cadeia inteira
    assert models_logged(caplog, "gemini_api_error") == [PRIMARY]
    assert models_logged(caplog, "gemini_model_quota_exhausted") == [PRIMARY]
    assert models_logged(caplog, "gemini_model_responded") == [BACKUP]


async def test_primary_success_does_not_touch_backup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    models, _ = install(monkeypatch, {PRIMARY: [text_response(ANSWER)], BACKUP: []})
    caplog.set_level(logging.INFO, logger=gemini.logger.name)

    result = await google_generate(FAKE_KEY, [PRIMARY, BACKUP])("prompt")

    assert result == Generated(ANSWER, PRIMARY)
    assert models.calls == [PRIMARY]
    assert models_logged(caplog, "gemini_model_responded") == [PRIMARY]


async def test_all_models_out_of_daily_quota_is_quota_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, _ = install(monkeypatch, {PRIMARY: [daily_quota()], BACKUP: [daily_quota()]})

    with pytest.raises(GeminiQuotaExhaustedError):
        await google_generate(FAKE_KEY, [PRIMARY, BACKUP])("prompt")
    assert models.calls == [PRIMARY, BACKUP]


@pytest.mark.parametrize(
    "error",
    [api_error(503, "high demand"), api_error(400, "invalid argument"), ConnectionResetError()],
)
async def test_other_errors_do_not_fall_back(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    # Só a cota diária troca de modelo: instabilidade e erro nosso seguem as regras de retry.
    models, _ = install(monkeypatch, {PRIMARY: [error], BACKUP: [text_response(ANSWER)]})

    with pytest.raises(GeminiUnavailableError):
        await google_generate(FAKE_KEY, [PRIMARY, BACKUP], max_retries=0)("prompt")
    assert models.calls == [PRIMARY]


async def test_per_minute_429_retries_the_same_model(monkeypatch: pytest.MonkeyPatch) -> None:
    models, _ = install(
        monkeypatch,
        {PRIMARY: [api_error(429, "rate", PER_MINUTE), text_response(ANSWER)], BACKUP: []},
    )

    result = await google_generate(FAKE_KEY, [PRIMARY, BACKUP])("prompt")

    assert result == Generated(ANSWER, PRIMARY)
    assert models.calls == [PRIMARY, PRIMARY]


# ---------- memória do modelo esgotado ----------


async def test_exhausted_model_is_skipped_until_the_quota_resets() -> None:
    now = [0.0]
    calls: list[str] = []
    primary_has_quota = [False]

    async def primary(_: str) -> str:
        calls.append("p")
        if not primary_has_quota[0]:
            raise GeminiQuotaExhaustedError
        return "do primário"

    async def backup(_: str) -> str:
        calls.append("b")
        return "do reserva"

    chain = with_model_fallback(
        [("p", primary), ("b", backup)], clock=lambda: now[0], seconds_until_reset=lambda: 100.0
    )

    assert await chain("x") == Generated("do reserva", "b")
    # Não gasta uma chamada (nem tempo) num modelo que já disse "acabou por hoje".
    assert await chain("x") == Generated("do reserva", "b")
    assert calls == ["p", "b", "b"]

    now[0] = 101.0  # virou o dia no Pacífico
    primary_has_quota[0] = True
    assert await chain("x") == Generated("do primário", "p")


async def test_every_model_known_exhausted_fails_without_calling() -> None:
    calls: list[str] = []

    async def exhausted(_: str) -> str:
        calls.append("x")
        raise GeminiQuotaExhaustedError

    chain = with_model_fallback(
        [("p", exhausted), ("b", exhausted)], clock=lambda: 0.0, seconds_until_reset=lambda: 100.0
    )
    with pytest.raises(GeminiQuotaExhaustedError):
        await chain("x")
    with pytest.raises(GeminiQuotaExhaustedError):
        await chain("x")
    assert calls == ["x", "x"]  # a segunda checagem não chamou o Google


@pytest.mark.parametrize(
    ("now", "seconds"),
    [
        (datetime(2026, 10, 9, 6, 30, tzinfo=UTC), 30 * 60),  # 23:30 PDT
        (datetime(2026, 12, 1, 7, 0, tzinfo=UTC), 60 * 60),  # 23:00 PST
        (datetime(2026, 10, 9, 7, 0, tzinfo=UTC), 24 * 60 * 60),  # meia-noite em ponto
    ],
)
def test_quota_resets_at_pacific_midnight(now: datetime, seconds: int) -> None:
    assert seconds_until_quota_reset(now) == seconds


# ---------- analisador ----------


async def test_check_result_records_the_model_that_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, {PRIMARY: [daily_quota()], BACKUP: [text_response(ANSWER)]})
    reserves: list[int] = []

    async def reserve() -> bool:
        reserves.append(1)
        return True

    result = await GeminiAnalyzer(google_generate(FAKE_KEY, [PRIMARY, BACKUP])).analyze(
        data(), reserve
    )

    assert result.model == BACKUP
    assert reserves == [1]  # trocar de modelo não gasta outra unidade da cota diária
    assert "model" not in result.to_json()  # não vai para a página pública


async def test_plain_text_generate_keeps_working() -> None:
    async def generate(_: str) -> str:
        return ANSWER

    result = await GeminiAnalyzer(generate).analyze(data())
    assert result.veredito == "nao_verificavel"
    assert result.model is None


async def test_all_models_exhausted_does_not_spend_our_daily_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, _ = install(monkeypatch, {PRIMARY: [daily_quota()], BACKUP: [daily_quota()]})
    analyzer = GeminiAnalyzer(google_generate(FAKE_KEY, [PRIMARY, BACKUP]))
    reserves: list[int] = []

    async def reserve() -> bool:
        reserves.append(1)
        return True

    with pytest.raises(GeminiQuotaExhaustedError):
        await analyzer.analyze(data(), reserve)
    # Próxima checagem: todos já sabidamente sem cota; falha sem reservar nem chamar o Google.
    with pytest.raises(GeminiQuotaExhaustedError):
        await analyzer.analyze(data(), reserve)
    assert reserves == [1]
    assert models.calls == [PRIMARY, BACKUP]


async def test_invalid_answer_then_every_model_out_of_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Decisão: a 1ª resposta veio fora do schema e, na 2ª, a cadeia inteira esgotou → "volte
    # amanhã". A unidade da 1ª tentativa fica gasta (houve chamada real ao Google).
    models, _ = install(
        monkeypatch,
        {PRIMARY: [text_response("isto não é JSON"), daily_quota()], BACKUP: [daily_quota()]},
    )
    reserves: list[int] = []

    async def reserve() -> bool:
        reserves.append(1)
        return True

    with pytest.raises(GeminiQuotaExhaustedError):
        await GeminiAnalyzer(google_generate(FAKE_KEY, [PRIMARY, BACKUP])).analyze(data(), reserve)
    assert reserves == [1, 1]
    assert models.calls == [PRIMARY, PRIMARY, BACKUP]


async def test_exhausted_model_is_rechecked_after_at_most_an_hour() -> None:
    now = [0.0]
    calls: list[str] = []

    async def primary(_: str) -> str:
        calls.append("p")
        if now[0] < 3600:
            raise GeminiQuotaExhaustedError
        return "voltou"

    async def backup(_: str) -> str:
        return "reserva"

    chain = with_model_fallback(
        [("p", primary), ("b", backup)],
        clock=lambda: now[0],
        seconds_until_reset=lambda: 20 * 3600.0,  # a meia-noite do Pacífico ainda está longe
    )
    assert await chain("x") == Generated("reserva", "b")
    now[0] = 3599.0
    assert await chain("x") == Generated("reserva", "b")
    assert calls == ["p"]
    now[0] = 3600.0  # ex.: o plano mudou no meio do dia
    assert await chain("x") == Generated("voltou", "p")


async def test_daily_quota_named_only_in_the_message_also_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, _ = install(
        monkeypatch,
        {
            PRIMARY: [api_error(429, "Quota exceeded: GenerateRequestsPerDayPerProjectPerModel")],
            BACKUP: [text_response(ANSWER)],
        },
    )
    assert await google_generate(FAKE_KEY, [PRIMARY, BACKUP])("prompt") == Generated(ANSWER, BACKUP)
    assert models.calls == [PRIMARY, BACKUP]


# ---------- modelo lento (504 de prazo repetido): troca em vez de insistir ----------
# Teste em produção de 10/10/2026: o 3.8-flash deu 504 "Deadline expired" em 24 s e de novo
# em 24 s; medido depois sem o nosso teto, não respondeu nem em 60 s. Uma 3ª tentativa no
# mesmo modelo só gastaria o orçamento; o próximo da cadeia ainda pode responder.


def deadline_504() -> Exception:
    return api_error(504, "Deadline expired before operation could complete.")


async def test_repeated_504_switches_to_the_next_model(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    models, _ = install(
        monkeypatch,
        {PRIMARY: [deadline_504(), deadline_504()], BACKUP: [text_response(ANSWER)]},
    )
    caplog.set_level(logging.INFO, logger=gemini.logger.name)

    result = await google_generate(FAKE_KEY, [PRIMARY, BACKUP])("prompt")

    assert result == Generated(ANSWER, BACKUP)
    assert models.calls == [PRIMARY, PRIMARY, BACKUP]  # sem 3ª tentativa no lento
    assert models_logged(caplog, "gemini_model_slow") == [PRIMARY]


async def test_a_single_504_retries_the_same_model(monkeypatch: pytest.MonkeyPatch) -> None:
    models, _ = install(monkeypatch, {PRIMARY: [deadline_504(), text_response(ANSWER)], BACKUP: []})

    result = await google_generate(FAKE_KEY, [PRIMARY, BACKUP])("prompt")

    assert result == Generated(ANSWER, PRIMARY)
    assert models.calls == [PRIMARY, PRIMARY]


async def test_attempts_cut_by_our_timeout_twice_also_switch() -> None:
    calls: list[str] = []

    async def hangs(_: str) -> str:
        calls.append("p")
        await asyncio.sleep(10)
        return "nunca"

    async def answers(_: str) -> str:
        calls.append("b")
        return "do reserva"

    chain = with_model_fallback(
        [
            ("p", with_retries(hangs, attempt_timeout=0.02, base_delay=0, model="p")),
            ("b", answers),
        ]
    )
    assert await chain("x") == Generated("do reserva", "b")
    assert calls == ["p", "p", "b"]


async def test_slow_model_is_skipped_for_a_while_then_tried_again() -> None:
    now = [0.0]
    calls: list[str] = []
    primary_slow = [True]

    async def primary(_: str) -> str:
        calls.append("p")
        if primary_slow[0]:
            raise GeminiModelSlowError("p")
        return "do primário"

    async def backup(_: str) -> str:
        calls.append("b")
        return "do reserva"

    chain = with_model_fallback([("p", primary), ("b", backup)], clock=lambda: now[0])

    assert await chain("x") == Generated("do reserva", "b")
    # As próximas checagens não pagam de novo ~50 s no modelo lento.
    assert await chain("x") == Generated("do reserva", "b")
    assert calls == ["p", "b", "b"]

    now[0] = SLOW_MODEL_COOLDOWN_SECONDS
    primary_slow[0] = False
    assert await chain("x") == Generated("do primário", "p")


async def test_every_model_slow_is_unavailable_not_out_of_quota() -> None:
    now = [0.0]
    calls: list[str] = []

    async def slow(_: str) -> str:
        calls.append("x")
        raise GeminiModelSlowError("x")

    chain = with_model_fallback([("p", slow), ("b", slow)], clock=lambda: now[0])
    with pytest.raises(GeminiUnavailableError):
        await chain("x")
    # Todos em espera: tenta mesmo assim (lento ainda é melhor que nenhum), sem "sem cota".
    assert not chain.all_exhausted()
    with pytest.raises(GeminiUnavailableError):
        await chain("x")
    assert calls == ["x", "x", "x", "x"]


async def test_without_retries_a_single_504_already_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GEMINI_MAX_RETRIES=0: não há "2ª vez" no mesmo modelo; o prazo estourado já troca.
    models, _ = install(monkeypatch, {PRIMARY: [deadline_504()], BACKUP: [text_response(ANSWER)]})

    result = await google_generate(FAKE_KEY, [PRIMARY, BACKUP], max_retries=0)("prompt")

    assert result == Generated(ANSWER, BACKUP)
    assert models.calls == [PRIMARY, BACKUP]


async def test_other_error_between_504s_restarts_the_count(monkeypatch: pytest.MonkeyPatch) -> None:
    # "Seguidas": 504, 503, 504 não são dois prazos seguidos; segue as regras de retry.
    models, _ = install(
        monkeypatch,
        {
            PRIMARY: [deadline_504(), api_error(503, "high demand"), deadline_504()],
            BACKUP: [text_response(ANSWER)],
        },
    )

    with pytest.raises(GeminiUnavailableError) as caught:
        await google_generate(FAKE_KEY, [PRIMARY, BACKUP])("prompt")
    assert not isinstance(caught.value, GeminiModelSlowError)
    assert models.calls == [PRIMARY, PRIMARY, PRIMARY]


async def test_sdk_read_timeouts_also_count_as_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    models, _ = install(
        monkeypatch,
        {
            PRIMARY: [httpx.ReadTimeout("lento"), httpx.ReadTimeout("lento")],
            BACKUP: [text_response(ANSWER)],
        },
    )

    result = await google_generate(FAKE_KEY, [PRIMARY, BACKUP])("prompt")

    assert result == Generated(ANSWER, BACKUP)
    assert models.calls == [PRIMARY, PRIMARY, BACKUP]
