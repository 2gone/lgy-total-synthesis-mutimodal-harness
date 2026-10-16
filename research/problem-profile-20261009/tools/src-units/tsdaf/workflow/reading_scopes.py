"""Host-owned edit scopes; an empty independent scope asserts no scientific fact."""

from copy import deepcopy

from tsdaf.lanes.literature.container_enrichment import content_hash
from tsdaf.lanes.literature.gate_models import FusionResult

INDEPENDENT = "INDEPENDENT"


def with_independent_scope(context, *, paper_id, package_sha256, execution_id):
    context = deepcopy(context)
    gid = "READING-SCOPE-" + content_hash([paper_id, package_sha256, execution_id])[:24]
    groups = context.setdefault("groups", [])
    if not any(group.get("route_group_id") == gid for group in groups):
        result = FusionResult(
            work_unit_id=gid, package_sha256=package_sha256, facts=(), sources={},
            evidence_registry_refs=(), note="Independent experimental accounts; no route asserted.",
        )
        groups.append({
            "route_group_id": gid, "scope_kind": INDEPENDENT,
            "display_name": "independent experiments",
            "result": result.model_dump(mode="json"), "reads": {},
        })
    context.setdefault("block_provenance", {}).setdefault(gid, {})
    return context
