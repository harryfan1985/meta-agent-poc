"""Coordinator(§4.1)— M0 执行期。

按 DAG 拓扑序调度;gather_inputs 组装;run;RuntimeGate 机判;通过才 put。
M0 不实现恢复路由:gate 失败直接 SurfaceFailure(留给 M1 的 ErrorAttributor/RecoveryRouter)。
"""
from __future__ import annotations

from typing import Optional

from .context import TASK_INPUT, ContextStore
from .dag import topo_order
from .runtime_gate import RuntimeGate
from .schemas import ExecutableSwarm, SurfaceFailure


def execute(swarm: ExecutableSwarm, task_input: dict, store: Optional[ContextStore] = None) -> dict:
    store = store or ContextStore()
    store.put(TASK_INPUT, task_input)  # 原始输入登记为虚拟源(§4.2)

    policy = swarm.plan.verification_policy
    for spec_id in topo_order(swarm.spec_ids, swarm.dag):
        spec = swarm.spec(spec_id)
        inputs = store.gather_inputs(spec_id, swarm)  # 可能抛 ContractMismatch
        output = swarm.agent(spec_id)(inputs, store.history(spec_id))

        gate = RuntimeGate.check(output, spec, message=inputs, policy=policy)
        if gate.ok:
            store.put(spec_id, output)  # 仅验证通过才向下游传播
        else:
            # M0:无恢复,直接上浮(不返回未验证答案)
            raise SurfaceFailure(
                f"gate failed: {[f.evidence for f in gate.feedback]}",
                spec_id=spec_id,
                gate_result=gate,
            )
    return store.final_output(swarm)
