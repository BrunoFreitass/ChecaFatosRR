import pytest
from app.repositories.checks import Limits, StoredCheck
from app.routers.checks import _build_fallback_transparency, to_out
from app.services.check_service import CheckSettings, build_transparency_metadata
from app.services.factcheck import AgencyCheck
from app.services.gemini import AnalysisInput
from datetime import UTC, datetime


def test_build_transparency_metadata_link() -> None:
    data = AnalysisInput(
        kind="link",
        content="Texto da matéria",
        title="Título da Notícia",
        url="https://g1.globo.com/noticia/teste",
        agency_checks=[AgencyCheck("Lupa", "Título", "Falso", "https://lupa.uol.com.br/x")],
        content_urls=[],
        source_domain="g1.globo.com",
    )
    res = build_transparency_metadata(
        data=data,
        final_domain="g1.globo.com",
        agency_checks=data.agency_checks,
        agencies_failed=False,
        model="gemini-3.8-flash",
    )
    assert res["extrator"] == "trafilatura"
    assert "Web (g1.globo.com)" in res["plataformas_consultadas"]
    assert "Google Fact Check Tools API" in res["plataformas_consultadas"]
    assert "Agência Lupa" in res["plataformas_consultadas"]
    assert "Google Gemini (gemini-3.8-flash)" in res["plataformas_consultadas"]
    assert "g1.globo.com" in res["fontes_identificadas"]
    etapas = [e["etapa"] for e in res["etapas"]]
    assert "Extração de Conteúdo Web" in etapas
    assert "Cruzamento com Base de Checagens" in etapas
    assert "Validação Cognitiva e Consistência" in etapas


def test_build_transparency_metadata_texto() -> None:
    data = AnalysisInput(
        kind="texto",
        content="Mensagem encaminhada no WhatsApp com link https://site.com/aviso",
        title=None,
        url=None,
        agency_checks=[],
        content_urls=["https://site.com/aviso"],
    )
    res = build_transparency_metadata(
        data=data,
        final_domain=None,
        agency_checks=[],
        agencies_failed=False,
        model="gemini-3.8-flash",
    )
    assert res["extrator"] == "texto_direto"
    assert "site.com" in res["fontes_identificadas"]
    assert any("Processamento de Mensagem" in e["etapa"] for e in res["etapas"])


def test_build_transparency_metadata_agencies_only() -> None:
    data = AnalysisInput(
        kind="link",
        content="Notícia",
        title="Título",
        url="https://folhabv.com.br/x",
        agency_checks=[],
        content_urls=[],
        source_domain="folhabv.com.br",
    )
    res = build_transparency_metadata(
        data=data,
        final_domain="folhabv.com.br",
        agency_checks=[],
        agencies_failed=True,
        model=None,
        agencies_only=True,
    )
    assert any("Consolidação de Evidências" in e["etapa"] for e in res["etapas"])
    alerta_steps = [e for e in res["etapas"] if e["status"] == "alerta"]
    assert len(alerta_steps) >= 1


def test_to_out_generates_fallback_transparency() -> None:
    settings = CheckSettings(
        limits=Limits(
            cache_hours=24,
            checks_per_hour=10,
            checks_per_day=20,
            lookups_per_hour=60,
            prompt_version="v-teste",
        ),
        daily_gemini_checks=200,
        model="gemini-3.8-flash",
    )
    stored = StoredCheck(
        public_id="k7mqp9xwvrzt",
        kind="link",
        url="https://g1.globo.com/rj/noticia/ruas.ghtml",
        domain="g1.globo.com",
        title="Título da notícia",
        published_at=None,
        texto=None,
        veredito="nao_verificavel",
        confianca=80,
        result={"resumo": "Resumo de teste"},
        created_at=datetime.now(UTC),
        prompt_version="v-teste",
    )
    out = to_out(stored, settings)
    assert out.transparencia is not None
    assert out.transparencia.extrator == "trafilatura"
    assert "Web (g1.globo.com)" in out.transparencia.plataformas_consultadas
    assert len(out.transparencia.etapas) == 3
