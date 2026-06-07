# Meta-Agent 落地方案:Spec 驱动的 Agent 编码系统

> 依据论文:*Meta-Agent: From Task Descriptions to Verified Multi-Agent Systems*(Andy Xu, Yu-Wing Tai,Dartmouth,arXiv:2605.25233,2026)
>
>
> 标注约定:**【论文】** = 论文明确描述的机制;**【工程补全】** = 论文未指定、本方案为可落地补充的具体决策,可替换。

---

## 0. 一句话定位

把"为某个任务搭一套多 agent 系统"这件事本身,变成一条**自动化流水线**:输入一段自然语言任务描述 → 编译出一张带 I/O 契约和验证标准的 agent 有向无环图(DAG)→ 为每个节点生成可执行 agent 代码 → 在构造期和执行期都做验证,失败时按"错误类型"做最小代价回退。

**【论文】** 核心论断:reliability 不是靠事后 self-reflection 补救,而是把**显式、结构化的验证**贯穿构造与执行两个阶段;并用"三级错误归因"让恢复成本与错误局部性成正比(局部重试 < 上游重跑 < 重新规划)。消融实验里,去掉验证掉 7.1 分、去掉外部检索 grounding 掉 5.5 分,是最吃重的两个组件。

---

## 1. 论文方法 → 工程组件映射

| 论文概念 | 工程组件 | 职责 |
|---|---|---|
| Prompt Analysis(Stage 1) | `IntentParser` | 任务描述 T → 结构化 `ParsedIntent`,web 检索补任务示例 |
| Architecture / Swarm Planning(Stage 2) | `SwarmPlanner` | 分解为少量子任务,产出 `AgentSpec` 列表 + DAG 边 |
| API Research / Grounding(Stage 3) | `GroundingResearcher` | 按 spec 逐个做定向检索,把外部知识写进 spec |
| Code Generation(Stage 4) | `AgentCodeGen` | 每个 spec → 一个带 `run(message, history)` 接口的 Python 模块 |
| Construction Verification(Stage 5) | `ConstructionVerifier` | 静态 + 行为双检,产出**带类型**的失败信号 |
| Coordinator / Orchestrator | `Coordinator` | 按 DAG 拓扑序调度,依赖就绪即派发 |
| In-memory context store | `ContextStore` | 路由各 agent 的中间产物 |
| Execution Verification | `RuntimeGate` | 每个中间输出消费前校验 `y_i ∈ C_i` |
| 三级错误归因 | `ErrorAttributor` + `RecoveryRouter` | 分类 local / upstream / structural 并选恢复策略 |
| Generate→Verify→Attribute→Refine | 贯穿上述模块的统一闭环 | — |

**【工程补全】本方案在论文组件之外增补的引擎级组件**(详见对应章节):

| 工程组件 | 职责 | 章节 |
|---|---|---|
| `ToolRegistry` | 抽象工具名 → 后端真实定义的单一出口,杜绝工具格式自由发挥 | §3.6 |
| `ArtifactLoader` | 把四形态 `AgentArtifact`(fixture/prompt/module/external_agent)统一加载成 callable | §4.1 |
| `VerificationPolicy` + `ConstitutionRule` | 按 `risk_tier` 决定验证强度;不可覆写的安全红线进 spec | §2 / §3.2 |
| `VerifierBackend` 栈 | 机判/模型判/agent 判统一后端协议,按里程碑分档上线 | §4.5 |
| `AssertionSpec` | 把自然语言断言编译成可机判断言(M0/M1 主力) | §2 / §3.5 |
| `Claim` + `EvidenceRef` | 模型判残差:抽 claim 逐条对证据核查,而非 judge 整段 | §2 / §4.5 |
| `AgentRuntimeAdapter` | control plane:把节点委派给外部 code agent(Claude Code/opencode/Pi),产物仍过 gate | architecture.md |
| `BudgetMeter` / `TraceEvent` / `SurfaceFailure` | 预算兜底、可观测、失败上浮 | §7.2 / §7.3 |

---

## 2. 核心数据模型

**【论文】** 附录 A/B/C 给出了 `ParsedIntent`、`SwarmPlan`、`AgentSpec`(含 `io_contract` 与 `verification_criteria`)的确切字段。下面用 Pydantic 还原为可执行 schema,这是整个系统的"spec 真相源"。

```python
from pydantic import BaseModel, Field, PrivateAttr
from typing import Literal, Optional
from enum import Enum

# ---------- Stage 1: ParsedIntent ----------
class TaskExample(BaseModel):
    task_type: str
    example: str
    source_url: Optional[str] = None      # 来自 web_search 的 provenance

class ParsedIntent(BaseModel):
    goal: str
    domain: str
    tone: str = ""                        # 论文遗留字段;code/math 任务通常为空
    entities: list[str]
    constraints: list[str]
    task_examples: list[TaskExample]      # 论文:覆盖任务空间的 5~8 类示例

# ---------- Agent Spec(DAG 节点) ----------
# 【工程补全】字段必须可机判:不是 "type -- desc" 自由串,而是结构化 FieldSpec,
# 可直接编译成 JSON Schema 交 jsonschema/Pydantic 在 RuntimeGate 做机判校验。
class FieldSpec(BaseModel):
    type: Literal["string", "number", "integer",
                  "boolean", "array", "object"]
    description: str
    items: Optional[dict] = None          # array 元素 schema(JSON Schema 片段)
    enum: Optional[list] = None           # 取值枚举(可选)

class IOContract(BaseModel):
    input_schema: dict[str, FieldSpec]    # {field: FieldSpec};键即字段名
    output_schema: dict[str, FieldSpec]
    required_in: list[str] = Field(default_factory=list)  # 必填输入字段(其余视为可选)
    required_out: list[str] = Field(default_factory=list) # 必填输出字段
    description: str

    def out_jsonschema(self) -> dict:     # 编译成标准 JSON Schema,供机判校验
        return {"type": "object", "required": self.required_out,
                "properties": {k: v.model_dump(exclude_none=True)
                               for k, v in self.output_schema.items()},
                "additionalProperties": False}

    # M1:required_in/out 必须分别属于 input_schema/output_schema;
    # 输出契约默认封闭,未声明字段不得传播。

# 【工程补全】论文的 behavioral_assertions 是自然语言列表。为保证 M1
# RuntimeGate 能全部机判,这里增加可执行断言层:保留原文断言供 prompt/trace
# 展示,同时把可机判部分编译成 AssertionSpec。无法机判的语义断言才留给
# §3.7 aspect verifier 面板。
class AssertionSpec(BaseModel):
    assertion_id: str
    kind: Literal["field_present", "field_absent", "equals_input",
                  "contains", "not_contains", "regex_match",
                  "jsonschema", "python_assert", "model_check"]
    target_path: str                      # JSON Pointer, e.g. "/raw_signature"
    expected: Optional[object] = None
    expression: Optional[str] = None      # python_assert/model_check 的表达式或问题
    description: str
    # 字符串值与下文 FailureType/FailureSubtype 枚举保持一致;实现时可直接
    # 收窄成枚举类型,文档中放在这里是为了让 VerificationCriteria 就近完整。
    failure_type: Literal["spec_adherence", "grounding", "contract"] = "spec_adherence"
    failure_subtype: str = "schema_violation"

class VerificationCriteria(BaseModel):
    behavioral_assertions: list[str]      # 论文字段:人类可读的行为断言
    machine_assertions: list[AssertionSpec] = Field(default_factory=list)  # 工程字段:M0/M1 优先执行
    required_tools: list[str] = Field(default_factory=list)
    forbidden_patterns: list[str]         # 必须不出现的模式(如"不得输出代码")

# ---------- 风险策略与宪法约束 ----------
# 【工程补全】借鉴 Agent-as-Judge Survey 的成本/可靠性权衡与 CSDD 的
# "spec 层锁定安全边界":先按风险级别决定验证强度,再让 planner 在任务分解
# 前消费不可覆写的 policy/constitution rules。
class VerificationPolicy(BaseModel):
    risk_tier: Literal["low", "medium", "high", "critical"] = "low"
    allow_model_verification: bool = False
    require_human_review: bool = False
    min_verifier_votes: int = 1
    max_verifier_cost_tier: Literal["free", "cheap", "expensive"] = "free"
    conservative_mode: bool = True        # 高风险默认宁可误拒,不放过错误

class ConstitutionRule(BaseModel):
    rule_id: str
    scope: Literal["global", "domain", "swarm", "agent", "tool"] = "swarm"
    severity: Literal["block", "warn", "review"] = "block"
    description: str
    machine_assertion: Optional[AssertionSpec] = None
    applies_to_tools: list[str] = Field(default_factory=list)

# ---------- Stage 3 产物:Grounding ----------
class Recommendation(BaseModel):
    name: str                             # API / 库 / 文档条目名
    url: str                              # provenance,来自定向检索
    auth_method: Optional[str] = None     # e.g. "api_key" / "oauth" / None
    relevance_score: float = Field(ge=0, le=1)

class GroundingResult(BaseModel):
    directive: str                        # 该 spec 派生出的检索 directive(查询)
    research_summary: str                 # 供 codegen 注入系统提示的知识摘要
    recommendations: list[Recommendation] = Field(default_factory=list)
    retrieved_at: Optional[str] = None    # ISO 时间戳,便于缓存/失效判断

class AgentSpec(BaseModel):
    spec_id: str
    role: str
    tools: list[str] = Field(default_factory=list)         # e.g. ["web_search", "file_generator"]
    dependencies: list[str] = Field(default_factory=list)  # 指向其它 spec_id
    risk_tier: Literal["low", "medium", "high", "critical"] = "low"
    io_contract: IOContract
    verification_criteria: VerificationCriteria
    # 【工程补全】grounding 阶段回填(纯推理 agent 保持 None):
    grounding: Optional[GroundingResult] = None

# ---------- Stage 2: SwarmPlan ----------
class DagEdge(BaseModel):
    from_spec: str
    to_spec: str

class SwarmPlan(BaseModel):
    swarm_name: str
    summary: str
    coordination_strategy: str            # 论文:严格拓扑序的分阶段描述
    specs: list[AgentSpec]
    dag_edges: list[DagEdge]
    verification_policy: VerificationPolicy = Field(default_factory=VerificationPolicy)
    constitution_rules: list[ConstitutionRule] = Field(default_factory=list)

# ---------- 工具注册表(详见 §3.6)----------
# 【工程补全】spec.tools 里只出现"抽象工具名";后端真实的工具定义由注册表
# 唯一产出,模型/codegen 不得自由拼装格式(论文 math Pass 2 就栽在工具格式)。
class ToolDefinition(BaseModel):
    name: str                             # 抽象名,e.g. "web_search"
    backend_schema: dict                  # 后端真实工具定义(Anthropic tool schema)
    handler_ref: str                      # 执行期实际调用的 handler 标识
    requires_network: bool = False        # 沙箱据此决定是否放行出网
    side_effects: Literal["none", "read", "write"] = "none"

# ---------- 构造产物 ----------
class AgentArtifact(BaseModel):
    spec_id: str
    implementation_kind: Literal["python_module", "prompt_template",
                                 "fixture", "external_agent"] = "python_module"
    module_path: Optional[str] = None     # python_module:生成的 .py 文件
    entrypoint: str = "run"               # python_module:run(message, history) -> dict
    prompt_template: Optional[str] = None # prompt_template:参数化 system prompt
    handler_ref: Optional[str] = None     # fixture/M0:内置 handler 名,非生成代码
    adapter_name: Optional[str] = None    # external_agent:走 AgentRuntimeAdapter
                                          # (Claude Code/opencode/Pi/ACP/A2A;详见 architecture.md)
    passed: bool = False

# ---------- 构造期最终产物:ExecutableSwarm ----------
# 【工程补全】§4 执行期全程消费此对象。它把"规划真相(plan)"、"可执行体
# (loaded callables)"和"构造期凭证(artifacts)"绑定在一起,并提供 §4 用到的
# 三个访问器。spec/artifact 在 plan 内已校验过 DAG 无环,这里只做索引。
class ExecutableSwarm(BaseModel):
    plan: SwarmPlan                       # specs + dag_edges(已通过环检测)
    artifacts: dict[str, AgentArtifact]   # spec_id -> 构造凭证(passed=True)

    # spec_id -> 已加载的 run(message, history) callable;由 ArtifactLoader(§4.1)
    # 在执行前把各形态 artifact(fixture/prompt/module/external_agent)统一加载成
    # callable 填充。PrivateAttr:不进序列化、非共享默认值。
    _loaded: dict = PrivateAttr(default_factory=dict)

    def spec(self, spec_id: str) -> AgentSpec:
        return next(s for s in self.plan.specs if s.spec_id == spec_id)

    def agent(self, spec_id: str):
        return self._loaded[spec_id]      # -> callable(message, history) -> dict

    @property
    def dag(self) -> list[DagEdge]:
        return self.plan.dag_edges

# ---------- 失败信号(两条正交轴,见 §5)----------
# 轴 A「locality / 在哪修」→ 驱动恢复路由(§5/§6)
class FailureType(str, Enum):
    SPEC_ADHERENCE = "spec_adherence"     # → 重新生成代码(带反馈)
    GROUNDING      = "grounding"          # → 重跑 API research
    CONTRACT       = "contract"           # → 重新规划架构

# 轴 B「content / 错的是什么」→ 驱动 rubric 结构化反馈(借鉴 DeepVerifier)
# 【工程补全】每个顶层类型下挂细粒度子类,子类绑定一个 rubric 反馈模板;
# 子类只让"反馈更准",不改变路由(路由仍只看 failure_type)。
class FailureSubtype(str, Enum):
    # spec_adherence 下
    ROLE_CONFUSION   = "role_confusion"     # 越界/混入其它职责(论文 math classifier)
    SCHEMA_VIOLATION = "schema_violation"   # 输出不满足 output_schema
    FORBIDDEN_HIT    = "forbidden_hit"      # 命中 forbidden_patterns
    TOOL_MISUSE      = "tool_misuse"        # 工具调用方式/格式错误
    TIMEOUT          = "timeout"            # 沙箱超时(执行期)
    OUTPUT_TOO_LARGE = "output_too_large"   # 输出超限
    # grounding 下
    MISSING_KNOWLEDGE = "missing_knowledge" # 缺必要外部知识
    STALE_SOURCE      = "stale_source"      # provenance 过期/不可达
    IRRELEVANT_RESULT = "irrelevant_result" # 检索返回不相关内容
    # contract 下
    FIELD_MISMATCH    = "field_mismatch"    # 上下游字段对不上(契约可追溯性破坏)
    DECOMP_FLAW       = "decomp_flaw"       # 分解本身有缺陷
    CYCLE_DETECTED    = "cycle_detected"    # DAG 环检测失败(构造期)

# rubric 派生的结构化修正(不是标量分;借鉴 DeepVerifier 的 rubric-guided feedback)
class StructuredFeedback(BaseModel):
    subtype: FailureSubtype
    evidence: str                         # 可溯源:指向违反的 spec 子句/字段/断言
    expected: str                         # spec 要求的样子
    actionable_fix: str                   # 下一轮 refine 的具体改法(非"judge 分")

# ---------- 可溯源证据 / Claim 验证 ----------
# 【工程补全,借鉴 DeepVerifier】模型判不要直接 judge 整段输出,而是先抽取
# claim,再逐 claim 对照 spec/input/upstream/grounding/tool/trace 证据核查。
class EvidenceRef(BaseModel):
    source_type: Literal["spec", "input", "upstream_output",
                         "grounding", "tool_result", "trace"]
    ref: str                              # JSON Pointer / URL / trace event id / spec clause id
    quote_or_hash: Optional[str] = None   # 短摘录或内容 hash,便于审计与去重
    trust_level: Literal["trusted", "verified", "untrusted", "tainted"] = "untrusted"
    taint_tags: list[str] = Field(default_factory=list)  # e.g. external_web / user_supplied / prompt_injection

class Claim(BaseModel):
    claim_id: str
    text: str
    source_path: str                      # 输出中的 JSON Pointer 或文本 span id
    required_evidence: list[EvidenceRef] = Field(default_factory=list)
    verification_status: Literal["unchecked", "supported",
                                 "contradicted", "insufficient"] = "unchecked"

class TestCase(BaseModel):
    case_id: str
    spec_id: str
    input_payload: dict
    assertions: list[str] = Field(default_factory=list)  # assertion_id 列表
    expected_partial_output: Optional[dict] = None
    source: Literal["planner", "verifier_generated",
                    "regression", "human"] = "planner"

class GoldenVerificationCase(BaseModel):
    case_id: str
    spec_id: str
    message: dict
    output: dict
    expected_ok: bool
    expected_failure_type: Optional[Literal["spec_adherence", "grounding", "contract"]] = None
    expected_subtypes: list[str] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)

class VerificationCoverage(BaseModel):
    schema_fields_total: int = 0
    schema_fields_checked: int = 0
    assertions_total: int = 0
    assertions_checked: int = 0
    claims_total: int = 0
    claims_verified: int = 0
    dag_edges_total: int = 0
    dag_edges_checked: int = 0
    tool_calls_total: int = 0
    tool_calls_gated: int = 0
    regression_cases_total: int = 0
    regression_cases_run: int = 0

class VerifierDisagreement(BaseModel):
    assertion_id: str
    votes_for: int = 0
    votes_against: int = 0
    failed_aspects: list[str] = Field(default_factory=list)
    resolution: Literal["majority", "priority",
                        "adjudicator", "human_review"] = "majority"

class VerifierHealth(BaseModel):
    backend: str
    version: str
    precision: float = 0.0
    recall: float = 0.0
    false_accept_rate: float = 1.0
    false_reject_rate: float = 1.0
    last_calibrated_at: Optional[str] = None
    enabled: bool = False

class MetamorphicRelation(BaseModel):
    relation_id: str
    spec_id: str
    transform: str                        # 输入变换描述/函数引用
    expected_relation: str                # 输出关系,如 invariant/equivalent/monotonic

class MutationCase(BaseModel):
    mutation_id: str
    spec_id: str
    mutation_type: Literal["drop_field", "wrong_value",
                           "forbidden_insert", "stale_source",
                           "off_by_one", "tainted_evidence"]
    expected_caught_by: list[str] = Field(default_factory=list)  # assertion/backend ids

# 顶层类型优先级:多 aspect/多检查同时失败时,取优先级最高者做路由(见 §3.7)
FAILURE_PRIORITY = [FailureType.CONTRACT,      # 分解/契约问题最该先处理
                    FailureType.GROUNDING,
                    FailureType.SPEC_ADHERENCE]

# 【工程补全】构造期 ConstructionVerifier 与执行期 RuntimeGate **共用**的验证结果。
# 两条轴都在这里:failure_type 管路由(轴 A)、feedback 管修复(轴 B),
# 与 FailureSignal 同构,保证 rubric 反馈能一路流到 refine,而非退化成扁平字符串。
class GateResult(BaseModel):
    ok: bool
    failure_type: Optional[FailureType] = None    # 轴 A
    feedback: list[StructuredFeedback] = Field(default_factory=list)  # 轴 B(同 FailureSignal)

class ToolGateResult(BaseModel):
    ok: bool
    tool_name: str
    stage: Literal["pre", "post"]         # 调用前 / 调用后
    gate_result: GateResult

class FailureSignal(BaseModel):
    spec_id: str
    failure_type: FailureType             # 轴 A:决定回退阶段(§6)
    feedback: list[StructuredFeedback] = Field(default_factory=list)  # 轴 B:决定怎么改(喂给 refine)
    pass_index: int = 1

# 【工程补全】ErrorAttributor 的产出、RecoveryRouter 的输入(见 §5)。
# 与 architecture.md / frontend.md 的核心模型同名,统一为 RecoveryAction。
class RecoveryAction(BaseModel):
    kind: Literal["local", "upstream", "structural"]
    target: Optional[str] = None          # local/upstream:责任 spec_id
    subgraph: list[str] = Field(default_factory=list)   # structural:受影响子图
    feedback: list[StructuredFeedback] = Field(default_factory=list)  # 带给重试的结构化反馈

class SurfaceFailure(BaseModel):
    reason: str
    gate_result: GateResult
    suggested_next_action: Literal["retry", "replan",
                                   "human_review", "abort"] = "abort"
    trace_summary_id: Optional[str] = None
```

