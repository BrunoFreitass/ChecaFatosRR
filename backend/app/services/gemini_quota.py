"""Cota global do dia + cota do modelo numa transação só.

Cada transação custa ~0,7 s entre o Render (Virginia) e o Supabase (São Paulo), e cada resposta
pedida ao Gemini reservava as duas em separado. Aqui a unidade global fica pendente até o 1º
modelo realmente chamado e é reservada junto com a dele; os modelos seguintes da cadeia (troca
por 503, prazo etc.) reservam só a própria. Global esgotada: GlobalQuotaRefusedError, sem
gastar nada e sem marcar o modelo como "sem cota".
"""

import logging
from datetime import date
from typing import Protocol

from app.repositories.checks import GLOBAL_QUOTA_REFUSED, GeminiPurpose, GlobalQuotaRefused
from app.services.gemini import GlobalQuotaRefusedError, GlobalQuotaUnavailableError

logger = logging.getLogger(__name__)


class QuotaRepository(Protocol):
    async def reserve_gemini_and_model(
        self, purpose: GeminiPurpose, purpose_limit: int, model: str, model_limit: int
    ) -> date | GlobalQuotaRefused | None: ...
    async def reserve_gemini_model(self, model: str, daily_limit: int) -> date | None: ...
    async def release_gemini(self, purpose: GeminiPurpose) -> None: ...
    async def release_gemini_model(self, model: str, day: date) -> None: ...


class CombinedQuota:
    """reserve/release (cota global) e model_budget/model_release (cota do modelo) no formato
    que GeminiAnalyzer e AiModerator já recebem."""

    def __init__(self, repo: QuotaRepository, purpose: GeminiPurpose, daily_limit: int) -> None:
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
        try:
            result = await self._repo.reserve_gemini_and_model(
                self._purpose, self._limit, model, rpd
            )
        except Exception as exc:
            # Sem poder contar, a IA não é chamada: a cota global nunca é furada.
            logger.warning(
                "gemini_quota_reservation_failed",
                extra={"purpose": self._purpose, "model": model},
                exc_info=True,
            )
            raise GlobalQuotaUnavailableError from exc
        if result is GLOBAL_QUOTA_REFUSED:
            self._pending = False
            logger.info("gemini_global_quota_refused", extra={"purpose": self._purpose})
            raise GlobalQuotaRefusedError
        if isinstance(
            result, date
        ):  # None = sem cota do modelo: a global segue pendente (próximo modelo)
            self._pending, self._reserved = False, True
        return result

    async def model_release(self, model: str, day: date) -> None:
        await self._repo.release_gemini_model(model, day)
