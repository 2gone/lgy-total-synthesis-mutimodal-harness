你是本实验的 controller、工程执行者和结果整理者。你的职责不是自己解析化学论文，而是严格组织三个受控实验，通过调用项目中已经配置好的 **Gemini 3.7 Flash High**，判断它能否直接承担全合成论文中的全文阅读、Scheme 筛选、Scheme 文字读取和空间绑定任务，从而决定独立 OCR 模块是否还有必要。

本实验不评价 OCSR。不要调用 OCSR，不要生成 SMILES，不要要求 Gemini 识别具体化学结构。所有 molecular structures 在正式系统中将由独立 OCSR + human validation 提供，视为近似 oracle。

## 1. 输入包

使用：

`gemini_ocr_three_experiment_pack/`

先阅读：

* `README.md`
* `docs/EXPERIMENT_PROTOCOL.md`
* `experiment_manifest.json`
* `templates/gemini_evidence_output.schema.json`
* `templates/ocr_output.schema.json`

包含五篇论文：

* `ineleganolide`
* `alchivemycin_A`
* `hyperfirin`
* `kalmanol`
* `principinol_B`

禁止在 Gemini 预测完成和封存之前读取或使用：

`private_eval/`

该目录只能用于所有模型输出已经写盘、不可修改之后的 controller-side evaluation，并且其内容永远不得进入 Gemini context。

---

## 2. 必须完成三个完整实验

五篇论文全部参加三个条件。

因此至少完成：

**5 papers × 3 conditions = 15 independent Gemini runs**

不同 condition 之间必须使用完全独立的新 context。

不得把 Experiment A 的 Gemini 输出提供给 B。

不得把 A 或 B 的输出提供给 C。

不得让一个 paper 的 Gemini 输出成为另一个 paper 的输入。

Gemini model、thinking/configuration、sampling parameters、输出 Schema 和 worker instructions 在三组实验中保持完全相同。

唯一变化是输入材料。

---

# Experiment A — PDF Only

对每篇论文建立独立 Gemini task。

Gemini 只接收：

* 完整原始 PDF

不得提供：

* high-resolution page PNG
* external OCR
* manually selected Scheme pages
* private reference
* OCSR results
* previous Gemini results

目的：

测试 Gemini 原生 PDF multimodal/document capability 是否已经能够完成：

* visual-unit classification
* Scheme text reading
* spatial relation understanding
* reaction-edge reconstruction
* supporting-text retrieval

输出目录：

`results/A_pdf_only/<paper_id>/`

至少保存：

* `raw_response.*`
* `parsed_output.json`
* `run_metadata.json`

raw response 必须在任何后处理前保存。

---

# Experiment B — PDF + High-Resolution Pages

重新开启完全独立 Gemini context。

Gemini 接收：

* 完整 PDF
* 该论文在 `hires_pages/<paper_id>/` 中的全部页面 PNG

注意：

必须提供**全部页面**，不能只给提前挑选的 Scheme page。

否则会人为降低 Scheme classification 难度。

不得提供：

* external OCR
* private reference
* Experiment A output
* OCSR

目的：

判断如果给 Gemini 原始 PDF 外，再补充高分辨率页面，是否显著改善：

* tiny scheme text reading
* visual classification
* arrow/condition binding
* explicit route reconstruction

输出目录：

`results/B_pdf_hires/<paper_id>/`

同样保存 raw response、parsed JSON 和 run metadata。

---

# Experiment C — PDF + High-Resolution Pages + External OCR

首先使用项目当前准备采用的**独立 OCR engine**，对五篇论文的 high-resolution page renders 统一执行 OCR。

OCR 必须独立于 Gemini。

不能让 Gemini 自己生成所谓“external OCR”。

在首次 Gemini C-condition call 之前：

1. 对全部页面完成 OCR；
2. 将 OCR 输出持久化；
3. 记录 OCR engine name 和 version；
4. 输出满足 `templates/ocr_output.schema.json`；
5. 一旦持久化，不再因为 Gemini 输出而重新修改 OCR。

OCR 应尽可能保留：

* literal text
* page number
* bounding box
* confidence

然后对每篇论文开启新的、完全独立的 Gemini context。

Gemini 接收：

* 完整 PDF
* 全部 high-resolution pages
* 该论文固定的 external OCR result

不得提供：

