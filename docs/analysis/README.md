# Meta-Agent 相关论文与项目分析

以 Meta-Agent (arXiv:2605.25233) 的验证机制为核心，对相关学术论文和开源项目进行深入分析。

## 论文分析

| 文档 | 论文 | arXiv | 与 Meta-Agent 关系 |
|------|------|-------|-------------------|
| [mav.md](mav.md) | Multi-Agent Verification (MAV) | 2502.20379 | 并行可叠加：gate → committee |
| [reveal.md](reveal.md) | ReVeal: Self-Evolving Code Agents | 2506.11442 | Training-time 互补：内化验证能力 |
| [verimap.md](verimap.md) | VERIMAP | 2510.17109 | 验证机制前身：三种验证器原型 |
| [deepverifier.md](deepverifier.md) | DeepVerifier | 2601.15808 | 失败信号内容轴：rubric + source-checkable |
| [agent-as-judge-survey.md](agent-as-judge-survey.md) | Agent-as-a-Judge Survey | 2601.05111 | 验证器理论基础：LLM→Agent judge 演进 |
| [formal-verification-sdd.md](formal-verification-sdd.md) | Constitutional SDD + Formal Verification | 2602.02584 / 2507.13290 | 形式化保障：security-by-construction + FQL |
| [anthropic-when-ai-builds-itself.md](anthropic-when-ai-builds-itself.md) | When AI builds itself | Anthropic Institute, 2026.06 | 产业实践：递归自我改进的证据链 |

## 开源项目分析

| 文档 | 覆盖项目 |
|------|---------|
| [open-source-tools.md](open-source-tools.md) | Instructor, Outlines, PydanticAI, Guidance, Guardrails AI, NeMo Guardrails, Promptfoo, Claude Code Hooks, OpenAI Agents SDK Guardrails |

## 核心设计文档

- [../meta_agent_spec_driven_plan.md](../meta_agent_spec_driven_plan.md) — Meta-Agent PoC 完整设计（634行）
- [../../README.md](../../README.md) — 项目总览
- [../../AGENTS.md](../../AGENTS.md) — AI agent / 协作者指引

## 关联图谱

```
                    Meta-Agent 验证机制 (2605.25233)
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
      理论前身              并行可叠加          训练期互补
          │                   │                   │
     VERIMAP              MAV (2502)          ReVeal (2506)
     三种验证器            BoN-MAV             TAPO + RL
          │                   │                   │
          ├─ StructuredVerifier                  │
          │   ↓                                  │
          │  Outlines / Instructor               │
          │  Guardrails AI                       │
          │                                      │
          ├─ AgentVerifier ──→ Agent-as-Judge Survey (2601)
          │                   DeepVerifier (2601)
          │
          └─ BaseVerifier ──→ LLM-as-Judge
                              Formal Verification (2507)
                              Constitutional SDD (2602)
```
