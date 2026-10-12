"""google_generate com o cliente do SDK simulado: classificação dos erros do Google.

Complementa test_gemini_retry.py (que testa a política de novas tentativas isolada).
"""

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import errors

from app.config import Settings
from app.services import gemini
from app.services.gemini import (
    MIN_ATTEMPT_SECONDS,
    AnalysisInput,
    GeminiAnalyzer,
    GeminiQuotaExhaustedError,
    GeminiUnavailableError,
    Generated,
    google_generate,
    with_retries,
)

pytestmark = pytest.mark.anyio

MODEL = "gemini-3.8-flash"

FAKE_KEY = "AIzaSyFAKE-chave-de-teste-0123456789abcdef"  # pragma: allowlist secret
ANSWER = json.dumps(
    {
        "veredito": "nao_verificavel",
        "resumo": "Sem evidência.",
        "pontos_suspeitos": [],
        "contexto_faltante": None,
        "checagens_agencias": [],
        "fontes_sugeridas": [],
        "confianca": 20,
    }
)


class FakeModels:
    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.calls = 0

    async def generate_content(self, **_: Any) -> Any:
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


async def _no_sleep(_: float) -> None:
    return None


def install_fake_client(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> FakeModels:
    models = FakeModels(script)

    class FakeClient:
        def __init__(self, **_: Any) -> None:
            self.aio = SimpleNamespace(models=models)

    monkeypatch.setattr("google.genai.Client", FakeClient)
    # Sem espera real entre tentativas.
    monkeypatch.setattr(gemini.asyncio, "sleep", _no_sleep)
    return models


def text_response(text: str | None, finish: str = "STOP", usage: Any = None) -> Any:
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=finish)],
        prompt_feedback=None,
        usage_metadata=usage,
    )


def api_error(
    code: int, message: str, details: list[dict[str, Any]] | None = None
) -> errors.APIError:
    body = {"error": {"code": code, "message": message, "status": "X", "details": details or []}}
    return errors.APIError(code, body)


def data() -> AnalysisInput:
    return AnalysisInput(
        kind="texto",
        content="URGENTE: a votação foi adiada!",
        title=None,
        url=None,
        agency_checks=[],
        content_urls=[],
    )


async def test_transient_google_errors_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    models = install_fake_client(
        monkeypatch,
        [api_error(503, "high demand"), api_error(500, "internal"), text_response(ANSWER)],
    )
    result = await GeminiAnalyzer(google_generate(FAKE_KEY, [MODEL])).analyze(data())
    assert result.veredito == "nao_verificavel"
    assert models.calls == 3


async def test_daily_quota_429_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    details = [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]
    models = install_fake_client(
        monkeypatch, [api_error(429, "quota", details), text_response(ANSWER)]
    )
    # Cota diária do Google esgotada: sem nova tentativa, e vira "esgotada hoje".
    with pytest.raises(GeminiQuotaExhaustedError):
        await google_generate(FAKE_KEY, [MODEL])("prompt")
    assert models.calls == 1


async def test_per_minute_429_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    # RPM 5 no nível gratuito: repetir em 1 a 2 s só gasta outra requisição do mesmo minuto.
    # Sem outro modelo, vira "limite por minuto" (a checagem responde no modo agências).
    details = [{"violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel"}]}]
    models = install_fake_client(
        monkeypatch, [api_error(429, "rate", details), text_response(ANSWER)]
    )
    with pytest.raises(gemini.GeminiRateLimitedError):
        await google_generate(FAKE_KEY, [MODEL])("prompt")
    assert models.calls == 1


async def test_bad_request_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    models = install_fake_client(
        monkeypatch, [api_error(400, "invalid argument"), text_response(ANSWER)]
    )
    with pytest.raises(GeminiUnavailableError):
        await google_generate(FAKE_KEY, [MODEL])("prompt")
    assert models.calls == 1


async def test_network_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    models = install_fake_client(
        monkeypatch, [ConnectionResetError("reset"), text_response(ANSWER)]
    )
    assert await google_generate(FAKE_KEY, [MODEL])("prompt") == Generated(ANSWER, MODEL)
    assert models.calls == 2


@pytest.mark.parametrize("finish", ["SAFETY", "MAX_TOKENS"])
async def test_empty_response_is_not_retried(monkeypatch: pytest.MonkeyPatch, finish: str) -> None:
    models = install_fake_client(monkeypatch, [text_response(None, finish), text_response(ANSWER)])
    with pytest.raises(GeminiUnavailableError):
        await google_generate(FAKE_KEY, [MODEL])("prompt")
    assert models.calls == 1


