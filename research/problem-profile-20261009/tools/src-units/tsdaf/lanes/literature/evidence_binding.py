"""Bind sealed private lane selections to Host-reconstructed source evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import field_validator, model_validator

from tsdaf.contracts._base import ContractModel, _sha256, _stable_string
from tsdaf.contracts.literature.schema import FactRootKind, validate_fact_root
from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.artifacts import seal_exact
from tsdaf.lanes.literature.evidence import (
    HostVerificationContext,
    ObservationContext,
    ObservationOriginator,
    SourceImagePixelBBox,
    VisualAssetSelection,
    materialize_text_observation,
    materialize_visual_observation,
)
from tsdaf.lanes.literature.evidence import (
    TextFragmentSelection as EvidenceTextFragmentSelection,
)
from tsdaf.lanes.literature.host_ports import PrivateArtifactStore
from tsdaf.lanes.literature.lane_models import TextFragmentSelection, VisualRegionSelection
from tsdaf.lanes.literature.lane_proposals import (
    RouteProposalProvenance,
    SealedInitialRouteLaneProposals,
    SealedTextProposal,
    SealedVisualProposal,
    verify_sealed_initial_route_lane_proposals,
)
from tsdaf.lanes.literature.quote_binding import TextQuoteIndex
from tsdaf.lanes.literature.reader_identity import ReaderReuseVerifier, verify_reader_identity
from tsdaf.lanes.literature.selection_resolution import (
    block_sentence_spans,
    resolve_text_selection,
    resolve_visual_selection,
)
from tsdaf.lanes.literature.source_inventory import (
    LiteratureSourceInventory,
    resolve_fusion_read_authority,
)
from tsdaf.lanes.literature.source_reads import SourceReader
from tsdaf.lanes.literature.source_scope import (
    ReadAuthority,
    VerifiedWorkUnitInput,
    _text_slices,
    _visual_slices,
)

__all__ = (
    "EvidenceIdentity",
    "FUSION_SELECTION_STAGE",
    "SealedWorkUnitEvidenceRegistry",
    "WorkUnitEvidenceEntry",
    "WorkUnitEvidenceRegistry",
    "bind_work_unit_evidence",
    "source_key",
    "source_location",
    "unit_tag",
)

# The stage a source the fusion read itself was bound under, as opposed to one a reader
# cited: the host writes this into the originator of every such observation, so a task
# downstream can tell what the fusion read from what it was handed. One spelling, read
# by the runtime that writes it and by every task that names it.
FUSION_SELECTION_STAGE = "literature-fusion-source-selection"

_BINDING_SCHEMA_VERSION: Literal["tsdaf.literature-work-unit-evidence-registry.v2"] = (
    "tsdaf.literature-work-unit-evidence-registry.v2"
)
_BINDING_STAGE = "literature-work-unit-evidence-binding"
_BINDING_STAGE_VERSION = "1.1.0"
_OBSERVATION_MEDIA_TYPE = "application/vnd.tsdaf.source-observation.v1+json"
_REGISTRY_MEDIA_TYPE = "application/vnd.tsdaf.literature-work-unit-evidence-registry+json"
_Modality = Literal["TEXT", "VISUAL"]


class EvidenceIdentity(ContractModel):
    """The exact public EvidenceRef shape without creating a second public contract."""

    observation_id: str
    source_observation_sha256: str

    @field_validator("observation_id")
    @classmethod
    def _identity(cls, value: str) -> str:
        return _stable_string(value, label="evidence observation id")

    @field_validator("source_observation_sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _sha256(value, label="source observation hash")


class WorkUnitEvidenceEntry(ContractModel):
    """One unique lane selection and its precise source location."""

    evidence_ref: EvidenceIdentity
    source_observation: dict[str, object]
    observation_ref: ArtifactRef
    modality: _Modality
    canonical_source_id: str
    selection_id: str
    work_unit_id: str
    work_unit_sha256: str
    authorized_scope_sha256: str

    @field_validator("canonical_source_id", "selection_id", "work_unit_id")
    @classmethod
    def _identity(cls, value: str) -> str:
        return _stable_string(value, label="evidence registry identity")

    @field_validator("work_unit_sha256", "authorized_scope_sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _sha256(value, label="evidence registry hash")

    @model_validator(mode="after")
    def _observation_closure(self) -> WorkUnitEvidenceEntry:
        if self.observation_ref.media_type != _OBSERVATION_MEDIA_TYPE:
            raise ValueError("evidence registry observation has the wrong media type")
        payload = canonical_json_bytes(self.source_observation)
        digest = hashlib.sha256(payload).hexdigest()
        if (
            self.observation_ref.sha256 != digest
            or self.observation_ref.byte_size != len(payload)
            or self.evidence_ref.source_observation_sha256 != digest
            or self.source_observation.get("observation_id") != self.evidence_ref.observation_id
        ):
            raise ValueError("evidence registry observation identity is not closed")
        if validate_fact_root(self.source_observation) is not FactRootKind.OBSERVATION:
            raise ValueError("evidence registry value is not a SourceObservation")
        if self.source_observation.get("kind") != self.modality:
            raise ValueError("evidence registry modality disagrees with its observation")
        return self


class WorkUnitEvidenceRegistry(ContractModel):
    """Private immutable index from reading selections to verified public evidence."""

    schema_version: Literal["tsdaf.literature-work-unit-evidence-registry.v2"]
    execution_id: str
    paper_id: str
    document_revision_id: str
    source_phase: Literal["MAIN"]
    package_sha256: str
    work_unit_id: str
    work_unit_sha256: str
    source_view_sha256: str
    entries: tuple[WorkUnitEvidenceEntry, ...]
    readings: tuple[dict[str, object], ...]
    source_errors: tuple[dict[str, object], ...] = ()

    @field_validator("execution_id", "paper_id", "document_revision_id", "work_unit_id")
    @classmethod
    def _identity(cls, value: str) -> str:
        return _stable_string(value, label="work-unit evidence registry identity")

    @field_validator("package_sha256", "work_unit_sha256", "source_view_sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _sha256(value, label="work-unit evidence registry hash")

    @model_validator(mode="after")
    def _entry_closure(self) -> WorkUnitEvidenceRegistry:
        identities = tuple(
            (entry.modality, entry.selection_id, entry.evidence_ref.observation_id)
            for entry in self.entries
        )
        if len(identities) != len(set(identities)):
            raise ValueError("work-unit evidence registry contains duplicate entries")
        if any(
            entry.work_unit_id != self.work_unit_id
            or entry.work_unit_sha256 != self.work_unit_sha256
            or entry.source_observation.get("paper_id") != self.paper_id
            or entry.source_observation.get("document_revision_id") != self.document_revision_id
            for entry in self.entries
        ):
            raise ValueError("work-unit evidence registry entry changes its authority")
        return self


@dataclass(frozen=True, slots=True)
class SealedWorkUnitEvidenceRegistry:
    registry: WorkUnitEvidenceRegistry
    registry_ref: ArtifactRef


def _created_from_refs(
    sealed: SealedTextProposal | SealedVisualProposal,
) -> tuple[ArtifactRef, ...]:
    provenance = sealed.provenance
    return (
        provenance.preparation_ref,
        provenance.prompt_ref,
        provenance.schema_ref,
        provenance.resource_manifest_ref,
        provenance.request_ref,
        provenance.response_ref,
        provenance.raw_record_ref,
        provenance.proposal_ref,
        sealed.provenance_ref,
        *((provenance.reuse_binding_ref,) if provenance.reuse_binding_ref is not None else ()),
    )


def _binding_digest(
    *,
    execution_id: str,
    work_unit: VerifiedWorkUnitInput,
    stage: str,
    selection_id: str,
    ordinal: int,
    kind: str,
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "binding_schema_version": _BINDING_SCHEMA_VERSION,
                "execution_id": execution_id,
                "work_unit_id": work_unit.work_unit_id,
                "stage": stage,
                "selection_id": selection_id,
                "selection_ordinal": ordinal,
                "kind": kind,
            }
        )
    ).hexdigest()


def _scope_digest(
    work_unit: VerifiedWorkUnitInput,
    *,
    modality: _Modality,
    source_id: str,
    read_authority: ReadAuthority | None = None,
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "work_unit_id": work_unit.work_unit_id,
                "work_unit_sha256": work_unit.work_unit_sha256,
                "read_authority": (read_authority or work_unit.read_authority).model_dump(
                    mode="json"
                ),
                "modality": modality,
                "canonical_source_id": source_id,
            }
        )
    ).hexdigest()


def _originator(
    *,
    execution_id: str,
    package: CanonicalPaperPackage,
    provenance: RouteProposalProvenance,
) -> ObservationOriginator:
    if provenance.reuse_binding_ref is not None:
        return ObservationOriginator(
            originator_kind="HOST",
            stage_name="literature-reader-evidence-rebinding",
            stage_version="1.0.0",
            execution_id=execution_id,
            model_profile=None,
            prompt_sha256=None,
            provider_schema_sha256=None,
            requested_by_reviewer_id=None,
            base_artifact_sha256=provenance.reuse_binding_ref.sha256,
        )
    return ObservationOriginator(
        originator_kind="MODEL",
        stage_name=f"literature-{provenance.stage.lower()}-source-selection",
        stage_version="1.0.0",
        execution_id=execution_id,
        model_profile=provenance.model_profile,
        prompt_sha256=provenance.prompt_ref.sha256,
        provider_schema_sha256=provenance.schema_ref.sha256,
        requested_by_reviewer_id=None,
        base_artifact_sha256=package.canonical_content_sha256,
    )


def _context(
    *,
    observation_id: str,
    anchor_id: str,
    work_unit: VerifiedWorkUnitInput,
    created_from: tuple[ArtifactRef, ...],
) -> ObservationContext:
    return ObservationContext(
        observation_id=observation_id,
        anchor_id=anchor_id,
        document_revision_id=work_unit.document_revision_id,
        source_phase="MAIN",
        payload={"payload_kind": "NAVIGATION_ONLY", "candidate_field_refs": []},
        producer_note=None,
        created_from_artifact_refs=created_from,
    )


def _entry(
    *,
    observation: dict[str, object],
    observation_ref: ArtifactRef,
    modality: _Modality,
    source_id: str,
    selection_id: str,
    work_unit: VerifiedWorkUnitInput,
    read_authority: ReadAuthority | None = None,
) -> WorkUnitEvidenceEntry:
    observation_id = observation.get("observation_id")
    if type(observation_id) is not str:
        raise ValueError("materialized SourceObservation has no string observation id")
    return WorkUnitEvidenceEntry(
        evidence_ref=EvidenceIdentity(
            observation_id=observation_id,
            source_observation_sha256=observation_ref.sha256,
        ),
        source_observation=observation,
        observation_ref=observation_ref,
        modality=modality,
        canonical_source_id=source_id,
        selection_id=selection_id,
        work_unit_id=work_unit.work_unit_id,
        work_unit_sha256=work_unit.work_unit_sha256,
        authorized_scope_sha256=_scope_digest(
            work_unit,
            modality=modality,
            source_id=source_id,
            read_authority=read_authority,
        ),
    )


def bind_work_unit_evidence(
    *,
    work_unit: VerifiedWorkUnitInput,
    source_reader: SourceReader,
    execution_id: str,
    proposals: SealedInitialRouteLaneProposals,
    artifact_store: PrivateArtifactStore,
    visual_payloads: Mapping[str, bytes],
    isolate_invalid_selections: bool = False,
    reuse_verifier: ReaderReuseVerifier | None = None,
) -> SealedWorkUnitEvidenceRegistry:
    """Materialize one verified observation per unique typed lane source selection."""

    if type(work_unit) is not VerifiedWorkUnitInput:
        raise TypeError("work_unit must be a VerifiedWorkUnitInput")
    exact_work_unit = VerifiedWorkUnitInput.model_validate(
        work_unit.model_dump(mode="python", by_alias=True, exclude_none=False)
    )
    execution_id = _stable_string(execution_id, label="3D Host execution id")

    preparation = verify_sealed_initial_route_lane_proposals(
        proposals=proposals, artifact_store=artifact_store, reuse_verifier=reuse_verifier
    )
    for lane, ref in (
        (proposals.text, preparation.text_reader_identity_ref),
        (proposals.visual, preparation.visual_reader_identity_ref),
    ):
        verify_reader_identity(
            store=artifact_store,
            ref=ref,
            stage=lane.provenance.stage,
            request_ref=lane.provenance.request_ref,
            resource_manifest_ref=lane.provenance.resource_manifest_ref,
            work_unit=exact_work_unit,
        )
    if (
        preparation.work_unit_id != exact_work_unit.work_unit_id
        or preparation.work_unit_sha256 != exact_work_unit.work_unit_sha256
        or preparation.source_view_sha256 != exact_work_unit.source_view_sha256
        or preparation.route_group_id != exact_work_unit.route_group_id
        or preparation.source_order != exact_work_unit.source_order
        or preparation.ordinal != exact_work_unit.ordinal
    ):
        raise ValueError("pair preparation does not close the exact work-unit identity")
    text_identity = tuple(
        (item.block_id, item.char_length, item.raw_text_sha256) for item in preparation.text_sources
    )
    expected_text_identity = tuple(
        (item.block_id, len(item.raw_text), item.raw_text_sha256)
        for item in exact_work_unit.text_sources
    )
    visual_identity = tuple(
        (
            item.visual_id,
            item.source_sha256,
            item.media_type,
            item.byte_size,
            item.width,
            item.height,
        )
        for item in preparation.visual_sources
    )
    expected_visual_identity = tuple(
        (
            item.visual_id,
            item.source_asset.sha256,
            item.source_asset.media_type,
            item.source_asset.byte_size,
            item.dimensions[0],
            item.dimensions[1],
        )
        for item in exact_work_unit.visual_sources
    )
    if text_identity != expected_text_identity or visual_identity != expected_visual_identity:
        raise ValueError("pair preparation source inventory differs from the work unit")

    exact_package = source_reader.package
    if (
        exact_package.manifest.paper_id != exact_work_unit.paper_id
        or exact_package.canonical_content_sha256 != exact_work_unit.package_sha256
        or exact_work_unit.source_phase != "MAIN"
    ):
        raise ValueError("work unit does not close the canonical MAIN package")

    text_half, visual_half = proposals.text, proposals.visual
    text_selections = text_half.proposal.source_selections
    visual_selections = visual_half.proposal.source_selections
    originators = {}
    created_from = {}
    for half, modality in ((text_half, "TEXT"), (visual_half, "VISUAL")):
        originators[modality] = _originator(
            execution_id=execution_id, package=exact_package, provenance=half.provenance
        )
        created_from[modality] = _created_from_refs(half)
    source_errors: list[dict[str, object]] = []
    if isolate_invalid_selections:
        text_sources = {s.block_id: s for s in exact_work_unit.text_sources}
        visual_sources = {s.visual_id: s for s in exact_work_unit.visual_sources}
        bad: set[tuple[str, str]] = set()
        for modality, selections in (("TEXT", text_selections), ("VISUAL", visual_selections)):
            for selection in selections:
                try:
                    if isinstance(selection, TextFragmentSelection):
                        if selection.block_id not in exact_work_unit.read_authority.text_block_ids:
                            raise ValueError("text selection is not authorized by the work unit")
                        resolve_text_selection(
                            raw_text=text_sources[selection.block_id].raw_text,
                            start_sentence=selection.start_sentence,
                            end_sentence=selection.end_sentence,
                        )
                    else:
                        if selection.visual_id not in exact_work_unit.read_authority.visual_ids:
                            raise ValueError("visual selection is not authorized by the work unit")
                        resolve_visual_selection(
                            x0=selection.x0,
                            y0=selection.y0,
                            x1=selection.x1,
                            y1=selection.y1,
                            dimensions=visual_sources[selection.visual_id].dimensions,
                        )
                except (KeyError, ValueError) as error:
                    bad.add((modality, selection.selection_id))
                    source_errors.append(
                        {
                            "modality": modality,
                            "selection": selection.model_dump(mode="json"),
                            "diagnostic": str(error),
                        }
                    )
        text_selections = tuple(s for s in text_selections if ("TEXT", s.selection_id) not in bad)
        visual_selections = tuple(
            s for s in visual_selections if ("VISUAL", s.selection_id) not in bad
        )
    entries = _materialize_selections(
        exact_work_unit=exact_work_unit,
        exact_package=exact_package,
        visual_payloads=visual_payloads,
        execution_id=execution_id,
        text_stage=text_half.provenance.stage,
        visual_stage=visual_half.provenance.stage,
        text_selections=text_selections,
        visual_selections=visual_selections,
        originators=originators,
        created_from=created_from,
        artifact_store=artifact_store,
    )
    readings: list[dict[str, object]] = []
    valid = {(entry.modality, entry.selection_id) for entry in entries}
    for half, modality in ((text_half, "TEXT"), (visual_half, "VISUAL")):
        readings.append(
            {
                "modality": modality,
                "overview": half.proposal.overview,
                "limitations": half.proposal.limitations,
                "source_errors": [e for e in source_errors if e["modality"] == modality],
                "unverified_observation_scopes": [
                    item.scope
                    for item in half.proposal.observations
                    if not all((modality, sid) in valid for sid in item.selection_ids)
                ],
                "observations": [
                    {
                        "scope": item.scope,
                        "text": item.text,
                        "source_ids": [
                            source_key(work_unit.work_unit_id, modality, sid)
                            for sid in item.selection_ids
                        ],
                    }
                    for item in half.proposal.observations
                    if all((modality, sid) in valid for sid in item.selection_ids)
                ],
            }
        )
    registry = WorkUnitEvidenceRegistry(
        schema_version=_BINDING_SCHEMA_VERSION,
        execution_id=execution_id,
        paper_id=exact_work_unit.paper_id,
        document_revision_id=exact_work_unit.document_revision_id,
        source_phase="MAIN",
        package_sha256=exact_work_unit.package_sha256,
        work_unit_id=exact_work_unit.work_unit_id,
        work_unit_sha256=exact_work_unit.work_unit_sha256,
        source_view_sha256=exact_work_unit.source_view_sha256,
        entries=tuple(entries),
        readings=tuple(readings),
        source_errors=tuple(source_errors),
    )
    registry_bytes = canonical_json_bytes(registry.model_dump(mode="json", exclude_none=False))
    registry_ref = seal_exact(
        artifact_store,
        registry_bytes,
        media_type=_REGISTRY_MEDIA_TYPE,
    )
    return SealedWorkUnitEvidenceRegistry(registry=registry, registry_ref=registry_ref)


def unit_tag(work_unit_id: str) -> str:
    """The tag a work unit puts before each name of its own: a reader's citation key here
    (`source_key`), and the fusion's facts, segments and questions (`name_binder`). The
    package-bound unit identity separates equal local names across units."""
    return work_unit_id[-16:] + ":"


def source_key(work_unit_id: str, modality: str, selection_id: str) -> str:
    """A reader's citation key: its selection under its modality, in the unit's tag."""
    return f"{unit_tag(work_unit_id)}{modality}:{selection_id}"


def text_source_quote(
    selection: Mapping[str, Any],
    package: CanonicalPaperPackage,
    *,
    document_revision_id: str,
    package_sha256: str,
    partners: Mapping[str, tuple[str, ...]],
    quote_index: TextQuoteIndex | None = None,
    source_phase: str = "MAIN",
) -> dict[str, Any]:
    """Rebuild an accepted TEXT read's exact extent from its recorded sentences.

    The caller supplies only previously delivered selections. An inherited source
    entry is not proof of delivery; this function does not grant that permission.
    """
    block_id = selection["canonical_source_id"]
    blocks = {block.block_id: block for block in package.blocks}
    block = blocks[block_id]
    spans = block_sentence_spans(block.raw_text)
    indices = [s["index"] for s in selection["sentences"]]
    if not indices or indices != list(range(indices[0], indices[-1] + 1)):
        raise ValueError("recorded text read is not one contiguous sentence range")
    if selection["modality"] != "TEXT" or any(
        spans[s["index"]].text != s["text"] for s in selection["sentences"]
    ):
        raise ValueError("recorded text read differs from the bound package")
    index = quote_index or TextQuoteIndex(package)
    if package_sha256 != index.package_sha256:
        raise ValueError("quote package identity differs from its verified input")
    bound: dict[str, Any] = {
        **index.bind(block_id, indices[0], indices[-1] + 1),
        "exact_text": block.raw_text[spans[indices[0]].char_start : spans[indices[-1]].char_end],
        "location": "source:" + block_id,
        "source_identity": {
            "package_sha256": package_sha256,
            "document_revision_id": document_revision_id,
            "source_id": block_id,
            "start_sentence": indices[0],
            "end_sentence": indices[-1] + 1,
            "source_phase": source_phase,
        },
    }
    if indices[-1] == len(spans) - 1:
        order = {b.block_id: i for i, b in enumerate(package.blocks)}
        later = [p for p in partners.get(block_id, ()) if order[p] > order[block_id]]
        if len(later) == 1:
            bound["continues_in"] = later[0]
    return bound


def source_location(
    entry: WorkUnitEvidenceEntry, sid: str, package: CanonicalPaperPackage
) -> dict[str, Any]:
    """What an entry's selection is, from its anchor in the package it was read from: a
    passage by the sentences it holds, a region by its box. The fusion is shown each source
    it may cite so (its task input and each read reply), and every task names a source the
    fusion read by it (`fusion_names.fusion_reads`), so the one derivation serves both."""
    blocks_by_id = {block.block_id: block for block in package.blocks}
    anchor = entry.source_observation["anchor"]
    assert isinstance(anchor, dict)
    location: dict[str, Any] = {
        "source_id": sid,
        "canonical_source_id": entry.canonical_source_id,
        "modality": entry.modality,
        # A model handed an id and no location tried the id; the location is the host's
        # to name (see the read location rule in resolve_root).
        "location": "source:" + entry.canonical_source_id,
    }
    if entry.modality == "TEXT":
        # Location and scope do not prove delivery. The runtime records actual source
        # reads and references in delivered observations, including inherited readings.
        location["sentences"] = [
            {"index": index, "text": span.text}
            for fragment in anchor["fragments"]
            for index, span in enumerate(
                block_sentence_spans(blocks_by_id[fragment["block_id"]].raw_text)
            )
            if span.char_start >= fragment["char_start"] and span.char_end <= fragment["char_end"]
        ]
    else:
        location["bbox_px"] = anchor["bbox_px"]
        location["source_dimensions"] = anchor["source_dimensions"]
        # The same box in the units and under the names a read's image_region takes
        # (edges normalized to 1000 of the image): shown only pixels, four of six
        # windows on a real paper wrote pixel edges into their first region read and
        # lost the turn to "less than or equal to 1000" (kalmanol r8, E56).
        width = anchor["source_dimensions"]["width_px"]
        height = anchor["source_dimensions"]["height_px"]
        box = anchor["bbox_px"]
        location["region"] = {
            "left": round(1000 * box["x_min_px"] / width),
            "top": round(1000 * box["y_min_px"] / height),
            "right": round(1000 * box["x_max_px"] / width),
            "bottom": round(1000 * box["y_max_px"] / height),
        }
    return location


def fusion_read_locations(
    registry: WorkUnitEvidenceRegistry, package: CanonicalPaperPackage
) -> dict[str, dict[str, Any]]:
    """Every source the fusion read itself, by the key its read minted, in the order it
    read them: the passage each cites (`source_location`).

    The host writes `FUSION_SELECTION_STAGE` into the originator of a selection it bound
    for the fusion's own read; a source a reader cited carries that reader's stage. This
    is how a task downstream tells the two apart, so a key the fusion was shown reads the
    same there as it did here, and one rule names both.
    """
    reads: dict[str, dict[str, Any]] = {}
    for entry in registry.entries:
        originator = entry.source_observation.get("originator")
        if not isinstance(originator, Mapping) or originator.get("stage_name") != (
            FUSION_SELECTION_STAGE
        ):
            continue
        sid = source_key(registry.work_unit_id, entry.modality, entry.selection_id)
        reads[sid] = source_location(entry, sid, package)
    return reads


def _materialize_selections(
    *,
    exact_work_unit: VerifiedWorkUnitInput,
    exact_package: CanonicalPaperPackage,
    visual_payloads: Mapping[str, bytes],
    execution_id: str,
    text_stage: str,
    visual_stage: str,
    text_selections: tuple[TextFragmentSelection, ...],
    visual_selections: tuple[VisualRegionSelection, ...],
    originators: dict[str, ObservationOriginator],
    created_from: dict[str, tuple[ArtifactRef, ...]],
    artifact_store: PrivateArtifactStore,
    source_inventory: LiteratureSourceInventory | None = None,
) -> tuple[WorkUnitEvidenceEntry, ...]:
    authority = resolve_fusion_read_authority(
        work_unit=exact_work_unit, package=exact_package, source_inventory=source_inventory
    )
    text_sources = {
        s.block_id: s
        for s in (
            _text_slices(exact_package, authority.text_block_ids)
            if source_inventory is not None
            else exact_work_unit.text_sources
        )
    }
    visual_sources = {
        s.visual_id: s
        for s in (
            _visual_slices(exact_package, authority.visual_ids)
            if source_inventory is not None
            else exact_work_unit.visual_sources
        )
    }
    # Validate every selection before writing any observation.
    for selection in text_selections:
        if (
            selection.block_id not in text_sources
            or selection.block_id not in authority.text_block_ids
        ):
            raise ValueError("text selection is not authorized by the work unit")
        resolve_text_selection(
            raw_text=text_sources[selection.block_id].raw_text,
            start_sentence=selection.start_sentence,
            end_sentence=selection.end_sentence,
        )
    for region in visual_selections:
        if region.visual_id not in visual_sources or region.visual_id not in authority.visual_ids:
            raise ValueError("visual selection is not authorized by the work unit")
        resolve_visual_selection(
            x0=region.x0,
            y0=region.y0,
            x1=region.x1,
            y1=region.y1,
            dimensions=visual_sources[region.visual_id].dimensions,
        )
    for visual_id in dict.fromkeys(region.visual_id for region in visual_selections):
        payload = visual_payloads[visual_id]
        source = visual_sources[visual_id].source_asset
        if (
            type(payload) is not bytes
            or len(payload) != source.byte_size
            or hashlib.sha256(payload).hexdigest() != source.sha256
        ):
            raise ValueError("visual payload differs from its verified source identity")
    verifier = HostVerificationContext(
        stage_name=_BINDING_STAGE,
        stage_version=_BINDING_STAGE_VERSION,
        execution_id=execution_id,
    )
    entries: list[WorkUnitEvidenceEntry] = []
    for ordinal, text_selection in enumerate(text_selections):
        resolved_text = resolve_text_selection(
            raw_text=text_sources[text_selection.block_id].raw_text,
            start_sentence=text_selection.start_sentence,
            end_sentence=text_selection.end_sentence,
        )
        observation_digest = _binding_digest(
            execution_id=execution_id,
            work_unit=exact_work_unit,
            stage=text_stage,
            selection_id=text_selection.selection_id,
            ordinal=ordinal,
            kind="observation",
        )
        anchor_digest = _binding_digest(
            execution_id=execution_id,
            work_unit=exact_work_unit,
            stage=text_stage,
            selection_id=text_selection.selection_id,
            ordinal=ordinal,
            kind="anchor",
        )
        observation = materialize_text_observation(
            exact_package,
            (
                EvidenceTextFragmentSelection(
                    fragment_index=0,
                    block_id=text_selection.block_id,
                    char_start=resolved_text.char_start,
                    char_end=resolved_text.char_end,
                ),
            ),
            "REACTION_DESCRIPTION",
            originators["TEXT"],
            context=_context(
                observation_id=f"OBS_3D_{observation_digest}",
                anchor_id=f"TANCH_3D_{anchor_digest}",
                work_unit=exact_work_unit,
                created_from=created_from["TEXT"],
            ),
            verifier=verifier,
        )
        observation_bytes = canonical_json_bytes(observation)
        observation_ref = seal_exact(
            artifact_store,
            observation_bytes,
            media_type=_OBSERVATION_MEDIA_TYPE,
        )
        entries.append(
            _entry(
                observation=observation,
                observation_ref=observation_ref,
                modality="TEXT",
                source_id=text_selection.block_id,
                selection_id=text_selection.selection_id,
                work_unit=exact_work_unit,
                read_authority=authority,
            )
        )

    for ordinal, visual_selection in enumerate(visual_selections):
        resolved_box = resolve_visual_selection(
            x0=visual_selection.x0,
            y0=visual_selection.y0,
            x1=visual_selection.x1,
            y1=visual_selection.y1,
            dimensions=visual_sources[visual_selection.visual_id].dimensions,
        )
        observation_digest = _binding_digest(
            execution_id=execution_id,
            work_unit=exact_work_unit,
            stage=visual_stage,
            selection_id=visual_selection.selection_id,
            ordinal=ordinal,
            kind="observation",
        )
        anchor_digest = _binding_digest(
            execution_id=execution_id,
            work_unit=exact_work_unit,
            stage=visual_stage,
            selection_id=visual_selection.selection_id,
            ordinal=ordinal,
            kind="anchor",
        )
        observation = materialize_visual_observation(
            exact_package,
            VisualAssetSelection(visual_id=visual_selection.visual_id),
            SourceImagePixelBBox(
                x_min_px=resolved_box.x_min_px,
                y_min_px=resolved_box.y_min_px,
                x_max_px=resolved_box.x_max_px,
                y_max_px=resolved_box.y_max_px,
            ),
            "REACTION_SCHEME",
            originators["VISUAL"],
            context=_context(
                observation_id=f"OBS_3D_{observation_digest}",
                anchor_id=f"VANCH_3D_{anchor_digest}",
                work_unit=exact_work_unit,
                created_from=created_from["VISUAL"],
            ),
            verifier=verifier,
        )
        observation_bytes = canonical_json_bytes(observation)
        observation_ref = seal_exact(
            artifact_store,
            observation_bytes,
            media_type=_OBSERVATION_MEDIA_TYPE,
        )
        entries.append(
            _entry(
                observation=observation,
                observation_ref=observation_ref,
                modality="VISUAL",
                source_id=visual_selection.visual_id,
                selection_id=visual_selection.selection_id,
                work_unit=exact_work_unit,
                read_authority=authority,
            )
        )

    return tuple(entries)


def bind_fusion_evidence(
    *,
    work_unit: VerifiedWorkUnitInput,
    source_reader: SourceReader,
    execution_id: str,
    text_selections: tuple[TextFragmentSelection, ...],
    visual_selections: tuple[VisualRegionSelection, ...],
    originator: ObservationOriginator,
    created_from: tuple[ArtifactRef, ...],
    artifact_store: PrivateArtifactStore,
    source_inventory: LiteratureSourceInventory | None = None,
) -> tuple[SealedWorkUnitEvidenceRegistry, dict[str, bytes]]:
    """Direct fusion selections retain their actual producer, never a fabricated reader."""
    exact_package = source_reader.package
    if exact_package.canonical_content_sha256 != work_unit.package_sha256:
        raise ValueError("fusion source package differs from its authority")
    if text_selections:
        source_reader.verify_text()
    visual_payloads = (
        source_reader.read_visuals(
            work_unit=work_unit,
            visual_ids=tuple(region.visual_id for region in visual_selections),
            source_inventory=source_inventory,
        )
        if visual_selections
        else {}
    )
    entries = _materialize_selections(
        exact_work_unit=work_unit,
        exact_package=exact_package,
        visual_payloads=visual_payloads,
        execution_id=execution_id,
        text_stage="FUSION_TEXT",
        visual_stage="FUSION_VISUAL",
        text_selections=text_selections,
        visual_selections=visual_selections,
        originators={"TEXT": originator, "VISUAL": originator},
        created_from={"TEXT": created_from, "VISUAL": created_from},
        artifact_store=artifact_store,
        source_inventory=source_inventory,
    )
    registry = WorkUnitEvidenceRegistry(
        schema_version=_BINDING_SCHEMA_VERSION,
        execution_id=execution_id,
        paper_id=work_unit.paper_id,
        document_revision_id=work_unit.document_revision_id,
        source_phase="MAIN",
        package_sha256=work_unit.package_sha256,
        work_unit_id=work_unit.work_unit_id,
        work_unit_sha256=work_unit.work_unit_sha256,
        source_view_sha256=(
            source_inventory.inventory_sha256 if source_inventory else work_unit.source_view_sha256
        ),
        entries=entries,
        readings=(),
    )
    ref = seal_exact(
        artifact_store,
        canonical_json_bytes(registry.model_dump(mode="json")),
        media_type=_REGISTRY_MEDIA_TYPE,
    )
    return SealedWorkUnitEvidenceRegistry(registry, ref), visual_payloads
