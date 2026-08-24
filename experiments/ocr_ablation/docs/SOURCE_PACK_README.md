# Gemini OCR Three-Experiment Input Pack

This package contains all static inputs needed for a controlled three-condition experiment over five user-supplied total-synthesis papers.

- `papers/`: original PDFs.
- `hires_pages/`: every PDF page rendered at 220 dpi.
- `experiment_manifest.json`: file paths, page counts, and hashes.
- `templates/gemini_evidence_output.schema.json`: required Gemini worker output contract.
- `templates/ocr_output.schema.json`: external OCR contract for Experiment C.
- `templates/human_scoring_template.csv`: manual pilot scoring sheet.
- `private_eval/visual_unit_reference.json`: controller-only visual-unit reference; never pass this file to Gemini.
- `docs/EXPERIMENT_PROTOCOL.md`: exact A/B/C isolation rules.

The Codex orchestration prompt is intentionally not included in this package.
