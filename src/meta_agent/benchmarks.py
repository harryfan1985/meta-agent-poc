"""HumanEval 风格 benchmark 加载 + 确定性正确性 oracle(§9)。

oracle 把 swarm 最终输出里的函数源码放进 code_sandbox 跑题目自带单测,通过才算成功——
确定性、不需模型判。可从 JSONL 加载真实 HumanEval,缺省用内置小子集(自洽,无需外部数据)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
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
        task = ("Implement the Python function for the problem below and return the COMPLETE "
                "function source (signature + body) in output field `code`.\n\n" + p.prompt)
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
