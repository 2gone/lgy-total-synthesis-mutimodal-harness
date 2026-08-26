from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from docs_support import Issue, load_and_validate_metadata, render_status


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate deterministic project status.")
    parser.add_argument("--root", type=Path, help="Repository root used by isolated tests.")
    parser.add_argument("--check", action="store_true", help="Check bytes without writing.")
    return parser


def _root(argument: Path | None) -> Path:
    root = argument if argument is not None else Path(__file__).resolve().parent.parent
    resolved = root.resolve()
    if not resolved.is_dir():
        raise OSError(f"repository root is not a directory: {resolved}")
    return resolved


def _print_issues(issues: list[Issue]) -> None:
    for problem in sorted(set(issues)):
        print(problem.format())


def _atomic_write(path: Path, content: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(arguments: list[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    try:
        root = _root(options.root)
        work_items, adrs, issues = load_and_validate_metadata(root)
        if issues:
            _print_issues(issues)
            return 1
        expected = render_status(root, work_items, adrs)
        status = root / "docs" / "STATUS.md"
        if options.check:
            if not status.is_file() or status.read_bytes() != expected:
                print("DOC009_STATUS_DRIFT docs/STATUS.md: generated status differs")
                return 1
            return 0
        if not status.parent.is_dir():
            raise OSError(f"status directory does not exist: {status.parent}")
        _atomic_write(status, expected)
        return 0
    except (OSError, ValueError) as exc:
        print(f"docs generation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
