"""Real-model verifier calibration (§4.7). Skipped by default.

跑法:
    META_AGENT_RUN_PROVIDER_EVAL=1 META_AGENT_M2_PROVIDER=openai \
    pytest tests/eval/test_verifier_calibration_eval.py -q -s

对 BaseJudge / AspectPanel 跑 golden 校准,打印 precision/recall/F1/false-accept。
"""
import os

import pytest

from meta_agent.calibration import CalibrationCase, calibrate
from meta_agent.llm import create_structured_llm
from meta_agent.schemas import (
    AgentSpec,
    AssertionSpec,
    FieldSpec,
    IOContract,
    VerificationCriteria,
)
from meta_agent.verifiers import AspectPanelBackend, BaseJudgeBackend

pytestmark = pytest.mark.eval

_MC = AssertionSpec(
    assertion_id="mc", kind="model_check", target_path="/answer",
    description="answer is a faithful, concise one-sentence summary of the input task text; "
                "it must be non-empty, not a refusal, and clearly about the same topic.")


def _summary_spec() -> AgentSpec:
    return AgentSpec(
        spec_id="summarizer", role="summarize text",
        io_contract=IOContract(
            input_schema={"task": FieldSpec(type="string", description="text")},
            output_schema={"answer": FieldSpec(type="string", description="summary")},
            required_in=["task"], required_out=["answer"]),
        verification_criteria=VerificationCriteria(machine_assertions=[_MC]))


_TASK = "The city council approved a new bike lane on Main Street after months of debate."
_M = {"task": _TASK}

GOLDEN = [
    CalibrationCase("good1", _summary_spec(),
                    {"answer": "The council approved a Main Street bike lane after long debate."}, True, _M),
    CalibrationCase("good2", _summary_spec(),
                    {"answer": "Main Street will get a new bike lane following council approval."}, True, _M),
    CalibrationCase("bad_empty", _summary_spec(), {"answer": ""}, False, _M),
    CalibrationCase("bad_unrelated", _summary_spec(),
                    {"answer": "Bananas are a good source of potassium."}, False, _M),
    CalibrationCase("bad_refusal", _summary_spec(), {"answer": "I cannot help with that."}, False, _M),
]


def _backend_llm():
    if os.getenv("META_AGENT_RUN_PROVIDER_EVAL") != "1":
        pytest.skip("set META_AGENT_RUN_PROVIDER_EVAL=1 to run verifier calibration")
    provider = os.getenv("META_AGENT_M2_PROVIDER", "anthropic")
    if provider == "openai":
        model = os.getenv("META_AGENT_OPENAI_MODEL")
        if not (os.getenv("OPENAI_API_KEY") and model):
            pytest.skip("set OPENAI_API_KEY and META_AGENT_OPENAI_MODEL")
        return create_structured_llm("openai", model, base_url=os.getenv("META_AGENT_OPENAI_BASE_URL"))
    model = os.getenv("META_AGENT_ANTHROPIC_MODEL")
    if not (os.getenv("ANTHROPIC_API_KEY") and model):
        pytest.skip("set ANTHROPIC_API_KEY and META_AGENT_ANTHROPIC_MODEL")
    return create_structured_llm("anthropic", model)


def test_calibrate_judge_and_panel_on_golden():
    llm = _backend_llm()
    for backend in (BaseJudgeBackend(llm), AspectPanelBackend(llm)):
        report = calibrate(backend, GOLDEN)
        print(f"\ncalibration[{backend.name}]:", report.summary())
        assert report.total == len(GOLDEN)
        assert not report.errors
        assert report.tp + report.tn >= 1  # sanity:非全错;真实阈值由观测设定
