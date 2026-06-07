import pytest

from meta_agent.artifacts import ArtifactLoader
from meta_agent.fixtures.function_completion import build_swarm
from meta_agent.schemas import AgentArtifact, SurfaceFailure


def test_artifact_preflight_missing_artifact_surfaces_contract_failure():
    swarm = build_swarm()
    del swarm.artifacts["code_verifier"]
    with pytest.raises(SurfaceFailure) as ei:
        ArtifactLoader().bind(swarm)
    assert ei.value.gate_result.failure_type.value == "contract"
    assert any("missing artifacts" in f.evidence for f in ei.value.gate_result.feedback)


def test_artifact_preflight_unknown_fixture_handler():
    swarm = build_swarm()
    swarm.artifacts["spec_analyzer"] = AgentArtifact(
        spec_id="spec_analyzer",
        implementation_kind="fixture",
        handler_ref="does_not_exist",
        passed=True,
    )
    with pytest.raises(SurfaceFailure) as ei:
        ArtifactLoader(fixture_registry={}).bind(swarm)
    assert any("unknown fixture" in f.evidence for f in ei.value.gate_result.feedback)


def test_artifact_preflight_missing_external_adapter():
    swarm = build_swarm()
    swarm.artifacts["spec_analyzer"] = AgentArtifact(
        spec_id="spec_analyzer",
        implementation_kind="external_agent",
        adapter_name="claude_code",
        passed=True,
    )
    with pytest.raises(SurfaceFailure) as ei:
        ArtifactLoader(fixture_registry={}).bind(swarm)
    assert any("unknown adapter_name" in f.evidence for f in ei.value.gate_result.feedback)
