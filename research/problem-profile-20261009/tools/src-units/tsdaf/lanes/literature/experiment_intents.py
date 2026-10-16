"""Scientific experiment choices; identity and evidence are assigned by the host.

Both Fusion and Reading use the same measurement choice. These models describe
the candidate unified profile; registering a model does not enable that profile.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from tsdaf.contracts._base import ContractModel, _unique
from tsdaf.lanes.literature.enrichment_values import BlockOperation, _handle_field
from tsdaf.lanes.literature.measurement_intent import MeasurementIntent as MeasurementIntent
from tsdaf.lanes.literature.source_parts import SourceSelection


class ExperimentOperation(BlockOperation):
    """A model-organized operation, with source wording supplied separately."""

    operation_key: str | None = Field(
        default=None,
        min_length=1,
        description="Optional meaningful name for referring to this operation in the same write.",
    )
    description: str = Field(
        min_length=1,
        description="Your complete organized description; author wording remains in source selections.",
    )


class PreparationUseIntent(ContractModel):
    operation: str = Field(
        min_length=1, description="The displayed operation using this preparation."
    )
    material: str | None = Field(
        default=None,
        min_length=1,
        description="Optional displayed material-use name; omission does not select a material.",
    )
    selection_ids: tuple[str, ...] = Field(
        default=(), description="Optional distinct source support."
    )
    explanation: str | None = Field(default=None, min_length=1)


class NewExperiment(ContractModel):
    """An independent experimental account; it does not add a step to the route."""

    item_id: str | None = _handle_field()
    title: str = Field(
        min_length=1, description="A meaningful title for this experimental account."
    )
    selection_ids: tuple[str, ...] = Field(min_length=1)
    explanation: str = Field(min_length=1)
    source_parts: tuple[SourceSelection, ...] = ()
    input_compound_ids: tuple[str, ...] = ()
    output_compound_ids: tuple[str, ...] = ()
    operations: tuple[ExperimentOperation, ...] = ()
    preparation_uses: tuple[PreparationUseIntent, ...] = ()

    @model_validator(mode="after")
    def distinct_names(self) -> NewExperiment:
        _unique(
            tuple(op.operation_key for op in self.operations if op.operation_key),
            label="operation names within this experiment",
        )
        for field in ("selection_ids", "input_compound_ids", "output_compound_ids"):
            _unique(getattr(self, field), label=field)
        return self
