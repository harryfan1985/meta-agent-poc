# 开源验证工具分析

## 概述

以 Meta-Agent 验证机制的三类需求（Type 1: LLM-as-Judge、Type 2: Structured/Code 验证、Type 3: Agent 级验证）为框架，分析 8 个开源工具。

---

## 一、Structured Output 基础设施

### Instructor

- **GitHub**: https://github.com/567-labs/instructor
- **定位**: 从 LLM 提取结构化数据的最快路径
- **核心机制**: Pydantic schema 定义 → LLM 输出 → 自动验证 → 失败自动重试
- **与 Meta-Agent 关系**: 可直接作为 `IOContract` 的 output 校验后端。`out_jsonschema()` 编译为 JSON Schema → Instructor 的 `response_model` 消费 → 自动重试直到 schema 匹配。覆盖 RuntimeGate 的 schema 校验。
- **降本**: 支持 15+ LLM provider，验证可用便宜模型
- **局限**: 仅做 schema 校验，不做语义验证 (behavioral_assertions)

### Outlines

- **GitHub**: https://github.com/dottxt-ai/outlines
- **定位**: 本地模型的 guaranteed schema compliance
- **核心机制**: Token 级约束生成——在生成时限制 token 选择，确保输出 100% 匹配 schema，零重试
- **与 Meta-Agent 关系**: 适合本地部署场景。如果在本地跑 codegen，Outlines 保证生成的 agent 代码语法正确
- **局限**: 需要本地模型支持，token 约束对推理质量有一定影响

### PydanticAI

- **GitHub**: https://github.com/pydantic/pydantic-ai
- **定位**: Pydantic 官方 agent runtime
- **核心机制**: 类型安全的 tool calling + structured output + agent loop
- **与 Meta-Agent 关系**: 可作为生成 agent 的 runtime 替代手写 `run()` 模板。PydanticAI agent 天然带 output validation
- **局限**: agent 框架而非验证框架——验证是副产品

### Guidance

- **GitHub**: https://github.com/guidance-ai/guidance
- **定位**: Regex + CFG 约束生成
- **核心机制**: 用正则表达式或上下文无关文法精确控制 LLM 输出格式
- **与 Meta-Agent 关系**: 适合需要精确格式控制的场景（如代码生成中的特定语法约束）
- **局限**: 社区活跃度下降，被 Instructor/Outlines 取代

---

## 二、Guardrails / 验证护栏

### Guardrails AI

- **GitHub**: https://github.com/guardrails-ai/guardrails
- **定位**: Input + Output 双向验证，自定义 validator
- **核心机制**: 

```python
from guardrails import Guard
guard = Guard().use(Validator1).use(Validator2)
result = guard(model_output)
```

- **与 Meta-Agent 关系**: 最直接可用的 RuntimeGate 实现。自定义 validator 可映射到 `behavioral_assertions` 和 `forbidden_patterns`
- **局限**: validator 需要手工编写（不如 VERIMAP 的 StructuredVerifier 自动生成）

### NVIDIA NeMo Guardrails

- **GitHub**: https://github.com/NVIDIA/NeMo-Guardrails
- **定位**: 企业级对话安全护栏
- **核心机制**: 基于 Colang 语言定义对话安全规则
- **与 Meta-Agent 关系**: 适合对话类 agent 的安全验证，与 Meta-Agent 的 `forbidden_patterns` 互补
- **局限**: 重型，对非对话任务过杀

### Promptfoo

- **GitHub**: https://github.com/promptfoo/promptfoo
- **定位**: LLM 输出评估 + 红队测试
- **核心机制**: 批量测试 prompt 变体，评估输出质量
- **与 Meta-Agent 关系**: 适合验证器的 calibration——用 Promptfoo 测试不同 AV prompt 的准确率
- **局限**: 评估工具，非实时验证

---

## 三、Agent 原生验证

### Claude Code Hooks

- **链接**: https://docs.anthropic.com/en/docs/claude-code/hooks
- **定位**: Pre/post-tool-call hook 机制
- **核心机制**: 在 agent 调用工具前/后执行自定义验证逻辑
- **与 Meta-Agent 关系**: 最轻量的"在现有 agent 上加验证"方案。pre-hook 可做 permission gate，post-hook 可做 output validation
- **优势**: 无需框架改动，直接在 Claude Code 上叠加
- **局限**: 绑定 Claude Code 生态

### OpenAI Agents SDK Guardrails

- **GitHub**: https://github.com/openai/openai-agents-python
- **定位**: 官方的 agent guardrails API
- **核心机制**: Input guardrail + Output guardrail，通过 decorator 注册
- **与 Meta-Agent 关系**: 与 Meta-Agent 的 RuntimeGate 同构——都是"消费前校验"
- **局限**: 绑定 OpenAI 生态

---

## 四、工具选择决策树

```
需要什么?
├── 结构化输出校验 (I/O Contract)
│   ├── 云 API → Instructor
│   └── 本地模型 → Outlines
├── 语义验证 (Behavioral Assertions)
│   ├── 可形式化 → VERIMAP StructuredVerifier (Python assert)
│   ├── 不可形式化 → MAV Aspect Verifier panel
│   └── 混合 → Meta-Agent 机判优先 + 模型判残差
├── 在现有 agent 上加验证
│   ├── Claude Code → Hooks
│   ├── OpenAI → Agents SDK Guardrails
│   └── 通用 → Guardrails AI
└── 验证器 Calibration
    └── Promptfoo
```

---

## 技术声明

分析基于各项目的 GitHub README、官方文档和公开 API 的阅读。Instructor、Outlines、Guidance 的定位对比来自 dev.to 的 "Top 5 Structured Output Libraries" (2026)。Guardrails AI 的描述来自 fast.io 的 "Best Guardrails Tools" (2026)。功能描述为公开信息，具体实现细节来自代码仓库的直接阅读。
