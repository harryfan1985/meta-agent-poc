"""Shared base for external code-agent adapters.

`CliCodeAgentAdapterBase` provides the provider-neutral safety shell for
ClaudeCodeAdapter/OpenCodeAdapter style backends: run in an ephemeral workspace
copy, capture changed paths, enforce an allowlist, and surface typed failures.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .schemas import (
    AgentSpec,
    FailureSubtype,
    FailureType,
    GateResult,
    StructuredFeedback,
    SurfaceFailure,
)


_IGNORE_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache"}


class CliCodeAgentAdapterBase:
    def __init__(
        self,
        workspace_root: str | Path,
        allowed_paths: list[str] | None = None,
        keep_worktree: bool = False,
        max_output_chars: int = 100_000,
    ):
        self.workspace_root = Path(workspace_root)
        self.allowed_paths = [p.strip("/") for p in (allowed_paths or [])]
        self.keep_worktree = keep_worktree
        self.max_output_chars = max_output_chars
        self.last_changed_paths: list[str] = []
        self.last_worktree: Path | None = None

    def invoke(self, spec: AgentSpec, message: dict, history: list) -> dict:
        worktree = self.create_ephemeral_worktree()
        self.last_worktree = worktree
        before = self._snapshot(worktree)
        try:
            output = self.run_agent(worktree, spec, message, history)
            if not isinstance(output, dict):
                raise self._surface(
                    "external agent returned non-dict output",
                    FailureSubtype.SCHEMA_VIOLATION.value,
                    f"got {type(output).__name__}",
                    "external agent must return dict matching AgentSpec.io_contract.output_schema",
                )

            size = len(json.dumps(output, ensure_ascii=False, default=str))
            if size > self.max_output_chars:
                raise self._surface(
                    "external agent output too large",
                    FailureSubtype.OUTPUT_TOO_LARGE.value,
                    f"output size {size} > {self.max_output_chars}",
                    f"≤ {self.max_output_chars} chars",
                )

            self.last_changed_paths = self.capture_changed_paths(worktree, before)
            violations = [p for p in self.last_changed_paths if not self._path_allowed(p)]
            if violations:
                raise self._surface(
                    "external agent modified paths outside allowed scope",
                    FailureSubtype.TOOL_MISUSE.value,
                    f"out-of-scope paths: {violations}",
                    f"only modify paths under {self.allowed_paths}",
                )
            return output
        finally:
            if not self.keep_worktree:
                shutil.rmtree(worktree, ignore_errors=True)

    def run_agent(self, worktree: Path, spec: AgentSpec, message: dict, history: list) -> dict:
        """Provider-specific adapter implementation hook."""
        raise NotImplementedError

    def create_ephemeral_worktree(self) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="meta-agent-ext-"))
        if self.workspace_root.exists():
            shutil.copytree(
                self.workspace_root,
                tmp,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(*_IGNORE_DIRS),
            )
        return tmp

    def capture_changed_paths(self, worktree: Path, before: dict[str, str]) -> list[str]:
        after = self._snapshot(worktree)
        paths = set(before) | set(after)
        return sorted(p for p in paths if before.get(p) != after.get(p))

    def _snapshot(self, root: Path) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        if not root.exists():
            return snapshot
        for path in root.rglob("*"):
            if any(part in _IGNORE_DIRS for part in path.relative_to(root).parts):
                continue
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                snapshot[rel] = self._hash_file(path)
        return snapshot

    def _hash_file(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _path_allowed(self, rel_path: str) -> bool:
        if not self.allowed_paths:
            return True
        clean = rel_path.strip("/")
        return any(clean == allowed or clean.startswith(f"{allowed}/") for allowed in self.allowed_paths)

    def _surface(self, reason: str, subtype: str, evidence: str, expected: str) -> SurfaceFailure:
        return SurfaceFailure(
            reason,
            gate_result=GateResult(
                ok=False,
                failure_type=FailureType.SPEC_ADHERENCE,
                feedback=[
                    StructuredFeedback(
                        subtype=subtype,
                        evidence=evidence,
                        expected=expected,
                        actionable_fix="修正 external_agent adapter、allowed_paths 或输出契约",
                    )
                ],
            ),
        )
