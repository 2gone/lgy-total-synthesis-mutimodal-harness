from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SCRIPT = REPOSITORY_ROOT / "scripts" / "generate_status.py"
VALIDATE_SCRIPT = REPOSITORY_ROOT / "scripts" / "validate_docs.py"

ADRS = (
    (
        "ADR-0001-single-distribution-multiple-lanes.md",
        "ADR-0001",
        "Single distribution, multiple lanes",
    ),
    ("ADR-0002-standard-paper-package-boundary.md", "ADR-0002", "Standard paper package boundary"),
    (
        "ADR-0003-immutable-artifacts-sqlite-index.md",
        "ADR-0003",
        "Immutable artifacts, SQLite index",
    ),
    ("ADR-0004-local-runner-before-prefect.md", "ADR-0004", "Local runner before Prefect"),
    (
        "ADR-0005-text-first-router-isolated-visual-triage.md",
        "ADR-0005",
        "Text-first Router, isolated visual triage",
    ),
    (
        "ADR-0006-immutable-handoff-and-release-receipt.md",
        "ADR-0006",
        "Immutable handoff and release receipt",
    ),
    ("ADR-0007-pinned-provider-profile.md", "ADR-0007", "Pinned provider profile"),
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _work_item_text(
    work_id: str,
    title: str,
    *,
    focus: bool,
    owner: str = "codex",
    depends_on: tuple[str, ...] = (),
) -> str:
    dependencies = (
        "[]" if not depends_on else "\n" + "\n".join(f"  - {item}" for item in depends_on)
    )
    return (
        "---\n"
        f"id: {work_id}\n"
        f"title: {title}\n"
        "status: active\n"
        f"focus: {'true' if focus else 'false'}\n"
        f"owner: {owner}\n"
        f"depends_on: {dependencies}\n"
        "acceptance:\n"
        "  - behavior is verified\n"
        "---\n\n"
        f"# {title}\n\n"
        "A controlled work item fixture.\n\n"
        "## Work Item Record\n\n"
        "### Background\n"
        "The fixture needs documented background.\n\n"
        "### Scope\n"
        "The fixture covers offline document governance.\n\n"
        "### Non-goals\n"
        "It does not call external services.\n\n"
        "### Plan\n"
        "Exercise the command boundary.\n\n"
        "### Checkpoint\n"
        "The baseline is ready.\n\n"
        "### Blockers\n"
        "None.\n\n"
        "### Next action\n"
        "Run validation.\n\n"
        "### Acceptance\n"
        "The subprocess returns the expected machine code.\n\n"
        "### Completion receipt\n"
        "Pending.\n"
    )


def write_work_item(
    root: Path,
    work_id: str,
    slug: str,
    title: str,
    *,
    focus: bool,
    owner: str = "codex",
    depends_on: tuple[str, ...] = (),
) -> Path:
    path = root / "docs" / "work" / f"{work_id}-{slug}.md"
    _write(
        path,
        _work_item_text(work_id, title, focus=focus, owner=owner, depends_on=depends_on),
    )
    return path


def _adr_text(adr_id: str, title: str) -> str:
    return (
        "---\n"
        f"id: {adr_id}\n"
        f"title: {title}\n"
        "status: accepted\n"
        "date: 2026-08-24\n"
        "supersedes: []\n"
        "superseded_by: null\n"
        "---\n\n"
        "## Context\n\n"
        "The repository needs a stable architectural decision.\n\n"
        "## Decision\n\n"
        "Use the decision named by this record.\n\n"
        "## Consequences\n\n"
        "Future changes require a superseding ADR.\n"
    )


def make_repository(root: Path) -> None:
    _write(
        root / "AGENTS.md",
        """# Agent Entry

## Project goal
Build an auditable TS-DAF routing pipeline.

## Required reading
Read the [charter](docs/PROJECT_CHARTER.md "project charter") and [roadmap][roadmap].

## Architecture boundaries
Keep one distribution with inward dependency direction and standard-package inputs.

## Current focus
Follow [WI-0001](docs/work/WI-0001-weak-article-router-v1.md#work-item-record).

## Tests
Run offline pytest and document validation commands.

## Paid calls
Paid calls require explicit live mode and never run in CI.

## Prohibitions
Do not rewrite immutable artifacts or accepted decisions.

## Status source
[Project status](docs/STATUS.md) is the only status source. [This heading](#agent-entry) exists.

`[ignored inline](missing-inline.md)`

```text
[ignored fence](missing-fence.md)
```

[roadmap]: docs/ROADMAP.md#slice-1
""",
    )
    _write(
        root / "CHANGELOG.md",
        """# Changelog

## [Unreleased]

- Establish offline documentation governance for Slice 1.
""",
    )
    _write(
        root / "docs" / "PROJECT_CHARTER.md",
        """# Project Charter

## WHY
Preserve scientific provenance while routing synthesis evidence.

## Scope
Accept normalized TS-DAF paper packages and produce governed routing records.

## Scientific goal
Select complete forward total-synthesis route evidence reproducibly.

## Non-goals
This slice does not parse PDFs or perform image chemistry extraction.
""",
    )
    _write(
        root / "docs" / "ROADMAP.md",
        """# Roadmap

## Dependencies
Each slice depends on the contracts and receipts sealed by earlier slices.

## Slice 1
Build the weak-article Router foundation.

## Slice 2
Add review and Gold-200 workflows.

## Slice 3
Package strong route text.

## Slice 4
Add the supporting-information lane.

## Slice 5
Scale through adapters and controlled rollout.

## Follow-on direction
Promote only validated artifacts across slice boundaries.
""",
    )
    _write(
        root / "docs" / "architecture" / "SYSTEM_OVERVIEW.md",
        (
            "# System Overview\n\n"
            "One distribution contains layered lanes. Standard packages enter a text-first "
            "Router, while only flagged visuals enter isolated triage. Artifacts are immutable "
            "and SQLite is derived. Plain-Python stages use a local Runner. Image jobs require a "
            "release receipt.\n"
        ),
    )
    _write(root / "docs" / "research" / "THESIS_MAINLINE_0824.md", "# Thesis mainline evidence\n")
    _write(
        root / "docs" / "research" / "HARNESS_ENGINEERING_0824.md",
        "# Harness engineering evidence\n",
    )
    write_work_item(
        root,
        "WI-0001",
        "weak-article-router-v1",
        "Weak Article Router v1",
        focus=True,
    )
    for filename, adr_id, title in ADRS:
        _write(root / "docs" / "decisions" / filename, _adr_text(adr_id, title))


def run_generate(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GENERATE_SCRIPT), "--root", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def run_validate(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATE_SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def ready_repository(root: Path) -> None:
    make_repository(root)
    generated = run_generate(root)
    assert generated.returncode == 0, generated.stdout + generated.stderr


def _codes(result: subprocess.CompletedProcess[str]) -> set[str]:
    return {line.split(" ", 1)[0] for line in result.stdout.splitlines() if line}


def test_valid_repository_prints_only_docs_ok(tmp_path: Path) -> None:
    ready_repository(tmp_path)

    result = run_validate(tmp_path)

    assert result.returncode == 0
    assert result.stdout == "DOCS_OK\n"
    assert result.stderr == ""


def test_missing_supplemental_required_path_is_doc001(tmp_path: Path) -> None:
    ready_repository(tmp_path)
    (tmp_path / "CHANGELOG.md").unlink()

    result = run_validate(tmp_path)

    assert result.returncode == 1
    assert "DOC001_REQUIRED_PATH" in _codes(result)


def test_missing_required_document_content_is_doc001(tmp_path: Path) -> None:
    ready_repository(tmp_path)
    charter = tmp_path / "docs" / "PROJECT_CHARTER.md"
    charter.write_text(
        charter.read_text(encoding="utf-8").replace("## Scientific goal", "## Objective"),
        encoding="utf-8",
        newline="\n",
    )

    result = run_validate(tmp_path)

    assert result.returncode == 1
    assert "DOC001_REQUIRED_PATH" in _codes(result)


def test_empty_required_document_section_is_doc001(tmp_path: Path) -> None:
    ready_repository(tmp_path)
    charter = tmp_path / "docs" / "PROJECT_CHARTER.md"
    charter.write_text(
        charter.read_text(encoding="utf-8").replace(
            "## Scientific goal\n"
            "Select complete forward total-synthesis route evidence reproducibly.\n\n",
            "## Scientific goal\n\n",
        ),
        encoding="utf-8",
        newline="\n",
    )

    result = run_validate(tmp_path)

    assert result.returncode == 1
    assert "DOC001_REQUIRED_PATH" in _codes(result)


def test_agents_focus_must_link_to_wi0001_is_doc001(tmp_path: Path) -> None:
    ready_repository(tmp_path)
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        agents.read_text(encoding="utf-8").replace(
            "docs/work/WI-0001-weak-article-router-v1.md#work-item-record",
            "docs/PROJECT_CHARTER.md#scope",
        ),
        encoding="utf-8",
        newline="\n",
    )

    result = run_validate(tmp_path)

    assert result.returncode == 1
    assert "DOC001_REQUIRED_PATH" in _codes(result)


def test_required_research_file_with_crlf_is_doc001(tmp_path: Path) -> None:
    ready_repository(tmp_path)
    research = tmp_path / "docs" / "research" / "THESIS_MAINLINE_0824.md"
    research.write_bytes(research.read_bytes().replace(b"\n", b"\r\n"))

    result = run_validate(tmp_path)

    assert result.returncode == 1
    assert "DOC001_REQUIRED_PATH" in _codes(result)


def test_extra_managed_markdown_with_crlf_and_bad_link_fails_closed(tmp_path: Path) -> None:
    ready_repository(tmp_path)
    (tmp_path / "docs" / "extra.md").write_bytes(b"# Extra\r\n\r\n[broken](missing.md)\r\n")

    result = run_validate(tmp_path)

    assert result.returncode == 1
    assert any(
        line.startswith("DOC005_LOCAL_LINK docs/extra.md:") for line in result.stdout.splitlines()
    )


def test_restricted_work_item_metadata_violation_is_doc002(tmp_path: Path) -> None:
    ready_repository(tmp_path)
    work_item = tmp_path / "docs" / "work" / "WI-0001-weak-article-router-v1.md"
    work_item.write_text(
        work_item.read_text(encoding="utf-8").replace(
            "owner: codex\n", "owner: codex\nunknown: value\n"
        ),
        encoding="utf-8",
        newline="\n",
    )

    result = run_validate(tmp_path)

    assert result.returncode == 1
    assert "DOC002_WORK_ITEM_SCHEMA" in _codes(result)


def test_yaml_comment_value_is_doc002(tmp_path: Path) -> None:
    ready_repository(tmp_path)
    work_item = tmp_path / "docs" / "work" / "WI-0001-weak-article-router-v1.md"
    work_item.write_text(
        work_item.read_text(encoding="utf-8").replace("owner: codex", "owner: # codex"),
        encoding="utf-8",
        newline="\n",
    )

    result = run_validate(tmp_path)

    assert result.returncode == 1
    assert "DOC002_WORK_ITEM_SCHEMA" in _codes(result)


def test_duplicate_work_item_id_is_doc003(tmp_path: Path) -> None:
    ready_repository(tmp_path)
    _write(
        tmp_path / "docs" / "work" / "WI-0001-duplicate.md",
        _work_item_text("WI-0001", "Duplicate", focus=False),
    )

    result = run_validate(tmp_path)

    assert result.returncode == 1
    assert "DOC003_DUPLICATE_WORK_ITEM_ID" in _codes(result)


def test_zero_focus_work_items_is_doc004(tmp_path: Path) -> None:
    ready_repository(tmp_path)
    work_item = tmp_path / "docs" / "work" / "WI-0001-weak-article-router-v1.md"
    work_item.write_text(
        work_item.read_text(encoding="utf-8").replace("focus: true", "focus: false"),
        encoding="utf-8",
        newline="\n",
    )

    result = run_validate(tmp_path)

    assert result.returncode == 1
    assert "DOC004_FOCUS_CARDINALITY" in _codes(result)


def test_multiple_focus_work_items_is_doc004(tmp_path: Path) -> None:
    ready_repository(tmp_path)
    write_work_item(tmp_path, "WI-0002", "second-focus", "Second focus", focus=True)

    result = run_validate(tmp_path)

    assert result.returncode == 1
    assert "DOC004_FOCUS_CARDINALITY" in _codes(result)


@pytest.mark.parametrize(
    ("old_link", "new_link"),
    (
        ("docs/PROJECT_CHARTER.md", "docs/DOES_NOT_EXIST.md"),
        ("docs/ROADMAP.md#slice-1", "docs/ROADMAP.md#missing-anchor"),
    ),
)
def test_broken_local_link_or_fragment_is_doc005(
    tmp_path: Path, old_link: str, new_link: str
) -> None:
    ready_repository(tmp_path)
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        agents.read_text(encoding="utf-8").replace(old_link, new_link),
        encoding="utf-8",
        newline="\n",
    )

    result = run_validate(tmp_path)

    assert result.returncode == 1
    assert "DOC005_LOCAL_LINK" in _codes(result)


