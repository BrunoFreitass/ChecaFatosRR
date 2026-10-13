"""Fallback de modelos do Gemini (GEMINI_MODEL_FALLBACKS).

Em 429 de cota DIÁRIA do modelo atual, tenta o próximo da lista; o log diz qual respondeu.
Só mocks: o SDK é simulado e nenhum teste chama o Google.
"""

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
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


def test_default_chain_starts_with_38_then_37() -> None:
    # Pool completo em test_model_pools; aqui, a ordem do topo e só nomes fixos.
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.gemini_models[:2] == (PRIMARY, BACKUP)
    assert not any(
        tag in model for model in settings.gemini_models for tag in ("latest", "preview", "exp")
    )


# Modelos fictícios precisam de limites (pool sem limites conhecidos não sobe).
FICTIONAL_LIMITS = "a-1=5/20,b-2=5/20,so-um=5/20,x-1=5/20,y-2=5/20"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" a-1 , b-2,,a-1 ", ("a-1", "b-2")), ("so-um", ("so-um",))],
)
def test_chain_parsing(raw: str, expected: tuple[str, ...]) -> None:
    settings = Settings(gemini_model_fallbacks=raw, gemini_model_limits=FICTIONAL_LIMITS)
    assert settings.gemini_models == expected


@pytest.mark.parametrize("raw", ["", " , ", "modelo com espaco", "a;b", "models/../x"])
def test_invalid_chain_is_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        Settings(gemini_model_fallbacks=raw)


def test_chain_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_MODEL_FALLBACKS", "x-1,y-2")
    monkeypatch.setenv("GEMINI_MODEL_LIMITS", FICTIONAL_LIMITS)
    assert Settings().gemini_models == ("x-1", "y-2")


