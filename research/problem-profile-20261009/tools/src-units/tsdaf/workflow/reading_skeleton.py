"""Replay accepted Fusion repairs over a reading's exact, sealed starting state.

The raw answer is the journal. Runtime and publication call this same transaction;
no graph patch language or mutable second scientific state is introduced.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from typing import Any, cast

from tsdaf.contracts.literature.expressions import (
    EXPRESSION_SETS,
    merge_expression_contributions,
    remap_expression_field,
)
from tsdaf.contracts.literature.objects import experiment_containers
from tsdaf.contracts.literature.schema import machine_profile
from tsdaf.contracts.literature.validation import (
    _ensure_reference_closure,
    evidence_refs_in_order,
    required_fact_observation_refs,
)
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.literature.completion_guard import PRODUCT_ACCOUNT_GAP, STEP_ACCOUNT_GAP
from tsdaf.lanes.literature.container_enrichment import (
    _refs,
    content_hash,
    register_compound_addition,
)
from tsdaf.lanes.literature.fact_projection import (
    RouteGroupFactProjection,
    present,
    project_route_group_facts,
    project_route_group_state,
)
from tsdaf.lanes.literature.source_parts import fact_source_ids
from tsdaf.lanes.literature.fusion_state import fact_references, final_fact_keys
from tsdaf.lanes.literature.gate_models import FactChange, FactOrigin, read_fusion_result
from tsdaf.lanes.literature.host_ports import PrivateArtifactStore
from tsdaf.lanes.literature.material_bindings import (
    advance_bindings,
    bound_rows,
    initial_bindings,
    material_position,
)
from tsdaf.lanes.literature.material_sources import (
    MATERIAL_SOURCE_FIELDS,
    material_sources_for,
    rekey_material_sources,
)
from tsdaf.lanes.literature.middle_layer import (
    ReadingAnswer,
    SelectionsHeld,
    compound_addition,
    has_public_scope,
    material_value_decision,
    reading_compound_view,
    validate_reading,
)
from tsdaf.lanes.literature.stable_keys import _unresolved_kind
from tsdaf.workflow.fusion_names import fusion_names
from tsdaf.workflow.reading_scopes import INDEPENDENT
from tsdaf.workflow.reading_source_names import reading_source_binder
from tsdaf.workflow.literature_fusion_actions import (
    fold_bound_fact_commands,
    apply_sparse_submission,
    bind_fact_change_names,
    name_binder,
)


@dataclass(frozen=True)
class BoundSkeletonRepair:
    """One verified atomic command for contribution replay, never a model DTO.

    Construction alone is not acceptance evidence. Callers must verify its
    normalized commands, source authorization and historical births against the
    original accepted call before selecting it as an active contribution.
    """

    item_id: str
    route_group_id: str
    changes: tuple[FactChange, ...]
    origins: Mapping[str, FactOrigin]
    proven_births: Mapping[str, FactOrigin]
    sources: Mapping[str, Any]
    delivered_text: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class RepairedReading:
    machine: dict[str, Any]
    groups: tuple[dict[str, Any], ...]
    identity: dict[str, dict[str, str]]
    written_ids: frozenset[str] = frozenset()
    material_sources: dict[str, Any] = dataclass_field(default_factory=dict)
    material_bindings: dict[str, str | None] = dataclass_field(default_factory=dict)
    repair_subjects: dict[str, tuple[tuple[str, str], ...]] = dataclass_field(default_factory=dict)


def verify_surviving_reference_births(result, commands: Sequence[BoundSkeletonRepair]) -> None:
    """Final relationships retain the identity their actual field author selected.

    A cleared or overwritten relationship contributes no constraint. The proof
    comes from original accepted snapshots, never from labels or scientific text.
    """
    by_origin = {
        canonical_json_bytes(origin.model_dump(mode="json")): command
        for command in commands for origin in command.origins.values()
    }
    for fact in result.facts:
        for field, origins in result.field_origins.get(fact.fact_id, {}).items():
            refs = fact_references(fact, {field})
            for origin in origins:
                command = by_origin.get(canonical_json_bytes(origin.model_dump(mode="json")))
                if command is None:
                    continue  # The sealed baseline owns this field, not this contribution.
                for ref in refs:
                    original = command.proven_births.get(ref)
                    if original is not None and original != result.fact_creation_origins.get(ref):
                        raise ValueError("surviving relationship refers to a retired identity: " + ref)


def reading_compound_bridges(answer, *, current, scope_machine, selections, bind):
    """The existing explicit finding-to-Fusion correspondence, never label matching.

    The old snapshot path and bound contribution path use the same identity and
    content construction. Each caller still limits bridges to actually referenced
    objects; merely having a finding does not assign it to every route group.
    """
    for finding in answer.findings:
        if finding.aspect != "new_compound" or not has_public_scope(finding, scope_machine):
            continue
        added = compound_addition(finding)
        cid = register_compound_addition(
            deepcopy(current["domain_payload"]),
            str(current["paper_id"]),
            added,
            _refs(SelectionsHeld(selections), added.selection_ids),
            profile=machine_profile(current),
        )
        fid = bind(
            "reading-compound-" + content_hash([str(current["paper_id"]), added.compound_key])[:24]
        )
        yield (
            finding,
            cid,
            fid,
            {
                "fact_id": fid,
                "kind": "COMPOUND",
                "status": "ACCEPTED",
                "labels": list(added.author_labels),
                "text": next(iter(added.names), ""),
                "source_ids": list(added.selection_ids),
                "rationale": added.explanation,
            },
        )


def prepare_finding_bridges(
    base,
    changes: Sequence[FactChange],
    *,
    answer: ReadingAnswer,
    current: Mapping[str, Any],
    selections: Mapping[str, Mapping[str, Any]],
    identity: Mapping[str, str],
    store: PrivateArtifactStore,
    finding_origins: Mapping[str, FactOrigin],
    finding_births: Mapping[str, FactOrigin] | None = None,
    required_keys: set[str] | None = None,
):
    """Rebuild required host bridge facts from this candidate's active findings.

    Host prerequisites are separate from model-authored repair children. They are
    not recorded as repair commands and disappear when their finding contribution
    disappears. The ordinary fact reducer must then accept or reject surviving
    references; no birth record supplies missing scientific content.
    """
    bind = name_binder(base)
    referenced = (
        required_keys
        if required_keys is not None
        else {token for change in changes for token in {change.fact_id, *fact_references(change)}}
    )
    known = {fact.fact_id for fact in base.facts}
    bridged = {}
    for finding, cid, fid, raw in reading_compound_bridges(
        answer,
        current=current,
        scope_machine=current,
        selections=selections,
        bind=bind,
    ):
        if fid in known or not {cid, fid} & referenced:
            continue
        # A public object already represented by a group fact has no second bridge.
        if cid in identity.values():
            continue
        sources = dict(base.sources)
        texts = {}
        source_bind = reading_source_binder(base, selections)
        for sid in raw["source_ids"]:
            ref = (dict(selections[sid]["evidence_ref"]),)
            if source_bind(sid) in sources and sources[source_bind(sid)] != ref:
                raise ValueError("finding bridge source differs from its sealed identity")
            sources[source_bind(sid)] = ref
            texts[source_bind(sid)] = {**selections[sid], "location": sid}
        base = base.model_copy(update={"sources": sources})
        bind_fact_change_names(
            raw, subject=lambda token: token, reference=lambda token: token, source=source_bind
        )
        field_origin = finding_origins.get(finding.item_id)
        updated, replies, _ = apply_sparse_submission(
            base,
            {"groups": [{"changes": [raw]}]},
            locations={},
            store=store,
            last_shown=base,
            validate_questions=lambda proposal: None,
            machine_profile=machine_profile(current),
            delivered_source_ids=set(texts),
            delivered_text=texts,
            accepted_origin=(lambda collection, index, child, origin=field_origin: origin)
            if field_origin is not None
            else None,
        )
        if any(row["outcome"] == "REJECTED" for row in replies):
            raise ValueError("Fusion rejected explicit finding bridge: " + str(replies))
        # The accepted current finding supplies scientific field provenance. Its
        # earlier creation action preserves identity only, never the revised words.
        birth = (finding_births or {}).get(finding.item_id)
        if birth is not None:
            updated = updated.model_copy(
                update={
                    "fact_creation_origins": {
                        **updated.fact_creation_origins,
                        fid: birth,
                    }
                }
            )
        base = updated
        known.add(fid)
        bridged[fid] = cid
    return base, bridged


def rewrite(value: Any, ids: Mapping[str, str]) -> Any:
    """Exactly one identity lookup, never follow a chain through another object."""
    if isinstance(value, str):
        return ids.get(value, value)
    if isinstance(value, list):
        return [rewrite(v, ids) for v in value]
    if isinstance(value, tuple):
        return [rewrite(v, ids) for v in value]
    if isinstance(value, dict):
        return {
            k: remap_expression_field(k, v, lambda child: rewrite(child, ids))
            for k, v in value.items()
        }
    return value


def _records(projection: Any, ids: Mapping[str, str], *, include_route: bool = True) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        rewrite(
            {
                "compounds": list(projection.compounds),
                "reaction_blocks": list(projection.reaction_blocks),
                **({"experimental_accounts": list(projection.experimental_accounts)}
                   if projection.experimental_accounts else {}),
                **({"unassigned_outcomes": list(projection.unassigned_outcomes)}
                   if projection.unassigned_outcomes else {}),
                "routes": [projection.route] if include_route else [],
                "characterization_records": list(projection.characterization_records),
                "paper_narratives": list(projection.paper_narratives),
                "field_conflicts": list(projection.field_conflicts),
            },
            ids,
        ),
    )


_ENTITY_KEYS = {
    "compounds": "compound_id",
    "reaction_blocks": "reaction_block_id",
    "experimental_accounts": "account_id",
    "unassigned_outcomes": "outcome_id",
    "local_intermediates": "local_intermediate_id",
    "routes": "route_id",
    "operations": "operation_id",
    "reported_outcomes": "outcome_id",
    "characterization_records": "characterization_id",
    "paper_narratives": "narrative_id",
    "field_conflicts": "conflict_id",
}


def _flatten(domain: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Operation/outcome identity is independent of the block currently containing it."""
    entities: dict[str, dict[str, Any]] = {}

    def visit(value: Any, collection: str = "") -> Any:
        if isinstance(value, list):
            if collection in _ENTITY_KEYS:
                key = _ENTITY_KEYS[collection]
                result = []
                for record in value:
                    identity = record[key]
                    if identity in entities:
                        raise ValueError("skeleton contains duplicate entity identity")
                    entities[identity] = visit(record)
                    result.append(identity)
                return result
            return [visit(v) for v in value]
        if isinstance(value, dict):
            return {k: visit(v, k) for k, v in value.items()}
        return value

    tree = visit(dict(domain))
    return entities, tree


