"""Tokens gastos na checagem (painel do admin, plano mapa-e-painel-admin §B): somados de todas as
respostas do Gemini da análise e devolvidos no CheckResult, sem mudar prompt nem comportamento."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services.gemini import GeminiAnalyzer, google_generate
from tests.unit.test_gemini_client import (
    ANSWER,
    FAKE_KEY,
    MODEL,
    data,
    install_fake_client,
    text_response,
)

pytestmark = pytest.mark.anyio


def usage(tokens_in: int | None, tokens_out: int | None, thoughts: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_token_count=tokens_in,
        candidates_token_count=tokens_out,
        thoughts_token_count=thoughts,
    )


async def test_tokens_of_the_answer_come_back_in_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(monkeypatch, [text_response(ANSWER, usage=usage(2100, 380, 0))])

    result = await GeminiAnalyzer(google_generate(FAKE_KEY, [MODEL])).analyze(data())

    assert result.tokens == {"in": 2100, "out": 380, "thoughts": 0}


async def test_an_answer_outside_the_schema_also_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 1ª resposta veio fora do schema (gastou tokens) e a 2ª valeu: o painel soma as duas.
    install_fake_client(
        monkeypatch,
        [
            text_response(json.dumps({"x": 1}), usage=usage(2000, 50, 10)),
            text_response(ANSWER, usage=usage(2100, 380, None)),
        ],
    )

    result = await GeminiAnalyzer(google_generate(FAKE_KEY, [MODEL])).analyze(data())

    assert result.tokens == {"in": 4100, "out": 430, "thoughts": 10}


async def test_no_usage_metadata_means_no_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_client(monkeypatch, [text_response(ANSWER)])

    result = await GeminiAnalyzer(google_generate(FAKE_KEY, [MODEL])).analyze(data())

    assert result.tokens is None


async def test_tokens_never_go_to_the_public_json(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_client(monkeypatch, [text_response(ANSWER, usage=usage(10, 5, 0))])

    result = await GeminiAnalyzer(google_generate(FAKE_KEY, [MODEL])).analyze(data())

    assert "tokens" not in result.to_json()


async def test_calls_outside_an_analysis_still_work(monkeypatch: pytest.MonkeyPatch) -> None:
    # Moderação e outros usos chamam o Gemini sem análise em volta: nada a somar, nada quebra.
    install_fake_client(monkeypatch, [text_response(ANSWER, usage=usage(10, 5, 0))])

    generated = await google_generate(FAKE_KEY, [MODEL])("prompt")
    assert getattr(generated, "text", generated) == ANSWER


async def test_two_analyses_at_the_same_time_do_not_mix_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(
        monkeypatch,
        [
            text_response(ANSWER, usage=usage(100, 1, 0)),
            text_response(ANSWER, usage=usage(7, 2, 0)),
        ],
    )
    analyzer = GeminiAnalyzer(google_generate(FAKE_KEY, [MODEL]))

    first, second = await asyncio.gather(analyzer.analyze(data()), analyzer.analyze(data()))

    assert first.tokens is not None and second.tokens is not None
    assert {first.tokens["in"], second.tokens["in"]} == {100, 7}
