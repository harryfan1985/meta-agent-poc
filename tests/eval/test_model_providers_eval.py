"""Real provider smoke tests. Skipped by default; run manually for M2 eval."""
import os

import pytest

from meta_agent.artifacts import ArtifactLoader
from meta_agent.construct import construct
from meta_agent.coordinator import execute
from meta_agent.llm import create_structured_llm
from meta_agent.stages import default_stages

pytestmark = pytest.mark.eval

SMOKE_SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "additionalProperties": False,
    "properties": {"answer": {"type": "integer"}},
}


def _provider_backend(provider: str, model_env: str, api_key_env: str):
    if os.getenv("META_AGENT_RUN_PROVIDER_EVAL") != "1":
        pytest.skip("set META_AGENT_RUN_PROVIDER_EVAL=1 to run real provider smoke tests")
    model = os.getenv(model_env)
    if not (os.getenv(api_key_env) and model):
        pytest.skip(f"set {api_key_env} and {model_env} to run this provider smoke test")
    return create_structured_llm(provider, model)


def test_anthropic_schema_generation_smoke():
    backend = _provider_backend("anthropic", "META_AGENT_ANTHROPIC_MODEL", "ANTHROPIC_API_KEY")
    out = backend.generate("Return the integer 42.", {"request": "answer"}, SMOKE_SCHEMA)
    assert out == {"answer": 42}


def test_openai_schema_generation_smoke():
    backend = _provider_backend("openai", "META_AGENT_OPENAI_MODEL", "OPENAI_API_KEY")
    out = backend.generate("Return the integer 42.", {"request": "answer"}, SMOKE_SCHEMA)
    assert out == {"answer": 42}


def test_m2_construct_execute_smoke():
    if os.getenv("META_AGENT_RUN_M2_E2E") != "1":
        pytest.skip("set META_AGENT_RUN_M2_E2E=1 to run real-model construct+execute smoke")
    backend = _provider_backend("anthropic", "META_AGENT_ANTHROPIC_MODEL", "ANTHROPIC_API_KEY")
    loader = ArtifactLoader(structured_llm=backend)
    stages = default_stages(backend, loader)

    task = (
        "Build a minimal swarm that reads input field `task` and returns output field "
        "`answer` as a concise string summary of the task."
    )
    swarm = construct(task, stages)
    loader.bind(swarm)
    out = execute(swarm, {"task": "say hello"})
    assert isinstance(out, dict)
