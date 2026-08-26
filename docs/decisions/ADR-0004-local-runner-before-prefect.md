---
id: ADR-0004
title: Local runner before Prefect
status: accepted
date: 2026-08-24
supersedes: []
superseded_by: null
---

## Context

Domain stages need a simple execution contract before the project commits to distributed orchestration behavior.

## Decision

Use a local runner first; Prefect is a future adapter.

## Consequences

Stages remain plain Python and orchestration-neutral. A future Prefect integration must preserve the local Runner's contracts and artifact semantics.
