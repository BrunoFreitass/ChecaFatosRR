"""Decisão da moderação de comentários (camadas 1 e 2) — TESTE OBRIGATÓRIO (etapa 4).

Regras combinadas (plano §9 e decisões de 12/10/2026):
- filtro local bloqueou: não publica, strike (as categorias locais são todas de strike);
- IA, categoria de strike: bloqueia sempre; strike automático só com confiança >= 0,85,
  abaixo disso vai para a fila do admin (strike_confirmation / ai_low_confidence);
- IA "ofensivo"/"spam": bloqueia com aviso, SEM strike;
- IA "desinformacao": publica e vai para a fila (sem bloqueio nem strike);
- Gemini fora, sem cota ou fora do schema: fail-open, publica e vai para a fila.
"""

import pytest

from app.services.moderation.local_filter import CLEAN, check_text
from app.services.moderation.pipeline import (
    AI_STRIKE_MIN_CONFIDENCE,
    AiRefused,
    AiUnavailable,
    AiVerdict,
    decide,
)


def test_local_block_is_a_strike_and_never_reaches_the_ai() -> None:
    decision = decide(check_text("que porra é essa"), None)

    assert not decision.publish
    assert decision.actor == "local_filter"
    assert decision.category == "palavrao"
    assert decision.strike is True
    assert decision.queue is None
    assert decision.rule and decision.explanation


def test_clean_text_without_an_ai_answer_is_a_bug() -> None:
    with pytest.raises(ValueError):
        decide(CLEAN, None)


def test_ai_ok_publishes() -> None:
    decision = decide(CLEAN, AiVerdict("ok", 0.99))
    assert decision.publish and not decision.strike and decision.queue is None


@pytest.mark.parametrize(
    "category", ["palavrao", "sexual", "crime", "discurso_de_odio", "incitacao_violencia"]
)
def test_ai_strike_category_with_high_confidence_blocks_with_strike(category: str) -> None:
    decision = decide(CLEAN, AiVerdict(category, AI_STRIKE_MIN_CONFIDENCE))  # type: ignore[arg-type]

    assert not decision.publish
    assert decision.actor == "ai_filter"
    assert decision.strike is True
    assert decision.queue is None
    assert decision.rule and decision.explanation  # o aviso de violação sempre aparece


def test_ai_strike_category_with_low_confidence_blocks_and_asks_the_admin() -> None:
    decision = decide(CLEAN, AiVerdict("crime", 0.84))

    assert not decision.publish  # bloqueia mesmo assim (e mostra o aviso)
    assert decision.strike is False
    assert decision.queue == "ai_low_confidence"
    assert decision.confidence == 0.84
    assert decision.rule and decision.explanation


@pytest.mark.parametrize("category", ["ofensivo", "spam"])
def test_offensive_and_spam_block_without_strike(category: str) -> None:
    decision = decide(CLEAN, AiVerdict(category, 0.99))  # type: ignore[arg-type]

    assert not decision.publish
    assert decision.strike is False
    assert decision.queue is None
    assert decision.counts_for_cooldown
    assert decision.rule and decision.explanation


def test_strike_blocks_do_not_count_for_the_offensive_cooldown() -> None:
    assert not decide(check_text("porra"), None).counts_for_cooldown
    assert not decide(CLEAN, AiVerdict("crime", 0.99)).counts_for_cooldown


def test_misinformation_is_published_and_queued_without_strike() -> None:
    decision = decide(CLEAN, AiVerdict("desinformacao", 0.99))

    assert decision.publish
    assert decision.strike is False
    assert decision.queue == "desinformacao"


@pytest.mark.parametrize("reason", ["indisponivel", "sem_cota", "resposta_invalida"])
def test_ai_unavailable_fails_open_into_the_queue(reason: str) -> None:
    decision = decide(CLEAN, AiUnavailable(reason))  # type: ignore[arg-type]

    assert decision.publish
    assert decision.strike is False
    assert decision.queue == "nao_moderado_ia"


def test_every_ai_category_has_a_violation_notice() -> None:
    for category in ["palavrao", "sexual", "crime", "discurso_de_odio", "incitacao_violencia"]:
        assert decide(CLEAN, AiVerdict(category, 0.5)).rule  # type: ignore[arg-type]


def test_content_refused_by_the_ai_is_blocked_and_sent_to_the_admin() -> None:
    # Revisão de segurança: o bloqueio de segurança do próprio Gemini (resposta vazia por
    # SAFETY) não pode virar fail-open: é justamente o pior conteúdo.
    decision = decide(CLEAN, AiRefused())

    assert not decision.publish
    assert decision.strike is False  # o admin decide se é strike
    assert decision.queue == "ai_low_confidence"
    assert decision.actor == "ai_filter"
    assert decision.rule and decision.explanation
