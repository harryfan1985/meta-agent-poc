# Architecture: Control Plane for Code Agents

## 架构定位

`meta-agent-poc` 不应被理解成"另一个 code agent"。它更适合作为 **code agent 之上的 spec / orchestration / verification control plane**。

与 Claude Code、opencode、Pi agent 等 code agent 的关系是:

```text
Meta-Agent Engine = 规格编译器 + DAG runtime + 验证/恢复控制面
Code Agent        = 某个 DAG 节点的执行后端 / 代码生成后端 / 修复后端
```

也就是说,Meta-Agent Engine 不替代这些 code agent,而是给它们加上:

```text
任务分解
I/O 契约
数据流约束
验证门
错误归因
恢复策略
安全策略
trace / replay
```

## 系统上下文

```text
User / Product Request
        │
        ▼
Meta-Agent Engine
  ├─ IntentParser
  ├─ SwarmPlanner
  ├─ ToolRegistry
  ├─ Constitution / Policy
  ├─ AgentSpec DAG
  ├─ ArtifactLoader
  ├─ Coordinator
  ├─ ContextStore
  ├─ RuntimeGate
  ├─ ErrorAttributor
  └─ RecoveryRouter
        │
        ▼
Agent Runtime Adapters
  ├─ ClaudeCodeAdapter
  ├─ OpenCodeAdapter
  ├─ PiAgentAdapter
  ├─ ACPAgentAdapter
  ├─ A2AAgentAdapter
  ├─ MCPToolAdapter
  ├─ LocalPythonAdapter
  ├─ PromptTemplateAdapter
  └─ GeneratedModuleAdapter
        │
        ▼
Workspace / Repo / Tools / Sandbox / Tests
```

关键边界:

```text
Meta-Agent Engine 负责:应该做什么、谁做、输入输出是什么、怎么验、失败怎么办。
Code Agent 负责:具体把某个节点的任务做出来。
```

## Code Agent 的角色

Claude Code、opencode、Pi agent 这类系统可以在架构里扮演多种角色。

### 1. AgentArtifact 执行后端

每个 `AgentSpec` 最终会被加载成统一 callable:

```python
run(message: dict, history: list) -> dict
```

这个 callable 背后可以是本地函数、生成的 Python module,也可以是外部 code agent。

示例:

```text
AgentSpec: code_synthesizer
input:  raw_signature, edge_cases, constraints
output: candidate_code
backend: ClaudeCodeAdapter
```

Engine 调度到该节点时,adapter 把结构化 `message` 转成 code agent 任务。code agent 完成后,adapter 再把结果解析回 `output_schema` 要求的 dict,交给 `RuntimeGate`。

### 2. Stage 4 CodeGen 后端

构造期的 `AgentCodeGen` 可以调用 code agent 生成某个 agent 的实现:

```text
AgentSpec
  → Claude Code / opencode / Pi agent 生成 generated/agents/{spec_id}.py
  → ConstructionVerifier 检查 run(message, history)、schema、forbidden patterns
  → 通过后成为 AgentArtifact
```

此时 code agent 是"实现生成者",但是否可信由 `ConstructionVerifier` 决定。

### 3. Recovery 后端

执行期失败后,`RecoveryRouter` 可把局部修复交给 code agent:

```text
local failure:
  只重试/修复当前 agent

upstream failure:
  重跑责任上游 agent

structural failure:
  回到构造期重建受影响子图
```

传给 code agent 的不是自由文本"修一下",而是结构化反馈:

```text
GateResult
StructuredFeedback.evidence
StructuredFeedback.expected
StructuredFeedback.actionable_fix
affected spec_id
允许修改范围
```

重要原则:

> Code agent 可以提出修复产物,但不能自己宣布修复成功。修复后仍必须经过 gate。

### 4. Tool Handler

code agent 的能力也可以作为工具挂入 `ToolRegistry`:

```text
tool name: repo_edit
handler: claude_code_adapter.edit
side_effects: write
requires_network: false
```

这样 Engine 可以控制:

```text
哪些节点能写文件
哪些节点能联网
哪些节点只能读
哪些工具调用必须先过 policy
```

### 5. AgentVerifierBackend

对复杂代码任务或高风险任务,code agent 也可以作为主动验证器:

```text
AgentVerifierBackend
  input: spec + output + trace summary + evidence refs
  action: 查证、运行测试、检查文件、定位不一致
  output: GateResult
```

