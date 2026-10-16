"""Deterministic MAIN baselines and append-only assembly of sealed enrichments."""

from __future__ import annotations


import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from tsdaf.contracts.execution import ExecutionStatus
from tsdaf.contracts.literature.objects import initialize_open_collections
from tsdaf.contracts.literature.schema import (
    MACHINE_SCHEMA_VERSIONS,
    MachineProfile,
    machine_profile,
    validate_profile_transition,
)
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.application_contributions import (
    finding_applications,
    remap_application_targets,
)
from tsdaf.lanes.literature.completion_guard import (
    cited_sites,
    products_without_an_account,
    route_steps,
    steps_without_an_account,
    unread_experimental_prose,
)
from tsdaf.lanes.literature.container_enrichment import (
    apply_container_enrichment_items,
    container_view,
    content_hash,
    public_narratives,
    withheld_narratives,
)
from tsdaf.lanes.literature.enrichment_models import (
    InvestigationReceipt,
    cited_selection_ids,
)
from tsdaf.lanes.literature.evidence import furniture_by_package
from tsdaf.lanes.literature.investigation_kinds import investigation_kind
from tsdaf.lanes.literature.material_bindings import advance_bindings, initial_bindings
from tsdaf.lanes.literature.material_sources import bound_material_sources, material_sources_for
from tsdaf.lanes.literature.middle_layer import ReadingAnswer
from tsdaf.lanes.literature.selection_resolution import resolve_text_selection
from tsdaf.lanes.literature.source_manifest import LiteratureSources, general_method_targets
from tsdaf.lanes.literature.stage_ledger import (
    EnrichmentLedgers,
    general_methods_first_blocks,
    remount_published_stages,
)
from tsdaf.workflow.investigation_origins import (
    InvestigationOrigins,
    expression_creation_keys,
    bind_creation_origins,
    narrative_creation_keys,
)
from tsdaf.workflow.literature_fact_publication import (
    FactPublisher,
    build_fact_publication_authority,
)
from tsdaf.workflow.literature_investigation_runtime import SealedInvestigation
from tsdaf.workflow.literature_paper_facts import (
    ASSEMBLY_VERSION,
    PublishedPaperFactSet,
    compose_detail_identity,
    finalize_public_result,
    golden_schema_sha256,
    reconcile_si_reasons,
    resolve_public_ref,
    seal_assembly_record,
    stable_main_receipt,
)
from tsdaf.workflow.literature_paper_runtime import LiteraturePaperReceipt
from tsdaf.workflow.local_runtime import LocalObjectStore, LocalRunner
from tsdaf.workflow.reading_skeleton import replay_skeleton_repairs
from tsdaf.workflow.reading_repair_mode import SPARSE, reading_repair_mode
from tsdaf.workflow.reading_repair_plan import verify_sparse_repair_plan
from tsdaf.workflow.source_investigation import restore_investigation_domain
from tsdaf.workflow.unit_decisions import paper_completeness

PUBLICATION_MEDIA = "application/vnd.tsdaf.literature-enrichment-publication+json"


def public_ref(ref: ArtifactRef, name: str) -> dict[str, object]:
    return {
        "sha256": ref.sha256,
        "media_type": ref.media_type,
        "byte_size": ref.byte_size,
        "logical_name": name,
    }


def seal_main_baseline(
    *,
    workspace: Path,
    sources: LiteratureSources,
    receipt: LiteraturePaperReceipt,
    model_profile_versions: Sequence[Mapping[str, object]],
) -> PublishedPaperFactSet:
    """The main baseline under its content-derived execution: one implementation,
    `stable_main_receipt` (E66); this keeps the older signature for its callers."""
    assert sources.main.package is not None and sources.main.package_root is not None
    return stable_main_receipt(
        workspace=workspace,
        package=sources.main.package,
        package_dir=sources.main.package_root,
        paper_receipt=receipt,
        model_profile_versions=model_profile_versions,
    )


@dataclass(frozen=True, slots=True)
class EnrichmentPublication:
    published: PublishedPaperFactSet
    receipt_ref: ArtifactRef
    original_main_ref: ArtifactRef
    # The increments this revision refused, one per item, as the sealed assembly record
    # also lists them (audit L8-08); each is an unresolved item in the published result.
    assembly_failures: tuple[dict[str, str], ...] = ()
    # Per-revision provenance, not ownership of an entire public field. The prior
    # sealed assembly remains authoritative for earlier application records.
    applications: tuple[dict[str, Any], ...] = ()
    creation_keys: dict[str, str] = field(default_factory=dict)


