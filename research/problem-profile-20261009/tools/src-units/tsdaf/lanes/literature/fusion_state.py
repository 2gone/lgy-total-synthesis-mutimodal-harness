"""Apply bounded scientific changes before the single public projection."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from pydantic_core import to_jsonable_python

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.literature.source_parts import fact_source_ids
from tsdaf.lanes.literature.gate_models import (
    FactCandidate,
    FactChange,
    FactConflict,
    FactOrigin,
    FusionProposal,
    FusionResult,
    ScientificFact,
    SealedQuestion,
    as_recorded,
)


def _repeated(ids: Iterable[str]) -> list[str]:
    """The ids that occur more than once, in first-seen order."""
    counts = Counter(ids)
    return [key for key, count in counts.items() if count > 1]


def canonical_fact_id(facts: dict[str, ScientificFact], fact_id: str) -> str:
    seen: set[str] = set()
    while facts[fact_id].same_as is not None:
        if fact_id in seen:
            raise ValueError(f"identity relation contains a cycle: {fact_id}")
        seen.add(fact_id)
        target = facts[fact_id].same_as
        assert target is not None
        if target not in facts or facts[target].kind != facts[fact_id].kind:
            raise ValueError(
                f"identity requires an existing fact of the same kind: {fact_id} same_as {target}"
            )
        fact_id = target
    return fact_id


def _conflict_id(subject: str) -> str:
    return "CONFLICT-" + hashlib.sha256(subject.encode()).hexdigest()[:16]


def _unseen_conflicts(current: FusionResult, before: FusionResult) -> dict[str, set[str]]:
    return {
        fid: {
            field
            for field, values in conflict.candidates.items()
            if fid not in before.conflicts
            or {
                canonical_json_bytes(c.value)
                for c in before.conflicts[fid].candidates.get(field, ())
            }
            != {canonical_json_bytes(c.value) for c in values}
        }
        for fid, conflict in current.conflicts.items()
    }


def validate_result(result: FusionResult) -> None:
    facts = {f.fact_id: f for f in result.facts}

    known = result.fields_known

    if len(facts) != len(result.facts):
        raise ValueError(
            "scientific fact IDs collide: " + ", ".join(_repeated(f.fact_id for f in result.facts))
        )
    for fid, conflict in result.conflicts.items():
        if (
            fid not in facts
            or not conflict.candidates.keys() <= ScientificFact.model_fields.keys() - {"fact_id"}
        ):
            raise ValueError("conflict references unknown fact fields")
        if any(
            not set(c.source_ids) <= result.sources.keys()
            for readings in conflict.candidates.values()
            for c in readings
        ):
            raise ValueError("conflict candidate has unknown source evidence")
        for field, readings in conflict.candidates.items():
            for candidate in readings:
                reading = ScientificFact.model_validate_json(
                    canonical_json_bytes({**facts[fid].model_dump(), field: candidate.value})
                )
                if field in {"source_ids", "source_parts"} and not set(fact_source_ids(reading)) <= result.sources.keys():
                    raise ValueError("conflict candidate has an unknown source value")
                references = fact_references(reading, {field})
                if not set(references) <= facts.keys():
                    raise ValueError("conflict candidate has a dangling scientific reference")
                if field == "measurement" and reading.measurement is not None:
                    target = reading.measurement.same_as
                    if reading.kind != "OUTCOME" or (target is not None and (
                        target == fid or facts[target].kind != "OUTCOME"
                    )):
                        raise ValueError("measurement candidate requires another OUTCOME")
    segments = {s.segment_id: s for s in result.segments}
    if len(segments) != len(result.segments) or segments.keys() & facts.keys():
        raise ValueError(
            "navigation IDs collide: "
            + ", ".join(
                sorted(
                    {
                        *_repeated(s.segment_id for s in result.segments),
                        *(segments.keys() & facts.keys()),
                    }
                )
            )
        )
    for segment in result.segments:
        missing = sorted(
            (set(segment.fact_ids) - facts.keys())
            | (set(segment.source_ids) - result.sources.keys())
        )
        if missing:
            raise ValueError(
                f"navigation {segment.segment_id} names what is not on record: "
                + ", ".join(missing)
            )
    for fact in facts.values():
        if fact.preparation_use is not None and (
            fact.kind != "NOTE" or len(fact.owners) != 1
            or fact.owners[0] not in facts or facts[fact.owners[0]].kind != "EXPERIMENT"
        ):
            raise ValueError("a preparation-use NOTE must name exactly one EXPERIMENT owner")
        if fact.source_parts and fact.kind != "EXPERIMENT":
            raise ValueError("account source_parts belong only to an EXPERIMENT")
        if not set(fact_source_ids(fact)) <= result.sources.keys():
            raise ValueError(f"unknown evidence source on {fact.fact_id}")
        if (
            known(fact, "status", "source_ids", "source_parts")
            and fact.status in {"ACCEPTED", "CONTEXT"}
            and not any(result.sources[s] for s in fact_source_ids(fact))
        ):
            raise ValueError(f"accepted fact needs verified source evidence: {fact.fact_id}")
        if not fact_references(fact, _RELATION_FIELDS) <= facts.keys():
            raise ValueError(f"dangling scientific reference on {fact.fact_id}")
        if (
            known(fact, "status")
            and fact.status != "REJECTED"
            and any(
                known(fact, field)
                and known(facts[ref], "status")
                and facts[ref].status == "REJECTED"
                for field in _RELATION_FIELDS
                for ref in fact_references(fact, {field})
            )
        ):
            raise ValueError(f"active fact refers to a withdrawn fact: {fact.fact_id}")
        if fact.measurement is not None:
            if fact.kind != "OUTCOME":
                raise ValueError("measurement intent belongs only to an OUTCOME")
            target = fact.measurement.same_as
            if target is not None and (target not in facts or facts[target].kind != "OUTCOME" or target == fact.fact_id):
                raise ValueError("same measurement must name another existing OUTCOME")
        if known(fact, "same_as", "kind") and fact.same_as is not None:
            canonical_fact_id(facts, fact.fact_id)
        if (
            known(fact, "status", "kind", "same_as")
            and fact.status == "ACCEPTED"
            and fact.same_as is None
        ):
            if (
                fact.kind in {"OPERATION", "MATERIAL", "CONDITION", "OUTCOME", "TARGET", "NOTE"}
                and known(fact, "text")
                and not fact.text.strip()
            ):
                raise ValueError(f"accepted fact needs actual content: {fact.fact_id}")
            if (
                fact.kind == "COMPOUND"
                and known(fact, "labels", "text")
                and not fact.labels
                and not fact.text.strip()
            ):
                raise ValueError(f"compound needs a source-grounded label or name: {fact.fact_id}")
            if fact.kind == "REACTION" and (
                (known(fact, "inputs") and not fact.inputs)
                or (known(fact, "outputs") and not fact.outputs)
            ):
                raise ValueError(f"accepted reaction needs explicit endpoints: {fact.fact_id}")
            allowed_owners = {
                "OPERATION": {"REACTION", "EXPERIMENT"},
                "MATERIAL": {"OPERATION"},
                "CONDITION": {"OPERATION", "MATERIAL"},
                "OUTCOME": {"OPERATION", "REACTION", "EXPERIMENT"},
                "TARGET": {"COMPOUND"},
            }.get(fact.kind)
            if allowed_owners is not None and known(fact, "owners"):
                wrong = [
                    f"{o} ({facts[o].kind})"
                    for o in fact.owners
                    if (known(facts[o], "kind") and facts[o].kind not in allowed_owners)
                    or (known(facts[o], "status") and facts[o].status == "REJECTED")
                ]
                if (not fact.owners and fact.kind != "OUTCOME") or wrong:
                    # The sentence names the owner and its kind beside the kinds allowed:
                    # a live model resent 54 outcomes owned by reactions three times
                    # because "owners of the right kind" never said which kind is right.
                    raise ValueError(
                        f"{fact.fact_id} ({fact.kind}) "
                        + (
                            "names no owner"
                            if not fact.owners
                            else "names owners of the wrong kind or rejected: " + ", ".join(wrong)
                        )
                        + f"; a {fact.kind} is owned by "
                        + " or ".join(sorted(allowed_owners))
                        + " facts"
                    )
            if any(
                known(fact, field) and known(facts[o], "kind") and facts[o].kind != "COMPOUND"
                for field in ("inputs", "outputs")
                for o in getattr(fact, field)
            ):
                raise ValueError(f"endpoints must reference compounds: {fact.fact_id}")
            if known(fact, "after") and any(
                known(facts[o], "kind") and facts[o].kind != "OPERATION" for o in fact.after
            ):
                raise ValueError(f"operation order must reference operations: {fact.fact_id}")
    for question in result.questions:
        unknown = sorted(set(question.subject_ids) - (facts.keys() | segments.keys()))
        if unknown:
            raise ValueError(f"unknown question subject: {', '.join(unknown)}")


def _change_values(
    old: ScientificFact | Mapping[str, Any] | None, change: FactChange
) -> dict[str, Any]:
    values = change.model_dump(exclude_unset=True, exclude={"clear", "withdraw", "add_source_ids"})
    for name in change.clear:
        values[name] = ScientificFact.model_fields[name].default
    if change.withdraw:
        values["status"] = "REJECTED"
    if change.add_source_ids:
        prior_sources = (
            old.source_ids
            if isinstance(old, ScientificFact)
            else old.get("source_ids", ())
            if old is not None
            else ()
        )
        values["source_ids"] = tuple(
            dict.fromkeys(
                (
                    *values.get("source_ids", prior_sources),
                    *change.add_source_ids,
                )
            )
        )
    return values


def _update_fact(old: ScientificFact | None, values: Mapping[str, Any]) -> ScientificFact:
    return ScientificFact.model_validate({**(old.model_dump() if old else {}), **values})


class _FactUpdates:
    """One field/origin/conflict updater; drafts never escape as scientific facts."""

    def __init__(self, base: FusionResult, *, deferred: bool = False) -> None:
        self.deferred = deferred
        self.facts: dict[str, ScientificFact | dict[str, Any]] = {f.fact_id: f for f in base.facts}
        self.conflicts = dict(base.conflicts)
        self.field_origins = {fid: dict(fields) for fid, fields in base.field_origins.items()}
        self.creation_origins = dict(base.fact_creation_origins)

    def apply(
        self, change: FactChange, *, origin: FactOrigin | None, birth: FactOrigin | None
    ) -> None:
        old = self.facts.get(change.fact_id)
        values = _change_values(old, change)
        if self.deferred:
            prior = old.model_dump() if isinstance(old, ScientificFact) else old or {}
            self.facts[change.fact_id] = {**prior, **values}
        else:
            assert old is None or isinstance(old, ScientificFact)
            self.facts[change.fact_id] = _update_fact(old, values)
        fields = self.field_origins.setdefault(change.fact_id, {})
        if old is None and birth is not None:
            self.creation_origins[change.fact_id] = birth
        for field in values.keys() - {"fact_id"}:
            incoming = (origin,) if origin is not None else ()
            if (
                field == "source_ids"
                and change.add_source_ids
                and field not in change.model_fields_set
            ):
                incoming = _origin_union(fields.get(field, ()), incoming)
            fields[field] = incoming
        conflict = self.conflicts.pop(change.fact_id, None)
        resolved = set(values)
        if change.add_source_ids and "source_ids" not in change.model_fields_set:
            resolved.discard("source_ids")
        if conflict is not None and (
            remaining := {k: v for k, v in conflict.candidates.items() if k not in resolved}
        ):
            self.conflicts[change.fact_id] = FactConflict(candidates=remaining)

    def result(self, base: FusionResult) -> FusionResult:
        return base.model_copy(
            update={
                "facts": tuple(ScientificFact.model_validate(f) for f in self.facts.values()),
                "fact_creation_origins": dict(self.creation_origins),
                "field_origins": {
                    fid: {field: origins for field, origins in fields.items() if origins}
                    for fid, fields in self.field_origins.items()
                    if any(fields.values())
                },
                "conflicts": dict(self.conflicts),
            }
        )


def final_fact_keys(base: FusionResult, commands: Sequence[Sequence[FactChange]]) -> set[str]:
    """Mechanical support lookup using the same updater, never a scientific result.

    Missing definitions and births may be supplied by an explicit finding bridge.
    The caller still proves authority and validates the whole candidate afterward.
    """
    updates = _FactUpdates(base, deferred=True)
    for changes in commands:
        for change in changes:
            updates.apply(change, origin=None, birth=None)
    return set(updates.facts) | {
        ref for fact in updates.facts.values() for ref in fact_references(fact)
    }


def fold_verified_fact_commands(
    base: FusionResult,
    commands: Sequence[tuple[Sequence[FactChange], Mapping[str, FactOrigin]]],
    *,
    proven_births: Mapping[str, FactOrigin],
) -> FusionResult:
    """Fold proved surviving contributions in order, validate the whole candidate.

    This is not a new-write entry point. Original acceptance, binding and source
    authority must already be proved. A temporarily incomplete draft receives no
    invented fields or birth, cannot be projected, and is never returned on failure.
    """
    updates = _FactUpdates(base, deferred=True)
    baseline = {fact.fact_id for fact in base.facts}
    for changes, origins in commands:
        for change in changes:
            if change.fact_id not in origins:
                raise ValueError("verified command lacks its original field action")
            if change.fact_id not in baseline and change.fact_id not in proven_births:
                raise ValueError("verified command lacks its historical birth")
            updates.apply(
                change, origin=origins[change.fact_id], birth=proven_births.get(change.fact_id)
            )
    result = updates.result(base)
    validate_result(result)
    return result


def apply_proposal(
    base: FusionResult,
    proposal: FusionProposal,
    *,
    origins: Mapping[str, FactOrigin] | None = None,
    proven_births: Mapping[str, FactOrigin] | None = None,
) -> FusionResult:
    """Atomic sparse changes. Omitted facts and questions are retained exactly.

    A contribution fold may supply historical births proved by the accepted audit
    and original target binding. These never supply scientific fields or field
    support, and never replace the birth of an object already present in base.
    """
    updates = _FactUpdates(base)
    changes = (*proposal.changes, *(c for group in proposal.groups for c in group.changes))
    for change in changes:
        origin = origins.get(change.fact_id) if origins is not None else None
        birth = (proven_births or {}).get(change.fact_id, origin)
        updates.apply(change, origin=origin, birth=birth)
    changed = updates.result(base)
    facts = {f.fact_id: f for f in changed.facts}
    segments = {s.segment_id: s for s in base.segments}
    questions = {q.question_id: q for q in base.questions}
    for question in proposal.questions:
        questions[question.question_id] = as_recorded(question)
    for segment in proposal.segments:
        segments[segment.segment_id] = segment
    assessments = {tuple(sorted(a.subject_ids)): a for a in base.experiment_assessments}
    for assessment in proposal.experiment_assessments:
        assessment_key = tuple(sorted(assessment.subject_ids))
        if assessment == assessments.get(assessment_key):
            # An exact echo of an assessment already on record is a no-op.
            continue
        unknown = sorted(set(assessment.subject_ids) - (facts.keys() | segments.keys()))
        if unknown:
            raise ValueError(
                f"experimental assessment references unknown subjects: {', '.join(unknown)}"
            )
        if unknown := sorted(set(assessment.source_ids) - base.sources.keys()):
            raise ValueError(
                "experimental assessment requires verified sources: " + ", ".join(unknown)
            )
        assessments[assessment_key] = assessment
    result = changed.model_copy(
        update={
            "questions": tuple(questions.values()),
            "segments": tuple(segments.values()),
            "overview": proposal.overview if proposal.overview is not None else base.overview,
            "experiment_assessments": tuple(assessments.values()),
            "note": proposal.note if proposal.note is not None else base.note,
        }
    )
    validate_result(result)
    return result


class StaleFactChange(ValueError):
    """A written field or referenced identity changed after reading."""

    def __init__(self, fact_id: str, seen: object, current: object):
        super().__init__(f"read version changed for {fact_id}; reread before changing it")
        self.fact_id, self.seen, self.current = fact_id, seen, current


_RELATION_FIELDS = frozenset({"owners", "inputs", "outputs", "after", "same_as", "measurement"})
_IDENTITY_FIELDS = frozenset({"kind", "status", "same_as", "measurement"})


def fact_references(
    fact: ScientificFact | FactChange | Mapping[str, Any],
    fields: set[str] | frozenset[str] = _RELATION_FIELDS,
) -> set[str]:
    def value(obj: Any, name: str) -> Any:
        return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)

    refs = {
        ref
        for name in fields & (_RELATION_FIELDS - {"measurement"})
        for ref in ((value(fact, name),) if name == "same_as" else (value(fact, name) or ()))
        if ref is not None
    }
    measurement = value(fact, "measurement")
    if "measurement" in fields and measurement is not None and value(measurement, "same_as"):
        refs.add(value(measurement, "same_as"))
    return refs


def _check_fact_change(
    current: Mapping[str, ScientificFact],
    before: Mapping[str, ScientificFact],
    fact_id: str,
    values: Mapping[str, Any],
    updated: ScientificFact,
    *,
    additive_evidence: bool,
    proposed: Mapping[str, Mapping[str, Any]],
    unseen_conflicts: Mapping[str, set[str]],
) -> None:
    """Check actual writes and identity; scientific dependencies belong to the model."""
    old = before.get(fact_id)
    fields = set(values) - {"fact_id"}
    required = fields | _IDENTITY_FIELDS
    if additive_evidence:
        required.discard("source_ids")
    requirements = {fact_id: required}
    if updated.status != "REJECTED":
        for relation in fields & _RELATION_FIELDS:
            refs = fact_references(updated, {relation}) | (
                fact_references(old, {relation}) if old else set()
            )
            for ref in refs:
                if ref not in before and ref not in current:
                    continue
                requirements.setdefault(ref, set()).update(_IDENTITY_FIELDS)
    # Retraction/identity changes cannot silently change incoming active relationships.
    if fields & _IDENTITY_FIELDS:
        for ref, fact in current.items():
            if fact.status != "REJECTED" and fact_id in fact_references(fact, _RELATION_FIELDS):
                requirements.setdefault(ref, set()).update(_RELATION_FIELDS | {"kind", "status"})
    absent = object()
    for ref, names in requirements.items():
        if pending := names & unseen_conflicts.get(ref, set()):
            raise StaleFactChange(
                ref, {"conflicted_fields": ()}, {"conflicted_fields": tuple(sorted(pending))}
            )
        previous, now = before.get(ref), current.get(ref)
        changed = {
            name
            for name in names
            if getattr(previous, name, absent) != getattr(now, name, absent)
            and proposed.get(ref, {}).get(name, absent) != getattr(now, name, absent)
        }
        if changed:
            raise StaleFactChange(
                ref,
                previous.model_dump(include=changed) if previous else None,
                now.model_dump(include=changed) if now else None,
            )


def validate_read_versions(
    current: FusionResult,
    changes: Sequence[FactChange],
    seen_results: Mapping[str, Mapping[str, FusionResult]],
    *,
    created_in_submission: Sequence[ScientificFact] = (),
) -> None:
    """Use each actual read snapshot; agreeing writes and independent fields commute."""
    current_facts = {f.fact_id: f for f in current.facts}
    snapshots = {}
    unseen = {}
    reads_by_identity = {
        id(snapshot): snapshot for reads in seen_results.values() for snapshot in reads.values()
    }
    facts_by_snapshot = {
        key: {f.fact_id: f for f in snapshot.facts} for key, snapshot in reads_by_identity.items()
    }
    unseen_by_snapshot = {
        key: _unseen_conflicts(current, snapshot) for key, snapshot in reads_by_identity.items()
    }
    for fid, reads in seen_results.items():
        own = reads.get(fid)
        facts = dict(facts_by_snapshot[id(own)]) if own else {}
        unseen[fid] = (
            dict(unseen_by_snapshot[id(own)])
            if own
            else {ref: set(conflict.candidates) for ref, conflict in current.conflicts.items()}
        )
        # Each change binds its own read and its explicitly referenced target reads.
        facts.update(
            {
                ref: facts_by_snapshot[id(snapshot)][ref]
                for ref, snapshot in reads.items()
                if ref in facts_by_snapshot[id(snapshot)]
            }
        )
        snapshots[fid] = facts
        for ref, snapshot in reads.items():
            unseen[fid][ref] = unseen_by_snapshot[id(snapshot)].get(ref, set())
    values = {
        c.fact_id: _change_values(snapshots.get(c.fact_id, {}).get(c.fact_id), c) for c in changes
    }
    for change in changes:
        if change.fact_id not in snapshots and change.fact_id in current_facts:
            raise ValueError(f"existing facts require an editable read location: {change.fact_id}")
        before = snapshots.get(change.fact_id, {})
        _check_fact_change(
            current_facts,
            before,
            change.fact_id,
            values[change.fact_id],
            _update_fact(
                snapshots.get(change.fact_id, {}).get(change.fact_id), values[change.fact_id]
            ),
            additive_evidence=bool(change.add_source_ids)
            and "source_ids" not in change.model_fields_set,
            proposed={
                **{fact.fact_id: {name: getattr(fact, name) for name in type(fact).model_fields}
                   for fact in created_in_submission},
                **values,
            },
            unseen_conflicts=unseen.get(change.fact_id, {}),
        )


def linked_change_batches(
    changes: Sequence[FactChange], existing_ids: set[str]
) -> tuple[tuple[int, ...], ...]:
    """Group references to new objects so creation never leaves dangling references."""

    by_id = {change.fact_id: index for index, change in enumerate(changes)}
    components = [{index} for index in range(len(changes))]
    for index, change in enumerate(changes):
        for ref in (
            *(change.owners or ()),
            *(change.inputs or ()),
            *(change.outputs or ()),
            *(change.after or ()),
            *((change.same_as,) if change.same_as else ()),
            *((change.measurement.same_as,)
              if getattr(change, "measurement", None) and change.measurement.same_as else ()),
        ):
            if ref not in by_id or ref in existing_ids:
                continue
            left = next(group for group in components if index in group)
            right = next(group for group in components if by_id[ref] in group)
            if left is not right:
                left.update(right)
                components.remove(right)
    return tuple(tuple(sorted(group)) for group in sorted(components, key=min))


def _origin_union(*groups: Sequence[FactOrigin]) -> tuple[FactOrigin, ...]:
    """Union accepted audit addresses, never reconstruct an address from its value."""
    unique = {
        canonical_json_bytes(origin.model_dump(mode="json")): origin
        for group in groups for origin in group
    }
    return tuple(unique[key] for key in sorted(unique))


def _candidate_union(readings: Iterable[FactCandidate]) -> tuple[FactCandidate, ...]:
    choices: dict[bytes, FactCandidate] = {}
    for reading in readings:
        key = canonical_json_bytes(reading.model_dump(mode="json", exclude={"origins"}))
        previous = choices.get(key)
        choices[key] = reading.model_copy(update={
            "origins": _origin_union(previous.origins if previous else (), reading.origins)
        })
    return tuple(choices[key] for key in sorted(choices))


def _restated(
    before: ScientificFact | None,
    other: ScientificFact | None,
    final: ScientificFact,
    field: str,
) -> bool:
    """True when `field` holds the same value before, on the other side and after."""
    if before is None or other is None:
        return False
    value = getattr(final, field)
    return bool(getattr(before, field) == value and getattr(other, field) == value)


def merge_child(
    parent: FusionResult,
    base: FusionResult,
    child: FusionResult,
    *,
    batches: Sequence[Sequence[FactChange]] = (),
    parent_batches: Sequence[Sequence[FactChange]] = (),
) -> FusionResult:
    """Merge sparse fields with the same preconditions and accepted atomic boundaries."""
    if parent.package_sha256 != child.package_sha256 or base.package_sha256 != child.package_sha256:
        raise ValueError("child scientific result belongs to another package")
    current = {f.fact_id: f for f in parent.facts}
    before = {f.fact_id: f for f in base.facts}
    child_facts = {f.fact_id: f for f in child.facts}
    conflicts = dict(parent.conflicts)
    field_origins = {fid: dict(fields) for fid, fields in parent.field_origins.items()}
    creation_origins = dict(parent.fact_creation_origins)
    for fid, origin in child.fact_creation_origins.items():
        if fid not in before and fid not in current:
            creation_origins[fid] = origin
    questions = {q.question_id: q for q in parent.questions}
    previous_questions = {q.question_id: q for q in base.questions}

    def unsettled(subject: str) -> bool:
        if subject in current:
            return subject in conflicts
        qid = _conflict_id(subject)
        question = questions.get(qid)
        return question is not None and question.status != "RESOLVED"

    for q in child.questions:
        existing_question = questions.get(q.question_id)
        if previous_questions.get(q.question_id) != q and (
            existing_question is None
            or existing_question == previous_questions.get(q.question_id)
            or existing_question == q
        ):
            questions[q.question_id] = q

    def retain_conflict(subject: str, references: set[str]) -> None:
        qid = _conflict_id(subject)
        previous = questions.get(qid)
        references.add(subject)
        if previous is not None and previous.status != "RESOLVED":
            references.update(previous.subject_ids)
        questions[qid] = SealedQuestion(
            question_id=qid,
            subject_ids=tuple(sorted(references)),
            question="Concurrent or stale local revisions disagree; all readings are retained.",
            status="UNRESOLVED",
            explanation=(
                "Merge cannot pick between concurrent revisions without a reconciling read."
            ),
        )

    changed = {
        fid: fact for fid, fact in child_facts.items()
        if before.get(fid) != fact or child.field_origins.get(fid) != base.field_origins.get(fid)
    }

    def delta(fact: ScientificFact) -> dict[str, Any]:
        old = before.get(fact.fact_id)
        return {
            name: value
            for name, value in fact.model_dump().items()
            if name != "fact_id" and (old is None or getattr(old, name) != value)
        }

    values = {fid: delta(fact) for fid, fact in changed.items()}
    parent_values = {fid: delta(fact) for fid, fact in current.items()}
    for side, modifications, final in (
        (child, values, child_facts), (parent, parent_values, current)
    ):
        for fid, fields in side.field_origins.items():
            for field, origins in fields.items():
                if origins != base.field_origins.get(fid, {}).get(field, ()):
                    modifications.setdefault(fid, {})[field] = getattr(final[fid], field)
    atomic = []
    replaced_sources = {
        side: {c.fact_id for batch in accepted for c in batch if "source_ids" in c.model_fields_set}
        for side, accepted in ((True, batches), (False, parent_batches))
    }
    for accepted, changes, final_facts, other_facts in (
        (batches, values, child_facts, current),
        (parent_batches, parent_values, current, child_facts),
    ):
        later_points: set[tuple[str, str]] = set()
        for batch in reversed(accepted):
            points = {
                (c.fact_id, field)
                for c in batch
                for field in _change_values(before.get(c.fact_id), c)
                if field != "fact_id"
                # A field written with the value it already had, on both sides, is not
                # a change to reconcile: a window that restated `kind: COMPOUND` on two
                # compounds in a 31-change batch while its siblings had cited them had
                # every fact of the batch, the paper's target among them, marked as a
                # conflict of a value against itself (JOC r6, E56).
                and not (
                    _restated(
                        before.get(c.fact_id),
                        other_facts.get(c.fact_id),
                        final_facts[c.fact_id],
                        field,
                    )
                    # ...unless a conflict on that field is what the restatement settles.
                    and not any(
                        c.fact_id in side.conflicts
                        and field in side.conflicts[c.fact_id].candidates
                        for side in (parent, child)
                    )
                )
            } - later_points
            later_points.update(points)
            # Accepted writes remain intentional even when they restore the baseline.
            # Later independent writes release their fields from earlier atomic groups.
            for fid, field in points:
                changes.setdefault(fid, {})[field] = getattr(final_facts[fid], field)
            if len(points) > 1:
                atomic.append(points)
    additive = {
        side: {
            fid
            for fid, fields in modifications.items()
            if "source_ids" in fields
            and fid in before
            and set(before[fid].source_ids) <= set(fields["source_ids"])
            and fid not in replaced_sources[side]
        }
        for side, modifications in ((True, values), (False, parent_values))
    }
    parent_conflicts, child_conflicts = (
        _unseen_conflicts(parent, base),
        _unseen_conflicts(child, base),
    )
    carried = {(fid, field) for fid, fields in child_conflicts.items() for field in fields}
    # An equal-valued candidate may have acquired another exact accepted submission
    # in a lower merge. Carry that audit association even without a scientific delta.
    carried.update(
        (fid, field) for fid, conflict in child.conflicts.items()
        for field, readings in conflict.candidates.items()
        if fid not in base.conflicts or readings != base.conflicts[fid].candidates.get(field, ())
    )
    blocked: set[tuple[str, str]] = set()
    causes: dict[tuple[str, str], set[str]] = {}
    while True:
        previous_blocked = blocked.copy()
        for other, modifications, updated_facts, is_child, unseen in (
            (current, values, child_facts, True, parent_conflicts),
            (child_facts, parent_values, current, False, child_conflicts),
        ):
            proposed = {
                fid: {
                    name: value
                    for name, value in fields.items()
                    if (fid, name) not in blocked
                    and not (
                        is_child
                        and fid in child.conflicts
                        and name in child.conflicts[fid].candidates
                    )
                }
                for fid, fields in modifications.items()
            }
            for fid, fields in proposed.items():
                if not is_child and unsettled(fid):
                    continue
                for name, value in fields.items():
                    try:
                        _check_fact_change(
                            other,
                            before,
                            fid,
                            {"fact_id": fid, name: value},
                            updated_facts[fid],
                            additive_evidence=name == "source_ids" and fid in additive[is_child],
                            proposed=proposed,
                            unseen_conflicts=unseen,
                        )
                    except StaleFactChange as error:
                        blocked.add((fid, name))
                        causes.setdefault((fid, name), set()).add(error.fact_id)
        for points in atomic:
            if points & blocked:
                references = {ref for point in points for ref in causes.get(point, ())}
                for point in points:
                    causes.setdefault(point, set()).update(references)
                blocked.update(points)
        if blocked == previous_blocked:
            break
    sources = {**parent.sources, **child.sources}
    affected = {fid for fid, _ in blocked | carried}
    # Facts keep the order they were saved in: the parent's first, then what the child
    # added, in the child's order. A material list or a temperature profile is
    # published in this order, which is the order the author wrote (E52); sorting by
    # the model's local names, as before, made it alphabetical.
    touched = values.keys() | affected
    in_order = [fid for fid in (*current, *child_facts) if fid in touched]
    for fid in [*dict.fromkeys(in_order), *sorted(touched - set(in_order))]:
        fact = changed.get(fid) or before.get(fid) or current[fid]
        safe = {
            name: value
            for name, value in values.get(fid, {}).items()
            if (fid, name) not in blocked | carried
        }
        if "source_ids" in safe and fid in current and fid in additive[True]:
            safe["source_ids"] = tuple(
                dict.fromkeys(
                    (
                        *before[fid].source_ids,
                        *sorted(
                            set((*current[fid].source_ids, *safe["source_ids"]))
                            - set(before[fid].source_ids)
                        ),
                    )
                )
            )
        merged_origins = field_origins.setdefault(fid, {})
        for field in safe:
            incoming = child.field_origins.get(fid, {}).get(field, ())
            # Both sides' known accepted writes can agree. Their actual indices are
            # united; the value comparison only implements the existing merge rule.
            if field in parent_values.get(fid, {}) and (
                getattr(current.get(fid), field, object()) == safe[field]
                or field == "source_ids" and fid in additive[True]
            ):
                incoming = _origin_union(merged_origins.get(field, ()), incoming)
            merged_origins[field] = incoming
        resolved = set(safe) - ({"source_ids"} if fid in additive[True] else set())
        inherited = (
            {k: v for k, v in conflicts[fid].candidates.items() if k not in resolved}
            if fid in conflicts
            else {}
        )
        if fid not in affected:
            current[fid] = _update_fact(current.get(fid), {"fact_id": fid, **safe})
            conflicts.pop(fid, None)
            if inherited:
                conflicts[fid] = FactConflict(
                    candidates=inherited,
                )
            continue
        conflict_fields = {field for subject, field in blocked | carried if subject == fid}
        candidates = inherited
        child_candidates = child.conflicts[fid].candidates if fid in child.conflicts else {}
        parent_fact = current.get(fid)
        parent_candidate_fields = {
            field
            for field in conflict_fields
            if field not in candidates
            and (
                field not in child_candidates
                or field in parent_values.get(fid, {})
                and not (field == "source_ids" and fid in additive[False])
            )
        }
        references = {ref for field in conflict_fields for ref in causes.get((fid, field), ())}
        # Only conflicting fields retain the baseline; independent values survive.
        anchor = before.get(fid) or min(
            [fact, *([parent_fact] if parent_fact else [])],
            key=lambda f: canonical_json_bytes(f.model_dump()),
        )
        independent = {
            name: value
            for name, value in parent_values.get(fid, {}).items()
            if (fid, name) not in blocked
        }
        current[fid] = _update_fact(anchor, {**independent, **safe})
        for field in conflict_fields:
            readings = list(child_candidates.get(field, ()))
            if field not in child_candidates:
                readings.append(
                    FactCandidate(
                        value=to_jsonable_python(getattr(fact, field)),
                        source_ids=fact_source_ids(fact),
                        origins=child.field_origins.get(fid, {}).get(field, ()),
                    )
                )
            if parent_fact is not None and field in parent_candidate_fields:
                readings.append(
                    FactCandidate(
                        value=to_jsonable_python(getattr(parent_fact, field)),
                        source_ids=fact_source_ids(parent_fact),
                        origins=parent.field_origins.get(fid, {}).get(field, ()),
                    )
                )
            candidates[field] = _candidate_union((*candidates.get(field, ()), *readings))
            # The private anchor is not a resolved reading of a conflicted field.
            merged_origins[field] = ()
        conflicts[fid] = FactConflict(
            candidates=dict(sorted(candidates.items())),
        )
        retain_conflict(fid, references)
    segments = {s.segment_id: s for s in parent.segments}
    previous_segments = {s.segment_id: s for s in base.segments}
    for segment in child.segments:
        sid = segment.segment_id
        old = previous_segments.get(sid)
        if segment == old:
            continue
        existing = segments.get(sid)
        if not unsettled(sid) and (existing is None or existing in (old, segment)):
            segments[sid] = segment
            continue
        # Navigation is part of the same revision, not a last-writer-wins side channel.
        navigations = [segment]
        if existing is not None and not unsettled(sid):
            navigations.append(existing)
        navigation_refs: set[str] = set()
        for navigation in navigations:
            key = hashlib.sha256(canonical_json_bytes(navigation.model_dump())).hexdigest()[:16]
            alternative_id = f"{sid}-ALT-{key}"
            segments[alternative_id] = navigation.model_copy(update={"segment_id": alternative_id})
            navigation_refs.add(alternative_id)
        segments[sid] = old or min(
            [segment, *([existing] if existing is not None else [])],
            key=lambda s: canonical_json_bytes(s.model_dump()),
        )
        retain_conflict(sid, navigation_refs)
    assessments = {tuple(sorted(a.subject_ids)): a for a in parent.experiment_assessments}
    prior_assessments = {tuple(sorted(a.subject_ids)): a for a in base.experiment_assessments}
    for assessment in child.experiment_assessments:
        assessment_key = tuple(sorted(assessment.subject_ids))
        old_assessment = prior_assessments.get(assessment_key)
        if assessment == old_assessment:
            continue
        existing_assessment = assessments.get(assessment_key)
        if existing_assessment is not None and existing_assessment not in (
            old_assessment,
            assessment,
        ):
            assessment = assessment.model_copy(
                update={
                    "status": "UNRESOLVED",
                    "explanation": "Concurrent experimental investigation conclusions disagree.",
                    "source_ids": tuple(
                        sorted(set((*existing_assessment.source_ids, *assessment.source_ids)))
                    ),
                }
            )
        assessments[assessment_key] = assessment
    result = parent.model_copy(
        update={
            "facts": tuple(current.values()),
            "fact_creation_origins": creation_origins,
            "field_origins": {
                fid: {field: origins for field, origins in fields.items() if origins}
                for fid, fields in field_origins.items() if any(fields.values())
            },
            "conflicts": dict(sorted(conflicts.items())),
            "questions": tuple(questions[k] for k in sorted(questions)),
            "sources": dict(sorted(sources.items())),
            "segments": tuple(segments[k] for k in sorted(segments)),
            "experiment_assessments": tuple(assessments[k] for k in sorted(assessments)),
            "source_errors": tuple(
                {
                    canonical_json_bytes(e): e
                    for e in (*parent.source_errors, *child.source_errors)
                }.values()
            ),
            "evidence_registry_refs": tuple(
                sorted(
                    set((*parent.evidence_registry_refs, *child.evidence_registry_refs)),
                    key=lambda ref: canonical_json_bytes(ref.model_dump()),
                )
            ),
        }
    )
    validate_result(result)
    return result
