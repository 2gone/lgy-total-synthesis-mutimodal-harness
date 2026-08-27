# Project Charter

## WHY

Total-synthesis literature must be converted into trustworthy downstream evidence without losing the exact relationship between scientific claims, source text, and source images. TS-DAF exists to make that routing reproducible, auditable, and safe to scale.

## Scope

TS-DAF is one Python distribution with internally layered lanes. It accepts a normalized TS-DAF
paper package, canonicalizes its inventory, routes article text and complete visual assets, records
immutable artifacts and execution history, gates downstream image work with receipts, and can run
the first image-processing stage from an authorized image job through molecule crops and OCSR.

The project-memory evidence is preserved in [Thesis Mainline](research/THESIS_MAINLINE_0824.md) and [Harness Engineering](research/HARNESS_ENGINEERING_0824.md).

## Scientific goal

For each total-synthesis paper, identify the complete forward synthetic route and the exact supporting text and visual assets, with every selection traceable to canonical source material and repeatable under pinned prompts, schemas, provider profiles, and model versions.

## Non-goals

The current production scope does not parse PDFs or raw MinerU ZIP archives, infer individual
reactions, bind OCSR molecules to their Scheme identities, build the final Image Package, silently
rewrite model judgments, create image-processing jobs without a release receipt, or provide a
review UI.
