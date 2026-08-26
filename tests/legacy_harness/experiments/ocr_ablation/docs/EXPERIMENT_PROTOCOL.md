# Three-Experiment Protocol

## Purpose
Evaluate whether the configured Gemini Flash High model can replace or substantially reduce the independent OCR branch for full-document total-synthesis parsing. OCSR accuracy is not evaluated here; molecular structures are treated as opaque visual objects / future oracle-linked entities.

## Fixed corpus
Five supplied total-synthesis papers. Every condition must process all five papers.

## Conditions

### Experiment A — PDF only
Input to Gemini: complete PDF only.
Do not provide external page renders, external OCR, preselected Scheme pages, or any private reference labels.

### Experiment B — PDF + high-resolution page renders
Input to Gemini: the same complete PDF plus all high-resolution page PNGs for that paper.
All pages are supplied, not only Scheme pages, so visual-unit selection remains part of the task.
No external OCR text is supplied.

### Experiment C — PDF + high-resolution page renders + external OCR
Input to Gemini: exactly the Experiment B materials plus external OCR output generated once from the high-resolution pages with the configured OCR engine. Persist OCR outputs before any Gemini call for Experiment C. OCR output must include literal text, page number, bounding box, and confidence when available.

## Fairness rules
1. Use the same Gemini model, model settings, worker instructions, output schema, and paper order for all three conditions.
2. Do not use outputs from A when running B, or outputs from A/B when running C. Each condition starts from a clean context.
3. OCSR / SMILES generation is forbidden in all three experiments.
4. The model must not receive files under `private_eval/`. The controller may use them only after all predictions for a paper/condition are sealed.
5. Save the raw Gemini response before normalization or scoring.
6. Do not manually correct model outputs before scoring.
7. Record token usage, wall time, and any API/model errors.

## Primary questions
- Can the model correctly distinguish main synthesis schemes from retrosynthesis, mechanisms, optimization, controls, biosynthesis, background, and failed/alternative routes?
- Can it literally recover small scheme text such as compound labels, step labels, reagents, temperatures, yields, and multi-step scope annotations?
- Can it bind that text to the correct reaction arrow / explicit molecular objects?
- Can it align the image-derived reaction with the correct body-text evidence?

## Primary metrics
1. Visual Unit Classification Accuracy
2. Visible Condition Exact Accuracy
3. Condition-to-Reaction Binding Accuracy
4. Explicit Route Edge Accuracy

## Secondary metrics
- Compound-label-to-visual-object association accuracy
- Text evidence retrieval precision / recall
- Uncertainty calibration / abstention quality
- Cost and latency per paper

## Decision rule for OCR
- If A ≈ B ≈ C: remove independent OCR from the main pipeline.
- If B > A and C ≈ B: retain high-resolution page/figure rendering, remove OCR.
- If C > B on condition extraction or binding: keep OCR as an auxiliary spatial-text evidence provider.
