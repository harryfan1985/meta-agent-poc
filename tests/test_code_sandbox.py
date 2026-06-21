"""§7.1 代码执行沙箱:正确/错误/超时/无代码。"""
from meta_agent.code_sandbox import run_code_tests

_TEST = ("def check(candidate):\n"
         "    assert candidate(2, 3) == 5\n"
         "    assert candidate(0, 0) == 0\n")


def test_correct_code_passes():
    r = run_code_tests("def add(a, b):\n    return a + b\n", _TEST, entry_point="add")
    assert r.passed is True and r.status == "passed"


def test_buggy_code_fails():
    r = run_code_tests("def add(a, b):\n    return a - b\n", _TEST, entry_point="add")
    assert r.passed is False and r.status == "failed"
    assert "AssertionError" in r.detail


def test_syntax_error_is_failed_not_crash():
    r = run_code_tests("def add(a, b):\n    return a +\n", _TEST, entry_point="add")
    assert r.passed is False  # 编译/语法错 → 不通过,但父进程不崩


def test_infinite_loop_is_killed():
    r = run_code_tests("def add(a, b):\n    while True:\n        pass\n", _TEST,
                       entry_point="add", timeout_s=4, cpu_s=1)
    assert r.passed is False and r.status == "timeout"


def test_runner_isolated_no_leak_to_parent():
    # 子进程里 exec,不污染父进程命名空间
    run_code_tests("def add(a, b):\n    return a + b\nimport os\n", _TEST, entry_point="add")
    assert "add" not in globals()
