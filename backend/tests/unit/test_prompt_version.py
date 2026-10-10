"""PROMPT_VERSION: entra na chave do cache de checagens.

Mudou o prompt (instruções, schema ou o código que monta o pedido) = resultado antigo deixa de
ser servido como cache. Ele continua no link público, com a análise da época.
"""

import hashlib
import re
from datetime import UTC, date, datetime

import pytest

from app.services import gemini
from app.services.factcheck import AgencyCheck
from app.services.gemini import (
    PROMPT_VERSION,
    SYSTEM_INSTRUCTION,
    AnalysisInput,
    build_generate_config,
    build_prompt,
    prompt_version,
)


def test_prompt_version_has_revision_and_content_hash() -> None:
    assert re.fullmatch(r"\d+-[0-9a-f]{12}", PROMPT_VERSION)
    assert PROMPT_VERSION == prompt_version(gemini.PROMPT_REVISION, SYSTEM_INSTRUCTION)


def test_changing_the_instructions_changes_the_version() -> None:
    edited = SYSTEM_INSTRUCTION + "\n11. Regra nova."
    assert prompt_version(gemini.PROMPT_REVISION, edited) != PROMPT_VERSION


def test_bumping_the_revision_changes_the_version() -> None:
    # Mudança em build_prompt/finalize não aparece no texto das instruções: sobe a revisão.
    bumped = prompt_version(gemini.PROMPT_REVISION + 1, SYSTEM_INSTRUCTION)
    assert bumped != PROMPT_VERSION


# Retrato do pedido montado (build_prompt com entrada fixa + configuração de geração). Se este
# teste falhar, o código que monta o pedido mudou: SUBA gemini.PROMPT_REVISION (senão o cache
# continua servindo respostas do comportamento antigo) e registre o novo retrato aqui.
PROMPT_SNAPSHOTS = {2: "4ebb032599f7"}  # pragma: allowlist secret (hash do pedido)


def request_fingerprint(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(gemini.secrets, "token_hex", lambda _n: "0" * 12)
    data = AnalysisInput(
        kind="link",
        content="Conteúdo fixo do retrato.\nSegunda linha.",
        title="Título fixo",
        url="https://g1.globo.com/rr/noticia/2026/10/10/retrato.ghtml",
        agency_checks=[
            AgencyCheck("Lupa", "t", "Falso", "https://lupa.uol.com.br/x", "2026-10-01")
        ],
        content_urls=[],
        published_at=date(2026, 10, 10),
        source_domain="g1.globo.com",
    )
    prompt = build_prompt(data, now=datetime(2026, 10, 10, 15, 0, tzinfo=UTC))
    pasted = build_prompt(
        AnalysisInput("texto", "Mensagem fixa.", None, None, [], []),
        now=datetime(2026, 10, 10, 15, 0, tzinfo=UTC),
    )
    config = build_generate_config(thinking_budget=0)
    thinking = config.thinking_config.thinking_budget if config.thinking_config else None
    knobs = (
        f"{config.temperature}|{config.max_output_tokens}|{config.response_mime_type}|{thinking}"
    )
    return hashlib.sha256(f"{prompt}\n---\n{pasted}\n---\n{knobs}".encode()).hexdigest()[:12]


def test_request_changes_require_a_revision_bump(monkeypatch: pytest.MonkeyPatch) -> None:
    assert request_fingerprint(monkeypatch) == PROMPT_SNAPSHOTS.get(gemini.PROMPT_REVISION), (
        "O pedido ao Gemini mudou: suba PROMPT_REVISION e registre o novo retrato."
    )
