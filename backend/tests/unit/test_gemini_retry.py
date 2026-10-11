"""Chamadas ao Gemini: novas tentativas em erro transitório, cota, timeout e logs.

Escritos ANTES da implementação (TDD), a partir do teste manual de 09/10/2026: o Google
respondeu 504 (~33 s) e 503 (~22 s) e a checagem falhou sem nenhuma nova tentativa.
"""

import asyncio
import json
import logging

import pytest

from app.config import Settings
from app.services.gemini import (
    MAX_PROMPT_CONTENT_CHARS,
    AnalysisInput,
    GeminiAnalyzer,
    GeminiCallError,
    GeminiUnavailableError,
    build_generate_config,
    build_prompt,
    safe_error_message,
    with_retries,
)

pytestmark = pytest.mark.anyio

FAKE_KEY = "AIzaSyFAKE-chave-de-teste-0123456789abcdef"  # pragma: allowlist secret


def data(content: str = "URGENTE: a votação em Roraima foi adiada para novembro!") -> AnalysisInput:
    return AnalysisInput(
        kind="texto", content=content, title=None, url=None, agency_checks=[], content_urls=[]
    )


def answer() -> str:
    return json.dumps(
        {
            "veredito": "nao_verificavel",
            "resumo": "Não há evidência de adiamento.",
            "pontos_suspeitos": [],
            "contexto_faltante": None,
            "checagens_agencias": [],
            "fontes_sugeridas": [],
            "confianca": 30,
        }
    )


class ScriptedCall:
    """Chamada crua ao modelo: cada item do roteiro é uma resposta ou uma exceção."""

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.calls = 0

    async def __call__(self, prompt: str) -> str | None:
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        if step == "pendurar":  # nunca responde (simula o 504 que só vem depois de ~58 s)
            await asyncio.Event().wait()
        return step  # type: ignore[return-value]


class Recorder:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def retrying(call: ScriptedCall, recorder: Recorder, **kwargs: object) -> object:
    options: dict[str, object] = {"sleep": recorder.sleep, "jitter": lambda: 0.5}
    return with_retries(call, **(options | kwargs))  # type: ignore[arg-type]


# ---------- 503/504 seguidos de sucesso ----------


async def test_503_and_504_then_success_completes_the_check() -> None:
    call = ScriptedCall(
        GeminiCallError(503, "high demand"), GeminiCallError(504, "deadline"), answer()
    )
    recorder = Recorder()

    result = await GeminiAnalyzer(retrying(call, recorder)).analyze(data())  # type: ignore[arg-type]

    assert result.veredito == "nao_verificavel"
    assert call.calls == 3


async def test_retry_does_not_reserve_new_daily_quota() -> None:
    call = ScriptedCall(GeminiCallError(503, "high demand"), GeminiCallError(429, "rate"), answer())
    reserved: list[int] = []

    async def reserve() -> bool:
        reserved.append(1)
        return True

    await GeminiAnalyzer(retrying(call, Recorder())).analyze(data(), reserve)  # type: ignore[arg-type]

    assert call.calls == 3
    assert reserved == [1]  # só a checagem; as novas tentativas de transporte não gastam cota


@pytest.mark.parametrize("status", [429, 500, 503, 504])
async def test_retryable_statuses(status: int) -> None:
    call = ScriptedCall(GeminiCallError(status, "transitório"), answer())
    await GeminiAnalyzer(retrying(call, Recorder())).analyze(data())  # type: ignore[arg-type]
    assert call.calls == 2


async def test_connection_failure_without_http_status_is_retried() -> None:
    call = ScriptedCall(GeminiCallError(None, "ConnectError"), answer())
    await GeminiAnalyzer(retrying(call, Recorder())).analyze(data())  # type: ignore[arg-type]
    assert call.calls == 2


# ---------- erros que NÃO devem ser repetidos ----------


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_client_errors_are_not_retried(status: int) -> None:
    call = ScriptedCall(GeminiCallError(status, "requisição inválida"), answer())
    recorder = Recorder()

    with pytest.raises(GeminiUnavailableError):
        await GeminiAnalyzer(retrying(call, recorder)).analyze(data())  # type: ignore[arg-type]

    assert call.calls == 1
    assert recorder.sleeps == []


async def test_gives_up_after_two_new_attempts() -> None:
    call = ScriptedCall(*[GeminiCallError(503, "high demand")] * 3, answer())

    with pytest.raises(GeminiUnavailableError):
        await GeminiAnalyzer(retrying(call, Recorder())).analyze(data())  # type: ignore[arg-type]

    assert call.calls == 3  # 1 original + 2 novas


# ---------- backoff e tempo ----------


async def test_exponential_backoff_with_jitter() -> None:
    call = ScriptedCall(GeminiCallError(503, "a"), GeminiCallError(503, "b"), answer())
    recorder = Recorder()

    await retrying(call, recorder, base_delay=1.0)("prompt")  # type: ignore[operator]

    # base * 2^n + jitter (0.5 * base): 1.5 s e depois 2.5 s
    assert recorder.sleeps == [1.5, 2.5]


