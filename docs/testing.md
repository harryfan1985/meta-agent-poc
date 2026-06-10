# Testing Strategy & Test Plan

> 实施前的测试用例设计。真相源:[meta_agent_spec_driven_plan.md](meta_agent_spec_driven_plan.md)(§ 引用均指它),running example 见其附录 A。
>
> 标注:**[det]** 确定性、可精确断言;**[eval]** 非确定性、用金标准+阈值;**[meta]** 验证"验证器"本身;**[sec]** 安全。

---

## 0. 测试哲学(本项目特有)

这个引擎的职责是**验证别的东西**,于是有"谁来验证验证器"的递归问题。解法是一条贯穿全设计的二分:

| | 确定性核 | 非确定性边 |
|---|---|---|
| 组件 | DAG、`gather_inputs`、机判 RuntimeGate、ErrorAttributor、ToolRegistry、Budget、ArtifactLoader、check_constitution | 模型判 verifier(BaseJudge/AspectPanel/AgentVerifier)、AgentCodeGen、IntentParser |
| 对应设计原则 | **机判优先**(§conception 原则 4) | **模型判只处理残差**(原则 5) |
| 测法 | 单元/属性测试,**精确断言**,**不调 LLM** | **金标准评测 + 阈值**(precision/false_accept/mutation_score),**不写等值断言** |
| CI | 每次提交必跑、必须全绿 | nightly / 发布前;阈值回归 |

**三条铁律:**
1. **确定性核必须 100% 可在无 LLM 下测**——这也是 §3.5/§4.5 "机判优先"的可测性红利。任何核心逻辑若依赖真实 LLM 才能测,说明设计漏了机判层。
2. **LLM 一律 mock/stub**(M0–M2 单测),真实模型只进 [eval] 套件。codegen 首过率、judge 准确率是**指标**不是单测断言。
3. **边界:不训练/不 SFT** → 没有任何"训练/数据集/loss"测试;`docs/analysis/reveal.md` 的 TAPO 等训练机制不在测试范围。

测试金字塔(本项目重心**下沉**,因为核心是确定性的):

```
         /  E2E  \        附录A端到端 + 全新任务构造(少)
        / Eval/Meta \     judge 校准 / mutation / metamorphic(中,nightly)
       / Integration  \   execute loop / 构造流水线(中)
      /   Unit (det)    \  schema/DAG/gather_inputs/gate/归因(多,快,无 LLM)
```

---

## 1. 按里程碑的测试范围(只测已存在的东西)

| 里程碑 | 测试重点 | 类型 |
|---|---|---|
| **M0** | schema 校验、DAG 环检测/拓扑、`gather_inputs` 契约映射、`ArtifactLoader` 四形态、Coordinator 调度、**附录 A 端到端(fixture)** | [det] unit + integration |
| **M1** | RuntimeGate 三类机判、`GateResult`/`StructuredFeedback` 两轴、ErrorAttributor 三级归因、RecoveryRouter、ToolGate 机判、Budget、golden case 最小集建立 | [det] + 错误注入 integration |
| **M2** | 构造流水线 Stage 1→5、`registry.validate`、`check_constitution`、类型化路由、代表性输入生成、构造期 E2E | [det] + [eval](codegen) |
| **M3** | verifier 后端栈、校准、bias mitigation、disagreement、health/drift、mutation/metamorphic、claim/evidence、external_agent adapter、BoN、跨模型迁移回归 | [eval] + [meta] + [sec] |

---

## 2. 按组件的测试用例(核心交付)

### 2.1 DAG(`dag.py`)— [det]

| ID | 用例 | 期望 |
|---|---|---|
| DAG-01 | 附录 A 边集求拓扑序 | spec_analyzer 早于其余;synthesizer 早于 verifier |
| DAG-02 | 注入环 `a→b→a` | 拒绝并报 `CYCLE_DETECTED`(构造期 contract 失败) |
| DAG-03 | 自环 `a→a` | 拒绝 |
| DAG-04 | diamond `a→b,a→c,b→d,c→d` | 合法拓扑序,d 最后 |
| DAG-05 | `ready` 随 `store` 更新重算 | 仅依赖全就绪的节点进入 ready |
| DAG-06 | 孤立节点 / 多入口 | 入口集 = 入度 0 全体 |
| DAG-07 [property] | hypothesis 随机生成 DAG | 输出拓扑序对所有边满足 from 在 to 之前 |

