"""Cota global do dia + cota do modelo numa transação só.

Cada transação custa ~0,7 s entre o Render (Virginia) e o Supabase (São Paulo), e cada resposta
pedida ao Gemini reservava as duas em separado. Aqui a unidade global fica pendente até o 1º
modelo realmente chamado e é reservada junto com a dele; os modelos seguintes da cadeia (troca
por 503, prazo etc.) reservam só a própria. Global esgotada: GlobalQuotaRefusedError, sem
gastar nada e sem marcar o modelo como "sem cota".
"""

from datetime import date
from typing import Any, Protocol

from app.repositories.checks import GLOBAL_QUOTA_REFUSED
from app.services.gemini import GlobalQuotaRefusedError


class QuotaRepository(Protocol):
    async def reserve_gemini_and_model(
        self, purpose: Any, purpose_limit: int, model: str, model_limit: int
    ) -> Any: ...
    async def reserve_gemini_model(self, model: str, daily_limit: int) -> date | None: ...
    async def release_gemini(self, purpose: Any) -> None: ...
    async def release_gemini_model(self, model: str, day: date) -> None: ...


class CombinedQuota:
    """reserve/release (cota global) e model_budget/model_release (cota do modelo) no formato
    que GeminiAnalyzer e AiModerator já recebem."""

    def __init__(self, repo: QuotaRepository, purpose: Any, daily_limit: int) -> None:
        self._repo = repo
        self._purpose = purpose
        self._limit = daily_limit
        self._pending = False  # unidade global pedida, ainda não reservada no banco
        self._reserved = False  # unidade global desta resposta reservada (para devolver)

    async def reserve(self) -> bool:
        """Uma unidade global por resposta pedida: fica pendente até o 1º modelo chamado."""
        self._pending, self._reserved = True, False
        return True

    async def release(self) -> None:
        """Nenhuma requisição chegou ao Google: a unidade global volta, se foi reservada."""
        if self._reserved:
            self._reserved = False
            await self._repo.release_gemini(self._purpose)

    async def model_budget(self, model: str, rpd: int) -> date | None:
        if not self._pending:
            return await self._repo.reserve_gemini_model(model, rpd)
        result = await self._repo.reserve_gemini_and_model(self._purpose, self._limit, model, rpd)
        if result is GLOBAL_QUOTA_REFUSED:
            self._pending = False
            raise GlobalQuotaRefusedError
        if result is not None:  # sem cota do modelo: a global segue pendente (próximo modelo)
            self._pending, self._reserved = False, True
        return result

    async def model_release(self, model: str, day: date) -> None:
        await self._repo.release_gemini_model(model, day)
