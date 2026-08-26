from __future__ import annotations

import argparse
import sys
from pathlib import Path

from docs_support import validate_repository


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate governed project documentation.")
    parser.add_argument("--root", type=Path, help="Repository root used by isolated tests.")
    return parser


def _root(argument: Path | None) -> Path:
    root = argument if argument is not None else Path(__file__).resolve().parent.parent
    resolved = root.resolve()
    if not resolved.is_dir():
        raise OSError(f"repository root is not a directory: {resolved}")
    return resolved


def main(arguments: list[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    try:
        problems = validate_repository(_root(options.root))
    except (OSError, ValueError) as exc:
        print(f"docs validation failed: {exc}", file=sys.stderr)
        return 2
    if problems:
        for problem in problems:
            print(problem.format())
        return 1
    print("DOCS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