### 2.2 `gather_inputs`(`context.py`)— [det]

| ID | 用例 | 期望 |
|---|---|---|
| GI-01 | 单上游单字段(planner←analyst.parsed_spec) | message 正确组装 |
| GI-02 | 多上游合并(synthesizer←analyst+planner) | 各字段取自正确来源 |
| GI-03 | 必填字段无来源 | 抛 `ContractMismatch(unresolved)` |
| GI-04 | 可选字段(不在 required_in)无来源 | 跳过,**不报错** |
| GI-05 | 两上游产同名字段 | 抛 `ContractMismatch(conflicts)` |
| GI-06 | task_input 接线:入口节点 | 从 `__task_input__` 取到 raw_signature/docstring |
| GI-07 | task_input 接线:非入口靠透传 | analyst 透传 raw_signature → synthesizer 可得;不透传则 ContractMismatch |
| GI-08 | 非直接依赖的上游字段 | 不可见(作用域限 dependencies) |
| GI-09 | 来源可追溯 | 每字段来源 spec_id 写入 trace |

### 2.3 ArtifactLoader(`artifacts.py`)— [det]

| ID | 用例 | 期望 |
|---|---|---|
| LOAD-01 | `fixture` | 返回 `fixture_registry[handler_ref]` |
| LOAD-02 | `python_module` | `import_entrypoint(module_path, entrypoint)` |
| LOAD-03 | `prompt_template` | `TemplateAgent(...).run` |
| LOAD-04 | `external_agent` | 绑定 `adapter.invoke`;adapter 缺失 → KeyError/明确报错 |
| LOAD-05 | 四形态一致性 | Coordinator 拿到的都是 `Callable[[dict,list],dict]`,行为对调用方无差别 |

### 2.4 Coordinator + ContextStore — [det] integration

| ID | 用例 | 期望 |
|---|---|---|
| CO-01 | 附录 A 串行执行 | 四节点按拓扑序执行,final_output 含 final_code |
| CO-02 | gate 通过才 put | 仅 ok 的输出进 store 并对下游可见 |
| CO-03 | gate 失败不传播 | 失败节点输出不进 store,触发 recovery,下游不启动 |
| CO-04 | 同层并发(可选) | 默认串行;开并发后无依赖节点并行且结果一致 |

### 2.5 RuntimeGate 机判(`runtime_gate.py`)— [det] ⭐ 核心

| ID | 用例 | 期望 |
|---|---|---|
| GATE-01 | 合法输出过 `out_jsonschema()` | `GateResult(ok=True)` |
| GATE-02 | 缺 required_out / 类型不符 | `ok=False`,`SCHEMA_VIOLATION`,feedback.evidence 指向字段 |
| GATE-03 | forbidden 命中(输出含 `import os`) | `FORBIDDEN_HIT` |
| GATE-04 | machine_assertion `field_present` 缺失(sa1 无 inequality_strict) | 失败 |
| GATE-05 | `equals_input` 不一致(raw_signature 未透传) | `field_mismatch`/`contract` |
| GATE-06 | `regex_match`(def has_close_elements) | 通过 |
| GATE-07 | 多检查同时失败 | `failure_type` 按 `FAILURE_PRIORITY`(contract>grounding>spec_adherence) |
| GATE-08 | **机判失败也产 StructuredFeedback** | evidence/expected/actionable_fix 三项非空,非扁平字符串 |
| GATE-09 | M0/M1 出现 `model_check` 且 `allow_model_verification=False` | 判 `contract`,逼回机判 |
| GATE-09b | `allow_model_verification=True` 但无 verifier backend | fail-closed,不得静默通过 |
| GATE-10 | 构造期/执行期同构 | ConstructionVerifier 与 RuntimeGate 返回同一 `GateResult` 形状 |

### 2.6 ErrorAttributor 三级归因 + RecoveryRouter — [det] ⭐ 核心

对齐论文 §3.3 四场景(附录 A.4 已给注入脚本):

