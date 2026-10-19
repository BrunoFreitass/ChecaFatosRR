from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from app.repositories.checks import AGENCIES_ONLY_VERSION, Limits, StoredCheck
from app.routers.checks import to_out
from app.services.check_service import CheckSettings
from app.services.verdict_policy import (
    AGENCIES_ONLY_NONE,
    AGENCIES_ONLY_SEARCH_FAILED,
    agencies_only_result,
    neutral_summary,
)

SETTINGS = CheckSettings(
    limits=Limits(cache_hours=24, checks_per_hour=10, lookups_per_hour=60, prompt_version="v"),
    daily_gemini_checks=200,
    model="gemini-teste",
)


def stored(result: dict[str, Any], *, prompt_version: str = "v") -> StoredCheck:
    return StoredCheck(
        public_id="k7mqp9xwvrzt",
        kind="link",
        url="https://g1.globo.com/rr/noticia.ghtml",
        domain="g1.globo.com",
        title="Título",
        published_at=None,
        texto=None,
        veredito="falso",
        confianca=80,
        result={"resumo": "Resumo.", **result},
        created_at=datetime.now(UTC),
        prompt_version=prompt_version,
    )


def test_status_recorded_at_check_time_is_shown() -> None:
    assert to_out(stored({"busca_agencias": "falhou"}), SETTINGS).busca_agencias == "falhou"
    assert to_out(stored({"busca_agencias": "ok"}), SETTINGS).busca_agencias == "ok"


def test_old_ai_check_without_the_record_is_unknown_not_invented() -> None:
    assert to_out(stored({}), SETTINGS).busca_agencias is None
    assert to_out(stored({"busca_agencias": "talvez"}), SETTINGS).busca_agencias is None


def test_old_agencies_only_check_is_read_from_its_honest_summary() -> None:
    failed = stored(
        {"modo": "agencias", "resumo": AGENCIES_ONLY_SEARCH_FAILED},
        prompt_version=AGENCIES_ONLY_VERSION,
    )
    none = stored(
        {"modo": "agencias", "resumo": AGENCIES_ONLY_NONE}, prompt_version=AGENCIES_ONLY_VERSION
    )
    assert to_out(failed, SETTINGS).busca_agencias == "falhou"
    assert to_out(none, SETTINGS).busca_agencias == "ok"


def test_agencies_only_result_records_the_search_status() -> None:
    assert agencies_only_result([], "t", search_failed=True)["busca_agencias"] == "falhou"
    assert agencies_only_result([], "t")["busca_agencias"] == "ok"


def test_neutral_summary_with_a_failed_search() -> None:
    text = neutral_summary(
        "link", "g1.globo.com", "Título", confirmed_origin=True, search_failed=True
    )
    assert "não encontramos" not in text.lower()
    assert text.endswith("A busca nas agências de checagem falhou nesta análise.")
    pasted = neutral_summary("texto", None, None, confirmed_origin=False, search_failed=True)
    assert pasted == "A busca nas agências de checagem falhou nesta análise."
    # Sem falha, o texto de antes.
    assert neutral_summary("texto", None, None, confirmed_origin=False) == (
        "Não encontramos checagens de agências sobre este conteúdo."
    )


def test_true_already_downgraded_by_finalize_with_failed_search_says_the_search_failed() -> None:
    # Como o finalize grava hoje: "verdadeiro" sem agência vira nao_verificavel com o texto
    # neutro "não encontramos". Com a busca falhada, a leitura troca pelo texto da falha.
    finalize_text = neutral_summary("link", "g1.globo.com", "Título", confirmed_origin=True)
    check = stored(
        {
            "veredito_ia": "verdadeiro",
            "busca_agencias": "falhou",
            "origem_confirmada": "g1.globo.com",
            "resumo": finalize_text,
        }
    )
    check = replace(check, veredito="nao_verificavel")

    out = to_out(check, SETTINGS)

    assert "não encontramos" not in out.resumo.lower()
    assert out.resumo == (
        "Conteúdo publicado em g1.globo.com (origem confirmada) sobre: Título. "
        "A busca nas agências de checagem falhou nesta análise."
    )


def test_downgraded_with_successful_search_keeps_the_finalize_text() -> None:
    finalize_text = neutral_summary("link", "g1.globo.com", "Título", confirmed_origin=True)
    check = replace(
        stored({"veredito_ia": "verdadeiro", "busca_agencias": "ok", "resumo": finalize_text}),
        veredito="nao_verificavel",
    )
    assert to_out(check, SETTINGS).resumo == finalize_text


def test_ai_text_is_not_rewritten_when_the_model_did_not_say_true() -> None:
    # Só o texto neutro (nosso) é refeito; o resumo da IA para "falso" fica como está.
    check = stored({"veredito_ia": "falso", "busca_agencias": "falhou", "resumo": "Texto da IA."})
    assert to_out(check, SETTINGS).resumo == "Texto da IA."


def test_pasted_text_true_with_failed_search_gets_only_the_failure_sentence() -> None:
    check = replace(
        stored({"veredito_ia": "verdadeiro", "busca_agencias": "falhou", "resumo": "x"}),
        kind="texto",
        url=None,
        domain=None,
        texto="Mensagem colada.",
        veredito="nao_verificavel",
    )
    assert (
        to_out(check, SETTINGS).resumo == "A busca nas agências de checagem falhou nesta análise."
    )


def test_failed_status_with_agencies_present_is_not_rewritten() -> None:
    agency = {
        "agencia": "Lupa",
        "titulo": "t",
        "veredito": "Falso",
        "url": "https://lupa.uol.com.br/x",
    }
    check = replace(
        stored(
            {
                "veredito_ia": "verdadeiro",
                "busca_agencias": "falhou",
                "checagens_agencias": [agency],
                "resumo": "Texto gravado.",
            }
        ),
        veredito="nao_verificavel",
    )
    assert to_out(check, SETTINGS).resumo == "Texto gravado."
