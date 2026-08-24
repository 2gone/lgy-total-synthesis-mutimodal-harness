# Total Synthesis Multimodal Harness — Repository Design

**Date:** 2026-08-24
**Status:** Approved
**Repository:** `total-synthesis-multimodal-harness`

## Goal

Create one clean, private GitHub repository that collaborators can clone and use to run two controlled Gemini experiments without receiving local credentials, partial results, caches, or condition-cross-contaminated inputs.

The only permitted request model is `gemini-3.7-flash-high`. CI is offline and never calls the paid model.

## Repository layout

```text
corpus/papers/                         # one deduplicated copy of each source PDF
experiments/
  ocr_ablation/                       # PDF / high-resolution page / OCR ablation
    prompts/
    manifests/
    templates/
    hires_pages/
    controller_only/private_eval/
  route_image_understanding/          # PDF / raw crop / tokenized crop experiment
    prompts/
    manifests/
    templates/
    route_images/{raw,tokenized}/
    previews/
harness/                              # shared controller code and suite adapters
tools/ocr/                            # reproducible Tesseract.js helper, no node_modules
tests/                                # offline behavior and repository-integrity tests
docs/                                 # runbook, collaboration, security, provenance
results/                              # empty collaboration result roots and instructions
.github/workflows/validate.yml        # tests and hashes only
```

## Experiment isolation

The two suites have separate prompts, manifests, schemas, input selectors, result roots, and seal inventories. Each condition receives only its declared assets:

- OCR A: PDF only.
- OCR B: PDF plus high-resolution page images.
- OCR C: PDF, high-resolution pages, and the sealed OCR artifact created for that paper.
- Route A: full PDF only.
- Route B: selected raw route images only.
- Route C: selected tokenized route images only and the exact token-identity preface required by the controller prompt.

Manifests are controller inputs and are never sent wholesale to the model. Controller-only evaluation references are inaccessible until the expected sealed outputs for a suite exist.

## Reproducibility and evidence

Every request records public generation parameters, an input inventory, raw streaming response, parsed response, validation report, and immutable seal hashes. Parseable but schema-invalid model output is preserved as observed; the Harness does not silently repair scientific content. Retries are limited to transport or unparseable-response failures.

All distributed assets are covered by a repository hash manifest. The previously truncated Kalmanol page image is replaced by the verified re-render and documented in data provenance.

## Security and licensing boundary

The repository is private. `.env`, credentials, private gateway values, old results, OCR intermediates, `node_modules`, and caches are excluded. `.env.example` contains placeholders only. Code and operational documentation do not grant redistribution rights for third-party papers; `NOTICE.md` states that collaborators must verify their authorization.

## Collaboration model

Contributors run `python -m harness validate --offline` before work, use `dry-run` to review the exact asset inventory, then run one suite/condition/paper at a time. Paid calls require local credentials. Pull requests are validated without secrets and without network model calls.
