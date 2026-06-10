# Implementation Stack: 开源项目重用分析

> 实现这套引擎需要/可重用哪些开源项目。范围覆盖全实现栈(非仅验证——验证类工具另见 [analysis/open-source-tools.md](analysis/open-source-tools.md))。
>
> 两条硬约束:
> - **§7.5 采用边界**:核心 schema / runtime / gate / 归因 = **自建真相源**;OSS 只作 adapter / backend,不进入 spec 真相源或核心热路径默认依赖。
> - **不训练边界**:任何训练 / 微调 / SFT / RL 框架一律**不引入**(spec-only)。

## 一句话原则

> **自建"控制面"(schema / DAG runtime / RuntimeGate / ErrorAttributor / RecoveryRouter),重用"执行面 + 基础设施"(LLM SDK、沙箱、协议、代码工具、trace、前端、测试)。**

凡是会让核心 schema/runtime 被某框架绑死的(把 LangGraph 当 runtime、把 Guardrails 当 gate 核心),一律降级为 adapter 或只研究——这是 §7.5 与 control-plane 定位的必然推论。

## 分级总表

| 级别 | 含义 | 项目 |
|---|---|---|
| **A 直接采用** | 地基,低风险,无替代必要 | Pydantic v2、jsonschema、`anthropic` SDK、`graphlib`(stdlib)、`ast`/`re`(stdlib)、pytest + pytest-cov + hypothesis、OpenTelemetry SDK |
| **B 选用(adapter/backend)** | 守 §7.5,按需引入,可替换 | LiteLLM、Instructor、Guardrails AI、Promptfoo/DeepEval、tree-sitter、GitPython、coverage.py、MCP/ACP/A2A SDK、e2b/microsandbox/gVisor、React Flow 等前端栈 |
| **C 只研究,不作核心** | 被差异化的对象,耦合即自毁定位 | LangGraph、AutoGen、CrewAI、MetaGPT、AFlow |
| **D 出界,不引** | 违反 spec-only 边界 | 任何训练/RL/SFT 框架(TRL、verl、ReVeal 的 TAPO 等) |

---

## 按 Layer 的重用地图

分层对应 [architecture.md](architecture.md) "推荐系统分层"。

### Layer 1 — Spec / Schema
| 需求 | 选择 | 级别 | 说明 |
|---|---|---|---|
| 数据模型 | **Pydantic v2** | A | §2 全部模型 |
| 机判 schema 校验 | **jsonschema** | A | `IOContract.out_jsonschema()` 的执行后端 |
| AgentSpec / IOContract / GateResult 本身 | **自建** | — | 真相源,不可外包 |

### Layer 2 — Planning / Construction(LLM 调用)
| 需求 | 选择 | 级别 | 说明 |
|---|---|---|---|
| 默认 LLM 后端 | **`anthropic` 官方 SDK** | A | 主后端 |
| 多 provider 可插拔 | **LiteLLM** | B | 兑现 executor-agnostic;planner/codegen/verifier 分别配模型 |
| 结构化输出 | **Instructor** | B | pydantic→自动重试;但**保留原生 JSON schema 路径**,故为 adapter 非必需(§7.5 M2) |
| 本地模型约束生成 | Outlines / Guidance | B | 仅本地部署;token 约束,非云 API 默认 |
| 重试/退避 | **tenacity** | B | 轻量 |

### Layer 3 — Runtime / 编排 ⚠️ 核心自建
| 需求 | 选择 | 级别 | 说明 |
|---|---|---|---|
| DAG 拓扑序 + 环检测 | **`graphlib.TopologicalSorter`(stdlib)** | A | 自带 topo + `CycleError`,**零依赖**够 M0 |
| 图算法 / 受影响子图 | networkx | B(可选) | `affected_subgraph` 等高级分析 |
| Coordinator / ContextStore / ArtifactLoader | **自建** | — | 核心 |
| LangGraph / AutoGen / CrewAI / MetaGPT | **不作核心 runtime** | C | 正是被差异化的对象;当核心会绑死 `Coordinator/RuntimeGate/RecoveryRouter`,自毁 control-plane 定位 |

