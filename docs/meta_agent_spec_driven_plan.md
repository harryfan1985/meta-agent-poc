# Meta-Agent 落地方案:Spec 驱动的 Agent 编码系统

> 依据论文:*Meta-Agent: From Task Descriptions to Verified Multi-Agent Systems*(Andy Xu, Yu-Wing Tai,Dartmouth,arXiv:2605.25233,NeurIPS 2026)
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
    required_in: list[str] = []           # 必填输入字段(其余视为可选)
    required_out: list[str] = []          # 必填输出字段
    description: str

    def out_jsonschema(self) -> dict:     # 编译成标准 JSON Schema,供机判校验
        return {"type": "object", "required": self.required_out,
                "properties": {k: v.model_dump(exclude_none=True)
                               for k, v in self.output_schema.items()}}

class VerificationCriteria(BaseModel):
    behavioral_assertions: list[str]      # 可执行/可判定的行为断言
    required_tools: list[str] = []
    forbidden_patterns: list[str]         # 必须不出现的模式(如"不得输出代码")

# ---------- Stage 3 产物:Grounding ----------
class Recommendation(BaseModel):
    name: str                             # API / 库 / 文档条目名
    url: str                              # provenance,来自定向检索
    auth_method: Optional[str] = None     # e.g. "api_key" / "oauth" / None
    relevance_score: float = Field(ge=0, le=1)

class GroundingResult(BaseModel):
    directive: str                        # 该 spec 派生出的检索 directive(查询)
    research_summary: str                 # 供 codegen 注入系统提示的知识摘要
    recommendations: list[Recommendation] = []
    retrieved_at: Optional[str] = None    # ISO 时间戳,便于缓存/失效判断

class AgentSpec(BaseModel):
    spec_id: str
    role: str
    tools: list[str] = []                 # e.g. ["web_search", "file_generator"]
    dependencies: list[str] = []          # 指向其它 spec_id
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
    module_path: str                      # 生成的 .py 文件
    entrypoint: str = "run"               # run(message, history) -> dict
    passed: bool = False

# ---------- 构造期最终产物:ExecutableSwarm ----------
# 【工程补全】§4 执行期全程消费此对象。它把"规划真相(plan)"、"可执行体
# (loaded callables)"和"构造期凭证(artifacts)"绑定在一起,并提供 §4 用到的
# 三个访问器。spec/artifact 在 plan 内已校验过 DAG 无环,这里只做索引。
class ExecutableSwarm(BaseModel):
    plan: SwarmPlan                       # specs + dag_edges(已通过环检测)
    artifacts: dict[str, AgentArtifact]   # spec_id -> 构造凭证(passed=True)

    # spec_id -> 已加载的 run(message, history) callable;由 loader 在执行前
    # importlib 加载 artifact.module_path 填充。PrivateAttr:不进序列化、非共享默认值。
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
    # grounding 下
    MISSING_KNOWLEDGE = "missing_knowledge" # 缺必要外部知识
    STALE_SOURCE      = "stale_source"      # provenance 过期/不可达
    # contract 下
    FIELD_MISMATCH    = "field_mismatch"    # 上下游字段对不上(契约可追溯性破坏)
    DECOMP_FLAW       = "decomp_flaw"       # 分解本身有缺陷

# rubric 派生的结构化修正(不是标量分;借鉴 DeepVerifier 的 rubric-guided feedback)
class StructuredFeedback(BaseModel):
    subtype: FailureSubtype
    evidence: str                         # 可溯源:指向违反的 spec 子句/字段/断言
    expected: str                         # spec 要求的样子
    actionable_fix: str                   # 下一轮 refine 的具体改法(非"judge 分")

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
    feedback: list[StructuredFeedback] = []       # 轴 B(同 FailureSignal)

class FailureSignal(BaseModel):
    spec_id: str
    failure_type: FailureType             # 轴 A:决定回退阶段(§6)
    feedback: list[StructuredFeedback] = []  # 轴 B:决定怎么改(喂给 refine)
    pass_index: int = 1
