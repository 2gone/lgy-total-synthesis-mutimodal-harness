# TS-DAF Mainline Promotion and Collaboration Design

## Goal

Make the production TS-DAF Harness the repository mainline without rewriting history. Preserve the
old OCR and route-image experiment Harness as an isolated test asset, promote the completed weak
article-routing and image-processing work in their existing order, and give collaborators a short,
accurate guide to the code and remaining validation work.

## Current repository facts

The repository history is linear:

```text
90b96d3  legacy multimodal experiment Harness on main
653f051  Task1-12 weak article Router and production TS-DAF foundation
348cf50  image processing v1: MolYOLO -> crop -> Gemini OCSR
```

Commit `653f051` already performed the repository-layout migration. The old experimental project is
complete and independently runnable beneath `tests/legacy_harness/`; production code, prompts,
contracts, governed documentation, and active tests are at the repository root. No second bulk move
is required.

`main` is an ancestor of the current image-processing branch. Promotion can therefore be a strict
fast-forward. A merge commit, squash, rebase, force-push, or history rewrite is unnecessary.

## Target mainline layout

- `src/tsdaf/`, `prompts/`, root `pyproject.toml`, and the governed root documentation are the
  production TS-DAF Harness.
- `tests/unit/`, `tests/integration/`, and `tests/live/` validate the production Harness.
- `tests/legacy_harness/` remains a complete, isolated historical OCR and route-image experiment
  project, including its own `pyproject.toml`, tools, data, prompts, and tests.
- Production modules must not import `tests/legacy_harness/`.
- `.env`, provider credentials, MolYOLO weights, and vendored MolYOLO runtime files remain outside
  Git. Only safe examples such as `.env.example` may be tracked.

## Production workflows on main

### Weak article-routing workflow (Task1-12)

The normalized paper-package path remains:

```text
package validation -> canonicalization -> weak article Router
-> optional visual triage -> immutable handoffs -> release-gated image job
```

The frozen contracts, immutable artifact store, local Runner, provider adapter, prompts, CLI, and
five-paper fixtures remain unchanged by mainline promotion.

### Image processing v1

The authorized image-job path remains:

```text
source verification -> MolYOLO/YOLOv10 -> normalized detections
-> deterministic molecule crops -> one-crop Gemini OCSR
```

It consumes an initial image package, not a PDF or MinerU package. It does not introduce a separate
OCR engine. It also does not yet perform molecule binding, Semantic Binder processing, or final
Image Package construction.

The fixed OCSR execution profile remains one crop per request, four concurrent workers, two retries,
a 120-second request timeout, `gemini-3.7-flash-high`, and `temperature=0`.

## Task 2 historical engineering debt

`scripts/check_import_boundaries.py` is accepted and frozen Task 2 engineering debt. It is a
repository guard, not a domain-architecture example or a general Python interpreter.

- Do not extend its Python-language coverage for hypothetical cases.
- Do not import it from production code or make it a runtime dependency.
- Prefer clear production imports that satisfy the accepted dependency rules.
- Reopen the checker only when real production code exposes an unavoidable false positive or a
  demonstrated false negative.
- The active Ruff boundary excludes `tests/legacy_harness/**`; the legacy project is checked with its
  own configuration.

This debt must be visible to collaborators so they do not spend project complexity on the checker or
mistake it for a reusable framework.

## API validation status

Offline behavior and recorded/fake-provider coverage are implemented. Paid live validation is not
complete and must not be reported as complete after promotion.

For the weak article Router:

- the selected second remote endpoint accepted Chat Completions text and forced function calls;
- the first paper run ended with `ROUTER_NO_SUBMIT`, after which the bounded turn limit was corrected;
- the next runs reached submission but ended with `ROUTER_SCHEMA_INVALID` because the remote
  compatibility layer returned obsolete or invented fields despite receiving the exact schema and
  `strict: true`;
- two later no-paper exact-schema probes ended with `PROVIDER_TRANSIENT` before normalized output;
- compatibility was therefore neither proved nor disproved, and Task 12 five-paper live gold remains
  paused and incomplete.

