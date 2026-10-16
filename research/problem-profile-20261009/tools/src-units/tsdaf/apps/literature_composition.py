"""Composition root for the private literature lanes.

Provider choice belongs to `tsdaf.apps`: the lane declares the port in
`tsdaf.lanes.literature.provider_port` and never imports an adapter. This module is
the single place that names the concrete Gemini adapter types the literature lanes
are authorized to run against, and the single place that reads credentials.

The literature route group needs three *independent* interaction streams -- T1, V1
and Gate 1 -- so every mode below builds three separate adapter instances. Two lanes
sharing one stream owner are refused inside the lane before any provider call.

One run is one paper's explicit route group, then the MAIN supplement investigation,
then -- when asked -- publication of the merged facts. (Until 2026-09-23 a route group's
handoffs ran on as reaction windows; the fusion now writes the whole route itself and
hands nothing on.) What is still not connected is named by
`unconnected_literature_options`.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from tsdaf.adapters.gemini.interactions import (
    GEMINI_MEDIUM_EFFORT_PROFILE,
    GEMINI_MODEL_ID,
    GEMINI_MODEL_PROFILE,
    GeminiAdapterError,
    GeminiInteraction,
    GeminiInteractions,
    RecordedGeminiInteractions,
)
from tsdaf.apps._provider_config import live_gateway_client, safe_base_url
from tsdaf.contracts.execution import (
    ExecutionStatus,
    FailureCategory,
    FailureCode,
    FailureDetail,
    TypedFailure,
    failure_category,
)
from tsdaf.contracts.literature.schema import MachineProfile
from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.partition import ExperimentalPartition
from tsdaf.contracts.reading_units import EXPERIMENTAL_KINDS, ReadingUnits, UnitKind
from tsdaf.contracts.routing import RoutingDisposition
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
from tsdaf.lanes.document.package_loader import PackageLoadError
from tsdaf.lanes.document.reading_units import partition_clean_view
from tsdaf.lanes.literature.bootstrap import VerifiedRouterHandoffs, verify_router_handoff_bundle
from tsdaf.lanes.literature.characterization_verification import characterization_verification
from tsdaf.lanes.literature.condition_categories import FIXED_CONDITION_FIELDS
from tsdaf.lanes.literature.container_enrichment import (
    readable_with_step_sources,
    step_sources,
)
from tsdaf.lanes.literature.enrichment_models import (
    ANSWER_COLLECTIONS,
    InvestigationReceipt,
    delivered_nothing,
)
from tsdaf.lanes.literature.evidence import furniture_by_package, observation_texts
from tsdaf.lanes.literature.fact_projection import PublicationNotReady
from tsdaf.lanes.literature.investigation_kinds import investigation_kind
from tsdaf.lanes.literature.middle_layer import ParagraphSet
from tsdaf.lanes.literature.provider_port import (
    AUTHORIZED_MODEL_PROFILE,
    MEDIUM_READER_MODEL_PROFILE,
    LaneInteractionClient,
    ProviderBinding,
)
from tsdaf.lanes.literature.round_handoff import paragraph_scopes, source_catalog, source_handoff
from tsdaf.lanes.literature.source_manifest import (
    UNKNOWN_SOURCE_STATUS,
    LiteratureSources,
    PaperSourceStatus,
    load_literature_sources,
)
from tsdaf.lanes.provider_attempts import PROVIDER_ATTEMPT_MEDIA_TYPE, read_provider_attempts
from tsdaf.workflow.call_budget import BudgetPaused, CallBudget
from tsdaf.workflow.literature_enrichment_publication import (
    publish_enriched_revision,
    round_reconciliation,
)
from tsdaf.workflow.literature_investigation_runtime import (
    InvestigationFailed,
    SealedInvestigation,
    run_literature_investigation,
)
from tsdaf.workflow.literature_paper_driver import LiteraturePaperRun, run_literature_paper
from tsdaf.workflow.literature_paper_facts import (
    PublishedPaperFactSet,
    experimental_checkpoint,
    resolve_public_ref,
    stable_main_receipt,
)
from tsdaf.workflow.local_runtime import LocalObjectStore, LocalRunner, LocalRuntimeError
from tsdaf.workflow.reading_units_runtime import (
    read_reading_units,
    run_reading_units_stage,
)
from tsdaf.workflow.review_queue import corpus_exclusion, review_queue_row

__all__ = (
    "LITERATURE_MODEL_PROFILE_VERSIONS",
    "LITERATURE_PROVIDER_BINDING",
    "compose_literature_workflow",
    "execute_literature_workflow",
    "literature_provider_binding",
    "unconnected_literature_options",
)


# Deployment identity for the public bundle. It belongs here because naming the
# provider behind a profile is exactly what the composition root exists to decide;
# neither the lanes nor the workflow may know it. FAST_MULTIMODAL is the tier the
# frozen design assigns to the weak-deconstruction layer and the visual sub-route,
# which is what T1, V1 and Gate 1 are.
LITERATURE_MODEL_PROFILE_VERSIONS: tuple[dict[str, object], ...] = (
    {
        "logical_role": "FAST_MULTIMODAL",
        "profile_id": GEMINI_MODEL_PROFILE,
        "profile_version": "4.2.0",
        "provider": "lpzproxy-openai-compatible",
        "model": GEMINI_MODEL_ID,
    },
    {
        "logical_role": "FAST_MULTIMODAL",
        "profile_id": GEMINI_MEDIUM_EFFORT_PROFILE,
        "profile_version": "1.0.0",
        "provider": "lpzproxy-openai-compatible",
        "model": GEMINI_MODEL_ID,
    },
)


def literature_provider_binding(
    *,
    backend_namespaces: tuple[tuple[str, str], ...] = (),
) -> ProviderBinding:
    """Return the only provider binding the literature lanes may be run with."""

    if (
        AUTHORIZED_MODEL_PROFILE != GEMINI_MODEL_PROFILE
        or MEDIUM_READER_MODEL_PROFILE != GEMINI_MEDIUM_EFFORT_PROFILE
    ):
        raise ValueError("literature lane profile pin drifted from the adapter profile")
    return ProviderBinding(
        client_types=(GeminiInteractions, RecordedGeminiInteractions),
        interaction_type=GeminiInteraction,
        backend_namespaces=backend_namespaces,
    )


def live_literature_provider_binding(base_url: str) -> ProviderBinding:
    """Version the nonsecret deployment location separately from request contents."""
    if safe_base_url(base_url) is None:
        raise ValueError("backend identity requires a safe base URL")
    digest = hashlib.sha256(
        ("gemini-openai-chat-completions-adapter.v1\n" + base_url).encode("utf-8")
    ).hexdigest()
    return literature_provider_binding(
        backend_namespaces=tuple((stage, f"live-v1:{digest}") for stage in ("T1", "V1", "GATE1"))
    )


def recorded_literature_provider_binding(
    clients: Sequence[LaneInteractionClient],
) -> ProviderBinding:
    """Bind each lane to the bytes its replay client actually loaded, not its path."""
    if any(type(client) is not RecordedGeminiInteractions for client in clients):
        raise TypeError("recorded backend identity requires exact replay clients")
    return literature_provider_binding(
        backend_namespaces=tuple(
            (stage, f"recorded-v1:{cast(RecordedGeminiInteractions, client).fixture_sha256}")
            for stage, client in zip(("T1", "V1", "GATE1"), clients, strict=True)
        )
    )


LITERATURE_PROVIDER_BINDING = literature_provider_binding()


def _failure_payload(
    code: FailureCode | str,
    message: str,
    *,
    execution_id: str,
    status: str = "ERROR",
    detail: str | None = None,
    route_group: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "execution_id": execution_id,
        "status": status,
        "code": code.value if isinstance(code, FailureCode) else code,
        "message": message,
    }
    if detail is not None:
        payload["detail"] = detail
    if route_group is not None:
        # A later stage failed after the route group sealed: what it sealed stands.
        payload["route_group"] = route_group
    return payload


def _diagnostic(error: Exception) -> str:
    """Say what actually went wrong, without putting a filesystem path in the payload.

    A blanket `PACKAGE_INVALID` that discarded its cause cost three diagnosis cycles
    against a live provider: every contract refusal, every provider-shape error and
    every genuinely invalid package produced the same four fields. Our own refusals
    carry English messages we wrote, so they are safe to surface; an `OSError` carries
    a path, so only its errno name is.
    """

    if isinstance(error, OSError):
        return f"OSError:{errno.errorcode.get(error.errno or 0, 'UNKNOWN')}"
    return f"{type(error).__name__}: {error}"


LaneClients = tuple[
    LaneInteractionClient,
    LaneInteractionClient,
    LaneInteractionClient,
    LaneInteractionClient | None,
    LaneInteractionClient | None,
    LaneInteractionClient | None,
    LaneInteractionClient | None,
]
# The stages an offline run replays: the three first-layer lanes it requires, the MAIN
# supplement investigation, the experimental-detail investigation, the reading of each
# supporting-information package into units (E81, E136), and the figure reader an
# investigation dispatches instead of being handed an image, each run when a fixture
# for it is supplied.
FIRST_LAYER_STAGES = ("T1", "V1", "GATE1")
SUPPLEMENT_STAGE = "SUPPLEMENT"
DETAIL_STAGE = "DETAIL"
UNITS_STAGE = "UNITS"
FIGURE_STAGE = "FIGURE"
CONNECTED_STAGES = (
    *FIRST_LAYER_STAGES,
    SUPPLEMENT_STAGE,
    DETAIL_STAGE,
    UNITS_STAGE,
    FIGURE_STAGE,
)


def _recorded_clients(fixtures: Mapping[str, Path]) -> LaneClients:
    """Replay one distinct recorded fixture per lane so no stream owner is shared."""

    stages = tuple(stage for stage in CONNECTED_STAGES if stage in fixtures)
    paths = tuple(fixtures[stage] for stage in stages)
    if len({path.resolve(strict=False) for path in paths}) != len(paths):
        raise ValueError("each literature lane requires its own recorded fixture")
    supplement = fixtures.get(SUPPLEMENT_STAGE)
    detail = fixtures.get(DETAIL_STAGE)
    units = fixtures.get(UNITS_STAGE)
    figure = fixtures.get(FIGURE_STAGE)
    return (
        RecordedGeminiInteractions(fixtures["T1"]),
        RecordedGeminiInteractions(fixtures["V1"]),
        RecordedGeminiInteractions(fixtures["GATE1"]),
        RecordedGeminiInteractions(supplement) if supplement is not None else None,
        RecordedGeminiInteractions(detail) if detail is not None else None,
        RecordedGeminiInteractions(units) if units is not None else None,
        RecordedGeminiInteractions(figure) if figure is not None else None,
    )


def _live_clients(api_key: str, base_url: str) -> LaneClients:
    """Build seven adapters over seven transports; each owns its own interaction stream."""

    # Streamed, and not as an optimization: the gateway closes a buffered call before
    # a long-thinking turn answers (E64; `_provider_config.live_gateway_client` holds
    # the rule and the evidence). The SI reading included -- the split it replaced
    # thought for 40-56 s on the c3 papers, a coin toss against the same ceiling.
    return (
        live_gateway_client(api_key, base_url),
        live_gateway_client(api_key, base_url),
        live_gateway_client(api_key, base_url),
        live_gateway_client(api_key, base_url),
        live_gateway_client(api_key, base_url),
        live_gateway_client(api_key, base_url),
        live_gateway_client(api_key, base_url),
    )


def _condition_evidence_counts(
    published: PublishedPaperFactSet,
    baseline: PublishedPaperFactSet,
) -> dict[str, int]:
    """Count final condition values by their text evidence's relation to their step.

    Public observations are the sealed projection of the receipt's selections/read
    scopes, already bound to content-derived document revisions. Compare to the
    pre-detail step anchors: enrichment must not expand its own reference boundary.
    Count each final field/note once, even if it has several citations. Missing or
    non-text evidence is unknown, never evidence that a condition is local. A mixed
    local/remote citation is outside. This is a sentinel, not a procedure resolver.
    """
    by_id = {o["observation_id"]: cast(Mapping[str, Any], o) for o in published.observations}
    domain = cast(Mapping[str, Any], published.machine["domain_payload"])
    counts = {"local": 0, "outside": 0, "unknown": 0}
    for block in domain["reaction_blocks"]:
        own = set(
            step_sources(baseline.machine, baseline.observations, (block["reaction_block_id"],))
        )
        for operation in block["operations"]:
            values = [
                [use["evidence_ref"] for use in operation[field]["evidence_uses"]]
                for field in FIXED_CONDITION_FIELDS.values()
                if operation[field].get("value") is not None
            ]
            values.extend(note["evidence_refs"] for note in operation["other_conditions"])
            for refs in values:
                sites: set[tuple[str, str]] = set()
                unknown = not refs or not own
                for ref in refs:
                    observation = by_id.get(ref["observation_id"], {})
                    fragments = observation.get("anchor", {}).get("fragments", ())
                    if not fragments:
                        unknown = True
                    sites.update(
                        (str(observation["document_revision_id"]), f["block_id"]) for f in fragments
                    )
                state = "outside" if own and sites - own else "unknown" if unknown else "local"
                counts[state] += 1
    return counts


def _published_counts(
    published: PublishedPaperFactSet,
    *,
    baseline: PublishedPaperFactSet | None = None,
    packages: Sequence[CanonicalPaperPackage] = (),
) -> dict[str, object]:
    """What one published machine result holds, in numbers a reader can compare."""
    machine = published.machine
    domain = machine["domain_payload"]
    unresolved = machine["unresolved_items"]
    assert isinstance(domain, Mapping) and isinstance(unresolved, list)
    texts = observation_texts(published.observations, packages)
    # The same page printing the publication's rekey set aside (E140), from the one
    # place it is computed, so this count and the published basis cannot disagree.
    furniture = tuple(v for v in furniture_by_package(packages).values() if v)
    return {
        "machine_result_id": machine["machine_result_id"],
        "machine_revision": machine["machine_revision"],
        "completeness": machine["completeness"],
        "coverage": machine["coverage"],
        "compound_count": len(domain["compounds"]),
        "reaction_block_count": len(domain["reaction_blocks"]),
        "route_count": len(domain["routes"]),
        "field_conflict_count": len(domain["field_conflicts"]),
        "unresolved_item_count": len(unresolved),
        # By reason (E76 (4)): what waits for the SI, what the author never said, what
        # a stage left behind -- one number each, not one for all.
        "unresolved_by_reason": {
            reason: sum(1 for item in unresolved if item["reason_code"] == reason)
            for reason in sorted({str(item["reason_code"]) for item in unresolved})
        },
        "si_registration": machine.get("si_registration"),
        "coverage_assessment_count": len(
            cast(list[object], machine.get("coverage_assessments", []))
        ),
        "observation_count": len(published.observations),
        "narrative_count": len(domain["paper_narratives"]),
        "characterization_count": len(domain["characterization_records"]),
        "characterization_source_verification": dict(
            sorted(
                Counter(
                    characterization_verification(record, texts, furniture=furniture)
                    for record in domain["characterization_records"]
                ).items()
            )
        ),
        # The author's method words the Host's table could not place: what to add to
        # the table next (ledger E47: the table grows, never shrinks).
        "unnormalized_method_verbatims": sorted(
            {
                str(record["method_verbatim"])
                for record in domain["characterization_records"]
                if record["method"] is None
            }
        ),
        "unparsed_method_verbatims": sorted(
            {
                str(record["method_verbatim"])
                for record in domain["characterization_records"]
                if record["method"] is not None and record["reported_value"] is None
            }
        ),
        "unparsed_characterization_count": sum(
            1
            for record in domain["characterization_records"]
            if record["method"] is not None and record["reported_value"] is None
        ),
        "condition_evidence_by_step": _condition_evidence_counts(published, baseline or published),
        "machine_artifact": dict(published.machine_artifact_ref),
    }


def _publication_block(
    *,
    workspace: Path,
    package: CanonicalPaperPackage,
    package_dir: Path,
    paper: LiteraturePaperRun,
    sources: LiteratureSources | None = None,
    source_status: PaperSourceStatus = UNKNOWN_SOURCE_STATUS,
    profile: MachineProfile = MachineProfile.V1,
    provider_binding: ProviderBinding = LITERATURE_PROVIDER_BINDING,
) -> tuple[dict[str, object], PublishedPaperFactSet | None]:
    """Publish the merged facts of this run, or say what stopped publication.

    Not ready is an outcome, not an error: the readiness record is sealed and named so
    the reason (no witnessed operations, no source-bound target, no eligible route)
    stays with the run.
    """
    try:
        # Published under an execution named by the facts' content, not the run's
        # label (E66): the same paper parsed the same way is the same publication.
        if profile.faithful_mapping:
            from tsdaf.workflow.paper_skeleton_publication import publish_paper_skeleton
            published = publish_paper_skeleton(workspace=workspace,package=package,package_dir=package_dir,
                receipt_ref=paper.receipt_ref,provider_binding=provider_binding,
                model_profile_versions=LITERATURE_MODEL_PROFILE_VERSIONS,sources=sources)
        else:
            published = stable_main_receipt(
                workspace=workspace,
                package=package,
                package_dir=package_dir,
                paper_receipt=paper.receipt,
                model_profile_versions=LITERATURE_MODEL_PROFILE_VERSIONS,
                sources=sources,
                source_status=source_status,
                profile=profile,
            )
    except PublicationNotReady as error:
        return {
            "status": "NOT_READY",
            "reason": error.reason,
            "subject": error.subject_id,
            "readiness_artifact": error.readiness_ref.model_dump(mode="json")
            if error.readiness_ref is not None
            else None,
        }, None
    return {
        "status": "PUBLISHED",
        "published_route_group_ids": list(published.published_route_group_ids),
        "excluded_route_groups": dict(published.excluded_route_groups),
        **_published_counts(
            published,
            packages=tuple(d.package for d in sources.documents if d.package is not None)
            if sources
            else (package,),
        ),
    }, published


def _experimental_sources(
    *,
    workspace: Path,
    package: CanonicalPaperPackage,
    route_execution_id: str,
    sources: LiteratureSources,
    si_units: Mapping[str, ReadingUnits | None],
) -> tuple[LiteratureSources, int | None]:
    """Inherit upstream semantic units for both locator and middle-layer reading.

    All routes' passages remain available on demand; step labels only choose what
    to deliver first. Never turn the first experimental unit back into a cut that
    reopens the trailing bibliography and crystal tables. Without units there is no
    ordinary text grant; exact sources of upstream facts can still be read through
    ``readable_with_step_sources``, and the investigation reports missing segmentation.
    """
    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)
    handoffs = verify_router_handoff_bundle(
        package=package,
        router_execution_id=route_execution_id,
        terminal_manifest_ref=runner.finish_execution(
            route_execution_id, ExecutionStatus.SUCCEEDED
        ),
        object_store=store,
        runner=runner,
    )
    main_revision = sources.main.registration.document_revision_id
    units: dict[str, ReadingUnits | None] = {main_revision: handoffs.units, **si_units}
    readable: dict[str, tuple[str, ...]] = {}
    narrative: dict[str, tuple[str, ...]] = {}
    for document in sources.active():
        revision = document.registration.document_revision_id
        reading = units.get(revision)
        if document.package is None:
            continue
        if reading is not None:
            reading.validate_against(document.package)
        upstream = reading.units if reading is not None else ()
        readable[revision] = tuple(
            block_id
            for unit in upstream
            if unit.kind in EXPERIMENTAL_KINDS
            for block_id in unit.readable_block_ids
        )
        narrative[revision] = tuple(
            dict.fromkeys(
                (
                    *document.step_block_ids,
                    *(
                        block_id
                        for unit in upstream
                        if unit.kind is UnitKind.NARRATIVE
                        for block_id in unit.reader_block_ids
                    ),
                )
            )
        )
    # This count decides whether a MAIN-only paper has an experimental obligation;
    # narrative passages now readable across routes do not create such an obligation.
    experimental_count = sum(
        len(unit.reader_block_ids)
        for unit in (handoffs.units.units if handoffs.units is not None else ())
        if unit.kind in EXPERIMENTAL_KINDS
    )
    if handoffs.units is not None and handoffs.units.has_experimental_part is None:
        experimental_count = None
    if handoffs.units is None:
        # A legacy verified cut can establish an experimental obligation, but cannot
        # stand in for missing semantic segmentation or grant its entire tail to read.
        _, legacy_experimental = partition_clean_view(package, handoffs.partition)
        experimental_count = len(legacy_experimental) if handoffs.partition is not None else None
    return (
        sources.with_readable(readable).with_units(units).with_step_blocks(narrative),
        experimental_count,
    )


def _router_handoffs(
    *, workspace: Path, package: CanonicalPaperPackage, route_execution_id: str
) -> VerifiedRouterHandoffs:
    """The sealed Router execution this run continues, verified once before any model
    runs: its routing result says whether the paper enters the chain at all (E86),
    its partition whether the main text has an experimental part (E83)."""
    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)
    return verify_router_handoff_bundle(
        package=package,
        router_execution_id=route_execution_id,
        terminal_manifest_ref=runner.finish_execution(
            route_execution_id, ExecutionStatus.SUCCEEDED
        ),
        object_store=store,
        runner=runner,
    )


def _main_has_experimental_part(handoffs: VerifiedRouterHandoffs) -> bool | None:
    """Use the sealed cut when present; an older run without one is unknown.

    A heading match is a navigation hint, not proof that a missing heading means
    there is no experimental part. The old run keeps full-package read authority.
    """
    if handoffs.units is not None:
        return handoffs.units.has_experimental_part
    if handoffs.partition is None:
        return None
    return handoffs.partition.cut_block_id is not None


def _si_views(
    sources: LiteratureSources, si_partitions: Mapping[str, ExperimentalPartition | None]
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """Legacy partition statistics, not source-reading authorization.

    Missing partitions remain unknown. Actual readable units and exact inherited
    anchors are supplied by _experimental_sources and LiteratureSources.
    """
    narrative: dict[str, tuple[str, ...]] = {}
    experimental: dict[str, tuple[str, ...]] = {}
    for document in sources.active_si:
        revision = document.registration.document_revision_id
        partition = si_partitions.get(revision)
        if document.package is None or partition is None:
            continue
        before, after = partition_clean_view(document.package, partition)
        narrative[revision] = before
        experimental[revision] = after
    return narrative, experimental


def _read_si_sources(
    *,
    workspace: Path,
    sources: LiteratureSources,
    execution_id: str,
    units_client: LaneInteractionClient | None,
    provider_is_live: bool,
    live: bool,
) -> tuple[dict[str, ReadingUnits | None], tuple[int, dict[str, object]] | None]:
    """Read every available SI into units (E81, E136), one sealed record per package,
    cached by the package's content like MAIN's. Without a units client the SIs have
    no ordinary text grant. A reading that cannot run stops the chain the way MAIN's
    does: an uncached offline package at LIVE_REQUIRED, anything else as a failure."""
    units: dict[str, ReadingUnits | None] = {}
    for document in sources.active_si:
        revision = document.registration.document_revision_id
        units[revision] = None
        if document.package is None or units_client is None:
            continue
        try:
            stage = run_reading_units_stage(
                workspace=workspace,
                package=document.package,
                execution_id=f"{execution_id}-units-{document.registration.attachment_id}",
                client=units_client,
                source_phase="SI",
                provider_is_live=provider_is_live,
                live=live,
            )
        except LocalRuntimeError as error:
            return units, (
                1,
                _failure_payload(
                    error.code,
                    f"The reading of {revision} could not persist its evidence.",
                    execution_id=execution_id,
                ),
            )
        if isinstance(stage, TypedFailure):
            code = 3 if stage.code is FailureCode.LIVE_REQUIRED else 1
            return units, (
                code,
                _failure_payload(
                    stage.code, stage.message, execution_id=execution_id, status="NOT_STARTED"
                ),
            )
        if stage.status not in {ExecutionStatus.SUCCEEDED, ExecutionStatus.CACHED}:
            failure = stage.failure
            return units, (
                1,
                _failure_payload(
                    failure.code if failure is not None else FailureCode.PROVIDER_REJECTED,
                    f"The reading of {revision} failed: "
                    + (failure.message if failure is not None else stage.status.value),
                    execution_id=execution_id,
                ),
            )
        assert stage.output_artifact is not None
        try:
            read = read_reading_units(LocalObjectStore(workspace), stage.output_artifact)
            read.validate_against(document.package)
        except (LocalRuntimeError, ValueError) as error:
            return units, (
                1,
                _failure_payload(
                    FailureCode.HASH_MISMATCH,
                    f"The sealed reading of {revision} could not be verified: {error}",
                    execution_id=execution_id,
                ),
            )
        units[revision] = read
    return units, None


def _si_block(
    sources: LiteratureSources, si_partitions: Mapping[str, ExperimentalPartition | None]
) -> list[dict[str, object]]:
    """What the run knew about each SI: registered, read, and how much of it lies either
    side of its cut."""
    narrative, experimental = _si_views(sources, si_partitions)
    block: list[dict[str, object]] = []
    for document in sources.active_si:
        revision = document.registration.document_revision_id
        partition = si_partitions.get(revision)
        block.append(
            {
                "document_revision_id": revision,
                "attachment_id": document.registration.attachment_id,
                "package_sha256": document.registration.package_sha256,
                "failure": document.failure,
                "read": partition is not None,
                "cut_block_id": partition.cut_block_id if partition is not None else None,
                "narrative_block_count": (
                    len(narrative[revision]) if revision in narrative else None
                ),
                "experimental_block_count": (
                    len(experimental[revision]) if revision in experimental else None
                ),
            }
        )
    return block


def _investigation_counts(receipt: InvestigationReceipt | None) -> dict[str, float]:
    """One task's own numbers, so a round can be read without opening its receipts.

    The e106 round came back as eight opaque receipts: which task spent forty turns to
    write nothing, and which wrote twelve things in eight, took a separate script over
    the sealed artifacts to answer, and the two tasks that delivered nothing were found
    that way rather than reported. Every number this rework is measured by is counted
    here instead. A turn is one request to the model, which is what `request_refs` holds;
    a task that never sealed a receipt has no turns and no findings.
    """
    answer = receipt.raw_model() if receipt is not None else None
    collections = (
        investigation_kind(receipt.kind).collections()
        if receipt is not None and receipt.raw_answer is not None
        else dict.fromkeys(ANSWER_COLLECTIONS)
    )
    counted = {collection: len(getattr(answer, collection, ())) for collection in collections}
    return {
        "turn_count": len(receipt.request_refs) if receipt is not None else 0,
        "thought_tokens": receipt.thought_tokens if receipt is not None else 0,
        "model_seconds": receipt.model_seconds if receipt is not None else 0.0,
        "read_scope_count": len(receipt.read_scopes) if receipt is not None else 0,
        "read_error_count": len(receipt.read_errors) if receipt is not None else 0,
        "finding_count": sum(counted.values()),
        "patch_count": counted.get("patches", 0),
        "new_condition_count": counted.get("new_conditions", 0),
        "characterization_count": counted.get("characterizations", 0),
        "narrative_count": counted.get("narratives", 0),
        "limitation_count": counted.get("limitations", 0),
        "coverage_count": counted.get("coverage", 0),
    }


def _investigation_block(sealed: SealedInvestigation) -> dict[str, object]:
    answer = sealed.receipt.raw_model()
    return {
        "execution_id": sealed.receipt.execution_id,
        "receipt_artifact": sealed.receipt_ref.model_dump(mode="json"),
        "failure": sealed.receipt.failure,
        "closed_out": sealed.receipt.closed_out,
        "status": getattr(answer, "status", None),
        "delivered_nothing": delivered_nothing(sealed.receipt),
        **_investigation_counts(sealed.receipt),
    }


@dataclass(frozen=True, slots=True)
class _DetailTask:
    """One experimental-detail task of a round: what it is responsible for."""

    attempt_id: str
    # The reaction blocks this task works on, or None for the paper's own task -- the
    # one that is given every block and writes what no single step owns (a compound the
    # route never saw, a measurement to add, each document's coverage).
    scope: tuple[str, ...] | None
    kind: str = "EXPERIMENTAL_DETAIL"


def _task_failure(error: BaseException) -> TypedFailure:
    """One detail task's own failure, for anything that is not already a typed one."""
    return TypedFailure(
        schema_version="tsdaf.typed-failure.v1",
        code=FailureCode.PACKAGE_INVALID,
        message="The experimental-detail task failed before it could seal a receipt.",
        request_id=None,
        details=(FailureDetail(key="cause", value=f"{type(error).__name__}: {error}"[:2000]),),
    )


