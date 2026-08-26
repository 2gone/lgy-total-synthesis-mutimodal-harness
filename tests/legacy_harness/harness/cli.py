from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .config import HarnessConfig
from .ocr import run_ocr
from .runner import dry_run_summary, run_cell
from .validation import validate_manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="total-synthesis-harness")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    subcommands = parser.add_subparsers(dest="command", required=True)

    validate = subcommands.add_parser("validate", help="verify the delivery manifest")
    validate.add_argument("--offline", action="store_true", help="document that no API call is allowed")

    for name in ("dry-run", "run"):
        command = subcommands.add_parser(name)
        command.add_argument("--suite", required=True)
        command.add_argument("--condition", required=True)
        command.add_argument("--paper", required=True)
        if name == "run":
            command.add_argument("--env-file", type=Path)
            command.add_argument("--max-attempts", type=int, default=3)
    ocr = subcommands.add_parser("ocr", help="generate and seal deterministic OCR evidence")
    ocr.add_argument("--paper", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.repo_root.resolve()
    if args.command == "validate":
        report = validate_manifest(root)
        print(
            json.dumps(
                {
                    "offline": True,
                    "ok": report.ok,
                    "checked_files": report.checked_files,
                    "problems": [problem.__dict__ for problem in report.problems],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if report.ok else 1
    if args.command == "dry-run":
        print(
            json.dumps(
                dry_run_summary(root, args.suite, args.condition, args.paper),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "ocr":
        print(json.dumps(run_ocr(root, args.paper), ensure_ascii=False, indent=2))
        return 0

    env_path = args.env_file.resolve() if args.env_file else root / ".env"
    seal = run_cell(
        repo_root=root,
        suite=args.suite,
        condition=args.condition,
        paper_id=args.paper,
        settings=HarnessConfig.from_file(env_path),
        max_attempts=args.max_attempts,
    )
    print(json.dumps(seal, ensure_ascii=False, indent=2))
    return 0
