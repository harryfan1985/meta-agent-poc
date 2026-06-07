"""meta-agent-poc: spec-driven multi-agent engine (M0 skeleton)."""
from .artifacts import ArtifactLoader
from .attribution import ErrorAttributor
from .budget import BudgetExceeded, BudgetMeter
from .context import TASK_INPUT, ContextStore
from .coordinator import execute
from .dag import (
    DagCycleError,
    affected_subgraph,
    descendants,
    entry_nodes,
    has_cycle,
    sink_nodes,
    topo_order,
)
from .runtime_gate import RuntimeGate
from .schemas import (
    AgentArtifact,
    AgentSpec,
    AssertionSpec,
    Budget,
    ContractMismatch,
    DagEdge,
    ExecutableSwarm,
    FailureType,
    FieldSpec,
    GateResult,
    IOContract,
    RecoveryAction,
    StructuredFeedback,
    SurfaceFailure,
    SwarmPlan,
    VerificationCriteria,
    VerificationPolicy,
)

__all__ = [
    "ArtifactLoader",
    "ErrorAttributor",
    "BudgetExceeded",
    "BudgetMeter",
    "ContextStore",
    "TASK_INPUT",
    "execute",
    "topo_order",
    "has_cycle",
    "entry_nodes",
    "sink_nodes",
    "descendants",
    "affected_subgraph",
    "DagCycleError",
    "RuntimeGate",
    "AgentArtifact",
    "AgentSpec",
    "AssertionSpec",
    "Budget",
    "ContractMismatch",
    "DagEdge",
    "ExecutableSwarm",
    "FailureType",
    "FieldSpec",
    "GateResult",
    "IOContract",
    "RecoveryAction",
    "StructuredFeedback",
    "SurfaceFailure",
    "SwarmPlan",
    "VerificationCriteria",
    "VerificationPolicy",
]
