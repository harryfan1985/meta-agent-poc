"""§7.2 预算:触顶 BudgetExceeded;execute 转 SurfaceFailure。"""
import pytest

from meta_agent.budget import BudgetExceeded, BudgetMeter
from meta_agent.coordinator import execute
from meta_agent.fixtures.function_completion import TASK_INPUT_EXAMPLE, build_swarm
from meta_agent.schemas import Budget, SurfaceFailure


def test_bud01_llm_calls_exceeded_raises():
    meter = BudgetMeter()
    with pytest.raises(BudgetExceeded) as ei:
        meter.reserve_run(Budget(max_llm_calls=0))
    assert ei.value.kind == "llm_calls"


def test_bud01_wall_exceeded_raises():
    # 注入一个"已经过去很久"的时钟
    t = {"v": 0.0}
    meter = BudgetMeter(clock=lambda: t["v"])
    t["v"] = 10_000.0
    with pytest.raises(BudgetExceeded) as ei:
        meter.check_wall(Budget(max_wall_seconds=1))
    assert ei.value.kind == "wall"


def test_bud02_execute_surfaces_on_budget():
    swarm = build_swarm()  # 4 节点 → 至少 4 次 record_run
    calls = {"n": 0}

    def counted_agent(message, history):
        calls["n"] += 1
        return {"raw_signature": "should not run", "parsed_spec": {}}

    swarm._loaded["spec_analyzer"] = counted_agent
    with pytest.raises(SurfaceFailure) as ei:
        execute(swarm, dict(TASK_INPUT_EXAMPLE), budget=Budget(max_llm_calls=0))
    assert "budget" in str(ei.value).lower()
    assert calls["n"] == 0
