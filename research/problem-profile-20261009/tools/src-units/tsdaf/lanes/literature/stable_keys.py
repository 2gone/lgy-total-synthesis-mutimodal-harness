"""Public ids from author anchors or proven host creations (E66, E308).

The projection builds its records under provisional ids; this pass rewrites every id
from a *stable key* spelled only from public fields: the paper, the target's author
label as the route group, the compounds' author labels, the blocks' substrate and
product labels, an operation's index in its block, a card's method, a narrative's
own words. Proven host creation keys take precedence for accounts, operations,
materials, measurements, narratives and unresolved questions. Measurement membership
is independent of identity; no mutable wording or route position invents an origin.

Each record also publishes the key (`stable_key`) and what it rests on (`key_basis`):
AUTHOR_LABEL for the author's numbering, AUTHOR_TEXT for the author's words that the
Host verified against the source, DERIVED_ORDINAL for a sequence the
Host had to assign (also used for host-created identities). Only the first two
are author-grounded links across independent runs.

The same pass merges records that share a key within one status class (a compound the
route group and a window each wrote), turns a disagreement inside a merge into a
field conflict rather than an overwrite, and orders every public list by public
fields, so the bytes of a result do not depend on the order facts were saved in.
"""

from __future__ import annotations

import copy
import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from tsdaf.contracts.literature.expressions import (
    EXPRESSION_SETS,
    merge_interpretations,
    rekey_source_readings,
)
from tsdaf.contracts.literature.public_ids import public_id
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.literature.author_labels import (
    author_label_key,
    author_label_signs_disagree,
)
from tsdaf.lanes.literature.author_words import text_contains
from tsdaf.lanes.literature.characterization_verification import characterization_verification
from tsdaf.lanes.literature.condition_categories import fold_text
from tsdaf.lanes.literature.material_bindings import material_position
from tsdaf.lanes.literature.material_meanings import MATERIAL_ROLES

# The projection dataclass (`fact_projection.RouteGroupFactProjection`) is not imported:
# that module calls this one, and the pass only needs the record fields it names, so
# it takes and returns the dataclass as `Any`.

__all__ = ("AUTHOR_LABEL", "AUTHOR_TEXT", "DERIVED_ORDINAL", "public_id", "rekey", "rekey_machine")

AUTHOR_LABEL = "AUTHOR_LABEL"
AUTHOR_TEXT = "AUTHOR_TEXT"
DERIVED_ORDINAL = "DERIVED_ORDINAL"

# Materials of one operation sort by role in the role table's order, which is the
# contract's; a retyped copy of it was a third definition of the vocabulary.
_MATERIAL_ROLE_ORDER = tuple(MATERIAL_ROLES)


def _digest(text: Any) -> str:
    value = fold_text(text).encode() if isinstance(text, str) else canonical_json_bytes(text)
    return hashlib.sha256(value).hexdigest()[:16]


def _remap_references(value: Any, ids: Mapping[str, str], *, reference: bool = False) -> Any:
    """Only identity fields change; ResolvedValue candidates inherit their field's role.

    A prose value equal to an old ID is still prose. This also traverses source
    readings and interpretation wrappers without rewriting their quoted text.
    """
    if isinstance(value, str):
        return ids.get(value, value) if reference else value
    if isinstance(value, list):
        return [_remap_references(v, ids, reference=reference) for v in value]
    if isinstance(value, dict):
        return {
            field: _remap_references(
                child,
                ids,
                reference=(
                    field.endswith(("_id", "_ids"))
                    or reference
                    and field in {"value", "candidates"}
                ),
            )
            for field, child in value.items()
        }
    return value


def _creation_key(
    record: Mapping[str, Any], field: str, keys: Mapping[str, str] | None
) -> str | None:
    supplied = (keys or {}).get(str(record.get(field, "")))
    if supplied:
        return supplied
    stable = record.get("stable_key")
    # A previously published creation retains its identity without inventing an origin.
    return stable if isinstance(stable, str) and ":CREATED:" in stable else None


def _material_identities(
    operation: dict[str, Any], keys: _Keys, creation_keys: Mapping[str, str] | None
) -> None:
    rows = _value(operation.get("materials"))
    if not isinstance(rows, list):
        return
    for index, row in enumerate(rows, 1):
        old = row.get("material_use_id")
        position = material_position(operation["operation_id"], index)
        created = (creation_keys or {}).get(old or position)
        if created:
            identity = public_id("MAT", created)
            row["material_use_id"] = identity
            keys.ids[old or position] = identity
        elif old:
            keys.ids[old] = old


def public_creation_key(paper_id: str, prefix: str, origin: Mapping[str, str]) -> str:
    """A host-proven creation, independent of the object's mutable scientific text."""
    digest = hashlib.sha256(canonical_json_bytes(dict(origin))).hexdigest()
    return f"{paper_id}|{prefix}:CREATED:{digest}"


def _value(field: Mapping[str, Any] | None) -> Any:
    if isinstance(field, Mapping) and "state" in field:
        return field.get("value") if field.get("state") == "PRESENT" else None
    return field


