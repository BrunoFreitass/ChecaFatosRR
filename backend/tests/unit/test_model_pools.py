"""Pools de modelos por função, com limites por modelo (AI Studio, nível gratuito, 12/10/2026).

CHECAGEM: 3.8-flash → 3.7-flash → 3.6-flash → 3.5-flash-lite → gemma-4-31b-it → "só agências".
MODERAÇÃO: 3.1-flash-lite → gemma-4-26b-a4b-it → fila (fail-open).
Sondagem real de 12/10: 2.5-flash = 404 (indisponível para contas novas); 3-flash só existe
como preview (fora: só nomes fixos); Gemma recusa thinking_budget. Só mocks aqui.
"""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.services import gemini
from app.services.gemini import (
    GeminiAnalyzer,
    GeminiRateLimitedError,
    Generated,
    RpmLimiter,
    google_generate,
)
from app.services.llm_models import (
    CHECK_TASK,
    DEFAULT_MODEL_LIMITS,
    ModelLimits,
    build_task_config,
    build_task_contents,
    extract_json,
    family,
    parse_model_limits,
)
from tests.unit.test_gemini_client import ANSWER, FAKE_KEY, api_error, data, text_response

pytestmark = pytest.mark.anyio


class Models:
    def __init__(self, scripts: dict[str, list[Any]]) -> None:
        self.scripts = scripts
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, **kwargs: Any) -> Any:
        self.calls.append({"model": model, **kwargs})
        step = self.scripts[model].pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    @property
    def models_called(self) -> list[str]:
        return [c["model"] for c in self.calls]


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


# ---------- configuração ----------


def test_default_pools_are_separate_and_follow_the_free_tier() -> None:
    from app.config import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.gemini_models == (
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemma-4-31b-it",
    )
    assert settings.gemini_moderation_models_list == ("gemini-3.1-flash-lite", "gemma-4-26b-a4b-it")
    assert not set(settings.gemini_models) & set(settings.gemini_moderation_models_list)
    # Só tetos de segurança globais: quem limita de verdade é o RPD de cada modelo.
    assert (settings.gemini_daily_checks, settings.gemini_daily_moderation) == (1000, 2000)
    assert settings.comments_daily_limit == 20


def test_pools_cannot_share_a_model() -> None:
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            gemini_model_fallbacks="gemini-3.8-flash,gemini-3.1-flash-lite",
            gemini_moderation_models="gemini-3.1-flash-lite",
        )


def test_known_limits_per_model() -> None:
    assert DEFAULT_MODEL_LIMITS["gemini-3.8-flash"] == ModelLimits(rpm=5, rpd=20)
    assert DEFAULT_MODEL_LIMITS["gemini-3.6-flash"] == ModelLimits(rpm=5, rpd=20)
    assert DEFAULT_MODEL_LIMITS["gemini-3.5-flash-lite"] == ModelLimits(rpm=15, rpd=500)
    assert DEFAULT_MODEL_LIMITS["gemini-3.1-flash-lite"] == ModelLimits(rpm=15, rpd=500)
    assert DEFAULT_MODEL_LIMITS["gemma-4-31b-it"] == ModelLimits(rpm=30, rpd=14400, tpm=16000)
    assert DEFAULT_MODEL_LIMITS["gemma-4-26b-a4b-it"] == ModelLimits(rpm=30, rpd=14400, tpm=16000)


def test_limits_can_be_overridden_by_the_environment() -> None:
    limits = parse_model_limits("gemini-3.6-flash=2/3, gemma-4-31b-it=10/100/8000")
    assert limits["gemini-3.6-flash"] == ModelLimits(rpm=2, rpd=3)
    assert limits["gemma-4-31b-it"] == ModelLimits(rpm=10, rpd=100, tpm=8000)
    assert limits["gemini-3.8-flash"] == DEFAULT_MODEL_LIMITS["gemini-3.8-flash"]  # mantém


@pytest.mark.parametrize(
    "raw", ["gemini-3.6-flash=5", "x=a/b", "=5/20", "gemini-3.6-flash=5/20/1/2"]
)
def test_invalid_limits_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_model_limits(raw)