| ID | 用例 | 期望 |
|---|---|---|
| ATTR-01 | 上游违反自身契约(analyst 没标严格不等,synthesizer 用了 ≤) | `upstream`,target=analyst |
| ATTR-02 | 输入对、本节点输出错(analyst 标了 synthesizer 忽略) | `local` + feedback |
| ATTR-03 | `failure_type=contract`(planner 破坏配对顺序) | `structural` |
| ATTR-04 | 缺字段(synthesizer 拿不到透传 raw_signature) | `structural` |
| ATTR-05 | local 重试超 `MAX_LOCAL_RETRIES` | 升级 `structural` |
| REC-01 | local 动作 | 重试同 agent,带 `StructuredFeedback` |
| REC-02 | upstream 动作 | 重跑 target 上游 → 再试本节点 |
| REC-03 | structural 动作 | 触发构造期重建受影响子图 |
| REC-04 | 预算触顶 | 产出 `SurfaceFailure`,**不返回未验证答案** |
| ATTR-ACC [meta] | 注入已知 N 类错误测分类准确率(§9.9) | locality 命中率 + target 命中率 ≥ 阈值 |

### 2.7 ToolRegistry + Tool Gates(§3.6 / §4.4)— [det]/[sec]

| ID | 用例 | 期望 |
|---|---|---|
| TOOL-01 | `registry.validate(plan)` 含未注册工具 | `contract` 失败拦截(不流到 codegen) |
| TOOL-02 | `schema_for` 产出 backend_schema | 模型不接触工具格式 |
| TOOL-03 [sec] | PreToolGate:`side_effects=write` 但 policy 不允许 | 拒绝 |
| TOOL-04 [sec] | PreToolGate:`requires_network` 但沙箱禁网 | 拒绝 |
| TOOL-05 | PostToolGate:工具输出超限 | `output_too_large` |
| TOOL-06 [sec] | PostToolGate:web_search 返回疑似注入文本 | 标 `tainted`,不作指令进 store |
| TOOL-07 | runtime preflight 传入 `ToolRegistry` 且 spec 引用未注册工具 | agent 不执行,`SurfaceFailure(contract)` |
| TOOL-08 | `ToolExecutor.invoke()` | pre gate 先于 handler,post gate 拒绝污染结果,handler 异常 typed surface |

### 2.8 Policy / Constitution(§3.2)— [det]

| ID | 用例 | 期望 |
|---|---|---|
| POL-01 | `check_constitution`:rule 使用 `model_check` machine_assertion(M3 前) | fail-closed,要求改成机判 |
| POL-02 | risk_tier 升级:涉及文件写/网络/执行的节点 | 自动 ≥ medium |
| POL-03 | task description / grounding 试图覆写 constitution | **不可覆写** |
| POL-04 | critical tier | `require_human_review`,不自动放行 |
| POL-05 | `required_tools` 未列入 `spec.tools` | preflight contract 失败 |
| POL-06 | `severity=block` rule 未挂入匹配 spec 的 `forbidden_patterns` | preflight contract 失败 |

### 2.9 Budget(§7.2)— [det]

| ID | 用例 | 期望 |
|---|---|---|
| BUD-01 | `max_llm_calls`/`max_tokens`/`max_wall_seconds` 超限 | `BudgetExceeded` |
| BUD-02 | 触顶 | `SurfaceFailure`(非未验证答案) |
| BUD-03 | 每次 agent 调用前 | 先 `reserve_run()` 预占调用额度,预算触顶时 agent 不执行 |
| BUD-04 | agent 调用后 | `record_run(tokens=...)` 只记录 token/耗时,不补扣调用次数 |

### 2.10 Verifier Backend Stack(§4.5,M3)— [det] 契约 + [eval]

| ID | 用例 | 期望 |
|---|---|---|
| VB-01 | 同一断言路由 | 只交第一个支持且预算允许的 backend |
| VB-02 | backend 失败 | **不自动降级**到更弱 verifier(防"贵验证失败便宜放行") |
| VB-03 [契约] | 所有 backend 返回值 | 必为 `GateResult`,禁裸 bool/标量分;分数只进 `TraceEvent.payload` |
| VB-04 [sec] | AgentVerifier `verification="none"` | 不触发二层 agentic verification(防递归) |
| VB-05 | AspectPanel 多 aspect 投票 | 产出 typed feedback,failure_type 按优先级 |

### 2.11 验证质量 / 校准(§4.7–4.12,§9.6–10)— [meta] ⭐ 差异化

