"""Minimal manual eval CLI for real StructuredLLM providers."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from .artifacts import ArtifactLoader
from .construct import construct
from .coordinator import execute
from .llm import create_structured_llm
from .stages import default_stages
from .trace import JsonlTracer


def _json_arg(value: str) -> dict:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        raise argparse.ArgumentTypeError(str(e)) from e
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("JSON value must be an object")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a real-model M2 construct+execute smoke test.")
    parser.add_argument("--provider", choices=["anthropic", "openai"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", help="OpenAI-compatible base URL, e.g. http://localhost:8000/v1")
    parser.add_argument("--task", required=True)
    parser.add_argument("--task-input-json", type=_json_arg)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--api-key-env")
    parser.add_argument("--trace-jsonl")
    args = parser.parse_args(argv)

    backend = create_structured_llm(
        args.provider,
        args.model,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        timeout=args.timeout,
        api_key_env=args.api_key_env,
        base_url=args.base_url,
    )
    loader = ArtifactLoader(structured_llm=backend)
    task_input = args.task_input_json if args.task_input_json is not None else {"task": args.task}
    stages = default_stages(backend, loader, task_input=task_input)

    if args.trace_jsonl:
        trace_path = Path(args.trace_jsonl)
    else:
        fd, name = tempfile.mkstemp(prefix="meta-agent-trace-", suffix=".jsonl")
        os.close(fd)
        trace_path = Path(name)
    with trace_path.open("w", encoding="utf-8") as stream:
        tracer = JsonlTracer(stream)
        swarm = construct(args.task, stages, tracer=tracer)
        loader.bind(swarm)
        output = execute(swarm, task_input, tracer=tracer)

    print(json.dumps({"output": output, "trace_jsonl": str(trace_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