def test_inline_link_with_parenthesized_title_suffix_is_valid(tmp_path: Path) -> None:
    ready_repository(tmp_path)
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        agents.read_text(encoding="utf-8").replace(
            '[charter](docs/PROJECT_CHARTER.md "project charter")',
            "[charter](docs/PROJECT_CHARTER.md (title))",
        ),
        encoding="utf-8",
        newline="\n",
    )

    result = run_validate(tmp_path)

    assert result.returncode == 0
    assert result.stdout == "DOCS_OK\n"
    assert result.stderr == ""


def test_invalid_adr_schema_is_doc006(tmp_path: Path) -> None:
    ready_repository(tmp_path)
    adr = tmp_path / "docs" / "decisions" / ADRS[0][0]
    adr.write_text(
        adr.read_text(encoding="utf-8").replace("date: 2026-08-24", "date: 2026-02-30"),
        encoding="utf-8",
        newline="\n",
    )

    result = run_validate(tmp_path)

    assert result.returncode == 1
    assert "DOC006_ADR_SCHEMA" in _codes(result)


def test_missing_adr_supersession_target_is_doc007(tmp_path: Path) -> None:
    ready_repository(tmp_path)
    adr = tmp_path / "docs" / "decisions" / ADRS[1][0]
    adr.write_text(
        adr.read_text(encoding="utf-8").replace("supersedes: []", "supersedes: [ADR-9999]"),
        encoding="utf-8",
        newline="\n",
    )

    result = run_validate(tmp_path)

    assert result.returncode == 1
    assert "DOC007_ADR_SUPERSESSION" in _codes(result)


