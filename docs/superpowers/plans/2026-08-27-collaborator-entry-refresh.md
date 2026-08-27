# TS-DAF Collaborator Entry Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the repository principles and both production workflows directly understandable to human collaborators and their agents, then integrate the verified tree into `main`.

**Architecture:** Keep one concise root entry, one agent policy entry, two focused workflow guides, and the existing code guide as the detailed code map. Update only current-status documentation; preserve historical plans/specs and the isolated legacy harness.

**Tech Stack:** Markdown, Python 3.11–3.13, pytest, Ruff, Mypy, Git, PowerShell.

## Global Constraints

- Do not change production behavior while refreshing documentation.
- Preserve every existing uncommitted implementation change and its provenance.
- Do not edit historical legacy README files or accepted ADR history.
- Do not expose or stage `.env`, API keys, `MolYOLO.pt`, `.vendor/`, live workspaces, or provider payloads.
- Keep CI and all default verification offline.
- Do not force-push, rebase, squash, or create an avoidable merge commit.

---

### Task 1: Refresh the collaborator entry and current facts

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/guides/CODEBASE_GUIDE_ZH.md`
- Modify: `docs/guides/CLI.md`
- Modify: `docs/guides/LIVE_GOLD.md`
- Modify: `docs/ROADMAP.md`
- Modify: `CHANGELOG.md`
- Create: `docs/guides/ARTICLE_ROUTING_WORKFLOW_ZH.md`
- Create: `docs/guides/IMAGE_PROCESSING_WORKFLOW_ZH.md`

**Interfaces:**
- Consumes: current Task 12 plan/ledger evidence and implemented article/image code.
- Produces: an accurate human/agent entry path with no duplicated source of truth.

- [x] **Step 1: Record stale statements and exact code boundaries**

Run `rg` over current guides for paused Task 12, HTTPS-only routing, image live claims, contract
boundaries, and workflow entry points. Compare each statement with production code and sealed evidence.

- [x] **Step 2: Write the root principles and focused workflow guides**

State the ten principles from the approved design in the root README. Each workflow guide must list
its exact input, output, stage sequence, key files, invariants, non-goals, current evidence and focused
offline verification command.

- [x] **Step 3: Align agent, CLI, live-gold, roadmap and changelog facts**

Make AGENTS point to both guides and forbid scope drift. Record the five-paper live PASS and route-only
loopback HTTP allowance without claiming image OCSR live validation.

- [x] **Step 4: Run documentation checks**

Run `python -m tsdaf docs check`, `python scripts/generate_status.py --check`, link/stale-term scans,
and `git diff --check`. Every command must exit zero or produce only explicitly historical matches.

### Task 2: Independent workflow documentation review

**Files:**
- Review only: all files from Task 1 and their linked production entry points.

**Interfaces:**
- Consumes: Task 1 documentation diff.
- Produces: one TS-DAF article-workflow review and one image-workflow review with Critical/Important findings.

- [x] **Step 1: Review article routing independently**

Verify normalized-package input, text-first routing, visual-triage isolation, handoff/receipt binding,
five-paper live status, URL policy, tests and non-goals against actual code/evidence.

- [x] **Step 2: Review image processing independently**

Verify authorized-job input, source/hash validation, MolYOLO/crop/OCSR flow, lane-private models, fixed
profile, current offline-only evidence and future binding boundary against actual code.

- [x] **Step 3: Resolve only real documentation defects**

Fix Critical/Important contradictions once. Record stylistic or future-scope suggestions without
blocking mainline integration.

### Task 3: Verify, commit and integrate

**Files:**
- Verify: active repository and `tests/legacy_harness/**`
- Update Git refs: feature branch, local `main`, and `origin/main`

**Interfaces:**
- Consumes: reviewed documentation and existing Task 12 implementation.
- Produces: a verified, normally pushed `main` with exact remote SHA confirmation.

- [x] **Step 1: Run the complete offline gates**

Run active pytest, Ruff check/format, Mypy, import-boundary, docs/status checks, legacy pytest,
`git diff --check`, and a sensitive-file/path scan. Stop on any failure.

- [ ] **Step 2: Commit the complete authorized change set**

Review staged names and staged diff, commit without secrets or external live artifacts, and confirm
the feature branch is clean.

- [ ] **Step 3: Fast-forward and verify main**

Fetch normally, confirm ancestry, fast-forward local `main`, and rerun the complete offline gates in
the main worktree. Do not clean up host-owned worktrees.

- [ ] **Step 4: Push and verify the remote**

Push `main` normally, then require local `main`, `origin/main`, and `git ls-remote` to report the same
commit. Report the commit and retained external receipt location.
