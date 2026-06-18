import pytest
from pydantic import ValidationError

from meta_agent.runtime_gate import RuntimeGate
from meta_agent.schemas import (
    AgentSpec,
    AssertionSpec,
    FailureType,
    FieldSpec,
    IOContract,
    VerificationCriteria,
    VerificationPolicy,
)


def _spec(machine_assertions=None, forbidden=None, out=None, required_out=None):
    out = out or {"final_code": FieldSpec(type="string", description="c"),
                  "passed": FieldSpec(type="boolean", description="p")}
    return AgentSpec(
        spec_id="t",
        io_contract=IOContract(output_schema=out, required_out=required_out or ["final_code", "passed"]),
        verification_criteria=VerificationCriteria(
            machine_assertions=machine_assertions or [],
            forbidden_patterns=forbidden or [],
        ),
    )


def test_gate01_schema_pass():
    g = RuntimeGate.check({"final_code": "def f(): pass", "passed": True}, _spec())
    assert g.ok is True


def test_gate02_schema_violation_missing_required():
    g = RuntimeGate.check({"final_code": "x"}, _spec())  # 缺 passed
    assert g.ok is False
    assert g.failure_type == FailureType.SPEC_ADHERENCE
    assert any(f.subtype == "schema_violation" for f in g.feedback)
    # 机判失败也产结构化反馈
    assert all(f.evidence for f in g.feedback)


def test_gate02b_schema_violation_wrong_type():
    g = RuntimeGate.check({"final_code": "x", "passed": "yes"}, _spec())  # passed 非 bool
    assert g.ok is False


def test_gate02c_extra_output_field_rejected_by_closed_contract():
    g = RuntimeGate.check(
        {"final_code": "x", "passed": True, "undeclared": "leak"},
        _spec(),
    )
    assert g.ok is False
    assert any("Additional properties" in f.evidence for f in g.feedback)


def test_iocontract_required_fields_must_be_declared():
    f = FieldSpec(type="string", description="x")
    with pytest.raises(ValidationError):
        IOContract(input_schema={"x": f}, required_in=["missing"])
    with pytest.raises(ValidationError):
        IOContract(output_schema={"x": f}, required_out=["missing"])


def test_gate03_forbidden_hit():
    g = RuntimeGate.check(
        {"final_code": "import os\n", "passed": True},
        _spec(forbidden=["import os"]),
    )
    assert g.ok is False
    assert any(f.subtype == "forbidden_hit" for f in g.feedback)


def test_gate04_field_present_missing():
    a = AssertionSpec(assertion_id="a1", kind="field_present", target_path="/passed")
    g = RuntimeGate.check({"final_code": "x"}, _spec(machine_assertions=[a]))
    assert g.ok is False  # 既 schema 缺 passed,也 a1 缺


def test_gate05_equals_input_mismatch_is_contract():
    a = AssertionSpec(
        assertion_id="sa2", kind="equals_input", target_path="/raw_signature",
        expected="$input.raw_signature", failure_type="contract", failure_subtype="field_mismatch",
    )
    spec = _spec(
        machine_assertions=[a],
        out={"raw_signature": FieldSpec(type="string", description="s")},
        required_out=["raw_signature"],
    )
    g = RuntimeGate.check(
        {"raw_signature": "WRONG"}, spec, message={"raw_signature": "def f()"}
    )
    assert g.ok is False
    assert g.failure_type == FailureType.CONTRACT
    assert any(f.subtype == "field_mismatch" for f in g.feedback)


def test_gate06_regex_match_pass():
    a = AssertionSpec(assertion_id="cs1", kind="regex_match", target_path="/final_code",
                      expression=r"def\s+has_close_elements")
    g = RuntimeGate.check(
        {"final_code": "def has_close_elements(): ...", "passed": True},
        _spec(machine_assertions=[a]),
    )
    assert g.ok is True


def test_gate07_priority_contract_over_spec_adherence():
    # 同时 forbidden(spec_adherence) + equals_input 失败(contract)→ 取 contract
    a = AssertionSpec(
        assertion_id="sa2", kind="equals_input", target_path="/final_code",
        expected="$input.x", failure_type="contract", failure_subtype="field_mismatch",
    )
    g = RuntimeGate.check(
        {"final_code": "import os", "passed": True},
        _spec(machine_assertions=[a], forbidden=["import os"]),
        message={"x": "other"},
    )
    assert g.ok is False
    assert g.failure_type == FailureType.CONTRACT


def test_gate09_model_check_rejected_in_m0():
    a = AssertionSpec(assertion_id="m1", kind="model_check", target_path="/final_code",
                      expression="是否正确?")
    g = RuntimeGate.check({"final_code": "x", "passed": True}, _spec(machine_assertions=[a]))
    assert g.ok is False
    assert g.failure_type == FailureType.CONTRACT  # 逼回机判


