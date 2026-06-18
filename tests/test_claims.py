"""M3 claim-evidence:抽取 / 挂证据 / 核查 / 聚合 + ClaimEvidenceBackend(确定性 stub)。"""
from meta_agent.claims import (
    ClaimEvidenceBackend,
    aggregate_claim_results,
    attach_evidence,
    extract_claims,
    verify_claim,
)
from meta_agent.llm import StubStructuredLLM
from meta_agent.runtime_gate import RuntimeGate
from meta_agent.schemas import (
    AgentSpec,
    AssertionSpec,
    Claim,
    EvidenceRef,
    FailureType,
    FieldSpec,
    IOContract,
    VerificationCriteria,
    VerificationPolicy,
)
from meta_agent.verifiers import VerifierRegistry

MC = AssertionSpec(assertion_id="m1", kind="model_check", target_path="/answer",
                   description="answer summarizes the task")
MC2 = AssertionSpec(assertion_id="m2", kind="model_check", target_path="/headline",
                    description="headline matches the topic")
FP = AssertionSpec(assertion_id="f1", kind="field_present", target_path="/answer")


def _spec(assertions):
    return AgentSpec(
        spec_id="t", role="summarize",
        io_contract=IOContract(
            input_schema={"task": FieldSpec(type="string", description="t")},
            output_schema={"answer": FieldSpec(type="string", description="a")},
            required_in=["task"], required_out=["answer"]),
        verification_criteria=VerificationCriteria(machine_assertions=assertions))


def _judge(verdict):
    return StubStructuredLLM(lambda s, u, sc: {"verdict": verdict, "reason": "r"})


# ---------------------------------------------------------------- extract / attach


def test_extract_claims_one_per_model_check():
    claims = extract_claims({"answer": "a summary", "headline": "topic"}, _spec([MC, MC2, FP]))
    assert {c.claim_id for c in claims} == {"claim::m1", "claim::m2"}  # field_present 不抽
    assert next(c for c in claims if c.claim_id == "claim::m1").text == "a summary"


def test_attach_evidence_collects_spec_input_upstream():
    claim = Claim(claim_id="claim::m1", text="x", source_path="/answer")
    refs = attach_evidence(claim, _spec([MC]), {"task": "do X"}, store={"up": {"k": "v"}})
    assert {r.source_type for r in refs} == {"spec", "input", "upstream_output"}
    assert any(r.source_type == "input" and r.trust_level == "verified" for r in refs)
    assert claim.required_evidence == refs


# ---------------------------------------------------------------- verify_claim


def test_verify_claim_insufficient_without_evidence():
    out = verify_claim(_judge("supported"), Claim(claim_id="c", text="x", source_path="/a"), [])
    assert out.verification_status == "insufficient"


def test_verify_claim_supported_and_contradicted():
    ev = [EvidenceRef(source_type="input", ref="/task", quote_or_hash="do X")]
    assert verify_claim(_judge("supported"), Claim(claim_id="c", text="x", source_path="/a"), ev).verification_status == "supported"
    assert verify_claim(_judge("contradicted"), Claim(claim_id="c", text="x", source_path="/a"), ev).verification_status == "contradicted"


def test_verify_claim_malformed_or_error_is_insufficient():
    ev = [EvidenceRef(source_type="input", ref="/task")]
    bad = verify_claim(StubStructuredLLM(lambda s, u, sc: {"nope": 1}), Claim(claim_id="c", text="x", source_path="/a"), ev)
    assert bad.verification_status == "insufficient"

    def boom(s, u, sc):
        raise RuntimeError("down")

    err = verify_claim(StubStructuredLLM(boom), Claim(claim_id="c", text="x", source_path="/a"), ev)
    assert err.verification_status == "insufficient"


# ---------------------------------------------------------------- aggregate


def test_aggregate_all_supported_ok():
    c = Claim(claim_id="c", text="x", source_path="/a", verification_status="supported")
    g = aggregate_claim_results([c])
    assert g.ok is True
    assert g.coverage.claims_total == 1 and g.coverage.claims_verified == 1


def test_aggregate_unsupported_fails_spec_adherence():
    g = aggregate_claim_results([
        Claim(claim_id="c1", text="x", source_path="/a", verification_status="supported"),
        Claim(claim_id="c2", text="y", source_path="/b", verification_status="insufficient"),
    ])
    assert g.ok is False and g.failure_type == FailureType.SPEC_ADHERENCE
    assert any("c2" in f.evidence and f.subtype == "claim_insufficient" for f in g.feedback)


def test_aggregate_no_claims_is_ok():
    assert aggregate_claim_results([]).ok is True


# ---------------------------------------------------------------- backend + gate


def test_claim_evidence_backend_supported_passes():
    backend = ClaimEvidenceBackend(_judge("supported"))
    g = backend.verify(MC, spec=_spec([MC]), message={"task": "do X"}, output={"answer": "did X"}, trace=[])
    assert g.ok is True


def test_claim_evidence_backend_contradicted_fails():
    backend = ClaimEvidenceBackend(_judge("contradicted"))
    g = backend.verify(MC, spec=_spec([MC]), message={"task": "do X"}, output={"answer": "unrelated"}, trace=[])
    assert g.ok is False and g.failure_type == FailureType.SPEC_ADHERENCE


def test_claim_evidence_backend_routes_in_gate_via_registry():
    reg = VerifierRegistry([ClaimEvidenceBackend(_judge("supported"))])
    g = RuntimeGate.check({"answer": "did X"}, _spec([MC]), message={"task": "do X"},
                          policy=VerificationPolicy(allow_model_verification=True), verifier=reg)
    assert g.ok is True