def test_settings_expose_the_merged_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Settings

    monkeypatch.setenv("GEMINI_MODEL_LIMITS", "gemini-3.6-flash=1/2")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.model_limits["gemini-3.6-flash"] == ModelLimits(rpm=1, rpd=2)


# ---------- família e configuração por modelo ----------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gemini-3.8-flash", "flash"),
        ("gemini-3.5-flash-lite", "lite"),
        ("gemini-3.1-flash-lite", "lite"),
        ("gemma-4-31b-it", "gemma"),
        ("gemma-4-26b-a4b-it", "gemma"),
    ],
)
def test_family(model: str, expected: str) -> None:
    assert family(model) == expected


def test_flash_keeps_the_native_json_schema_and_thinking_off() -> None:
    config = build_task_config(CHECK_TASK, "gemini-3.8-flash")
    assert config.response_schema is gemini.GeminiVerdict
    assert config.system_instruction == gemini.SYSTEM_INSTRUCTION
    assert config.thinking_config.thinking_budget == 0


def test_lite_does_not_send_a_thinking_budget() -> None:
    # 12/10: 3.5-flash-lite respondeu 400 com thinking_budget=0.
    config = build_task_config(CHECK_TASK, "gemini-3.5-flash-lite")
    assert config.thinking_config is None
    assert config.response_schema is gemini.GeminiVerdict


def test_gemma_gets_the_instruction_and_schema_inside_the_prompt() -> None:
    # 12/10: Gemma recusa thinking_budget (400); JSON nativo e system instruction não foram
    # confirmados: vão no próprio pedido, e a resposta passa pela validação Pydantic.
    config = build_task_config(CHECK_TASK, "gemma-4-31b-it")
    assert config.thinking_config is None
    assert config.system_instruction is None
    assert config.response_schema is None
    assert config.max_output_tokens >= 8192  # o Gemma pensa antes de responder

    contents = build_task_contents(CHECK_TASK, "gemma-4-31b-it", "PEDIDO")
    assert contents.startswith(gemini.SYSTEM_INSTRUCTION)
    assert '"veredito"' in contents and contents.endswith("PEDIDO")
    assert build_task_contents(CHECK_TASK, "gemini-3.8-flash", "PEDIDO") == "PEDIDO"


@pytest.mark.parametrize(
    "raw",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        'Aqui está:\n{"a": 1}\nEspero ter ajudado.',
    ],
)
def test_extract_json(raw: str) -> None:
    assert json.loads(extract_json(raw)) == {"a": 1}


async def test_gemma_answer_in_fences_is_cleaned(monkeypatch: pytest.MonkeyPatch) -> None:
    models = install(monkeypatch, {"gemma-4-31b-it": [text_response(f"```json\n{ANSWER}\n```")]})
    chain = google_generate(FAKE_KEY, ["gemma-4-31b-it"])

    result = await chain("pedido")

    assert result == Generated(ANSWER, "gemma-4-31b-it")
    sent = models.calls[0]
    assert sent["config"].system_instruction is None
    assert sent["contents"].startswith(gemini.SYSTEM_INSTRUCTION)


# ---------- limites por modelo ----------


def test_limiter_reads_rpm_per_model() -> None:
    limiter = RpmLimiter(
        per_model={"a": ModelLimits(rpm=1, rpd=10), "b": ModelLimits(rpm=2, rpd=10)}
    )
    assert limiter.try_acquire("a") and not limiter.try_acquire("a")
    assert limiter.try_acquire("b") and limiter.try_acquire("b") and not limiter.try_acquire("b")
    assert all(limiter.try_acquire("desconhecido") for _ in range(10))  # sem limite local


def test_limiter_respects_tokens_per_minute() -> None:
    limiter = RpmLimiter(per_model={"g": ModelLimits(rpm=30, rpd=100, tpm=16000)})
    assert limiter.try_acquire("g", tokens=9000)
    assert not limiter.has_room("g", tokens=8000)  # passaria de 16K no minuto
    assert limiter.try_acquire("g", tokens=7000)


