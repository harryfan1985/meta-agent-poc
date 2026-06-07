"""ArtifactLoader(§4.1)— 把四形态 artifact 统一加载成 Callable[[dict,list],dict]。

Coordinator 只看 callable,不关心形态。外部 agent 的输出与本地产物一样必须过 gate。
"""
from __future__ import annotations

import importlib
from typing import Callable

from .schemas import AgentArtifact, AgentSpec, ExecutableSwarm


def _import_entrypoint(module_path: str, entrypoint: str) -> Callable:
    mod = importlib.import_module(module_path)
    return getattr(mod, entrypoint)


class ArtifactLoader:
    def __init__(self, fixture_registry: dict[str, Callable] | None = None, adapters: dict | None = None):
        self.fixture_registry = fixture_registry or {}
        self.adapters = adapters or {}

    def load(self, artifact: AgentArtifact, spec: AgentSpec) -> Callable:
        kind = artifact.implementation_kind
        if kind == "fixture":
            return self.fixture_registry[artifact.handler_ref]
        if kind == "python_module":
            return _import_entrypoint(artifact.module_path, artifact.entrypoint)
        if kind == "prompt_template":
            raise NotImplementedError("prompt_template backend: M2")
        if kind == "external_agent":
            adapter = self.adapters[artifact.adapter_name]
            return lambda message, history: adapter.invoke(spec, message, history)
        raise ValueError(f"unknown implementation_kind: {kind}")

    def bind(self, swarm: ExecutableSwarm) -> ExecutableSwarm:
        """加载所有 artifact 填进 swarm._loaded,返回同一 swarm。"""
        for spec_id, artifact in swarm.artifacts.items():
            swarm._loaded[spec_id] = self.load(artifact, swarm.spec(spec_id))
        return swarm
