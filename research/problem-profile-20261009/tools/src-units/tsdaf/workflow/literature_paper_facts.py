"""Merge sealed initial and local findings before one public fact projection.

Receipts identify the immutable results and original evidence producers, including
cached runs. Successful units remain publishable when another unit fails. This
main-paper workflow publishes PARTIAL with explicit unresolved scope; structure
binding is outside this workflow.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from tsdaf.contracts.execution import ExecutionStatus, StageResult
from tsdaf.contracts.literature.expressions import deduplicate_public_expressions
from tsdaf.contracts.literature.objects import initialize_open_collections
from tsdaf.contracts.literature.schema import (
    MACHINE_SCHEMA_VERSIONS,
    MachineProfile,
    contract_pack_pins,
    machine_profile,
    validate_profile_transition,
)
from tsdaf.contracts.literature.stage_semantics import compile_operation_stages
from tsdaf.contracts.literature.validation import required_fact_observation_refs
from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.document.host_tables import journal_publishes_si_in_main_text
from tsdaf.lanes.literature.bootstrap import verify_router_handoff_bundle
from tsdaf.lanes.literature.condition_profiles import compile_condition_profiles
from tsdaf.lanes.literature.container_enrichment import content_hash
from tsdaf.lanes.literature.evidence import (
    furniture_by_package,
    keyed_observation,
    merge_keyed_observations,
    observation_texts,
)
from tsdaf.lanes.literature.evidence_binding import WorkUnitEvidenceRegistry
from tsdaf.lanes.literature.fact_projection import (
    PublicationNotReady,
    RouteGroupFactProjection,
    project_route_group_facts,
)
from tsdaf.lanes.literature.fusion_state import merge_child
from tsdaf.lanes.literature.gate_models import (
    FUSION_RECEIPT_VERSION,
    FUSION_RESULT_MEDIA,
    FactChange,
    FusionResult,
    read_fusion_result,
)
from tsdaf.lanes.literature.material_sources import bound_material_sources, rekey_material_sources
from tsdaf.lanes.literature.review_markers import deduplicate_review_markers
from tsdaf.lanes.literature.source_manifest import (
    UNKNOWN_SOURCE_STATUS,
    LiteratureSources,
    PaperSourceStatus,
)
from tsdaf.lanes.literature.stable_keys import rekey_machine
from tsdaf.lanes.literature.stage_ledger import EnrichmentLedgers
from tsdaf.workflow.literature_fact_publication import (
    FactPublicationAuthority,
    FactPublisher,
    PublishedFactSet,
    build_fact_publication_authority,
)
from tsdaf.workflow.literature_paper_runtime import (
    WINDOW_RECEIPT_MEDIA,
    LiteraturePaperReceipt,
    LiteratureWindowReceipt,
)
from tsdaf.workflow.literature_route_runtime import LiteratureRouteGroupReceipt
from tsdaf.workflow.local_runtime import LocalObjectStore, LocalRunner, LocalRuntimeError
from tsdaf.workflow.unit_decisions import delivered_complete, paper_completeness

__all__ = (
    "PublicationNotReady",
    "PaperPublicationNotReady",
    "PaperFactBundle",
    "PublishedPaperFactSet",
    "SHARED_CONTRACT_SCHEMA_SHA256",
    "build_paper_machine_result",
    "evidence_terminal_manifests",
    "publish_paper_facts",
    "reproject_sealed_run",
    "result_identity",
    "seal_assembly_record",
    "stable_main_receipt",
)

# The combined shared contract pack schema every consumer of the public bundle keys
# on. The pack builder writes it into the package beside each profile's fact schema
# (`resources/contract_pack_pins*.json`) -- one place, read here -- and
# `tests/integration/contracts/test_literature_shared_contract_pack.py` holds those
# files to the pack's own validation reports so the two cannot drift apart (E66).
# A result names the contract of the profile it was produced under, and nothing else:
# two results of one paper under two profiles are two results, not one (E158).
SHARED_CONTRACT_SCHEMA_SHA256: str = contract_pack_pins(MachineProfile.V1)[
    "schema_candidate_sha256"
]


def golden_schema_sha256(profile: MachineProfile) -> str:
    return contract_pack_pins(profile)["schema_candidate_sha256"]


_RECEIPT_MEDIA_TYPE = "application/vnd.tsdaf.literature-route-group-receipt+json"


@dataclass(frozen=True, slots=True)
class PaperFactBundle:
    """One paper's public machine result and the observations it cites."""

    machine: dict[str, object]
    observations: tuple[dict[str, object], ...]
    projections: tuple[RouteGroupFactProjection, ...]
    excluded_route_groups: Mapping[str, str]
    # Per public (keyed) observation id, the sha256 of every sealed observation it was
    # derived from (E83): the provenance the public form no longer carries. The
    # executions that verified those originals are what a later revision must list as
    # supporting executions for the originals to be in its authority.
    observation_provenance: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    verifying_execution_ids: frozenset[str] = frozenset()
    # Which published object each fusion fact became, per route group
    # (`compose_detail_identity`). Private to the Host: it never enters
    # `domain_payload`, which is what `result_identity` hashes and what the public
    # contract forbids unknown keys in. The detail lane reads it to give each block
    # task its own questions instead of the whole route's.
    block_provenance: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    material_sources: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PublishedPaperFactSet(PublishedFactSet):
    """Public artifacts alongside their exact private publication scope."""

    published_route_group_ids: tuple[str, ...]
    excluded_route_groups: Mapping[str, str]
    observation_provenance: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    verifying_execution_ids: frozenset[str] = frozenset()
    block_provenance: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    material_sources: Mapping[str, Any] = field(default_factory=dict)

    skeleton_groups: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class PublicObservations:
    """The public form of sealed observations (E66 (4), E83), and how to get there.

    `observations` are the keyed observations one per site, sorted by id; `remap` takes
    every sealed evidence ref `(observation_id, sha256)` to its public ref;
    `provenance` names per public id the sha256 of the sealed originals it came from,
    and `execution_ids` the executions that verified them. Two runs that read one site
    the same way publish one byte-identical observation; a site the lanes read twice in
    one run (a route group and a window: 32% of the six -a1 runs' observations)
    publishes once, cited for every field either reading cited it for.
    """

    observations: list[dict[str, object]]
    remap: dict[tuple[str, str], dict[str, str]]
    provenance: dict[str, tuple[str, ...]]
    execution_ids: frozenset[str]


