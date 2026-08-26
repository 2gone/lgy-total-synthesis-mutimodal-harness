---
id: ADR-0003
title: Immutable artifacts, SQLite index
status: accepted
date: 2026-08-24
supersedes: []
superseded_by: null
---

## Context

Scientific provenance and resumable execution require durable facts that do not depend on a mutable database.

## Decision

Artifacts are content-addressed, atomically written, and immutable; SQLite is a rebuildable index.

## Consequences

Sealed artifacts cannot be overwritten in place, and deleting the SQLite index must not destroy the evidence needed to reconstruct it.
