import argparse
import json

import pytest

from meta_agent import model_cli
from meta_agent.trace import make_event


def test_json_arg_requires_object():
    assert model_cli._json_arg('{"x": 1}') == {"x": 1}
    with pytest.raises(argparse.ArgumentTypeError):
        model_cli._json_arg("[1, 2]")
    with pytest.raises(argparse.ArgumentTypeError):
        model_cli._json_arg("{bad")


def test_model_cli_runs_construct_execute_smoke(monkeypatch, tmp_path, capsys):
    calls = {}

    class FakeLoader:
        def __init__(self, structured_llm):
            calls["loader_backend"] = structured_llm

        def bind(self, swarm):
            calls["bound_swarm"] = swarm
            return swarm

    def fake_create(provider, model, **kwargs):
        calls["provider"] = provider
        calls["model"] = model
        calls["kwargs"] = kwargs
        return "backend"

    def fake_default_stages(backend, loader):
        calls["stages_backend"] = backend
        calls["stages_loader"] = loader
        return "stages"

    def fake_construct(task, stages, tracer):
        calls["task"] = task
        calls["stages"] = stages
        tracer.emit(make_event("start", phase="construct"))
        return "swarm"

    def fake_execute(swarm, task_input, tracer):
        calls["execute"] = (swarm, task_input)
        tracer.emit(make_event("finish", payload={"ok": True}))
        return {"answer": "ok"}

    monkeypatch.setattr(model_cli, "ArtifactLoader", FakeLoader)
    monkeypatch.setattr(model_cli, "create_structured_llm", fake_create)
    monkeypatch.setattr(model_cli, "default_stages", fake_default_stages)
    monkeypatch.setattr(model_cli, "construct", fake_construct)
    monkeypatch.setattr(model_cli, "execute", fake_execute)

    trace_path = tmp_path / "trace.jsonl"
    rc = model_cli.main([
        "--provider", "anthropic",
        "--model", "claude-test",
        "--task", "do work",
        "--task-input-json", '{"x": "v"}',
        "--trace-jsonl", str(trace_path),
    ])

    assert rc == 0
    assert calls["provider"] == "anthropic"
    assert calls["model"] == "claude-test"
    assert calls["execute"] == ("swarm", {"x": "v"})
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"output": {"answer": "ok"}, "trace_jsonl": str(trace_path)}
    assert len(trace_path.read_text(encoding="utf-8").splitlines()) == 2
