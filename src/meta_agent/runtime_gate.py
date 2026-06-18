"""RuntimeGate(§4.3)— machine-verifiable runtime checks.

只做机判:schema(out_jsonschema)→ forbidden_patterns → machine_assertions。
model_check 在 M0/M1/M2 默认拒绝(判 contract);python_assert 走受限只读表达式后端。
所有失败都产出带类型 + StructuredFeedback 的 GateResult,不退化成 bool。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from jsonschema import Draft202012Validator

from .python_assert import PythonAssertError, evaluate_python_assert
from .schemas import (
    AgentSpec,
    AssertionSpec,
    FAILURE_PRIORITY,
    FailureSubtype,
    FailureType,
    GateResult,
    StructuredFeedback,
    VerificationCoverage,
    VerificationPolicy,
)

_MISSING = object()


def _resolve_pointer(obj: Any, pointer: str):
    """极简 JSON Pointer:返回 (found, value)。"""
    if pointer in ("", "/"):
        return True, obj
    cur = obj
    for part in [p for p in pointer.split("/") if p != ""]:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, _MISSING
    return True, cur


def _all_strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _all_strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _all_strings(v)]
    return []


def _safe_contains(haystack: Any, needle: Any) -> bool:
    """稳健 membership:string→子串(needle 强转字符串);list/dict/set→成员;
    类型不匹配不抛异常,返回 False。避免模型给出非串 expected/val 时 gate 崩溃。"""
    try:
        if isinstance(haystack, str):
            return str(needle) in haystack
        if isinstance(haystack, (list, tuple, set, dict)):
            return needle in haystack
    except TypeError:
        return False
    return False


class RuntimeGate:
    @staticmethod
    def check(
        output: dict,
        spec: AgentSpec,
        message: Optional[dict] = None,
        policy: Optional[VerificationPolicy] = None,
    ) -> GateResult:
        results: list[tuple[StructuredFeedback, FailureType]] = []
        vc = spec.verification_criteria

        # 1) schema(机判)
        schema = spec.io_contract.out_jsonschema()
        for err in sorted(Draft202012Validator(schema).iter_errors(output), key=lambda e: list(e.path)):
            loc = "/" + "/".join(str(p) for p in err.path) if err.path else "(root)"
            results.append((
                StructuredFeedback(
                    subtype=FailureSubtype.SCHEMA_VIOLATION.value,
                    evidence=f"schema {loc}: {err.message}",
                    expected=f"满足 output_schema 字段 {loc}",
                    actionable_fix=f"修正 {loc}",
                ),
                FailureType.SPEC_ADHERENCE,
            ))

        # 2) forbidden_patterns(机判)
        haystack = "\n".join(_all_strings(output))
        for pat in vc.forbidden_patterns:
            if pat in haystack:
                results.append((
                    StructuredFeedback(
                        subtype=FailureSubtype.FORBIDDEN_HIT.value,
                        evidence=f"命中 forbidden pattern {pat!r}",
                        expected=f"输出不得包含 {pat!r}",
                        actionable_fix=f"移除 {pat!r}",
                    ),
                    FailureType.SPEC_ADHERENCE,
                ))

        # 3) machine_assertions(机判优先)
        for a in vc.machine_assertions:
            try:
                fb = RuntimeGate._check_assertion(a, output, message or {}, policy)
                ft = (
                    FailureType.CONTRACT
                    if a.kind == "model_check"
                    else FailureType(a.failure_type)
                )
            except Exception as e:  # noqa: BLE001 — gate 永不因坏断言抛出,统一归为 spec_adherence
                fb = StructuredFeedback(
                    subtype=FailureSubtype.ASSERTION_ERROR.value,
                    evidence=f"[{a.assertion_id}] 断言执行异常: {type(e).__name__}: {e}",
                    expected="断言须可机判执行(避免类型/字段假设错误)",
                    actionable_fix=a.description,
                )
                ft = FailureType.SPEC_ADHERENCE
            if fb is not None:
                results.append((fb, ft))

        coverage = VerificationCoverage(
            schema_fields_total=len(spec.io_contract.output_schema),
            schema_fields_checked=len(spec.io_contract.output_schema),
            assertions_total=len(vc.machine_assertions),
            assertions_checked=len(vc.machine_assertions),
            forbidden_total=len(vc.forbidden_patterns),
            forbidden_checked=len(vc.forbidden_patterns),
        )

        if not results:
            return GateResult(ok=True, coverage=coverage)
        types = {ft for _, ft in results}
        ftype = next((t for t in FAILURE_PRIORITY if t in types), FailureType.SPEC_ADHERENCE)
        return GateResult(
            ok=False, failure_type=ftype, feedback=[fb for fb, _ in results], coverage=coverage
        )

    @staticmethod
    def _check_assertion(
        a: AssertionSpec, output: dict, message: dict, policy: Optional[VerificationPolicy]
    ) -> Optional[StructuredFeedback]:
        found, val = (_resolve_pointer(output, a.target_path) if a.target_path else (True, output))

        def fail(evidence: str, expected: str = "", subtype: Optional[str] = None) -> StructuredFeedback:
            return StructuredFeedback(
                subtype=subtype or a.failure_subtype,
                evidence=f"[{a.assertion_id}] {evidence}",
                expected=expected,
                actionable_fix=a.description,
            )

        k = a.kind
        if k == "field_present":
            if not found:
                return fail(f"字段缺失 {a.target_path}", "该字段必须存在")
        elif k == "field_absent":
            if found:
                return fail(f"字段不应存在 {a.target_path}", "该字段必须不存在")
        elif k == "equals_input":
            ref = a.expected
            if isinstance(ref, str) and ref.startswith("$input."):
                want = message.get(ref[len("$input."):], _MISSING)
            else:
                want = ref
            if not found or val != want:
                return fail(
                    f"{a.target_path}={val!r} 应等于 {want!r}",
                    str(want),
                    subtype=FailureSubtype.FIELD_MISMATCH.value,
                )
        elif k == "contains":
            if not found or not _safe_contains(val, a.expected):
                return fail(f"{a.target_path} 未包含 {a.expected!r}", f"包含 {a.expected!r}")
        elif k == "not_contains":
            if found and _safe_contains(val, a.expected):
                return fail(f"{a.target_path} 含禁止内容 {a.expected!r}", f"不含 {a.expected!r}")
        elif k == "regex_match":
            if not found or not re.search(a.expression or "", val if isinstance(val, str) else ""):
                return fail(f"{a.target_path} 不匹配 /{a.expression}/", f"匹配 /{a.expression}/")
        elif k == "jsonschema":
            try:
                Draft202012Validator(a.expected or {}).validate(val)
            except Exception as e:  # noqa: BLE001
                return fail(f"{a.target_path} 不满足子 schema: {e}", str(a.expected))
        elif k == "model_check":
            allow = bool(policy and policy.allow_model_verification)
            if not allow:
                # M0/M1:逼回机判(由 check() 映射成 contract)
                return fail(
                    "model_check 在 M0/M1 不允许;请改写为可机判断言",
                    "可机判断言(schema/field/regex/...)",
                    subtype="model_check_not_allowed",
                )
            return fail(
                "model_check 已被策略允许,但 verifier backend 尚未注册;不能静默通过",
                "注册 BaseJudge/AspectPanel/AgentVerifier backend 后再启用",
                subtype="model_check_backend_missing",
            )
        elif k == "python_assert":
            try:
                ok = evaluate_python_assert(a.expression, output=output, message=message, value=val)
            except PythonAssertError as e:
                return fail(
                    f"python_assert 无法执行:{e}",
                    "只读布尔表达式,仅可访问 output/input/value",
                    subtype="python_assert_error",
                )
            if not ok:
                return fail(
                    f"python_assert 返回 false: {a.expression}",
                    "表达式应返回 true",
                    subtype=a.failure_subtype or "python_assert_failed",
                )
        return None
