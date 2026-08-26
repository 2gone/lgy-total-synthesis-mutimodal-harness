---
id: ADR-0001
title: Single distribution, multiple lanes
status: accepted
date: 2026-08-24
supersedes: []
superseded_by: null
---

## Context

The system needs clear internal boundaries without fragmenting installation, release, and contract ownership across packages.

## Decision

Use one `tsdaf` distribution, layered by internal lane.

## Consequences

Lane dependencies must point inward toward shared contracts. A later packaging change requires a higher-numbered superseding ADR.
