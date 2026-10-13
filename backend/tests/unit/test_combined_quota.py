"""Cota global + cota do modelo numa transação só (13/10/2026: cada transação custa ~0,7 s entre
o Render na Virginia e o Supabase em São Paulo). A unidade global é reservada junto com a do
1º modelo efetivamente chamado; se a global acabou, nada é gasto e o modelo NÃO fica marcado
como "sem cota". Só mocks: nenhuma chamada ao Google."""

from datetime import date
from typing import Any

import pytest

from app.repositories.checks import GLOBAL_QUOTA_REFUSED
from app.services.gemini import (
    GeminiAnalyzer,
    GeminiQuotaExhaustedError,
    GlobalQuotaRefusedError,
    google_generate,
)
from app.services.gemini_quota import CombinedQuota
from app.services.moderation.ai_filter import google_moderator
from app.services.moderation.pipeline import AiUnavailable
from tests.unit.test_gemini_client import ANSWER, FAKE_KEY, data, text_response
from tests.unit.test_model_pools import install

pytestmark = pytest.mark.anyio
DAY = date(2026, 10, 13)


class Repo:
    """Repositório falso: registra cada ida ao banco (cada uma é uma transação)."""

    def __init__(self, combined: Any = DAY, model_only: Any = DAY) -> None:
        self.combined_answer = combined
        self.model_only_answer = model_only
        self.calls: list[tuple[Any, ...]] = []

    async def reserve_gemini_and_model(
        self, purpose: str, purpose_limit: int, model: str, model_limit: int
    ) -> Any:
        self.calls.append(("ambas", purpose, model))
        return self.combined_answer

    async def reserve_gemini_model(self, model: str, daily_limit: int) -> Any:
        self.calls.append(("modelo", model))
        return self.model_only_answer

    async def release_gemini(self, purpose: str) -> None:
        self.calls.append(("devolve_global", purpose))

    async def release_gemini_model(self, model: str, day: date) -> None:
        self.calls.append(("devolve_modelo", model, day))


async def test_global_unit_goes_with_the_first_model_only() -> None:
    repo = Repo()
    quota = CombinedQuota(repo, "checagem", 1000)

    assert await quota.reserve() is True  # adiada: nenhuma ida ao banco
    assert repo.calls == []
    assert await quota.model_budget("gemini-3.8-flash", 20) == DAY
    assert await quota.model_budget("gemini-3.7-flash", 20) == DAY  # troca de modelo

    assert repo.calls == [
        ("ambas", "checagem", "gemini-3.8-flash"),
        ("modelo", "gemini-3.7-flash"),
    ]


async def test_each_answer_reserves_its_own_global_unit() -> None:
    repo = Repo()
    quota = CombinedQuota(repo, "checagem", 1000)
    await quota.reserve()
    await quota.model_budget("gemini-3.8-flash", 20)

    await quota.reserve()  # 2ª resposta (a 1ª veio fora do schema)
    await quota.model_budget("gemini-3.8-flash", 20)

    assert [c[0] for c in repo.calls] == ["ambas", "ambas"]


async def test_global_refusal_raises_its_own_error() -> None:
    quota = CombinedQuota(Repo(combined=GLOBAL_QUOTA_REFUSED), "checagem", 1000)
    await quota.reserve()

    with pytest.raises(GlobalQuotaRefusedError):
        await quota.model_budget("gemini-3.8-flash", 20)


async def test_release_only_returns_a_global_unit_that_was_reserved() -> None:
    repo = Repo()
    quota = CombinedQuota(repo, "checagem", 1000)
    await quota.reserve()
    await quota.release()  # nenhum modelo foi chamado: nada reservado, nada a devolver
    assert repo.calls == []

    await quota.reserve()
    await quota.model_budget("gemini-3.8-flash", 20)
    await quota.release()
    assert repo.calls[-1] == ("devolve_global", "checagem")


async def test_check_without_global_quota_is_out_of_quota_and_model_stays_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global esgotada: a checagem sai como "sem cota" (o serviço serve a análise anterior), e o
    3.8 não vai para a lista de "sem cota" por 1 h por causa disso."""
    models = install(monkeypatch, {"gemini-3.8-flash": [text_response(ANSWER)]})
    chain = google_generate(FAKE_KEY, ["gemini-3.8-flash"])
    refused = CombinedQuota(Repo(combined=GLOBAL_QUOTA_REFUSED), "checagem", 1000)

    with pytest.raises(GeminiQuotaExhaustedError):
        await GeminiAnalyzer(chain).analyze(
            data(), refused.reserve, refused.release, model_budget=refused.model_budget
        )
    assert models.models_called == []

    ok = CombinedQuota(Repo(), "checagem", 1000)
    result = await GeminiAnalyzer(chain).analyze(
        data(), ok.reserve, ok.release, model_budget=ok.model_budget
    )
    assert result.model == "gemini-3.8-flash"  # não ficou marcado como sem cota


async def test_moderation_without_global_quota_is_unavailable_out_of_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = install(monkeypatch, {"gemini-3.1-flash-lite": [text_response(ANSWER)]})
    refused = CombinedQuota(Repo(combined=GLOBAL_QUOTA_REFUSED), "moderacao", 2000)

    result = await google_moderator(FAKE_KEY, ["gemini-3.1-flash-lite"]).classify(
        "Comentário.", refused.reserve, model_budget=refused.model_budget
    )

    assert result == AiUnavailable("sem_cota")
    assert models.models_called == []
