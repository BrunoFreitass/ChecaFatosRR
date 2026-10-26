""" "Últimas" com checagens de agência (26/10/2026), contra Postgres real.

Checagem de agência gravada sem IA (listed = false, versão "agencias") entra na lista; o modo
"só agências" e as mensagens coladas continuam fora. A lista traz as citadas e as relacionadas
para reconhecer, nas checagens antigas, o link que é a própria checagem (sem reescrever o banco).
"""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.checks import LinkSubject, NewCheck, SqlCheckRepository, Started, TextSubject
from tests.integration.test_checks_db import LIMITS, VERSION, link, message, new_user

pytestmark = pytest.mark.anyio

PAGE = {
    "agencia": "Estadão",
    "titulo": "Homem que aparece em vídeo não foi preso",
    "veredito": "Enganoso",
    "url": "https://www.estadao.com.br/estadao-verifica/x/",
    "alegacao": "Idoso preso por Pix",
}


async def _save(
    repo: SqlCheckRepository,
    user_id: UUID,
    subject: LinkSubject | TextSubject,
    result: dict[str, Any],
    *,
    listed: bool,
    version: str,
) -> str:
    started = await repo.begin_check(user_id, subject, LIMITS)
    assert isinstance(started, Started)
    stored = await repo.save_result(
        NewCheck(
            prompt_version=version,
            subject=started.subject,
            user_id=user_id,
            veredito="nao_verificavel",
            confianca=0,
            result=result,
            model=version,
            listed=listed,
            title=None,
            published_at=None,
            claim_token=started.claim_token,
        )
    )
    return stored.public_id


async def test_latest_includes_agency_checks_saved_unlisted_and_never_texts(
    session: AsyncSession,
) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    agency_page = {"modo": "checagem_de_agencia", "checagem_de_agencia": PAGE}
    agencia = await _save(repo, user_id, link(), agency_page, listed=False, version="agencias")
    so_agencias = await _save(
        repo, user_id, link(), {"modo": "agencias"}, listed=False, version="agencias"
    )
    texto = await _save(repo, user_id, message(), agency_page, listed=False, version="agencias")

    items = {item.public_id: item for item in await repo.latest(None, 1000)}

    assert agencia in items
    assert so_agencias not in items
    assert texto not in items  # mensagem colada nunca
    item = items[agencia]
    assert item.url is not None and item.url.startswith("https://exemplo.com.br/")
    assert item.agency_view["modo"] == "checagem_de_agencia"
    assert item.agency_view["checagem_de_agencia"]["veredito"] == "Enganoso"


async def test_latest_brings_the_cited_and_related_checks_for_the_agency_rule(
    session: AsyncSession,
) -> None:
    repo = SqlCheckRepository(session)
    user_id = await new_user(session)
    cited = [{"agencia": "Lupa", "titulo": "t", "veredito": "Falso", "url": "https://l.org/1"}]
    related = [{"agencia": "AFP", "titulo": "u", "veredito": "Falso", "url": "https://a.org/2"}]
    pid = await _save(
        repo,
        user_id,
        link(),
        {"checagens_agencias": cited, "checagens_relacionadas": related},
        listed=True,
        version=VERSION,
    )
    item = next(i for i in await repo.latest(None, 1000) if i.public_id == pid)
    assert item.agency_view["checagens_agencias"] == cited
    assert item.agency_view["checagens_relacionadas"] == related
    assert item.agency_view["modo"] is None
