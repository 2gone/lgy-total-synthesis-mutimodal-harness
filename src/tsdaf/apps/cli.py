from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path

from tsdaf.apps.commands.docs import run_docs_check
from tsdaf.apps.commands.package import run_package_validate
from tsdaf.apps.commands.route import run_route, run_route_resume
from tsdaf.apps.commands.run import run_inspect


def _package_validate(namespace: argparse.Namespace) -> int:
    return run_package_validate(namespace.package_dir)


def _route_run(namespace: argparse.Namespace) -> int:
    return run_route(namespace.package_dir, live=namespace.live)


def _route_resume(namespace: argparse.Namespace) -> int:
    return run_route_resume(namespace.execution_id, live=namespace.live)


def _run_inspect(namespace: argparse.Namespace) -> int:
    return run_inspect(namespace.execution_id)


def _docs_check(namespace: argparse.Namespace) -> int:
    del namespace
    return run_docs_check()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tsdaf")
    commands = parser.add_subparsers(dest="command", required=True)

    package = commands.add_parser("package", help="paper-package commands")
    package_commands = package.add_subparsers(dest="package_command", required=True)
    validate = package_commands.add_parser("validate", help="validate one paper package")
    validate.add_argument("package_dir", type=Path)
    validate.set_defaults(handler=_package_validate)

    route = commands.add_parser("route", help="article-routing commands")
    route_commands = route.add_subparsers(dest="route_command", required=True)
    route_run = route_commands.add_parser("run", help="route one paper package")
    route_run.add_argument("package_dir", type=Path)
    route_run.add_argument("--live", action="store_true")
    route_run.set_defaults(handler=_route_run)
    resume = route_commands.add_parser("resume", help="resume one route execution")
    resume.add_argument("execution_id")
    resume.add_argument("--live", action="store_true")
    resume.set_defaults(handler=_route_resume)

    run = commands.add_parser("run", help="execution inspection commands")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    inspect = run_commands.add_parser("inspect", help="verify and inspect one execution")
    inspect.add_argument("execution_id")
    inspect.set_defaults(handler=_run_inspect)

    docs = commands.add_parser("docs", help="repository documentation commands")
    docs_commands = docs.add_subparsers(dest="docs_command", required=True)
    check = docs_commands.add_parser("check", help="validate governed documentation")
    check.set_defaults(handler=_docs_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    namespace = _parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = namespace.handler
    return handler(namespace)