| ID | 用例 | 期望 |
|---|---|---|
| CAL-01 | golden cases 跑 backend | 正确算 precision/recall/false_accept/false_reject |
| CAL-02 | false_accept 超阈值 | backend `enabled=false`,不进 hot path |
| MUT-01 | 对附录 A gate 注入 6 类 mutation(drop_field/wrong_value/forbidden_insert/stale_source/off_by_one/tainted_evidence) | `mutation_score=caught/total` ≥ 阈值;低于即判该 gate 不可生产 |
| MET-01 | metamorphic:`has_close_elements` 输入 numbers 顺序置换 | 结果不变(顺序无关不变量) |
| MET-02 | metamorphic:重复执行 | deterministic |
| COV-01 | `VerificationCoverage` 计算 | schema/assertion/claim/edge/tool/regression 覆盖率正确 |
| COV-02 | critical tier 且 `claims_verified<claims_total` | 不自动放行 |
| DIS-01 | disagreement 按 risk_tier | low=majority / medium=priority / high=adjudicator / critical=human |
| DRIFT-01 | 模型/prompt/schema 版本变更 | 触发重跑 golden,更新 `VerifierHealth` |

### 2.12 Judge Bias Mitigation(§4.6,M3)— [meta]/[eval]

| ID | 用例 | 期望 |
|---|---|---|
| BIAS-01 | swap test:交换候选顺序复判 | 结论一致(无位置偏差) |
| BIAS-02 | verifier prompt 内容 | 不含生成器模型名/agent 名/pass index(匿名化) |
| BIAS-03 | verifier 输出非结构化 JSON | 判失败,**不自由文本兜底放行** |
| BIAS-04 | critical tier 模型判通过 | 必须有 ≥1 非模型证据锚点 |

### 2.13 Claim/Evidence + Taint(§4.5/§4.9,M3)— [det]/[sec]

| ID | 用例 | 期望 |
|---|---|---|
| CLM-01 | 抽 claim → 逐 claim 核查 | verifier 只答"claim 是否被证据支持" |
| CLM-02 | 证据不足 | `insufficient` → 失败(不放行) |
| CLM-03 [sec] | critical claim 仅 untrusted/tainted 证据 | 拒绝 |
| CLM-04 [sec] | grounding 外部文本 | 只进 data 区,不进 system instruction 区 |
| CLM-05 | tainted evidence | 可解释失败,不能支持通过 |

### 2.14 external_agent 威胁模型(§7.1 / architecture.md)— [sec] ⭐ 安全

| ID | 用例 | 期望 |
|---|---|---|
| EA-01 | adapter 返回任意输出 | 默认 `untrusted`,**必过 RuntimeGate 才传播** |
| EA-02 | adapter 改了允许范围外的文件 | `spec_adherence`+`tool_misuse`,拒绝 |
| EA-03 | 高风险任务 | 跑在一次性 worktree,提交前 diff 过 gate,**不落主工作区** |
| EA-04 | mock adapter 返回 `passed=True` | 仍必须过 gate,不能自宣成功 |
| EA-05 | `CliCodeAgentAdapterBase` | worktree 创建/清理、timeout、diff capture、输出限额正确;清理不残留 |
| EA-06 | 同一 AgentSpec 换 adapter(Claude↔OpenCode) | Coordinator/RuntimeGate/RecoveryRouter 不变,结果可比 |

### 2.15 沙箱(§7.1,python_assert / 生成代码)— [sec]

| ID | 用例 | 期望 |
|---|---|---|
| SBX-00 | M1 未接入 `python_assert` backend | fail-closed,不得静默通过 |
| SBX-01 | `python_assert` backend(M2) | 沙箱内只读输入/输出,不可写文件 |
| SBX-02 | 生成代码尝试出网(socket) | 阻断 |
| SBX-03 | 生成代码死循环 | 超时 → `TIMEOUT` |
| SBX-04 | 超大输出 | `OUTPUT_TOO_LARGE` |

### 2.16 构造流水线(Stage 1→5,M2)— [det]+[eval]

| ID | 用例 | 期望 |
|---|---|---|
| CON-01 | Stage 2 输出含环 | 退回重生成 |
| CON-02 | spec 的 verification_criteria 为空 | 强制非空,拒绝 |
| CON-03 | Stage 5 第 4 次 pass | ≤3 pass 上限,升级处理 |
| CON-04 | 类型化路由 | spec_adherence→Stage4 / grounding→Stage3 / contract→Stage2 |
| CON-05 | 代表性输入生成 | 从 assertion 派生 `TestCase`,覆盖度=assertion 覆盖率 |
| CON-06 [eval] | codegen 首过率(§8 M0 quick prototype) | 指标记录;<50% 触发降级方案(非单测断言) |

