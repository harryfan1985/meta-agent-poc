"""M3 变异测试(meta-verification):验"验证器本身有效"。

思路:取一个能通过 gate 的合法输出,注入已知缺陷(变异),断言 gate 现在**抓得到**。
gate 仍放行的变异 = "存活变异"(surviving mutant)= 验证器盲区,应补断言。

只依赖机判(默认无 LLM);也可传 verifier 测模型判分支。变异算子作用于输出 dict。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Optional

from .runtime_gate import RuntimeGate
from .schemas import AgentSpec, GateResult, MutationCase, VerificationPolicy


def _field_name(target_path: str) -> str:
    return target_path[1:] if target_path.startswith("/") else target_path


def apply_mutation(output: dict, mutation: MutationCase) -> dict:
    """对合法输出注入一处缺陷,返回变异副本(不改原 output)。"""
    out = copy.deepcopy(output)
    name = _field_name(mutation.target_path)
    mt = mutation.mutation_type
    if mt == "drop_field":
        out.pop(name, None)
    elif mt == "wrong_value":
        out[name] = mutation.payload if mutation.payload is not None else "__MUTATED_WRONG__"
    elif mt == "forbidden_insert":
        inject = mutation.payload if mutation.payload is not None else "```"
        base = out.get(name, "")
        out[name] = (base if isinstance(base, str) else str(base)) + str(inject)
    elif mt == "off_by_one":
        val = out.get(name)
        if isinstance(val, bool):
            out[name] = not val
        elif isinstance(val, (int, float)):
            out[name] = val + 1
        elif isinstance(val, list):
            out[name] = val + [val[-1]] if val else [None]
        elif isinstance(val, str):
            out[name] = val[:-1] if val else val + "x"
    return out


@dataclass
class MutationResult:
    mutation_id: str
    mutation_type: str
    killed: bool  # gate 抓到(失败)=被杀
    failure_subtypes: list[str] = field(default_factory=list)


@dataclass
class MutationReport:
    base_ok: bool  # 基准合法输出是否通过 gate(前提)
    results: list[MutationResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def killed(self) -> int:
        return sum(1 for r in self.results if r.killed)

    @property
    def survived(self) -> list[str]:
        return [r.mutation_id for r in self.results if not r.killed]

    @property
    def kill_rate(self) -> float:
        return self.killed / self.total if self.total else 0.0

    def summary(self) -> dict:
        return {
            "base_ok": self.base_ok,
            "total": self.total,
            "killed": self.killed,
            "survived": self.survived,
            "kill_rate": round(self.kill_rate, 4),
        }


def run_mutation_testing(
    spec: AgentSpec,
    base_output: dict,
    mutations: list[MutationCase],
    *,
    message: Optional[dict] = None,
    policy: Optional[VerificationPolicy] = None,
    verifier=None,
) -> MutationReport:
    """前提:base_output 须通过 gate(否则变异测试无意义,base_ok=False)。
    对每个变异:注入 → gate.check → 失败即"被杀"(验证器有效),通过即"存活"(盲区)。"""
    base_gate = RuntimeGate.check(base_output, spec, message=message, policy=policy, verifier=verifier)
    report = MutationReport(base_ok=base_gate.ok)
    if not base_gate.ok:
        return report  # 基准都不过,先修 base/spec,再谈变异
    for m in mutations:
        mutated = apply_mutation(base_output, m)
        gate = RuntimeGate.check(mutated, spec, message=message, policy=policy, verifier=verifier)
        report.results.append(MutationResult(
            mutation_id=m.mutation_id,
            mutation_type=m.mutation_type,
            killed=not gate.ok,
            failure_subtypes=[f.subtype for f in gate.feedback],
        ))
    return report
