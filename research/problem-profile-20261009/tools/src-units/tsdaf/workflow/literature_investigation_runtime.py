"""Independently sealed, content-cached detail and MAIN narrative investigations."""

from __future__ import annotations

from tsdaf.contracts.tool_read import READ_DELIVERY_VERSION
from tsdaf.workflow.compilation_transport import compilation_transport_descriptor

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tsdaf.contracts.execution import (
    ExecutionStatus,
    FailureCode,
    FailureDetail,
    StageResult,
    TypedFailure,
    failure_is_retryable,
)
from tsdaf.contracts.literature.schema import MachineProfile, machine_profile
from tsdaf.contracts.routing import VisualDecision
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.document.paper_identity import pairing_failure
from tsdaf.lanes.literature.bootstrap import verify_router_handoff_bundle
from tsdaf.lanes.literature.container_enrichment import content_hash
from tsdaf.lanes.literature.enrichment_models import InvestigationReceipt, TaskScope
from tsdaf.lanes.literature.gate_models import read_fusion_result
from tsdaf.lanes.literature.investigation_kinds import investigation_kind
from tsdaf.lanes.literature.middle_layer import ParagraphSet
from tsdaf.lanes.literature.model_names import NAMING_VERSION
from tsdaf.lanes.literature.profiles import load_lane_resources
from tsdaf.lanes.literature.provider_port import (
    AUTHORIZED_MODEL_PROFILE,
    LaneInteractionClient,
    ProviderBinding,
)
from tsdaf.lanes.literature.source_investigation import (
    INVESTIGATION_VERSION,
    investigation_resources,
)
from tsdaf.lanes.literature.source_manifest import LiteratureSources
from tsdaf.workflow.call_budget import CallBudget
from tsdaf.workflow.literature_route_runtime import LiteratureRouteGroupReceipt
from tsdaf.workflow.local_runtime import LocalObjectStore, LocalRunner
from tsdaf.workflow.reading_repair_mode import SNAPSHOT, SPARSE
from tsdaf.workflow.reading_scopes import with_independent_scope
from tsdaf.workflow.source_investigation import TaskContextTooLarge, investigate_sources, restore_investigation_domain

INVESTIGATION_MEDIA = "application/vnd.tsdaf.literature-investigation+json"
# Not a failure of the run: no client was configured for this lane, and the chain
# proceeds without it. Every other `failure` a receipt carries fails its stage.
NOT_CONFIGURED = "INVESTIGATION_PROVIDER_NOT_CONFIGURED"


def paragraph_target_binding(receipt, *, machine, context, baseline_ref, store, closure):
    """Reuse sealed locator meanings across profiles only for identical group facts.

    This is not scientific reassignment or a general revision migration. Changed
    facts, missing witnesses and split aliases must be located again or reviewed.
    The caller records the derivation; the old receipt is never modified.
    """
    old_ref = receipt.baseline_machine_ref
    if old_ref is None or baseline_ref is None or old_ref == baseline_ref:
        return {}, None
    if old_ref not in closure or receipt.input_ref not in closure:
        raise ValueError("paragraph baseline/input lacks its original sealed producer")
    old_machine = json.loads(store.read_bytes(old_ref))
    if machine_profile(old_machine) == machine_profile(machine):
        return {}, None
    original = json.loads(store.read_bytes(receipt.input_ref))
    if original.get("baseline_machine_sha256") != old_ref.sha256:
        raise ValueError("paragraph input differs from its original baseline")
    from tsdaf.contracts.literature.validation import fact_target_records

    def types(value):
        return {identity: kind for kind, records in fact_target_records(value).items() for identity in records}

    old_types, new_types = types(old_machine), types(machine)
    old_context = original.get("context", {})
    old_groups = {g["route_group_id"]: g for g in old_context.get("groups", ())}
    new_groups = {g["route_group_id"]: g for g in context.get("groups", ())}
    if (len(old_groups) != len(old_context.get("groups", ()))
            or len(new_groups) != len(context.get("groups", ()))):
        raise ValueError("paragraph target has ambiguous group identity")
    targets = {target for paragraph in receipt.raw_model().paragraphs for target in paragraph.about_ids}
    mapped, proof = {}, []
    checked_groups = set()
    for target in sorted(targets):
        witnesses = [(gid, fid) for gid, ids in old_context.get("block_provenance", {}).items()
                     for fid, public in ids.items() if public == target]
        if not witnesses or target not in old_types:
            raise ValueError("paragraph target lacks an exact original fact witness")
        destinations = set()
        for gid, fid in witnesses:
            if gid not in checked_groups:
                if gid not in old_groups or gid not in new_groups:
                    raise ValueError("paragraph target group is unavailable")
                before = read_fusion_result(canonical_json_bytes(old_groups[gid]["result"]))
                after = read_fusion_result(canonical_json_bytes(new_groups[gid]["result"]))
                if before != after:
                    raise ValueError("paragraph target facts changed across profiles")
                checked_groups.add(gid)
            destination = context.get("block_provenance", {}).get(gid, {}).get(fid)
            if destination is None or new_types.get(destination) != old_types[target]:
                raise ValueError("paragraph target lacks a current same-type projection")
            destinations.add(destination)
        if len(destinations) != 1:
            raise ValueError("paragraph target projection is ambiguous")
        mapped[target] = destinations.pop()
        proof.append({"original": target, "current": mapped[target], "witnesses": witnesses})
    return mapped, {"original_input": receipt.input_ref.model_dump(mode="json"),
                    "original_baseline": old_ref.model_dump(mode="json"),
                    "current_baseline": baseline_ref.model_dump(mode="json"), "targets": proof}


