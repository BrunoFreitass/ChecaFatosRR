"""Dublê do AsyncSession do SQLAlchemy, com a MESMA interface assíncrona.

`execute` e `scalar` são corrotinas, como no real: um `await` esquecido ou mal posicionado
(`await session.execute(...).mappings()`) quebra o teste com AttributeError e com o aviso
"coroutine was never awaited" (que vira erro com `pytest -W error`).

As respostas são roteiros: `session.on("trecho do SQL", rows=[...])` ou `scalar=...`. Toda
instrução precisa rodar dentro de `async with session.begin()` (convenção dos repositórios).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

_MISSING = object()


class FakeResult:
    def __init__(self, rows: list[Mapping[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "FakeResult":
        return self

    def one(self) -> Mapping[str, Any]:
        if len(self._rows) != 1:
            raise AssertionError(f"one() esperava 1 linha, veio {len(self._rows)}")
        return self._rows[0]

    def first(self) -> Mapping[str, Any] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Mapping[str, Any]]:
        return list(self._rows)


@dataclass
class _Rule:
    fragment: str
    rows: list[Mapping[str, Any]] | None
    scalar: Any
    when: Callable[[dict[str, Any]], bool] | None
    once: bool


@dataclass
class Statement:
    sql: str
    params: dict[str, Any] = field(default_factory=dict)


class _Transaction:
    def __init__(self, session: "FakeAsyncSession") -> None:
        self._session = session

    async def __aenter__(self) -> "_Transaction":
        if self._session.in_transaction:
            raise AssertionError("begin() dentro de outra transação")
        self._session.in_transaction = True
        self._session.transactions += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._session.in_transaction = False


class FakeAsyncSession:
    def __init__(self) -> None:
        self._rules: list[_Rule] = []
        self.statements: list[Statement] = []
        self.in_transaction = False
        self.transactions = 0

    def on(
        self,
        fragment: str,
        *,
        rows: list[Mapping[str, Any]] | None = None,
        scalar: Any = _MISSING,
        when: Callable[[dict[str, Any]], bool] | None = None,
        once: bool = False,
    ) -> "FakeAsyncSession":
        self._rules.append(_Rule(" ".join(fragment.split()), rows, scalar, when, once))
        return self

    def begin(self) -> _Transaction:
        return _Transaction(self)

    def _match(self, statement: Any, params: dict[str, Any] | None) -> _Rule | None:
        if not self.in_transaction:
            raise AssertionError("instrução fora de `async with session.begin()`")
        sql = " ".join(str(statement).split())
        params = dict(params or {})
        self.statements.append(Statement(sql, params))
        for rule in self._rules:
            if rule.fragment in sql and (rule.when is None or rule.when(params)):
                if rule.once:
                    self._rules.remove(rule)
                return rule
        return None

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> FakeResult:
        rule = self._match(statement, params)
        if rule is None or rule.rows is None:
            return FakeResult([])
        return FakeResult(rule.rows)

    async def scalar(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        rule = self._match(statement, params)
        if rule is None or rule.scalar is _MISSING:
            return None
        return rule.scalar

    def sql_log(self) -> list[str]:
        return [statement.sql for statement in self.statements]
