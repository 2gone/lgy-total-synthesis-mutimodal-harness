from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import unquote

WORK_ID_RE = re.compile(r"^WI-[0-9]{4}$")
ADR_ID_RE = re.compile(r"^ADR-[0-9]{4}$")
WORK_FILENAME_RE = re.compile(r"^(WI-[0-9]{4})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
ADR_FILENAME_RE = re.compile(r"^(ADR-[0-9]{4})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

WORK_KEYS = {
    "id",
    "title",
    "status",
    "focus",
    "owner",
    "depends_on",
    "acceptance",
}
ADR_KEYS = {"id", "title", "status", "date", "supersedes", "superseded_by"}
WORK_STATUSES = {"planned", "active", "blocked", "complete"}
ADR_STATUSES = {"accepted", "superseded"}
WORK_RECORD_HEADINGS = (
    "Background",
    "Scope",
    "Non-goals",
    "Plan",
    "Checkpoint",
    "Blockers",
    "Next action",
    "Acceptance",
    "Completion receipt",
)

NAMED_ADRS = (
    "docs/decisions/ADR-0001-single-distribution-multiple-lanes.md",
    "docs/decisions/ADR-0002-standard-paper-package-boundary.md",
    "docs/decisions/ADR-0003-immutable-artifacts-sqlite-index.md",
    "docs/decisions/ADR-0004-local-runner-before-prefect.md",
    "docs/decisions/ADR-0005-text-first-router-isolated-visual-triage.md",
    "docs/decisions/ADR-0006-immutable-handoff-and-release-receipt.md",
    "docs/decisions/ADR-0007-pinned-provider-profile.md",
)
REQUIRED_PATHS = (
    "AGENTS.md",
    "CHANGELOG.md",
    "docs/PROJECT_CHARTER.md",
    "docs/ROADMAP.md",
    "docs/STATUS.md",
    "docs/architecture/SYSTEM_OVERVIEW.md",
    "docs/research/THESIS_MAINLINE_0824.md",
    "docs/research/HARNESS_ENGINEERING_0824.md",
    "docs/work/WI-0001-weak-article-router-v1.md",
    *NAMED_ADRS,
)


@dataclass(frozen=True, order=True)
class Issue:
    code: str
    path: str
    detail: str

    def format(self) -> str:
        return f"{self.code} {self.path}: {self.detail}"


@dataclass(frozen=True)
class WorkItem:
    path: Path
    id: str
    title: str
    status: str
    focus: bool
    owner: str
    depends_on: tuple[str, ...]
    acceptance: tuple[str, ...]


@dataclass(frozen=True)
class Adr:
    path: Path
    id: str
    title: str
    status: str
    date: str
    supersedes: tuple[str, ...]
    superseded_by: str | None


class DocumentFormatError(ValueError):
    pass


def relative_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def issue(code: str, root: Path, path: Path | str, detail: str) -> Issue:
    normalized = path if isinstance(path, str) else relative_path(root, path)
    return Issue(code, normalized, detail)


def read_managed_text(path: Path) -> tuple[bytes, str]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise DocumentFormatError("UTF-8 BOM is not allowed")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentFormatError("file is not valid UTF-8") from exc
    if "\r" in text:
        raise DocumentFormatError("only LF line endings are allowed")
    if not text.endswith("\n"):
        raise DocumentFormatError("file must end with LF")
    if text.endswith("\n\n"):
        raise DocumentFormatError("file must end with exactly one LF")
    return raw, text


def _parse_scalar(value: str) -> object:
    if not value:
        raise DocumentFormatError("empty scalar requires an indented list")
    if value.startswith("#") or re.search(r"\s#", value):
        raise DocumentFormatError("comment values are not supported")
    if value in {"|", ">"} or value.startswith(("&", "*", "!", "{", "'", '"')):
        raise DocumentFormatError("unsupported YAML construct")
    if "{" in value or "}" in value:
        raise DocumentFormatError("flow maps are not supported")
    if value == "[]":
        return []
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        values = [part.strip() for part in inner.split(",")]
        if any(not part or any(mark in part for mark in "[]{}#") for part in values):
            raise DocumentFormatError("invalid scalar list")
        return values
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "null":
        return None
    if value.startswith("[") or value.endswith("]"):
        raise DocumentFormatError("invalid scalar list")
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise DocumentFormatError("frontmatter must start at byte zero")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise DocumentFormatError("frontmatter closing delimiter is missing") from exc
    metadata_lines = lines[1:end]
    body = "\n".join(lines[end + 1 :]) + "\n"
    if body.startswith("---\n"):
        raise DocumentFormatError("only one frontmatter block is allowed")

    metadata: dict[str, object] = {}
    current_list: str | None = None
    for line_number, line in enumerate(metadata_lines, start=2):
        if line.startswith("  - "):
            if current_list is None:
                raise DocumentFormatError(f"line {line_number}: list item has no key")
            item = line[4:]
            parsed = _parse_scalar(item)
            if not isinstance(parsed, str):
                raise DocumentFormatError(f"line {line_number}: list item must be a scalar string")
            values = metadata[current_list]
            if not isinstance(values, list):
                raise DocumentFormatError(f"line {line_number}: invalid list state")
            values.append(parsed)
            continue
        if line.startswith((" ", "\t")):
            raise DocumentFormatError(f"line {line_number}: unsupported indentation")
        match = re.fullmatch(r"([^:]+):(.*)", line)
        if match is None:
            raise DocumentFormatError(f"line {line_number}: expected key: value")
        key = match.group(1)
        if KEY_RE.fullmatch(key) is None:
            raise DocumentFormatError(f"line {line_number}: invalid key")
        if key in metadata:
            raise DocumentFormatError(f"line {line_number}: duplicate key {key}")
        remainder = match.group(2)
        if remainder and not remainder.startswith(" "):
            raise DocumentFormatError(f"line {line_number}: one space is required after colon")
        value = remainder[1:] if remainder else ""
        if value == "":
            metadata[key] = []
            current_list = key
        else:
            metadata[key] = _parse_scalar(value)
            current_list = None
    return metadata, body


def _nonempty_string(value: object, *, forbid_pipe: bool = False) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and "\n" not in value
        and (not forbid_pipe or "|" not in value)
    )


def _string_list(value: object, *, nonempty: bool) -> bool:
    return (
        isinstance(value, list)
        and (bool(value) or not nonempty)
        and all(_nonempty_string(item) for item in value)
        and len(value) == len(set(value))
    )


def _record_errors(body: str) -> list[str]:
    lines = body.splitlines()
    record_positions = [index for index, line in enumerate(lines) if line == "## Work Item Record"]
    if len(record_positions) != 1:
        return ["body must contain exactly one ## Work Item Record section"]
    record_lines = lines[record_positions[0] + 1 :]
    headings = [
        (index, line[4:]) for index, line in enumerate(record_lines) if line.startswith("### ")
    ]
    if tuple(name for _, name in headings) != WORK_RECORD_HEADINGS:
        return ["Work Item Record headings must match the required order exactly"]
    errors: list[str] = []
    for position, (start, name) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(record_lines)
        content = [line.strip() for line in record_lines[start + 1 : end] if line.strip()]
        if not content:
            errors.append(f"Work Item Record section {name} is empty")
    return errors


def _adr_body_errors(body: str) -> list[str]:
    lines = body.splitlines()
    headings = [(index, line[3:]) for index, line in enumerate(lines) if line.startswith("## ")]
    expected = ("Context", "Decision", "Consequences")
    if tuple(name for _, name in headings) != expected:
        return ["ADR sections must be Context, Decision, and Consequences in order"]
    errors: list[str] = []
    for position, (start, name) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        content = [line.strip() for line in lines[start + 1 : end] if line.strip()]
        if not content:
            errors.append(f"ADR section {name} is empty")
    return errors


def load_and_validate_metadata(root: Path) -> tuple[list[WorkItem], list[Adr], list[Issue]]:
    root = root.resolve()
    issues: list[Issue] = []
    work_items: list[WorkItem] = []
    adrs: list[Adr] = []
    work_id_paths: dict[str, list[Path]] = defaultdict(list)
    adr_id_paths: dict[str, list[Path]] = defaultdict(list)
    focus_values: list[tuple[Path, bool]] = []

    work_dir = root / "docs" / "work"
    work_paths = sorted(work_dir.glob("*.md")) if work_dir.is_dir() else []
    for path in work_paths:
        path_issues: list[str] = []
        try:
            _, text = read_managed_text(path)
            metadata, body = parse_frontmatter(text)
        except DocumentFormatError as exc:
            issues.append(issue("DOC002_WORK_ITEM_SCHEMA", root, path, str(exc)))
            continue

        work_id = metadata.get("id")
        if isinstance(work_id, str) and WORK_ID_RE.fullmatch(work_id):
            work_id_paths[work_id].append(path)
        if isinstance(metadata.get("focus"), bool):
            focus_values.append((path, bool(metadata["focus"])))

        filename_match = WORK_FILENAME_RE.fullmatch(path.name)
        if filename_match is None:
            path_issues.append("filename must match WI-NNNN-lowercase-kebab-slug.md")
        if set(metadata) != WORK_KEYS:
            path_issues.append("frontmatter keys must match the fixed work-item schema")
        if not isinstance(work_id, str) or WORK_ID_RE.fullmatch(work_id) is None:
            path_issues.append("id must match WI-NNNN")
        elif filename_match is not None and filename_match.group(1) != work_id:
            path_issues.append("frontmatter id must match filename prefix")
        if work_id == "WI-0001" and path.name != "WI-0001-weak-article-router-v1.md":
            path_issues.append("WI-0001 has a fixed stable path")
        title = metadata.get("title")
        status = metadata.get("status")
        focus = metadata.get("focus")
        owner = metadata.get("owner")
        dependencies = metadata.get("depends_on")
        acceptance = metadata.get("acceptance")
        if not _nonempty_string(title, forbid_pipe=True):
            path_issues.append("title must be a nonempty single line without pipe")
        if not isinstance(status, str) or status not in WORK_STATUSES:
            path_issues.append("status must be planned, active, blocked, or complete")
        if not isinstance(focus, bool):
            path_issues.append("focus must be a YAML boolean")
        if not _nonempty_string(owner):
            path_issues.append("owner must be a nonempty single line")
        if not _string_list(dependencies, nonempty=False):
            path_issues.append("depends_on must be a duplicate-free WI ID list")
        elif any(WORK_ID_RE.fullmatch(item) is None for item in dependencies):
            path_issues.append("depends_on entries must match WI-NNNN")
        if not _string_list(acceptance, nonempty=True):
            path_issues.append("acceptance must be a nonempty duplicate-free scalar list")
        path_issues.extend(_record_errors(body))

        for detail in sorted(set(path_issues)):
            issues.append(issue("DOC002_WORK_ITEM_SCHEMA", root, path, detail))

        basic_types_valid = (
            isinstance(work_id, str)
            and WORK_ID_RE.fullmatch(work_id) is not None
            and _nonempty_string(title, forbid_pipe=True)
            and isinstance(status, str)
            and status in WORK_STATUSES
            and isinstance(focus, bool)
            and _nonempty_string(owner)
            and _string_list(dependencies, nonempty=False)
            and all(WORK_ID_RE.fullmatch(item) for item in dependencies)
            and _string_list(acceptance, nonempty=True)
        )
        if basic_types_valid:
            work_items.append(
                WorkItem(
                    path=path,
                    id=work_id,
                    title=title,
                    status=status,
                    focus=focus,
                    owner=owner,
                    depends_on=tuple(dependencies),
                    acceptance=tuple(acceptance),
                )
            )

    for work_id, paths in work_id_paths.items():
        if len(paths) > 1:
            for path in paths:
                issues.append(
                    issue(
                        "DOC003_DUPLICATE_WORK_ITEM_ID",
                        root,
                        path,
                        f"{work_id} is declared {len(paths)} times",
                    )
                )

    focus_count = sum(value for _, value in focus_values)
    if focus_count != 1:
        issues.append(
            Issue(
                "DOC004_FOCUS_CARDINALITY",
                "docs/work",
                f"expected exactly one focus: true, found {focus_count}",
            )
        )

    known_work_ids = set(work_id_paths)
    for item in work_items:
        for dependency in item.depends_on:
            if dependency == item.id:
                issues.append(
                    issue(
                        "DOC002_WORK_ITEM_SCHEMA", root, item.path, "depends_on cannot contain self"
                    )
                )
            elif dependency not in known_work_ids:
                issues.append(
                    issue(
                        "DOC002_WORK_ITEM_SCHEMA",
                        root,
                        item.path,
                        f"depends_on target {dependency} does not exist",
                    )
                )

    decisions_dir = root / "docs" / "decisions"
    adr_paths = sorted(decisions_dir.glob("*.md")) if decisions_dir.is_dir() else []
    for path in adr_paths:
        path_issues = []
        try:
            _, text = read_managed_text(path)
            metadata, body = parse_frontmatter(text)
        except DocumentFormatError as exc:
            issues.append(issue("DOC006_ADR_SCHEMA", root, path, str(exc)))
            continue

        adr_id = metadata.get("id")
        if isinstance(adr_id, str) and ADR_ID_RE.fullmatch(adr_id):
            adr_id_paths[adr_id].append(path)
        filename_match = ADR_FILENAME_RE.fullmatch(path.name)
        if filename_match is None:
            path_issues.append("filename must match ADR-NNNN-lowercase-kebab-slug.md")
        if set(metadata) != ADR_KEYS:
            path_issues.append("frontmatter keys must match the fixed ADR schema")
        if not isinstance(adr_id, str) or ADR_ID_RE.fullmatch(adr_id) is None:
            path_issues.append("id must match ADR-NNNN")
        elif filename_match is not None and filename_match.group(1) != adr_id:
            path_issues.append("frontmatter id must match filename prefix")
        title = metadata.get("title")
        status = metadata.get("status")
        decision_date = metadata.get("date")
        supersedes = metadata.get("supersedes")
        superseded_by = metadata.get("superseded_by")
        if not _nonempty_string(title, forbid_pipe=True):
            path_issues.append("title must be a nonempty single line without pipe")
        if not isinstance(status, str) or status not in ADR_STATUSES:
            path_issues.append("status must be accepted or superseded")
        if not isinstance(decision_date, str) or DATE_RE.fullmatch(decision_date) is None:
            path_issues.append("date must use YYYY-MM-DD")
        else:
            try:
                date.fromisoformat(decision_date)
            except ValueError:
                path_issues.append("date must be a real calendar date")
        if not _string_list(supersedes, nonempty=False):
            path_issues.append("supersedes must be a duplicate-free ADR ID list")
        elif any(ADR_ID_RE.fullmatch(item) is None for item in supersedes):
            path_issues.append("supersedes entries must match ADR-NNNN")
        if not (
            superseded_by is None
            or (isinstance(superseded_by, str) and ADR_ID_RE.fullmatch(superseded_by))
        ):
            path_issues.append("superseded_by must be null or ADR-NNNN")
        path_issues.extend(_adr_body_errors(body))

        for detail in sorted(set(path_issues)):
            issues.append(issue("DOC006_ADR_SCHEMA", root, path, detail))

        basic_types_valid = (
            isinstance(adr_id, str)
            and ADR_ID_RE.fullmatch(adr_id) is not None
            and _nonempty_string(title, forbid_pipe=True)
            and isinstance(status, str)
            and status in ADR_STATUSES
            and isinstance(decision_date, str)
            and DATE_RE.fullmatch(decision_date) is not None
            and _string_list(supersedes, nonempty=False)
            and all(ADR_ID_RE.fullmatch(item) for item in supersedes)
            and (
                superseded_by is None
                or (isinstance(superseded_by, str) and ADR_ID_RE.fullmatch(superseded_by))
            )
        )
        if basic_types_valid:
            try:
                date.fromisoformat(decision_date)
            except ValueError:
                basic_types_valid = False
        if basic_types_valid:
            adrs.append(
                Adr(
                    path=path,
                    id=adr_id,
                    title=title,
                    status=status,
                    date=decision_date,
                    supersedes=tuple(supersedes),
                    superseded_by=superseded_by,
                )
            )

    for adr_id, paths in adr_id_paths.items():
        if len(paths) > 1:
            for path in paths:
                issues.append(
                    issue(
                        "DOC006_ADR_SCHEMA",
                        root,
                        path,
                        f"{adr_id} is declared {len(paths)} times",
                    )
                )

    issues.extend(_validate_supersession(root, adrs))
    return work_items, adrs, sorted(set(issues))


def _numeric_id(identifier: str) -> int:
    return int(identifier.rsplit("-", 1)[1])


def _markdown_table_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|")


def _validate_supersession(root: Path, adrs: list[Adr]) -> list[Issue]:
    issues: list[Issue] = []
    by_id = {adr.id: adr for adr in adrs}
    successors: dict[str, list[str]] = defaultdict(list)

    for adr in adrs:
        for target_id in adr.supersedes:
            successors[target_id].append(adr.id)
            target = by_id.get(target_id)
            if target is None:
                issues.append(
                    issue(
                        "DOC007_ADR_SUPERSESSION",
                        root,
                        adr.path,
                        f"supersedes target {target_id} does not exist",
                    )
                )
                continue
            if target_id == adr.id:
                issues.append(
                    issue(
                        "DOC007_ADR_SUPERSESSION", root, adr.path, "an ADR cannot supersede itself"
                    )
                )
            if _numeric_id(target_id) >= _numeric_id(adr.id):
                issues.append(
                    issue(
                        "DOC007_ADR_SUPERSESSION",
                        root,
                        adr.path,
                        f"superseded ADR {target_id} must have a smaller number",
                    )
                )
            if adr.status != "accepted":
                issues.append(
                    issue(
                        "DOC007_ADR_SUPERSESSION", root, adr.path, "successor ADR must be accepted"
                    )
                )
            if target.status != "superseded":
                issues.append(
                    issue(
                        "DOC007_ADR_SUPERSESSION",
                        root,
                        target.path,
                        f"status must be superseded by {adr.id}",
                    )
                )
            if target.superseded_by != adr.id:
                issues.append(
                    issue(
                        "DOC007_ADR_SUPERSESSION",
                        root,
                        target.path,
                        f"superseded_by must be {adr.id}",
                    )
                )

    for target_id, successor_ids in successors.items():
        if len(successor_ids) > 1:
            for successor_id in successor_ids:
                successor = by_id.get(successor_id)
                if successor is not None:
                    issues.append(
                        issue(
                            "DOC007_ADR_SUPERSESSION",
                            root,
                            successor.path,
                            f"{target_id} has multiple successors",
                        )
                    )

    for adr in adrs:
        if adr.superseded_by is not None:
            successor = by_id.get(adr.superseded_by)
            if successor is None:
                issues.append(
                    issue(
                        "DOC007_ADR_SUPERSESSION",
                        root,
                        adr.path,
                        f"superseded_by target {adr.superseded_by} does not exist",
                    )
                )
            elif adr.id not in successor.supersedes:
                issues.append(
                    issue(
                        "DOC007_ADR_SUPERSESSION",
                        root,
                        adr.path,
                        f"{adr.superseded_by} does not declare the reverse supersedes relation",
                    )
                )
            if adr.status != "superseded":
                issues.append(
                    issue(
                        "DOC007_ADR_SUPERSESSION",
                        root,
                        adr.path,
                        "an ADR with superseded_by must have superseded status",
                    )
                )
        elif adr.status == "superseded":
            issues.append(
                issue(
                    "DOC007_ADR_SUPERSESSION",
                    root,
                    adr.path,
                    "a superseded ADR must name superseded_by",
                )
            )

    graph = {adr.id: tuple(adr.supersedes) for adr in adrs}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            adr = by_id[identifier]
            issues.append(
                issue("DOC007_ADR_SUPERSESSION", root, adr.path, "supersession cycle detected")
            )
            return
        if identifier in visited:
            return
        visiting.add(identifier)
        for target in graph.get(identifier, ()):
            if target in graph:
                visit(target)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in sorted(graph):
        visit(identifier)
    return issues


def render_status(root: Path, work_items: list[WorkItem], adrs: list[Adr]) -> bytes:
    focused = [item for item in work_items if item.focus]
    if len(focused) != 1:
        raise ValueError("status rendering requires exactly one focused work item")
    focus = focused[0]
    work_sorted = sorted(work_items, key=lambda item: (not item.focus, _numeric_id(item.id)))
    adr_sorted = sorted(adrs, key=lambda adr: _numeric_id(adr.id))

    lines = [
        "# Project Status",
        "",
        "<!-- GENERATED by scripts/generate_status.py; DO NOT EDIT. -->",
        "",
        f"Focus: [{focus.id}](work/{focus.path.name})",
        "",
        "## Work Items",
        "",
        "| ID | Title | Status | Owner | Focus | Depends on |",
        "|---|---|---|---|---|---|",
    ]
    for item in work_sorted:
        dependencies = ", ".join(sorted(item.depends_on, key=_numeric_id)) or "—"
        lines.append(
            f"| [{item.id}](work/{item.path.name}) | {_markdown_table_cell(item.title)} | "
            f"{_markdown_table_cell(item.status)} | {_markdown_table_cell(item.owner)} | "
            f"{'true' if item.focus else 'false'} | {_markdown_table_cell(dependencies)} |"
        )
    lines.extend(
        [
            "",
            "## Decisions",
            "",
            "| ID | Title | Status | Supersedes | Superseded by |",
            "|---|---|---|---|---|",
        ]
    )
    for adr in adr_sorted:
        supersedes = ", ".join(sorted(adr.supersedes, key=_numeric_id)) or "—"
        superseded_by = adr.superseded_by or "—"
        lines.append(
            f"| [{adr.id}](decisions/{adr.path.name}) | {_markdown_table_cell(adr.title)} | "
            f"{_markdown_table_cell(adr.status)} | {_markdown_table_cell(supersedes)} | "
            f"{_markdown_table_cell(superseded_by)} |"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _heading_entries(text: str) -> list[tuple[int, int, str]]:
    headings: list[tuple[int, int, str]] = []
    in_fence = False
    fence_character = ""
    fence_length = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence = True
                fence_character = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_character and len(marker) >= fence_length:
                in_fence = False
            continue
        if in_fence:
            continue
        match = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if match:
            headings.append((line_number, len(match.group(1)), match.group(2).strip()))
    return headings


def _headings(text: str) -> list[tuple[int, str]]:
    return [(line_number, name) for line_number, _, name in _heading_entries(text)]


def _require_sections(root: Path, path: Path, required: tuple[str, ...], label: str) -> list[Issue]:
    try:
        _, text = read_managed_text(path)
    except DocumentFormatError as exc:
        return [issue("DOC001_REQUIRED_PATH", root, path, str(exc))]
    headings = _heading_entries(text)
    lines = text.splitlines()
    problems: list[Issue] = []
    for name in required:
        matches = [
            index
            for index, (_, _, heading) in enumerate(headings)
            if heading.casefold() == name.casefold()
        ]
        if not matches:
            problems.append(
                issue(
                    "DOC001_REQUIRED_PATH", root, path, f"missing required {label} section: {name}"
                )
            )
            continue
        heading_index = matches[0]
        start_line, level, _ = headings[heading_index]
        end_line = len(lines)
        for next_line, next_level, _ in headings[heading_index + 1 :]:
            if next_level <= level:
                end_line = next_line - 1
                break
        content = [line.strip() for line in lines[start_line:end_line] if line.strip()]
        if not content:
            problems.append(
                issue(
                    "DOC001_REQUIRED_PATH", root, path, f"required {label} section is empty: {name}"
                )
            )
    return problems


def _section_text(text: str, name: str) -> str:
    headings = _heading_entries(text)
    lines = text.splitlines()
    for index, (line_number, level, heading) in enumerate(headings):
        if heading.casefold() != name.casefold():
            continue
        end_line = len(lines)
        for next_line, next_level, _ in headings[index + 1 :]:
            if next_level <= level:
                end_line = next_line - 1
                break
        return "\n".join(lines[line_number:end_line]) + "\n"
    return ""


def _section_links_to(text: str, section: str, destination: str) -> bool:
    section_text = _section_text(text, section)
    destinations, _ = _markdown_destinations(section_text)
    return any(unquote(item).partition("#")[0] == destination for item in destinations)


def validate_required_paths_and_content(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for relative in REQUIRED_PATHS:
        path = root / relative
        if not path.is_file():
            issues.append(Issue("DOC001_REQUIRED_PATH", relative, "required file is missing"))
        elif not relative.startswith(("docs/work/", "docs/decisions/")):
            try:
                read_managed_text(path)
            except DocumentFormatError as exc:
                issues.append(issue("DOC001_REQUIRED_PATH", root, path, str(exc)))

    agents = root / "AGENTS.md"
    if agents.is_file():
        issues.extend(
            _require_sections(
                root,
                agents,
                (
                    "Project goal",
                    "Required reading",
                    "Architecture boundaries",
                    "Current focus",
                    "Tests",
                    "Paid calls",
                    "Prohibitions",
                    "Status source",
                ),
                "AGENTS",
            )
        )
        try:
            _, agents_text = read_managed_text(agents)
        except DocumentFormatError:
            pass
        else:
            if not _section_links_to(
                agents_text,
                "Current focus",
                "docs/work/WI-0001-weak-article-router-v1.md",
            ):
                issues.append(
                    issue(
                        "DOC001_REQUIRED_PATH",
                        root,
                        agents,
                        "Current focus must link to WI-0001",
                    )
                )
            if not _section_links_to(agents_text, "Status source", "docs/STATUS.md"):
                issues.append(
                    issue(
                        "DOC001_REQUIRED_PATH",
                        root,
                        agents,
                        "Status source must link to docs/STATUS.md",
                    )
                )
    charter = root / "docs" / "PROJECT_CHARTER.md"
    if charter.is_file():
        issues.extend(
            _require_sections(
                root, charter, ("WHY", "Scope", "Scientific goal", "Non-goals"), "charter"
            )
        )
    roadmap = root / "docs" / "ROADMAP.md"
    if roadmap.is_file():
        issues.extend(
            _require_sections(
                root,
                roadmap,
                (
                    "Dependencies",
                    "Slice 1",
                    "Slice 2",
                    "Slice 3",
                    "Slice 4",
                    "Slice 5",
                    "Follow-on direction",
                ),
                "roadmap",
            )
        )
    changelog = root / "CHANGELOG.md"
    if changelog.is_file():
        issues.extend(
            _require_sections(root, changelog, ("Changelog", "[Unreleased]"), "changelog")
        )
    return issues


def _mask_markdown_code(text: str) -> list[str]:
    output: list[str] = []
    in_fence = False
    fence_character = ""
    fence_length = 0
    for line in text.splitlines():
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence = True
                fence_character = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_character and len(marker) >= fence_length:
                in_fence = False
            output.append(" " * len(line))
            continue
        if in_fence:
            output.append(" " * len(line))
            continue

        characters = list(line)
        index = 0
        while index < len(line):
            if line[index] != "`":
                index += 1
                continue
            end_run = index
            while end_run < len(line) and line[end_run] == "`":
                end_run += 1
            marker = line[index:end_run]
            close = line.find(marker, end_run)
            if close < 0:
                index = end_run
                continue
            for masked in range(index, close + len(marker)):
                characters[masked] = " "
            index = close + len(marker)
        output.append("".join(characters))
    return output


def _destination(value: str) -> str:
    value = value.strip()
    if value.startswith("<"):
        closing = value.find(">")
        return value[1:closing] if closing >= 0 else value
    title = re.search(r"\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\))\s*$", value)
    if title:
        value = value[: title.start()]
    return value.strip()


def _inline_link_spans(line: str) -> list[tuple[int, int, str]]:
    links: list[tuple[int, int, str]] = []
    index = 0
    while index < len(line):
        if line[index] != "[":
            index += 1
            continue
        label_end = index + 1
        while label_end < len(line):
            if line[label_end] == "\\":
                label_end += 2
                continue
            if line[label_end] == "]":
                break
            label_end += 1
        if label_end >= len(line) or label_end + 1 >= len(line) or line[label_end + 1] != "(":
            index += 1
            continue

        depth = 1
        cursor = label_end + 2
        while cursor < len(line):
            if line[cursor] == "\\":
                cursor += 2
                continue
            if line[cursor] == "(":
                depth += 1
            elif line[cursor] == ")":
                depth -= 1
                if depth == 0:
                    links.append((index, cursor + 1, line[label_end + 2 : cursor]))
                    index = cursor + 1
                    break
            cursor += 1
        else:
            index += 1
    return links


def _markdown_destinations(text: str) -> tuple[list[str], list[str]]:
    lines = _mask_markdown_code(text)
    definitions: dict[str, str] = {}
    for line in lines:
        match = re.match(r"^ {0,3}\[([^\]]+)\]:\s*(.+?)\s*$", line)
        if match:
            label = " ".join(match.group(1).split()).casefold()
            definitions[label] = _destination(match.group(2))

    destinations: list[str] = []
    missing_references: list[str] = []
    for line in lines:
        if re.match(r"^ {0,3}\[[^\]]+\]:", line):
            continue
        inline_links = _inline_link_spans(line)
        for _, _, raw_destination in inline_links:
            destinations.append(_destination(raw_destination))
        characters = list(line)
        for start, end, _ in inline_links:
            for masked in range(start, end):
                characters[masked] = " "
        inline_removed = "".join(characters)
        for match in re.finditer(r"!?\[([^\]]+)\]\[([^\]]*)\]", inline_removed):
            label = match.group(2) or match.group(1)
            normalized = " ".join(label.split()).casefold()
            if normalized in definitions:
                destinations.append(definitions[normalized])
            else:
                missing_references.append(label)
    return destinations, missing_references


def github_anchors(text: str) -> set[str]:
    counts: Counter[str] = Counter()
    anchors: set[str] = set()
    for _, heading in _headings(text):
        normalized = heading.strip().lower()
        kept: list[str] = []
        for character in normalized:
            category = unicodedata.category(character)
            if character == "-" or character == "_" or character.isspace():
                kept.append(character)
            elif not category.startswith("P") and not category.startswith("S"):
                kept.append(character)
        slug = re.sub(r"\s+", "-", "".join(kept))
        slug = re.sub(r"-+", "-", slug).strip("-")
        occurrence = counts[slug]
        counts[slug] += 1
        anchors.add(slug if occurrence == 0 else f"{slug}-{occurrence}")
    return anchors


def validate_local_links(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    candidates: list[Path] = []
    for relative in ("AGENTS.md", "CHANGELOG.md"):
        path = root / relative
        if path.is_file():
            candidates.append(path)
    docs = root / "docs"
    if docs.is_dir():
        candidates.extend(path for path in docs.rglob("*.md") if path.is_file())

    root_resolved = root.resolve()
    anchor_cache: dict[Path, set[str]] = {}
    for source in sorted(set(candidates)):
        try:
            _, text = read_managed_text(source)
        except DocumentFormatError as exc:
            issues.append(
                issue(
                    "DOC005_LOCAL_LINK",
                    root,
                    source,
                    f"cannot scan managed Markdown: {exc}",
                )
            )
            continue
        destinations, missing_references = _markdown_destinations(text)
        for label in missing_references:
            issues.append(
                issue(
                    "DOC005_LOCAL_LINK",
                    root,
                    source,
                    f"reference label {label!r} has no definition",
                )
            )
        for encoded in destinations:
            destination = unquote(encoded).strip()
            lowered = destination.casefold()
            if lowered.startswith(("http:", "https:", "mailto:")):
                continue
            if not destination:
                issues.append(issue("DOC005_LOCAL_LINK", root, source, "empty link destination"))
                continue
            if lowered.startswith("file:"):
                issues.append(issue("DOC005_LOCAL_LINK", root, source, "file: links are forbidden"))
                continue
            if "\\" in destination:
                issues.append(issue("DOC005_LOCAL_LINK", root, source, "backslashes are forbidden"))
                continue
            path_part, separator, fragment = destination.partition("#")
            if re.match(r"^[A-Za-z]:", path_part) or path_part.startswith("/"):
                issues.append(
                    issue("DOC005_LOCAL_LINK", root, source, "absolute paths are forbidden")
                )
                continue
            target = source if path_part == "" else (source.parent / path_part).resolve()
            try:
                target.relative_to(root_resolved)
            except ValueError:
                issues.append(
                    issue("DOC005_LOCAL_LINK", root, source, "link escapes repository root")
                )
                continue
            if not target.is_file():
                issues.append(
                    issue(
                        "DOC005_LOCAL_LINK", root, source, f"target does not exist: {destination}"
                    )
                )
                continue
            if separator and fragment:
                if target not in anchor_cache:
                    try:
                        _, target_text = read_managed_text(target)
                    except DocumentFormatError:
                        anchor_cache[target] = set()
                    else:
                        anchor_cache[target] = github_anchors(target_text)
                if fragment.casefold() not in anchor_cache[target]:
                    issues.append(
                        issue(
                            "DOC005_LOCAL_LINK",
                            root,
                            source,
                            f"fragment does not exist: {destination}",
                        )
                    )
    return issues


def validate_size_budgets(root: Path) -> list[Issue]:
    budgets: dict[Path, int] = {
        root / "AGENTS.md": 12 * 1024,
        root / "CHANGELOG.md": 64 * 1024,
        root / "docs" / "PROJECT_CHARTER.md": 32 * 1024,
        root / "docs" / "ROADMAP.md": 32 * 1024,
        root / "docs" / "STATUS.md": 32 * 1024,
        root / "docs" / "architecture" / "SYSTEM_OVERVIEW.md": 64 * 1024,
        root / "docs" / "research" / "THESIS_MAINLINE_0824.md": 128 * 1024,
        root / "docs" / "research" / "HARNESS_ENGINEERING_0824.md": 128 * 1024,
    }
    work_dir = root / "docs" / "work"
    if work_dir.is_dir():
        budgets.update({path: 128 * 1024 for path in work_dir.glob("*.md")})
    decisions_dir = root / "docs" / "decisions"
    if decisions_dir.is_dir():
        budgets.update({path: 32 * 1024 for path in decisions_dir.glob("*.md")})

    issues: list[Issue] = []
    for path, budget in sorted(budgets.items(), key=lambda pair: pair[0].as_posix()):
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > budget:
            issues.append(
                issue(
                    "DOC008_SIZE_BUDGET",
                    root,
                    path,
                    f"{size} bytes exceeds {budget}-byte budget",
                )
            )
    return issues


def validate_status_drift(
    root: Path, work_items: list[WorkItem], adrs: list[Adr], metadata_issues: list[Issue]
) -> list[Issue]:
    if metadata_issues:
        return []
    status = root / "docs" / "STATUS.md"
    expected = render_status(root, work_items, adrs)
    if not status.is_file():
        return [Issue("DOC009_STATUS_DRIFT", "docs/STATUS.md", "generated status is missing")]
    if status.read_bytes() != expected:
        return [Issue("DOC009_STATUS_DRIFT", "docs/STATUS.md", "generated status differs")]
    return []


def validate_repository(root: Path) -> list[Issue]:
    work_items, adrs, metadata_issues = load_and_validate_metadata(root)
    issues = [
        *validate_required_paths_and_content(root),
        *metadata_issues,
        *validate_local_links(root),
        *validate_size_budgets(root),
        *validate_status_drift(root, work_items, adrs, metadata_issues),
    ]
    return sorted(set(issues))