> **【论文】** 关键点:验证不是返回布尔值,而是返回**带类型的失败信号**,类型直接决定回退到哪一阶段(见 §6 路由表)。这是"最小代价恢复"的实现基础。
>
> **【工程补全,借鉴 DeepVerifier / arXiv:2601.15808】** 失败信号有**两条正交轴**:`failure_type`(在哪修,管路由)与 `feedback[].subtype`(错的是什么,管 rubric 结构化反馈)。前者沿用论文,后者把原先的自由文本 `issues` 升级为**可溯源、含 expected/fix 的结构化修正**,提升 refine 命中率(详见 §5)。全部为**规则/spec 层**机制,**不涉及任何模型训练**(§7 非目标)。

---

## 3. 构造期(Phase 1)详细设计

总入口:`construct(task_description: str) -> ExecutableSwarm`。五个 Stage 串行,任一 Stage 的产物都要过验证才进入下一步。

**构造流水线(含新增 Schema 对齐检查)**:

```
Stage 1 (IntentParser) → load_constitution(domain) → Stage 2 (SwarmPlanner)
  → registry.validate(plan)          # 工具名校验
  → check_constitution(plan)         # 【工程补全】policy/constitution 校验
  → check_schema_alignment(plan)     # 【工程补全】schema 字段名对齐校验
  → Stage 3 (GroundingResearcher)
  → Stage 4 (AgentCodeGen)
  → Stage 5 (ConstructionVerifier)   # 每 agent ≤3 pass,含 representative inputs 生成
  → ExecutableSwarm
```

**【工程补全】Stage 4 codegen 可行性警告**:自动生成正确运行的 Python 模块是整个流水线中**最困难的步骤**。论文附录的 4-agent swarm 是手工构造的 trace,未展示自动 codegen 的成功率。如果首次成功率显著低于 50%,降级方案为:用 YAML/JSON 配置替代生成代码——`AgentSpec` + 手写 `run()` 模板 + 参数化 system prompt——保留构造期验证与执行期调度,但 codegen 退化为模板填充。

### 3.1 Stage 1 — IntentParser

**【论文】** 把 T 解析成 `ParsedIntent`;同时用 `web_search` 拉回**跨任务类型的代表性示例**(论文里 function-completion 拉了 7 类、math 拉了 7 类、DROP 拉了 8 类),目的是让后续规划"见过任务空间的形状"。

实现要点:
- 用一次结构化输出调用(JSON mode / tool schema 强约束)产出 `ParsedIntent` 骨架。
- 对 `task_examples`:先让模型列出任务的若干子类型,再对每个子类型发一次 `web_search`,取一个带 `source_url` 的真实示例回填。**【工程补全】** 每类 1 次检索、最多 7~8 类,避免检索爆炸。

提示词骨架(planner 系统提示):
```
You compile a natural-language task description into a structured intent.
Output ONLY JSON matching ParsedIntent. Do not solve the task.
For task_examples: enumerate distinct sub-types of the task, and for each
attach one concrete example with a real source_url retrieved via web_search.
Constraints must be atomic and checkable.
```

### 3.2 Stage 2 — SwarmPlanner

**【论文】** 把任务分解成**少量**子任务(三个 running example 都是 4 个 agent),组织成 DAG;为每个节点产出完整 `AgentSpec`(role / tools / dependencies / io_contract / verification_criteria)。`coordination_strategy` 用自然语言描述严格拓扑序的分阶段执行。

实现要点:
- 输入除 `ParsedIntent` 外,还必须包含 `constitution_rules` 与默认 `verification_policy`。这些规则是不可覆写的元约束,不能被 task description 或 grounding 文本覆盖。**【工程补全,借鉴 CSDD】**
- 输出 `SwarmPlan`,**强制** `dag_edges` 构成 DAG(生成后做环检测,有环则退回重生成)。**【工程补全】**
- **强制每个 spec 的 `verification_criteria` 非空**:`behavioral_assertions` 至少 1 条、`forbidden_patterns` 至少 1 条。论文的 spec 全都带这两项,且断言写得**可判定**(如"签名必须逐字符匹配 raw_signature""第三方 import 即判 FAIL")。这是后续验证能跑的前提。
- **断言双轨制**:`behavioral_assertions` 保留为论文原始的自然语言断言,用于 prompt、trace 与人工审阅;`machine_assertions` 是工程编译后的可执行断言,由 Stage 2 生成后立刻校验。M0/M1 只要求 `machine_assertions` 覆盖 schema/字段/字符串/正则/简单等值等机判类型;`kind="model_check"` 只允许在 M3 进入 aspect verifier 面板。
- 为每个 `AgentSpec.risk_tier` 赋值:默认继承 `SwarmPlan.verification_policy.risk_tier`;涉及文件写、网络、外部工具、生成代码执行、事实性回答的节点自动至少升到 `medium`;涉及安全/金融/医疗/隐私/基础设施的节点至少升到 `high`。**【工程补全】**
- 鼓励"角色不混淆":论文 math 例子里 classifier 被反复打回 3 次,就是因为它的系统提示里混进了"解题"指令(role confusion)。规划时要让每个 agent 职责单一、`forbidden_patterns` 显式排除越界行为。