限制:

```text
verification="none"
```

即 verifier agent 自身不能递归触发另一层 agentic verification,否则会形成无限验证循环。

## Adapter 抽象

为了兼容 Claude Code、opencode、Pi agent,核心引擎不应硬编码任何一个产品。推荐提供统一 adapter 协议:

```python
class AgentRuntimeAdapter:
    name: str
    capabilities: set[str]
    max_risk_tier: str

    def prepare(self, spec: AgentSpec) -> None:
        ...

    def invoke(self, spec: AgentSpec, message: dict, history: list) -> dict:
        ...

    def repair(self, spec: AgentSpec, feedback: list[StructuredFeedback]) -> dict:
        ...

    def cleanup(self) -> None:
        ...
```

`AgentArtifact` 的权威定义在**设计文档 §2**(真相源,勿在此重复声明),其 `implementation_kind` 已含 `fixture / prompt_template / python_module / external_agent`,外部 agent 形态用 `adapter_name` 指向 `AgentRuntimeAdapter`。本文档只描述 adapter 协议,不另立 schema。

这样 Claude Code、opencode、Pi agent 都只是 adapter,不会污染核心 spec/runtime。

### ClaudeCodeAdapter 与 OpenCodeAdapter 的共享边界

`ClaudeCodeAdapter` 和 `OpenCodeAdapter` 是同一个 `AgentRuntimeAdapter` 抽象下的 **sibling backend**,不是继承替代关系,也不是谁包装谁。它们都服务于同一个 control-plane contract:

```text
AgentSpec + message + history
  → 外部 code agent 执行
  → output dict / artifact / diff / trace
  → RuntimeGate / ConstructionVerifier
```

因此可以共用的是**引擎契约和通用执行基座**,不能共用的是**产品适配层**。

推荐实现三层结构:

```text
AgentRuntimeAdapter
  └─ CliCodeAgentAdapterBase
       ├─ ClaudeCodeAdapter
       └─ OpenCodeAdapter
```

`CliCodeAgentAdapterBase` 只放和产品无关的基础设施:

```text
create_ephemeral_worktree()
render_contract_reminder()
run_process_with_timeout()
capture_diff()
enforce_output_size_limit()
emit_trace_event()
normalize_exit_status()
validate_against_output_schema()
cleanup_worktree()
```

`ClaudeCodeAdapter` 和 `OpenCodeAdapter` 分别实现产品语义:

| 维度 | ClaudeCodeAdapter | OpenCodeAdapter |
|---|---|---|
| 调用入口 | Claude Code CLI / SDK / hooks | `opencode run` / `opencode serve` / `opencode acp` |
| 结构化输出 | 可利用 Claude Code 的 JSON / stream JSON / JSON Schema 能力 | 需要把 OpenCode JSON events reduce 成最终 `output dict` |
| 会话模型 | Claude Code session / resume / worktree 语义 | OpenCode session / attach / headless server / ACP 语义 |
| 权限映射 | Claude Code tools、permission mode、hook gate | OpenCode agent config、MCP、permission prompt、server policy |
| 隔离方式 | 可复用 Claude Code worktree 能力,高风险仍由引擎校验 diff | 引擎创建临时 worktree,通过 `--dir` 或 ACP cwd 传入 |
| 事件解析 | Claude stream / hook events → `TraceEvent` | OpenCode raw JSON events / session export → `TraceEvent` |
| 失败映射 | CLI exit、schema violation、hook denial、tool denial | exit、event error、permission denial、agent/config error |

不能把两者强行写成一个 `CodeAgentAdapter(provider="...")`,否则差异会泄漏成大量 provider 分支:

```text
if provider == "claude": ...
if provider == "opencode": ...
```

这会让 adapter 同时承担两套 CLI 参数、两套权限模型、两套事件格式、两套会话语义,最终削弱 control plane 最重要的性质:同一个 `AgentSpec` 可以替换执行后端,但核心 `Coordinator` / `RuntimeGate` / `RecoveryRouter` 完全不变。

更好的边界是:

```text
共用:
  AgentRuntimeAdapter contract
  worktree / subprocess / timeout / diff / trace 基座
  schema validation 和 RuntimeGate

分开:
  CLI 参数渲染
  agent 配置生成
  tool/permission 映射
  event stream parser
  session/resume 语义
  provider-specific error attribution hints
```

