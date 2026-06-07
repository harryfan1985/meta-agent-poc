"""§4.4 工具门 + §3.6 注册表 最小机判版。"""
from meta_agent.schemas import ToolDefinition
from meta_agent.tools import PostToolGate, PreToolGate, ToolRegistry


def _registry():
    return ToolRegistry([
        ToolDefinition(
            name="web_search", handler_ref="h_web", requires_network=True, side_effects="read",
            backend_schema={"type": "object", "required": ["query"],
                            "properties": {"query": {"type": "string"}}},
        ),
        ToolDefinition(name="repo_edit", handler_ref="h_edit", side_effects="write"),
    ])


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