def public_observations(
    sealed: Sequence[Mapping[str, object]],
    prior_provenance: Mapping[str, tuple[str, ...]] | None = None,
) -> PublicObservations:
    """See `PublicObservations`. An observation already in public form (a prior
    revision's) keeps the provenance that revision recorded for it."""
    merged = merge_keyed_observations(sealed)
    public_sha = {identifier: _sha256(value) for identifier, value in merged.items()}
    remap: dict[tuple[str, str], dict[str, str]] = {}
    provenance: dict[str, list[str]] = {}
    execution_ids: set[str] = set()
    for observation in sealed:
        original = (str(observation["observation_id"]), _sha256(observation))
        identifier = str(keyed_observation(observation)["observation_id"])
        remap[original] = {
            "observation_id": identifier,
            "source_observation_sha256": public_sha[identifier],
        }
        originals = provenance.setdefault(identifier, [])
        if "anchor_key" in observation:
            for sha in (prior_provenance or {}).get(original[0], ()):
                if sha not in originals:
                    originals.append(sha)
        elif original[1] not in originals:
            originals.append(original[1])
        verification = observation.get("verification")
        if isinstance(verification, Mapping) and isinstance(verification.get("execution_id"), str):
            execution_ids.add(verification["execution_id"])
    return PublicObservations(
        observations=[merged[identifier] for identifier in sorted(merged)],
        remap=remap,
        provenance={k: tuple(v) for k, v in provenance.items()},
        execution_ids=frozenset(execution_ids),
    )


def _sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class PublicResult:
    """A machine result and its observations in public form: every id from one rule."""

    machine: dict[str, object]
    observations: list[dict[str, object]]
    provenance: dict[str, tuple[str, ...]]
    execution_ids: frozenset[str]
    id_map: dict[str, str]
    material_sources: dict[str, Any] = field(default_factory=dict)


def finalize_public_result(
    machine: Mapping[str, object],
    sealed_observations: Sequence[Mapping[str, object]],
    *,
    paper_id: str,
    prior_provenance: Mapping[str, tuple[str, ...]] | None = None,
    material_sources: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    packages: Sequence[CanonicalPaperPackage] = (),
    ledgers: EnrichmentLedgers | None = None,
    profile: MachineProfile = MachineProfile.V1,
    creation_keys: Mapping[str, str] | None = None,
    read_public: Callable[[Mapping[str, Any]], bytes] | None = None,
    observation_set: PublicObservations | None = None,
) -> PublicResult:
    """The one entry every publication goes through last (E66, E84): the facts' ids by
    `rekey_machine`, the observations' by their anchor keys, and every reference in
    the result rewritten to the public observation it cites.

    `ledgers` is the publication's private record of declared stages, elisions and
    unassigned values (E157). Under the v1 profile nothing of it is published. Under
    v1.1 it is compiled into the operations' four stage items here and nowhere else
    (E158): after `rekey_machine`, so a stage hangs on the operation's final stable
    key, and before the observations are keyed, so a stage's evidence is rewritten to
    the public observation it cites with the rest of the result's."""
    validate_profile_transition(machine, profile)
    if profile.faithful_mapping:
        return _finalize_explicit_result(
            machine, sealed_observations, paper_id=paper_id,
            prior_provenance=prior_provenance, material_sources=(
                ledgers.material_sources if ledgers is not None else material_sources or {}),
            read_public=read_public, observation_set=observation_set,
        )
    id_map: dict[str, str] = {}
    keyed = rekey_machine(
        compile_condition_profiles(machine, ledgers),
        paper_id=paper_id,
        id_map=id_map,
        characterization_texts=observation_texts(sealed_observations, packages),
        furniture=tuple(v for v in furniture_by_package(packages).values() if v),
        creation_keys=creation_keys,
    )
    stage_id_map: dict[str, str] = {}
    if profile.stages:
        keyed = compile_operation_stages(
            keyed,
            None if machine_profile(machine).expression else ledgers,
            id_map,
            rekey_relations=profile.expression,
            stage_id_map=stage_id_map,
        )
        keyed["schema_version"] = MACHINE_SCHEMA_VERSIONS[profile]
        keyed["schema_golden_sha256"] = golden_schema_sha256(profile)
    public_set = public_observations(sealed_observations, prior_provenance)
    remapped = remap_evidence_refs(keyed, public_set.remap)
    if profile.expression:
        deduplicate_public_expressions(remapped)
    deduplicate_review_markers(remapped)
    refs = [
        {"observation_id": oid, "source_observation_sha256": digest}
        for oid, digest in sorted(required_fact_observation_refs(remapped))
    ]
    remapped["source_observation_refs"] = refs
    domain = dict(cast(Mapping[str, object], remapped["domain_payload"]))
    domain["source_observation_refs"] = list(refs)
    remapped["domain_payload"] = domain
    cited = {oid for oid, _ in required_fact_observation_refs(remapped)}
    sources_for_public = remap_evidence_refs(
        ledgers.material_sources if ledgers is not None else material_sources or {},
        public_set.remap,
    )
    if profile.expression:
        deduplicate_public_expressions(sources_for_public)
    return PublicResult(
        machine=remapped,
        observations=[o for o in public_set.observations if o["observation_id"] in cited],
        provenance={k: v for k, v in public_set.provenance.items() if k in cited},
        execution_ids=public_set.execution_ids,
        id_map=id_map,
        material_sources=rekey_material_sources(
            sources_for_public,
            id_map,
            [*remapped["domain_payload"]["reaction_blocks"],
             *remapped["domain_payload"].get("experimental_accounts", [])],
            stage_id_map=stage_id_map,
        ),
    )



def _finalize_explicit_result(
    machine, sealed_observations, *, paper_id, prior_provenance, material_sources, read_public, observation_set=None,
) -> PublicResult:
    """Only transport observation identities; accepted scientific structure is final.

    No semantic key merge, ordering, string parsing, stage reconstruction or expression
    deduplication is allowed after the two explicit model compilations.
    """
    from tsdaf.contracts.literature.public_science import load_public_science

    if machine["paper_id"] != paper_id:
        raise ValueError("explicit result belongs to another paper")
    def read(ref):
        if read_public is None:
            raise ValueError("explicit result needs its listed public scientific artifacts")
        return read_public(ref)
    bundle = load_public_science(machine, read)
    public_set = observation_set or public_observations(sealed_observations, prior_provenance)
    remapped = remap_evidence_refs(machine, public_set.remap)
    required = required_fact_observation_refs(remapped, public_science=bundle)
    known = {(str(o["observation_id"]), _sha256(o)) for o in public_set.observations}
    if not required <= known:
        raise ValueError("explicit result is missing a public source observation")
    refs = [{"observation_id": oid, "source_observation_sha256": sha}
            for oid, sha in sorted(required)]
    remapped["source_observation_refs"] = refs
    remapped["domain_payload"]["source_observation_refs"] = copy.deepcopy(refs)
    remapped["schema_golden_sha256"] = golden_schema_sha256(machine_profile(machine))
    cited = {oid for oid, _ in required}
    return PublicResult(
        machine=remapped,
        observations=[o for o in public_set.observations if o["observation_id"] in cited],
        provenance={k:v for k,v in public_set.provenance.items() if k in cited},
        execution_ids=public_set.execution_ids,
        id_map={},  # Every accepted public object already has its birth-derived identity.
        material_sources=remap_evidence_refs(material_sources, public_set.remap),
    )

