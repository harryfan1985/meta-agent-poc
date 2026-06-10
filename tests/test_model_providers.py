import builtins
import sys
import types

import pytest

from meta_agent.llm import (
    AnthropicStructuredLLM,
    OpenAIStructuredLLM,
    create_structured_llm,
    generate_validated,
)
from meta_agent.schemas import SurfaceFailure

SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "additionalProperties": False,
    "properties": {"answer": {"type": "integer"}},
}


class _AnthropicMessages:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.response


class _AnthropicClient:
    def __init__(self, response=None, exc=None):
        self.messages = _AnthropicMessages(response, exc)


def test_anthropic_adapter_extracts_forced_tool_input():
    response = {
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "name": "emit_structured_output", "input": {"answer": 42}},
        ],
    }
    client = _AnthropicClient(response=response)
    backend = AnthropicStructuredLLM("claude-test", client=client)

    assert backend.generate("sys", {"x": 1}, SCHEMA) == {"answer": 42}
    call = client.messages.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "emit_structured_output"}
    assert call["tools"][0]["strict"] is True
    assert call["tools"][0]["input_schema"] == SCHEMA


def test_anthropic_refusal_surfaces_typed_failure():
    backend = AnthropicStructuredLLM("claude-test", client=_AnthropicClient(response={"stop_reason": "refusal"}))
    with pytest.raises(SurfaceFailure) as ei:
        backend.generate("sys", {}, SCHEMA)
    assert ei.value.gate_result.feedback[0].subtype == "model_refusal"


def test_anthropic_empty_output_surfaces_typed_failure():
    backend = AnthropicStructuredLLM("claude-test", client=_AnthropicClient(response={"content": []}))
    with pytest.raises(SurfaceFailure) as ei:
        backend.generate("sys", {}, SCHEMA)
    assert ei.value.gate_result.feedback[0].subtype == "model_empty_output"


def test_anthropic_timeout_surfaces_typed_failure():
    backend = AnthropicStructuredLLM("claude-test", client=_AnthropicClient(exc=TimeoutError("slow")))
    with pytest.raises(SurfaceFailure) as ei:
        backend.generate("sys", {}, SCHEMA)
    assert ei.value.gate_result.feedback[0].subtype == "model_timeout"


class _OpenAIResponses:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.response


class _OpenAIClient:
    def __init__(self, response=None, exc=None):
        self.responses = _OpenAIResponses(response, exc)


def test_openai_adapter_extracts_responses_structured_text():
    response = {
        "status": "completed",
        "output": [{"content": [{"text": '{"answer": 7}'}]}],
    }
    client = _OpenAIClient(response=response)
    backend = OpenAIStructuredLLM("gpt-test", client=client)

    assert backend.generate("sys", {"x": 1}, SCHEMA) == {"answer": 7}
    call = client.responses.calls[0]
    assert call["text"]["format"]["type"] == "json_schema"
    assert call["text"]["format"]["strict"] is True
    assert call["text"]["format"]["schema"] == SCHEMA


def test_openai_adapter_extracts_content_level_parsed_output():
    response = {
        "status": "completed",
        "output": [{"content": [{"parsed": {"answer": 8}}]}],
    }
    backend = OpenAIStructuredLLM("gpt-test", client=_OpenAIClient(response=response))

    assert backend.generate("sys", {}, SCHEMA) == {"answer": 8}


class _ChatCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return {"choices": [{"message": {"content": '{"answer": 9}'}}]}


class _ChatClient:
    def __init__(self):
        self.chat = types.SimpleNamespace(completions=_ChatCompletions())


def test_openai_adapter_falls_back_to_chat_completions():
    client = _ChatClient()
    backend = OpenAIStructuredLLM("gpt-test", client=client)

    assert backend.generate("sys", {}, SCHEMA) == {"answer": 9}
    call = client.chat.completions.calls[0]
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True


def test_openai_invalid_json_surfaces_schema_violation():
    backend = OpenAIStructuredLLM(
        "gpt-test",
        client=_OpenAIClient(response={"status": "completed", "output": [{"content": [{"text": "nope"}]}]}),
    )
    with pytest.raises(SurfaceFailure) as ei:
        backend.generate("sys", {}, SCHEMA)
    assert ei.value.gate_result.feedback[0].subtype == "model_schema_violation"


def test_provider_factory_creates_expected_adapters():
    assert isinstance(create_structured_llm("anthropic", "claude-test", client=_AnthropicClient()), AnthropicStructuredLLM)
    assert isinstance(create_structured_llm("openai", "gpt-test", client=_OpenAIClient()), OpenAIStructuredLLM)
    with pytest.raises(ValueError):
        create_structured_llm("anthropic", "")
    with pytest.raises(ValueError):
        create_structured_llm("other", "m")  # type: ignore[arg-type]


def test_generate_validated_still_performs_project_schema_check():
    backend = AnthropicStructuredLLM(
        "claude-test",
        client=_AnthropicClient(
            response={
                "content": [
                    {"type": "tool_use", "name": "emit_structured_output", "input": {"answer": "bad"}},
                    {"type": "tool_use", "name": "emit_structured_output", "input": {"answer": 3}},
                ]
            }
        ),
    )

    # The fake provider always returns the first invalid block; project-side validation must reject it.
    with pytest.raises(SurfaceFailure) as ei:
        generate_validated(backend, "sys", {}, SCHEMA, max_retries=0)
    assert ei.value.gate_result.feedback[0].subtype == "schema_violation"


def test_missing_api_key_surfaces_clear_error(monkeypatch):
    class FakeAnthropicModule:
        class Anthropic:
            def __init__(self, **kwargs):
                raise AssertionError("should fail before client creation")

    monkeypatch.setitem(sys.modules, "anthropic", FakeAnthropicModule)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    backend = AnthropicStructuredLLM("claude-test")

    with pytest.raises(SurfaceFailure) as ei:
        backend.generate("sys", {}, SCHEMA)
    assert ei.value.gate_result.feedback[0].subtype == "model_backend_error"
    assert "ANTHROPIC_API_KEY" in ei.value.gate_result.feedback[0].evidence


def test_openai_missing_api_key_surfaces_clear_error(monkeypatch):
    class FakeOpenAIModule:
        class OpenAI:
            def __init__(self, **kwargs):
                raise AssertionError("should fail before client creation")

    monkeypatch.setitem(sys.modules, "openai", FakeOpenAIModule)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    backend = OpenAIStructuredLLM("gpt-test")

    with pytest.raises(SurfaceFailure) as ei:
        backend.generate("sys", {}, SCHEMA)
    assert ei.value.gate_result.feedback[0].subtype == "model_backend_error"
    assert "OPENAI_API_KEY" in ei.value.gate_result.feedback[0].evidence


def test_missing_sdk_surfaces_clear_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    backend = AnthropicStructuredLLM("claude-test")

    with pytest.raises(SurfaceFailure) as ei:
        backend.generate("sys", {}, SCHEMA)
    assert ei.value.gate_result.feedback[0].subtype == "model_backend_error"
    assert "anthropic" in ei.value.gate_result.feedback[0].evidence.lower()


def test_openai_missing_sdk_surfaces_clear_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openai":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    backend = OpenAIStructuredLLM("gpt-test")

    with pytest.raises(SurfaceFailure) as ei:
        backend.generate("sys", {}, SCHEMA)
    assert ei.value.gate_result.feedback[0].subtype == "model_backend_error"
    assert "openai" in ei.value.gate_result.feedback[0].evidence.lower()
