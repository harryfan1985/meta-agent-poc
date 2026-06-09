"""可观测(§7.3):Tracer 接口 + 几种 sink。

execute()/adapter 全程发 TraceEvent;默认 NullTracer 零开销、行为不变。
MVP 落地为 JSONL(JsonlTracer);生产可换 OpenTelemetry 导出。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional, Protocol, TextIO

from .schemas import GateResult, TraceEvent


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Tracer(Protocol):
    def emit(self, event: TraceEvent) -> None: ...


class NullTracer:
    """默认:不记录,零开销。"""

    def emit(self, event: TraceEvent) -> None:  # noqa: D401
        return None


class ListTracer:
    """收集到内存,便于测试/回放。"""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def emit(self, event: TraceEvent) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.event for e in self.events]


class JsonlTracer:
    """逐行 JSONL 落盘(MVP 存储)。"""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def emit(self, event: TraceEvent) -> None:
        self._stream.write(event.model_dump_json() + "\n")


def make_event(
    event: str,
    *,
    phase: str = "execute",
    stage: str = "",
    spec_id: Optional[str] = None,
    gate_result: Optional[GateResult] = None,
    recovery_kind: Optional[str] = None,
    tokens: int = 0,
    latency_ms: int = 0,
    payload: Optional[dict] = None,
) -> TraceEvent:
    return TraceEvent(
        ts=now_iso(),
        phase=phase,  # type: ignore[arg-type]
        stage=stage,
        spec_id=spec_id,
        event=event,  # type: ignore[arg-type]
        gate_result=gate_result,
        recovery_kind=recovery_kind,
        tokens=tokens,
        latency_ms=latency_ms,
        payload=payload or {},
    )
