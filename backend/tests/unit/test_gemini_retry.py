"""Chamadas ao Gemini: novas tentativas em erro transitório, cota, timeout e logs.

Escritos ANTES da implementação (TDD), a partir do teste manual de 09/10/2026: o Google
respondeu 504 (~33 s) e 503 (~22 s) e a checagem falhou sem nenhuma nova tentativa.
"""

import asyncio
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from app.config import Settings
from app.services import gemini
from app.services.gemini import (
    MAX_PROMPT_CONTENT_CHARS,
    AnalysisInput,
    GeminiAnalyzer,
    GeminiCallError,
    GeminiModelSlowError,
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


async def test_503_and_500_then_success_completes_the_check() -> None:
    call = ScriptedCall(
        GeminiCallError(503, "high demand"), GeminiCallError(500, "internal"), answer()
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


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retryable_statuses(status: int) -> None:
    call = ScriptedCall(GeminiCallError(status, "transitório"), answer())
    await GeminiAnalyzer(retrying(call, Recorder())).analyze(data())  # type: ignore[arg-type]
    assert call.calls == 2


async def test_deadline_504_is_handed_to_the_next_model_not_retried() -> None:
    # Prazo estourado: o mesmo prompt no mesmo modelo estoura de novo (ver test_gemini_fallback).
    call = ScriptedCall(GeminiCallError(504, "Deadline expired"), answer())
    with pytest.raises(GeminiModelSlowError):
        await retrying(call, Recorder())("prompt")  # type: ignore[operator]
    assert call.calls == 1


@contextmanager
def has_next_model() -> Iterator[None]:
    """Como dentro de uma ModelChain com outro modelo depois deste. Dentro do teste (e não
    numa fixture): o runner async reaproveitado não veria o ContextVar da fixture."""
    token = gemini._HAS_NEXT_MODEL.set(True)
    try:
        yield
    finally:
        gemini._HAS_NEXT_MODEL.reset(token)


async def test_first_503_hands_the_model_over() -> None:
    # 503 "high demand" (13/10/2026: 3.8 deu 503 duas vezes, ~5 s jogados fora): havendo
    # próximo modelo, a cadeia troca já na 1ª falha, sem nova tentativa no mesmo.
    call = ScriptedCall(GeminiCallError(503, "a"), answer())
    with has_next_model(), pytest.raises(GeminiModelSlowError):
        await retrying(call, Recorder())("prompt")  # type: ignore[operator]
    assert call.calls == 1


async def test_without_a_next_model_503s_keep_retrying() -> None:
    call = ScriptedCall(GeminiCallError(503, "a"), GeminiCallError(503, "b"), answer())
    assert await retrying(call, Recorder())("prompt") == answer()  # type: ignore[operator]
    assert call.calls == 3


async def test_503_count_restarts_after_another_error() -> None:
    call = ScriptedCall(GeminiCallError(503, "a"), GeminiCallError(500, "b"), answer())
    assert await retrying(call, Recorder())("prompt") == answer()  # type: ignore[operator]
    assert call.calls == 3


async def test_503_on_the_last_attempt_also_hands_the_model_over() -> None:
    call = ScriptedCall(
        GeminiCallError(None, "rede"), GeminiCallError(None, "rede"), GeminiCallError(503, "c")
    )
    with has_next_model(), pytest.raises(GeminiModelSlowError):
        await retrying(call, Recorder())("prompt")  # type: ignore[operator]
    assert call.calls == 3


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
    call = ScriptedCall(*[GeminiCallError(500, "internal")] * 3, answer())

    with pytest.raises(GeminiUnavailableError):
        await GeminiAnalyzer(retrying(call, Recorder())).analyze(data())  # type: ignore[arg-type]

    assert call.calls == 3  # 1 original + 2 novas


# ---------- backoff e tempo ----------


async def test_exponential_backoff_with_jitter() -> None:
    call = ScriptedCall(GeminiCallError(503, "a"), GeminiCallError(500, "b"), answer())
    recorder = Recorder()

    await retrying(call, recorder, base_delay=1.0)("prompt")  # type: ignore[operator]

    # base * 2^n + jitter (0.5 * base): 1.5 s e depois 2.5 s
    assert recorder.sleeps == [1.5, 2.5]


async def test_attempt_that_hangs_is_cut_and_handed_to_the_next_model() -> None:
    call = ScriptedCall("pendurar", answer())

    with pytest.raises(GeminiModelSlowError):
        await retrying(call, Recorder(), attempt_timeout=0.05)("prompt")  # type: ignore[operator]
    assert call.calls == 1


async def test_total_budget_stops_retries() -> None:
    call = ScriptedCall("pendurar", "pendurar", "pendurar", answer())

    with pytest.raises(GeminiUnavailableError):
        await retrying(call, Recorder(), attempt_timeout=0.05, budget_seconds=0.08)("prompt")  # type: ignore[operator]

    assert call.calls < 3  # o orçamento total corta antes da última tentativa


def test_worst_case_fits_under_the_cloudflare_cut() -> None:
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
    # O Render responde atrás do Cloudflare, que corta em ~100 s (524): NENHUMA requisição
    # passa de REQUEST_LIMIT_SECONDS (90), nem a de quem esperou outra checagem e assumiu.
    # As chamadas ao banco (begin_check, claim, reserve, save...) ficam fora dos tetos das
    # etapas: DB_MARGIN_SECONDS é a folga delas.
    from app.services.check_service import (
        DB_MARGIN_SECONDS,
        REQUEST_LIMIT_SECONDS,
        WAIT_MAX_SECONDS,
    )

    assert REQUEST_LIMIT_SECONDS <= 90
    assert DB_MARGIN_SECONDS >= 8
    assert worst_check + DB_MARGIN_SECONDS <= REQUEST_LIMIT_SECONDS
    assert TAKEOVER_LIMIT_SECONDS + worst_check + DB_MARGIN_SECONDS <= REQUEST_LIMIT_SECONDS
    assert TAKEOVER_LIMIT_SECONDS >= 1.5  # ainda dá para assumir na 1ª consulta
    assert WAIT_MAX_SECONDS + DB_MARGIN_SECONDS <= REQUEST_LIMIT_SECONDS
    # Quem espera outra checagem igual espera quase o pior caso dela.
    assert WAIT_MAX_SECONDS >= worst_check - 5


def test_budget_above_the_request_limit_is_rejected() -> None:
    # Um GEMINI_BUDGET_SECONDS maior no painel quebraria o teto de 90 s: falha no boot.
    with pytest.raises(ValueError):
        Settings(gemini_budget_seconds=60)


def test_budget_leaves_a_useful_attempt_for_the_next_model() -> None:
    # Modelo lento: 1 tentativa cortada no teto + backoff, e o próximo da cadeia ainda
    # precisa de tempo para responder (saudável: 7 a 13 s).
    from app.services.gemini import DEADLINE_FAILURES_TO_SWITCH

    settings = Settings()
    first_model = DEADLINE_FAILURES_TO_SWITCH * settings.gemini_attempt_timeout_seconds + 2
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


async def test_first_500_also_hands_the_model_over() -> None:
    call = ScriptedCall(GeminiCallError(500, "a"), answer())
    with has_next_model(), pytest.raises(GeminiModelSlowError):
        await retrying(call, Recorder())("prompt")  # type: ignore[operator]
    assert call.calls == 1


async def test_short_remaining_time_still_gets_an_attempt() -> None:
    """Sobra menos que o teto por tentativa (18 s), mas ainda 12 s: o modelo é tentado com o
    tempo que resta (13/10/2026: com 48 s gastos, o 3.5-flash-lite nem foi chamado)."""
    call = ScriptedCall(answer())
    generate = with_retries(call, attempt_timeout=18.0, budget_seconds=48.0)  # type: ignore[arg-type]
    with gemini.deadline_scope(12.0):
        assert await generate("prompt") == answer()
    assert call.calls == 1


def test_flash_attempt_cap_is_eighteen_seconds() -> None:
    """Respostas boas medidas: 2 a 7 s. 18 s ainda sobra e deixa ~30 s do orçamento de 48 s
    para os modelos seguintes (antes, 25 s num modelo travado comia metade)."""
    assert Settings().gemini_attempt_timeout_seconds == 18
