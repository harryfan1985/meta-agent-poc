"""Real-model M3 消融(full vs no_verification)。Skipped by default。

跑法:
    META_AGENT_RUN_PROVIDER_EVAL=1 META_AGENT_RUN_M2_E2E=1 META_AGENT_M2_PROVIDER=openai \
    pytest tests/eval/test_m3_ablation_eval.py -q -s
"""
import os

import pytest

from meta_agent.ablation import AblationConfig, run_ablation
from meta_agent.artifacts import ArtifactLoader
from meta_agent.llm import create_structured_llm
from meta_agent.stages import default_stages

pytestmark = pytest.mark.eval

CASES = [
    ("Build a minimal swarm that reads input field `task` and returns output field "
     "`answer` as a concise string summary of the task.", {"task": "say hello"}),
    ("Build a small swarm that reads input field `text` and returns output field "
     "`sentiment` as exactly one of positive/negative/neutral.", {"text": "I really love this"}),
]
CONFIGS = [
    AblationConfig("full", with_verification=True),
    AblationConfig("no_verification", with_verification=False),
]


def _backend():
    if os.getenv("META_AGENT_RUN_PROVIDER_EVAL") != "1" or os.getenv("META_AGENT_RUN_M2_E2E") != "1":
        pytest.skip("set META_AGENT_RUN_PROVIDER_EVAL=1 and META_AGENT_RUN_M2_E2E=1 to run ablation")
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


def test_m3_ablation_full_vs_no_verification():
    backend = _backend()
    runs = int(os.getenv("META_AGENT_M3_ABLATION_RUNS", "1"))

    def build_for_config(cfg):
        def build(task_input):
            loader = ArtifactLoader(structured_llm=backend)
            stages = default_stages(backend, loader, task_input=task_input,
                                    with_verification=cfg.with_verification)
            return stages, loader
        return build

    report = run_ablation(CASES, CONFIGS, build_for_config, runs=runs)
    print("\nM3 ablation:", report.summary())
    assert set(report.pass_rates()) == {"full", "no_verification"}
