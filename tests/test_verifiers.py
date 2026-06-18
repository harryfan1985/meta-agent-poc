"""M3 verifier 后端栈:BaseJudge / AspectPanel / Registry + RuntimeGate model_check 集成。

确定性:judge 用 StubStructuredLLM 注入 verdict,不接真实 LLM。
"""
from meta_agent.artifacts import ArtifactLoader
from meta_agent.construction_verifier import ConstructionVerifier
from meta_agent.coordinator import execute
from meta_agent.llm import StubStructuredLLM
from meta_agent.runtime_gate import RuntimeGate
from meta_agent.schemas import (
    AgentArtifact,
    AgentSpec,
    AssertionSpec,
    ExecutableSwarm,
    FailureType,
    FieldSpec,
    IOContract,
    SwarmPlan,
    VerificationCriteria,
    VerificationPolicy,
)
from meta_agent.verifiers import AspectPanelBackend, BaseJudgeBackend, VerifierRegistry

MC = AssertionSpec(assertion_id="m1", kind="model_check", target_path="/answer",
                   description="answer must concisely summarize the task")


def _spec(machine_assertions=None):
    return AgentSpec(
        spec_id="t", role="summarize",
        io_contract=IOContract(
            output_schema={"answer": FieldSpec(type="string", description="a")},
            required_out=["answer"]),
        verification_criteria=VerificationCriteria(machine_assertions=machine_assertions or []),
    )


def _judge(verdict, reason="r"):
    return StubStructuredLLM(lambda s, u, sch: {"verdict": verdict, "reason": reason})


# ---------------------------------------------------------------- BaseJudge


def test_base_judge_pass():
    g = BaseJudgeBackend(_judge("pass")).verify(MC, spec=_spec(), message={}, output={"answer": "hi"}, trace=[])
    assert g.ok is True


def test_base_judge_fail_is_spec_adherence():
    g = BaseJudgeBackend(_judge("fail", "not a summary")).verify(
        MC, spec=_spec(), message={}, output={"answer": "x"}, trace=[])
    assert g.ok is False
    assert g.failure_type == FailureType.SPEC_ADHERENCE
    assert "not a summary" in g.feedback[0].evidence


def test_base_judge_parse_failure_fails_closed():
    bad = StubStructuredLLM(lambda s, u, sch: {"nope": 1})  # 无 verdict 字段
    g = BaseJudgeBackend(bad).verify(MC, spec=_spec(), message={}, output={"answer": "x"}, trace=[])
    assert g.ok is False


def test_base_judge_backend_error_fails_closed():
    def boom(s, u, sch):
        raise RuntimeError("judge down")

    g = BaseJudgeBackend(StubStructuredLLM(boom)).verify(
        MC, spec=_spec(), message={}, output={"answer": "x"}, trace=[])
    assert g.ok is False
    assert g.feedback[0].subtype == "model_verifier_error"


def test_base_judge_prompt_hides_generator_identity():
    """自我增强偏差缓解:judge 输入不得含 agent 名 / pass index。"""
    stub = _judge("pass")
    BaseJudgeBackend(stub).verify(MC, spec=_spec(), message={"task": "x"}, output={"answer": "hi"}, trace=[])
    _sys, user, _schema = stub.calls[0]
    assert "spec_id" not in user and "pass" not in user and "agent" not in user


# ---------------------------------------------------------------- AspectPanel


def test_aspect_panel_unanimous_pass():
    g = AspectPanelBackend(_judge("pass")).verify(MC, spec=_spec(), message={}, output={"answer": "hi"}, trace=[])
    assert g.ok is True


def test_aspect_panel_majority_pass():
    def by_aspect(s, u, sch):  # 2 pass / 1 fail → 2/3 >= 0.5
        return {"verdict": "fail" if u["aspect"] == "spec_adherence" else "pass", "reason": u["aspect"]}

    g = AspectPanelBackend(StubStructuredLLM(by_aspect)).verify(
        MC, spec=_spec(), message={}, output={"answer": "hi"}, trace=[])
    assert g.ok is True


def test_aspect_panel_below_threshold_rejects_with_disagreement():
    def by_aspect(s, u, sch):  # 1 pass / 2 fail → 1/3 < 0.5
        return {"verdict": "pass" if u["aspect"] == "correctness" else "fail", "reason": u["aspect"]}

    g = AspectPanelBackend(StubStructuredLLM(by_aspect)).verify(
        MC, spec=_spec(), message={}, output={"answer": "hi"}, trace=[])
    assert g.ok is False
    assert any(f.subtype == "aspect_panel_rejected" and "disagreement" in f.evidence for f in g.feedback)


