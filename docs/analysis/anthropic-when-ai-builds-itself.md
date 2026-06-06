# Anthropic: When AI builds itself 分析

## 文章信息

- **标题**: When AI builds itself
- **作者**: Marina Favaro, Jack Clark (Anthropic Institute)
- **发表**: 2026年6月 | https://www.anthropic.com/institute/recursive-self-improvement
- **性质**: Anthropic Institute 立场文章，含大量内部未公开数据

## 核心论点

**递归自我改进 (recursive self-improvement) 已不是理论猜想，而是正在 Anthropic 内部发生的过程。** 文章用四类证据构建了从"AI辅助编码"到"AI自主研究"的递进论证：

```
2021-2023: 人手写代码
2023-2025: Chatbot 辅助（代码片段 → 复制粘贴）
2025-2026: Coding agent 自主编辑文件
Today:     Agent 可自行运行代码、委派工作给其他 agent
20XX?:     闭合循环——Agent 能自己构建和训练模型
```

---

## 关键数据

### 工程生产力

| 指标 | 数据 | 时间 |
|------|------|------|
| Claude 代码占比 | **>80%** 合并到代码库的行由 Claude 撰写 | 2026.05 |
| 工程师产出 | **8×** 代码量/季度（vs 2021-2025 baseline） | 2026 Q2 |
| 任务时长翻倍周期 | 每 **4 个月**翻倍（此前每 7 个月） | 2024→2026 |
| Claude Opus 4.6 | 可完成 **12 小时**人类等价任务 | 2026 |
| Claude Mythos Preview | METR 评估：可工作 **≥16 小时**，"超出 METR 可测量范围" | 2026 |

### Claude Code 成功率

按任务难度分层（2026.05）：

- 开放性问题（最难的 tier）：**76%**，6 个月内 +50pp
- 示例：一次例行升级导致数万个训练任务崩溃，Claude 在约 2 小时内独自隔离了触发崩溃的 obscure debugging flag → 正常需要 2-3 天人工

### 研究能力

| 指标 | 数据 |
|------|------|
| 代码优化 | 52× 加速（Claude Mythos Preview, 2026.04）vs 人类研究员 4×（4-8小时）|
| 开放式安全研究 | Agent 恢复 97% 的差距 vs 人类 23%（800 累计小时，$18K compute）|
| 研究方向判断 | Mythos Preview 在 64% 的情况下选择比人类更好的下一步（vs Opus 4.5 的 51%）|

### 代码质量

- 2025 年底: Claude 代码质量仍低于人类
- **2026 年中期: 大致持平**（内部有争议但多数认同）
- 预期: **一年内严格更好**
- Claude reviewer 回顾性分析: 可捕获过去线上事故中 **~1/3** 的 bug

### 基础设施压力

- GitHub: 2025 全年 **10 亿**次 commit → 2026 年中 **每周 2.75 亿**（年化 **140 亿**）
- GitHub COO: "正以极限速度推进容量"

---

## 三个未来场景

### 场景 1: 趋势停滞（不太可能）

- S-curve 弯曲，边际收益递减
- 或受供应链（芯片/电力/互联带宽）限制
- 即使冻结在今日水平，Project Glasswing 已发现 **10000+** 高/严重漏洞
- **Anthropic 认为此场景概率最低**

### 场景 2: 持续复合效率增益（最可能）

- AI 开发大幅自动化，人类仍设定方向
- 100 人公司可做 10000-100000 人组织的工作
- **Amdahl 定律成为核心瓶颈**：加速一部分流程只是把瓶颈移到别处
- 已观察到的瓶颈：人类代码审查跟不上生成速度

### 场景 3: 完全递归自我改进

- AI 系统自己能设计和训练后继模型
- 进展速度仅受限于算力/算法效率
- 人类角色转移至：**监督、验证、核实** AI 系统的"虚拟实验室"
- Alignment 问题的解决与否是最不确定的因素

---

## 与 Meta-Agent 验证机制的关联

### 直接关联

这篇文章从 **产业实践角度** 验证了我们在做的工作方向：

1. **验证将成为人类最后的堡垒**：场景 3 中明确定位"人类角色转移至 oversight, validation, and verification"。Meta-Agent 的 verification gate + error attribution 正是这个过渡的基础设施。

2. **Amdahl 定律 → 验证自动化同样需要加速**：如果代码审查已成为瓶颈（AI 写得比人审得快），那么验证也必须自动化。Meta-Agent 的 RuntimeGate 和 ConstructionVerifier 就是验证自动化的工程实现。

3. **Claude reviewer 捕获 1/3 的 bug**：这证明了自动化验证的可行性，也暴露了当前 2/3 的漏报率。Meta-Agent 的 MAV committee（多验证器投票）正是降低漏报的方向。

4. **从 "Claude 辅助" 到 "Claude 自主" 再到 "Claude 审查 Claude"**：这恰好对应 Meta-Agent 的三级错误归因——local retry（Claude 自我修复）、upstream fix（Claude 回溯依赖）、structural re-plan（Claude 重新规划任务）。

5. **任务时长 4 个月翻倍 → Long-horizon 验证的紧迫性**：12 小时任务已经超出单个人类的注意力范围，周级/月级任务意味着验证必须完全自动化。Meta-Agent 的 DAG + ContextStore 架构专为 long-horizon 设计。

### 数据反哺

| Anthropic 数据 | Meta-Agent 启示 |
|---------------|----------------|
| 1/3 bug 被 Claude reviewer 捕获 | RuntimeGate 的 recall 需要 >50%，当前估计不足 |
| 代码质量刚达到人类水平 | Stage 4 codegen 的质量预期应校准为"接近人类" |
| 64% 方向判断优于人类 | ErrorAttributor 的 structural 判定仍有提升空间 |
| 800 个修复消除 1000x 错误 | Grounding + verification 的乘数效应：一次正确修复可消除大量失败 |
| GitHub 140 亿 commits/年 | 代码级 AST 验证（Spec Gap 方向）的规模需求 |

---

## 核心引用

"人类的相对优势目前仍在于看到更大的图景和跳出即时任务框架的思考。" — Anthropic 员工

"如今工作的形态大致是：人类有想法，模型能以比以前快一个数量级的速度实现、测试和评估它们。" — Anthropic 员工

"Claude 更快，创造零债务，但每一次交互都是对人类协作的一次失去的出价。" — Anthropic 员工

"在一切都运转良好的日子里，我不禁觉得我所做的一切都不重要，一切都被自动化了，比我更好更快。但然后有些日子一切都会崩溃..." — Anthropic 员工

---

## 技术声明

分析基于 Anthropic 官方网站原文 (anthropic.com/institute/recursive-self-improvement, 2026.06) 的直接提取。所有数字来自原文引用和图表，未经过独立验证。引用的员工语录来自 Anthropic 内部讨论，反映 2026 年 5 月个人观点。Project Glasswing、Mythos Preview 等为 Anthropic 内部代号，具体技术细节未公开。
