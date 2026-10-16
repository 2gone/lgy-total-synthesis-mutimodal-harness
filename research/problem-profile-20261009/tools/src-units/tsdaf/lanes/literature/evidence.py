"""Deterministic Host reconstruction of frozen SourceObservation roots."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from tsdaf.contracts.literature.schema import FactRootKind, validate_fact_root
from tsdaf.contracts.package import CanonicalPaperPackage, SourcePhase, stable_revision_id
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef, BoundingBox
from tsdaf.lanes.document.page_furniture import page_furniture
from tsdaf.lanes.literature.selection_resolution import (
    normalize_pixel_extent,
    resolve_visual_selection,
)

__all__ = (
    "HostVerificationContext",
    "ObservationContext",
    "ObservationOriginator",
    "SourceImagePixelBBox",
    "TextFragmentSelection",
    "TextValueRange",
    "VisualAssetSelection",
    "furniture_by_package",
    "keyed_observation",
    "materialize_text_observation",
    "materialize_visual_observation",
    "merge_keyed_observations",
    "observation_anchor_key",
)

_SHA256_LENGTH = 64


def _require_string(value: str, *, label: str) -> None:
    if not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{label} must be a nonempty canonical string")


def _require_sha256(value: str, *, label: str) -> None:
    if len(value) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: object) -> str:
    return _sha256_bytes(canonical_json_bytes(value))


@dataclass(frozen=True, slots=True)
class TextFragmentSelection:
    """Model-selected canonical block-local range with no caller-controlled text or hash."""

    fragment_index: int
    block_id: str
    char_start: int
    char_end: int

    def __post_init__(self) -> None:
        if self.fragment_index < 0:
            raise ValueError("fragment index must be non-negative")
        _require_string(self.block_id, label="fragment block id")
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ValueError("fragment range must have positive block-local extent")


@dataclass(frozen=True, slots=True)
class TextValueRange:
    """A payload token range relative to one Host-materialized fragment."""

    fragment_index: int
    char_start: int
    char_end: int

    def __post_init__(self) -> None:
        if self.fragment_index < 0 or self.char_start < 0 or self.char_end <= self.char_start:
            raise ValueError("value range must have positive fragment-local extent")


@dataclass(frozen=True, slots=True)
class VisualAssetSelection:
    """A canonical visual ID, with asset identity left exclusively to the Host."""

    visual_id: str

    def __post_init__(self) -> None:
        _require_string(self.visual_id, label="visual id")


@dataclass(frozen=True, slots=True)
class SourceImagePixelBBox:
    """Positive-area coordinates in the original package image pixel domain."""

    x_min_px: int
    y_min_px: int
    x_max_px: int
    y_max_px: int

    def __post_init__(self) -> None:
        values = (self.x_min_px, self.y_min_px, self.x_max_px, self.y_max_px)
        if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
            raise TypeError("source image bbox coordinates must be integers")
        if (
            self.x_min_px < 0
            or self.y_min_px < 0
            or self.x_max_px <= self.x_min_px
            or self.y_max_px <= self.y_min_px
        ):
            raise ValueError("source image bbox must have non-negative positive-area coordinates")


@dataclass(frozen=True, slots=True)
class ObservationOriginator:
    """Typed public originator payload supplied from the authorized execution context."""

    originator_kind: Literal["HOST", "MODEL", "HUMAN"]
    stage_name: str
    stage_version: str
    execution_id: str
    model_profile: str | None
    prompt_sha256: str | None
    provider_schema_sha256: str | None
    requested_by_reviewer_id: str | None
    base_artifact_sha256: str

    def __post_init__(self) -> None:
        _require_string(self.stage_name, label="originator stage name")
        _require_string(self.stage_version, label="originator stage version")
        _require_string(self.execution_id, label="originator execution id")
        _require_sha256(self.base_artifact_sha256, label="originator base artifact hash")


@dataclass(frozen=True, slots=True)
class ObservationContext:
    """Explicit stable identity and payload inputs that cannot be inferred from a package."""

    observation_id: str
    anchor_id: str
    document_revision_id: str
    source_phase: Literal["MAIN", "SI"]
    payload: Mapping[str, object]
    producer_note: str | None
    created_from_artifact_refs: tuple[ArtifactRef, ...]
    # The paper the observation belongs to when that is not the package's own id: a
    # supporting-information package paired to its main paper by DOI keeps its intake
    # id, and its observations are the main paper's (ledger E76 (2)).
    paper_id: str | None = None

    def __post_init__(self) -> None:
        _require_string(self.observation_id, label="observation id")
        if self.paper_id is not None:
            _require_string(self.paper_id, label="observation paper id")
        _require_string(self.anchor_id, label="anchor id")
        _require_string(self.document_revision_id, label="document revision id")
        if not isinstance(self.created_from_artifact_refs, tuple) or any(
            not isinstance(reference, ArtifactRef) for reference in self.created_from_artifact_refs
        ):
            raise TypeError("created_from_artifact_refs must be a tuple of ArtifactRef values")


@dataclass(frozen=True, slots=True)
class HostVerificationContext:
    """Typed identity of the Host verifier that performs all reconstruction."""

    stage_name: str
    stage_version: str
    execution_id: str

    def __post_init__(self) -> None:
        _require_string(self.stage_name, label="verifier stage name")
        _require_string(self.stage_version, label="verifier stage version")
        _require_string(self.execution_id, label="verifier execution id")


def _payload_contains_hash(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            (isinstance(key, str) and key.endswith("sha256")) or _payload_contains_hash(child)
            for key, child in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return any(_payload_contains_hash(child) for child in value)
    return False


def _validate_host_trust(
    package: CanonicalPaperPackage,
    originator: ObservationOriginator,
    context: ObservationContext,
    verifier: HostVerificationContext,
) -> None:
    if originator.execution_id != verifier.execution_id:
        raise ValueError("originator and verifier execution identities must match")
    created_hashes = {reference.sha256 for reference in context.created_from_artifact_refs}
    if (
        originator.base_artifact_sha256 != package.canonical_content_sha256
        and originator.base_artifact_sha256 not in created_hashes
    ):
        raise ValueError("originator base artifact is not the package or a created artifact")
    if originator.originator_kind == "MODEL":
        required_hashes = (originator.prompt_sha256, originator.provider_schema_sha256)
        if any(value is None or value not in created_hashes for value in required_hashes):
            raise ValueError("MODEL provenance hashes require matching created artifacts")
    if context.payload.get("payload_kind") == "VISUAL_REACTION_ARROW":
        related = context.payload.get("related_label_evidence_refs")
        if related != [] and related != ():
            raise ValueError(
                "related_label_evidence_refs must be empty until Host registry linking"
            )
    if _payload_contains_hash(context.payload):
        raise ValueError("caller payload hashes are not accepted by the Host materializer")


def _public_created_refs(references: Sequence[ArtifactRef]) -> list[dict[str, object]]:
    return [
        {
            "sha256": reference.sha256,
            "media_type": reference.media_type,
            "byte_size": reference.byte_size,
            "logical_name": None,
        }
        for reference in references
    ]


def _verification_input_sha256(
    package: CanonicalPaperPackage,
    anchor: Mapping[str, object],
    verifier: HostVerificationContext,
) -> str:
    return _canonical_sha256(
        {
            "package_sha256": package.canonical_content_sha256,
            "anchor": anchor,
            "verifier_version": verifier.stage_version,
        }
    )


def _materialize_text_payload(
    payload: Mapping[str, object], exact_fragments: Sequence[str]
) -> dict[str, object]:
    if "raw_token_texts" in payload:
        raise ValueError("raw_token_texts must be reconstructed by the Host")
    payload_kind = payload.get("payload_kind")
    if payload_kind != "TEXT_FIELD_EVIDENCE":
        return copy.deepcopy(dict(payload))
    if set(payload) != {"payload_kind", "candidate_field_refs", "value_ranges"}:
        raise ValueError("text evidence payload has invalid Host-input fields")
    ranges_value = payload["value_ranges"]
    if not isinstance(ranges_value, Sequence) or isinstance(ranges_value, str | bytes):
        raise TypeError("text value_ranges must be a sequence")
    ranges = tuple(ranges_value)
    if not ranges or any(not isinstance(item, TextValueRange) for item in ranges):
        raise ValueError("text value_ranges must contain typed nonempty ranges")
    typed_ranges = tuple(item for item in ranges if isinstance(item, TextValueRange))
    ordering = tuple((item.fragment_index, item.char_start, item.char_end) for item in typed_ranges)
    if ordering != tuple(sorted(ordering)) or len(ordering) != len(set(ordering)):
        raise ValueError("text value ranges must be unique and canonically ordered")
    previous_by_fragment: dict[int, int] = {}
    tokens: list[str] = []
    for item in typed_ranges:
        if item.fragment_index >= len(exact_fragments):
            raise ValueError("value range references a missing fragment")
        fragment = exact_fragments[item.fragment_index]
        if item.char_end > len(fragment):
            raise ValueError("value range exceeds its verified fragment")
        if item.char_start < previous_by_fragment.get(item.fragment_index, 0):
            raise ValueError("text value ranges overlap")
        previous_by_fragment[item.fragment_index] = item.char_end
        tokens.append(fragment[item.char_start : item.char_end])
    return {
        "payload_kind": "TEXT_FIELD_EVIDENCE",
        "candidate_field_refs": copy.deepcopy(payload["candidate_field_refs"]),
        "value_ranges": [asdict(item) for item in typed_ranges],
        "raw_token_texts": tokens,
    }


def materialize_text_observation(
    package: CanonicalPaperPackage,
    fragments: Sequence[TextFragmentSelection],
    role: str,
    originator: ObservationOriginator,
    *,
    context: ObservationContext,
    verifier: HostVerificationContext,
) -> dict[str, object]:
    """Re-slice ordered selections and emit one strict public TEXT observation."""

    if type(package) is not CanonicalPaperPackage:
        raise TypeError("package must be a CanonicalPaperPackage")
    if not isinstance(originator, ObservationOriginator):
        raise TypeError("originator must be an ObservationOriginator")
    if not isinstance(context, ObservationContext):
        raise TypeError("context must be an ObservationContext")
    if not isinstance(verifier, HostVerificationContext):
        raise TypeError("verifier must be a HostVerificationContext")
    _validate_host_trust(package, originator, context, verifier)
    selections = tuple(fragments)
    if not selections or any(not isinstance(item, TextFragmentSelection) for item in selections):
        raise ValueError("fragments must contain typed nonempty selections")
    if tuple(item.fragment_index for item in selections) != tuple(range(len(selections))):
        raise ValueError("fragment indices must match their ordered positions")
    identities = tuple((item.block_id, item.char_start, item.char_end) for item in selections)
    if len(identities) != len(set(identities)):
        raise ValueError("fragment selections contain a duplicate")

    blocks_by_id = {block.block_id: block for block in package.blocks}
    exact_fragments: list[str] = []
    anchor_fragments: list[dict[str, object]] = []
    selected_blocks: set[str] = set()
    for selection in selections:
        block = blocks_by_id.get(selection.block_id)
        if block is None:
            raise ValueError("fragment block does not belong to the canonical package")
        if selection.char_end > len(block.raw_text):
            raise ValueError("fragment range exceeds its canonical block")
        exact = block.raw_text[selection.char_start : selection.char_end]
        exact_fragments.append(exact)
        selected_blocks.add(block.block_id)
        anchor_fragments.append(
            {
                "block_id": block.block_id,
                "char_start": selection.char_start,
                "char_end": selection.char_end,
                "fragment_sha256": _sha256_bytes(exact.encode("utf-8")),
            }
        )

    selected_in_canonical_order = tuple(
        block for block in package.blocks if block.block_id in selected_blocks
    )
    page_indices = sorted({block.page_index for block in selected_in_canonical_order})
    section_paths: list[list[str]] = []
    seen_sections: set[tuple[str, ...]] = set()
    for block in selected_in_canonical_order:
        if block.section_path not in seen_sections:
            seen_sections.add(block.section_path)
            section_paths.append(list(block.section_path))
    anchor: dict[str, object] = {
        "anchor_id": context.anchor_id,
        "document_revision_id": context.document_revision_id,
        "fragments": anchor_fragments,
        "joined_text_sha256": _canonical_sha256(exact_fragments),
        "page_indices": page_indices,
        "section_paths": section_paths,
    }
    output_payload = _materialize_text_payload(context.payload, exact_fragments)
    originator_payload = asdict(originator)
    observation: dict[str, object] = {
        "schema_version": "tsdaf.source-observation.v1",
        "observation_id": context.observation_id,
        "paper_id": context.paper_id or package.manifest.paper_id,
        "document_revision_id": context.document_revision_id,
        "source_phase": context.source_phase,
        "kind": "TEXT",
        "anchor": anchor,
        "semantic_role": role,
        "payload": output_payload,
        "originator": originator_payload,
        "verification": {
            "verifier_kind": "HOST",
            "verifier_stage": verifier.stage_name,
            "verifier_version": verifier.stage_version,
            "execution_id": verifier.execution_id,
            "verified_package_sha256": package.canonical_content_sha256,
            "verified_anchor_sha256": _canonical_sha256(anchor),
            "verification_input_sha256": _verification_input_sha256(package, anchor, verifier),
        },
        "producer_note": context.producer_note,
        "created_from_artifact_refs": _public_created_refs(context.created_from_artifact_refs),
    }
    if validate_fact_root(observation) is not FactRootKind.OBSERVATION:
        raise ValueError("materialized value is not a SourceObservation")
    return observation


def visual_label(package: CanonicalPaperPackage, visual_id: str) -> str | None:
    """The author's own label for a figure, as its group is printed ("Scheme 2"), or None
    where the group carries none. What a task reads the figure by (batch 2A); its caption
    is the evidence's longer name (`_visual_logical_name`)."""
    visual = next(item for item in package.visual_assets if item.visual_id == visual_id)
    group = next(
        (item for item in package.visual_groups if item.visual_group_id == visual.visual_group_id),
        None,
    )
    return group.label_candidates[0] if group is not None and group.label_candidates else None


