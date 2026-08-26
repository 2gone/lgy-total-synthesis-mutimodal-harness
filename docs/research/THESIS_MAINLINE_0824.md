# 论文主线、研究意义、Gold 构建与实验矩阵（正式版）

## 1. 核心命题：建立面向全合成文献的数据适配框架

本工作的核心并不是开发新的 OCSR，也不是把“图片识别”和“文本抽取”简单串联起来，而是解决一个更基础的数据问题：

> **如何将原本面向人阅读的全合成文献，系统地适配为结构标准化、证据可追溯、能够跨论文比较和机器处理的 route-level synthesis data？**

全合成知识天然分散在不同模态中：分子结构和编号主要存在于 Scheme，反应条件分布在箭头与图注，反应性质、路线逻辑和关键步骤则更多依赖正文描述。现有结构识别和文本抽取能力虽然可以分别获得局部信息，但这些信息并不能天然组成一条可靠、统一、可计算的全合成路线。

因此，本研究真正关注的是从：

**document-native chemical information**

到：

**route-native synthesis data**

之间缺失的数据适配层。

我们据此提出 **Total Synthesis Data Adaptation Framework（TS-DAF）**：

**Human-readable Literature → Multimodal Evidence Reconstruction → Data Adaptation → Standardized Route Database**

其中 OCSR、OCR、Text Model 和 Multimodal Model 都只是可替换的信息获取组件；真正稳定且具有方法学意义的核心，是**如何组织、约束、融合和标准化这些异质证据，使其成为可计算的全合成路线数据。**

---

## 2. 研究空缺与研究意义：现有体系缺少“完整合成路线”这一数据层

现有工作主要解决三个层级的问题：

- **Chemical Structure Recognition**：chemical drawing → molecular structure
- **Reaction Information Extraction**：text / Scheme → reagent, condition, product
- **Single-Reaction Database Construction**：single transformation → reaction record

但全合成文献真正表达的是更高一层的知识：

> **一个复杂目标分子，是通过怎样的分子序列、反应步骤和路线组织逐步构建出来的？**

这意味着机器不仅需要识别单个结构或单个反应，还必须恢复：

- 显式分子及其论文编号
- 分子与分子之间的路线连接关系
- 原文 reaction operations
- 反应条件、产率和选择性
- 多步合并信息
- 立体化学
- Scheme、图注与正文之间的证据关系
- 不同论文之间统一的分子与路线表示

因此，本研究填补的不是另一个模型能力缺口，而是：

> **从 scientific document representation 到 synthesis route representation 的数据层空缺。**

这一层具有长期价值，因为模型可以快速迭代和替换，但高质量的 route-level data representation、evidence provenance 和 standardization framework 是后续所有系统共同依赖的基础。

其意义主要体现在三个方面：

### 数据基础设施价值
建立统一的 machine-readable total-synthesis representation，使不同期刊、不同年代、不同绘图风格的全合成路线真正能够进入同一个数据空间。

### AI for Chemistry 价值
未来的 synthesis retrieval、route comparison、reaction recommendation、retrosynthesis evaluation 和 chemistry agents 都需要完整的 route-level data，而不仅是孤立 reaction records。本研究提供的是这一类系统缺失的 **data adaptation layer**。

### 化学知识组织价值
传统全合成知识主要以 Scheme 和 prose 的形式保存，难以规模化检索、比较和复用。路线被标准化后，全合成文献可以从“供人阅读的文献集合”转化为“可由机器检索和分析的结构化知识资源”。

---

## 3. 三项核心贡献：Framework、Workflow、Database

### Contribution 1 — Total Synthesis Data Adaptation Framework
本研究首先建立一套专门适配全合成文献的数据框架。

其核心不是简单定义几个字段，而是建立从论文证据到路线数据的系统映射关系：

**Paper → Route → Compound → Reaction Block → Operation → Evidence**

这一框架同时保留：

- 论文中明确绘出的分子路线
- 原文实际报告的 reaction operations
- 每个字段对应的 source evidence
- 分子标准化后的机器可读身份
- 图像与正文冲突或不一致的信息

因此，TS-DAF 的核心作用是：

> **定义全合成文献中的信息，应该如何被适配为统一、可审核、可扩展的 route-level data。**

---

