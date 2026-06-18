"""M3 verifier 校准:混淆矩阵 / 指标 / 阈值门(确定性,stub judge)。"""
import json

from meta_agent.calibration import (
    CalibrationCase,
    calibrate,
    calibrated_backends,
)
from meta_agent.llm import StubStructuredLLM
from meta_agent.schemas import (
    AgentSpec,
    AssertionSpec,
    FieldSpec,
    IOContract,
    VerificationCriteria,
)
from meta_agent.verifiers import BaseJudgeBackend

MC = AssertionSpec(assertion_id="m1", kind="model_check", target_path="/answer",
                   description="answer must be a good summary")


def _spec(machine_assertions=None):
    return AgentSpec(
        spec_id="t", role="summarize",
        io_contract=IOContract(
            output_schema={"answer": FieldSpec(type="string", description="a")},
            required_out=["answer"]),
        verification_criteria=VerificationCriteria(
            machine_assertions=[MC] if machine_assertions is None else machine_assertions),
    )


def _case(cid, answer, expected_ok):
    return CalibrationCase(case_id=cid, spec=_spec(), output={"answer": answer}, expected_ok=expected_ok)


def _judge(respond):
    return BaseJudgeBackend(StubStructuredLLM(respond))


def _prefix_judge():
    """answer 以 'good' 开头 → pass,否则 fail。"""
    def respond(s, u, sch):
        ans = (u.get("output") or {}).get("answer", "")
        return {"verdict": "pass" if ans.startswith("good") else "fail", "reason": "x"}
    return _judge(respond)


# good summary(T,pass)→TP;bad(F,fail)→TN;good but wrong(F,pass)→FP;meh(T,fail)→FN
CASES = [
    _case("tp", "good summary", True),
    _case("tn", "bad", False),
    _case("fp", "good but wrong", False),
    _case("fn", "meh", True),
]


def test_calibrate_confusion_matrix():
    r = calibrate(_prefix_judge(), CASES)
    assert (r.tp, r.tn, r.fp, r.fn) == (1, 1, 1, 1)
    assert r.total == 4
    assert r.precision == 0.5 and r.recall == 0.5 and r.f1 == 0.5
    assert r.false_accept_rate == 0.5 and r.false_reject_rate == 0.5
    assert r.accuracy == 0.5
    assert r.backend == "base_judge"


def test_report_meets_thresholds():
    r = calibrate(_prefix_judge(), CASES)
    assert r.meets(min_f1=0.4) is True
    assert r.meets(min_f1=0.6) is False
    assert r.meets(max_false_accept_rate=0.6) is True
    assert r.meets(max_false_accept_rate=0.4) is False
    assert r.meets(min_f1=0.4, max_false_accept_rate=0.4) is False  # 任一不达即否


def test_perfect_judge_metrics():
    def respond(s, u, sch):
        ans = (u.get("output") or {}).get("answer", "")
        return {"verdict": "pass" if "ACCEPT" in ans else "fail", "reason": "x"}
    cases = [
        CalibrationCase("a", _spec(), {"answer": "ACCEPT"}, True),
        CalibrationCase("b", _spec(), {"answer": "reject me"}, False),
    ]
    r = calibrate(_judge(respond), cases)
    assert r.f1 == 1.0 and r.false_accept_rate == 0.0 and r.accuracy == 1.0


def test_calibrated_backends_filters_below_threshold():
    cases = [
        CalibrationCase("a", _spec(), {"answer": "ACCEPT good"}, True),
        CalibrationCase("b", _spec(), {"answer": "reject bad"}, False),
        CalibrationCase("c", _spec(), {"answer": "ACCEPT good2"}, True),
        CalibrationCase("d", _spec(), {"answer": "reject bad2"}, False),
    ]
    good = _judge(lambda s, u, sch: {
        "verdict": "pass" if "ACCEPT" in (u.get("output") or {}).get("answer", "") else "fail", "reason": "x"})
    always_accept = _judge(lambda s, u, sch: {"verdict": "pass", "reason": "x"})  # 全放行 → 高 false accept

    kept, reports = calibrated_backends([good, always_accept], cases,
                                        min_f1=0.9, max_false_accept_rate=0.1)
    assert good in kept and always_accept not in kept
    assert len(reports) == 2


def test_case_without_model_check_recorded_as_error():
    spec = _spec(machine_assertions=[])  # 无 model_check
    r = calibrate(_prefix_judge(), [CalibrationCase("nomc", spec, {"answer": "x"}, True)])
    assert r.total == 0
    assert any("nomc" in e for e in r.errors)


def test_summary_is_json_serializable():
    json.dumps(calibrate(_prefix_judge(), CASES).summary())