def compose_detail_identity(
    group_maps: Mapping[str, Mapping[str, str]],
    second_map: Mapping[str, str],
) -> dict[str, dict[str, str]]:
    """Which public id each fusion fact became, per route group.

    Two passes rename a record on its way to a result, and each is applied once here.
    They must not be merged into one map and followed: `rekey_machine` can mint an id
    that is spelled like one the route group's own `rekey` handed out, and a chained
    lookup would walk out of one object and into another (E111 -- a record may not be
    rewritten into another record's identity).

    Every entry is kept, including facts that became something the result does not
    publish as a record of its own. Those reach no step, and the task that reads this
    map says so in as many words (`unmapped_question_ids`) rather than passing over
    them: a subject nobody was asked about is the failure the map exists to prevent.
    """
    return {
        group_id: {
            fact_id: second_map.get(public_id, public_id)
            for fact_id, public_id in group_map.items()
        }
        for group_id, group_map in group_maps.items()
    }


def si_registration_state(
    sources: LiteratureSources | None,
    *,
    has_experimental_part: bool | None,
    si_read: bool,
) -> str:
    """The paper's SI state as the public result says it (E77 `si_registration`, E83):
    REGISTERED_READ when a registered SI was read, REGISTERED_UNREAD when one is
    registered and available, NOT_APPLICABLE when the journal is in the Host's table of
    journals that print the experimental part in the main text *and* the main text has
    one, NONE otherwise -- a paper waiting for an SI nobody has paired yet.
    """
    if sources is not None and sources.si_registered:
        return "REGISTERED_READ" if si_read else "REGISTERED_UNREAD"
    main = sources.main.package if sources is not None else None
    identity = main.manifest.identity if main is not None else None
    journal = identity.journal if identity is not None else None
    if has_experimental_part and journal and journal_publishes_si_in_main_text(journal):
        return "NOT_APPLICABLE"
    return "NONE"


# Where a WAIT_FOR_SI item goes when the paper's SI state does not admit it (E77's
# rule: a reason that speaks of the SI is only true in one state). The item keeps its
# id and evidence; the reason and the sentence say what the Host knows instead.
_WAIT_FOR_SI_RECONCILED: dict[str, tuple[str, str]] = {
    "NONE": (
        "SI_UNAVAILABLE",
        "The route group stopped this for the supporting information; none is registered "
        "for this paper.",
    ),
    "NOT_APPLICABLE": (
        "INSUFFICIENT_EVIDENCE",
        "The route group stopped this for a supporting information; the journal prints "
        "the experimental part in the main text and this paper has one.",
    ),
    "REGISTERED_READ": (
        "INSUFFICIENT_EVIDENCE",
        "The route group stopped this for the supporting information, which the detail "
        "investigation has since read without resolving it.",
    ),
}


def reconcile_si_reasons(items: Sequence[Mapping[str, Any]], state: str) -> list[dict[str, Any]]:
    """The unresolved items with every WAIT_FOR_SI rewritten to what the SI state
    admits; REGISTERED_UNREAD admits it as it is."""
    rewrite = _WAIT_FOR_SI_RECONCILED.get(state)
    result: list[dict[str, Any]] = []
    for item in items:
        record = dict(item)
        if rewrite is not None and record.get("reason_code") == "WAIT_FOR_SI":
            record["reason_code"] = rewrite[0]
            record.setdefault("statement", rewrite[1])
        result.append(record)
    return result


def no_experimental_source_item(paper_id: str) -> dict[str, Any]:
    """The one PAPER-scoped item a paper gets when it has no experimental source at all:
    no supporting information registered and no experimental part in the main text
    (E77 NO_EXPERIMENTAL_SOURCE), so the loss is said once rather than per operation."""
    return {
        "unresolved_item_id": "UNR-PAPER-NO_EXPERIMENTAL_SOURCE",
        "_kind": "NO_EXPERIMENTAL_SOURCE",
        "scope": "PAPER",
        "subject_id": paper_id,
        "reason_code": "NO_EXPERIMENTAL_SOURCE",
        "statement": (
            "No supporting information is registered for this paper and its main text has "
            "no experimental part: no procedure or characterization can be read anywhere."
        ),
        "evidence_refs": [],
    }


def _is_evidence_ref(value: object) -> bool:
    return isinstance(value, Mapping) and set(value) == {
        "observation_id",
        "source_observation_sha256",
    }


def remap_evidence_refs(value: Any, remap: Mapping[tuple[str, str], Mapping[str, str]]) -> Any:
    """`value` with every evidence ref `{observation_id, source_observation_sha256}` it
    holds, at any depth, replaced by the public ref; a ref the map does not know is a
    dangling reference and stays as it is for the closure check to name. A list of
    refs comes back in public order, one ref per public observation (two sealed
    readings of one site are one public observation), so the result's bytes do not
    depend on the sealed ids, and keying it again changes nothing."""
    if isinstance(value, Mapping):
        if _is_evidence_ref(value):
            key = (str(value["observation_id"]), str(value["source_observation_sha256"]))
            return dict(remap.get(key, value))
        return {k: remap_evidence_refs(v, remap) for k, v in value.items()}
    if isinstance(value, list):
        items = [remap_evidence_refs(v, remap) for v in value]
        if items and all(_is_evidence_ref(item) for item in items):
            unique = {(r["observation_id"], r["source_observation_sha256"]): r for r in items}
            return [unique[k] for k in sorted(unique)]
        return items
    if isinstance(value, tuple):
        return tuple(remap_evidence_refs(v, remap) for v in value)
    return value


class PaperPublicationNotReady(PublicationNotReady):
    """No route qualified; preserve each exclusion without parsing a diagnostic."""

    def __init__(self, paper_id: str, excluded_route_groups: Mapping[str, str]) -> None:
        self.excluded_route_groups = dict(excluded_route_groups)
        super().__init__(
            paper_id,
            "NO_ELIGIBLE_ROUTE: "
            + "; ".join(
                f"{group_id}={reason}" for group_id, reason in excluded_route_groups.items()
            ),
        )


# What a unit's sealed decision says about its delivery lives in one place with the
# runtime that writes it and the receipts that check it (E52).


def _public_artifact(ref: ArtifactRef, logical_name: str) -> dict[str, object]:
    """Strip an internal ref down to the four keys the public contract permits.

    `relative_path` names a location inside our workspace and is refused outright by
    the publisher, so it must not survive the crossing.
    """

    return {
        "sha256": ref.sha256,
        "media_type": ref.media_type,
        "byte_size": ref.byte_size,
        "logical_name": logical_name,
    }


def _read_group_receipt(store: LocalObjectStore, ref: ArtifactRef) -> LiteratureRouteGroupReceipt:
    if ref.media_type != _RECEIPT_MEDIA_TYPE:
        raise ValueError("route group outcome does not reference a route group receipt")
    store.verify(ref)
    return LiteratureRouteGroupReceipt.model_validate_json(store.read_bytes(ref))


