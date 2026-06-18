"""M3 模型判 verifier 后端栈(§4.6 / §7.3)。

`model_check` 断言在 M0/M1/M2 一律 fail-closed;M3 由注册的 `VerifierBackend` 评判。
所有后端返回统一 `GateResult`(绝不裸 bool / 标量分);分数/票数只作证据写进 feedback,
不参与恢复路由。偏差缓解(§4.6):隐去生成器身份;要求结构化 JSON,解析失败=验证失败。

本模块只做"后端栈 + 注册表 + 单 judge / 多 aspect 面板",经 StructuredLLM 接缝调模型,
确定性测试用 StubStructuredLLM 注入。Claim/Evidence(§4.5)与校准(§4.7)在后续切片接入。
"""
from __future__ import annotations

from typing import Callable, Optional, Protocol, runtime_checkable

from .schemas import (
    AgentSpec,
    AssertionSpec,
    FailureType,
    GateResult,
    StructuredFeedback,
)

# verifier 必须输出结构化 verdict;解析失败 = 验证失败(不自由文本兜底放行)。
VERDICT_SCHEMA = {
    "type": "object",
    "required": ["verdict", "reason"],
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "reason": {"type": "string"},
    },
}

_JUDGE_SYS = (
    "You are a strict output verifier. Decide whether the agent OUTPUT satisfies the "
    "CRITERION for the given ASPECT. Judge only against the criterion and the provided "
    "input/output evidence; ignore who or what produced the output. If the output is not "
    "clearly supported, return fail. Return ONLY JSON: {\"verdict\": \"pass\"|\"fail\", "
    "\"reason\": <short string>}."
)


@runtime_checkable
class VerifierBackend(Protocol):
    name: str
    supports: set[str]  # 支持的 assertion kind 集合
    cost_tier: str  # free | cheap | expensive

    def verify(
        self,
        assertion: AssertionSpec,
        *,
        spec: AgentSpec,
        message: dict,
        output: dict,
        trace: list,
    ) -> GateResult: ...


def _fail(subtype: str, evidence: str, expected: str = "", fix: str = "",
          ftype: FailureType = FailureType.SPEC_ADHERENCE) -> GateResult:
    return GateResult(
        ok=False, failure_type=ftype,
        feedback=[StructuredFeedback(subtype=subtype, evidence=evidence,
                                     expected=expected, actionable_fix=fix)],
    )


def _criterion(assertion: AssertionSpec, spec: AgentSpec) -> str:
    """构造判定准则,隐去 agent 名 / 模型名 / pass index(缓解自我增强偏差)。"""
    return assertion.description or assertion.expression or f"output must satisfy role: {spec.role}"


class BaseJudgeBackend:
    """单 LLM judge(便宜档)。经 StructuredLLM 接缝产出结构化 verdict。"""

    name = "base_judge"
    supports = {"model_check"}
    cost_tier = "cheap"

    def __init__(self, structured_llm, *, aspect: str = "correctness"):
        self.llm = structured_llm
        self.aspect = aspect

    def verify(self, assertion, *, spec, message, output, trace) -> GateResult:
        user = {
            "aspect": self.aspect,
            "criterion": _criterion(assertion, spec),
            "output": output,
            "input": message,
            "required_output_fields": list(spec.io_contract.output_schema),
        }
        try:
            res = self.llm.generate(_JUDGE_SYS, user, VERDICT_SCHEMA)
        except Exception as e:  # noqa: BLE001 — 后端/解析失败一律 fail-closed,不放行
            return _fail("model_verifier_error",
                         f"[{assertion.assertion_id}/{self.aspect}] judge 失败: {type(e).__name__}: {e}",
                         "verifier 须返回结构化 verdict", assertion.description)
        verdict = (res or {}).get("verdict")
        if verdict == "pass":
            return GateResult(ok=True)
        reason = (res or {}).get("reason", "") if isinstance(res, dict) else ""
        return _fail(
            assertion.failure_subtype or "model_check_failed",
            f"[{assertion.assertion_id}/{self.aspect}] {reason}".strip(),
            "output 满足 model_check 准则", assertion.description,
        )


class AspectPanelBackend:
    """MAV 风格多 aspect 面板:逐 aspect judge,按通过比例投票(§4.6)。

    分歧(非一致)作证据写进失败 feedback,不参与路由。默认 pass_threshold=0.5(多数通过)。
    """

    name = "aspect_panel"
    supports = {"model_check"}
    cost_tier = "expensive"

    def __init__(self, structured_llm, *, aspects: Optional[list[str]] = None,
                 pass_threshold: float = 0.5):
        self.llm = structured_llm
        self.aspects = aspects or ["correctness", "completeness", "spec_adherence"]
        self.pass_threshold = pass_threshold

    def verify(self, assertion, *, spec, message, output, trace) -> GateResult:
        votes: list[tuple[str, GateResult]] = []
        for aspect in self.aspects:
            r = BaseJudgeBackend(self.llm, aspect=aspect).verify(
                assertion, spec=spec, message=message, output=output, trace=trace)
            votes.append((aspect, r))
        n = len(votes)
        n_pass = sum(1 for _, r in votes if r.ok)
        disagreement = 0 < n_pass < n
        if n and (n_pass / n) >= self.pass_threshold:
            return GateResult(ok=True)
        # 失败:聚合不通过 aspect 的 feedback;票数/分歧只作证据。
        failed = [(asp, r) for asp, r in votes if not r.ok]
        feedback = [fb for _, r in failed for fb in r.feedback]
        tally = f"panel {n_pass}/{n} pass" + (" (disagreement)" if disagreement else "")
        feedback.append(StructuredFeedback(
            subtype="aspect_panel_rejected",
            evidence=f"[{assertion.assertion_id}] {tally}; failed aspects: "
                     f"{[asp for asp, _ in failed]}",
            expected=f"至少 {self.pass_threshold:.0%} aspect 通过",
            actionable_fix=assertion.description,
        ))
        return GateResult(ok=False, failure_type=FailureType.SPEC_ADHERENCE, feedback=feedback)


class VerifierRegistry:
    """按 assertion kind 路由到**第一个**支持且预算允许的 backend(不自动降级)。"""

    def __init__(self, backends: Optional[list[VerifierBackend]] = None):
        self._backends: list[VerifierBackend] = list(backends or [])

    def register(self, backend: VerifierBackend) -> "VerifierRegistry":
        self._backends.append(backend)
        return self

    def for_kind(self, kind: str,
                 budget_allows: Callable[[VerifierBackend], bool] = lambda b: True) -> Optional[VerifierBackend]:
        for b in self._backends:
            if kind in b.supports and budget_allows(b):
                return b
        return None
