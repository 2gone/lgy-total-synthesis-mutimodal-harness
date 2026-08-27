# Image Processing v1 Design

## Goal

Connect the released `ImageProcessingJob` boundary to the supplied MolYOLO / YOLOv10 runtime,
produce auditable molecule crops, and run one fixed Gemini OCSR request per crop.

This is deliberately a small integration slice. It does not change the document lane, the frozen
global contract bundle, or the existing article-router workflow.

## Scope

The production path is:

1. Load one authorized `ImageProcessingJob`.
2. Resolve and verify its exact source image from an image package.
3. Run MolYOLO / YOLOv10 once for that source image.
4. Normalize boxes, assign stable local M-IDs, and materialize padded PNG crops.
5. Run one Gemini request for each crop, with at most four requests in flight.
6. Store immutable stage outputs and provider evidence with the existing local runtime.

## Non-goals

- No PDF or MinerU package parsing.
- No standalone OCR engine.
- No full-Scheme semantic binding.
- No compound-number or arrow-condition extraction.
- No molecule binding or final multimodal Image Package.
- No OCSR accuracy gate, candidate voting, or automatic chemical adjudication.
- No modification of `tsdaf.contracts` or its frozen schema bundle.
- No committed third-party weights or vendored Ultralytics runtime.

## Dependency Direction

The dependency direction remains:

```text
CLI -> image composition -> image processing runtime -> image lane protocols
                                                    -> concrete adapters
```

`tsdaf.lanes.image` defines the private models and the detector protocol. The concrete MolYOLO
adapter depends on that protocol. The lane never imports the concrete adapter, OpenAI, Torch, or
Ultralytics.

## File Layout

```text
src/tsdaf/
├── adapters/
│   └── moldet/
│       ├── __init__.py
│       └── molyolo_v10.py
├── lanes/
│   └── image/
│       ├── __init__.py
│       ├── job_factory.py
│       ├── models.py
│       ├── source_loader.py
│       ├── detector.py
│       ├── cropper.py
│       └── ocsr.py
├── workflow/
│   └── image_processing_runtime.py
└── apps/
    ├── image_composition.py
    └── commands/
        └── image.py

prompts/molecule_ocsr/v1/
├── manifest.toml
├── system.md
└── task.md
```

## Source Boundary

`source_loader.py` is the only boundary allowed to turn
`ImageProcessingJob.route_image_item.source_asset.relative_path` into a local file.

It must:

- reject absolute and escaping paths;
- reject non-files and symlinks;
- verify SHA-256, byte size, and declared image media type;
- verify dimensions when the handoff declares them;
- put the exact original bytes into `LocalObjectStore`;
- return a frozen in-memory `LoadedSourceImage` value.

`LoadedSourceImage` is not a new public or persisted contract. The job JSON and source image become
explicit stage input artifacts.

## Private Image Models

Only two versioned root result models are added, both private to `tsdaf.lanes.image`.

### MoleculeCropSet

One result per source image. It records:

- job ID and route image item ID;
- source image artifact and dimensions;
- detector identity, weights hash, runtime hash, versions, device, and fixed inference settings;
- crop policy;
- raw and retained detection counts;
- ordered molecule detections;
- an optional review overlay;
- operational quality flags such as `ZERO_DETECTIONS`.

Each molecule records:

- `molecule_id`, for example `RIMG001:M001`;
- raw YOLO float `xyxy`;
- authoritative integer pixel `xyxy`;
- detector score and class;
- requested and sampled crop boxes;
- crop dimensions and crop artifact.

### GeminiOCSRResult

One result per crop and therefore one result per provider request. It records:

- job ID, route image item ID, and molecule ID;
- the exact crop artifact;
- `TRANSCRIBED` or `NO_SMILES`;
- the initial SMILES string;
- whether Gemini marked the drawing abstract.

