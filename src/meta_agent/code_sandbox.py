"""§7.1 执行不可信生成代码的子进程沙箱(benchmark 正确性 oracle 用)。

隔离手段:独立子进程(`python -I`,忽略环境/用户 site)+ CPU/内存上限(子进程内 setrlimit)
+ wall 超时(subprocess timeout)+ 干净命名空间 exec。任一越界即 kill,绝不挂起父进程。

网络:darwin 无 seccomp,出网阻断为 best-effort——不主动 import socket,且 HumanEval 这类纯函数
不需要网络;真实强隔离(sandbox-exec / 容器 / seccomp)留部署期。绝不在主进程内 exec 生成代码。
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

# 子进程内运行:设资源上限 → 拼 program(code + test + check 调用)→ 干净命名空间 exec。
_RUNNER = r"""
import json, sys
try:
    import resource
except Exception:
    resource = None

def _limit(cpu_s, mem_bytes):
    if resource is None:
        return
    for res, val in ((getattr(resource, "RLIMIT_CPU", None), cpu_s),
                     (getattr(resource, "RLIMIT_AS", None), mem_bytes)):
        if res is None:
            continue
        try:
            resource.setrlimit(res, (val, val))
        except Exception:
            pass

def main():
    p = json.load(sys.stdin)
    _limit(int(p["cpu_s"]), int(p["mem_bytes"]))
    entry = p.get("entry_point") or ""
    program = p["code"] + "\n" + p.get("test", "") + ("\ncheck(%s)\n" % entry if entry else "")
    ns = {}
    try:
        exec(compile(program, "<candidate>", "exec"), ns)
        sys.stdout.write(json.dumps({"passed": True, "error": ""}))
    except Exception as e:
        sys.stdout.write(json.dumps({"passed": False, "error": "%s: %s" % (type(e).__name__, e)}))

main()
"""


@dataclass
class CodeRunResult:
    passed: bool
    status: Literal["passed", "failed", "timeout", "error"]
    detail: str = ""


def run_code_tests(
    code: str,
    test: str,
    *,
    entry_point: str = "",
    timeout_s: float = 5.0,
    cpu_s: int = 2,
    mem_mb: int = 512,
) -> CodeRunResult:
    """在子进程沙箱里执行 code + test(+ check(entry_point)),返回是否通过。"""
    payload = json.dumps({
        "code": code, "test": test, "entry_point": entry_point,
        "cpu_s": cpu_s, "mem_bytes": mem_mb * 1024 * 1024,
    })
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _RUNNER],
            input=payload, text=True, capture_output=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return CodeRunResult(False, "timeout", f"wall timeout > {timeout_s}s")
    out = (proc.stdout or "").strip()
    if not out:
        # 无输出:多半被资源上限(SIGXCPU/内存)杀掉或解释器崩溃
        if proc.returncode and proc.returncode < 0:
            return CodeRunResult(False, "timeout", f"killed by signal {-proc.returncode}")
        return CodeRunResult(False, "error", (proc.stderr or "no output")[:500])
    try:
        result = json.loads(out)
    except json.JSONDecodeError:
        return CodeRunResult(False, "error", f"unparseable runner output: {out[:300]}")
    if result.get("passed"):
        return CodeRunResult(True, "passed")
    return CodeRunResult(False, "failed", result.get("error", "")[:500])
