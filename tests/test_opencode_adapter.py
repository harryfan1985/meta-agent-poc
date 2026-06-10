"""OpenCodeAdapter 单测:全程注入 fake runner,不需要真实 opencode/creds。
真实 `opencode run` 实跑属 [eval],见 tests/eval/。"""
import json

import pytest

from meta_agent.artifacts import ArtifactLoader
from meta_agent.coordinator import execute
from meta_agent.external_agents import OpenCodeAdapter
from meta_agent.fixtures.function_completion import (
    CANDIDATE_CODE,
    TASK_INPUT_EXAMPLE,
    build_plan,
)
from meta_agent.schemas import (
    AgentArtifact,
    AgentSpec,
    ExecutableSwarm,
    FieldSpec,
    IOContract,
    SurfaceFailure,
)


def _spec():
    return AgentSpec(
        spec_id="t",
        role="echo",
        io_contract=IOContract(
            input_schema={"x": FieldSpec(type="string", description="x")},
            output_schema={"answer": FieldSpec(type="integer", description="a")},
            required_out=["answer"],
        ),
    )


def _runner_returning(stdout, exit_code=0, stderr=""):
    def runner(argv, cwd, timeout):
        return exit_code, stdout, stderr
    return runner


def _text_event(text):
    return json.dumps({"type": "text", "text": text})


# ---- build_command / render_prompt ----

def test_build_command_with_and_without_model():
    a = OpenCodeAdapter(model="opencode/qwen3.6-plus-free", runner=_runner_returning(""))
    argv = a.build_command("/tmp/wt", "hello")
    assert argv[:6] == ["opencode", "run", "--dir", "/tmp/wt", "--format", "json"]
    assert "-m" in argv and "opencode/qwen3.6-plus-free" in argv
    assert argv[-1] == "hello"

    a2 = OpenCodeAdapter(runner=_runner_returning(""))
    assert "-m" not in a2.build_command("/tmp/wt", "hi")


def test_render_prompt_includes_contract_and_feedback():
    a = OpenCodeAdapter(runner=_runner_returning(""))
    p = a.render_prompt(_spec(), {"x": "v"}, history=[{"subtype": "schema_violation", "evidence": "缺 answer"}])
    assert "answer(integer)" in p and "必填" in p
    assert "缺 answer" in p  # 重试反馈进 prompt


# ---- parse_output ----

def test_parse_bare_json_in_text_event():
    a = OpenCodeAdapter(runner=_runner_returning(""))
    out = a.parse_output(_text_event('{"answer": 42}'), _spec())
    assert out == {"answer": 42}


def test_parse_fenced_json():
    a = OpenCodeAdapter(runner=_runner_returning(""))
    out = a.parse_output(_text_event('结果如下:\n```json\n{"answer": 7}\n```'), _spec())
    assert out == {"answer": 7}


def test_parse_picks_last_brace_block():
    a = OpenCodeAdapter(runner=_runner_returning(""))
    out = a.parse_output(_text_event('草稿 {"answer": 1} 最终 {"answer": 2}'), _spec())
    assert out == {"answer": 2}


def test_parse_error_event_surfaces():
    a = OpenCodeAdapter(runner=_runner_returning(""))
    ev = json.dumps({"type": "error", "error": {"data": {"message": "boom"}}})
    with pytest.raises(SurfaceFailure) as ei:
        a.parse_output(ev, _spec())
    assert "boom" in ei.value.gate_result.feedback[0].evidence


def test_parse_no_json_surfaces_schema_violation():
    a = OpenCodeAdapter(runner=_runner_returning(""))
    with pytest.raises(SurfaceFailure) as ei:
        a.parse_output("I cannot help with that.", _spec())
    assert ei.value.gate_result.feedback[0].subtype == "schema_violation"


# ---- run_agent / invoke (full path with worktree) ----

def test_invoke_exit_nonzero_surfaces():
    a = OpenCodeAdapter(runner=_runner_returning("", exit_code=1, stderr="auth error"), workspace_root="")
    with pytest.raises(SurfaceFailure) as ei:
        a.invoke(_spec(), {"x": "v"}, [])
    assert "exit=1" in ei.value.gate_result.feedback[0].evidence


