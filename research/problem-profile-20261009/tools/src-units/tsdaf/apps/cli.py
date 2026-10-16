from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path

from tsdaf.apps.commands.docs import run_docs_check
from tsdaf.apps.commands.image import run_image
from tsdaf.apps.commands.literature import run_literature_route
from tsdaf.apps.commands.package import run_package_validate
from tsdaf.apps.commands.route import run_route, run_route_resume
from tsdaf.apps.commands.run import run_export_calls, run_export_router_calls, run_inspect
from tsdaf.contracts.literature.schema import MachineProfile


def _package_validate(namespace: argparse.Namespace) -> int:
    return run_package_validate(namespace.package_dir)


def _route_run(namespace: argparse.Namespace) -> int:
    return run_route(
        namespace.package_dir, live=namespace.live, review_queue=namespace.review_queue
    )


def _route_resume(namespace: argparse.Namespace) -> int:
    return run_route_resume(
        namespace.execution_id, live=namespace.live, review_queue=namespace.review_queue
    )


def _route_export_calls(namespace: argparse.Namespace) -> int:
    return run_export_router_calls(
        namespace.workspace, namespace.execution_id, namespace.out_dir, source=namespace.source
    )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be a nonnegative integer")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive number")
    return parsed


def _image_run(namespace: argparse.Namespace) -> int:
    return run_image(
        namespace.job_path,
        namespace.source_package_dir,
        bundle_root=namespace.molyolo_bundle,
        workspace=namespace.workspace,
        device=namespace.device,
        workers=namespace.workers,
        retries=namespace.retries,
        timeout=namespace.timeout,
        execution_id=namespace.execution_id,
        live=namespace.live,
    )


def _literature_run(namespace: argparse.Namespace) -> int:
    return run_literature_route(
        namespace.package_dir,
        workspace=namespace.workspace,
        route_execution_id=namespace.route_execution_id,
        document_revision_id=namespace.document_revision_id,
        route_group_id=namespace.route_group_id,
        execution_id=namespace.execution_id,
        live=namespace.live,
        publish=namespace.publish,
        text_fixture=namespace.text_fixture,
        visual_fixture=namespace.visual_fixture,
        gate_fixture=namespace.gate_fixture,
        detail_fixture=namespace.detail_fixture,
        supplement_fixture=namespace.supplement_fixture,
        units_fixture=namespace.units_fixture,
        source_manifest=namespace.source_manifest,
        previous_publication=namespace.previous_publication,
        si_packages=tuple(namespace.si_packages or ()),
        machine_profile=MachineProfile(namespace.machine_profile),
    )


def _run_inspect(namespace: argparse.Namespace) -> int:
    return run_inspect(namespace.execution_id)


