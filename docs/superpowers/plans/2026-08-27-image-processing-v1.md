# Image Processing v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect an authorized `ImageProcessingJob` to MolYOLO crops and one-crop Gemini OCSR results without changing the frozen document workflow or global contracts.

**Architecture:** Keep all new data models private to the image lane. A source loader verifies exact image bytes, a detector protocol isolates the concrete MolYOLO adapter, and the existing local runtime seals one detection stage per image plus one OCSR stage per crop. Application composition owns concrete clients, credentials, prompt location, and runtime-bundle paths.

**Tech Stack:** Python 3.11–3.13, Pydantic v2, Pillow, existing `LocalObjectStore`/`LocalRunner`, vendored MolYOLO-compatible Ultralytics, OpenAI-compatible Gemini adapter.

## Global Constraints

- Do not modify the frozen `tsdaf.contracts` models or schema bundle.
- Do not import the legacy harness from production code.
- Do not read PDF or MinerU packages; read only `source_asset.relative_path` from the supplied image package.
- Do not commit weights, vendored Ultralytics, secrets, or `.env` contents.
- Use one Gemini request per crop, four workers, two retries, 120-second timeout, fixed `gemini-3.7-flash-high`, and temperature zero by default.
- Keep the article-router Gemini profile behavior unchanged.
- Use TDD for each production behavior and run the focused test after every red/green cycle.
- Do not create molecule-binding or final Image Package contracts.

---

### Task 1: Private image models and verified source loading

**Files:**
- Create: `src/tsdaf/lanes/image/models.py`
- Create: `src/tsdaf/lanes/image/source_loader.py`
- Modify: `src/tsdaf/lanes/image/__init__.py`
- Modify: `pyproject.toml`
- Test: `tests/unit/image/test_source_loader.py`
- Test: `tests/unit/image/test_models.py`

**Interfaces:**
- Produces: `LoadedSourceImage`, `MoleculeCropSet`, `GeminiOCSRResult`, and `load_source_image(job, package_dir, store)`.
- Consumes: existing `ImageProcessingJob`, `ArtifactRef`, and `LocalObjectStore`.

- [ ] **Step 1: Write failing source-boundary tests**

```python
loaded = load_source_image(job=job, package_dir=package_dir, store=store)
assert loaded.source_artifact.sha256 == job.route_image_item.source_asset.sha256
assert loaded.dimensions == (80, 60)
```

Add separate tests proving absolute/escaping paths, wrong hashes, wrong sizes, mismatched media types,
symlinks, and mismatched declared dimensions fail before detection.

- [ ] **Step 2: Run the tests and verify RED**

Run: `python -m pytest tests/unit/image/test_source_loader.py tests/unit/image/test_models.py -q`

Expected: collection fails because the new image modules do not exist.

- [ ] **Step 3: Implement the minimal models and loader**

