"""M0 core data models (§2 truth source, M0 subset).

机判优先:这些模型是 RuntimeGate / Coordinator 直接消费的契约真相源。
仅含 M0/M1 需要的部分;§4.6–4.12 的验证质量模型(VerifierHealth 等)留到 M3。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field, PrivateAttr

# ---------------------------------------------------------------- IO contract


class FieldSpec(BaseModel):
    """单字段定义,可编译进 JSON Schema 供机判校验。"""

    type: Literal["string", "number", "integer", "boolean", "array", "object"]
    description: str = ""
    items: Optional[dict] = None
    enum: Optional[list] = None


class IOContract(BaseModel):
    input_schema: dict[str, FieldSpec] = Field(default_factory=dict)
    output_schema: dict[str, FieldSpec] = Field(default_factory=dict)
    required_in: list[str] = Field(default_factory=list)
    required_out: list[str] = Field(default_factory=list)
    description: str = ""

    def out_jsonschema(self) -> dict:
        """编译成标准 JSON Schema,供 RuntimeGate 机判 output。"""
        return {
            "type": "object",
            "required": list(self.required_out),
            "properties": {
                k: v.model_dump(exclude_none=True) for k, v in self.output_schema.items()
            },
        }


# ---------------------------------------------------------------- assertions

AssertionKind = Literal[
    "field_present",
    "field_absent",
    "equals_input",
    "contains",
    "not_contains",
    "regex_match",
    "jsonschema",
    "python_assert",  # M1 沙箱后端
    "model_check",  # M3 模型判;M0/M1 默认拒绝
]


class AssertionSpec(BaseModel):
    assertion_id: str
    kind: AssertionKind
    target_path: str = ""  # JSON Pointer,如 "/raw_signature"
    expected: Optional[Any] = None
    expression: Optional[str] = None
    description: str = ""
    failure_type: Literal["spec_adherence", "grounding", "contract"] = "spec_adherence"
    failure_subtype: str = "schema_violation"


class VerificationCriteria(BaseModel):
    behavioral_assertions: list[str] = Field(default_factory=list)
    machine_assertions: list[AssertionSpec] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_patterns: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- policy


class VerificationPolicy(BaseModel):
    risk_tier: Literal["low", "medium", "high", "critical"] = "low"
    allow_model_verification: bool = False


# ---------------------------------------------------------------- spec / DAG


class AgentSpec(BaseModel):
    spec_id: str
    role: str = ""
    tools: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    risk_tier: Literal["low", "medium", "high", "critical"] = "low"
    io_contract: IOContract
    verification_criteria: VerificationCriteria = Field(default_factory=VerificationCriteria)


class DagEdge(BaseModel):
    from_spec: str
    to_spec: str


class SwarmPlan(BaseModel):
    swarm_name: str
    summary: str = ""
    coordination_strategy: str = ""
    specs: list[AgentSpec]
    dag_edges: list[DagEdge] = Field(default_factory=list)
    verification_policy: VerificationPolicy = Field(default_factory=VerificationPolicy)


# ---------------------------------------------------------------- artifacts


class AgentArtifact(BaseModel):
    spec_id: str
    implementation_kind: Literal[
        "python_module", "prompt_template", "fixture", "external_agent"
    ] = "fixture"
    module_path: Optional[str] = None
    entrypoint: str = "run"
    prompt_template: Optional[str] = None
    handler_ref: Optional[str] = None  # fixture
    adapter_name: Optional[str] = None  # external_agent
    passed: bool = False


class ExecutableSwarm(BaseModel):
    """构造期最终产物;§4 执行期全程消费。"""

    plan: SwarmPlan
    artifacts: dict[str, AgentArtifact] = Field(default_factory=dict)

    # spec_id -> Callable[[dict, list], dict];由 ArtifactLoader(§4.1)在执行前填充。
    _loaded: dict[str, Callable] = PrivateAttr(default_factory=dict)

    def spec(self, spec_id: str) -> AgentSpec:
        return next(s for s in self.plan.specs if s.spec_id == spec_id)

    def agent(self, spec_id: str) -> Callable:
        return self._loaded[spec_id]

    @property
    def dag(self) -> list[DagEdge]:
        return self.plan.dag_edges

    @property
    def spec_ids(self) -> list[str]:
        return [s.spec_id for s in self.plan.specs]


# ---------------------------------------------------------------- failure signal


class FailureType(str, Enum):
    SPEC_ADHERENCE = "spec_adherence"
    GROUNDING = "grounding"
    CONTRACT = "contract"


# 多检查同时失败时,按此优先级选 failure_type 做路由(§3.7)。
FAILURE_PRIORITY: list[FailureType] = [
    FailureType.CONTRACT,
    FailureType.GROUNDING,
    FailureType.SPEC_ADHERENCE,
]


class FailureSubtype(str, Enum):
    ROLE_CONFUSION = "role_confusion"
    SCHEMA_VIOLATION = "schema_violation"
    FORBIDDEN_HIT = "forbidden_hit"
    TOOL_MISUSE = "tool_misuse"
    TIMEOUT = "timeout"
    OUTPUT_TOO_LARGE = "output_too_large"
    MISSING_KNOWLEDGE = "missing_knowledge"
    STALE_SOURCE = "stale_source"
    IRRELEVANT_RESULT = "irrelevant_result"
    FIELD_MISMATCH = "field_mismatch"
    DECOMP_FLAW = "decomp_flaw"
    CYCLE_DETECTED = "cycle_detected"


class StructuredFeedback(BaseModel):
    subtype: str
    evidence: str  # 可溯源:指向违反的字段/断言
    expected: str = ""
    actionable_fix: str = ""


class VerificationCoverage(BaseModel):
    """验证充分性(§4.8):gate passed 只说已执行的检查过了,不说验得够。
    M1 填充 schema / assertion / forbidden;claim/edge/tool/regression 留 M2/M3。"""

    schema_fields_total: int = 0
    schema_fields_checked: int = 0
    assertions_total: int = 0
    assertions_checked: int = 0
    forbidden_total: int = 0
    forbidden_checked: int = 0
    claims_total: int = 0
    claims_verified: int = 0
    tool_calls_total: int = 0
    tool_calls_gated: int = 0


class GateResult(BaseModel):
    """构造期 ConstructionVerifier 与执行期 RuntimeGate 共用。"""

    ok: bool
    failure_type: Optional[FailureType] = None  # 轴 A:路由
    feedback: list[StructuredFeedback] = Field(default_factory=list)  # 轴 B:修复
    coverage: Optional[VerificationCoverage] = None  # §4.8 验证充分性


class RecoveryAction(BaseModel):
    kind: Literal["local", "upstream", "structural"]
    target: Optional[str] = None
    subgraph: list[str] = Field(default_factory=list)
    feedback: list[StructuredFeedback] = Field(default_factory=list)


# ---------------------------------------------------------------- budget (§7.2)


class Budget(BaseModel):
    max_llm_calls: int = 200
    max_tokens: int = 2_000_000
    max_wall_seconds: int = 1800
    max_local_retries: int = 2  # 单 agent 本地重试上限
    max_upstream_reruns: int = 2  # 单上游重跑上限
    max_construct_passes: int = 3  # 构造期验证 pass(M2)
    max_replans: int = 2  # structural 重规划(M2)
    bon_n: int = 1  # §3.4 BoN 候选数,1=关闭


# ---------------------------------------------------------------- tools (§3.6 / §4.4)


class ToolDefinition(BaseModel):
    name: str
    backend_schema: dict = Field(default_factory=dict)  # 参数 JSON Schema
    handler_ref: str = ""
    requires_network: bool = False
    side_effects: Literal["none", "read", "write"] = "none"


class ToolGateResult(BaseModel):
    ok: bool
    tool_name: str
    stage: Literal["pre", "post"]
    gate_result: GateResult


# ---------------------------------------------------------------- golden cases (§4.7)


class GoldenVerificationCase(BaseModel):
    """verifier 校准基线(§4.7)。M1 建最小集校准机判 gate;M3 用于模型判校准。
    evidence_refs 在 M1 用松散 dict(完整 EvidenceRef 属 M3)。"""

    case_id: str
    spec_id: str
    message: dict = Field(default_factory=dict)
    output: dict = Field(default_factory=dict)
    expected_ok: bool
    expected_failure_type: Optional[Literal["spec_adherence", "grounding", "contract"]] = None
    expected_subtypes: list[str] = Field(default_factory=list)
    evidence_refs: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------- exceptions


class ContractMismatch(Exception):
    """gather_inputs 缺字段/歧义 → §5 归因为 structural。"""

    def __init__(self, spec_id: str, unresolved=None, conflicts=None):
        self.spec_id = spec_id
        self.unresolved = unresolved or []
        self.conflicts = conflicts or {}
        super().__init__(
            f"ContractMismatch({spec_id}): unresolved={self.unresolved} conflicts={self.conflicts}"
        )


class SurfaceFailure(Exception):
    """预算触顶或验证失败且无法恢复时上浮,而非返回未验证答案(M0 无恢复)。"""

    def __init__(self, reason: str, spec_id: Optional[str] = None, gate_result: Optional[GateResult] = None):
        self.reason = reason
        self.spec_id = spec_id
        self.gate_result = gate_result
        super().__init__(f"SurfaceFailure({spec_id}): {reason}")
