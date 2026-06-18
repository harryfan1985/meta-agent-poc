"""M2 task-level 评测脚手架(§9 评测协议)。

对一组 (task, task_input) 跑 construct→execute,统计**成功率**与**失败类型分布**
(按 phase=construct/execute 与 failure_type=spec_adherence/contract/grounding/…)。

确定性核可用 stub stages 单测;真实 provider 评测由调用方注入 default_stages
(见 tests/eval/test_m2_eval_harness_eval.py)。harness 只做编排+归因,不接 LLM。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Optional

from .construct import Stages, construct
from .coordinator import execute
from .schemas import Budget, SurfaceFailure

# build: 给定 task_input,返回该次运行用的 (Stages, loader)。
# loader 必须是 construct 时 Stage 5 用的同一个,以便随后 loader.bind(swarm) 执行。
BuildFn = Callable[[dict], tuple[Stages, object]]


@dataclass
class CaseOutcome:
    """单次 construct→execute 的带类型归因结果。"""

    task: str
    run: int
    ok: bool
    phase: Optional[str] = None  # construct | execute | None(成功)
    failure_type: Optional[str] = None  # spec_adherence | contract | grounding | exception | ...
    failure_subtype: Optional[str] = None
    spec_id: Optional[str] = None
    detail: str = ""


@dataclass
class EvalReport:
    outcomes: list[CaseOutcome] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def failure_by_type(self) -> dict[str, int]:
        return dict(Counter(o.failure_type for o in self.outcomes if not o.ok and o.failure_type))

    def failure_by_phase(self) -> dict[str, int]:
        return dict(Counter(o.phase for o in self.outcomes if not o.ok and o.phase))

    def summary(self) -> dict:
        return {
            "total": self.total,
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 4),
            "failure_by_phase": self.failure_by_phase(),
            "failure_by_type": self.failure_by_type(),
        }


def _attribute(exc: SurfaceFailure, phase: str) -> CaseOutcome:
    gate = exc.gate_result
    ftype = getattr(getattr(gate, "failure_type", None), "value", None)
    feedback = getattr(gate, "feedback", None)
    subtype = getattr(feedback[0], "subtype", None) if feedback else None
    return CaseOutcome(
        task="", run=0, ok=False, phase=phase,
        failure_type=ftype, failure_subtype=subtype,
        spec_id=exc.spec_id, detail=exc.reason,
    )


def evaluate_case(
    task: str,
    task_input: dict,
    build: BuildFn,
    *,
    budget: Optional[Budget] = None,
    tracer=None,
) -> CaseOutcome:
    """单次 construct→execute。任何失败都归因为 CaseOutcome 而非中断,便于批量统计。"""
    stages, loader = build(task_input)
    try:
        swarm = construct(task, stages, budget=budget, tracer=tracer)
    except SurfaceFailure as e:
        return _attribute(e, "construct")
    except Exception as e:  # noqa: BLE001 — 评测稳健性:任何异常计为失败
        return CaseOutcome(task="", run=0, ok=False, phase="construct",
                           failure_type="exception", detail=f"{type(e).__name__}: {e}")
    try:
        loader.bind(swarm)
        execute(swarm, task_input, budget=budget, tracer=tracer)
    except SurfaceFailure as e:
        return _attribute(e, "execute")
    except Exception as e:  # noqa: BLE001
        return CaseOutcome(task="", run=0, ok=False, phase="execute",
                           failure_type="exception", detail=f"{type(e).__name__}: {e}")
    return CaseOutcome(task="", run=0, ok=True)


def run_eval(
    cases: list[tuple[str, dict]],
    build: BuildFn,
    *,
    runs: int = 1,
    budget: Optional[Budget] = None,
) -> EvalReport:
    """对每个 (task, task_input) 跑 runs 次,汇总成 EvalReport。"""
    report = EvalReport()
    for task, task_input in cases:
        for r in range(1, runs + 1):
            outcome = evaluate_case(task, task_input, build, budget=budget)
            outcome.task = task
            outcome.run = r
            report.outcomes.append(outcome)
    return report
