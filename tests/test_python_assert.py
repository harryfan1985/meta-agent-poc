import pytest

from meta_agent.python_assert import PythonAssertError, evaluate_python_assert


def _eval(expr):
    return evaluate_python_assert(
        expr,
        output={"final_code": "def f(): pass", "n": 3},
        message={"raw_signature": "def f()"},
        value="def f(): pass",
    )


def test_python_assert_evaluates_safe_expression():
    assert _eval('"def f" in value and len(value) > output["n"]') is True
    assert _eval('input["raw_signature"] == "def f()"') is True


@pytest.mark.parametrize(
    "expr,expected",
    [
        (None, "expression is required"),
        ("value = 1", "syntax error"),
        ("unknown_name", "not allowed"),
        ("lambda x: x", "unsupported syntax"),
        ("len(obj=value)", "keyword arguments"),
        ("value.strip()", "only safe helper calls"),
        ("value.__class__", "attribute access"),
        ('output["missing"] == 1', "evaluation error"),
    ],
)
def test_python_assert_rejects_unsafe_or_invalid_expression(expr, expected):
    with pytest.raises(PythonAssertError) as ei:
        _eval(expr)
    assert expected in str(ei.value)