def evidence_terminal_manifests(
    store: LocalObjectStore,
    runner: LocalRunner,
    paper_receipt: LiteraturePaperReceipt,
) -> tuple[ArtifactRef, ...]:
    """Recover the sealed terminal manifest of every execution that verified evidence.

    Which executions those are is read off the evidence, not assumed. Each unit -- a
    route group, or (in a receipt sealed before reaction windows were retired, 2026-09-23)
    one of its windows -- owns an execution and verified the observations it materialized
    under it, so each authorizes its own; that is what let a paper publish nine windows
    when a tenth was refused.

    A unit served from the stage cache is the reason this reads rather than assumes. Its
    observations carry the execution that *first* produced them, which is some earlier
    paper run, so binding the bundle to only this run's executions refused every cached
    unit -- and a re-run after any prompt change is almost entirely cached units.

    `finish_execution` is idempotent and returns the manifest that run already sealed,
    so this recovers the refs without a second seal. The primary execution is excluded:
    the caller passes it separately.
    """

    execution_ids: list[str] = []
    all_receipts = [o.receipt_ref for o in paper_receipt.route_group_outcomes] + [
        o.receipt_ref for o in paper_receipt.reaction_window_outcomes
    ]
    for receipt_ref in all_receipts:
        if receipt_ref is None:
            continue
        receipt = (
            _read_group_receipt(store, receipt_ref)
            if receipt_ref.media_type == _RECEIPT_MEDIA_TYPE
            else _read_window_receipt(store, receipt_ref)
        )
        result = _read_result(store, receipt.gate_proposal_ref)
        execution_ids.extend(
            _registry_execution_id(store, ref) for ref in result.evidence_registry_refs
        )
    return tuple(
        _sealed_successful_manifest(runner, execution_id)
        for execution_id in dict.fromkeys(execution_ids)
        if execution_id != paper_receipt.literature_execution_id
    )


def _sealed_successful_manifest(runner: LocalRunner, execution_id: str) -> ArtifactRef:
    """Recover one verifying execution's manifest, refusing one that did not succeed.

    A stage is cached on content, so a stage that succeeded inside an execution some
    later stage then failed stays reusable, and a later run can be served from it. Asking
    `finish_execution` for SUCCEEDED there raises a hash mismatch naming neither the
    evidence nor the reason -- the same discarded-cause failure that cost three live
    diagnosis cycles elsewhere in this chain.
    """

    try:
        return runner.finish_execution(execution_id, ExecutionStatus.SUCCEEDED)
    except LocalRuntimeError as error:
        raise ValueError(
            f"evidence was verified inside execution {execution_id!r}, "
            "which did not seal successfully, so it cannot authorize a published fact"
        ) from error


def _registry_execution_id(store: LocalObjectStore, ref: ArtifactRef) -> str:
    store.verify(ref)
    return WorkUnitEvidenceRegistry.model_validate_json(store.read_bytes(ref)).execution_id


def _read_window_receipt(store: LocalObjectStore, ref: ArtifactRef) -> LiteratureWindowReceipt:
    if ref.media_type != WINDOW_RECEIPT_MEDIA:
        raise ValueError("window outcome does not reference a reaction window receipt")
    store.verify(ref)
    return LiteratureWindowReceipt.model_validate_json(store.read_bytes(ref))


def _read_result(store: LocalObjectStore, ref: ArtifactRef) -> FusionResult:
    if ref.media_type != FUSION_RESULT_MEDIA:
        raise ValueError("new execution cannot consume an old private Gate result")
    store.verify(ref)
    return read_fusion_result(store.read_bytes(ref))


def _merge_windows(
    store: LocalObjectStore,
    paper_receipt: LiteraturePaperReceipt,
    group_id: str,
    initial: FusionResult,
) -> FusionResult:
    result = initial
    windows: dict[str, LiteratureWindowReceipt] = {}
    accepted_batches: dict[str, tuple[str, list[tuple[FactChange, ...]]]] = {}

    def read_audit(ref: ArtifactRef) -> dict[str, Any]:
        payload = store.read_bytes(ref)
        if len(payload) != ref.byte_size or hashlib.sha256(payload).hexdigest() != ref.sha256:
            raise ValueError("fusion audit bytes differ from their sealed identity")
        record: dict[str, Any] = json.loads(payload)
        return record

    def merge_ref(ref: ArtifactRef) -> None:
        nonlocal result
        if ref.sha256 in accepted_batches:
            return
        child = _read_result(store, ref)
        if child.parent_result_ref is None:
            return
        base = _read_result(store, child.parent_result_ref)
        if base.question_id is not None:
            merge_ref(child.parent_result_ref)
        window = windows[ref.sha256]
        audit = read_audit(window.gate_provenance_ref)
        if (
            audit.get("schema_version") != FUSION_RECEIPT_VERSION
            or ArtifactRef.model_validate(audit["result_ref"]) != ref
            or audit["work_unit_id"] != child.work_unit_id
            or audit["execution_id"] != window.window_execution_id
        ):
            raise ValueError("window fusion audit differs from its accepted result")
        from tsdaf.lanes.literature.gate_models import fusion_write_collections
        from tsdaf.workflow.fusion_recorded_profile import fusion_call_profile
        batches = []
        for attempt in audit["attempt_refs"]:
            call = read_audit(ArtifactRef.model_validate(attempt))
            if not call.get("batches"):
                continue
            profile = fusion_call_profile(call, read_audit)
            change_model = fusion_write_collections(unified_expression=profile.unified_expression)["changes"]
            batches.extend(tuple(change_model.model_validate_json(canonical_json_bytes(c)) for c in batch)
                           for batch in call["batches"])
        # Ancestor operations already belong to this child's baseline, not a
        # concurrent parent write. Keep the actual order of all other branches.
        inherited: set[str] = set()
        ancestor = child.parent_result_ref.sha256
        while ancestor in accepted_batches:
            inherited.add(ancestor)
            ancestor = accepted_batches[ancestor][0]
        parent_batches = [
            batch
            for revision, (_, accepted) in accepted_batches.items()
            if revision not in inherited
            for batch in accepted
        ]
        result = merge_child(result, base, child, batches=batches, parent_batches=parent_batches)
        accepted_batches[ref.sha256] = (child.parent_result_ref.sha256, batches)

    for outcome in paper_receipt.reaction_window_outcomes:
        if outcome.receipt_ref is None or outcome.route_group_id != group_id:
            continue
        window = _read_window_receipt(store, outcome.receipt_ref)
        if (
            window.route_group_id != group_id
            or window.depth != outcome.depth
            or window.child_handoff_ref.sha256 != outcome.child_handoff_sha256
        ):
            raise ValueError("paper window receipt differs from sealed scientific scope")
        windows[window.gate_proposal_ref.sha256] = window
    for window in windows.values():
        merge_ref(window.gate_proposal_ref)
    return result


