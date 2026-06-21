import pytest

from meta_agent.coordinator import execute
from meta_agent.fixtures.function_completion import TASK_INPUT_EXAMPLE, build_swarm
from meta_agent.schemas import ContractMismatch, SurfaceFailure


def test_e2e01_appendix_a_end_to_end_pass():
    """M0 验收:has_close_elements 4-agent swarm 端到端 PASS。"""
    swarm = build_swarm()
    out = execute(swarm, dict(TASK_INPUT_EXAMPLE))
    assert out["passed"] is True
    assert "def has_close_elements" in out["final_code"]


def test_e2e03_drop_passthrough_caught_at_source_gate():
    """注入 raw_signature 透传丢失。设计发现:raw_signature 在 analyzer 的 required_out 里,
    所以被 analyzer **自己的 gate** 先抓(schema + sa2),而非下游 ContractMismatch——
    即源头契约先生效。下游 gather_inputs 的 ContractMismatch 见 test_context 单测。"""
    swarm = build_swarm(analyzer_handler="fx_spec_analyzer_drop_passthrough")
    with pytest.raises(SurfaceFailure) as ei:
        execute(swarm, dict(TASK_INPUT_EXAMPLE))
    assert ei.value.spec_id == "spec_analyzer"
    subtypes = {f.subtype for f in ei.value.gate_result.feedback}
    assert "schema_violation" in subtypes or "field_mismatch" in subtypes


def test_e2e02_local_persistent_failure_surfaces_after_retries():
    """持续 local 错误(每次都丢 inequality_strict)→ 本地重试耗尽 → structural 上浮。"""
    swarm = build_swarm(analyzer_handler="fx_spec_analyzer_drop_inequality")
    with pytest.raises(SurfaceFailure) as ei:
        execute(swarm, dict(TASK_INPUT_EXAMPLE))
    assert ei.value.spec_id == "spec_analyzer"
    assert any("sa1" in f.evidence for f in ei.value.gate_result.feedback)


def test_e2e05_contract_misalignment_surfaces_structural():
    """契约错配(下游必填字段不在任何上游输出里)→ gather_inputs ContractMismatch
    → coordinator 上浮 structural。这是真正的 structural(分解缺陷),区别于
    'agent 漏吐它本应吐的字段'(那被源头自己 gate 抓)。"""
    from meta_agent.artifacts import ArtifactLoader
    from meta_agent.schemas import (
        AgentArtifact,
        AgentSpec,
        DagEdge,
        ExecutableSwarm,
        FieldSpec,
        IOContract,
        SwarmPlan,
    )

    f = FieldSpec(type="string", description="x")
    # a 声明 need_b 于 output_schema(过 preflight 数据流静态检查),但 handler 运行时不吐它
    # → 这是 preflight 抓不到、只能在执行期 gather_inputs 暴露的 structural 错配。
    a = AgentSpec(spec_id="a", io_contract=IOContract(
        output_schema={"out_a": f, "need_b": f}, required_out=["out_a"]))
    b = AgentSpec(spec_id="b", dependencies=["a"], io_contract=IOContract(
        input_schema={"need_b": f}, required_in=["need_b"],
        output_schema={"out_b": f}, required_out=["out_b"]))
    plan = SwarmPlan(swarm_name="misaligned", specs=[a, b],
                     dag_edges=[DagEdge(from_spec="a", to_spec="b")])
    fixtures = {"ha": lambda m, h: {"out_a": "x"}, "hb": lambda m, h: {"out_b": "y"}}
    swarm = ExecutableSwarm(plan=plan, artifacts={
        "a": AgentArtifact(spec_id="a", handler_ref="ha", passed=True),
        "b": AgentArtifact(spec_id="b", handler_ref="hb", passed=True),
    })
    ArtifactLoader(fixture_registry=fixtures).bind(swarm)
    with pytest.raises(SurfaceFailure) as ei:
        execute(swarm, {})
    assert ei.value.spec_id == "b"
    assert "contract mismatch" in str(ei.value).lower()


def test_e2e04_local_recovery_succeeds_on_retry():
    """注入可自愈的 local 错误:首跑丢 inequality_strict(sa1 失败→local 重试),
    二跑修正 → gate 通过 → 端到端 PASS。验证带反馈的本地重试闭环。"""
    from meta_agent.fixtures.function_completion import fx_spec_analyzer

    state = {"calls": 0}

    def self_correcting(message, history):
        state["calls"] += 1
        out = fx_spec_analyzer(message, history)
        if state["calls"] == 1:  # 首跑制造 sa1 失败
            del out["parsed_spec"]["inequality_strict"]
        return out  # 二跑起正常 → 通过

    swarm = build_swarm()
    swarm._loaded["spec_analyzer"] = self_correcting  # 覆盖为有状态 handler

    out = execute(swarm, dict(TASK_INPUT_EXAMPLE))
    assert out["passed"] is True
    assert state["calls"] == 2  # 恰好一次重试后成功


def test_e2e06_agent_exception_surfaces_typed_failure():
    swarm = build_swarm()

    def boom(message, history):
        raise RuntimeError("fixture exploded")

    swarm._loaded["spec_analyzer"] = boom
    with pytest.raises(SurfaceFailure) as ei:
        execute(swarm, dict(TASK_INPUT_EXAMPLE))
    assert ei.value.spec_id == "spec_analyzer"
    assert ei.value.gate_result.failure_type.value == "spec_adherence"
    assert any(f.subtype == "runtime_error" for f in ei.value.gate_result.feedback)