```

> **【论文】** 关键点:验证不是返回布尔值,而是返回**带类型的失败信号**,类型直接决定回退到哪一阶段(见 §6 路由表)。这是"最小代价恢复"的实现基础。
>
> **【工程补全,借鉴 DeepVerifier / arXiv:2601.15808】** 失败信号有**两条正交轴**:`failure_type`(在哪修,管路由)与 `feedback[].subtype`(错的是什么,管 rubric 结构化反馈)。前者沿用论文,后者把原先的自由文本 `issues` 升级为**可溯源、含 expected/fix 的结构化修正**,提升 refine 命中率(详见 §5)。全部为**规则/spec 层**机制,**不涉及任何模型训练**(§7 非目标)。

---

## 3. 构造期(Phase 1)详细设计

总入口:`construct(task_description: str) -> ExecutableSwarm`。五个 Stage 串行,任一 Stage 的产物都要过验证才进入下一步。

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
- 输出 `SwarmPlan`,**强制** `dag_edges` 构成 DAG(生成后做环检测,有环则退回重生成)。**【工程补全】**
- **强制每个 spec 的 `verification_criteria` 非空**:`behavioral_assertions` 至少 1 条、`forbidden_patterns` 至少 1 条。论文的 spec 全都带这两项,且断言写得**可判定**(如"签名必须逐字符匹配 raw_signature""第三方 import 即判 FAIL")。这是后续验证能跑的前提。
- 鼓励"角色不混淆":论文 math 例子里 classifier 被反复打回 3 次,就是因为它的系统提示里混进了"解题"指令(role confusion)。规划时要让每个 agent 职责单一、`forbidden_patterns` 显式排除越界行为。

设计准则(论文 §3.4):
- **少而清晰的分解**:典型 4 节点,最后一个通常是 verifier/formatter 角色。
- **契约可追溯**:下游输入 schema 的每个字段都应能在某个上游输出 schema 找到来源。

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

**【工程补全,可选旋钮:BoN 候选选优,借鉴 BoN-MAV / arXiv:2502.20379】** 默认是"生成 1 个 → 构造期验证 → 失败带反馈顺序重试(≤3 pass)",但论文 math classifier 把 3 次 pass 用满,churn 重。可改为**并行 BoN**:一次生成 N 个候选实现 → 全部过 §3.7 多 aspect 构造期验证 → **按赞成数选最优**;只有最优仍不过才进入顺序 refine 循环。这是"**token 换往返次数与首过率**"的权衡旋钮(`N` 可配,默认 1 即退回顺序模式),BoN+多验证器的扩展性优于 self-consistency。

### 3.5 Stage 5 — ConstructionVerifier(构造期验证)

**【论文】** 对每个生成的 agent `âᵢ` 验证其是否满足 spec `σᵢ`,两类互补检查:

1. **静态验证(static)**:代码结构良好、必需接口齐全、能无运行时错误地实例化。
2. **行为验证(behavioral)**:由一个 verifier 模型在**代表性输入**上模拟执行 `âᵢ`,检查输出是否满足 I/O 契约和行为断言。

**【论文】** 失败返回**带类型**的信号 `f ∈ {spec_adherence, grounding, contract}`,每个类型路由到对应上游阶段(见 §6)。每个 agent 最多 **3 次验证 pass**(论文 math classifier 用满了 3 次)。

实现要点:
- 静态检查:`importlib` 试加载 + `inspect` 校验 `run` 签名 + AST 扫 `forbidden_patterns`(如禁止第三方 import 时,扫 import 节点)。
- 行为检查:为每个 `behavioral_assertion` 构造小输入,跑 agent,断言里能机判的(签名逐字符匹配、禁用 import)直接用代码判,省 token;**机判覆盖不到的语义断言走 §3.7 的多 aspect 验证器面板**(赞成聚合,结果仍映射成带类型失败信号)。**【工程补全】** 断言尽量"代码可判 > 模型判",降低成本与误判。

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
                RecoveryRouter.apply(Recovery(kind="structural",
                    subgraph=affected_subgraph(spec_id, swarm)), spec_id, swarm, store)
                break
            y = swarm.agent(spec_id).run(inputs, store.history(spec_id))

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

设计决策(均为 **【工程补全】**):
- **按字段名匹配**:依赖 Stage 2 规划时就让上下游 schema 字段名对齐(契约可追溯性);名字不齐属于规划缺陷,应在构造期就被 `contract` 验证拦下,而非执行期硬猜。
- **缺字段 / 歧义 → 不静默处理**:任一字段无来源或有多个来源,直接抛 `ContractMismatch`。它在 §5 归因里映射为 `missing_required_field(...)→ structural`(分解本身有缺陷),而不是让 agent 拿着残缺/猜测的输入去跑。
- **作用域限直接依赖**:不做跨层"全局变量池"式取值,避免隐式耦合绕过 DAG;需要某上游字段就必须在 `dependencies` 里显式声明,保持 DAG 是唯一的数据流真相。
- **可追溯**:`message` 每个字段都记录来源 `spec_id`,写入 trace,供 §5 upstream 归因快速定位责任上游。

### 4.3 RuntimeGate(执行期验证)

**【论文】** 公式 (2):中间输出 `yᵢ` 在传给下游前,验证 `yᵢ ∈ Cᵢ`,其中 `Cᵢ` 编码 schema 约束、行为断言、forbidden patterns。不通过则**不向下游传播**,并触发恢复。

实现:`schema 校验(机判,用 §2 `out_jsonschema()`)→ forbidden_patterns 扫描(机判)→ behavioral_assertions(机判优先,necessary 时走 §3.7 多 aspect 验证器面板)`,任一不过即返回 `GateResult(ok=False, ...)`。

**统一产物**:RuntimeGate 与构造期 ConstructionVerifier 都返回 §2 的 `GateResult` —— 同时带 `failure_type`(轴 A)与 `feedback: list[StructuredFeedback]`(轴 B)。**机判项失败也要产出 `StructuredFeedback`**(如 schema 校验失败 → `subtype=SCHEMA_VIOLATION`,`evidence` 指向具体字段、`expected` 取 `out_jsonschema` 该字段、`actionable_fix` 描述补法),不只机判面板。这样 rubric 反馈能一路流到 §5 的 refine,**不退化成扁平字符串**;`failure_type` 多个时按 `FAILURE_PRIORITY` 取最高者。`GateResult` 直接喂给 §5 的 `ErrorAttributor`。

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
            return Recovery(kind="upstream", target=d)
    # 2) 失败信号指向契约/分解不匹配(下游需要的字段上游根本没产出)→ structural
    if gate.failure_type == FailureType.CONTRACT or missing_required_field(gate, store):
        return Recovery(kind="structural", subgraph=affected_subgraph(spec_id, swarm))
    # 3) 否则本地重试(带结构化反馈 §2 StructuredFeedback),超过上限再升级
    if local_retries(spec_id) < MAX_LOCAL_RETRIES:    # 【工程补全】默认 2
        return Recovery(kind="local", target=spec_id, feedback=gate.feedback)
    return Recovery(kind="structural", subgraph=affected_subgraph(spec_id, swarm))
```