### Contribution 2 — Evidence-Grounded Multi-Model Reconstruction Workflow
在 TS-DAF 之上，建立一个不依赖新模型训练的 **Multi-Model Collaborative Workflow**。

不同模块只负责其最适合的证据类型：

- **OCSR**：molecular structure
- **OCR / Layout**：compound number、arrow、condition text、spatial relation
- **Lightweight Text Model**：全文 routing、reaction description、condition、yield、正文语义
- **Weak Multimodal Model**：Scheme layout、image–text association、局部视觉复核
- **Strong Text Model**：在证据已经被组织后完成 reaction / route synthesis reasoning
- **Prompt Factory**：通过确定性规则组装 Text Package 与 Image Package，并注入目标 Schema 和预设 Prompt

核心设计原则是：

> **Models extract evidence; the framework organizes evidence; the strong model reconstructs the route.**

---

### Contribution 3 — Evidence-Grounded Route-Level Database
最终产出不是简单的 reaction collection，而是一套具有完整来源关系和统一分子身份的 **route-level total synthesis database**。

数据库应至少支持：

- 从 target 回溯完整显式合成路线
- 查询任意中间体在路线中的位置
- 查询某一步对应的原文条件和正文描述
- 跨论文检索相同或相似 molecular structures
- 跨论文比较 reaction blocks 和 route organization
- 从数据库记录直接返回原始 Scheme / text evidence
- 将单步数据进一步导出为兼容其他 reaction database 的形式

本阶段优先完成：

**Framework → Workflow → Gold Benchmark → Database**

而不是把统计分析设为主论文的必要前提。

---

## 4. 论文故事：从缺失的数据层，到可规模化的全合成知识基础设施

整篇论文建议围绕一条直接且稳定的故事展开：

### 第一幕 — The Missing Data Adaptation Layer
大量全合成知识已经存在于科学文献中，但主要以 Scheme 和 prose 的形式为人类阅读而组织。现有 OCSR 可以识别结构，文本模型可以抽取叙述，reaction extraction 可以得到局部单步信息，但这些局部能力并不能直接产生：

> **standardized, evidence-grounded, complete synthesis routes**

真正缺少的是连接：

**Scientific Literature**

与：

**Route-Level Synthesis Data**

之间的适配层。

### 第二幕 — Total Synthesis Data Adaptation Framework
我们提出 TS-DAF，将全合成文献中的 molecular structures、compound numbering、reaction arrows、conditions、captions、body-text descriptions、route relations 和 source evidence 统一映射到 route-level data model。

### 第三幕 — Multi-Model Reconstruction under a Common Data Contract
在这一数据框架上实现自动化工作流。通过 Text Package、Image Package、validated structures、Prompt Factory 和 Strong Text Reasoner，把原始论文转化为完整 route records。

### 第四幕 — A Scalable Route-Level Total Synthesis Database
将冻结后的工作流应用到数千至上万篇全合成文献，形成大规模标准化路线数据库，证明 TS-DAF 不只是 annotation schema，而是一个真正可扩展的：

> **literature-to-data adaptation framework**

整篇论文的闭环为：

**Data Gap → Data Adaptation Framework → Multi-Model Reconstruction → Gold Validation → Route-Level Database**

---

## 5. Gold 与批量构建系统：300-Route Evidence-Grounded Benchmark

### 5.1 Gold 设计：200 Development + 100 Unseen Validation

本研究最终只保留两类正式 Gold。

| Gold | 数量 | 用途 | 是否允许用于调试 |
|---|---:|---|---:|
| **Development Gold** | **200 routes** | Schema refinement、Prompt/Harness 设计、全部消融、错误分析 | 是 |
| **Unseen Validation Gold** | **100 routes** | Workflow freeze 后的最终独立验证 | 否 |

补充原则：

- 在 200 条 Development Gold 中，可以固定抽取 **50-route Fast Subset** 用于日常快速迭代；
- 这个 50-route 子集只是内部开发子集，**不构成第三套 Gold**；
- 100 条 Validation Gold 必须在 workflow、Prompt Factory、Prompt Template、Schema 与 retrieval policy 冻结后再运行最终系统；
- Validation Gold 不参与 prompt tuning、规则修改或模型选择。

建议对 100 条 Validation Gold 预先分层：

- **70 Representative Routes**：常规单路线、布局清晰、文本引用直接；
- **30 Challenge Routes**：多 Scheme、汇聚路线、复杂编号、跨段落引用、多步合并条件、复杂版式。