def experimental_checkpoint(
    store: LocalObjectStore,
    receipt: LiteraturePaperReceipt,
    package: CanonicalPaperPackage | None = None,
) -> dict[str, object]:
    """Deliver existing containers' scientific context and explicit scoped assessment.

    An absent assessment is unknown. A scoped sufficient assessment covers only its
    referenced subjects and their descendants; a separate unresolved area stays open.

    ``package`` is the paper the run was made from: the one the fusion read its own
    sources out of, so each group carries what it read, by the key its read minted, for
    a task downstream to name as the fusion named it (`fusion_read_locations`). Without
    it the groups carry no reads, and a downstream task names one as it is handed it.
    """
    from tsdaf.lanes.literature.evidence_binding import (
        WorkUnitEvidenceRegistry,
        fusion_read_locations,
    )

    groups: list[dict[str, Any]] = []
    for outcome in receipt.route_group_outcomes:
        if outcome.receipt_ref is None:
            groups.append(
                {"route_group_id": outcome.route_group_id, "failure": outcome.failure_code}
            )
            continue
        group = _read_group_receipt(store, outcome.receipt_ref)
        result = _merge_windows(
            store, receipt, outcome.route_group_id, _read_result(store, group.gate_proposal_ref)
        )
        reads: dict[str, dict[str, Any]] = {}
        if package is not None:
            for registry_ref in result.evidence_registry_refs:
                store.verify(registry_ref)
                reads.update(
                    fusion_read_locations(
                        WorkUnitEvidenceRegistry.model_validate_json(
                            store.read_bytes(registry_ref)
                        ),
                        package,
                    )
                )
        groups.append(
            {
                "route_group_id": outcome.route_group_id,
                "result": result.model_dump(mode="json"),
                "reads": reads,
            }
        )
    return {"groups": groups}


def _result_observations(store: LocalObjectStore, result: FusionResult) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    for registry_ref in result.evidence_registry_refs:
        store.verify(registry_ref)
        registry = WorkUnitEvidenceRegistry.model_validate_json(store.read_bytes(registry_ref))
        observations.extend(dict(entry.source_observation) for entry in registry.entries)
    return observations


def reproject_sealed_run(
    store: LocalObjectStore,
    paper_receipt: LiteraturePaperReceipt,
    *,
    paper_id: str,
    packages: Sequence[CanonicalPaperPackage] = (),
    profile: MachineProfile = MachineProfile.V1,
    accepted_refs: Sequence[ArtifactRef] = (),
    runner: LocalRunner | None = None,
) -> dict[str, RouteGroupFactProjection | str]:
    """Project every route group of a sealed run again, with today's projection, from
    the sealed facts (no model). Optional packages verify card author text; absent
    sources leave that identity basis derived. By route group id: the projection,
    or the reason it is not publishable. The same sealed run must give the same
    counts until the projection changes on purpose (ledger E72, deep audit L7-10)."""
    from tsdaf.workflow.fact_source_selections import fusion_fact_selections

    projected: dict[str, RouteGroupFactProjection | str] = {}
    furniture = tuple(v for v in furniture_by_package(packages).values() if v)
    for outcome in paper_receipt.route_group_outcomes:
        if outcome.receipt_ref is None:
            projected[outcome.route_group_id] = outcome.failure_code or "NO_SEALED_RECEIPT"
            continue
        group = _read_group_receipt(store, outcome.receipt_ref)
        result = _merge_windows(
            store,
            paper_receipt,
            outcome.route_group_id,
            _read_result(store, group.gate_proposal_ref),
        )
        try:
            projected[outcome.route_group_id] = project_route_group_facts(
                route_group_id=outcome.route_group_id,
                result=result,
                paper_id=paper_id,
                profile=profile,
                source_selections=fusion_fact_selections(
                    result, packages=packages, store=store, accepted_refs=accepted_refs, runner=runner),
                characterization_texts=observation_texts(
                    _result_observations(store, result), packages
                ),
                furniture=furniture,
            )
        except PublicationNotReady as error:
            projected[outcome.route_group_id] = error.reason
    return projected


