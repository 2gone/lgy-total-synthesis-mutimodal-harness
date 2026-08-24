你是本实验的 controller 和 harness owner。你需要调用项目中已经配置好的 Gemini 3.7 Flash High，完成一个以“全合成路线图片理解”为中心的三条件受控实验。

本实验不评价 OCSR。不要让 Gemini 生成 SMILES、InChI、分子式或推断化学结构。正式系统中的分子结构将由独立 OCSR + human validation 提供，视为 oracle layer。

## 1. 输入包

使用：

`total_synthesis_image_understanding_pack/`

首先读取：

* `README.md`
* `docs/ROUTE_IMAGE_SELECTION.md`
* `manifests/image_experiment_manifest.json`

五篇论文为：

* ineleganolide
* alchivemycin_A
* hyperfirin
* kalmanol
* principinol_B

每篇论文必须完成三个完全独立的 Gemini run，共：

**5 papers × 3 conditions = 15 primary runs**

不同条件之间必须使用新的独立 context，不得传递此前模型输出。

除输入材料外，Gemini model、thinking level、sampling parameters、worker prompt 和输出格式必须保持一致。

---

# 2. 三个实验条件

## Condition A — Full PDF Baseline

Gemini 只接收该论文的完整 PDF。

不得提供：

* route image crops
* tokenized images
* previous outputs
* OCSR results
  -人工筛选结果

这个条件是辅助 baseline，用于观察模型在完整文章上下文中能否：

* 找出实际执行的 total-synthesis route images；
* 排除 retrosynthesis、mechanism、optimization、biosynthesis 和 failed alternatives；
* 恢复路线图中的反应关系与条件。

输出目录：

`results/A_full_pdf/<paper_id>/`

---

## Condition B — Raw Route Images

Gemini 不接收 PDF。

根据 `image_experiment_manifest.json`，一次性向 Gemini 提供该论文对应的所有：

`route_images/raw/<paper_id>/*.png`

例如 Alchivemycin A 需要在同一个 task 中同时提供：

* Figure 3a
* Figure 4
* Figure 5c

不要额外提供正文、caption、OCR、OCSR 或人工说明。

这个条件是核心实验，测试：

> 模型只看实际全合成路线图时，能否理解显式分子节点、编号、箭头、条件和路线关系。

输出目录：

`results/B_raw_route_images/<paper_id>/`

---

## Condition C — Tokenized Route Images

Gemini 不接收 PDF，也不接收 Raw Route Images。

一次性向 Gemini 提供该论文对应的所有：

`route_images/tokenized/<paper_id>/*.png`

Token 规则：

* `Mxx` 是稳定的 visual molecule entity；
* 括号中的内容是论文 compound label 或名称；
* 所有 chemical structure information 均被故意删除；
* 相同 `Mxx` 表示同一个显式分子实体；
* 箭头、条件、yield、step labels 和布局仍然保留。

同时在 Worker Prompt 开头加入一句：

> Each box labelled Mxx represents one molecular object. The value in parentheses is the visible paper compound label. Molecular structures are intentionally hidden. Treat the Mxx identifiers as the only molecular identities available.

这个条件测试：

> 在完全没有分子结构语义的情况下，模型能否仅根据 layout、paper labels、arrows 和 conditions 重建路线。

输出目录：

`results/C_tokenized_route_images/<paper_id>/`

---

# 3. Gemini Worker Task

You are analyzing one organic total-synthesis paper or a set of route images from that paper.

Molecular structure recognition is outside your task.

Do not generate SMILES, InChI, molecular formulas, molecular names inferred from structures, or unshown intermediates.

Your task is to recover the explicit documentary reaction route.

## A. Visual-unit identification

For each provided visual unit report:

* source page or supplied image name;
* Scheme/Figure/panel identifier if visible;
* whether it is:

  * MAIN_SYNTHESIS_ROUTE
  * RETROSYNTHESIS
  * MECHANISM
  * OPTIMIZATION
  * CONTROL_EXPERIMENT
  * FAILED_OR_ALTERNATIVE_ROUTE
  * BIOSYNTHESIS
  * CHARACTERIZATION
  * OTHER

For the raw-route and tokenized-route conditions, the supplied images are expected to be route candidates, but you must still report if an image contains characterization inserts, side products, screening information, or non-route material.

## B. Molecular-object inventory

Enumerate every explicit molecular object.

Use:

* the paper compound number when visible;
* the provided Mxx identifier in tokenized images;
* otherwise assign a temporary visual ID.

