"""M3 verifier 校准协议(§4.7)。

上模型判 verifier 前,用 golden cases 量化其判定质量:precision / recall /
false-accept-rate / false-reject-rate / F1。普通任务优化 F1,高风险任务优先压低
false accept(把坏输出放行最危险)。未达阈值的 backend 不进 runtime hot path。

"positive" = accept(gate.ok=True)。混淆矩阵:
  tp: 应通过且判通过   fp: 应拒绝但判通过(false accept,最危险)
  fn: 应通过但判拒绝   tn: 应拒绝且判拒绝
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .schemas import AgentSpec, AssertionSpec


@dataclass
class CalibrationCase:
    """一条 golden 校准用例:已知 ground-truth 的 (spec, output) 对。"""

    case_id: str
    spec: AgentSpec
    output: dict
    expected_ok: bool  # ground truth:该输出是否应通过 verifier
    message: dict = field(default_factory=dict)


@dataclass
class CalibrationReport:
    backend: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    errors: list[str] = field(default_factory=list)  # 用例缺 model_check 等

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        return self.tp / d if d else 0.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn
        return self.tp / d if d else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def accuracy(self) -> float:
        return (self.tp + self.tn) / self.total if self.total else 0.0

    @property
    def false_accept_rate(self) -> float:
        """应拒绝中被错误放行的比例(高风险任务关键指标)。"""
        d = self.fp + self.tn
        return self.fp / d if d else 0.0

    @property
    def false_reject_rate(self) -> float:
        d = self.fn + self.tp
        return self.fn / d if d else 0.0

    def meets(self, *, min_f1: Optional[float] = None,
              max_false_accept_rate: Optional[float] = None) -> bool:
        if min_f1 is not None and self.f1 < min_f1:
            return False
        if max_false_accept_rate is not None and self.false_accept_rate > max_false_accept_rate:
            return False
        return True

    def summary(self) -> dict:
        return {
            "backend": self.backend,
            "total": self.total,
            "tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "accuracy": round(self.accuracy, 4),
            "false_accept_rate": round(self.false_accept_rate, 4),
            "false_reject_rate": round(self.false_reject_rate, 4),
            "errors": self.errors,
        }


def _model_check_assertion(spec: AgentSpec) -> Optional[AssertionSpec]:
    for a in spec.verification_criteria.machine_assertions:
        if a.kind == "model_check":
            return a
    return None


def calibrate(backend, cases: list[CalibrationCase]) -> CalibrationReport:
    """对每条 golden case 跑 backend.verify(其 model_check 断言),累计混淆矩阵。"""
    report = CalibrationReport(backend=getattr(backend, "name", type(backend).__name__))
    for c in cases:
        assertion = _model_check_assertion(c.spec)
        if assertion is None:
            report.errors.append(f"{c.case_id}: spec 无 model_check 断言,跳过")
            continue
        gate = backend.verify(assertion, spec=c.spec, message=c.message, output=c.output, trace=[])
        predicted_ok = bool(gate.ok)
        if c.expected_ok and predicted_ok:
            report.tp += 1
        elif c.expected_ok and not predicted_ok:
            report.fn += 1
        elif (not c.expected_ok) and predicted_ok:
            report.fp += 1
        else:
            report.tn += 1
    return report


def calibrated_backends(
    backends: list,
    cases: list[CalibrationCase],
    *,
    min_f1: Optional[float] = None,
    max_false_accept_rate: Optional[float] = None,
) -> tuple[list, list[CalibrationReport]]:
    """跑校准并只保留达阈值的 backend(§4.7:未达阈值不进 hot path)。
    返回 (通过的 backend 列表, 全部报告)。"""
    reports = [calibrate(b, cases) for b in backends]
    kept = [
        b for b, r in zip(backends, reports)
        if r.meets(min_f1=min_f1, max_false_accept_rate=max_false_accept_rate)
    ]
    return kept, reports