def test_gate09b_model_check_allowed_but_backend_missing_fails_closed():
    a = AssertionSpec(assertion_id="m1", kind="model_check", target_path="/final_code")
    g = RuntimeGate.check(
        {"final_code": "x", "passed": True}, _spec(machine_assertions=[a]),
        policy=VerificationPolicy(allow_model_verification=True),
    )
    assert g.ok is False
    assert g.failure_type == FailureType.CONTRACT
    assert any(f.subtype == "model_check_backend_missing" for f in g.feedback)


def test_gate_field_absent():
    a = AssertionSpec(assertion_id="fa", kind="field_absent", target_path="/passed")
    g = RuntimeGate.check({"final_code": "x", "passed": True}, _spec(machine_assertions=[a]))
    assert g.ok is False
    # 不存在则通过
    a2 = AssertionSpec(assertion_id="fa2", kind="field_absent", target_path="/secret")
    g2 = RuntimeGate.check({"final_code": "x", "passed": True}, _spec(machine_assertions=[a2]))
    assert g2.ok is True


def test_gate_contains_and_not_contains():
    c = AssertionSpec(assertion_id="c1", kind="contains", target_path="/final_code", expected="def ")
    assert RuntimeGate.check({"final_code": "def f()", "passed": True}, _spec(machine_assertions=[c])).ok
    nc = AssertionSpec(assertion_id="n1", kind="not_contains", target_path="/final_code", expected="TODO")
    bad = RuntimeGate.check({"final_code": "TODO later", "passed": True}, _spec(machine_assertions=[nc]))
    assert bad.ok is False


def test_gate_contains_with_nonstring_expected_does_not_raise():
    """模型给出非串 expected(list)而目标是字符串:须归为 spec_adherence,绝不抛 TypeError。"""
    c = AssertionSpec(assertion_id="c1", kind="contains", target_path="/answer", expected=["x"])
    spec = _spec(machine_assertions=[c],
                 out={"answer": FieldSpec(type="string", description="a")}, required_out=["answer"])
    g = RuntimeGate.check({"answer": "hello world"}, spec)
    assert g.ok is False
    assert g.failure_type == FailureType.SPEC_ADHERENCE


def test_gate_assertion_execution_error_is_attributed_not_raised():
    """任何断言执行异常(如非法正则)都转成 typed spec_adherence/assertion_error,gate 永不抛。"""
    a = AssertionSpec(assertion_id="boom", kind="regex_match", target_path="/answer", expression="(")
    spec = _spec(machine_assertions=[a],
                 out={"answer": FieldSpec(type="string", description="a")}, required_out=["answer"])
    g = RuntimeGate.check({"answer": "x"}, spec)
    assert g.ok is False
    assert g.failure_type == FailureType.SPEC_ADHERENCE
    assert any(f.subtype == "assertion_error" for f in g.feedback)


def test_gate_jsonschema_subschema():
    sub = {"type": "object", "required": ["k"], "properties": {"k": {"type": "string"}}}
    a = AssertionSpec(assertion_id="js", kind="jsonschema", target_path="/meta", expected=sub)
    spec = _spec(
        machine_assertions=[a],
        out={"meta": FieldSpec(type="object", description="m")}, required_out=["meta"],
    )
    ok = RuntimeGate.check({"meta": {"k": "v"}}, spec, message={})
    assert ok.ok is True
    bad = RuntimeGate.check({"meta": {"k": 1}}, spec, message={})
    assert bad.ok is False


def test_gate_python_assert_passes_restricted_expression():
    a = AssertionSpec(assertion_id="pa", kind="python_assert", target_path="/final_code",
                      expression='"def f" in value and value == output["final_code"]')
    g = RuntimeGate.check(
        {"final_code": "def f(): pass", "passed": True},
        _spec(machine_assertions=[a]),
    )
    assert g.ok is True


def test_gate_python_assert_false_is_typed_failure():
    a = AssertionSpec(
        assertion_id="pa",
        kind="python_assert",
        target_path="/final_code",
        expression='"def f" in value',
        failure_subtype="field_mismatch",
    )
    g = RuntimeGate.check({"final_code": "x", "passed": True}, _spec(machine_assertions=[a]))
    assert g.ok is False
    assert any(f.subtype == "field_mismatch" for f in g.feedback)


def test_gate_python_assert_rejects_unsafe_expression():
    a = AssertionSpec(
        assertion_id="pa",
        kind="python_assert",
        target_path="/final_code",
        expression="value.__class__",
    )
    g = RuntimeGate.check({"final_code": "x", "passed": True}, _spec(machine_assertions=[a]))
    assert g.ok is False
    assert any(f.subtype == "python_assert_error" for f in g.feedback)
