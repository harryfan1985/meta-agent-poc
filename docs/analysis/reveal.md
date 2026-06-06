# ReVeal 分析

## 论文信息

- **标题**: ReVeal: Self-Evolving Code Agents via Reliable Self-Verification
- **发表**: arXiv 2506.11442 | **ICLR 2026** (已接收)
- **领域**: Software Engineering (cs.SE); Machine Learning (cs.LG)

## 核心思想

Meta-Agent 和 MAV 都是 **inference-time** 的验证策略——验证器是外部的、固定的。ReVeal 是 **training-time** 的——通过多轮 RL 训练，让 agent **内化**验证能力。训练后的 agent 在推理时能自主生成测试用例、自我验证、根据验证结果精准修复——不需要外部 verifier。

核心洞察：**生成和验证不应是分离的能力**。一个同时擅长两者的 agent，比"强生成器 + 外部验证器"的组合更高效。

## 方法: TAPO 算法

TAPO (Turn-Aware Policy Optimization) 是 ReVeal 的核心 RL 算法。每轮训练包含：

```
Turn t:
  1. Agent 生成代码 draft
  2. Agent 自主生成测试用例
  3. 调用 code executor 执行测试
  4. 根据执行结果，agent 自我验证并决定: 接受 / 修改 / 重写
  5. → Turn t+1 (最多 20+ turns)
```

### 奖励设计

ReVeal 的关键创新在于 joint reward：

```
R_joint = α · R_generation + β · R_verification
```

- **R_generation (outcome reward)**: 最终代码是否通过所有测试
- **R_verification (dense per-turn reward)**: 每轮生成质量 + 测试用例质量 + 自我验证准确性
- α 和 β 是可调系数，控制生成和验证的优化权重

TAPO 联合优化两个目标——这使生成和验证能力**协同进化**。训练曲线 (Figure 4a,b) 显示：使用 joint reward 的 TAPO 比单独优化生成或验证都更稳定。

### 训练后行为

训练后的 ReVeal 模型在 inference 时表现出一个习得的策略：

1. 先写简单版本
2. 生成针对性测试
3. 根据测试失败定位问题
4. 精准修复——不改无关代码

这与人类开发者的 TDD 循环高度相似。且即使不继续训练，多轮 self-verify 仍能持续改进输出（最多 20+ turns）——说明验证能力已被**内化到模型参数中**。

## 关键实验数据

- **LiveCodeBench V6**: 随推理轮数增加，Pass@1 和 Pass@10 均持续增长
- **对比未经 ReVeal 训练的 baseline**: DAPO-Qwen-32B 等强 baseline 在多轮后**退化**（前几轮有增益，随后下降），ReVeal 持续上升
- **测试用例质量**: Figure 4(d) 显示 TAPO with joint rewards 生成的测试用例准确率更高
- **Qwen2.5-32B-Instruct** 是主要实验基座

## 与 Meta-Agent 的关系

### 互补维度

| Meta-Agent | ReVeal | 组合 |
|-----------|--------|------|
| 固定外部 verifier | 训练内化的 self-verifier | 内化验证(第一轮) + 外部 gate(第二轮) = defense-in-depth |
| 构造期生成 Cᵢ | 训练期学会生成测试 | 自动生成的测试可补充 manual Cᵢ |
| 不训练 | RL 训练 | 训练后 agent 在 Meta-Agent DAG 中执行 |
| 结构级验证(DAG) | agent 级验证(self) | 结构编排 + 自我验证 双层保障 |

### 关键差异

- ReVeal **需要训练资源**（RL on Qwen2.5-32B），Meta-Agent PoC 的硬边界是"不训练"。这意味着短期内不能直接集成 ReVeal 的 TAPO 训练。
- 但 ReVeal 的**架构思想**可借鉴：joint reward 的设计思路可以转化为 Meta-Agent 的 feedback 设计——让构造期验证的 `StructuredFeedback` 同时覆盖"生成质量"和"验证准确性"两个维度。
- ReVeal 的"多轮 self-evolve"对应 Meta-Agent 的 local-retry 循环——但 ReVeal 证明了训练可以让这个循环更高效。

## 工程启示

1. **验证能力可以被训练**：不是只能靠外部 verifier。如果未来放开训练边界，TAPO 是最高杠杆的投资
2. **joint reward > separate reward**：同时优化生成和验证比分别优化效果好——这暗示 Meta-Agent 的 feedback 设计也应同时关注两个维度
3. **Self-verify 有天花板**：即使是 ReVeal，多轮后也会饱和。外部 structural gate（Meta-Agent 的 DAG 级验证）仍有价值
4. **基座模型的选择很重要**：ReVeal 在 Qwen2.5-32B 上有效；小模型(7B/14B)能否受益于 TAPO 需要验证

## 技术声明

分析基于 arXiv 摘要、OpenReview ICLR 2026 论文页面、Hugging Face papers 页面、EmergentMind 论文摘要，以及 ICLR 2026 OpenReview PDF 的部分内容。训练曲线和实验数据来自论文 Figure 4 和 Figure 6 的描述。论文全文和代码未公开获取。
