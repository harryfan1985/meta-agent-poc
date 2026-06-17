"""Stage 5 ConstructionVerifier(§3.5):静态 + 行为,统一 GateResult。"""
import textwrap

from meta_agent.artifacts import ArtifactLoader
from meta_agent.construction_verifier import ConstructionVerifier, representative_input
from meta_agent.construct import Stages, construct
from meta_agent.coordinator import execute
from meta_agent.fixtures.function_completion import (
    CANDIDATE_CODE,
    FIXTURES,
    TASK_INPUT_EXAMPLE,
    build_plan,
)
from meta_agent.schemas import (
    AgentArtifact,
    AgentSpec,
    FailureType,
    FieldSpec,
    IOContract,
    ParsedIntent,
    VerificationCriteria,
)

# code_verifier 的代表性输入需含目标函数,否则 cv1 python_assert 失败(真实 M2 由 planner 提供)
SAMPLE_INPUTS = {
    "spec_analyzer": dict(TASK_INPUT_EXAMPLE),
    "code_verifier": {"candidate_code": CANDIDATE_CODE, "parsed_spec": {}},
}


def _verifier():
    return ConstructionVerifier(ArtifactLoader(fixture_registry=FIXTURES), sample_inputs=SAMPLE_INPUTS)


def _fixture_artifact(spec_id, handler):
    return AgentArtifact(spec_id=spec_id, implementation_kind="fixture", handler_ref=handler, passed=True)


def test_representative_input_by_type():
    spec = AgentSpec(spec_id="t", io_contract=IOContract(input_schema={
        "s": FieldSpec(type="string", description=""),
        "b": FieldSpec(type="boolean", description=""),
        "a": FieldSpec(type="array", description=""),
    }))
    assert representative_input(spec) == {"s": "x", "b": True, "a": []}


def test_behavioral_pass_on_good_fixture():
    plan = build_plan()
    spec = next(s for s in plan.specs if s.spec_id == "spec_analyzer")
    gate = _verifier().verify(_fixture_artifact("spec_analyzer", "fx_spec_analyzer"), spec)
    assert gate.ok is True


def test_wildcard_sample_input_is_used_when_spec_sample_missing():
    plan = build_plan()
    spec = next(s for s in plan.specs if s.spec_id == "spec_analyzer")
    verifier = ConstructionVerifier(
        ArtifactLoader(fixture_registry=FIXTURES),
        sample_inputs={"*": dict(TASK_INPUT_EXAMPLE)},
    )
    gate = verifier.verify(_fixture_artifact("spec_analyzer", "fx_spec_analyzer"), spec)
    assert gate.ok is True


def test_midstream_node_gathers_from_upstream_sample_output():
    """核心:中游节点的代表性输入来自上游样例产出(而非全局 task_input)。"""
    plan = build_plan()
    sa = next(s for s in plan.specs if s.spec_id == "spec_analyzer")
    ap = next(s for s in plan.specs if s.spec_id == "algo_planner")
    v = ConstructionVerifier(ArtifactLoader(fixture_registry=FIXTURES),
                             task_input=dict(TASK_INPUT_EXAMPLE))
    # 入口节点先验 → 产出进入累积器
    assert v.verify(_fixture_artifact("spec_analyzer", "fx_spec_analyzer"), sa).ok is True
    # algo_planner 无显式 sample:parsed_spec 应取自 spec_analyzer 的样例产出
    gate = v.verify(_fixture_artifact("algo_planner", "fx_algo_planner"), ap)
    assert gate.ok is True
    assert v._sample_outputs["algo_planner"]["approach"]["algorithm"] == "pairwise_compare"


def test_entry_node_falls_back_to_representative_input():
    """无 task_input / sample 时,入口节点用按类型生成的最小输入。"""
    plan = build_plan()
    sa = next(s for s in plan.specs if s.spec_id == "spec_analyzer")
    gate = ConstructionVerifier(ArtifactLoader(fixture_registry=FIXTURES)).verify(
        _fixture_artifact("spec_analyzer", "fx_spec_analyzer"), sa)
    assert gate.ok is True


def test_midstream_typemin_fallback_when_upstream_missing():
    """中游节点在上游样例缺失时用类型最小值兜底,行为运行不中断。"""
    plan = build_plan()
    ap = next(s for s in plan.specs if s.spec_id == "algo_planner")
    gate = ConstructionVerifier(ArtifactLoader(fixture_registry=FIXTURES)).verify(
        _fixture_artifact("algo_planner", "fx_algo_planner"), ap)
    assert gate.ok is True  # parsed_spec 用 {} 兜底,fixture 仍产出合法 approach


def test_behavioral_fail_routes_typed():
    plan = build_plan()
    spec = next(s for s in plan.specs if s.spec_id == "spec_analyzer")
    # 丢 inequality_strict → sa1 失败 → spec_adherence
    gate = _verifier().verify(
        _fixture_artifact("spec_analyzer", "fx_spec_analyzer_drop_inequality"), spec)
    assert gate.ok is False
    assert gate.failure_type == FailureType.SPEC_ADHERENCE


def test_static_artifact_load_failure():
    plan = build_plan()
    spec = next(s for s in plan.specs if s.spec_id == "spec_analyzer")
    # 指向不存在的 fixture handler → 加载失败
    gate = _verifier().verify(_fixture_artifact("spec_analyzer", "no_such_handler"), spec)
    assert gate.ok is False
    assert any(f.subtype == "schema_violation" for f in gate.feedback)


def test_static_ast_forbidden_import(tmp_path):
    mod = tmp_path / "bad_agent.py"
    mod.write_text(textwrap.dedent("""
        import os
        def run(message, history):
            return {"out": "x"}
    """))
    import sys
    sys.path.insert(0, str(tmp_path))
    try:
        spec = AgentSpec(
            spec_id="t",
            io_contract=IOContract(output_schema={"out": FieldSpec(type="string", description="")},
                                   required_out=["out"]),
            verification_criteria=VerificationCriteria(forbidden_patterns=["os"]),
        )
        art = AgentArtifact(spec_id="t", implementation_kind="python_module",
                            module_path="bad_agent", entrypoint="run")
        gate = ConstructionVerifier(ArtifactLoader()).verify(art, spec)
        assert gate.ok is False
        assert any(f.subtype == "forbidden_hit" for f in gate.feedback)
    finally:
        sys.path.remove(str(tmp_path))


def test_construct_to_execute_e2e_no_llm():
    """全新 plan → 构造期验证(真实 ConstructionVerifier)→ bind → execute 端到端 PASS。
    codegen 用 fixture(无 LLM),验证 construct() 编排接真实 Stage 5 的闭环。"""
    loader = ArtifactLoader(fixture_registry=FIXTURES)
    handler_map = {
        "spec_analyzer": "fx_spec_analyzer", "algo_planner": "fx_algo_planner",
        "code_synthesizer": "fx_code_synthesizer", "code_verifier": "fx_code_verifier",
    }

    def codegen(spec, plan, feedback):
        return _fixture_artifact(spec.spec_id, handler_map[spec.spec_id])

    stages = Stages(
        parse=lambda t: ParsedIntent(goal=t),
        plan=lambda pi: build_plan(),
        ground=lambda p: p,
        codegen=codegen,
        verify=ConstructionVerifier(loader, sample_inputs=SAMPLE_INPUTS).verify,
    )
    swarm = construct("has_close_elements", stages)
    assert all(a.passed for a in swarm.artifacts.values())

    loader.bind(swarm)
    out = execute(swarm, dict(TASK_INPUT_EXAMPLE))
    assert out["passed"] is True
    assert "def has_close_elements" in out["final_code"]