class InvestigationFailed(ValueError):
    """The lane ran and failed; its receipt is sealed and bound to the attempt, its
    stage is not a success and is never served from the cache (ledger E63)."""

    def __init__(
        self,
        failure: TypedFailure,
        receipt_ref: ArtifactRef,
        *,
        stage_name: str,
        content_key: str,
    ) -> None:
        super().__init__(failure.message)
        self.failure = failure
        self.receipt_ref = receipt_ref
        self.stage_name = stage_name
        self.content_key = content_key


def stage_failure(failure: str) -> TypedFailure:
    """The typed failure a receipt's `failure` names: the provider's own code when the
    lane recorded one, otherwise a rejection carrying the lane's diagnostic.

    A code may be followed by `:` and the lane's own words. Without that, a failure the
    Host caused could only be reported by giving up its code: the f03 round's thirteen
    block tasks had an input the Host had built past its own limit, recorded it as
    `INVESTIGATION_INPUT_OR_PROVIDER_FAILURE:ValueError`, and were therefore classified
    as provider rejections and dispatched a second time -- twenty-six executions that
    reached no provider at all. The diagnostic survived and the classification did not,
    which is the wrong way round.
    """
    code_name, _, diagnostic = failure.partition(":")
    try:
        code = FailureCode(code_name)
    except ValueError:
        return TypedFailure(
            schema_version="tsdaf.typed-failure.v1",
            code=FailureCode.PROVIDER_REJECTED,
            message="The investigation lane failed before delivering an answer.",
            request_id=None,
            details=(FailureDetail(key="cause", value=failure[:2000]),),
        )
    return TypedFailure(
        schema_version="tsdaf.typed-failure.v1",
        code=code,
        message="The investigation provider interaction failed.",
        request_id=None,
        details=((FailureDetail(key="cause", value=diagnostic[:2000]),) if diagnostic else ()),
    )


def stage_status(failure: str | None) -> tuple[ExecutionStatus, TypedFailure | None]:
    """The stage status a receipt's `failure` decides: a success only when there is no
    failure or no client was configured; otherwise retryable exactly when the
    contract says the code is. The one rule, used by the invoke and by its test."""
    if failure is None or failure == NOT_CONFIGURED:
        return ExecutionStatus.SUCCEEDED, None
    failed = stage_failure(failure)
    return (
        ExecutionStatus.FAILED_RETRYABLE
        if failure_is_retryable(failed.code)
        else ExecutionStatus.FAILED_TERMINAL
    ), failed


@dataclass(frozen=True, slots=True)
class SealedInvestigation:
    receipt: InvestigationReceipt
    receipt_ref: ArtifactRef
    terminal_manifest_ref: ArtifactRef