* A/B outputs
* private reference
* OCSR

输出目录：

`results/C_pdf_hires_ocr/<paper_id>/`

---

# 3. 三组实验使用完全相同的 Gemini Worker Task

对 Gemini 只要求完成 evidence extraction 和 route-layout understanding。

不要要求它生成最终数据库。

不要要求 SMILES。

不要要求它推断未绘中间体。

不要让它使用外部资料。

Gemini 的任务如下。

---

## Gemini Worker Task

You are a multimodal scientific-document evidence extractor specialized in organic total-synthesis literature.

Molecular structure recognition is explicitly outside your task.

Do NOT generate SMILES, InChI, molecular formulas, or inferred molecular structures.

Treat each drawn molecule as a visual entity.

Use its visible paper compound number when unambiguous. Otherwise assign a temporary visual ID such as M01, M02, M03.

Your goal is to determine whether the documentary information required to reconstruct the explicit total-synthesis route can be recovered from the provided paper.

### Task A — Visual-unit classification

Inspect the complete document and identify every Scheme, Figure, panel, table, or visually distinct chemical unit relevant to synthesis.

Classify each as exactly one of:

* MAIN_SYNTHESIS_ROUTE
* RETROSYNTHESIS
* MECHANISM
* OPTIMIZATION
* CONTROL_EXPERIMENT
* FAILED_OR_ALTERNATIVE_ROUTE
* BIOSYNTHESIS
* BACKGROUND
* CHARACTERIZATION
* OTHER

A MAIN_SYNTHESIS_ROUTE must represent chemistry actually performed as part of the reported synthesis leading to the target or a required synthetic fragment.

Do not classify retrosynthetic plans, mechanistic proposals, optimization screens, unsuccessful alternatives, or biosynthetic pathways as MAIN_SYNTHESIS_ROUTE merely because they contain chemical structures.

### Task B — Literal scheme-text recovery

For every MAIN_SYNTHESIS_ROUTE visual unit, recover all visible non-structural text as literally as possible.

Include:

* compound numbers
* step labels
* reagents
* catalysts
* ligands
* solvents
* additives
* temperatures
* times
* pressures
* yields
* ee
* dr
* ratios
* scale
* “then”
* “over two steps”
* “over three steps”
* “brsm”
* “one pot”
* panel labels

Do not normalize away information during this stage.

If uncertain, report uncertainty instead of guessing.

### Task C — Reaction-layout reconstruction

Using visual layout and arrows, reconstruct explicit reaction relations.

Represent each molecular object by:

* its paper compound number when confidently visible;
* otherwise a temporary visual object ID.

For every apparent reaction transition return:

* input object(s)
* output object(s)
* source step label(s)
* visible arrow-associated text
* page / Scheme / panel
* confidence

Do not invent an intermediate that is not explicitly drawn.

If several source operations occur between two explicitly drawn molecular endpoints, preserve the operation labels without inventing intermediate structures.

### Task D — Supporting-text retrieval

Locate the body-text sentences and caption passages that directly discuss every MAIN_SYNTHESIS_ROUTE unit and its transformations.

Return the original source text verbatim or in short verbatim spans.

Do not replace it with a summary.

For each span identify:

* page
* section if identifiable
* discussed Scheme/Figure
* compound numbers mentioned
* reaction or transformation discussed

Ignore unrelated introduction, biology, references, and general background unless needed to interpret the synthesis.

### Task E — Reaction-centric evidence packets

For each reconstructed reaction relation, align:

IMAGE EVIDENCE:

* input/output visual entities
* compound numbers
* step labels
* visible arrow text
* location

TEXT EVIDENCE:

* relevant verbatim body/caption evidence
* location
* explicit reaction description

Preserve source disagreements.

Do not resolve image/text conflicts using chemical intuition.

### Task F — Uncertainty

Explicitly flag:

* unreadable scheme text
* ambiguous molecule-label pairing
* ambiguous arrow ownership
* ambiguous yield scope
* image-text conflict
* reaction discussed in text but not explicitly drawn
* conditions visible in image but absent from body
* conditions stated in body but absent from image

Output must validate against:

`templates/gemini_evidence_output.schema.json`

Do not include free-form chemistry essays outside the schema output.

---

# 4. OCR-specific handling in Experiment C

External OCR is evidence, not authority.