Challenge 标签必须在模型运行前根据文档难度预先确定，不能根据模型好坏事后划分。

---

### 5.2 Gold 批量构建：轻量审核系统 + 双层人工复核

Gold 的关键不在于纯手工重标全部信息，而在于建立一个**模型先行、人工快速裁定、证据完整回放**的轻量审核系统。

#### 批量 Gold 生产流程（文本示意图）

```text
Paper Pool
   │
   ├─► Weak Text Routing
   │      └─ 识别 total synthesis 相关论文、相关 Scheme、相关正文段落
   │
   ├─► Scheme Routing
   │      └─ 找到真正的 route figures / schemes
   │
   ├─► Molecule Localization
   │      └─ MolDet / YOLO 给出 molecule bbox 与 visual IDs (M01, M02, ...)
   │
   ├─► OCSR Pre-annotation
   │      └─ 生成 raw structures
   │
   ├─► Text Package Builder
   │      └─ 组织 caption / body-text / condition evidence
   │
   ├─► Image Package Builder
   │      └─ 组织 label / arrow / condition / layout evidence
   │
   ├─► Strong Model Draft Extraction
   │      └─ 先给出 route / reaction draft record
   │
   ├─► Human Review UI
   │      ├─ Reviewer A 审核
   │      ├─ Reviewer B 复核
   │      └─ 必要时 adjudication
   │
   └─► Final Gold Record
          ├─ route record
          ├─ reaction records
          ├─ validated structures
          └─ evidence links
```

#### 轻量审核界面：应展示什么

人工审核系统不需要复杂，只要足够高效、足够可审计。建议单页面固定展示 5 个区块：

1. **Raw Scheme / Tokenized Scheme**
2. **Molecule List**：M-ID、compound number、raw OCSR、validated structure
3. **Text Evidence Panel**：caption、正文相关段落、条件原文
4. **Model Draft Record**：当前 route / reaction draft
5. **Review Controls**：accept / revise / reject / unresolved / comment

核心原则是：

> **人工不是从零填写，而是对模型已抽出的证据和草稿做快速裁定。**

#### 哪些必须人工逐条裁定，哪些只需强复核

**必须人工逐条裁定**
- 显式 molecule identity（结构、编号绑定）
- reaction edge / route connectivity
- operation grouping
- 关键产率 / ee / dr / 关键 conditions
- source conflict

**可由模型先抽出、人工强复核**
- reagent / catalyst / solvent lists
- reaction description
- caption-body 对齐
- step labels
- evidence linking

这样 2–3 人并行时，300 条 Gold 是可操作的。

---

### 5.3 结构输入层验证：人工校正 OCSR 的准确性

本文不建立独立的 OCSR benchmark，也不把 OCSR 作为主研究对象。  
OCSR 在本工作中的角色是：

> **为 route reconstruction 提供 molecular structure pre-annotation。**

因此，结构层验证只解决一个问题：

> **人工校正后的 structure layer 是否足够可信，可以作为本工作的 Gold 输入。**

具体做法非常明确：

- 在全部 **300 Gold routes 的显式分子**上保留两份结果：
  - **Raw OCSR Output**
  - **Human-Validated Structure**
- 由两位审核者完成结构确认，必要时进行 adjudication
- 自动比较 Raw OCSR 与 Human-Validated Structure

#### 结构层验证指标

- **Connectivity Exact Match**：raw OCSR 与人工最终结构的连接关系是否完全一致
- **Exact Structure Match**：连接关系与明确立体化学是否完全一致
- **Human Correction Rate**：多少分子需要人工修改
- **Stereo Correction Rate**
- **Reviewer Agreement**：人工第一次独立审核的一致率
- **Adjudication Rate**

这部分的定位是：

> **Structure-Layer Validation / Quality Control**

而不是 OCSR benchmark。

#### 下游敏感性检查（小规模）

在 **50-route Development subset** 上，再做一次小规模敏感性检查：

- **Raw OCSR structures**
- **Human-Validated structures**

其余 Harness 完全相同，比较：

- Explicit Molecule Recovery
- Route Edge F1
- Route Topology Exact Match
- Whole-Route Exact Match

