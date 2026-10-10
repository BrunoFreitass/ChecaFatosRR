"""Eval de vereditos (backend/evals/checagem.py): só a parte determinística, sem rede."""

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from evals.checagem import (
    MAX_REAL_CALLS,
    Case,
    CaseResult,
    RealRunRefusedError,
    budget_reserve,
    grade,
    load_cases,
    recording_day,
    replay,
    require_real_run,
    run_real,
)

pytestmark = pytest.mark.anyio

CASES_FILE = Path(__file__).parents[2] / "evals" / "casos_checagem.json"


def case(case_id: str, accepted: list[str], categoria: str = "noticia_real") -> Case:
    return Case(
        id=case_id,
        categoria=categoria,
        origem="sintético",
        kind="texto",
        url=None,
        titulo=None,
        conteudo="Texto de teste com conteúdo suficiente para a checagem.",
        publicado_em=None,
        checagens=[],
        aceitos=accepted,
    )


def result(
    c: Case,
    veredito: str,
    pontos: list[str] | None = None,
    pontos_ia: list[str] | None = None,
    resumo: str = "",
) -> CaseResult:
    return CaseResult(
        case=c,
        veredito=veredito,
        veredito_ia=veredito,
        pontos=pontos or [],
        pontos_ia=pontos_ia if pontos_ia is not None else (pontos or []),
        resumo=resumo,
    )


# ---------- casos ----------


def test_case_file_is_valid_and_covers_the_three_categories() -> None:
    cases = load_cases(CASES_FILE)
    assert {c.categoria for c in cases} == {"noticia_real", "boato_checado", "whatsapp"}
    assert len({c.id for c in cases}) == len(cases)


def test_invalid_expected_verdict_is_rejected(tmp_path: Path) -> None:
    raw = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    raw["casos"][0]["aceitos"] = ["talvez"]
    bad = tmp_path / "casos.json"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(bad)


# ---------- métricas ----------


def test_grade_counts_false_unverifiable_and_false_false() -> None:
    news = case("noticia", ["verdadeiro"])
    rumor = case("boato", ["falso"], "boato_checado")
    chain = case("zap", ["falso", "enganoso", "nao_verificavel"], "whatsapp")
    report = grade(
        [
            result(news, "nao_verificavel"),  # falso "Não verificável"
            result(rumor, "falso"),  # acerto
            result(chain, "verdadeiro"),  # erro, mas não é nenhum dos dois falsos medidos
        ]
    )
    assert report.total == 3
    assert report.acertos == 1
    assert report.falso_nao_verificavel == ["noticia"]
    assert report.falso_falso == []


def test_grade_flags_false_false_on_real_news() -> None:
    news = case("noticia", ["verdadeiro"])
    report = grade([result(news, "falso")])
    assert report.falso_falso == ["noticia"]


def test_grade_measures_the_date_mistake_in_the_raw_answer() -> None:
    # Mede o PROMPT: quantas vezes a IA ainda comete o erro (antes do filtro).
    news = case("noticia", ["verdadeiro"])
    raw = ["Fatos futuros: outubro de 2026"]
    report = grade([result(news, "verdadeiro", [], raw)], date(2026, 10, 9))
    assert report.erro_de_data_ia == ["noticia"]


def test_grade_lists_surviving_future_words_for_human_review() -> None:
    # Pontos com "futuro"/"ainda não ocorreu" que o filtro manteve: revisão humana.
    news = case("noticia", ["verdadeiro"])
    point = "Dá como certa a posse de 01/01/2027, que ainda não ocorreu"
    report = grade([result(news, "verdadeiro", [point])], date(2026, 10, 9))
    assert report.erro_de_data_ia == []
    assert report.revisar_futuro == ["noticia"]


# ---------- replay (sem chamar o Gemini) ----------


