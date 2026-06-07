"""Construction-time validators shared by runtime and tests.

这些检查把"坏计划"挡在 execute() 前面,避免执行期才以 KeyError/
隐式数据流分叉等形式暴露。
"""
from __future__ import annotations

from collections import Counter

from .schemas import (
    FailureSubtype,
    FailureType,
    GateResult,
    StructuredFeedback,
    SurfaceFailure,
    SwarmPlan,
)


def validate_plan(plan: SwarmPlan) -> list[str]:
    """校验 DAG 边与 spec.dependencies 是否一致。"""
    issues: list[str] = []
    spec_ids = [s.spec_id for s in plan.specs]
    counts = Counter(spec_ids)
    duplicates = sorted([spec_id for spec_id, count in counts.items() if count > 1])
    if duplicates:
        issues.append(f"duplicate spec_id: {duplicates}")

    known = set(spec_ids)
    predecessors: dict[str, set[str]] = {spec_id: set() for spec_id in spec_ids}
    for edge in plan.dag_edges:
        if edge.from_spec not in known or edge.to_spec not in known:
            issues.append(f"edge references unknown node: {edge.from_spec} -> {edge.to_spec}")
            continue
        predecessors[edge.to_spec].add(edge.from_spec)

    for spec in plan.specs:
        declared = set(spec.dependencies)
        unknown_deps = sorted(declared - known)
        if unknown_deps:
            issues.append(f"{spec.spec_id}: dependencies reference unknown nodes: {unknown_deps}")
        expected = predecessors.get(spec.spec_id, set())
        if declared != expected:
            issues.append(
                f"{spec.spec_id}: dependencies {sorted(declared)} != dag predecessors {sorted(expected)}"
            )
    return issues


def surface_contract_issues(reason: str, issues: list[str]) -> SurfaceFailure:
    feedback = [
        StructuredFeedback(
            subtype=FailureSubtype.DECOMP_FLAW.value,
            evidence=issue,
            expected="SwarmPlan/AgentArtifact 在执行前通过 construction-time preflight",
            actionable_fix="修正计划、DAG、dependencies 或 artifact 绑定",
        )
        for issue in issues
    ]
    return SurfaceFailure(
        reason,
        gate_result=GateResult(
            ok=False,
            failure_type=FailureType.CONTRACT,
            feedback=feedback,
        ),
    )


def assert_valid_plan(plan: SwarmPlan) -> None:
    issues = validate_plan(plan)
    if issues:
        raise surface_contract_issues("plan preflight failed", issues)
