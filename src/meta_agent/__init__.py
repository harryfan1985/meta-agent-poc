"""meta-agent-poc: spec-driven multi-agent engine (M0 skeleton)."""
from .artifacts import ArtifactLoader
from .context import TASK_INPUT, ContextStore
from .coordinator import execute
from .dag import DagCycleError, entry_nodes, has_cycle, sink_nodes, topo_order
from .runtime_gate import RuntimeGate
from .schemas import (
    AgentArtifact,
    AgentSpec,
    AssertionSpec,
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
    "ContextStore",
    "TASK_INPUT",
    "execute",
    "topo_order",
    "has_cycle",
    "entry_nodes",
    "sink_nodes",
    "DagCycleError",
    "RuntimeGate",
    "AgentArtifact",
    "AgentSpec",
    "AssertionSpec",
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
