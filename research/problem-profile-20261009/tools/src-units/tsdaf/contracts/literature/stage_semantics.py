"""Public stages, procedure-source relationships and their deterministic binding.

Stage identity comes from a cited source boundary, independent of the model's stage
name or explanation. Ordering follows verified source coordinates; unverified
boundaries remain explicit. Model interpretations retain their own evidence.

The v1.3 expression profile keeps each declared procedure source with its scope,
optional stage, relation and adoption. A unique explicit target is needed to support
or replace an existing source; the host never guesses one from explanatory prose.
Own stages take precedence over following a unique whole-operation source. Earlier
profiles retain their legacy scalar-source compilation and coverage behavior.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from typing import Any, Protocol, cast

from tsdaf.contracts.literature.expressions import (
    ConditionAdoption,
    SourcePrimary,
    SourceRelation,
    expressed_value,
)
from tsdaf.contracts.literature.objects import experiment_containers
from tsdaf.contracts.literature.procedure_targets import procedure_target_kind
from tsdaf.contracts.literature.public_ids import public_id

__all__ = (
    "STABLE_KEY_INFIX",
    "STAGE_ID_PREFIX",
    "boundary_hash_of",
    "compile_operation_stages",
    "coverage_status",
    "display_order",
    "effective_stages",
    "stage_id_for",
    "stage_stable_key",
)

STAGE_ID_PREFIX = "STAGE"
STABLE_KEY_INFIX = "|STAGE:"
# The closed sets a gap's `kind` and a `procedure_source`'s `kind` come from are the
# contract's, declared once in `fact_schema_v1_1.json`; this module emits values and
# does not restate the vocabulary. A fifth gap kind for "these words may be two
# transformations" is not added: that question is its own investigation and has no real
# sample yet, and `MISSING_BOUNDARY`'s free-text explanation is its input (ruling R1).
ORDER_UNRESOLVED = "ORDER_UNRESOLVED"

COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
UNRESOLVED = "UNRESOLVED"
VERIFIED = "VERIFIED"


class Ledgers(Protocol):
    """What the publication's private record offers this module.

    Structural, not imported: the ledger is a lane object and the contract layer
    depends on nothing outside itself. The three attributes are the ones
    `stage_ledger.EnrichmentLedgers` fills while a round's receipts are applied.
    """

    stages: dict[str, list[dict[str, Any]]]
    elisions: dict[str, dict[str, Any]]
    unassigned: list[dict[str, Any]]


def stage_stable_key(operation_stable_key: str, boundary_hash: str) -> str:
    """The stage's readable key: the operation's, and the boundary it begins at.

    The name a task gave the stretch is not in it. Two tasks that called one sentence
    `post_addition` and `stage-78C` declared one stage, and a task that rewords its
    own name on a later revision does not mint a second one (E66, E157).
    """
    return f"{operation_stable_key}{STABLE_KEY_INFIX}{boundary_hash}"


def stage_id_for(operation_stable_key: str, boundary_hash: str) -> str:
    """The stage's public id, from its key by the one rule (`public_ids.public_id`)."""
    return public_id(STAGE_ID_PREFIX, stage_stable_key(operation_stable_key, boundary_hash))


def boundary_hash_of(stage: Mapping[str, Any]) -> str:
    """The boundary hash a published stage carries, read back off its key.

    This is how the next round re-mounts its ledger from what the last one published:
    the private ledger's identity is the public key's tail, so a stage declared again
    in a later revision meets the one already published instead of minting a second.
    """
    key = str(stage["stable_key"])
    head, _, tail = key.rpartition(STABLE_KEY_INFIX)
    if not head:
        raise ValueError("a published stage's stable_key must carry its boundary hash")
    return tail