async def test_full_tpm_moves_to_the_next_model(monkeypatch: pytest.MonkeyPatch) -> None:
    models = install(
        monkeypatch, {"gemma-4-31b-it": [], "gemini-3.6-flash": [text_response(ANSWER)]}
    )
    limits = {"gemma-4-31b-it": ModelLimits(rpm=30, rpd=100, tpm=1000)}
    chain = google_generate(FAKE_KEY, ["gemma-4-31b-it", "gemini-3.6-flash"], limits=limits)

    result = await chain("x" * 9000)  # ~3000 tokens: não cabe no TPM de 1000

    assert result == Generated(ANSWER, "gemini-3.6-flash")
    assert models.models_called == ["gemini-3.6-flash"]


async def test_model_out_of_its_daily_budget_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    models = install(
        monkeypatch,
        {"gemini-3.8-flash": [text_response(ANSWER)], "gemini-3.7-flash": [text_response(ANSWER)]},
    )
    asked: list[tuple[str, int]] = []

    async def budget(model: str, rpd: int) -> bool:
        asked.append((model, rpd))
        return model != "gemini-3.8-flash"  # o 3.8 já fez as 20 do dia

    chain = google_generate(FAKE_KEY, ["gemini-3.8-flash", "gemini-3.7-flash"])
    reserved: list[int] = []

    async def reserve() -> bool:
        reserved.append(1)
        return True

    result = await GeminiAnalyzer(chain).analyze(data(), reserve, model_budget=budget)

    assert result.model == "gemini-3.7-flash"
    assert models.models_called == ["gemini-3.7-flash"]  # o esgotado nem foi chamado
    assert asked == [("gemini-3.8-flash", 20), ("gemini-3.7-flash", 20)]


async def test_daily_budget_failure_does_not_block_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, {"gemini-3.8-flash": [text_response(ANSWER)]})

    async def broken(_: str, __: int) -> bool:
        raise ConnectionError("banco fora")

    result = await GeminiAnalyzer(google_generate(FAKE_KEY, ["gemini-3.8-flash"])).analyze(
        data(), model_budget=broken
    )
    assert result.model == "gemini-3.8-flash"


async def test_bad_request_on_a_model_with_successor_moves_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Config errada num modelo (400) não pode derrubar a cadeia inteira.
    models = install(
        monkeypatch,
        {
            "gemini-3.5-flash-lite": [api_error(400, "Request contains an invalid argument.")],
            "gemma-4-31b-it": [text_response(ANSWER)],
        },
    )
    chain = google_generate(FAKE_KEY, ["gemini-3.5-flash-lite", "gemma-4-31b-it"])

    assert await chain("pedido") == Generated(ANSWER, "gemma-4-31b-it")
    assert models.models_called == ["gemini-3.5-flash-lite", "gemma-4-31b-it"]


async def test_all_full_still_answers_with_agencies_only(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, {"a": [text_response(ANSWER)]})
    chain = google_generate(FAKE_KEY, ["a"], limits={"a": ModelLimits(rpm=1, rpd=10)})
    await chain("pedido")
    with pytest.raises(GeminiRateLimitedError):
        await chain("pedido")


# ---------- revisões (12/10/2026) ----------


@pytest.mark.parametrize(
    "raw",
    [
        'Raciocínio {rascunho} e depois: {"a": 1}',
        '{"a": 1} nota: {fim}',
        'pensando...\n```json\n{"a": {"b": 2}}\n```',
    ],
)
def test_extract_json_skips_braces_around_the_object(raw: str) -> None:
    assert isinstance(json.loads(extract_json(raw)), dict)


async def test_invalid_key_400_does_not_walk_the_whole_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = install(
        monkeypatch,
        {
            "gemini-3.8-flash": [api_error(400, "API key not valid. Please pass a valid API key.")],
            "gemini-3.7-flash": [text_response(ANSWER)],
        },
    )
    with pytest.raises(gemini.GeminiUnavailableError):
        await google_generate(FAKE_KEY, ["gemini-3.8-flash", "gemini-3.7-flash"])("pedido")
    assert models.models_called == ["gemini-3.8-flash"]  # a chave vale para todos