It does not contain model confidence, reasoning, free-form notes, RDKit canonicalization, or a
chemical correctness claim. Later human annotation is authoritative.

## Coordinate and Identity Rules

The authoritative box format is source-image pixels with top-left origin:

```text
[x0, y0, x1, y1)
```

The right and bottom edges are exclusive. Float detector boxes are converted with `floor` for the
start and `ceil` for the end, then clamped to the source dimensions. Zero-area boxes are rejected.

Overlaps are suppressed before deterministic row-aware reading-order sorting. M-IDs use the fixed
route image item ID plus a three-digit local index. M-ID is not a permanent chemical entity ID.
Future annotations must also bind the crop artifact SHA-256.

## MolYOLO Runtime Bundle

The supplied handoff archive is used as an extracted external runtime bundle. The configured root
must contain:

```text
weights/MolYOLO.pt
.vendor/molyolo_runtime/ultralytics/__init__.py
```

The adapter verifies these paths and records deterministic hashes. It lazily imports the vendored
YOLOv10 runtime so document-only commands and CI do not require Torch or Ultralytics. The model is
loaded once and reused for all jobs in one application invocation.

Fixed v1 detection and crop defaults are:

```text
image size: 1024
confidence threshold: 0.25
IoU threshold: 0.5
horizontal padding: 15%
vertical padding: 20%
minimum padding: 16 pixels
crop background: white
crop format: RGB PNG
```

## Gemini OCSR Request Profile

The request behavior matches the handoff code:

```text
one crop per request
default workers: 4
default retries: 2 (three total attempts)
default timeout: 120 seconds per request
model: gemini-3.7-flash-high
temperature: 0
```

The image lane constructs the prompt input and parses the JSON result. It never reads credentials,
creates a client, sends HTTP, sleeps, or classifies provider failures.

The existing Gemini adapter receives a separate pinned molecule-OCSR profile while preserving the
article-router profile byte-for-byte. The profile adds temperature zero, a 120-second client
timeout, and three total transport attempts. Runtime concurrency is a four-worker thread pool.

Each crop is its own `LocalRunner` stage and cache identity. A failed crop does not invalidate
successful crop calls. `NO_SMILES` is a successful semantic result, not a provider failure.

## Composition and CLI

`image_composition.py` owns all concrete wiring:

- `LocalObjectStore` and `LocalRunner`;
- MolYOLO bundle and concrete detector;
- molecule OCSR prompt path;
- Gemini OCSR adapter and live credentials;
- fixed/default worker, retry, and timeout settings.

The CLI only parses arguments and emits canonical JSON. It exposes one small command:

```text
tsdaf image run JOB_JSON SOURCE_PACKAGE_DIR --molyolo-bundle BUNDLE_DIR
  [--workspace DIR] [--device DEVICE] [--workers 4]
  [--retries 2] [--timeout 120] [--execution-id ID] [--live]
```

The model and temperature are not CLI options. Credentials come from the already-populated process
environment. Production code does not parse `C:\.env` or persist secrets.

## Runtime and Evidence

Detection/cropping is one local stage per image. OCSR is one live stage per crop. Stage content keys
bind source bytes, job bytes, weights/runtime hashes, detector/crop settings, crop bytes, prompt
hash, response schema hash, and fixed model profile as applicable.

Provider request and response bytes, request ID, token counts, latency, and transport attempts are
stored as runtime events. The execution is sealed only after every crop has a terminal OCSR result.

## Verification Scope

This implementation provides only the tests needed to prevent obvious integration breakage:

- source path and identity verification;
- coordinate normalization, ordering, padding, and M-ID assignment;
- lazy MolYOLO adapter conversion using a fake model boundary;
- one-crop OCSR request construction and result parsing;
- the fixed OCSR adapter profile;
- a small fake-detector/recorded-provider runtime path;
- CLI argument/default behavior.

Full nine-image, GPU, remote, chemical-accuracy, and performance validation are left to the assigned
test team.
