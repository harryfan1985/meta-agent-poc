# meta-agent-poc

[![CI](https://github.com/harryfan1985/meta-agent-poc/actions/workflows/ci.yml/badge.svg)](https://github.com/harryfan1985/meta-agent-poc/actions/workflows/ci.yml)

> Spec 驱动的多 Agent 编码系统 —— 论文 *Meta-Agent: From Task Descriptions to Verified Multi-Agent Systems* 的工程落地 PoC。

把"为某个任务搭一套多 agent 系统"本身变成一条**自动化流水线**:

```
自然语言任务描述
  └─▶ 编译出带 I/O 契约与验证标准的 agent DAG
       └─▶ 为每个节点生成可执行 agent 代码
            └─▶ 构造期 + 执行期双重验证
                 └─▶ 失败时按「错误类型」做最小代价回退
```

核心论断:reliability 不靠事后 self-reflection 补救,而是把**显式、结构化的验证**贯穿构造与执行两个阶段,并用**三级错误归因**(local < upstream < structural)让恢复成本与错误局部性成正比。

## 当前状态

**M0–M3 框架已落地**(verifier 后端栈 / 校准 / claim-evidence / mutation / 沙箱加固 / 消融 / HumanEval benchmark + 正确性 oracle),`src/meta_agent/`,229 测试,覆盖约 94%,确定性测试不依赖真实 LLM:

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **M0** | schema / DAG / `gather_inputs` / RuntimeGate 机判 / ArtifactLoader 四形态 / Coordinator / 附录 A 端到端 | ✅ |
| **M1** | 三级归因 + 恢复闭环 + 预算 + 工具门 + 构造期校验预检 + golden + coverage | ✅ |
| **opencode** | `OpenCodeAdapter`(control-plane 执行后端,产物经 RuntimeGate) | ✅ |
| **可观测** | `TraceEvent` 接入执行期与 external agent adapter(JSONL/回放数据底座) | ✅ |
| **M2 thin** | `construct()` + Stage 1/2/3 StructuredLLM 接缝 + Stage 4 `prompt_template` + Stage 5 `ConstructionVerifier` + 受限 `python_assert` 机判后端 | ✅ |
| **M2 eval** | `eval_harness`(成功率 + 失败类型/阶段分布)+ `StructuredLLM` adapter(Anthropic/OpenAI/OpenAI 兼容 URL,`json_object` 降级)。对 bitfun 实测:小任务集(3 任务)成功率经 RuntimeGate 健壮化 + planner 断言收敛后 2/6 → 15/15;失败路由按 phase/failure_type 可观测 | ✅ |
| **M3 verifier 后端栈** | `VerifierBackend` 协议 + `VerifierRegistry`(首个支持者,不降级)+ `BaseJudgeBackend`/`AspectPanelBackend`(多 aspect 投票);`model_check` 接入 RuntimeGate/execute/ConstructionVerifier:未允许或无后端 → fail-closed(contract),有后端 → 评判(spec_adherence)。偏差缓解:隐生成器身份 + 结构化 verdict,解析失败=验证失败 | 🟡 |
| **M3 校准** | `calibration`:对 verifier 后端跑 golden cases,量化 precision/recall/F1/false-accept/false-reject;`calibrated_backends` 按阈值过滤(未达标不进 hot path)。bitfun 实测 F1=1.0、false-accept=0(集偏清晰,borderline 集待补) | 🟡 |
| **M3 claim-evidence** | `claims`:抽 claim → 挂可溯源证据 → 逐 claim 核查 → 聚合(§4.5)。`ClaimEvidenceBackend` 可注册进 registry;证据作数据非指令,证据不足 insufficient 不放行 | 🟡 |
| **M3 mutation** | `mutations`:往合法输出注入缺陷(drop/wrong/forbidden/off-by-one),验 gate 抓得到;暴露存活变异(验证器盲区)。kill_rate 报告 | 🟡 |
| **M3 沙箱** | `sandbox`:python_assert 资源边界(表达式长度/AST 节点数/深度上限 + 禁 `*`/`**` 防内存膨胀);net/fs/import 已被表达式语法层挡掉(§7.1) | 🟡 |
| **M3 消融** | `ablation`:在 `eval_harness` 上切换验证/grounding 旋钮量化各自贡献;Stage 3 grounding 接缝(注入 `web_search` 写回 `spec.grounding`)。框架就绪 | 🟡 |
| **M3 benchmark** | `code_sandbox`(子进程 + setrlimit + wall 超时跑不可信代码)+ `benchmarks`(HumanEval 加载 + 确定性正确性 oracle)+ `eval_harness`/`ablation` 接 oracle 计分。bitfun 实测 builtin 子集 pass@1=0/3(失败在 construct/execute,oracle 计分链路验证通过)——thin 流水线 codegen 待加强 | 🟡 |
| **M3 ⏭️** | 提升 HumanEval pass@1(code 专用 spec/codegen)/ 真实 HumanEval 全集 + 更多数据集 / verifier 防偏差 swap-test / 真实 web_search provider | ⏭️ |

> 快速跑通:`pip install -e ".[dev]" && pytest`。

真实模型 smoke/eval:

```bash
pip install -e ".[models,dev]"
export ANTHROPIC_API_KEY=...
export META_AGENT_ANTHROPIC_MODEL=...
META_AGENT_RUN_PROVIDER_EVAL=1 pytest tests/eval -q

meta-agent-model-eval \
  --provider anthropic \
  --model "$META_AGENT_ANTHROPIC_MODEL" \
  --task "Build a small swarm for a simple coding task" \
  --task-input-json '{"task":"say hello"}'
```

OpenAI-compatible provider 复用 `openai` adapter,只需指定兼容服务的 base URL:

```bash
export OPENAI_API_KEY=dummy-or-real
export META_AGENT_OPENAI_MODEL=local-model
export META_AGENT_OPENAI_BASE_URL=http://localhost:8000/v1
META_AGENT_RUN_PROVIDER_EVAL=1 pytest tests/eval -q

meta-agent-model-eval \
  --provider openai \
  --model "$META_AGENT_OPENAI_MODEL" \
  --base-url "$META_AGENT_OPENAI_BASE_URL" \
  --task "Build a small swarm for a simple coding task"
```

M2 task-level E2E 默认用 Anthropic;如要用 OpenAI-compatible endpoint,沿用上面的
`OPENAI_API_KEY` / `META_AGENT_OPENAI_MODEL` / `META_AGENT_OPENAI_BASE_URL`,并设置:

```bash
export META_AGENT_M2_PROVIDER=openai
export META_AGENT_RUN_M2_E2E=1
META_AGENT_RUN_PROVIDER_EVAL=1 pytest tests/eval -q
```

`StructuredLLM` provider adapter 只负责把 `system + user + JSON Schema` 变成 dict;输出仍会经过本项目自己的 `jsonschema`/Pydantic/RuntimeGate 校验。

设计文档:

- 📄 **[docs/meta_agent_spec_driven_plan.md](docs/meta_agent_spec_driven_plan.md)** —— 唯一的设计真相源(数据模型、构造期/执行期设计、错误归因、技术选型、落地路线、评测协议)。
- 📄 **[docs/conception.md](docs/conception.md)** —— 项目概念定义与范围边界:spec-driven multi-agent engine / compiler-runtime。
- 📄 **[docs/architecture.md](docs/architecture.md)** —— 系统架构与 code agent 集成方式:Meta-Agent Engine 作为 control plane,Claude Code/opencode/Pi agent 作为 execution plane。
- 📄 **[docs/frontend.md](docs/frontend.md)** —— 前端设计:graph-first workflow debugger / verification cockpit。
- 📄 **[docs/testing.md](docs/testing.md)** —— 实施前的测试策略与测试用例(按 M0–M3 + 组件,确定性核单测 / 模型判金标准 / mutation 等 meta 验证)。
- 📄 **[docs/implementation-stack.md](docs/implementation-stack.md)** —— 开源项目重用分析:自建控制面 vs 重用执行面,按 Layer/里程碑分级(A 采用 / B 选用 / C 只研究 / D 出界)。
- 📄 **[docs/analysis/](docs/analysis/README.md)** —— 相关论文与开源验证工具调研(MAV、DeepVerifier、VERIMAP、Agent-as-Judge、Guardrails 等),作为设计参考而非真相源。
- 📄 **[AGENTS.md](AGENTS.md)** —— 给在本仓库工作的 AI agent / 协作者的项目指引。

## 系统组成

| 阶段 | 组件 | 职责 |
|---|---|---|
| 构造期 Stage 1 | `IntentParser` | 任务描述 → 结构化 `ParsedIntent`(含 web 检索补任务示例) |
| 构造期 Stage 2 | `SwarmPlanner` | 分解为少量子任务 → `AgentSpec` 列表 + DAG |
| 构造期 Stage 3 | `GroundingResearcher` | 按 spec 定向检索,把外部知识写回 spec |
| 构造期 Stage 4 | `AgentCodeGen` | 每个 spec → `prompt_template` / Python 模块 / 外部 code agent artifact |
| 构造期 Stage 5 | `ConstructionVerifier` | 静态 + 行为双检,产出**带类型**的失败信号 |
| 执行期 | `Coordinator` / `ContextStore` | 按 DAG 拓扑序调度,路由中间产物 |
| 执行期 | `RuntimeGate` | 每个中间输出消费前校验 `yᵢ ∈ Cᵢ` |
| 执行期 | `ErrorAttributor` + `RecoveryRouter` | 分类 local / upstream / structural 并选恢复策略 |

## 落地路线

| 里程碑 | 内容 |
|---|---|
| **M0** | Pydantic schema + `ArtifactLoader` + `Coordinator` + `ContextStore` + 拓扑执行 + DAG 环检测;用手写 `SwarmPlan` 配置 + fixture artifacts 跑通论文 4-agent swarm |
| **M1** | `RuntimeGate` + `ErrorAttributor` + `RecoveryRouter`;四类错误归因单测 |
| **M2** | 构造期 Stage 1→5 全流水线;带类型失败信号 + 类型化路由;先以 `prompt_template` thin impl 跑通,再接真实 LLM eval |
| **M3** | 6 个 benchmark 跑分 + 消融 + 成本预算 + 沙箱加固 |

详见 [设计文档 §8](docs/meta_agent_spec_driven_plan.md)。

## 技术栈

- **Python 3.11+** —— 引擎语言;artifact 可为 fixture/prompt/python_module/external_agent
- **Anthropic API**(默认,可插拔)—— 框架 executor-agnostic,各组件可分别配模型
- **Pydantic** —— 所有 Stage 产物按 schema 强校验
- **沙箱执行** —— 生成代码默认不可信,强隔离 + 禁网 + 超时 + 资源上限

> 论文出处:Andy Xu, Yu-Wing Tai (Dartmouth), arXiv:2605.25233, 2026。
