"""§5 三级归因(轴 A)四分支单测。"""
from meta_agent.attribution import ErrorAttributor
from meta_agent.context import TASK_INPUT, ContextStore
from meta_agent.fixtures.function_completion import TASK_INPUT_EXAMPLE, build_swarm
from meta_agent.schemas import (
    Budget,
    FailureType,
    GateResult,
    StructuredFeedback,
)


def _failing_gate(ftype=FailureType.SPEC_ADHERENCE, subtype="schema_violation"):
    return GateResult(
        ok=False,
        failure_type=ftype,
        feedback=[StructuredFeedback(subtype=subtype, evidence="e", expected="x", actionable_fix="f")],
    )


def test_attr_local():
    swarm = build_swarm()
    store = ContextStore()
    # spec_analyzer 是入口(无依赖):输入对、输出错、未超重试 → local
    action = ErrorAttributor.classify(
        "spec_analyzer", _failing_gate(), store, swarm, local_retries=0, budget=Budget()
    )
    assert action.kind == "local"
    assert action.target == "spec_analyzer"
    assert action.feedback  # 带结构化反馈


def test_attr_structural_contract():
    swarm = build_swarm()
    store = ContextStore()
    action = ErrorAttributor.classify(
        "spec_analyzer", _failing_gate(FailureType.CONTRACT, "field_mismatch"),
        store, swarm, local_retries=0, budget=Budget(),
    )
    assert action.kind == "structural"
    assert "spec_analyzer" in action.subgraph
    # 受影响子图含下游
    assert "algo_planner" in action.subgraph


def test_attr_structural_when_local_exhausted():
    swarm = build_swarm()
    store = ContextStore()
    b = Budget(max_local_retries=2)
    action = ErrorAttributor.classify(
        "spec_analyzer", _failing_gate(), store, swarm, local_retries=2, budget=b
    )
    assert action.kind == "structural"  # 超本地重试上限 → 升级


def test_attr_upstream_when_dep_output_invalid():
    swarm = build_swarm()
    store = ContextStore()
    # 直接 seed 一个"自身 recheck 不过"的上游输出:缺 inequality_strict(sa1 失败)
    store.put("spec_analyzer", {
        "raw_signature": TASK_INPUT_EXAMPLE["raw_signature"],
        "parsed_spec": {"edge_cases": []},  # 缺 inequality_strict
    })
    store._inputs["spec_analyzer"] = {  # 供 sa2 equals_input recheck 用
        "raw_signature": TASK_INPUT_EXAMPLE["raw_signature"],
        "docstring": "d",
    }
    # algo_planner 依赖 spec_analyzer;它失败时归因应指向上游
    action = ErrorAttributor.classify(
        "algo_planner", _failing_gate(), store, swarm, local_retries=0, budget=Budget()
    )
    assert action.kind == "upstream"
    assert action.target == "spec_analyzer"
