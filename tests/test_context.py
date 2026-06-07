import pytest

from meta_agent.context import TASK_INPUT, ContextStore
from meta_agent.fixtures.function_completion import TASK_INPUT_EXAMPLE, build_swarm
from meta_agent.schemas import ContractMismatch


def _store_with_task():
    s = ContextStore()
    s.put(TASK_INPUT, dict(TASK_INPUT_EXAMPLE))
    return s


def test_store_invalidate_and_inputs_and_history():
    swarm = build_swarm()
    store = _store_with_task()
    store.gather_inputs("spec_analyzer", swarm)  # 记录 inputs
    assert store.inputs("spec_analyzer")["raw_signature"] == TASK_INPUT_EXAMPLE["raw_signature"]
    store.put("spec_analyzer", {"raw_signature": "s", "parsed_spec": {}})
    assert store.has("spec_analyzer")
    store.invalidate("spec_analyzer")  # upstream 重跑前清除
    assert not store.has("spec_analyzer")
    assert store.inputs("spec_analyzer") == {}
    assert store.history("spec_analyzer") == []


def test_gi06_entry_node_from_task_input():
    swarm = build_swarm()
    store = _store_with_task()
    msg = store.gather_inputs("spec_analyzer", swarm)
    assert msg["raw_signature"] == TASK_INPUT_EXAMPLE["raw_signature"]
    assert msg["docstring"] == TASK_INPUT_EXAMPLE["docstring"]


def test_gi01_02_multi_upstream_merge():
    swarm = build_swarm()
    store = _store_with_task()
    # 模拟上游已产出
    store.put("spec_analyzer", {"raw_signature": "sig", "parsed_spec": {"x": 1}})
    store.put("algo_planner", {"approach": {"algorithm": "pairwise"}})
    msg = store.gather_inputs("code_synthesizer", swarm)
    assert msg == {
        "raw_signature": "sig",
        "parsed_spec": {"x": 1},
        "approach": {"algorithm": "pairwise"},
    }
    # 可追溯:来源记录正确
    src = store.sources("code_synthesizer")
    assert src["approach"] == "algo_planner"
    assert src["raw_signature"] == "spec_analyzer"


def test_gi03_missing_required_field_raises():
    swarm = build_swarm()
    store = _store_with_task()
    # analyst 丢了 raw_signature 透传 → synthesizer 必填字段无来源
    store.put("spec_analyzer", {"parsed_spec": {"x": 1}})
    store.put("algo_planner", {"approach": {"algorithm": "pairwise"}})
    with pytest.raises(ContractMismatch) as ei:
        store.gather_inputs("code_synthesizer", swarm)
    assert "raw_signature" in ei.value.unresolved


def test_gi07_optional_field_resolved_from_dep():
    swarm = build_swarm()
    store = _store_with_task()
    # code_verifier 依赖 spec_analyzer + code_synthesizer。raw_signature 虽非其必填,
    # 但来自直接依赖 spec_analyzer 且可解析 → 应包含(可选字段有来源即取)。
    store.put("spec_analyzer", {"raw_signature": "sig", "parsed_spec": {"x": 1}})
    store.put("code_synthesizer", {"candidate_code": "def has_close_elements(): ..."})
    msg = store.gather_inputs("code_verifier", swarm)
    assert set(msg) == {"candidate_code", "parsed_spec", "raw_signature"}
    assert store.sources("code_verifier")["raw_signature"] == "spec_analyzer"


def test_gi08_field_only_in_nondependency_not_pulled():
    """作用域限直接依赖:字段只在非依赖的上游里 → 不可见 → 必填则 ContractMismatch。"""
    from meta_agent.schemas import (
        AgentSpec,
        DagEdge,
        ExecutableSwarm,
        FieldSpec,
        IOContract,
        SwarmPlan,
    )

    f = FieldSpec(type="string", description="x")
    u1 = AgentSpec(spec_id="u1", io_contract=IOContract(output_schema={"a": f}, required_out=["a"]))
    u2 = AgentSpec(spec_id="u2", io_contract=IOContract(output_schema={"b": f}, required_out=["b"]))
    d = AgentSpec(
        spec_id="d",
        dependencies=["u1"],  # 只依赖 u1
        io_contract=IOContract(input_schema={"b": f}, required_in=["b"]),  # 但要 u2 的 b
    )
    plan = SwarmPlan(
        swarm_name="t", specs=[u1, u2, d],
        dag_edges=[DagEdge(from_spec="u1", to_spec="d")],
    )
    swarm = ExecutableSwarm(plan=plan)
    store = ContextStore()
    store.put("u1", {"a": "x"})
    store.put("u2", {"b": "y"})  # b 存在,但 u2 非 d 的依赖
    with pytest.raises(ContractMismatch) as ei:
        store.gather_inputs("d", swarm)
    assert "b" in ei.value.unresolved


def test_gi05_conflict_raises():
    # 构造一个人工歧义:两个依赖都产出同名必填字段
    from meta_agent.schemas import (
        AgentSpec,
        DagEdge,
        ExecutableSwarm,
        FieldSpec,
        IOContract,
        SwarmPlan,
    )

    f = FieldSpec(type="string", description="x")
    up1 = AgentSpec(spec_id="u1", io_contract=IOContract(output_schema={"v": f}, required_out=["v"]))
    up2 = AgentSpec(spec_id="u2", io_contract=IOContract(output_schema={"v": f}, required_out=["v"]))
    down = AgentSpec(
        spec_id="d",
        dependencies=["u1", "u2"],
        io_contract=IOContract(input_schema={"v": f}, required_in=["v"]),
    )
    plan = SwarmPlan(
        swarm_name="t",
        specs=[up1, up2, down],
        dag_edges=[DagEdge(from_spec="u1", to_spec="d"), DagEdge(from_spec="u2", to_spec="d")],
    )
    swarm = ExecutableSwarm(plan=plan)
    store = ContextStore()
    store.put("u1", {"v": "a"})
    store.put("u2", {"v": "b"})
    with pytest.raises(ContractMismatch) as ei:
        store.gather_inputs("d", swarm)
    assert "v" in ei.value.conflicts