def _merge(before: Any, after: Any, current: Any, path: str) -> Any:
    if before == after:
        return deepcopy(current)
    if current == before or current == after:
        return deepcopy(after)
    if path.endswith((".evidence_refs", ".evidence_uses")) and all(
        isinstance(v, list) for v in (before, after, current)
    ):
        prior = {content_hash(v) for v in before}
        revised = {content_hash(v) for v in after}
        present = {content_hash(v) for v in current}
        if prior == revised:
            return deepcopy(current)
        if present == prior:
            return deepcopy(after)
        if prior <= revised:
            return list(
                {
                    content_hash(v): deepcopy(v)
                    for v in [*current, *(v for v in after if content_hash(v) not in prior)]
                }.values()
            )
    if isinstance(before, dict) and isinstance(after, dict) and isinstance(current, dict):
        result = deepcopy(current)
        for key in before.keys() | after.keys():
            if key in EXPRESSION_SETS:
                values = merge_expression_contributions(
                    before.get(key, []), after.get(key, []), current.get(key, [])
                )
                if values:
                    result[key] = values
                else:
                    result.pop(key, None)
                continue
            if key in {"stable_key", "key_basis"}:
                continue  # Internal task identities; the public finalizer alone keys them.
            if key not in before:
                if key in current and current[key] != after[key]:
                    raise ValueError(f"skeleton repair overlaps saved detail at {path}.{key}")
                result[key] = deepcopy(after[key])
            elif key not in after:
                if current.get(key) != before[key]:
                    raise ValueError(f"skeleton repair would remove saved detail at {path}.{key}")
                result.pop(key, None)
            else:
                result[key] = _merge(before[key], after[key], current.get(key), f"{path}.{key}")
        return result
    raise ValueError(
        f"skeleton repair overlaps saved detail at {path}; preserve or resolve it explicitly"
    )


