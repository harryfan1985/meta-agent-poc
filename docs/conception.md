# Conception: Spec-Driven Multi-Agent Engine

## 一句话定义

`meta-agent-poc` 的工程范围可以理解为一个 **spec-driven multi-agent system engine / compiler-runtime**。

它不是单纯的 agent 框架,也不是单纯的 workflow runner,而是把自然语言任务编译成可验证、可执行、可恢复的多 Agent DAG 的引擎:

```text
自然语言任务
  → 编译成带契约的 multi-agent DAG
  → 生成或装配 agent 实现
  → 按 DAG 执行
  → 在构造期和执行期做验证
  → 失败后基于类型化归因做最小代价恢复
```

## 双重身份

这个项目同时包含两个核心身份:

```text
Compiler: 任务描述 → AgentSpec DAG
Runtime:  执行、验证、恢复这个 DAG
```

因此它的核心不在于某个具体模型、某个 benchmark 或某种 prompt 编排方式,而在于下面这组引擎级抽象:

```text
AgentSpec
IOContract
VerificationCriteria
DAG
RuntimeGate
ErrorAttributor
RecoveryRouter
```

## 范围边界

这个项目是:

- spec-driven multi-agent engine
- agent DAG compiler/runtime
- verified agent workflow runtime
- contract-based agent orchestration system
- 可恢复的多 agent 执行引擎

这个项目不是:

- 普通 chat agent
- 只负责 prompt 编排的 workflow 框架
- 单纯的 AutoGen / LangGraph 替代品
- 模型训练或微调系统
- 通用 sandbox 平台

## 核心能力

最终系统应支持:

- 从自然语言任务生成结构化 `ParsedIntent`
- 规划带 I/O 契约的 `AgentSpec` DAG
- 使用 `ToolRegistry` 管理工具定义和权限
- 通过 `ArtifactLoader` 装配 fixture、prompt template 或 Python module agent
- 由 `Coordinator` 按 DAG 拓扑序执行
- 由 `ContextStore` 按直接依赖路由中间产物
- 由 `RuntimeGate` 在消费前验证每个 agent 输出
- 由 `ConstructionVerifier` 在构造期检查 agent 是否满足 spec
- 由 `ErrorAttributor` 区分 local / upstream / structural 错误
- 由 `RecoveryRouter` 执行最小代价恢复
- 通过 `GateResult` 和 `StructuredFeedback` 保留类型化、可溯源的失败信号

## 设计原则

1. **Spec 是真相源**  
   Agent 的角色、输入输出、工具、依赖和验证标准都必须结构化表达。

2. **DAG 是数据流真相源**  
   下游只能消费直接依赖的上游输出,不能依赖隐式全局上下文。

3. **验证不是布尔值**  
   验证失败必须带类型,用于决定回退到 codegen、grounding、planning,或执行期的 local/upstream/structural 恢复。

4. **机判优先于模型判**  
   schema、字段、正则、AST、工具名、DAG 环检测等都应优先用代码判断。

5. **模型验证只处理残差**  
   语义类验证进入 claim/evidence 或 aspect verifier 流程,不能替代机判。

6. **恢复成本匹配错误局部性**  
   能本地重试就不重跑上游,能重跑上游就不重建全图。

7. **不返回未验证答案**  
   预算触顶或验证失败时 surface failure,而不是给出未经验证的最终结果。

## 产品化表述

> 一个把自然语言任务编译成可验证、可执行、可恢复的多 Agent DAG 的 spec-driven engine。

## 工程化表述

> 核心是 `AgentSpec + IOContract + VerificationCriteria + DAG + RuntimeGate + RecoveryRouter` 这一套 compiler-runtime,而不是某个具体模型后端或单一 agent workflow。

## 具体实例

以上抽象不是空中楼阁:设计文档[附录 A](meta_agent_spec_driven_plan.md) 把论文 function-completion swarm(`has_close_elements`)用现行 schema **完整实例化**了一次——4 个 `AgentSpec` + DAG + I/O 契约 + 机判断言 + fixture + 端到端 gate 验收。它既验证了 schema 足以表达真实任务,也是 M0 可直接抄的种子。要理解这套引擎"长什么样",从附录 A 入手最快。
