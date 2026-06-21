"""HumanEval 风格 benchmark 加载 + 确定性正确性 oracle(§9)。

oracle 把 swarm 最终输出里的函数源码放进 code_sandbox 跑题目自带单测,通过才算成功——
确定性、不需模型判。可从 JSONL 加载真实 HumanEval,缺省用内置小子集(自洽,无需外部数据)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .code_sandbox import run_code_tests


@dataclass
class HumanEvalProblem:
    task_id: str
    prompt: str  # 函数签名 + docstring
    entry_point: str  # 目标函数名
    test: str  # def check(candidate): ... (HumanEval 格式)
    canonical_solution: str = ""  # 完整参考实现(签名+体),用于自测/golden


# 内置小子集:完整函数体的 canonical(便于无外部数据自洽测试)。
_BUILTIN: list[HumanEvalProblem] = [
    HumanEvalProblem(
        task_id="builtin/has_close_elements",
        prompt=("def has_close_elements(numbers, threshold):\n"
                "    \"\"\"Return True if any two numbers are closer than threshold.\"\"\"\n"),
        entry_point="has_close_elements",
        test=("def check(candidate):\n"
              "    assert candidate([1.0, 2.0, 3.0], 0.5) is False\n"
              "    assert candidate([1.0, 2.8, 3.0], 0.3) is True\n"
              "    assert candidate([], 1.0) is False\n"),
        canonical_solution=(
            "def has_close_elements(numbers, threshold):\n"
            "    for i in range(len(numbers)):\n"
            "        for j in range(i + 1, len(numbers)):\n"
            "            if abs(numbers[i] - numbers[j]) < threshold:\n"
            "                return True\n"
            "    return False\n"),
    ),
    HumanEvalProblem(
        task_id="builtin/sum_list",
        prompt=("def sum_list(xs):\n"
                "    \"\"\"Return the sum of the numbers in xs (0 for empty).\"\"\"\n"),
        entry_point="sum_list",
        test=("def check(candidate):\n"
              "    assert candidate([]) == 0\n"
              "    assert candidate([1, 2, 3]) == 6\n"
              "    assert candidate([-1, 1]) == 0\n"),
        canonical_solution=("def sum_list(xs):\n    total = 0\n    for x in xs:\n        total += x\n    return total\n"),
    ),
    HumanEvalProblem(
        task_id="builtin/is_palindrome",
        prompt=("def is_palindrome(s):\n"
                "    \"\"\"Return True if s reads the same forwards and backwards.\"\"\"\n"),
        entry_point="is_palindrome",
        test=("def check(candidate):\n"
              "    assert candidate('') is True\n"
              "    assert candidate('aba') is True\n"
              "    assert candidate('ab') is False\n"),
        canonical_solution=("def is_palindrome(s):\n    return s == s[::-1]\n"),
    ),
    # —— 更难:含边界陷阱,模型易写出"看似对实则错"的实现 ——
    HumanEvalProblem(
        task_id="builtin/below_zero",
        prompt=("def below_zero(operations):\n"
                "    \"\"\"Given deposit/withdrawal ops on a zero-start balance, return True if the\n"
                "    running balance EVER goes strictly below zero. Balance of exactly 0 is not below.\"\"\"\n"),
        entry_point="below_zero",
        test=("def check(candidate):\n"
              "    assert candidate([]) is False\n"
              "    assert candidate([1, -1]) is False\n"      # 触底为 0,不算 below
              "    assert candidate([1, -2, 3]) is True\n"
              "    assert candidate([-1]) is True\n"),
        canonical_solution=("def below_zero(operations):\n    balance = 0\n    for op in operations:\n"
                            "        balance += op\n        if balance < 0:\n            return True\n    return False\n"),
    ),
    HumanEvalProblem(
        task_id="builtin/intersperse",
        prompt=("def intersperse(numbers, delimiter):\n"
                "    \"\"\"Insert `delimiter` between every two consecutive elements of `numbers`.\n"
                "    No trailing delimiter; empty input returns [].\"\"\"\n"),
        entry_point="intersperse",
        test=("def check(candidate):\n"
              "    assert candidate([], 4) == []\n"
              "    assert candidate([1], 4) == [1]\n"
              "    assert candidate([1, 2, 3], 4) == [1, 4, 2, 4, 3]\n"),
        canonical_solution=("def intersperse(numbers, delimiter):\n    if not numbers:\n        return []\n"
                            "    result = [numbers[0]]\n    for n in numbers[1:]:\n        result.append(delimiter)\n"
                            "        result.append(n)\n    return result\n"),
    ),
    HumanEvalProblem(
        task_id="builtin/rolling_max",
        prompt=("def rolling_max(numbers):\n"
                "    \"\"\"Return a list where each element is the maximum of all elements seen so far\n"
                "    (running maximum), same length as input.\"\"\"\n"),
        entry_point="rolling_max",
        test=("def check(candidate):\n"
              "    assert candidate([]) == []\n"
              "    assert candidate([3, 1, 2]) == [3, 3, 3]\n"
              "    assert candidate([1, 2, 3, 2, 3, 4, 2]) == [1, 2, 3, 3, 3, 4, 4]\n"),
        canonical_solution=("def rolling_max(numbers):\n    result = []\n    m = None\n    for n in numbers:\n"
                            "        m = n if m is None else max(m, n)\n        result.append(m)\n    return result\n"),
    ),
    HumanEvalProblem(
        task_id="builtin/count_distinct_characters",
        prompt=("def count_distinct_characters(s):\n"
                "    \"\"\"Count how many distinct characters s has, IGNORING case ('A' and 'a' are same).\"\"\"\n"),
        entry_point="count_distinct_characters",
        test=("def check(candidate):\n"
              "    assert candidate('') == 0\n"
              "    assert candidate('xyzXYZ') == 3\n"        # 大小写折叠
              "    assert candidate('Jerry') == 4\n"),
        canonical_solution=("def count_distinct_characters(s):\n    return len(set(s.lower()))\n"),
    ),
    HumanEvalProblem(
        task_id="builtin/greatest_common_divisor",
        prompt=("def greatest_common_divisor(a, b):\n"
                "    \"\"\"Return the greatest common divisor of integers a and b.\"\"\"\n"),
        entry_point="greatest_common_divisor",
        test=("def check(candidate):\n"
              "    assert candidate(3, 5) == 1\n"
              "    assert candidate(25, 15) == 5\n"
              "    assert candidate(0, 7) == 7\n"
              "    assert candidate(12, 8) == 4\n"),
        canonical_solution=("def greatest_common_divisor(a, b):\n    while b:\n        a, b = b, a % b\n    return a\n"),
    ),
    HumanEvalProblem(
        task_id="builtin/flip_case",
        prompt=("def flip_case(s):\n"
                "    \"\"\"Swap the case of every letter in s (lower<->upper).\"\"\"\n"),
        entry_point="flip_case",
        test=("def check(candidate):\n"
              "    assert candidate('') == ''\n"
              "    assert candidate('Hello') == 'hELLO'\n"
              "    assert candidate('aBc') == 'AbC'\n"),
        canonical_solution=("def flip_case(s):\n    return s.swapcase()\n"),
    ),
    HumanEvalProblem(
        task_id="builtin/fib",
        prompt=("def fib(n):\n"
                "    \"\"\"Return the n-th Fibonacci number (fib(0)=0, fib(1)=1).\"\"\"\n"),
        entry_point="fib",
        test=("def check(candidate):\n"
              "    assert candidate(0) == 0\n"
              "    assert candidate(1) == 1\n"
              "    assert candidate(5) == 5\n"
              "    assert candidate(10) == 55\n"),
        canonical_solution=("def fib(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n"),
    ),
    HumanEvalProblem(
        task_id="builtin/unique",
        prompt=("def unique(l):\n"
                "    \"\"\"Return the sorted list of unique elements in l.\"\"\"\n"),
        entry_point="unique",
        test=("def check(candidate):\n"
              "    assert candidate([]) == []\n"
              "    assert candidate([3, 1, 2, 2, 3]) == [1, 2, 3]\n"
              "    assert candidate([5, 5, 5]) == [5]\n"),
        canonical_solution=("def unique(l):\n    return sorted(set(l))\n"),
    ),
    HumanEvalProblem(
        task_id="builtin/max_element",
        prompt=("def max_element(l):\n"
                "    \"\"\"Return the maximum element of the non-empty list l.\"\"\"\n"),
        entry_point="max_element",
        test=("def check(candidate):\n"
              "    assert candidate([1, 2, 3]) == 3\n"
              "    assert candidate([-5, -2, -9]) == -2\n"
              "    assert candidate([7]) == 7\n"),
        canonical_solution=("def max_element(l):\n    return max(l)\n"),
    ),
]


def load_humaneval_subset(path: Optional[str] = None, limit: Optional[int] = None) -> list[HumanEvalProblem]:
    """从 JSONL(HumanEval 字段:task_id/prompt/entry_point/test/canonical_solution)加载;
    无 path 用内置子集。"""
    if path is None:
        problems = list(_BUILTIN)
    else:
        problems = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            problems.append(HumanEvalProblem(
                task_id=r["task_id"], prompt=r["prompt"], entry_point=r["entry_point"],
                test=r["test"], canonical_solution=r.get("canonical_solution", "")))
    return problems[:limit] if limit else problems


def to_eval_cases(problems: list[HumanEvalProblem]) -> list[tuple[str, dict]]:
    """映射成 eval_harness 的 (task, task_input)。"""
    cases = []
    for p in problems:
        task = ("Implement the Python function for the problem below. This is an atomic task: "
                "design a SINGLE-agent swarm — one agent that reads input field `prompt` and "
                "returns output field `code` containing the COMPLETE function source "
                "(signature + body).\n\n" + p.prompt)
        cases.append((task, {"prompt": p.prompt, "entry_point": p.entry_point}))
    return cases


def extract_code(output: dict, entry_point: str) -> Optional[str]:
    """从 swarm 输出里取目标函数源码:优先常见字段名,否则扫所有字符串值。"""
    if not isinstance(output, dict):
        return None
    needle = f"def {entry_point}"
    for k in ("code", "final_code", "solution", "implementation", "answer"):
        v = output.get(k)
        if isinstance(v, str) and needle in v:
            return v
    for v in output.values():
        if isinstance(v, str) and needle in v:
            return v
    return None


def make_oracle(problem: HumanEvalProblem, *, timeout_s: float = 5.0) -> Callable[[dict], bool]:
    """返回 oracle(output)->bool:抽取代码 → 沙箱跑题目单测 → 通过即 True。"""
    def oracle(output: dict) -> bool:
        code = extract_code(output, problem.entry_point)
        if not code:
            return False
        return run_code_tests(code, problem.test, entry_point=problem.entry_point,
                              timeout_s=timeout_s).passed
    return oracle


def oracle_for_cases(problems: list[HumanEvalProblem]) -> Callable[[str, dict], Optional[Callable[[dict], bool]]]:
    """给 eval_harness/ablation 的 oracle_for:按 task_input.entry_point 找对应题目的 oracle。"""
    by_entry = {p.entry_point: p for p in problems}

    def lookup(task: str, task_input: dict):
        p = by_entry.get((task_input or {}).get("entry_point"))
        return make_oracle(p) if p else None

    return lookup


@dataclass
class BenchmarkReport:
    """多次跑的 per-problem 可靠性 + 聚合 pass@1。区分 reliable / flaky / never,
    让"pass@1=X"这种结论可解释、可信(而非单次跑撞运气)。"""

    results: dict[str, list[bool]] = field(default_factory=dict)  # entry_point -> [ok per run]
    detail: dict[str, list[str]] = field(default_factory=dict)    # entry_point -> [失败 phase/type]

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.results.values())

    @property
    def passed(self) -> int:
        return sum(sum(v) for v in self.results.values())

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def per_problem(self) -> dict[str, float]:
        return {k: (sum(v) / len(v) if v else 0.0) for k, v in self.results.items()}

    def reliable(self) -> list[str]:  # 每次都过
        return [k for k, v in self.results.items() if v and all(v)]

    def flaky(self) -> list[str]:  # 时过时不过
        return [k for k, v in self.results.items() if 0 < sum(v) < len(v)]

    def never(self) -> list[str]:  # 从没过
        return [k for k, v in self.results.items() if v and not any(v)]

    def summary(self) -> dict:
        runs = len(next(iter(self.results.values()))) if self.results else 0
        return {
            "problems": len(self.results),
            "runs_per_problem": runs,
            "total": self.total, "passed": self.passed,
            "pass_rate": round(self.pass_rate, 4),
            "per_problem": {k: round(v, 4) for k, v in self.per_problem().items()},
            "reliable": sorted(self.reliable()),
            "flaky": sorted(self.flaky()),
            "never": sorted(self.never()),
        }


def run_benchmark(problems: list[HumanEvalProblem], build, *, runs: int = 1,
                  budget=None, on_result=None) -> BenchmarkReport:
    """每题跑 runs 次,oracle 计分,汇总 per-problem 可靠性。
    on_result(entry_point, run_idx, ok, outcome) 可选:用于流式打印进度(长跑防丢)。"""
    from .eval_harness import evaluate_case  # 延迟导入避免环

    report = BenchmarkReport()
    for p in problems:
        task, ti = to_eval_cases([p])[0]
        oracle = make_oracle(p)
        report.results[p.entry_point] = []
        report.detail[p.entry_point] = []
        for r in range(1, runs + 1):
            o = evaluate_case(task, ti, build, budget=budget, oracle=oracle)
            report.results[p.entry_point].append(o.ok)
            if not o.ok:
                report.detail[p.entry_point].append(f"{o.phase or ''}/{o.failure_type or ''}")
            if on_result:
                on_result(p.entry_point, r, o.ok, o)
    return report
