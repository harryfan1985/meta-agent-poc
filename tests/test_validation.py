import pytest

from meta_agent.coordinator import execute
from meta_agent.fixtures.function_completion import TASK_INPUT_EXAMPLE, build_swarm
from meta_agent.schemas import SurfaceFailure
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