**【工程补全】** 重试/重跑/重规划各设上限与全局预算(总 LLM 调用数 / 时间 / 成本),触顶则"surface the failure 而非给未验证答案"——这正是论文 math swarm 的 `coordination_strategy` 写明的兜底原则。

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
| 编排语言 | Python 3.11+ | 【工程补全】生成产物即 Python 模块,语言一致最省事 |
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

所有重试/重跑/重规划在动作前 `meter.check(budget)`;超限**不再尝试**,直接 surface 当前最佳 `GateResult` 与失败原因,而非给未验证答案。

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
    payload: dict = {}                # 输入/输出摘要(脱敏)
```

一条任务的执行 = 一串 `TraceEvent`,可回放、可做消融对比、可聚类失败(`gate_result.feedback[].subtype` 是天然聚类键)。MVP 落地为 JSONL,生产换可回放事件日志(§7 存储行)。

---

## 8. 分阶段落地路线

> **排期原则**:先打平论文 baseline(M0–M2,只用论文机制 + 必要的可机判收口),**再叠加 MAV/DeepVerifier 增强(M3)**。aspect 面板(§3.7)、BoN 旋钮(§3.4)、轴 B 的 rubric 反馈属于"锦上添花",**不进 PoC 前期**,避免一上来背全套。

**Milestone 0 — 骨架与 schema(1~2 周)**
- 落地 §2 全部 Pydantic 模型(含**可机判的 `IOContract`/`FieldSpec`/`out_jsonschema`**、统一 `GateResult`);搭 `Coordinator` + `ContextStore`(含 `gather_inputs` §4.2)+ 拓扑执行 + DAG 环检测。
- 手写一个 4-agent swarm(直接复刻论文附录 A 的 function-completion 四节点)跑通执行期,先不接构造期。验收:`has_close_elements` 例子端到端 PASS。

**Milestone 1 — 执行期验证 + 三级归因 + 结构化反馈(1~2 周)**
- 实现 `RuntimeGate`(schema/forbidden/assertion 三检,**全部机判**)+ `ErrorAttributor` + `RecoveryRouter`。
- 落地**两条轴**:`GateResult` 产出 `failure_type`(轴 A)+ `StructuredFeedback`(轴 B,先做机判项的 evidence/expected/fix);local 重试消费结构化反馈。
- 用论文 §3.3 的四个归因场景做单测(local / upstream / contract / structural 各一)。验收:注入 4 类错误都能被正确分类并恢复。

**Milestone 2 — 构造期全流水线 + 工具注册表(2~3 周)**
- 依次实现 Stage 1→5;构造期验证产出 `GateResult` 并按 §6 路由;每 agent ≤3 pass。
- 落地**工具注册表**(§3.6):`registry.validate(plan)` 在 Stage 2 后拦截未注册工具;Stage 4 工具定义只从注册表出。
- 验收:给一段全新的编码任务描述(不在示例里),自动产出可执行 swarm 并通过构造期验证。

**Milestone 3 — MAV/DeepVerifier 增强 + 评测、加固(2~4 周)**
- **增强(baseline 打平后才上)**:§3.7 多 aspect 面板(模型判残差,便宜档)+ source-checkable 分解;§3.4 BoN 候选选优旋钮;rubric 反馈扩到语义断言;轴 B 子类随 trace 增长(§5 future-work)。
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
1. 任务成功率(对齐上表)。
2. 错误恢复率:注入中间错误后仍成功完成的比例。
3. 工作流稳定性:long-horizon 任务的级联失败率。
4. 成本:每任务 LLM 调用数 / token / 墙钟时间(论文各 Stage 耗时 100~700s 量级,可作上界参考)。
5. **消融自检**:本地复现"去验证掉 ~7 分"的趋势,验证"验证是承重组件"而非摆设。

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
| 弱于专家手工系统 | **【论文 Limitations】** 全自动 vs 专家先验 | 预留"轻量领域先验"注入点(论文 future work 方向):允许人工为特定 domain 追加 spec 模板/断言 |

---

## 附:与相邻工作的边界(便于技术汇报)

- **MetaGPT**:预定义 workflow + 仅 local 验证 → Meta-Agent 是**按任务合成**结构 + 构造期&执行期统一验证。
- **AutoGen**:对话式图 + post-hoc 验证 → Meta-Agent 验证前置到"执行开始之前"。
- **VeriMAP(2510.17109)**:把验证函数嵌入已实例化的规划图 → Meta-Agent 额外在**构造期**就验 spec/tool/依赖,并支持类型化重建。
- **AFlow**:自动 workflow 生成(本论文主基线)→ Meta-Agent 在五项 benchmark 上更优且无需迭代式 workflow 优化。
- **MAV / BoN-MAV(2502.20379)**:沿"验证器数量"做 test-time 扩展,多 aspect 验证器**赞成投票选最优候选**(selection)→ 本方案**借用**其多验证器/weak-to-strong/BoN 思路加固"模型判残差"(§3.7)与降本(§7),但**不采纳**其纯投票聚合替代我们的**类型化归因**:MAV 给票数、不给错误类型,无法驱动 §5/§6 的最小代价路由。即 MAV 解决"选哪个最好",Meta-Agent 解决"错在哪、回退到哪"。
- **DeepVerifier(2601.15808)**:针对 deep-research agent,用**失败分类法 + rubric 结构化反馈 + source-checkable 分解**做测试期自我细化(单生成者迭代)→ 本方案**借用**其三项机制补齐失败信号的**内容轴**(§2 `FailureSubtype`/`StructuredFeedback`、§3.7 source-checkable 分解、§5 两条正交轴),与我们已有的 **locality 轴**正交叠加。两点**不采纳**:① 其 **DeepVerifier-4K SFT / 微调 verifier**——本项目训练自由、边界限于 spec(§7 非目标);② 其单生成者自我细化只对应我们的 **local-retry/Stage 5 refine**,不处理跨 DAG 的 upstream/structural 归因。即 DeepVerifier 强化"错的是什么、怎么改",Meta-Agent 仍独有"错在哪、回退到哪"。
