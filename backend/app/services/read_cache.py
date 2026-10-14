"""Cache curto, no processo, para leituras públicas (PR cache-leitura, teste de carga 14/10/2026).

/checks/latest e /comments ficavam ~1 s no banco (Virgínia↔São Paulo) a cada visita. Aqui:
- cada chave vale TTL segundos; pedidos simultâneos da mesma chave fazem UMA leitura;
- invalidate() apaga tudo na hora (comentário criado, apagado, moderado, denunciado, usuário
  banido): a próxima leitura vai ao banco e o autor vê o próprio comentário;
- leitura que começou ANTES de uma invalidação não é guardada nem compartilhada com quem pede
  depois (geração);
- tamanho limitado (ids aleatórios em ?checagem= não fazem a memória crescer sem fim).
Só respostas públicas, iguais para todos: nada autenticado ou pessoal entra aqui.
"""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

# Teste de carga: home e Comunidade a cada visita. 15 s para a lista de checagens (só muda com
# checagem nova, e essa invalida na hora); 10 s para comentários.
LATEST_TTL = 15.0
COMMENTS_TTL = 10.0


class ReadCache[T]:
    def __init__(
        self,
        ttl: float,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._clock = clock
        self._entries: OrderedDict[Hashable, tuple[float, T]] = OrderedDict()
        self._loading: dict[tuple[int, Hashable], asyncio.Future[T]] = {}
        self._generation = 0

    def __len__(self) -> int:
        return len(self._entries)

    async def get(self, key: Hashable, load: Callable[[], Awaitable[T]]) -> T:
        entry = self._entries.get(key)
        if entry is not None and entry[0] > self._clock():
            self._entries.move_to_end(key)
            return entry[1]
        generation = self._generation
        task = self._loading.get((generation, key))
        if task is None:
            task = asyncio.ensure_future(load())
            self._loading[(generation, key)] = task
            task.add_done_callback(lambda done: self._finish(generation, key, done))
        # shield: quem desiste (conexão fechada) não cancela a leitura dos outros.
        return await asyncio.shield(task)

    def _finish(self, generation: int, key: Hashable, task: asyncio.Future[T]) -> None:
        self._loading.pop((generation, key), None)
        if task.cancelled() or task.exception() is not None:
            return  # erro não fica guardado: o próximo pedido tenta de novo
        if generation != self._generation:
            return  # houve escrita no meio: esta leitura pode estar velha
        self._entries[key] = (self._clock() + self._ttl, task.result())
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def invalidate(self) -> None:
        self._generation += 1
        self._entries.clear()


@dataclass
class ReadCaches:
    """Os caches do processo (app.state.read_caches)."""

    latest: ReadCache[Any] = field(default_factory=lambda: ReadCache(ttl=LATEST_TTL))
    comments: ReadCache[Any] = field(default_factory=lambda: ReadCache(ttl=COMMENTS_TTL))


def get_read_caches(request: Request) -> ReadCaches:
    return request.app.state.read_caches  # type: ignore[no-any-return]
