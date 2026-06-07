"""RuntimeGate(§4.3)— M0 机判骨架。

只做机判:schema(out_jsonschema)→ forbidden_patterns → machine_assertions。
model_check 在 M0/M1 默认拒绝(判 contract);python_assert 属 M1 沙箱后端。
所有失败都产出带类型 + StructuredFeedback 的 GateResult,不退化成 bool。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from jsonschema import Draft202012Validator

from .schemas import (
    AgentSpec,
    AssertionSpec,
    FAILURE_PRIORITY,
    FailureSubtype,
    FailureType,
    GateResult,
    StructuredFeedback,
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
            fb = RuntimeGate._check_assertion(a, output, message or {}, policy)
            if fb is not None:
                ft = (
                    FailureType.CONTRACT
                    if a.kind == "model_check"
                    else FailureType(a.failure_type)
                )
                results.append((fb, ft))

        if not results:
            return GateResult(ok=True)
        types = {ft for _, ft in results}
        ftype = next((t for t in FAILURE_PRIORITY if t in types), FailureType.SPEC_ADHERENCE)
        return GateResult(ok=False, failure_type=ftype, feedback=[fb for fb, _ in results])

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
            if not found or a.expected not in (val or ""):
                return fail(f"{a.target_path} 未包含 {a.expected!r}", f"包含 {a.expected!r}")
        elif k == "not_contains":
            if found and a.expected in (val or ""):
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
        elif k == "python_assert":
            # python_assert 属 M1 沙箱后端;M0 不执行
            return fail(
                "python_assert 属 M1 沙箱后端,M0 不执行;请降级为 regex/contains",
                "M0 等价机判检查",
                subtype="python_assert_unsupported",
            )
        return None
