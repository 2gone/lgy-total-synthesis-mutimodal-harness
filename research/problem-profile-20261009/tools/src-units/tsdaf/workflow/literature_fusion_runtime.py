"""One ongoing fusion execution: the fusion task of one route group."""

from __future__ import annotations

import copy
import functools
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tsdaf.contracts.tool_read import READ_DELIVERY_VERSION
from tsdaf.contracts.execution import ProviderInteractionError
from tsdaf.contracts.literature.schema import MachineProfile
from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.contracts.tool_read import (
    DirectorySelection,
    QuerySelection,
    Selection,
    TextSelection,
    WholeSelection,
)
from tsdaf.lanes.document.page_furniture import package_continuity
from tsdaf.lanes.literature.completion_guard import finish_left
from tsdaf.lanes.literature.evidence import ObservationOriginator
from tsdaf.lanes.literature.evidence_binding import (
    FUSION_SELECTION_STAGE,
    SealedWorkUnitEvidenceRegistry,
    WorkUnitEvidenceRegistry,
    bind_fusion_evidence,
    source_key,
    source_location,
    text_source_quote,
)
from tsdaf.lanes.literature.gate_models import (
    CURRENT_LOCATION,
    FUSION_RECEIPT_VERSION,
    FUSION_RESULT_MEDIA,
    FactOrigin,
    FinishFusion,
    FusionProposal,
    FusionResult,
    ReadFusion,
    fusion_tools,
    read_fusion_result,
)
from tsdaf.lanes.literature.host_ports import PrivateArtifactStore
from tsdaf.lanes.literature.lane_models import TextFragmentSelection, VisualRegionSelection
from tsdaf.lanes.literature.lane_proposals import (
    SealedInitialRouteLaneProposals,
    verify_sealed_initial_route_lane_proposals,
)
from tsdaf.lanes.literature.local_visual_read import (
    materialize_local_visual_read,
    read_cached_local_visual_read,
)
from tsdaf.lanes.literature.model_names import NAMING_VERSION, Names
from tsdaf.lanes.literature.profiles import fusion_profile, fusion_prompt_text
from tsdaf.lanes.literature.progress_views import fusion_view
from tsdaf.lanes.literature.provider_port import LaneInteractionClient, ProviderBinding
from tsdaf.lanes.literature.reader_identity import ReaderReuseVerifier
from tsdaf.lanes.literature.selection_resolution import (
    ResolvedPixelBox,
    block_sentence_spans,
)
from tsdaf.lanes.literature.source_inventory import (
    LiteratureSourceInventory,
    resolve_fusion_read_authority,
)
from tsdaf.lanes.literature.source_inventory_models import EXPERIMENTAL_PART_EXCLUSION
from tsdaf.lanes.literature.source_manifest import UNKNOWN_SOURCE_STATUS, PaperSourceStatus
from tsdaf.lanes.literature.source_reads import SourceReader
from tsdaf.lanes.literature.source_scope import VerifiedWorkUnitInput
from tsdaf.workflow.action_progress import ACTION_PROGRESS_ORIGIN, bounded_action_view
from tsdaf.workflow.content_window import DELIVERED_KEY, SourceDelivery
from tsdaf.workflow.context_projection import project_context
from tsdaf.workflow.fusion_names import FusionNames, fusion_names, fusion_reads
from tsdaf.workflow.literature_fusion_actions import (
    apply_sparse_submission,
    binder_spec,
    inherited_references,
    reading_candidates,
    reading_navigation_page,
    reading_text_parts,
    remember_location,
    result_view,
    submission_targets,
)
from tsdaf.workflow.literature_fusion_progress import (
    FUSION_PROGRESS_MEDIA,
    FusionCheckpoint,
    accept_record,
    read_exact,
    restore_fusion_progress,
)
from tsdaf.workflow.naming_evidence import (
    DomainNaming,
    fusion_domain,
    names_from_component,
    restore_fusion_domain,
)
from tsdaf.workflow.read_dispatch import (
    RecordTarget,
    SourceTarget,
    fragment_of,
    project_record_value,
    resolve_read_location,
)
from tsdaf.workflow.resource_reads import (
    READ_BYTES,
    ReadFailure,
    ReadPlan,
    ReadValue,
    pointer_value,
    resource_binding,
    resource_receipt,
)
from tsdaf.workflow.source_navigation import authorized_visual_location
from tsdaf.workflow.state_resources import (
    STATE_LOCATION_HISTORY,
)
from tsdaf.workflow.text_search import SEARCH_LOCATION, text_search_value
from tsdaf.workflow.tool_execution import (
    CallContext,
    FinishIntent,
    ToolConsumer,
    WriteOutcome,
    call_receipt,
    diagnostic_value,
    execute_tool_turns,
    lane_interaction_event,
    seal_artifact,
)
from tsdaf.workflow.tool_turns import ToolTurnState
from tsdaf.workflow.unit_decisions import decision_for_delivery
from tsdaf.workflow.write_delivery import WriteDelivery, write_action_id, write_result
from tsdaf.workflow.write_registry import item_key

FUSION_AUDIT_MEDIA = "application/vnd.tsdaf.fusion-audit+json"


@dataclass(frozen=True, slots=True)
class SealedFusionOutcome:
    proposal: FusionResult
    proposal_ref: ArtifactRef
    provenance_ref: ArtifactRef
    decision: str


def _provider_history(
    history: list[dict[str, Any]], store: PrivateArtifactStore
) -> list[dict[str, Any]]:
    return project_context(history, progress={}, read_bytes=store.read_bytes)


def experimental_part_note(
    package: CanonicalPaperPackage,
    source_inventory: LiteratureSourceInventory | None,
    readable_block_ids: Sequence[str],
    *,
    status: PaperSourceStatus = UNKNOWN_SOURCE_STATUS,
) -> str:
    """The sentence a route group is told about where this paper's experimental detail
    is, generated from what the Host knows (ledger E76 (5), E83), not guessed by the
    model: the document has an experimental part that is withheld from this task; it
    has none and a supplement is registered; it has none and no supplement is
    registered. Without the first, a group barred from the experimental section
    searched for it twenty turns and stopped every segment with WAIT_FOR_SI on a paper
    that has no supplement (ledger E51, JOC r4); without the third, it still can.

    It says where the detail is and nothing about what to write: what the fusion
    writes -- every step, the operations and named materials the main text states -- is
    the prompt's rule, the same on every paper. The first branch used to tell the group
    to hand each step to a window and write only its reaction; under it the group wrote
    no material on 11 of 12 runs (windows-muscle skeleton-rules, 2026-09-23).
    """
    if source_inventory is None:
        return ""
    readable = set(readable_block_ids)
    withheld = [
        entry.source_id
        for entry in source_inventory.entries
        if entry.exclusion_reason == EXPERIMENTAL_PART_EXCLUSION
        and entry.source_id not in readable
        and entry.readable
    ]
    if withheld:
        first = next((b for b in package.blocks if b.block_id == withheld[0]), None)
        opening = " ".join(first.raw_text.split())[:80] if first is not None else withheld[0]
        return (
            f" This document has an upstream experimental section ({len(withheld)} blocks, "
            f"beginning '{opening}') outside this task's read scope. The detail lane receives "
            "that scope after skeleton publication."
        )
    has_part = status.has_experimental_part
    scope = (
        "MAIN experimental scope is not established" if has_part is None else
        "upstream classified a MAIN experimental section" if has_part else
        "upstream classified MAIN without an experimental section"
    )
    return f" Source availability: {scope}; SI registered={status.si_registered}."


def withheld_experimental_part_note(
    package: CanonicalPaperPackage,
    source_inventory: LiteratureSourceInventory | None,
    readable_block_ids: Sequence[str],
) -> str:
    """The first of the three sentences alone (kept for its callers)."""
    return experimental_part_note(
        package,
        source_inventory,
        readable_block_ids,
        status=PaperSourceStatus(si_registered=True, has_experimental_part=True),
    )