@dataclass(frozen=True, slots=True)
class _DetailOutcome:
    task: _DetailTask
    sealed: SealedInvestigation | None
    failure: TypedFailure | None
    # Failed attempts may have verified receipts of text actually delivered. Retried
    # attempts remain part of this round. Accepted findings at a capacity INPUT stop
    # travel separately in sealed, still bearing their failure and PARTIAL state.
    failure_receipts: tuple[InvestigationReceipt, ...] = ()

    def counts(self) -> dict[str, float]:
        """This task's numbers; a task that never sealed a receipt reports zeros."""
        return _investigation_counts(self.sealed.receipt if self.sealed is not None else None)

    def delivered_nothing(self) -> bool:
        """The same host fact one step earlier: a task with no receipt delivered nothing."""
        return self.sealed is None or delivered_nothing(self.sealed.receipt)

    def as_record(self) -> dict[str, object]:
        scope = list(self.task.scope) if self.task.scope is not None else None
        if self.sealed is not None:
            return {
                "reaction_block_ids": scope,
                **_investigation_block(self.sealed),
                **(
                    {"failure_code": self.failure.code.value, "failure": self.failure.message}
                    if self.failure
                    else {}
                ),
            }
        # A task that failed before sealing carries the same keys as one that did, so a
        # reader of the round never has to ask which shape it is holding.
        return {
            "reaction_block_ids": scope,
            "execution_id": self.task.attempt_id,
            "status": "FAILED",
            "failure_code": self.failure.code.value if self.failure else None,
            "failure": self.failure.message if self.failure else None,
            "closed_out": None,
            "delivered_nothing": True,
            **self.counts(),
        }