def _site_key(stage: Mapping[str, Any]) -> tuple[str, int] | None:
    """Where this stage begins, as the document reads: the block and the character.

    Block ids are `P{page}-B{block}`, so their lexical order is the package's reading
    order -- a property of the package format, not a new field. Two stretches declared
    from one selection differ here and nowhere else, which is why the site is what the
    order is read from (f07's B000 declared two stages citing one selection).
    """
    boundary = stage.get("boundary")
    if not isinstance(boundary, Mapping):
        return None
    return (str(boundary["block_id"]), int(boundary["char_start"]))


def display_order(stages: Sequence[Mapping[str, Any]]) -> tuple[int | None, ...]:
    """Each stage's `display_index`, in the order given: 1-based over the stages whose
    boundary was found in the source, `None` for the rest (R3)."""
    placed = sorted(
        (site, index)
        for index, stage in enumerate(stages)
        if (site := _site_key(stage)) is not None
    )
    order: dict[int, int] = {index: rank for rank, (_, index) in enumerate(placed, start=1)}
    return tuple(order.get(index) for index in range(len(stages)))


def effective_stages(
    operation: Mapping[str, Any], all_operations: Iterable[Mapping[str, Any]]
) -> tuple[Mapping[str, Any], ...]:
    """The stretches that apply to this operation, following an elision by identity.

    An operation that says "as above" publishes no stages of its own; a consumer that
    reads `operation["stages"]` on it sees an empty array and would record it as a
    procedure with nothing in it. Here the elision is followed once, to the operation
    it named. A general-methods unit or a citation outside the document has no public
    operation to follow, so there is nothing to return and the consumer is meant to
    read `procedure_source` itself (R4).
    """
    source = operation.get("procedure_source")
    if "procedure_sources" in operation:
        if operation.get("stages"):
            return tuple(operation["stages"])
        candidates = [
            row
            for row in operation["procedure_sources"]
            if row["extent"] == "WHOLE_OWNERS" and row["adoption"] in {"ADOPTED", "SUPPORTING"}
        ]
        targets = {
            (row["kind"], row.get("operation_id"), row.get("unit_block_id")) for row in candidates
        }
        if len(targets) != 1:
            return tuple(operation.get("stages") or ())
        source = candidates[0]
    if not isinstance(source, Mapping):
        return tuple(operation.get("stages") or ())
    if source.get("kind") != "OPERATION":
        return ()
    target = source.get("operation_id")
    for other in all_operations:
        if other.get("operation_id") == target:
            return tuple(other.get("stages") or ())
    return ()


def coverage_status(
    stages: Sequence[Mapping[str, Any]],
    procedure_source: Mapping[str, Any] | None,
    unassigned: Sequence[Mapping[str, Any]],
    gaps: Sequence[Mapping[str, Any]],
) -> str:
    """The three states, in one place. An operation nobody divided and that elides to
    nobody has said nothing about its procedure's shape, which is not the same as
    saying it has one stretch."""
    if procedure_source is not None and procedure_source.get("kind") == "UNRESOLVED":
        return UNRESOLVED
    if not stages and procedure_source is None:
        return UNRESOLVED
    if gaps or unassigned:
        return PARTIAL
    return COMPLETE


def _public_stage(entry: Mapping[str, Any], operation_stable_key: str) -> dict[str, Any]:
    boundary_hash = str(entry["anchor_hash"])
    sites = [site for site in entry.get("sites") or () if site]
    boundary: dict[str, Any] | None = None
    if sites:
        package, block, char_start, _ = min(sites)
        boundary = {
            "package_sha256": str(package),
            "block_id": str(block),
            "char_start": int(char_start),
        }
    return {
        "stage_id": stage_id_for(operation_stable_key, boundary_hash),
        "stable_key": stage_stable_key(operation_stable_key, boundary_hash),
        "display_index": None,
        "description": str(entry["stage_verbatim"]),
        "verification": str(entry["verification"]),
        "boundary": boundary,
        "boundary_evidence_refs": [dict(ref) for ref in entry["boundary_evidence_refs"]],
        **(
            {"model_interpretations": deepcopy(entry["model_interpretations"])}
            if entry.get("model_interpretations")
            else {}
        ),
    }