def fusion_source_evidence(
    row: dict[str, Any],
    parts: Sequence[dict[str, Any]],
    *,
    locations: Mapping[str, Mapping[str, Any]],
    package: CanonicalPaperPackage,
) -> tuple[SourceDelivery, ...]:
    """Actual source receipts only; initial reader interpretations are not sources."""
    if row.get("outcome") != "READ" or row.get("origin") != "SOURCE":
        return ()
    found = []
    blocks = {block.block_id: block for block in package.blocks}
    for selected in row.get("selected_sources", ()):
        location = selected.get("location")
        binding = locations.get(location) if isinstance(location, str) else None
        if binding is None or binding.get("kind") != "STANDARD_SOURCE":
            continue
        expected = binding["selection"]
        if {k: v for k, v in selected.items() if k != "location"} != {
            k: v for k, v in expected.items() if k != "location"
        }:
            continue
        source = selected["canonical_source_id"]
        numbers: frozenset[int] = frozenset()
        if selected["modality"] == "TEXT":
            block = blocks.get(source)
            if block is None:
                continue
            canonical = {
                i: span.text for i, span in enumerate(block_sentence_spans(block.raw_text))
            }
            sentences = selected.get("sentences", [])
            if not sentences or any(
                type(sentence.get("index")) is not int
                or canonical.get(sentence["index"]) != sentence.get("text")
                for sentence in sentences
            ):
                continue
            numbers = frozenset(sentence["index"] for sentence in sentences)
        else:
            image = (binding.get("display") or {}).get("image_ref", {}).get("sha256")
            if not isinstance(image, str) or not any(
                part.get("artifact_ref", {}).get("sha256") == image for part in parts
            ):
                continue
        found.append(
            SourceDelivery("source:" + source, location, selected["source_id"], numbers, True)
        )
    return tuple(found)