设计准则(论文 §3.4):
- **少而清晰的分解**:典型 4 节点,最后一个通常是 verifier/formatter 角色。
- **契约可追溯**:下游输入 schema 的每个字段都应能在某个上游输出 schema 找到来源。

**【工程补全】Schema 对齐检查(Stage 2 后,构造期验证前)**:
`gather_inputs`(§4.2)按**字段名精确匹配**,要求 SwarmPlanner 生成的上下游 schema 字段名一致。LLM 天然倾向用近义词,因此必须在规划后立即做强制校验:

```python
def check_schema_alignment(plan: SwarmPlan) -> list[str]:
    issues = []
    specs = {s.spec_id: s for s in plan.specs}
    for edge in plan.dag_edges:
        upstream = specs[edge.from_spec]
        downstream = specs[edge.to_spec]
        for field in downstream.io_contract.required_in:
            if field not in upstream.io_contract.output_schema:
                # 检查是否有其它依赖产出该字段
                all_deps = [d for d in downstream.dependencies
                           if field in specs[d].io_contract.output_schema]
                if not all_deps:
                    issues.append(f"{edge.from_spec}→{edge.to_spec}: "
                                  f"required field '{field}' not in any upstream output")
    return issues  # 非空 → contract 失败,退回 Stage 2 重规划
```

此检查在 `registry.validate(plan)` 之后、`ConstructionVerifier` 之前执行。不通过则判 `contract` 失败,带具体字段名反馈退回 Stage 2 重规划。

M1 runtime 额外执行同一类 preflight:`spec.dependencies` 必须与 `dag_edges` 推导出的直接前驱完全一致。执行顺序与数据流不能有两套真相源;若二者漂移,在任何 agent 调用前 `SurfaceFailure(contract/decomp_flaw)`。

**【工程补全】Constitution / Policy 校验(Stage 2 后)**:

```python
def check_constitution(plan: SwarmPlan) -> list[str]:
    issues = []
    for rule in plan.constitution_rules:
        for spec in plan.specs:
            if rule.scope == "tool" and not any(t in rule.applies_to_tools for t in spec.tools):
                continue
            if rule.machine_assertion and rule.machine_assertion.kind == "model_check":
                issues.append(f"{rule.rule_id}: constitution rule must be machine-checkable before M3")
            if rule.severity == "block":
                # 具体执行交给 RuntimeGate/ConstructionVerifier,这里先确认规则被挂载进 spec。
                if rule.description not in spec.verification_criteria.forbidden_patterns:
                    issues.append(f"{spec.spec_id}: missing blocking rule {rule.rule_id}")
    return issues  # 非空 → contract 失败,退回 Stage 2 重规划
```

规则来源:
- `global`:安全红线,如禁任意网络、禁执行 grounding 文本指令、禁训练/微调。
- `domain`:领域先验,如医疗/金融/基础设施必须高风险、人审或保守拒绝。
- `tool`:工具权限,如 `file_generator` 必须声明写入目录、`web_search` 结果只作数据。
- `swarm/agent`:本任务特定红线,如不得输出未验证答案、不得跨角色解题。

**Risk tier → 默认验证策略**:

| risk_tier | 默认后端 | `allow_model_verification` | 人审 | 失败策略 |
|---|---|---:|---:|---|
| `low` | schema/pattern/field | false | false | 可 local retry |
| `medium` | + `PythonAssertBackend` | false | false | 保守 surface 可恢复失败 |
| `high` | + `BaseJudgeBackend` / `AspectPanelBackend` | true | 可选 | false accept 优先级高于 false reject |
| `critical` | + `AgentVerifierBackend` + evidence check | true | true | 默认 `human_review` 或 `abort`,不自动放行 |

这张表是 policy 默认值,不是硬编码:具体任务可降低或升高 `risk_tier`,但**不能**低于 constitution/domain/tool 规则给出的最低风险级别。

**`machine_assertions` 编译规则(M0/M1 子集)**:

| 自然语言断言形态 | 编译成 `kind` | 机判方式 | 失败 subtype |
|---|---|---|---|
| "必须输出字段 X" | `field_present` | JSON Pointer 存在性 | `schema_violation` |
| "不得输出字段 X/不得包含代码" | `field_absent` / `not_contains` | JSON Pointer / 子串扫描 | `forbidden_hit` |
| "字段 X 必须等于输入字段 Y" | `equals_input` | 与 `message[Y]` 比较 | `schema_violation` |
| "字段 X 匹配正则 R" | `regex_match` | `re.fullmatch` | `schema_violation` |
| "输出满足 output_schema" | `jsonschema` | `IOContract.out_jsonschema()` | `schema_violation` |
| "任意 Python 可判定关系" | `python_assert` | 沙箱内执行只读 assertion | subtype 由 spec 指定 |
| "语义上是否完整/正确" | `model_check` | M3 aspect panel | subtype 由 aspect 映射 |

断言编译失败本身也是 `contract` 问题:如果 planner 写出无法解析、无法定位字段、或依赖未知输入字段的断言,不要把它传给 RuntimeGate 硬跑,应退回 Stage 2 重写 spec。这样 M1 可以坚持"全部机判",同时不给 M3 的模型判残差留下含混入口。

### 3.3 Stage 3 — GroundingResearcher(设计期 grounding)

**【论文】** grounding 放在**构造期**而非执行期:对每个"需要外部知识"的 spec 发一条**定向检索 directive**(one search per agent),把结果(`research_summary` + 带 provenance 的 API/文档推荐)写回该 spec 的 `grounding` 字段。论文强调这能"在生成前就让 agent 拿到所需知识",显著降低执行期因信息缺失导致的验证失败。

实现要点:
- directive 由 spec 自动派生:`role + io_contract.description + tools` → 检索查询。
- 产出 `recommendations[]`(name / url / auth_method / relevance_score)和 `directive_results[]`(per-spec summary)。
- **【工程补全】** 对不需要外部知识的纯推理 agent(如论文 math 的 solver,tools=[]),跳过检索。

### 3.4 Stage 4 — AgentCodeGen

**【论文】** 把每个 spec 编译成一个**带标准接口 `run(message, history)` 的可执行 Python 模块**,内含该 agent 的系统提示和工具配置。注意:本阶段产物是"完整可执行的多 agent 系统",不是高层计划。

模块模板(生成目标):
```python
# generated/agents/{spec_id}.py
SYSTEM_PROMPT = """..."""            # 由 role + io_contract + grounding 合成
TOOLS = [...]                        # 由 spec.tools 映射成具体工具定义

def run(message: dict, history: list) -> dict:
    """
    message: {field: value} 满足 io_contract.input_schema
    返回: dict 满足 io_contract.output_schema
    """
    resp = llm_call(SYSTEM_PROMPT, message, history, tools=TOOLS)
    return parse_structured(resp, schema=OUTPUT_SCHEMA)
```

**【工程补全】** 工具映射层:把 spec 里抽象的 `"web_search"`、`"file_generator"` 映射成当前后端真正的工具定义(经 §3.6 注册表,`TOOLS = registry.schema_for(spec.tools)`)。论文 math 例子的 Pass 2 失败正是"web_search 用了 server-side 格式需要特定 SDK 处理"——所以工具配置必须由统一映射层产出,不能让模型自由发挥格式。

**【工程补全】AgentArtifact 四种实现形态**:

| `implementation_kind` | 用途 | 执行方式 | 进入阶段 |
|---|---|---|---|
| `fixture` | M0 手工 swarm / 单测 / 错误注入 | `handler_ref` 指向内置 deterministic handler | M0 |
| `prompt_template` | codegen 降级方案:不生成 Python 文件,只生成 system prompt + schema | 通用 `TemplateAgent.run()` 调 LLM | M2 兜底 |
| `python_module` | 论文目标形态:每个 spec 一个生成模块 | importlib 加载 `module_path:entrypoint` | M2+ |
| `external_agent` | control-plane 形态:节点交给外部 code agent 执行 | `adapter_name` 指向 `AgentRuntimeAdapter`(Claude Code/opencode/Pi/ACP/A2A) | M3+ |

这解决 M0 的边界问题:**M0 不手写 generated agent Python 模块**,而是手写 `SwarmPlan` 配置 + `fixture` artifacts,用内置 deterministic handler 模拟 4 个 agent 的输出。这样先验证 `Coordinator` / `ContextStore` / DAG / schema 路由,不把风险提前放到 codegen。M2 才把 `fixture` 替换为 `prompt_template` 或 `python_module`。

> **control-plane 说明**:`external_agent` 是 [architecture.md](architecture.md) 的核心形态——本引擎不自己实现节点,而是把受约束的节点任务委派给外部 code agent。无论哪种形态,产物都**必须过 `ConstructionVerifier`/`RuntimeGate`**:code agent 可以提出产物,但不能自己宣布成功(详见 architecture.md "责任分界")。adapter 协议细节不在本文档展开,真相源 schema 只需 `adapter_name` 这个挂载点。`ClaudeCodeAdapter`、`OpenCodeAdapter` 等 adapter 可以共享 worktree/subprocess/diff/trace 等无产品语义的执行基座,但各自的 CLI 参数、权限模型、事件解析、会话语义必须留在 provider-specific adapter 内,不能扩散进核心 schema。

**【工程补全,可选旋钮:BoN 候选选优,借鉴 BoN-MAV / arXiv:2502.20379】** 默认是"生成 1 个 → 构造期验证 → 失败带反馈顺序重试(≤3 pass)",但论文 math classifier 把 3 次 pass 用满,churn 重。可改为**并行 BoN**:一次生成 N 个候选实现 → 全部过 §3.7 多 aspect 构造期验证 → **按赞成数选最优**;只有最优仍不过才进入顺序 refine 循环。这是"**token 换往返次数与首过率**"的权衡旋钮(`N` 可配,默认 1 即退回顺序模式),BoN+多验证器的扩展性优于 self-consistency。

### 3.5 Stage 5 — ConstructionVerifier(构造期验证)

**【论文】** 对每个生成的 agent `âᵢ` 验证其是否满足 spec `σᵢ`,两类互补检查:

1. **静态验证(static)**:代码结构良好、必需接口齐全、能无运行时错误地实例化。
2. **行为验证(behavioral)**:由一个 verifier 模型在**代表性输入**上模拟执行 `âᵢ`,检查输出是否满足 I/O 契约和行为断言。

**【论文】** 失败返回**带类型**的信号 `f ∈ {spec_adherence, grounding, contract}`,每个类型路由到对应上游阶段(见 §6)。每个 agent 最多 **3 次验证 pass**(论文 math classifier 用满了 3 次)。

实现要点:
- 静态检查:`importlib` 试加载 + `inspect` 校验 `run` 签名 + AST 扫 `forbidden_patterns`(如禁止第三方 import 时,扫 import 节点)。
- 行为检查:为每个 `behavioral_assertion` 构造小输入,跑 agent,断言里能机判的(签名逐字符匹配、禁用 import)直接用代码判,省 token;**机判覆盖不到的语义断言走 §3.7 的多 aspect 验证器面板**(赞成聚合,结果仍映射成带类型失败信号)。**【工程补全】** 断言尽量"代码可判 > 模型判",降低成本与误判。
- **【工程补全】代表性输入生成**:行为验证需要"代表性输入"来模拟执行 âᵢ。输入从 `machine_assertions` / `behavioral_assertions` 自动派生为 §2 `TestCase`:每条 assertion 至少生成 1 个最小可行的输入用例(如"检查签名匹配"→ 传入符合 `input_schema` 的标准调用;"检查 forbidden_patterns"→ 传入故意触发禁止模式的输入)。这些用例由 `ConstructionVerifier` 自动构造(先用规则生成,必要时用 LLM 根据 assertion + `io_contract.input_schema` 生成),而非手工编写。用例覆盖度 = assertion 覆盖率;失败用例进入 regression set,供 M3 verifier calibration 复用。

### 3.6 工具注册表(Tool Registry,横切 Stage 3/4/执行期)

**【工程补全】** 论文反复点名"工具格式错误"是 Stage 4 的典型失败源(math Pass 2:`web_search` 用了 server-side 格式、需要特定 SDK 处理)。根因是**让模型自由拼工具定义**。本方案用一个**单一出口**的注册表消除这个自由度:`spec.tools` 全程只携带抽象名,真实定义只能从注册表取。

