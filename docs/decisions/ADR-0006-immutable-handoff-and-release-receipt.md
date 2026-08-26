---
id: ADR-0006
title: Immutable handoff and release receipt
status: accepted
date: 2026-08-24
supersedes: []
superseded_by: null
---

## Context

Downstream image processing must not consume unreviewed or silently changing routing selections.

## Decision

The handoff is immutable and `PROPOSED`; without a release receipt, no image job is created.

## Consequences

Review and release remain explicit transitions. Receipts bind the exact handoff hash that downstream work is allowed to consume.
