# System Overview

## Distribution and dependency direction

TS-DAF is one `tsdaf` distribution with multiple internal lanes. Contracts and immutable data types sit inward; applications and provider adapters depend on them. Inner layers do not import CLI, environment configuration, Gemini, Prefect, SQLite implementations, or the legacy harness.

## Input boundary

Slice 1 accepts only normalized TS-DAF paper packages. PDF parsing and raw MinerU ZIP parsing remain outside this slice.

## Router and visual triage

The main Router is text-first and receives a complete visual inventory without image bytes. Only items marked `NEEDS_VISUAL_VERIFICATION` enter a separate, bounded visual-triage lane.

## Artifact and index truth

Artifacts are content-addressed, atomically written, sealed, and immutable. SQLite is a derived, rebuildable index; immutable manifests and artifacts remain the recoverable source of truth.

## Execution model

Stages are plain Python units coordinated first by a local Runner. Prefect is a future adapter and cannot define domain behavior.

## Handoff and release gate

The host materializes exact canonical text and complete source image assets into an immutable `PROPOSED` handoff. No image job may be created until a release receipt binds and authorizes that handoff.
