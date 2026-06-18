"""M3 变异测试:算子 + kill/survive 报告(确定性,机判)。"""
import json

from meta_agent.fixtures.function_completion import CANDIDATE_CODE, build_plan
from meta_agent.mutations import apply_mutation, run_mutation_testing
from meta_agent.schemas import MutationCase

BASE = {"final_code": CANDIDATE_CODE, "passed": True}


def _cv_spec():
    return next(s for s in build_plan().specs if s.spec_id == "code_verifier")


def test_apply_mutation_operators():
    drop = apply_mutation(BASE, MutationCase(mutation_id="d", mutation_type="drop_field", target_path="/final_code"))
    assert "final_code" not in drop and drop["passed"] is True  # 不改原 output 的其它字段
    wrong = apply_mutation(BASE, MutationCase(mutation_id="w", mutation_type="wrong_value",
                                              target_path="/final_code", payload="x = 1"))
    assert wrong["final_code"] == "x = 1"
    inj = apply_mutation(BASE, MutationCase(mutation_id="f", mutation_type="forbidden_insert",
                                            target_path="/final_code", payload="import os"))
    assert "import os" in inj["final_code"]
    flip = apply_mutation(BASE, MutationCase(mutation_id="o", mutation_type="off_by_one", target_path="/passed"))
    assert flip["passed"] is False
    assert BASE["passed"] is True  # 原 output 未被破坏


def test_apply_mutation_is_pure():
    apply_mutation(BASE, MutationCase(mutation_id="d", mutation_type="drop_field", target_path="/final_code"))
    assert "final_code" in BASE  # 原对象不受影响


def test_mutation_report_kills_and_exposes_survivor():
    muts = [
        MutationCase(mutation_id="drop_code", mutation_type="drop_field", target_path="/final_code"),
        MutationCase(mutation_id="break_code", mutation_type="wrong_value", target_path="/final_code", payload="x = 1"),
        MutationCase(mutation_id="inject_forbidden", mutation_type="forbidden_insert",
                     target_path="/final_code", payload="import os"),
        MutationCase(mutation_id="flip_passed", mutation_type="off_by_one", target_path="/passed"),
    ]
    report = run_mutation_testing(_cv_spec(), BASE, muts)
    assert report.base_ok is True
    assert report.killed == 3
    assert report.survived == ["flip_passed"]  # passed 无断言 → 存活,暴露验证器盲区
    assert report.kill_rate == 0.75
    # 被杀变异带出命中的 subtype(可溯源)
    killed = {r.mutation_id: r.failure_subtypes for r in report.results if r.killed}
    assert any("forbidden_hit" in s for s in killed["inject_forbidden"])


def test_mutation_testing_requires_valid_base():
    report = run_mutation_testing(
        _cv_spec(), {"passed": True},  # 缺 final_code → base 本就不过
        [MutationCase(mutation_id="x", mutation_type="drop_field", target_path="/passed")])
    assert report.base_ok is False and report.total == 0


def test_summary_is_json_serializable():
    report = run_mutation_testing(_cv_spec(), BASE, [
        MutationCase(mutation_id="drop_code", mutation_type="drop_field", target_path="/final_code")])
    json.dumps(report.summary())
