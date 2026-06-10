"""§4.7 GoldenVerificationCase 最小集:确认机判 gate 行为符合预期(M3 校准基线)。"""
from meta_agent.fixtures.function_completion import build_golden_cases, build_swarm
from meta_agent.runtime_gate import RuntimeGate


def test_golden_cases_match_gate_behavior():
    swarm = build_swarm()
    cases = build_golden_cases()
    assert len(cases) >= 4
    for c in cases:
        spec = swarm.spec(c.spec_id)
        gate = RuntimeGate.check(c.output, spec, message=c.message,
                                 policy=swarm.plan.verification_policy)
        assert gate.ok is c.expected_ok, f"{c.case_id}: ok mismatch"
        if not c.expected_ok:
            assert gate.failure_type.value == c.expected_failure_type, f"{c.case_id}: ftype"
            got = {f.subtype for f in gate.feedback}
            assert set(c.expected_subtypes).issubset(got), f"{c.case_id}: subtypes {got}"


def test_golden_coverage_attached():
    """每次 gate 都带 VerificationCoverage(§4.8)。"""
    swarm = build_swarm()
    spec = swarm.spec("code_verifier")
    gate = RuntimeGate.check(
        {"final_code": "def has_close_elements(): ...", "passed": True},
        spec, message={}, policy=swarm.plan.verification_policy,
    )
    cov = gate.coverage
    assert cov is not None
    assert cov.schema_fields_total == 2  # final_code, passed
    assert cov.schema_fields_checked == cov.schema_fields_total
    assert cov.assertions_total == 1  # cv1
    assert cov.assertions_checked == 1


def test_function_completion_cv1_uses_python_assert():
    """附录 A 的 cv1 已从 M0/M1 regex 降级版切回 M2 python_assert。"""
    spec = build_swarm().spec("code_verifier")
    [assertion] = spec.verification_criteria.machine_assertions
    assert assertion.assertion_id == "cv1"
    assert assertion.kind == "python_assert"
