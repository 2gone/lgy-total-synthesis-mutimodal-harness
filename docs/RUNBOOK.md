# Runbook

## 1. Install

Requirements: Python 3.11 or newer; Node.js 20 or newer only for the OCR prerequisite.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m harness validate --offline
```

For OCR conditions:

```powershell
npm ci --prefix tools/ocr
```

## 2. Configure paid calls locally

Copy `.env.example` to `.env` and fill the two placeholders. The parser also accepts the supplied legacy curl-style example containing a chat-completions URL and a bearer header.

The model is hard-locked to `gemini-3.7-flash-high`; there is no model CLI flag or environment override. Fixed generation parameters are temperature 0, top-p 1, maximum output 32768 tokens, streaming enabled, and a 900-second request timeout.

## 3. Inspect without making a call

```powershell
python -m harness dry-run --suite ocr_ablation --condition A_pdf_only --paper ineleganolide
python -m harness dry-run --suite route_image_understanding --condition C_tokenized_route_images --paper kalmanol
```

`dry-run` never loads `.env` and never calls the gateway. Review every listed asset before proceeding.

## 4. OCR ablation

Run A and B independently. Generate the deterministic OCR artifact before C:

```powershell
python -m harness run --suite ocr_ablation --condition A_pdf_only --paper ineleganolide
python -m harness run --suite ocr_ablation --condition B_pdf_hires --paper ineleganolide
python -m harness ocr --paper ineleganolide
python -m harness run --suite ocr_ablation --condition C_pdf_hires_ocr --paper ineleganolide
```

Repeat for the five paper IDs in `experiments/ocr_ablation/manifests/source_manifest.json`.

## 5. Route-image understanding

```powershell
python -m harness run --suite route_image_understanding --condition A_full_pdf --paper ineleganolide
python -m harness run --suite route_image_understanding --condition B_raw_route_images --paper ineleganolide
python -m harness run --suite route_image_understanding --condition C_tokenized_route_images --paper ineleganolide
```

Route B receives raw crops only. Route C receives tokenized crops only and automatically prepends the exact Mxx identity sentence required by the experiment protocol.

## 6. Failure and retry rules

- HTTP/transport failures and unparseable responses may be retried, up to three attempts by default.
- Every failed attempt is preserved under `attempts/NNN/`.
- A parseable schema-invalid response is an observation: it is sealed immediately and is not retried or corrected.
- Three repeated gateway/parse failures stop the run with a nonzero exit; report the gateway problem rather than changing the model or inputs.
- An existing `seal.json` prevents replacement on resume.

## 7. Evaluation barrier

Do not inspect `controller_only/private_eval/` during generation. The Harness only exposes controller-only files after all 15 suite cells have valid seal structures. Scoring is a separate controller phase.
