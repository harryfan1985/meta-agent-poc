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


def test_e2e02_local_drop_inequality_surfaces_gate_failure():
    """注入 local 错误:analyst 丢 inequality_strict → sa1 gate 失败(M0 无恢复,上浮)。"""
    swarm = build_swarm(analyzer_handler="fx_spec_analyzer_drop_inequality")
    with pytest.raises(SurfaceFailure) as ei:
        execute(swarm, dict(TASK_INPUT_EXAMPLE))
    assert ei.value.spec_id == "spec_analyzer"
    assert any("sa1" in f.evidence for f in ei.value.gate_result.feedback)