def _run_export_calls(namespace: argparse.Namespace) -> int:
    return run_export_calls(namespace.workspace, namespace.execution_id, namespace.out_dir)


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
    route_run.add_argument(
        "--review-queue",
        type=Path,
        help="the human review queue CSV (default: review_queue.csv in the working directory)",
    )
    route_run.set_defaults(handler=_route_run)
    resume = route_commands.add_parser("resume", help="resume one route execution")
    resume.add_argument("execution_id")
    resume.add_argument("--live", action="store_true")
    resume.add_argument("--review-queue", type=Path)
    resume.set_defaults(handler=_route_resume)
    route_export = route_commands.add_parser(
        "export-calls", help="export the Router's calls of one sealed route execution"
    )
    route_export.add_argument("execution_id")
    route_export.add_argument("--workspace", type=Path, default=Path.cwd() / ".tsdaf-workspace")
    route_export.add_argument("--out-dir", type=Path, required=True)
    route_export.add_argument("--source", help="the corpus file's name (default: execution id)")
    route_export.set_defaults(handler=_route_export_calls)

    image = commands.add_parser("image", help="source-image processing commands")
    image_commands = image.add_subparsers(dest="image_command", required=True)
    image_run = image_commands.add_parser(
        "run", help="detect, crop, and transcribe molecules from one source image"
    )
    image_run.add_argument("job_path", type=Path)
    image_run.add_argument("source_package_dir", type=Path)
    image_run.add_argument("--molyolo-bundle", type=Path, required=True)
    image_run.add_argument("--workspace", type=Path, default=Path.cwd() / ".tsdaf-workspace")
    image_run.add_argument("--device", default="auto")
    image_run.add_argument("--workers", type=_positive_int, default=4)
    image_run.add_argument("--retries", type=_nonnegative_int, default=2)
    image_run.add_argument("--timeout", type=_positive_float, default=120.0)
    image_run.add_argument("--execution-id")
    image_run.add_argument("--live", action="store_true")
    image_run.set_defaults(handler=_image_run)

    literature = commands.add_parser("literature", help="literature route-parsing commands")
    literature_commands = literature.add_subparsers(dest="literature_command", required=True)
    literature_run = literature_commands.add_parser(
        "run",
        help=(
            "run every literature route group the Router sealed, through Gate 1; "
            "name one group with --route-group-id to run just that group"
        ),
    )
    literature_run.add_argument("package_dir", type=Path)
    literature_run.add_argument("--workspace", type=Path, default=Path.cwd() / ".tsdaf-workspace")
    literature_run.add_argument("--route-execution-id", required=True)
    literature_run.add_argument("--document-revision-id", required=True)
    literature_run.add_argument(
        "--machine-profile",
        choices=tuple(profile.value for profile in MachineProfile if profile.production_enabled),
        default=MachineProfile.V1.value,
        help=(
            "explicit published fact contract and writing capabilities for this run; "
            "newer profiles are opt-in and preserve older sealed results"
        ),
    )
    literature_run.add_argument("--route-group-id")
    literature_run.add_argument(
        "--publish",
        action="store_true",
        help="also seal the public MachinePaperResult and SourceObservation bundle",
    )
    literature_run.add_argument("--execution-id")
    literature_run.add_argument("--text-fixture", type=Path)
    literature_run.add_argument("--visual-fixture", type=Path)
    literature_run.add_argument("--gate-fixture", type=Path)
    literature_run.add_argument("--detail-fixture", type=Path)
    literature_run.add_argument("--supplement-fixture", type=Path)
    literature_run.add_argument(
        "--figure-fixture",
        type=Path,
        help="recorded figure-reader (V3) interactions for the image reads of an offline run",
    )
    literature_run.add_argument(
        "--units-fixture",
        type=Path,
        help="recorded reading-units interactions for the SI packages of an offline run",
    )
    sources = literature_run.add_mutually_exclusive_group()
    sources.add_argument(
        "--source-manifest",
        type=Path,
        help="registered normalized MAIN/SI revisions; MAIN must match the sealed Router input",
    )
    sources.add_argument(
        "--si-package",
        dest="si_packages",
        action="append",
        type=Path,
        help=(
            "a supporting-information package directory; repeatable. The Host derives its "
            "registration and pairs it with the paper by DOI (needs --publish)"
        ),
    )
    literature_run.add_argument(
        "--previous-publication",
        type=Path,
        help="sealed publication receipt to continue the direct machine revision chain",
    )
    literature_run.add_argument("--live", action="store_true")
    literature_run.set_defaults(handler=_literature_run)

    run = commands.add_parser("run", help="execution inspection commands")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    inspect = run_commands.add_parser("inspect", help="verify and inspect one execution")
    inspect.add_argument("execution_id")
    inspect.set_defaults(handler=_run_inspect)
    export_calls = run_commands.add_parser(
        "export-calls", help="export the model calls of one sealed run to the replay corpus"
    )
    export_calls.add_argument("execution_id", help="the route group's execution id")
    export_calls.add_argument(
        "--workspace", type=Path, default=Path.cwd() / ".tsdaf-workspace", help="run workspace"
    )
    export_calls.add_argument("--out-dir", type=Path, required=True)
    export_calls.set_defaults(handler=_run_export_calls)

    docs = commands.add_parser("docs", help="repository documentation commands")
    docs_commands = docs.add_subparsers(dest="docs_command", required=True)
    check = docs_commands.add_parser("check", help="validate governed documentation")
    check.set_defaults(handler=_docs_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    namespace = _parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = namespace.handler
    return handler(namespace)