def test_invoke_returns_dict_and_cleans_worktree():
    a = OpenCodeAdapter(runner=_runner_returning(_text_event('{"answer": 99}')), workspace_root="")
    out = a.invoke(_spec(), {"x": "v"}, [])
    assert out == {"answer": 99}
    assert a.last_worktree is not None and not a.last_worktree.exists()  # 已清理


def test_invoke_emits_control_plane_audit_event():
    from meta_agent.trace import ListTracer

    tr = ListTracer()
    a = OpenCodeAdapter(
        runner=_runner_returning(_text_event('{"answer": 1}')), workspace_root="", tracer=tr
    )
    a.invoke(_spec(), {"x": "v"}, [])
    audits = [e for e in tr.events if e.event == "llm_call" and e.stage == "external_agent"]
    assert len(audits) == 1
    p = audits[0].payload
    assert p["adapter"] == "OpenCodeAdapter"
    assert p["exit_code"] == 0  # 真实退出码回写
    assert p["argv"][:2] == ["opencode", "run"]
    assert p["changed_paths"] == []  # canned runner 未改文件


# ---- control-plane 端到端:external_agent 经 RuntimeGate ----

def _canned_adapter(obj):
    return OpenCodeAdapter(runner=_runner_returning(_text_event(json.dumps(obj))), workspace_root="")


def test_external_agent_end_to_end_through_gate():
    """附录 A swarm 全部节点改用 external_agent(opencode adapter,注入 canned runner),
    输出与本地产物一样必须过 RuntimeGate,端到端 PASS。验证 control-plane 路径。"""
    plan = build_plan()
    outputs = {
        "spec_analyzer": {
            "raw_signature": TASK_INPUT_EXAMPLE["raw_signature"],
            "parsed_spec": {"inequality_strict": True, "edge_cases": []},
        },
        "algo_planner": {"approach": {"algorithm": "pairwise_compare"}},
        "code_synthesizer": {"candidate_code": CANDIDATE_CODE},
        "code_verifier": {"final_code": CANDIDATE_CODE, "passed": True},
    }
    artifacts = {
        sid: AgentArtifact(spec_id=sid, implementation_kind="external_agent", adapter_name=sid, passed=True)
        for sid in outputs
    }
    adapters = {sid: _canned_adapter(obj) for sid, obj in outputs.items()}
    swarm = ExecutableSwarm(plan=plan, artifacts=artifacts)
    ArtifactLoader(adapters=adapters).bind(swarm)

    out = execute(swarm, dict(TASK_INPUT_EXAMPLE))
    assert out["passed"] is True
    assert "def has_close_elements" in out["final_code"]


def test_external_agent_bad_output_caught_by_gate():
    """external_agent 产出违反契约(analyzer 缺 inequality_strict)→ 被 gate 拦下,
    印证'外部 agent 不能自宣成功,产物必过 gate'。"""
    plan = build_plan()
    bad = {
        "spec_analyzer": {"raw_signature": TASK_INPUT_EXAMPLE["raw_signature"], "parsed_spec": {}},
        "algo_planner": {"approach": {"algorithm": "x"}},
        "code_synthesizer": {"candidate_code": CANDIDATE_CODE},
        "code_verifier": {"final_code": CANDIDATE_CODE, "passed": True},
    }
    artifacts = {
        sid: AgentArtifact(spec_id=sid, implementation_kind="external_agent", adapter_name=sid, passed=True)
        for sid in bad
    }
    adapters = {sid: _canned_adapter(obj) for sid, obj in bad.items()}
    swarm = ExecutableSwarm(plan=plan, artifacts=artifacts)
    ArtifactLoader(adapters=adapters).bind(swarm)

    with pytest.raises(SurfaceFailure) as ei:
        execute(swarm, dict(TASK_INPUT_EXAMPLE))
    assert ei.value.spec_id == "spec_analyzer"