def build_paper_machine_result(
    *,
    store: LocalObjectStore,
    package: CanonicalPaperPackage,
    paper_receipt: LiteraturePaperReceipt,
    authority: FactPublicationAuthority,
    model_profile_versions: Sequence[Mapping[str, object]],
    sources: LiteratureSources | None = None,
    source_status: PaperSourceStatus = UNKNOWN_SOURCE_STATUS,
    group_targets: Mapping[str, Sequence[str]] | None = None,
    profile: MachineProfile = MachineProfile.V1,
    runner: LocalRunner | None = None,
) -> PaperFactBundle:
    """Project every decided route group and assemble one public machine result.

    `sources` and `source_status` are what the Host knew about the paper's sources
    (E83): they decide the result's `si_registration`, whether a WAIT_FOR_SI stop is
    admitted as such, and whether the paper gets the one NO_EXPERIMENTAL_SOURCE item.
    Without them the result says nothing about the SI, as every result before did.
    `group_targets` is what the Router named each route group's targets
    (`route_group_targets`): a group that projected nothing is keyed by that name,
    so the item saying it was omitted survives a Router rerun (E84 audit).
    """

    if not isinstance(store, LocalObjectStore):
        raise TypeError("store must be a LocalObjectStore")
    if type(package) is not CanonicalPaperPackage:
        raise TypeError("package must be a CanonicalPaperPackage")
    if type(paper_receipt) is not LiteraturePaperReceipt:
        raise TypeError("paper_receipt must be a LiteraturePaperReceipt")
    if not isinstance(authority, FactPublicationAuthority):
        raise TypeError("authority must be a FactPublicationAuthority")
    if not model_profile_versions:
        raise ValueError("the public contract requires at least one model profile version")
    if authority.paper_id != package.manifest.paper_id:
        raise ValueError("publication authority is for a different paper")
    if paper_receipt.package_sha256 != package.canonical_content_sha256:
        raise ValueError("paper receipt is for a different canonical package")

    packages = (
        tuple(d.package for d in sources.documents if d.package is not None)
        if sources
        else (package,)
    )
    furniture = tuple(v for v in furniture_by_package(packages).values() if v)
    projections: list[RouteGroupFactProjection] = []
    excluded: dict[str, str] = {}
    observations: list[dict[str, object]] = []
    seen_observation_ids: set[str] = set()
    stage_output_refs: list[dict[str, object]] = []

    for outcome in paper_receipt.route_group_outcomes:
        if outcome.receipt_ref is None:
            excluded[outcome.route_group_id] = outcome.failure_code or "NO_SEALED_RECEIPT"
            continue
        group = _read_group_receipt(store, outcome.receipt_ref)
        stage_output_refs.append(
            _public_artifact(outcome.receipt_ref, f"route-group-receipt-{outcome.route_group_id}")
        )
        result = _merge_windows(
            store,
            paper_receipt,
            outcome.route_group_id,
            _read_result(store, group.gate_proposal_ref),
        )
        group_observations = _result_observations(store, result)
        from tsdaf.workflow.fact_source_selections import fusion_fact_selections
        try:
            projections.append(
                project_route_group_facts(
                    route_group_id=outcome.route_group_id,
                    result=result,
                    paper_id=package.manifest.paper_id,
                    profile=profile,
                    source_selections=fusion_fact_selections(
                        result, packages=packages, store=store,
                        accepted_refs=authority.artifact_closure, runner=runner),
                    characterization_texts=observation_texts(group_observations, packages),
                    furniture=furniture,
                )
            )
        except PublicationNotReady as error:
            excluded[outcome.route_group_id] = error.reason
            continue
        for observation in group_observations:
            observation_id = str(observation["observation_id"])
            if observation_id not in seen_observation_ids:
                seen_observation_ids.add(observation_id)
                observations.append(observation)

    if not projections:
        raise PaperPublicationNotReady(package.manifest.paper_id, excluded)

    # The projections' records as they came (keyed per route already); the stage items
    # -- which unit did not finish -- name what they are about, so the one keying rule
    # names them too, and never by a Router ordinal where the paper's own words exist
    # (E84 audit): a unit of a projected group is keyed under that group's key
    # (`<paper>|RG:<target label>|UNR:...`); a group that projected nothing is keyed by
    # the targets the Router named for it, and only a group the Router never named
    # falls back to its id, marked as derived.
    compounds = [item for projection in projections for item in projection.compounds]
    reaction_blocks = [item for projection in projections for item in projection.reaction_blocks]
    routes = [projection.route for projection in projections]
    unresolved: list[dict[str, Any]] = [
        dict(item) for projection in projections for item in projection.unresolved_items
    ]
    by_group = {projection.route_group_id: projection for projection in projections}

    def subject_of(group_id: str | None) -> tuple[str, str]:
        projection = by_group.get(group_id or "")
        if projection is not None:
            return "ROUTE", str(projection.route["route_id"])
        return "PAPER", package.manifest.paper_id

    def group_name(group_id: str) -> str:
        targets = (group_targets or {}).get(group_id) or ()
        return "+".join(targets) if targets else f"~{group_id}"

    def stage_item(kind: str, scope: str, subject_id: str) -> dict[str, Any]:
        return {
            "unresolved_item_id": f"UNR-STAGE-{kind}",
            "scope": scope,
            "subject_id": subject_id,
            "reason_code": "STAGE_PARTIAL",
            "evidence_refs": [],
            "_kind": kind,
        }

    unresolved.extend(
        stage_item(f"OMITTED:{group_name(group_id)}:{reason}", "PAPER", package.manifest.paper_id)
        for group_id, reason in excluded.items()
    )
    unresolved.extend(
        stage_item(
            f"WINDOW:{window.input_label}>{window.output_label}", *subject_of(window.route_group_id)
        )
        for window in paper_receipt.reaction_window_outcomes
        if window.failure_code or not delivered_complete(window.gate_decision)
    )
    unresolved.extend(
        stage_item("GROUP", *subject_of(outcome.route_group_id))
        for outcome in paper_receipt.route_group_outcomes
        if outcome.gate_decision is not None and not delivered_complete(outcome.gate_decision)
    )
    # Where two readings disagreed about one reported outcome (the conflicts a fusion
    # result records; a sealed window merged into its parent is where most came from).
    # This was hardcoded empty, so a disagreement the Gate found -- or one the Host
    # derived from the alignment the Gate proposed -- never reached the bundle at all.
    field_conflicts = [item for projection in projections for item in projection.field_conflicts]
    coverage_assessments = [
        item for projection in projections for item in projection.coverage_assessments
    ]
    # The SI state, and the unresolved items reconciled with it (E77's rule that a reason
    # speaking of the SI is only true in one state). A paper with no experimental source
    # at all says so once. Unknown status keeps the pre-E83 result: no SI field at all.
    si_state: str | None = None
    if sources is not None or source_status.has_experimental_part is not None:
        has_part = source_status.has_experimental_part
        si_state = si_registration_state(sources, has_experimental_part=has_part, si_read=False)
        unresolved = reconcile_si_reasons(unresolved, si_state)
        if si_state == "NONE" and has_part is False:
            unresolved.append(no_experimental_source_item(package.manifest.paper_id))
    si_revisions = (
        sorted(
            document.package.stable_revision_id("SI")
            for document in sources.active_si
            if document.package is not None
        )
        if sources is not None and si_state in {"REGISTERED_UNREAD", "REGISTERED_READ"}
        else []
    )

    # Narratives and characterization cards come from NOTE facts (ledger E47); the
    # supplement channel may add narratives later. Biological findings never enter
    # the main result: the supplement channel keeps them in its own receipt.
    domain_payload: dict[str, object] = {
        "paper_id": package.manifest.paper_id,
        "source_observation_refs": [],
        "compounds": compounds,
        "reaction_blocks": reaction_blocks,
        "routes": routes,
        "field_conflicts": field_conflicts,
        "paper_narratives": [
            item for projection in projections for item in projection.paper_narratives
        ],
        "characterization_records": [
            item for projection in projections for item in projection.characterization_records
        ],
        "biological_findings": [],
    }
    if profile.open_expression:
        initialize_open_collections(domain_payload)
    if profile.unified_expression:
        domain_payload["experimental_accounts"] = [
            item for projection in projections for item in projection.experimental_accounts
        ]
        domain_payload["unassigned_outcomes"] = [
            item for projection in projections for item in projection.unassigned_outcomes
        ]
    machine: dict[str, object] = {
        "result_kind": "MACHINE_RESULT",
        "machine_result_id": "MPR-pending",
        "paper_id": package.manifest.paper_id,
        # The public revision is a function of the document (E66 (4), E83); the run's
        # label stays in the paper receipt as a run coordinate.
        "main_package_revision_id": package.stable_revision_id("MAIN"),
        "si_package_revision_ids": si_revisions,
        "supersedes_machine_result_sha256": None,
        "machine_revision": 1,
        # The literature chain's first revision reads the main article only; the
        # registered SI is read by the detail investigation and published as revision 2.
        "coverage": "MAIN_ONLY",
        # Derived from the terminal state of the units that made it (E83), in one place.
        "completeness": paper_completeness(
            group_decisions=(
                (o.gate_decision, o.failure_code) for o in paper_receipt.route_group_outcomes
            ),
            window_decisions=(
                (o.gate_decision, o.failure_code) for o in paper_receipt.reaction_window_outcomes
            ),
            excluded_groups=len(excluded),
            pending_units=len(paper_receipt.pending_work_unit_refs),
        ),
        "domain_payload": domain_payload,
        "source_observation_refs": [],
        "source_artifact_refs": [
            _public_artifact(ref, f"source-artifact-{index:04d}")
            for index, ref in enumerate(authority.review_readable_artifacts, start=1)
        ],
        "stage_output_refs": stage_output_refs,
        # A cross-modal outcome disagreement is sourced on both sides and does not stop
        # a reviewer reading the block, so it is reported without blocking the result.
        "blocking_conflicts": [item for item in field_conflicts if item["blocking"]],
        "unresolved_items": unresolved,
        "coverage_assessments": coverage_assessments,
        "schema_version": MACHINE_SCHEMA_VERSIONS[profile],
        "schema_golden_sha256": golden_schema_sha256(profile),
        "model_profile_versions": [dict(item) for item in model_profile_versions],
        "execution_id": authority.execution_id,
        "execution_manifest_ref": _public_artifact(
            authority.terminal_manifest_ref, "execution-manifest"
        ),
    }
    if si_state is not None:
        machine["si_registration"] = si_state
    # Every public id from the one rule, every observation keyed by its anchor, every
    # reference rewritten (E84): the last step of assembling a result.
    public = finalize_public_result(
        machine,
        observations,
        paper_id=package.manifest.paper_id,
        material_sources={
            oid: rows for p in projections for oid, rows in p.material_sources.items()
        },
        packages=packages,
        profile=profile,
    )
    machine = public.machine
    domain_payload = cast(dict[str, object], machine["domain_payload"])
    identity = result_identity(package, model_profile_versions, domain_payload, profile=profile)
    machine["machine_result_id"] = f"MPR-{identity}"
    return PaperFactBundle(
        machine=machine,
        observations=tuple(public.observations),
        projections=tuple(projections),
        excluded_route_groups=dict(excluded),
        observation_provenance=public.provenance,
        verifying_execution_ids=public.execution_ids,
        material_sources=bound_material_sources(machine, public.material_sources),
        block_provenance=compose_detail_identity(
            {p.route_group_id: p.fusion_public_ids for p in projections}, public.id_map
        ),
    )