### Layer 4 — Verification(机判自建,模型判可借)
| 需求 | 选择 | 级别 | 说明 |
|---|---|---|---|
| forbidden import 扫描 | **`ast`(stdlib)** | A | 扫 import 节点 |
| 正则 / contains / not_contains | **`re`(stdlib)** | A | PatternBackend |
| validator 护栏后端 | Guardrails AI | B | 可选 `VerifierBackend` / `ToolGate` 后端,映射 PII/secrets/injection/provenance 等安全与事实性检查;不替代 `GateResult`/归因/恢复 |
| 对话安全规则 | NeMo Guardrails | B(慎) | 仅对话类;非对话过杀 |
| verifier 校准(eval 期) | **Promptfoo / DeepEval / Ragas** | B | golden case 跑 precision/false_accept;**不在 runtime gate 实时调** |
| GateResult / StructuredFeedback / 三级归因 | **自建** | — | 差异化命根子 |

### Layer 4b — 沙箱 ⚠️ 该重用别自建
| 需求 | 选择 | 级别 | 说明 |
|---|---|---|---|
| 跑生成代码 / python_assert | **e2b** 或 **microsandbox** | B | 专为"跑 LLM 生成代码"造,省自建隔离 |
| 强隔离 | **gVisor / Firecracker** | B | 多租户/对抗(§7.1 强隔离档) |
| 轻量本地 | nsjail / bubblewrap / `subprocess+resource+seccomp` | B | MVP 档(§7.1) |
| RestrictedPython | 谨慎 | — | 进程内限制,**不足以隔离不可信代码** |

### Layer 5 — Recovery
| 需求 | 选择 | 级别 |
|---|---|---|
| ErrorAttributor / RecoveryRouter / BudgetMeter / SurfaceFailure | **自建** | — |

### Layer 6 — Adapter / 协议(重用官方 SDK)
| 需求 | 选择 | 级别 | 说明 |
|---|---|---|---|
| MCP(工具/资源) | **官方 `mcp` Python SDK** | B | ToolRegistry/ContextStore 边界 |
| Claude Code 后端 | **Claude Agent SDK / Claude Code CLI** | B | ClaudeCodeAdapter |
| opencode 后端 | **opencode CLI / ACP** | B | OpenCodeAdapter(用 Zed 系 ACP) |
| 远端 agent | **a2a SDK** / ACP | B | 注意 ACP 有 IBM-BeeAI 与 Zed 两套同名,按对接对象选 |
| 结构化模型 provider | **Anthropic/OpenAI 官方 Python SDK** | B | 只作为 `StructuredLLM` adapter;Stage/Runtime 只看 `generate(system,user,json_schema)->dict` |
| 代码 AST(代码任务/验证) | **tree-sitter** | B | 跨语言 AST |
| 代码 workspace / diff | **GitPython**(或 `git worktree` subprocess) | B | 正好支撑 external_agent 威胁模型(一次性 worktree + diff 过 gate) |
| 可执行证据 | **pytest + coverage.py** | B | 代码任务的硬证据 |
| 代码智能 | LSP(multilspy / pygls) | B(可选) | symbol/references/diagnostics |

### 可观测
| 需求 | 选择 | 级别 |
|---|---|---|
| Trace 导出 | **OpenTelemetry SDK**(TraceEvent → span)、structlog | A/B |
| MVP 存储 | JSONL(stdlib) | A |

### 前端(见 [frontend.md](frontend.md))
| 需求 | 选择 | 级别 |
|---|---|---|
| DAG 画布 | **React Flow** | B |
| spec/JSON 查看编辑 | Monaco Editor | B |
| trace/gate 表格 | TanStack Table | B |
| 实时事件流 | AG-UI / SSE / WebSocket | B |

### 测试(见 [testing.md](testing.md))
| 需求 | 选择 | 级别 | 说明 |
|---|---|---|---|
| 单元/属性/覆盖 | **pytest + hypothesis + pytest-cov** | A | 确定性核 |
| 源码变异测试 | mutmut / cosmic-ray | B(可选) | ⚠️ 测**我们自己代码**质量;与 testing.md 的 `MutationCase`(变异 agent 输出/spec,bespoke)是两回事,别混 |

