"""Real-model M2 task-level eval via eval_harness. Skipped by default.

跑法:
    META_AGENT_RUN_PROVIDER_EVAL=1 META_AGENT_RUN_M2_E2E=1 \
    META_AGENT_M2_PROVIDER=openai META_AGENT_M2_EVAL_RUNS=3 \
    pytest tests/eval/test_m2_eval_harness_eval.py -q -s

harness 输出成功率 + 失败类型分布(phase/failure_type),用于观测真实 task-level 可靠性。
"""
import os

import pytest

from meta_agent.artifacts import ArtifactLoader
from meta_agent.eval_harness import run_eval
from meta_agent.llm import create_structured_llm
from meta_agent.stages import default_stages

pytestmark = pytest.mark.eval

# 小而多样的任务集:不同输入字段名/输出形态,逼真考察 planner 的分解与契约设计。
CASES: list[tuple[str, dict]] = [
    ("Build a minimal swarm that reads input field `task` and returns output field "
     "`answer` as a concise string summary of the task.", {"task": "say hello"}),
    ("Build a small swarm that reads input field `text` and returns output field "
     "`sentiment` as exactly one of positive/negative/neutral.", {"text": "I really love this product"}),
    ("Build a swarm that reads input field `topic` and returns output field "
     "`headline` as a short news-style headline string.", {"topic": "local library reopening"}),
]


def _backend():
    if os.getenv("META_AGENT_RUN_PROVIDER_EVAL") != "1" or os.getenv("META_AGENT_RUN_M2_E2E") != "1":
        pytest.skip("set META_AGENT_RUN_PROVIDER_EVAL=1 and META_AGENT_RUN_M2_E2E=1 to run M2 eval")
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


def test_m2_eval_harness_reports_success_rate():
    backend = _backend()
    runs = int(os.getenv("META_AGENT_M2_EVAL_RUNS", "2"))

    def build(task_input):
        loader = ArtifactLoader(structured_llm=backend)
        return default_stages(backend, loader, task_input=task_input), loader

    report = run_eval(CASES, build, runs=runs)
    print("\nM2 eval summary:", report.summary())
    for o in report.outcomes:
        if not o.ok:
            print(f"  FAIL [{o.task[:40]}...] run={o.run} {o.phase}/{o.failure_type}"
                  f"/{o.failure_subtype} spec={o.spec_id}: {o.detail}")

    # harness 的职责是“测量”,不在此处硬卡阈值(真实成功率随模型波动);只做完整性 + 非全败 sanity。
    assert report.total == len(CASES) * runs
    assert report.passed >= 1
