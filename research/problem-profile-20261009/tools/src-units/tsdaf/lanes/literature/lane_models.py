"""Source-grounded observations shared by whole-route and targeted readers.

Readers describe meaning in connected prose, without constructing scientific tables.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from tsdaf.contracts._base import ContractModel, _unique

Nonempty = Annotated[str, StringConstraints(min_length=1)]


class TextFragmentSelection(ContractModel):
    selection_id: Nonempty = Field(description="Local name of this selection, cited by ID.")
    block_id: Annotated[str, StringConstraints(pattern=r"^MAIN-P[0-9]{4}-B[0-9]{4}$")] = Field(
        description="The MAIN text block, MAIN-P####-B#### as the catalog names it."
    )
    start_sentence: int = Field(ge=0, description="Inclusive start of the sentence range.")
    end_sentence: int = Field(ge=1, description="Exclusive end of numbered sentence range.")

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.start_sentence >= self.end_sentence:
            raise ValueError("text selection must be a nonempty half-open sentence range")
        return self


# A region of an image, and the one rule a region obeys, for every reader that marks one.
# The models below are rendered as the schemas a reader answers with, and pydantic renders
# a class docstring as the schema's description, so what explains them to a maintainer is
# written in comments.
class _ImageRegion(ContractModel):
    coordinate_space: Literal["NORMALIZED_1000"] = Field(
        default="NORMALIZED_1000", description="Each axis runs 0..1000 across the image."
    )
    x0: int = Field(ge=0, le=1000, description="Left edge, 0..1000.")
    y0: int = Field(ge=0, le=1000, description="Top edge, 0..1000.")
    x1: int = Field(ge=0, le=1000, description="Right edge, 0..1000; greater than x0.")
    y1: int = Field(ge=0, le=1000, description="Bottom edge, 0..1000; greater than y0.")

    @model_validator(mode="after")
    def _area(self) -> Self:
        if self.x0 >= self.x1 or self.y0 >= self.y1:
            raise ValueError("visual selection must have positive normalized-1000 area")
        return self


class _Named(ContractModel):
    selection_id: Nonempty = Field(description="Local name of this selection, cited by ID.")


class _NamedMainImage(_Named):
    visual_id: Annotated[str, StringConstraints(pattern=r"^MAIN-P[0-9]{4}-A[0-9]{4}$")] = Field(
        description="The MAIN image, MAIN-P####-A#### as the catalog names it."
    )


# A region of one of the MAIN images a route reader is shown, which it names. The bases
# are listed region-first so the fields keep the order the frozen V1 schema was generated
# in: the name, the image, then the region.
class VisualRegionSelection(_ImageRegion, _NamedMainImage):
    pass


# A region of the one image a figure reader (V3) is shown. It names no image: the Host
# dispatched that image and knows which it is. Asking the reader to repeat it put V1's
# rule -- only a MAIN image has a name -- on a reader that is also sent supplement
# figures, and 93 of 95 readings of one were refused (f09).
class FigureRegionSelection(_ImageRegion, _Named):
    pass


class ReadingObservation(ContractModel):
    scope: Nonempty = Field(description="Readable local subject or connected route range.")
    text: Nonempty = Field(
        description="Connected finding: what happened to what, order, and relevant limits."
    )
    selection_ids: tuple[Nonempty, ...] = Field(min_length=1)


class _Reading(ContractModel):
    overview: Nonempty = Field(description="Whole route framework, or answer to assigned question.")
    observations: tuple[ReadingObservation, ...]
    limitations: str = Field(
        default="", description="Unreadable, absent or unaddressable evidence and its scope."
    )

    def validate_selections(
        self,
        selections: tuple[TextFragmentSelection, ...]
        | tuple[VisualRegionSelection, ...]
        | tuple[FigureRegionSelection, ...],
    ) -> None:
        ids = {s.selection_id for s in selections}
        _unique(tuple(s.selection_id for s in selections), label="source selections")
        for observation in self.observations:
            _unique(observation.selection_ids, label="observation selections")
            if not set(observation.selection_ids) <= ids:
                raise ValueError("observation cites an absent source selection")


class TextReading(_Reading):
    source_selections: tuple[TextFragmentSelection, ...]

    @model_validator(mode="after")
    def _closure(self) -> Self:
        self.validate_selections(self.source_selections)
        return self


class VisualReading(_Reading):
    source_selections: tuple[VisualRegionSelection, ...]

    @model_validator(mode="after")
    def _closure(self) -> Self:
        self.validate_selections(self.source_selections)
        return self


# What a figure reader says about the one image it was shown (V3).
class VisualDetailReading(_Reading):
    source_selections: tuple[FigureRegionSelection, ...]

    @model_validator(mode="after")
    def _closure(self) -> Self:
        self.validate_selections(self.source_selections)
        return self
