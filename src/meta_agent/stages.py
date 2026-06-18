"""构造期 Stage 1/2/3 薄实现 + default_stages 装配(§3)。

每个 Stage 是 StructuredLLM 接缝上的薄包装:prompt 进、Pydantic 模型出(校验+重试)。
单测用 StubStructuredLLM 确定性驱动;真实 prompt 调优属 [eval]。
Stage 4 用 codegen.prompt_template_codegen(确定性),Stage 5 用 ConstructionVerifier。
"""
from __future__ import annotations

from .codegen import prompt_template_codegen
from .construct import Stages
from .construction_verifier import ConstructionVerifier
from .llm import generate_model
from .schemas import GateResult, GroundingInfo, ParsedIntent, SwarmPlan

_INTENT_SYS = (
    "Compile the natural-language task into a structured intent. "
    "Output ONLY JSON matching ParsedIntent. Do not solve the task. "
    "Constraints must be atomic and checkable."
)

_PLAN_SYS = (
    "Decompose the task into a DAG of AgentSpecs using the FEWEST agents that cleanly "
    "separate concerns (1-4; use a SINGLE agent when the task is atomic). "
    "Each spec needs role, io_contract (typed input/output fields), dependencies, "
    "and non-empty verification_criteria (>=1 behavioral assertion, >=1 forbidden pattern). "
    "Pick forbidden patterns that the agent output will never legitimately contain "
    "(e.g. ``` code fences); never forbid content the spec must emit. "
    "For machine_assertions use ONLY these robust kinds: field_present, regex_match, "
    "equals_input. NEVER use python_assert, model_check, contains, not_contains or jsonschema. "
    "Keep machine_assertions minimal and assert only what the output reliably contains; "
    "when unsure, leave machine_assertions empty and rely on schema + behavioral_assertions. "
    "Each spec's `dependencies` MUST equal exactly the set of `from_spec` of the dag_edges "
    "pointing to it (no more, no less). "
    "Every spec input field MUST come from either the swarm task input or an upstream "
    "spec's output field (verbatim field name). "
    "Leave `constitution_rules` empty ([]); do not invent governance rules. "
    "Output ONLY JSON matching SwarmPlan."
)


class IntentParser:
    """Stage 1:NL task -> ParsedIntent。"""

    def __init__(self, backend):
        self.backend = backend

    def parse(self, task: str) -> ParsedIntent:
        return generate_model(self.backend, _INTENT_SYS, {"task": task}, ParsedIntent)


class SwarmPlanner:
    """Stage 2:ParsedIntent -> SwarmPlan(编译器大脑)。"""

    def __init__(self, backend):
        self.backend = backend

    def plan(self, parsed: ParsedIntent) -> SwarmPlan:
        return generate_model(self.backend, _PLAN_SYS, {"intent": parsed.model_dump()}, SwarmPlan)


class GroundingResearcher:
    """Stage 3:定向检索回填 grounding。默认 pass-through(纯推理 agent 无需 grounding);
    注入 web_search(Callable[[str], list[str]])后,按 spec 定向检索并把摘要写回 spec.grounding。"""

    def __init__(self, backend=None, web_search=None):
        self.backend = backend
        self.web_search = web_search

    @staticmethod
    def _query_for(spec, plan: SwarmPlan) -> str:
        return (f"{plan.summary} {spec.role}").strip() or spec.spec_id

    def ground(self, plan: SwarmPlan) -> SwarmPlan:
        if self.web_search is None:
            return plan  # 默认 pass-through
        grounded = plan.model_copy(deep=True)
        for spec in grounded.specs:
            results = self.web_search(self._query_for(spec, grounded)) or []
            if results:
                spec.grounding = GroundingInfo(research_summary=" ".join(results)[:1000])
        return grounded


def _passthrough_verify(artifact, spec) -> GateResult:
    """消融:关闭构造期验证——不门控,直接放行(Stage 5 ablation)。"""
    return GateResult(ok=True)


def default_stages(backend, loader, task_input: dict | None = None,
                   sample_inputs: dict | None = None, *,
                   with_verification: bool = True, web_search=None) -> Stages:
    """把 Stage 1/2/3(LLM 接缝)+ Stage 4(确定性模板化)+ Stage 5(验证器)装成 Stages。

    task_input:swarm 级样例输入,Stage 5 行为验证按 DAG 把它喂给入口节点,
    中游节点改用上游样例产出。sample_inputs 为显式 per-spec 覆盖(可选)。
    消融旋钮:with_verification=False 关闭构造期验证;web_search 注入真实 grounding。
    """
    verify = (
        ConstructionVerifier(loader, task_input=task_input, sample_inputs=sample_inputs).verify
        if with_verification else _passthrough_verify
    )
    return Stages(
        parse=IntentParser(backend).parse,
        plan=SwarmPlanner(backend).plan,
        ground=GroundingResearcher(backend, web_search=web_search).ground,
        codegen=prompt_template_codegen,
        verify=verify,
    )
