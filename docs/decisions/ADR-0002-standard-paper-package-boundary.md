---
id: ADR-0002
title: Standard paper package boundary
status: accepted
date: 2026-08-24
supersedes: []
superseded_by: null
---

## Context

Routing behavior cannot be stable while it also owns heterogeneous document extraction and archive repair.

## Decision

Slice 1 accepts only normalized TS-DAF packages and does not parse PDFs or raw MinerU ZIP archives.

## Consequences

Upstream tooling must normalize inputs before routing. Package validation can remain deterministic and independent of extraction providers.