def _sorted_refs(refs: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    unique = {(r["observation_id"], r["source_observation_sha256"]): dict(r) for r in refs}
    return [unique[k] for k in sorted(unique)]


class _Keys:
    """Assigns keys and derived ids, resolving collisions with an ordinal suffix that
    marks the record DERIVED_ORDINAL; remembers old id -> new id for the references."""

    def __init__(self) -> None:
        self.taken: dict[str, int] = {}
        self.ids: dict[str, str] = {}

    def assign(
        self, record: dict[str, Any], id_field: str, prefix: str, key: str, basis: str
    ) -> str:
        seen = self.taken.get(key, 0)
        self.taken[key] = seen + 1
        if seen:
            key, basis = f"{key}#{seen + 1}", DERIVED_ORDINAL
        new_id = public_id(prefix, key)
        old_id = record.get(id_field)
        if isinstance(old_id, str):
            self.ids[old_id] = new_id
        record[id_field] = new_id
        record["stable_key"] = key
        record["key_basis"] = basis
        return key


def _verified(
    text: str | None, refs: Iterable[Mapping[str, str]], texts: Mapping[str, str] | None
) -> bool | None:
    """Whether the author's words are found in one of the cited observations, on the
    rule every quote is held to (`text_contains`): None when the check cannot run."""
    if texts is None or not text:
        return None
    return any(
        ref["observation_id"] in texts and text_contains(texts[ref["observation_id"]], text)
        for ref in refs
    )


def rekey(
    projection: Any,
    *,
    paper_id: str,
    observation_texts: Mapping[str, str] | None = None,
    characterization_texts: Mapping[str, str] | None = None,
    furniture: Sequence[str] = (),
    id_map: dict[str, str] | None = None,
    creation_keys: Mapping[str, str] | None = None,
) -> Any:
    """`id_map`, when given, is filled with every old id -> new id the pass decided,
    for a caller that holds references outside the projection (`rekey_machine`).

    `furniture` is the page's own printing of the packages `characterization_texts`
    was recovered from (`evidence.furniture_by_package`): the card verification that
    decides between AUTHOR_TEXT and DERIVED_ORDINAL sets it aside before comparing,
    so a quote made from a cleaned sentence still matches its source (E140)."""
    compounds = [dict(c) for c in projection.compounds]
    blocks = copy.deepcopy(list(projection.reaction_blocks))
    accounts = copy.deepcopy(list(getattr(projection, "experimental_accounts", ())))
    unassigned = copy.deepcopy(list(getattr(projection, "unassigned_outcomes", ())))
    route = dict(projection.route)
    narratives = [dict(n) for n in projection.paper_narratives]
    cards = [dict(c) for c in projection.characterization_records]
    unresolved = [dict(u) for u in projection.unresolved_items]
    conflicts = [dict(c) for c in projection.field_conflicts]
    keys = _Keys()
    if id_map is not None:
        keys.ids = id_map

    # -- the route group: the target's label ---------------------------------------
    by_old_id = {c["compound_id"]: c for c in compounds}
    target_label: str | None = None
    for path in route.get("target_paths", ()):
        target = by_old_id.get(path.get("target_compound_id"))
        if target and target.get("author_labels"):
            target_label = author_label_key(target["author_labels"])
            break
    route_derived = target_label is None
    group = (
        f"{paper_id}|RG:{target_label}"
        if target_label
        else f"{paper_id}|RG:~{projection.route_group_id}"
    )

    def basis(kind: str) -> str:
        return DERIVED_ORDINAL if route_derived else kind

    # -- compounds: key by the complete author-label identity --------------------
    def label_key(record: dict[str, Any]) -> str | None:
        labels = record.get("author_labels") or []
        return author_label_key(labels)

    def downstream_anchor(record: dict[str, Any]) -> tuple[str, int] | None:
        """The nearest labeled compound downstream along the route and the number of
        steps to it: an unlabeled intermediate's place in the author's numbering
        ("two steps before 39"). Stable as long as the route between them is."""
        frontier = [(record, 0)]
        seen = {record["compound_id"]}
        while frontier:
            current, distance = frontier.pop(0)
            for successor_id in sorted(current.get("successor_ids", [])):
                successor = by_old_id.get(successor_id)
                if successor is None or successor_id in seen:
                    continue
                label = label_key(successor)
                if label is not None:
                    return label, distance + 1
                seen.add(successor_id)
                frontier.append((successor, distance + 1))
        return None

    def compound_key(record: dict[str, Any]) -> tuple[str, str]:
        label = label_key(record)
        if label is not None:
            return f"CMP:{label}", basis(AUTHOR_LABEL)
        anchor = downstream_anchor(record)
        if anchor is not None:
            return f"CMP:{anchor[0]}-{anchor[1]}", DERIVED_ORDINAL
        names = record.get("names") or []
        if names:
            found = _verified(names[0], record.get("evidence_refs", ()), observation_texts)
            return f"CMP:~{fold_text(names[0])}", basis(AUTHOR_TEXT if found else DERIVED_ORDINAL)
        return "CMP:?", DERIVED_ORDINAL

    # Only the author's label merges records: two unlabeled compounds at the same
    # distance from one anchor are two compounds, and keep distinct (suffixed) keys.
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in compounds:
        tail, kind = compound_key(record)
        grouped[
            tail
            if kind == basis(AUTHOR_LABEL) and label_key(record)
            else f"{tail}\0{record['compound_id']}"
        ].append(record)
    merged_compounds: list[dict[str, Any]] = []
    old_to_survivor: dict[str, str] = {}

    def content_order(key: str) -> tuple[str, str, str]:
        # Collisions are numbered in an order read off the records' own content, never
        # off the provisional id (which came from the model's fact name).
        record = grouped[key][0]
        return (
            key.split("\0")[0],
            fold_text((record.get("names") or [""])[0]),
            ",".join(sorted(r["observation_id"] for r in record.get("evidence_refs", []))),
        )

    for key_tail in sorted(grouped, key=content_order):
        members = sorted(grouped[key_tail], key=lambda c: (c.get("names") or [""])[0])
        survivor = members[0]
        for other in members[1:]:
            if author_label_signs_disagree(
                survivor.get("author_labels", ()), other.get("author_labels", ())
            ):
                # The author used the sign to tell two samples apart: not one record.
                conflicts.append(
                    {
                        "conflict_id": None,
                        "target_object_type": "COMPOUND",
                        "target_object_id": survivor["compound_id"],
                        "field_key": "author_labels",
                        "candidate_values": [
                            {
                                "candidate_id": None,
                                "value": list(survivor["author_labels"]),
                                "evidence_uses": [
                                    {"evidence_ref": r, "role": "CONFLICT"}
                                    for r in survivor["evidence_refs"]
                                ],
                            },
                            {
                                "candidate_id": None,
                                "value": list(other["author_labels"]),
                                "evidence_uses": [
                                    {"evidence_ref": r, "role": "CONFLICT"}
                                    for r in other["evidence_refs"]
                                ],
                            },
                        ],
                        "blocking": False,
                    }
                )
                merged_compounds.append(other)
                continue
            old_to_survivor[other["compound_id"]] = survivor["compound_id"]
            for expression in EXPRESSION_SETS:
                if expression in survivor or expression in other:
                    survivor[expression] = merge_interpretations(
                        survivor.get(expression, []), other.get(expression, [])
                    )
            for field in ("author_labels", "names", "predecessor_ids", "successor_ids"):
                survivor[field] = list(
                    dict.fromkeys([*survivor.get(field, []), *other.get(field, [])])
                )
            survivor["evidence_refs"] = _sorted_refs(
                [*survivor["evidence_refs"], *other["evidence_refs"]]
            )
            mine, theirs = survivor.get("structure"), other.get("structure")
            if _value(mine) is None and _value(theirs) is not None:
                survivor["structure"] = theirs
            elif (
                _value(mine) is not None
                and _value(theirs) is not None
                and _value(mine) != _value(theirs)
            ):
                conflicts.append(
                    {
                        "conflict_id": None,
                        "target_object_type": "COMPOUND",
                        "target_object_id": survivor["compound_id"],
                        "field_key": "structure",
                        "candidate_values": [
                            {
                                "candidate_id": None,
                                "value": _value(mine),
                                "evidence_uses": list((mine or {}).get("evidence_uses", [])),
                            },
                            {
                                "candidate_id": None,
                                "value": _value(theirs),
                                "evidence_uses": list((theirs or {}).get("evidence_uses", [])),
                            },
                        ],
                        "blocking": False,
                    }
                )
        merged_compounds.append(survivor)
    compounds = merged_compounds
    for record in compounds:
        tail, kind = compound_key(record)
        keys.assign(record, "compound_id", "CMP", f"{group}|{tail}", kind)
    for old, survivor_id in old_to_survivor.items():
        keys.ids[old] = keys.ids[survivor_id]

    def compound_tail(old_id: str | None) -> str:
        new_id = keys.ids.get(old_id or "")
        for record in compounds:
            if record["compound_id"] == new_id:
                return str(record["stable_key"]).rsplit("|", 1)[-1][len("CMP:") :]
        return "?"

    def label_of(old_id: str | None) -> str:
        new_id = keys.ids.get(old_id or "")
        for record in compounds:
            if record["compound_id"] == new_id:
                labels = record.get("author_labels") or []
                return author_label_key(labels) or "~" + compound_tail(old_id).lstrip("~")
        return "?"

    # -- independent experiment containers never become route members -------------
    for account in accounts:
        created = _creation_key(account, "account_id", creation_keys)
        account_key = created or account["stable_key"]
        keys.assign(account, "account_id", "ACCOUNT", account_key, DERIVED_ORDINAL)
        account.pop("key_basis", None)  # ExperimentalAccount has no such schema field.

    # -- blocks, operations, outcomes ---------------------------------------------
    for block in sorted(blocks, key=lambda b: b.get("source_order", 0)):
        inputs, outputs = (
            _value(block.get("input_compound_ids")),
            _value(block.get("output_compound_ids")),
        )
        if inputs is None or outputs is None:
            tail, kind = "RXB:?", DERIVED_ORDINAL
        else:
            tail = (
                "RXB:"
                + ",".join(sorted(label_of(i) for i in inputs))
                + ">"
                + ",".join(sorted(label_of(o) for o in outputs))
            )
            kind = basis(AUTHOR_LABEL)
        block_key = keys.assign(block, "reaction_block_id", "RXB", f"{group}|{tail}", kind)
    containers = [*blocks, *accounts]
    for block in containers:
        block_key = block["stable_key"]
        for intermediate in block.get("local_intermediates", []):
            old = intermediate["local_intermediate_id"]
            created = (creation_keys or {}).get(old)
            intermediate["local_intermediate_id"] = keys.ids[old] = (
                public_id("INT", created) if created else old
            )
        for operation in sorted(block.get("operations", []), key=lambda o: o["sequence_index"]):
            _material_identities(operation, keys, creation_keys)
            op_key = keys.assign(
                operation,
                "operation_id",
                "OP",
                _creation_key(operation, "operation_id", creation_keys)
                or f"{block_key}|OP:{operation['sequence_index']}",
                DERIVED_ORDINAL,
            )
            intermediate = _value(operation.get("operation_intermediate"))
            if isinstance(intermediate, dict) and intermediate.get("local_intermediate_id"):
                old = intermediate["local_intermediate_id"]
                intermediate["local_intermediate_id"] = keys.ids[old] = public_id(
                    "INT", f"{op_key}|INT"
                )
        block["operations"] = sorted(block.get("operations", []), key=lambda o: o["sequence_index"])
    # Outcomes after every operation has its id: their span names operations by index.
    for block in [*containers, {"reported_outcomes": unassigned}]:
        for outcome in block.get("reported_outcomes", []):
            kind_value = _value(outcome.get("kind")) or "?"
            products = (
                ",".join(
                    sorted(label_of(p) for p in (_value(outcome.get("product_compound_ids")) or []))
                )
                or "?"
            )
            op_ids = _value(outcome.get("operation_ids")) or []
            indices = sorted(
                op["sequence_index"]
                for b in containers
                for op in b["operations"]
                if op["operation_id"] in {keys.ids.get(o, o) for o in op_ids}
            )
            span = f"{indices[0]}-{indices[-1]}" if indices else "?"
            created = _creation_key(outcome, "outcome_id", creation_keys)
            # Assigned/unassigned membership is not measurement identity. Legacy
            # records without a creation retain their existing public key, if known.
            existing = outcome.get("stable_key")
            if "stable_key" not in block and not (created or existing):
                keys.ids[outcome["outcome_id"]] = outcome["outcome_id"]
                continue
            keys.assign(
                outcome,
                "outcome_id",
                "OUT",
                created or existing or f"{block['stable_key']}|OUT:{kind_value}:{products}:{span}",
                DERIVED_ORDINAL
                if created
                else outcome.get("key_basis")
                or (basis(AUTHOR_LABEL) if kind_value != "?" else DERIVED_ORDINAL),
            )
        block["reported_outcomes"] = sorted(
            block["reported_outcomes"], key=lambda o: o.get("stable_key", o["outcome_id"])
        )

    # -- target paths and assertions -----------------------------------------------
    for path in route.get("target_paths", []):
        keys.assign(
            path,
            "target_path_id",
            "TGT",
            f"{group}|TGT:{label_of(path.get('target_compound_id'))}",
            basis(AUTHOR_LABEL),
        )
    for assertion in route.get("target_assertions", []):
        subject = _value(assertion.get("subject")) or "?"
        keys.assign(
            assertion,
            "assertion_id",
            "ASSERT",
            f"{group}|ASSERT:{label_of(assertion.get('target_compound_id'))}:{subject}",
            basis(AUTHOR_LABEL) if subject != "?" else DERIVED_ORDINAL,
        )

    # -- cards and narratives --------------------------------------------------------
    for card in cards:
        owners = ",".join(sorted(label_of(c) for c in card.get("compound_ids", [])))
        method = card.get("method") or fold_text(card.get("method_verbatim") or "")
        discriminator = fold_text(str(card.get("reported_ion") or card.get("sample_context") or ""))
        keys.assign(
            card,
            "characterization_id",
            "CHAR",
            _creation_key(card, "characterization_id", creation_keys)
            or f"{group}|CMP:{owners}|CHAR:{method}:{discriminator}",
            basis(AUTHOR_TEXT)
            if characterization_verification(card, characterization_texts, furniture=furniture)
            == "VERIFIED"
            else DERIVED_ORDINAL,
        )
    for narrative in narratives:
        text = narrative.get("author_text")
        found = _verified(text, narrative.get("evidence_refs", ()), observation_texts)
        if text:
            tail, kind = (
                f"NARR:{narrative['kind']}:{_digest(text)}",
                basis(AUTHOR_TEXT) if found else DERIVED_ORDINAL,
            )
        else:
            tail, kind = (
                f"NARR:{narrative['kind']}:~{_digest(narrative.get('explanation') or '')}",
                DERIVED_ORDINAL,
            )
        created = _creation_key(narrative, "narrative_id", creation_keys)
        narrative_key = keys.assign(
            narrative,
            "narrative_id",
            "NARR",
            created or f"{group}|{tail}",
            DERIVED_ORDINAL if created else kind,
        )
        if found is False:
            unresolved.append(
                {
                    "unresolved_item_id": None,
                    "scope": "PAPER",
                    "subject_id": paper_id,
                    "reason_code": "UNPARSED_SOURCE",
                    "evidence_refs": list(narrative.get("evidence_refs", [])),
                    "_kind": "NARRATIVE_NOT_VERBATIM",
                    "_subject_key": narrative_key,
                }
            )

    # -- references between records ------------------------------------------------
    def remap(value: Any) -> Any:
        return _remap_references(value, keys.ids, reference=True)

    for narrative in narratives:
        rekey_source_readings(narrative, keys.ids)
    for record in compounds:
        for field in ("predecessor_ids", "successor_ids"):
            record[field] = sorted(set(remap(record.get(field, []))))
    for block in containers:
        for field in ("input_compound_ids", "output_compound_ids"):
            block[field] = remap(block[field])
        for field in (
            "mechanism_context_ids",
            "optimization_context_ids",
            "failed_attempt_context_ids",
        ):
            if field in block:
                block[field] = sorted(set(remap(block[field])))
        for operation in block["operations"]:
            if "operation_intermediate" in operation:
                operation["operation_intermediate"] = remap(operation["operation_intermediate"])
            materials = _value(operation.get("materials"))
            if isinstance(materials, list):
                for material in materials:
                    material["compound_id"] = remap(material.get("compound_id"))
                    rekey_source_readings(material, keys.ids)
                order = sorted(
                    range(len(materials)), key=lambda index: _material_order(materials[index])
                )
                remapped = {old + 1: new + 1 for new, old in enumerate(order)}
                for row in materials:
                    if "summary_material_index" in row:
                        row["summary_material_index"] = remapped[row["summary_material_index"]]
                materials[:] = [materials[index] for index in order]
        for outcome in block["reported_outcomes"]:
            rekey_source_readings(outcome, keys.ids)
            outcome["operation_ids"] = remap(outcome["operation_ids"])
            outcome["product_compound_ids"] = remap(outcome["product_compound_ids"])
    route["compound_ids"] = sorted(set(remap(route.get("compound_ids", []))))
    route["reaction_block_ids"] = [
        b["reaction_block_id"] for b in sorted(blocks, key=lambda b: b.get("source_order", 0))
    ]
    for path in route.get("target_paths", []):
        path["target_compound_id"] = remap(path["target_compound_id"])
        path["reaction_block_ids"] = remap(path["reaction_block_ids"])
    for assertion in route.get("target_assertions", []):
        assertion["target_compound_id"] = remap(assertion["target_compound_id"])
    for card in cards:
        card["compound_ids"] = sorted(set(remap(card["compound_ids"])))

    # -- unresolved items and conflicts: keyed by their subject ----------------------
    subject_keys = {
        **{c["compound_id"]: c["stable_key"] for c in compounds},
        **{b["reaction_block_id"]: b["stable_key"] for b in blocks},
        **{a["account_id"]: a["stable_key"] for a in accounts},
        **{op["operation_id"]: op["stable_key"] for b in containers for op in b["operations"]},
        **{o["outcome_id"]: o["stable_key"] for b in containers for o in b["reported_outcomes"]},
        **{o["outcome_id"]: o["stable_key"] for o in unassigned if o.get("stable_key")},
        **{c["characterization_id"]: c["stable_key"] for c in cards},
        **{n["narrative_id"]: n["stable_key"] for n in narratives},
        **{p["target_path_id"]: p["stable_key"] for p in route.get("target_paths", [])},
        **{a["assertion_id"]: a["stable_key"] for a in route.get("target_assertions", [])},
    }
    scope_keys = {"ROUTE": group, "PAPER": paper_id}
    for item in unresolved:
        item["subject_id"] = remap(item["subject_id"])
        item["evidence_refs"] = _sorted_refs(item.get("evidence_refs", []))
        subject_key = (
            item.pop("_subject_key", None)
            or subject_keys.get(item["subject_id"])
            or scope_keys.get(item["scope"], group)
        )
        kind = item.pop("_kind", "UNRESOLVED")
        created = item.get("creation_key") or _creation_key(
            item, "unresolved_item_id", creation_keys
        )
        keys.assign(
            item,
            "unresolved_item_id",
            "UNR",
            created or f"{subject_key}|UNR:{kind}",
            DERIVED_ORDINAL,
        )
    for conflict in conflicts:
        conflict["target_object_id"] = remap(conflict["target_object_id"])
        subject_key = subject_keys.get(conflict["target_object_id"], group)
        conflict_key = keys.assign(
            conflict,
            "conflict_id",
            "CONF",
            f"{subject_key}|CONF:{conflict['field_key']}",
            DERIVED_ORDINAL,
        )
        for index, candidate in enumerate(conflict.get("candidate_values", []), 1):
            candidate["candidate_id"] = public_id("CAND", f"{conflict_key}:{index}")

    # -- canonical order of every public list ----------------------------------------
    compounds.sort(key=lambda c: c["stable_key"])
    blocks.sort(key=lambda b: (b.get("source_order", 0), b["stable_key"]))
    for record in compounds:
        record["evidence_refs"] = _sorted_refs(record["evidence_refs"])
    for block in containers:
        block["evidence_refs"] = _sorted_refs(block["evidence_refs"])
        for operation in block["operations"]:
            operation["evidence_refs"] = _sorted_refs(operation["evidence_refs"])
    accounts.sort(key=lambda a: a["stable_key"])
    unassigned.sort(key=lambda o: o.get("stable_key", o["outcome_id"]))
    cards.sort(key=lambda c: c["stable_key"])
    narratives.sort(key=lambda n: n["stable_key"])
    unresolved.sort(key=lambda u: u["stable_key"])
    conflicts.sort(key=lambda c: c["stable_key"])
    route["evidence_refs"] = _sorted_refs(route.get("evidence_refs", []))
    # A route-local id map is not an allowlist: paper/route roots keep their identity,
    # and another route's objects may not be keyed until the later global pass.
    # Preserve every reference for that pass and the public closure validator.
    assessments = []
    for assessment in getattr(projection, "coverage_assessments", ()):
        subjects = sorted({keys.ids.get(s, s) for s in assessment["subject_ids"]})
        assessments.append({**assessment, "subject_ids": subjects})
    assessments.sort(key=lambda a: (a["subject_ids"], a["status"]))
    minted = frozenset(keys.ids.values())
    references = {old: new for old, new in keys.ids.items() if old not in minted}

    def current(records: Any) -> Any:
        return _remap_references(records, references)

    additions = {}
    if hasattr(projection, "experimental_accounts"):
        additions["experimental_accounts"] = tuple(current(accounts))
    if hasattr(projection, "unassigned_outcomes"):
        additions["unassigned_outcomes"] = tuple(current(unassigned))
    return replace(
        projection,
        **additions,
        compounds=tuple(current(compounds)),
        reaction_blocks=tuple(current(blocks)),
        route=current(route),
        observation_refs=tuple(_sorted_refs(projection.observation_refs)),
        unresolved_items=tuple(current(unresolved)),
        field_conflicts=tuple(current(conflicts)),
        paper_narratives=tuple(current(narratives)),
        characterization_records=tuple(current(cards)),
        coverage_assessments=tuple(assessments),
    )


# ---------------------------------------------------------------------------------
# One entry for a whole result (E84): every public id of a machine result -- the
# projection's, the enrichment's additions, the paper-level items -- is minted by
# `rekey`, route by route, and nothing else mints one.
# ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RouteView:
    """One route's records in the shape `rekey` reads (the projection's fields, no
    import of the projection module: it calls this one)."""

    route_group_id: str
    compounds: tuple[dict[str, Any], ...]
    reaction_blocks: tuple[dict[str, Any], ...]
    route: dict[str, Any]
    observation_refs: tuple[dict[str, str], ...]
    unresolved_items: tuple[dict[str, Any], ...]
    field_conflicts: tuple[dict[str, Any], ...]
    paper_narratives: tuple[dict[str, Any], ...]
    characterization_records: tuple[dict[str, Any], ...]
    coverage_assessments: tuple[dict[str, Any], ...]
    experimental_accounts: tuple[dict[str, Any], ...] = ()
    unassigned_outcomes: tuple[dict[str, Any], ...] = ()


def _route_group_id(route: Mapping[str, Any]) -> str:
    route_id = str(route.get("route_id", ""))
    return route_id.removeprefix("ROUTE-") or route_id


def _unresolved_kind(item: Mapping[str, Any]) -> str:
    """The kind a keyed item was keyed under, else the reason it came with."""
    marked = item.get("_kind")
    if isinstance(marked, str) and marked:
        return marked
    key = item.get("stable_key")
    if isinstance(key, str) and "|UNR:" in key:
        return key.rsplit("|UNR:", 1)[1].split("#", 1)[0]
    return str(item.get("reason_code", "UNRESOLVED"))


def _endpoints(block: Mapping[str, Any]) -> set[str]:
    inputs = _value(block.get("input_compound_ids")) or []
    outputs = _value(block.get("output_compound_ids")) or []
    return set(inputs) | set(outputs)


def _by_content(record: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(
        {k: v for k, v in record.items() if k not in {"unresolved_item_id", "conflict_id"}}
    )


def _keyed_first(record: Mapping[str, Any]) -> tuple[int, str, int, bytes]:
    """Records a previous pass keyed come first, in their key's order, the rest by
    content: an ordinal a published revision assigned never shifts because a record
    with the same key arrived in a later revision."""
    key = record.get("stable_key")
    if isinstance(key, str) and key:
        base, _, ordinal = key.partition("#")
        return (0, base, int(ordinal) if ordinal.isdigit() else 0, b"")
    return (1, "", 0, _by_content(record))


def _material_order(material: Mapping[str, Any]) -> tuple[int, int, str]:
    return (
        material.get("sequence_index") or 0,
        _MATERIAL_ROLE_ORDER.index(material["role"])
        if material.get("role") in _MATERIAL_ROLE_ORDER
        else 99,
        fold_text(material.get("verbatim_name") or ""),
    )


def rekey_machine(
    machine: Mapping[str, Any],
    *,
    paper_id: str,
    observation_texts: Mapping[str, str] | None = None,
    characterization_texts: Mapping[str, str] | None = None,
    furniture: Sequence[str] = (),
    id_map: dict[str, str] | None = None,
    creation_keys: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The machine result with public ids derived by `rekey` (E66), route by route.

    Independent accounts and unassigned measurements are keyed once alongside the
    first route, without adding them to that route. A route-free result uses only an
    internal grouping; no synthetic route is published. `creation_keys` maps exact
    object ids to host-proven creation keys. A material without an id requires an
    explicit baseline `material_position(old_operation_id, index)` binding; absence
    never causes an identity to be inferred from its words, amount or current order.

    A record that already carries its stable key keeps its id (the rule is a function
    of content, so applying it again changes nothing); a record added later by the
    detail investigation -- a compound, a block with its operations, an operation on a
    public block, a card, an unresolved item -- gets the id the same rule gives it,
    and merges with the record it repeats when their keys agree. Records no route
    lists are attributed to the route whose compounds they name (a block by its
    endpoints, a card by its compounds, an item or a conflict by its subject), else to
    the first route. Every reference in the result, the root's blocking view and the
    stage outputs named by a compound id follow the new ids. `id_map`, when given,
    is filled with every old id -> new id for a caller that holds ids of its own.
    """
    result = copy.deepcopy(dict(machine))
    domain = dict(result["domain_payload"])
    routes = [dict(r) for r in domain.get("routes", [])]
    has_routes = bool(routes)
    if not routes:
        routes = [{"route_id": "", "compound_ids": [], "reaction_block_ids": []}]
    compounds = {c["compound_id"]: dict(c) for c in domain.get("compounds", [])}
    blocks = {b["reaction_block_id"]: dict(b) for b in domain.get("reaction_blocks", [])}
    cards = [dict(c) for c in domain.get("characterization_records", [])]
    narratives = [dict(n) for n in domain.get("paper_narratives", [])]
    accounts = list(domain.get("experimental_accounts", []))
    unassigned = list(domain.get("unassigned_outcomes", []))
    conflicts = [dict(c) for c in domain.get("field_conflicts", [])]
    unresolved = [dict(u) for u in result.get("unresolved_items", [])]
    assessments = [dict(a) for a in result.get("coverage_assessments", [])]

    # -- attribute every record to one route ----------------------------------------
    route_blocks: list[set[str]] = [set(r.get("reaction_block_ids", [])) for r in routes]
    route_compounds: list[set[str]] = [set(r.get("compound_ids", [])) for r in routes]
    for index, block_ids in enumerate(route_blocks):
        for block_id in block_ids:
            if block_id in blocks:
                route_compounds[index] |= _endpoints(blocks[block_id])
    listed_blocks = set().union(*route_blocks)
    for block_id in sorted(
        set(blocks) - listed_blocks, key=lambda b: (blocks[b].get("source_order", 0), b)
    ):
        overlap = [len(_endpoints(blocks[block_id]) & members) for members in route_compounds]
        index = max(range(len(routes)), key=lambda i: (overlap[i], -i))
        route_blocks[index].add(block_id)
        route_compounds[index] |= _endpoints(blocks[block_id])
    listed_compounds = set().union(*route_compounds)
    # Explicit account-only or unassigned measurement subjects need identities but
    # have no declared route membership. Keep the legacy orphan rule for all others.
    independent_compounds: set[str] = set()
    for account in accounts:
        independent_compounds |= _endpoints(account)
        for operation in account["operations"]:
            for material in _value(operation.get("materials")) or ():
                if material.get("compound_id"):
                    independent_compounds.add(material["compound_id"])
    for outcome in [*unassigned, *(o for a in accounts for o in a["reported_outcomes"])]:
        independent_compounds.update(_value(outcome.get("product_compound_ids")) or ())
    independent_compounds -= listed_compounds
    route_compounds[0] |= set(compounds) - listed_compounds

    subject_route: dict[str, int] = {}
    for index, route in enumerate(routes):
        for compound_id in route_compounds[index]:
            subject_route.setdefault(compound_id, index)
        for block_id in route_blocks[index]:
            subject_route.setdefault(block_id, index)
            block = blocks[block_id]
            for operation in block.get("operations", []):
                subject_route.setdefault(operation["operation_id"], index)
            for outcome in block.get("reported_outcomes", []):
                subject_route.setdefault(outcome["outcome_id"], index)
        subject_route.setdefault(str(route.get("route_id")), index)
        for path in route.get("target_paths", []):
            subject_route.setdefault(path["target_path_id"], index)
        for assertion in route.get("target_assertions", []):
            subject_route.setdefault(assertion["assertion_id"], index)

    def route_of(subject_id: object) -> int:
        return subject_route.get(str(subject_id), 0)

    per_route: list[dict[str, list[dict[str, Any]]]] = [
        {"cards": [], "narratives": [], "conflicts": [], "unresolved": [], "assessments": []}
        for _ in routes
    ]
    for card in cards:
        owners = card.get("compound_ids") or [None]
        per_route[route_of(owners[0])]["cards"].append(card)
    for narrative in narratives:
        subjects = narrative.get("subjects") or [{}]
        per_route[route_of(subjects[0].get("object_id"))]["narratives"].append(narrative)
    for conflict in conflicts:
        per_route[route_of(conflict.get("target_object_id"))]["conflicts"].append(conflict)
    for item in unresolved:
        item["_kind"] = _unresolved_kind(item)
        per_route[route_of(item.get("subject_id"))]["unresolved"].append(item)
    for assessment in assessments:
        subjects = assessment.get("subject_ids") or [None]
        per_route[route_of(subjects[0])]["assessments"].append(assessment)

    # -- key each route's records with the one rule --------------------------------
    if id_map is None:
        id_map = {}
    keyed_routes = []
    for index, route in enumerate(routes):
        ordered_blocks = sorted(
            route_blocks[index], key=lambda b: (blocks[b].get("source_order", 0), b)
        )
        route["reaction_block_ids"] = ordered_blocks
        route["compound_ids"] = sorted(route_compounds[index] - independent_compounds)
        projection = _RouteView(
            route_group_id=_route_group_id(route),
            compounds=tuple(compounds[c] for c in sorted(route_compounds[index])),
            reaction_blocks=tuple(blocks[b] for b in ordered_blocks),
            route=route,
            observation_refs=tuple(domain.get("source_observation_refs", [])),
            unresolved_items=tuple(sorted(per_route[index]["unresolved"], key=_keyed_first)),
            field_conflicts=tuple(sorted(per_route[index]["conflicts"], key=_keyed_first)),
            paper_narratives=tuple(per_route[index]["narratives"]),
            characterization_records=tuple(per_route[index]["cards"]),
            coverage_assessments=tuple(per_route[index]["assessments"]),
            experimental_accounts=tuple(accounts) if index == 0 else (),
            unassigned_outcomes=tuple(unassigned) if index == 0 else (),
        )
        keyed_routes.append(
            rekey(
                projection,
                paper_id=paper_id,
                observation_texts=observation_texts,
                characterization_texts=characterization_texts,
                furniture=furniture,
                id_map=id_map,
                creation_keys=creation_keys,
            )
        )

    # -- reassemble, and let every reference follow the ids -------------------------
    # An id this pass minted is already current and is never looked up again. `id_map`
    # maps an old id to its new one, and `follow` walks every string of every record
    # without being able to tell "this string IS my identity" from "this string names
    # someone else" -- so without this line a record that is minted the id another
    # record just vacated has its own identity rewritten into that record's new id, and
    # the two converge on one public id while keeping two different stable keys.
    #
    # Measured on the e109 round of decumbenine_B: enrichment filled the published
    # melting-point card's empty `sample_context` with "white solid", which is part of a
    # card's key, so that card moved from CHAR-1c5e76515a2f970a to CHAR-f80c32880c9f950b
    # and left its old key free; the detail lane's own card for the same measurement then
    # keyed to exactly what was vacated, minted CHAR-1c5e76515a2f970a, and was sent
    # through the first card's mapping. Publication refused the result -- a machine may
    # not carry two cards under one id -- and the HRMS on that compound did the same.
    # The `#2` ordinal a few lines up is the other case and is not this one: it settles
    # two records that share a key, and these two never did.
    minted = frozenset(id_map.values())

    def follow(value: Any) -> Any:
        return _remap_references(
            value, {old: new for old, new in id_map.items() if old not in minted}
        )

    domain["compounds"] = follow([c for k in keyed_routes for c in k.compounds])
    domain["reaction_blocks"] = follow([b for k in keyed_routes for b in k.reaction_blocks])
    domain["routes"] = follow([k.route for k in keyed_routes]) if has_routes else []
    if "experimental_accounts" in domain:
        domain["experimental_accounts"] = follow(
            [a for k in keyed_routes for a in k.experimental_accounts]
        )
    if "unassigned_outcomes" in domain:
        domain["unassigned_outcomes"] = follow(
            [o for k in keyed_routes for o in k.unassigned_outcomes]
        )
    domain["characterization_records"] = follow(
        [c for k in keyed_routes for c in k.characterization_records]
    )
    domain["paper_narratives"] = follow([n for k in keyed_routes for n in k.paper_narratives])
    domain["field_conflicts"] = follow([c for k in keyed_routes for c in k.field_conflicts])
    if "review_markers" in domain:
        original_operations = {
            op["operation_id"]: op
            for block in [*machine["domain_payload"]["reaction_blocks"],
                          *machine["domain_payload"].get("experimental_accounts", [])]
            for op in block["operations"]
        }
        markers = []
        for marker in domain["review_markers"]:
            updated = dict(marker)
            old_target = marker["target_object_id"]
            updated["target_object_id"] = id_map.get(old_target, old_target)
            if "material_index" in marker and old_target in original_operations:
                materials = _value(original_operations[old_target].get("materials")) or []
                order = sorted(range(len(materials)), key=lambda i: _material_order(materials[i]))
                old_index = marker["material_index"] - 1
                if old_index not in order:
                    raise ValueError("review marker material_index is outside the main value")
                updated["material_index"] = order.index(old_index) + 1
            markers.append(updated)
        domain["review_markers"] = markers
    result["domain_payload"] = domain
    result["unresolved_items"] = follow([u for k in keyed_routes for u in k.unresolved_items])
    result["blocking_conflicts"] = [c for c in domain["field_conflicts"] if c.get("blocking")]
    if "coverage_assessments" in result:
        # These are pure references. Map their original identities exactly once,
        # using the complete map, even if an old id was minted for another object.
        # Do not feed a mixture of locally mapped and not-yet-mapped references
        # through the mixed identity/reference protection in `follow`.
        result["coverage_assessments"] = sorted(
            [{**a, "subject_ids": sorted({id_map.get(s, s) for s in a["subject_ids"]})}
             for a in assessments],
            key=lambda a: (a["subject_ids"], a["status"]),
        )
    # A stage output sealed per compound is named by that compound's id.
    renamed = []
    for ref in result.get("stage_output_refs", []):
        ref = dict(ref)
        name = ref.get("logical_name")
        if isinstance(name, str) and name.startswith("characterization-package-"):
            old_id = name[len("characterization-package-") :]
            ref["logical_name"] = "characterization-package-" + id_map.get(old_id, old_id)
        renamed.append(ref)
    if renamed:
        result["stage_output_refs"] = renamed
    return result