def result_identity(
    package: CanonicalPaperPackage,
    model_profile_versions: Sequence[Mapping[str, object]],
    domain_payload: Mapping[str, object],
    *,
    profile: MachineProfile = MachineProfile.V1,
) -> str:
    """What names a published result: the paper, the package bytes, the contract, the
    model profiles and the public facts themselves (E66). No run label: the same
    content published under any execution is the same result, and the stable main
    receipt's execution id is derived from this same digest. The contract is the
    profile's, so one paper published under v1 and under v1.1 is two results."""
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "paper_id": package.manifest.paper_id,
                "package_sha256": package.canonical_content_sha256,
                "schema_golden_sha256": golden_schema_sha256(profile),
                "model_profile_versions": [dict(item) for item in model_profile_versions],
                "domain_payload": domain_payload,
            }
        )
    ).hexdigest()


ASSEMBLY_VERSION = "2.25.0"


def resolve_public_ref(store: LocalObjectStore, ref: Mapping[str, Any]) -> ArtifactRef:
    """The store's own reference to content a public ref names; a public ref carries
    the writer's path, which is not this store's."""
    resolved = store.resolve_content(
        sha256=str(ref["sha256"]),
        media_type=str(ref["media_type"]),
        byte_size=int(ref["byte_size"]),
    )
    if resolved is None:
        raise ValueError("a published artifact is not available in the local object store")
    return resolved


def seal_assembly_record(
    workspace: Path,
    name: str,
    payload: dict[str, Any],
    inputs: tuple[ArtifactRef, ...] = (),
    *,
    execution_id: str | None = None,
) -> tuple[ArtifactRef, ArtifactRef]:
    """Seal one assembly record as its own stage, under an execution named by its
    content unless the caller names one; sealing the same record again is the same
    stage (the runner keys on content), so the seal is idempotent."""
    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)
    ref = store.put_bytes(
        canonical_json_bytes(payload), media_type="application/vnd.tsdaf." + name + "+json"
    )
    execution_id = execution_id or name + "-" + content_hash(
        [ASSEMBLY_VERSION, ref.sha256, [r.sha256 for r in inputs]]
    )

    def invoke(attempt_id: str, content_key: str) -> StageResult:
        now = datetime.now(UTC)
        return StageResult(
            schema_version="tsdaf.stage-result.v1",
            execution_id=execution_id,
            attempt_id=attempt_id,
            stage_name=name,
            stage_version=ASSEMBLY_VERSION,
            input_content_key=content_key,
            status=ExecutionStatus.SUCCEEDED,
            output_artifact=ref,
            failure=None,
            cached_from_result=None,
            started_at=now,
            finished_at=now,
        )

    result = runner.run_stage(
        execution_id=execution_id,
        stage_name=name,
        stage_version=ASSEMBLY_VERSION,
        input_artifacts=(ref, *inputs),
        settings={},
        invoke=invoke,
    )
    if isinstance(result, StageResult) is False:
        raise ValueError("could not seal literature assembly evidence")
    return ref, runner.finish_execution(execution_id, ExecutionStatus.SUCCEEDED)


def route_group_targets(
    store: LocalObjectStore,
    runner: LocalRunner,
    package: CanonicalPaperPackage,
    paper_receipt: LiteraturePaperReceipt,
) -> dict[str, tuple[str, ...]]:
    """What the Router named each route group's targets, from its sealed routing
    result: the public name a stage item about an unprojected group is keyed by
    (E84 audit). `finish_execution` is idempotent here as everywhere: the Router's
    execution was sealed before any group ran."""
    terminal = runner.finish_execution(paper_receipt.route_execution_id, ExecutionStatus.SUCCEEDED)
    handoffs = verify_router_handoff_bundle(
        package=package,
        router_execution_id=paper_receipt.route_execution_id,
        terminal_manifest_ref=terminal,
        object_store=store,
        runner=runner,
    )
    return {
        group.route_group_id: tuple(group.target_names) for group in handoffs.routing.route_groups
    }