def run_literature_investigation(
    *,
    workspace: Path,
    sources: LiteratureSources,
    router_execution_id: str,
    execution_id: str,
    kind: str,
    provider_binding: ProviderBinding,
    client: LaneInteractionClient | None,
    # The figure reader's own stream (V3). A detail task is never handed an image; when
    # it reads one, a separate short sub-execution reads the figure and answers in words.
    # Without this client the task has prose and nothing else, and says so on an image read.
    figure_client: LaneInteractionClient | None = None,
    call_budget: CallBudget | None = None,
    provider_is_live: bool,
    live: bool,
    baseline_machine_ref: ArtifactRef | None = None,
    machine: dict[str, Any] | None = None,
    observations: tuple[dict[str, Any], ...] = (),
    context: dict[str, Any] | None = None,
    first_layer_refs: tuple[ArtifactRef, ...] = (),
    block_ids: tuple[str, ...] | None = None,
    shares_the_round: bool = False,
    paragraph_receipts: tuple[SealedInvestigation, ...] = (),
    record_receipt: SealedInvestigation | None = None,
) -> SealedInvestigation:
    if call_budget is not None:
        call_budget.require_execution(execution_id)
        for configured in (client, figure_client):
            if configured is not None:
                call_budget.require_client(configured)
    from tsdaf.lanes.literature.investigation_kinds import new_investigation_kind
    spec = new_investigation_kind(kind,machine_profile(machine) if machine is not None else None)
    if provider_is_live and not live:
        raise ValueError("live investigation requires explicit live authorization")
    # A task reads the documents its kind names, and a document it does not read is none
    # of its input: not in its catalog, its searches or its reads, and not in this
    # stage's identity, so a supplement arriving does not rerun the main text's narrative.
    sources = sources.of_phases(spec.phases)
    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)
    main = sources.main.package
    assert main is not None
    handoffs = verify_router_handoff_bundle(
        package=main,
        router_execution_id=router_execution_id,
        terminal_manifest_ref=runner.finish_execution(
            router_execution_id, ExecutionStatus.SUCCEEDED
        ),
        object_store=store,
        runner=runner,
    )
    bootstrap = handoffs.bootstrap
    routing = handoffs.routing
    allowed_visuals = tuple(
        d.visual_id
        for d in routing.visual_decisions
        if d.final_decision not in {VisualDecision.ANALYTICAL, VisualDecision.UNCERTAIN}
    )
    supplement = spec.input_mode == "FIRST_LAYER"
    if supplement and block_ids is not None:
        raise ValueError("MAIN supplementary information is not scoped to reaction blocks")
    if supplement and (
        baseline_machine_ref is not None or machine is not None or observations or context
    ):
        raise ValueError("MAIN supplementary information consumes only sealed first-layer input")
    first_layer_results = []
    first_layer_source_selections = {}
    first_layer_profiles = {}
    for ref in first_layer_refs:
        group = LiteratureRouteGroupReceipt.model_validate_json(store.read_bytes(ref))
        terminal = runner.finish_execution(group.literature_execution_id, ExecutionStatus.SUCCEEDED)
        closure = runner.verified_execution_artifact_closure(
            group.literature_execution_id, terminal
        )
        if ref not in closure or group.gate_proposal_ref not in closure:
            raise ValueError("first-layer supplement input lacks a sealed producer")
        result = read_fusion_result(store.read_bytes(group.gate_proposal_ref))
        if result.package_sha256 != main.canonical_content_sha256:
            raise ValueError("first-layer supplement input differs from MAIN")
        from tsdaf.workflow.fusion_recorded_profile import fusion_call_profile

        def read_first_layer(ref):
            if ref not in closure:
                raise ValueError("first-layer contract lacks its sealed producer")
            return json.loads(store.read_bytes(ref))

        audit = read_first_layer(group.gate_provenance_ref)
        if ArtifactRef.model_validate(audit["result_ref"]) != group.gate_proposal_ref:
            raise ValueError("first-layer contract differs from its result")
        attempts = [read_first_layer(ArtifactRef.model_validate(attempt)) for attempt in audit["attempt_refs"]]
        profiles = {fusion_call_profile(attempt, read_first_layer) for attempt in attempts
                    if "resources_ref" in attempt or "interaction_ref" in attempt}
        if len(profiles) > 1:
            raise ValueError("first-layer producer has conflicting contract profiles")
        first_layer_profiles[result.work_unit_id] = next(iter(profiles), MachineProfile.V1)
        from tsdaf.workflow.fact_source_selections import fusion_fact_selections
        first_layer_source_selections[result.work_unit_id] = fusion_fact_selections(
            result, packages=(main,), store=store, accepted_refs=closure, runner=runner)
        first_layer_results.append(result.model_dump(mode="json"))
    for doc in sources.active():
        if doc.package is None:
            continue
        if doc.package.canonical_content_sha256 != doc.registration.package_sha256:
            raise ValueError("registered document identity differs from its verified package")
        # MAIN is the paper; an SI belongs to it by the one pairing rule (E79), which
        # lets a DOI-paired SI keep its own intake id.
        if doc.registration.source_phase == "MAIN":
            if doc.package.manifest.paper_id != sources.paper_id:
                raise ValueError("registered document identity differs from its verified package")
        elif pairing_failure(main.manifest, doc.package.manifest) is not None:
            raise ValueError("registered SI does not belong to the paper")
    context = (
        {
            "routing": routing.model_dump(mode="json"),
            "first_layer_sha256": bootstrap.bootstrap_sha256,
            "route_results": first_layer_results,
        }
        if supplement
        else context or {}
    )
    paragraph_refs: list[ArtifactRef] = []
    paragraph_evidence_refs: list[ArtifactRef] = []
    if spec.accepts_paragraphs:
        paragraphs: list[dict[str, Any]] = []
        target_bindings = []
        selections: dict[str, Any] = {}
        paragraph_observations: dict[str, Any] = {}
        # One call for every paragraph task: their closures share the paper's upstream,
        # which one call per task reverified once per task.
        closures = runner.verified_execution_artifact_closures(
            [(s.receipt.execution_id, s.terminal_manifest_ref) for s in paragraph_receipts]
        )
        for sealed, closure in zip(paragraph_receipts, closures, strict=True):
            receipt = sealed.receipt
            if sealed.receipt_ref not in closure:
                raise ValueError("paragraph input lacks a sealed producer")
            original = InvestigationReceipt.model_validate_json(
                store.read_bytes(sealed.receipt_ref)
            )
            if original != receipt or receipt.paper_id != sources.paper_id:
                raise ValueError("paragraph input differs from its sealed paper binding")
            paragraph_refs.append(sealed.receipt_ref)
            raw = receipt.raw_model()
            if raw is None:
                continue
            if not isinstance(raw, ParagraphSet):
                raise ValueError("paper reading consumes paragraph sets")
            target_map, target_proof = paragraph_target_binding(
                receipt, machine=machine, context=context, baseline_ref=baseline_machine_ref,
                store=store, closure=closure,
            )
            if target_proof is not None:
                target_bindings.append({"receipt": sealed.receipt_ref.model_dump(mode="json"), **target_proof})
                for original_ref in (receipt.input_ref, receipt.baseline_machine_ref):
                    if original_ref not in paragraph_evidence_refs:
                        paragraph_evidence_refs.append(original_ref)
            original_observations = {str(o["observation_id"]): o for o in receipt.observations}
            by_sha256 = {ref.sha256: ref for ref in closure}
            for paragraph in raw.paragraphs:
                for sid in paragraph.selection_ids:
                    binding = receipt.selections[sid]
                    evidence = binding["evidence_ref"]
                    assert isinstance(evidence, dict)
                    observation_ref = by_sha256.get(evidence["source_observation_sha256"])
                    if observation_ref is None or json.loads(store.read_bytes(observation_ref)) != (
                        original_observations.get(evidence["observation_id"])
                    ):
                        raise ValueError("paragraph observation lacks its original sealed binding")
                    # A selection id is minted from what was read -- revision, block,
                    # range -- never from who read it, so two tasks that located one
                    # passage hold one id (E108 ①). The passage is delivered once,
                    # bound to the first sealed observation of it; the ledger below,
                    # not the name, says which tasks located it.
                    first = selections.setdefault(sid, binding)
                    if any(first[k] != binding[k] for k in ("source_identity", "exact_text")):
                        raise ValueError(f"selection {sid} names two different passages")
                    if first is binding and observation_ref not in paragraph_evidence_refs:
                        paragraph_evidence_refs.append(observation_ref)
                paragraphs.append(
                    {**paragraph.model_dump(mode="json"), "task": receipt.execution_id,
                     "about_ids": [target_map.get(target, target) for target in paragraph.about_ids]}
                )
            paragraph_observations.update(
                (str(o["observation_id"]), o) for o in receipt.observations
            )
        used = {str(value["evidence_ref"]["observation_id"]) for value in selections.values()}
        context = {
            # The reading owns its paragraph inputs; these row sources instead belong
            # to the dispatched machine and are checked against that exact baseline.
            "material_sources": context.get("material_sources") or {},
            "groups": context.get("groups") or [],
            "block_provenance": context.get("block_provenance") or {},
            "existing_observations": list(observations),
            "observation_provenance": context.get("observation_provenance") or {},
            "paragraphs": paragraphs,
            "paragraph_selections": selections,
            "paragraph_observations": [paragraph_observations[oid] for oid in sorted(used)],
            **({"paragraph_target_bindings": target_bindings} if target_bindings else {}),
        }
    task: TaskScope = "BLOCK" if block_ids is not None else "PAPER" if shares_the_round else "WHOLE"
    profile = machine_profile(machine) if machine is not None else None
    if spec.records_science and (profile is None or not profile.unified_expression):
        raise ValueError("experimental recording requires the candidate unified expression profile")
    if kind == "EXPERIMENTAL_READING" and profile is not None and profile.unified_expression:
        context = with_independent_scope(
            context, paper_id=sources.paper_id,
            package_sha256=main.canonical_content_sha256, execution_id=execution_id,
        )
    inherited_selections = None
    if kind == "EXPERIMENTAL_READING" and profile is not None and profile.unified_expression:
        from tsdaf.workflow.fact_source_selections import inherited_fact_selections

        inherited_selections = inherited_fact_selections(
            groups=context.get("groups", ()), machine=machine, sources=sources,
            store=store, runner=runner, restore_domain=restore_investigation_domain,
        )
    from tsdaf.lanes.literature.middle_layer import EXPLICIT_CURRENT_UPDATE
    record_handoff = None
    mapping_artifacts = ()
    if record_receipt is not None:
        if (not spec.typed_reading and kind != 'EXPERIMENTAL_MAPPING') or profile is None or not profile.unified_expression:
            raise ValueError('sealed records are consumed only by the candidate final mapping task')
        from tsdaf.workflow.experimental_record_handoff import verify_record_handoff
        record_handoff = verify_record_handoff(record_receipt, store=store, runner=runner,
            sources=sources, baseline_ref=baseline_machine_ref)
        context = {**context, **record_handoff.context()}
    if kind == 'EXPERIMENTAL_MAPPING':
        if record_handoff is None or profile is None or not profile.faithful_mapping:
            raise ValueError('final document mapping requires a verified recording and faithful baseline')
        from tsdaf.workflow.literature_paper_facts import public_observations
        from tsdaf.workflow.public_scientific_records import seal_public_records
        cohort = public_observations((*observations, *record_handoff.receipt.observations),
                                     context.get('observation_provenance'))
        published_records = seal_public_records(record_handoff, producer_receipt_ref=record_receipt.receipt_ref,
                                                store=store, observation_set=cohort)
        from tsdaf.workflow.final_mapping_sources import mapping_source_choices
        mapping_artifacts = (published_records.artifact,)
        context = {**context, 'mapping_records':published_records.value,
                   'mapping_record_ref':published_records.public_ref,
                   'mapping_sources':mapping_source_choices(machine,record_handoff,sources)}
        if block_ids is not None:
            if ('mapping_assignment' in context
                    and context['mapping_assignment'] != list(block_ids)):
                raise ValueError('mapping assignment differs from dispatched block_ids')
            context['mapping_assignment'] = list(block_ids)
        elif context.get('mapping_assignment') not in (None, []):
            raise ValueError('paper mapping assignment cannot name a reaction block')
        from tsdaf.workflow.final_science_mapping import mapping_assignment
        assigned = mapping_assignment(machine, context)
        task = 'WHOLE' if assigned is None else 'BLOCK' if assigned else 'PAPER'
    # The old update protocol is only for historical typed readers.
    current_updates = record_handoff is not None and spec.typed_reading
    prompt, tools = investigation_resources(
        kind, task=task,
        expression=profile.expression if profile is not None else False,
        open_expression=profile.open_expression if profile is not None else False,
        unified_expression=profile.unified_expression if profile is not None else False,
        current_updates=current_updates,
    )
    input_payload = {
        "kind": kind,
        **({"record_source_version": 1} if spec.records_science else {}),
        **({"record_receipt_ref": record_receipt.receipt_ref.model_dump(mode="json"),
            "record_terminal_ref": record_receipt.terminal_manifest_ref.model_dump(mode="json")}
           if record_handoff is not None else {}),
        **({"finding_update_mode": EXPLICIT_CURRENT_UPDATE} if current_updates else {}),
        "reading_repair_mode": (
            SPARSE if kind == "EXPERIMENTAL_READING" and profile is not None
            and profile.unified_expression else SNAPSHOT
        ),
        "first_layer_sha256": bootstrap.bootstrap_sha256,
        "source_identities": [d.identity() for d in sources.active()],
        "context": context,
        "first_layer_refs": [r.model_dump(mode="json") for r in first_layer_refs],
        "paragraph_receipt_refs": [r.model_dump(mode="json") for r in paragraph_refs],
        "paragraph_evidence_refs": [r.model_dump(mode="json") for r in paragraph_evidence_refs],
        "baseline_machine_sha256": baseline_machine_ref.sha256 if baseline_machine_ref else None,
        # The steps this task is responsible for. Every block task of one round shares
        # every other input; without its own scope here the stage's content key is the
        # same for all of them and the second is served the first one's sealed answer.
        "block_ids": list(block_ids) if block_ids is not None else None,
        # What of each document the task may read, and the reading it was handed, are
        # inputs too: a document's identity is its registration, which says nothing of
        # either, so a task widened to its steps' own paragraphs (E142) or handed a
        # different reading was served the narrower task's sealed answer.
        "readable": {
            d.registration.document_revision_id: content_hash(list(d.readable_ids))
            for d in sources.active()
        },
        "units": {
            d.registration.document_revision_id: (
                None if d.units is None else content_hash(d.units.model_dump(mode="json"))
            )
            for d in sources.active()
        },
    }
    input_ref = store.put_bytes(
        canonical_json_bytes(input_payload),
        media_type="application/vnd.tsdaf.investigation-input+json",
    )
    stage_name = "literature-" + kind.lower()

    def invoke(attempt_id: str, content_key: str) -> StageResult:
        started = datetime.now(UTC)

        def bind(*refs: ArtifactRef) -> None:
            runner.bind_attempt_artifacts(
                execution_id, attempt_id, refs, stage_name=stage_name, content_key=content_key
            )

        def emit_event(event_type: str, data: dict[str, Any]) -> None:
            runner.append_event(
                execution_id,
                event_type,
                data,
                attempt_id=attempt_id,
                stage_name=stage_name,
                content_key=content_key,
            )

        if client is None:
            receipt = InvestigationReceipt(
                kind=kind,
                paper_id=sources.paper_id,
                execution_id=execution_id,
                input_ref=input_ref,
                baseline_machine_ref=baseline_machine_ref,
                failure="INVESTIGATION_PROVIDER_NOT_CONFIGURED",
                source_identities=tuple(d.identity() for d in sources.active()),
            )
        else:
            try:
                receipt = investigate_sources(
                    kind=kind,
                    sources=sources,
                    execution_id=execution_id,
                    input_ref=input_ref,
                    context=context,
                    store=store,
                    client=client,
                    figure_client=figure_client,
                    call_budget=call_budget,
                    provider_binding=provider_binding,
                    bind=bind,
                    emit_event=emit_event,
                    baseline_machine_ref=baseline_machine_ref,
                    machine=machine,
                    observations=observations,
                    allowed_main_visual_ids=allowed_visuals,
                    recovery_artifacts=runner.verified_stage_artifacts(
                        execution_id, stage_name=stage_name, content_key=content_key
                    ),
                    block_ids=block_ids,
                    shares_the_round=shares_the_round,
                    inherited_selections=inherited_selections,
                    first_layer_source_selections=first_layer_source_selections,
                    first_layer_profiles=first_layer_profiles,
                )
            except TaskContextTooLarge as error:
                # Caught before the general rule below, and named: the Host built this
                # input, so it is INPUT, and the dispatcher retries nothing but PROVIDER.
                receipt = error.receipt or InvestigationReceipt(
                    kind=kind,
                    paper_id=sources.paper_id,
                    execution_id=execution_id,
                    input_ref=input_ref,
                    baseline_machine_ref=baseline_machine_ref,
                    failure=f"{FailureCode.CONTEXT_TOO_LARGE.value}:{error}",
                    source_identities=tuple(d.identity() for d in sources.active()),
                )
            except (ValueError, TypeError, OSError) as error:
                receipt = InvestigationReceipt(
                    kind=kind,
                    paper_id=sources.paper_id,
                    execution_id=execution_id,
                    input_ref=input_ref,
                    baseline_machine_ref=baseline_machine_ref,
                    failure="INVESTIGATION_INPUT_OR_PROVIDER_FAILURE:" + type(error).__name__,
                    source_identities=tuple(d.identity() for d in sources.active()),
                )
        receipt_ref = store.put_bytes(
            canonical_json_bytes(receipt.model_dump(mode="json")), media_type=INVESTIGATION_MEDIA
        )
        status, failed = stage_status(receipt.failure)
        if failed is None:
            output: ArtifactRef | None = receipt_ref
        else:
            # The lane failed. The receipt is still sealed and bound to this attempt
            # (the diagnostics are evidence), but the stage is a failure: not a
            # success the chain reports rc 0 over, and never a cache hit for a rerun.
            bind(receipt_ref)
            output = None
        return StageResult(
            schema_version="tsdaf.stage-result.v1",
            execution_id=execution_id,
            attempt_id=attempt_id,
            stage_name=stage_name,
            stage_version=INVESTIGATION_VERSION,
            input_content_key=content_key,
            status=status,
            output_artifact=output,
            failure=failed,
            cached_from_result=None,
            started_at=started,
            finished_at=datetime.now(UTC),
        )

    stage = runner.run_stage(
        execution_id=execution_id,
        stage_name=stage_name,
        stage_version=INVESTIGATION_VERSION,
        input_artifacts=(
            input_ref,
            bootstrap.routing_result_ref,
            *first_layer_refs,
            *paragraph_refs,
            *paragraph_evidence_refs,
            *(record_handoff.closure if record_handoff is not None else ()),
            *mapping_artifacts,
            *((baseline_machine_ref,) if baseline_machine_ref else ()),
        ),
        settings={
            **({"argument_codec":compilation_transport_descriptor()}
               if kind == "EXPERIMENTAL_MAPPING" else {}),
            "resources_sha256": content_hash(
                [
                    prompt,
                    tools,
                    NAMING_VERSION,
                    INVESTIGATION_VERSION, READ_DELIVERY_VERSION,
                    client.context_policy if client else None,
                ]
            ),
            "configured": client is not None,
            # Whether figures could be read at all is part of what this stage is. A run
            # without a figure reader answers every image read with a refusal, and its
            # sealed answer must never be served to a rerun that has one (E63).
            "figures_readable": figure_client is not None,
            # And so is which reader: a stage sealed while V3 refused every reading of a
            # supplement figure must not be served to a rerun whose reader accepts them.
            "figure_reader": load_lane_resources("V3").resource_manifest_sha256
            if figure_client is not None
            else None,
        },
        # The profile (and so the effort) the lane runs at is part of the stage's
        # identity, as it is for the route stage (deep audit D-5 / U-4).
        model_profile=AUTHORIZED_MODEL_PROFILE,
        invoke=invoke,
    )
    if isinstance(stage, TypedFailure):
        raise ValueError("investigation could not seal its execution")
    if stage.output_artifact is None:
        assert stage.failure is not None
        if stage.status is not ExecutionStatus.FAILED_RETRYABLE:
            runner.finish_execution(execution_id, ExecutionStatus.FAILED_TERMINAL)
        receipts = [
            ref
            for ref in runner.verified_stage_artifacts(
                execution_id, stage_name=stage_name, content_key=stage.input_content_key
            )
            if ref.media_type == INVESTIGATION_MEDIA
        ]
        if not receipts:
            raise ValueError("investigation failed without a sealed receipt")
        raise InvestigationFailed(
            stage.failure,
            receipts[-1],
            stage_name=stage_name,
            content_key=stage.input_content_key,
        )
    receipt = InvestigationReceipt.model_validate_json(store.read_bytes(stage.output_artifact))
    runner.finish_execution(execution_id, ExecutionStatus.SUCCEEDED)
    # Cache hits keep the original observation producer, not this caller's execution.
    producer_manifest = runner.finish_execution(receipt.execution_id, ExecutionStatus.SUCCEEDED)
    return SealedInvestigation(receipt, stage.output_artifact, producer_manifest)