Use strict Pydantic models for the two persisted root values and a frozen dataclass for
`LoadedSourceImage`. Resolve the relative path under the package root, verify bytes with SHA-256,
verify Pillow format and dimensions, and put the unchanged bytes into the object store.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python -m pytest tests/unit/image/test_source_loader.py tests/unit/image/test_models.py -q`

Expected: all focused tests pass.

### Task 2: Detector protocol, deterministic crops, and MolYOLO adapter

**Files:**
- Create: `src/tsdaf/lanes/image/detector.py`
- Create: `src/tsdaf/lanes/image/cropper.py`
- Create: `src/tsdaf/adapters/moldet/__init__.py`
- Create: `src/tsdaf/adapters/moldet/molyolo_v10.py`
- Test: `tests/unit/image/test_detector.py`
- Test: `tests/unit/image/test_cropper.py`
- Test: `tests/unit/adapters/test_molyolo_v10.py`

**Interfaces:**
- Produces: `MoleculeDetector` protocol, `RawMoleculeDetection`, `prepare_detections(...)`, `materialize_crop_set(...)`, and `MolYoloV10Adapter`.
- Consumes: `LoadedSourceImage`, image-private models, and an extracted handoff bundle root.

- [ ] **Step 1: Write failing normalization and crop tests**

```python
prepared = prepare_detections(route_image_item_id="RIMG001", width=100, height=80, raw=raw)
assert prepared[0].molecule_id == "RIMG001:M001"
assert prepared[0].bbox_xyxy_px == (10, 20, 31, 41)
```

Cover overlap suppression, row-aware ordering, exclusive integer boxes, zero-area rejection, white
edge padding, stable crop dimensions, and crop artifact identity.

- [ ] **Step 2: Run the image tests and verify RED**

Run: `python -m pytest tests/unit/image/test_detector.py tests/unit/image/test_cropper.py -q`

Expected: imports fail because the detector and cropper do not exist.

- [ ] **Step 3: Implement the protocol and deterministic crop path**

Keep Pillow and object-store writes in `cropper.py`. Keep model inference out of the lane. Produce one
canonical `MoleculeCropSet` with RGB PNG artifacts and a small review overlay.

- [ ] **Step 4: Write and run failing adapter tests**

Use an injected fake model factory so tests exercise adapter conversion without Torch. Verify bundle
validation, lazy one-time model loading, fixed prediction arguments, and raw result conversion.

Run: `python -m pytest tests/unit/adapters/test_molyolo_v10.py -q`

Expected: import fails because the concrete adapter does not exist.

- [ ] **Step 5: Implement the minimal MolYOLO adapter and verify GREEN**

The adapter must require `weights/MolYOLO.pt` and the vendored runtime entry point, hash the bundle,
load `YOLOv10` lazily, reuse one model instance, and return only provider-neutral raw detections.

Run: `python -m pytest tests/unit/image/test_detector.py tests/unit/image/test_cropper.py tests/unit/adapters/test_molyolo_v10.py -q`

Expected: all focused tests pass without importing real Torch.

### Task 3: One-crop OCSR and the fixed Gemini OCSR profile

**Files:**
- Create: `src/tsdaf/lanes/image/ocsr.py`
- Create: `prompts/molecule_ocsr/v1/manifest.toml`
- Create: `prompts/molecule_ocsr/v1/system.md`
- Create: `prompts/molecule_ocsr/v1/task.md`
- Modify: `src/tsdaf/adapters/gemini/interactions.py`
- Modify: `src/tsdaf/adapters/gemini/__init__.py`
- Test: `tests/unit/image/test_ocsr.py`
- Test: `tests/unit/adapters/test_gemini_interactions.py`

**Interfaces:**
- Produces: `MoleculeOCSR`, `MoleculeOCSRPrompt`, `build_request(molecule, crop_bytes)`, `parse_result(...)`, and `GeminiInteractions.for_molecule_ocsr(...)`.
- Preserves: existing `GeminiInteractions.from_api_key(...)` request bytes and retry behavior.

- [ ] **Step 1: Write failing one-crop request and parser tests**

```python
request = ocsr.build_request(molecule=molecule, crop_bytes=png_bytes)
assert request.image_count == 1
assert request.molecule_id == "RIMG001:M001"
assert ocsr.parse_result('{"readable":true,"smiles":"CCO","is_abstract":false}', molecule).smiles == "CCO"
```

Cover `NO_SMILES`, fenced JSON recovery, wrong JSON shapes, and exact crop/M-ID association.

- [ ] **Step 2: Run the OCSR tests and verify RED**

Run: `python -m pytest tests/unit/image/test_ocsr.py -q`

Expected: import fails because `ocsr.py` does not exist.

- [ ] **Step 3: Implement prompt loading, request construction, and parsing**

Load and hash `manifest.toml`, `system.md`, and `task.md`. Build exactly one image input per call and
return one `GeminiOCSRResult`. Do not import OpenAI or inspect environment variables in the lane.

- [ ] **Step 4: Write failing adapter-profile tests**

Assert the existing main profile still emits `reasoning_effort="high"` and four total attempts. Add
behavior tests proving the OCSR profile emits `temperature=0`, omits reasoning effort, uses a client
timeout of 120 seconds, and stops after three total attempts by default.

Run: `python -m pytest tests/unit/adapters/test_gemini_interactions.py -q`

Expected: new OCSR constructor/profile assertions fail while existing tests remain green.

- [ ] **Step 5: Refactor the adapter minimally and verify GREEN**

Make request generation and retry count instance-profile driven. Keep the existing constructor and
recorded fixture behavior unchanged. Add only the pinned OCSR constructor needed by image composition.

Run: `python -m pytest tests/unit/image/test_ocsr.py tests/unit/adapters/test_gemini_interactions.py -q`

Expected: all focused tests pass.

### Task 4: Image processing runtime and application composition

**Files:**
- Create: `src/tsdaf/workflow/image_processing_runtime.py`
- Create: `src/tsdaf/apps/image_composition.py`
- Test: `tests/unit/workflow/test_image_processing_runtime.py`
- Test: `tests/integration/image/test_image_processing_workflow.py`

**Interfaces:**
- Produces: `run_image_processing(...) -> tuple[StageResult, tuple[StageResult, ...]]` and `execute_image_workflow(...) -> tuple[int, dict[str, object]]`.
- Consumes: verified job/source, injected detector, injected Gemini interaction callable, prompt directory, `LocalObjectStore`, and `LocalRunner`.

- [ ] **Step 1: Write a failing fake-detector/recorded-provider integration test**

```python
code, payload = execute_image_workflow(
    job_path=job_path,
    source_package_dir=package_dir,
    workspace=workspace,
    detector=fake_detector,
    interaction_create=fake_interaction,
    provider_is_live=False,
    live=False,
    workers=4,
    retries=2,
    timeout=120.0,
)
assert code == 0
assert payload["molecule_count"] == 2
assert payload["ocsr_result_count"] == 2
```

Verify two provider calls each contain one crop, M-ID results are not crossed, request/response
artifacts are recorded, and rerunning uses cached stage outputs.

- [ ] **Step 2: Run the integration test and verify RED**

Run: `python -m pytest tests/unit/workflow/test_image_processing_runtime.py tests/integration/image/test_image_processing_workflow.py -q`

Expected: imports fail because the runtime and composition do not exist.

- [ ] **Step 3: Implement the two stage wrappers**

Detection/cropping is one local stage. Submit each crop as its own live OCSR stage through a
`ThreadPoolExecutor(max_workers=workers)`. Record one interaction event per molecule. Finish the
execution only when all results succeed or are cached.

- [ ] **Step 4: Implement composition and verify GREEN**

Composition validates CLI-ready settings, locates the prompt, builds the object store/runner and
concrete adapters, and converts failures to safe JSON payloads. It receives an injectable environment
mapping and never persists credential values.

Run: `python -m pytest tests/unit/workflow/test_image_processing_runtime.py tests/integration/image/test_image_processing_workflow.py -q`

Expected: all focused tests pass.

### Task 5: CLI wiring, docs, and proportional verification

**Files:**
- Create: `src/tsdaf/apps/commands/image.py`
- Modify: `src/tsdaf/apps/cli.py`
- Modify: `docs/guides/CLI.md`
- Test: `tests/integration/test_cli.py`

**Interfaces:**
- Produces: `tsdaf image run` with fixed model/temperature and default workers/retries/timeout.
- Consumes: `execute_image_workflow` from image composition.

- [ ] **Step 1: Write failing CLI tests**

Test parser behavior for required job, source package, and MolYOLO bundle paths; defaults of `4`, `2`,
and `120`; explicit `--live`; optional workspace/device/execution ID; and rejection of invalid numeric
values. Assert there is no model or temperature option.

- [ ] **Step 2: Run the CLI test and verify RED**

Run: `python -m pytest tests/integration/test_cli.py -q`

Expected: `image` is not a registered command.

- [ ] **Step 3: Add the thin command and documentation**

The command passes parsed values to image composition and emits canonical JSON. Document the extracted
bundle layout and process-environment credential requirement without documenting any secret value.

- [ ] **Step 4: Run focused and full verification**

Run:

```text
python -m pytest tests/unit/image tests/unit/adapters/test_molyolo_v10.py tests/unit/adapters/test_gemini_interactions.py tests/unit/workflow/test_image_processing_runtime.py tests/integration/image tests/integration/test_cli.py -q
python -m ruff check src tests
python -m mypy src/tsdaf
python -m pytest -q
```

Expected: every command exits zero. No live model, real weight, GPU, or chemical-accuracy run is part of
this implementation verification.
