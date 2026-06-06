# VERIMAP 分析

## 论文信息

- **标题**: Verification-Aware Planning for Multi-Agent Systems
- **发表**: EACL 2026 / arXiv 2510.17109
- **代码**: https://github.com/megagonlabs/veriMAP (开源)

## 核心思想

VERIMAP 是 Meta-Agent 验证机制的**直接前身**。它首次将验证函数 (Verification Functions, VFs) 显式嵌入 multi-agent 规划图中，让每个 agent 的输出在执行时经过验证门控。

核心创新：Plan → Execute → Verify 循环，而非传统的 Plan → Execute → (事后检查)。

## 架构

VERIMAP 的核心组件：

```
Input Task → Verification-aware Planner
  → DAG of subtasks (每个 subtask 带 I/O 契约 + VFs)
  → Coordinator (按 DAG 拓扑序调度)
  → Executor (执行 subtask)
  → Verifier (验证输出, 三种类型)
  → Task Result
```

### 三种验证器类型

从开源代码直接提取：

#### 1. BaseVerifier — LLM-as-Judge

```python
def verify(self, verify_prompt, agent_input, agent_output) -> Dict:
    prompt = self.prompts.verify_agent.format(...)
    response = model_prompting(model, prompt, temperature=0.0)
    verification_data = json.loads(response)
    # 必须包含: {"success_score": float, "reasoning": str}
    return verification_data
```

- temperature=0.0 确保确定性
- 结构化 JSON 输出
- 容错: JSON 解析失败 → 默认 success_score=0.0

#### 2. StructuredVerifier — 可执行代码验证

```python
def verify(self, verification, agent_input, agent_output):
    name, code = verification["name"], verification["code"]
    # 在 agent 的 workspace 中执行验证代码
    exec_locals = {**tool_handlers, "task_input": agent_input, "task_output": agent_output}
    exec(code, exec_locals)  # AssertionError → 验证失败
    return {"success_score": 1.0 or 0.0, "reasoning": [...]}
```

- 验证条件是 Python assert 语句
- 在 agent 的 workspace 中执行，可访问工具函数
- AssertionError → 失败，SyntaxError → 验证代码本身有 bug

#### 3. AgentVerifier — Agent-as-Verifier

```python
class AgentVerifier:
    def __init__(self, llm, verifier_config, environment):
        self.react_agent = ReActAgent(config=..., env=environment, model=verifier_llm)
        self.react_agent.verification = "none"  # 关键: 防止无限递归

    def verify(self, verify_prompt, agent_input, agent_output):
        response = self.react_agent.act(prompt)
        return parse_json(response)
```

- 验证器是完整的 ReActAgent，可使用工具、搜索、执行代码
- `verification="none"` 截断验证递归
- 适用于需要多步推理判断正确性的任务

## 协调模式

VERIMAP 支持多种协调模式：

| 模式 | 说明 |
|------|------|
| none | 单 agent baseline |
| DAG | DAG 协调 (最常用) |
| Chain | 链式协调 |
| Graph | 图协调 |

## 支持任务

五类任务: Code Generation (HumanEval, BigCodeBench-Hard), Reasoning (BBH), RAG (MultiHopRAG), Math (GSM8K, Olympiads), Research.

## 与 Meta-Agent 的关键差异

| 维度 | VERIMAP | Meta-Agent |
|------|---------|-----------|
| 验证器类型 | 三种独立类型 | 统一 verifier model + typed routing |
| 构造期验证 | 无 (仅执行期) | 静态 + 行为双重检查 |
| 失败分类 | pass/fail | F={spec_adherence, grounding, contract} |
| 恢复路由 | 重试 agent | 类型化路由 → 精确定位责任阶段(§6) |
| 自动 Agent 构造 | 无 (需手工配置) | 5-Stage 构造期全自动 |
| Grounding | 无 | Stage 3 Web Search grounding |

VERIMAP 是 Meta-Agent 的"验证引擎原型"——Meta-Agent 继承了其三种验证器思想，增加了构造期验证、类型化失败信号和三级错误归因，形成了一个完整的 agent 系统合成框架。

## 工程启示

1. **三种验证器各有适用场景**：BaseVerifier 最通用，StructuredVerifier 最可靠（对可形式化的任务），AgentVerifier 最强（对复杂任务）但最贵
2. **验证递归必须被截断**：AgentVerifier 的 `verification="none"` 是关键设计——不加这行会无限递归
3. **Python assert 是简洁而强大的验证原语**：StructuredVerifier 的核心就是 `exec(assert_code)`——零幻觉、可审计
4. **DAG 协调模式是最实用的**：VERIMAP 文档建议"you should use DAG most of the time"

## 技术声明

分析基于 VERIMAP 开源代码仓库 (megagonlabs/veriMAP) 的直接阅读，包括 `verifier/base_verifier.py`, `verifier/structured_verifier.py`, `verifier/agent_verifier.py`, `main.py`, 和 README.md 的完整内容。代码为 Python，许可未在仓库中明确标注。
