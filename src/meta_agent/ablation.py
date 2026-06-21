"""M3 消融评测(§9):在 eval_harness 上切换组件(验证 / grounding),量化各自贡献。

论文消融:去掉验证掉 7.1 分、去掉 grounding 掉 5.5 分——是最吃重的两个组件。本框架对每个
配置跑一遍 eval_harness,汇总各配置成功率与相对 baseline 的差(component contribution)。

注意:eval_harness 度量的是"能否端到端跑通(construct+execute 不 surface)",不是最终答案
正确率。要复现论文"验证提分"的结论需为每个 benchmark 任务接正确性 oracle(留作 benchmark 工作);
本框架负责消融的编排与对比,task 集与 oracle 由调用方注入。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .eval_harness import BuildFn, EvalReport, run_eval


@dataclass(frozen=True)
class AblationConfig:
    name: str
    with_verification: bool = True
    with_grounding: bool = True


@dataclass
class AblationReport:
    rows: list[tuple[AblationConfig, EvalReport]] = field(default_factory=list)

    def pass_rates(self) -> dict[str, float]:
        return {cfg.name: report.pass_rate for cfg, report in self.rows}

    def deltas(self, baseline: str) -> dict[str, float]:
        """相对 baseline 的成功率差:baseline_rate - cfg_rate。
        正值 = 去掉该组件后掉分(组件有贡献),对应论文 7.1 / 5.5。"""
        rates = self.pass_rates()
        base = rates.get(baseline, 0.0)
        return {name: round(base - rate, 4) for name, rate in rates.items() if name != baseline}

    def summary(self, baseline: str = "full") -> dict:
        return {
            "pass_rates": {k: round(v, 4) for k, v in self.pass_rates().items()},
            "deltas_vs_" + baseline: self.deltas(baseline),
            "failure_by_type": {
                cfg.name: report.failure_by_type() for cfg, report in self.rows
            },
        }


def run_ablation(
    cases: list[tuple[str, dict]],
    configs: list[AblationConfig],
    build_for_config: Callable[[AblationConfig], BuildFn],
    *,
    runs: int = 1,
    oracle_for=None,
) -> AblationReport:
    """对每个配置构造对应的 build(注入相应 Stages 旋钮)并跑 eval_harness。
    oracle_for 透传给 run_eval:消融按正确性(而非完成率)计分时使用。"""
    report = AblationReport()
    for cfg in configs:
        eval_report = run_eval(cases, build_for_config(cfg), runs=runs, oracle_for=oracle_for)
        report.rows.append((cfg, eval_report))
    return report


# 标准三配置:全开 + 去验证 + 去 grounding(对齐论文消融轴)
STANDARD_ABLATIONS = [
    AblationConfig("full", with_verification=True, with_grounding=True),
    AblationConfig("no_verification", with_verification=False, with_grounding=True),
    AblationConfig("no_grounding", with_verification=True, with_grounding=False),
]
