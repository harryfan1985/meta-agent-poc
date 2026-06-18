"""§7.1 python_assert 资源边界加固。"""
import ast

import pytest

from meta_agent.python_assert import PythonAssertError, evaluate_python_assert
from meta_agent.sandbox import (
    MAX_AST_DEPTH,
    MAX_AST_NODES,
    MAX_EXPR_CHARS,
    SandboxLimitError,
    enforce_expression_limits,
)


def _enforce(expr):
    enforce_expression_limits(expr, ast.parse(expr, mode="eval"))


def _eval(expr):
    return evaluate_python_assert(expr, output={"n": 3}, message={}, value="abc")


def test_enforce_passes_normal_expression():
    _enforce('"a" in value and len(value) > output["n"]')  # 不抛


def test_enforce_rejects_too_long():
    with pytest.raises(SandboxLimitError) as ei:
        _enforce('value == "' + "x" * (MAX_EXPR_CHARS + 1) + '"')
    assert "too long" in str(ei.value)


def test_enforce_rejects_too_many_nodes():
    with pytest.raises(SandboxLimitError) as ei:
        _enforce("+".join(["1"] * (MAX_AST_NODES + 5)))
    assert "too complex" in str(ei.value)


def test_enforce_rejects_too_deep():
    expr = "not " * (MAX_AST_DEPTH + 3) + "value"  # 深度随 not 链增长,节点数仍很少
    with pytest.raises(SandboxLimitError) as ei:
        _enforce(expr)
    assert "deeply nested" in str(ei.value)


@pytest.mark.parametrize("expr", ['"x" * 1000000', 'value * 99', '2 ** 64', '[0] * 100000000'])
def test_enforce_rejects_repetition_and_power(expr):
    with pytest.raises(SandboxLimitError) as ei:
        _enforce(expr)
    assert "disabled to bound memory" in str(ei.value)


def test_evaluate_rejects_memory_blowup_as_python_assert_error():
    with pytest.raises(PythonAssertError) as ei:
        _eval('"x" * 100000000 == ""')
    assert "sandbox limit" in str(ei.value)


def test_evaluate_still_accepts_legitimate_assertions():
    assert _eval('len(value) == 3') is True
    assert _eval('"a" in value') is True
