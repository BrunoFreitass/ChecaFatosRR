"""Checagem estática: nenhum `await` aplicado ao fim de uma cadeia sobre o banco.

`await session.execute(...).mappings().one()` faz o await valer para `.one()` e chama
`.mappings()` na corrotina — erro que só aparece em produção se o caminho não tiver teste.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
ASYNC_SESSION_METHODS = {"execute", "scalar", "scalars", "stream", "get", "commit", "flush"}


def misplaced_awaits(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        value = node.value
        while isinstance(value, (ast.Call, ast.Attribute)):
            inner = value.func if isinstance(value, ast.Call) else value.value
            # <algo>.metodo onde <algo> é a chamada assíncrona: o await ficou no lugar errado.
            if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Call):
                call = inner.value.func
                if isinstance(call, ast.Attribute) and call.attr in ASYNC_SESSION_METHODS:
                    lines.append(node.lineno)
                    break
            value = inner
    return lines


def test_detector_finds_the_bug_pattern() -> None:
    bad = "async def f(s):\n    return await s.execute(q).mappings().one()\n"
    good = "async def f(s):\n    r = await s.execute(q)\n    return r.mappings().one()\n"
    assert misplaced_awaits(ast.parse(bad)) == [2]
    assert misplaced_awaits(ast.parse(good)) == []


def test_no_misplaced_await_in_app() -> None:
    problems = [
        f"{path.relative_to(APP.parent)}:{line}"
        for path in sorted(APP.rglob("*.py"))
        for line in misplaced_awaits(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert problems == []