职责与接口:
```python
class ToolRegistry:
    def get(self, name: str) -> ToolDefinition: ...      # 抽象名 → 后端定义,缺失即 raise
    def schema_for(self, names: list[str]) -> list[dict] # codegen/executor 注入用的 backend_schema 列表
    def handler(self, name: str):                        # 执行期按 handler_ref 取真实可调用
    def validate(self, plan: SwarmPlan) -> list[str]     # 规划后即校验:所有 spec.tools 均已注册
```

三处接入点:
- **Stage 2 后**:`registry.validate(plan)` —— 任何 spec 引用了未注册工具,直接判 `contract` 失败回退重规划,**不让坏工具名流到 codegen**。
- **Stage 3(grounding)**:directive 派生时可参考 `ToolDefinition` 的 `requires_network/auth_method`,避免给纯本地工具发无谓检索。
- **Stage 4(codegen)**:模块里的 `TOOLS = registry.schema_for(spec.tools)`,**模型不接触工具格式**,只写"调用哪个工具名"的逻辑。
- **执行期**:工具调用经 `registry.handler(name)` 落地;沙箱依据 `requires_network` 决定是否放行出网、依据 `side_effects` 决定文件系统写权限(对齐 §7 安全红线)。
- **工具调用前后**:所有 handler 调用包在 §4.4 `PreToolGate` / `PostToolGate` 中,参数、权限、输出、taint、大小限制都产出 `ToolGateResult` 并写入 trace。

新增/变更工具只动注册表一处,生成代码与 spec 都无需改 —— 这也是"跨模型可迁移"(§9)的工程前提之一。

### 3.7 多 Aspect 验证器面板(MAV,**仅用于模型判残差**)

**【工程补全,借鉴 Multi-Agent Verification, arXiv:2502.20379】** 我们坚持"机判优先"(§3.5/§4.3):能用代码判定的断言(签名逐字符匹配、AST 扫 import、schema 校验)一律机判,确定性强、近乎零成本。但总有一部分语义类断言**只能靠模型判**。这部分目前的薄弱点是"**一个 verifier 模型给布尔**"。

MAV 的结论可直接补强这一残差:**多个多样化的 aspect verifier + 赞成投票**,比单一 verifier、甚至比 reward model 扩展性更好;且具备 **weak-to-strong**(用一组弱/便宜模型也能提升强生成器)。

设计:把模型判残差从"一个 verifier"换成"**一个 aspect 面板**",每个 aspect 一个独立 verifier 调用,按赞成数聚合;**但保留我们的类型化包裹** —— 不退化成纯布尔投票。

```python
# aspect → (顶层 failure_type 轴 A, 细粒度 subtype 轴 B)
ASPECT_MAP = {
    "correctness":        (FailureType.SPEC_ADHERENCE, FailureSubtype.SCHEMA_VIOLATION),
    "contract_adherence": (FailureType.CONTRACT,       FailureSubtype.FIELD_MISMATCH),
    "forbidden_pattern":  (FailureType.SPEC_ADHERENCE, FailureSubtype.FORBIDDEN_HIT),
    "role_confinement":   (FailureType.SPEC_ADHERENCE, FailureSubtype.ROLE_CONFUSION),
}

def panel_verify(output, criteria, aspect_models) -> GateResult:
    # 每个 aspect 独立投票,不赞成时同时给出 rubric 结构化反馈(轴 B)
    fb = []
    for aspect, (ftype, subtype) in ASPECT_MAP.items():
        verdict = aspect_models[aspect].approve(output, criteria, aspect=aspect)
        if not verdict.ok:                 # verdict 带 evidence/expected/fix(source-checkable)
            fb.append(StructuredFeedback(subtype=subtype, evidence=verdict.evidence,
                                         expected=verdict.expected, actionable_fix=verdict.fix))
    if not fb:
        return GateResult(ok=True)
    # 多 aspect 同时失败:failure_type 取 FAILURE_PRIORITY 中最高者(轴 A,不丢信息)
    failed_types = {ASPECT_MAP[a][0] for a in ASPECT_MAP
                    if any(f.subtype == ASPECT_MAP[a][1] for f in fb)}
    ftype = next(t for t in FAILURE_PRIORITY if t in failed_types)
    return GateResult(ok=False, failure_type=ftype, feedback=fb)
```

边界(**务必遵守**):
- **不替代类型化归因**:投票只增强"模型判残差"的可靠性,`failure_type` 仍由不赞成的 aspect 推导,三级归因(§5)与路由(§6)不变。这是我们相对 MetaGPT/AutoGen 的命根子,MAV 是 selection 扩展、不带类型信息,替代不了它。
- **不吃掉机判**:机判项(§3.5)继续走代码,只有机判覆盖不到的语义断言才进面板。
- 面板默认用**便宜档**模型(见 §7 验证器档位),用"多个便宜模型投票"替"一个贵模型判",成本与可靠性同时改善。

复用位置:**Stage 5 构造期行为验证**与 **§4.3 RuntimeGate** 的模型判残差都走这套面板。

**source-checkable 分解(借鉴 DeepVerifier / arXiv:2601.15808)**:每个 aspect 的判定都应分解为**可溯源核对的子问题** —— 每个子问题对应一个具体的 spec 子句(`io_contract` 某字段 / 某条 `behavioral_assertion` / `grounding` 某 provenance),verifier 只回答"输出在这一点上是否符合该 spec 子句"。好处:① 利用"验证比生成容易"的不对称性,把模糊的整体判断拆成一串好判的小判断;② 不赞成时天然带出 §2 `StructuredFeedback` 的 `evidence`(指向哪条 spec 子句)。这也给 **Stage 2 派生 `behavioral_assertions`** 一个准则:每条断言都应写成"可溯源到某 spec 子句、可被单点核对"的形式。纯 spec/规则层,**不涉及训练**。

---

## 4. 执行期(Phase 2)详细设计

```python
def execute(swarm: ExecutableSwarm, task_input: dict) -> dict:
    store = ContextStore()
    ready = topo_ready_nodes(swarm.dag)            # 入度为 0
    while not all_done(swarm):
        for spec_id in ready:
            try:
                inputs = store.gather_inputs(spec_id, swarm)   # 按 io_contract 组装
            except ContractMismatch as e:                      # 缺字段/歧义 = 分解缺陷
                RecoveryRouter.apply(RecoveryAction(kind="structural",
                    subgraph=affected_subgraph(spec_id, swarm)), spec_id, swarm, store)
                break
            y = swarm.agent(spec_id)(inputs, store.history(spec_id))

            gate = RuntimeGate.check(y, swarm.spec(spec_id).verification_criteria)
            if gate.ok:
                store.put(spec_id, y)                      # 仅验证通过才向下游传播
            else:
                action = ErrorAttributor.classify(spec_id, gate, store, swarm)
                RecoveryRouter.apply(action, spec_id, swarm, store)   # 见 §5
                break  # 重新计算 ready 集合
        ready = recompute_ready(swarm, store)
    return store.final_output(swarm)
```

### 4.1 Coordinator

**【论文】** 按 DAG 拓扑序调度,依赖满足即派发,输出存入 context store 并传给下游。**【工程补全】** 同层无依赖节点可并发;并发度做成可配置(默认串行以便调试,论文三个例子都是严格串行分阶段)。

执行前由 `ArtifactLoader` 把 `AgentArtifact` 统一加载成 `Callable[[dict, list], dict]`:

```python
class ArtifactLoader:
    def __init__(self, adapters: dict = None):
        self.adapters = adapters or {}        # adapter_name -> AgentRuntimeAdapter

    def load(self, artifact: AgentArtifact, spec: AgentSpec):
        if artifact.implementation_kind == "fixture":
            return fixture_registry[artifact.handler_ref]
        if artifact.implementation_kind == "prompt_template":
            return TemplateAgent(artifact.prompt_template).run
        if artifact.implementation_kind == "python_module":
            return import_entrypoint(artifact.module_path, artifact.entrypoint)
        if artifact.implementation_kind == "external_agent":
            adapter = self.adapters[artifact.adapter_name]   # Claude Code/opencode/Pi/ACP/A2A
            return lambda message, history: adapter.invoke(spec, message, history)
```

`Coordinator` 只看 callable,不关心 agent 是 fixture、prompt 模板、生成模块还是外部 code agent(adapter)。这样 M0/M2/M3 共享同一个执行期,**外部 agent 的输出和本地产物一样必须过 `RuntimeGate`**,不开后门。

M1 preflight:`ArtifactLoader.bind()` 必须先验证每个 `spec_id` 都有 artifact、无多余 artifact、fixture `handler_ref` 已注册、`python_module.module_path` 存在、`external_agent.adapter_name` 已注册。失败统一上浮为 `SurfaceFailure(contract/decomp_flaw)`,不允许运行期裸 `KeyError`。

### 4.2 ContextStore

**【论文】** in-memory context store 路由中间产物。**【工程补全】** 接口:`put(spec_id, output)` / `gather_inputs(spec_id, swarm)` / `history(spec_id)`。生产环境可换持久化后端以支持回放与调试(类似论文相关工作 AgentGit 的 branching/rollback 思路)。

**`gather_inputs` 字段映射算法**:目标是为下游 `spec_id` 组装一个满足其 `io_contract.input_schema` 的 `message`。映射的合法来源**仅限其直接依赖**(`spec.dependencies`)的已存输出,这与 §3.2"契约可追溯性"(下游每个输入字段都应能在某个上游输出找到来源)是同一约束的执行期落地。

```python
def gather_inputs(self, spec_id, swarm):
    spec = swarm.spec(spec_id)
    deps = spec.dependencies
    message, unresolved, conflicts = {}, [], {}
    for field in spec.io_contract.input_schema:           # 按字段名匹配
        sources = [d for d in deps
                   if self.has(d) and field in swarm.spec(d).io_contract.output_schema]
        if not sources:
            if field in spec.io_contract.required_in:     # 仅必填字段缺失才算缺
                unresolved.append(field)
            # 可选字段无来源 → 跳过,不报错
        elif len(sources) > 1:
            conflicts[field] = sources                    # 多个上游都产出同名字段 → 歧义
        else:
            message[field] = self.get(sources[0])[field]
    if unresolved or conflicts:
        # 不静默丢字段:抛结构性信号,交 ErrorAttributor 判 structural(契约/分解缺陷)
        raise ContractMismatch(spec_id, unresolved=unresolved, conflicts=conflicts)
    return message
```

**原始 task_input 的接线**(【工程补全】,由附录 A 实例暴露的设计点):`gather_inputs` 只从直接依赖取字段,但 `raw_signature` 这类**原始任务输入**不由任何 agent 产出。约定:`execute()` 启动时把 `task_input` 以**保留虚拟源 `__task_input__`** 登记进 `ContextStore`,并视为所有**入口节点**(入度 0)的隐式依赖;非入口节点若要用原始输入,必须由某个上游 agent 在其 `output_schema` 里**显式透传**(如 `spec_analyzer` 把 `raw_signature` 原样放进输出)。这样 DAG 仍是唯一数据流真相,task_input 也可追溯,不会变成隐式全局变量。

设计决策(均为 **【工程补全】**):
- **按字段名匹配**:依赖 Stage 2 规划时就让上下游 schema 字段名对齐(契约可追溯性);名字不齐属于规划缺陷,应在构造期就被 `contract` 验证拦下,而非执行期硬猜。
- **缺字段 / 歧义 → 不静默处理**:任一字段无来源或有多个来源,直接抛 `ContractMismatch`。它在 §5 归因里映射为 `missing_required_field(...)→ structural`(分解本身有缺陷),而不是让 agent 拿着残缺/猜测的输入去跑。
- **作用域限直接依赖**:不做跨层"全局变量池"式取值,避免隐式耦合绕过 DAG;需要某上游字段就必须在 `dependencies` 里显式声明,保持 DAG 是唯一的数据流真相。
- **可追溯**:`message` 每个字段都记录来源 `spec_id`,写入 trace,供 §5 upstream 归因快速定位责任上游。

### 4.3 RuntimeGate(执行期验证)

**【论文】** 公式 (2):中间输出 `yᵢ` 在传给下游前,验证 `yᵢ ∈ Cᵢ`,其中 `Cᵢ` 编码 schema 约束、行为断言、forbidden patterns。不通过则**不向下游传播**,并触发恢复。

实现:`schema 校验(机判,用 §2 `out_jsonschema()`)→ forbidden_patterns 扫描(机判)→ machine_assertions(机判优先)→ behavioral_assertions 中残留的 model_check(necessary 时走 §3.7 多 aspect 验证器面板)`,任一不过即返回 `GateResult(ok=False, ...)`。

