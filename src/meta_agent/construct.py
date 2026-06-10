"""构造期编排(Phase 1,§3/§6)+ 类型化失败路由。

M2 骨架:`construct()` 是纯编排,5 个 Stage 作为可注入 callable(Stages),
默认实现接真实 LLM(后续),测试注入 stub 验证路由——保持确定性核可单测。

类型化路由(§6):构造期验证失败的 GateResult.failure_type 决定回退到哪个 Stage
- spec_adherence → 重新生成代码(Stage 4),带 StructuredFeedback
- grounding      → 重跑 grounding(Stage 3),再 Stage 4
- contract       → 重新规划架构(Stage 2),整体重来

预算:每 spec ≤ max_construct_passes 次 Stage4/5 pass;contract 触发重规划 ≤ max_replans。
触顶 surface failure,绝不产出未验证的 swarm。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .schemas import (
    AgentArtifact,
    AgentSpec,
    Budget,
    ExecutableSwarm,
    FailureType,
    GateResult,
    ParsedIntent,
    SurfaceFailure,
    SwarmPlan,
)
from .trace import NullTracer, make_event
from .validation import assert_valid_plan


@dataclass
class Stages:
    """五个构造期 Stage 的可注入实现。"""

    parse: Callable[[str], ParsedIntent]  # Stage 1
    plan: Callable[[ParsedIntent], SwarmPlan]  # Stage 2
    ground: Callable[[SwarmPlan], SwarmPlan]  # Stage 3
    codegen: Callable[[AgentSpec, SwarmPlan, list], AgentArtifact]  # Stage 4(spec, plan, feedback)
    verify: Callable[[AgentArtifact, AgentSpec], GateResult]  # Stage 5 → 带类型 GateResult


class _Replan(Exception):
    """contract 失败:架构有问题,冒泡到顶层触发 Stage 2 重规划。"""

    def __init__(self, gate: GateResult, spec_id: str):
        self.gate = gate
        self.spec_id = spec_id


def construct(
    task: str,
    stages: Stages,
    budget: Optional[Budget] = None,
    tracer=None,
    tool_registry=None,
) -> ExecutableSwarm:
    budget = budget or Budget()
    tracer = tracer or NullTracer()

    parsed = stages.parse(task)
    tracer.emit(make_event("start", phase="construct", stage="stage1_parse",
                           payload={"goal": parsed.goal}))

    replans = 0
    while True:
        plan = stages.plan(parsed)
        assert_valid_plan(plan, tool_registry=tool_registry)  # registry.validate + constitution
        plan = stages.ground(plan)
        assert_valid_plan(plan, tool_registry=tool_registry)
        tracer.emit(make_event("start", phase="construct", stage="stage2_plan",
                               payload={"swarm": plan.swarm_name, "n_specs": len(plan.specs)}))

        try:
            plan, artifacts = _build_artifacts(plan, stages, budget, tracer, tool_registry)
        except _Replan as r:
            replans += 1
            tracer.emit(make_event("recovery", phase="construct", stage="stage2_plan",
                                   spec_id=r.spec_id, recovery_kind="structural",
                                   payload={"reason": "contract", "replan": replans}))
            if replans > budget.max_replans:
                tracer.emit(make_event("finish", phase="construct",
                                       payload={"ok": False, "reason": "replans_exhausted"}))
                raise SurfaceFailure(
                    "construct: replans exhausted", spec_id=r.spec_id, gate_result=r.gate
                )
            continue  # 重新规划

        tracer.emit(make_event("finish", phase="construct", payload={"ok": True}))
        return ExecutableSwarm(plan=plan, artifacts=artifacts)


def _find_spec(plan: SwarmPlan, spec_id: str) -> AgentSpec:
    return next(s for s in plan.specs if s.spec_id == spec_id)


def _build_artifacts(plan: SwarmPlan, stages: Stages, budget: Budget, tracer, tool_registry=None) -> tuple[SwarmPlan, dict]:
    artifacts: dict[str, AgentArtifact] = {}
    spec_index = 0
    while spec_index < len(plan.specs):
        spec = plan.specs[spec_index]
        feedback: list = []
        gate: Optional[GateResult] = None
        for _pass in range(1, budget.max_construct_passes + 1):
            artifact = stages.codegen(spec, plan, feedback)
            gate = stages.verify(artifact, spec)
            tracer.emit(make_event("gate_result", phase="construct", stage="stage5_verify",
                                   spec_id=spec.spec_id, gate_result=gate,
                                   payload={"pass": _pass}))
            if gate.ok:
                artifact.passed = True
                artifacts[spec.spec_id] = artifact
                break

            # 类型化路由
            if gate.failure_type == FailureType.CONTRACT:
                raise _Replan(gate, spec.spec_id)  # → Stage 2 重规划
            if gate.failure_type == FailureType.GROUNDING:
                plan = stages.ground(plan)  # 重跑 grounding(可能返回新 plan,不只就地回填)
                assert_valid_plan(plan, tool_registry=tool_registry)
                spec = _find_spec(plan, spec.spec_id)
            # spec_adherence(及 grounding 后)→ 带反馈重生成代码
            feedback = gate.feedback
        else:
            raise SurfaceFailure(
                f"construct: passes exhausted for {spec.spec_id}",
                spec_id=spec.spec_id, gate_result=gate,
            )
        spec_index += 1
    return plan, artifacts