---

## 按里程碑的引入时机

只在需要时引入,避免前期背全套(对齐 §8 排期):

| 里程碑 | 新增依赖 | 备注 |
|---|---|---|
| **M0** | pydantic、jsonschema、`graphlib`(stdlib)、pytest、hypothesis | 纯确定性核,**不接 LLM** |
| **M1** | (沙箱最小档)`subprocess`+resource;OpenTelemetry/structlog | RuntimeGate 机判 + python_assert 最小沙箱 |
| **M2** | `anthropic` SDK、LiteLLM、Instructor(可选) | 构造期接真实 LLM;保留原生 JSON 路径 |
| **M3** | Promptfoo/DeepEval、e2b/microsandbox 或 gVisor、MCP/A2A SDK、Claude Code/opencode adapter、tree-sitter/GitPython/pytest 证据、React Flow 前端 | 增强 + 沙箱加固 + control-plane adapter + 前端 |

---

## 为什么编排框架(C 级)不作核心

LangGraph / AutoGen / CrewAI / MetaGPT 都能跑多 agent 图,诱惑很大。但它们与本项目是**同层竞争**关系,不是基础设施:

- 本项目的卖点是 **typed 验证 + 三级归因 + 类型化恢复**,这要求 `Coordinator/RuntimeGate/RecoveryRouter` 由我们完全掌控。
- 若用 LangGraph 当 runtime,DAG 调度、状态路由、错误处理都进了它的抽象,我们的"每输出消费前过 gate""failure_type 驱动最小代价恢复"就要去迁就它的生命周期——核心差异化被稀释。
- 正确做法:**研究**它们的 DAG/状态/人审设计(取经),但 runtime **自建**;它们若暴露为可调用 agent,经 ACP/A2A adapter 当 `external_agent` 执行后端即可(Layer 6),而非核心。

这与 conception.md "不是单纯的 AutoGen/LangGraph 替代品"和 architecture.md "control plane 不绑定执行后端"一致。

---

## spec 编写 / 对齐层(agent-spec / spec-kit / OpenSpec 类):另一层,不作核心