如果未来某个 code agent 稳定暴露 ACP/A2A server,可以再通过 `ACPAgentAdapter` 或 `A2AAgentAdapter` 收敛 transport 层。但即便 transport 收敛,安装、认证、权限、工具、事件细节仍应保留 provider profile,不能进入核心 schema。

## 接口协议选型

没有一个业界协议能完整覆盖本项目的内部上下文接口。推荐采用:

```text
内部 canonical schema + 外部标准协议 adapter
```

内部真相源仍然是:

```text
AgentSpec
IOContract
VerificationCriteria
DAG
GateResult
StructuredFeedback
RecoveryAction
```

外部协议只负责边界互操作,不能替代上述核心模型。

### 协议职责矩阵

| 协议 | 最适合解决 | 本项目中的位置 | 不负责 |
|---|---|---|---|
| MCP | 工具、资源、prompt、workspace roots 接入 | `ToolRegistry` / `ContextStore` / repo-docs-tools adapter | Agent DAG、错误归因、恢复路由 |
| ACP | framework-agnostic agent service 调用 | `ACPAgentAdapter` / 外部 agent transport | 内部 spec 真相源、RuntimeGate |
| A2A | 跨 agent task lifecycle、agent card、message、artifact | `A2AAgentAdapter` / 远端 agent delegation | 字段级数据流、contract mismatch |
| AG-UI | UI 事件流、人审、approval、进度展示 | human-in-loop / frontend adapter | agent 执行与验证逻辑 |
| OpenTelemetry / CloudEvents | trace、metrics、事件分发 | `TraceEvent` 导出 / observability | 业务语义、恢复决策 |
| LSP / DAP / Git | 代码 workspace 语义和调试上下文 | code intelligence tools, 可经 MCP 暴露 | agent 通信协议 |

### MCP: 工具和资源接入

MCP 适合做工具/上下文边界:

```text
ToolRegistry        → MCP Tools
ContextStore output → MCP Resources
Workspace roots     → MCP Roots
Prompt templates    → MCP Prompts
Policy gate         → MCP tool/resource 调用前的 permission layer
```

适用场景:

- Claude Code 接入 repo、issue、docs、test runner。
- 外部文档/数据库/搜索作为受控 resources。
- 本项目把 `ContextStore` 的某些已验证产物暴露给其它 MCP client。

限制:

- MCP server 返回的资源只作数据,不得作为系统指令。
- MCP tool 必须映射到 `ToolDefinition`,声明 `requires_network` 和 `side_effects`。
- MCP 不决定 `GateResult`,也不参与 local/upstream/structural 归因。

### ACP: 轻量 agent service 互操作

ACP 适合把不同框架或语言实现的 agent 包装成统一 agent service。它尤其适合:

```text
BeeAI / CrewAI / LangChain / LangGraph / 自研 agent 混用
内部企业环境的 framework-agnostic agent 调用
一次性或短生命周期结构化结果返回
轻量 HTTP/REST 风格 agent service 暴露
```

在本项目中,ACP 应作为 `AgentRuntimeAdapter` 的一种 transport:

```python
class ACPAgentAdapter(AgentRuntimeAdapter):
    def invoke(self, spec: AgentSpec, message: dict, history: list) -> dict:
        request = map_spec_to_acp_request(spec, message, history)
        response = acp_client.invoke(request)
        return parse_structured_result(response, spec.io_contract.output_schema)
```

映射关系:

```text
AgentSpec.role / io_contract.description → ACP task/request payload
AgentSpec.tools                          → ACP capability metadata
ContextStore.gather_inputs()             → ACP request input
ACP response                             → Agent output dict
RuntimeGate                              → 本项目内部验证,不交给 ACP 决定
```

优先选择 ACP 的情况:

- 需要把第三方 agent framework 快速接入。
- 外部 agent 更像"服务"而不是完整任务生命周期系统。
- 不需要复杂 artifact lifecycle 或 Agent Card discovery。
- 需要在 Python/TypeScript agent 之间保持轻量互通。

### A2A: 远端 agent 委派和 artifact lifecycle

A2A 适合更完整的跨 agent 任务委派:

```text
Agent Card
Task
Message
Artifact
Part
```

在本项目中,A2A 适合作为远端 agent delegation adapter:

