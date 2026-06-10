"""结构化模型接缝 + prompt_template 路径(确定性,StubStructuredLLM,不调真实 LLM)。"""
import pytest

from meta_agent.artifacts import ArtifactLoader
from meta_agent.codegen import prompt_template_codegen, synthesize_system_prompt
from meta_agent.construct import Stages, construct
from meta_agent.coordinator import execute
from meta_agent.fixtures.function_completion import CANDIDATE_CODE, TASK_INPUT_EXAMPLE, build_plan
from meta_agent.llm import StubStructuredLLM, TemplateAgent, generate_validated
from meta_agent.schemas import (
    AgentArtifact,
    ExecutableSwarm,
    ParsedIntent,
    SurfaceFailure,
)

_SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "integer"}}}


# ---- generate_validated ----

def test_generate_validated_returns_on_valid():
    backend = StubStructuredLLM(lambda s, u, j: {"answer": 42})
    assert generate_validated(backend, "sys", {}, _SCHEMA) == {"answer": 42}
    assert len(backend.calls) == 1


def test_generate_validated_retries_then_succeeds():
    seq = iter([{"answer": "nope"}, {"answer": 7}])  # 第一次违反 schema(string),第二次通过
    backend = StubStructuredLLM(lambda s, u, j: next(seq))
    out = generate_validated(backend, "sys", {}, _SCHEMA, max_retries=2)
    assert out == {"answer": 7}
    assert len(backend.calls) == 2
    # 校验反馈进了第二次调用的 user
    assert "_validation_feedback" in backend.calls[1][1]


def test_generate_validated_surfaces_after_retries():
    backend = StubStructuredLLM(lambda s, u, j: {"answer": "bad"})
    with pytest.raises(SurfaceFailure) as ei:
        generate_validated(backend, "sys", {}, _SCHEMA, max_retries=1)
    assert ei.value.gate_result.feedback[0].subtype == "schema_violation"


# ---- TemplateAgent ----

def test_template_agent_runs_and_validates():
    backend = StubStructuredLLM(lambda s, u, j: {"answer": 1})
    agent = TemplateAgent("be terse", _SCHEMA, backend)
    assert agent.run({"x": "v"}, []) == {"answer": 1}
    # message 进了 user.inputs
    assert backend.calls[0][1]["inputs"] == {"x": "v"}


# ---- Stage 4 deterministic templating ----

def test_synthesize_prompt_includes_contract():
    spec = next(s for s in build_plan().specs if s.spec_id == "code_synthesizer")
    p = synthesize_system_prompt(spec, build_plan())
    assert "candidate_code(string)" in p
    assert "Required output fields: candidate_code" in p
    assert "import os" in p  # forbidden 进提示


def test_codegen_produces_prompt_template_artifact():
    spec = next(s for s in build_plan().specs if s.spec_id == "spec_analyzer")
    art = prompt_template_codegen(spec, build_plan(), [])
    assert art.implementation_kind == "prompt_template"
    assert art.prompt_template  # 非空合成提示


# ---- 端到端:construct(prompt_template codegen)→ execute,全程 StubStructuredLLM ----

def _stub_backend_for_swarm():
    # 按目标输出 schema 的字段名,返回一组满足契约的 deterministic 输出
    outputs = {
        frozenset(["raw_signature", "parsed_spec"]): {
            "raw_signature": TASK_INPUT_EXAMPLE["raw_signature"],
            "parsed_spec": {"inequality_strict": True},
        },
        frozenset(["approach"]): {"approach": {"algorithm": "pairwise_compare"}},
        frozenset(["candidate_code"]): {"candidate_code": CANDIDATE_CODE},
        frozenset(["final_code", "passed"]): {"final_code": CANDIDATE_CODE, "passed": True},
    }

    def responder(system, user, json_schema):
        keys = frozenset(json_schema.get("properties", {}).keys())
        return outputs[keys]

    return StubStructuredLLM(responder)


def test_construct_prompt_template_then_execute_e2e():
    backend = _stub_backend_for_swarm()
    loader = ArtifactLoader(structured_llm=backend)

    # verify stage:直接复用 RuntimeGate via ConstructionVerifier
    from meta_agent.construction_verifier import ConstructionVerifier
    verifier = ConstructionVerifier(loader, sample_inputs={
        "spec_analyzer": dict(TASK_INPUT_EXAMPLE),
        "code_verifier": {"candidate_code": CANDIDATE_CODE, "parsed_spec": {}},
    })

    stages = Stages(
        parse=lambda t: ParsedIntent(goal=t),
        plan=lambda pi: build_plan(),
        ground=lambda p: p,
        codegen=prompt_template_codegen,  # Stage 4 确定性模板化
        verify=verifier.verify,
    )
    swarm = construct("has_close_elements", stages)
    assert all(a.implementation_kind == "prompt_template" for a in swarm.artifacts.values())
    assert all(a.passed for a in swarm.artifacts.values())

    loader.bind(swarm)
    out = execute(swarm, dict(TASK_INPUT_EXAMPLE))
    assert out["passed"] is True
    assert "def has_close_elements" in out["final_code"]