def run_fusion(
    *,
    work_unit: VerifiedWorkUnitInput,
    source_reader: SourceReader,
    proposals: SealedInitialRouteLaneProposals,
    evidence: SealedWorkUnitEvidenceRegistry | None,
    execution_id: str,
    provider_binding: ProviderBinding,
    interaction: LaneInteractionClient,
    artifact_store: PrivateArtifactStore,
    source_inventory: LiteratureSourceInventory | None = None,
    bind_artifacts: Callable[..., None],
    recovery_artifacts: tuple[ArtifactRef, ...] = (),
    emit_event: Callable[[str, dict[str, Any]], None] | None = None,
    machine_profile: MachineProfile = MachineProfile.V1,
    source_status: PaperSourceStatus = UNKNOWN_SOURCE_STATUS,
    reuse_verifier: ReaderReuseVerifier | None = None,
) -> SealedFusionOutcome:
    machine_profile.require_production()
    package = source_reader.package
    work_unit = VerifiedWorkUnitInput.model_validate(work_unit.model_dump(mode="python"))
    verify_sealed_initial_route_lane_proposals(
        proposals=proposals, artifact_store=artifact_store, reuse_verifier=reuse_verifier
    )
    if package.canonical_content_sha256 != work_unit.package_sha256:
        raise ValueError("fusion package does not match its authority")
    if evidence is not None:
        if evidence.registry.work_unit_id != work_unit.work_unit_id:
            raise ValueError("fusion evidence belongs to another unit")
        if read_exact(artifact_store, evidence.registry_ref) != canonical_json_bytes(
            evidence.registry.model_dump(mode="json")
        ):
            raise ValueError("fusion evidence registry differs from sealed bytes")
    base = FusionResult(
        work_unit_id=work_unit.work_unit_id,
        package_sha256=work_unit.package_sha256,
        facts=(),
        sources={},
        evidence_registry_refs=(),
        note="Initial observations.",
    )
    registry_refs = tuple(
        dict.fromkeys(
            (*base.evidence_registry_refs, *((evidence.registry_ref,) if evidence else ()))
        )
    )
    sources = dict(base.sources)
    source_index: list[dict[str, Any]] = []
    readings: list[dict[str, object]] = []
    source_errors: list[dict[str, object]] = []
    for registry_ref in registry_refs:
        registry = (
            evidence.registry
            if evidence is not None and registry_ref == evidence.registry_ref
            else WorkUnitEvidenceRegistry.model_validate_json(
                read_exact(artifact_store, registry_ref)
            )
        )
        if (
            registry.package_sha256 != work_unit.package_sha256
            or registry.document_revision_id != work_unit.document_revision_id
        ):
            raise ValueError("inherited reading evidence belongs to another document")
        readings.extend(registry.readings)
        source_errors.extend(registry.source_errors)
        for entry in registry.entries:
            sid = source_key(registry.work_unit_id, entry.modality, entry.selection_id)
            sources[sid] = (entry.evidence_ref.model_dump(),)
            source_index.append(source_location(entry, sid, package))
    base = base.model_copy(
        update={
            "work_unit_id": work_unit.work_unit_id,
            "sources": sources,
            "evidence_registry_refs": registry_refs,
            "source_errors": tuple(source_errors),
        }
    )
    client = provider_binding.require_client(interaction, label="fusion")
    loop = ToolTurnState()
    audit_refs: list[ArtifactRef] = []

    def seal(
        payload: object, media: str = FUSION_AUDIT_MEDIA, *, accept: bool = True
    ) -> ArtifactRef:
        return seal_artifact(artifact_store, bind_artifacts, payload, media, accept=accept)

    prompt = fusion_prompt_text(unified_expression=machine_profile.unified_expression)
    tools_bytes = canonical_json_bytes(
        fusion_tools(unified_expression=machine_profile.unified_expression)
    )
    prompt_ref = seal(prompt.encode(), "text/vnd.tsdaf.fusion-prompt+markdown")
    schema_ref = seal(tools_bytes, "application/vnd.tsdaf.fusion-schema+json")
    resources_ref = seal(
        {
            "profile": fusion_profile(unified_expression=machine_profile.unified_expression),
            "machine_profile": machine_profile.value,
            # What the model is shown of the host's ids is part of what it is asked (batch
            # 2A, 2d): a task under other names is a new task, never a resumed one.
            "naming": NAMING_VERSION, "read_delivery": READ_DELIVERY_VERSION,
            "prompt_ref": prompt_ref.model_dump(mode="json"),
            "schema_ref": schema_ref.model_dump(mode="json"),
        },
        "application/vnd.tsdaf.fusion-resources+octets",
    )
    read_authority = resolve_fusion_read_authority(
        work_unit=work_unit,
        package=package,
        source_inventory=source_inventory,
    )
    caption_figures: dict[str, list[str]] = {}
    for visual in work_unit.visual_sources:
        if visual.visual_id in read_authority.visual_ids:
            for block in visual.caption_block_ids:
                caption_figures.setdefault(block, []).append("source:" + visual.visual_id)
    source_context = (
        {
            e.source_id: e.model_dump(
                mode="json",
                exclude={"source_id", "source_kind", "shard_id", "readable", "sentence_count"},
            )
            for e in source_inventory.entries
        }
        if source_inventory
        else {}
    )
    task_catalog = [
        {"location": CURRENT_LOCATION, "kind": "CURRENT_RESULT"},
        {"location": "history", "kind": "DIRECTORY"},
        *[
            {
                "location": "source:" + block.block_id,
                "kind": "STANDARD_SOURCE",
                "modality": "TEXT",
                "sentence_count": len(block_sentence_spans(block.raw_text)),
                "section_path": list(block.section_path),
                "preview": block.raw_text[:240],
                **source_context.get(block.block_id, {}),
            }
            for block in package.blocks
            if block.block_id in read_authority.text_block_ids
        ],
        *[
            {
                "location": "source:" + visual.visual_id,
                "kind": "STANDARD_SOURCE",
                "modality": "VISUAL",
                "caption": visual.native_caption,
                **source_context.get(visual.visual_id, {}),
            }
            for visual in package.visual_assets
            if visual.visual_id in read_authority.visual_ids
        ],
    ]
    # The version the model's next write is compared with when it names no view, or
    # names CURRENT_LOCATION (`compared_version`): the result as the latest progress
    # listed it, or as the model's own last write left it, whichever came later -- never
    # round 0 (audit L4-04, step 6 (3)).
    last_shown = base
    loop.iteration = 0
    # The current result is sealed when a record refers to it, not each time it changes:
    # a source read changed it once per source and resealed the whole result each time,
    # though only the version a call's audit or a checkpoint names is ever referred to
    # (batch 4 redundancy, problem 12). One function hands the reference out, and the
    # cache is keyed by the result object itself, so a restored result keeps the exact
    # reference its checkpoint named rather than being resealed from re-read bytes.
    sealed: tuple[FusionResult, ArtifactRef] | None = None

    def current_ref() -> ArtifactRef:
        nonlocal sealed
        if sealed is None or sealed[0] is not base:
            sealed = (base, seal(base.model_dump(mode="json"), FUSION_RESULT_MEDIA))
        return sealed[1]

    locations: dict[str, dict[str, Any]] = {}

    def register_version() -> None:
        # History lists the versions the result has had. A write registers the version it
        # made here; they used to be registered only as a side effect of rendering the
        # result to the model, which no longer happens.
        remember_location(
            locations, kind="CURRENT_RESULT", result_ref=current_ref().model_dump(mode="json")
        )

    loop.next_call = 0
    loop.finished = False
    checkpoint_ref: ArtifactRef | None = None
    restored = restore_fusion_progress(
        recovery_artifacts,
        execution_id=execution_id,
        work_unit_id=work_unit.work_unit_id,
        resources_sha256=resources_ref.sha256,
        store=artifact_store,
    )
    if restored is not None:
        progress, checkpoint_ref = restored.checkpoint, restored.ref
        initial_result_ref = progress.initial_result_ref
        base = read_fusion_result(read_exact(artifact_store, progress.result_ref))
        sealed = (base, progress.result_ref)
        last_shown = base
        if (
            base.work_unit_id != work_unit.work_unit_id
            or base.package_sha256 != work_unit.package_sha256
        ):
            raise ValueError("restored fusion result belongs to another task")
        loop.history, locations = restored.history, restored.locations
        loop.next_call, loop.iteration = progress.next_call, progress.iteration
        audit_refs = restored.audit_refs
        loop.outbox = restored.image_parts
        if progress.pending_ref is not None:
            loop.pending = {
                **json.loads(read_exact(artifact_store, progress.pending_ref)),
                "interaction_ref": progress.pending_ref.model_dump(mode="json"),
            }
        loop.finished = progress.finished
    else:
        initial_result_ref = current_ref()
        register_version()

    delivery = WriteDelivery(loop.history)
    history_size, audit_size, image_size = len(loop.history), len(audit_refs), len(loop.outbox)
    # What the checkpoint chain already holds: everything restored, or nothing yet -- the
    # starting result's history entry, registered above, goes into the first checkpoint.
    saved_locations = set(restored.locations) if restored is not None else set()

    def save_progress() -> None:
        nonlocal checkpoint_ref, history_size, audit_size, image_size
        image_size = min(image_size, len(loop.outbox))
        checkpoint = FusionCheckpoint(
            execution_id=execution_id,
            work_unit_id=work_unit.work_unit_id,
            resources_sha256=resources_ref.sha256,
            previous_ref=checkpoint_ref,
            initial_result_ref=initial_result_ref,
            result_ref=current_ref(),
            history_added=tuple(loop.history[history_size:]),
            locations_added={
                key: value for key, value in locations.items() if key not in saved_locations
            },
            pending_ref=ArtifactRef.model_validate(loop.pending["interaction_ref"])
            if loop.pending
            else None,
            image_parts_added=tuple(loop.outbox[image_size:]),
            next_call=loop.next_call,
            iteration=loop.iteration,
            audit_refs_added=tuple(audit_refs[audit_size:]),
            # No question is dispatched on any more (2026-09-23 ruling). The field stays,
            # empty, so a checkpoint written before then still reads back.
            dispatch_questions_added=(),
            finished=loop.finished,
        )
        checkpoint_ref = accept_record(
            checkpoint,
            media_type=FUSION_PROGRESS_MEDIA,
            store=artifact_store,
            bind=bind_artifacts,
        )
        history_size, audit_size, image_size = len(loop.history), len(audit_refs), len(loop.outbox)
        saved_locations.update(locations)

    # Stable observation entrances use the same MODEL_WORK reader and sealed artifacts.
    # Rebuild deterministically on resume; checkpoint locations may never be rebound.
    reading_tokens = [
        remember_location(
            locations,
            kind="MODEL_WORK",
            artifact_ref=seal(reading, "application/vnd.tsdaf.model-reading+json").model_dump(
                mode="json"
            ),
            owner_task=reading.get("work_unit_id"),
            provided_to=work_unit.work_unit_id,
            ordinal=index,
        )
        for index, reading in enumerate(readings)
    ]
    observation_entries: list[dict[str, Any]] = []
    for index, (reading, root) in enumerate(zip(readings, reading_tokens, strict=True)):
        observations = reading.get("observations", [])
        if not isinstance(observations, list):
            raise ValueError("sealed reading observations must be a list")
        for ordinal, observation in enumerate(observations):
            value = {
                **observation,
                "modality": reading["modality"],
                "limitations": reading.get("limitations", []),
                "overview_location": root,
            }
            original_ref = seal(value, "application/vnd.tsdaf.model-reading+json").model_dump(
                mode="json"
            )
            parts = reading_text_parts(value, max_bytes=READ_BYTES)
            next_location: str | None = None
            for number in range(len(parts) - 1, -1, -1):
                part = parts[number]
                if len(parts) > 1:
                    part = {**part, "next": {"location": next_location} if next_location else None}
                token = remember_location(
                    locations,
                    kind="MODEL_WORK",
                    artifact_ref=seal(part, "application/vnd.tsdaf.model-reading+json").model_dump(
                        mode="json"
                    ),
                    owner_task=reading.get("work_unit_id"),
                    provided_to=work_unit.work_unit_id,
                    ordinal=index,
                    observation=ordinal,
                    scope=observation["scope"],
                    **(
                        {
                            "part": number + 1,
                            "parts": len(parts),
                            "original_ref": original_ref,
                            "coverage": part["coverage"],
                        }
                        if len(parts) > 1
                        else {}
                    ),
                )
                next_location = token
            observation_entries.append(
                {
                    "location": token,
                    "scope": observation["scope"],
                    "modality": reading["modality"],
                    "source_ids": observation["source_ids"],
                    **({"parts": len(parts)} if len(parts) > 1 else {}),
                }
            )

    def reading_navigation(entries: list[dict[str, Any]], label: str) -> dict[str, Any]:
        token = remember_location(locations, kind="DIRECTORY", entries=entries, label=label)
        return reading_navigation_page(entries, token, max_bytes=READ_BYTES)

    def related_readings(selected: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return reading_navigation(
            reading_candidates(observation_entries, selected, source_index),
            "related upstream interpretations",
        )

    if restored is None:
        # The first thing this task should read is what the lanes concluded, not an index
        # of every block in the paper. Sources stay readable at the catalog state location
        # given in every turn's progress; only the handful of places worth opening first
        # travel in the task input.
        catalog = [entry for entry in task_catalog if entry.get("kind") != "STANDARD_SOURCE"]
        # Saved model work keeps its own immutable identity, named here so a reading
        # delivered whole below can be referred to by location.
        inlined = len(canonical_json_bytes(readings)) <= READINGS_INLINE_MAX
        initial_view = {
            "machine_profile": machine_profile.value,
            "task": (
                "Write the route the main text tells and draws: every step, and what the "
                "narrative and the schemes state of how each was run."
            ),
            # No work unit id: the model never names its own task (batch 2A, 2d).
            # No result here: what the task has saved -- nothing yet -- is its progress,
            # which every request carries, this first one included.
            "depth": work_unit.depth,
            # The first layer's readings are this task's input, delivered whole and kept
            # in front of the model every turn; on a real paper, handing them out only as
            # locations to read sent a model through forty turns of re-reading them as
            # the window turned over, and it never wrote.
            "readings": [
                {**reading, "location": token}
                for reading, token in zip(readings, reading_tokens, strict=True)
            ]
            if inlined
            else [],
            # Which sources this task may use, and how much of each -- not their text.
            # A conclusion worth checking is checked by reading the source itself.
            "source_index": [
                {
                    **{key: value for key, value in item.items() if key != "sentences"},
                    **(
                        {"sentence_count": len(item["sentences"])}
                        if isinstance(item.get("sentences"), list)
                        else {}
                    ),
                }
                for item in source_index
            ],
            "upstream_interpretations": reading_navigation(
                observation_entries, "upstream interpretations"
            ),
            "source_errors": source_errors,
            "opening_locations": catalog,
            "sources": (
                f"{sum(1 for entry in task_catalog if entry.get('kind') == 'STANDARD_SOURCE')} "
                "authorized sources back these conclusions. To check one, search them by "
                f"what they say: read location '{SEARCH_LOCATION}' with "
                'select {"kind": "query", "terms": "..."}. Every hit answers with '
                "its own location, which reads like any other source."
                + experimental_part_note(
                    package,
                    source_inventory,
                    read_authority.text_block_ids,
                    status=source_status,
                )
            ),
        }
        # Saved model work remains inspectable with its own immutable identity. A reading
        # delivered whole in the task input is not also an opening location: listed as
        # one, a model read it back first thing on every real paper (E54); it is in the
        # delivered ledger instead, so a read of it is answered IN_CONTEXT.
        delivered_items: list[dict[str, Any]] = []
        delivered_rows: list[dict[str, Any]] = []
        for index, token in enumerate(reading_tokens):
            if not inlined:
                catalog.append({"location": token, "kind": "MODEL_WORK"})
                continue
            delivered_items.append({"location": token, "select": {"kind": "whole"}})
            delivered_rows.append(
                {
                    "outcome": "READ",
                    "location": token,
                    "origin": "MODEL_WORK",
                    "delivered_as": f"readings[{index}] of the task input",
                }
            )
        if inlined:
            # The whole upstream text already delivered also covers its derived scope
            # entrances. Reopening a different alias must not cause another model read.
            for token, binding in locations.items():
                if binding.get("kind") != "MODEL_WORK" or "observation" not in binding:
                    continue
                delivered_items.append({"location": token, "select": {"kind": "whole"}})
                delivered_rows.append(
                    {
                        "outcome": "READ",
                        "location": token,
                        "origin": "MODEL_WORK",
                        "delivered_as": (
                            f"readings[{binding['ordinal']}].observations"
                            f"[{binding['observation']}] of the task input"
                        ),
                    }
                )
        initial_step: dict[str, Any] = {
            "type": "user_input",
            "content": [{"type": "text", "text": canonical_json_bytes(initial_view).decode()}],
        }
        if delivered_items:
            initial_step[DELIVERED_KEY] = {
                "items": delivered_items,
                "reply": {"outcome": "READ", "items": delivered_rows},
            }
        loop.history.append(initial_step)
        save_progress()

    def validate_questions(proposal: FusionProposal) -> None:
        for question in proposal.questions:
            text_ids = question.text_block_ids or work_unit.read_authority.text_block_ids
            visual_ids = question.visual_ids or work_unit.read_authority.visual_ids
            if not set(text_ids) <= set(read_authority.text_block_ids) or not set(
                visual_ids
            ) <= set(read_authority.visual_ids):
                raise ValueError("reading source exceeds the authorized source catalog")

    def queue_visual(
        selection: dict[str, Any],
        call_id: str,
        visual_bytes: bytes,
        *,
        mapping_ref: ArtifactRef | None = None,
        overview: bool = False,
    ) -> dict[str, Any]:
        visual_id = selection["canonical_source_id"]
        source = source_reader.visual_source(visual_id)
        if mapping_ref is None:
            view = materialize_local_visual_read(
                source=source,
                original_bytes=visual_bytes,
                sampled_box=ResolvedPixelBox(**selection["bbox_px"]),
                store=artifact_store,
                overview=overview,
            )
        else:
            view = read_cached_local_visual_read(
                source=source,
                original_bytes=visual_bytes,
                mapping_ref=mapping_ref,
                store=artifact_store,
            )
        bind_artifacts(view.mapping_ref, view.image_ref)
        display = {
            "mapping_ref": view.mapping_ref.model_dump(mode="json"),
            "image_ref": view.image_ref.model_dump(mode="json"),
            "dimensions": list(view.dimensions),
            "source_frame": view.source_frame("source:" + visual_id, source.dimensions),
        }
        loop.outbox.extend(
            [
                {
                    "type": "text",
                    "text": canonical_json_bytes(
                        {
                            "read_for": call_id,
                            "source_id": selection["source_id"],
                            "visual_id": visual_id,
                            "bbox_px": selection["bbox_px"],
                            "display": _shown_display(display),
                        }
                    ).decode(),
                },
                {
                    "type": "image",
                    "mime_type": "image/png",
                    "artifact_ref": view.image_ref.model_dump(mode="json"),
                    "resolution": "high",
                },
            ]
        )
        return display

    def reread_source(location: str, call_id: str) -> dict[str, Any]:
        """A whole read of a source location this task was already given: the exact
        original selection and its citable receipt, with a visual's image shown again."""
        selection = locations[location]["selection"]
        if selection["modality"] == "VISUAL":
            visual_id = selection["canonical_source_id"]
            payloads = source_reader.read_visuals(
                work_unit=work_unit, visual_ids=(visual_id,), source_inventory=source_inventory
            )
            queue_visual(
                selection,
                call_id,
                payloads[visual_id],
                mapping_ref=ArtifactRef.model_validate(
                    locations[location]["display"]["mapping_ref"]
                ),
            )
        else:
            source_reader.verify_text()
        return {
            "outcome": "READ",
            "kind": "STANDARD_SOURCE",
            "location": location,
            "selected_sources": [selection],
            "upstream_interpretations": related_readings([selection]),
        }

    def read_source(
        answer: ReadFusion,
        call_id: str,
        request_ref: ArtifactRef,
        response_ref: ArtifactRef,
        record: dict[str, Any],
        prefix: str,
    ) -> dict[str, Any]:
        nonlocal base
        sid = answer.location.removeprefix("source:")
        text_selections: tuple[TextFragmentSelection, ...] = ()
        visual_selections: tuple[VisualRegionSelection, ...] = ()
        if sid in read_authority.text_block_ids:
            if answer.region is not None:
                raise ValueError("text source does not have a visual region")
            block = next(b for b in package.blocks if b.block_id == sid)
            text_selections = (
                TextFragmentSelection(
                    selection_id="read",
                    block_id=sid,
                    start_sentence=answer.start_sentence
                    if answer.start_sentence is not None
                    else 0,
                    end_sentence=answer.end_sentence
                    if answer.end_sentence is not None
                    else len(block_sentence_spans(block.raw_text)),
                ),
            )
        elif sid in read_authority.visual_ids:
            if answer.start_sentence is not None:
                raise ValueError("visual source does not have sentence bounds")
            x0, y0, x1, y1 = answer.region or (0, 0, 1000, 1000)
            visual_selections = (
                VisualRegionSelection(
                    selection_id="read",
                    visual_id=sid,
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                ),
            )
        else:
            raise ValueError("source is outside the authorized catalog")
        direct, visual_payloads = bind_fusion_evidence(
            work_unit=work_unit,
            source_reader=source_reader,
            source_inventory=source_inventory,
            execution_id=execution_id,
            text_selections=tuple(
                sel.model_copy(update={"selection_id": prefix + sel.selection_id})
                for sel in text_selections
            ),
            visual_selections=tuple(
                sel.model_copy(update={"selection_id": prefix + sel.selection_id})
                for sel in visual_selections
            ),
            originator=ObservationOriginator(
                originator_kind="MODEL",
                stage_name=FUSION_SELECTION_STAGE,
                stage_version="2.0.0",
                execution_id=execution_id,
                model_profile=record["model_profile"],
                prompt_sha256=prompt_ref.sha256,
                provider_schema_sha256=schema_ref.sha256,
                requested_by_reviewer_id=None,
                base_artifact_sha256=work_unit.package_sha256,
            ),
            created_from=(
                resources_ref,
                prompt_ref,
                schema_ref,
                request_ref,
                response_ref,
            ),
            artifact_store=artifact_store,
        )
        bind_artifacts(direct.registry_ref)
        added_sources = {
            source_key(work_unit.work_unit_id, entry.modality, entry.selection_id): (
                entry.evidence_ref.model_dump(),
            )
            for entry in direct.registry.entries
        }
        selected_sources = [
            source_location(
                entry,
                source_key(work_unit.work_unit_id, entry.modality, entry.selection_id),
                package,
            )
            for entry in direct.registry.entries
        ]
        for selected_source in selected_sources:
            display = None
            if selected_source["modality"] == "VISUAL":
                display = queue_visual(
                    selected_source,
                    call_id,
                    visual_payloads[selected_source["canonical_source_id"]],
                    overview=answer.region is None,
                )
                selected_source["display"] = _shown_display(display)
            selected_source["kind"] = "STANDARD_SOURCE"
            selected_source["location"] = remember_location(
                locations,
                kind="STANDARD_SOURCE",
                selection=selected_source.copy(),
                display=display,
                work_unit_id=work_unit.work_unit_id,
            )
        result = base.model_copy(
            update={
                "sources": {**base.sources, **added_sources},
                "evidence_registry_refs": (
                    *base.evidence_registry_refs,
                    direct.registry_ref,
                ),
            }
        )
        base = result
        return {
            "outcome": "READ",
            "selected_sources": selected_sources,
            "upstream_interpretations": related_readings(selected_sources),
        }

    def sentence_spans(text: str) -> list[tuple[int, int]]:
        return [(s.char_start, s.char_end) for s in block_sentence_spans(text)]

    def seal_readable(value: Any) -> dict[str, Any]:
        return seal(value, "application/vnd.tsdaf.readable-record+json").model_dump(mode="json")

    def perform_with_snapshot(
        body: Callable[[], dict[str, Any]], pixels: int
    ) -> Callable[[], ReadValue]:
        def perform() -> ReadValue:
            nonlocal base
            before_locations, before_outbox = dict(locations), list(loop.outbox)
            before_base = base
            try:
                payload = body()
                after_locations = dict(locations)
                after_base = base
                added_content = list(loop.outbox[len(before_outbox) :])
            finally:
                locations.clear()
                locations.update(before_locations)
                loop.outbox[:] = before_outbox
                base = before_base

            def accept_read() -> None:
                nonlocal base
                locations.update({key:value for key,value in after_locations.items()
                                  if key not in before_locations or before_locations[key] != value})
                added_sources = {key:value for key,value in after_base.sources.items()
                                 if key not in before_base.sources or before_base.sources[key] != value}
                added_refs = tuple(ref for ref in after_base.evidence_registry_refs
                                   if ref not in before_base.evidence_registry_refs
                                   and ref not in base.evidence_registry_refs)
                if added_sources or added_refs:
                    base = base.model_copy(update={'sources':{**base.sources, **added_sources},
                        'evidence_registry_refs':(*base.evidence_registry_refs, *added_refs)})

            display = payload.get("selected_sources", [{}])[0].get("display")
            actual_pixels = (
                display["dimensions"][0] * display["dimensions"][1] if display else pixels
            )
            return ReadValue(payload, added_content, image_pixels=actual_pixels, accept=accept_read)

        return perform

    def source_body(
        binding: dict[str, Any],
        root: str,
        sid: str,
        selection: Selection | None,
        region: tuple[int, int, int, int] | None,
        *,
        call_id: str,
        request_ref: ArtifactRef,
        response_ref: ArtifactRef,
        record: dict[str, Any],
        prefix: str,
    ) -> Callable[[], dict[str, Any]]:
        def body() -> dict[str, Any]:
            if binding["kind"] == "STANDARD_SOURCE" and selection is None:
                payload = reread_source(root, call_id)
            else:
                source_args: dict[str, Any] = {"location": "source:" + sid}
                if isinstance(selection, TextSelection):
                    block = next(b for b in package.blocks if b.block_id == sid)
                    source_args.update(
                        start_sentence=selection.start_sentence,
                        end_sentence=selection.end_sentence
                        or len(block_sentence_spans(block.raw_text)),
                    )
                elif region is not None and sid in read_authority.visual_ids:
                    source_args["region"] = region
                elif selection is not None and not isinstance(selection, WholeSelection):
                    # This packing carries a sentence range or an image region, nothing
                    # else. A selector it has no room for must say so: arriving here as a
                    # whole read would answer a question the caller did not ask.
                    raise ReadFailure(
                        "UNSUPPORTED_SELECTION",
                        f"This source cannot be read with a '{selection.kind}' selection.",
                    )
                payload = read_source(
                    ReadFusion(**source_args), call_id, request_ref, response_ref, record, prefix
                )
            actual = payload["selected_sources"][0].get("location", root)
            payload.update(location=actual, origin="SOURCE", coverage="SELECTED_SOURCE")
            return payload

        return body

    def record_body(
        binding: dict[str, Any],
        root: str,
        pointer: str,
        selection: Selection | None,
        current_at_resolution: ArtifactRef,
        max_bytes: int = READ_BYTES,
    ) -> Callable[[], dict[str, Any]]:
        def body() -> dict[str, Any]:
            origin = "MODEL_WORK"
            if binding["kind"] == "DIRECTORY" and binding.get("label"):
                if pointer or (
                    selection is not None and not isinstance(selection, DirectorySelection)
                ):
                    raise ReadFailure(
                        "UNSUPPORTED_SELECTION",
                        "Navigation offers directory pages; omit select or use kind directory.",
                    )
                page = selection or DirectorySelection(kind="directory")
                return reading_navigation_page(
                    binding["entries"],
                    root,
                    offset=page.offset,
                    limit=page.limit,
                    max_bytes=max_bytes,
                )
            is_directory = binding["kind"] == "DIRECTORY" or binding.get("directory", False)
            if binding["kind"] == "CONTENT":
                parent = binding["resource"]
                artifact = ArtifactRef.model_validate(parent["artifact_ref"])
                value = json.loads(read_exact(artifact_store, artifact))
                actual = root
            else:
                owner_task = binding.get("owner_task")
                if binding["kind"] == "DIRECTORY":
                    value = binding["entries"]
                    artifact = seal(value, "application/vnd.tsdaf.readable-record+json")
                elif binding["kind"] == "MODEL_WORK":
                    artifact = ArtifactRef.model_validate(binding["artifact_ref"])
                    value = json.loads(read_exact(artifact_store, artifact))
                else:
                    artifact = ArtifactRef.model_validate(binding["result_ref"])
                    saved = read_fusion_result(read_exact(artifact_store, artifact))
                    owner_task = saved.work_unit_id
                    # Rendering navigation must not mint new write capabilities.
                    temporary: dict[str, dict[str, Any]] = {}
                    value = result_view(
                        saved,
                        artifact,
                        temporary,
                        historical=artifact != current_at_resolution,
                        fact_ids={binding["fact_id"]} if "fact_id" in binding else None,
                        include_metadata="fact_id" not in binding,
                    )
                    partial = (
                        pointer
                        or (selection is not None and not isinstance(selection, WholeSelection))
                        or len(canonical_json_bytes(value)) > max_bytes
                    )
                    if partial:
                        value.pop("fact_locations", None)
                        temporary = {
                            key: entry for key, entry in temporary.items() if "fact_id" not in entry
                        }
                    locations.update(temporary)
                value_ref = seal(value, "application/vnd.tsdaf.readable-record+json")
                parent = resource_binding(
                    value_ref.model_dump(mode="json"),
                    origin=origin,
                    owner_task=owner_task,
                    provided_to=work_unit.work_unit_id,
                )
                parent["root_ref"] = binding.get("original_ref", artifact.model_dump(mode="json"))
                if "coverage" in binding:
                    parent["derivation"] = [{"path": "/text", "coverage": binding["coverage"]}]
                if binding["kind"] not in {"DIRECTORY", "MODEL_WORK"}:
                    # The receipt's version is the science of the result read, the same
                    # digest `versions("current")` reports, so the two compare.
                    parent["science_version"] = _science_digest(saved)
                actual = remember_location(
                    locations, kind="CONTENT", resource=parent, directory=is_directory
                )
            if pointer:
                value = pointer_value(value, pointer)
            effective = (
                DirectorySelection(kind="directory")
                if is_directory and selection is None
                else selection
            )
            projected = project_record_value(
                value,
                effective,
                location=actual + ("#" + pointer if pointer else ""),
                item_key="fact_id",
                sentence_spans=sentence_spans, max_bytes=max_bytes,
            )
            selected_binding = parent
            fragment = fragment_of(
                parent, projected, seal_value=seal_readable, pointer=pointer, selection=selection
            )
            if fragment is not None:
                selected_binding = fragment
                projected["location"] = remember_location(
                    locations, kind="CONTENT", resource=selected_binding, directory=False
                )
            elif is_directory and not pointer:
                # History entries offer their targets directly; pagination is pinned.
                start = projected["offset"]
                projected["entries"] = value[start : start + len(projected["entries"])]
            payload = {**projected, **resource_receipt(selected_binding), "kind": binding["kind"]}
            if (
                projected["outcome"] == "READ"
                and isinstance(projected["value"], dict)
                and "fact_locations" in projected["value"]
            ):
                payload.update(projected["value"])
                del payload["value"]
            if projected["outcome"] == "READ" and binding["kind"] in {
                "CURRENT_RESULT",
                "HISTORICAL_RESULT",
            }:
                cited = inherited_references(projected["value"])
                anchors = [*source_index, *reads_now().values()]
                payload["upstream_interpretations"] = related_readings(
                    [source for source in anchors if source["source_id"] in cited]
                )
            return payload

        return body

    def resolve_read(
        location: str,
        selection: Selection | None,
        *,
        call_id: str,
        request_ref: ArtifactRef,
        response_ref: ArtifactRef,
        record: dict[str, Any],
        prefix: str,
        state_resources: Mapping[str, Any],
    ) -> ReadPlan:
        def resolve_root(root: str) -> SourceTarget | RecordTarget | None:
            root = authorized_visual_location(
                root, {vid: [{"location": "source:" + vid}]
                       for vid in read_authority.visual_ids}
            )
            binding = copy.deepcopy(locations.get(root))
            if root == SEARCH_LOCATION:
                # Searching is reading by what the text says rather than by where it is.
                # The authorised block ids are the same ones a direct read is checked
                # against, so a query cannot reach past this task's scope.
                def perform_search(pointer: str, selected: Selection | None, max_bytes=READ_BYTES) -> ReadValue:
                    if not isinstance(selected, QuerySelection):
                        raise ReadFailure(
                            "INVALID_READ",
                            "This location is searched: select kind 'query' with terms.",
                        )
                    # Shaped like every other read: the answer travels in ``value``, so a
                    # model parses one result form rather than one per way of selecting.
                    return ReadValue(
                        {
                            "outcome": "READ",
                            "location": SEARCH_LOCATION,
                            "value": text_search_value(
                                package,
                                selected,
                                authorized_block_ids=read_authority.text_block_ids,
                                location_of=lambda block_id: f"source:{block_id}",
                                figure_of=lambda block_id: caption_figures.get(block_id),
                            ),
                        }
                    )

                return RecordTarget(perform_search, with_budget=perform_search)
            if root == CURRENT_LOCATION:
                binding = {
                    "kind": "CURRENT_RESULT",
                    "result_ref": current_ref().model_dump(mode="json"),
                }
            elif root == "history":
                binding = {"kind": "DIRECTORY", "entries": history_entries()}
            elif root.startswith("source:"):
                sid = root.removeprefix("source:")
                if sid not in (*read_authority.text_block_ids, *read_authority.visual_ids):
                    raise ReadFailure("UNAVAILABLE", "Unknown or unauthorized read location.")
                binding = {"kind": "SOURCE_ENTRY", "source_id": sid}
            if binding is None:
                return None
            current_at_resolution = current_ref()
            if binding["kind"] not in {"SOURCE_ENTRY", "STANDARD_SOURCE"}:
                pinned = binding

                def perform_record(pointer: str, selected: Selection | None, max_bytes=READ_BYTES) -> ReadValue:
                    return perform_with_snapshot(
                        record_body(pinned, root, pointer, selected, current_at_resolution, max_bytes), 0
                    )()

                return RecordTarget(perform_record, with_budget=perform_record)
            sid = binding.get("source_id") or binding["selection"]["canonical_source_id"]
            if sid in read_authority.visual_ids:
                medium: str = "visuals"
            elif sid in read_authority.text_block_ids:
                medium = "text"
            else:
                raise ReadFailure("UNAVAILABLE", "Unknown or unauthorized read location.")
            saved = binding["kind"] == "STANDARD_SOURCE"
            display = binding.get("display") if saved else None
            source_binding = binding

            def plan(
                selected: Selection | None, region: tuple[int, int, int, int] | None, pixels: int
            ) -> Callable[[], ReadValue]:
                return perform_with_snapshot(
                    source_body(
                        source_binding,
                        root,
                        sid,
                        selected,
                        region,
                        call_id=call_id,
                        request_ref=request_ref,
                        response_ref=response_ref,
                        record=record,
                        prefix=prefix,
                    ),
                    pixels,
                )

            return SourceTarget(
                medium="visuals" if medium == "visuals" else "text",
                plan=plan,
                saved=saved,
                preview_location="source:" + sid,
                preview_extra={"source_id": sid},
                allow_adjacent=False,
                display_pixels=(
                    display["dimensions"][0] * display["dimensions"][1]
                    if medium == "visuals" and display
                    else None
                ),
                dimensions=lambda: source_reader.visual_source(sid).dimensions,
                raw_text=lambda: next(b.raw_text for b in package.blocks if b.block_id == sid),
            )

        return resolve_read_location(
            location,
            selection,
            task_id=execution_id,
            store=artifact_store,
            state_resources=state_resources,
            persisted_locations=locations,
            resolve_root=resolve_root,
            sentence_spans=sentence_spans,
        )

    def initial_context() -> dict[str, Any]:
        if loop.iteration == 0:
            return loop.history[0]
        initial = json.loads(loop.history[0]["content"][0]["text"])
        for entry in initial["source_index"]:
            for key in ("text", "raw_text", "numbered_sentences", "selected_text", "sentences"):
                entry.pop(key, None)
        return {
            "type": "user_input",
            "content": [{"type": "text", "text": canonical_json_bytes(initial).decode()}],
        }

    turn_binding = {
        "execution_id": execution_id,
        "work_unit_id": work_unit.work_unit_id,
        "resources_sha256": resources_ref.sha256,
    }

    inherited_source_ids: set[str] = set()
    for ref in audit_refs:
        if ref.media_type == FUSION_AUDIT_MEDIA:
            inherited_source_ids.update(
                json.loads(read_exact(artifact_store, ref)).get("inherited_source_ids", [])
            )

    def history_entries() -> list[dict[str, Any]]:
        """Earlier states of the result that said something different from the current one.

        The versions are the ones registered where they were made (`register_version`) or
        pinned by a read of 'current'. A version whose science matches the current one, or
        an earlier listed one, is not listed: it has nothing a model should go and read --
        on a real paper a model once read eight such versions in forty turns and wrote none.
        """
        current_digest = _science_digest(base)
        listed: set[str] = set()
        entries = []
        for key, value in locations.items():
            if "result_ref" not in value or "fact_id" in value:
                continue
            ref = ArtifactRef.model_validate(value["result_ref"])
            digest = _science_digest(read_fusion_result(read_exact(artifact_store, ref)))
            if digest == current_digest or digest in listed:
                continue
            listed.add(digest)
            entries.append({"location": key, "kind": "HISTORICAL_RESULT"})
        return entries

    def versions(location: str) -> str | None:
        # The two locations whose content moves under a fixed name. A read of either
        # reports as its version the digest of what it sealed: the current result, or
        # the directory of earlier ones; the same digest is what a later request checks.
        root = location.partition("#")[0]
        if root == CURRENT_LOCATION:
            # What the result says, not which artifact sealed it: a source read reseals
            # the result (its citable sources grew) and used to move this version, so
            # every read pushed the model's own view of its facts out of the window and
            # it read the view again -- three of seventeen turns on a real window (E54).
            return _science_digest(base)
        if root == "history":
            return hashlib.sha256(canonical_json_bytes(history_entries())).hexdigest()
        return None

    def turn_progress(current_states: Mapping[str, Any]) -> dict[str, Any]:
        # No menu of state locations, and no list of every source read so far: the
        # recent actions here already name each read and its location, bounded, and the
        # content window shows or points to what was read. The locations still resolve
        # for a read that names one. What the task has saved is listed by the route and
        # by id (`fusion_view`), recomputed every turn; it was the result in storage
        # shape, which above 8,000 bytes became a directory of eight keys and their byte
        # sizes -- on a2 from the first write on (fusion measurement, 2026-09-24).
        nonlocal last_shown
        last_shown = base
        return {
            "unresolved_writes": delivery.pending(),
            "action_progress": bounded_action_view(current_states[STATE_LOCATION_HISTORY].value),
            "saved": fusion_view(base),
        }

    readers = tuple(str(reading["modality"]) for reading in readings)

    def reads_now() -> dict[str, Mapping[str, Any]]:
        # A source this task read itself, by the key its read minted: named by what was
        # read, in the order it read them, and never by the request and the call in its
        # key. The place it was read at takes that name without its modality.
        return fusion_reads(
            [
                binding["selection"]
                for binding in locations.values()
                if binding.get("kind") == "STANDARD_SOURCE"
            ]
        )

    def names_now(seed: Names | None = None) -> FusionNames:
        # Rebuilt from the task as it stands on every request and every call, so a place
        # read this turn is named on the next one, and a name once given stays given.
        return fusion_names(
            base,
            locations=locations,
            history=loop.history,
            readers=readers,
            route_group_id=work_unit.route_group_id,
            read=reads_now(),
            seed=seed,
        )

    def make_domain_names(previous: dict[str, Any] | None) -> DomainNaming:
        prefix, bound = binder_spec(base)
        seed = names_from_component(previous["table"]) if previous is not None else None
        return fusion_domain(names_now(seed), prefix=prefix, bound=bound)

    def after_projection(visible: list[dict[str, Any]]) -> None:
        for step in visible:
            if step["type"] not in {"user_input", "function_result"}:
                continue
            for part in step.get("content", step.get("result", [])):
                if part.get("type") == "text" and part["text"].startswith("{"):
                    payload = json.loads(part["text"])
                    if isinstance(payload, dict) and payload.get("origin") in {
                        ACTION_PROGRESS_ORIGIN,
                        "EXECUTION_RECORD",
                    }:
                        continue
                    inherited_source_ids.update(inherited_references(payload))

    def interaction_extras(
        prepared: Any, completed: Any, response: dict[str, Any]
    ) -> dict[str, Any]:
        extras: dict[str, Any] = {
            "execution_id": execution_id,
            "resources_ref": resources_ref.model_dump(mode="json"),
            "attempt": loop.iteration,
            "delivered_source_ids": sorted(
                {
                    value["selection"]["source_id"]
                    for value in locations.values()
                    if value["kind"] == "STANDARD_SOURCE"
                }
            ),
            "inherited_source_ids": sorted(inherited_source_ids),
        }
        extras.update(
            outcome="RETURNED",
            tool_call_count=sum(
                step.get("type") == "function_call" for step in response.get("steps", [])
            ),
        )
        return extras

    def on_provider_error(error: ProviderInteractionError, audit: ArtifactRef | None) -> None:
        raise error

    def on_turn_recorded(interaction_ref: ArtifactRef, audit: ArtifactRef | None) -> None:
        audit_refs.append(interaction_ref)

    def on_interaction(record: dict[str, Any]) -> None:
        if emit_event is not None:
            emit_event("literature_lane_interaction", lane_interaction_event("fusion", record))

    def on_recovered(record: dict[str, Any]) -> None:
        inherited_source_ids.update(record["inherited_source_ids"])
        audit_refs.append(ArtifactRef.model_validate(record["interaction_ref"]))

    def targets(name: str, raw: Any) -> dict[str, str | None]:
        return (
            submission_targets(
                raw, base, locations, unified_expression=machine_profile.unified_expression
            )
            if isinstance(raw, dict) else {}
        )

    def resolve_for(location: str, selection: Selection | None, ctx: CallContext) -> ReadPlan:
        from tsdaf.workflow.resource_reads import read_item_action_id
        from tsdaf.contracts.tool_read import item_name
        identity = read_item_action_id(ctx.interaction_sha256, ctx.call_index, item_name(ctx.read_item))
        return resolve_read(
            location,
            selection,
            call_id=ctx.call["call_id"],
            request_ref=ctx.request_ref,
            response_ref=ctx.response_ref,
            record=ctx.record,
            prefix=identity + ":",
            state_resources=ctx.state_resources,
        )

    @functools.cache
    def quote_partners() -> Mapping[str, tuple[str, ...]]:
        return package_continuity(package).partners

    @functools.cache
    def quote_index():
        from tsdaf.lanes.literature.quote_binding import TextQuoteIndex

        return TextQuoteIndex(package)

    @functools.cache
    def quote_binding(location: str) -> dict[str, Any]:
        selection = locations[location]["selection"]
        quote = text_source_quote(
            selection,
            package,
            document_revision_id=work_unit.document_revision_id,
            package_sha256=work_unit.package_sha256,
            partners=quote_partners(),
            quote_index=quote_index(),
        )
        evidence = base.sources.get(selection["source_id"], ())
        if len(evidence) == 1:
            quote["evidence_ref"] = dict(evidence[0])
        return quote

    def apply_write(
        name: str,
        raw: dict[str, Any],
        ctx: CallContext,
        write_targets: dict[str, str | None],
        repairs: dict[str, list[str]],
    ) -> WriteOutcome:
        nonlocal base, last_shown
        result, items, accepted_batches = apply_sparse_submission(
            base,
            raw,
            locations=locations,
            store=artifact_store,
            last_shown=last_shown,
            accepted_origin=lambda collection, index, change_index: FactOrigin(
                interaction_ref=ArtifactRef.model_validate(ctx.record["interaction_ref"]),
                call_index=ctx.call_index,
                action_id=write_action_id(
                    ctx.interaction_sha256, ctx.call_index, item_key(collection, index)
                ),
                item=item_key(collection, index),
                change_index=change_index,
            ),
            complete_characterization=True,
            machine_profile=machine_profile,
            validate_questions=validate_questions,
            delivered_source_ids=set(ctx.record["delivered_source_ids"]),
            inherited_source_ids=frozenset(ctx.record["inherited_source_ids"]),
            delivered_text={
                binding["selection"]["source_id"]: quote_binding(location)
                for location, binding in locations.items()
                if binding["kind"] == "STANDARD_SOURCE"
                and binding["selection"]["modality"] == "TEXT"
                and binding["selection"]["source_id"] in ctx.record["delivered_source_ids"]
            },
            render_reference=ctx.naming.diagnose_reference if ctx.naming is not None else None,
        )
        changed = result.model_copy(update={"field_origins": base.field_origins}) != base
        if result != base:
            base = result
            register_version()
        # The model's next write is compared with what this one left, whatever its outcome:
        # a PARTIAL write's accepted part is what the model now knows it saved.
        last_shown = base
        rejected = any(item["outcome"] == "REJECTED" for item in items)
        rows = []
        for item in items:
            key = item_key(item["collection"], item.get("index"))
            row = write_result(
                interaction_sha256=ctx.interaction_sha256,
                call_index=ctx.call_index,
                item=key,
                tool=name,
                target=write_targets.get(key),
                outcome=item["outcome"],
                repairs=repairs.get(key, []),
                diagnostic=item.get("diagnostic"),
            )
            item["action_id"] = row["action_id"]
            rows.append(row)
        outcome = (
            "PARTIAL"
            if rejected and changed
            else "REJECTED"
            if rejected
            else "SAVED"
            if changed
            else "NO_CHANGE"
        )
        # Each item's outcome and the ids it landed on -- no echo of what the model just
        # wrote. What is saved after it is the progress at the end of this same turn,
        # listed by the route; the reply used to carry the touched facts with the graph
        # around them and the counts, 167,008 characters for a2's one write (E187).
        reply = {"outcome": outcome, "items": items}
        batches = [
            [c.model_dump(mode="json", exclude_unset=True) for c in batch]
            for batch in accepted_batches
        ]
        return WriteOutcome(reply, rows, {"batches": batches})

    def finish(name: str, raw: dict[str, Any], ctx: CallContext) -> FinishIntent:
        answer = FinishFusion.model_validate_json(canonical_json_bytes(raw))
        # PARTIAL saves an explicit stop even when nothing is publishable. COMPLETE
        # still requires the shape named by progress; publication checks independently.
        if answer.disposition == "COMPLETE" and (left := finish_left(base)):
            raise ValueError("; ".join(left))
        return FinishIntent(disposition=answer.disposition, note=answer.note)

    def finish_reply(completion: dict[str, Any]) -> dict[str, Any]:
        return {
            "outcome": "FINISHED",
            "result_ref": current_ref().model_dump(mode="json"),
            "delivery": completion,
        }

    def audit(ctx: CallContext, event: dict[str, Any]) -> None:
        # The receipt's shape is the executor's (`call_receipt`); this lane adds the
        # result it was writing into and, for a write, the batches it applied.
        call_record = call_receipt(ctx, event)
        if event["kind"] in {"READ", "WRITE"}:
            call_record["result_ref"] = current_ref().model_dump(mode="json")
        if event["kind"] == "WRITE":
            call_record["batches"] = event["batches"]
        audit_refs.append(seal(call_record))

    def on_close_out(completion: dict[str, Any]) -> None:
        # No interaction to bind: the Host ended the task. The record says so, and carries
        # the same delivery a finish would, so the outcome below reads it the same way.
        audit_refs.append(
            seal(
                {
                    "closed_out": completion["closed_out"],
                    "outcome": "FINISHED",
                    "note": completion["note"],
                    "delivery": completion,
                    "result_ref": current_ref().model_dump(mode="json"),
                }
            )
        )

    execute_tool_turns(
        loop,
        ToolConsumer(
            label="fusion",
            name="fusion",
            task_id=execution_id,
            turn_binding=turn_binding,
            request_media="application/vnd.tsdaf.fusion-request+octets",
            response_media="application/vnd.tsdaf.fusion-response+octets",
            interaction_media=FUSION_AUDIT_MEDIA,
            delivery=delivery,
            catalog_data={"entries": task_catalog},
            locations=locations,
            wrap_state_content=True,
            window_chars=FUSION_WINDOW_CHARS_WITH_IMAGES,
            window_images=FUSION_WINDOW_IMAGES,
            versions=versions,
            evidence_view=lambda row, parts: fusion_source_evidence(
                row, parts, locations=locations, package=package
            ),
            result_ref=current_ref,
            system_instruction=prompt,
            tools=json.loads(tools_bytes),
            tool_kinds={"read": "read", "write": "write", "finish_fusion": "finish"},
            progress=turn_progress,
            initial=initial_context,
            after_projection=after_projection,
            present=lambda visible: names_now().present(visible),
            resolve=lambda raw: names_now().resolve(raw),
            make_domain_names=make_domain_names,
            restore_domain_names=restore_fusion_domain,
            interaction_extras=interaction_extras,
            on_provider_error=on_provider_error,
            on_turn_recorded=on_turn_recorded,
            on_interaction=on_interaction,
            on_recovered=on_recovered,
            targets=targets,
            resolve_read=resolve_for,
            apply_write=apply_write,
            finish=finish,
            finish_reply=finish_reply,
            on_close_out=on_close_out,
            audit=audit,
            diagnose=diagnostic_value,
            rejected_errors=(ValueError, KeyError, TypeError),
            finished_message="fusion was explicitly finished earlier in this response",
            unknown_tool_message="unknown fusion tool",
            reject_message="This action was not applied; earlier saved actions remain.",
        ),
        store=artifact_store,
        bind=bind_artifacts,
        client=client,
        provider_binding=provider_binding,
        recovery_artifacts=recovery_artifacts,
        checkpoint=save_progress,
    )
    # Explicit invocation stop; scientific goal completion is unassessed.
    decision = decision_for_delivery(
        delivery.completion["disposition"] if delivery.completion else None
    )
    audit_ref = seal(
        {
            "schema_version": FUSION_RECEIPT_VERSION,
            "execution_id": execution_id,
            "work_unit_id": work_unit.work_unit_id,
            "result_ref": current_ref().model_dump(mode="json"),
            "decision": decision,
            "delivery": delivery.completion,
            "attempt_refs": [ref.model_dump(mode="json") for ref in audit_refs],
            # No work is handed on any more (2026-09-23 ruling). The key stays, empty, so
            # the audit keeps its version and sealed windows' audits still read.
            "child_refs": [],
        }
    )
    return SealedFusionOutcome(base, current_ref(), audit_ref, decision)


# Characters of readings delivered inline in the task input; the two reader views of a
# real paper came to 13K, above the 8K that used to drop them.
READINGS_INLINE_MAX = 48_000
# The content window of a fusion task: a real paper's blocks run 1.5-3K each and a task
# reads six to eight of them alongside the reader views -- and a route group reads
# fifteen (kalmanol r7) before it writes, with its own blocks now delivered up front on
# top (E54). The window holds that working set whole; a re-read costs a whole turn.
FUSION_WINDOW_CHARS = 96_000
# This is the lane every one of the 80 image_region reads in the production replay corpus
# came from, and a cropped scheme is what it writes a step from, so four pictures stay
# shown. An image is now charged to the window (`content_window.ReadEntry.size`) and four
# of decumbenine_B's overview PNGs are about 501,000 base64 characters, so the window has
# to hold the prose working set and those four: below that the first picture overflows
# the budget on its own and latches it shut for every older read behind it.
FUSION_WINDOW_IMAGES = 4
# Pictures no longer spend the character budget (`content_window.ReadEntry.text_size`);
# the name is kept for its callers.
FUSION_WINDOW_CHARS_WITH_IMAGES = FUSION_WINDOW_CHARS


# The crop's own artifacts: the host opens the same crop again from them (`reread_source`);
# the model is shown the crop's size and where it sits on the original, never the hashes
# that seal it (batch 2A, 2d).
_CROP_ARTIFACTS = frozenset({"mapping_ref", "image_ref"})


def _shown_display(display: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in display.items() if key not in _CROP_ARTIFACTS}


def _science_digest(result: FusionResult) -> str:
    """What a result says, apart from which sources it may cite."""
    return hashlib.sha256(
        canonical_json_bytes(
            result.model_dump(mode="json", exclude={"sources", "evidence_registry_refs"})
        )
    ).hexdigest()
