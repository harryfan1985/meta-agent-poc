"""Coordinator(§4.1)+ 恢复闭环(§5)。

M1:按 DAG 拓扑序执行;gate 失败 → ErrorAttributor 归因 → 按 locality 恢复:
- local:带 StructuredFeedback 原地重试(≤ budget.max_local_retries)
- upstream:清除责任上游、回退重跑、再前进
- structural:M1 直接 SurfaceFailure(重规划属 M2)
预算触顶或不可恢复 → SurfaceFailure,绝不返回未验证答案。
RecoveryRouter 的应用逻辑随循环状态内联于此(§5)。
"""
from __future__ import annotations

from collections import defaultdict
from time import monotonic
from typing import Optional

from .attribution import ErrorAttributor
from .budget import BudgetExceeded, BudgetMeter
from .context import TASK_INPUT, ContextStore
from .dag import topo_order
from .runtime_gate import RuntimeGate
from .schemas import (
    Budget,
    ContractMismatch,
    ExecutableSwarm,
    FailureSubtype,
    FailureType,
    GateResult,
    StructuredFeedback,
    SurfaceFailure,
)
from .trace import NullTracer, make_event
from .validation import assert_valid_plan


def execute(
    swarm: ExecutableSwarm,
    task_input: dict,
    budget: Optional[Budget] = None,
    store: Optional[ContextStore] = None,
    tool_registry=None,
    tracer=None,
    verifier=None,
) -> dict:
    budget = budget or Budget()
    meter = BudgetMeter()
    store = store or ContextStore()
    tracer = tracer or NullTracer()  # 默认零开销;行为不变
    store.put(TASK_INPUT, task_input)

    assert_valid_plan(swarm.plan, tool_registry=tool_registry)
    order = topo_order(swarm.spec_ids, swarm.dag)
    policy = swarm.plan.verification_policy
    local_retries: dict[str, int] = defaultdict(int)
    upstream_reruns: dict[str, int] = defaultdict(int)
    history: dict[str, list] = defaultdict(list)

    i = 0
    try:
        while i < len(order):
            spec_id = order[i]
            spec = swarm.spec(spec_id)
            meter.check_wall(budget)
            t0 = monotonic()
            tracer.emit(make_event("start", stage="node", spec_id=spec_id,
                                   payload={"attempt": local_retries[spec_id] + 1}))

            try:
                inputs = store.gather_inputs(spec_id, swarm)
            except ContractMismatch as e:
                # 缺字段/歧义 = 分解缺陷 → structural;M1 上浮(replan 属 M2)
                tracer.emit(make_event("recovery", stage="node", spec_id=spec_id,
                                       recovery_kind="structural",
                                       payload={"reason": "contract_mismatch", "detail": str(e)}))
                tracer.emit(make_event("finish", spec_id=spec_id,
                                       payload={"ok": False, "reason": "contract_mismatch"}))
                raise SurfaceFailure(f"structural (contract mismatch): {e}", spec_id=spec_id)

            meter.reserve_run(budget)
            tracer.emit(make_event("llm_call", stage="node", spec_id=spec_id))
            try:
                output = swarm.agent(spec_id)(inputs, history[spec_id])
            except SurfaceFailure:
                tracer.emit(make_event("finish", spec_id=spec_id,
                                       payload={"ok": False, "reason": "agent surface"}))
                raise
            except Exception as e:  # noqa: BLE001
                tracer.emit(make_event("finish", spec_id=spec_id,
                                       payload={"ok": False, "reason": "agent runtime error"}))
                raise SurfaceFailure(
                    f"agent runtime error: {e}",
                    spec_id=spec_id,
                    gate_result=GateResult(
                        ok=False,
                        failure_type=FailureType.SPEC_ADHERENCE,
                        feedback=[
                            StructuredFeedback(
                                subtype=FailureSubtype.RUNTIME_ERROR.value,
                                evidence=f"{type(e).__name__}: {e}",
                                expected="agent callable returns dict matching output_schema without raising",
                                actionable_fix="修复 agent 实现、fixture、adapter 或运行环境",
                            )
                        ],
                    ),
                ) from e
            meter.record_run(budget)

            gate = RuntimeGate.check(output, spec, message=inputs, policy=policy, verifier=verifier)
            tracer.emit(make_event("gate_result", stage="runtime_gate", spec_id=spec_id,
                                   gate_result=gate, latency_ms=int((monotonic() - t0) * 1000)))
            if gate.ok:
                store.put(spec_id, output)  # 仅验证通过才向下游传播
                i += 1
                continue

            action = ErrorAttributor.classify(
                spec_id, gate, store, swarm, local_retries[spec_id], budget
            )
            tracer.emit(make_event("recovery", stage="node", spec_id=spec_id,
                                   recovery_kind=action.kind,
                                   payload={"target": action.target, "subgraph": action.subgraph}))

            if action.kind == "local":
                local_retries[spec_id] += 1
                history[spec_id] = history[spec_id] + [fb.model_dump() for fb in action.feedback]
                continue  # 原地重试(i 不变)

            # 【M1 发现】upstream 应用段在"全量 gate + 纯机判归因"下不可达:任何
            # 坏到 recheck 不过的上游输出,早在它自己 put 时的 gate 就被拦下,不会留到
            # 下游来归因。upstream 要真正生效需模型判识别"过 gate 但语义错"(M3)。
            # classify 的 upstream 逻辑已单测;此处应用段保留待 M3。
            if action.kind == "upstream":  # pragma: no cover
                target = action.target
                if upstream_reruns[target] >= budget.max_upstream_reruns:
                    raise SurfaceFailure(
                        f"upstream reruns exhausted for {target}", spec_id=spec_id, gate_result=gate
                    )
                upstream_reruns[target] += 1
                history[target] = history[target] + [fb.model_dump() for fb in action.feedback]
                store.invalidate(target)
                i = order.index(target)  # 回退到责任上游重跑
                continue

            # structural:M1 不重规划,直接上浮
            tracer.emit(make_event("finish", spec_id=spec_id,
                                   payload={"ok": False, "reason": "structural"}))
            raise SurfaceFailure(f"structural at {spec_id}", spec_id=spec_id, gate_result=gate)

    except BudgetExceeded as e:
        tracer.emit(make_event("budget", payload={"detail": str(e)}))
        tracer.emit(make_event("finish", payload={"ok": False, "reason": "budget_exceeded"}))
        raise SurfaceFailure(f"budget exceeded: {e}") from e

    tracer.emit(make_event("finish", payload={"ok": True}))
    return store.final_output(swarm)