def _observation_refs(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    return [
        {"observation_id": o["observation_id"], "source_observation_sha256": content_hash(o)}
        for o in observations
    ]


CHARACTERIZATION_PACKAGE_MEDIA_TYPE = "application/vnd.tsdaf.characterization-package+json"


def _seal_characterization_packages(
    store: LocalObjectStore,
    machine: Mapping[str, Any],
    receipt: InvestigationReceipt,
    placed: Mapping[str, list[str]],
) -> list[tuple[str, ArtifactRef]]:
    """One sealed package per compound the receipt's cards characterize (ledger E47).

    The card in the main result stays light; the package beside it carries the same
    cards with the cited passages, so a reviewer opens one artifact per compound. The
    main result references each package as a stage output.
    """
    if receipt.answer is None or not receipt.answer.characterizations:
        return []
    # Where each card landed: its own CHAR-ENR record, or the fusion card it merged into.
    by_id = {}
    for card in receipt.answer.characterizations:
        for record_id in placed.get(card.characterization_id, ()):
            by_id.setdefault(record_id, []).append(card)
    records = [
        record
        for record in machine["domain_payload"]["characterization_records"]
        if record["characterization_id"] in by_id
    ]
    refs: list[tuple[str, ArtifactRef]] = []
    for compound_id in sorted({cid for record in records for cid in record["compound_ids"]}):
        cards = [record for record in records if compound_id in record["compound_ids"]]
        excerpts = []
        for record in cards:
            for sid in dict.fromkeys(sid for contribution in by_id[record["characterization_id"]]
                                     for sid in contribution.selection_ids):
                selection = receipt.selections[sid]
                excerpts.append(
                    {
                        "characterization_id": record["characterization_id"],
                        "selection_id": sid,
                        "document_revision_id": selection.get("document_revision_id"),
                        "source_id": selection.get("source_id"),
                        "source_phase": selection.get("source_phase"),
                        "exact_text": selection.get("exact_text"),
                    }
                )
        package = {
            "schema_version": "tsdaf.characterization-package.v1",
            "paper_id": receipt.paper_id,
            "compound_id": compound_id,
            "cards": cards,
            "excerpts": excerpts,
        }
        ref = store.put_bytes(
            canonical_json_bytes(package), media_type=CHARACTERIZATION_PACKAGE_MEDIA_TYPE
        )
        refs.append((compound_id, ref))
    return refs


def _load_investigations(
    store: LocalObjectStore, runner: LocalRunner, sealed: Sequence[SealedInvestigation],
    *, closures_by_receipt=None,
) -> list[InvestigationReceipt]:
    """Each sealed receipt, checked against its producer's terminal closure -- the
    closures verified in one call, since a round's tasks share their upstream."""
    receipts = []
    for one in sealed:
        receipt = InvestigationReceipt.model_validate_json(store.read_bytes(one.receipt_ref))
        if receipt != one.receipt:
            raise ValueError("investigation memory differs from its sealed result")
        receipts.append(receipt)
    closures = runner.verified_execution_artifact_closures(
        [
            (receipt.execution_id, one.terminal_manifest_ref)
            for receipt, one in zip(receipts, sealed, strict=True)
        ]
    )
    for one, closure in zip(sealed, closures, strict=True):
        if one.receipt_ref not in closure:
            raise ValueError("investigation is not in its producer's terminal closure")
        if closures_by_receipt is not None:
            closures_by_receipt[one.receipt_ref] = tuple(closure)
    return receipts


def _cited_sites(receipt: InvestigationReceipt) -> set[tuple[str, str]]:
    """The source blocks this receipt's findings actually point at, by revision and block.

    A citation names a selection and a selection names a range of one block; the range is
    dropped here because the question asked of it is whether a block the Host delivered
    was used at all, and a task that cited two sentences of the paragraph it was handed
    used the paragraph.
    """
    answer = receipt.raw_model()
    return cited_sites(cited_selection_ids(answer, receipt.selections), receipt.selections)


def round_reconciliation(
    dispatched_against: Mapping[str, Any],
    sources: LiteratureSources,
    receipts: Sequence[InvestigationReceipt],
    failed_tasks: Sequence[Mapping[str, Any]] = (),
    *,
    delivered_receipts: Mapping[str, InvestigationReceipt] | None = None,
    written_ids: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Mechanical activity records, never a scientific completion verdict.

    The baseline supplies dispatched scope. Delivery, citation and writing counts do
    not establish understanding, absence or a need to add scientific values.
    Returns (steps without recorded additions, experimental prose not delivered,
    host deliveries without a citation); publication retains these in its audit.
    """
    written: set[str] = set(written_ids)
    excused: set[str] = set()
    scopes: dict[str, list[dict[str, Any]]] = {}
    uncited: list[dict[str, Any]] = []
    for receipt in receipts:
        # A locator's raw paragraphs are its delivery. Its compiled answer deliberately
        # has no extracted values; compilation must not erase the citations it made.
        delivered = (delivered_receipts or {}).get(receipt.execution_id, receipt)
        cited_sites = _cited_sites(delivered)
        for scope in receipt.read_scopes:
            revision = str(scope.get("document_revision_id"))
            scopes.setdefault(revision, []).append(dict(scope))
            source_id = str(scope.get("source_id"))
            if scope.get("delivered_by_host") and (revision, source_id) not in cited_sites:
                uncited.append(
                    {
                        "document_revision_id": revision,
                        "source_id": source_id,
                        "execution_id": receipt.execution_id,
                        **({"step": scope["step"]} if scope.get("step") else {}),
                    }
                )
        answer = receipt.answer
        if answer is None:
            continue
        written.update(patch.target_id for patch in answer.patches)
        written.update(stage.operation_id for stage in answer.new_stages)
        written.update(elision.operation_id for elision in answer.procedure_elisions)
        written.update(condition.operation_id for condition in answer.new_conditions)
        written.update(outcome.reaction_block_id for outcome in answer.new_outcomes)
        written.update(operation.reaction_block_id for operation in answer.new_operations)
        written.update(
            compound_id for card in answer.characterizations for compound_id in card.compound_ids
        )
        excused.update(limitation.scope for limitation in answer.limitations)
    unaccounted: list[dict[str, Any]] = [
        dict(step)
        for step in steps_without_an_account(
            container_view(dispatched_against), written_ids=written, named_scopes=excused
        )
    ]
    # A round is reconciled against the tasks it dispatched, not only against the ones
    # that came back. Without this a task refused before it reached a provider leaves
    # its steps looking exactly like steps a task looked at and found nothing in -- and
    # on f03 that is thirteen of thirteen. "Nobody found anything" and "nobody looked"
    # are different findings, and only one of them is a result.
    steps_of = {
        str(step["reaction_block_id"]): step
        for step in route_steps(container_view(dispatched_against))
    }
    for task in failed_tasks:
        block_ids = task.get("reaction_block_ids")
        for block_id in block_ids if block_ids is not None else [None]:
            named = steps_of.get(str(block_id), {})
            unaccounted = [row for row in unaccounted if row.get("reaction_block_id") != block_id]
            unaccounted.append(
                {
                    "reaction_block_id": block_id,
                    "step": named.get("step", "the paper"),
                    "failure_code": task.get("failure_code"),
                }
            )
    unread: list[dict[str, Any]] = []
    for doc in sources.active():
        # Only a document whose experimental part the split marked can be reconciled;
        # with the whole paper readable the host cannot tell experiment from discussion.
        if doc.package is None or doc.readable_block_ids is None:
            continue
        revision = doc.registration.document_revision_id
        for left in unread_experimental_prose(
            doc.package.blocks,
            readable_ids=doc.part_ids,
            delivered_scopes=scopes.get(revision, ()),
        ):
            unread.append({"document_revision_id": revision, **left})
    return unaccounted, unread, uncited


def _counts_against_the_paper(receipt: InvestigationReceipt) -> bool:
    """Apply the kind's scientific declaration / actual failure policy once."""
    return investigation_kind(receipt.kind).counts_against_paper(receipt)


def _publication_receipt(
    receipt: InvestigationReceipt,
    machine: Mapping[str, Any],
    *,
    task_baseline: Mapping[str, Any] | None = None,
    material_bindings: Mapping[str, str | None] | None = None,
    creation_keys: Mapping[str, str] | None = None,
    method_targets: Mapping[str, Mapping[str, str]] | None = None,
    measurement_machines: Mapping[str, Mapping[str, Any]] | None = None,
) -> InvestigationReceipt:
    """An ephemeral application view; the caller keeps the original sealed identity."""
    if receipt.raw_answer is None:
        return receipt
    answer = investigation_kind(receipt.kind).publication_answer(
        receipt, machine, task_baseline=task_baseline, material_bindings=material_bindings,
        creation_keys=creation_keys, method_targets=method_targets,
        measurement_machines=measurement_machines,
    )
    return receipt.model_copy(update={"answer": answer, "raw_answer": None})


def _read_registered_si_revisions(
    sources: LiteratureSources, receipts: Sequence[InvestigationReceipt]
) -> set[str]:
    """Current SI text that verified tasks actually received, by registered revision."""
    documents = {
        document.registration.document_revision_id: document
        for document in sources.active_si
        if document.package is not None
    }
    read: set[str] = set()
    for receipt in receipts:
        for scope in receipt.read_scopes:
            revision = scope.get("document_revision_id")
            document = documents.get(revision) if isinstance(revision, str) else None
            if document is None or document.package is None:
                continue
            if document.identity() not in receipt.source_identities:
                continue
            if (
                scope.get("source_phase") != "SI"
                or scope.get("package_sha256") != document.package.canonical_content_sha256
            ):
                continue
            source_id = scope.get("source_id")
            if source_id not in document.readable_ids:
                continue
            block = next((b for b in document.package.blocks if b.block_id == source_id), None)
            if block is None:
                continue
            start = scope.get("start_sentence")
            end = scope.get("end_sentence")
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
            ):
                continue
            try:
                selection = resolve_text_selection(
                    raw_text=block.raw_text,
                    start_sentence=start,
                    end_sentence=end,
                )
            except (TypeError, ValueError):
                continue
            if selection.text.strip():
                read.add(document.registration.document_revision_id)
    return read


def publish_enriched_revision(
    *,
    workspace: Path,
    sources: LiteratureSources,
    baseline: PublishedPaperFactSet,
    previous: EnrichmentPublication | None = None,
    details: Sequence[SealedInvestigation] = (),
    record_receipts: Sequence[SealedInvestigation] = (),
    narrative: SealedInvestigation | None = None,
    failed_details: Sequence[Mapping[str, Any]] = (),
    profile: MachineProfile = MachineProfile.V1,
) -> EnrichmentPublication:
    """Apply only fresh receipts. A late independent narrative creates a direct next version.

    `failed_details` is the round's other half: one entry per task that never sealed a
    receipt, with the blocks it was dispatched for and the code it failed with. Only
    sealed receipts used to reach here, so a task refused before it reached a provider
    left no trace of having been dispatched at all -- on f03, thirteen of thirteen.

    `details` is one experimental-detail receipt per task, **in the order they are to be
    applied**, and that order is the caller's to fix: applying a receipt mints
    `PREVIOUS-<hash>` for a value it displaces and hashes a conflict candidate's value into
    its id, so two runs that apply the same receipts in a different order publish different
    bytes. The caller orders them by the route's own order -- window index, then block --
    never by which task finished first.
    """
    if profile.faithful_mapping:
        from tsdaf.workflow.final_science_publication import publish_final_snapshot
        return publish_final_snapshot(workspace=workspace,sources=sources,baseline=baseline,previous=previous,
            details=details,record_receipts=record_receipts,narrative=narrative,failed_details=failed_details)
    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)
    origin_index = InvestigationOrigins(store.read_bytes)
    original_ref = resolve_public_ref(store, baseline.machine_artifact_ref)
    if previous is not None and previous.original_main_ref != original_ref:
        raise ValueError("the previous publication belongs to a different original MAIN baseline")
    prior = previous.published if previous is not None else baseline
    prior_ref = resolve_public_ref(store, prior.machine_artifact_ref)
    if json.loads(store.read_bytes(prior_ref)) != prior.machine:
        raise ValueError("previous machine differs from its immutable artifact")
    prior_machine = cast(dict[str, Any], prior.machine)
    validate_profile_transition(prior_machine, profile)
    known = {r["sha256"] for r in prior_machine["stage_output_refs"]}
    from tsdaf.workflow.experimental_record_handoff import verify_record_handoff
    if record_receipts and not profile.unified_expression:
        raise ValueError("scientific recording publication requires the candidate unified profile")
    verified_records = {
        sealed.receipt_ref.sha256: verify_record_handoff(sealed, store=store, runner=runner,
            sources=sources, baseline_ref=prior_ref)
        for sealed in record_receipts
    }
    mapped_records = set()
    for final in (*details, narrative):
        if final is None:
            continue
        task_input = json.loads(store.read_bytes(final.receipt.input_ref))
        if record_receipts and final.receipt.kind == "EXPERIMENTAL_READING" and not task_input.get("record_receipt_ref"):
            raise ValueError("a recording round cannot adopt an unbound final mapping")
        if ref := task_input.get("record_receipt_ref"):
            key = ArtifactRef.model_validate(ref).sha256
            if key not in verified_records:
                raise ValueError("final mapping requires its independently verified recording receipt")
            expected = verified_records[key].context()
            if any(task_input.get("context", {}).get(k) != v for k, v in expected.items()):
                raise ValueError("final mapping input differs from its sealed scientific records")
            if final.receipt.baseline_machine_ref != prior_ref:
                raise ValueError("final mapping baseline differs from its recording")
            if not final.receipt.failure and final.receipt.closed_out is None:
                mapped_records.add(key)
    missing_final = set(verified_records) - mapped_records - known
    if missing_final:
        failed_details = (*failed_details, *(
            {"reaction_block_ids": None, "failure_code": "FINAL_MAPPING_NOT_DELIVERED"}
            for _ in missing_final
        ))
    new = [
        sealed
        for sealed in (*record_receipts, *details, narrative)
        if sealed is not None and sealed.receipt_ref.sha256 not in known
    ]
    closures_by_receipt = {}
    additions: list[tuple[SealedInvestigation, InvestigationReceipt]] = list(
        zip(new, _load_investigations(store, runner, new,
                                    closures_by_receipt=closures_by_receipt), strict=True)
    )
    if not additions and previous is not None and machine_profile(prior_machine) is profile:
        return previous
    machine: dict[str, Any] = copy.deepcopy(dict(prior.machine))
    if profile is not MachineProfile.V1:
        machine["schema_version"] = MACHINE_SCHEMA_VERSIONS[profile]
        machine["schema_golden_sha256"] = golden_schema_sha256(profile)
    if profile.open_expression:
        initialize_open_collections(machine["domain_payload"])
    observations = {o["observation_id"]: dict(o) for o in prior.observations}
    # The Host's private record of this revision's stages, elisions and unassigned
    # values (E157): one object for every receipt of the round, and handed to the
    # public assembly last. It is not a field of any fact set -- the previous
    # publication is rebuilt from disk, and its private record is rebuilt from the
    # public stages it published (batch 10), not carried in a private field.
    packages = tuple(d.package for d in sources.documents if d.package is not None)
    ledgers = EnrichmentLedgers(
        material_sources=material_sources_for(prior.machine, prior.material_sources),
        furniture=furniture_by_package(packages),
        general_methods_first_blocks=general_methods_first_blocks(
            (d.registration.document_revision_id, d.units) for d in sources.documents
        ),
    )
    if machine_profile(prior_machine).stages:
        remount_published_stages(ledgers, prior_machine)
    assembly_failures: list[dict[str, str]] = []
    applications: list[dict[str, Any]] = []
    creation_keys = dict(previous.creation_keys) if previous is not None else {}
    # What this round records about its own receipts -- a refused increment, a withheld
    # narrative, a receipt that did not deliver in full -- rather than about the facts.
    # These carry a provisional `_kind` that `finalize_public_result` reads and removes,
    # so a machine holding one is not yet a valid public fact root; the next receipt's
    # projection validates the machine it is handed, so they are added after the last
    # increment, in the order the loop produced them.
    round_items: list[dict[str, Any]] = []
    # Per-compound characterization packages sealed by this revision; they enter the
    # assembly record so the publication's closure authorizes their public refs. They
    # embed the cards' ids, so they are sealed after every id is final (E84).
    package_refs: list[ArtifactRef] = []
    to_seal: list[tuple[InvestigationReceipt, dict[str, list[str]]]] = []
    projected: list[tuple[SealedInvestigation, InvestigationReceipt]] = []
    skeleton_written: set[str] = set()
    skeleton_groups = tuple(prior.skeleton_groups)
    skeleton_identity = {gid: dict(ids) for gid, ids in prior.block_provenance.items()}
    material_bindings = initial_bindings(machine)
    delivered_receipts = {receipt.execution_id: receipt for _, receipt in additions}
    from tsdaf.workflow.fact_source_selections import inherited_fact_selections

    inherited_selections = inherited_fact_selections(
        groups=skeleton_groups, machine=prior.machine, sources=sources,
        store=store, runner=runner, restore_domain=restore_investigation_domain,
    )
    for sealed, receipt in additions:
        if receipt.paper_id != sources.paper_id:
            raise ValueError("enrichment belongs to a different paper")
        # Do not authorize a revision by a package-local ID, nor silently rebind a
        # cached result to a revised attachment with different bytes.
        identities = {d.registration.document_revision_id: d.identity() for d in sources.documents}
        for identity in receipt.source_identities:
            if identity != identities.get(str(identity["document_revision_id"])):
                raise ValueError("enrichment source identity is stale or not registered")
        reason = receipt.failure
        creation_origins = origin_index.derive(receipt)
        from tsdaf.workflow.reading_measurement_context import MeasurementContexts
        measurement_machines = (MeasurementContexts(store.read_bytes).restore(
            receipt, origins=origin_index, closure=closures_by_receipt[sealed.receipt_ref],
            restore_domain=restore_investigation_domain,
        ) if receipt.kind == "EXPERIMENTAL_READING" else {})
        spec = investigation_kind(receipt.kind)
        if spec.records_science and sealed.receipt_ref.sha256 not in verified_records:
            raise ValueError("scientific records must enter through the verified record handoff")
        if spec.publication_channel == "CONTAINER":
            try:
                # Only after the original receipt and source bindings were verified:
                # Keep cumulative wrapping and task-time scientific comparison separate.
                # Earlier receipts enrich materials, but do not change what this task saw.
                raw_reading = receipt.raw_model()
                candidate_written = frozenset[str]()
                candidate_machine = machine
                candidate_groups, candidate_identity = skeleton_groups, skeleton_identity
                candidate_ledgers = copy.deepcopy(ledgers)
                comparison = prior.machine
                candidate_bindings = dict(material_bindings)
                task_input = (json.loads(store.read_bytes(receipt.input_ref))
                              if receipt.input_ref is not None else {})
                if isinstance(raw_reading, ReadingAnswer):
                    from tsdaf.lanes.literature.middle_layer import verify_reading_update_mode
                    verify_reading_update_mode(raw_reading, task_input)
                sparse = reading_repair_mode(task_input) == SPARSE
                if isinstance(raw_reading, ReadingAnswer) and (raw_reading.skeleton_repairs or sparse):
                    if receipt.input_ref is None:
                        raise ValueError("skeleton repair has no sealed task input")
                    if task_input["baseline_machine_sha256"] != content_hash(prior.machine):
                        raise ValueError("skeleton repair is bound to a stale machine")
                    if sparse:
                        plan = verify_sparse_repair_plan(
                            receipt, machine=prior.machine, store=store,
                            accepted_refs=closures_by_receipt[sealed.receipt_ref],
                            restore_domain=restore_investigation_domain,
                            inherited_selections=inherited_selections,
                        )
                        replay = plan.replay
                        origins = {}
                    else:
                        replay = replay_skeleton_repairs
                        origins = {"repair_origins": origin_index.repair_birth_origins}
                    repaired = replay(
                        raw_reading,
                        machine=machine,
                        context=task_input["context"],
                        selections=receipt.selections,
                        store=store,
                        **origins,
                        material_sources=candidate_ledgers.material_sources,
                        material_bindings=candidate_bindings,
                        inherited_selections=inherited_selections,
                    )
                    comparison = replay(
                        raw_reading,
                        machine=prior.machine,
                        context=task_input["context"],
                        selections=receipt.selections,
                        store=store,
                        **origins,
                        inherited_selections=inherited_selections,
                    ).machine
                    candidate_machine = repaired.machine
                    candidate_bindings = repaired.material_bindings
                    candidate_groups, candidate_identity = repaired.groups, repaired.identity
                    candidate_written = repaired.written_ids
                    candidate_ledgers.material_sources = repaired.material_sources
                receipt = _publication_receipt(
                    receipt,
                    candidate_machine,
                    task_baseline=comparison,
                    measurement_machines=measurement_machines,
                    material_bindings=candidate_bindings,
                    creation_keys=expression_creation_keys(sources.paper_id, creation_origins),
                    method_targets=general_method_targets(tuple(
                        document for document in sources.documents
                        if document.registration.document_revision_id in {
                            str(identity["document_revision_id"])
                            for identity in receipt.source_identities
                        }
                    )),
                )
                before_detail = candidate_machine
                # Every receipt of this round was dispatched against `prior.machine`
                # and is checked against it, while it is applied to the machine the
                # receipts before it have already enriched.
                applied_locations: dict[tuple[str, int], list[dict[str, str]]] = {}
                candidate_machine, refused, placed = apply_container_enrichment_items(
                    candidate_machine,
                    tuple(observations.values()),
                    receipt,
                    bound_to=prior.machine,
                    ledgers=candidate_ledgers,
                    applied_locations=applied_locations,
                )
                # Publish this receipt's skeleton and detail as one successful assembly.
                material_bindings = advance_bindings(
                    candidate_bindings,
                    before_detail,
                    candidate_machine,
                    {key: key for key in initial_bindings(before_detail)},
                )
                machine, ledgers = candidate_machine, candidate_ledgers
                skeleton_groups, skeleton_identity = candidate_groups, candidate_identity
                skeleton_written.update(candidate_written)
                applications.extend(
                    {"receipt_ref": sealed.receipt_ref.model_dump(mode="json"), **row}
                    for row in bind_creation_origins(
                        finding_applications(
                            raw_reading, receipt.answer, applied_locations, refused
                        ),
                        creation_origins,
                    )
                )
                # Audit L8-08: a refused increment is one unresolved item that says why, and
                # one assembly diagnostic; the receipt's accepted increments are published.
                # Ids here are provisional: the one rule keys every item below.
                for failure in refused:
                    item_key = f"{failure['collection']}:{failure['index']}"
                    round_items.append(
                        {
                            "unresolved_item_id": "UNR-ENR-ITEM-"
                            + content_hash([sealed.receipt_ref.sha256, item_key])[:24],
                            "_kind": "REFUSED_INCREMENT",
                            "scope": failure["scope"],
                            "subject_id": failure["subject_id"],
                            "reason_code": "STAGE_PARTIAL",
                            "statement": failure["reason"],
                            "evidence_refs": failure["evidence_refs"],
                        }
                    )
                    assembly_failures.append(
                        {
                            "receipt_sha256": sealed.receipt_ref.sha256,
                            "item": item_key,
                            "reason": failure["reason"],
                        }
                    )
                to_seal.append((receipt, placed))
            except ValueError as error:
                # A stale experimental delta cannot discard a valid independent N result.
                reason = str(error)
                assembly_failures.append(
                    {"receipt_sha256": sealed.receipt_ref.sha256, "reason": reason}
                )
        else:
            receipt = _publication_receipt(receipt, machine)
        narratives = {
            n["narrative_id"]: n for n in machine["domain_payload"]["paper_narratives"]
        }
        item_keys = narrative_creation_keys(sources.paper_id, creation_origins)
        narrative_applications: list[dict[str, Any]] = []
        narrative_failures: list[dict[str, Any]] = []
        for item in public_narratives(
            receipt, machine, creation_keys=item_keys, applications=narrative_applications,
            failures=narrative_failures,
        ):
            narratives[item["narrative_id"]] = item
        if spec.records_science:
            for row in narrative_applications:
                row["collection"] = "records"
        for row in bind_creation_origins(narrative_applications, creation_origins):
            if row["public_targets"] and row["item_id"] in item_keys:
                creation_keys[row["public_targets"][0]["object_id"]] = item_keys[row["item_id"]]
            applications.append(
                {"receipt_ref": sealed.receipt_ref.model_dump(mode="json"), **row}
            )
        machine["domain_payload"]["paper_narratives"] = list(narratives.values())
        for failure in narrative_failures:
            item_key = f"narratives:{failure['index']}"
            assembly_failures.append({
                "receipt_sha256": sealed.receipt_ref.sha256,
                "item": item_key, "reason": failure["reason"],
            })
            round_items.append({
                "unresolved_item_id": "UNR-ENR-ITEM-"
                + content_hash([sealed.receipt_ref.sha256, item_key])[:24],
                "_kind": "REFUSED_INCREMENT", "scope": "PAPER",
                "subject_id": sources.paper_id, "reason_code": "STAGE_PARTIAL",
                "statement": failure["reason"], "evidence_refs": failure["evidence_refs"],
            })
        for withheld in withheld_narratives(receipt, machine):
            round_items.append(
                {
                    "unresolved_item_id": "UNR-ENR-NARRATIVE-"
                    + content_hash(
                        [sealed.receipt_ref.sha256, withheld["kind"], withheld["title"]]
                    )[:24],
                    "_kind": f"WITHHELD_NARRATIVE:{withheld['kind']}",
                    "scope": "PAPER",
                    "subject_id": sources.paper_id,
                    "reason_code": "STAGE_PARTIAL",
                    "evidence_refs": withheld["evidence_refs"],
                }
            )
        projected.append((sealed, receipt))
        for observation in receipt.observations:
            oid = observation["observation_id"]
            if oid in observations and observations[oid] != observation:
                raise ValueError("an observation identity cannot change across machine revisions")
            observations[oid] = dict(observation)
        # Only a declared scientific partial or an actual failure creates this public
        # item. Empty collections are legitimate for a completed investigation.
        if reason or spec.counts_against_paper(delivered_receipts[receipt.execution_id]):
            round_items.append(
                {
                    "unresolved_item_id": "UNR-ENR-" + sealed.receipt_ref.sha256,
                    "_kind": f"PARTIAL_RECEIPT:{receipt.kind}",
                    "scope": "PAPER",
                    "subject_id": sources.paper_id,
                    "reason_code": "STAGE_PARTIAL",
                    "evidence_refs": _observation_refs(receipt.observations),
                }
            )
        machine["stage_output_refs"].append(public_ref(sealed.receipt_ref, receipt.kind))
    # The sealed references are unchanged. Reconciliation, completeness and package
    # assembly read exactly the application view, never a second compilation.
    additions = projected
    # What this round of detail tasks left open, derived once from every receipt's own
    # ledger rather than claimed by any one of them. One task that failed leaves its own
    # steps without an account and says so here; it does not speak for the others.
    detail_receipts = [r for _, r in additions if investigation_kind(r.kind).reconcile_detail]
    unaccounted: list[dict[str, Any]] = []
    unread_prose: list[dict[str, Any]] = []
    uncited_deliveries: list[dict[str, Any]] = []
    if detail_receipts or failed_details:
        unaccounted, unread_prose, uncited_deliveries = round_reconciliation(
            prior_machine,
            sources,
            detail_receipts,
            failed_details,
            delivered_receipts=delivered_receipts,
            written_ids=frozenset(skeleton_written),
        )
    missing_products = (
        products_without_an_account(
            container_view(prior_machine),
            characterized_ids=(
                cid
                for card in machine["domain_payload"]["characterization_records"]
                for cid in card["compound_ids"]
            ),
            named_scopes=(
                limitation.scope
                for receipt in detail_receipts
                if receipt.answer is not None
                for limitation in receipt.answer.limitations
            ),
        )
        if detail_receipts
        else []
    )
    # Reading activity is audit evidence only. A failed dispatch is an actual execution
    # failure; do not conflate it with a task that completed without adding a value.
    for step in unaccounted:
        if not step.get("failure_code"):
            continue
        block_id = step["reaction_block_id"]
        round_items.append(
            {
                "unresolved_item_id": "UNR-ENR-REFUSED-"
                + content_hash([prior_ref.sha256, str(block_id)])[:24],
                "_kind": "TASK_DID_NOT_RUN",
                "scope": "REACTION_BLOCK" if block_id is not None else "PAPER",
                "subject_id": block_id if block_id is not None else sources.paper_id,
                "reason_code": "STAGE_PARTIAL",
                "statement": f"The task dispatched for {step['step']} did not run "
                f"({step['failure_code']}).",
                "evidence_refs": [],
            }
        )
    machine["unresolved_items"].extend(round_items)
    if not any(r["sha256"] == original_ref.sha256 for r in machine["stage_output_refs"]):
        machine["stage_output_refs"].append(public_ref(original_ref, "original-main-baseline"))
    # Every public id from the one rule (E66, E84): the records the receipts added get
    # the ids the rule gives them and merge with what they repeat; the observations
    # (E83) are the prior revision's keyed ones as they are and the receipts' sealed
    # ones keyed now, one per site; every reference follows, before the closure is
    # taken. The packages are sealed last, under the final ids of the cards they hold.
    public = finalize_public_result(
        machine,
        tuple(observations.values()),
        paper_id=sources.paper_id,
        prior_provenance=prior.observation_provenance,
        packages=packages,
        ledgers=ledgers,
        profile=profile,
        creation_keys=creation_keys,
    )
    machine = cast(dict[str, Any], public.machine)
    applications = remap_application_targets(applications, public.id_map)
    narrative_ids = {n["narrative_id"] for n in machine["domain_payload"]["paper_narratives"]}
    creation_keys = {
        public.id_map.get(identity, identity): key for identity, key in creation_keys.items()
        if public.id_map.get(identity, identity) in narrative_ids
    }
    all_observations = tuple(public.observations)
    for receipt, placed in to_seal:
        followed = {
            card_id: [public.id_map.get(record_id, record_id) for record_id in record_ids]
            for card_id, record_ids in placed.items()
        }
        for compound_id, ref in _seal_characterization_packages(store, machine, receipt, followed):
            package_refs.append(ref)
            machine["stage_output_refs"].append(
                public_ref(ref, f"characterization-package-{compound_id}")
            )
    prior_state = prior_machine.get("si_registration")
    registered_by_revision: dict[str, str] = {}
    if prior_state is not None:
        for document in sources.active_si:
            package = document.package
            if package is not None:
                registered_by_revision[document.registration.document_revision_id] = (
                    package.stable_revision_id("SI")
                )
    revisions = sorted(
        set(prior_machine["si_package_revision_ids"])
        | {str(o["document_revision_id"]) for o in all_observations if o["source_phase"] == "SI"}
        | set(registered_by_revision.values())
    )
    machine["si_package_revision_ids"] = revisions
    if prior_state is not None:
        stable_registered = set(registered_by_revision.values())
        registered = stable_registered | set(registered_by_revision)
        if not registered and prior_state in {"REGISTERED_UNREAD", "REGISTERED_READ"}:
            raise ValueError(
                "previous SI registration cannot be rebound without a current available SI"
            )
        previous_si = set(prior_machine["si_package_revision_ids"])
        retained_read = (
            prior_state == "REGISTERED_READ"
            and bool(previous_si)
            and previous_si <= registered
            and all(
                revision not in previous_si or stable_id in previous_si
                for revision, stable_id in registered_by_revision.items()
            )
            and bool(previous_si & stable_registered)
        )
        read = retained_read or bool(
            _read_registered_si_revisions(sources, [receipt for _, receipt in additions])
        )
        state = (
            ("REGISTERED_READ" if read else "REGISTERED_UNREAD") if registered else str(prior_state)
        )
        machine["coverage"] = "MAIN_SI" if read else "MAIN_ONLY"
        machine["si_registration"] = state
        machine["unresolved_items"] = reconcile_si_reasons(machine["unresolved_items"], state)
    else:
        # Historical results without SI registration keep their existing source-based
        # coverage rule; do not reinterpret their sealed observations as a new state.
        machine["coverage"] = "MAIN_SI" if revisions else "MAIN_ONLY"
    # Completeness follows the same rule as the first revision (E83): it stays what it
    # was, and every receipt added must be a COMPLETE delivery with nothing withheld.
    # Which receipts count, and why a detail task counts differently from a narrative
    # one, is `_counts_against_the_paper`.
    machine["completeness"] = paper_completeness(
        group_decisions=(),
        window_decisions=(),
        excluded_groups=0,
        pending_units=0,
        prior=str(prior_machine["completeness"]),
        partial_receipts=sum(1 for _, receipt in additions if _counts_against_the_paper(receipt))
        + (1 if failed_details else 0),
    )
    if additions or machine_profile(prior_machine) is not profile:
        machine["machine_revision"] = prior_machine["machine_revision"] + 1
        machine["supersedes_machine_result_sha256"] = prior_ref.sha256
    inputs = (original_ref, prior_ref, *(s.receipt_ref for s, _ in additions), *package_refs)
    assembly_payload = {
        "schema_version": "tsdaf.literature-enrichment-assembly.v1",
        "machine_schema_version": machine["schema_version"],
        "schema_golden_sha256": machine["schema_golden_sha256"],
        "original_main_ref": original_ref.model_dump(mode="json"),
        "previous_machine_ref": prior_ref.model_dump(mode="json"),
        "investigation_refs": [s.receipt_ref.model_dump(mode="json") for s, _ in additions],
        "source_identities": [d.identity() for d in sources.documents],
        "assembly_failures": assembly_failures,
        "applications": applications,
        "creation_keys": creation_keys,
        "activity_diagnostics": {
            "steps_without_recorded_additions": unaccounted,
            "experimental_prose_not_delivered": unread_prose,
            "host_deliveries_without_citation": uncited_deliveries,
            "products_without_characterization_or_limitation": missing_products,
        },
        "skeleton_groups": list(skeleton_groups),
        "block_provenance": compose_detail_identity(skeleton_identity, public.id_map),
        "domain_sha256": content_hash(machine["domain_payload"]),
    }
    _, terminal = seal_assembly_record(
        workspace, "literature-enrichment-assembly", assembly_payload, inputs
    )
    terminal_data = json.loads(store.read_bytes(terminal))
    # The executions whose closures hold the sealed originals of every public
    # observation: the prior revision's, and those of the receipts added now.
    supporting_ids = set(prior.verifying_execution_ids) | set(public.execution_ids)
    supporting_ids.update(
        (str(prior.machine["execution_id"]), str(baseline.machine["execution_id"]))
    )
    supporting_ids.update(r.execution_id for _, r in additions)
    # These refs were already verified with each receipt by _load_investigations.
    # A failed producer is valid diagnostic evidence, not a request to rewrite its
    # terminal status. The same applies to producers inherited from a prior revision.
    investigation_terminals = {
        receipt.execution_id: sealed.terminal_manifest_ref for sealed, receipt in additions
    }
    supporting = tuple(
        investigation_terminals[eid]
        if eid in investigation_terminals
        else runner.verified_terminal_manifest(eid)
        for eid in sorted(supporting_ids)
        if eid != terminal_data["execution_id"]
    )
    assert sources.main.package is not None and sources.main.package_root is not None
    authority = build_fact_publication_authority(
        sources.main.package,
        sources.main.package_root,
        terminal,
        store,
        runner,
        supporting_terminal_manifests=supporting,
        sources=sources,
    )
    machine["execution_id"] = authority.execution_id
    machine["execution_manifest_ref"] = public_ref(terminal, "execution-manifest")
    machine["source_artifact_refs"] = [
        public_ref(r, "source-artifact") for r in authority.review_readable_artifacts
    ]
    machine["machine_result_id"] = "MPR-" + content_hash(
        [ASSEMBLY_VERSION, prior_ref.sha256, assembly_payload]
    )
    published = FactPublisher().publish(
        machine, all_observations, authority, store, provenance=public.provenance
    )
    result = PublishedPaperFactSet(
        **published.__dict__,
        published_route_group_ids=prior.published_route_group_ids,
        excluded_route_groups=prior.excluded_route_groups,
        material_sources=bound_material_sources(machine, public.material_sources),
        observation_provenance=dict(public.provenance),
        verifying_execution_ids=frozenset(supporting_ids),
        block_provenance=compose_detail_identity(skeleton_identity, public.id_map),
        skeleton_groups=skeleton_groups,
    )
    publication_payload = {
        "schema_version": "tsdaf.literature-enrichment-publication.v1",
        "assembly_version": ASSEMBLY_VERSION,
        "applications": applications,
        "creation_keys": creation_keys,
        "original_main_ref": original_ref.model_dump(mode="json"),
        "material_sources": dict(result.material_sources),
        "skeleton_groups": list(result.skeleton_groups),
        "block_provenance": dict(result.block_provenance),
        # A loaded revision needs the same original-observation authority as an
        # in-memory revision. Public keyed observations cannot reconstruct it.
        "observation_provenance": dict(result.observation_provenance),
        "verifying_execution_ids": sorted(result.verifying_execution_ids),
        "machine_ref": dict(result.machine_artifact_ref),
        "observation_refs": list(result.observation_artifact_refs),
        "published_route_group_ids": list(result.published_route_group_ids),
        "excluded_route_groups": dict(result.excluded_route_groups),
        "execution_manifest_ref": terminal.model_dump(mode="json"),
    }
    publication_ref, _ = seal_assembly_record(
        workspace,
        "literature-enrichment-publication",
        publication_payload,
        (
            resolve_public_ref(store, result.machine_artifact_ref),
            terminal,
            original_ref,
            *(resolve_public_ref(store, r) for r in result.observation_artifact_refs),
        ),
    )
    return EnrichmentPublication(
        result, publication_ref, original_ref, assembly_failures=tuple(assembly_failures),
        applications=tuple(applications),
        creation_keys=creation_keys,
    )


