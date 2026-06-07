import pytest

from meta_agent.dag import (
    DagCycleError,
    entry_nodes,
    has_cycle,
    sink_nodes,
    topo_order,
)
from meta_agent.schemas import DagEdge


def E(f, t):
    return DagEdge(from_spec=f, to_spec=t)


APPENDIX_A = (
    ["spec_analyzer", "algo_planner", "code_synthesizer", "code_verifier"],
    [
        E("spec_analyzer", "algo_planner"),
        E("spec_analyzer", "code_synthesizer"),
        E("algo_planner", "code_synthesizer"),
        E("spec_analyzer", "code_verifier"),
        E("code_synthesizer", "code_verifier"),
    ],
)


def _before(order, a, b):
    return order.index(a) < order.index(b)


def test_dag01_topo_order_appendix_a():
    nodes, edges = APPENDIX_A
    order = topo_order(nodes, edges)
    assert _before(order, "spec_analyzer", "algo_planner")
    assert _before(order, "spec_analyzer", "code_synthesizer")
    assert _before(order, "algo_planner", "code_synthesizer")
    assert _before(order, "code_synthesizer", "code_verifier")


def test_dag02_cycle_detected():
    nodes = ["a", "b", "c"]
    edges = [E("a", "b"), E("b", "c"), E("c", "a")]
    assert has_cycle(nodes, edges) is True
    with pytest.raises(DagCycleError):
        topo_order(nodes, edges)


def test_dag03_self_loop_rejected():
    with pytest.raises(DagCycleError):
        topo_order(["a"], [E("a", "a")])


def test_dag04_diamond():
    nodes = ["a", "b", "c", "d"]
    edges = [E("a", "b"), E("a", "c"), E("b", "d"), E("c", "d")]
    order = topo_order(nodes, edges)
    assert order[0] == "a" and order[-1] == "d"


def test_dag06_entry_and_sink():
    nodes, edges = APPENDIX_A
    assert entry_nodes(nodes, edges) == ["spec_analyzer"]
    assert sink_nodes(nodes, edges) == ["code_verifier"]
