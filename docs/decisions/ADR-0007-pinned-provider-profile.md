---
id: ADR-0007
title: Pinned provider profile
status: accepted
date: 2026-08-24
supersedes: []
superseded_by: null
---

## Context

Provider, prompt, and schema drift would make routing results irreproducible and cost behavior difficult to audit.

## Decision

Pin and record provider profile, prompt, and schema versions; make no paid calls by default.

## Consequences

Live execution must be explicit and record the selected versions and hashes. CI and default commands remain offline.
