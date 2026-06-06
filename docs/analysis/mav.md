# MAV (Multi-Agent Verification) 分析

## 论文信息

- **标题**: Multi-Agent Verification: Scaling Test-Time Compute with Multiple Verifiers
- **作者**: Shalev Lifshitz (ArdaLabs.AI), Sheila A. McIlraith (University of Toronto, Vector Institute), Yilun Du (Harvard University)
- **发表**: arXiv 2502.20379, 2025 | ICLR 2025 Workshops (VerifAI, MCDC, SSI-FM, Reasoning and Planning for LLMs)
- **代码**: https://github.com/Shalev-Lifshitz/MultiAgentVerification (MIT, 29 stars)

## 核心思想

提出 test-time compute 的**第四个缩放维度**：除增加候选输出数(n)、增加推理步数、使用更强的验证模型外，还可以**增加验证器的数量(m)**。

传统范式用一个 verifier 评分候选输出。MAV 用 m 个 Aspect Verifier (AV) 并行验证同一批候选，聚合投票结果。这不是简单的 ensemble——每个 AV 检查输出的**不同方面**（格式正确性、推理完整性、计算正确性、符号一致性、边界条件、最终答案），形成多维度的质量评估。

### Aspect Verifiers (AVs)

AVs 是 off-the-shelf LLM，无需训练，只需要不同的验证 prompt。例如 MATH 数据集上使用 6 个领域特化 AV：

| AV | 检查内容 |
|----|---------|
| Format Checker | 答案格式是否符合要求 |
| Reasoning Completeness | 推理步骤是否完整 |
| Calculation Correctness | 计算过程是否正确 |
| Symbol Consistency | 符号使用是否一致 |
| Edge Case Coverage | 边界条件是否覆盖 |
| Final Answer Match | 最终答案数值是否正确 |

每个 AV 做 binary 判定 (✓/✗)，temperature=0.0 确保确定性。AV 的 prompt 是手工设计的领域特化模板，可从代码仓库 `src/prompts/vera_prompts.py` 直接查看。

## 算法: BoN-MAV

```
BoN-MAV@(n, m):
  1. Generator LLM 采样 n 个候选输出 (temperature=0.7)
  2. m 个 AV 各自对每个候选做 binary 判断
  3. 每个候选得分 = Σ AV approvals
  4. 选得分最高的候选
```

BoN-MAV 是 BoN-RM (Best-of-N with Reward Model) 和 self-consistency (cons@n) 的泛化——当 m=1 且 AV 是 reward model 时退化为 BoN-RM。

## 关键发现

### 1. 双维缩放均有效

Figure 1 (MATH, Gemini-1.5-Flash)：

- 增加 n (候选数)：logarithmic gain，n=16 时趋于饱和
- 增加 m (验证器数)：**near-linear gain**，m=6 时仍在上行
- BoN-MAV@(16,6) 达到 ~66%，对比 self-consistency ~59%，BoN-RM ~62%

### 2. Weak-to-Strong Generalization

**最违反直觉的发现**：用较弱的模型作为 verifier（如 Gemini-Flash 验证 Gemini-Pro 的输出），仍能显著提升强模型的性能。原因：验证比生成更容易（verification asymmetry）——判断一个答案是否正确，比自己生成正确答案简单。

### 3. Self-Improvement

同一模型同时用于生成和验证（Gemini-Flash 既生成候选，也作为 AV）——仍然有效。不需要额外的强模型或 reward model。

### 4. 支持数据集

代码仓库支持四个数据集：MATH、MMLU-Pro、GPQA-Diamond、HumanEval。每个数据集使用不同的领域特化 AV 集合（通过 `load_domain_specific_verifiers()` 加载）。

## 与 Meta-Agent 的关系

### 直接可组合

Meta-Agent 的每个 verification gate 是单点验证 (`yᵢ ∈ Cᵢ`，一个 verifier 判定)。MAV 的 committee 模式可直接叠加：

- **构造期 Stage 5 行为验证**：用 m 个 AV 替代单一 verifier model，多数投票决定是否通过
- **执行期 RuntimeGate 的模型判残差**：将 §3.7 的多 aspect 面板（ASPECT_MAP）映射到领域特化的 AV

### 关键边界

MAV 给票数、不给错误类型——它解决"选哪个最好"，不解决"错在哪、回退到哪"。因此 MAV 不能替代 Meta-Agent 的类型化归因（local/upstream/structural + typed failure signal），只能作为 verification gate **内部**的 selection 增强，且仅用于模型判残差（机判项继续走代码）。详见设计文档 §3.7 和附：与相邻工作的边界。

### 降本效果

MAV 验证器用便宜档（Haiku 级），用"多个便宜模型投票"替代"一个贵模型单独判"。Meta-Agent 设计文档 §7 技术选型中的"验证器档位"直接受益于此发现。

## 工程启示

1. **验证器不需要和生成器同级别**：用 Haiku/Gemini-Flash 验证 Sonnet/Pro 的输出既有效又便宜
2. **多验证器 > 单验证器，且边际收益持续**：如果预算允许，增加 3-5 个 AV 的回报远高于增加候选数
3. **AV prompt 是验证质量的关键**：手工设计的领域特化 prompt 决定了 AV 的判别能力——通用 prompt 的 AV 效果接近随机
4. **verification asymmetry 是根本原理**：验证比生成容易——利用这个不对称性，把复杂度从生成侧转移到验证侧

## 技术声明

分析基于论文 arXiv 摘要、开源代码仓库（Shalev-Lifshitz/MultiAgentVerification）的 README 和代码结构阅读、OpenReview 摘要，以及论文 Figure 1 中提取的实验数据。AV prompt 的具体内容来自代码仓库 `src/prompts/vera_prompts.py` 的引用。论文全文因网络限制未直接获取。