def load_previous_publication(workspace: Path, path: Path) -> EnrichmentPublication:
    """Load a previously sealed publication receipt, never an editable Machine draft."""
    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)
    raw = path.read_bytes()
    payload = json.loads(raw)
    if payload.get("schema_version") != "tsdaf.literature-enrichment-publication.v1":
        raise ValueError("previous-publication must name an enrichment publication receipt")
    ref = store.resolve_content(
        sha256=content_hash(payload),
        media_type=PUBLICATION_MEDIA,
        byte_size=len(canonical_json_bytes(payload)),
    )
    if ref is None:
        raise ValueError("previous publication is not sealed in this workspace")
    terminal = ArtifactRef.model_validate(payload["execution_manifest_ref"])
    manifest = json.loads(store.read_bytes(terminal))
    runner.verified_execution_artifact_closure(manifest["execution_id"], terminal)
    machine = json.loads(store.read_bytes(resolve_public_ref(store, payload["machine_ref"])))
    if machine["execution_manifest_ref"]["sha256"] != terminal.sha256:
        raise ValueError("previous publication's machine names a different execution")
    observations = tuple(
        json.loads(store.read_bytes(resolve_public_ref(store, r)))
        for r in payload["observation_refs"]
    )
    original_ref = ArtifactRef.model_validate(payload["original_main_ref"])
    inputs = (
        resolve_public_ref(store, payload["machine_ref"]),
        terminal,
        original_ref,
        *(resolve_public_ref(store, r) for r in payload["observation_refs"]),
    )
    versions = {
        stage["stage_version"]
        for stage in manifest["stage_records"]
        if stage["stage_name"] == "literature-enrichment-assembly"
    }
    if len(versions) != 1:
        raise ValueError("previous publication has no unique sealed assembly version")
    assembly_version = next(iter(versions))
    if payload.get("assembly_version", assembly_version) != assembly_version:
        raise ValueError("publication assembly version differs from its verified producer")
    record_id = "literature-enrichment-publication-" + content_hash(
        [
            assembly_version,
            ref.sha256,
            [r.sha256 for r in inputs],
        ]
    )
    record_terminal = runner.finish_execution(record_id, ExecutionStatus.SUCCEEDED)
    if ref not in runner.verified_execution_artifact_closure(record_id, record_terminal):
        raise ValueError("previous publication receipt has no verified seal")
    if machine_profile(machine).faithful_mapping:
        from tsdaf.contracts.literature.validation import validate_fact_bundle
        validate_fact_bundle(machine,observations,
            read_public=lambda ref:store.read_bytes(resolve_public_ref(store,ref)))
    return EnrichmentPublication(
        PublishedPaperFactSet(
            machine=machine,
            observations=observations,
            machine_artifact_ref=payload["machine_ref"],
            observation_artifact_refs=tuple(payload["observation_refs"]),
            review_readable_artifacts=tuple(machine["source_artifact_refs"]),
            published_route_group_ids=tuple(payload["published_route_group_ids"]),
            excluded_route_groups=payload["excluded_route_groups"],
            material_sources=payload.get("material_sources", {}),
            skeleton_groups=tuple(payload.get("skeleton_groups", ())),
            block_provenance=payload.get("block_provenance", {}),
            observation_provenance={
                key: tuple(values)
                for key, values in payload.get("observation_provenance", {}).items()
            },
            verifying_execution_ids=frozenset(payload.get("verifying_execution_ids", ())),
        ),
        ref,
        original_ref,
        applications=tuple(payload.get("applications", ())),
        creation_keys=dict(payload.get("creation_keys", {})),
    )