```text
AgentSpec        → A2A Task 描述
AgentArtifact    → A2A Artifact
AgentRuntimeAdapter → A2A Client
外部 code agent  → A2A Server
capabilities     → Agent Card
```

优先选择 A2A 的情况:

- 需要 agent discovery / Agent Card。
- 需要长任务状态、streaming、artifact 生命周期。
- 需要跨组织、跨系统、跨 vendor 的远端 agent 委派。
- 任务有多轮 message 与多 artifact 交付。

### ACP vs A2A

ACP 和 A2A 都能用于外部 agent 调用,但默认取舍是:

| 情况 | 优先 |
|---|---|
| 内部企业环境、混合多个 agent framework、轻量服务调用 | ACP |
| 远端 agent discovery、标准任务生命周期、artifact exchange | A2A |
| 只是调用工具/读资源/暴露 repo 上下文 | MCP |

二者都不应替代内部 DAG runtime。它们只负责"如何调用外部 agent";`Coordinator`、`ContextStore`、`RuntimeGate`、`RecoveryRouter` 仍由本项目控制。

### AG-UI: 人机交互和人审

AG-UI 适合前端/人审事件流:

```text
TraceEvent       → UI event stream
SurfaceFailure   → human review request
Recovery decision → approval / intervention event
Tool call        → approval / progress event
```

适用场景:

- high / critical risk tier 需要人工确认。
- 展示长任务进度、验证失败、恢复动作。
- 用户批准高副作用工具调用。

### OpenTelemetry / CloudEvents: 可观测边界

`TraceEvent` 可以导出到 OpenTelemetry span/event 或 CloudEvents:

```text
GateResult       → span event
RecoveryAction   → CloudEvent
BudgetExceeded   → span status / event
Verifier votes   → span attributes
```

这让系统后续可以接入 Jaeger、Grafana、Honeycomb、Datadog 或自建 event log。

### LSP / DAP / Git: 代码上下文协议

代码任务不要只靠 LLM 读文件。代码 workspace 能力应复用成熟协议:

```text
LSP: symbol / definition / references / diagnostics
DAP: debug sessions
Git: diff / commit / branch / blame
pytest / coverage: executable evidence
```

这些能力可以通过 MCP tool/resource 暴露给 Claude Code、opencode 或 AgentVerifierBackend。

### 选型原则

1. **内部 schema 不外包**  
   任何外部协议都不能替代 `AgentSpec`、`IOContract`、`GateResult`。

2. **边界协议可替换**  
   同一个 `AgentSpec` 可以通过 ACP、A2A、Claude Code CLI 或本地 Python 执行。

3. **工具和 agent 分离**  
   工具优先走 MCP;外部 agent 优先走 ACP/A2A。

4. **验证结果归本项目所有**  
   外部 agent 可以返回候选产物,但通过/失败由 `RuntimeGate` 和 `ConstructionVerifier` 判定。

5. **高风险协议调用必须过 policy**  
   ACP/A2A/MCP 调用都要经过 `ConstitutionRule`、`VerificationPolicy` 和 `ToolDefinition.side_effects` 检查。

## 与 Claude Code 的结合

Claude Code 适合扮演:

```text
codegen backend
local retry backend
repo-edit tool handler
test runner tool handler
agent verifier for code tasks
```

示意 adapter:

```python
class ClaudeCodeAdapter:
    def run_agent(self, spec: AgentSpec, message: dict, history: list) -> dict:
        ...

    def generate_artifact(self, spec: AgentSpec) -> AgentArtifact:
        ...

    def repair_artifact(self, artifact: AgentArtifact,
                        feedback: list[StructuredFeedback]) -> AgentArtifact:
        ...

    def verify_code_claim(self, claim: Claim,
                          evidence: list[EvidenceRef]) -> GateResult:
        ...
```

Claude Code 的优势:

```text
真实操作 repo
能编辑多文件
能跑测试
适合长代码任务
```

Engine 对它的约束:

```text
限制任务边界
强制输出 schema
记录 trace
验证结果
失败归因
防止越权改动
```

## 与 opencode 的结合

opencode 这类开源/终端型 code agent 更适合:

```text
本地可控 executor
CI 中的 code agent backend
可替换 Claude Code 的 adapter
低成本 repair worker
```

典型包装:

```python
class OpenCodeAdapter:
    def invoke(self, spec: AgentSpec, message: dict, history: list) -> dict:
        prompt = render_task(spec, message, history)
        result = subprocess_run_opencode(prompt)
        return parse_structured_result(result, spec.io_contract.output_schema)
```

