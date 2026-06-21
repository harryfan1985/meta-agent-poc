import pytest

from meta_agent.coordinator import execute
from meta_agent.fixtures.function_completion import TASK_INPUT_EXAMPLE, build_swarm
from meta_agent.schemas import AssertionSpec, ConstitutionRule, DagEdge, SurfaceFailure, ToolDefinition
from meta_agent.tools import ToolRegistry
from meta_agent.validation import validate_plan


def test_plan_dependencies_must_match_dag_predecessors():
    swarm = build_swarm()
    spec = swarm.spec("code_synthesizer")
    spec.dependencies = ["spec_analyzer"]  # dag 还声明 algo_planner -> code_synthesizer
    issues = validate_plan(swarm.plan)
    assert any("code_synthesizer" in issue and "dag predecessors" in issue for issue in issues)


def test_execute_rejects_plan_dependency_dag_drift_before_running():
    swarm = build_swarm()
    spec = swarm.spec("code_synthesizer")
    spec.dependencies = ["spec_analyzer"]
    calls = {"n": 0}

    def counted_agent(message, history):
        calls["n"] += 1
        return {"raw_signature": "x", "parsed_spec": {"inequality_strict": True}}

    swarm._loaded["spec_analyzer"] = counted_agent
    with pytest.raises(SurfaceFailure) as ei:
        execute(swarm, dict(TASK_INPUT_EXAMPLE))
    assert ei.value.gate_result.failure_type.value == "contract"
    assert calls["n"] == 0


def test_promote_consumed_outputs_makes_consumed_field_required():
    from meta_agent.validation import promote_consumed_outputs

    swarm = build_swarm()
    # 让 spec_analyzer 把 raw_signature 设为可选(声明但不必产出)
    sa = swarm.spec("spec_analyzer")
    sa.io_contract.required_out = ["parsed_spec"]  # 去掉 raw_signature
    assert "raw_signature" not in sa.io_contract.required_out

    new = promote_consumed_outputs(swarm.plan)
    new_sa = next(s for s in new.specs if s.spec_id == "spec_analyzer")
    # 下游(code_synthesizer/code_verifier)必填消费 raw_signature → 被提升回 required_out
    assert "raw_signature" in new_sa.io_contract.required_out
    # 原 plan 不被改(纯函数)
    assert "raw_signature" not in swarm.spec("spec_analyzer").io_contract.required_out


def test_promote_consumed_outputs_noop_when_already_satisfied():
    from meta_agent.validation import promote_consumed_outputs

    swarm = build_swarm()  # 良构:消费字段都已是生产者 required_out
    assert promote_consumed_outputs(swarm.plan) is swarm.plan  # 免深拷贝,返回原对象


def test_plan_rejects_input_not_provided_by_dependencies():
    swarm = build_swarm()
    spec = swarm.spec("code_synthesizer")
    spec.io_contract.required_in = spec.io_contract.required_in + ["goal"]  # 无上游产出 goal
    issues = validate_plan(swarm.plan)
    assert any("code_synthesizer" in i and "required inputs not provided" in i for i in issues)


def test_plan_rejects_cyclic_dag():
    swarm = build_swarm()
    swarm.plan.dag_edges.append(DagEdge(from_spec="code_verifier", to_spec="spec_analyzer"))
    issues = validate_plan(swarm.plan)
    assert any("cycle" in issue for issue in issues)


def test_plan_rejects_required_tool_not_declared_in_spec_tools():
    swarm = build_swarm()
    spec = swarm.spec("spec_analyzer")
    spec.verification_criteria.required_tools = ["web_search"]
    issues = validate_plan(swarm.plan)
    assert any("required_tools not declared" in issue for issue in issues)


def test_plan_rejects_unregistered_tools_when_registry_provided():
    swarm = build_swarm()
    spec = swarm.spec("spec_analyzer")
    spec.tools = ["web_search", "ghost"]
    spec.verification_criteria.required_tools = ["web_search"]
    registry = ToolRegistry([
        ToolDefinition(name="web_search", handler_ref="h_web", requires_network=True)
    ])
    issues = validate_plan(swarm.plan, tool_registry=registry)
    assert any("unregistered tools" in issue and "ghost" in issue for issue in issues)


def test_execute_rejects_unregistered_tool_before_running():
    swarm = build_swarm()
    swarm.spec("spec_analyzer").tools = ["ghost"]
    calls = {"n": 0}

    def counted_agent(message, history):
        calls["n"] += 1
        return {"raw_signature": "x", "parsed_spec": {"inequality_strict": True}}

    swarm._loaded["spec_analyzer"] = counted_agent
    with pytest.raises(SurfaceFailure) as ei:
        execute(swarm, dict(TASK_INPUT_EXAMPLE), tool_registry=ToolRegistry())
    assert ei.value.gate_result.failure_type.value == "contract"
    assert calls["n"] == 0


def test_constitution_model_check_rule_rejected_before_m3():
    swarm = build_swarm()
    swarm.plan.constitution_rules = [
        ConstitutionRule(
            rule_id="c1",
            description="must not leak secrets",
            machine_assertion=AssertionSpec(assertion_id="m", kind="model_check"),
        )
    ]
    issues = validate_plan(swarm.plan)
    assert any("machine-checkable before M3" in issue for issue in issues)


def test_constitution_blocking_rule_must_be_attached_to_matching_specs():
    swarm = build_swarm()
    swarm.plan.constitution_rules = [
        ConstitutionRule(rule_id="c1", description="SECRET", severity="block")
    ]
    issues = validate_plan(swarm.plan)
    assert any("missing blocking rule c1" in issue for issue in issues)
    for spec in swarm.plan.specs:
        spec.verification_criteria.forbidden_patterns.append("SECRET")
    assert validate_plan(swarm.plan) == []


def test_high_risk_requires_review_or_conservative_mode():
    swarm = build_swarm()
    swarm.spec("spec_analyzer").risk_tier = "high"
    swarm.plan.verification_policy.require_human_review = False
    swarm.plan.verification_policy.conservative_mode = False
    issues = validate_plan(swarm.plan)
    assert any("high/critical risk requires" in issue for issue in issues)
