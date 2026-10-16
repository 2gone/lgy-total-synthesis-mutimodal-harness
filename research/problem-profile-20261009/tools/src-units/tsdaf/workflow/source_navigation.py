"""Resolve exact image names within the caller's existing authorized locations."""

from collections.abc import Mapping, Sequence
from typing import Any

from tsdaf.workflow.resource_reads import ReadFailure


def authorized_visual_location(
    name: str, candidates: Mapping[str, Sequence[Mapping[str, Any]]]
) -> str:
    """Read-only aliases; duplicate IDs offer actual locations, never a guessed revision.

    Callers supply authorized images only. No prefix stripping, label matching or
    case folding is performed here, and no selection/evidence identity is minted.
    """
    options = list({row["location"]: dict(row) for row in candidates.get(name, ())}.values())
    if len(options) == 1:
        return str(options[0]["location"])
    if len(options) > 1:
        raise ReadFailure(
            "AMBIGUOUS_LOCATION",
            "This image ID names more than one authorized source. Read the intended "
            "document's offered location.",
            available_reads=options,
        )
    return name
