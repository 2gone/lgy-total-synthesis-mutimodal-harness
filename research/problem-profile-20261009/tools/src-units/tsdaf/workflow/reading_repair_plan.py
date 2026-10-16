"""Verify a sparse Reading history once, then replay its bound contributions.

This disposable plan is derived from the existing accepted call receipts. It is
neither a model contract nor a second journal, and legacy snapshots do not enter it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import copy, deepcopy
from dataclasses import dataclass, replace
from typing import Any

from pydantic import TypeAdapter

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.container_enrichment import content_hash
from tsdaf.lanes.literature.enrichment_values import cited_selection_ids
from tsdaf.lanes.literature.source_parts import fact_source_ids
from tsdaf.lanes.literature.fusion_state import fact_references
from tsdaf.lanes.literature.gate_models import FactOrigin, read_fusion_result
from tsdaf.lanes.literature.middle_layer import (
    ReadingAnswer,
    SelectionsHeld,
    UnifiedSkeletonRepair,
    reading_compound_view,
)
from tsdaf.lanes.literature.material_write_dependencies import ReadingWritePlan, reading_write_groups
from tsdaf.workflow.accepted_artifacts import read_artifact
from tsdaf.workflow.fusion_names import fusion_names
from tsdaf.workflow.investigation_origins import (
    InvestigationOrigins,
    withdraw_item_state,
    write_item_state,
)
from tsdaf.workflow.literature_fusion_actions import (
    bind_fact_change_names,
    binder_spec,
    name_binder,
    reconstructed_repair_command,
    verified_repair_command,
)
from tsdaf.workflow.reading_repair_mode import SPARSE, reading_repair_mode
from tsdaf.workflow.reading_source_names import reading_source_binder
from tsdaf.workflow.reading_skeleton import (
    BoundSkeletonRepair,
    RepairedReading,
    RepairProjectionMemo,
    prepare_finding_bridges,
    replay_skeleton_repairs,
)
from tsdaf.workflow.repair_target_names import RepairTargetNames
from tsdaf.workflow.write_delivery import write_action_id


def sparse_reading_write_groups(raw, saved, view: RepairedReading) -> ReadingWritePlan:
    """Schedule declared references, without validating or interpreting science.

    Each repair is already an explicit atomic group. New parent/child repairs
    remain directed; only edits of existing references require a joint candidate.
    Malformed declarations still name attempted parents so a child cannot silently
    fall back to the old parent after that parent's submitted update was refused.
    """
    ordinary = reading_write_groups(raw, saved)
    targets = RepairTargetNames.of(view.identity)
    groups = {g["route_group_id"]: g for g in view.groups}
    states = {gid: read_fusion_result(canonical_json_bytes(g["result"]))
              for gid, g in groups.items()}
    facts = {(gid, f.fact_id): f.model_dump(mode="json")
             for gid, state in states.items() for f in state.facts}
    actions = {}
    declarations = {}

    def changes(value):
        if (not isinstance(value, Mapping) or not isinstance(value.get("route_group_id"), str)
                or value["route_group_id"] not in states):
            return ()
        gid = value["route_group_id"]
        value = targets.resolve(value)
        entries = value.get("repair", {}).get("changes", ()) if isinstance(value.get("repair"), Mapping) else ()
        bind = name_binder(states[gid])
        result = []
        for entry in entries if isinstance(entries, (list, tuple)) else ():
            if not isinstance(entry, Mapping) or not isinstance(entry.get("fact_id"), str):
                continue
            entry = deepcopy(dict(entry))
            bind_fact_change_names(entry, subject=bind, reference=bind)
            result.append(((gid, entry["fact_id"]), entry))
        return result

    for i, value in enumerate(raw.get("skeleton_repairs", ())):
        key = f"skeleton_repairs:{i}"
        actions[key] = changes(value)
        for subject, _ in actions[key]:
            declarations.setdefault(subject, set()).add(key)
    for i, value in enumerate(raw.get("withdrawals", ())):
        if not isinstance(value, Mapping):
            continue
        matched = [row for row in saved if row["collection"] == "skeleton_repairs"
                   and (value.get("item_id") or value.get("location"))
                   and (not value.get("item_id") or row["item_id"] == value["item_id"])
                   and (not value.get("location") or row["location"] == value["location"])]
        if len(matched) != 1:
            continue
        row = matched[0]
        subjects = view.repair_subjects.get(row["item_id"], ())
        if not subjects:
            subjects = tuple(subject for subject, _ in changes(row["value"]))
        key = f"withdrawals:{i}"
        actions[key] = [(subject, None) for subject in subjects]
        for subject in subjects:
            declarations.setdefault(subject, set()).add(key)

    parent = {key: key for key in actions}
    dependencies = {key: set(value) for key, value in ordinary.prerequisites.items()}

    def root(key):
        parent.setdefault(key, key)
        if parent[key] != key:
            parent[key] = root(parent[key])
        return parent[key]

    def unite(keys):
        for key in keys[1:]:
            parent[root(key)] = root(keys[0])

    for group in ordinary.atomic_groups:
        unite(group)

    def references(value):
        if not value or value.get("withdraw") or value.get("status") == "REJECTED":
            return set()
        # Invalid DTO shapes are left to the item validator, never a whole-call failure.
        clean = {name: [v for v in value.get(name, ()) if isinstance(v, str)]
                 for name in ("owners", "inputs", "outputs", "after")
                 if isinstance(value.get(name), (list, tuple))}
        if isinstance(value.get("same_as"), str):
            clean["same_as"] = value["same_as"]
        if isinstance(value.get("measurement"), Mapping) and isinstance(value["measurement"].get("same_as"), str):
            clean["measurement"] = value["measurement"]
        return fact_references(clean)

    critical = {"kind", "status", "owners", "same_as", "measurement"}
    for key, entries in actions.items():
        for subject, change in entries:
            gid, _ = subject
            old = facts.get(subject)
            after = {**(old or {}), **change} if change is not None else None
            if after and isinstance(after.get("clear"), (list, tuple)):
                for field in after["clear"]:
                    if isinstance(field, str):
                        after.pop(field, None)
            for ref in references(after):
                dependencies.setdefault(key, set()).update(declarations.get((gid, ref), set()) - {key})
            if old is None:
                continue
            for ref in references(old):
                for prerequisite in declarations.get((gid, ref), ()):
                    if prerequisite == key:
                        continue
                    before_parent = facts.get((gid, ref), {})
                    for target, update in actions[prerequisite]:
                        if target != (gid, ref):
                            continue
                        structural = update is None or update.get("withdraw") or any(
                            field in update and update[field] != before_parent.get(field)
                            for field in critical
                        ) or bool({field for field in update.get("clear", ())
                                   if isinstance(field, str)} & critical
                                  if isinstance(update.get("clear", ()), (list, tuple)) else False)
                        if structural:
                            unite([prerequisite, key])
    grouped = {}
    for key in parent:
        grouped.setdefault(root(key), []).append(key)
    return ReadingWritePlan(
        atomic_groups=tuple(tuple(keys) for keys in grouped.values() if len(keys) > 1),
        prerequisites={key: tuple(sorted(values)) for key, values in dependencies.items()},
    )


@dataclass(frozen=True)
class VerifiedRepairPlan:
    repairs: tuple[BoundSkeletonRepair, ...]
    finding_origins: Mapping[str, FactOrigin]
    finding_births: Mapping[str, FactOrigin]
    generation: str

    def replay(self, answer: ReadingAnswer, *, projection_memo=None, **kwargs: Any) -> RepairedReading:
        project = projection_memo.replay if projection_memo is not None else replay_skeleton_repairs
        return project(
            answer,
            bound_repairs=self.repairs,
            finding_origins=self.finding_origins,
            finding_births=self.finding_births,
            **kwargs,
        )


def verify_request_repair_targets(
    snapshot: Mapping[str, Any], expected: RepairedReading
) -> RepairTargetNames:
    """The entire frozen table must match an independently replayed earlier prefix."""
    component = snapshot.get("components", {}).get("domain", {}).get("repair_targets")
    names = RepairTargetNames.from_component(component)
    if names.component() != RepairTargetNames.of(expected.identity).component():
        raise ValueError("request repair targets differ from the verified prior projection")
    return names


def _answer_at(template: ReadingAnswer, typed: Mapping[Any, Any]) -> ReadingAnswer:
    values: dict[str, Any] = {"status": "PARTIAL", "explanation": "Verified accepted prefix."}
    for (collection, _), item in typed.items():
        values.setdefault(collection, []).append(item)
    values = {key: tuple(value) if isinstance(value, list) else value
              for key, value in values.items()}
    return type(template).model_validate(values)


class _RepairFold:
    """One disposable complete-batch reducer for verified and provisional inputs.

    Evidence decoding stays at the boundary. This fold owns no durable journal
    and changes science only through the existing bound repair projection.
    """

    def __init__(self, *, machine, context, selections, store, template, interactions, inherited_selections=None, projection_memo=None):
        self.machine, self.context = machine, context
        self.selections, self.store = selections, store
        self.template, self.interactions = template, interactions
        self.inherited_selections = inherited_selections
        self.projection_memo = projection_memo if projection_memo is not None else RepairProjectionMemo()
        self.commands: list[tuple[str, BoundSkeletonRepair]] = []
        self.active_repairs: dict[str, str] = {}
        self.finding_origins: dict[str, FactOrigin] = {}
        self.finding_births: dict[str, FactOrigin] = {}
        self.finding_actions: dict[str, FactOrigin] = {}
        self.active: Mapping[Any, Any] = {}
        self.typed: dict[Any, Any] = {}
        self.adapters: dict[str, TypeAdapter] = {}
        self.answer = _answer_at(template, self.typed)
        self.projected = self.project()

    def plan(self) -> VerifiedRepairPlan:
        surviving = tuple(
            command
            for incarnation, command in self.commands
            if self.active_repairs.get(command.item_id) == incarnation
        )
        return VerifiedRepairPlan(
            surviving, dict(self.finding_origins), dict(self.finding_births), ""
        )

    def project(self) -> RepairedReading:
        return self.plan().replay(
            self.answer,
            machine=self.machine,
            context=self.context,
            selections=self.selections,
            store=self.store,
            inherited_selections=self.inherited_selections,
            projection_memo=self.projection_memo,
        )

    def request(self, snapshot):
        return (
            verify_request_repair_targets(snapshot, self.projected),
            {g["route_group_id"]: g for g in self.projected.groups},
        )

    def advance(
        self,
        batch,
        *,
        request,
        targets,
        request_groups,
        actions,
        batch_events,
        pending_commands=None,
    ):
        for key in batch["removed"]:
            # A batch can create and withdraw an item before its final state.
            self.active.pop(key, None)
            self.typed.pop(key, None)
        for key, row in batch["updates"].items():
            previous = self.active.get(key)
            if (previous is None or previous["value"] != row["value"]
                    or previous["origin"] != row["origin"]):
                collection = key[0]
                if collection not in self.adapters:
                    self.adapters[collection] = TypeAdapter(
                        type(self.template).model_fields[collection].annotation)
                # Saved rows have the JSON contract shape (including arrays for
                # strict tuple fields); parse only the changed row in that mode.
                self.typed[key] = self.adapters[collection].validate_json(
                    canonical_json_bytes([row["value"]]))[0]
            self.active[key] = row
        self.answer = _answer_at(self.template, self.typed)
        if not cited_selection_ids(self.answer, self.selections) <= set(
            [*request["delivered_selection_ids"], *request.get("inherited_selection_ids", ())]
        ):
            raise ValueError("accepted finding cites a selection not delivered before its response")
        sha, call_index, _ = batch["batch"]
        for entry in batch["items"]:
            saved = entry["saved"]
            if saved["collection"] != "findings" or not entry.get(
                "field_changed", saved.get("outcome") == "SAVED"
            ):
                continue
            handle = saved["item_id"]
            origin = FactOrigin(
                interaction_ref=self.interactions[sha],
                call_index=call_index,
                action_id=entry["action_id"],
                item=entry["item"],
                change_index=0,
            )
            self.finding_actions[entry["action_id"]] = origin
            self.finding_origins[handle] = origin
        self.finding_origins = {
            key: value
            for key, value in self.finding_origins.items()
            if ("findings", key) in self.active
        }
        self.finding_births = {}
        for collection, key in self.active:
            if collection != "findings":
                continue
            if key not in self.finding_origins:
                raise ValueError("active finding lacks its accepted field action")
            creation = self.active[("findings", key)]["origin"]
            if creation is None or creation["creation_action_id"] not in self.finding_actions:
                raise ValueError("active finding lacks its original accepted birth")
            self.finding_births[key] = self.finding_actions[creation["creation_action_id"]]
        batch_births: dict[str, dict[str, FactOrigin]] = {}
        command_start = len(self.commands)
        current = reading_compound_view(self.answer, SelectionsHeld(self.selections), self.machine)
        for entry in batch["items"]:
            if entry["action_id"] not in actions:
                continue
            action = actions[entry["action_id"]]
            submitted = targets.resolve(entry["submitted"], strict=True)
            parsed = UnifiedSkeletonRepair.model_validate_json(canonical_json_bytes(submitted))
            gid = parsed.route_group_id
            groups = [g for g in self.projected.groups if g["route_group_id"] == gid]
            if len(groups) != 1 or gid not in self.projected.identity:
                raise ValueError("accepted repair requires its exact verified group")
            group = groups[0]
            base = read_fusion_result(canonical_json_bytes(group["result"]))
            shown_group = request_groups[gid]
            shown_base = read_fusion_result(canonical_json_bytes(shown_group["result"]))
            # Bind current delivered selections before the group adds its namespace.
            # An explicitly selected historical group source keeps its full old key.
            source_bind = reading_source_binder(base, self.selections)
            source_bound = deepcopy(submitted)
            for change in source_bound["repair"]["changes"]:
                bind_fact_change_names(change, subject=lambda token: token,
                                       reference=lambda token: token,
                                       source=lambda token: source_bind(token) if token in self.selections else token)
            raw = fusion_names(
                shown_base,
                locations={},
                history=(),
                readers=(),
                route_group_id=gid,
                read=shown_group.get("reads", {}),
            ).resolve(source_bound)
            parsed = UnifiedSkeletonRepair.model_validate_json(canonical_json_bytes(raw))
            bind = name_binder(base)
            sources = dict(base.sources)
            texts = {}
            source_bind = reading_source_binder(base, self.selections)
            for sid in [*request["delivered_selection_ids"], *request.get("inherited_selection_ids", ())]:
                if sid not in self.selections:
                    raise ValueError("delivered repair source has no immutable selection")
                selection = self.selections[sid]
                refs = (dict(selection["evidence_ref"]),)
                if source_bind(sid) in sources and sources[source_bind(sid)] != refs:
                    raise ValueError("repair source differs from its sealed identity")
                sources[source_bind(sid)] = refs
                texts[source_bind(sid)] = {**selection, "location": sid}
            base = base.model_copy(update={"sources": sources})
            base, bridges = prepare_finding_bridges(
                base,
                parsed.repair.changes,
                answer=self.answer,
                current=current,
                selections=self.selections,
                identity=self.projected.identity[gid],
                store=self.store,
                finding_origins=self.finding_origins,
                finding_births=self.finding_births,
            )
            if len(set(bridges.values())) != len(bridges):
                raise ValueError("finding bridge has ambiguous identity correspondence")
            bridge_targets = {cid: fid for fid, cid in bridges.items()}
            for change in raw["repair"]["changes"]:
                bind_fact_change_names(
                    change,
                    subject=lambda token, targets=bridge_targets: targets.get(token, token),
                    reference=lambda token, targets=bridge_targets: targets.get(token, token),
                    source=source_bind,
                )
            parsed = UnifiedSkeletonRepair.model_validate_json(canonical_json_bytes(raw))
            prefix, bound = binder_spec(base)
            normalization = dict(
                prefix=prefix,
                bound=bound,
                known={f.fact_id for f in base.facts},
                delivered_source_ids=set(texts),
                inherited_source_ids=frozenset(s for f in base.facts for s in fact_source_ids(f)),
            )
            if pending_commands is not None:
                changes, command = reconstructed_repair_command(parsed.repair, **normalization)
                pending_commands[entry["item"]] = command
            else:
                changes = verified_repair_command(
                    parsed.repair,
                    action["command_evidence"],
                    **normalization,
                )
            original = FactOrigin.model_validate(action["origin"])
            origins = {
                change.fact_id: original.model_copy(update={"change_index": i})
                for i, change in enumerate(changes)
            }
            known = {f.fact_id for f in base.facts}
            # The full original birth snapshot also binds referenced identities.
            # Replay merges only actual target births as scientific support.
            births = dict(base.fact_creation_origins)
            shown_ids = {fact.fact_id for fact in shown_base.facts}
            for submitted_change, change in zip(parsed.repair.changes, changes, strict=True):
                if submitted_change.fact_id in shown_ids and change.fact_id not in known:
                    raise ValueError("selected repair target was withdrawn before this batch")
                for ref in fact_references(submitted_change):
                    if ref in shown_base.fact_creation_origins:
                        births[bind(ref)] = shown_base.fact_creation_origins[ref]
            created = batch_births.setdefault(gid, {})
            for fid, origin in origins.items():
                if fid not in known:
                    births[fid] = created.setdefault(fid, origin)
            self.commands.append(
                (
                    action["incarnation"],
                    BoundSkeletonRepair(
                        parsed.item_id or action["value"]["item_id"],
                        gid,
                        changes,
                        origins,
                        births,
                        base.sources,
                        texts,
                    ),
                )
            )
        for event in batch_events:
            if event["kind"] == "WRITE":
                action = actions[event["action_id"]]
                self.active_repairs[action["value"]["item_id"]] = action["incarnation"]
            else:
                self.active_repairs.pop(event["item_id"])
        # Earlier children may refer to a new object defined later in this
        # atomic batch. Fill only absent births from actual same-batch targets;
        # never overwrite the original identity selected by an old reference.
        for i in range(command_start, len(self.commands)):
            incarnation, command = self.commands[i]
            births = {**batch_births.get(command.route_group_id, {}), **command.proven_births}
            self.commands[i] = incarnation, replace(command, proven_births=births)
        # Only a whole accepted atomic repair candidate proves its closure.
        self.projected = self.project()


def _verified_history(
    receipt,
    *,
    machine: Mapping[str, Any],
    store,
    accepted_refs: Sequence[ArtifactRef],
    restore_domain,
    allow_call_prefix: bool = False,
    inherited_selections=None,
    projection_memo=None,
) -> tuple[VerifiedRepairPlan, _RepairFold]:
    """Prove original request binding and each complete accepted repair candidate.

    `machine` is the exact task-time baseline, not the later cumulative machine.
    A caller may apply the returned bound plan to either only after this proof.
    In-flight unsealed writes are deliberately outside this historical entry point.
    Finding source and birth authority is checked here; the caller retains the
    ordinary complete Reading validation and method/scope context.
    """
    if receipt.input_ref is None or receipt.input_ref not in accepted_refs:
        raise ValueError("sparse repair plan requires its accepted sealed task input")
    task_input = json.loads(read_artifact(store.read_bytes, receipt.input_ref))
    if reading_repair_mode(task_input) != SPARSE:
        raise ValueError("sparse repair history requires an explicit sealed sparse mode")
    if task_input.get("baseline_machine_sha256") != content_hash(machine):
        raise ValueError("sparse repair plan baseline differs from its sealed input")
    context = task_input["context"]
    template = receipt.raw_model()
    if not isinstance(template, ReadingAnswer):
        raise ValueError("sparse repair plan requires a Reading answer")
    history = InvestigationOrigins(store.read_bytes).verified_repair_history(
        receipt,
        restore_domain=restore_domain,
        allow_call_prefix=allow_call_prefix,
        require_command_receipts=True,
        accepted_refs=accepted_refs,
        include_request_context=True,
    )
    actions = {row["origin"]["action_id"]: row for row in history["actions"]}
    events: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for event in history["events"]:
        batch = (
            history["actions"][event["action_index"]]["batch"]
            if event["kind"] == "WRITE"
            else event["batch"]
        )
        events.setdefault(tuple(batch), []).append(event)
    interactions = {ref.sha256: ref for ref in receipt.audit_refs}
    fold = _RepairFold(
        machine=machine,
        context=context,
        selections=receipt.selections,
        store=store,
        template=template,
        interactions=interactions,
        inherited_selections=inherited_selections,
        projection_memo=projection_memo,
    )
    for request in history["request_contexts"]:
        targets, request_groups = fold.request(request["naming"])
        # This response's start stays frozen across every one of its calls.
        fold.request_context = request, targets, request_groups
        for batch in request["batches"]:
            batch_events = [
                {
                    **event,
                    "action_id": history["actions"][event["action_index"]]["origin"]["action_id"],
                }
                if event["kind"] == "WRITE"
                else event
                for event in events.get(tuple(batch["batch"]), ())
            ]
            fold.advance(
                batch,
                request=request,
                targets=targets,
                request_groups=request_groups,
                actions=actions,
                batch_events=batch_events,
            )
    if fold.active_repairs != history["active"]:
        raise ValueError("verified repair plan does not match the accepted active incarnations")
    result = fold.plan()
    result = replace(
        result,
        generation=content_hash(
            {
                "events": history["events"],
                "accepted_batches": [
                    [
                        batch["batch"],
                        [
                            (entry["action_id"], entry["saved"]["outcome"])
                            for entry in batch["items"]
                        ],
                    ]
                    for request in history["request_contexts"]
                    for batch in request["batches"]
                ],
                "commands": [
                    [
                        incarnation,
                        command.item_id,
                        [c.model_dump(mode="json", exclude_unset=True) for c in command.changes],
                        {
                            fid: origin.model_dump(mode="json")
                            for fid, origin in command.origins.items()
                        },
                    ]
                    for incarnation, command in fold.commands
                ],
                "findings": {
                    key: value.model_dump(mode="json")
                    for key, value in fold.finding_origins.items()
                },
            }
        ),
    )
    return result, fold


def verify_sparse_repair_plan(receipt, **kwargs) -> VerifiedRepairPlan:
    """Verify accepted sealed history; provisional candidates cannot enter here."""
    return _verified_history(receipt, **kwargs)[0]


@dataclass(frozen=True)
class RepairPreview:
    """Ephemeral validation result, never a sealed acceptance or durable generation."""

    projection: RepairedReading
    commands: Mapping[str, Mapping[str, Any]]
    creation_origins: Mapping[tuple[str, str], Mapping[str, str]]


def preview_sparse_repairs(
    receipt, flight, *, machine, store, accepted_refs, restore_domain
) -> RepairPreview:
    """Validate actual in-memory batches after a strictly verified call baseline.

    No fake call receipt is constructed. Each preview owns its disposable fold,
    so a failed batch or registration cannot mutate the accepted history.
    """
    return prepare_sparse_repair_preview(
        receipt,
        machine=machine,
        store=store,
        accepted_refs=accepted_refs,
        restore_domain=restore_domain,
    )(flight)


def prepare_sparse_repair_preview(receipt, **kwargs):
    """Advance only registered batches; pending validation owns an isolated branch."""
    _, fold = _verified_history(
        receipt,
        allow_call_prefix=True,
        **kwargs,
    )
    baseline = receipt.raw_model()
    accepted = ()
    call = None

    def preview(flight):
        nonlocal fold, accepted, call
        # Comparison refs are separately authenticated request inputs, absent from
        # the JSON Reading answer. They are not skeleton-repair dependencies.
        comparison = flight.baseline.model_copy(update={"findings": tuple(
            finding.model_copy() for finding in flight.baseline.findings
        )})
        for finding in comparison.findings:
            if hasattr(finding, "_measurement_binding"):
                finding._measurement_binding = None
        if baseline != comparison:
            raise ValueError("live repair history differs from its call-start answer")
        identity = (flight.context.interaction_sha256, flight.context.call_index)
        if call is not None and call != identity:
            raise ValueError("live repair preview belongs to a different call")
        call = identity
        if flight.accepted[:len(accepted)] != accepted:
            raise ValueError("live repair accepted prefix changed within its call")
        if len(flight.accepted) > len(accepted):
            # Decode actual saved replies through the same reducer. A validated
            # pending candidate is not acceptance: registration may still fail
            # or return a reconciled value. Do not promote that trial branch.
            advanced = _fork_repair_fold(fold)
            _preview_sparse_repairs(advanced, receipt.execution_id, flight,
                                    accepted_start=len(accepted), include_pending=False)
            accepted += deepcopy(flight.accepted[len(accepted):])
            fold = advanced
        return _preview_sparse_repairs(
            _fork_repair_fold(fold), receipt.execution_id, flight,
            accepted_start=len(accepted),
        )

    return preview


def _fork_repair_fold(fold):
    branch = copy(fold)
    branch.commands = list(fold.commands)
    # Rows are replaced or removed by the shared reducer, never edited in place.
    branch.active = dict(fold.active)
    branch.typed = dict(fold.typed)
    branch.adapters = dict(fold.adapters)
    for attr in ("active_repairs", "finding_origins", "finding_births", "finding_actions"):
        setattr(branch, attr, dict(getattr(fold, attr)))
    return branch


def _preview_sparse_repairs(fold, execution_id, flight, *, accepted_start=0, include_pending=True):
    ctx = flight.context
    if ctx.naming is None or not hasattr(fold, "request_context"):
        raise ValueError("live repair requires its verified frozen request")
    request, targets, request_groups = fold.request_context
    if (
        ctx.naming.snapshot() != request["naming"]
        or ctx.record["iteration"] != request["iteration"]
        or ctx.interaction_sha256 not in fold.interactions
    ):
        raise ValueError("live repair context differs from its accepted response")
    commands = {}
    batches = [(entries, False) for entries in flight.accepted[accepted_start:]]
    if include_pending and flight.candidate is not None and flight.pending:
        batches.append((flight.pending, True))
    for batch_index, (entries, provisional) in enumerate(batches, start=accepted_start):
        active = dict(fold.active)
        touched, removed_keys = set(), {}
        active_repairs = dict(fold.active_repairs)
        items, actions, events = [], {}, []
        final_values = {
            (name, value["item_id"]): value
            for name, values in (
                flight.candidate.model_dump(mode="json") if provisional else {}
            ).items()
            if isinstance(values, list)
            for value in values
            if isinstance(value, dict) and value.get("item_id")
        }
        for entry in entries:
            key = entry["item"]
            collection = key.split(":")[0]
            action_id = write_action_id(ctx.interaction_sha256, ctx.call_index, key)
            if entry["value"] is None:
                removed = withdraw_item_state(active, entry["submitted"])
                touched.add(removed)
                removed_keys[removed] = None
                if removed[0] == "skeleton_repairs":
                    active_repairs.pop(removed[1])
                    events.append({"kind": "WITHDRAW", "item_id": removed[1]})
                continue
            value = entry["value"]
            handle = value["item_id"]
            if provisional:
                # The ordinary writer already performed scope/measurement
                # inheritance. Use that complete candidate, never repeat it here.
                saved = {
                    "collection": collection,
                    "item_id": handle,
                    "value": final_values[(collection, handle)],
                    "location": entry["location"],
                }
                changed = entry["candidate_changed"]
            else:
                saved = entry["saved"]
                changed = saved["outcome"] == "SAVED"
            origin = write_item_state(
                active,
                collection=collection,
                item_id=handle,
                location=saved["location"],
                value=saved["value"],
                new_origin={
                    "execution_id": execution_id,
                    "collection": collection,
                    "item_id": handle,
                    "creation_action_id": action_id,
                }
                if changed
                else None,
            )
            touched.add((collection, handle))
            items.append(
                {
                    "item": key,
                    "submitted": entry["submitted"],
                    "saved": saved,
                    "action_id": action_id,
                    "field_changed": changed,
                }
            )
            if collection == "skeleton_repairs":
                if origin is None:
                    raise ValueError("live repair lacks its original wrapper birth")
                incarnation = origin["creation_action_id"]
                active_repairs[handle] = incarnation
                actions[action_id] = {
                    "value": value,
                    "incarnation": incarnation,
                    "origin": FactOrigin(
                        interaction_ref=fold.interactions[ctx.interaction_sha256],
                        call_index=ctx.call_index,
                        action_id=action_id,
                        item=key,
                        change_index=0,
                    ).model_dump(mode="json"),
                    **({"command_evidence": entry["repair_command"]} if not provisional else {}),
                }
                events.append({"kind": "WRITE", "action_id": action_id})
        if provisional and {key: row["value"] for key, row in active.items()} != final_values:
            raise ValueError("live candidate changes an item outside its recorded pending batch")
        fold.advance(
            {
                "batch": (ctx.interaction_sha256, ctx.call_index, batch_index),
                "items": items,
                "updates": {key: deepcopy(value) for key, value in active.items() if key in touched},
                "removed": tuple(removed_keys),
            },
            request=request,
            targets=targets,
            request_groups=request_groups,
            actions=actions,
            batch_events=events,
            pending_commands=commands if provisional else None,
        )
        if fold.active_repairs != active_repairs:
            raise ValueError("live repair lifecycle differs from its candidate")
    return RepairPreview(fold.projected, commands, {
        key: dict(row["origin"]) for key, row in fold.active.items() if row["origin"] is not None
    })