def _verified_failed_receipt(
    workspace: Path,
    error: InvestigationFailed,
    *,
    execution_id: str,
    kind: str,
    sources: LiteratureSources,
) -> InvestigationReceipt | None:
    """Only an attempt-bound receipt may contribute failed-task delivery evidence."""
    store = LocalObjectStore(workspace)
    try:
        refs = LocalRunner(workspace, store).verified_stage_artifacts(
            execution_id, stage_name=error.stage_name, content_key=error.content_key
        )
        if error.receipt_ref not in refs:
            return None
        receipt = InvestigationReceipt.model_validate_json(store.read_bytes(error.receipt_ref))
    except (LocalRuntimeError, OSError, ValueError):
        return None
    if (
        receipt.execution_id != execution_id
        or receipt.kind != kind
        or receipt.failure is None
        or receipt.source_identities != tuple(d.identity() for d in sources.active())
    ):
        return None
    return receipt


def _request_retry_is_terminal(
    workspace: Path, error: InvestigationFailed, *, execution_id: str
) -> bool:
    """Respect the adapter's sealed terminal decision, without a second retry policy."""
    if error.failure.code not in {
        FailureCode.PROVIDER_REJECTED,
        FailureCode.PROVIDER_TRANSIENT,
        FailureCode.PROVIDER_RATE_LIMIT,
    }:
        return False
    store = LocalObjectStore(workspace)
    refs = LocalRunner(workspace, store).verified_stage_artifacts(
        execution_id, stage_name=error.stage_name, content_key=error.content_key
    )
    if error.receipt_ref not in refs:
        return False
    failed = []
    for ref in refs:
        if ref.media_type != PROVIDER_ATTEMPT_MEDIA_TYPE:
            continue
        record = json.loads(store.read_bytes(ref))
        audit = read_provider_attempts(
            store, ref, request_bytes=canonical_json_bytes(record["request"])
        )
        if audit["attempts"][-1]["outcome"] == "FAILED":
            failed.append(audit)
    # Earlier successful requests/figures are not evidence about the terminal one.
    if len(failed) != 1:
        return False
    attempts = failed[0]["attempts"]
    return attempts[-1].get("failure_code") == error.failure.code.value and all(
        a["outcome"] == "FAILED" and a.get("retry_scheduled") is (i < len(attempts))
        for i, a in enumerate(attempts, 1)
    )


