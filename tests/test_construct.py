"""M2 构造期编排 + 类型化路由(§6)。stub 驱动,确定性,不调 LLM。"""
from collections import defaultdict

import pytest

from meta_agent.construct import Stages, construct
from meta_agent.fixtures.function_completion import build_plan
from meta_agent.schemas import (
    AgentArtifact,
    Budget,
    FailureType,
    GateResult,
    ParsedIntent,
    StructuredFeedback,
    SurfaceFailure,
)
from meta_agent.trace import ListTracer

FB = [StructuredFeedback(subtype="schema_violation", evidence="e", expected="x", actionable_fix="f")]


def _codegen_counter():
    calls = defaultdict(int)

    def codegen(spec, plan, feedback):
        calls[spec.spec_id] += 1
        return AgentArtifact(spec_id=spec.spec_id, implementation_kind="fixture", handler_ref="h")

    return codegen, calls


def _stages(verify, codegen=None, plan=None):
    cg = codegen or _codegen_counter()[0]
    return Stages(
        parse=lambda t: ParsedIntent(goal=t),
        plan=plan or (lambda pi: build_plan()),
        ground=lambda p: p,
        codegen=cg,
        verify=verify,
    )


def test_happy_path_produces_swarm():
    swarm = construct("write has_close_elements", _stages(verify=lambda a, s: GateResult(ok=True)))
    assert set(swarm.artifacts) == {"spec_analyzer", "algo_planner", "code_synthesizer", "code_verifier"}
    assert all(a.passed for a in swarm.artifacts.values())


def test_spec_adherence_retries_codegen():
    codegen, cg_calls = _codegen_counter()

    def verify(a, s):
        # spec_analyzer 第一次 spec_adherence 失败,之后通过
        if s.spec_id == "spec_analyzer" and cg_calls["spec_analyzer"] == 1:
            return GateResult(ok=False, failure_type=FailureType.SPEC_ADHERENCE, feedback=FB)
        return GateResult(ok=True)

    construct("t", _stages(verify=verify, codegen=codegen))
    assert cg_calls["spec_analyzer"] == 2  # 带反馈重生成一次


def test_contract_triggers_replan():
    plan_calls = [0]

    def plan(pi):
        plan_calls[0] += 1
        return build_plan()

    state = {"first": True}

    def verify(a, s):
        if state["first"]:
            state["first"] = False
            return GateResult(ok=False, failure_type=FailureType.CONTRACT, feedback=FB)
        return GateResult(ok=True)

    construct("t", _stages(verify=verify, plan=plan))
    assert plan_calls[0] == 2  # contract → Stage 2 重规划一次


def test_preflight_failure_triggers_replan():
    plan_calls = [0]

    def plan(pi):
        plan_calls[0] += 1
        p = build_plan()
        if plan_calls[0] == 1:
            p.specs[0].dependencies = ["nonexistent"]  # 预检 contract 失败 → 重规划
        return p

    swarm = construct("t", _stages(verify=lambda a, s: GateResult(ok=True), plan=plan))
    assert plan_calls[0] == 2  # 预检失败 → Stage 2 重规划一次后成功
    assert set(swarm.artifacts) == {"spec_analyzer", "algo_planner", "code_synthesizer", "code_verifier"}


def test_preflight_replans_exhausted_surfaces():
    def plan(pi):
        p = build_plan()
        p.specs[0].dependencies = ["nonexistent"]  # 始终非法 → 触顶
        return p

    with pytest.raises(SurfaceFailure) as ei:
        construct("t", _stages(verify=lambda a, s: GateResult(ok=True), plan=plan),
                  budget=Budget(max_replans=2))
    assert "preflight" in str(ei.value).lower()


def test_grounding_failure_reruns_grounding_then_retries():
    ground_calls = [0]

    def ground(p):
        ground_calls[0] += 1
        if ground_calls[0] == 2:
            grounded = p.model_copy(deep=True)
            grounded.specs[0].role = "grounded analyzer"
            return grounded
        return p

    cg_calls = defaultdict(int)
    roles_seen = []

    def codegen(spec, plan, feedback):
        cg_calls[spec.spec_id] += 1
        if spec.spec_id == "spec_analyzer":
            roles_seen.append(spec.role)
        return AgentArtifact(spec_id=spec.spec_id, implementation_kind="fixture", handler_ref="h")

    state = {"first": True}

    def verify(a, s):
        if s.spec_id == "spec_analyzer" and state["first"]:
            state["first"] = False
            return GateResult(ok=False, failure_type=FailureType.GROUNDING, feedback=FB)
        return GateResult(ok=True)

    stages = Stages(
        parse=lambda t: ParsedIntent(goal=t),
        plan=lambda pi: build_plan(),
        ground=ground,
        codegen=codegen,
        verify=verify,
    )
    swarm = construct("t", stages)
    # 一次初始 ground + 一次因 grounding 失败重跑;spec_analyzer codegen 重试一次
    assert ground_calls[0] == 2
    assert cg_calls["spec_analyzer"] == 2
    assert roles_seen[-1] == "grounded analyzer"
    assert swarm.spec("spec_analyzer").role == "grounded analyzer"


def test_passes_exhausted_surfaces():
    verify = lambda a, s: GateResult(ok=False, failure_type=FailureType.SPEC_ADHERENCE, feedback=FB)
    with pytest.raises(SurfaceFailure) as ei:
        construct("t", _stages(verify=verify), budget=Budget(max_construct_passes=2))
    assert "passes exhausted" in str(ei.value)


def test_replans_exhausted_surfaces():
    verify = lambda a, s: GateResult(ok=False, failure_type=FailureType.CONTRACT, feedback=FB)
    with pytest.raises(SurfaceFailure) as ei:
        construct("t", _stages(verify=verify), budget=Budget(max_replans=2))
    assert "replans exhausted" in str(ei.value)


def test_construct_emits_trace_events():
    tr = ListTracer()
    construct("t", _stages(verify=lambda a, s: GateResult(ok=True)), tracer=tr)
    phases = {e.phase for e in tr.events}
    assert phases == {"construct"}
    assert tr.kinds().count("gate_result") == 4  # 每 spec 一次构造期验证
    assert tr.events[-1].event == "finish" and tr.events[-1].payload["ok"] is True
