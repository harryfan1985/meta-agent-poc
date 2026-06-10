"""Shared base for external code-agent adapters.

`CliCodeAgentAdapterBase` provides the provider-neutral safety shell for
ClaudeCodeAdapter/OpenCodeAdapter style backends: run in an ephemeral workspace
copy, capture changed paths, enforce an allowlist, and surface typed failures.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from .schemas import (
    AgentSpec,
    FailureSubtype,
    FailureType,
    GateResult,
    StructuredFeedback,
    SurfaceFailure,
)
from .trace import NullTracer, make_event


_IGNORE_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache"}


class CliCodeAgentAdapterBase:
    def __init__(
        self,
        workspace_root: str | Path,
        allowed_paths: list[str] | None = None,
        keep_worktree: bool = False,
        max_output_chars: int = 100_000,
        tracer=None,
    ):
        self.workspace_root = Path(workspace_root)
        self.allowed_paths = [p.strip("/") for p in (allowed_paths or [])]
        self.keep_worktree = keep_worktree
        self.max_output_chars = max_output_chars
        self.tracer = tracer or NullTracer()  # 默认零开销;传入同一 tracer 可与执行期 trace 合流
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
            self._emit_audit(spec, worktree, size)
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

    def _emit_audit(self, spec: AgentSpec, worktree: Path, output_size: int) -> None:
        """control-plane 审计事件:外部 agent 在隔离 worktree 里改了什么、用了什么命令。"""
        self.tracer.emit(make_event(
            "llm_call",
            stage="external_agent",
            spec_id=spec.spec_id,
            payload={
                "adapter": type(self).__name__,
                "worktree": str(worktree),
                "changed_paths": self.last_changed_paths,
                "output_size": output_size,
                "argv": getattr(self, "last_argv", None),
                "exit_code": getattr(self, "last_exit_code", None),
            },
        ))

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


# (argv, cwd, timeout_seconds) -> (exit_code, stdout, stderr)
ProcRunner = Callable[[list, str, float], tuple]


def _default_runner(argv: list, cwd: str, timeout: float) -> tuple:
    proc = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )
    return proc.returncode, proc.stdout, proc.stderr


class OpenCodeAdapter(CliCodeAgentAdapterBase):
    """control-plane 执行后端:把某节点委派给 opencode CLI(`opencode run`)。

    产物默认 untrusted,经基类(worktree/diff/越权拦截/超限)后,**仍必须过 RuntimeGate**。
    `runner` 可注入,使命令构建/事件解析/失败映射等确定性逻辑无需真实 opencode 即可单测;
    真实 `opencode run` 的实跑属 [eval](见 tests/eval)。
    """

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        runner: Optional[ProcRunner] = None,
        timeout: float = 180.0,
        workspace_root: str | Path = "",
        allowed_paths: list[str] | None = None,
        keep_worktree: bool = False,
        max_output_chars: int = 100_000,
        tracer=None,
    ):
        super().__init__(
            workspace_root=workspace_root,
            allowed_paths=allowed_paths,
            keep_worktree=keep_worktree,
            max_output_chars=max_output_chars,
            tracer=tracer,
        )
        self.model = model
        self.runner = runner or _default_runner
        self.timeout = timeout
        self.last_argv: list[str] = []
        self.last_exit_code: Optional[int] = None

    # ---- provider hook ----
    def run_agent(self, worktree: Path, spec: AgentSpec, message: dict, history: list) -> dict:
        prompt = self.render_prompt(spec, message, history)
        argv = self.build_command(worktree, prompt)
        self.last_argv = argv
        try:
            exit_code, stdout, stderr = self.runner(argv, str(worktree), self.timeout)
        except subprocess.TimeoutExpired as e:  # pragma: no cover - 真实进程超时
            raise self._surface(
                "opencode run timed out", FailureSubtype.TIMEOUT.value,
                f">{self.timeout}s", "opencode run 应在预算内完成",
            ) from e
        self.last_exit_code = exit_code
        if exit_code != 0:
            raise self._surface(
                "opencode run exited non-zero", FailureSubtype.TOOL_MISUSE.value,
                f"exit={exit_code}: {(stderr or '')[:500]}", "opencode run 应成功退出",
            )
        return self.parse_output(stdout, spec)

    # ---- provider-specific bits (可被子类/测试覆盖)----
    def build_command(self, worktree: Path, prompt: str) -> list[str]:
        argv = ["opencode", "run", "--dir", str(worktree), "--format", "json"]
        if self.model:
            argv += ["-m", self.model]
        argv.append(prompt)
        return argv

    def render_prompt(self, spec: AgentSpec, message: dict, history: list) -> str:
        out = spec.io_contract.output_schema
        fields = ", ".join(f"{k}({v.type})" for k, v in out.items())
        required = ", ".join(spec.io_contract.required_out)
        retry = ""
        if history:
            retry = "\n上一轮验证反馈(请修正):\n" + json.dumps(history[-1], ensure_ascii=False)
        return (
            f"角色:{spec.role or spec.spec_id}\n"
            f"输入(JSON):{json.dumps(message, ensure_ascii=False)}\n"
            f"只输出一个 JSON 对象,字段:{fields};必填:{required}。"
            f"不要任何解释或代码围栏之外的文字。{retry}"
        )

    def parse_output(self, stdout: str, spec: AgentSpec) -> dict:
        events = self._iter_json_objects(stdout)
        for ev in events:
            if isinstance(ev, dict) and ev.get("type") == "error":
                err = ev.get("error")
                msg = err.get("data", {}).get("message") if isinstance(err, dict) else err
                raise self._surface(
                    "opencode reported error event", FailureSubtype.TOOL_MISUSE.value,
                    str(msg)[:500], "opencode run 应无 error 事件",
                )
        text = self._reduce_text(events) if events else stdout
        obj = self._extract_json_object(text)
        if obj is None:
            raise self._surface(
                "no JSON object in opencode output", FailureSubtype.SCHEMA_VIOLATION.value,
                (text or "")[:500], "agent 必须输出满足 output_schema 的 JSON 对象",
            )
        return obj

    # ---- parsing helpers ----
    @staticmethod
    def _iter_json_objects(stdout: str) -> list:
        out = []
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    @staticmethod
    def _reduce_text(events: list) -> str:
        chunks: list[str] = []

        def walk(o: Any) -> None:
            if isinstance(o, dict):
                t = o.get("text")
                if isinstance(t, str):
                    chunks.append(t)
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        for ev in events:
            walk(ev)
        return "\n".join(chunks) if chunks else json.dumps(events, ensure_ascii=False)

    @staticmethod
    def _extract_json_object(text: str) -> Optional[dict]:
        if not text:
            return None
        candidates: list[str] = []
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            candidates.append(m.group(1))
        block = OpenCodeAdapter._last_brace_block(text)
        if block:
            candidates.append(block)
        for c in candidates:
            try:
                v = json.loads(c)
            except json.JSONDecodeError:
                continue
            if isinstance(v, dict):
                return v
        return None

    @staticmethod
    def _last_brace_block(text: str) -> Optional[str]:
        depth = 0
        start: Optional[int] = None
        last: Optional[str] = None
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}" and depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    last = text[start : i + 1]
        return last
