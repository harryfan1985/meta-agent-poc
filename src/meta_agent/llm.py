"""结构化模型接缝(§7 LLM 后端,schema-driven)。

与 opencode(执行面节点执行器)不同:这是"prompt 进、满足 JSON Schema 的结构化
对象出"的轻接缝。构造期 Stage 1/2/3、执行期 prompt_template agent、M3 模型判 verifier
都复用它。单测用 StubStructuredLLM,真实后端(Anthropic native tool/JSON schema)留 [eval]。
"""
from __future__ import annotations

from typing import Callable, Protocol

from jsonschema import Draft202012Validator

from .schemas import FailureSubtype, FailureType, GateResult, StructuredFeedback, SurfaceFailure


class StructuredLLM(Protocol):
    """后端契约:给系统提示 + 输入 + 目标 JSON Schema,返回一个 dict。
    校验/重试由 generate_validated 统一兜底,后端只管产出。"""

    def generate(self, system: str, user: dict, json_schema: dict) -> dict: ...


class StubStructuredLLM:
    """确定性测试替身:由注入的 responder 决定返回。记录调用便于断言。"""

    def __init__(self, responder: Callable[[str, dict, dict], dict]):
        self.responder = responder
        self.calls: list[tuple] = []

    def generate(self, system: str, user: dict, json_schema: dict) -> dict:
        self.calls.append((system, user, json_schema))
        return self.responder(system, user, json_schema)


def generate_validated(
    backend: StructuredLLM,
    system: str,
    user: dict,
    json_schema: dict,
    *,
    max_retries: int = 2,
) -> dict:
    """生成 → JSON Schema 校验 → 不过则带校验错重试(预算内)→ 仍不过 surface。
    这条小循环是 construct() 类型化路由的缩影。"""
    last_errors: list[str] = []
    attempt_user = dict(user)
    for _ in range(max_retries + 1):
        out = backend.generate(system, attempt_user, json_schema)
        errors = sorted(Draft202012Validator(json_schema).iter_errors(out), key=lambda e: list(e.path))
        if not errors:
            return out
        last_errors = [
            ("/" + "/".join(str(p) for p in e.path) if e.path else "(root)") + ": " + e.message
            for e in errors[:3]
        ]
        attempt_user = {**user, "_validation_feedback": last_errors}  # 反馈进下一轮
    raise SurfaceFailure(
        "structured generation failed schema after retries",
        gate_result=GateResult(
            ok=False,
            failure_type=FailureType.SPEC_ADHERENCE,
            feedback=[
                StructuredFeedback(
                    subtype=FailureSubtype.SCHEMA_VIOLATION.value,
                    evidence="; ".join(last_errors),
                    expected="输出满足目标 JSON Schema",
                    actionable_fix="修正模型输出或 prompt/后端",
                )
            ],
        ),
    )


def generate_model(backend, system: str, user: dict, model_cls, *, max_retries: int = 2):
    """生成并校验为 Pydantic 模型实例(嵌套模型用此,比裸 jsonschema 稳)。
    校验失败带错重试;仍不过 surface。构造期 Stage 1/2/3 用它。"""
    from pydantic import ValidationError

    schema = model_cls.model_json_schema()
    last = ""
    attempt_user = dict(user)
    for _ in range(max_retries + 1):
        out = backend.generate(system, attempt_user, schema)
        try:
            return model_cls.model_validate(out)
        except ValidationError as e:
            last = str(e)
            attempt_user = {**user, "_validation_feedback": last}
    raise SurfaceFailure(
        f"structured generation failed validation for {model_cls.__name__}",
        gate_result=GateResult(
            ok=False,
            failure_type=FailureType.SPEC_ADHERENCE,
            feedback=[
                StructuredFeedback(
                    subtype=FailureSubtype.SCHEMA_VIOLATION.value,
                    evidence=last[:500],
                    expected=f"valid {model_cls.__name__}",
                    actionable_fix="修正模型输出或 prompt/后端",
                )
            ],
        ),
    )


class TemplateAgent:
    """执行期 prompt_template agent:合成系统提示 + 输出 schema → 调 StructuredLLM。
    run(message, history) -> dict(已校验 output_schema)。"""

    def __init__(self, system_prompt: str, output_schema: dict, backend: StructuredLLM, max_retries: int = 2):
        self.system_prompt = system_prompt
        self.output_schema = output_schema
        self.backend = backend
        self.max_retries = max_retries

    def run(self, message: dict, history: list) -> dict:
        return generate_validated(
            self.backend,
            self.system_prompt,
            {"inputs": message, "history": history},
            self.output_schema,
            max_retries=self.max_retries,
        )