# Said in place of a caption the source does not hold as text, so that a reader does
# not go looking for one.
NO_CAPTION_TEXT = (
    "The source holds no caption text for this figure: whatever legend it has is printed "
    "inside the image."
)


def visual_caption(package: CanonicalPaperPackage, visual_id: str) -> str:
    """The caption a reader of this figure is shown: its own, else its group's, else why none.

    A scheme's caption is often held on its group rather than on the image (the image's own
    is empty): across two papers (2026-10-10) tasks that had read "Scheme 2" went on to
    search "Scheme 2 caption", "Scheme 2. Reagents", because the read had answered null.
    Only a group whose caption was confirmed for it is used; an ambiguous one may belong to
    the table beside it.
    """
    visual = next(item for item in package.visual_assets if item.visual_id == visual_id)
    if visual.native_caption:
        return visual.native_caption
    group = next(
        (item for item in package.visual_groups if item.visual_group_id == visual.visual_group_id),
        None,
    )
    if group is not None and group.caption_text and group.association_status == "CONFIRMED":
        return group.caption_text
    return NO_CAPTION_TEXT


def _visual_logical_name(package: CanonicalPaperPackage, visual_id: str) -> str | None:
    visual = next(item for item in package.visual_assets if item.visual_id == visual_id)
    if visual.native_caption is not None:
        return visual.native_caption
    group = next(
        (item for item in package.visual_groups if item.visual_group_id == visual.visual_group_id),
        None,
    )
    if group is not None and group.label_candidates:
        return group.label_candidates[0]
    return None


