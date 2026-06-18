"""M2 eval harness:确定性核(stub/fixture stages,不接 LLM)。"""
import json

from meta_agent.artifacts import ArtifactLoader
from meta_agent.construct import Stages
from meta_agent.construction_verifier import ConstructionVerifier
from meta_agent.eval_harness import evaluate_case, run_eval
from meta_agent.fixtures.function_completion import FIXTURES, TASK_INPUT_EXAMPLE, build_plan
from meta_agent.schemas import (
    AgentArtifact,
    Budget,
    FailureType,
    GateResult,
    ParsedIntent,
    StructuredFeedback,
)

HANDLER_MAP = {
    "spec_analyzer": "fx_spec_analyzer", "algo_planner": "fx_algo_planner",
    "code_synthesizer": "fx_code_synthesizer", "code_verifier": "fx_code_verifier",
}
FB = [StructuredFeedback(subtype="schema_violation", evidence="e", expected="x", actionable_fix="f")]


def _artifact(spec_id, handler):
    return AgentArtifact(spec_id=spec_id, implementation_kind="fixture", handler_ref=handler, passed=True)


def _build_ok(task_input):
    """真实 Stage 5 行为 dry-run + fixture codegen → construct/execute 全程 PASS。"""
    loader = ArtifactLoader(fixture_registry=FIXTURES)

    def codegen(spec, plan, feedback):
        return _artifact(spec.spec_id, HANDLER_MAP[spec.spec_id])

    stages = Stages(
        parse=lambda t: ParsedIntent(goal=t),
        plan=lambda pi: build_plan(),
        ground=lambda p: p,
        codegen=codegen,
        verify=ConstructionVerifier(loader, task_input=task_input).verify,
    )
    return stages, loader


def _build_construct_fail(failure_type):
    def build(task_input):
        loader = ArtifactLoader(fixture_registry=FIXTURES)
        stages = Stages(
            parse=lambda t: ParsedIntent(goal=t),
            plan=lambda pi: build_plan(),
            ground=lambda p: p,
            codegen=lambda spec, plan, fb: _artifact(spec.spec_id, "h"),
            verify=lambda a, s: GateResult(ok=False, failure_type=failure_type, feedback=FB),
        )
        return stages, loader

    return build


def _build_execute_fail(task_input):
    """构造期一律放行,但 code_verifier 执行期产出不含目标函数 → execute 期 gate 失败。"""
    registry = {**FIXTURES, "fx_bad_verifier": lambda m, h: {"final_code": "no function", "passed": True}}
    loader = ArtifactLoader(fixture_registry=registry)

    def codegen(spec, plan, feedback):
        handler = "fx_bad_verifier" if spec.spec_id == "code_verifier" else HANDLER_MAP[spec.spec_id]
        return _artifact(spec.spec_id, handler)

    stages = Stages(
        parse=lambda t: ParsedIntent(goal=t),
        plan=lambda pi: build_plan(),
        ground=lambda p: p,
        codegen=codegen,
        verify=lambda a, s: GateResult(ok=True),  # 跳过构造期 dry-run,只验执行期归因
    )
    return stages, loader


def test_run_eval_all_pass():
    report = run_eval([("t", dict(TASK_INPUT_EXAMPLE))], _build_ok, runs=3)
    assert report.total == 3
    assert report.passed == 3
    assert report.pass_rate == 1.0
    assert report.failure_by_type() == {}


def test_attributes_construct_spec_adherence_failure():
    report = run_eval([("t", {})], _build_construct_fail(FailureType.SPEC_ADHERENCE),
                      runs=2, budget=Budget(max_construct_passes=2))
    assert report.passed == 0
    assert report.failure_by_phase() == {"construct": 2}
    assert report.failure_by_type() == {"spec_adherence": 2}


def test_attributes_construct_contract_failure():
    report = run_eval([("t", {})], _build_construct_fail(FailureType.CONTRACT),
                      runs=1, budget=Budget(max_replans=1))
    assert report.failure_by_phase() == {"construct": 1}
    assert report.failure_by_type() == {"contract": 1}


def test_attributes_execute_phase_failure():
    outcome = evaluate_case("t", dict(TASK_INPUT_EXAMPLE), _build_execute_fail,
                            budget=Budget(max_local_retries=1, max_upstream_reruns=1))
    assert outcome.ok is False
    assert outcome.phase == "execute"


def test_summary_is_json_serializable():
    report = run_eval([("a", dict(TASK_INPUT_EXAMPLE)), ("b", dict(TASK_INPUT_EXAMPLE))],
                      _build_ok, runs=1)
    s = report.summary()
    assert s["total"] == 2 and s["passed"] == 2 and s["pass_rate"] == 1.0
    json.dumps(s)  # 必须可序列化(便于落盘/上报)