async def test_replay_regrades_recorded_answers_with_current_post_validation() -> None:
    news = Case(
        id="noticia-hoje",
        categoria="noticia_real",
        origem="sintético",
        kind="link",
        url="https://g1.globo.com/rr/noticia/2026/10/09/exemplo.ghtml",
        titulo="Exemplo",
        conteudo="Texto da notícia de hoje.",
        publicado_em=date(2026, 10, 9),
        checagens=[],
        aceitos=["verdadeiro"],
    )
    recorded = {
        "noticia-hoje": json.dumps(
            {
                "veredito": "verdadeiro",
                "resumo": "Notícia de veículo conhecido.",
                "pontos_suspeitos": ["Acontecimentos de outubro de 2026, projetando fatos futuros"],
                "contexto_faltante": None,
                "checagens_agencias": [],
                "fontes_sugeridas": [],
                "confianca": 85,
            }
        )
    }
    results = await replay([news], recorded, today=date(2026, 10, 9))
    assert results[0].pontos_ia == [  # a resposta crua fica para medir o prompt
        "Acontecimentos de outubro de 2026, projetando fatos futuros"
    ]
    assert results[0].veredito == "verdadeiro"
    assert results[0].pontos == []  # a pós-validação de hoje descarta o "futuro"


# ---------- trava das chamadas reais ----------


def test_real_run_needs_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHECAFATOS_EVAL_REAL", raising=False)
    with pytest.raises(RealRunRefusedError):
        require_real_run(["a"])


def test_real_run_is_capped_at_three_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECAFATOS_EVAL_REAL", "1")
    assert MAX_REAL_CALLS == 3
    require_real_run(["a", "b", "c"])
    with pytest.raises(RealRunRefusedError):
        require_real_run(["a", "b", "c", "d"])


async def test_run_real_itself_refuses_without_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    # A trava não depende de quem chama a CLI: run_real direto também recusa.
    monkeypatch.delenv("CHECAFATOS_EVAL_REAL", raising=False)
    with pytest.raises(RealRunRefusedError):
        await run_real([case("a", ["verdadeiro"])])


async def test_run_real_refuses_more_than_three_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECAFATOS_EVAL_REAL", "1")
    cases = [case(f"c{i}", ["verdadeiro"]) for i in range(4)]
    with pytest.raises(RealRunRefusedError):
        await run_real(cases)


def test_repeated_ids_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECAFATOS_EVAL_REAL", "1")
    with pytest.raises(RealRunRefusedError, match="repetid"):
        require_real_run(["a", "a"])


def test_replay_uses_the_recording_day() -> None:
    # Reproduzível: reavaliar depois de 25/10 não muda o que era "futuro" no dia da gravação.
    assert recording_day({"data": "2026-10-09"}, date(2026, 11, 1)) == date(2026, 10, 9)
    assert recording_day({}, date(2026, 11, 1)) == date(2026, 11, 1)


async def test_budget_reserve_allows_at_most_three_model_calls() -> None:
    reserve, used = budget_reserve(MAX_REAL_CALLS)
    allowed = [await reserve() for _ in range(5)]
    assert allowed == [True, True, True, False, False]
    assert used() == 3


# ---------- origem confirmada (link) ----------


def g1_case() -> Case:
    return Case(
        id="g1",
        categoria="noticia_real",
        origem="real",
        kind="link",
        url="https://g1.globo.com/rj/noticia/2026/10/10/x.ghtml",
        titulo="TRE-RJ retotaliza votos",
        conteudo="O TRE-RJ retotaliza os votos neste sábado.",
        publicado_em=date(2026, 10, 10),
        checagens=[],
        aceitos=["nao_verificavel"],
    )


def test_link_case_input_has_confirmed_origin() -> None:
    assert g1_case().to_input().source_domain == "g1.globo.com"
    assert case("t", ["verdadeiro"]).to_input().source_domain is None


@pytest.mark.parametrize(
    "texto",
    [
        "O conteúdo traz uma reportagem formatada como notícia do portal G1",
        "Detalha uma suposta reportagem do G1 sobre a retotalização",
        "A página se apresenta como matéria do G1",
        "Falta confirmar a autenticidade da matéria",
        "A página se passa por matéria do G1",
        "Pode ser uma montagem com o layout do G1",
        "Não é possível confirmar que a reportagem seja do G1",
    ],
)
def test_grade_flags_authorship_questioned_on_confirmed_links(texto: str) -> None:
    report = grade([result(g1_case(), "nao_verificavel", resumo=texto)], date(2026, 10, 10))
    assert report.autoria_questionada == ["g1"]