For image processing v1:

- unit and recorded/fake integration behavior are implemented;
- no real MolYOLO weights/GPU run has been accepted into the repository evidence;
- no remote Gemini OCSR canary has been accepted into the repository evidence;
- molecule binding and final Image Package testing are future work.

CI and default commands remain offline. A paid live test requires explicit authorization, an
external persistent workspace, and credentials injected into the process environment. Code and CLI
must never load `C:\.env` directly.

## Required test matrix

Promotion is gated by independent evidence for both production workflows and the preserved legacy
project.

1. **Active repository:** run the complete offline suite, Ruff, Ruff formatting, Mypy, import-boundary
   validation, documentation validation, and diff checks.
2. **Weak article Router:** run its unit and integration coverage, including the five normalized paper
   fixtures, recorded/fake provider behavior, handoff/release gates, CLI, cache, resume, and immutable
   evidence checks. Do not claim Task 12 live gold success.
3. **Image processing v1:** run source-loader, coordinate, crop, MolYOLO-adapter, one-crop OCSR,
   runtime-cache, CLI, and end-to-end recorded/fake integration tests. Real weights/GPU and remote
   OCSR remain explicit future live gates.
4. **Legacy Harness:** run its own test suite with `tests/legacy_harness` on `PYTHONPATH` and its own
   `pyproject.toml`, proving the move did not make the historical package unusable.
5. **Post-fast-forward:** rerun the active offline suite and governance checks from the actual `main`
   worktree before pushing `main`.

## Collaborator documentation

Add one concise Chinese guide at `docs/guides/CODEBASE_GUIDE_ZH.md`. It must explain:

- the production-versus-legacy directory boundary;
- the `CLI -> command -> application composition -> workflow runtime -> lane/adapter` call path;
- how the weak article-routing output authorizes and feeds image processing;
- stable contracts and private image-lane models;
- key entry files for both workflows;
- external MolYOLO bundle and Gemini configuration without credentials;
- the Task 2 checker debt and stop conditions;
- the exact current API/live-validation status;
- offline, focused, legacy, and future live test commands;
- the extension point for Semantic Binder, molecule binding, and final Image Package work.

Update `README.md` to make production TS-DAF the obvious repository identity and link the guide.
Update `docs/architecture/SYSTEM_OVERVIEW.md`, `docs/PROJECT_CHARTER.md`, and `docs/ROADMAP.md` only
where their old Slice 1 wording directly contradicts the implemented image-processing capability.
Update `CHANGELOG.md` with the two production workflows and legacy relocation. Do not edit generated
`docs/STATUS.md` by hand.

## Promotion procedure

1. Add and validate the collaborator documentation on `codex/image-processing-v1`.
2. Commit the documentation as a separate, reviewable commit.
3. Confirm the feature worktree and the existing `main` worktree are clean and `main` has not moved.
4. Run the required pre-promotion test matrix.
5. In the existing main worktree, run `git merge --ff-only codex/image-processing-v1`.
6. Run the required post-fast-forward checks from `main`.
7. Push `main` normally to `origin`; never force-push.
8. Verify `origin/main` equals local `main` and contains no `.env`, weights, or vendored runtime.

If `main` has moved, either worktree is dirty, the fast-forward is impossible, or any required test
fails, stop without pushing and report the exact blocker.

## Acceptance criteria

- `origin/main` ends at the documented collaboration commit and contains `653f051` and `348cf50` as
  preserved ancestors.
- The root repository presents production TS-DAF rather than the old experiment Harness.
- The old experiment Harness remains independently runnable at `tests/legacy_harness/`.
- Both production workflows have explicit, passing offline evidence and clearly separated future
  live gates.
- Task 2 debt and the incomplete API/live status are prominent and not misrepresented as closed.
- Collaborators can identify the code entry points, run the correct tests, and find the future
  binding extension point without reconstructing decisions from chat history.
- No `.env`, credential, MolYOLO weight, or vendored runtime file is committed or pushed.
