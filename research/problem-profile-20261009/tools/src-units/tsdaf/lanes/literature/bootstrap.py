"""Verification of sealed Router handoffs before literature parsing starts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Self, TypeVar

from pydantic import field_validator, model_validator

from tsdaf.contracts._base import ContractModel, _sha256, _stable_string
from tsdaf.contracts.execution import ExecutionStatus, StageResult
from tsdaf.contracts.handoff import CoarseTextHandoff, VisualRoutingHandoff
from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.partition import EXPERIMENTAL_PARTITION_MEDIA_TYPE, ExperimentalPartition
from tsdaf.contracts.reading_units import READING_UNITS_MEDIA_TYPE, ReadingUnits
from tsdaf.contracts.routing import ArticleRoutingResult
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.host_ports import (
    ExecutionClosureVerifier,
    VerifiedArtifactStore,
)

__all__ = ("LiteratureBootstrapHandoff", "VerifiedRouterHandoffs", "verify_router_handoff_bundle")

_ROUTER_STAGE_NAME = "main-article-router"
_ROUTING_MEDIA_TYPE = "application/vnd.tsdaf.article-routing-result+json"
_VISUAL_HANDOFF_MEDIA_TYPE = "application/vnd.tsdaf.visual-routing-handoff+json"
_COARSE_HANDOFF_MEDIA_TYPE = "application/vnd.tsdaf.coarse-text-handoff+json"
_STAGE_RESULT_MEDIA_TYPE = "application/vnd.tsdaf.stage-result+json"
_EVENT_LOG_MEDIA_TYPE = "application/x-ndjson"
_ModelT = TypeVar("_ModelT", bound=ContractModel)


class LiteratureBootstrapHandoff(ContractModel):
    """Private immutable seed bundle; it makes no full-coverage claim."""

    schema_version: Literal["tsdaf.literature-bootstrap-handoff.v1"]
    bootstrap_id: str
    paper_id: str
    package_sha256: str
    router_execution_id: str
    router_stage_name: Literal["main-article-router"]
    router_stage_version: str
    router_attempt_id: str | None
    router_content_key: str
    router_terminal_manifest_ref: ArtifactRef
    router_event_log_ref: ArtifactRef
    routing_result_ref: ArtifactRef
    coarse_text_handoff_ref: ArtifactRef
    visual_routing_handoff_ref: ArtifactRef
    coarse_seed_block_ids: tuple[str, ...]
    visual_seed_ids: tuple[str, ...]
    bootstrap_sha256: str

    @field_validator(
        "bootstrap_id",
        "paper_id",
        "router_execution_id",
        "router_stage_version",
    )
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _stable_string(value, label="literature bootstrap identity")

    @field_validator("router_attempt_id")
    @classmethod
    def _validate_attempt(cls, value: str | None) -> str | None:
        if value is not None:
            return _stable_string(value, label="literature bootstrap attempt id")
        return value

    @field_validator("package_sha256", "router_content_key", "bootstrap_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256(value, label="literature bootstrap hash")

    @model_validator(mode="after")
    def _validate_projection(self) -> Self:
        projection = self.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
            exclude={"bootstrap_id", "bootstrap_sha256"},
        )
        expected = hashlib.sha256(canonical_json_bytes(projection)).hexdigest()
        if self.bootstrap_sha256 != expected or self.bootstrap_id != f"LBOOT-{expected}":
            raise ValueError("literature bootstrap identity does not match its projection")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedRouterHandoffs:
    """One closure verification, reused by source compilation and catalogue projection."""

    bootstrap: LiteratureBootstrapHandoff
    routing: ArticleRoutingResult
    visual: VisualRoutingHandoff
    coarse: CoarseTextHandoff
    # The experimental partition the Router was narrowed by, verified against the same
    # package; None for a Router execution sealed without one (ledger E47). Derived from
    # the reading units when the execution sealed those (E136), read as sealed when it
    # was the retired split stage that ran.
    partition: ExperimentalPartition | None = None
    # The reading units themselves, when the execution sealed them: what the detail
    # lane's candidates are drawn from (E136, batch two). None on older executions.
    units: ReadingUnits | None = None


def _read_ref_bound_bytes(store: VerifiedArtifactStore, ref: ArtifactRef) -> bytes:
    payload = store.read_bytes(ref)
    if type(payload) is not bytes or len(payload) != ref.byte_size:
        raise ValueError("Router artifact payload has the wrong byte size")
    if hashlib.sha256(payload).hexdigest() != ref.sha256:
        raise ValueError("Router artifact payload has the wrong SHA-256")
    return payload


def _canonical_model(
    store: VerifiedArtifactStore,
    ref: ArtifactRef,
    model_type: type[_ModelT],
) -> _ModelT:
    payload = _read_ref_bound_bytes(store, ref)
    model = model_type.model_validate_json(payload)
    expected = canonical_json_bytes(
        model.model_dump(mode="json", by_alias=True, exclude_none=False)
    )
    if payload != expected:
        raise ValueError("Router handoff artifact is not canonical JSON")
    return model


def _canonical_json(store: VerifiedArtifactStore, ref: ArtifactRef) -> dict[str, object]:
    payload = _read_ref_bound_bytes(store, ref)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Router manifest artifact is not valid JSON") from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
        raise ValueError("Router manifest artifact is not canonical JSON")
    return value


def _event_rows(store: VerifiedArtifactStore, ref: ArtifactRef) -> tuple[dict[str, object], ...]:
    if ref.media_type != _EVENT_LOG_MEDIA_TYPE:
        raise ValueError("Router execution closure has the wrong event-log media type")
    payload = _read_ref_bound_bytes(store, ref)
    rows: list[dict[str, object]] = []
    for sequence, line in enumerate(payload.splitlines(keepends=True), start=1):
        if not line.endswith(b"\n"):
            raise ValueError("Router event log has a partial line")
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Router event log is invalid JSONL") from error
        if (
            not isinstance(row, dict)
            or canonical_json_bytes(row) + b"\n" != line
            or row.get("sequence") != sequence
        ):
            raise ValueError("Router event log is not canonical and ordered")
        rows.append(row)
    return tuple(rows)


def _artifact(value: object, *, label: str) -> ArtifactRef:
    try:
        return ArtifactRef.model_validate(value)
    except ValueError as error:
        raise ValueError(f"{label} is not a typed artifact reference") from error


def _stage_result(store: VerifiedArtifactStore, ref: ArtifactRef) -> StageResult:
    if ref.media_type != _STAGE_RESULT_MEDIA_TYPE:
        raise ValueError("Router stage result artifact has the wrong media type")
    result = _canonical_model(store, ref, StageResult)
    assert isinstance(result, StageResult)
    return result


def verify_router_handoff_bundle(
    *,
    package: CanonicalPaperPackage,
    router_execution_id: str,
    terminal_manifest_ref: ArtifactRef,
    object_store: VerifiedArtifactStore,
    runner: ExecutionClosureVerifier,
) -> VerifiedRouterHandoffs:
    """Verify one exact sealed Router event and retain its verified typed artifacts."""

    if type(package) is not CanonicalPaperPackage:
        raise TypeError("package must be a CanonicalPaperPackage")
    if not isinstance(router_execution_id, str) or not router_execution_id.strip():
        raise ValueError("router_execution_id must be a nonempty string")
    if not isinstance(terminal_manifest_ref, ArtifactRef):
        raise TypeError("terminal_manifest_ref must be an ArtifactRef")
    if not isinstance(object_store, VerifiedArtifactStore) or not isinstance(
        runner, ExecutionClosureVerifier
    ):
        raise TypeError("object_store and runner must provide verified local evidence reads")

    closure = runner.verified_execution_artifact_closure(router_execution_id, terminal_manifest_ref)
    manifest = _canonical_json(object_store, terminal_manifest_ref)
    if (
        manifest.get("schema_version") != "tsdaf.local-execution-manifest.v1"
        or manifest.get("record_kind") != "terminal_execution_manifest"
        or manifest.get("execution_id") != router_execution_id
        or manifest.get("status") != ExecutionStatus.SUCCEEDED.value
    ):
        raise ValueError("Router terminal manifest has an invalid identity or status")
    event_log_ref = _artifact(manifest.get("event_log"), label="Router event log")
    if event_log_ref not in closure:
        raise ValueError("Router terminal manifest event log is outside execution closure")
    events = _event_rows(object_store, event_log_ref)
    handoff_events = tuple(
        event for event in events if event.get("event_type") == "handoffs_materialized"
    )
    if len(handoff_events) != 1:
        raise ValueError("Router execution must contain exactly one handoffs_materialized event")
    handoff_event = handoff_events[0]

    stage_records = manifest.get("stage_records")
    if not isinstance(stage_records, list):
        raise ValueError("Router terminal manifest has no stage records")
    router_records = tuple(
        record
        for record in stage_records
        if isinstance(record, dict) and record.get("stage_name") == _ROUTER_STAGE_NAME
    )
    if len(router_records) != 1:
        raise ValueError("Router terminal manifest must bind exactly one Router stage")
    stage_record = router_records[0]
    stage_ref = _artifact(stage_record.get("result_ref"), label="Router stage result")
    if stage_ref not in closure:
        raise ValueError("Router stage result is outside terminal execution closure")
    stage = _stage_result(object_store, stage_ref)
    if stage.status is ExecutionStatus.CACHED:
        raise ValueError("CACHED Router stage is not supported by bootstrap v1")
    if (
        stage.execution_id != router_execution_id
        or stage.stage_name != _ROUTER_STAGE_NAME
        or stage.stage_version != stage_record.get("stage_version")
        or stage.input_content_key != stage_record.get("content_key")
        or stage.status.value != stage_record.get("status")
        or stage.status is not ExecutionStatus.SUCCEEDED
        or stage.output_artifact is None
    ):
        raise ValueError("Router stage binding contradicts the terminal manifest")

    closure_identities = {(ref.sha256, ref.media_type, ref.byte_size) for ref in closure}
    package_bytes = canonical_json_bytes(
        package.model_dump(mode="json", by_alias=True, exclude_none=False)
    )
    package_identity = (
        hashlib.sha256(package_bytes).hexdigest(),
        "application/vnd.tsdaf.canonical-paper-package+json",
        len(package_bytes),
    )
    if package_identity not in closure_identities:
        raise ValueError("Router execution does not bind the supplied canonical package")
    if handoff_event.get("stage_name") != stage.stage_name:
        raise ValueError("handoff event stage binding does not match the Router stage")
    if handoff_event.get("content_key") != stage.input_content_key:
        raise ValueError("handoff event content key does not match the Router stage")
    if handoff_event.get("attempt_id") != stage.attempt_id:
        raise ValueError("handoff event attempt does not match the Router stage")
    if not events or events[-1].get("event_type") != "execution_finished":
        raise ValueError("Router event log is not terminal")
    if events.index(handoff_event) >= len(events) - 1:
        raise ValueError("handoff event is not sealed before execution termination")

    data = handoff_event.get("data")
    expected_keys = {
        "routing_result_artifact",
        "visual_handoff_artifact",
        "coarse_text_handoff_artifact",
        "visual_item_count",
        "coarse_text_item_count",
    }
    # Router executions sealed before either stage existed carry neither optional key;
    # those the split stage ran on carry `experimental_partition_artifact`, those the
    # reading-units stage ran on carry `reading_units_artifact` -- each null when its
    # stage did not run. One record, never both.
    optional_keys = {"reading_units_artifact", "experimental_partition_artifact"}
    if not isinstance(data, dict) or set(data) - optional_keys != expected_keys:
        raise ValueError("handoff materialization event payload is invalid")
    routing_ref = _artifact(data["routing_result_artifact"], label="routing result artifact")
    if routing_ref != stage.output_artifact:
        raise ValueError("routing result artifact is not the Router stage output")
    visual_ref = _artifact(data["visual_handoff_artifact"], label="visual handoff artifact")
    coarse_ref = _artifact(data["coarse_text_handoff_artifact"], label="coarse handoff artifact")
    if routing_ref.media_type != _ROUTING_MEDIA_TYPE:
        raise ValueError("routing result artifact has the wrong media type")
    if visual_ref.media_type != _VISUAL_HANDOFF_MEDIA_TYPE:
        raise ValueError("visual handoff artifact has the wrong media type")
    if coarse_ref.media_type != _COARSE_HANDOFF_MEDIA_TYPE:
        raise ValueError("coarse handoff artifact has the wrong media type")

    routing = _canonical_model(object_store, routing_ref, ArticleRoutingResult)
    visual = _canonical_model(object_store, visual_ref, VisualRoutingHandoff)
    coarse = _canonical_model(object_store, coarse_ref, CoarseTextHandoff)
    assert isinstance(routing, ArticleRoutingResult)
    assert isinstance(visual, VisualRoutingHandoff)
    assert isinstance(coarse, CoarseTextHandoff)
    routing.validate_against(package)
    visual.validate_against(package, routing)
    coarse.validate_against(package, routing)
    partition: ExperimentalPartition | None = None
    units: ReadingUnits | None = None
    raw_units_ref = data.get("reading_units_artifact")
    raw_partition_ref = data.get("experimental_partition_artifact")
    if raw_units_ref is not None and raw_partition_ref is not None:
        raise ValueError("handoff names both reading units and a sealed partition")
    if raw_units_ref is not None:
        units_ref = _artifact(raw_units_ref, label="reading units artifact")
        if units_ref.media_type != READING_UNITS_MEDIA_TYPE:
            raise ValueError("reading units artifact has the wrong media type")
        if units_ref not in closure:
            raise ValueError("reading units artifact is outside execution closure")
        loaded_units = _canonical_model(object_store, units_ref, ReadingUnits)
        assert isinstance(loaded_units, ReadingUnits)
        loaded_units.validate_against(package)
        units = loaded_units
        partition = loaded_units.partition()
    elif raw_partition_ref is not None:
        partition_ref = _artifact(raw_partition_ref, label="experimental partition artifact")
        if partition_ref.media_type != EXPERIMENTAL_PARTITION_MEDIA_TYPE:
            raise ValueError("experimental partition artifact has the wrong media type")
        if partition_ref not in closure:
            raise ValueError("experimental partition artifact is outside execution closure")
        loaded = _canonical_model(object_store, partition_ref, ExperimentalPartition)
        assert isinstance(loaded, ExperimentalPartition)
        loaded.validate_against(package)
        partition = loaded
    if routing.execution_id != router_execution_id:
        raise ValueError("routing result execution identity does not match terminal manifest")
    if data["visual_item_count"] != len(visual.items):
        raise ValueError("visual handoff count does not match its typed artifact")
    if data["coarse_text_item_count"] != len(coarse.items):
        raise ValueError("coarse handoff count does not match its typed artifact")

    coarse_seed_block_ids = tuple(item.block.block_id for item in coarse.items)
    visual_seed_ids = tuple(
        visual_id
        for route_group in visual.route_groups
        for visual_id in route_group.ordered_visual_ids
    )
    projection = {
        "schema_version": "tsdaf.literature-bootstrap-handoff.v1",
        "paper_id": package.manifest.paper_id,
        "package_sha256": package.canonical_content_sha256,
        "router_execution_id": router_execution_id,
        "router_stage_name": _ROUTER_STAGE_NAME,
        "router_stage_version": stage.stage_version,
        "router_attempt_id": stage.attempt_id,
        "router_content_key": stage.input_content_key,
        "router_terminal_manifest_ref": terminal_manifest_ref.model_dump(mode="json"),
        "router_event_log_ref": event_log_ref.model_dump(mode="json"),
        "routing_result_ref": routing_ref.model_dump(mode="json"),
        "coarse_text_handoff_ref": coarse_ref.model_dump(mode="json"),
        "visual_routing_handoff_ref": visual_ref.model_dump(mode="json"),
        "coarse_seed_block_ids": list(coarse_seed_block_ids),
        "visual_seed_ids": list(visual_seed_ids),
    }
    digest = hashlib.sha256(canonical_json_bytes(projection)).hexdigest()
    bootstrap = LiteratureBootstrapHandoff(
        schema_version="tsdaf.literature-bootstrap-handoff.v1",
        bootstrap_id=f"LBOOT-{digest}",
        paper_id=package.manifest.paper_id,
        package_sha256=package.canonical_content_sha256,
        router_execution_id=router_execution_id,
        router_stage_name="main-article-router",
        router_stage_version=stage.stage_version,
        router_attempt_id=stage.attempt_id,
        router_content_key=stage.input_content_key,
        router_terminal_manifest_ref=terminal_manifest_ref,
        router_event_log_ref=event_log_ref,
        routing_result_ref=routing_ref,
        coarse_text_handoff_ref=coarse_ref,
        visual_routing_handoff_ref=visual_ref,
        coarse_seed_block_ids=coarse_seed_block_ids,
        visual_seed_ids=visual_seed_ids,
        bootstrap_sha256=digest,
    )
    return VerifiedRouterHandoffs(bootstrap, routing, visual, coarse, partition, units)
