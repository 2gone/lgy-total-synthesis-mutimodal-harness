# Agent Entry

## Project goal

Build an auditable TS-DAF pipeline that routes evidence for total-synthesis research while preserving exact source provenance.

## Required reading

Read the [Chinese collaborator code guide](docs/guides/CODEBASE_GUIDE_ZH.md), [project charter](docs/PROJECT_CHARTER.md), [system overview](docs/architecture/SYSTEM_OVERVIEW.md), [roadmap](docs/ROADMAP.md), current [status](docs/STATUS.md), and then the focused work item.

## Architecture boundaries

One `tsdaf` distribution contains inward-facing lanes. Slice 1 accepts normalized TS-DAF packages, keeps the main Router text-first, isolates visual triage, and treats immutable artifacts as truth while SQLite remains derived.

## Current focus

The governed current focus remains
[WI-0001 Weak Article Router v1](docs/work/WI-0001-weak-article-router-v1.md).

## Implemented workflows

The repository contains the Task1–12 weak article-routing workflow and image processing v1 from an
authorized image job through MolYOLO, deterministic crop, and one-crop Gemini OCSR. Semantic binding
and the final Image Package are not implemented. Governance status remains sourced from the focused
work item and the generated status.

## Tests

Run `python -m pytest` for the offline suite and `python scripts/validate_docs.py` for documentation governance. Generate status with `python scripts/generate_status.py`, then verify it with `python scripts/generate_status.py --check`.

## Paid calls

Default execution is offline. A paid provider call requires an explicit future `--live` action; CI must never make paid calls.

## Prohibitions

Do not parse PDFs or raw MinerU ZIPs in Slice 1, send images to the main Router, mutate sealed artifacts, edit accepted ADR history in place, expose credentials, or import the legacy harness from production code.

## Status source

[docs/STATUS.md](docs/STATUS.md) is the only project-status source. It is generated and must not be edited by hand.
