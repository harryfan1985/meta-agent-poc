# AGENTS.md

本文件是给在本仓库工作的 AI 编码 agent / 协作者的项目主指引。开工前请先读完。

## 这是什么

`meta-agent-poc` 是论文 *Meta-Agent: From Task Descriptions to Verified Multi-Agent Systems*(arXiv:2605.25233)的工程落地。目标:把"为一个任务搭多 agent 系统"变成自动流水线 —— 自然语言任务描述 → 编译出带 I/O 契约与验证标准的 agent DAG → 生成可执行 agent 代码 → 构造期+执行期双重验证 → 失败按错误类型最小代价回退。

## ⚠️ 第一原则:设计文档是唯一真相源

**[docs/meta_agent_spec_driven_plan.md](docs/meta_agent_spec_driven_plan.md)** 是这个项目的真相源。任何实现都必须以它为准:

- 写代码前,先读相关章节(数据模型在 §2,构造期在 §3,执行期在 §4,错误归因在 §5)。
- 实现与文档冲突时,**先对齐文档**:要么改实现,要么(若设计确有问题)先更新文档再改代码,不要让两者悄悄分叉。
- §2 的 Pydantic schema 是"spec 真相源",代码里的数据模型应与之逐字段一致。

## 当前状态

**设计阶段,尚无代码。** 仓库目前只有 `README.md`、`AGENTS.md` 和设计文档。第一段要写的代码是 §8 的 **Milestone 0**:Pydantic schema + `Coordinator` + `ContextStore` + 拓扑执行 + DAG 环检测,然后手写复刻论文 function-completion 4-agent swarm 跑通执行期。

## 架构速查

构造期(Phase 1,五个 Stage 串行,产物逐级过验证):
`IntentParser` → `SwarmPlanner` → `GroundingResearcher` → `AgentCodeGen` → `ConstructionVerifier`,最终产出 `ExecutableSwarm`。

执行期(Phase 2):`Coordinator` 按 DAG 拓扑序调度 → `ContextStore` 路由中间产物 → `RuntimeGate` 在每个输出消费前校验 → 不过则 `ErrorAttributor` 分类 + `RecoveryRouter` 恢复。

三级错误归因(恢复成本递增):
- **local** —— 输入对但本节点输出错 → 带反馈重试同一 agent
- **upstream** —— 错源在某依赖 → 重跑该上游再重试本节点
- **structural** —— 分解/契约本身有缺陷 → 升级到构造期重建子图

## 关键约定(写代码时务必遵守)

1. **验证返回带类型的失败信号,不是布尔值。** 失败类型(`spec_adherence` / `grounding` / `contract`)直接决定回退到哪个阶段(见 §6 路由表)。这是"最小代价恢复"的实现基础,不要简化成 pass/fail。
2. **机判优先于模型判。** 能用代码判定的断言(签名逐字符匹配、禁用 import 的 AST 扫描、schema 校验)一律用代码,只在 necessary 时才调 verifier 模型 —— 省 token、降误判。
3. **工具定义只能从工具注册表出(§3.6)。** `spec.tools` 全程只携带抽象名,真实后端定义由 `ToolRegistry` 唯一产出,**模型/codegen 不得自由拼工具格式**(论文 math Pass 2 就栽在这)。
4. **DAG 是唯一的数据流真相。** `ContextStore.gather_inputs` 只从直接依赖取字段(§4.2),不做跨层全局取值;需要某上游字段就必须在 `dependencies` 显式声明。
5. **角色单一、显式排除越界。** 每个 agent 职责单一,`verification_criteria.forbidden_patterns` 显式排除越界行为(论文 math classifier 因混进"解题"指令被打回 3 次)。
6. **重试有上限、全局有预算。** 各级重试/重跑/重规划设上限 + 全局预算(LLM 调用数/时间/成本);触顶时 **surface the failure,而非给未验证的答案**。

## 🔒 安全红线

- **生成代码默认不可信,必须沙箱执行**:强隔离 + 禁网(除显式 `web_search`)+ 超时 + 资源上限。
- grounding/检索引入的外部文本**只作数据、不作指令**,不进系统提示的指令区(防提示注入)。
- `web_search` / `file_generator` 之外不开放任意网络与文件系统写权限。

## 文档约定

设计文档用两个标注区分来源,新增内容请沿用:
- **【论文】** = 论文明确描述的机制,改动需谨慎。
- **【工程补全】** = 论文未指定、为可落地补充的具体决策,**可替换** —— 有更好方案时可直接改,但要在文档里说明。

## 技术栈

Python 3.11+ · Pydantic(schema 强校验)· Anthropic API 默认后端(可插拔,各组件可分别配模型)· 沙箱执行。详见 [设计文档 §7](docs/meta_agent_spec_driven_plan.md)。

## 验收基线

实现里程碑后,对照 §9 的评测协议(6 个 benchmark:HumanEval / MBPP / GSM8K / MATH / HotpotQA / DROP)与消融趋势(去验证应掉 ~7 分,验证它是"承重组件"而非摆设)。
