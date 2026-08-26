# Total-Synthesis Image Understanding Experiment Pack

This pack corrects the experimental focus from full-document OCR to **route-image understanding**.

## Three controlled conditions

1. **A - Full PDF baseline**: give the complete paper to the worker model. This is a secondary baseline for full-document behavior.
2. **B - Raw route images**: give only the selected performed total-synthesis route images listed for that paper. Retrosynthesis, mechanisms, optimization panels, biosynthesis, and failed alternatives are excluded unless inseparable from the performed route.
3. **C - Tokenized route images**: use the same crops, but every molecular drawing is fully erased and replaced by a stable visual ID such as `M03` and its visible paper label in parentheses. Conditions, arrows, labels, yields, and layout are retained.

## Why the tokenized condition exists

It measures whether the model can understand route topology, molecule-label pairing, arrow ownership, condition binding, and operation grouping **without using molecular structure semantics**. OCSR is outside this experiment and is treated as a separately validated oracle layer.

## Input grouping

For one paper and one condition, provide all route images listed under that paper in `manifests/image_experiment_manifest.json` in a single worker task.

## Token convention

`Mxx` is a stable visual molecule entity. The value in parentheses is the visible paper compound label or name. Repeated depictions of the same compound reuse the same `Mxx`. A label-only compound that is referenced from another figure is recorded in the manifest but is not given a fabricated structure token.

## Important

The package contains no worker prompt. The controller prompt should be supplied separately so the static input pack remains model-agnostic.
