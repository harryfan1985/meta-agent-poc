"""§7.3 TraceEvent 可观测:事件序列、恢复/预算事件、JSONL 往返。"""
import io
import json

import pytest

from meta_agent.coordinator import execute
from meta_agent.fixtures.function_completion import TASK_INPUT_EXAMPLE, build_swarm
from meta_agent.schemas import Budget, SurfaceFailure, TraceEvent
from meta_agent.trace import JsonlTracer, ListTracer


def test_happy_path_event_sequence():
    tr = ListTracer()
    out = execute(build_swarm(), dict(TASK_INPUT_EXAMPLE), tracer=tr)
    assert out["passed"] is True
    # 4 节点各 start/llm_call/gate_result,末尾 finish(ok)
    assert tr.kinds().count("gate_result") == 4
    assert all(e.gate_result.ok for e in tr.events if e.event == "gate_result")
    last = tr.events[-1]
    assert last.event == "finish" and last.payload["ok"] is True
    # gate_result 带 latency
    assert all(e.latency_ms >= 0 for e in tr.events if e.event == "gate_result")


def test_local_retry_then_structural_emits_recovery_events():
    tr = ListTracer()
    with pytest.raises(SurfaceFailure):
        execute(build_swarm(analyzer_handler="fx_spec_analyzer_drop_inequality"),
                dict(TASK_INPUT_EXAMPLE), tracer=tr)
    recoveries = [e.recovery_kind for e in tr.events if e.event == "recovery"]
    assert recoveries.count("local") >= 1        # 本地重试
    assert recoveries[-1] == "structural"        # 耗尽后升级
    assert tr.events[-1].payload["reason"] == "structural"


def test_budget_exceeded_emits_budget_and_finish():
    tr = ListTracer()
    with pytest.raises(SurfaceFailure):
        execute(build_swarm(), dict(TASK_INPUT_EXAMPLE), budget=Budget(max_llm_calls=0), tracer=tr)
    kinds = tr.kinds()
    assert "budget" in kinds
    assert tr.events[-1].event == "finish"
    assert tr.events[-1].payload["reason"] == "budget_exceeded"
    assert "llm_call" not in kinds  # 预算在调用前拦下,agent 未跑


def test_contract_mismatch_emits_structural_recovery():
    from meta_agent.artifacts import ArtifactLoader
    from meta_agent.schemas import (
        AgentArtifact, AgentSpec, DagEdge, ExecutableSwarm, FieldSpec, IOContract, SwarmPlan,
    )

    f = FieldSpec(type="string", description="x")
    # a 声明 need_b 但 handler 不吐 → preflight 通过,执行期 gather_inputs 才暴露 structural。
    a = AgentSpec(spec_id="a", io_contract=IOContract(
        output_schema={"out_a": f, "need_b": f}, required_out=["out_a"]))
    b = AgentSpec(spec_id="b", dependencies=["a"], io_contract=IOContract(
        input_schema={"need_b": f}, required_in=["need_b"],
        output_schema={"out_b": f}, required_out=["out_b"]))
    plan = SwarmPlan(swarm_name="m", specs=[a, b], dag_edges=[DagEdge(from_spec="a", to_spec="b")])
    fixtures = {"ha": lambda m, h: {"out_a": "x"}, "hb": lambda m, h: {"out_b": "y"}}
    swarm = ExecutableSwarm(plan=plan, artifacts={
        "a": AgentArtifact(spec_id="a", handler_ref="ha", passed=True),
        "b": AgentArtifact(spec_id="b", handler_ref="hb", passed=True)})
    ArtifactLoader(fixture_registry=fixtures).bind(swarm)

    tr = ListTracer()
    with pytest.raises(SurfaceFailure):
        execute(swarm, {}, tracer=tr)
    rec = [e for e in tr.events if e.event == "recovery" and e.recovery_kind == "structural"]
    assert rec and rec[0].payload["reason"] == "contract_mismatch"


def test_jsonl_tracer_roundtrip():
    buf = io.StringIO()
    execute(build_swarm(), dict(TASK_INPUT_EXAMPLE), tracer=JsonlTracer(buf))
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert lines
    # 每行可解析回 TraceEvent
    events = [TraceEvent.model_validate_json(ln) for ln in lines]
    assert events[-1].event == "finish"
    assert any(e.event == "gate_result" and e.gate_result is not None for e in events)


def test_agent_surface_emits_finish_and_reraises():
    # agent(如 external_agent adapter)自身 SurfaceFailure → 重抛前发 finish
    swarm = build_swarm()

    def surfacing(message, history):
        raise SurfaceFailure("adapter blew up", spec_id="spec_analyzer")

    swarm._loaded["spec_analyzer"] = surfacing
    tr = ListTracer()
    with pytest.raises(SurfaceFailure):
        execute(swarm, dict(TASK_INPUT_EXAMPLE), tracer=tr)
    assert tr.events[-1].event == "finish"
    assert tr.events[-1].payload["reason"] == "agent surface"


def test_default_no_tracer_unchanged():
    # 不传 tracer 仍正常返回(NullTracer 零开销)
    out = execute(build_swarm(), dict(TASK_INPUT_EXAMPLE))
    assert out["passed"] is True
