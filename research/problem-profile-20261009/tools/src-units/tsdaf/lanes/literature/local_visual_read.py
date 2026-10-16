"""Deterministic display derivatives after the existing source authorization/read.

This is an internal materializer, not a provider tool or an authorization entry point.
The caller binds its returned descriptor to the existing citable source location.
Never use a previous display derivative as the input for a subsequent read.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from io import BytesIO

from PIL import Image

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.host_ports import PrivateArtifactStore, VerifiedArtifactStore
from tsdaf.lanes.literature.selection_resolution import ResolvedPixelBox
from tsdaf.lanes.literature.source_scope import VisualSourceSlice


@dataclass(frozen=True, slots=True)
class LocalVisualRead:
    """Exact delivered bytes and the sealed source-to-display mapping."""

    payload: bytes
    image_ref: ArtifactRef
    mapping_ref: ArtifactRef
    sampled_box: ResolvedPixelBox
    dimensions: tuple[int, int]

    def source_frame(self, location: str, dimensions: tuple[int, int]) -> dict[str, object]:
        """Model-facing coordinate reference; the immutable image mapping is unchanged."""
        box = self.sampled_box
        return {
            "frame": "source",
            "location": location,
            "dimensions": list(dimensions),
            "coordinate_scale": 1000,
            "display_bounds_px": {
                "left": box.x_min_px,
                "top": box.y_min_px,
                "right": box.x_max_px,
                "bottom": box.y_max_px,
            },
        }


def read_cached_local_visual_read(
    *,
    source: VisualSourceSlice,
    original_bytes: bytes,
    mapping_ref: ArtifactRef,
    store: VerifiedArtifactStore,
) -> LocalVisualRead:
    """Reopen one selected cached view without re-cropping, re-encoding or writing.

    The caller first resolves a task-authorized saved location to its mapping_ref,
    and checks current source access through the existing source reader. A model
    supplied arbitrary hash is not a saved location and must not reach this helper.
    This does not enqueue images: request composition chooses which views to send.
    """
    source = VisualSourceSlice.model_validate(source.model_dump(mode="python"))
    _check_original_bytes(source, original_bytes)
    if mapping_ref.media_type != "application/vnd.tsdaf.local-visual-read+json":
        raise ValueError("cached view requires a display mapping reference")
    mapping_bytes = _read_exact(store, mapping_ref)
    mapping = json.loads(mapping_bytes)
    if not isinstance(mapping, dict):
        raise ValueError("cached view mapping must be an object")
    if (
        mapping.get("schema_version") != "tsdaf.local-visual-read.v1"
        or mapping.get("visual_id") != source.visual_id
        or mapping.get("source_asset") != source.source_asset.model_dump(mode="json")
        or mapping.get("source_dimensions") != list(source.dimensions)
    ):
        raise ValueError("cached view belongs to a different source")
    box = ResolvedPixelBox(**mapping["sampled_box"])
    edges = _checked_edges(box, source.dimensions)
    sampling = mapping.get("sampling")
    if sampling not in {"original-pixels", "overview-lanczos-1024"}:
        raise ValueError("cached view has an unknown sampling policy")
    if sampling == "overview-lanczos-1024" and edges != (0, 0, *source.dimensions):
        raise ValueError("cached overview must cover the whole original")
    image_ref = ArtifactRef.model_validate(mapping["image_ref"])
    if image_ref.media_type != "image/png":
        raise ValueError("cached display must be PNG")
    payload = _read_exact(store, image_ref)
    with Image.open(BytesIO(payload)) as image:
        image.load()
        dimensions = image.size
        if image.format != "PNG" or list(dimensions) != mapping["display_dimensions"]:
            raise ValueError("cached display differs from its mapped dimensions or format")
    if sampling == "original-pixels" and dimensions != (edges[2] - edges[0], edges[3] - edges[1]):
        raise ValueError("cached detail dimensions differ from the original pixel extent")
    if sampling == "overview-lanczos-1024" and max(dimensions) > 1024:
        raise ValueError("cached overview exceeds its display size")
    return LocalVisualRead(payload, image_ref, mapping_ref, box, dimensions)


def materialize_local_visual_read(
    *,
    source: VisualSourceSlice,
    original_bytes: bytes,
    sampled_box: ResolvedPixelBox,
    store: PrivateArtifactStore,
    overview: bool = False,
) -> LocalVisualRead:
    """Sample exact original pixels; only an explicit overview may downsample.

    Authorization and filesystem checks remain in the existing source reader. Byte
    identity is checked again here so a mismatched payload cannot acquire provenance.
    The box is already resolved by the existing selection resolver or a saved location;
    it must be fully inside the source. No padding, hidden expansion or super-resolution.
    """
    source = VisualSourceSlice.model_validate(source.model_dump(mode="python"))
    if type(overview) is not bool:
        raise TypeError("overview must be a boolean")
    edges = _checked_edges(sampled_box, source.dimensions)
    # Revalidate even a box supplied through an unchecked object construction.
    sampled_box = ResolvedPixelBox(*edges)
    width, height = source.dimensions
    if overview and edges != (0, 0, width, height):
        raise ValueError("overview requires the whole original image")
    identity = source.source_asset
    _check_original_bytes(source, original_bytes)
    with Image.open(BytesIO(original_bytes)) as original:
        if original.size != source.dimensions:
            raise ValueError("original image dimensions differ from the source")
        # Supported original formats remain governed by the source reader.
        original.load()
        sampled = original.crop(edges)
        if overview:
            sampled.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        # Strip incidental EXIF/ICC metadata: provenance lives in the mapping.
        clean = Image.new(sampled.mode, sampled.size)
        clean.paste(sampled)
        clean.save(buffer, format="PNG")
        payload = buffer.getvalue()
        dimensions = sampled.size
    image_ref = _store_exact(store, payload, "image/png")
    mapping_ref = _store_exact(
        store,
        canonical_json_bytes(
            {
                "schema_version": "tsdaf.local-visual-read.v1",
                "visual_id": source.visual_id,
                "source_asset": identity.model_dump(mode="json"),
                "source_dimensions": source.dimensions,
                "sampled_box": asdict(sampled_box),
                "display_dimensions": dimensions,
                "image_ref": image_ref.model_dump(mode="json"),
                "sampling": "overview-lanczos-1024" if overview else "original-pixels",
            }
        ),
        "application/vnd.tsdaf.local-visual-read+json",
    )
    return LocalVisualRead(payload, image_ref, mapping_ref, sampled_box, dimensions)


def _checked_edges(box: ResolvedPixelBox, dimensions: tuple[int, int]) -> tuple[int, int, int, int]:
    edges = (box.x_min_px, box.y_min_px, box.x_max_px, box.y_max_px)
    if any(type(edge) is not int for edge in edges):
        raise ValueError("sampled pixel edges must be integers")
    ResolvedPixelBox(*edges)
    if edges[2] > dimensions[0] or edges[3] > dimensions[1]:
        raise ValueError("sampled box lies outside the original image")
    return edges


def _check_original_bytes(source: VisualSourceSlice, payload: bytes) -> None:
    if (
        len(payload) != source.source_asset.byte_size
        or hashlib.sha256(payload).hexdigest() != source.source_asset.sha256
    ):
        raise ValueError("original image bytes differ from the source identity")


def _read_exact(store: VerifiedArtifactStore, ref: ArtifactRef) -> bytes:
    store.verify(ref)
    payload = store.read_bytes(ref)
    if len(payload) != ref.byte_size or hashlib.sha256(payload).hexdigest() != ref.sha256:
        raise ValueError("cached view bytes differ from the selected artifact")
    return payload


def _store_exact(store: PrivateArtifactStore, payload: bytes, media_type: str) -> ArtifactRef:
    ref = store.put_bytes(payload, media_type=media_type)
    if (
        ref.sha256 != hashlib.sha256(payload).hexdigest()
        or ref.byte_size != len(payload)
        or ref.media_type != media_type
    ):
        raise ValueError("stored display artifact differs from the materialized bytes")
    store.verify(ref)
    if store.read_bytes(ref) != payload:
        raise ValueError("stored display artifact readback differs from the materialized bytes")
    return ref
