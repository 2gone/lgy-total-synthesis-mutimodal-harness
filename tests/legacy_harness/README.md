# Total Synthesis Multimodal Harness

A private, reproducible collaboration package for two controlled experiments using exactly `gemini-3.7-flash-high`:

1. `ocr_ablation` — compare PDF-only, PDF + high-resolution pages, and PDF + pages + deterministic OCR.
2. `route_image_understanding` — compare a full PDF, selected raw route crops, and structure-hidden tokenized route crops.

The repository contains experiment inputs and an execution Harness, but no credentials and no previous model outputs. The two suites have separate prompts, manifests, schemas, result roots, and evaluation barriers.

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
python -m harness validate --offline
python -m harness dry-run --suite ocr_ablation --condition A_pdf_only --paper ineleganolide
```

Fill `.env` locally before a paid run. The model name is locked in code and is not configurable.

See `docs/RUNBOOK.md`, `docs/COLLABORATION.md`, and each experiment's supplied controller prompt before running or scoring experiments.