def _detail_tasks(
    execution_id: str, machine: Mapping[str, object], *, per_block: bool
) -> tuple[_DetailTask, ...]:
    """The round the detail lane runs: one task per step, then one for the paper.

    The order is the route's own -- the order the blocks appear in the published fact
    set -- and it is the order the receipts are applied in, because applying a receipt
    mints an id for the value it displaces: two runs that apply the same receipts in a
    different order publish different bytes (`publish_enriched_revision`). Which task
    finished first never enters it.

    Without a client factory there is one stream to read from and the round is the one
    paper-scoped task it has always been; a recorded fixture is one stream, and reading
    it from nineteen tasks would hand the whole of it to whichever asked first.
    """
    paper = _DetailTask(execution_id + "-detail", None)
    if not per_block:
        return (paper,)
    domain = cast(Mapping[str, object], machine["domain_payload"])
    blocks = cast(list[Mapping[str, object]], domain["reaction_blocks"])
    return (
        *(
            _DetailTask(f"{execution_id}-detail-B{index:03d}", (str(block["reaction_block_id"]),))
            for index, block in enumerate(blocks)
        ),
        paper,
    )


def _mapping_tasks(execution_id, machine, *, per_block):
    """Use the same ordered experiment containers as the locator dispatch."""
    return tuple(_DetailTask(
        task.attempt_id.replace(execution_id + '-detail', execution_id + '-mapping', 1),
        task.scope, 'EXPERIMENTAL_MAPPING')
        for task in _detail_tasks(execution_id, machine, per_block=per_block))


def round_left(
    machine: Mapping[str, Any],
    sources: LiteratureSources,
    outcomes: Sequence[_DetailOutcome],
) -> dict[str, Any] | None:
    """Current source handoff for the paper task, without changing scientific finish.

    The eligible experimental prose comes from the round's own source directory. Read
    scopes and accepted paragraph selections partition it; a guessed compound label
    never proves that a source was delivered or assigned.
    """
    receipts = [o.sealed.receipt for o in outcomes if o.sealed is not None]
    failed = [
        {
            "reaction_block_ids": list(o.task.scope) if o.task.scope else None,
            "failure_code": o.failure.code.value if o.failure is not None else None,
        }
        for o in outcomes
        if o.sealed is None
    ]
    unaccounted, _, _ = round_reconciliation(machine, sources, receipts, failed)
    located = [r for r in receipts if isinstance(r.raw_model(), ParagraphSet)]
    left: dict[str, Any] = {
        "steps_unaccounted": (
            [row for row in unaccounted if row.get("failure_code")] if located else unaccounted
        ),
    }
    catalog = source_catalog(sources)
    delivered = [
        scope
        for outcome in outcomes
        for receipt in (
            *((outcome.sealed.receipt,) if outcome.sealed is not None else ()),
            *outcome.failure_receipts,
        )
        for scope in receipt.read_scopes
    ]
    handed_off: list[Mapping[str, Any]] = []
    for receipt in receipts:
        answer = receipt.raw_model()
        if isinstance(answer, ParagraphSet):
            handed_off.extend(paragraph_scopes(answer, receipt.selections))
    left["source_handoff"] = {"catalog": catalog, "delivered": delivered, "handed_off": handed_off}
    left.update(source_handoff(catalog, delivered, handed_off))
    return (
        left
        if any(left[key] for key in ("steps_unaccounted", "remaining_sources", "handled_sources"))
        else None
    )


def unconnected_literature_options(
    *,
    route_group_id: str | None,
    publish: bool,
    source_manifest: Path | None,
    previous_publication: Path | None,
    execution_id: str,
    additional_fixtures: bool = False,
    si_packages: tuple[Path, ...] = (),
    machine_profile: MachineProfile = MachineProfile.V1,
) -> tuple[int, dict[str, object]] | None:
    del additional_fixtures  # The detail lane is connected; its fixture is a lane fixture.
    if route_group_id is None and not machine_profile.faithful_mapping:
        message = (
            "Automatic whole-paper execution is not connected after removal of the old "
            "layer scheduler. A run takes one explicit route group and executes it; it "
            "does not schedule the other groups."
        )
    elif previous_publication is not None:
        message = (
            "Enrichment against a previous publication is not connected. A run publishes "
            "the facts it sealed itself and enriches them from the sources it was given."
        )
    elif (source_manifest is not None or si_packages) and not publish:
        message = (
            "A source manifest or --si-package registers the documents the "
            "experimental-detail lane reads after publication; it needs --publish."
        )
    else:
        return None
    return 2, _failure_payload(
        "WORKFLOW_NOT_CONNECTED",
        message,
        execution_id=execution_id,
        status="NOT_STARTED",
    )


