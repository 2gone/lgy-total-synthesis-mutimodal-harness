"""Accepted findings to actual application locations, not a second fact store.

The current task's feedback and sealed assembly consume this same join. Locations
identify the object and field this application touched; they do not establish
scientific support, adoption, or ownership of every value in that field.
"""

from collections.abc import Mapping, Sequence
from typing import Any


def finding_applications(
    answer: Any,
    compiled: Any,
    locations: Mapping[tuple[str, int], Sequence[Mapping[str, str]]],
    failures: Sequence[Mapping[str, Any]],
    *, narrative_applications: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    contributions = getattr(compiled, "finding_contributions", {})
    target_limits = getattr(compiled, "finding_target_limits", {})
    uncompiled = getattr(compiled, "uncompiled_findings", {})
    refused = {(f["collection"], f["index"]): f["reason"] for f in failures}
    result = []
    for finding in getattr(answer, "findings", ()):
        if finding.aspect == "nothing_to_record":
            result.append({
                "collection": "findings", "item_id": finding.item_id,
                "finding_id": finding.finding_id, "state": "RECORDED",
                "public_targets": [],
                "note": "No addition recorded: " + finding.explanation,
            })
            continue
        items = contributions.get(finding.finding_id, ())
        if items and all(collection == "narratives" for collection, _ in items):
            result.extend(dict(row) for row in narrative_applications
                          if row.get("collection") == "findings" and row.get("item_id") == finding.item_id)
            continue
        reasons = [refused[key] for key in items if key in refused]
        targets = []
        for key in items:
            if key in refused:
                continue
            if key not in locations:
                reasons.append("Compiled contribution has no recorded public application location.")
            for target in locations.get(key, ()):
                if (finding.finding_id in target_limits
                        and dict(target) not in target_limits[finding.finding_id]):
                    continue
                if dict(target) not in targets:
                    targets.append(dict(target))
        row = {
            "collection": "findings",
            "item_id": finding.item_id,
            "finding_id": finding.finding_id,
            "state": ("PARTLY_APPLIED" if reasons else "APPLIED")
            if targets
            else "NOT_APPLIED"
            if items and all(k in refused for k in items)
            else "SAVED_ONLY",
            "public_targets": targets,
        }
        if not items:
            reasons.append(
                uncompiled.get(finding.finding_id, "No public contribution was compiled.")
            )
        if reasons:
            row["reasons"] = list(dict.fromkeys(reasons))
        result.append(row)
    return result


def remap_application_targets(
    records: Sequence[Mapping[str, Any]], id_map: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Follow the finalizer's explicit identity map, never match source wording."""
    return [
        {
            **record,
            "public_targets": [
                {**target, "object_id": id_map.get(target["object_id"], target["object_id"])}
                for target in record["public_targets"]
            ],
        }
        for record in records
    ]