def procedure_readings(operation: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One forward representation; a legacy scalar retains its declared whole scope.

    Migration does not invent the model's relation, explanation or lost local scope.
    Simultaneous authorities are rejected rather than silently choosing one.
    """
    if "procedure_sources" in operation and "procedure_source" in operation:
        raise ValueError("procedure sources have two independent authorities")
    if "procedure_sources" in operation:
        return deepcopy(operation["procedure_sources"])
    old = operation.get("procedure_source")
    return [{**deepcopy(old), "extent": "WHOLE_OWNERS", "adoption": "ADOPTED"}] if old else []


def apply_procedure_reading(
    operation: Mapping[str, Any],
    incoming: Mapping[str, Any],
    *,
    relation: SourceRelation,
    primary: SourcePrimary | None,
    against: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Keep each explicit source. A scientific ruling is never a contribution ID.

    Only a unique established whole or named-stage source is a determinate correction
    target. An unlocated PART cannot identify which old contribution it replaces.
    """
    rows = procedure_readings(operation)
    row = deepcopy(dict(incoming))
    row["relation"] = relation
    if primary is not None:
        row["ruling"] = primary
    baseline = procedure_readings(operation if against is None else against)
    candidates = [
        old
        for old in baseline
        if old["adoption"] in {"ADOPTED", "SUPPORTING"}
        and old["extent"] == row["extent"]
        and old.get("stage_id") == row.get("stage_id")
        and (row["extent"] == "WHOLE_OWNERS" or row.get("stage_id") is not None)
    ]

    def target(record: Mapping[str, Any]) -> tuple[Any, ...]:
        return (record["kind"], record.get("operation_id"), record.get("unit_block_id"),
                record.get("target_source_document_id"), record.get("target_source_revision_id"))

    targets = {target(old) for old in candidates}
    # A bare EXTERNAL flag carries no identity for two external references.
    unique = len(targets) == 1 and (
        next(iter(targets))[0] != "EXTERNAL"
        or sum(old["adoption"] == "ADOPTED" for old in candidates) == 1
    )
    displaced: list[dict[str, Any]] = []
    role: ConditionAdoption = "ADOPTED"
    if relation == "SAME":
        matching = [old for old in candidates if target(old) == target(row)]
        match_unique = bool(matching) and (
            row["kind"] != "EXTERNAL" or sum(old["adoption"] == "ADOPTED" for old in matching) == 1
        )
        if match_unique:
            role = "SUPPORTING"
        else:
            role = "UNRESOLVED"
            row["resolution_note"] = (
                "The declared SAME source has no uniquely matching whole or named-stage "
                "source in the supplied skeleton. Existing source readings retain their roles."
            )
    elif relation == "CONTRADICTS":
        if primary == "SKELETON":
            role = "ALTERNATIVE"
        elif unique and primary == "FINDING":
            displaced = candidates
        else:
            role = "UNRESOLVED"
            row["resolution_note"] = (
                "The declared contradiction has no unique existing whole or named-stage "
                "source to replace. Existing source readings retain their roles."
            )
    if row["kind"] == "UNRESOLVED":
        role, displaced = "UNRESOLVED", []
    row["adoption"] = role
    # Only the full public meaning is idempotent. A new task may have a different
    # skeleton: identical words must not inherit an earlier task's unresolved role.
    if row in rows:
        return rows
    for old in rows:
        if any(
            all(old.get(k) == v for k, v in candidate.items() if k != "adoption")
            for candidate in displaced
        ):
            old["adoption"] = "ALTERNATIVE"
    rows.append(row)
    return rows


def procedure_source_value(entry: Mapping[str, Any]) -> dict[str, Any]:
    """The elision as the contract publishes it: which of the three it is, and the id
    it named. `register_elision` drops the fields the write left unset, so which key
    survived is what the task chose (E157)."""
    kind = procedure_target_kind(entry, allow_unresolved=True)
    operation_id = entry.get("referenced_operation_id")
    unit_block_id = entry.get("referenced_unit_block_id")
    result = {
        "kind": kind,
        "operation_id": operation_id,
        "unit_block_id": unit_block_id,
        "evidence_refs": [dict(ref) for ref in entry.get("evidence_refs") or ()],
    }

    if kind == "GENERAL_METHODS_UNIT":
        for key in ("target_source_document_id", "target_source_revision_id"):
            if entry.get(key) is not None:
                result[key] = entry[key]
    return result


def _unassigned_fact(entry: Mapping[str, Any]) -> dict[str, Any]:
    """A whole-operation value a stretch displaced, in the author's own words and with
    the collection or field it was written through -- visibly, rather than filed over
    what the stretches say or dropped (E157)."""
    category_word = expressed_value(entry["category_verbatim"])
    text = expressed_value(entry["text"])
    if not isinstance(category_word, str) or not isinstance(text, str):
        raise ValueError("unassigned fact requires known condition words; preserve its source note")
    return {
        "category": entry.get("category"),
        "category_verbatim": category_word,
        "text": text,
        "about": entry.get("about"),
        "source": str(entry["source"]),
        "evidence_refs": [dict(ref) for ref in entry.get("evidence_refs") or ()],
    }


def rekey_public_stage_references(
    machine: Mapping[str, Any],
    id_map: Mapping[str, str] | None = None,
    *,
    stage_id_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Change owner-derived identities and their references together, never meanings.

    The published boundary is sufficient even without a private ledger. No note text,
    array position or source similarity participates in identifying its stage.
    """
    result = deepcopy(dict(machine))
    for marker in result["domain_payload"].get("review_markers", ()):
        if (
            marker.get("target_object_type") == "OPERATION"
            and marker.get("field_key") == "procedure_source"
        ):
            marker["field_key"] = "procedure_sources"
    for block in experiment_containers(result):
        for operation in block["operations"]:
            if "procedure_source" in operation or "procedure_sources" in operation:
                operation["procedure_sources"] = procedure_readings(operation)
                operation.pop("procedure_source", None)
                for source in operation["procedure_sources"]:
                    if source.get("operation_id") is not None:
                        source["operation_id"] = (id_map or {}).get(
                            source["operation_id"], source["operation_id"]
                        )
            parent = str(operation.get("stable_key") or operation["operation_id"])
            mapping = {}
            for stage in operation.get("stages", []):
                anchor = boundary_hash_of(stage)
                target = stage_id_for(parent, anchor)
                mapping[stage["stage_id"]] = target
                if stage_id_map is not None:
                    stage_id_map[stage["stage_id"]] = target
                stage["stage_id"] = target
                stage["stable_key"] = stage_stable_key(parent, anchor)
            # The same operation-local, simultaneous map binds explicit order
            # edges. Display order and source wording never invent precedence.
            for edge in operation.get("stage_precedence", ()):
                for endpoint in ("before_stage_id", "after_stage_id"):
                    edge[endpoint] = mapping.get(edge[endpoint], edge[endpoint])
            rows = list(operation.get("other_conditions", []))
            rows.extend(operation.get("procedure_sources", []))
            materials = operation["materials"]
            for values in [
                materials.get("value"),
                *(c.get("value") for c in materials.get("candidates", [])),
            ]:
                if isinstance(values, list):
                    rows.extend(values)
            for row in rows:
                if "stage_id" in row:
                    row["stage_id"] = mapping.get(row["stage_id"], row["stage_id"])
            for gap in operation.get("stage_coverage", {}).get("gaps", []):
                gap["stage_ids"] = [mapping.get(sid, sid) for sid in gap["stage_ids"]]
    return result


def compile_operation_stages(
    machine: Mapping[str, Any],
    ledgers: Ledgers | None,
    id_map: Mapping[str, str],
    *,
    rekey_relations: bool = False,
    stage_id_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The machine with every operation the ledger has something about carrying its
    four v1.1 items. Operations nobody divided are left exactly as they are.

    Called from `finalize_public_result` after `rekey_machine` and before the
    observations are keyed, so a stage hangs on the operation's final stable key and
    its evidence is rewritten with the rest of the result's (plan §10.1). `id_map` is
    that pass's own record of which provisional id became which public one; the ledger
    holds the ids the receipts were applied under.
    """
    result = (
        rekey_public_stage_references(machine, id_map, stage_id_map=stage_id_map)
        if rekey_relations
        else deepcopy(dict(machine))
    )
    if ledgers is None:
        return result

    def resolved(operation_id: str) -> str:
        return id_map.get(operation_id, operation_id)

    # Accumulated, not assigned: `rekey_machine` can give two records it merged one
    # public id, and a ledger entry of either of them belongs to the operation that
    # came out of the merge.
    staged: dict[str, list[Mapping[str, Any]]] = {}
    for declared_on, declared in ledgers.stages.items():
        staged.setdefault(resolved(declared_on), []).extend(declared)
    elided = (
        {} if rekey_relations else {resolved(op): entry for op, entry in ledgers.elisions.items()}
    )
    displaced: dict[str, list[Mapping[str, Any]]] = {}
    for entry in ledgers.unassigned:
        displaced.setdefault(resolved(str(entry["operation_id"])), []).append(entry)

    # Apply the same stage ledger to both kinds of experiment. Their supplied
    # collection membership and scientific ownership remain unchanged.
    for block in experiment_containers(result):
        operations = []
        for operation in block["operations"]:
            operation_id = str(operation["operation_id"])
            entries = staged.get(operation_id, [])
            elision = elided.get(operation_id)
            unassigned = displaced.get(operation_id, [])
            if (
                not entries
                and elision is None
                and not unassigned
                and not operation.get("procedure_sources")
            ):
                operations.append(operation)
                continue
            parent_key = str(operation.get("stable_key") or operation_id)
            stages = [_public_stage(entry, parent_key) for entry in entries]
            for stage, index in zip(stages, display_order(stages), strict=True):
                stage["display_index"] = index
            stages.sort(
                key=lambda s: (s["display_index"] is None, s["display_index"] or 0, s["stable_key"])
            )
            gaps = [
                {
                    "kind": ORDER_UNRESOLVED,
                    "stage_ids": [stage["stage_id"]],
                    "explanation": (
                        "The author's words for this stretch were not found in what the "
                        "task cited, so where it stands in the procedure is undetermined."
                    ),
                    "evidence_refs": list(stage["boundary_evidence_refs"]),
                }
                for stage in stages
                if stage["verification"] != VERIFIED
            ]
            source = procedure_source_value(elision) if elision is not None else None
            if source is not None and source["operation_id"] is not None:
                source["operation_id"] = resolved(source["operation_id"])
            facts = [_unassigned_fact(entry) for entry in unassigned]
            enriched = dict(operation)
            enriched["stages"] = stages
            enriched["stage_coverage"] = {
                "status": coverage_status(
                    stages,
                    source
                    if source is not None
                    else next(
                        (
                            row
                            for row in operation.get("procedure_sources", ())
                            if row["extent"] == "WHOLE_OWNERS"
                            and row["adoption"] in {"ADOPTED", "SUPPORTING"}
                        ),
                        None,
                    ),
                    facts,
                    gaps,
                ),
                "gaps": gaps,
            }
            enriched["unassigned_facts"] = facts
            if source is not None:
                enriched["procedure_source"] = source
            operations.append(enriched)
        cast(dict[str, Any], block)["operations"] = operations
    return result