如果差异明显，说明人工结构确认是必要的质量控制层；如果差异较小，说明 TS-DAF 对一定程度的 OCSR 噪声具有鲁棒性。

---

## 6. 实验矩阵：验证的不是 OCSR，而是“信息如何被适配成完整路线”

本研究的实验设计应与论文主线一致：**主论文验证的是 TS-DAF 如何把不同层级的文献证据逐步适配成 route-level synthesis data。**

### 6.1 总实验图：四个实验统一解释

| Experiment | 核心目的 | 数据 | 主要回答的问题 |
|---|---|---|---|
| **Exp.1 Structure-Layer Validation** | 证明结构输入层可信 | 300 Gold 中全部显式分子；50-route sensitivity subset | 我们的人工校正 OCSR 是否足够准？ |
| **Exp.2 Information-Ladder Ablation** | 拆解 TS-DAF 各信息层的贡献 | 200 Development Gold | 为什么要有 Text Package、Image Package、Validated Structure、Retrieval、Visual Query？ |
| **Exp.3 End-to-End Route Reconstruction** | 评测冻结系统对未见路线的真实效果 | 100 Unseen Validation Gold | 最终系统对没见过的全合成文献到底有多准？ |
| **Exp.4 Scaling to Database** | 证明系统可从 benchmark 扩展到数据库 | 5,000 / 10,000 / 15,000–20,000 papers | 这套 workflow 能否真正规模化建库？ |

这四个实验形成一条完整证据链：

**Input Reliability → Adaptation Value → Unseen Generalization → Real-World Scalability**

---

### 6.2 Exp.1 — Structure-Layer Validation

这是输入层质量控制实验。

#### 目的
证明人工校正后的 molecular structure input 是可信的，可以作为 route-level reconstruction 的基础输入。

#### 主要结果
- Connectivity Exact Match
- Exact Structure Match
- Correction Rate
- Reviewer Agreement
- Adjudication Rate

#### 论文作用
堵住“你的 Gold 结构是否可靠”的问题，但不抢主论文方法学主线。

---

### 6.3 Exp.2 — Information-Ladder Ablation

这是整篇论文最重要的实验。

#### 核心问题
> **Strong Text Model 获得的信息逐层增加时，完整路线恢复能力如何变化？**

#### 信息阶梯（文本示意图）

```text
A0  Full Text Only
        ↓
A1  TEXT PACKAGE Only
        ↓
A2  IMAGE PACKAGE Only
        ↓
A3  TEXT + IMAGE, No Structure
        ↓
A4  + Human-Validated Structure
        ↓
A5  + On-Demand Text Retrieval
        ↓
A6  Full Harness (+ Visual Query)
```

#### 具体消融矩阵

| Variant | Full Text | TEXT PACKAGE | Image Relation Evidence | Validated Structures | Text Retrieval | Visual Query | 目的 |
|---|---:|---:|---:|---:|---:|---:|---|
| **A0 — Full-Text Baseline** | ✓ | — | — | — | — | — | 强模型直接面对粗全文的基线 |
| **A1 — Text Package Only** | — | ✓ | — | — | — | — | 测量 Text Package 的价值 |
| **A2 — Image Package Only** | — | — | ✓ | ✓ | — | — | 仅凭 Scheme 相关证据能恢复多少 |
| **A3 — Text + Image, No Structure** | — | ✓ | ✓ | — | — | — | 测量编号、箭头、条件、版式信息的作用 |
| **A4 — + Validated Structure** | — | ✓ | ✓ | ✓ | — | — | 测量精确结构身份的增益 |
| **A5 — + Text Retrieval** | — | ✓ | ✓ | ✓ | ✓ | — | 测量按需补文本证据的价值 |
| **A6 — Full Harness** | — | ✓ | ✓ | ✓ | ✓ | ✓ | 最终 TS-DAF workflow |

#### 最关键的比较
- **A0 vs A1**：Text Package 是否减少噪声并提升解析质量
- **A1 vs A3**：Image evidence 的增益
- **A3 vs A4**：精确 molecular structure 是否真正帮助 route reasoning
- **A4 vs A5**：按需 Text Retrieval 的价值
- **A5 vs A6**：局部 Visual Query 的额外价值

#### 论文作用
证明 TS-DAF 不是“堆模型”，而是“数据适配层逐层提供有效信息”。

---

### 6.4 Exp.3 — End-to-End Route Reconstruction

