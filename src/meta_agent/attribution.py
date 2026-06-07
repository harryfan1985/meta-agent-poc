"""三级错误归因(§5 轴 A:locality「在哪修」)。

恢复成本随局部性递增:local < upstream < structural。
M1 为机判版:upstream 仅在"某依赖的已存输出自身 recheck 不过"时触发
(语义型上游错误需模型判,属 M3)。
"""
from __future__ import annotations

from .dag import affected_subgraph
from .runtime_gate import RuntimeGate
from .schemas import Budget, ExecutableSwarm, FailureType, GateResult, RecoveryAction


class ErrorAttributor:
    @staticmethod
    def classify(
        spec_id: str,
        gate: GateResult,
        store,
        swarm: ExecutableSwarm,
        local_retries: int,
        budget: Budget,
    ) -> RecoveryAction:
        spec = swarm.spec(spec_id)

        # 1) upstream:某依赖的已存输出违反其自身契约 → 重跑该上游
        for dep in spec.dependencies:
            if store.has(dep):
                recheck = RuntimeGate.check(
                    store.get(dep),
                    swarm.spec(dep),
                    message=store.inputs(dep),
                    policy=swarm.plan.verification_policy,
                )
                if not recheck.ok:
                    return RecoveryAction(kind="upstream", target=dep, feedback=recheck.feedback)

        # 2) structural:契约/分解本身有问题 → 升级重建子图
        if gate.failure_type == FailureType.CONTRACT:
            return RecoveryAction(
                kind="structural", subgraph=affected_subgraph(spec_id, swarm.dag), feedback=gate.feedback
            )

        # 3) local:输入对、本节点输出错 → 带反馈本地重试;超上限再升级 structural
        if local_retries < budget.max_local_retries:
            return RecoveryAction(kind="local", target=spec_id, feedback=gate.feedback)
        return RecoveryAction(
            kind="structural", subgraph=affected_subgraph(spec_id, swarm.dag), feedback=gate.feedback
        )
