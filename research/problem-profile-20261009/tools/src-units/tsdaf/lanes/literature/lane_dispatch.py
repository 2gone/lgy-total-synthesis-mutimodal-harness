"""Seal independent reader requests before dispatch and preserve each lane's result.

Requests are prepared independently before either runs. Worker scheduling may overlap
or finish one lane first; independence comes from request contents and stream ownership.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from tsdaf.contracts._base import ContractModel, _sha256, _stable_string
from tsdaf.contracts.execution import ProviderInteractionError, TypedFailure
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.artifacts import seal_exact
from tsdaf.lanes.literature.host_ports import (
    PrivateArtifactStore,
)
from tsdaf.lanes.literature.profiles import (
    LaneStage,
    PreparedLaneCall,
)
from tsdaf.lanes.literature.provider_port import (
    AUTHORIZED_MODEL_PROFILE,
    LaneCompletedInteraction,
    LaneInteractionClient,
    LanePreparedInteraction,
    ProviderBinding,
    require_lane_model_profile,
)
from tsdaf.lanes.literature.reader_identity import (
    READER_IDENTITY_MEDIA_TYPE,
    build_reader_identity,
)
from tsdaf.lanes.literature.route_root import validate_route_group_root_shape
from tsdaf.lanes.literature.source_scope import VerifiedWorkUnitInput
from tsdaf.lanes.literature.text_lane import build_text_route_request
from tsdaf.lanes.literature.visual_lane import build_visual_route_request
from tsdaf.lanes.provider_attempts import seal_provider_attempts

__all__ = (
    "LaneResourceArtifacts",
    "LanePairStages",
    "PairPreparationReceipt",
    "RawInitialRouteLaneExchanges",
    "RawLaneExchange",
    "RawLaneInteractionRecord",
    "dispatch_literature_lanes",
)

_MEDIA_TYPES = {
    "T1": {
        "prompt": "text/vnd.tsdaf.literature-t1-prompt+markdown",
        "schema": "application/vnd.tsdaf.literature-t1-schema+json",
        "manifest": "application/vnd.tsdaf.literature-t1-resource-manifest+json",
        "request": "application/vnd.tsdaf.literature-t1-interaction-request+json",
        "response": "application/vnd.tsdaf.literature-t1-interaction-response+octets",
        "record": "application/vnd.tsdaf.literature-t1-raw-interaction-record+json",
    },
    "V1": {
        "prompt": "text/vnd.tsdaf.literature-v1-prompt+markdown",
        "schema": "application/vnd.tsdaf.literature-v1-schema+json",
        "manifest": "application/vnd.tsdaf.literature-v1-resource-manifest+json",
        "request": "application/vnd.tsdaf.literature-v1-interaction-request+json",
        "response": "application/vnd.tsdaf.literature-v1-interaction-response+octets",
        "record": "application/vnd.tsdaf.literature-v1-raw-interaction-record+json",
    },
}
_TextStage = Literal["T1"]
_VisualStage = Literal["V1"]
_PairStage = Literal["T1", "V1"]
_TEXT_STAGES: tuple[_TextStage, ...] = ("T1",)
_VISUAL_STAGES: tuple[_VisualStage, ...] = ("V1",)


def _pair_stage(stage: LaneStage) -> _PairStage:
    """Narrow a prepared call to a stage this pair dispatcher actually has a pair for.

    Not every lane stage is half of a pair. V3, the figure reader a detail task
    dispatches on its own, has no peer lane, no work unit and no preparation receipt,
    and the pair receipt and raw record below are defined only over the two that do.
    """

    if stage not in ("T1", "V1"):
        raise ValueError("raw lane exchange is only defined for a dispatched lane stage")
    return stage


@dataclass(frozen=True, slots=True)
class LanePairStages:
    """The two stages one layer dispatches together."""

    text: _TextStage
    visual: _VisualStage

    def __post_init__(self) -> None:
        if self.text not in _TEXT_STAGES or self.visual not in _VISUAL_STAGES:
            raise ValueError("a lane pair is one text stage and one visual stage")
        if self.text[1:] != self.visual[1:]:
            raise ValueError("a lane pair must dispatch two stages of the same layer")


_PREPARATION_MEDIA_TYPE = "application/vnd.tsdaf.literature-route-pair-preparation-receipt+json"


class PairTextSourceIdentity(ContractModel):
    block_id: str
    char_length: int = Field(ge=1)
    raw_text_sha256: str

    @field_validator("block_id")
    @classmethod
    def _block_id(cls, value: str) -> str:
        return _stable_string(value, label="pair text source id")

    @field_validator("raw_text_sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _sha256(value, label="pair text source hash")


class PairVisualSourceIdentity(ContractModel):
    visual_id: str
    source_sha256: str
    media_type: str
    byte_size: int = Field(ge=1)
    width: int = Field(ge=1)
    height: int = Field(ge=1)

    @field_validator("visual_id", "media_type")
    @classmethod
    def _text(cls, value: str) -> str:
        return _stable_string(value, label="pair visual source identity")

    @field_validator("source_sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _sha256(value, label="pair visual source hash")


class PairPreparationReceipt(ContractModel):
    """Original sources actually included in each sealed request, before dispatch.

    Text identities cover the full numbered blocks and images cover the full original
    pixels. A preparation proves a request's contents, not its delivery or a scientific
    coverage claim; raw responses and failure audits record what happened next.
    """

    schema_version: Literal["tsdaf.initial-route-pair-preparation-receipt.v4"]
    work_unit_id: str
    work_unit_sha256: str
    source_view_sha256: str
    route_group_id: str
    source_order: int = Field(ge=0)
    ordinal: int = Field(ge=0)
    stages: tuple[_PairStage, ...]
    text_sources: tuple[PairTextSourceIdentity, ...]
    visual_sources: tuple[PairVisualSourceIdentity, ...]
    text_request_ref: ArtifactRef | None
    visual_request_ref: ArtifactRef | None
    text_reader_identity_ref: ArtifactRef | None = None
    visual_reader_identity_ref: ArtifactRef | None = None

    @model_serializer(mode="wrap")
    def _omit_legacy_identity_defaults(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        result: dict[str, object] = handler(self)
        for field in ("text_reader_identity_ref", "visual_reader_identity_ref"):
            if getattr(self, field) is None:
                result.pop(field, None)
        return result

    @field_validator("work_unit_id", "route_group_id")
    @classmethod
    def _text(cls, value: str) -> str:
        return _stable_string(value, label="pair preparation identity")

    @field_validator("work_unit_sha256", "source_view_sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _sha256(value, label="pair preparation hash")

    @model_validator(mode="after")
    def _request_closure(self) -> Self:
        if self.stages != ("T1", "V1"):
            raise ValueError("reading preparation has an invalid selected stage set")
        for modality, ref in (
            ("T", self.text_request_ref),
            ("V", self.visual_request_ref),
        ):
            stage = next((item for item in self.stages if item.startswith(modality)), None)
            sources = self.text_sources if modality == "T" else self.visual_sources
            if stage is None and sources:
                raise ValueError("unrequested lane cannot report supplied original sources")
            if (stage is not None) != (ref is not None):
                raise ValueError("selected lane request does not close its preparation receipt")
            if (
                stage is not None
                and ref is not None
                and ref.media_type != _MEDIA_TYPES[stage]["request"]
            ):
                raise ValueError("prepared request has the wrong stage media type")
        return self


@dataclass(frozen=True, slots=True)
class LaneResourceArtifacts:
    prompt_ref: ArtifactRef
    schema_ref: ArtifactRef
    resource_manifest_ref: ArtifactRef


class RawLaneInteractionRecord(ContractModel):
    """Sealed adapter metadata and artifact closure for one raw lane interaction."""

    schema_version: Literal["tsdaf.raw-lane-interaction-record.v1"]
    provider_attempt_audit_ref: ArtifactRef | None = None
    stage: _PairStage
    model_profile: str
    work_unit_id: str
    source_view_sha256: str
    prompt_ref: ArtifactRef
    schema_ref: ArtifactRef
    resource_manifest_ref: ArtifactRef
    request_ref: ArtifactRef
    request_sha256: str
    response_ref: ArtifactRef
    response_sha256: str
    interaction_id: str | None
    output_text: str | None
    request_id: str | None
    total_input_tokens: int | None = Field(ge=0)
    total_output_tokens: int | None = Field(ge=0)
    total_tokens: int | None = Field(ge=0)
    total_cached_tokens: int | None = Field(ge=0)
    total_thought_tokens: int | None = Field(ge=0)
    total_tool_use_tokens: int | None = Field(ge=0)
    elapsed_ms: float = Field(ge=0)
    transport_attempts: int = Field(ge=1)

    @model_serializer(mode="wrap")
    def _omit_absent_attempt_audit(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        result: dict[str, object] = handler(self)
        if self.provider_attempt_audit_ref is None:
            result.pop("provider_attempt_audit_ref", None)
        return result

    @field_validator("work_unit_id", "interaction_id", "request_id")
    @classmethod
    def _identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _stable_string(value, label="raw lane interaction identity")

    @field_validator("source_view_sha256", "request_sha256", "response_sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _sha256(value, label="raw lane interaction hash")

    @model_validator(mode="after")
    def _artifact_closure(self) -> Self:
        require_lane_model_profile(self.stage, self.model_profile)
        media = _MEDIA_TYPES[self.stage]
        refs = {
            "prompt": self.prompt_ref,
            "schema": self.schema_ref,
            "manifest": self.resource_manifest_ref,
            "request": self.request_ref,
            "response": self.response_ref,
        }
        if any(ref.media_type != media[kind] for kind, ref in refs.items()):
            raise ValueError("raw lane interaction resource media types do not match its stage")
        if self.request_ref.sha256 != self.request_sha256:
            raise ValueError("raw lane request ref does not close its request hash")
        if self.response_ref.sha256 != self.response_sha256:
            raise ValueError("raw lane response ref does not close its response hash")
        return self


@dataclass(frozen=True, slots=True)
class RawLaneExchange:
    stage: _PairStage
    resources: LaneResourceArtifacts
    request_bytes: bytes
    response_bytes: bytes
    record: RawLaneInteractionRecord
    record_ref: ArtifactRef
    reuse_binding_ref: ArtifactRef | None = None

    def __post_init__(self) -> None:
        record_bytes = canonical_json_bytes(self.record.model_dump(mode="json", exclude_none=False))
        if self.stage != self.record.stage:
            raise ValueError("raw lane exchange stage does not match its record")
        if self.resources != LaneResourceArtifacts(
            prompt_ref=self.record.prompt_ref,
            schema_ref=self.record.schema_ref,
            resource_manifest_ref=self.record.resource_manifest_ref,
        ):
            raise ValueError("raw lane exchange resources do not match its record")
        if (
            hashlib.sha256(self.request_bytes).hexdigest() != self.record.request_sha256
            or hashlib.sha256(self.response_bytes).hexdigest() != self.record.response_sha256
        ):
            raise ValueError("raw lane exchange bytes do not match its record")
        if (
            self.record_ref.media_type != _MEDIA_TYPES[self.stage]["record"]
            or self.record_ref.sha256 != hashlib.sha256(record_bytes).hexdigest()
            or self.record_ref.byte_size != len(record_bytes)
        ):
            raise ValueError("raw lane exchange record ref does not close its canonical record")

    @property
    def request_sha256(self) -> str:
        return self.record.request_sha256

    @property
    def request_ref(self) -> ArtifactRef:
        return self.record.request_ref

    @property
    def response_sha256(self) -> str:
        return self.record.response_sha256

    @property
    def response_ref(self) -> ArtifactRef:
        return self.record.response_ref


ReaderExecutor = Callable[
    [Literal["T1", "V1"], PairPreparationReceipt, ArtifactRef, Callable[[], RawLaneExchange]],
    RawLaneExchange,
]


class LanePairProviderError(ProviderInteractionError):
    """A failed pair retains its immutable requests and any completed peer reading."""

    def __init__(
        self,
        failure: TypedFailure,
        *,
        preparation: PairPreparationReceipt,
        preparation_ref: ArtifactRef,
        audit_ref: ArtifactRef,
        completed: tuple[RawLaneExchange, ...],
    ) -> None:
        super().__init__(failure)
        self.preparation = preparation
        self.preparation_ref = preparation_ref
        self.audit_ref = audit_ref
        self.completed = completed


@dataclass(frozen=True, slots=True)
class RawInitialRouteLaneExchanges:
    """One dispatched lane pair: its sealed preparation and both raw answers."""

    preparation: PairPreparationReceipt
    preparation_ref: ArtifactRef
    text: RawLaneExchange
    visual: RawLaneExchange


def _seal_resources(
    store: PrivateArtifactStore,
    prepared: PreparedLaneCall,
) -> LaneResourceArtifacts:
    media = _MEDIA_TYPES[prepared.stage]
    resources = prepared.resources
    return LaneResourceArtifacts(
        prompt_ref=seal_exact(store, resources.prompt_bytes, media_type=media["prompt"]),
        schema_ref=seal_exact(store, resources.schema_bytes, media_type=media["schema"]),
        resource_manifest_ref=seal_exact(
            store,
            resources.resource_manifest_bytes,
            media_type=media["manifest"],
        ),
    )


def _interaction_owner(client: LaneInteractionClient, *, stage: str) -> object:
    owner = client.interaction_stream_owner
    if owner is None:
        raise ValueError(f"{stage} interaction stream owner is invalid")
    return owner


def _raw_exchange(
    *,
    store: PrivateArtifactStore,
    work_unit: VerifiedWorkUnitInput,
    prepared: PreparedLaneCall,
    adapter_prepared: LanePreparedInteraction,
    resources: LaneResourceArtifacts,
    interaction: LaneCompletedInteraction,
    request_ref: ArtifactRef,
    response_ref: ArtifactRef,
    provider_attempt_audit_ref: ArtifactRef | None = None,
) -> RawLaneExchange:
    stage = _pair_stage(prepared.stage)
    if type(interaction.request_bytes) is not bytes:
        raise TypeError("interaction request bytes must be exact bytes")
    if interaction.request_bytes != adapter_prepared.request_bytes:
        raise ValueError("interaction request bytes differ from the frozen request")
    if type(interaction.response_bytes) is not bytes:
        raise TypeError("interaction response bytes must be exact bytes")
    request_sha256 = hashlib.sha256(interaction.request_bytes).hexdigest()
    response_sha256 = hashlib.sha256(interaction.response_bytes).hexdigest()
    record = RawLaneInteractionRecord(
        schema_version="tsdaf.raw-lane-interaction-record.v1",
        provider_attempt_audit_ref=provider_attempt_audit_ref,
        stage=stage,
        model_profile=adapter_prepared.model_profile,
        work_unit_id=work_unit.work_unit_id,
        source_view_sha256=work_unit.source_view_sha256,
        prompt_ref=resources.prompt_ref,
        schema_ref=resources.schema_ref,
        resource_manifest_ref=resources.resource_manifest_ref,
        request_ref=request_ref,
        request_sha256=request_sha256,
        response_ref=response_ref,
        response_sha256=response_sha256,
        interaction_id=interaction.interaction_id,
        output_text=interaction.output_text,
        request_id=interaction.request_id,
        total_input_tokens=interaction.total_input_tokens,
        total_output_tokens=interaction.total_output_tokens,
        total_tokens=interaction.total_tokens,
        total_cached_tokens=interaction.total_cached_tokens,
        total_thought_tokens=interaction.total_thought_tokens,
        total_tool_use_tokens=interaction.total_tool_use_tokens,
        elapsed_ms=interaction.elapsed_ms,
        transport_attempts=interaction.transport_attempts,
    )
    record_ref = seal_exact(
        store,
        canonical_json_bytes(record.model_dump(mode="json", exclude_none=False)),
        media_type=_MEDIA_TYPES[prepared.stage]["record"],
    )
    return RawLaneExchange(
        stage=stage,
        resources=resources,
        request_bytes=interaction.request_bytes,
        response_bytes=interaction.response_bytes,
        record=record,
        record_ref=record_ref,
    )


def _dispatch_lane_pair(
    *,
    stages: LanePairStages,
    work_unit: VerifiedWorkUnitInput,
    text_call: PreparedLaneCall | None,
    visual_call: PreparedLaneCall | None,
    provider_binding: ProviderBinding,
    text_client: LaneInteractionClient,
    visual_client: LaneInteractionClient,
    artifact_store: PrivateArtifactStore,
    reader_executor: ReaderExecutor | None = None,
) -> RawInitialRouteLaneExchanges:
    """Execute one already-authorized pair; this establishes no authority of its own.

    The caller owns what the two requests say and which work unit authorizes them.
    Both requests are prepared and sealed before dispatch; each worker receives only
    its own prepared request. Successful answers survive a peer's provider failure.
    """

    if type(stages) is not LanePairStages:
        raise TypeError("stages must be a LanePairStages")
    if type(work_unit) is not VerifiedWorkUnitInput:
        raise TypeError("work_unit must be a VerifiedWorkUnitInput")
    exact = work_unit
    calls = tuple(call for call in (text_call, visual_call) if call is not None)
    if not calls or any(call.stage not in (stages.text, stages.visual) for call in calls):
        raise ValueError("prepared lane calls do not match the selected stages")
    clients = (text_client, visual_client)
    selected = []
    identity_refs: dict[str, ArtifactRef] = {}
    owners: list[object] = []
    for call, client in zip((text_call, visual_call), clients, strict=True):
        if call is None:
            continue
        client = provider_binding.require_client(client, label=call.stage)
        owner = _interaction_owner(client, stage=call.stage)
        if any(owner is previous for previous in owners):
            raise ValueError("a lane pair cannot share an interaction stream owner")
        owners.append(owner)
        profile = AUTHORIZED_MODEL_PROFILE
        require_lane_model_profile(call.stage, profile)
        adapter_call = client.prepare(**call.call_kwargs)
        if adapter_call.model_profile != profile:
            raise ValueError("lane request profile differs from its assigned reasoning effort")
        resources = _seal_resources(artifact_store, call)
        request_ref = seal_exact(
            artifact_store,
            adapter_call.request_bytes,
            media_type=_MEDIA_TYPES[call.stage]["request"],
        )
        identity = build_reader_identity(
            stage=_pair_stage(call.stage),
            work_unit=exact,
            request_ref=request_ref,
            resource_manifest_ref=resources.resource_manifest_ref,
            backend_namespace=provider_binding.namespace_for(call.stage),
        )
        identity_refs[call.stage] = seal_exact(
            artifact_store,
            canonical_json_bytes(identity.model_dump(mode="json")),
            media_type=READER_IDENTITY_MEDIA_TYPE,
        )
        selected.append((call, adapter_call, client, resources, request_ref))
    requests = {call.stage: ref for call, _, _, _, ref in selected}
    text_request_ref = requests.get(stages.text)
    visual_request_ref = requests.get(stages.visual)
    preparation = PairPreparationReceipt(
        schema_version="tsdaf.initial-route-pair-preparation-receipt.v4",
        work_unit_id=exact.work_unit_id,
        work_unit_sha256=exact.work_unit_sha256,
        source_view_sha256=exact.source_view_sha256,
        route_group_id=exact.route_group_id,
        source_order=exact.source_order,
        ordinal=exact.ordinal,
        stages=tuple(_pair_stage(call.stage) for call in calls),
        text_sources=tuple(
            PairTextSourceIdentity(
                block_id=source.block_id,
                char_length=len(source.raw_text),
                raw_text_sha256=source.raw_text_sha256,
            )
            for source in exact.text_sources
            if text_request_ref is not None
        ),
        visual_sources=tuple(
            PairVisualSourceIdentity(
                visual_id=source.visual_id,
                source_sha256=source.source_asset.sha256,
                media_type=source.source_asset.media_type,
                byte_size=source.source_asset.byte_size,
                width=source.dimensions[0],
                height=source.dimensions[1],
            )
            for source in exact.visual_sources
            if visual_request_ref is not None
        ),
        text_request_ref=text_request_ref,
        visual_request_ref=visual_request_ref,
        text_reader_identity_ref=identity_refs.get(stages.text),
        visual_reader_identity_ref=identity_refs.get(stages.visual),
    )
    preparation_ref = seal_exact(
        artifact_store,
        canonical_json_bytes(preparation.model_dump(mode="json", exclude_none=False)),
        media_type=_PREPARATION_MEDIA_TYPE,
    )

    def execute_one(
        item: tuple[
            PreparedLaneCall,
            LanePreparedInteraction,
            LaneInteractionClient,
            LaneResourceArtifacts,
            ArtifactRef,
        ],
    ) -> RawLaneExchange:
        prepared, adapter_prepared, client, resources, request_ref = item

        def invoke() -> RawLaneExchange:
            interaction = client.execute_prepared(adapter_prepared)
            provider_attempt_ref = seal_provider_attempts(
                artifact_store, interaction, request_bytes=adapter_prepared.request_bytes
            )
            response_ref = seal_exact(
                artifact_store,
                interaction.response_bytes,
                media_type=_MEDIA_TYPES[prepared.stage]["response"],
            )
            return _raw_exchange(
                store=artifact_store,
                work_unit=exact,
                prepared=prepared,
                adapter_prepared=adapter_prepared,
                resources=resources,
                interaction=interaction,
                request_ref=request_ref,
                response_ref=response_ref,
                provider_attempt_audit_ref=provider_attempt_ref,
            )

        if reader_executor is None:
            return invoke()
        return reader_executor(_pair_stage(prepared.stage), preparation, preparation_ref, invoke)

    failures: list[tuple[_PairStage, TypedFailure]] = []
    failed_attempt_refs: list[ArtifactRef] = []
    completed: list[RawLaneExchange] = []
    with ThreadPoolExecutor(
        max_workers=len(selected), thread_name_prefix="literature-lane-pair"
    ) as executor:
        futures = [executor.submit(execute_one, item) for item in selected]
        for (prepared, adapter_prepared, _, _, _), future in zip(selected, futures, strict=True):
            try:
                completed.append(future.result())
            except ProviderInteractionError as error:
                audit_ref = seal_provider_attempts(
                    artifact_store, error, request_bytes=adapter_prepared.request_bytes
                )
                if audit_ref is not None:
                    failed_attempt_refs.append(audit_ref)
                failures.append((_pair_stage(prepared.stage), error.failure))
    if failures:
        audit_ref = seal_exact(
            artifact_store,
            canonical_json_bytes(
                {
                    "schema_version": "tsdaf.lane-pair-provider-failure.v2",
                    "preparation_ref": preparation_ref.model_dump(mode="json"),
                    "completed_record_refs": [
                        item.record_ref.model_dump(mode="json") for item in completed
                    ],
                    "reuse_binding_refs": [
                        item.reuse_binding_ref.model_dump(mode="json")
                        for item in completed
                        if item.reuse_binding_ref is not None
                    ],
                    "failures": [
                        {"stage": stage, "failure": failure.model_dump(mode="json")}
                        for stage, failure in failures
                    ],
                    **(
                        {
                            "provider_attempt_audit_refs": [
                                ref.model_dump(mode="json") for ref in failed_attempt_refs
                            ]
                        }
                        if failed_attempt_refs
                        else {}
                    ),
                }
            ),
            media_type="application/vnd.tsdaf.lane-pair-provider-failure+json",
        )
        raise LanePairProviderError(
            failures[0][1],
            preparation=preparation,
            preparation_ref=preparation_ref,
            audit_ref=audit_ref,
            completed=tuple(completed),
        )
    return RawInitialRouteLaneExchanges(
        preparation=preparation,
        preparation_ref=preparation_ref,
        text=completed[0],
        visual=completed[1],
    )


def dispatch_literature_lanes(
    *,
    work_unit: VerifiedWorkUnitInput,
    visual_payloads: Mapping[str, bytes],
    provider_binding: ProviderBinding,
    text_client: LaneInteractionClient,
    visual_client: LaneInteractionClient,
    artifact_store: PrivateArtifactStore,
    reader_executor: ReaderExecutor | None = None,
) -> RawInitialRouteLaneExchanges:
    """Consume the task already verified by the workflow entry, without recompiling it.

    This lane function establishes no caller authority. The route runtime verifies
    sealed source authority before both cache lookup and dispatch.
    """
    if work_unit.depth != 1:
        raise ValueError("reader assignment does not match the task depth")
    stages = LanePairStages(text="T1", visual="V1")
    work_unit = validate_route_group_root_shape(work_unit)
    text_call = build_text_route_request(work_unit)
    visual_call = build_visual_route_request(work_unit, visual_payloads)
    return _dispatch_lane_pair(
        stages=stages,
        work_unit=work_unit,
        text_call=text_call,
        visual_call=visual_call,
        provider_binding=provider_binding,
        text_client=text_client,
        visual_client=visual_client,
        artifact_store=artifact_store,
        reader_executor=reader_executor,
    )
