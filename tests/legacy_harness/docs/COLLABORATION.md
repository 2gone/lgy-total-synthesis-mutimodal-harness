# Collaboration guide

## Branch scope

Use one result branch per suite or a smaller paper/condition batch. Do not mix Harness changes and scientific outputs in the same pull request.

Before committing:

```powershell
python -m pytest
python -m harness validate --offline
git status --short
```

## Result contract

For every model cell, submit the entire sealed directory:

- `attempts/NNN/`
- `raw_response.sse` or `raw_response.json`
- `parsed_output.json`
- `validation_report.json`
- `run_metadata.json`
- `seal.json`

For OCR prerequisites, include `ocr_output.json`, `ocr_metadata.json`, its attempt output, and `seal.json`.

Never edit a sealed cell. If a protocol or Harness change requires a rerun, use a new branch and document why the old observation is superseded; do not delete historical evidence from an already reviewed result branch.

## Isolation checklist

- Confirm the dry-run asset list matches the chosen suite, condition, and paper.
- Do not paste outputs or OCR evidence from another condition into prompts.
- Do not provide source manifests or controller-only references to the model.
- Do not run OCSR or add SMILES, InChI, formulas, or inferred structures.
- Keep human scoring and architecture decisions after all generation seals exist.

## Expected controller summaries

The route-image experiment produces `per_paper_comparison.csv`, `aggregate_comparison.csv`, `error_analysis.md`, and `ARCHITECTURE_DECISION.md`. Use the templates in the experiment directory. The OCR experiment uses its supplied human-scoring template and controller prompt.
