"""结构化模型接缝(§7 LLM 后端,schema-driven)。

与 opencode(执行面节点执行器)不同:这是"prompt 进、满足 JSON Schema 的结构化
对象出"的轻接缝。构造期 Stage 1/2/3、执行期 prompt_template agent、M3 模型判 verifier
都复用它。单测用 StubStructuredLLM,真实后端(Anthropic native tool/JSON schema)留 [eval]。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

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


@dataclass(frozen=True)
class StructuredLLMConfig:
    """Provider adapter config. `model` stays explicit to avoid stale built-in defaults."""

    provider: Literal["anthropic", "openai"]
    model: str
    max_tokens: int = 4096
    temperature: float = 0.0
    timeout: float = 60.0
    api_key_env: str | None = None
    base_url: str | None = None


def _model_failure(reason: str, subtype: str, evidence: str = "") -> SurfaceFailure:
    return SurfaceFailure(
        reason,
        gate_result=GateResult(
            ok=False,
            failure_type=FailureType.SPEC_ADHERENCE,
            feedback=[
                StructuredFeedback(
                    subtype=subtype,
                    evidence=evidence[:1000],
                    expected="provider returns a dict matching the requested JSON Schema",
                    actionable_fix="check provider credentials, model, schema support, or adapter parsing",
                )
            ],
        ),
    )


def _failure_subtype(exc: SurfaceFailure) -> str | None:
    """从 SurfaceFailure 取首条 feedback 的 subtype,用于路由判定。"""
    gate = getattr(exc, "gate_result", None)
    feedback = getattr(gate, "feedback", None) if gate is not None else None
    if feedback:
        return getattr(feedback[0], "subtype", None)
    return None


def _is_timeout_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    return isinstance(exc, TimeoutError) or "timeout" in name or "timedout" in name


def _get_api_key(env_name: str) -> str:
    key = os.getenv(env_name)
    if not key:
        raise _model_failure(
            "model api key missing",
            "model_backend_error",
            f"environment variable {env_name!r} is not set",
        )
    return key


def _strip_json_text(text: str) -> str:
    """容忍 chatty/reasoning 模型:剥离 ```json 围栏与前后散文,取最外层 JSON 对象。
    仅用于 json_object 降级路径;不影响原生结构化输出的 schema_violation 语义。"""
    if not isinstance(text, str):
        return text
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        return s
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        return s[start : end + 1]
    return s


def _coerce_dict(value: Any, *, reason: str) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            raise _model_failure(reason, "model_schema_violation", f"invalid JSON text: {e}") from e
        if isinstance(parsed, dict):
            return parsed
    if value in (None, "", []):
        raise _model_failure(reason, "model_empty_output", f"got {type(value).__name__}")
    raise _model_failure(reason, "model_schema_violation", f"got {type(value).__name__}")


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _extract_text_from_openai_response(response: Any) -> str:
    text = _obj_get(response, "output_text")
    if isinstance(text, str) and text.strip():
        return text
    chunks: list[str] = []
    for item in _obj_get(response, "output", []) or []:
        for content in _obj_get(item, "content", []) or []:
            t = _obj_get(content, "text")
            if isinstance(t, str):
                chunks.append(t)
    return "\n".join(chunks)


def _extract_parsed_from_openai_response(response: Any) -> Any:
    parsed = _obj_get(response, "output_parsed")
    if parsed is not None:
        return parsed
    for item in _obj_get(response, "output", []) or []:
        for content in _obj_get(item, "content", []) or []:
            parsed = _obj_get(content, "parsed")
            if parsed is not None:
                return parsed
    return None


def _extract_text_from_openai_chat(response: Any) -> str:
    choices = _obj_get(response, "choices", []) or []
    if not choices:
        return ""
    message = _obj_get(choices[0], "message")
    parsed = _obj_get(message, "parsed")
    if parsed is not None:
        return json.dumps(parsed, ensure_ascii=False)
    content = _obj_get(message, "content")
    return content if isinstance(content, str) else ""


class AnthropicStructuredLLM:
    """StructuredLLM adapter using a forced synthetic tool call."""

    def __init__(
        self,
        model: str,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: float = 60.0,
        api_key_env: str = "ANTHROPIC_API_KEY",
        client: Any = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.api_key_env = api_key_env
        self.client = client

    def _client(self):
        if self.client is not None:
            return self.client
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - environment dependent
            raise _model_failure(
                "anthropic sdk missing",
                "model_backend_error",
                'install optional extra: pip install -e ".[anthropic]"',
            ) from e
        self.client = anthropic.Anthropic(api_key=_get_api_key(self.api_key_env), timeout=self.timeout)
        return self.client

    def generate(self, system: str, user: dict, json_schema: dict) -> dict:
        tool_name = "emit_structured_output"
        try:
            response = self._client().messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=system,
                messages=[{"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
                tools=[
                    {
                        "name": tool_name,
                        "description": "Emit the final structured output for the requested schema.",
                        "strict": True,
                        "input_schema": json_schema,
                    }
                ],
                tool_choice={"type": "tool", "name": tool_name},
            )
        except SurfaceFailure:
            raise
        except Exception as e:  # noqa: BLE001
            if _is_timeout_error(e):
                raise _model_failure("anthropic request timed out", "model_timeout", str(e)) from e
            raise _model_failure("anthropic request failed", "model_backend_error", str(e)) from e

        if _obj_get(response, "stop_reason") == "refusal":
            raise _model_failure("anthropic model refused", "model_refusal", repr(response))
        for block in _obj_get(response, "content", []) or []:
            if _obj_get(block, "type") == "tool_use" and _obj_get(block, "name") == tool_name:
                return _coerce_dict(_obj_get(block, "input"), reason="anthropic tool output invalid")
            if _obj_get(block, "type") == "refusal":
                raise _model_failure("anthropic model refused", "model_refusal", repr(block))
        raise _model_failure("anthropic response missing forced tool output", "model_empty_output", repr(response))


class OpenAIStructuredLLM:
    """StructuredLLM adapter using strict JSON Schema structured outputs."""

    def __init__(
        self,
        model: str,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: float = 60.0,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str | None = None,
        structured_mode: str = "auto",  # auto | json_schema | json_object
        client: Any = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.api_key_env = api_key_env
        self.base_url = base_url
        self.structured_mode = structured_mode
        self.client = client

    def _client(self):
        if self.client is not None:
            return self.client
        try:
            import openai
        except ImportError as e:  # pragma: no cover - environment dependent
            raise _model_failure(
                "openai sdk missing",
                "model_backend_error",
                'install optional extra: pip install -e ".[openai]"',
            ) from e
        kwargs = {"api_key": _get_api_key(self.api_key_env), "timeout": self.timeout}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        self.client = openai.OpenAI(**kwargs)
        return self.client

    def generate(self, system: str, user: dict, json_schema: dict) -> dict:
        if self.structured_mode == "json_object":
            return self._generate_json_object(system, user, json_schema)
        try:
            return self._generate_native(system, user, json_schema)
        except SurfaceFailure as e:
            # auto 仅在"端点不支持原生 structured 这条传输"时降级(provider 的 400/404,
            # subtype=model_backend_error),如 bitfun 的 json_schema 不可用。模型内容类失败
            # (schema_violation / refusal / timeout / empty)是真实错误,必须如实 surface。
            if self.structured_mode == "json_schema" or _failure_subtype(e) != "model_backend_error":
                raise
            return self._generate_json_object(system, user, json_schema)

    def _generate_json_object(self, system: str, user: dict, json_schema: dict) -> dict:
        """降级路径:chat.completions + json_object,把 schema 注入 prompt;
        下游 generate_model/TemplateAgent 仍用 jsonschema 校验+重试。"""
        client = self._client()
        sys = (system or "") + (
            "\n\nReturn ONLY a single JSON object conforming to this JSON Schema "
            "(no prose, no markdown fences):\n" + json.dumps(json_schema, ensure_ascii=False)
        )
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": sys},
                    {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
                ],
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                response_format={"type": "json_object"},
            )
        except SurfaceFailure:
            raise
        except Exception as e:  # noqa: BLE001
            if _is_timeout_error(e):
                raise _model_failure("openai request timed out", "model_timeout", str(e)) from e
            raise _model_failure("openai request failed", "model_backend_error", str(e)) from e
        text = _extract_text_from_openai_chat(response)
        if "refusal" in text.lower() and not text.strip().startswith("{"):
            raise _model_failure("openai model refused", "model_refusal", text)
        return _coerce_dict(_strip_json_text(text), reason="openai json_object output invalid")

    def _generate_native(self, system: str, user: dict, json_schema: dict) -> dict:
        try:
            client = self._client()
            if hasattr(client, "responses"):
                response = client.responses.create(
                    model=self.model,
                    instructions=system,
                    input=json.dumps(user, ensure_ascii=False),
                    max_output_tokens=self.max_tokens,
                    temperature=self.temperature,
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "structured_output",
                            "strict": True,
                            "schema": json_schema,
                        }
                    },
                )
            else:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
                    ],
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "structured_output",
                            "strict": True,
                            "schema": json_schema,
                        },
                    },
                )
        except SurfaceFailure:
            raise
        except Exception as e:  # noqa: BLE001
            if _is_timeout_error(e):
                raise _model_failure("openai request timed out", "model_timeout", str(e)) from e
            raise _model_failure("openai request failed", "model_backend_error", str(e)) from e

        if _obj_get(response, "status") == "refused":
            raise _model_failure("openai model refused", "model_refusal", repr(response))
        parsed = _extract_parsed_from_openai_response(response)
        if parsed is not None:
            return _coerce_dict(parsed, reason="openai parsed output invalid")
        text = _extract_text_from_openai_response(response) or _extract_text_from_openai_chat(response)
        if "refusal" in text.lower() and not text.strip().startswith("{"):
            raise _model_failure("openai model refused", "model_refusal", text)
        return _coerce_dict(text, reason="openai structured output invalid")


def create_structured_llm(
    provider: Literal["anthropic", "openai"],
    model: str,
    *,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    timeout: float = 60.0,
    api_key_env: str | None = None,
    base_url: str | None = None,
    client: Any = None,
) -> StructuredLLM:
    if not model:
        raise ValueError("model is required")
    if provider == "anthropic":
        return AnthropicStructuredLLM(
            model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            api_key_env=api_key_env or "ANTHROPIC_API_KEY",
            client=client,
        )
    if provider == "openai":
        return OpenAIStructuredLLM(
            model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            api_key_env=api_key_env or "OPENAI_API_KEY",
            base_url=base_url,
            client=client,
        )
    raise ValueError(f"unknown structured LLM provider: {provider!r}")


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
