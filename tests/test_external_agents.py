import pytest

from meta_agent.external_agents import CliCodeAgentAdapterBase
from meta_agent.fixtures.function_completion import build_swarm
from meta_agent.schemas import SurfaceFailure


class WritingAdapter(CliCodeAgentAdapterBase):
    def __init__(self, *args, write_path="allowed/out.txt", output=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.write_path = write_path
        self.output = output or {"result": "ok"}

    def run_agent(self, worktree, spec, message, history):
        target = worktree / self.write_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("changed", encoding="utf-8")
        return self.output


def _spec():
    return build_swarm().spec("spec_analyzer")


def test_cli_adapter_runs_in_ephemeral_copy_and_captures_diff(tmp_path):
    (tmp_path / "allowed").mkdir()
    (tmp_path / "allowed" / "out.txt").write_text("original", encoding="utf-8")

    adapter = WritingAdapter(tmp_path, allowed_paths=["allowed"])
    out = adapter.invoke(_spec(), {}, [])

    assert out == {"result": "ok"}
    assert adapter.last_changed_paths == ["allowed/out.txt"]
    assert (tmp_path / "allowed" / "out.txt").read_text(encoding="utf-8") == "original"
    assert adapter.last_worktree is not None
    assert not adapter.last_worktree.exists()


def test_cli_adapter_rejects_out_of_scope_diff(tmp_path):
    adapter = WritingAdapter(tmp_path, allowed_paths=["allowed"], write_path="secret/out.txt")
    with pytest.raises(SurfaceFailure) as ei:
        adapter.invoke(_spec(), {}, [])

    assert ei.value.gate_result.failure_type.value == "spec_adherence"
    assert any(f.subtype == "tool_misuse" for f in ei.value.gate_result.feedback)
    assert not (tmp_path / "secret" / "out.txt").exists()


def test_cli_adapter_rejects_oversized_output(tmp_path):
    adapter = WritingAdapter(
        tmp_path,
        allowed_paths=["allowed"],
        output={"result": "x" * 200},
        max_output_chars=20,
    )
    with pytest.raises(SurfaceFailure) as ei:
        adapter.invoke(_spec(), {}, [])

    assert any(f.subtype == "output_too_large" for f in ei.value.gate_result.feedback)


def test_cli_adapter_rejects_non_dict_output(tmp_path):
    adapter = WritingAdapter(tmp_path, allowed_paths=["allowed"], output=["not", "dict"])
    with pytest.raises(SurfaceFailure) as ei:
        adapter.invoke(_spec(), {}, [])

    assert any(f.subtype == "schema_violation" for f in ei.value.gate_result.feedback)
