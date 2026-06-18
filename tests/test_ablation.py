"""M3 消融评测框架 + Stage 3 grounding 接缝(确定性,不接 LLM)。"""
import json

from meta_agent.ablation import STANDARD_ABLATIONS, AblationConfig, run_ablation
from meta_agent.artifacts import ArtifactLoader
from meta_agent.construct import Stages
from meta_agent.fixtures.function_completion import FIXTURES, TASK_INPUT_EXAMPLE, build_plan
from meta_agent.schemas import (
    AgentArtifact,
    AgentSpec,
    GateResult,
    IOContract,
    ParsedIntent,
    StructuredFeedback,
)
from meta_agent.schemas import FailureType
from meta_agent.stages import GroundingResearcher, default_stages

HANDLER_MAP = {
    "spec_analyzer": "fx_spec_analyzer", "algo_planner": "fx_algo_planner",
    "code_synthesizer": "fx_code_synthesizer", "code_verifier": "fx_code_verifier",
}
FB = [StructuredFeedback(subtype="decomp_flaw", evidence="e", expected="x", actionable_fix="f")]


# ---------------------------------------------------------------- Stage 3 grounding 接缝


def test_grounding_passthrough_without_web_search():
    plan = build_plan()
    assert GroundingResearcher().ground(plan) is plan  # 默认不改


def test_grounding_writes_summary_with_web_search():
    gr = GroundingResearcher(web_search=lambda q: [f"fact:{q[:8]}"])
    out = gr.ground(build_plan())
    assert out is not build_plan()  # 深拷贝(不改原 plan)
    assert all(s.grounding is not None and s.grounding.research_summary for s in out.specs)


def test_default_stages_verification_toggle():
    off = default_stages("backend", ArtifactLoader(), with_verification=False)
    spec = AgentSpec(spec_id="x", io_contract=IOContract())
    assert off.verify(AgentArtifact(spec_id="x"), spec).ok is True  # pass-through 放行


# ---------------------------------------------------------------- 消融框架


def _ok_build(task_input):
    loader = ArtifactLoader(fixture_registry=FIXTURES)
    stages = Stages(
        parse=lambda t: ParsedIntent(goal=t),
        plan=lambda pi: build_plan(),
        ground=lambda p: p,
        codegen=lambda spec, plan, fb: AgentArtifact(
            spec_id=spec.spec_id, implementation_kind="fixture",
            handler_ref=HANDLER_MAP[spec.spec_id], passed=True),
        verify=lambda a, s: GateResult(ok=True),
    )
    return stages, loader


def _fail_build(task_input):
    loader = ArtifactLoader(fixture_registry=FIXTURES)
    stages = Stages(
        parse=lambda t: ParsedIntent(goal=t),
        plan=lambda pi: build_plan(),
        ground=lambda p: p,
        codegen=lambda spec, plan, fb: AgentArtifact(spec_id=spec.spec_id, implementation_kind="fixture", handler_ref="h"),
        verify=lambda a, s: GateResult(ok=False, failure_type=FailureType.CONTRACT, feedback=FB),
    )
    return stages, loader


def test_run_ablation_tabulates_pass_rates_and_deltas():
    # 用 grounding 轴区分:有 grounding → ok build;无 → fail build(演示框架对比)
    def build_for_config(cfg):
        return _ok_build if cfg.with_grounding else _fail_build

    report = run_ablation([("t", dict(TASK_INPUT_EXAMPLE))], STANDARD_ABLATIONS, build_for_config)
    rates = report.pass_rates()
    assert rates["full"] == 1.0
    assert rates["no_verification"] == 1.0
    assert rates["no_grounding"] == 0.0

    deltas = report.deltas("full")
    assert deltas["no_grounding"] == 1.0   # 去 grounding 掉满分 → 贡献=1.0
    assert deltas["no_verification"] == 0.0
    json.dumps(report.summary())  # 可序列化上报


def test_ablation_config_defaults():
    c = AblationConfig("x")
    assert c.with_verification is True and c.with_grounding is True