opencode 的价值是本地化、CI 友好、替换成本低;但它仍只是执行者,不是 spec 真相源。

## 与 Pi agent 的结合

如果 Pi agent 是偏产品/工作流/代码执行型 agent,它可以作为:

```text
domain-specific agent backend
research / planning / coding worker
long-horizon task executor
```

只要它能被包装成 `AgentRuntimeAdapter.invoke(...) -> dict`,就可以作为某类 `AgentArtifact` 的执行后端。

## 责任分界

最清晰的分界是:

```text
Meta-Agent Engine owns:
  - spec
  - DAG
  - contracts
  - verification criteria
  - policy
  - tool permission
  - trace
  - recovery routing

Code Agent owns:
  - implementation attempt
  - file edits
  - test execution
  - local reasoning
  - code repair proposal
```

一句话:

> Code agent 可以提出产物,Meta-Agent Engine 决定产物是否可信、是否传播、失败后回退到哪里。

## 典型执行流程

以代码任务为例:

```text
1. 用户提出任务
2. Meta-Agent 生成 AgentSpec DAG
3. SwarmPlanner 决定哪些节点用 Claude Code / opencode / local fixture
4. Coordinator 调度第一个 ready 节点
5. Adapter 调用 code agent 执行
6. code agent 编辑文件或返回结构化输出
7. RuntimeGate 校验输出
8. 通过则写入 ContextStore
9. 失败则 ErrorAttributor 分类
10. RecoveryRouter 决定让同一个 code agent 修、重跑上游、还是重规划
11. 所有 trace 留存,用于回放、调试和 verifier calibration
```

这套流程的**具体实例**见设计文档[附录 A](meta_agent_spec_driven_plan.md):function-completion 4-agent swarm 已用现行 schema 完整写出。M0 用 `implementation_kind="fixture"` 跑通同一执行期;control-plane 形态只需把节点的 artifact 换成 `implementation_kind="external_agent"`(`adapter_name` 指向 `ClaudeCodeAdapter` 等),**执行期、gate、归因、恢复完全复用,不改一行**——这正是"control plane 不绑定执行后端"的体现。

## 推荐系统分层

```text
Layer 1: Spec Layer
  ParsedIntent, AgentSpec, IOContract, VerificationCriteria, ConstitutionRule

Layer 2: Planning / Construction Layer
  IntentParser, SwarmPlanner, GroundingResearcher, AgentCodeGen, ConstructionVerifier

Layer 3: Runtime Layer
  ArtifactLoader, Coordinator, ContextStore, DAG scheduler

Layer 4: Verification Layer
  RuntimeGate, VerifierBackend, VerificationFunctionRegistry, Claim/Evidence

Layer 5: Recovery Layer
  ErrorAttributor, RecoveryRouter, BudgetMeter, SurfaceFailure

Layer 6: Adapter Layer
  ClaudeCodeAdapter, OpenCodeAdapter, PiAgentAdapter,
  ACPAgentAdapter, A2AAgentAdapter, MCPToolAdapter,
  LocalPythonAdapter, PromptTemplateAdapter, GeneratedModuleAdapter
```

Code agent 都在 Layer 6。核心 engine 不被任何单一 agent 产品绑定。

## 架构价值

这种集成方式带来几个关键收益:

- **避免 code agent 失控**:spec、DAG、policy 限制任务边界。
- **避免做完就算**:每个输出消费前都过 `RuntimeGate`。
- **支持多 code agent 混用**:不同节点可以使用不同 adapter。
- **支持局部恢复**:根据 local / upstream / structural 做最小代价恢复。
- **方便评测和替换**:同一 `AgentSpec` DAG 可换不同 backend 比较成功率、成本、耗时、失败类型。

## 核心判断

这个引擎和 Claude Code / opencode / Pi agent 的最佳关系不是竞争关系,而是控制面与执行面的关系:

```text
Meta-Agent Engine = control plane
Code Agents       = execution plane
```

换句话说:

> Meta-Agent Engine 负责让任务可规划、可验证、可恢复。Claude Code / opencode / Pi agent 负责把某个受约束的节点任务做出来。

这也是项目最重要的系统架构定位:不是再造一个 code agent,而是给多个 code agent 提供 **spec、DAG、verification、recovery** 这层系统化外骨架。