async def test_model_refusing_its_config_stays_out_for_an_hour() -> None:
    now = [0.0]
    calls: list[str] = []

    async def refuses(_: str) -> str:
        calls.append("p")
        raise gemini.GeminiModelSlowError("p", "400")

    async def backup(_: str) -> str:
        calls.append("b")
        return "ok"

    chain = gemini.with_model_fallback([("p", refuses), ("b", backup)], clock=lambda: now[0])
    await chain("x")
    now[0] = gemini.SLOW_MODEL_COOLDOWN_SECONDS + 1  # passou o cooldown de "lento"
    await chain("x")
    assert calls == ["p", "b", "b"]  # o 400 continua no fim da fila


async def test_counter_refusal_is_not_a_call_to_google(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, {"gemini-3.8-flash": [], "gemini-3.7-flash": []})
    limits = {
        "gemini-3.8-flash": ModelLimits(rpm=5, rpd=20),
        "gemini-3.7-flash": ModelLimits(rpm=0, rpd=20),
    }
    chain = google_generate(FAKE_KEY, ["gemini-3.8-flash", "gemini-3.7-flash"], limits=limits)
    released: list[int] = []

    async def budget(model: str, _: int) -> bool:
        return model != "gemini-3.8-flash"

    async def release() -> None:
        released.append(1)

    # 3.7 sem vaga no minuto (rpm 0 na tabela = sem limite local; aqui forçamos pelo saturate)
    chain._limiter.saturate("gemini-3.7-flash")
    with pytest.raises(GeminiRateLimitedError) as caught:
        await GeminiAnalyzer(chain).analyze(data(), release=release, model_budget=budget)
    assert caught.value.called is False
    assert released == [1]


async def test_zero_rpd_means_the_model_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    models = install(
        monkeypatch,
        {"gemini-3.8-flash": [text_response(ANSWER)], "gemini-3.7-flash": [text_response(ANSWER)]},
    )
    limits = {
        "gemini-3.8-flash": ModelLimits(rpm=5, rpd=0),  # desligado sem deploy
        "gemini-3.7-flash": ModelLimits(rpm=5, rpd=20),
    }
    chain = google_generate(FAKE_KEY, ["gemini-3.8-flash", "gemini-3.7-flash"], limits=limits)

    assert await chain("pedido") == Generated(ANSWER, "gemini-3.7-flash")
    assert models.models_called == ["gemini-3.7-flash"]


def test_pool_model_without_limits_is_rejected() -> None:
    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, gemini_model_fallbacks="gemini-3.8-flash,modelo-sem-tabela")  # type: ignore[call-arg]


def test_gemma_gets_a_longer_attempt_than_the_flash(monkeypatch: pytest.MonkeyPatch) -> None:
    # O Gemma pensa antes de responder: tentativa própria, mais longa que a do Flash-Lite.
    seen: dict[str, float] = {}
    real_with_retries = gemini.with_retries

    def spy(call: Any, **kwargs: Any) -> Any:
        seen[kwargs["model"]] = kwargs["attempt_timeout"]
        return real_with_retries(call, **kwargs)

    monkeypatch.setattr(gemini, "with_retries", spy)
    monkeypatch.setattr("google.genai.Client", lambda **_: SimpleNamespace(aio=None))
    from app.services.moderation.ai_filter import (
        ATTEMPT_SHARE,
        GEMMA_ATTEMPT_SHARE,
        google_moderator,
    )

    google_moderator(FAKE_KEY, ["gemini-3.1-flash-lite", "gemma-4-26b-a4b-it"], budget_seconds=18)

    assert seen["gemini-3.1-flash-lite"] == pytest.approx(18 * ATTEMPT_SHARE)
    assert seen["gemma-4-26b-a4b-it"] == pytest.approx(18 * GEMMA_ATTEMPT_SHARE)
    assert seen["gemma-4-26b-a4b-it"] > seen["gemini-3.1-flash-lite"]


async def test_moderation_retries_only_when_gemma_answered_out_of_schema() -> None:
    from app.services.moderation.ai_filter import AiModerator
    from app.services.moderation.pipeline import AiUnavailable

    answers = [Generated("não é json", "gemini-3.1-flash-lite")]
    calls: list[int] = []

    async def generate(_: str) -> Generated:
        calls.append(1)
        return answers.pop(0)

    async def quota() -> bool:
        return True

    result = await AiModerator(generate).classify("comentário", quota)

    assert result == AiUnavailable("resposta_invalida")
    assert calls == [1]  # Flash-Lite tem JSON nativo: repetir não ajuda