@pytest.mark.parametrize(
    "texto",
    [
        # Dúvida sobre FATO (permitida pela regra 10), não sobre quem publicou.
        "Não é possível confirmar se o TSE já publicou o resultado",
        "O candidato se apresenta como renovação",
        "A reportagem cita o suposto autor do crime",
        "Relata supostas fraudes investigadas pela PF",
        "O candidato teria agido supostamente sem autorização",
        "Parte do pressuposto de que a lei mudou",
    ],
)
def test_grade_does_not_flag_reported_allegations(texto: str) -> None:
    # "Suposto" para marcar uma alegação relatada é correto; o alarme é só dúvida de autoria.
    report = grade([result(g1_case(), "nao_verificavel", resumo=texto)], date(2026, 10, 10))
    assert report.autoria_questionada == []


def test_suposto_on_links_goes_to_human_review() -> None:
    # "Suposta retotalização" (o caso do teste manual): revisão humana, não falha automática.
    report = grade(
        [result(g1_case(), "nao_verificavel", resumo="Detalha uma suposta retotalização")],
        date(2026, 10, 10),
    )
    assert report.revisar_suposto == ["g1"]
    assert report.autoria_questionada == []


def test_authorship_doubt_in_missing_context_or_raw_points_counts() -> None:
    doubt = "Confirmar a autenticidade da matéria do G1"
    by_context = CaseResult(
        case=g1_case(),
        veredito="nao_verificavel",
        veredito_ia="nao_verificavel",
        pontos=[],
        contexto=doubt,
    )
    by_raw_point = result(g1_case(), "nao_verificavel", [], [doubt])
    report = grade([by_context, by_raw_point], date(2026, 10, 10))
    assert report.autoria_questionada == ["g1", "g1"]


def test_lookalike_link_case_may_question_authorship() -> None:
    # Caso de domínio que imita o veículo: a IA DEVE desconfiar; não é regressão.
    fake = replace(g1_case(), id="imita-g1", autoria_confirmada=False)
    report = grade(
        [result(fake, "falso", resumo="A página se passa por notícia do G1")], date(2026, 10, 10)
    )
    assert report.autoria_questionada == []


def test_invalid_case_url_is_reported_with_its_id(tmp_path: Path) -> None:
    raw = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    raw["casos"][0]["url"] = "http://localhost/x"
    bad = tmp_path / "casos.json"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match=raw["casos"][0]["id"]):
        load_cases(bad)


def test_grade_does_not_flag_unchecked_facts_or_pasted_text() -> None:
    ok = "Publicado pelo G1; os fatos ainda não foram checados por agências."
    pasted = "Mensagem formatada como notícia do G1, sem link para a matéria"
    report = grade(
        [
            result(g1_case(), "nao_verificavel", resumo=ok),
            result(case("zap", ["falso"], "whatsapp"), "falso", resumo=pasted),
        ],
        date(2026, 10, 10),
    )
    assert report.autoria_questionada == []


def test_real_g1_case_is_in_the_case_file() -> None:
    real = {c.id: c for c in load_cases(CASES_FILE)}["g1-tre-rj-retotalizacao"]
    assert real.kind == "link"
    assert real.url and real.url.startswith("https://g1.globo.com/")
    assert real.publicado_em == date(2026, 10, 10)
    assert real.aceitos == ["nao_verificavel"]
    assert "sintético" not in real.origem


async def test_manual_test_recording_is_clean_utf8_and_flags_the_reported_problem() -> None:
    path = CASES_FILE.parent / "gravacoes" / "20261010-teste-manual-g1.json"
    raw = path.read_text(encoding="utf-8")
    assert "retotaliza" + "ção" in raw and "Ã" not in raw  # sem dupla codificação
    recorded = json.loads(raw)["respostas"]
    cases = [c for c in load_cases(CASES_FILE) if c.id in recorded]
    results = await replay(cases, recorded, today=date(2026, 10, 10))
    report = grade(results, date(2026, 10, 10))
    # A resposta antiga (antes da regra 10) é exatamente o problema relatado.
    assert report.acertos == 1
    assert report.autoria_questionada == ["g1-tre-rj-retotalizacao"]
    assert report.erro_de_data_ia == []


def test_real_news_cases_keep_only_an_excerpt() -> None:
    # Direito autoral: casos reais guardam título + 2-3 parágrafos, nunca a matéria inteira.
    for c in load_cases(CASES_FILE):
        if c.categoria == "noticia_real" and "sintético" not in c.origem:
            paragraphs = [p for p in c.conteudo.split("\n") if p.strip()]
            assert len(paragraphs) <= 3, c.id
            assert len(c.conteudo) <= 1500, c.id
            assert "trecho" in c.origem, c.id