async def test_attempt_that_hangs_is_cut_and_retried() -> None:
    call = ScriptedCall("pendurar", answer())

    result = await retrying(call, Recorder(), attempt_timeout=0.05)("prompt")  # type: ignore[operator]

    assert result == answer()
    assert call.calls == 2


async def test_total_budget_stops_retries() -> None:
    call = ScriptedCall("pendurar", "pendurar", "pendurar", answer())

    with pytest.raises(GeminiUnavailableError):
        await retrying(call, Recorder(), attempt_timeout=0.05, budget_seconds=0.08)("prompt")  # type: ignore[operator]

    assert call.calls < 3  # o orçamento total corta antes da última tentativa


def test_worst_case_fits_cloud_run_timeout() -> None:
    from app.repositories.checks import CLAIM_TTL_SECONDS
    from app.services.check_service import (
        AGENCIES_TIMEOUT_SECONDS,
        READ_LINK_TIMEOUT_SECONDS,
        TAKEOVER_LIMIT_SECONDS,
        WORST_CHECK_SECONDS,
    )

    settings = Settings()
    gemini = min(
        settings.gemini_budget_seconds,
        (settings.gemini_max_retries + 1) * settings.gemini_attempt_timeout_seconds,
    )
    worst_check = READ_LINK_TIMEOUT_SECONDS + AGENCIES_TIMEOUT_SECONDS + gemini
    assert worst_check <= WORST_CHECK_SECONDS
    # O claim não expira no meio de uma checagem legítima.
    assert CLAIM_TTL_SECONDS > worst_check
    # Quem esperou e assumiu ainda cabe no --timeout 180 do Cloud Run (plano B).
    assert TAKEOVER_LIMIT_SECONDS + worst_check < 180
    # Quem espera outra checagem igual espera quase o pior caso dela.
    from app.services.check_service import WAIT_MAX_SECONDS

    assert WAIT_MAX_SECONDS >= worst_check - 5


def test_budget_leaves_a_useful_attempt_for_the_next_model() -> None:
    # Modelo lento: 2 tentativas cortadas no teto + backoff, e o próximo da cadeia ainda
    # precisa de tempo para responder (saudável: 7 a 13 s).
    settings = Settings()
    first_model = 2 * settings.gemini_attempt_timeout_seconds + 2  # backoff de 1 a 2 s
    assert settings.gemini_budget_seconds - first_model >= 20


# ---------- texto enviado ao modelo ----------


def test_text_sent_to_gemini_is_truncated_to_about_8k() -> None:
    marker = "#"  # não aparece no texto fixo do prompt
    prompt = build_prompt(data(content=marker * 20_000))
    assert prompt.count(marker) == MAX_PROMPT_CONTENT_CHARS
    assert MAX_PROMPT_CONTENT_CHARS == 8000


# ---------- modelo e thinking ----------


def test_default_model_is_fixed_stable_flash_with_thinking_off() -> None:
    settings = Settings()
    assert settings.gemini_models[0] == "gemini-3.8-flash"
    assert settings.gemini_thinking_budget == 0


def test_thinking_level_replaces_the_budget_when_set() -> None:
    # Modelos 3.x: o nível é o controle nativo (o 3.8-flash recusa MINIMAL com 400, aceita LOW).
    from google.genai import types

    config = build_generate_config(thinking_budget=0, thinking_level="low")
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert config.thinking_config.thinking_budget is None  # os dois juntos dão 400


def test_thinking_level_setting_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().gemini_thinking_level == ""  # padrão: thinking_budget=0
    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "minimal")
    assert Settings().gemini_thinking_level == "minimal"
    with pytest.raises(ValueError):
        Settings(gemini_thinking_level="maximo")


def test_generate_config_disables_thinking_and_uses_json_schema() -> None:
    config = build_generate_config(thinking_budget=0)
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_budget == 0
    assert config.response_mime_type == "application/json"


# ---------- logs sem a chave ----------


async def test_error_log_has_google_message_and_timing(caplog: pytest.LogCaptureFixture) -> None:
    call = ScriptedCall(
        GeminiCallError(503, "This model is currently experiencing high demand."), answer()
    )
    with caplog.at_level(logging.WARNING, logger="app.services.gemini"):
        await retrying(call, Recorder())("prompt")  # type: ignore[operator]

    record = next(r for r in caplog.records if r.getMessage() == "gemini_api_error")
    assert record.status == 503  # type: ignore[attr-defined]
    assert "high demand" in record.google_message  # type: ignore[attr-defined]
    assert record.attempt == 1  # type: ignore[attr-defined]
    assert isinstance(record.elapsed_ms, int)  # type: ignore[attr-defined]


def test_google_message_never_carries_the_key() -> None:
    raw = f"API key not valid: {FAKE_KEY}. Please pass a valid API key." + "x" * 1000
    message = safe_error_message(raw, FAKE_KEY)
    assert FAKE_KEY not in message
    assert "AIza" not in message  # qualquer chave do Google, mesmo que não seja a nossa
    assert len(message) <= 300
