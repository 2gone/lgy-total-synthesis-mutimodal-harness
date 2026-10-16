"""Host-only materialization of immutable public literature evidence."""

from .evidence import (
    HostVerificationContext,
    ObservationContext,
    ObservationOriginator,
    SourceImagePixelBBox,
    TextFragmentSelection,
    TextValueRange,
    VisualAssetSelection,
    materialize_text_observation,
    materialize_visual_observation,
)

__all__ = (
    "HostVerificationContext",
    "ObservationContext",
    "ObservationOriginator",
    "SourceImagePixelBBox",
    "TextFragmentSelection",
    "TextValueRange",
    "VisualAssetSelection",
    "materialize_text_observation",
    "materialize_visual_observation",
)
