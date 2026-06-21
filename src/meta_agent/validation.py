"""Construction-time validators shared by runtime and tests.

这些检查把"坏计划"挡在 execute() 前面,避免执行期才以 KeyError/
隐式数据流分叉等形式暴露。
"""
from __future__ import annotations

from collections import Counter
from typing import Protocol

from .dag import DagCycleError, topo_order
from .schemas import (
    FailureSubtype,
    FailureType,
    GateResult,
    StructuredFeedback,
    SurfaceFailure,
    SwarmPlan,
)


class ToolRegistryLike(Protocol):
    def validate(self, tool_names) -> list[str]:
        ...


_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _rule_applies(rule, spec) -> bool:
    if rule.scope == "tool":
        return bool(set(rule.applies_to_tools) & set(spec.tools))
    return rule.scope in {"global", "domain", "swarm", "agent"}


def validate_plan(plan: SwarmPlan, tool_registry: ToolRegistryLike | None = None) -> list[str]:
    """校验 DAG、工具注册、policy 与 constitution 的 construction-time 约束。"""
    issues: list[str] = []
    spec_ids = [s.spec_id for s in plan.specs]
    counts = Counter(spec_ids)
    duplicates = sorted([spec_id for spec_id, count in counts.items() if count > 1])
    if duplicates:
        issues.append(f"duplicate spec_id: {duplicates}")

    known = set(spec_ids)
    predecessors: dict[str, set[str]] = {spec_id: set() for spec_id in spec_ids}
    edges_ref_ok = True
    for edge in plan.dag_edges:
        if edge.from_spec not in known or edge.to_spec not in known:
            issues.append(f"edge references unknown node: {edge.from_spec} -> {edge.to_spec}")
            edges_ref_ok = False
            continue
        predecessors[edge.to_spec].add(edge.from_spec)

    # 环检测:仅在边引用合法时做,避免与 unknown-node 报告重复;环属结构性缺陷。
    if edges_ref_ok:
        try:
            topo_order(spec_ids, plan.dag_edges)
        except DagCycleError as e:
            issues.append(f"dag has a cycle: {e}")

    out_fields = {s.spec_id: set(s.io_contract.output_schema) for s in plan.specs}
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

        # 数据流可追溯性:非入口节点的每个必填输入必须由某个依赖的 output_schema 提供
        # (镜像执行期 gather_inputs:中游只能读直接依赖的产出)。入口节点输入来自 task_input,
        # 无法静态校验,跳过。闭合此 preflight 缺口,坏数据流在构造期即触发重规划而非执行期才崩。
        if declared and not unknown_deps:
            provided = set().union(*(out_fields.get(d, set()) for d in declared))
            missing_in = sorted(set(spec.io_contract.required_in) - provided)
            if missing_in:
                issues.append(
                    f"{spec.spec_id}: required inputs not provided by dependencies: {missing_in}"
                )

        missing_required_tools = sorted(set(spec.verification_criteria.required_tools) - set(spec.tools))
        if missing_required_tools:
            issues.append(
                f"{spec.spec_id}: required_tools not declared in spec.tools: {missing_required_tools}"
            )

        if spec.risk_tier in {"high", "critical"} and not (
            plan.verification_policy.require_human_review
            or plan.verification_policy.conservative_mode
        ):
            issues.append(
                f"{spec.spec_id}: high/critical risk requires human review or conservative mode"
            )

    if plan.verification_policy.min_verifier_votes < 1:
        issues.append("verification_policy.min_verifier_votes must be >= 1")

    if tool_registry is not None:
        for spec in plan.specs:
            missing = tool_registry.validate(spec.tools)
            if missing:
                issues.append(f"{spec.spec_id}: unregistered tools: {sorted(missing)}")
            required_missing = tool_registry.validate(spec.verification_criteria.required_tools)
            if required_missing:
                issues.append(
                    f"{spec.spec_id}: unregistered required_tools: {sorted(required_missing)}"
                )

    for rule in plan.constitution_rules:
        if rule.scope == "tool" and not rule.applies_to_tools:
            issues.append(f"{rule.rule_id}: tool-scoped rule must declare applies_to_tools")
        if rule.machine_assertion and rule.machine_assertion.kind == "model_check":
            issues.append(f"{rule.rule_id}: constitution rule must be machine-checkable before M3")
        if rule.severity != "block":
            continue
        for spec in plan.specs:
            if not _rule_applies(rule, spec):
                continue
            if rule.description not in spec.verification_criteria.forbidden_patterns:
                issues.append(f"{spec.spec_id}: missing blocking rule {rule.rule_id}")
    return issues


def promote_consumed_outputs(plan: SwarmPlan) -> SwarmPlan:
    """计划规范化:把"被下游必填消费、且生产者已声明"的字段提升为生产者的 required_out。

    这样生产者**自己的 gate** 就会强制它产出该字段(缺则 spec_adherence,可带反馈本地重试),
    而不是等下游 gather_inputs 才以结构性 ContractMismatch 暴露——把"声明却没产出"从
    不可恢复的 structural 降级为生产者处可恢复的 local 失败,显著降执行期 flakiness。
    """
    out_decl = {s.spec_id: set(s.io_contract.output_schema) for s in plan.specs}
    promote: dict[str, set[str]] = {}
    for spec in plan.specs:
        for dep in spec.dependencies:
            for field in spec.io_contract.required_in:
                if field in out_decl.get(dep, set()):
                    promote.setdefault(dep, set()).add(field)
    if not any(promote.get(s.spec_id, set()) - set(s.io_contract.required_out) for s in plan.specs):
        return plan  # 已满足,免深拷贝
    new = plan.model_copy(deep=True)
    for spec in new.specs:
        add = promote.get(spec.spec_id)
        if add:
            ro = list(spec.io_contract.required_out)
            ro += [f for f in sorted(add) if f not in ro]
            spec.io_contract.required_out = ro
    return new


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


def assert_valid_plan(plan: SwarmPlan, tool_registry: ToolRegistryLike | None = None) -> None:
    issues = validate_plan(plan, tool_registry=tool_registry)
    if issues:
        raise surface_contract_issues("plan preflight failed", issues)
