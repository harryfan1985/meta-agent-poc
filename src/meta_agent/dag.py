"""DAG 拓扑序与环检测。M0 用标准库 graphlib,零依赖。"""
from __future__ import annotations

from graphlib import CycleError, TopologicalSorter

from .schemas import DagEdge


class DagError(Exception):
    pass


class DagCycleError(DagError):
    pass


def _predecessors(node_ids: list[str], edges: list[DagEdge]) -> dict[str, set[str]]:
    nodes = set(node_ids)
    preds: dict[str, set[str]] = {n: set() for n in node_ids}
    for e in edges:
        if e.from_spec not in nodes or e.to_spec not in nodes:
            raise DagError(f"edge references unknown node: {e.from_spec} -> {e.to_spec}")
        if e.from_spec == e.to_spec:
            raise DagCycleError(f"self-loop: {e.from_spec}")
        preds[e.to_spec].add(e.from_spec)
    return preds


def topo_order(node_ids: list[str], edges: list[DagEdge]) -> list[str]:
    """返回拓扑序;有环抛 DagCycleError。"""
    preds = _predecessors(node_ids, edges)
    try:
        return list(TopologicalSorter(preds).static_order())
    except CycleError as e:  # graphlib 检测到环
        raise DagCycleError(str(e)) from e


def has_cycle(node_ids: list[str], edges: list[DagEdge]) -> bool:
    try:
        topo_order(node_ids, edges)
        return False
    except DagCycleError:
        return True


def entry_nodes(node_ids: list[str], edges: list[DagEdge]) -> list[str]:
    """入度为 0 的节点。"""
    preds = _predecessors(node_ids, edges)
    return [n for n in node_ids if not preds[n]]


def sink_nodes(node_ids: list[str], edges: list[DagEdge]) -> list[str]:
    """出度为 0 的节点(最终产物来源)。"""
    has_out = {e.from_spec for e in edges}
    return [n for n in node_ids if n not in has_out]


def descendants(node: str, edges: list[DagEdge]) -> set[str]:
    """node 的所有下游可达节点(不含自身)。用于 structural 受影响子图。"""
    adj: dict[str, list[str]] = {}
    for e in edges:
        adj.setdefault(e.from_spec, []).append(e.to_spec)
    seen: set[str] = set()
    stack = list(adj.get(node, []))
    while stack:
        n = stack.pop()
        if n not in seen:
            seen.add(n)
            stack.extend(adj.get(n, []))
    return seen


def affected_subgraph(node: str, edges: list[DagEdge]) -> list[str]:
    """structural 恢复要重建的子图:node + 其全部下游。"""
    return [node] + sorted(descendants(node, edges))