def merge_material_field_sources(
    original: Mapping[str, Any],
    revised: Mapping[str, Any],
    prior: Mapping[str, Any],
    *,
    fact_id: str,
) -> dict[str, Any]:
    """One three-way source rule for old projection overlays and exact material-row repairs.

    Different adopted values take the revised value's sources. Equal values keep the
    existing three-way evidence decision, including independent same-value evidence
    and explicit refusal when a replacement overlaps separately saved support.
    """
    fields = deepcopy(dict(revised["fields"]))
    fields.update(
        {k: deepcopy(v) for k, v in prior["fields"].items() if k not in MATERIAL_SOURCE_FIELDS}
    )
    for key in MATERIAL_SOURCE_FIELDS:
        if original["value"].get(key) != revised["value"].get(key) and prior["value"].get(
            key
        ) != revised["value"].get(key):
            continue
        refs = _merge(
            original["fields"].get(key, []),
            revised["fields"].get(key, []),
            prior["fields"].get(key, []),
            f"material.{fact_id}.{key}.evidence_refs",
        )
        if refs:
            fields[key] = refs
        else:
            fields.pop(key, None)
    return fields


def overlay_material_sources(
    previous: Mapping[str, Any],
    old: RouteGroupFactProjection,
    new: RouteGroupFactProjection,
    old_ids: Mapping[str, str],
    new_ids: Mapping[str, str],
    blocks: list[dict[str, Any]],
    *,
    handled_operations: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Follow selected MATERIAL identities and the same three-way evidence decisions."""
    result = rekey_material_sources(previous, {}, blocks)

    def entries(
        projection: RouteGroupFactProjection, ids: Mapping[str, str]
    ) -> dict[str, tuple[str, dict[str, Any]]]:
        rows = rekey_material_sources(
            projection.material_sources, ids,
            rewrite([*projection.reaction_blocks, *projection.experimental_accounts], ids)
        )
        return {
            fid: (
                ids.get(oid, oid),
                next(
                    row
                    for row in rows[ids.get(oid, oid)]
                    if row["value"]["sequence_index"] == index
                ),
            )
            for fid, (oid, index) in projection.material_fact_rows.items()
        }

    before, after = entries(old, old_ids), entries(new, new_ids)
    operations = {op["operation_id"]: op for b in blocks for op in b["operations"]}
    for fid, (oid, revised) in after.items():
        if oid in handled_operations:
            continue
        op = operations.get(oid)
        if op is None or op["materials"]["state"] != "PRESENT":
            continue
        values = op["materials"]["value"]
        if revised["value"] not in values:
            continue  # A separately accepted wrapping value remains authoritative.
        index = values.index(revised["value"])
        if oid not in result:
            result[oid] = [{"value": deepcopy(v), "fields": {}} for v in values]
        fields = deepcopy(revised["fields"])
        if fid in before:
            old_oid, original = before[fid]
            prior = next(
                (
                    row
                    for row in previous.get(old_oid, ())
                    if row["value"] in (original["value"], revised["value"])
                ),
                original,
            )
            fields = merge_material_field_sources(original, revised, prior, fact_id=fid)
        result[oid][index] = {"value": deepcopy(revised["value"]), "fields": fields}
    return result


def material_repair_values(
    current: Mapping[str, Any],
    old: RouteGroupFactProjection,
    new: RouteGroupFactProjection,
    old_ids: Mapping[str, str],
    new_ids: Mapping[str, str],
    fact_positions: Mapping[str, str | None],
    sources: Mapping[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str | None], dict[str, Any], dict[str, str]]:
    """Merge exact MATERIAL rows, preserving detail rows and rebinding their references.

    Current positions are supplied by the repair journal, never discovered by names or
    full-row equality. Detail-only rows retain their order after their nearest surviving
    predecessor; a removed target or cross-operation relationship is explicitly refused.
    """
    from tsdaf.lanes.literature.material_bindings import material_rows

    current_rows = material_rows(current)

    def projected(
        projection: RouteGroupFactProjection, ids: Mapping[str, str]
    ) -> dict[str, tuple[str, dict[str, Any]]]:
        ops = {o["operation_id"]: o for b in (*projection.reaction_blocks, *projection.experimental_accounts)
               for o in b["operations"]}
        return {
            fid: (ids.get(oid, oid), rewrite(ops[oid]["materials"]["value"][index - 1], ids))
            for fid, (oid, index) in projection.material_fact_rows.items()
        }

    before, after = projected(old, old_ids), projected(new, new_ids)

    def projected_sources(projection: RouteGroupFactProjection) -> dict[str, dict[str, Any]]:
        return {
            fid: deepcopy(projection.material_sources[oid][index - 1].get("fields", {}))
            for fid, (oid, index) in projection.material_fact_rows.items()
        }

    before_sources, after_sources = projected_sources(old), projected_sources(new)
    # Only operations retained on both sides can contain previously saved detail.
    old_ops = {
        old_ids.get(o["operation_id"], o["operation_id"])
        for b in (*old.reaction_blocks, *old.experimental_accounts)
        for o in b["operations"]
    }
    new_ops = {
        new_ids.get(o["operation_id"], o["operation_id"])
        for b in (*new.reaction_blocks, *new.experimental_accounts)
        for o in b["operations"]
    }
    shared = old_ops & new_ops
    tokens = {position: fid for fid, position in fact_positions.items() if position is not None}
    values: dict[str, list[dict[str, Any]]] = {}
    transitions: dict[str, str | None] = {}
    updated_sources = deepcopy(dict(sources))
    all_target_positions: dict[str, tuple[str, int]] = {}
    row_plans: dict[str, list[tuple[str, dict[str, Any], dict[str, Any], str | None]]] = {}
    for oid in shared:
        rows = [
            (position, row) for position, row in current_rows.items() if row["operation_id"] == oid
        ]
        rows.sort(key=lambda pair: pair[1]["index"])
        if not rows and not any(target_oid == oid for target_oid, _ in after.values()):
            continue
        carried = {}
        for position, row in rows:
            token = tokens.get(position, position)
            fields = {}
            available = sources.get(oid, [])
            if row["index"] < len(available) and available[row["index"]]["value"] == row["value"]:
                fields = deepcopy(available[row["index"]].get("fields", {}))
            carried[token] = (deepcopy(row["value"]), fields, position)
        plan: list[tuple[str, dict[str, Any], dict[str, Any], str | None]] = []
        for fid, (target_oid, revised) in after.items():
            if target_oid != oid:
                continue
            if fid in before:
                selected_position = fact_positions.get(fid)
                if selected_position not in current_rows:
                    raise ValueError(f"material repair has no unique current binding for {fid}")
                original = before[fid][1]
                value, fields, source_position = carried.get(fid, (None, {}, selected_position))
                if value is None:
                    raise ValueError(
                        "moving an enriched material between operations requires explicit ownership"
                    )
                # Array order is host bookkeeping, not a competing material value.
                original = {**original, "sequence_index": value.get("sequence_index")}
                revised = {**revised, "sequence_index": value.get("sequence_index")}
                merged = _merge(original, revised, value, f"material.{fid}")
                fields = merge_material_field_sources(
                    {"value": original, "fields": before_sources.get(fid, {})},
                    {"value": revised, "fields": after_sources.get(fid, {})},
                    {"value": value, "fields": fields},
                    fact_id=fid,
                )
                plan.append((fid, merged, fields, source_position))
            else:
                plan.append((fid, deepcopy(revised), deepcopy(after_sources.get(fid, {})), None))
        for fid, (original_oid, _original) in before.items():
            if original_oid == oid and fid not in after and fid in carried:
                value, _, position = carried[fid]
                # Explicitly withdrawing this MATERIAL withdraws its own attached
                # detail too; immutable prior revisions retain it. Separate use rows
                # remain, and their incoming summary links are checked below.
                transitions[position] = None
        # Extras carry no skeleton identity. Retain relative order without name matching.
        last = None
        for position, _row in rows:
            token = tokens.get(position, position)
            if token in before:
                if any(entry[0] == token for entry in plan):
                    last = token
                continue
            value, fields, _ = carried[token]
            insert = next((i + 1 for i, e in enumerate(plan) if e[0] == last), 0)
            plan.insert(insert, (token, value, fields, position))
            last = token
        row_plans[oid] = plan
        for index, (token, _, _, _) in enumerate(plan, 1):
            all_target_positions[token] = (oid, index)
    for oid, plan in row_plans.items():
        values[oid] = []
        updated_sources[oid] = []
        for index, (_token, value, fields, previous) in enumerate(plan, 1):
            target = value.get("summary_material_index")
            if target is not None:
                old_target = material_position(oid, target)
                target_token = tokens.get(old_target, old_target)
                if (
                    target_token not in all_target_positions
                    or all_target_positions[target_token][0] != oid
                ):
                    raise ValueError(
                        "material summary was removed or moved; explicitly rebind its detail first"
                    )
                value["summary_material_index"] = all_target_positions[target_token][1]
            value["sequence_index"] = index
            values[oid].append(value)
            updated_sources[oid].append({"value": deepcopy(value), "fields": fields})
            if previous is not None:
                transitions[previous] = material_position(oid, index)
    final_facts = {
        fid: material_position(*all_target_positions[fid])
        if fid in all_target_positions
        else material_position(new_ids.get(oid, oid), index)
        for fid, (oid, index) in new.material_fact_rows.items()
    }
    for fid, prior_position in fact_positions.items():
        if prior_position is not None and prior_position not in transitions:
            transitions[prior_position] = final_facts.get(fid)
    return values, transitions, updated_sources, final_facts


def overlay_projection(
    machine: Mapping[str, Any],
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    *,
    material_rows: Mapping[str, list[dict[str, Any]]] | None = None,
    material_transitions: Mapping[str, str | None] | None = None,
    old_roots: Mapping[str, Any] | None = None,
    new_roots: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Three-way projection by entity identity; never replace a whole enriched block."""
    before, old_tree = _flatten(old)
    after, new_tree = _flatten(new)
    current, tree = _flatten(machine["domain_payload"])
    merged = deepcopy(current)
    for identity in before.keys() | after.keys():
        if identity not in before:
            if identity in current:
                # A Host-bridged E1 compound already exists; its detail/evidence survives.
                if "compound_id" not in after[identity]:
                    raise ValueError("new skeleton identity collides with a saved object")
                merged[identity] = {**deepcopy(after[identity]), **deepcopy(current[identity])}
                for field in ("predecessor_ids", "successor_ids"):
                    merged[identity][field] = deepcopy(after[identity][field])
                merged[identity]["evidence_refs"] = list(
                    {
                        content_hash(ref): deepcopy(ref)
                        for ref in [
                            *current[identity]["evidence_refs"],
                            *after[identity]["evidence_refs"],
                        ]
                    }.values()
                )
            else:
                merged[identity] = deepcopy(after[identity])
        elif identity not in after:
            if current.get(identity) != before[identity]:
                raise ValueError("withdrawing this skeleton object would discard saved detail")
            merged.pop(identity, None)
        else:
            if identity not in current:
                raise ValueError("skeleton baseline identity is absent from the current machine")
            old_record, new_record = deepcopy(before[identity]), deepcopy(after[identity])
            if material_rows is not None and identity in material_rows:
                # Row correspondence was established by the accepted MATERIAL identity map.
                # Keep the ordinary three-way merge for the table's other fields.
                old_record["materials"]["value"] = deepcopy(current[identity]["materials"]["value"])
                new_record["materials"]["value"] = deepcopy(current[identity]["materials"]["value"])
            merged[identity] = _merge(old_record, new_record, current[identity], identity)
            if material_rows is not None and identity in material_rows:
                values = [deepcopy(row["value"]) for row in material_rows[identity]]
                if values:
                    # Rows and their resolved envelope are one value. In particular,
                    # withdrawing the last skeleton report may leave independent uses.
                    envelope = merged[identity]["materials"]
                    # Legacy snapshots can have table-level support without private
                    # per-row metadata. Preserve that already-merged support at its
                    # original granularity; do not manufacture precise field sources.
                    support = evidence_refs_in_order(
                        [
                            [
                                u["evidence_ref"]
                                for u in envelope["evidence_uses"]
                                if u["role"] == "SUPPORT"
                            ],
                            material_rows[identity],
                        ]
                    )
                    refs = [
                        {"observation_id": oid, "source_observation_sha256": digest}
                        for oid, digest in support
                    ]
                    resolved = present(values, refs)
                    resolved["candidates"] = envelope["candidates"]
                    resolved["evidence_uses"].extend(
                        use for use in envelope["evidence_uses"] if use["role"] != "SUPPORT"
                    )
                    merged[identity]["materials"] = resolved
                else:
                    # The revised skeleton owns the empty-table state/reason.
                    envelope = merged[identity]["materials"]
                    merged[identity]["materials"] = {
                        **deepcopy(after[identity]["materials"]),
                        "candidates": envelope["candidates"],
                    }
    # Each group owns only its listed entities. Other groups and detail-only records stay.
    for collection in old_tree.keys() | new_tree.keys():
        removed = set(old_tree.get(collection, ()))
        retained = [i for i in tree.get(collection, ()) if i not in removed]
        tree[collection] = list(dict.fromkeys([*retained, *new_tree.get(collection, ())]))

    def expand(value: Any, collection: str = "") -> Any:
        if isinstance(value, list):
            if collection in _ENTITY_KEYS:
                return [expand(merged[i]) for i in value]
            return [expand(v) for v in value]
        if isinstance(value, dict):
            return {k: expand(v, k) for k, v in value.items()}
        return value

    result = deepcopy(dict(machine))
    result["domain_payload"] = expand(tree)
    # The verified route projections identify their own root contributions. Never
    # infer ownership from a final subject, text, or stable-key prefix.
    for field in ("unresolved_items", "coverage_assessments"):
        prior = list((old_roots or {}).get(field, ()))
        revised = list((new_roots or {}).get(field, ()))
        if prior == revised:
            continue
        retained = list(result.get(field, ()))
        for item in prior:
            if item in retained:
                retained.remove(item)
            elif field == "unresolved_items" and any(
                row["unresolved_item_id"] == item["unresolved_item_id"] for row in retained
            ):
                raise ValueError("route root contribution changed outside its verified projection")
        for item in revised:
            if item in retained:
                continue
            if field == "unresolved_items" and any(
                row["unresolved_item_id"] == item["unresolved_item_id"] for row in retained
            ):
                raise ValueError("new route root contribution collides with another contribution")
            retained.append(deepcopy(item))
        result[field] = retained
    markers = []
    for marker in result["domain_payload"].get("review_markers", []):
        if marker.get("field_key") == "materials" and "material_index" in marker:
            previous = material_position(marker["target_object_id"], marker["material_index"])
            position = (material_transitions or {}).get(previous, previous)
            if position is None:
                continue  # Marker's explicitly withdrawn material lives in the old revision.
            marker = deepcopy(marker)
            marker["target_object_id"], index = position.rsplit("#m", 1)
            marker["material_index"] = int(index)
        markers.append(marker)
    if "review_markers" in result["domain_payload"]:
        result["domain_payload"]["review_markers"] = markers
    retired = before.keys() - after.keys()
    result["unresolved_items"] = [
        item
        for item in result["unresolved_items"]
        if not (
            item["subject_id"] in retired
            and _unresolved_kind(item) in {PRODUCT_ACCOUNT_GAP, STEP_ACCOUNT_GAP}
        )
    ]
    # Only current-object absence gaps expire with a withdrawn object. Scientific
    # unresolved items, coverage and other references must still close; no silent loss.
    refs = [
        {"observation_id": oid, "source_observation_sha256": digest}
        for oid, digest in sorted(required_fact_observation_refs(result))
    ]
    result["source_observation_refs"] = refs
    result["domain_payload"]["source_observation_refs"] = refs
    _ensure_reference_closure(result)
    return result


def repair_projection_inputs(answer, **kwargs):
    """Exact dependencies of the projection below; never a scientific classifier."""
    compounds = tuple(f for f in answer.findings if f.aspect == "new_compound")
    detached = tuple(f for f in answer.findings if f.aspect == "material" and f.clear_summary)
    names = {f.finding_id for f in detached}
    handles = {f.item_id for f in compounds}
    bound = kwargs.get("bound_repairs")
    repairs = answer.skeleton_repairs if bound is None else bound
    sources_used = bool(compounds or detached or repairs)
    return (
        kwargs["machine"],
        {key: kwargs["context"].get(key) for key in (
            "groups", "block_provenance", "material_sources", "existing_observations",
            "observation_provenance")},
        compounds, detached,
        answer.segments if detached else (),
        tuple(r for r in answer.conflicts if r.finding in names),
        bound is None, repairs,
        # Sparse commands distinguish omission from an explicit default/clear.
        # Pydantic equality alone intentionally does not preserve that presence.
        tuple(tuple(change.model_dump(mode="json", exclude_unset=True)
                    for change in command.changes) for command in bound)
        if bound is not None else None,
        kwargs.get("material_sources"), kwargs.get("material_bindings"),
        kwargs.get("selections") if sources_used else None,
        kwargs.get("inherited_selections") if repairs else None,
        kwargs.get("delivered") if bound is None and repairs else None,
        kwargs.get("repair_origins") if bound is None and repairs else None,
        {key: value for key, value in (kwargs.get("finding_origins") or {}).items() if key in handles},
        {key: value for key, value in (kwargs.get("finding_births") or {}).items() if key in handles},
    )


class RepairProjectionMemo:
    """One disposable pure result; evidence verification remains with every caller.

    Results are read-only to consumers. A new candidate is built by replay's own
    copying boundary. Never pass this runtime object into publication or recovery.
    """

    def __init__(self):
        self.inputs = self.result = self.store = None

    def replay(self, answer, **kwargs):
        # Legacy snapshot commands may dereference artifacts inside their apply;
        # retain that verification instead of memoizing it with pure projection.
        if kwargs.get("bound_repairs") is None and answer.skeleton_repairs:
            return replay_skeleton_repairs(answer, **kwargs)
        inputs = repair_projection_inputs(answer, **kwargs)
        if self.result is not None and self.store is kwargs["store"] and self.inputs == inputs:
            return self.result
        result = replay_skeleton_repairs(answer, **kwargs)
        self.inputs, self.result, self.store = deepcopy(inputs), result, kwargs["store"]
        return result


def replay_skeleton_repairs(
    answer: ReadingAnswer,
    *,
    machine: Mapping[str, Any],
    context: Mapping[str, Any],
    selections: Mapping[str, Mapping[str, Any]],
    store: PrivateArtifactStore,
    delivered: set[str] | None = None,
    material_sources: Mapping[str, Any] | None = None,
    material_bindings: Mapping[str, str | None] | None = None,
    repair_origins: Mapping[str, Mapping[str, FactOrigin]] | None = None,
    bound_repairs: Sequence[BoundSkeletonRepair] | None = None,
    finding_origins: Mapping[str, FactOrigin] | None = None,
    finding_births: Mapping[str, FactOrigin] | None = None,
    inherited_selections: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
) -> RepairedReading:
    # Repair candidates may change nested material relationships. The sealed input
    # remains the baseline for withdrawal and for rejected/replaced candidates.
    current = deepcopy(reading_compound_view(answer, SelectionsHeld(selections), machine))
    bindings = (
        dict(material_bindings) if material_bindings is not None else initial_bindings(machine)
    )
    material_rows = (
        deepcopy(dict(material_sources))
        if material_sources is not None
        else (material_sources_for(machine, context.get("material_sources") or {}))
    )
    # An explicitly saved detach is visible to a subsequent skeleton withdrawal in
    # this task. Validate that intent against the same baseline, then carry only its
    # declared relationship change into the repair candidate. Other material values
    # still compile once after the repair journal. Nothing is changed on rejection.
    detached = tuple(f for f in answer.findings if f.aspect == "material" and f.clear_summary)
    if detached:
        names = {f.finding_id for f in detached}
        detach_answer = type(answer)(
            status="PARTIAL",
            explanation="Explicit summary detach before skeleton revision.",
            findings=detached,
            segments=answer.segments,
            conflicts=tuple(r for r in answer.conflicts if r.finding in names),
        )
        validate_reading(
            detach_answer, SelectionsHeld(selections), current, material_bindings=bindings
        )
        rulings = {r.finding: r for r in detach_answer.conflicts}
        selected_rows = bound_rows(current, bindings)
        for finding in detached:
            assert finding.skeleton_material_id is not None
            selected = selected_rows[finding.skeleton_material_id]
            oid, position = selected["operation_id"], selected["index"]
            operation = next(
                o
                for b in experiment_containers(current)
                for o in b["operations"]
                if o["operation_id"] == oid
            )
            value = operation["materials"]["value"][position]
            if material_value_decision(
                finding, value, machine_profile(current), rulings.get(finding.finding_id),
                selections=selections,
            )[3]:
                continue
            previous = deepcopy(value)
            value.pop("summary_material_index", None)
            value["relation_evidence_refs"] = list(
                {
                    content_hash(ref): ref
                    for ref in [
                        *value.get("relation_evidence_refs", []),
                        *_refs(SelectionsHeld(selections), finding.selection_ids),
                    ]
                }.values()
            )
            rows = material_rows.get(oid, [])
            if position < len(rows) and rows[position]["value"] == previous:
                rows[position]["value"] = deepcopy(value)
                rows[position]["fields"].pop("summary_material_index", None)
    groups = {
        str(g["route_group_id"]): deepcopy(dict(g))
        for g in context.get("groups", ())
        if "result" in g
    }
    identity = deepcopy(dict(context.get("block_provenance") or {}))
    # The public baseline and original Fusion result cite the same immutable observations
    # under different derived names. Normalize only references, by sealed provenance.
    observation_ids = {
        str(o["observation_id"]): o for o in context.get("existing_observations", ())
    }
    refs = {
        digest: {
            "observation_id": oid,
            "source_observation_sha256": content_hash(observation_ids[oid]),
        }
        for oid, digests in (context.get("observation_provenance") or {}).items()
        if oid in observation_ids
        for digest in digests
    }

    def evidence(value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"observation_id", "source_observation_sha256"}:
                return refs.get(value["source_observation_sha256"], value)
            return {k: evidence(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [evidence(v) for v in value]
        return value

    fact_positions_by_group: dict[str, dict[str, str | None]] = {}
    written_ids: set[str] = set()
    if bound_repairs is None:
        repair_batches = [(item,) for item in answer.skeleton_repairs]
    else:
        by_group: dict[str, list[BoundSkeletonRepair]] = {}
        for command in bound_repairs:
            by_group.setdefault(command.route_group_id, []).append(command)
        repair_batches = list(by_group.values())
    for batch in repair_batches:
        item = batch[0]
        gid = item.route_group_id
        if gid not in groups or gid not in identity:
            raise ValueError(
                "skeleton repair requires an exact supplied group and its identity map"
            )
        group = groups[gid]
        independent = group.get("scope_kind") == INDEPENDENT
        if independent and not machine_profile(machine).unified_expression:
            raise ValueError("independent edit scope requires the unified profile")
        project = project_route_group_state if independent else project_route_group_facts
        base = read_fusion_result(canonical_json_bytes(group["result"]))
        bind = name_binder(base)
        source_selections = dict((inherited_selections or {}).get(gid, {}))
        source_bind = reading_source_binder(base, selections) if bound_repairs is not None else bind
        for sid, selection in selections.items():
            key = source_bind(sid)
            if key in source_selections and any(
                source_selections[key].get(k) != selection.get(k) for k in ("exact_text", "evidence_ref")
            ):
                raise ValueError("current source differs from the original accepted selection")
            source_selections[key] = {**selection, "location": sid}
        for command in batch:
            if not isinstance(command, BoundSkeletonRepair):
                continue
            for sid, selection in command.delivered_text.items():
                if sid in source_selections and any(
                    source_selections[sid].get(k) != selection.get(k) for k in ("exact_text", "evidence_ref")
                ):
                    raise ValueError("source selection differs from its accepted command")
                source_selections[sid] = selection
        old = project(
            route_group_id=gid,
            result=base,
            paper_id=str(machine["paper_id"]),
            profile=machine_profile(machine),
            source_selections=source_selections,
            public_targets=current,
        )
        old_ids = {
            pid: identity[gid][fid]
            for fid, pid in old.fusion_public_ids.items()
            if fid in identity[gid]
        }
        # The sealed group projects its route identity; public keying preserves it.
        # Shared/empty step membership is not evidence of route ownership.
        routes = [
            route
            for route in current["domain_payload"]["routes"]
            if route["route_id"] == old.route["route_id"]
        ]
        if not independent and len(routes) != 1:
            raise ValueError("skeleton repair requires its exact sealed route identity")
        if not independent:
            old_ids[old.route["route_id"]] = routes[0]["route_id"]
        reverse = {pid: fid for fid, pid in identity[gid].items()}
        if isinstance(item, BoundSkeletonRepair):
            # Fold every surviving command for this sealed group before projecting.
            # Intermediate counterfactual states may lack a later independent definition.
            sources = dict(base.sources)
            births: dict[str, FactOrigin] = {}
            for command in batch:
                for sid, evidence_refs in command.sources.items():
                    if sid in sources and sources[sid] != tuple(evidence_refs):
                        raise ValueError("bound repair source differs from its sealed identity")
                    sources[sid] = tuple(evidence_refs)
                targets = {change.fact_id for change in command.changes}
                for fid, birth in command.proven_births.items():
                    if fid not in targets:
                        continue  # An unrelated historical snapshot supplies no support.
                    if fid in births and births[fid] != birth:
                        raise ValueError("bound repair has conflicting historical births")
                    births[fid] = birth
            base = base.model_copy(update={"sources": sources})
            accepted = [command.changes for command in batch]
            required = final_fact_keys(base, accepted)
            base, bridged = prepare_finding_bridges(
                base,
                (),
                answer=answer,
                current=current,
                selections=selections,
                identity=identity[gid],
                store=store,
                finding_origins=finding_origins or {},
                finding_births=finding_births,
                required_keys=required,
            )
            updated = fold_bound_fact_commands(
                base,
                [(command.changes, command.origins) for command in batch],
                proven_births=births,
                delivered_texts=[command.delivered_text for command in batch],
                machine_profile=machine_profile(machine),
                complete_characterization=machine_profile(machine).expression,
            )
            verify_surviving_reference_births(updated, batch)
        else:
            raw = item.repair.model_dump(mode="json", exclude_unset=True)
            recorded_births = (repair_origins or {}).get(item.item_id, {})
            raw = fusion_names(
                base,
                locations={},
                history=(),
                readers=(),
                route_group_id=gid,
                read=group.get("reads", {}),
            ).resolve(raw)
            # Track actual submitted children through name resolution and any explicit
            # compound bridge insertion; a bridge is not a model-authored repair child.
            child_births = {
                id(change): recorded_births[original.fact_id]
                for original, change in zip(item.repair.changes, raw.get("changes", ()), strict=True)
                if original.fact_id in recorded_births
            }
            sources = dict(base.sources)
            texts: dict[str, Mapping[str, Any]] = {}
            allowed = set(selections) if delivered is None else delivered
            for sid in allowed:
                if sid in selections:
                    sources[bind(sid)] = (dict(selections[sid]["evidence_ref"]),)
                    texts[bind(sid)] = {**selections[sid], "location": sid}
            base = base.model_copy(update={"sources": sources})
            bridged: dict[str, str] = {}
            declared = {c.get("fact_id") for c in raw.get("changes", ())}
            referenced = {
                v
                for c in raw.get("changes", ())
                for key in ("inputs", "outputs", "owners", "after")
                for v in (c.get(key) or ())
            }
            for _finding, cid, fid, bridge in reading_compound_bridges(
                answer, current=current, scope_machine=machine, selections=selections, bind=bind,
            ):
                if cid not in reverse and cid in (referenced | declared):
                    reverse[cid] = fid
                    bridged[fid] = cid
                    # Only bridge an explicitly accepted compound; no name-based guessing.
                    raw.setdefault("changes", []).insert(0, bridge)
            for change in raw.get("changes", ()):
                bind_fact_change_names(
                    change, subject=lambda token: reverse.get(token, token),
                    reference=lambda token: reverse.get(token, token), source=bind,
                )
            updated, replies, accepted = apply_sparse_submission(
                base,
                {"groups": [raw]},
                locations={},
                store=store,
                last_shown=base,
                complete_characterization=machine_profile(machine).expression,
                machine_profile=machine_profile(machine),
                validate_questions=lambda proposal: None,
                public_targets=True,
                delivered_source_ids={bind(s) for s in allowed},
                inherited_source_ids=frozenset(s for f in base.facts for s in fact_source_ids(f)),
                delivered_text=texts,
            )
            if any(r["outcome"] == "REJECTED" for r in replies):
                raise ValueError("Fusion rejected skeleton repair: " + str(replies))
            # Only verified saved repair children prove birth. Do not reuse their first
            # action as the origin of subsequently edited scientific field values.
            creation_origins = dict(updated.fact_creation_origins)
            existing = {fact.fact_id for fact in base.facts}
            for raw_change, applied in zip(
                raw.get("changes", ()), (change for batch in accepted for change in batch), strict=True
            ):
                if applied.fact_id not in existing and id(raw_change) in child_births:
                    creation_origins[applied.fact_id] = child_births[id(raw_change)]
            updated = updated.model_copy(update={"fact_creation_origins": creation_origins})
        if independent and any(
            fact.kind == "REACTION" and fact.status != "REJECTED" for fact in updated.facts
        ):
            raise ValueError(
                "Independent experiments have no route: use EXPERIMENT for the account, "
                "or select the existing route editing scope for a REACTION."
            )
        new = project(
            route_group_id=gid,
            result=updated,
            paper_id=str(machine["paper_id"]),
            profile=machine_profile(machine),
            source_selections=source_selections,
            public_targets=current,
        )
        mapped = {**identity[gid], **bridged}
        for fid in new.fusion_public_ids:
            mapped.setdefault(
                fid, new.fusion_public_ids[fid] if machine_profile(machine).unified_expression
                else "REPAIR-" + content_hash([gid, fid])[:24]
            )
        new_ids = {pid: mapped[fid] for fid, pid in new.fusion_public_ids.items()}
        if len(set(new_ids.values())) != len(new_ids):
            raise ValueError("skeleton repair has ambiguous identity correspondence")
        if not independent:
            new_ids[new.route["route_id"]] = routes[0]["route_id"]
        fact_positions = fact_positions_by_group.setdefault(
            gid,
            {
                fid: bindings.get(material_position(old_ids.get(oid, oid), index))
                for fid, (oid, index) in old.material_fact_rows.items()
            },
        )
        material_values, transitions, material_rows, final_facts = material_repair_values(
            current,
            replace(
                old,
                reaction_blocks=tuple(evidence(old.reaction_blocks)),
                experimental_accounts=tuple(evidence(old.experimental_accounts)),
                material_sources=evidence(old.material_sources),
            ),
            replace(
                new,
                reaction_blocks=tuple(evidence(new.reaction_blocks)),
                experimental_accounts=tuple(evidence(new.experimental_accounts)),
                material_sources=evidence(new.material_sources),
            ),
            old_ids,
            new_ids,
            fact_positions,
            material_rows,
        )
        before_repair = current
        current = overlay_projection(
            current,
            evidence(_records(old, old_ids, include_route=not independent)),
            evidence(_records(new, new_ids, include_route=not independent)),
            material_rows={oid: material_rows[oid] for oid in material_values},
            material_transitions=transitions,
            old_roots=evidence(rewrite({
                "unresolved_items": list(old.unresolved_items),
                "coverage_assessments": list(old.coverage_assessments),
            }, old_ids)),
            new_roots=evidence(rewrite({
                "unresolved_items": list(new.unresolved_items),
                "coverage_assessments": list(new.coverage_assessments),
            }, new_ids)),
        )
        bindings = advance_bindings(bindings, before_repair, current, transitions)
        fact_positions_by_group[gid] = dict(final_facts)
        material_rows = overlay_material_sources(
            material_rows,
            replace(old, material_sources=evidence(old.material_sources)),
            replace(new, material_sources=evidence(new.material_sources)),
            old_ids,
            new_ids,
            list(experiment_containers(current)),
            handled_operations=frozenset(material_values),
        )
        # Application locations are derived after identity mapping. Relations can
        # move without changing either their own birth or an account's identity.
        destinations = {
            **{fid: old_ids.get(pid, pid) for fid, pid in old.relation_destinations.items()},
            **{fid: new_ids.get(pid, pid) for fid, pid in new.relation_destinations.items()},
        }
        affected = {**mapped, **destinations}
        written_ids.update(
            affected[change.fact_id]
            for batch in accepted
            for change in batch
            if change.fact_id in affected
        )
        identity[gid] = mapped
        group["result"] = updated.model_dump(mode="json")
        if bound_repairs is not None:
            # Reuse the group's existing source-name inventory for private keys.
            # Only navigation metadata is copied; original words/evidence remain
            # authoritative in the accepted source receipt.
            reads = dict(group.get("reads", {}))
            for command in batch:
                for sid, selection in command.delivered_text.items():
                    source = selection.get("source_identity", {})
                    if "source_id" in source:
                        text = selection.get("exact_text") is not None
                        reads[sid] = {
                            "modality": "TEXT" if text else "VISUAL",
                            "canonical_source_id": source["source_id"],
                            **{key: source[key] for key in ("start_sentence", "end_sentence") if key in source},
                            **({"region": dict(zip(("x0", "y0", "x1", "y1"), source["region"], strict=True))}
                               if not text and source.get("region") else {}),
                        }
            group["reads"] = reads
    subjects: dict[str, set[tuple[str, str]]] = {}
    for command in bound_repairs or ():
        subjects.setdefault(command.item_id, set()).update(
            (command.route_group_id, change.fact_id) for change in command.changes
        )
    return RepairedReading(
        current, tuple(groups.values()), identity, frozenset(written_ids), material_rows, bindings,
        {key: tuple(sorted(values)) for key, values in subjects.items()},
    )