def stable_main_receipt(
    *,
    workspace: Path,
    package: CanonicalPaperPackage,
    package_dir: Path,
    paper_receipt: LiteraturePaperReceipt,
    model_profile_versions: Sequence[Mapping[str, object]],
    sources: LiteratureSources | None = None,
    source_status: PaperSourceStatus = UNKNOWN_SOURCE_STATUS,
    profile: MachineProfile = MachineProfile.V1,
) -> PublishedPaperFactSet:
    """Publish the run's merged facts under an execution named by their content (E66).

    The run's own execution id is the CLI's label; a result published under it is a
    different result on every rerun. Here the public facts are assembled once under
    the run's authority only to be named, the stable id `MAIN-<identity>` is sealed
    as an assembly record whose inputs are the run's sealed receipts, and the
    publication is made under that execution. The same content again is the same
    stage, the same publication and the same ids. A result that is not ready is
    sealed as such under the run's id, as before.
    """
    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)
    terminal_manifest = runner.finish_execution(
        paper_receipt.literature_execution_id, ExecutionStatus.SUCCEEDED
    )
    authority = build_fact_publication_authority(
        package,
        package_dir,
        terminal_manifest,
        store,
        runner,
        supporting_terminal_manifests=evidence_terminal_manifests(store, runner, paper_receipt),
        sources=sources,
    )
    try:
        named = build_paper_machine_result(
            store=store,
            package=package,
            paper_receipt=paper_receipt,
            authority=authority,
            runner=runner,
            model_profile_versions=model_profile_versions,
            sources=sources,
            source_status=source_status,
            group_targets=route_group_targets(store, runner, package, paper_receipt),
            profile=profile,
        )
    except PublicationNotReady:
        # Seals the readiness record under the run's id and raises, as always.
        return publish_paper_facts(
            workspace=workspace,
            package=package,
            package_dir=package_dir,
            paper_receipt=paper_receipt,
            model_profile_versions=model_profile_versions,
            sources=sources,
            source_status=source_status,
            profile=profile,
        )
    identity = result_identity(
        package,
        model_profile_versions,
        named.machine["domain_payload"],  # type: ignore[arg-type]
        profile=profile,
    )
    stable_id = "MAIN-" + identity
    receipt_refs = tuple(
        ref
        for ref in (
            *[o.receipt_ref for o in paper_receipt.route_group_outcomes],
            *[w.receipt_ref for w in paper_receipt.reaction_window_outcomes],
        )
        if ref is not None
    )
    # The stable execution is named by content alone; the run's receipts are its
    # provenance, sealed beside it under their own content-derived execution so a
    # second run with the same facts (different receipts) is the same publication.
    seal_assembly_record(
        workspace,
        "literature-main-receipt",
        {
            "schema_version": "tsdaf.literature-main-receipt.v1",
            "paper_id": package.manifest.paper_id,
            "package_sha256": package.canonical_content_sha256,
            "identity": identity,
        },
        (),
        execution_id=stable_id,
    )
    provenance_ref, _ = seal_assembly_record(
        workspace,
        "literature-main-provenance",
        {
            "schema_version": "tsdaf.literature-main-provenance.v1",
            "identity": identity,
            "receipt_refs": [ref.model_dump(mode="json") for ref in receipt_refs],
            "source_execution_ids": sorted(
                {
                    paper_receipt.literature_execution_id,
                    *(w.window_execution_id for w in paper_receipt.reaction_window_outcomes),
                }
            ),
        },
        receipt_refs,
    )
    stable = paper_receipt.model_copy(update={"literature_execution_id": stable_id})
    published = publish_paper_facts(
        workspace=workspace,
        package=package,
        package_dir=package_dir,
        paper_receipt=stable,
        model_profile_versions=model_profile_versions,
        sources=sources,
        source_status=source_status,
        profile=profile,
    )
    # The publication record: one row per published result and the run that
    # published it. It names the result and carries the provenance record, so the
    # run's receipts are one hop from the stable execution: record -> provenance ->
    # receipts, every link a content reference in the object store.
    seal_assembly_record(
        workspace,
        "literature-publication",
        {
            "schema_version": "tsdaf.literature-publication-record.v1",
            "paper_id": package.manifest.paper_id,
            "machine_result_id": published.machine["machine_result_id"],
            "machine_revision": published.machine["machine_revision"],
            "execution_id": stable_id,
            "machine_artifact": dict(published.machine_artifact_ref),
            "material_sources": dict(published.material_sources),
            "observation_count": len(published.observations),
            "provenance": {
                "sha256": provenance_ref.sha256,
                "media_type": provenance_ref.media_type,
                "byte_size": provenance_ref.byte_size,
            },
        },
        (resolve_public_ref(store, published.machine_artifact_ref), provenance_ref),
    )
    return published


def publish_paper_facts(
    *,
    workspace: Path,
    package: CanonicalPaperPackage,
    package_dir: Path,
    paper_receipt: LiteraturePaperReceipt,
    model_profile_versions: Sequence[Mapping[str, object]],
    sources: LiteratureSources | None = None,
    source_status: PaperSourceStatus = UNKNOWN_SOURCE_STATUS,
    profile: MachineProfile = MachineProfile.V1,
) -> PublishedPaperFactSet:
    """Verify the sealed execution, assemble the bundle and publish it."""

    if not isinstance(workspace, Path) or not isinstance(package_dir, Path):
        raise TypeError("workspace and package_dir must be pathlib.Path values")
    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)
    # `finish_execution` is idempotent and returns the terminal manifest, which the
    # paper run has already sealed; this recovers that ref without a second seal.
    terminal_manifest = runner.finish_execution(
        paper_receipt.literature_execution_id, ExecutionStatus.SUCCEEDED
    )
    authority = build_fact_publication_authority(
        package,
        package_dir,
        terminal_manifest,
        store,
        runner,
        supporting_terminal_manifests=evidence_terminal_manifests(store, runner, paper_receipt),
        sources=sources,
    )
    try:
        bundle = build_paper_machine_result(
            store=store,
            package=package,
            paper_receipt=paper_receipt,
            authority=authority,
            runner=runner,
            model_profile_versions=model_profile_versions,
            sources=sources,
            source_status=source_status,
            group_targets=route_group_targets(store, runner, package, paper_receipt),
            profile=profile,
        )
    except PublicationNotReady as error:
        receipt_ref = store.put_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "tsdaf.literature-publication-readiness.v1",
                    "status": "NOT_READY",
                    "paper_id": package.manifest.paper_id,
                    "reason": error.reason,
                    "excluded_route_groups": error.excluded_route_groups
                    if isinstance(error, PaperPublicationNotReady)
                    else {},
                    "execution_manifest_ref": terminal_manifest.model_dump(mode="json"),
                    "paper_receipt": paper_receipt.model_dump(mode="json"),
                }
            ),
            media_type="application/vnd.tsdaf.literature-publication-readiness+json",
        )
        readiness_id = paper_receipt.literature_execution_id + "-PUBLICATION"

        def seal_readiness(attempt_id: str, content_key: str) -> StageResult:
            now = datetime.now(UTC)
            return StageResult(
                schema_version="tsdaf.stage-result.v1",
                execution_id=readiness_id,
                attempt_id=attempt_id,
                stage_name="literature_publication_readiness",
                stage_version="1.0.0",
                input_content_key=content_key,
                status=ExecutionStatus.SUCCEEDED,
                output_artifact=receipt_ref,
                failure=None,
                cached_from_result=None,
                started_at=now,
                finished_at=now,
            )

        runner.run_stage(
            execution_id=readiness_id,
            stage_name="literature_publication_readiness",
            stage_version="1.0.0",
            input_artifacts=(receipt_ref,),
            settings={},
            invoke=seal_readiness,
        )
        runner.finish_execution(readiness_id, ExecutionStatus.SUCCEEDED)
        error.readiness_ref = receipt_ref
        raise
    published = FactPublisher().publish(
        bundle.machine,
        bundle.observations,
        authority,
        store,
        provenance=bundle.observation_provenance,
    )
    return PublishedPaperFactSet(
        machine=published.machine,
        observations=published.observations,
        machine_artifact_ref=published.machine_artifact_ref,
        observation_artifact_refs=published.observation_artifact_refs,
        review_readable_artifacts=published.review_readable_artifacts,
        published_route_group_ids=tuple(p.route_group_id for p in bundle.projections),
        excluded_route_groups=bundle.excluded_route_groups,
        observation_provenance=bundle.observation_provenance,
        verifying_execution_ids=bundle.verifying_execution_ids,
        block_provenance=bundle.block_provenance,
        material_sources=bundle.material_sources,
    )