> **【M0 实现发现】`forbidden_patterns` 是"全输出字符串扫描",会误命中合法的透传字段。** 例:`spec_analyzer` 透传 `raw_signature`(值形如 `"def has_close_elements(...)"`),若给它配 `forbidden_patterns=["def "]` 就会假阳性。结论:`forbidden_patterns` 只适合"整个节点输出都不该出现某模式"的粗粒度禁止;**需要字段级排除时,改用带 `target_path` 的 `not_contains` `AssertionSpec`(machine_assertion),只扫指定字段**。规划期(Stage 2)对会透传/携带代码的节点应优先用字段级 `not_contains`,不要把代码类模式塞进全局 `forbidden_patterns`。

**统一产物**:RuntimeGate 与构造期 ConstructionVerifier 都返回 §2 的 `GateResult` —— 同时带 `failure_type`(轴 A)与 `feedback: list[StructuredFeedback]`(轴 B)。**机判项失败也要产出 `StructuredFeedback`**(如 schema 校验失败 → `subtype=SCHEMA_VIOLATION`,`evidence` 指向具体字段、`expected` 取 `out_jsonschema` 该字段、`actionable_fix` 描述补法),不只机判面板。这样 rubric 反馈能一路流到 §5 的 refine,**不退化成扁平字符串**;`failure_type` 多个时按 `FAILURE_PRIORITY` 取最高者。`GateResult` 直接喂给 §5 的 `ErrorAttributor`。

### 4.4 Tool Gates(工具调用前后验证)

**【工程补全,借鉴 Claude Code Hooks / Agents SDK Guardrails】** agent 输出验证不够,工具调用本身也必须经过 gate。所有工具调用经 `ToolRegistry.handler(name)` 前后分别触发:

```text
PreToolGate:
  验证工具名已注册、参数满足工具 schema、side_effects 被 policy 允许、
  risk_tier 足够、目标路径/网络权限合法。

PostToolGate:
  验证工具返回 schema、异常、输出大小、taint 标签、prompt injection 风险,
  并把 tool result 标记成 EvidenceRef 或拒绝进入 ContextStore。
```

典型失败:
- 未注册工具 / 越权工具 → `contract` + `tool_misuse`
- 写入非授权路径 → `spec_adherence` + `tool_misuse`
- web_search 返回疑似提示注入 → `grounding` + `irrelevant_result` 或 tainted evidence
- 工具输出超限 → `spec_adherence` + `output_too_large`

`ToolGateResult` 与 `GateResult` 同构,也要进入 trace。工具 gate 失败时,不要让 agent 看到未经脱敏/净化的原始工具输出。

### 4.5 Verifier Backend Stack(验证后端栈)

**【工程补全】** 新增调研文档把验证能力分成三类:LLM-as-Judge、Structured/Code verifier、Agent-as-Verifier。工程实现上不要让这些类型散落在 `RuntimeGate` 与 `ConstructionVerifier` 内部,而是统一成一个后端协议:

```python
class VerifierBackend:
    name: str
    supports: set[str]                    # assertion kind 集合
    cost_tier: Literal["free", "cheap", "expensive"]

    def verify(self, assertion: AssertionSpec, *,
               spec: AgentSpec, message: dict,
               output: dict, trace: list) -> GateResult: ...
```

默认后端顺序:

| 后端 | 覆盖 | 默认阶段 | 备注 |
|---|---|---|---|
| `JsonSchemaBackend` | `jsonschema`, required fields | M0 | 纯机判,无 LLM |
| `PatternBackend` | `contains`, `not_contains`, `regex_match`, forbidden patterns | M0 | 纯机判 |
| `FieldRelationBackend` | `field_present`, `field_absent`, `equals_input` | M0 | 纯机判 |
| `PythonAssertBackend` | `python_assert` | M2 | 必须走沙箱,只读输入/输出;M1 未接入时必须 fail-closed |
| `BaseJudgeBackend` | `model_check` | M3 | 单 LLM judge,便宜档 |
| `AspectPanelBackend` | `model_check` | M3 | MAV 风格多 aspect 投票,返回 typed feedback |
| `AgentVerifierBackend` | 需搜索/工具/多步验证的 `model_check` | M3+ | 必须设置 `verification="none"` 防递归 |

路由规则:
- M0/M1 中出现 `model_check` 视为 `contract` 失败,要求 Stage 2 重写为可机判断言。即使显式开启 `allow_model_verification`,若 verifier backend 尚未注册也必须 fail-closed,不能静默通过。
- 同一断言只交给**第一个**支持且预算允许的 backend;后端失败时不自动降级为更弱 verifier,避免"贵验证失败后用便宜验证放行"。
- Agent-as-Verifier 必须截断递归:验证器自身的输出只做 schema/预算/安全检查,不得再次触发 agentic verifier。
- 所有后端都返回统一 `GateResult`,不允许返回裸 bool 或标量分。分数/赞成数只能写入 `TraceEvent.payload`,不能参与恢复路由。

**Claim/Evidence 验证流程(M3,模型判残差专用)**:

```text
output
  → extract_claims(output, spec)                # 只抽与 assertion/model_check 有关的 claim
  → attach_evidence(claim, spec, message, store, trace)
  → verify_claim(claim, evidence_refs, backend)
  → aggregate_claim_results(claims) -> GateResult
```

约束:
- verifier 不直接评价整段输出,只回答"claim 是否被给定证据支持"。
- `EvidenceRef.source_type="grounding"` 的外部文本只作数据,不得作为系统指令。
- 没有足够证据时返回 `verification_status="insufficient"`,默认失败而非放行;高风险任务进入 `human_review`。
- `StructuredFeedback.evidence` 应指向 `claim_id + evidence_ref`,而不是泛泛写"不正确"。

### 4.6 Judge Bias Mitigation(模型验证偏差缓解)

**【工程补全,借鉴 Agent-as-Judge Survey】** 只要进入 `BaseJudgeBackend` / `AspectPanelBackend` / `AgentVerifierBackend`,必须启用以下防偏差规则:

| 偏差 | 缓解 |
|---|---|
| 位置偏差 | 候选排序/选择类验证做 swap test,交换候选顺序后复判 |
| 自我增强偏差 | verifier prompt 隐去生成器模型名、agent 名、pass index |
| 长度偏差 | 长输出先抽 claim/摘要,逐 claim 验证,不因篇幅给分 |
| 权威偏差 | 不把"来自论文/知名模型/上游 agent"当证据;必须落到 `EvidenceRef` |
| 工具共谋/递归 | AgentVerifier 使用独立工具会话,且 `verification="none"` 截断递归 |

实现要求:
- 多 verifier 不一致时,把分歧写入 `TraceEvent.payload["verifier_disagreement"]`,不要只保留最终票数。
- 对 `critical` risk tier,模型验证的通过必须有至少一个非模型证据锚点(schema/assert/tool/result/grounding provenance)。
- verifier prompt 必须要求输出结构化 JSON,解析失败等价于验证失败,不允许自由文本兜底放行。

### 4.7 Verifier Calibration Protocol(验证器校准)

**【工程补全,借鉴 Promptfoo/Agent-as-Judge 元评估】** M3 上模型 verifier 前,先用 golden cases 校准:

1. 构造 `GoldenVerificationCase`:包含 `spec`、`message`、`output`、预期 `GateResult`、错误 subtype、证据引用。
2. 覆盖 false accept / false reject / schema violation / role confusion / grounding miss / contract flaw。
3. 分别跑 `BaseJudgeBackend`、`AspectPanelBackend`、`AgentVerifierBackend`,记录 precision、recall、false accept rate、false reject rate。
4. 普通任务优化 F1;高风险任务优先压低 false accept。
5. 校准结果写入 trace 与 release note;未达阈值的 backend 不进入 runtime hot path。

### 4.8 Verification Coverage(验证覆盖率)

**【工程补全】** Gate passed 只表示已执行的检查通过,不表示验证充分。每次构造期/执行期验证都应产出 §2 `VerificationCoverage`,回答"验了多少":

| 覆盖项 | 含义 |
|---|---|
| schema coverage | `output_schema` 字段中被实际检查的比例 |
| assertion coverage | `machine_assertions` / `behavioral_assertions` 被执行的比例 |
| claim coverage | 输出 claim 中被 evidence 核查的比例 |
| edge coverage | DAG 边上的字段流是否被验证 |
| tool coverage | 工具调用是否经过 pre/post gate |
| regression coverage | 历史失败是否有回归用例并被执行 |

默认策略:
- `critical` risk tier 不允许 `claims_verified < claims_total` 时自动放行。
- `schema_fields_checked < schema_fields_total` 属于 verifier 实现缺陷,不能静默忽略。
- coverage 写入 `TraceEvent.payload["coverage"]`,用于后续消融与 verifier health 分析。

### 4.9 Evidence Trust / Taint Model(证据可信度与污染标记)

**【工程补全,防 prompt injection / 证据污染】** `EvidenceRef` 带 `trust_level` 与 `taint_tags`:

| trust_level | 语义 |
|---|---|
| `trusted` | 本地 spec、输入、已验证上游输出、受控测试结果 |
| `verified` | 经独立工具或 verifier 核实的外部证据 |
| `untrusted` | 默认外部 web、用户提供文本、未校验工具输出 |
| `tainted` | 命中注入/过期/格式异常/来源不明等风险 |

规则:
- grounding/web_search 结果默认 `untrusted`,只能作为数据,不能作为指令。
- `critical` claim 不能只依赖 `untrusted` 或 `tainted` evidence。
- tainted evidence 可用于说明失败原因,但不能支持通过。
- 进入 prompt 的外部 evidence 必须脱指令化:只放在 data/evidence 区,不得进入 system instruction 区。

### 4.10 Disagreement Resolution(验证器分歧处理)

多 verifier 不一致不能只记录票数,必须决策:

| risk_tier | 默认分歧处理 |
|---|---|
| `low` | majority vote |
| `medium` | failure priority(`contract` > `grounding` > `spec_adherence`) |
| `high` | adjudicator verifier 复判 |
| `critical` | human review 或 abort |

`VerifierDisagreement` 写入 trace。若分歧集中在同一 aspect,说明 aspect prompt 或 evidence 不足,进入 verifier calibration queue。

### 4.11 Verifier Health / Drift Monitoring(验证器健康与漂移)

**【工程补全,借鉴持续校准】** 模型、prompt、工具、schema 版本都会漂移。每个 verifier backend 都维护 §2 `VerifierHealth`:

- 模型版本、prompt 版本、backend 版本变化后必须重跑 golden cases。
- `false_accept_rate` 超阈值时自动 `enabled=false`,不进入 runtime hot path。
- `false_reject_rate` 升高时降低自动恢复力度,避免无效 retry。
- health trend 写入 trace/metrics,供 dashboard 展示。

### 4.12 Metamorphic / Mutation Testing(属性与变异测试)

**Metamorphic testing** 用于检查输出是否满足不变量/等价关系,适合代码、数学、转换类任务:

```text
输入顺序变换后结果不变
等价表达式结果一致
边界值不崩溃
重复执行 deterministic
小扰动不改变核心结论
```

**Mutation testing** 用于验证 gate 本身是否能抓错:

```text
删掉 required field
改错一个边界条件
插入 forbidden pattern
替换 grounding source
制造 off-by-one
把 evidence 标成 tainted
```

指标:

```text
mutation_score = caught_mutations / total_mutations
```

如果 mutation_score 过低,说明 assertion / verifier aspect 不够强,不能把对应 gate 当成生产可用。

---

## 5. 三级错误归因与恢复

> **【工程补全,借鉴 DeepVerifier / arXiv:2601.15808】两条正交轴。** 一次失败要回答两个互不相同的问题:
> - **轴 A — locality「在哪修」**(本节,论文):local / upstream / structural → 决定**恢复路由**(重试谁/重跑谁/重规划哪块)。
> - **轴 B — content「错的是什么」**(§2 `FailureSubtype` + `StructuredFeedback`):细粒度子类 + rubric 结构化反馈 → 决定**怎么改**(喂给下一轮 refine 的 `evidence/expected/actionable_fix`)。
>
> 二者正交叠加:**轴 A 选恢复动作,轴 B 让该动作的反馈精准**。轴 B 纯属 spec/规则层(rubric 模板由 §2 子类派生),**不涉及模型训练**(§7 非目标)。下面是轴 A。

**【论文】** 给定 agent `aᵢ` 的失败,分三类,恢复成本随局部性递增:

| 错误类型 | 判定 | 恢复策略 | 成本 |
|---|---|---|---|
| **Local** | 输入正确但 `aᵢ` 输出错 | 带 verifier 反馈**重试同一 agent** | 最低 |
| **Upstream** | 失败源于某个依赖 | 定位责任上游 agent,**重跑该上游**,再重试 `aᵢ` | 中 |
| **Structural** | 任务分解本身有缺陷(如 I/O 契约错误) | **升级到构造期,重建受影响子图**(重新规划) | 最高 |