### 2.17 端到端(E2E)

| ID | 用例 | 期望 |
|---|---|---|
| E2E-01 | 附录 A swarm(fixture) | `has_close_elements` 端到端 PASS(M0 验收) |
| E2E-02 | 注入 local 错误后恢复 | 恢复后 PASS(M1) |
| E2E-03 | 注入 structural(丢透传) | `ContractMismatch`→structural,正确处理或 SurfaceFailure(M1) |
| E2E-04 [eval] | 全新任务描述 → 构造 | 自动产出可执行 swarm 并过构造期验证(M2) |
| E2E-05 [eval] | 跨模型迁移回归(§9) | 同 swarm 换 backend,成功率不回退 |

---

## 3. 测试基础设施

- **框架**:`pytest` + `pytest-cov`;属性测试 `hypothesis`;`jsonschema` 校验断言。
- **LLM 替身**:`StubVerifierBackend` / `StubAgentAdapter` / `FakeLLM`(确定性返回),M0–M2 单测全程用;真实 LLM 仅 `tests/eval/`(标记 `@pytest.mark.eval`,默认跳过,需显式 `META_AGENT_RUN_PROVIDER_EVAL=1` + provider API key,nightly/手动跑)。
- **fixtures**:`tests/fixtures/function_completion.py` = 附录 A 的 SwarmPlan + 4 fixture artifacts + 正常/各类错误注入变体(直接服务 E2E-01~03、ATTR-*、MUT-01)。
- **golden cases**:`tests/golden/*.json` = `GoldenVerificationCase` 集,覆盖 false accept/reject、各 subtype;M1 建最小集,M3 用于 CAL-*。
- **mutation/metamorphic harness**:`tests/meta/` 独立目录,产出 `mutation_score` 报告。
- **沙箱测试**:在受控临时目录 + 资源限额下跑,断言越权被阻断;CI 里标 `@pytest.mark.sandbox`。

## 4. 覆盖率目标与 CI 门禁

| 范围 | 目标 | CI |
|---|---|---|
| 确定性核(schema/dag/context/runtime_gate/attribution/registry/budget) | **行 ≥ 90%,分支 ≥ 85%** | 每次提交,必须全绿 |
| 安全用例([sec]:沙箱/越权/注入/递归/taint) | 关键路径 **100%** | 每次提交 |
| 构造流水线 | 行 ≥ 75% | 每次提交(LLM mock) |
| [eval]/[meta]:judge 校准/mutation/codegen 首过率/跨模型 | 阈值回归,非覆盖率 | nightly / 发布前 |

门禁原则:**确定性核 + 安全用例**红就阻断合并;[eval]/[meta] 跑阈值,退化告警但按策略决定是否阻断(judge false_accept 升高视为发布阻断项,见 §9)。

## 5. 测试优先级(实施顺序)

1. **schema 往返 + DAG + gather_inputs**(M0 地基,纯 [det],附录 A 当夹具)
2. **RuntimeGate 机判 + GateResult 两轴**(M1 核心)
3. **ErrorAttributor 四场景 + RecoveryRouter**(M1 差异化,附录 A.4 脚本)
4. **ToolGate / Policy / Budget**(M1–M2 安全与兜底)
5. **构造流水线 + 类型化路由**(M2)
6. **verifier 后端栈 + 校准 + mutation/metamorphic + external_agent**(M3 [meta]/[sec])

## 6. 不测什么

- LLM 内部行为、模型权重、provider SDK 自身(mock 边界即可)。
- **任何训练/微调/数据集**——超出项目边界(spec-only)。
- 生成代码/judge 的**逐字输出等值**——非确定性,只看 schema 合规 + 金标准阈值。
- 框架代码(pydantic/pytest/jsonschema)、一次性脚本、纯 getter/setter。
- 前端可视化像素级回归(M0/M1 前端只读,留到有 UI 后单列 frontend 测试计划)。

---

## 7. 与设计的闭环

本测试计划把 §9 的"验证质量类验收(6–10)"落成可执行套件:gate 强度→MUT-01、判官质量→CAL-01/02、验证充分性→COV-01/02、归因准确率→ATTR-ACC、判官稳健性→DRIFT-01/BIAS-01。即:**这个引擎卖点是"可信验证",所以它的测试套件必须能证明自己的验证是可信的**——这正是 [meta] 类用例存在的理由。
