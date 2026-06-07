"""工具注册表(§3.6)+ 工具门(§4.4)。M1 最小机判版。

ToolRegistry:抽象工具名 → 后端真实定义的单一出口。
PreToolGate / PostToolGate:工具调用前后机判(名/参数 schema/side_effects/网络/输出大小/taint)。
所有结果统一 ToolGateResult(内含 GateResult),进 trace;失败不让 agent 看到未净化输出。
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

from jsonschema import Draft202012Validator

from .schemas import (
    FailureSubtype,
    FailureType,
    GateResult,
    StructuredFeedback,
    ToolDefinition,
    ToolGateResult,
    VerificationPolicy,
)


class ToolRegistry:
    def __init__(self, tools: Optional[Iterable[ToolDefinition]] = None):
        self._tools: dict[str, ToolDefinition] = {t.name: t for t in (tools or [])}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> ToolDefinition:
        return self._tools[name]

    def schema_for(self, names: list[str]) -> list[dict]:
        return [self._tools[n].backend_schema for n in names]

    def validate(self, tool_names: Iterable[str]) -> list[str]:
        """规划后校验:返回未注册的工具名(非空即 contract 失败)。"""
        return [n for n in tool_names if n not in self._tools]


def _fb(subtype: str, evidence: str, expected: str = "", fix: str = "") -> StructuredFeedback:
    return StructuredFeedback(subtype=subtype, evidence=evidence, expected=expected, actionable_fix=fix)


class PreToolGate:
    @staticmethod
    def check(
        tool_name: str,
        params: dict,
        registry: ToolRegistry,
        policy: Optional[VerificationPolicy] = None,
        allowed_side_effects: Optional[set[str]] = None,
        allow_network: bool = False,
    ) -> ToolGateResult:
        allowed_side_effects = allowed_side_effects if allowed_side_effects is not None else {"none", "read"}
        fb: list[StructuredFeedback] = []
        ftype = FailureType.SPEC_ADHERENCE

        if not registry.has(tool_name):
            return ToolGateResult(
                ok=False, tool_name=tool_name, stage="pre",
                gate_result=GateResult(
                    ok=False, failure_type=FailureType.CONTRACT,
                    feedback=[_fb(FailureSubtype.TOOL_MISUSE.value,
                                  f"未注册工具 {tool_name!r}", "工具必须在 ToolRegistry 注册")],
                ),
            )

        tool = registry.get(tool_name)

        # 参数 schema(机判)
        if tool.backend_schema:
            errs = sorted(Draft202012Validator(tool.backend_schema).iter_errors(params), key=lambda e: list(e.path))
            for e in errs:
                loc = "/" + "/".join(str(p) for p in e.path) if e.path else "(root)"
                fb.append(_fb(FailureSubtype.TOOL_MISUSE.value, f"参数 {loc}: {e.message}", "满足工具参数 schema"))

        # side_effects 权限
        if tool.side_effects not in allowed_side_effects:
            fb.append(_fb(FailureSubtype.TOOL_MISUSE.value,
                          f"side_effects={tool.side_effects} 不被 policy 允许",
                          f"允许集 {sorted(allowed_side_effects)}"))

        # 网络权限(沙箱默认禁网)
        if tool.requires_network and not allow_network:
            fb.append(_fb(FailureSubtype.TOOL_MISUSE.value,
                          f"工具 {tool_name} 需联网但当前禁网", "仅放行显式授权出网的工具"))

        ok = not fb
        return ToolGateResult(
            ok=ok, tool_name=tool_name, stage="pre",
            gate_result=GateResult(ok=ok, failure_type=None if ok else ftype, feedback=fb),
        )


class PostToolGate:
    @staticmethod
    def check(
        tool_name: str,
        result: object,
        max_output_chars: int = 100_000,
        taint_tags: Optional[list[str]] = None,
    ) -> ToolGateResult:
        fb: list[StructuredFeedback] = []
        ftype = FailureType.SPEC_ADHERENCE

        size = len(json.dumps(result, ensure_ascii=False, default=str))
        if size > max_output_chars:
            fb.append(_fb(FailureSubtype.OUTPUT_TOO_LARGE.value,
                          f"工具输出 {size} 字符 > 上限 {max_output_chars}", f"≤ {max_output_chars}"))

        taint_tags = taint_tags or []
        if taint_tags:
            # 命中污染(如 prompt_injection)→ 拒绝进入 ContextStore 作指令
            ftype = FailureType.GROUNDING
            fb.append(_fb(FailureSubtype.IRRELEVANT_RESULT.value,
                          f"工具输出命中污染标记 {taint_tags};只作数据、不作指令,且不放行",
                          "外部工具输出默认 untrusted"))

        ok = not fb
        return ToolGateResult(
            ok=ok, tool_name=tool_name, stage="post",
            gate_result=GateResult(ok=ok, failure_type=None if ok else ftype, feedback=fb),
        )
