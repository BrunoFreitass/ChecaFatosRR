"""Limitador em memória (janela deslizante) para rotas baratas, por usuário.

Vale por instância: com N instâncias no Cloud Run, o limite real é até N vezes o configurado.
Basta para conter abuso de POST /me e /apelido/disponivel (que gravam no log de
moderação). Os limites de checagem ficam no Postgres (repositories/checks.py).
"""

import math
import time
from collections import deque
from collections.abc import Callable

_MAX_KEYS = 10_000


class SlidingWindowLimiter:
    def __init__(
        self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> int | None:
        """Registra um uso. Devolve None se liberado, ou os segundos até liberar."""
        now = self._clock()
        if len(self._hits) > _MAX_KEYS:
            self._prune(now)
        if len(self._hits) > _MAX_KEYS:
            # Ainda cheio (todas ativas): descarta as chaves mais antigas, para não varrer
            # o dicionário inteiro a cada uso.
            for key in list(self._hits)[: len(self._hits) - _MAX_KEYS // 2]:
                del self._hits[key]
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return max(1, math.ceil(hits[0] + self._window - now))
        hits.append(now)
        return None

    def _prune(self, now: float) -> None:
        expired = [k for k, v in self._hits.items() if not v or v[-1] <= now - self._window]
        for key in expired:
            del self._hits[key]


class RateLimiters:
    """Um conjunto por aplicação (app.state), para os testes não compartilharem estado."""

    def __init__(self) -> None:
        self.profile_create = SlidingWindowLimiter(limit=10, window_seconds=600)
        self.nickname_check = SlidingWindowLimiter(limit=30, window_seconds=60)
        # Leitura pública de comentários, por IP: contém robôs simples (o IP vem do
        # X-Forwarded-For e pode ser forjado; não é a única defesa).
        self.comments_read = SlidingWindowLimiter(limit=120, window_seconds=60)
        # Denúncias por usuário, folgado: o limite de 10/dia (no banco) só conta as aceitas;
        # este freia as recusadas (repetidas, inexistentes), que também custam uma consulta.
        self.comment_report = SlidingWindowLimiter(limit=30, window_seconds=600)
