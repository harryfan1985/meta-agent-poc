"""§4.4 工具门 + §3.6 注册表 最小机判版。"""
import pytest

from meta_agent.schemas import SurfaceFailure, ToolDefinition
from meta_agent.tools import PostToolGate, PreToolGate, ToolExecutor, ToolRegistry


def _registry():
    return ToolRegistry([
        ToolDefinition(
            name="web_search", handler_ref="h_web", requires_network=True, side_effects="read",
            backend_schema={"type": "object", "required": ["query"],
                            "properties": {"query": {"type": "string"}}},
        ),
        ToolDefinition(name="repo_edit", handler_ref="h_edit", side_effects="write"),
    ])


def _registry_with_handlers(calls):
    return ToolRegistry(
        [
            ToolDefinition(
                name="web_search", handler_ref="h_web", requires_network=True, side_effects="read",
                backend_schema={"type": "object", "required": ["query"],
                                "properties": {"query": {"type": "string"}}},
            ),
            ToolDefinition(name="repo_edit", handler_ref="h_edit", side_effects="write"),
        ],
        handlers={
            "h_web": lambda params: calls.append(("web_search", params)) or {"data": params["query"]},
            "h_edit": lambda params: calls.append(("repo_edit", params)) or {"ok": True},
        },
    )


def test_registry_validate_unregistered():
    reg = _registry()
    assert reg.validate(["web_search", "ghost"]) == ["ghost"]
    assert reg.has("web_search") and not reg.has("ghost")


def test_pre_unregistered_tool_is_contract():
    g = PreToolGate.check("ghost", {}, _registry())
    assert g.ok is False
    assert g.gate_result.failure_type.value == "contract"
    assert any(f.subtype == "tool_misuse" for f in g.gate_result.feedback)


def test_pre_param_schema_violation():
    g = PreToolGate.check("web_search", {"query": 123}, _registry(), allow_network=True)
    assert g.ok is False
    assert any(f.subtype == "tool_misuse" for f in g.gate_result.feedback)


def test_pre_write_side_effect_rejected_by_default():
    g = PreToolGate.check("repo_edit", {}, _registry())  # 默认 allowed={none,read}
    assert g.ok is False
    assert any("side_effects" in f.evidence for f in g.gate_result.feedback)


def test_pre_write_allowed_when_policy_permits():
    g = PreToolGate.check("repo_edit", {}, _registry(), allowed_side_effects={"none", "read", "write"})
    assert g.ok is True


def test_pre_network_blocked_by_default():
    g = PreToolGate.check("web_search", {"query": "x"}, _registry(), allow_network=False)
    assert g.ok is False
    assert any("禁网" in f.evidence or "联网" in f.evidence for f in g.gate_result.feedback)


def test_pre_ok():
    g = PreToolGate.check("web_search", {"query": "x"}, _registry(), allow_network=True)
    assert g.ok is True


def test_post_output_too_large():
    g = PostToolGate.check("web_search", {"data": "x" * 1000}, max_output_chars=100)
    assert g.ok is False
    assert any(f.subtype == "output_too_large" for f in g.gate_result.feedback)


def test_post_tainted_rejected():
    g = PostToolGate.check("web_search", {"data": "ok"}, taint_tags=["prompt_injection"])
    assert g.ok is False
    assert g.gate_result.failure_type.value == "grounding"


def test_post_ok():
    g = PostToolGate.check("web_search", {"data": "ok"})
    assert g.ok is True


def test_tool_executor_invokes_handler_when_gates_pass():
    calls = []
    executor = ToolExecutor(_registry_with_handlers(calls), allow_network=True)
    out = executor.invoke("web_search", {"query": "meta-agent"})
    assert out == {"data": "meta-agent"}
    assert calls == [("web_search", {"query": "meta-agent"})]


def test_tool_executor_pre_gate_blocks_handler_call():
    calls = []
    executor = ToolExecutor(_registry_with_handlers(calls), allow_network=False)
    with pytest.raises(SurfaceFailure) as ei:
        executor.invoke("web_search", {"query": "meta-agent"})
    assert ei.value.gate_result.failure_type.value == "spec_adherence"
    assert calls == []


def test_tool_executor_post_gate_blocks_tainted_result():
    calls = []
    executor = ToolExecutor(_registry_with_handlers(calls), allow_network=True)
    with pytest.raises(SurfaceFailure) as ei:
        executor.invoke("web_search", {"query": "meta-agent"}, taint_tags=["prompt_injection"])
    assert ei.value.gate_result.failure_type.value == "grounding"
    assert calls == [("web_search", {"query": "meta-agent"})]


def test_tool_executor_missing_handler_surfaces_typed_failure():
    reg = ToolRegistry([ToolDefinition(name="web_search", handler_ref="missing")])
    executor = ToolExecutor(reg)
    with pytest.raises(SurfaceFailure) as ei:
        executor.invoke("web_search", {})
    assert ei.value.gate_result.failure_type.value == "spec_adherence"
    assert any("handler_ref" in f.evidence for f in ei.value.gate_result.feedback)
