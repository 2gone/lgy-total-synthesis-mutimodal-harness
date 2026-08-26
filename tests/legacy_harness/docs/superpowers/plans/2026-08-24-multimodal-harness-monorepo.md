# Total Synthesis Multimodal Harness — Implementation Plan

> Execute this plan with the `executing-plans` workflow. Implement Harness behavior with strict red-green-refactor cycles.

**Goal:** Deliver and publish a clean private monorepo containing two isolated controlled-experiment suites and an offline-verifiable shared Harness.

**Architecture:** A Python package owns suite inventories, payload construction, streaming API calls, evidence sealing, evaluation barriers, and repository validation. Static experiment assets remain suite-local, while identical PDFs are deduplicated into a shared corpus. A small Node helper performs OCR only for the OCR-ablation suite.

**Technology:** Python 3.11+, pytest, jsonschema, stdlib HTTP/SSE, Node.js/Tesseract.js, GitHub Actions.

---

## Task 1: Scaffold and source-material audit

**Files:** repository directories, `.gitignore`, `pyproject.toml`, `README.md`.

1. Verify the source ZIP and the existing OCR pack inventories without reading controller-only answer content.
2. Create the normalized directory tree in the dated delivery directory.
3. Copy one verified copy of each PDF to `corpus/papers/`.
4. Copy high-resolution pages, selected route images, previews, source prompts, templates, and private evaluation material into their isolated suite directories.
5. Confirm no old result, cache, `node_modules`, OCR intermediate, or `.env` was copied.

## Task 2: Specify expected Harness behavior with failing tests

**Files:** `tests/test_config.py`, `tests/test_suites.py`, `tests/test_sealing.py`, `tests/test_validation.py`, `tests/test_evaluation.py`.

1. Test that generated requests always use `gemini-3.7-flash-high` and public metadata never exposes a secret.
2. Test exact condition inventories for both suites, including route-image input isolation and the token preface only in Route C.
3. Test that streaming output and schema-invalid parsed output are preserved and sealed immutably.
4. Test that controller-only evaluation access is blocked until all expected suite seals exist.
5. Test offline repository validation against missing, modified, and valid assets.
6. Run tests and record expected failures caused by the absent production package.

## Task 3: Implement minimal Harness core

**Files:** `harness/config.py`, `harness/suites.py`, `harness/api.py`, `harness/sealing.py`, `harness/evaluation.py`, `harness/validation.py`, `harness/cli.py`, `harness/__main__.py`.

1. Implement configuration parsing and the immutable exact-model lock.
2. Implement suite/condition asset selection without passing controller manifests as model content.
3. Implement OpenAI-compatible streamed chat-completions handling and raw-event preservation.
4. Implement attempt persistence, JSON/schema validation, sealing, and immutable resume behavior.
5. Implement evaluation barriers and private-path guard.
6. Implement `validate`, `dry-run`, and `run` CLI commands.
7. Run each focused test after its minimal implementation, then run the full suite.

## Task 4: Package experiment-specific materials

**Files:** `experiments/**`, `tools/ocr/**`, `results/**`.

1. Preserve both supplied controller prompts as provenance artifacts.
2. Provide concise worker prompts derived from each protocol without answer leakage.
3. Provide strict JSON schemas and human-scoring templates.
4. Adapt the tested Tesseract.js helper for OCR C prerequisites; include lockfile, exclude dependencies.
5. Provide empty result roots and instructions, not prior observations.

## Task 5: Collaboration, security, provenance, and CI

**Files:** `.env.example`, `.github/workflows/validate.yml`, `docs/*.md`, `NOTICE.md`, `.github/CODEOWNERS`.

1. Document setup, run order, fixed parameters, retry rules, result submission, and no-cross-condition constraints.
2. Document the repaired Kalmanol page and deduplicated PDF hashes.
3. Add credential and gateway safety guidance.
4. Add offline GitHub Actions validation with no model credentials.

## Task 6: Unified manifest and final verification

**Files:** `MANIFEST.sha256.json`.

1. Generate deterministic hashes for distributed files, excluding Git metadata and the manifest itself.
2. Run pytest, offline validation, JSON parsing, source-material counts, secret-pattern scan, and Git ignored-file checks.
3. Confirm no partial results or local-only material exists.
4. Commit the verified implementation branch, merge it into `main`, and re-run verification on `main`.

## Task 7: Private GitHub delivery

1. Confirm the target GitHub repository does not already exist.
2. Create `hululuw/total-synthesis-multimodal-harness` as private.
3. Push `main`, confirm remote visibility and default branch, and report the repository URL and verification results.