def test_router_builds_the_chain_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, ...]] = []

    def fake_google_generate(_key: str, models: tuple[str, ...], **_: Any) -> Any:
        seen.append(models)
        return _no_sleep

    monkeypatch.setattr(checks_router, "google_generate", fake_google_generate)
    checks_router._analyzer.cache_clear()
    try:
        settings = Settings(
            gemini_api_key="k",
            gemini_model_fallbacks="a-1,b-2",
            gemini_model_limits=FICTIONAL_LIMITS,
        )
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
    [api_error(401, "key"), api_error(403, "forbidden"), ConnectionResetError()],
)
async def test_other_errors_do_not_fall_back(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    # Cota diária, prazo, 400, 404 e 500/503 persistentes trocam de modelo; o resto segue o
    # retry.
    models, _ = install(monkeypatch, {PRIMARY: [error], BACKUP: [text_response(ANSWER)]})

    with pytest.raises(GeminiUnavailableError):
        await google_generate(FAKE_KEY, [PRIMARY, BACKUP], max_retries=0)("prompt")
    assert models.calls == [PRIMARY]


async def test_per_minute_429_moves_to_the_next_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cada modelo tem o seu RPM: o próximo responde na hora (detalhes em test_gemini_rpm).
    models, _ = install(
        monkeypatch,
        {PRIMARY: [api_error(429, "rate", PER_MINUTE)], BACKUP: [text_response(ANSWER)]},
    )

    result = await google_generate(FAKE_KEY, [PRIMARY, BACKUP])("prompt")

    assert result == Generated(ANSWER, BACKUP)
    assert models.calls == [PRIMARY, BACKUP]


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


# ---------- modelo lento (prazo estourado): troca em vez de insistir ----------
# Teste em produção de 10/10/2026: o 3.8-flash deu 504 "Deadline expired" em 24 s e de novo
# em 24 s; medido depois sem o nosso teto, não respondeu nem em 60 s. O mesmo prompt no mesmo
# modelo estoura de novo, e a resposta precisa sair antes do corte do Cloudflare (~100 s): no
# PRIMEIRO prazo estourado a cadeia já passa ao próximo modelo.


def deadline_504() -> Exception:
    return api_error(504, "Deadline expired before operation could complete.")


async def test_a_504_switches_to_the_next_model(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    models, _ = install(monkeypatch, {PRIMARY: [deadline_504()], BACKUP: [text_response(ANSWER)]})
    caplog.set_level(logging.INFO, logger=gemini.logger.name)

    result = await google_generate(FAKE_KEY, [PRIMARY, BACKUP])("prompt")

    assert result == Generated(ANSWER, BACKUP)
    assert models.calls == [PRIMARY, BACKUP]  # sem 2ª tentativa no lento
    assert models_logged(caplog, "gemini_model_slow") == [PRIMARY]


async def test_other_transient_errors_still_retry_the_same_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, _ = install(
        monkeypatch,
        # Erro de rede (sem status): transitório, repete no mesmo modelo. O 503 troca de
        # modelo já na 1ª falha (13/10/2026).
        {PRIMARY: [httpx.ConnectError("rede"), text_response(ANSWER)], BACKUP: []},
    )

    result = await google_generate(FAKE_KEY, [PRIMARY, BACKUP])("prompt")

    assert result == Generated(ANSWER, PRIMARY)
    assert models.calls == [PRIMARY, PRIMARY]


async def test_attempt_cut_by_our_timeout_also_switches() -> None:
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
    assert calls == ["p", "b"]


async def test_sdk_read_timeout_also_counts_as_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    models, _ = install(
        monkeypatch,
        {PRIMARY: [httpx.ReadTimeout("lento")], BACKUP: [text_response(ANSWER)]},
    )

    result = await google_generate(FAKE_KEY, [PRIMARY, BACKUP])("prompt")

    assert result == Generated(ANSWER, BACKUP)
    assert models.calls == [PRIMARY, BACKUP]


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
    # As próximas checagens não pagam de novo ~25 s no modelo lento.
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


async def test_single_model_chain_that_is_slow_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cadeia de um modelo só: sem reserva, o prazo estourado vira "tente de novo" (503),
    # nunca "sem cota" (que serviria a análise anterior).
    models, _ = install(monkeypatch, {PRIMARY: [deadline_504()]})

    with pytest.raises(GeminiUnavailableError) as caught:
        await google_generate(FAKE_KEY, [PRIMARY])("prompt")
    assert not isinstance(caught.value, GeminiQuotaExhaustedError)
    assert models.calls == [PRIMARY]


# ---------- 503 persistente (ensaio em produção, 12/10/2026) ----------
# "high demand" seguido no mesmo modelo: a cadeia passa ao próximo, e o sobrecarregado vai
# para o fim da fila por SLOW_MODEL_COOLDOWN_SECONDS, sem passar do teto de 90 s.


def overloaded() -> Exception:
    return api_error(503, "The model is overloaded. Please try again later.")


async def test_persistent_503_switches_to_the_next_model(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    models, _ = install(
        monkeypatch,
        {PRIMARY: [overloaded(), overloaded()], BACKUP: [text_response(ANSWER)]},
    )
    caplog.set_level(logging.INFO, logger=gemini.logger.name)

    result = await google_generate(FAKE_KEY, [PRIMARY, BACKUP])("prompt")

    assert result == Generated(ANSWER, BACKUP)
    assert models.calls == [PRIMARY, BACKUP]  # troca já no 1º 503 (13/10/2026)
    assert models_logged(caplog, "gemini_model_slow") == [PRIMARY]


async def test_overloaded_model_goes_to_the_end_of_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, _ = install(
        monkeypatch,
        {
            PRIMARY: [overloaded()],
            BACKUP: [text_response(ANSWER), text_response(ANSWER)],
        },
    )
    chain = google_generate(FAKE_KEY, [PRIMARY, BACKUP])

    await chain("prompt")
    assert await chain("prompt") == Generated(ANSWER, BACKUP)
    assert models.calls == [PRIMARY, BACKUP, BACKUP]  # 2ª checagem já vai ao reserva


async def test_without_retries_the_first_503_already_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models, _ = install(monkeypatch, {PRIMARY: [overloaded()], BACKUP: [text_response(ANSWER)]})

    result = await google_generate(FAKE_KEY, [PRIMARY, BACKUP], max_retries=0)("prompt")

    assert result == Generated(ANSWER, BACKUP)
    assert models.calls == [PRIMARY, BACKUP]


@pytest.mark.parametrize("status", [404, 500])
async def test_missing_or_broken_model_hands_over_to_the_next(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    # /code-review (12/10/2026): modelo renomeado (404) ou com erro interno não pode desligar a
    # cadeia inteira, se há outro modelo depois dele.
    models, _ = install(
        monkeypatch,
        {PRIMARY: [api_error(status, "x")] * 3, BACKUP: [text_response(ANSWER)]},
    )

    result = await google_generate(FAKE_KEY, [PRIMARY, BACKUP], max_retries=0)("prompt")

    assert result == Generated(ANSWER, BACKUP)
    assert models.calls == [PRIMARY, BACKUP]


async def test_slow_503_switches_at_once_to_leave_time_for_the_next_model() -> None:
    # 503 que demorou (~22 s no teste de 09/10): repetir no mesmo modelo comeria o tempo
    # do próximo. Troca já, sem backoff.
    now = [0.0]
    calls: list[str] = []
    sleeps: list[float] = []

    async def primary(_: str) -> str:
        calls.append("p")
        now[0] += 22
        raise gemini.GeminiCallError(503, "high demand")

    async def backup(_: str) -> str:
        calls.append("b")
        return "do reserva"

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def clock() -> float:
        return now[0]

    chain = with_model_fallback(
        [
            ("p", with_retries(primary, clock=clock, sleep=sleep, jitter=lambda: 0, model="p")),
            ("b", with_retries(backup, clock=clock, sleep=sleep, jitter=lambda: 0, model="b")),
        ],
        clock=clock,
    )
    token = gemini._DEADLINE.set(48.0)
    try:
        assert await chain("x") == Generated("do reserva", "b")
    finally:
        gemini._DEADLINE.reset(token)
    assert calls == ["p", "b"]
    assert sleeps == []


async def test_switch_after_503_respects_the_total_budget() -> None:
    # Sem tempo útil para o próximo modelo: falha ("tente de novo") em vez de passar do teto.
    now = [0.0]
    calls: list[str] = []

    async def primary(_: str) -> str:
        calls.append("p")
        now[0] += 44
        raise gemini.GeminiCallError(503, "high demand")

    async def backup(_: str) -> str:
        calls.append("b")
        return "do reserva"

    def clock() -> float:
        return now[0]

    chain = with_model_fallback(
        [
            ("p", with_retries(primary, clock=clock, jitter=lambda: 0, model="p")),
            ("b", with_retries(backup, clock=clock, jitter=lambda: 0, model="b")),
        ],
        clock=clock,
    )
    token = gemini._DEADLINE.set(48.0)
    try:
        with pytest.raises(GeminiUnavailableError) as caught:
            await chain("x")
    finally:
        gemini._DEADLINE.reset(token)
    assert not isinstance(caught.value, GeminiQuotaExhaustedError)
    assert calls == ["p"]


async def test_last_model_still_retries_a_503(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sem próximo modelo, trocar não ajuda: o 503 volta a ser repetido no mesmo modelo
    # (e o modelo não vai para o fim da fila).
    models, _ = install(monkeypatch, {PRIMARY: [overloaded(), overloaded(), text_response(ANSWER)]})

    result = await google_generate(FAKE_KEY, [PRIMARY])("prompt")

    assert result == Generated(ANSWER, PRIMARY)
    assert models.calls == [PRIMARY, PRIMARY, PRIMARY]


async def test_backup_after_a_switch_retries_its_own_503(monkeypatch: pytest.MonkeyPatch) -> None:
    models, _ = install(
        monkeypatch,
        {
            PRIMARY: [overloaded()],
            BACKUP: [overloaded(), overloaded(), text_response(ANSWER)],
        },
    )

    result = await google_generate(FAKE_KEY, [PRIMARY, BACKUP])("prompt")

    assert result == Generated(ANSWER, BACKUP)
    assert models.calls == [PRIMARY, BACKUP, BACKUP, BACKUP]  # o último repete o próprio 503


async def test_slow_models_are_still_tried_after_a_503() -> None:
    """Incidente de 13/10/2026 (18:31 UTC): os outros modelos estavam em cooldown, o 3.8 não
    via "próximo", repetiu o 503 três vezes e a checagem caiu em "só agências" com ~38 s
    sobrando. Modelo em cooldown continua na fila (por último) e conta como próximo."""
    now = [0.0]
    calls: list[str] = []
    backup_script: list[object] = [gemini.GeminiCallError(503, "a"), "nunca"]

    async def primary(_: str) -> str:
        calls.append("p")
        if len(calls) == 1:  # 1ª checagem: fica lento (vai para o fim da fila)
            raise GeminiModelSlowError("p")
        return "do primário, que se recuperou"

    async def backup(_: str) -> str:
        calls.append("b")
        step = backup_script.pop(0)
        if isinstance(step, Exception):
            raise step
        return str(step)

    def clock() -> float:
        return now[0]

    chain = with_model_fallback(
        [
            ("p", primary),
            ("b", with_retries(backup, clock=clock, sleep=_no_sleep, jitter=lambda: 0, model="b")),
        ],
        clock=clock,
    )
    backup_script.insert(0, "do reserva")  # 1ª checagem: o primário fica lento, o reserva responde
    assert await chain("x") == Generated("do reserva", "b")

    # 2ª: o reserva vem antes (o primário está em cooldown) e dá 503 uma vez: troca na hora
    # para o primário, em vez de repetir até desistir da checagem.
    assert await chain("x") == Generated("do primário, que se recuperou", "p")
    assert calls == ["p", "b", "b", "p"]


async def test_missing_model_alone_is_still_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, {PRIMARY: [api_error(404, "not found")]})
    with pytest.raises(GeminiUnavailableError) as caught:
        await google_generate(FAKE_KEY, [PRIMARY])("prompt")
    assert not isinstance(caught.value, GeminiQuotaExhaustedError)


async def test_disabled_model_is_not_a_successor(monkeypatch: pytest.MonkeyPatch) -> None:
    """RPD 0 = modelo desligado (os Gemma, 13/10/2026). Ele não conta como "próximo": o
    último modelo de verdade não desiste de si por causa dele e repete o 503."""
    from app.services.model_limits import ModelLimits

    models, _ = install(monkeypatch, {PRIMARY: [overloaded(), text_response(ANSWER)], BACKUP: []})
    limits = {PRIMARY: ModelLimits(rpm=60, rpd=1000), BACKUP: ModelLimits(rpm=60, rpd=0)}

    result = await google_generate(FAKE_KEY, [PRIMARY, BACKUP], limits=limits)("prompt")

    assert result == Generated(ANSWER, PRIMARY)
    assert models.calls == [PRIMARY, PRIMARY]  # o desligado nunca é chamado


async def test_chain_with_every_model_disabled_still_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Todos desligados: a cadeia existe e falha como "sem cota" (o pedido vira só agências),
    sem chamar o Google."""
    from app.services.gemini import GeminiQuotaExhaustedError
    from app.services.model_limits import ModelLimits

    models, _ = install(monkeypatch, {PRIMARY: [], BACKUP: []})
    limits = {PRIMARY: ModelLimits(rpm=60, rpd=0), BACKUP: ModelLimits(rpm=60, rpd=0)}

    with pytest.raises(GeminiQuotaExhaustedError):
        await google_generate(FAKE_KEY, [PRIMARY, BACKUP], limits=limits)("prompt")
    assert models.calls == []


def _skipped(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [
        (getattr(r, "model", ""), getattr(r, "reason", ""))
        for r in caplog.records
        if r.getMessage() == "gemini_model_skipped"
    ]


async def test_model_out_of_quota_is_skipped_with_a_log(caplog: pytest.LogCaptureFixture) -> None:
    """Todo modelo pulado deixa rastro (antes, só o momento em que ficava sem cota)."""
    caplog.set_level(logging.INFO, logger=gemini.logger.name)
    now = [0.0]

    async def exhausted(_: str) -> str:
        raise gemini.GeminiQuotaExhaustedError

    async def ok(_: str) -> str:
        return "ok"

    chain = with_model_fallback(
        [("a", exhausted), ("b", ok)], clock=lambda: now[0], seconds_until_reset=lambda: 600
    )
    await chain("x")  # "a" fica sem cota por 600 s
    caplog.clear()
    now[0] = 100.0

    assert await chain("x") == Generated("ok", "b")
    assert _skipped(caplog) == [("a", "sem_cota")]
    record = next(r for r in caplog.records if r.getMessage() == "gemini_model_skipped")
    assert record.seconds_left == 500  # type: ignore[attr-defined]


async def test_model_at_its_per_minute_limit_is_skipped_with_a_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.services.gemini import RpmLimiter

    caplog.set_level(logging.INFO, logger=gemini.logger.name)
    limiter = RpmLimiter(0, clock=lambda: 0.0)
    limiter.saturate("a")

    async def ok(_: str) -> str:
        return "ok"

    chain = with_model_fallback([("a", ok), ("b", ok)], clock=lambda: 0.0, limiter=limiter)

    assert await chain("x") == Generated("ok", "b")
    assert _skipped(caplog) == [("a", "limite_minuto")]


async def test_disabled_model_is_logged_as_skipped_on_every_check(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.services.model_limits import ModelLimits

    caplog.set_level(logging.INFO, logger=gemini.logger.name)
    install(monkeypatch, {PRIMARY: [text_response(ANSWER), text_response(ANSWER)], BACKUP: []})
    limits = {PRIMARY: ModelLimits(rpm=60, rpd=1000), BACKUP: ModelLimits(rpm=60, rpd=0)}
    chain = google_generate(FAKE_KEY, [PRIMARY, BACKUP], limits=limits)

    await chain("prompt")
    await chain("prompt")

    assert _skipped(caplog) == [(BACKUP, "desligado"), (BACKUP, "desligado")]


async def test_broken_model_is_not_a_successor_for_a_503() -> None:
    """Modelo em cooldown por 400/404 está quebrado (config recusada ou nome inexistente), não
    lento: não conta como próximo. O último modelo bom repete o próprio 503 em vez de trocar
    para um modelo que certamente falha."""
    calls: list[str] = []
    backup_script: list[object] = ["do reserva", gemini.GeminiCallError(503, "a"), "de novo"]

    async def primary(_: str) -> str:
        calls.append("p")
        raise GeminiModelSlowError("p", "404")

    async def backup(_: str) -> str:
        calls.append("b")
        step = backup_script.pop(0)
        if isinstance(step, Exception):
            raise step
        return str(step)

    chain = with_model_fallback(
        [
            ("p", primary),
            ("b", with_retries(backup, sleep=_no_sleep, jitter=lambda: 0, model="b")),
        ],
        clock=lambda: 0.0,
    )
    assert await chain("x") == Generated("do reserva", "b")  # o primário dá 404: quebrado

    assert await chain("x") == Generated("de novo", "b")
    assert calls == ["p", "b", "b", "b"]
