---
id: ADR-0005
title: Text-first Router, isolated visual triage
status: accepted
date: 2026-08-24
supersedes: []
superseded_by: null
---

## Context

Most routing decisions should be grounded in canonical text without paying the cost or accepting the ambiguity of unrestricted multimodal context.

## Decision

The main Router is text-first; only `NEEDS_VISUAL_VERIFICATION` enters isolated triage.

## Consequences

The main Router never receives images. Visual requests stay bounded, auditable, and limited to explicitly uncertain inventory items.
