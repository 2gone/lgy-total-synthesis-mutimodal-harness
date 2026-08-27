# Agent Entry

## Project goal

Build an auditable TS-DAF pipeline for total-synthesis research while preserving source provenance, immutable evidence, and recovery.

## Required reading

1. Read [README.md](README.md), then this file.
2. Read [CODEBASE_GUIDE_ZH.md](docs/guides/CODEBASE_GUIDE_ZH.md), the applicable focused workflow guide, [CLI.md](docs/guides/CLI.md), [ROADMAP.md](docs/ROADMAP.md), and the current focused work item.
3. Use [docs/STATUS.md](docs/STATUS.md) only as generated status evidence; never edit it. Read the charter and architecture material only when the bounded task needs them.

## Architecture boundaries

The production call direction is `CLI -> commands -> application composition -> workflow runtime -> lane / adapter`. The article Main Router is text-first and has no image bytes; explicitly flagged assets alone enter isolated visual triage. The image lane receives an authorized job and original image package, and keeps crop/SMILES/detector details private.

## Current focus

The governed current focus is [WI-0001 Weak Article Router v1](docs/work/WI-0001-weak-article-router-v1.md).

## Operating policy

- Preserve all unrelated dirty changes byte-for-byte. Keep each bounded task to one implementer and one independent reviewer; Minor preferences are nonblocking.
- TS-DAF is a small Harness. Add an abstraction only for at least two current consumers. If two review/fix rounds expose the same root cause, stop patching and request a scope/design decision.
- Keep every rule in one source of truth. Host validation and provider-visible schema agree; do not silently rewrite an invalid model result.
- Treat raw PDFs/assets as immutable evidence, normalized outputs as separately hashed derivatives, and content-addressed artifacts/seals/events/manifests as truth. SQLite is rebuildable derived state.
- Keep the Main Router text-first: it receives no image bytes; only explicitly flagged assets enter isolated visual triage. `VisualRoutingHandoff`/`CoarseTextHandoff`, release receipt, and image job bind by content hash; no valid receipt means no image job.
- Keep crop/SMILES/detector details in image-lane private models. Do not extend frozen global document contracts for image internals.
- The Task 2 import checker is frozen tool debt only: never a production dependency, architecture model, or Python interpreter project.

## Prohibitions

Do not parse PDFs or raw MinerU ZIPs in the production workflow, import `tests/legacy_harness/` from production, mutate sealed artifacts, alter accepted ADR history in place, or introduce cross-layer imports to bypass the composition/runtime/lane/adapter boundary. The legacy harness is historical and runs only with its own configuration.

Default commands and CI must remain offline. Use live providers only with explicit authorization. Credentials enter only through process environment; never write, print, log, or place them in artifacts/docs. The CLI never reads `.env` directly.

## Tests

Run the smallest relevant offline verification once; avoid redundant full-suite runs. For documentation changes run `python -m tsdaf docs check` and `python scripts/generate_status.py --check`.

## Paid calls

Default commands and CI must remain offline. Use live providers only with explicit authorization. Credentials enter only through process environment; never write, print, log, or place them in artifacts/docs. The CLI never reads `.env` directly.

## Verification and Git

Inspect the diff and staged files for secrets, `.env` paths, weights, vendored runtime, and external live-workspace artifacts.

Do not commit or push unless the user explicitly authorizes it. When authorized, stage only files in scope and do not absorb unrelated dirty changes.

## Status source

[docs/STATUS.md](docs/STATUS.md) is generated project-status evidence and must never be edited by hand.