这是最终考试实验。

#### 数据
**100 Unseen Validation Gold**

#### 条件
在进入该实验前，必须冻结：

- Schema
- Weak Reader
- Image Package Builder
- Prompt Factory
- Prompt Template
- Retrieval policy
- Visual Query policy
- Strong Model configuration

#### 评测内容
只运行一次冻结后的 Full Harness（A6），不再修改任何规则。

#### 论文作用
回答最关键的问题：

> **最终系统面对完全未见过的全合成文献，能否稳定恢复完整路线？**

---

### 6.5 Exp.4 — Scaling to the Route-Level Database

这是数据库价值实验。

#### 目的
证明这不是只能在 Gold 上跑的小系统，而是一套可以真正支撑数据库构建的 workflow。

#### 运行规模
建议至少报告三档：

- **5,000 papers**：minimum publishable
- **10,000 papers**：standard target
- **15,000–20,000 papers**：stretch target

#### 报告内容
- paper acquisition count
- total-synthesis relevance filter yield
- scheme detection count
- route reconstruction count
- automatic QC pass rate
- database size（routes / reaction blocks / compounds）
- failure profile
- throughput
- cost / paper

#### 论文作用
把工作从“benchmark + prototype”提升为“data infrastructure + scalable database construction”。

---

### 6.6 评测指标：从分子恢复到整条路线完全正确

本文不只报告字段级 F1，而建立四层评价指标。

#### Level 1 — Molecule / Identity Layer
- **Explicit Molecule Recovery**：显式 molecule node 的恢复
- Molecule Node Precision / Recall / F1
- **Compound Label Association Accuracy**
- Target Identification Accuracy

#### Level 2 — Reaction / Operation Layer
- **Reaction Block Exact Match**
- Directed Reaction Edge Precision / Recall / F1
- Operation Label Accuracy
- Operation-to-Reaction Assignment Accuracy
- Reaction Description Accuracy
- Reagent / Catalyst / Solvent F1
- Temperature / Pressure / Yield / ee / dr Accuracy

#### Level 3 — Route Layer
- **Route Edge F1**
- **Route Topology Exact Match**
- **Whole-Route Record Exact Match**

建议把：

> **Route Edge F1 + Route Topology Exact Match**

作为主文最核心的两个 endpoint。

#### Level 4 — Evidence & Reliability Layer
- Evidence Grounding Accuracy
- Unsupported Field Rate
- Source-Conflict Detection Recall
- Schema Validity Rate
- Unresolved Field Rate
- Input Tokens / Route
- Cost / Route
- Text Retrieval Calls / Route
- Visual Query Calls / Route

---

### 6.7 统计方式

- 所有主消融使用 **paired route-level comparison**
- 以 **route 为 bootstrap 单位**
- 对 Route Edge F1、Molecule F1、Condition F1 等报告 **95% bootstrap CI**
- 对 Whole-Route Exact Match 报告 exact / bootstrap CI
- 关键成对版本可使用 McNemar test
- 不把同一路线中的多个 operation 当成完全独立样本

---

## 7. 后续科学分析：作为数据库价值的延伸，而非当前主线的前置条件

路线数据库建立后，未来可以进一步研究：

- Target Similarity Trajectory
- Molecular Complexity Accumulation
- Reaction Contribution to Structural Advancement
- Temporal Evolution of Synthesis Strategy
- Route Similarity and Strategy Retrieval

这些问题能够进一步把数据库转化为合成化学研究工具，但在当前工作中建议定位为：

> **Downstream scientific opportunities enabled by the database**

而不是主论文能否成立的必要条件。

如果一个月内出现非常稳定且有解释力的统计结果，可以作为附加结果加入；如果没有，也不影响当前论文的核心闭环，因为本研究首先需要证明的是：

> **全合成文献可以通过统一的数据适配框架，被可靠、规模化地转化为 route-level synthesis data。**

---

## 最终一句话定位

> **We introduce a total-synthesis data adaptation framework that transforms multimodal scientific literature into standardized, evidence-grounded and route-level synthesis data through a modular multi-model reconstruction workflow.**

进一步强调其数据基础设施价值时可以表述为：

> **Rather than developing another structure-recognition model, we establish the missing data layer between human-readable total-synthesis literature and machine-readable synthesis knowledge.**