def compose_literature_workflow(
    *,
    package_dir: Path,
    workspace: Path,
    route_execution_id: str,
    document_revision_id: str,
    route_group_id: str | None,
    execution_id: str,
    live: bool,
    publish: bool = False,
    recorded_fixtures: Mapping[str, Path] | None = None,
    environment: Mapping[str, str] | None = None,
    source_manifest: Path | None = None,
    previous_publication: Path | None = None,
    si_packages: tuple[Path, ...] = (),
    machine_profile: MachineProfile = MachineProfile.V1,
) -> tuple[int, dict[str, object]]:
    """Assemble provider adapters around the private literature lanes."""

    if not machine_profile.production_enabled:
        return 2, _failure_payload(
            "CONFIGURATION_INVALID",
            "Internal candidate profile is not enabled for production.",
            execution_id=execution_id,
        )
    unavailable = unconnected_literature_options(
        route_group_id=route_group_id,
        publish=publish,
        source_manifest=source_manifest,
        previous_publication=previous_publication,
        execution_id=execution_id,
        additional_fixtures=bool(
            recorded_fixtures and set(recorded_fixtures) - set(CONNECTED_STAGES)
        ),
        si_packages=si_packages,
        machine_profile=machine_profile,
    )
    if unavailable is not None:
        return unavailable

    if live and recorded_fixtures is not None:
        return 2, _failure_payload(
            "CONFIGURATION_INVALID",
            "A live literature run cannot also replay recorded fixtures.",
            execution_id=execution_id,
        )
    if not live and recorded_fixtures is None:
        return 2, _failure_payload(
            "CONFIGURATION_INVALID",
            "An offline literature run requires one recorded fixture per lane.",
            execution_id=execution_id,
        )

    if live:
        values = os.environ if environment is None else environment
        api_key = values.get("OPENAI_API_KEY")
        base_url = safe_base_url(values.get("OPENAI_BASE_URL"))
        if api_key is None or not api_key.strip():
            return 2, _failure_payload(
                "CONFIGURATION_INVALID",
                "Explicit live literature parsing requires a nonblank OPENAI_API_KEY.",
                execution_id=execution_id,
            )
        if base_url is None:
            return 2, _failure_payload(
                "CONFIGURATION_INVALID",
                "Explicit live literature parsing requires a safe OPENAI_BASE_URL.",
                execution_id=execution_id,
            )
        try:
            clients = _live_clients(api_key.strip(), base_url)
            provider_binding = live_literature_provider_binding(base_url)
        except Exception:
            return 2, _failure_payload(
                "CONFIGURATION_INVALID",
                "The fixed OpenAI-compatible literature adapters could not be initialized.",
                execution_id=execution_id,
            )
    else:
        assert recorded_fixtures is not None
        missing = tuple(
            stage
            for stage in CONNECTED_STAGES
            if stage in recorded_fixtures or stage in FIRST_LAYER_STAGES
            if stage not in recorded_fixtures or not recorded_fixtures[stage].is_file()
        )
        if missing:
            return 2, _failure_payload(
                "CONFIGURATION_INVALID",
                f"Missing recorded literature fixtures for: {', '.join(missing)}.",
                execution_id=execution_id,
            )
        try:
            clients = _recorded_clients(recorded_fixtures)
            provider_binding = recorded_literature_provider_binding(clients[:3])
        except (GeminiAdapterError, OSError, TypeError, ValueError):
            return 2, _failure_payload(
                "CONFIGURATION_INVALID",
                "The recorded literature interaction fixtures are invalid.",
                execution_id=execution_id,
            )

    return execute_literature_workflow(
        package_dir=package_dir,
        workspace=workspace,
        route_execution_id=route_execution_id,
        document_revision_id=document_revision_id,
        route_group_id=route_group_id,
        execution_id=execution_id,
        publish=publish,
        provider_binding=provider_binding,
        text_client=clients[0],
        visual_client=clients[1],
        gate_client=clients[2],
        supplement_client=clients[3],
        detail_client=clients[4],
        # One task per step, each on its own stream: only a live run can build them,
        # and a recorded fixture is one stream that the round cannot be split across.
        detail_clients=(
            (lambda: live_gateway_client(api_key.strip(), base_url))
            if live and api_key is not None and base_url is not None
            else None
        ),
        units_client=clients[5],
        figure_client=clients[6],
        # One figure reader per detail task, for the same reason each task has its own
        # detail stream: a V3 turn sits between two of its task's turns, so two tasks
        # sharing one transport would interleave their figure conversations.
        figure_clients=(
            (lambda: live_gateway_client(api_key.strip(), base_url))
            if live and api_key is not None and base_url is not None
            else None
        ),
        provider_is_live=live,
        live=live,
        source_manifest=source_manifest,
        previous_publication=previous_publication,
        si_packages=si_packages,
        machine_profile=machine_profile,
    )


