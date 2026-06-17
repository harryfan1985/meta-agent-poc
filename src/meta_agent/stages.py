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
from .schemas import ParsedIntent, SwarmPlan

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
    """Stage 3:定向检索回填 grounding。M2 默认 pass-through(纯推理 agent 无需 grounding)。
    真实 web_search 接入属 [eval]。"""

    def __init__(self, backend=None):
        self.backend = backend

    def ground(self, plan: SwarmPlan) -> SwarmPlan:
        return plan


def default_stages(backend, loader, task_input: dict | None = None,
                   sample_inputs: dict | None = None) -> Stages:
    """把 Stage 1/2/3(LLM 接缝)+ Stage 4(确定性模板化)+ Stage 5(验证器)装成 Stages。

    task_input:swarm 级样例输入,Stage 5 行为验证按 DAG 把它喂给入口节点,
    中游节点改用上游样例产出。sample_inputs 为显式 per-spec 覆盖(可选)。
    """
    return Stages(
        parse=IntentParser(backend).parse,
        plan=SwarmPlanner(backend).plan,
        ground=GroundingResearcher(backend).ground,
        codegen=prompt_template_codegen,
        verify=ConstructionVerifier(loader, task_input=task_input,
                                    sample_inputs=sample_inputs).verify,
    )
