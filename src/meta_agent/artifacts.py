"""ArtifactLoader(§4.1)— 把四形态 artifact 统一加载成 Callable[[dict,list],dict]。

Coordinator 只看 callable,不关心形态。外部 agent 的输出与本地产物一样必须过 gate。
"""
from __future__ import annotations

import importlib
from typing import Callable

from .schemas import AgentArtifact, AgentSpec, ExecutableSwarm
from .validation import surface_contract_issues


def _import_entrypoint(module_path: str, entrypoint: str) -> Callable:
    mod = importlib.import_module(module_path)
    return getattr(mod, entrypoint)


class ArtifactLoader:
    def __init__(
        self,
        fixture_registry: dict[str, Callable] | None = None,
        adapters: dict | None = None,
        structured_llm=None,
    ):
        self.fixture_registry = fixture_registry or {}
        self.adapters = adapters or {}
        self.structured_llm = structured_llm  # prompt_template 后端(StructuredLLM)

    def load(self, artifact: AgentArtifact, spec: AgentSpec) -> Callable:
        kind = artifact.implementation_kind
        if kind == "fixture":
            return self.fixture_registry[artifact.handler_ref]
        if kind == "python_module":
            return _import_entrypoint(artifact.module_path, artifact.entrypoint)
        if kind == "prompt_template":
            if self.structured_llm is None:
                raise ValueError("prompt_template artifact requires a structured_llm backend")
            from .llm import TemplateAgent
            agent = TemplateAgent(
                artifact.prompt_template or "",
                spec.io_contract.out_jsonschema(),
                self.structured_llm,
            )
            return agent.run
        if kind == "external_agent":
            adapter = self.adapters[artifact.adapter_name]
            return lambda message, history: adapter.invoke(spec, message, history)
        raise ValueError(f"unknown implementation_kind: {kind}")

    def validate(self, swarm: ExecutableSwarm) -> list[str]:
        """执行前校验 artifact 覆盖与后端引用,避免运行期裸 KeyError。"""
        issues: list[str] = []
        spec_ids = set(swarm.spec_ids)
        artifact_ids = set(swarm.artifacts)
        missing = sorted(spec_ids - artifact_ids)
        extra = sorted(artifact_ids - spec_ids)
        if missing:
            issues.append(f"missing artifacts for specs: {missing}")
        if extra:
            issues.append(f"artifacts reference unknown specs: {extra}")

        for spec_id, artifact in swarm.artifacts.items():
            if spec_id != artifact.spec_id:
                issues.append(f"{spec_id}: artifact.spec_id mismatch: {artifact.spec_id}")
            kind = artifact.implementation_kind
            if kind == "fixture":
                if not artifact.handler_ref:
                    issues.append(f"{spec_id}: fixture artifact missing handler_ref")
                elif artifact.handler_ref not in self.fixture_registry:
                    issues.append(f"{spec_id}: unknown fixture handler_ref {artifact.handler_ref!r}")
            elif kind == "python_module":
                if not artifact.module_path:
                    issues.append(f"{spec_id}: python_module artifact missing module_path")
            elif kind == "prompt_template":
                if not artifact.prompt_template:
                    issues.append(f"{spec_id}: prompt_template artifact missing prompt_template")
                elif self.structured_llm is None:
                    issues.append(f"{spec_id}: prompt_template artifact requires structured_llm backend")
            elif kind == "external_agent":
                if not artifact.adapter_name:
                    issues.append(f"{spec_id}: external_agent artifact missing adapter_name")
                elif artifact.adapter_name not in self.adapters:
                    issues.append(f"{spec_id}: unknown adapter_name {artifact.adapter_name!r}")
        return issues

    def bind(self, swarm: ExecutableSwarm) -> ExecutableSwarm:
        """加载所有 artifact 填进 swarm._loaded,返回同一 swarm。"""
        issues = self.validate(swarm)
        if issues:
            raise surface_contract_issues("artifact preflight failed", issues)
        for spec_id, artifact in swarm.artifacts.items():
            try:
                swarm._loaded[spec_id] = self.load(artifact, swarm.spec(spec_id))
            except Exception as e:  # noqa: BLE001
                raise surface_contract_issues(
                    "artifact load failed",
                    [f"{spec_id}: {type(e).__name__}: {e}"],
                ) from e
        return swarm
