"""ContextStore + gather_inputs(§4.2)。DAG 是唯一数据流真相。"""
from __future__ import annotations

from typing import Any

from .dag import sink_nodes
from .schemas import ContractMismatch, ExecutableSwarm

# 原始 task_input 的保留虚拟源:入口节点(入度 0)的隐式依赖(§4.2)。
TASK_INPUT = "__task_input__"


class ContextStore:
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        self._sources: dict[str, dict[str, str]] = {}  # spec_id -> {field: source_id}

    def put(self, spec_id: str, output: dict) -> None:
        self._data[spec_id] = output

    def has(self, spec_id: str) -> bool:
        return spec_id in self._data

    def get(self, spec_id: str) -> dict:
        return self._data[spec_id]

    def history(self, spec_id: str) -> list:  # M0:无历史
        return []

    def sources(self, spec_id: str) -> dict[str, str]:
        return self._sources.get(spec_id, {})

    def _provides(self, source_id: str, field: str, swarm: ExecutableSwarm) -> bool:
        if source_id == TASK_INPUT:
            return self.has(TASK_INPUT) and field in self.get(TASK_INPUT)
        if not self.has(source_id):
            return False
        # 声明的 output_schema(契约可追溯性)+ 实际存在(稳健性:防上游产出残缺)。
        # 真实流里 gate 保证两者一致;此处双查可避免对未过 gate 的输出 KeyError。
        return (
            field in swarm.spec(source_id).io_contract.output_schema
            and field in self.get(source_id)
        )

    def gather_inputs(self, spec_id: str, swarm: ExecutableSwarm) -> dict[str, Any]:
        """为下游组装满足 input_schema 的 message。来源仅限直接依赖;
        入口节点用 __task_input__。缺必填字段或歧义 → ContractMismatch。"""
        spec = swarm.spec(spec_id)
        deps = spec.dependencies
        pool = list(deps) if deps else [TASK_INPUT]

        message: dict[str, Any] = {}
        unresolved: list[str] = []
        conflicts: dict[str, list[str]] = {}
        srcmap: dict[str, str] = {}

        for field in spec.io_contract.input_schema:
            srcs = [d for d in pool if self._provides(d, field, swarm)]
            if not srcs:
                if field in spec.io_contract.required_in:
                    unresolved.append(field)  # 必填缺失
                # 可选字段无来源 → 跳过,不报错
            elif len(srcs) > 1:
                conflicts[field] = srcs  # 多上游同名 → 歧义
            else:
                message[field] = self.get(srcs[0])[field]
                srcmap[field] = srcs[0]

        if unresolved or conflicts:
            raise ContractMismatch(spec_id, unresolved=unresolved, conflicts=conflicts)

        self._sources[spec_id] = srcmap
        return message

    def final_output(self, swarm: ExecutableSwarm) -> dict:
        sinks = [s for s in sink_nodes(swarm.spec_ids, swarm.dag) if self.has(s)]
        if len(sinks) == 1:
            return self.get(sinks[0])
        return {s: self.get(s) for s in sinks}
