"""Restricted python_assert evaluator for M2 machine verification.

This is intentionally an expression-only backend. It exposes read-only bindings
(`output`, `input`, `value`) and a tiny set of pure helpers; imports, attribute
access, comprehensions, lambdas, assignments, and arbitrary calls are rejected
before evaluation.
"""
from __future__ import annotations

import ast
from typing import Any


class PythonAssertError(ValueError):
    """Raised when a python_assert expression is invalid or unsafe."""


_SAFE_FUNCTIONS = {
    "abs": abs,
    "bool": bool,
    "float": float,
    "int": int,
    "len": len,
    "max": max,
    "min": min,
    "str": str,
    "sum": sum,
}

_ALLOWED_NAMES = {"output", "input", "value", *_SAFE_FUNCTIONS}

_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.BinOp,
    ast.Compare,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Subscript,
    ast.Slice,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.Call,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
)


class _SafeExpressionValidator(ast.NodeVisitor):
    def generic_visit(self, node: ast.AST) -> None:
        if not isinstance(node, _ALLOWED_NODES):
            raise PythonAssertError(f"unsupported syntax: {type(node).__name__}")
        super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id not in _ALLOWED_NAMES:
            raise PythonAssertError(f"name {node.id!r} is not allowed")

    def visit_Call(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_FUNCTIONS:
            raise PythonAssertError("only safe helper calls are allowed")
        if node.keywords:
            raise PythonAssertError("keyword arguments are not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # pragma: no cover - defensive
        raise PythonAssertError("attribute access is not allowed")


def evaluate_python_assert(
    expression: str | None,
    *,
    output: dict,
    message: dict,
    value: Any,
) -> bool:
    """Evaluate a restricted boolean expression for RuntimeGate.

    The expression is not a Python statement: no `assert`, assignment, import, or
    function definitions. It should return a truthy value.
    """
    if not expression:
        raise PythonAssertError("expression is required")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise PythonAssertError(f"syntax error: {e.msg}") from e
    _SafeExpressionValidator().visit(tree)
    code = compile(tree, "<python_assert>", "eval")
    env = {"output": output, "input": message, "value": value, **_SAFE_FUNCTIONS}
    try:
        return bool(eval(code, {"__builtins__": {}}, env))  # noqa: S307 - AST-validated
    except Exception as e:  # noqa: BLE001
        raise PythonAssertError(f"evaluation error: {type(e).__name__}: {e}") from e
