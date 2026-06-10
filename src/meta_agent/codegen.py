"""Stage 4 AgentCodeGen 默认实现:确定性 prompt_template 拼装(§3.4)。

最佳设计:Stage 4 默认产物是 prompt_template,不是生成 Python 代码——
- 基本确定性(从 spec 模板化系统提示),不调 LLM、不需沙箱;
- 智能在 planner(Stage 2)与执行时;
- 天然可被 ConstructionVerifier 在代表性输入上验证。
python_module(LLM 生成代码,要沙箱)和 external_agent(opencode,代码编辑)是可选后端。
"""
from __future__ import annotations

from .schemas import AgentArtifact, AgentSpec, SwarmPlan


def synthesize_system_prompt(spec: AgentSpec, plan: SwarmPlan) -> str:
    io = spec.io_contract
    in_fields = ", ".join(f"{k}({v.type})" for k, v in io.input_schema.items()) or "(none)"
    out_fields = ", ".join(f"{k}({v.type})" for k, v in io.output_schema.items()) or "(none)"
    lines = [
        f"You are the agent: {spec.role or spec.spec_id}.",
        f"Task: {io.description}" if io.description else "",
        f"Input fields: {in_fields}.",
        f"Output ONLY a JSON object with fields: {out_fields}.",
        f"Required output fields: {', '.join(io.required_out) or '(none)'}.",
    ]
    grounding = getattr(spec, "grounding", None)
    if grounding and getattr(grounding, "research_summary", ""):
        lines.append(f"Background knowledge: {grounding.research_summary}")
    vc = spec.verification_criteria
    if vc.behavioral_assertions:
        lines.append("Must satisfy: " + "; ".join(vc.behavioral_assertions) + ".")
    if vc.forbidden_patterns:
        lines.append("Must NOT contain: " + ", ".join(repr(p) for p in vc.forbidden_patterns) + ".")
    return "\n".join(line for line in lines if line)


def prompt_template_codegen(spec: AgentSpec, plan: SwarmPlan, feedback: list) -> AgentArtifact:
    """Stage 4 default:把 spec 拼成 prompt_template artifact(确定性,无 LLM)。"""
    system = synthesize_system_prompt(spec, plan)
    if feedback:  # 构造期 spec_adherence 回退时把反馈追加进提示
        system += "\nPrior verification feedback (fix these): " + "; ".join(
            getattr(f, "actionable_fix", "") or getattr(f, "evidence", "") for f in feedback
        )
    return AgentArtifact(
        spec_id=spec.spec_id,
        implementation_kind="prompt_template",
        prompt_template=system,
    )