# ---------- validação real de 13/10/2026 (4 chamadas autorizadas) ----------


def test_render_turns_off_both_gemma_models() -> None:
    # gemma-4-31b-it: 149 s e 3.596 tokens de raciocínio numa checagem (nunca cabe nos 48 s);
    # gemma-4-26b-a4b-it: 500 interno nas duas chamadas. Ficam na cadeia, desligados (RPD 0).
    from tests.unit.test_deploy_files import render_service

    env = {item["key"]: item.get("value") for item in render_service()["envVars"]}  # type: ignore[index]
    limits = parse_model_limits(str(env["GEMINI_MODEL_LIMITS"]))
    assert limits["gemma-4-31b-it"].rpd == 0
    assert limits["gemma-4-26b-a4b-it"].rpd == 0
    assert limits["gemini-3.5-flash-lite"].rpd == 500  # validado: 2,2 s, "falso" 95%


def test_moderation_primary_has_time_for_a_slow_answer() -> None:
    # 3.1-flash-lite levou 6,8 s num comentário (13/10/2026): a fatia dele era de 4,5 s.
    from app.services.moderation.ai_filter import ATTEMPT_SHARE, MODERATION_BUDGET_SECONDS

    assert MODERATION_BUDGET_SECONDS * ATTEMPT_SHARE >= 9


@pytest.mark.parametrize(
    ("error", "refunded"),
    [
        (api_error(503, "high demand"), True),
        (api_error(500, "internal"), True),
        (api_error(504, "deadline"), False),  # o Google pode ter processado
    ],
)
async def test_overload_refunds_the_model_unit(
    monkeypatch: pytest.MonkeyPatch, error: Exception, refunded: bool
) -> None:
    """503/500 = recusado sem processar: a unidade do contador diário do modelo volta, no dia
    da reserva (13/10/2026: 503 em série esgotavam os 20/dia dos Flash sem nenhuma resposta)."""
    from datetime import date

    install(monkeypatch, {"gemini-3.8-flash": [error], "gemini-3.7-flash": [text_response(ANSWER)]})
    day = date(2026, 10, 13)
    released: list[tuple[str, date]] = []

    async def budget(_: str, __: int) -> date:
        return day

    async def model_release(model: str, reserved_day: date) -> None:
        released.append((model, reserved_day))

    chain = google_generate(FAKE_KEY, ["gemini-3.8-flash", "gemini-3.7-flash"])
    result = await GeminiAnalyzer(chain).analyze(
        data(), model_budget=budget, model_release=model_release
    )

    assert result.model == "gemini-3.7-flash"  # o 3.8 falhou e a cadeia trocou
    assert released == ([("gemini-3.8-flash", day)] if refunded else [])


async def test_timeout_does_not_refund_the_model_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    install(
        monkeypatch,
        {
            "gemini-3.8-flash": [httpx.ReadTimeout("lento")],
            "gemini-3.7-flash": [text_response(ANSWER)],
        },
    )
    released: list[object] = []

    async def budget(_: str, __: int) -> object:
        from datetime import date

        return date(2026, 10, 13)

    async def model_release(*args: object) -> None:
        released.append(args)

    chain = google_generate(FAKE_KEY, ["gemini-3.8-flash", "gemini-3.7-flash"])
    result = await GeminiAnalyzer(chain).analyze(
        data(), model_budget=budget, model_release=model_release
    )
    assert result.model == "gemini-3.7-flash"
    assert released == []  # o Google pode ter processado: a unidade fica


async def test_refund_failure_does_not_break_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date

    install(monkeypatch, {"gemini-3.8-flash": [api_error(503, "x"), text_response(ANSWER)]})

    async def budget(_: str, __: int) -> date:
        return date(2026, 10, 13)

    async def broken(*_: object) -> None:
        raise ConnectionError("banco fora")

    result = await GeminiAnalyzer(google_generate(FAKE_KEY, ["gemini-3.8-flash"])).analyze(
        data(), model_budget=budget, model_release=broken
    )
    assert result.model == "gemini-3.8-flash"