def materialize_visual_observation(
    package: CanonicalPaperPackage,
    asset_ref: VisualAssetSelection,
    bbox: SourceImagePixelBBox,
    role: str,
    originator: ObservationOriginator,
    *,
    context: ObservationContext,
    verifier: HostVerificationContext,
) -> dict[str, object]:
    """Emit a VISUAL observation from one original package image and pixel bbox."""

    if type(package) is not CanonicalPaperPackage:
        raise TypeError("package must be a CanonicalPaperPackage")
    if not isinstance(asset_ref, VisualAssetSelection):
        raise TypeError("asset_ref must be a VisualAssetSelection")
    if isinstance(bbox, BoundingBox):
        raise TypeError("bbox must use the source_image_px coordinate domain, not PDF geometry")
    if not isinstance(bbox, SourceImagePixelBBox):
        raise TypeError("bbox must be a SourceImagePixelBBox in the source_image_px domain")
    if not isinstance(originator, ObservationOriginator):
        raise TypeError("originator must be an ObservationOriginator")
    if not isinstance(context, ObservationContext):
        raise TypeError("context must be an ObservationContext")
    if not isinstance(verifier, HostVerificationContext):
        raise TypeError("verifier must be a HostVerificationContext")
    _validate_host_trust(package, originator, context, verifier)

    visual = next(
        (item for item in package.visual_assets if item.visual_id == asset_ref.visual_id),
        None,
    )
    if visual is None:
        raise ValueError("visual asset does not belong to the canonical package")
    if visual.asset not in package.manifest.assets:
        raise ValueError("visual asset identity or size does not match the package manifest")
    if visual.source.source_asset_ref != visual.asset:
        raise ValueError("visual asset identity does not match its source locator")
    if visual.source.page_index != visual.page_index:
        raise ValueError("visual asset page identity does not match its source locator")
    if not visual.asset.media_type.startswith("image/"):
        raise ValueError("visual source asset must be an original package image")
    if visual.width is None or visual.height is None:
        raise ValueError("visual source dimensions are missing")
    width = visual.width
    height = visual.height
    if bbox.x_max_px > width or bbox.y_max_px > height:
        raise ValueError("source image bbox exceeds verified original image dimensions")

    normalized = {
        "x_min": normalize_pixel_extent(bbox.x_min_px, width),
        "y_min": normalize_pixel_extent(bbox.y_min_px, height),
        "x_max": normalize_pixel_extent(bbox.x_max_px, width),
        "y_max": normalize_pixel_extent(bbox.y_max_px, height),
    }
    if normalized["x_min"] >= normalized["x_max"] or normalized["y_min"] >= normalized["y_max"]:
        raise ValueError("source image bbox collapses after normalized_1000 conversion")
    public_asset: dict[str, object] = {
        "sha256": visual.asset.sha256,
        "media_type": visual.asset.media_type,
        "byte_size": visual.asset.byte_size,
        "logical_name": _visual_logical_name(package, visual.visual_id),
    }
    bbox_px = asdict(bbox)
    anchor_input: dict[str, object] = {
        "asset": visual.asset.sha256,
        "page": visual.page_index,
        "dimensions": [width, height],
        "bbox": [
            normalized["x_min"],
            normalized["y_min"],
            normalized["x_max"],
            normalized["y_max"],
        ],
    }
    anchor: dict[str, object] = {
        "anchor_id": context.anchor_id,
        "document_revision_id": context.document_revision_id,
        "source_asset": public_asset,
        "page_index": visual.page_index,
        "source_dimensions": {"width_px": width, "height_px": height},
        "bbox_norm_1000": normalized,
        "bbox_px": bbox_px,
        "anchor_sha256": _canonical_sha256(anchor_input),
    }
    output_payload = copy.deepcopy(dict(context.payload))
    originator_payload = asdict(originator)
    observation: dict[str, object] = {
        "schema_version": "tsdaf.source-observation.v1",
        "observation_id": context.observation_id,
        "paper_id": context.paper_id or package.manifest.paper_id,
        "document_revision_id": context.document_revision_id,
        "source_phase": context.source_phase,
        "kind": "VISUAL",
        "anchor": anchor,
        "semantic_role": role,
        "payload": output_payload,
        "originator": originator_payload,
        "verification": {
            "verifier_kind": "HOST",
            "verifier_stage": verifier.stage_name,
            "verifier_version": verifier.stage_version,
            "execution_id": verifier.execution_id,
            "verified_package_sha256": package.canonical_content_sha256,
            "verified_anchor_sha256": _canonical_sha256(anchor),
            "verification_input_sha256": _verification_input_sha256(package, anchor, verifier),
        },
        "producer_note": context.producer_note,
        "created_from_artifact_refs": _public_created_refs(context.created_from_artifact_refs),
    }
    if validate_fact_root(observation) is not FactRootKind.OBSERVATION:
        raise ValueError("materialized value is not a SourceObservation")
    return observation


