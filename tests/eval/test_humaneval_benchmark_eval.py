"""Real-model HumanEval benchmark:oracle 计分的 pass@1 + 验证消融。Skipped by default。

跑法:
    META_AGENT_RUN_PROVIDER_EVAL=1 META_AGENT_RUN_M2_E2E=1 META_AGENT_M2_PROVIDER=openai \
    META_AGENT_HE_LIMIT=3 pytest tests/eval/test_humaneval_benchmark_eval.py -q -s

oracle 在子进程沙箱里跑题目自带单测 → 确定性正确率。消融用正确率(而非完成率)计分,
delta 才反映验证的真实贡献。
"""
import os

import pytest

from meta_agent.ablation import AblationConfig, run_ablation
from meta_agent.artifacts import ArtifactLoader
from meta_agent.benchmarks import load_humaneval_subset, oracle_for_cases, to_eval_cases
from meta_agent.eval_harness import run_eval
from meta_agent.llm import create_structured_llm
from meta_agent.stages import default_stages

pytestmark = pytest.mark.eval


def _backend():
    if os.getenv("META_AGENT_RUN_PROVIDER_EVAL") != "1" or os.getenv("META_AGENT_RUN_M2_E2E") != "1":
        pytest.skip("set META_AGENT_RUN_PROVIDER_EVAL=1 and META_AGENT_RUN_M2_E2E=1 to run benchmark")
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


def _problems():
    return load_humaneval_subset(limit=int(os.getenv("META_AGENT_HE_LIMIT", "3")))


def test_humaneval_pass_at_1():
    backend = _backend()
    problems = _problems()
    cases = to_eval_cases(problems)

    def build(task_input):
        loader = ArtifactLoader(structured_llm=backend)
        return default_stages(backend, loader, task_input=task_input), loader

    report = run_eval(cases, build, oracle_for=oracle_for_cases(problems))
    print("\nHumanEval pass@1:", report.summary())
    for o in report.outcomes:
        print(f"  {'OK ' if o.ok else 'XX '}{o.task[:24]}... {o.phase or 'pass'}/{o.failure_type or ''}")
    assert report.total == len(problems)


def test_humaneval_verification_ablation():
    backend = _backend()
    problems = _problems()
    cases = to_eval_cases(problems)

    def build_for_config(cfg):
        def build(task_input):
            loader = ArtifactLoader(structured_llm=backend)
            stages = default_stages(backend, loader, task_input=task_input,
                                    with_verification=cfg.with_verification)
            return stages, loader
        return build

    configs = [AblationConfig("full", with_verification=True),
               AblationConfig("no_verification", with_verification=False)]
    report = run_ablation(cases, configs, build_for_config, oracle_for=oracle_for_cases(problems))
    print("\nHumanEval verification ablation:", report.summary())
    assert set(report.pass_rates()) == {"full", "no_verification"}
