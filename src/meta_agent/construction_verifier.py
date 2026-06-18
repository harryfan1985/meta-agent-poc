"""Stage 5 ConstructionVerifier(§3.5)。

对每个生成的 agent 验证是否满足 spec,两类互补检查,统一返回 §2 GateResult
(与执行期 RuntimeGate 同构):
1. 静态:artifact 能加载成 callable、签名正确、(python_module)AST 扫 forbidden import。
2. 行为:在代表性输入上跑 agent,复用 RuntimeGate.check 判 I/O 契约 + 断言 + forbidden。

代表性输入:优先用注入的 sample_inputs(real M2 来自 planner 的 TestCase),
否则按 io_contract 字段类型生成最小值。
"""
from __future__ import annotations

import ast
import inspect
from typing import Optional

from .runtime_gate import RuntimeGate
from .schemas import (
    AgentArtifact,
    AgentSpec,
    FailureSubtype,
    FailureType,
    FieldSpec,
    GateResult,
    StructuredFeedback,
    VerificationPolicy,
)

_SAMPLE = {
    "string": "x",
    "integer": 0,
    "number": 0.0,
    "boolean": True,
    "array": [],
    "object": {},
}


def _sample_value(fs: FieldSpec):
    return _SAMPLE.get(fs.type, None)


def representative_input(spec: AgentSpec) -> dict:
    """按 io_contract.input_schema 字段类型生成最小可行输入。"""
    return {f: _sample_value(fs) for f, fs in spec.io_contract.input_schema.items()}


def _fb(subtype: str, evidence: str, expected: str = "", fix: str = "") -> StructuredFeedback:
    return StructuredFeedback(subtype=subtype, evidence=evidence, expected=expected, actionable_fix=fix)


def _fail(subtype: str, evidence: str, expected: str = "", fix: str = "",
          ftype: FailureType = FailureType.SPEC_ADHERENCE) -> GateResult:
    return GateResult(ok=False, failure_type=ftype, feedback=[_fb(subtype, evidence, expected, fix)])


def _static_python_import_check(module_path: str, forbidden_patterns: list) -> list:
    """python_module:AST 扫顶层 import,命中 forbidden_pattern 即报。"""
    fb: list = []
    path = module_path
    try:
        import importlib.util
        spec_obj = importlib.util.find_spec(module_path)
        if spec_obj and spec_obj.origin and spec_obj.origin not in ("builtin", "frozen"):
            path = spec_obj.origin  # 模块名 → 真实 .py 文件
    except (ImportError, ValueError, ModuleNotFoundError, AttributeError):
        pass  # module_path 可能本就是文件路径
    try:
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except Exception:  # noqa: BLE001 — 解析不了在 load 阶段已会报错
        return fb
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module.split(".")[0])
    for mod in imported:
        for pat in forbidden_patterns:
            if mod == pat or pat == f"import {mod}":
                fb.append(_fb(FailureSubtype.FORBIDDEN_HIT.value,
                              f"forbidden import {mod!r}(命中 {pat!r})",
                              f"不得 import {mod}", f"移除 import {mod}"))
    return fb


class ConstructionVerifier:
    def __init__(self, loader, task_input: Optional[dict] = None,
                 sample_inputs: Optional[dict] = None, verifier=None):
        self.loader = loader  # ArtifactLoader
        self.task_input = task_input or {}  # swarm 级样例输入(入口节点 & 透传源)
        self.sample_inputs = sample_inputs or {}  # 显式 per-spec 覆盖;"*" = 入口默认
        self.verifier = verifier  # M3 VerifierRegistry(model_check);None → fail-closed
        # 行为验证的拓扑 dry-run 累积:spec_id -> 该节点通过 gate 的实际样例产出,
        # 供下游按 DAG 组装代表性输入(镜像执行期 gather_inputs:中游只能取直接依赖产出)。
        self._sample_outputs: dict[str, dict] = {}

    def _gather_sample_input(self, spec: AgentSpec) -> dict:
        # 1) 显式 per-spec 覆盖优先
        if spec.spec_id in self.sample_inputs:
            return dict(self.sample_inputs[spec.spec_id])
        deps = spec.dependencies
        # 2) 入口节点:swarm task_input(或 "*" 默认 / 类型最小值兜底)
        if not deps:
            base = self.task_input or self.sample_inputs.get("*")
            return dict(base) if base else representative_input(spec)
        # 3) 中游节点:镜像 gather_inputs——按字段从直接依赖的样例产出取;
        #    上游样例缺该字段时用类型最小值兜底,保证行为运行不因缺字段中断。
        msg: dict = {}
        for field, fs in spec.io_contract.input_schema.items():
            value = None
            for d in deps:
                out = self._sample_outputs.get(d)
                if out and field in out:
                    value = out[field]
                    break
            msg[field] = value if value is not None else _sample_value(fs)
        return msg

    def verify(self, artifact: AgentArtifact, spec: AgentSpec) -> GateResult:
        # 1) 静态:加载成 callable
        try:
            agent = self.loader.load(artifact, spec)
        except Exception as e:  # noqa: BLE001
            return _fail(FailureSubtype.SCHEMA_VIOLATION.value,
                         f"artifact 加载失败:{type(e).__name__}: {e}",
                         "artifact 必须能加载成 run(message, history) callable",
                         "修复 handler_ref/module_path/adapter_name")

        # 1b) 签名:接受 (message, history)
        try:
            params = [p for p in inspect.signature(agent).parameters.values()
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)]
            n = sum(1 for p in params if p.kind != p.VAR_POSITIONAL)
            has_varargs = any(p.kind == p.VAR_POSITIONAL for p in params)
            if not has_varargs and n < 2:
                return _fail(FailureSubtype.SCHEMA_VIOLATION.value,
                             f"callable 签名只接受 {n} 个位置参数",
                             "run(message, history)", "修正 agent 接口签名")
        except (TypeError, ValueError):
            pass  # 内置/不可内省的 callable,跳过签名检查

        # 1c) python_module:AST forbidden import
        if artifact.implementation_kind == "python_module" and artifact.module_path:
            import_fb = _static_python_import_check(
                artifact.module_path, spec.verification_criteria.forbidden_patterns)
            if import_fb:
                return GateResult(ok=False, failure_type=FailureType.SPEC_ADHERENCE,
                                  feedback=import_fb)

        # 2) 行为:按 DAG 组装代表性输入(中游取上游样例产出)→ 跑 → 复用 RuntimeGate
        msg = self._gather_sample_input(spec)
        try:
            output = agent(msg, [])
        except Exception as e:  # noqa: BLE001
            return _fail(FailureSubtype.RUNTIME_ERROR.value,
                         f"代表性输入上运行失败:{type(e).__name__}: {e}",
                         "agent 在代表性输入上不抛异常",
                         "修复 agent 实现")
        if not isinstance(output, dict):
            return _fail(FailureSubtype.SCHEMA_VIOLATION.value,
                         f"输出非 dict:{type(output).__name__}",
                         "返回满足 output_schema 的 dict")

        # 显式注册 verifier 即视为授权构造期 model_check;否则保持 fail-closed。
        policy = VerificationPolicy(allow_model_verification=True) if self.verifier is not None else None
        result = RuntimeGate.check(output, spec, message=msg, policy=policy, verifier=self.verifier)
        if result.ok:
            # 仅通过 gate 的产出才向下游传播(与执行期一致),供后续节点组装输入
            self._sample_outputs[spec.spec_id] = output
        return result