@pytest.mark.parametrize(
    ("relative_path", "budget"),
    (
        ("AGENTS.md", 12 * 1024),
        ("CHANGELOG.md", 64 * 1024),
        ("docs/PROJECT_CHARTER.md", 32 * 1024),
        ("docs/ROADMAP.md", 32 * 1024),
        ("docs/STATUS.md", 32 * 1024),
        ("docs/architecture/SYSTEM_OVERVIEW.md", 64 * 1024),
        ("docs/research/THESIS_MAINLINE_0824.md", 128 * 1024),
        (f"docs/decisions/{ADRS[0][0]}", 32 * 1024),
        ("docs/work/WI-0001-weak-article-router-v1.md", 128 * 1024),
    ),
)
def test_path_budget_overflow_is_doc008(tmp_path: Path, relative_path: str, budget: int) -> None:
    ready_repository(tmp_path)
    target = tmp_path / relative_path
    target.write_bytes(target.read_bytes() + (b"x" * (budget + 1)) + b"\n")

    result = run_validate(tmp_path)

    assert result.returncode == 1
    assert "DOC008_SIZE_BUDGET" in _codes(result)


def test_work_item_change_without_regeneration_is_doc009(tmp_path: Path) -> None:
    ready_repository(tmp_path)
    work_item = tmp_path / "docs" / "work" / "WI-0001-weak-article-router-v1.md"
    work_item.write_text(
        work_item.read_text(encoding="utf-8").replace(
            "title: Weak Article Router v1", "title: Renamed Router"
        ),
        encoding="utf-8",
        newline="\n",
    )

    result = run_validate(tmp_path)

    assert result.returncode == 1
    assert "DOC009_STATUS_DRIFT" in _codes(result)