[agent-spec](https://github.com/ZhangHanDong/agent-spec)(Rust CLI)、[OpenSpec](https://github.com/Fission-AI/OpenSpec)(TS/npm)、[GitHub spec-kit](https://github.com/github/spec-kit)(Python CLI `specify`)这类"spec-driven 开发工作流"和本项目同名"spec-driven",但**海拔不同**,不能用于 AgentSpec 核心:

| 维度 | agent-spec / OpenSpec / spec-kit 的 spec | 本项目 AgentSpec(§2) |
|---|---|---|
| 本质 | 人机对齐的**任务契约/需求文档** | 机器可执行的**契约真相源** |
| 格式 | DSL / Markdown / BDD contract | Pydantic / JSON Schema(`FieldSpec`/`out_jsonschema`) |
| 消费者 | 人、LLM/code agent、CLI verifier | **确定性 runtime**(RuntimeGate/Coordinator/ErrorAttributor) |
| 阶段 | 写码**前**的对齐 + task-level guard | 编译→执行→**验证**→恢复(运行时) |
| 语言 | Rust / TypeScript / Python 等 | Python/Pydantic |

**为何 AgentSpec 核心不重用**:OpenSpec"spec = Markdown 散文"正是本项目刻意抛弃的形态——`IOContract` 已从 `"type -- desc"` 自由串升级为可机判 `FieldSpec`(就是为了让 `RuntimeGate` 机判),用 Markdown 当 spec 会推翻"机判优先"与"spec 是机器真相源"(conception 原则 1)。

**agent-spec 的特殊价值**:相比 spec-kit/OpenSpec 更偏"写码前对齐",agent-spec 已经把 task contract 拆成 `Intent` / `Decisions` / `Boundaries` / `Completion Criteria`,并把 BDD scenario 绑定到显式 `Test:` selector,还提供 `lifecycle` / `guard` / `explain` / `stamp` 这类可接 CI 的质量门。它对本项目最有价值的是**上游 TaskContract authoring + deterministic acceptance evidence**:

```text
agent-spec Task Contract
  Intent              → ParsedIntent / SwarmPlan.summary
  Decisions           → SwarmPlanner constraints / ConstitutionRule
  Boundaries          → allowed_paths / forbidden_patterns / external_agent diff gate
  Completion Criteria → TestCase / GoldenVerificationCase / machine_assertions
  lifecycle/guard     → 构造期/执行期 evidence,可作为 RuntimeGate 的外部证据
```

但它仍不能替代 `AgentSpec`:agent-spec 验证"代码是否满足 task contract",不负责编译多 agent DAG、字段级 `ContextStore` 数据流、local/upstream/structural 归因、`RecoveryRouter` 或 external_agent 运行时 gate。正确关系是**前门/旁路 verifier**,不是核心 runtime。

**唯一可能(可选、低优先、是约定非库)**:可坐在 **IntentParser 上游**当人机对齐"前门",把自然语言任务 + 约束在编译成 AgentSpec DAG **之前**结构化。若选择接入,优先做 `agent-spec contract/plan/lifecycle --format json` 的 CLI adapter,而不是把 agent-spec DSL 直接塞进 §2 schema。但 PoC 不需要(M0–M2 无关);真要做人审/对齐,frontend.md 的 cockpit + AG-UI 更贴合本项目的 trace/gate 体系。

**spec-kit vs OpenSpec(若选前门,spec-kit 更优)**:
- **同语言**:spec-kit 是 Python 3.11+/uv,没有 OpenSpec 的 TS↔Python 摩擦;GitHub 背书、30+ agent 生态 → 作可选前门的**首选候选**。
- **constitution 撞名,别混**:spec-kit 有 `.specify/memory/constitution.md`(散文,给 LLM 读);本项目 `ConstitutionRule`(§2)是**可机判 `machine_assertion`、gate/planner 强制、不可覆写**。概念收敛是好事,但实现海拔不同,**不可互替**。
- **"executable spec"是话术**:spec-kit 宣称 spec "executable",实为 Markdown 经 prompt 喂 agent,**不被机器执行**;本项目是字面机器执行(`RuntimeGate` 跑 `out_jsonschema()`/`AssertionSpec`)。**共享措辞、含义相反**,勿据此误判可重用。
- **互补而非替代**:spec-kit 的 `/implement` 把活交给 Copilot/Claude Code,但**无运行时验证/gate/归因/恢复**——这恰是本项目差异化。理论上 spec-kit 当前门 + 本引擎当受验证执行控制面可互补,但集成成本高、PoC 无关。

> 通用判据:凡"spec = 给人/LLM 读的散文、写码前对齐"的工具(agent-spec / spec-kit / OpenSpec 类)→ 至多 IntentParser 上游约定或外部 evidence backend;**绝不**进入 AgentSpec 真相源。AgentSpec 必须可机判、由 runtime 消费。"executable specification"措辞在这类工具里多指"约束 agent 生成代码/跑 task-level gate",与本项目"被 runtime 机器执行"含义不同。

---

## 选型决策树

```
要解决什么?
├── 数据模型 / schema 校验 ──────────────→ Pydantic + jsonschema(A,自建 spec)
├── DAG 调度 / 环检测 ──────────────────→ graphlib(A);不要 LangGraph 当核心(C)
├── LLM 调用 ──────────────────────────→ anthropic SDK(A)+ LiteLLM 多 provider(B)
├── 结构化输出 ────────────────────────→ 原生 JSON schema(A)/ Instructor(B,可选)
├── 机判验证 ──────────────────────────→ ast/re/jsonschema(A,自建 backend)
├── 模型判验证校准(eval 期)───────────→ Promptfoo/DeepEval(B)
├── 跑不可信代码 ──────────────────────→ e2b/microsandbox/gVisor(B,别自建)
├── 接外部 code agent ─────────────────→ Claude Agent SDK / opencode / MCP/ACP/A2A(B,adapter)
├── 代码证据(AST/diff/test)──────────→ tree-sitter/GitPython/pytest(B)
├── trace/可观测 ──────────────────────→ OpenTelemetry(A/B)
└── 前端 cockpit ──────────────────────→ React Flow + Monaco + AG-UI(B)
```