Do not infer a structure.

Distinguish:

* explicitly drawn object;
* label-only reference to a compound drawn elsewhere;
* duplicate depiction of an already listed compound;
* characterization depiction such as X-ray/ORTEP.

## C. Literal visible-text extraction

Read all route-relevant visible text as literally as possible:

* compound labels
* operation labels
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
* scales
* “then”
* “over two steps”
* “over three steps”
* “brsm”
* “one pot”

Do not normalize or expand abbreviations during literal extraction.

If uncertain, return the visible alternatives and mark UNCERTAIN.

## D. Explicit reaction relations

For every reaction arrow or displayed transition report:

* reaction ID;
* input molecular entity or entities;
* output molecular entity or entities;
* source operation label or labels;
* visible arrow-associated text;
* single-step versus displayed multi-operation sequence;
* confidence.

Do not invent a molecule that is not explicitly drawn.

If several operations occur between two explicit endpoints, retain all operation labels but keep the same explicit endpoints.

## E. Route assembly

Assemble the performed explicit synthesis route or route branches in order.

Report:

* route entry entities;
* convergent branches;
* branch-merging reactions;
* ordered explicit reaction relations;
* final target;
* duplicated entities reused across panels;
* side products that are drawn but are not part of the selected main path.

## F. Uncertainty

Flag:

* ambiguous molecule-label association;
* ambiguous arrow ownership;
* unreadable condition;
* unclear yield scope;
* route discontinuity;
* duplicate versus distinct compound ambiguity;
* side product versus main-route product ambiguity;
* information that would require text outside the supplied images.

Do not use outside chemical knowledge to fill gaps.

---

# 4. Required output

Return one structured result per paper containing:

1. visual_units
2. molecular_entities
3. literal_visible_text
4. reaction_relations
5. assembled_routes
6. side_products_and_characterization
7. uncertainties

Save both:

* raw Gemini response
* parsed structured result
* run metadata

Record:

* exact model identifier
* condition
* input files
* timestamps
* latency
* token usage if available
* retries and errors

Seal each result immediately after generation. Do not modify sealed predictions after looking at another condition.

---

# 5. Evaluation

The central comparison is:

**A Full PDF vs B Raw Route Images vs C Tokenized Route Images**

Primary metrics:

## Route-image selection

Only for Condition A:

* relevant-route visual unit recall
* false-positive classification of retrosynthesis/mechanism/optimization/failed routes

## Molecular-object and label understanding

* explicit object recall
* paper-label-to-object association accuracy
* duplicate-object handling accuracy

Do not score molecular graph accuracy.

## Literal condition recovery

* reagent accuracy
* catalyst/ligand accuracy
* solvent accuracy
* temperature/time/pressure accuracy
* yield/ee/dr accuracy
* multistep-scope accuracy

## Spatial binding

* condition-to-reaction binding accuracy
* operation-label-to-reaction binding accuracy
* input/output association accuracy

## Route reconstruction

* explicit node F1
* explicit route-edge F1
* branch-merging accuracy
* reaction-block accuracy
* whole-route exact match

---

# 6. Interpretation

## B versus A

If B is better than A:

Full-document context is introducing noise or reducing visual resolution. Production should first select route images and then analyze them.

If A is better than B:

正文或跨页上下文 materially helps route reconstruction, and the future harness should add a Text Package after image interpretation.

## C versus B

If C is close to B:

Molecular structure semantics are not essential for route-layout understanding. The route parser can be developed before OCSR is available using stable Mxx tokens.

If B is substantially better than C:

Molecular drawings provide useful visual cues for identity continuity, repeated compounds, branch merging, or reaction interpretation.

## A versus B/C

Condition A remains a full-document baseline, not the main production design.

The main production question is whether route-image understanding should operate on:

* Raw Route Images;
* Tokenized Route Images;
* or Raw/Normalized Route Images plus a separate Text Package.

---

# 7. Final report

Create:

* `results/per_paper_comparison.csv`
* `results/aggregate_comparison.csv`
* `results/error_analysis.md`
* `results/ARCHITECTURE_DECISION.md`

The final architecture decision must state:

1. whether route images should be selected before model analysis;
2. whether molecular drawings materially improve route understanding;
3. whether a tokenized representation is sufficient before OCSR is available;
4. which errors require later Text Package support;
5. the recommended minimal production harness.

Do not evaluate OCSR.

Do not recommend training a new vision model.

The purpose is to determine how well a general multimodal worker understands actual total-synthesis route images and which input representation should be used in the final harness.