When OCR and Gemini vision disagree, Gemini must preserve the disagreement.

For example:

OCR:
`BBr3`

Gemini visual reading:
`BCl3`

Do not silently choose one.

Return a conflict / uncertainty.

The purpose of Experiment C is specifically to determine whether external OCR improves:

* literal text recovery;
* spatial binding;
* condition-to-reaction association.

---

# 5. Run metadata

For every Gemini call record:

* paper_id
* experiment condition
* exact Gemini model identifier
* relevant model configuration
* start/end timestamp
* wall-clock duration
* token/input usage if available
* output usage if available
* errors/retries
* input file list
* hashes or manifest references

No silent retry with altered prompts.

If retry is necessary, record it.

---

# 6. Do not evaluate during generation

Complete and seal all 15 primary model outputs first.

A result is considered sealed once:

* raw response exists;
* parsed output exists;
* file hash has been recorded.

Do not edit sealed outputs.

Only after all 15 are sealed may you inspect:

`private_eval/visual_unit_reference.json`

That file is controller-side reference material only.

Never pass it to Gemini.

---

# 7. Evaluation

Evaluate A, B and C separately.

Primary metrics:

### 1. Visual Unit Classification Accuracy

Can the model distinguish:

main synthesis
vs retrosynthesis
vs mechanism
vs optimization
vs failed route
vs biosynthesis?

### 2. Visible Condition Exact Accuracy

For visible reaction text, measure exact or chemically equivalent recovery of:

* reagent names
* solvents
* temperatures
* yields
* step annotations

Do not score typography differences as chemical errors.

### 3. Condition-to-Reaction Binding Accuracy

A condition counts as correct only if both:

* text is correctly recovered;
* it is assigned to the correct reaction transition.

This metric is more important than raw OCR accuracy.

### 4. Explicit Route Edge Accuracy

Compare reconstructed explicit molecular-object transitions.

Do not evaluate molecular graph correctness.

OCSR is out of scope.

Secondary metrics:

* compound-label-to-object association
* supporting-text retrieval quality
* ambiguity/abstention behavior
* latency per paper
* cost / token usage per paper

For fields without existing machine-readable Gold, generate a side-by-side human review table using:

`templates/human_scoring_template.csv`

Do not invent Gold automatically from another model.

---

# 8. Required comparison outputs

After scoring, create:

`results/comparison_summary.csv`

with rows for every paper × metric and columns:

* A_PDF_ONLY
* B_PDF_HIRES
* C_PDF_HIRES_OCR

Also create:

`results/aggregate_summary.csv`

with aggregate results across five papers.

Create:

`results/error_analysis.md`

Separate errors into at least:

* wrong visual-unit classification
* text unreadable
* text misread
* compound-label mismatch
* wrong arrow ownership
* wrong condition binding
* route-edge error
* supporting-text retrieval error
* over-inference
* failure to abstain

---

# 9. Final OCR decision

Create:

`results/OCR_DECISION.md`

Use the following interpretation.

## Case 1

A ≈ B ≈ C

Conclusion:

Independent OCR is unnecessary.

Prefer the simplest PDF-native Gemini path.

## Case 2

B clearly > A, while C ≈ B

Conclusion:

The main limitation is PDF/image resolution.

Use PDF + extracted high-resolution pages or Scheme images.

Independent OCR is unnecessary.

## Case 3

C clearly > B

Especially if improvement appears in:

* Visible Condition Exact Accuracy
* Condition-to-Reaction Binding Accuracy

Conclusion:

Retain OCR as a low-level spatial-text evidence module.

Do not treat OCR as the semantic parser.

## Case 4

Gemini performs well on text recovery but poorly on spatial binding

Conclusion:

OCR alone will not solve the main problem.

The future workflow requires explicit layout / relation reasoning.

---

# 10. Final controller report

At completion, report concisely:

1. whether Gemini PDF-native understanding is sufficient;
2. whether high-resolution pages materially improve performance;
3. whether external OCR materially improves performance beyond high-resolution pages;
4. which error category dominates;
5. whether independent OCR should be:

   * removed,
   * retained as fallback,
   * or retained in the main pipeline;
6. recommended simplified production Harness.

Do not evaluate OCSR.

Do not recommend training a new model.

The purpose of this experiment is to decide the architecture of the document-understanding branch, not to optimize chemical structure recognition.
