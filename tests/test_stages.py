"""构造期 Stage 1/2/3 + default_stages(确定性,StubStructuredLLM,不调真实 LLM)。"""
from meta_agent.artifacts import ArtifactLoader
from meta_agent.construct import construct
from meta_agent.coordinator import execute
from meta_agent.fixtures.function_completion import CANDIDATE_CODE, TASK_INPUT_EXAMPLE, build_plan
from meta_agent.llm import StubStructuredLLM
from meta_agent.schemas import ParsedIntent, SwarmPlan
from meta_agent.stages import IntentParser, SwarmPlanner, default_stages


def test_intent_parser_returns_parsed_intent():
    backend = StubStructuredLLM(lambda s, u, j: {"goal": "g", "constraints": ["c"]})
    pi = IntentParser(backend).parse("write has_close_elements")
    assert isinstance(pi, ParsedIntent)
    assert pi.goal == "g" and pi.constraints == ["c"]
    # task 进了 user
    assert backend.calls[0][1]["task"] == "write has_close_elements"


def test_swarm_planner_returns_swarm_plan():
    canned = build_plan().model_dump(mode="json")
    backend = StubStructuredLLM(lambda s, u, j: canned)
    plan = SwarmPlanner(backend).plan(ParsedIntent(goal="g"))
    assert isinstance(plan, SwarmPlan)
    assert {s.spec_id for s in plan.specs} == {
        "spec_analyzer", "algo_planner", "code_synthesizer", "code_verifier"}


def test_default_stages_construct_to_execute_e2e():
    """全栈构造期(Stage1/2/3 走 StructuredLLM + Stage4 模板化 + Stage5 验证器)→ execute。
    一个 backend 同时服务规划(返 SwarmPlan)和执行期 prompt_template agent(返节点输出)。"""
    plan_dict = build_plan().model_dump(mode="json")
    node_outputs = {
        frozenset(["raw_signature", "parsed_spec"]): {
            "raw_signature": TASK_INPUT_EXAMPLE["raw_signature"],
            "parsed_spec": {"inequality_strict": True},
        },
        frozenset(["approach"]): {"approach": {"algorithm": "pairwise_compare"}},
        frozenset(["candidate_code"]): {"candidate_code": CANDIDATE_CODE},
        frozenset(["final_code", "passed"]): {"final_code": CANDIDATE_CODE, "passed": True},
    }

    def responder(system, user, json_schema):
        props = frozenset(json_schema.get("properties", {}).keys())
        if props in node_outputs:        # 执行期 prompt_template agent 的节点输出
            return node_outputs[props]
        if "goal" in props:              # Stage 1 ParsedIntent
            return {"goal": "write has_close_elements"}
        return plan_dict                 # Stage 2 SwarmPlan

    backend = StubStructuredLLM(responder)
    loader = ArtifactLoader(structured_llm=backend)
    stages = default_stages(backend, loader, sample_inputs={
        "spec_analyzer": dict(TASK_INPUT_EXAMPLE),
        "code_verifier": {"candidate_code": CANDIDATE_CODE, "parsed_spec": {}},
    })

    swarm = construct("write has_close_elements", stages)
    assert all(a.implementation_kind == "prompt_template" for a in swarm.artifacts.values())

    loader.bind(swarm)
    out = execute(swarm, dict(TASK_INPUT_EXAMPLE))
    assert out["passed"] is True
    assert "def has_close_elements" in out["final_code"]
