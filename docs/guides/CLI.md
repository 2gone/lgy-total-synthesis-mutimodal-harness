# TS-DAF Slice 1 CLI

The command line has six fixed commands:

```text
tsdaf package validate <package-dir>
tsdaf route run <package-dir> [--live]
tsdaf route resume <execution-id> [--live]
tsdaf image run <job-json> <image-package-dir> --molyolo-bundle <bundle-dir> [--live]
tsdaf run inspect <execution-id>
tsdaf docs check
```

Commands other than `docs check` emit one compact, key-sorted JSON document and a newline.
Successful and control-flow results are written to standard output. Configuration, input, and
evidence failures are written to standard error. Invalid or incomplete command syntax is handled
by argparse.

## Exit codes

- `0`: success, including an already-terminal resume.
- `1`: invalid input, provider or routing failure, or unverifiable local evidence.
- `2`: invalid command syntax or live/check-out configuration.
- `3`: uncached work requires an explicitly enabled live invocation (`LIVE_REQUIRED`).

`docs check` retains the documentation validator's existing text output and exit-code behavior.

## Checkout and workspace

Routing requires the current source checkout and its governed prompt catalog at
`prompts/article_router/v1`. The CLI discovers that checkout; it does not install or duplicate the
prompts. A missing checkout or prompt catalog is a safe exit `2` error.

Routing and inspection use `.tsdaf-workspace` under the current working directory. The workspace
contains content-addressed objects, sealed execution records and event logs, terminal manifests,
and a rebuildable SQLite projection. `run inspect` rebuilds that projection from verified immutable
evidence before reporting an execution.

## Package validation

`package validate` validates the complete manifest inventory and its declared hashes before
canonicalization. Success reports the source schema version, paper ID, block and visual counts,
total source-asset bytes, canonical package SHA-256, and `status="VALID"`. A failure reports an
existing domain code and a safe package-relative locator; it does not expose an absolute path or a
raw exception.

## Offline and live routing

`route run` is offline by default. Offline mode never reads provider environment variables and
never invokes a provider. It may reuse verified local content cache; otherwise it returns exit `3`
with `LIVE_REQUIRED` before creating a workspace, execution, or attempt.

Live work is enabled only by `--live`. It requires nonblank `OPENAI_API_KEY` and an absolute HTTPS
`OPENAI_BASE_URL` ending exactly in `/v1`, without userinfo, a query, or a fragment. The model is
fixed to `gemini-3.7-flash-high`; legacy `GEMINI_API_KEY` and `GEMINI_MODEL` do not select live
behavior. Missing or invalid live configuration returns exit `2` before workspace mutation or SDK
import. Set credentials only in a shell or credential manager, for example
`$env:OPENAI_API_KEY='...'`; no key is written to settings, events, artifacts, inspection output,
or error output.

A successful route validates and canonicalizes the package, runs or reuses the Main Article Router
with optional visual triage, and materializes both proposed handoffs. The handoffs are stored as
immutable content-addressed JSON objects and bound into exactly one `handoffs_materialized` event
before the execution is sealed successful. This command does not issue a release receipt, create an
image job, copy images, or produce named exports.

## Image processing

`image run` consumes an authorized image-job JSON file and the original image package. It verifies
the declared source image, runs the external MolYOLO/YOLOv10 bundle, normalizes detections, writes
one crop per molecule, and sends each crop in a separate Gemini OCSR request when `--live` is set.
It does not read a PDF or MinerU package, perform molecule binding, or build the final Image Package.

The extracted MolYOLO bundle must contain `weights/MolYOLO.pt` and
`.vendor/molyolo_runtime/ultralytics/__init__.py`; its runtime and weight hashes are recorded as
detector provenance. OCSR uses the fixed `gemini-3.7-flash-high` profile with `temperature=0`.
Defaults are four concurrent single-crop requests, two retries, and a 120-second request timeout.
The optional `--workers`, `--retries`, `--timeout`, `--device`, `--workspace`, and
`--execution-id` flags only control execution; there is no batch-size, model, or temperature flag.

Offline image processing can reuse verified local results but will return `LIVE_REQUIRED` for any
uncached OCSR crop. Live mode uses the same `OPENAI_API_KEY` and safe `OPENAI_BASE_URL` rules as
article routing. The CLI never reads a `.env` file directly.

## Resume

`route resume` first rebuilds and verifies local evidence. An already-terminal execution returns
success with `resume="ALREADY_TERMINAL"` and does not change execution, event, object, or manifest
evidence. A nonterminal execution can resume only from its most recent verified package directory,
canonical package hash, and fixed stage configuration. The command does not support changing the
package, prompt, model, provider profile, or workspace.

Offline resume may finish handoff materialization for a locally completed stage. If another
provider attempt would be required, it returns `LIVE_REQUIRED` without calling a provider, adding
an attempt, or changing execution evidence. Explicit-live resume uses the same OpenAI-compatible
key, base URL, and fixed-model rules as `route run`.

## Inspection and observability

`run inspect` verifies the execution through the rebuilt local index and reports deterministic,
JSON-ready execution status, stage outputs, attempts, event metadata, artifact references, handoff
references, and observability facts. Unknown or tampered executions return safe exit `1` output
without a traceback.

Observability reports only recorded facts: cache use; model profile, prompt hash, and schema hash
when an attempt exists; router turns; visual-triage batches; response-derived function-call count;
provider token totals; summed latency; and safe error types and codes. Request and response bytes
and API keys are never included. A missing provider field remains `null`, not zero.

Slice 1 has no frozen visual-token calculation or pricing snapshot. Inspection therefore reports
`visual_tokens` as `null` and reports cost exactly as:

```json
{"amount_usd":null,"currency":"USD","pricing_snapshot_version":"unconfigured"}
```
