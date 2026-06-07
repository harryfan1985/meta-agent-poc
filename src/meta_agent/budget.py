"""统一预算(§7.2)。触顶抛 BudgetExceeded;coordinator 转成 SurfaceFailure。"""
from __future__ import annotations

import time

from .schemas import Budget


class BudgetExceeded(Exception):
    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        self.detail = detail
        super().__init__(f"BudgetExceeded({kind}): {detail}")


class BudgetMeter:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.llm_calls = 0
        self.tokens = 0
        self.started_at = clock()

    def check_wall(self, budget: Budget) -> None:
        if self._clock() - self.started_at > budget.max_wall_seconds:
            raise BudgetExceeded("wall", f">{budget.max_wall_seconds}s")

    def record_run(self, budget: Budget, tokens: int = 0) -> None:
        """每次 agent 调用前后计量;超限即抛。"""
        self.llm_calls += 1
        self.tokens += tokens
        if self.llm_calls > budget.max_llm_calls:
            raise BudgetExceeded("llm_calls", f"{self.llm_calls}>{budget.max_llm_calls}")
        if self.tokens > budget.max_tokens:
            raise BudgetExceeded("tokens", f"{self.tokens}>{budget.max_tokens}")
        self.check_wall(budget)