def execute_literature_workflow(
    *,
    package_dir: Path,
    workspace: Path,
    route_execution_id: str,
    document_revision_id: str,
    route_group_id: str | None,
    execution_id: str,
    provider_binding: ProviderBinding,
    publish: bool = False,
    text_client: LaneInteractionClient,
    visual_client: LaneInteractionClient,
    gate_client: LaneInteractionClient,
    supplement_client: LaneInteractionClient | None = None,
    detail_client: LaneInteractionClient | None = None,
    detail_clients: Callable[[], LaneInteractionClient] | None = None,
    units_client: LaneInteractionClient | None = None,
    figure_client: LaneInteractionClient | None = None,
    figure_clients: Callable[[], LaneInteractionClient] | None = None,
    provider_is_live: bool,
    live: bool,
    source_manifest: Path | None = None,
    previous_publication: Path | None = None,
    si_packages: tuple[Path, ...] = (),
    machine_profile: MachineProfile = MachineProfile.V1,
    investigation_call_limit: int | None = None,
) -> tuple[int, dict[str, object]]:
    """Compile the skeleton, then organize notes and compile the final document.

    Faithful mode runs grouped T/V and one paper-wide Fusion. The earlier profile
    explicitly retains its single-group Gate and supplement for historical runs.
    """

    unavailable = unconnected_literature_options(
        route_group_id=route_group_id,
        publish=publish,
        source_manifest=source_manifest,
        previous_publication=previous_publication,
        execution_id=execution_id,
        si_packages=si_packages,
        machine_profile=machine_profile,
    )
    if unavailable is not None:
        return unavailable

    if not isinstance(package_dir, Path) or not isinstance(workspace, Path):
        raise TypeError("package_dir and workspace must be pathlib.Path values")
    if not isinstance(execution_id, str) or not execution_id.strip():
        raise ValueError("execution_id must be a nonempty string")
    assert route_group_id is not None or machine_profile.faithful_mapping
    try:
        package = canonicalize_paper_package(package_dir)
    except PackageLoadError as error:
        return 1, _failure_payload(error.code, str(error), execution_id=execution_id)
    # The paper's sources, once, before any model runs (E83): MAIN as the Router bound
    # it and every registered SI. What the Host knows from them -- whether an SI is
    # registered, whether the main text has an experimental part (the Router's sealed
    # cut, else the heading test) -- is told to the route group and written into the
    # published result, rather than guessed by the model in either place.
    try:
        handoffs = _router_handoffs(
            workspace=workspace, package=package, route_execution_id=route_execution_id
        )
    except (OSError, ValueError, LocalRuntimeError) as error:
        return 1, _failure_payload(
            FailureCode.PACKAGE_INVALID,
            "The sealed Router execution could not be verified.",
            execution_id=execution_id,
            detail=_diagnostic(error),
        )
    # A paper the Router rejected, or one whose routes all target what the corpus
    # does not collect, does not enter the chain (E86, Router v1.4): no lane runs, no
    # execution is sealed under this id, and the run says so with its own return code
    # (3: the paper's outcome, not a failure) and the row the review queue holds.
    excluded = corpus_exclusion(handoffs.routing)
    if handoffs.routing.disposition is RoutingDisposition.REJECTED or excluded is not None:
        row = review_queue_row(handoffs.routing, package.manifest)
        assert row is not None
        verdict = "ROUTER_REJECTED" if excluded is None else "OUT_OF_CORPUS"
        return 3, {
            "execution_id": execution_id,
            "status": verdict,
            "code": verdict,
            "message": (
                "The Router rejected this paper; it is in the human review queue and "
                "does not enter the literature chain."
                if excluded is None
                else excluded + "; the paper is in the review queue."
            ),
            "route_execution_id": route_execution_id,
            "review_queue_row": {
                key: value
                for key, value in row.as_record().items()
                if key not in {"queued_at", "reviewed_by", "review_disposition", "reviewed_at"}
            },
        }
    try:
        sources = load_literature_sources(
            main_package=package,
            main_root=package_dir,
            main_revision=document_revision_id,
            manifest_path=source_manifest,
            si_roots=si_packages,
        )
        source_status = PaperSourceStatus(
            si_registered=sources.si_registered,
            has_experimental_part=_main_has_experimental_part(handoffs),
        )
    except (OSError, ValueError, LocalRuntimeError) as error:
        return 1, _failure_payload(
            FailureCode.PACKAGE_INVALID,
            "The registered sources could not be verified.",
            execution_id=execution_id,
            detail=_diagnostic(error),
        )
    payload_sources: dict[str, object] = {
        "si_registered": source_status.si_registered,
        "main_has_experimental_part": source_status.has_experimental_part,
    }
    try:
        paper = run_literature_paper(
            workspace=workspace,
            package=package,
            package_root=package_dir,
            route_execution_id=route_execution_id,
            execution_id=execution_id,
            document_revision_id=document_revision_id,
            route_group_ids=(route_group_id,) if route_group_id is not None else (),
            provider_binding=provider_binding,
            text_client=text_client,
            visual_client=visual_client,
            gate_client=gate_client,
            provider_is_live=provider_is_live,
            live=live,
            source_status=source_status,
            machine_profile=machine_profile,
        )
    except LocalRuntimeError as error:
        return 1, _failure_payload(
            error.code,
            "The local literature execution evidence could not be verified.",
            execution_id=execution_id,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return 1, _failure_payload(
            FailureCode.PACKAGE_INVALID,
            "The literature route group inputs or runtime configuration are invalid.",
            execution_id=execution_id,
            detail=_diagnostic(error),
        )
    if machine_profile.faithful_mapping:
        compilation = paper.receipt.compilation
        payload: dict[str, object] = {
            "execution_id": execution_id, "status": ExecutionStatus.SUCCEEDED.value,
            "source_status": payload_sources, "route_execution_id": route_execution_id,
            "route_group_ids": [group.receipt.route_group_id for group in paper.groups],
            "group_failures": dict(paper.receipt.group_failures),
            "paper_receipt_artifact": paper.receipt_ref.model_dump(mode="json"),
            "model_delivery": compilation.answer.model_delivery,
            "execution_stop": compilation.failure or compilation.closed_out,
        }
    else:
        failure = paper.group_failures.get(route_group_id)
        if failure is not None:
            # The runtime's typed failure names its cause and phase; a payload that dropped
            # them cost diagnosis cycles against a live provider before.
            detail = "; ".join(f"{item.key}={item.value}" for item in failure.details) or None
            return (
                3 if failure.code is FailureCode.LIVE_REQUIRED else 1,
                _failure_payload(
                    failure.code, failure.message, execution_id=execution_id, detail=detail
                ),
            )
        (outcome,) = paper.groups
        receipt = outcome.receipt
        assert outcome.stage.output_artifact is not None
        payload: dict[str, object] = {
            "execution_id": execution_id,
            "status": ExecutionStatus.SUCCEEDED.value,
            "source_status": payload_sources,
            "stage_status": outcome.stage.status.value,
            "route_execution_id": receipt.route_execution_id,
            "route_group_id": receipt.route_group_id,
            "group_execution_id": receipt.literature_execution_id,
            "work_unit_id": receipt.work_unit_id,
            "evidence_entry_count": receipt.evidence_entry_count,
            "investigation_disposition": receipt.gate_decision,
            "saved_child_handoff_count": len(receipt.child_work_unit_refs),
            "receipt_artifact": outcome.stage.output_artifact.model_dump(mode="json"),
            "paper_receipt_artifact": paper.receipt_ref.model_dump(mode="json"),
            # Historical: every handoff the group sealed used to run as a reaction window,
            # decided or failed. Retired 2026-09-23; a fresh receipt's list is always empty.
            "windows": [
                {
                    "window_execution_id": window.window_execution_id,
                    "child_handoff_sha256": window.child_handoff_sha256,
                    "scope": f"{window.input_label} -> {window.output_label}",
                    "gate_decision": window.gate_decision,
                    "failure_code": window.failure_code,
                    "saved_child_handoff_count": window.child_work_unit_count,
                }
                for window in paper.receipt.reaction_window_outcomes
            ],
            "pending_work_unit_count": len(paper.receipt.pending_work_unit_refs),
        }
    published: PublishedPaperFactSet | None = None
    if publish:
        payload["publication"], published = _publication_block(
            workspace=workspace,
            package=package,
            package_dir=package_dir,
            paper=paper,
            sources=sources,
            source_status=source_status,
            profile=machine_profile,
            provider_binding=provider_binding,
        )
    detail_configured = detail_client is not None or detail_clients is not None
    if supplement_client is None and (not detail_configured or published is None):
        return 0, payload
    # Each SI read into units; its cut (E81) is derived from them, and the units
    # themselves wait for the detail lane's candidates (E136, batch two).
    si_units, stopped = _read_si_sources(
        workspace=workspace,
        sources=sources,
        execution_id=execution_id,
        units_client=units_client,
        provider_is_live=provider_is_live,
        live=live,
    )
    si_partitions = {
        revision: None if units is None else units.partition()
        for revision, units in si_units.items()
    }
    payload["si_sources"] = _si_block(sources, si_partitions)
    if stopped is not None:
        code, failure_payload = stopped
        failure_payload["route_group"] = payload
        return code, failure_payload
    # The supplement's narrative reading feeds the enriched revision, not the baseline
    # the detail round is dispatched against, so it runs beside the round rather than
    # ahead of it: on f07 it held the wall clock alone for 9.6 minutes (E144 ⑤). Its
    # client is its own (never the detail tasks'), and the runner already carries a
    # round of concurrent executions.
    call_budget = None
    if investigation_call_limit is not None:
        task_ids = [execution_id + "-supplement", execution_id + "-reading"]
        if machine_profile.unified_expression:
            task_ids.append(execution_id + "-recording")
        if machine_profile.faithful_mapping:
            if published is None:
                task_ids.append(execution_id + '-mapping')
            else:
                task_ids.extend(task.attempt_id for task in _mapping_tasks(
                    execution_id, published.machine, per_block=detail_clients is not None))
        if published is not None:
            task_ids.extend(
                task.attempt_id
                for task in _detail_tasks(
                    execution_id, published.machine, per_block=detail_clients is not None
                )
            )
        call_budget = CallBudget(
            investigation_call_limit,
            workspace=workspace,
            execution_ids=tuple(task_ids + [task + "-R1" for task in task_ids]),
        )
    supplement_future: Future[SealedInvestigation] | None = None
    supplement_pool: ThreadPoolExecutor | None = None
    if supplement_client is not None and not machine_profile.faithful_mapping:
        supplement_pool = ThreadPoolExecutor(max_workers=1)
        supplement_future = supplement_pool.submit(
            run_literature_investigation,
            workspace=workspace,
            sources=sources,
            router_execution_id=route_execution_id,
            execution_id=execution_id + "-supplement",
            kind="SUPPLEMENTAL_INFORMATION",
            provider_binding=provider_binding,
            client=supplement_client,
            figure_client=figure_client,
            call_budget=call_budget,
            provider_is_live=provider_is_live,
            live=live,
            first_layer_refs=(outcome.stage.output_artifact,),
        )
    try:
        return _enrich_after_publication(
            workspace=workspace,
            package=package,
            route_execution_id=route_execution_id,
            execution_id=execution_id,
            payload=payload,
            paper=paper,
            published=published,
            supplement_future=supplement_future,
            detail_client=detail_client,
            detail_clients=detail_clients,
            figure_client=figure_client,
            call_budget=call_budget,
            figure_clients=figure_clients,
            provider_binding=provider_binding,
            provider_is_live=provider_is_live,
            live=live,
            sources=sources,
            si_units=si_units,
            machine_profile=machine_profile,
        )
    except BudgetPaused:
        return _paused_investigation(execution_id, payload, call_budget)
    finally:
        if supplement_pool is not None:
            supplement_pool.shutdown(wait=True)


def _paused_investigation(
    execution_id: str,
    payload: dict[str, object],
    call_budget: CallBudget | None,
) -> tuple[int, dict[str, object]]:
    assert call_budget is not None
    return 3, {
        "status": "PAUSED",
        "reason": "INVESTIGATION_CALL_BUDGET",
        "execution_id": execution_id,
        "call_limit": call_budget.limit,
        "calls_used": call_budget.used,
        "resume": "Use the same execution, inputs and resources with an adequate total call limit.",
        "route_group": payload,
    }


def _settle_supplement(
    future: Future[SealedInvestigation] | None,
    *,
    execution_id: str,
    payload: dict[str, object],
) -> tuple[SealedInvestigation | None, tuple[int, dict[str, object]] | None]:
    """The supplement's sealed receipt once it is in, or the failure the chain reports.

    The lane ran and failed: the chain says so under the lane's own code, with the
    publication it already made still in the payload (E63).
    """
    if future is None:
        return None, None
    try:
        supplement = future.result()
    except InvestigationFailed as error:
        return None, (
            1,
            _failure_payload(
                error.failure.code,
                "The supplement investigation failed; its receipt is sealed.",
                execution_id=execution_id,
                detail=error.failure.message,
                route_group=payload,
            ),
        )
    except LocalRuntimeError as error:
        return None, (
            1,
            _failure_payload(
                error.code,
                "The local supplement investigation evidence could not be verified.",
                execution_id=execution_id,
                route_group=payload,
            ),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return None, (
            1,
            _failure_payload(
                FailureCode.PACKAGE_INVALID,
                "The supplement investigation inputs or runtime configuration are invalid.",
                execution_id=execution_id,
                detail=_diagnostic(error),
                route_group=payload,
            ),
        )
    payload["supplement"] = _investigation_block(supplement)
    return supplement, None


def _enrich_after_publication(
    *,
    workspace: Path,
    package: CanonicalPaperPackage,
    route_execution_id: str,
    execution_id: str,
    payload: dict[str, object],
    paper: LiteraturePaperRun,
    published: PublishedPaperFactSet | None,
    supplement_future: Future[SealedInvestigation] | None,
    detail_client: LaneInteractionClient | None,
    detail_clients: Callable[[], LaneInteractionClient] | None,
    figure_client: LaneInteractionClient | None,
    figure_clients: Callable[[], LaneInteractionClient] | None,
    provider_binding: ProviderBinding,
    provider_is_live: bool,
    live: bool,
    sources: LiteratureSources,
    si_units: Mapping[str, ReadingUnits | None],
    machine_profile: MachineProfile = MachineProfile.V1,
    call_budget: CallBudget | None = None,
) -> tuple[int, dict[str, object]]:
    """Run the detail lane over the experimental sources and publish the next revision.

    Without a publication there are no containers to enrich, so the supplement's
    receipt stands on its own (as before) and nothing else runs. With one, the detail
    lane runs when it has a client, and whatever receipts are new -- the detail's, the
    supplement's -- become machine revision 2 of this run's own baseline.
    """
    if published is None:
        _, failed = _settle_supplement(
            supplement_future, execution_id=execution_id, payload=payload
        )
        return failed if failed is not None else (0, payload)
    store = LocalObjectStore(workspace)
    details: tuple[SealedInvestigation, ...] = ()
    record_receipts: tuple[SealedInvestigation, ...] = ()
    # The round's other half: the tasks that sealed no receipt. Declared beside
    # `details` because a run with no detail round at all still publishes, and both
    # halves have to mean "nothing happened" rather than "not defined here".
    failed_details: list[dict[str, object]] = []
    try:
        sources, experimental_block_count = _experimental_sources(
            workspace=workspace,
            package=package,
            route_execution_id=route_execution_id,
            sources=sources,
            si_units=si_units,
        )
        if detail_client is not None or detail_clients is not None:
            if (experimental_block_count == 0 and not sources.active_si
                    and not machine_profile.faithful_mapping):
                # The same shape a round reports, with no tasks in it: one key with
                # two meanings is how a reader (and `export_detail_task`) gets it wrong.
                payload["experimental_detail"] = {
                    "status": "NO_EXPERIMENTAL_SOURCES",
                    "experimental_block_count": 0,
                    "si_document_count": 0,
                    "task_count": 0,
                    "failed_task_count": 0,
                    "empty_task_count": 0,
                    "turn_total": 0,
                    "thought_total": 0,
                    "model_seconds_total": 0.0,
                    "finding_total": 0,
                    "read_error_total": 0,
                    "tasks": [],
                }
            else:
                context = {} if machine_profile.faithful_mapping else dict(experimental_checkpoint(store, paper.receipt, package))
                if published.skeleton_groups:
                    context["groups"] = list(published.skeleton_groups)
                context["original_main_baseline_ref"] = dict(published.machine_artifact_ref)
                context["existing_public_containers"] = {
                    key: published.machine["domain_payload"][key]  # type: ignore[index]
                    for key in ("compounds", "reaction_blocks", "routes")
                }
                context["existing_observations"] = list(published.observations)
                context["observation_provenance"] = dict(published.observation_provenance)
                # Which published step each fusion fact became: what lets a block task
                # be given the questions about its own step instead of the route's
                # (`project_detail_group_context`). Private to the Host and never part
                # of the public result.
                context["material_sources"] = dict(published.material_sources)
                context["block_provenance"] = {
                    group_id: dict(mapping)
                    for group_id, mapping in published.block_provenance.items()
                }

                # One experimental-detail task that fails is that task's failure, not the
                # paper's: the route is published with what the others found and the
                # receipt says which task failed and why. The whole enrichment used to be
                # abandoned here (`return 1`, nothing published) -- tolerable while there
                # was one task for the paper, self-inflicted once there is one per step.
                # This is how a reaction window used to fail, back when they ran
                # (`literature_paper_facts` recorded the window's own failure_code and the
                # paper still published; retired 2026-09-23).
                def investigate(
                    attempt_id: str,
                    scope: tuple[str, ...] | None,
                    client: LaneInteractionClient,
                    figure: LaneInteractionClient | None,
                    extra: Mapping[str, Any] | None = None,
                    *,
                    kind: str = "EXPERIMENTAL_DETAIL",
                    paragraph_receipts: tuple[SealedInvestigation, ...] = (),
                    record_receipt: SealedInvestigation | None = None,
                ) -> SealedInvestigation:
                    if call_budget is not None:
                        call_budget.check_running()
                    return run_literature_investigation(
                        workspace=workspace,
                        # A task may also read the paragraphs its steps were built
                        # from, wherever they stand (E142); a task over the whole route
                        # -- the paper's, the middle layer -- every paragraph the route
                        # cites (2A, 2026-09-25).
                        # Mapping tasks share the same verified note/source tables.
                        # Their assignment changes write scope, not source grants.
                        sources=sources if kind == 'EXPERIMENTAL_MAPPING' else readable_with_step_sources(
                            sources, published.machine, published.observations, scope
                        ),
                        router_execution_id=route_execution_id,
                        execution_id=attempt_id,
                        kind=kind,
                        provider_binding=provider_binding,
                        client=client,
                        figure_client=figure,
                        call_budget=call_budget,
                        provider_is_live=provider_is_live,
                        live=live,
                        baseline_machine_ref=resolve_public_ref(
                            store, published.machine_artifact_ref
                        ),
                        machine=dict(published.machine),
                        observations=tuple(dict(o) for o in published.observations),
                        context={**context, **(extra or {})},
                        block_ids=scope,
                        # Whether one task per step is running beside this one. The
                        # paper's own task keeps what no single step owns and gives up
                        # what its neighbours cover; a round that is one task keeps
                        # everything, because there are no neighbours (E112).
                        shares_the_round=detail_clients is not None,
                        paragraph_receipts=paragraph_receipts,
                        record_receipt=record_receipt,
                    )

                def run_task(
                    task: _DetailTask,
                    extra: Mapping[str, Any] | None = None,
                    *,
                    paragraph_receipts: tuple[SealedInvestigation, ...] = (),
                    record_receipt: SealedInvestigation | None = None,
                ) -> _DetailOutcome:
                    """One task; audited request failures do not restart its work.

                    A task that fails is that task's failure. The route publishes with
                    what the other tasks found and the receipt says which task failed
                    and why -- the whole enrichment used to be abandoned here, which was
                    tolerable with one task for a paper and self-inflicted with one per
                    step. This is how a reaction window used to fail, back when they ran.
                    """
                    client = detail_clients() if detail_clients is not None else detail_client
                    assert client is not None
                    figure = figure_clients() if figure_clients is not None else figure_client
                    try:
                        return _DetailOutcome(
                            task,
                            investigate(
                                task.attempt_id,
                                task.scope,
                                client,
                                figure,
                                extra,
                                kind=task.kind,
                                paragraph_receipts=paragraph_receipts,
                                record_receipt=record_receipt,
                            ),
                            None,
                        )
                    except InvestigationFailed as error:
                        first = _verified_failed_receipt(
                            workspace,
                            error,
                            execution_id=task.attempt_id,
                            kind=task.kind,
                            sources=sources,
                        )
                        failed_reads = (first,) if first is not None else ()
                        # The adapter owns retries of the unchanged request. A verified
                        # terminal audit must not buy another whole conversation here.
                        # The historical one-task retry remains only for providers
                        # without a conclusive audit; Host/model refusals never retry.
                        if failure_category(
                            error.failure.code
                        ) is not FailureCategory.PROVIDER or _request_retry_is_terminal(
                            workspace, error, execution_id=task.attempt_id
                        ):
                            # A capacity stop occurs before the next request, but may
                            # follow accepted writes. Carry those verified facts with
                            # their FAILED terminal and limitation; do not retry or
                            # manufacture a model completion.
                            saved = None
                            if (
                                error.failure.code is FailureCode.CONTEXT_TOO_LARGE
                                and first is not None
                                and first.raw_model() is not None
                            ):
                                saved = SealedInvestigation(
                                    first,
                                    error.receipt_ref,
                                    LocalRunner(
                                        workspace, LocalObjectStore(workspace)
                                    ).finish_execution(
                                        first.execution_id, ExecutionStatus.FAILED_TERMINAL
                                    ),
                                )
                            return _DetailOutcome(task, saved, error.failure, failed_reads)
                        retry = detail_clients() if detail_clients is not None else client
                        again_figure = figure_clients() if figure_clients is not None else figure
                        try:
                            return _DetailOutcome(
                                task,
                                investigate(
                                    task.attempt_id + "-R1",
                                    task.scope,
                                    retry,
                                    again_figure,
                                    extra,
                                    kind=task.kind,
                                    paragraph_receipts=paragraph_receipts,
                                record_receipt=record_receipt,
                                ),
                                None,
                                failed_reads,
                            )
                        except InvestigationFailed as again:
                            second = _verified_failed_receipt(
                                workspace,
                                again,
                                execution_id=task.attempt_id + "-R1",
                                kind=task.kind,
                                sources=sources,
                            )
                            return _DetailOutcome(
                                task,
                                None,
                                again.failure,
                                (*failed_reads, *((second,) if second is not None else ())),
                            )
                        except BudgetPaused:
                            raise
                        except Exception as again:  # noqa: BLE001 -- see below
                            return _DetailOutcome(task, None, _task_failure(again), failed_reads)
                    except BudgetPaused:
                        raise
                    except Exception as error:  # noqa: BLE001
                        # Everything else this task could raise is still only this task's.
                        # The lane raises a bare ValueError when it cannot seal its own
                        # execution, and the local runtime raises its own error type; one
                        # of those escaping the pool would be caught by the paper-level
                        # handler below and throw away the sealed work of every other
                        # task in the round -- which is the failure this dispatch exists
                        # to end, made nineteen times more likely by there being nineteen
                        # tasks.
                        return _DetailOutcome(task, None, _task_failure(error))

                tasks = (() if experimental_block_count == 0 and not sources.active_si else
                    _detail_tasks(execution_id, published.machine, per_block=detail_clients is not None))
                paper_task: dict[str, Any]
                if not tasks:
                    outcomes = []
                    paper_task = {"dispatched": False, "after_steps": False}
                elif detail_clients is None:
                    outcomes = [run_task(task) for task in tasks]
                    paper_task = {"dispatched": True, "after_steps": False}
                else:
                    # One client per task, and no cap: the step tasks of a round are
                    # independent and the gateway takes them in parallel. A shared
                    # client could not be used concurrently in any case -- reconnecting
                    # replaces the transport the other tasks are mid-stream on. The
                    # paper's own task comes after them, for what they left (E144).
                    steps, papers_own = tasks[:-1], tasks[-1]
                    if steps:
                        with ThreadPoolExecutor(max_workers=len(steps)) as pool:
                            outcomes = list(pool.map(run_task, steps))
                    else:
                        outcomes = []
                    # A faithful task receives actual upstream records, never a
                    # Host-generated scientific remainder based on read/citation counts.
                    left = None if machine_profile.faithful_mapping else round_left(published.machine, sources, outcomes)
                    paper_task = {
                        "dispatched": True,
                        "after_steps": True,
                        "left": {k: len(v) for k, v in (left or {}).items()},
                    }
                    outcomes.append(run_task(papers_own, {} if machine_profile.faithful_mapping else {"round_left": left or {}}))
                paragraphs = tuple(o.sealed for o in outcomes if o.sealed is not None)
                paragraph_task_count = len(outcomes)
                recording_outcome = None
                reading_outcome = None
                if machine_profile.unified_expression:
                    recording_outcome = run_task(
                        _DetailTask(execution_id + "-recording", None, "EXPERIMENTAL_RECORDING"),
                        paragraph_receipts=paragraphs,
                    )
                    outcomes.append(recording_outcome)
                    sealed_record = recording_outcome.sealed
                    if (sealed_record is not None and recording_outcome.failure is None
                            and sealed_record.receipt.failure is None
                            and sealed_record.receipt.closed_out is None):
                        # Verify explicit model submission and the whole evidence handoff
                        # before scheduling another model task. Nonempty records alone
                        # never turn a host stop into a scientific finish.
                        from tsdaf.workflow.experimental_record_handoff import verify_record_handoff
                        verify_record_handoff(sealed_record, store=store,
                            runner=LocalRunner(workspace, store), sources=sources,
                            baseline_ref=resolve_public_ref(store, published.machine_artifact_ref))
                        record_receipts = (sealed_record,)
                mapping_outcomes = []
                if machine_profile.faithful_mapping and record_receipts:
                    mapping_tasks = _mapping_tasks(execution_id, published.machine,
                                                   per_block=detail_clients is not None)
                    def run_mapping(task):
                        return run_task(task,
                            {'mapping_assignment':list(task.scope or ())} if detail_clients is not None else None,
                            paragraph_receipts=paragraphs, record_receipt=record_receipts[0])
                    if detail_clients is not None and len(mapping_tasks) > 1:
                        with ThreadPoolExecutor(max_workers=len(mapping_tasks)-1) as pool:
                            mapping_outcomes = list(pool.map(run_mapping, mapping_tasks[:-1]))
                    mapping_outcomes.append(run_mapping(mapping_tasks[-1]))
                    outcomes.extend(mapping_outcomes)
                    reading_outcome = mapping_outcomes[-1]
                elif not machine_profile.unified_expression or record_receipts:
                    reading_outcome = run_task(
                        _DetailTask(execution_id + ("-mapping" if machine_profile.faithful_mapping else "-reading"),
                                    None, "EXPERIMENTAL_MAPPING" if machine_profile.faithful_mapping else "EXPERIMENTAL_READING"),
                        paragraph_receipts=paragraphs,
                        record_receipt=record_receipts[0] if record_receipts else None,
                    )
                    outcomes.append(reading_outcome)
                details = tuple(o.sealed for o in outcomes
                    if o.sealed is not None and o.task.kind != "EXPERIMENTAL_RECORDING")
                failed_details = [
                    {
                        "reaction_block_ids": list(o.task.scope) if o.task.scope else None,
                        "failure_code": o.failure.code.value if o.failure is not None else None,
                    }
                    for o in outcomes
                    if o.sealed is None
                ]
                # The round's own totals. Every acceptance number of the detail-lane
                # rework -- turns spent, findings delivered, reads refused, tasks that
                # came back with nothing -- is read off this block, so a run can be
                # compared with the one before it without a script over the receipts.
                payload["experimental_detail"] = {
                    "experimental_block_count": experimental_block_count,
                    "si_document_count": len(sources.active_si),
                    "task_count": len(outcomes),
                    "paper_task": paper_task,
                    "paragraph_task_count": paragraph_task_count,
                    "recording_task": recording_outcome.as_record() if recording_outcome else None,
                    "reading_task": reading_outcome.as_record() if reading_outcome else None,
                    **({"mapping_tasks": [o.as_record() for o in mapping_outcomes]}
                       if machine_profile.faithful_mapping else {}),
                    "failed_task_count": sum(1 for o in outcomes if o.failure is not None),
                    "empty_task_count": sum(1 for o in outcomes if o.delivered_nothing()),
                    "turn_total": sum(o.counts()["turn_count"] for o in outcomes),
                    # Thinking per second is what tells a slow gateway from a slower
                    # harness, and it is the one number a wall clock cannot be read for:
                    # the e108 fusion phase looked 1.69x slower than e106 while doing
                    # less work, and the whole of it was the rate (183 -> 121 tok/s).
                    "thought_total": sum(o.counts()["thought_tokens"] for o in outcomes),
                    "model_seconds_total": round(
                        sum(o.counts()["model_seconds"] for o in outcomes), 1
                    ),
                    "finding_total": sum(o.counts()["finding_count"] for o in outcomes),
                    "read_error_total": sum(o.counts()["read_error_count"] for o in outcomes),
                                "tasks": [o.as_record() for o in outcomes],
                }
        if call_budget is not None:
            call_budget.check_running()
        supplement, failed = _settle_supplement(
            supplement_future, execution_id=execution_id, payload=payload
        )
        if failed is not None:
            return failed
        if not details and not record_receipts and supplement is None:
            return 0, payload
        enriched = publish_enriched_revision(
            workspace=workspace,
            sources=sources,
            baseline=published,
            details=details,
            record_receipts=record_receipts,
            narrative=supplement,
            # A task that failed sealed no receipt, so it used to reach the publisher as
            # nothing at all -- and a round where no task ran looked the same as a round
            # whose tasks found nothing.
            failed_details=failed_details,
            profile=machine_profile,
        )
    except BudgetPaused:
        if supplement_future is not None:
            try:
                _, failed = _settle_supplement(
                    supplement_future, execution_id=execution_id, payload=payload
                )
                if failed is not None:
                    # Its diagnostic already refers to payload; avoid a self-cycle.
                    payload["supplement_failure"] = {
                        key: value for key, value in failed[1].items() if key != "route_group"
                    }
            except BudgetPaused:
                pass
        return _paused_investigation(execution_id, payload, call_budget)
    except InvestigationFailed as error:
        return 1, _failure_payload(
            error.failure.code,
            "The experimental detail investigation failed; its receipt is sealed.",
            execution_id=execution_id,
            detail=error.failure.message,
            route_group=payload,
        )
    except LocalRuntimeError as error:
        return 1, _failure_payload(
            error.code,
            "The local enrichment evidence could not be verified.",
            execution_id=execution_id,
            route_group=payload,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return 1, _failure_payload(
            FailureCode.PACKAGE_INVALID,
            "The enrichment inputs or runtime configuration are invalid.",
            execution_id=execution_id,
            detail=_diagnostic(error),
            route_group=payload,
        )
    publication = payload["publication"]
    assert isinstance(publication, dict)
    publication["enriched"] = {
        "receipt_artifact": enriched.receipt_ref.model_dump(mode="json"),
        **_published_counts(
            enriched.published,
            baseline=published,
            packages=tuple(d.package for d in sources.documents if d.package is not None),
        ),
    }
    return 0, payload