# ---------------------------------------------------------------------------------
# The public observation: keyed by its anchor, free of run coordinates (E66 (4), E83).
# ---------------------------------------------------------------------------------

KEYED_OBSERVATION_PREFIX = "OBS-"
KEYED_ANCHOR_PREFIX = "ANCH-"
# This identifies the deterministic public anchor projection, not the producer
# that verified the original source. Its complete verification stays in the
# sealed originals referenced by publication provenance.
PUBLIC_ANCHOR_PROJECTION_STAGE = "literature-public-anchor-projection"
PUBLIC_ANCHOR_PROJECTION_VERSION = "1.0.0"


def _digest16(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()[:16]


def _refs(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("observation field is not a mapping")
    return dict(value)


def observation_anchor_key(observation: Mapping[str, object]) -> str:
    """The human-readable key a public observation's id derives from: the package the
    site was read in, where the site is, what was read there, and as what.

    Text: the package hash and the phase it was read as, the fragments (block and
    character range), the hash of the joined text, the semantic role, the payload kind,
    and a digest of the reading (the payload without the fields it was cited for).
    Visual: the same with the anchor's own content hash (asset, page, dimensions, box)
    as the site. Nothing about the run: two runs that read the same site the same way
    get the same key, and the same id.
    """
    verification = observation["verification"]
    anchor = observation["anchor"]
    payload = observation["payload"]
    assert isinstance(verification, Mapping) and isinstance(anchor, Mapping)
    assert isinstance(payload, Mapping)
    package_sha = str(verification["verified_package_sha256"])
    kind = str(observation["kind"])
    if kind == "TEXT":
        fragments = anchor["fragments"]
        assert isinstance(fragments, Sequence)
        site = ",".join(
            f"{f['block_id']}:{f['char_start']}-{f['char_end']}"
            for f in fragments
            if isinstance(f, Mapping)
        )
        content = str(anchor["joined_text_sha256"])
    else:
        site = str(anchor["anchor_sha256"])
        content = site
    reading = _digest16({k: v for k, v in payload.items() if k != "candidate_field_refs"})
    return "|".join(
        (
            package_sha,
            str(observation["source_phase"]),
            kind,
            site,
            content,
            str(observation["semantic_role"]),
            str(payload["payload_kind"]),
            reading,
        )
    )


def keyed_observation(observation: Mapping[str, object]) -> dict[str, object]:
    """The public form of an observation: its id and anchor id derived from its anchor
    key, its document revision the stable one, and no run coordinate in its bytes --
    no originator, no verifying execution, no artifacts it was created from (all of
    which stay in the binding record that sealed it). Applying it twice is the same
    as once, and two observations of one site from two runs come out byte-identical.
    """
    key = observation_anchor_key(observation)
    public = copy.deepcopy(dict(observation))
    anchor = _mapping(public["anchor"])
    verification = _mapping(public["verification"])
    if public["kind"] == "VISUAL" and anchor.get("bbox_px") is not None:
        # The site is the normalized box (it is what the key digests); the pixel box a
        # reading arrived with can differ by a pixel between two readings of one site,
        # so the public form derives it from the normalized box the one way the crop
        # resolver does, and two readings of a site are the same bytes.
        normalized = _mapping(anchor["bbox_norm_1000"])
        dimensions = _mapping(anchor["source_dimensions"])
        anchor["bbox_px"] = asdict(
            resolve_visual_selection(
                x0=int(normalized["x_min"]),  # type: ignore[call-overload]
                y0=int(normalized["y_min"]),  # type: ignore[call-overload]
                x1=int(normalized["x_max"]),  # type: ignore[call-overload]
                y1=int(normalized["y_max"]),  # type: ignore[call-overload]
                dimensions=(
                    int(dimensions["width_px"]),  # type: ignore[call-overload]
                    int(dimensions["height_px"]),  # type: ignore[call-overload]
                ),
            )
        )
    phase: SourcePhase = "SI" if public["source_phase"] == "SI" else "MAIN"
    revision = stable_revision_id(phase, str(verification["verified_package_sha256"]))
    digest = _digest16(key)
    anchor["anchor_id"] = KEYED_ANCHOR_PREFIX + digest
    anchor["document_revision_id"] = revision
    verification.pop("execution_id", None)
    verification["verifier_stage"] = PUBLIC_ANCHOR_PROJECTION_STAGE
    verification["verifier_version"] = PUBLIC_ANCHOR_PROJECTION_VERSION
    verification["verified_anchor_sha256"] = _canonical_sha256(anchor)
    verification["verification_input_sha256"] = _canonical_sha256(
        {
            "package_sha256": verification["verified_package_sha256"],
            "anchor": anchor,
            "verifier_version": verification["verifier_version"],
        }
    )
    public.pop("originator", None)
    payload = _mapping(public["payload"])
    refs = payload.get("candidate_field_refs")
    if isinstance(refs, list):
        payload["candidate_field_refs"] = sorted(refs, key=canonical_json_bytes)
    public["payload"] = payload
    public["observation_id"] = KEYED_OBSERVATION_PREFIX + digest
    public["document_revision_id"] = revision
    public["anchor"] = anchor
    public["verification"] = verification
    public["anchor_key"] = key
    public["created_from_artifact_refs"] = []
    if validate_fact_root(public) is not FactRootKind.OBSERVATION:
        raise ValueError("keyed value is not a SourceObservation")
    return public


def merge_keyed_observations(
    observations: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    """Keyed observations by id, one per site: readings of one site cited for
    different fields fold into one observation citing all of them; anything else that
    differs under one key is a Host defect, not a merge."""
    merged: dict[str, dict[str, object]] = {}
    for observation in observations:
        public = keyed_observation(observation)
        identifier = str(public["observation_id"])
        existing = merged.get(identifier)
        if existing is None:
            merged[identifier] = public
            continue
        payload = _mapping(existing["payload"])
        incoming = _mapping(public["payload"])
        refs = _refs(payload.get("candidate_field_refs"))
        for ref in _refs(incoming.get("candidate_field_refs")):
            if ref not in refs:
                refs.append(ref)
        payload["candidate_field_refs"] = sorted(refs, key=canonical_json_bytes)
        existing["payload"] = payload
        if existing.get("producer_note") is None:
            existing["producer_note"] = public.get("producer_note")
        rest = {k: v for k, v in public.items() if k not in {"payload", "producer_note"}}
        kept = {k: v for k, v in existing.items() if k not in {"payload", "producer_note"}}
        if rest != kept:
            differing = sorted(k for k in rest if rest[k] != kept.get(k))
            raise ValueError(
                f"observation {identifier} was read two ways under one key: {differing} "
                f"{[(rest[k], kept.get(k)) for k in differing]}"
            )
    return merged


def furniture_by_package(
    packages: Sequence[CanonicalPaperPackage],
) -> dict[str, str | None]:
    """The page's own printing of each package, by the package's content hash.

    `observation_texts` recovers a source by its original offsets, footer and all, so
    every text comparison against a recovered source sets the page's printing aside
    first (E140). This is the one place that printing is computed for a set of
    packages: the publication paths and the composition report all take it from
    here, so "which footer to clear" is one rule, not one per caller. A package
    that prints nothing maps to None.
    """
    return {
        package.canonical_content_sha256: page_furniture(
            [(block.block_id, block.raw_text) for block in package.blocks]
        )
        for package in packages
    }


def observation_texts(
    observations: Sequence[Mapping[str, Any]],
    packages: Sequence[CanonicalPaperPackage],
) -> dict[str, str]:
    """Recover only hash-verified TEXT anchor slices from available canonical packages.

    Inverse of materialize_text_observation's fragment rules. Missing packages,
    images and invalid anchors supply no text; payload/model prose is never a source.
    Revision labels can be aliases, so the verifier's exact package hash binds bytes.
    """
    by_hash = {package.canonical_content_sha256: package for package in packages}
    recovered: dict[str, str] = {}
    for observation in observations:
        if observation.get("kind") != "TEXT":
            continue
        verification = observation.get("verification") or {}
        package = by_hash.get(str(verification.get("verified_package_sha256")))
        if package is None:
            continue
        anchor = observation.get("anchor") or {}
        fragments = anchor.get("fragments") or []
        if not fragments:
            continue
        blocks = {block.block_id: block for block in package.blocks}
        exact: list[str] = []
        for fragment in fragments:
            block = blocks.get(str(fragment.get("block_id")))
            start, end = fragment.get("char_start"), fragment.get("char_end")
            if (
                block is None
                or type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(block.raw_text)
            ):
                break
            text = block.raw_text[start:end]
            if _sha256_bytes(text.encode("utf-8")) != fragment.get("fragment_sha256"):
                break
            exact.append(text)
        if len(exact) == len(fragments) and _canonical_sha256(exact) == anchor.get(
            "joined_text_sha256"
        ):
            # Preserve the selected order; a newline prevents fragments becoming one word.
            recovered[str(observation["observation_id"])] = "\n".join(exact)
    return recovered
