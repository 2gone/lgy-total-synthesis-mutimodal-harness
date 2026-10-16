"""Thin ``tsdaf literature`` command."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from tsdaf.apps.literature_composition import (
    compose_literature_workflow,
    unconnected_literature_options,
)
from tsdaf.contracts.literature.schema import MachineProfile
from tsdaf.contracts.schema import canonical_json_bytes

__all__ = ("run_literature_route",)


def _emit(payload: dict[str, object], *, error: bool) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(canonical_json_bytes(payload).decode("utf-8") + "\n")


def run_literature_route(
    package_dir: Path,
    *,
    workspace: Path,
    route_execution_id: str,
    document_revision_id: str,
    route_group_id: str | None,
    execution_id: str | None,
    live: bool,
    publish: bool = False,
    text_fixture: Path | None,
    visual_fixture: Path | None,
    gate_fixture: Path | None,
    detail_fixture: Path | None = None,
    supplement_fixture: Path | None = None,
    units_fixture: Path | None = None,
    figure_fixture: Path | None = None,
    source_manifest: Path | None = None,
    previous_publication: Path | None = None,
    si_packages: tuple[Path, ...] = (),
    machine_profile: MachineProfile = MachineProfile.V1,
) -> int:
    """Run the profile's skeleton and detail workflow.

    Faithful compilation consumes all Router groups. Historical profiles require an
    explicit group. Production profile guards remain in application composition.
    """

    resolved_execution_id = execution_id or f"exec_literature_{uuid.uuid4().hex}"
    unavailable = unconnected_literature_options(
        route_group_id=route_group_id,
        publish=publish,
        source_manifest=source_manifest,
        previous_publication=previous_publication,
        execution_id=resolved_execution_id,
        additional_fixtures=detail_fixture is not None,
        si_packages=si_packages,
        machine_profile=machine_profile,
    )
    if unavailable is not None:
        code, payload = unavailable
        _emit(payload, error=True)
        return code
    fixtures = None
    if live:
        fixture_paths = (
            text_fixture,
            visual_fixture,
            gate_fixture,
            supplement_fixture,
            detail_fixture,
            units_fixture,
        )
        if any(path is not None for path in fixture_paths):
            _emit(
                {
                    "execution_id": resolved_execution_id,
                    "status": "NOT_STARTED",
                    "code": "CONFIGURATION_INVALID",
                    "message": "A live literature run cannot also replay recorded fixtures.",
                },
                error=True,
            )
            return 2
    else:
        supplied = {
            "T1": text_fixture,
            "V1": visual_fixture,
            "GATE1": gate_fixture,
        }
        if any(value is None for value in supplied.values()):
            _emit(
                {
                    "execution_id": resolved_execution_id,
                    "status": "ERROR",
                    "code": "CONFIGURATION_INVALID",
                    "message": (
                        "An offline literature run requires --text-fixture, "
                        "--visual-fixture and --gate-fixture."
                    ),
                },
                error=True,
            )
            return 2
        fixtures = {stage: path for stage, path in supplied.items() if path is not None}
        if supplement_fixture is not None:
            fixtures["SUPPLEMENT"] = supplement_fixture
        if detail_fixture is not None:
            fixtures["DETAIL"] = detail_fixture
        if units_fixture is not None:
            fixtures["UNITS"] = units_fixture
        if figure_fixture is not None:
            fixtures["FIGURE"] = figure_fixture

    code, payload = compose_literature_workflow(
        package_dir=package_dir,
        workspace=workspace,
        route_execution_id=route_execution_id,
        document_revision_id=document_revision_id,
        route_group_id=route_group_id,
        execution_id=resolved_execution_id,
        live=live,
        publish=publish,
        recorded_fixtures=fixtures,
        source_manifest=source_manifest,
        previous_publication=previous_publication,
        si_packages=si_packages,
        machine_profile=machine_profile,
    )
    _emit(payload, error=code not in {0, 3})
    return code
