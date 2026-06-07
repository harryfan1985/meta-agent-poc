# Frontend Design: Verification Cockpit

## UI 定位

这个引擎的前端不应是普通聊天界面,而应是一个 **graph-first 的 multi-agent workflow debugger / verification cockpit**。

推荐定位:

```text
Graph-first Workbench
  + Trace / Verification Inspector
  + Human Review Console
```

它服务的核心对象不是"一段对话",而是:

```text
AgentSpec
DAG
IOContract
RuntimeGate
GateResult
RecoveryRouter
TraceEvent
```

因此 UI 要优先展示结构化事实:谁在执行、字段怎么流动、哪个 gate 失败、为什么失败、系统如何恢复。

## 为什么不是纯 Chat UI

纯聊天界面会把本项目最有价值的信息压扁成文本记录,难以回答:

```text
哪个 agent 失败?
哪个字段缺失?
哪个上游有问题?
哪个 verifier 拒绝?
恢复动作为什么发生?
预算在哪里耗尽?
```

Chat 可以作为任务输入入口,但不能作为主界面。主界面必须围绕 **DAG + spec + verification + trace** 组织。

## 主界面布局

推荐默认布局:

```text
┌──────────────────────────────────────────────────────────────┐
│ Top Bar: task / run status / budget / elapsed / risk tier     │
├───────────────┬──────────────────────────────┬───────────────┤
│ Left Nav      │ Center: DAG / execution graph │ Right Inspect │
│ Tasks         │ Agent nodes + dependency edge │ Spec          │
│ Swarms        │ Status color + progress       │ I/O contract  │
│ Runs          │                              │ GateResult    │
│ Verification  │                              │ Recovery      │
│ Policy        │                              │ Trace detail  │
├───────────────┴──────────────────────────────┴───────────────┤
│ Bottom Panel: event stream / logs / budget / raw JSON         │
└──────────────────────────────────────────────────────────────┘
```

状态颜色:

```text
gray    not started
blue    running
green   gate passed
yellow  recovering / human review
red     failed
purple  structural issue
```

## 主要视图

### 1. Swarm Designer / Plan View

用于查看或编辑 `AgentSpec DAG`。

核心元素:

```text
DAG 画布
Agent 节点
依赖边
输入输出字段
risk tier
tools
verification criteria
constitution rules
```

点击节点后,右侧 inspector 展示:

```text
role
dependencies
input_schema
output_schema
machine_assertions
forbidden_patterns
tools
risk_tier
constitution rules
```

第一版可以只读;后续再支持可视化编辑 DAG。

### 2. Run Monitor / Execution View

用于观察一次 run 的实时状态。

核心展示:

```text
当前 ready nodes
正在执行的 agent
ContextStore 中已有 outputs
RuntimeGate 检查结果
RecoveryRouter 动作
BudgetMeter
```

交互重点是"看系统在做什么",而不是和 agent 闲聊。

### 3. Verification Inspector

这是本项目最有差异化的 UI。

每个 `GateResult` 应可展开:

```text
ok / failed
failure_type
failure_subtypes
evidence
expected
actionable_fix
verifier backend
machine_assertion id
claim / evidence refs
```

对 claim/evidence 验证,UI 应像代码审查一样展示:

```text
Claim
  status: supported / contradicted / insufficient
  evidence refs
  source path
  verifier notes
```

这个视图回答:

```text
为什么这个输出没有被传播?
为什么这次恢复是 local 而不是 upstream?
```

### 4. Human Review Console

高风险任务或 `SurfaceFailure` 需要人工介入。

支持操作:

```text
approve retry
approve replan
reject final answer
request more evidence
override with reason
mark as regression case
```

所有人工操作必须结构化写入 trace,不能只落成自由文本评论。

### 5. Trace / Replay / Calibration View

给开发者、评测和 verifier 校准使用。

核心能力:

```text
按时间线查看 TraceEvent
按 DAG 查看 trace
按 agent 查看历史失败
按 verifier 查看 false accept / false reject
回放某次 run
导出 GoldenVerificationCase
对比不同 backend
```

## 信息架构

```text
App
├── Tasks
│   ├── New Task
│   └── Task History
├── Swarms
│   ├── Plans
│   ├── Agent Specs
│   └── Artifacts
├── Runs
│   ├── Live Monitor
│   ├── Trace Replay
│   └── Recovery History
├── Verification
│   ├── Gate Results
│   ├── Claims & Evidence
│   ├── Golden Cases
│   └── Verifier Calibration
├── Policy
│   ├── Constitution Rules
│   ├── Tool Permissions
│   └── Risk Tiers
└── Adapters
    ├── MCP Tools
    ├── ACP Agents
    ├── A2A Agents
    ├── Claude Code
    ├── opencode
    └── Pi Agent
```

## 前端协议

前端不应直接理解所有外部协议细节。后端 adapter 层应先把 MCP / ACP / A2A / tool events 归一化成本项目内部事件。

推荐消费模型:

```text
Frontend consumes:
  TraceEvent
  GateResult
  SurfaceFailure
  RecoveryAction
  AgentSpec
  SwarmPlan

Backend adapters consume:
  MCP
  ACP
  A2A
  LSP / DAP / Git
```

实时通道:

```text
Backend → Frontend:
  AG-UI events 或 SSE/WebSocket 自定义事件

Trace export:
  OpenTelemetry / JSONL

Tool/resource integration:
  MCP

External agent status:
  ACP / A2A adapter events → TraceEvent → UI
```

AG-UI 适合承载:

```text
agent started
tool call started
tool call ended
gate result
recovery action
human approval requested
text message delta
state delta
```

## 技术建议

Web 前端推荐:

```text
React / TypeScript
React Flow 或同类 graph canvas
AG-UI 或 SSE/WebSocket event stream
Monaco Editor 用于 JSON/YAML/spec 查看与编辑
TanStack Table 用于 trace/gate/result 表格
OpenTelemetry 后端导出用于长期观测
```

组件选择原则:

- DAG 画布优先支持自动布局、缩放、节点状态、自定义节点。
- Inspector 优先支持结构化 schema、diff、JSON path、evidence link。
- Event log 必须可过滤、可搜索、可定位到 DAG 节点。
- Human review 操作必须产生结构化事件。

## 最小可用前端(M0/M1)

第一版不要做全功能。M0/M1 前端可以只读:

```text
1. DAG viewer
2. selected node inspector
3. run event log
4. GateResult panel
5. JSON view for AgentSpec / ContextStore / TraceEvent
```

M0/M1 目标是让开发者看清楚:

```text
DAG 是否正确
字段怎么流
哪个节点输出了什么
gate 为什么通过/失败
```

不建议一开始实现:

```text
可视化 DAG 编辑器
复杂权限管理
完整 calibration dashboard
多租户审计
自定义 widget 市场
```

## 视觉风格

应是安静、密集、工程化的工作台:

```text
高信息密度
少装饰
强状态色
表格 + inspector + graph
稳定尺寸和可扫描布局
```

更像:

```text
CI dashboard
workflow debugger
distributed tracing UI
DAG orchestration console
code review tool
```

而不是:

```text
ChatGPT clone
Notion-style doc editor
marketing dashboard
agent playground
```

## 核心判断

这个引擎最合适的 UI 是:

> 一个 graph-first 的 multi-agent workflow debugger / verification cockpit。

一句话:

```text
主屏看 DAG,右侧看 spec,中间看执行,底部看 trace,失败时进入 verification / recovery inspector。
```

这能把系统最有价值的东西展示出来:

```text
spec
DAG
验证
归因
恢复
```
