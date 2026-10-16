"""Close sealed initial lane responses into typed private proposal artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Self, TypeVar

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from tsdaf.contracts._base import ContractModel, _sha256, _stable_string
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.artifacts import seal_exact
from tsdaf.lanes.literature.host_ports import PrivateArtifactStore
from tsdaf.lanes.literature.lane_dispatch import (
    LaneResourceArtifacts,
    PairPreparationReceipt,
    RawInitialRouteLaneExchanges,
    RawLaneExchange,
    RawLaneInteractionRecord,
)
from tsdaf.lanes.literature.lane_models import TextReading, VisualReading
from tsdaf.lanes.literature.profiles import (
    STAGE_PROFILES,
    load_lane_resources,
)
from tsdaf.lanes.literature.provider_port import require_lane_model_profile
from tsdaf.lanes.literature.reader_identity import (
    READER_CACHE_OUTPUT_MEDIA,
    READER_REUSE_MEDIA,
    ReaderCacheOutput,
    ReaderReuseBinding,
    ReaderReuseVerifier,
    verify_reader_identity,
)

__all__ = (
    "RouteProposalProvenance",
    "SealedInitialRouteLaneProposals",
    "SealedTextProposal",
    "SealedVisualProposal",
    "seal_initial_route_lane_proposals",
    "verify_sealed_initial_route_lane_proposals",
)

_MEDIA_TYPES = {
    "T1": {
        "prompt": "text/vnd.tsdaf.literature-t1-prompt+markdown",
        "schema": "application/vnd.tsdaf.literature-t1-schema+json",
        "manifest": "application/vnd.tsdaf.literature-t1-resource-manifest+json",
        "request": "application/vnd.tsdaf.literature-t1-interaction-request+json",
        "response": "application/vnd.tsdaf.literature-t1-interaction-response+octets",
        "record": "application/vnd.tsdaf.literature-t1-raw-interaction-record+json",
        "proposal": "application/vnd.tsdaf.literature-t1-route-proposal+json",
    },
    "V1": {
        "prompt": "text/vnd.tsdaf.literature-v1-prompt+markdown",
        "schema": "application/vnd.tsdaf.literature-v1-schema+json",
        "manifest": "application/vnd.tsdaf.literature-v1-resource-manifest+json",
        "request": "application/vnd.tsdaf.literature-v1-interaction-request+json",
        "response": "application/vnd.tsdaf.literature-v1-interaction-response+octets",
        "record": "application/vnd.tsdaf.literature-v1-raw-interaction-record+json",
        "proposal": "application/vnd.tsdaf.literature-v1-route-proposal+json",
    },
}
_PREPARATION_MEDIA_TYPE = "application/vnd.tsdaf.literature-route-pair-preparation-receipt+json"
_PROVENANCE_MEDIA_TYPE = "application/vnd.tsdaf.literature-route-proposal-provenance+json"
_Stage = Literal["T1", "V1"]
_TEXT_STAGES: frozenset[str] = frozenset({"T1"})


class _NormalizedTextPart(ContractModel):
    type: Literal["text"]
    text: str = Field(min_length=1)


class _NormalizedModelOutputStep(ContractModel):
    type: Literal["model_output"]
    content: tuple[_NormalizedTextPart, ...] = Field(min_length=1)


class _NormalizedUsage(ContractModel):
    total_input_tokens: int | None = Field(ge=0)
    total_output_tokens: int | None = Field(ge=0)
    total_tokens: int | None = Field(ge=0)
    total_cached_tokens: int | None = Field(ge=0)
    total_thought_tokens: int | None = Field(ge=0)
    total_tool_use_tokens: int | None = Field(ge=0)


class _NormalizedResponse(ContractModel):
    id: str
    status: Literal["completed"]
    steps: tuple[_NormalizedModelOutputStep, ...] = Field(min_length=1)
    output_text: str = Field(min_length=1)
    usage: _NormalizedUsage
    provider_model: str | None = None

    @field_validator("id", "provider_model")
    @classmethod
    def _metadata(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _stable_string(value, label="normalized response metadata")

    @model_validator(mode="after")
    def _output_closure(self) -> Self:
        joined = "".join(part.text for step in self.steps for part in step.content)
        if joined != self.output_text:
            raise ValueError("normalized response model output differs from output_text")
        return self


class RouteProposalProvenance(ContractModel):
    """Private sealed provenance for one accepted initial-lane proposal."""

    schema_version: Literal["tsdaf.route-proposal-provenance.v2"]
    stage: _Stage
    stage_profile: str
    model_profile: str
    work_unit_id: str
    work_unit_sha256: str
    source_view_sha256: str
    preparation_ref: ArtifactRef
    prompt_ref: ArtifactRef
    schema_ref: ArtifactRef
    resource_manifest_ref: ArtifactRef
    request_ref: ArtifactRef
    response_ref: ArtifactRef
    raw_record_ref: ArtifactRef
    proposal_ref: ArtifactRef
    proposal_sha256: str
    interaction_id: str
    request_id: str | None
    reuse_binding_ref: ArtifactRef | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_reuse(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        value: dict[str, object] = handler(self)
        if self.reuse_binding_ref is None:
            value.pop("reuse_binding_ref", None)
        return value

    @field_validator("work_unit_id", "interaction_id", "request_id")
    @classmethod
    def _identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _stable_string(value, label="route proposal provenance identity")

    @field_validator("work_unit_sha256", "source_view_sha256", "proposal_sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _sha256(value, label="route proposal provenance hash")

    @model_validator(mode="after")
    def _fixed_profile_and_refs(self) -> Self:
        media = _MEDIA_TYPES[self.stage]
        current_profile = STAGE_PROFILES[self.stage]
        if self.stage_profile != current_profile:
            raise ValueError("route proposal provenance has the wrong stage profile")
        require_lane_model_profile(self.stage, self.model_profile)
        expected_media = {
            self.prompt_ref: media["prompt"],
            self.schema_ref: media["schema"],
            self.resource_manifest_ref: media["manifest"],
            self.request_ref: media["request"],
            self.response_ref: media["response"],
            self.raw_record_ref: media["record"],
            self.proposal_ref: media["proposal"],
            self.preparation_ref: _PREPARATION_MEDIA_TYPE,
        }
        if any(ref.media_type != expected for ref, expected in expected_media.items()):
            raise ValueError("route proposal provenance has a mismatched artifact media type")
        if self.proposal_ref.sha256 != self.proposal_sha256:
            raise ValueError("route proposal provenance does not close its proposal ref")
        return self


_PROPOSAL_MODELS: dict[str, type[TextReading] | type[VisualReading]] = {
    "T1": TextReading,
    "V1": VisualReading,
}


@dataclass(frozen=True, slots=True)
class SealedTextProposal:
    proposal: TextReading
    proposal_ref: ArtifactRef
    provenance: RouteProposalProvenance
    provenance_ref: ArtifactRef


@dataclass(frozen=True, slots=True)
class SealedVisualProposal:
    proposal: VisualReading
    proposal_ref: ArtifactRef
    provenance: RouteProposalProvenance
    provenance_ref: ArtifactRef


@dataclass(frozen=True, slots=True)
class SealedInitialRouteLaneProposals:
    text: SealedTextProposal
    visual: SealedVisualProposal


_SealedLaneProposal = SealedTextProposal | SealedVisualProposal


_ContractT = TypeVar("_ContractT", bound=ContractModel)


def _read_exact(
    store: PrivateArtifactStore,
    ref: ArtifactRef,
    *,
    media_type: str,
    expected: bytes | None = None,
) -> bytes:
    if type(ref) is not ArtifactRef or ref.media_type != media_type:
        raise ValueError("upstream artifact ref has the wrong type or media type")
    store.verify(ref)
    payload = store.read_bytes(ref)
    if type(payload) is not bytes:
        raise TypeError("private artifact readback must be exact bytes")
    if (
        hashlib.sha256(payload).hexdigest() != ref.sha256
        or len(payload) != ref.byte_size
        or (expected is not None and payload != expected)
    ):
        raise ValueError("upstream artifact readback differs in bytes, hash, or size")
    return payload


def _read_contract(
    store: PrivateArtifactStore,
    ref: ArtifactRef,
    *,
    media_type: str,
    model: type[_ContractT],
) -> _ContractT:
    payload = _read_exact(store, ref, media_type=media_type)
    try:
        raw: object = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("sealed contract artifact is not canonical JSON") from None
    if canonical_json_bytes(raw) != payload:
        raise ValueError("sealed contract artifact is not canonical JSON")
    validated = model.model_validate_json(payload)
    host_canonical = canonical_json_bytes(validated.model_dump(mode="json", exclude_none=False))
    if payload != host_canonical:
        raise ValueError("sealed contract artifact is not Host-canonical model JSON")
    return validated


def load_reader_exchange(
    *,
    store: PrivateArtifactStore,
    stage: _Stage,
    record_ref: ArtifactRef,
    reuse_binding_ref: ArtifactRef | None = None,
) -> RawLaneExchange:
    record = _read_contract(
        store, record_ref, media_type=_MEDIA_TYPES[stage]["record"], model=RawLaneInteractionRecord
    )
    return RawLaneExchange(
        stage=stage,
        resources=LaneResourceArtifacts(
            record.prompt_ref, record.schema_ref, record.resource_manifest_ref
        ),
        request_bytes=_read_exact(
            store, record.request_ref, media_type=_MEDIA_TYPES[stage]["request"]
        ),
        response_bytes=_read_exact(
            store, record.response_ref, media_type=_MEDIA_TYPES[stage]["response"]
        ),
        record=record,
        record_ref=record_ref,
        reuse_binding_ref=reuse_binding_ref,
    )


def _original_preparation(
    *,
    exchange: RawLaneExchange,
    preparation: PairPreparationReceipt,
    preparation_ref: ArtifactRef | None,
    store: PrivateArtifactStore,
    reuse_verifier: ReaderReuseVerifier | None,
) -> PairPreparationReceipt:
    if exchange.reuse_binding_ref is None:
        return preparation
    if reuse_verifier is None:
        raise ValueError("reader reuse requires a Host execution verifier")
    binding = _read_contract(
        store, exchange.reuse_binding_ref, media_type=READER_REUSE_MEDIA, model=ReaderReuseBinding
    )
    if binding.current_preparation_ref != preparation_ref:
        raise ValueError("reader reuse binding changes current preparation")
    reuse_verifier(binding)
    output = _read_contract(
        store, binding.output_ref, media_type=READER_CACHE_OUTPUT_MEDIA, model=ReaderCacheOutput
    )
    if output.raw_record_ref != exchange.record_ref or output.identity_ref != binding.identity_ref:
        raise ValueError("reader reuse output changes original record or identity")
    original = _read_contract(
        store,
        output.original_preparation_ref,
        media_type=_PREPARATION_MEDIA_TYPE,
        model=PairPreparationReceipt,
    )
    for pair in (preparation, original):
        identity_ref = (
            pair.text_reader_identity_ref
            if exchange.stage == "T1"
            else pair.visual_reader_identity_ref
        )
        request_ref = pair.text_request_ref if exchange.stage == "T1" else pair.visual_request_ref
        if identity_ref != binding.identity_ref or request_ref != exchange.request_ref:
            raise ValueError("reader reuse differs from current or original request/identity")
    return original


def validate_reader_output(
    *,
    exchange: RawLaneExchange,
    preparation: PairPreparationReceipt,
    preparation_ref: ArtifactRef,
    store: PrivateArtifactStore,
    reuse_verifier: ReaderReuseVerifier | None = None,
) -> TextReading | VisualReading:
    record, response = _close_exchange(
        exchange=exchange,
        stage=exchange.stage,
        preparation=preparation,
        preparation_ref=preparation_ref,
        store=store,
        reuse_verifier=reuse_verifier,
    )
    return _parse_reader_response(record, response)


def _parse_reader_response(
    record: RawLaneInteractionRecord, response: bytes
) -> TextReading | VisualReading:
    return _PROPOSAL_MODELS[record.stage].model_validate_json(
        _normalized_response(response, record).output_text
    )


def _close_exchange(
    *,
    exchange: RawLaneExchange,
    stage: _Stage,
    preparation: PairPreparationReceipt,
    store: PrivateArtifactStore,
    preparation_ref: ArtifactRef | None = None,
    reuse_verifier: ReaderReuseVerifier | None = None,
) -> tuple[RawLaneInteractionRecord, bytes]:
    if exchange.stage != stage:
        raise ValueError("raw lane exchange has the wrong stage")
    media = _MEDIA_TYPES[stage]
    frozen = load_lane_resources(stage)
    record = _read_contract(
        store,
        exchange.record_ref,
        media_type=media["record"],
        model=RawLaneInteractionRecord,
    )
    if record != exchange.record:
        raise ValueError("sealed raw interaction record differs from the in-memory exchange")
    if record.stage != stage:
        raise ValueError("raw interaction record has the wrong stage")
    preparation = _original_preparation(
        exchange=exchange,
        preparation=preparation,
        preparation_ref=preparation_ref,
        store=store,
        reuse_verifier=reuse_verifier,
    )
    if (
        record.work_unit_id != preparation.work_unit_id
        or record.source_view_sha256 != preparation.source_view_sha256
    ):
        raise ValueError("raw interaction record has a cross-lane work unit or source view")
    if exchange.resources != (
        type(exchange.resources)(
            prompt_ref=record.prompt_ref,
            schema_ref=record.schema_ref,
            resource_manifest_ref=record.resource_manifest_ref,
        )
    ):
        raise ValueError("raw interaction resources differ from its sealed record")
    _read_exact(
        store,
        record.prompt_ref,
        media_type=media["prompt"],
        expected=frozen.prompt_bytes,
    )
    _read_exact(
        store,
        record.schema_ref,
        media_type=media["schema"],
        expected=frozen.schema_bytes,
    )
    _read_exact(
        store,
        record.resource_manifest_ref,
        media_type=media["manifest"],
        expected=frozen.resource_manifest_bytes,
    )
    is_text = stage in _TEXT_STAGES
    expected_request_ref = (
        preparation.text_request_ref if is_text else preparation.visual_request_ref
    )
    expected_request_sha256 = expected_request_ref.sha256 if expected_request_ref else None
    if (
        record.request_ref != expected_request_ref
        or record.request_sha256 != expected_request_sha256
    ):
        raise ValueError("raw interaction request ref differs from pair preparation")
    _read_exact(
        store,
        record.request_ref,
        media_type=media["request"],
        expected=exchange.request_bytes,
    )
    verify_reader_identity(
        store=store,
        stage=stage,
        ref=preparation.text_reader_identity_ref
        if is_text
        else preparation.visual_reader_identity_ref,
        request_ref=record.request_ref,
        resource_manifest_ref=record.resource_manifest_ref,
    )
    response_bytes = _read_exact(
        store,
        record.response_ref,
        media_type=media["response"],
        expected=exchange.response_bytes,
    )
    return record, response_bytes


def _normalized_response(
    response_bytes: bytes,
    record: RawLaneInteractionRecord,
) -> _NormalizedResponse:
    try:
        raw: object = json.loads(response_bytes.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("normalized response is not canonical JSON") from None
    if canonical_json_bytes(raw) != response_bytes:
        raise ValueError("normalized response is not canonical JSON")
    try:
        response = _NormalizedResponse.model_validate_json(response_bytes)
    except ValidationError as error:
        raise ValueError(f"normalized response shape is invalid: {error}") from None
    if response.id != record.interaction_id:
        raise ValueError("normalized response interaction id disagrees with its raw record")
    if response.output_text != record.output_text:
        raise ValueError("normalized response output disagrees with its raw record")
    usage = response.usage
    if (
        usage.total_input_tokens,
        usage.total_output_tokens,
        usage.total_tokens,
        usage.total_cached_tokens,
        usage.total_thought_tokens,
        usage.total_tool_use_tokens,
    ) != (
        record.total_input_tokens,
        record.total_output_tokens,
        record.total_tokens,
        record.total_cached_tokens,
        record.total_thought_tokens,
        record.total_tool_use_tokens,
    ):
        raise ValueError("normalized response usage disagrees with its raw record")
    return response


def _provenance(
    *,
    stage: _Stage,
    proposal_ref: ArtifactRef,
    preparation: PairPreparationReceipt,
    preparation_ref: ArtifactRef,
    exchange: RawLaneExchange,
    record: RawLaneInteractionRecord,
) -> RouteProposalProvenance:
    interaction_id = record.interaction_id
    if interaction_id is None:
        raise ValueError("accepted proposal response has no interaction id")
    return RouteProposalProvenance(
        schema_version="tsdaf.route-proposal-provenance.v2",
        stage=stage,
        stage_profile=STAGE_PROFILES[stage],
        model_profile=record.model_profile,
        work_unit_id=preparation.work_unit_id,
        work_unit_sha256=preparation.work_unit_sha256,
        source_view_sha256=preparation.source_view_sha256,
        preparation_ref=preparation_ref,
        prompt_ref=record.prompt_ref,
        schema_ref=record.schema_ref,
        resource_manifest_ref=record.resource_manifest_ref,
        request_ref=record.request_ref,
        response_ref=record.response_ref,
        raw_record_ref=exchange.record_ref,
        proposal_ref=proposal_ref,
        proposal_sha256=proposal_ref.sha256,
        interaction_id=interaction_id,
        request_id=record.request_id,
        reuse_binding_ref=exchange.reuse_binding_ref,
    )


def _verify_sealed_lane(
    *,
    stage: _Stage,
    sealed: _SealedLaneProposal,
    store: PrivateArtifactStore,
    reuse_verifier: ReaderReuseVerifier | None = None,
) -> PairPreparationReceipt:
    expected_type = SealedTextProposal if stage in _TEXT_STAGES else SealedVisualProposal
    proposal_model = _PROPOSAL_MODELS[stage]
    if type(sealed) is not expected_type:
        raise TypeError("sealed lane proposal has the wrong concrete type")
    proposal = proposal_model.model_validate(
        sealed.proposal.model_dump(mode="python", exclude_none=False)
    )
    provenance = RouteProposalProvenance.model_validate(
        sealed.provenance.model_dump(mode="python", exclude_none=False)
    )
    if proposal != sealed.proposal or provenance != sealed.provenance:
        raise ValueError("in-memory sealed lane proposal or provenance is not exact")
    media = _MEDIA_TYPES[stage]
    stored_proposal = _read_contract(
        store,
        sealed.proposal_ref,
        media_type=media["proposal"],
        model=proposal_model,
    )
    stored_provenance = _read_contract(
        store,
        sealed.provenance_ref,
        media_type=_PROVENANCE_MEDIA_TYPE,
        model=RouteProposalProvenance,
    )
    if stored_proposal != proposal or stored_provenance != provenance:
        raise ValueError("sealed proposal or provenance differs from its in-memory value")
    if provenance.stage != stage or provenance.proposal_ref != sealed.proposal_ref:
        raise ValueError("sealed proposal provenance changes its stage or proposal")

    preparation = _read_contract(
        store,
        provenance.preparation_ref,
        media_type=_PREPARATION_MEDIA_TYPE,
        model=PairPreparationReceipt,
    )
    if (
        preparation.work_unit_id != provenance.work_unit_id
        or preparation.work_unit_sha256 != provenance.work_unit_sha256
        or preparation.source_view_sha256 != provenance.source_view_sha256
    ):
        raise ValueError("sealed proposal preparation changes its work-unit authority")

    exchange = load_reader_exchange(
        store=store,
        stage=stage,
        record_ref=provenance.raw_record_ref,
        reuse_binding_ref=provenance.reuse_binding_ref,
    )
    record, response = _close_exchange(
        exchange=exchange,
        stage=stage,
        preparation=preparation,
        store=store,
        preparation_ref=provenance.preparation_ref,
        reuse_verifier=reuse_verifier,
    )
    expected_provenance = _provenance(
        stage=stage,
        proposal_ref=sealed.proposal_ref,
        preparation=preparation,
        preparation_ref=provenance.preparation_ref,
        exchange=exchange,
        record=record,
    )
    if expected_provenance != provenance:
        raise ValueError("sealed raw record disagrees with proposal provenance")
    if _parse_reader_response(record, response) != proposal:
        raise ValueError("normalized response disagrees with sealed typed proposal")
    return preparation


def _verify_shared_closure(
    *,
    text_stage: _Stage,
    visual_stage: _Stage,
    text: _SealedLaneProposal,
    visual: _SealedLaneProposal,
    artifact_store: PrivateArtifactStore,
    reuse_verifier: ReaderReuseVerifier | None = None,
) -> PairPreparationReceipt:
    """Both lanes close the same preparation."""
    lanes = ((text_stage, text), (visual_stage, visual))
    closures = [
        _verify_sealed_lane(
            stage=stage, sealed=lane, store=artifact_store, reuse_verifier=reuse_verifier
        )
        for stage, lane in lanes
    ]
    preparation = closures[0]
    if any(value != closures[0] for value in closures[1:]):
        raise ValueError("a lane pair does not share one exact sealed preparation")
    if preparation.stages != tuple(stage for stage, _ in lanes):
        raise ValueError("sealed proposals do not match the lane pair")
    return preparation


def verify_sealed_initial_route_lane_proposals(
    *,
    proposals: SealedInitialRouteLaneProposals,
    artifact_store: PrivateArtifactStore,
    reuse_verifier: ReaderReuseVerifier | None = None,
) -> PairPreparationReceipt:
    """Prove the complete sealed 3C2 pair closure without reading canonical paper sources."""

    if type(proposals) is not SealedInitialRouteLaneProposals:
        raise TypeError("proposals must be SealedInitialRouteLaneProposals")
    return _verify_shared_closure(
        text_stage="T1",
        visual_stage="V1",
        text=proposals.text,
        visual=proposals.visual,
        artifact_store=artifact_store,
        reuse_verifier=reuse_verifier,
    )


def _seal_lane_pair(
    *,
    text_stage: _Stage,
    visual_stage: _Stage,
    exchanges: RawInitialRouteLaneExchanges,
    artifact_store: PrivateArtifactStore,
    reuse_verifier: ReaderReuseVerifier | None = None,
) -> tuple[SealedTextProposal, SealedVisualProposal]:
    """Validate one accepted dispatch closure and seal both typed lane proposals."""

    preparation = _read_contract(
        artifact_store,
        exchanges.preparation_ref,
        media_type=_PREPARATION_MEDIA_TYPE,
        model=PairPreparationReceipt,
    )
    if preparation != exchanges.preparation:
        raise ValueError("sealed preparation differs from the in-memory exchange")
    if preparation.stages != (text_stage, visual_stage):
        raise ValueError("the dispatched readers do not match the sealed stage pair")
    # Both exchanges are closed, and both answers parsed, before a single proposal
    # artifact is written. A store that hands back a corrupted visual response must not
    # leave a sealed text proposal behind claiming the pair ran.
    lanes = ((text_stage, exchanges.text), (visual_stage, exchanges.visual))
    closed = [
        _close_exchange(
            exchange=exchange,
            stage=stage,
            preparation=preparation,
            store=artifact_store,
            preparation_ref=exchanges.preparation_ref,
            reuse_verifier=reuse_verifier,
        )
        for stage, exchange in lanes
    ]
    parsed = [
        _parse_reader_response(record, response_bytes)
        for (stage, _), (record, response_bytes) in zip(lanes, closed, strict=True)
    ]

    text: SealedTextProposal | None = None
    visual: SealedVisualProposal | None = None
    for (stage, exchange), (record, _), proposal in zip(lanes, closed, parsed, strict=True):
        proposal_ref = seal_exact(
            artifact_store,
            canonical_json_bytes(proposal.model_dump(mode="json", exclude_none=False)),
            media_type=_MEDIA_TYPES[stage]["proposal"],
        )
        provenance = _provenance(
            stage=stage,
            proposal_ref=proposal_ref,
            preparation=preparation,
            preparation_ref=exchanges.preparation_ref,
            exchange=exchange,
            record=record,
        )
        provenance_ref = seal_exact(
            artifact_store,
            canonical_json_bytes(provenance.model_dump(mode="json", exclude_none=False)),
            media_type=_PROVENANCE_MEDIA_TYPE,
        )
        if isinstance(proposal, TextReading):
            text = SealedTextProposal(proposal, proposal_ref, provenance, provenance_ref)
        else:
            visual = SealedVisualProposal(proposal, proposal_ref, provenance, provenance_ref)
    assert text is not None and visual is not None
    return text, visual


def seal_initial_route_lane_proposals(
    *,
    exchanges: RawInitialRouteLaneExchanges,
    artifact_store: PrivateArtifactStore,
    reuse_verifier: ReaderReuseVerifier | None = None,
) -> SealedInitialRouteLaneProposals:
    """Validate exact accepted-3B closure and seal typed T1/V1 proposals."""

    text, visual = _seal_lane_pair(
        text_stage="T1",
        visual_stage="V1",
        exchanges=exchanges,
        artifact_store=artifact_store,
        reuse_verifier=reuse_verifier,
    )
    return SealedInitialRouteLaneProposals(text=text, visual=visual)
