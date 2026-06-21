"""HumanEval 加载 + 正确性 oracle(确定性,沙箱跑自带单测)。"""
from meta_agent.artifacts import ArtifactLoader
from meta_agent.benchmarks import (
    extract_code,
    load_humaneval_subset,
    make_oracle,
    oracle_for_cases,
    run_benchmark,
    to_eval_cases,
)
from meta_agent.construct import Stages
from meta_agent.eval_harness import run_eval
from meta_agent.schemas import (
    AgentArtifact,
    AgentSpec,
    FieldSpec,
    GateResult,
    IOContract,
    ParsedIntent,
    SwarmPlan,
)


def test_load_builtin_subset():
    problems = load_humaneval_subset()
    assert len(problems) >= 3
    p = problems[0]
    assert p.entry_point and p.test and p.canonical_solution


def test_load_subset_limit():
    assert len(load_humaneval_subset(limit=2)) == 2


def test_to_eval_cases_shape():
    cases = to_eval_cases(load_humaneval_subset(limit=1))
    task, task_input = cases[0]
    assert "output field `code`" in task
    assert "prompt" in task_input and "entry_point" in task_input


def test_extract_code_prefers_known_fields_then_scans():
    assert extract_code({"code": "def f(): pass"}, "f") == "def f(): pass"
    assert extract_code({"misc": "def f(): pass"}, "f") == "def f(): pass"  # 扫值
    assert extract_code({"code": "no function here"}, "f") is None
    assert extract_code("not a dict", "f") is None


def test_oracle_accepts_canonical_rejects_buggy():
    p = load_humaneval_subset()[1]  # sum_list
    oracle = make_oracle(p)
    assert oracle({"code": p.canonical_solution}) is True
    assert oracle({"code": "def sum_list(xs):\n    return 999\n"}) is False
    assert oracle({"answer": "I cannot help"}) is False  # 无可用代码


def test_oracle_for_cases_routes_by_entry_point():
    problems = load_humaneval_subset(limit=2)
    lookup = oracle_for_cases(problems)
    oracle = lookup("task", {"entry_point": problems[0].entry_point})
    assert oracle is not None
    assert oracle({"code": problems[0].canonical_solution}) is True
    assert lookup("task", {"entry_point": "unknown_fn"}) is None


def _code_build(code):
    """单节点 fixture swarm,直接产出 {"code": code};construct 用 passthrough verify。"""
    def build(task_input):
        spec = AgentSpec(
            spec_id="coder", role="write code",
            io_contract=IOContract(
                input_schema={"prompt": FieldSpec(type="string", description="p")},
                output_schema={"code": FieldSpec(type="string", description="c")},
                required_in=["prompt"], required_out=["code"]))
        loader = ArtifactLoader(fixture_registry={"fx_code": lambda m, h: {"code": code}})
        stages = Stages(
            parse=lambda t: ParsedIntent(goal=t),
            plan=lambda pi: SwarmPlan(swarm_name="s", specs=[spec], dag_edges=[]),
            ground=lambda p: p,
            codegen=lambda sp, pl, fb: AgentArtifact(
                spec_id="coder", implementation_kind="fixture", handler_ref="fx_code", passed=True),
            verify=lambda a, s: GateResult(ok=True),
        )
        return stages, loader
    return build


def test_eval_harness_oracle_scores_correctness_end_to_end():
    p = load_humaneval_subset(limit=1)[0]  # has_close_elements
    cases = to_eval_cases([p])
    oracle_for = oracle_for_cases([p])

    ok = run_eval(cases, _code_build(p.canonical_solution), oracle_for=oracle_for)
    assert ok.passed == 1 and ok.pass_rate == 1.0

    wrong = run_eval(cases, _code_build("def has_close_elements(numbers, threshold):\n    return True\n"),
                     oracle_for=oracle_for)
    assert wrong.passed == 0
    assert wrong.outcomes[0].phase == "oracle" and wrong.outcomes[0].failure_type == "incorrect"


def test_all_builtin_canonicals_pass_their_tests():
    """credibility 前提:oracle 对每道题的参考解都判过(否则计分本身不可信)。"""
    for p in load_humaneval_subset():
        assert make_oracle(p)({"code": p.canonical_solution}) is True, p.entry_point


def _build_returning(code_for):
    """code_for(entry_point)->源码;单节点 fixture swarm 直接产出。"""
    def build(ti):
        spec = AgentSpec(
            spec_id="coder", role="write code",
            io_contract=IOContract(
                input_schema={"prompt": FieldSpec(type="string", description="p")},
                output_schema={"code": FieldSpec(type="string", description="c")},
                required_in=["prompt"], required_out=["code"]))
        code = code_for(ti["entry_point"])
        loader = ArtifactLoader(fixture_registry={"fx": lambda m, h: {"code": code}})
        stages = Stages(
            parse=lambda t: ParsedIntent(goal=t),
            plan=lambda pi: SwarmPlan(swarm_name="s", specs=[spec], dag_edges=[]),
            ground=lambda p: p,
            codegen=lambda sp, pl, fb: AgentArtifact(
                spec_id="coder", implementation_kind="fixture", handler_ref="fx", passed=True),
            verify=lambda a, s: GateResult(ok=True))
        return stages, loader
    return build


def test_run_benchmark_classifies_reliable_never_flaky():
    probs = load_humaneval_subset(limit=3)
    canon = {p.entry_point: p.canonical_solution for p in probs}

    # 全对 → reliable;runs=2 都过
    rep = run_benchmark(probs, _build_returning(lambda ep: canon[ep]), runs=2)
    assert rep.pass_rate == 1.0
    assert sorted(rep.reliable()) == sorted(canon) and not rep.flaky() and not rep.never()
    assert rep.summary()["runs_per_problem"] == 2

    # 全错(返回不含目标函数的占位)→ never
    bad = run_benchmark(probs, _build_returning(lambda ep: "def other(): pass"), runs=2)
    assert bad.passed == 0 and sorted(bad.never()) == sorted(canon)


def test_run_benchmark_detects_flaky():
    probs = load_humaneval_subset(limit=1)
    ep = probs[0].entry_point
    canon = probs[0].canonical_solution
    state = {"n": 0}

    def code_for(_ep):  # 交替:对、错、对、错
        state["n"] += 1
        return canon if state["n"] % 2 == 1 else "def other(): pass"

    rep = run_benchmark(probs, _build_returning(code_for), runs=2)
    assert rep.flaky() == [ep]  # 一过一不过
    assert rep.per_problem()[ep] == 0.5