**【论文 §3.3 running example 的归因示例】**(function completion):
- synthesizer 用了 `≤` 而 analyst 从没标注严格不等 → **upstream**,带反馈重跑 analyst。
- analyst 标了但 synthesizer 忽略 → **local**,synthesizer 本地重试。
- planner 选了破坏配对顺序的排序算法 → **contract violation**,重新调用 planner。
- 没有任何现有 agent 能合理解决 → **structural**,coordinator 重新规划相关子图。

判定逻辑(可落地启发式):
```python
def classify(spec_id, gate, store, swarm):
    deps = swarm.spec(spec_id).dependencies
    # 1) 上游输出是否已违反其自身契约?→ upstream
    for d in deps:
        if store.has(d) and not RuntimeGate.recheck(store.get(d), swarm.spec(d)):
            return RecoveryAction(kind="upstream", target=d)
    # 2) 失败信号指向契约/分解不匹配(下游需要的字段上游根本没产出)→ structural
    if gate.failure_type == FailureType.CONTRACT or missing_required_field(gate, store):
        return RecoveryAction(kind="structural", subgraph=affected_subgraph(spec_id, swarm))
    # 3) 否则本地重试(带结构化反馈 §2 StructuredFeedback),超过上限再升级
    if local_retries(spec_id) < MAX_LOCAL_RETRIES:    # 【工程补全】默认 2
        return RecoveryAction(kind="local", target=spec_id, feedback=gate.feedback)
    return RecoveryAction(kind="structural", subgraph=affected_subgraph(spec_id, swarm))
```

**【工程补全】** 重试/重跑/重规划各设上限与全局预算(总 LLM 调用数 / 时间 / 成本),触顶则"surface the failure 而非给未验证答案"——这正是论文 math swarm 的 `coordination_strategy` 写明的兜底原则。

> **【M1 实现发现:upstream 归因需要模型判,纯机判抓不住】** 上面 classify 第 1 步"上游输出违反其自身契约 → upstream"在**全量 gate 的流水线 + 纯机判**下**几乎不可达**:任何"坏到 recheck 不过"的上游输出,早在它自己 `put` 时的 gate 就被拦下(判 local),根本不会留到下游来归因。而论文真正的 upstream 案例(analyst 用了 `≤`、synthesizer 据此出错)是**上游输出"过了自己的 gate 但语义错"**——机判检测不到"语义错",只有下游失败时回看才暴露。
>
> 结论:**机判版 upstream 归因只覆盖"上游输出自身非法";"上游过 gate 但语义错"这类必须靠模型判**(§3.7 aspect 面板 / §4.5 claim-evidence),据下游失败证据反向定位责任上游。因此 upstream 的**真实恢复路径属 M3**;M0/M1 在全机判下主要走 local 与 structural 两条。这印证了把"模型判残差"留到 M3 的分档是必要的,而非可选装饰。(实现中 M1 的 coordinator upstream 应用段已标 `pragma: no cover` 待 M3。)

**【工程补全,future-work,借鉴 DeepVerifier 的自动构建分类法】** 轴 B 的 `FailureSubtype` 初版为手工枚举;后续可从 trace(§7 可观测)里记录的失败聚类,**把高频新失败提炼成新子类 + 新 rubric 模板**,让分类法随运行增长。注意:这里的"进化"**纯粹在 spec/rubric 层**(增删枚举与文本模板),**不训练、不微调任何模型**(§7 非目标)。

---

## 6. 统一验证循环与失败路由

**【论文】** 闭环:`Generate → Verify → Attribute → Refine`,在构造期与执行期都跑。构造期失败的类型化路由:

| 失败类型 | 触发条件 | 回退到的阶段 |
|---|---|---|
| `spec_adherence` | 生成的实现不满足 spec(角色越界、未按 schema 输出) | Stage 4 **重新生成代码**(带结构化反馈) |
| `grounding` | 缺失/错误的外部知识 | Stage 3 **重跑 API research** |
| `contract` | I/O 契约本身有问题 | Stage 2 **重新规划架构** |

这套"类型化路由"是论文相对 MetaGPT(local 验证)/ AutoGen(post-hoc)/ self-reflection 的关键差异:**不做全局重算,只重算责任组件**。

---

## 7. 技术选型

| 层 | 选择 | 说明 |
|---|---|---|
| 编排语言 | Python 3.11+ | 【工程补全】引擎与生成模块同语言最省事;artifact 可为 fixture/prompt/python_module/external_agent 四形态 |
| LLM 后端 | 可插拔,默认 Anthropic API | **【论文】** 框架 executor-agnostic;论文用 GPT-4o-mini 做主对比、Claude Sonnet 4.6 把均分从 82.7 提到 87.9。各组件(planner/codegen/verifier/executor)可分别配模型 |
| 验证器档位 | **【工程补全】** 独立于生成器,默认便宜档(Haiku 级)| 借鉴 MAV 的 weak-to-strong:用一组**弱/便宜**模型组面板投票即可提升强生成器(§3.7);生成走强档、验证走便宜档,直接压低"验证开销大"风险(§10)|
| 结构化输出 | tool/JSON schema 强约束 | 所有 Stage 产物都按 Pydantic schema 校验,解析失败即重生成 |
| 代码沙箱 | **【工程补全】** gVisor / Firecracker microVM 或容器 + seccomp | 论文执行期把验证过的代码"在 sandboxed subprocess 跑隐藏单测";本方案要求强隔离 + 禁网(除显式 web_search)+ 超时 + 资源上限 |
| 工具层 | 统一工具注册表 | 把 `web_search` / `file_generator` 等抽象工具映射成后端真实定义,避免 Stage 4 的格式错误 |
| 存储 | MVP 用内存;生产用可回放事件日志 | 支持 trajectory 回放与调试 |
| 可观测 | 每个 Stage / agent / verification pass 全量 trace | 论文附录给的就是逐 stage JSON trace,直接作为日志格式 |

> 安全红线:生成代码默认**不可信**,必须沙箱执行;web_search/file_generator 之外不开放任意网络与文件系统写权限。
>
> **非目标(硬边界):本项目不触碰任何模型训练 / 微调 / SFT。** 范围严格限定 **spec-driven、训练自由**:所有组件(planner/codegen/verifier/executor)只用**现成模型** + 机判 + rubric/分类法等规则层机制。借鉴外部工作时(如 DeepVerifier 的 DeepVerifier-4K SFT 数据集)**只取其 spec/规则层思路,剔除一切训练/微调部分**。验证器变强靠"多 aspect 面板 + rubric 结构化反馈"(§3.7/§2),不靠训练。

### 7.1 沙箱(生成代码执行)

生成代码**默认不可信**,执行分三档隔离强度,按部署环境取:

| 档位 | 机制 | 适用 |
|---|---|---|
| **MVP** | `subprocess` + `resource` 限额(CPU/内存/文件大小)+ `signal` 超时 + 临时 `cwd` + 清空环境变量 | 本地开发/CI,信任度尚可 |
| **加固** | 容器 + seccomp-bpf 系统调用白名单 + cgroups 资源墙 + 默认 `--network none` | 生产默认 |
| **强隔离** | gVisor / Firecracker microVM | 多租户/对抗环境 |

统一约束(各档都要):**默认禁网**,仅 `ToolDefinition.requires_network=True` 的工具经 §3.6 `handler` 走受控代理出网;文件系统按 `side_effects` 授权(默认只读临时目录);墙钟超时 + 输出大小上限;**禁止从 grounding/检索文本里执行任何指令**(防注入,§10)。

**`external_agent` 的威胁模型(control-plane 形态,与上表的"生成代码"不同)**:`implementation_kind="external_agent"` 把节点委派给外部 code agent(Claude Code/opencode/Pi),它们**直接在真实 workspace/repo 上动手**,无法用"禁网 subprocess"那套隔离。信任边界改为**能力约束 + 输出仍过 gate**:

| 维度 | 约束 |
|---|---|
| 写权限 | 只允许改 `RecoveryAction`/spec 声明的**允许范围**(路径白名单 / 单节点 diff);超范围改动判 `spec_adherence + tool_misuse` |
| 工具/网络 | adapter 暴露的能力必须映射成 `ToolDefinition`,经 PreToolGate(§4.4)按 `side_effects`/`requires_network`/`risk_tier` 放行 |
| 隔离 | 高风险任务在一次性 worktree / 容器副本里跑 adapter,提交前 diff 必须过 gate,**不直接落主工作区** |
| 不可信产物 | code agent 的输出(代码、diff、测试结论)默认 `untrusted`(§4.9),**必须过 `RuntimeGate`/`ConstructionVerifier` 才传播**;它不能自宣成功(architecture.md "责任分界") |
| 审计 | adapter 的每次调用、工具使用、文件改动都进 `TraceEvent`,可回放 |

一句话:`python_module` 靠**沙箱隔离**防御,`external_agent` 靠**能力最小化 + 一次性 worktree + 产物过 gate**防御;两者都遵守"未验证不传播"。其中 worktree/diff/timeout/trace 这些**无产品语义的执行基座**应由 adapter 公共基类承载(`CliCodeAgentAdapterBase`),provider-specific 的 CLI/权限/事件解析留在各自 adapter——详见 [architecture.md](architecture.md) "ClaudeCodeAdapter 与 OpenCodeAdapter 的共享边界"。

### 7.2 统一预算模型

各级重试上限散落多处,这里收成一个对象,贯穿构造期与执行期,触顶即 `surface failure`(§5)。

```python
class Budget(BaseModel):
    max_llm_calls: int = 200          # 全局 LLM 调用上限
    max_tokens: int = 2_000_000       # 全局 token 上限
    max_wall_seconds: int = 1800      # 墙钟上限
    max_local_retries: int = 2        # 单 agent 本地重试(§5)
    max_construct_passes: int = 3     # 单 agent 构造期验证 pass(§3.5,论文上限)
    max_replans: int = 2              # structural 重规划次数
    bon_n: int = 1                    # §3.4 BoN 候选数,1=关闭

class BudgetMeter(BaseModel):         # 运行时累计,任一超限 → raise BudgetExceeded
    llm_calls: int = 0; tokens: int = 0; started_at: float = 0
```

所有重试/重跑/重规划在动作前 `meter.check(budget)`;agent 调用必须先 `reserve_run()` 预占预算,再执行外部 LLM/code agent,最后 `record_run(tokens=...)` 记录 token/耗时。超限**不再尝试**,直接 surface 当前最佳 `GateResult` 与失败原因,而非给未验证答案。

触顶输出统一为 §2 `SurfaceFailure`:低/中风险可建议 `retry` 或 `replan`;高风险建议 `human_review`;critical 默认 `abort` 或 `human_review`,不自动返回未验证答案。

### 7.3 Trace Schema(可观测,且为"分类法进化"提供数据)

逐 Stage / agent / verification pass 全量结构化 trace。它不仅是日志,还是 §5 future-work"`FailureSubtype` 随 trace 增长"的**数据源**,故需 schema 化。

```python
class TraceEvent(BaseModel):
    ts: str                           # ISO 时间戳
    phase: Literal["construct", "execute"]
    stage: str                        # e.g. "stage2_plan" / "runtime_gate"
    spec_id: Optional[str] = None
    event: Literal["start", "llm_call", "gate_result",
                   "recovery", "budget", "finish"]
    gate_result: Optional[GateResult] = None   # 含 failure_type + 结构化 feedback
    recovery_kind: Optional[str] = None        # local/upstream/structural
    tokens: int = 0; latency_ms: int = 0
    payload: dict = Field(default_factory=dict)  # 输入/输出摘要(脱敏)

class TraceSummary(BaseModel):
    trace_id: str
    covered_events: list[str]          # TraceEvent id 列表;实现时 event 需有稳定 id
    summary: str
    open_questions: list[str] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
```

一条任务的执行 = 一串 `TraceEvent`,可回放、可做消融对比、可聚类失败(`gate_result.feedback[].subtype` 是天然聚类键)。MVP 落地为 JSONL,生产换可回放事件日志(§7 存储行)。长轨迹进入 AgentVerifier 前先生成 `TraceSummary`,verifier 只能基于 summary + 可追溯 `EvidenceRef` 提问/核查,避免把数百万 token trajectory 直接塞进 judge。

### 7.4 Verification Function Registry

**【工程补全,借鉴 VERIMAP StructuredVerifier】** `AssertionSpec.kind` 不应散落在多个 if/else 里。实现一个注册表,把断言类型、执行后端、沙箱需求和版本绑定:

```python
class VerificationFunction(BaseModel):
    name: str
    assertion_kind: str
    backend: str                         # e.g. JsonSchemaBackend / PythonAssertBackend
    input_contract: dict = Field(default_factory=dict)
    output_contract: dict = Field(default_factory=dict)
    deterministic: bool = True
    sandbox_required: bool = True
    version: str = "v1"

class VerificationFunctionRegistry:
    def get(self, assertion_kind: str) -> VerificationFunction: ...
    def validate_assertions(self, plan: SwarmPlan) -> list[str]: ...
```

M0/M1 注册 `jsonschema` / `field_present` / `field_absent` / `equals_input` / `contains` / `not_contains` / `regex_match`;M2 注册带沙箱的 `python_assert`;M3 注册 `model_check`。新增 `ast_no_imports`、`pytest_passes`、`mypy_clean`、`symbolic_check` 等能力时只扩注册表,不改 `RuntimeGate` 主流程。

### 7.5 外部工具采用边界

**【工程补全】** `docs/analysis/open-source-tools.md` 提供工具候选,但核心 runtime 不绑定重型框架:

| 阶段 | 默认依赖 | 可评估但不进入核心热路径 |
|---|---|---|
| M0/M1 | Pydantic / jsonschema / pytest | Guardrails AI、PydanticAI 暂不引入 |
| M2 | Instructor 可作为 Stage 结构化输出 adapter | 不绑定单一 provider;保留原生 JSON schema 路径 |
| M3 | Promptfoo 用于 verifier calibration | 不在 runtime gate 实时调用 Promptfoo |
| 本地模型实验 | Outlines 可用于 constrained generation | 不作为云 API 默认路径 |

原则:外部库只能作为 adapter/backend,不能成为 spec 真相源;`IOContract`、`AssertionSpec`、`GateResult` 仍由本仓库 schema 定义。

> 全实现栈(非仅验证)的开源重用分级、按 Layer/里程碑的引入时机、以及"编排框架为何不作核心"的论证,见 [implementation-stack.md](implementation-stack.md)。

---

## 8. 分阶段落地路线

> **排期原则**:先打平论文 baseline(M0–M2,只用论文机制 + 必要的可机判收口),**再叠加 MAV/DeepVerifier 增强(M3)**。aspect 面板(§3.7)、BoN 旋钮(§3.4)、轴 B 的 rubric 反馈属于"锦上添花",**不进 PoC 前期**,避免一上来背全套。

- **Milestone 0 — 骨架与 schema(1~2 周)**  
  - 落地 §2 全部 Pydantic 模型(含**可机判的 `IOContract`/`FieldSpec`/`out_jsonschema`**、统一 `GateResult`);搭 `Coordinator` + `ContextStore`(含 `gather_inputs` §4.2)+ 拓扑执行 + DAG 环检测。  
  - **明确"手写 swarm"的含义**:手工构造 `SwarmPlan` 的 JSON/YAML 配置(含 4 个 `AgentSpec` + DAG 边 + I/O 契约 + 验证标准)和 `implementation_kind="fixture"` 的 `AgentArtifact`,然后让 `ArtifactLoader` + `Coordinator` + `ContextStore` 加载并执行,而非手写 generated agent Python 模块。**完整可抄的种子见[附录 A](#附录-arunning-example--function-completion-4-agent-swarm)**(4 个 AgentSpec + DAG + fixture + gate 验收已写全)。验收:`has_close_elements` 例子端到端 PASS。  
  - **推荐初始包结构**:`src/meta_agent/schemas.py`(§2)、`dag.py`(环检测/拓扑序)、`artifacts.py`(`ArtifactLoader`)、`context.py`、`coordinator.py`、`runtime_gate.py`(先放 schema/pattern/machine assertion 机判骨架)、`verification_registry.py`、`fixtures/function_completion.py`。测试对应放在 `tests/test_*.py`。
  - **M0 明确不做**:不接真实 LLM、不做 Stage 1→5 自动构造、不跑模型 verifier、不实现恢复路由。M0 只证明"合法 ExecutableSwarm 可按 DAG 和契约稳定执行"。  
  - **快速原型:codegen 可行性验证** —— 手写 10 个 `AgentSpec`(覆盖 code/math/reasoning),测试自动 codegen 的首次成功率。如果 <50%,考虑降级方案(YAML 配置替代 Python 代码生成)。

**Milestone 1 — 执行期验证 + 三级归因 + 结构化反馈(1~2 周)**
- 实现 `RuntimeGate`(schema/forbidden/`machine_assertions` 三检,**全部机判**)+ `ErrorAttributor` + `RecoveryRouter`;`python_assert` 在 M1 fail-closed(沙箱 backend 推迟到 M2),`model_check` 默认拒绝,且 backend 缺失时即使策略开启也不得静默通过。
- 实现 `PreToolGate` / `PostToolGate` 的最小机判版本:工具名、参数 schema、side_effects、输出大小、taint 标签。
- 落地**两条轴**:`GateResult` 产出 `failure_type`(轴 A)+ `StructuredFeedback`(轴 B,先做机判项的 evidence/expected/fix);local 重试消费结构化反馈;预算触顶输出 `SurfaceFailure`。
- 产出 `VerificationCoverage`,至少覆盖 schema/assertion/tool 三类覆盖率。
- 用论文 §3.3 的四个归因场景做单测(local / upstream / contract / structural 各一)。验收:四类都能被 `classify` **正确分类**;**local 与 structural 走完整 E2E 恢复**,**upstream 在 M1 仅单测 `classify`**(E2E 的 upstream 恢复需模型判,属 M3——见 §5「M1 实现发现」)。
- 建 `GoldenVerificationCase` 最小集,覆盖 schema violation / forbidden hit / field mismatch / timeout,为 M3 校准留基线。

**Milestone 2 — 构造期全流水线 + 工具注册表(2~3 周)**
- 依次实现 Stage 1→5;构造期验证产出 `GateResult` 并按 §6 路由;每 agent ≤3 pass。
- 落地**工具注册表**(§3.6):`registry.validate(plan)` 在 Stage 2 后拦截未注册工具;Stage 4 工具定义只从注册表出。
- 验收:给一段全新的编码任务描述(不在示例里),自动产出可执行 swarm 并通过构造期验证。

**Milestone 3 — MAV/DeepVerifier 增强 + 评测、加固(2~4 周)**
- **增强(baseline 打平后才上)**:§3.7 多 aspect 面板(模型判残差,便宜档)+ claim/evidence source-checkable 分解;§3.4 BoN 候选选优旋钮;rubric 反馈扩到语义断言;轴 B 子类随 trace 增长(§5 future-work)。
- 启用 Judge Bias Mitigation 与 Verifier Calibration Protocol:用 golden cases 测 precision/recall/false accept/false reject,未达阈值的模型 verifier 不进入 runtime hot path;维护 `VerifierHealth` 与 drift 监控。
- 启用 `VerifierDisagreement` 分歧处理、evidence trust/taint policy、metamorphic testing 与 mutation testing,并报告 mutation_score。
- 接论文 6 个 benchmark 跑分;做消融;加成本/时间预算与兜底(§7 预算模型);加沙箱加固与并发。
- 验收:HumanEval/MBPP 达到与论文同量级(§9);**消融自检**——关掉 §3.7 面板,验证"验证仍是承重组件"的趋势。

---

## 9. 评测与验收

**【论文】** 评测协议follow AFlow;6 个 benchmark:
- 代码:HumanEval(pass@1)、MBPP(pass@1)
- 数学:GSM8K、MATH(solve rate)
- 阅读理解:HotpotQA、DROP

**【论文】参考分数(GPT-4o-mini executor)**:Meta-Agent 均分 82.7,六项中五项最优(仅 HotpotQA 略低于 AFlow);MATH 较 AFlow +13.4 是最大增益。换 Claude Sonnet 4.6 后均分升到 87.9 且无单项回退——说明构造出的 workflow **跨模型可迁移、不需重调**,这也应作为本方案的回归验收项。

**【论文】消融(DROP,逐个移除构造期组件)**:
- 去 verification:−7.1(最重)
- 去 API research(grounding):−5.5
- 去 planning:−3.5
- 去 prompt analysis:−2.4

**验收指标(本方案)**:

*结果类(对齐论文)*
1. 任务成功率(对齐上表)。
2. 错误恢复率:注入中间错误后仍成功完成的比例。
3. 工作流稳定性:long-horizon 任务的级联失败率。
4. 成本:每任务 LLM 调用数 / token / 墙钟时间(论文各 Stage 耗时 100~700s 量级,可作上界参考)。
5. **消融自检**:本地复现"去验证掉 ~7 分"的趋势,验证"验证是承重组件"而非摆设。

*验证质量类(本方案核心差异化必须自测,对应 §4.7–4.12)*
6. **gate 强度** —— `mutation_score`(§4.12):注入 mutation 后被 gate 抓住的比例;过低说明 assertion/aspect 形同虚设。
7. **判官质量** —— 模型 verifier 的 precision / recall / **false_accept_rate** / false_reject_rate(§4.7 golden cases 校准);高风险任务优先压低 false accept,未达阈值的 backend 不进 hot path。
8. **验证充分性** —— `VerificationCoverage`(§4.8):schema/assertion/claim/edge/tool/regression 覆盖率;"gate passed"必须配 coverage,否则只是验了局部。
9. **归因准确率** —— 注入已知错误,`ErrorAttributor` 把它判成正确 locality(local/upstream/structural)的比例 + 选对责任 `target` 的比例。
10. **判官稳健性** —— `VerifierHealth` 漂移监控(§4.11):模型/prompt/schema 版本变更后 false_accept_rate 的变化;swap-test 偏差检测(§4.6)通过率。

> 取舍:6~10 是本方案相对论文 baseline 的"承重组件自检"。即便结果类指标(1~5)持平论文,若 mutation_score / false_accept 不达标,也判**验证不可生产**——因为这套系统卖点正是"可信验证",而非分数本身。

---

## 10. 风险与规避

| 风险 | 来源 | 规避 |
|---|---|---|
| 验证开销大 | **【论文 §3.4】** 每个中间结果都要验 | 断言"机判优先于模型判";只在 necessary 时调 verifier 模型;模型判残差用**便宜档 aspect 面板**(§3.7/§7,weak-to-strong)替单一贵模型;同层并发 |
| 角色混淆(role confusion) | 论文 math classifier 被打回 3 次 | 规划期强制单一职责 + 充分的 `forbidden_patterns`;构造期行为验证专门查越界 |
| 工具格式错误 | 论文 math Pass 2 失败 | 统一工具注册表产出格式,模型不自由拼工具定义 |
| 生成代码不可信 | 自动生成 + 执行 | 三档隔离沙箱、禁网、超时、资源上限(§7.1) |
| 提示注入 | web_search / 文档 grounding 引入外部文本 | grounding 内容只作"数据"不作"指令";检索结果不进系统提示的指令区(§7.1) |
| 无限重试/成本失控 | 闭环回退 | 统一 `Budget`/`BudgetMeter`(§7.2):各级重试上限 + 全局预算;触顶 surface failure 而非给未验证答案 |
| 模型 judge 偏差 | 位置/长度/权威/自我增强偏差 | §4.6 Judge Bias Mitigation:swap test、匿名化、claim/evidence 验证、分歧入 trace |
| 验证覆盖不足 | gate passed 但只验了局部字段/断言 | §4.8 `VerificationCoverage`:schema/assertion/claim/edge/tool/regression coverage 入 trace |
| 工具调用绕过验证 | agent 在工具调用时越权或引入污染输出 | §4.4 PreToolGate/PostToolGate:参数、权限、side_effects、taint、输出大小双向检查 |
| 证据污染 | 外部 web/user/tool 文本被当成可信依据或指令 | §4.9 Evidence trust/taint:外部证据默认 untrusted,tainted 不支持通过 |
| verifier 漂移 | 模型/prompt/tool 版本变化导致 false accept 上升 | §4.11 VerifierHealth:golden cases 校准、漂移监控、超阈值自动禁用 |
| gate 过弱 | assertion 看似存在但抓不住典型错误 | §4.12 mutation testing:用 mutation_score 评估 gate 强度 |
| policy 只写不执行 | 安全红线停留在文档层 | §2 `ConstitutionRule` + §3.2 `check_constitution(plan)`,把红线变成 planner/verifier 消费的 spec |
| 外部框架锁死核心 | Guardrails/PydanticAI/Promptfoo 等能力诱人但重 | §7.5:只作为 adapter/backend,不成为 spec 真相源或 runtime hot path 默认依赖 |
| 弱于专家手工系统 | **【论文 Limitations】** 全自动 vs 专家先验 | 预留"轻量领域先验"注入点(论文 future work 方向):允许人工为特定 domain 追加 spec 模板/断言 |

---

## 附录 A:Running Example —— function-completion 4-agent swarm

**【工程补全】** 把论文 function-completion 例子(HumanEval `has_close_elements(numbers, threshold)`)用现行 schema **完整实例化**一次。目的有三:① 压测 §2 schema 是否够用(本附录就暴露了"task_input 接线"这个设计点,已回填 §4.2);② 给 **M0** 一份可直接抄的种子(M0 = 手写此 `SwarmPlan` + `fixture` artifacts 跑通执行期);③ 让满纸抽象第一次落地。

### A.1 DAG 与契约可追溯性

```text
__task_input__ {raw_signature, docstring}
        │(入口隐式依赖)
        ▼
  spec_analyzer ──────────────┐──────────────┐
   out: parsed_spec,          │              │
        raw_signature(透传)   ▼              ▼
                         algo_planner   code_synthesizer ──► code_verifier
                          out: approach   out: candidate_code   out: final_code, passed
```

边集(每条下游输入字段都能在某上游输出找到来源,满足 §3.2):

```python
dag_edges = [
    DagEdge(from_spec="spec_analyzer",   to_spec="algo_planner"),
    DagEdge(from_spec="spec_analyzer",   to_spec="code_synthesizer"),
    DagEdge(from_spec="algo_planner",    to_spec="code_synthesizer"),
    DagEdge(from_spec="spec_analyzer",   to_spec="code_verifier"),
    DagEdge(from_spec="code_synthesizer",to_spec="code_verifier"),
]
```

### A.2 四个 AgentSpec(节选关键字段)

```python
spec_analyzer = AgentSpec(
    spec_id="spec_analyzer", role="把签名+docstring解析成结构化规格,不写代码",
    dependencies=[], risk_tier="low",
    io_contract=IOContract(
        input_schema={"raw_signature": FieldSpec(type="string", description="函数签名"),
                      "docstring":     FieldSpec(type="string", description="自然语言描述")},
        output_schema={"raw_signature": FieldSpec(type="string", description="原样透传给下游"),
                       "parsed_spec":   FieldSpec(type="object", description="edge_cases/约束/严格不等标记")},
        required_in=["raw_signature", "docstring"],
        required_out=["raw_signature", "parsed_spec"],
        description="signature+docstring → 结构化 spec"),
    verification_criteria=VerificationCriteria(
        behavioral_assertions=["必须标注阈值比较是否为严格不等(< vs <=)"],
        machine_assertions=[
            AssertionSpec(assertion_id="sa1", kind="field_present",
                          target_path="/parsed_spec/inequality_strict",
                          description="必须显式给出严格不等标记",
                          failure_subtype="schema_violation"),
            AssertionSpec(assertion_id="sa2", kind="equals_input",
                          target_path="/raw_signature", expected="$input.raw_signature",
                          description="raw_signature 必须原样透传",
                          failure_type="contract", failure_subtype="field_mismatch")],
        # 【M0 修正】原 forbidden_patterns=["def ","return ["] 会误命中**透传的**
        # raw_signature(其值含 "def "),见 §4.3。spec_analyzer 输出是结构化 parsed_spec
        # + 透传签名,没有可塞代码的自由文本字段,故不配代码类 forbidden;只留粗粒度
        # 代码围栏禁止。若某节点确有自由文本输出字段要禁代码,用字段级 not_contains。
        forbidden_patterns=["```"]),
)