async def test_key_never_reaches_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install_fake_client(monkeypatch, [api_error(400, f"API key not valid: {FAKE_KEY}")])
    with pytest.raises(GeminiUnavailableError):
        await google_generate(FAKE_KEY, [MODEL])("prompt")
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "gemini_api_error" in logged
    assert FAKE_KEY not in logged


# ---------- prazo único entre o analisador e as novas tentativas ----------


async def test_retries_respect_the_analyzer_deadline() -> None:
    calls = 0

    async def hangs(_: str) -> str | None:
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()
        return None

    generate = with_retries(hangs, attempt_timeout=0.05, budget_seconds=48, sleep=_no_sleep)
    analyzer = GeminiAnalyzer(generate, budget_seconds=MIN_ATTEMPT_SECONDS + 0.12)

    with pytest.raises(GeminiUnavailableError):
        await analyzer.analyze(data())
    # O analisador só começa com tempo >= MIN_ATTEMPT_SECONDS; as novas tentativas respeitam
    # o prazo DELE (não os 48 s próprios) e não passam de 3.
    assert 1 <= calls <= 3


async def test_no_quota_unit_without_time_left() -> None:
    reserved: list[int] = []

    async def reserve() -> bool:
        reserved.append(1)
        return True

    async def invalid_json(_: str) -> str | None:
        return "{quebrado"

    analyzer = GeminiAnalyzer(invalid_json, budget_seconds=MIN_ATTEMPT_SECONDS - 0.1)
    with pytest.raises(GeminiUnavailableError):
        await analyzer.analyze(data(), reserve)
    assert reserved == []  # não havia tempo para uma resposta útil: nem gasta cota


# ---------- segredos fora do repr ----------


def test_settings_repr_hides_secrets() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        gemini_api_key=FAKE_KEY,
        google_factcheck_api_key="fact-check-secreta",  # pragma: allowlist secret
        database_url="postgresql://usuario:senha-do-banco@host/db",  # pragma: allowlist secret
        supabase_secret_key="sb_secret_xyz",  # pragma: allowlist secret
        supabase_jwt_secret="jwt-secreto",  # pragma: allowlist secret
        ban_hash_salt="sal-secreto",
    )
    shown = repr(settings) + str(settings)
    secrets = (
        FAKE_KEY,
        "fact-check-secreta",
        "senha-do-banco",
        "sb_secret_xyz",
        "jwt-secreto",
        "sal-secreto",
    )
    for secret in secrets:
        assert secret not in shown
    assert settings.gemini_api_key == FAKE_KEY  # continua acessível no código


# ---------- uso (tamanho do texto e tokens) no log ----------


async def test_usage_is_logged_with_prompt_size_and_tokens(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Mostra se o thinking está mesmo desligado (thoughts_tokens) e o peso de cada checagem.
    usage = SimpleNamespace(
        prompt_token_count=2100, candidates_token_count=380, thoughts_token_count=0
    )
    install_fake_client(monkeypatch, [text_response(ANSWER, usage=usage)])
    caplog.set_level(logging.INFO, logger=gemini.logger.name)

    await google_generate(FAKE_KEY, [MODEL])("p" * 6509)

    [record] = [r for r in caplog.records if r.getMessage() == "gemini_usage"]
    assert record.model == MODEL
    assert record.prompt_chars == 6509
    assert (record.input_tokens, record.output_tokens, record.thoughts_tokens) == (2100, 380, 0)
    assert isinstance(record.elapsed_ms, int)
    assert FAKE_KEY not in caplog.text


async def test_usage_log_tolerates_missing_metadata(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install_fake_client(monkeypatch, [text_response(ANSWER)])
    caplog.set_level(logging.INFO, logger=gemini.logger.name)

    await google_generate(FAKE_KEY, [MODEL])("prompt")

    [record] = [r for r in caplog.records if r.getMessage() == "gemini_usage"]
    assert record.thoughts_tokens is None


async def test_error_log_has_the_prompt_size(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install_fake_client(monkeypatch, [api_error(400, "invalid")])
    caplog.set_level(logging.WARNING, logger=gemini.logger.name)

    with pytest.raises(GeminiUnavailableError):
        await google_generate(FAKE_KEY, [MODEL])("p" * 1234)

    [record] = [r for r in caplog.records if r.getMessage() == "gemini_api_error"]
    assert record.prompt_chars == 1234