def test_aspect_panel_all_fail_tally():
    g = AspectPanelBackend(_judge("fail")).verify(MC, spec=_spec(), message={}, output={"answer": "x"}, trace=[])
    assert g.ok is False
    assert any("0/3 pass" in f.evidence for f in g.feedback)


# ---------------------------------------------------------------- Registry + Gate


def test_registry_routes_first_supporting_backend():
    judge = BaseJudgeBackend(_judge("pass"))
    reg = VerifierRegistry([judge])
    assert reg.for_kind("model_check") is judge
    assert reg.for_kind("python_assert") is None


def test_registry_respects_budget_predicate_no_downgrade():
    cheap = BaseJudgeBackend(_judge("pass"))
    reg = VerifierRegistry([cheap])
    # 预算不允许任何 backend → None(不降级到更弱 verifier)
    assert reg.for_kind("model_check", budget_allows=lambda b: False) is None


def test_gate_model_check_passes_with_registered_backend():
    reg = VerifierRegistry([BaseJudgeBackend(_judge("pass"))])
    g = RuntimeGate.check({"answer": "a concise summary"}, _spec(machine_assertions=[MC]),
                          policy=VerificationPolicy(allow_model_verification=True), verifier=reg)
    assert g.ok is True


def test_gate_model_check_fails_with_registered_backend_is_spec_adherence():
    reg = VerifierRegistry([BaseJudgeBackend(_judge("fail", "too vague"))])
    g = RuntimeGate.check({"answer": "x"}, _spec(machine_assertions=[MC]),
                          policy=VerificationPolicy(allow_model_verification=True), verifier=reg)
    assert g.ok is False
    assert g.failure_type == FailureType.SPEC_ADHERENCE


def test_gate_model_check_no_backend_fails_closed_contract():
    g = RuntimeGate.check({"answer": "x"}, _spec(machine_assertions=[MC]),
                          policy=VerificationPolicy(allow_model_verification=True),
                          verifier=VerifierRegistry([]))
    assert g.ok is False
    assert g.failure_type == FailureType.CONTRACT
    assert any(f.subtype == "model_check_backend_missing" for f in g.feedback)


# ---------------------------------------------------------------- end-to-end threading

def _mc_node_spec():
    return AgentSpec(
        spec_id="summarizer", role="summarize",
        io_contract=IOContract(
            input_schema={"task": FieldSpec(type="string", description="t")},
            output_schema={"answer": FieldSpec(type="string", description="a")},
            required_in=["task"], required_out=["answer"]),
        verification_criteria=VerificationCriteria(machine_assertions=[MC]),
    )


def _sum_loader():
    return ArtifactLoader(fixture_registry={"fx_sum": lambda m, h: {"answer": "summary of " + m.get("task", "")}})


def test_construction_verifier_runs_model_check_with_registry():
    cv = ConstructionVerifier(_sum_loader(), task_input={"task": "say hi"},
                              verifier=VerifierRegistry([BaseJudgeBackend(_judge("pass"))]))
    art = AgentArtifact(spec_id="summarizer", implementation_kind="fixture", handler_ref="fx_sum", passed=True)
    assert cv.verify(art, _mc_node_spec()).ok is True


def test_construction_verifier_model_check_fails_closed_without_registry():
    cv = ConstructionVerifier(_sum_loader(), task_input={"task": "say hi"})  # 无 verifier
    art = AgentArtifact(spec_id="summarizer", implementation_kind="fixture", handler_ref="fx_sum", passed=True)
    g = cv.verify(art, _mc_node_spec())
    assert g.ok is False and g.failure_type == FailureType.CONTRACT


def test_execute_threads_verifier_for_model_check():
    spec = _mc_node_spec()
    plan = SwarmPlan(swarm_name="s", specs=[spec], dag_edges=[],
                     verification_policy=VerificationPolicy(allow_model_verification=True))
    art = AgentArtifact(spec_id="summarizer", implementation_kind="fixture", handler_ref="fx_sum", passed=True)
    swarm = ExecutableSwarm(plan=plan, artifacts={"summarizer": art})
    loader = _sum_loader()
    loader.bind(swarm)
    out = execute(swarm, {"task": "hi"}, verifier=VerifierRegistry([BaseJudgeBackend(_judge("pass"))]))
    assert out == {"answer": "summary of hi"}