algo_planner = AgentSpec(
    spec_id="algo_planner", role="据 parsed_spec 选算法与遍历顺序,不写最终代码",
    dependencies=["spec_analyzer"], risk_tier="low",
    io_contract=IOContract(
        input_schema={"parsed_spec": FieldSpec(type="object", description="来自 analyst")},
        output_schema={"approach": FieldSpec(type="object",
                          description="algorithm + ordering_note + 是否保配对顺序")},
        required_in=["parsed_spec"], required_out=["approach"],
        description="parsed_spec → approach"),
    verification_criteria=VerificationCriteria(
        behavioral_assertions=["所选算法不得破坏阈值配对的成立条件"],
        machine_assertions=[AssertionSpec(assertion_id="ap1", kind="field_present",
                          target_path="/approach/algorithm", description="必须给出算法选择")],
        forbidden_patterns=["import "]),
)

code_synthesizer = AgentSpec(
    spec_id="code_synthesizer", role="据 spec+approach 写候选实现",
    dependencies=["spec_analyzer", "algo_planner"], risk_tier="medium",
    io_contract=IOContract(
        input_schema={"raw_signature": FieldSpec(type="string", description="透传自 analyst"),
                      "parsed_spec":   FieldSpec(type="object", description="来自 analyst"),
                      "approach":      FieldSpec(type="object", description="来自 planner")},
        output_schema={"candidate_code": FieldSpec(type="string", description="完整函数实现")},
        required_in=["raw_signature", "parsed_spec", "approach"],
        required_out=["candidate_code"],
        description="spec+approach → candidate_code"),
    verification_criteria=VerificationCriteria(
        behavioral_assertions=["实现必须使用 analyst 标注的严格不等语义"],
        machine_assertions=[
            AssertionSpec(assertion_id="cs1", kind="regex_match",
                          target_path="/candidate_code", expression=r"def\s+has_close_elements",
                          description="必须定义目标函数",
                          failure_subtype="schema_violation")],
        forbidden_patterns=["import os", "subprocess", "open("]),   # §7.1 安全红线
)

code_verifier = AgentSpec(
    spec_id="code_verifier", role="对照 spec 校验候选代码并定稿(verifier/formatter)",
    dependencies=["spec_analyzer", "code_synthesizer"], risk_tier="medium",
    io_contract=IOContract(
        input_schema={"candidate_code": FieldSpec(type="string", description="来自 synthesizer"),
                      "raw_signature":  FieldSpec(type="string", description="透传自 analyst"),
                      "parsed_spec":    FieldSpec(type="object", description="来自 analyst")},
        output_schema={"final_code": FieldSpec(type="string", description="定稿代码"),
                       "passed":     FieldSpec(type="boolean", description="是否通过自检")},
        required_in=["candidate_code", "parsed_spec"], required_out=["final_code", "passed"],
        description="candidate_code+spec → final_code"),
    verification_criteria=VerificationCriteria(
        behavioral_assertions=["final_code 行为必须满足 parsed_spec 的全部 edge_cases"],
        machine_assertions=[
            AssertionSpec(assertion_id="cv1", kind="python_assert",
                          target_path="/final_code",
                          expression="'def has_close_elements' in output['final_code']",
                          description="定稿必须含目标函数(沙箱内只读判定)",
                          failure_subtype="schema_violation")],
        forbidden_patterns=["import os", "subprocess"]),
)
```

### A.3 SwarmPlan + M0 fixture artifacts

```python
plan = SwarmPlan(
    swarm_name="humaneval_has_close_elements",
    summary="signature→spec→approach→code→verified code 的 4 节点严格拓扑序",
    coordination_strategy="严格串行:analyst → planner → synthesizer → verifier",
    specs=[spec_analyzer, algo_planner, code_synthesizer, code_verifier],
    dag_edges=dag_edges,
    verification_policy=VerificationPolicy(risk_tier="medium",
        allow_model_verification=False),   # M0/M1:全机判,不开模型判
)

# M0:不生成 Python 模块,用 fixture(确定性 handler)模拟四个 agent 的输出
artifacts = {
    "spec_analyzer":    AgentArtifact(spec_id="spec_analyzer",    implementation_kind="fixture",
                                      handler_ref="fx_spec_analyzer",    passed=True),
    "algo_planner":     AgentArtifact(spec_id="algo_planner",     implementation_kind="fixture",
                                      handler_ref="fx_algo_planner",     passed=True),
    "code_synthesizer": AgentArtifact(spec_id="code_synthesizer", implementation_kind="fixture",
                                      handler_ref="fx_code_synthesizer", passed=True),
    "code_verifier":    AgentArtifact(spec_id="code_verifier",    implementation_kind="fixture",
                                      handler_ref="fx_code_verifier",    passed=True),
}
swarm = ExecutableSwarm(plan=plan, artifacts=artifacts)
# ArtifactLoader(§4.1)把 fixture 装配成 callable 填进 swarm._loaded
```

### A.4 一次执行的 gate 验收(端到端)

> 纯机判项(`sa1/sa2/ap1/cs1` + forbidden 扫描)M0/M1 即可跑;`cv1` 是 `python_assert`,按 §4.5 属 M2 沙箱后端——M0/M1 临时降级为 `contains`/`regex_match` 等价检查,直到沙箱 backend 接入后再换回 `python_assert`。

```text
task_input = {raw_signature:"def has_close_elements(numbers: List[float], threshold: float) -> bool",
              docstring:"...任意两数差小于 threshold 则 True..."}

1. spec_analyzer  ← __task_input__         gate: sa1(严格不等标记present)✓ sa2(raw_signature透传)✓
2. algo_planner   ← parsed_spec            gate: ap1(algorithm present)✓
3. code_synthesizer ← raw_signature+parsed_spec+approach
                                           gate: cs1(def 正则)✓ forbidden(无 import os)✓
4. code_verifier  ← candidate_code+parsed_spec
                                           gate: cv1(regex 等价机判;M2 可换 python_assert 沙箱)✓ → final_code, passed=True
→ store.final_output(swarm) = {final_code, passed:True}   # has_close_elements 端到端 PASS
```

**注入错误自检(M1 用)**:把 `fx_spec_analyzer` 改成不输出 `inequality_strict` → `sa1` 失败,`failure_type=spec_adherence` / `subtype=schema_violation` → `ErrorAttributor` 判 **local**(输入对、本节点输出错)→ 带 `StructuredFeedback` 本地重试。

> **【M0 实现修正】** 原文曾写"把 `fx_spec_analyzer` 丢掉 `raw_signature` 透传 → `code_synthesizer` 的 `gather_inputs` 抛 `ContractMismatch` → structural"。实测**不成立**:`raw_signature` 在 `spec_analyzer` 的 `required_out` 里,丢失会被 **`spec_analyzer` 自己的 gate**(schema required + `sa2` equals_input)先抓住,在源头就 `SurfaceFailure`,根本到不了下游。**真正触发下游 `gather_inputs` 的 `ContractMismatch`(structural)需要"契约错配":下游必填字段不在任何上游 `output_schema`/`required_out` 里**——即分解缺陷,而非运行期某个 agent 漏吐一个它本应吐的字段。这条已由 `test_gi08`(字段只存在于非依赖上游 → unresolved)单测覆盖。两类归因(local / structural)仍可做 M1 单测,但 structural 要用"契约错配"而非"丢透传"来构造。

> 这个实例已验证现行 schema 足以表达论文 running example;唯一暴露的缺口(原始 task_input 如何到达非入口节点)已在 §4.2 收口。

---

## 附:与相邻工作的边界(便于技术汇报)

- **MetaGPT**:预定义 workflow + 仅 local 验证 → Meta-Agent 是**按任务合成**结构 + 构造期&执行期统一验证。
- **AutoGen**:对话式图 + post-hoc 验证 → Meta-Agent 验证前置到"执行开始之前"。
- **VeriMAP(2510.17109)**:把验证函数嵌入已实例化的规划图 → Meta-Agent 额外在**构造期**就验 spec/tool/依赖,并支持类型化重建。
- **AFlow**:自动 workflow 生成(本论文主基线)→ Meta-Agent 在五项 benchmark 上更优且无需迭代式 workflow 优化。
- **MAV / BoN-MAV(2502.20379)**:沿"验证器数量"做 test-time 扩展,多 aspect 验证器**赞成投票选最优候选**(selection)→ 本方案**借用**其多验证器/weak-to-strong/BoN 思路加固"模型判残差"(§3.7)与降本(§7),但**不采纳**其纯投票聚合替代我们的**类型化归因**:MAV 给票数、不给错误类型,无法驱动 §5/§6 的最小代价路由。即 MAV 解决"选哪个最好",Meta-Agent 解决"错在哪、回退到哪"。
- **DeepVerifier(2601.15808)**:针对 deep-research agent,用**失败分类法 + rubric 结构化反馈 + source-checkable 分解**做测试期自我细化(单生成者迭代)→ 本方案**借用**其三项机制补齐失败信号的**内容轴**(§2 `FailureSubtype`/`StructuredFeedback`、§3.7 source-checkable 分解、§5 两条正交轴),与我们已有的 **locality 轴**正交叠加。两点**不采纳**:① 其 **DeepVerifier-4K SFT / 微调 verifier**——本项目训练自由、边界限于 spec(§7 非目标);② 其单生成者自我细化只对应我们的 **local-retry/Stage 5 refine**,不处理跨 DAG 的 upstream/structural 归因。即 DeepVerifier 强化"错的是什么、怎么改",Meta-Agent 仍独有"错在哪、回退到哪"。
