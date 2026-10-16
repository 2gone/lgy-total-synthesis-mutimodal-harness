"""Passive scientific measurement choice shared by both writing producers."""

from typing import Literal

from pydantic import ConfigDict, Field, RootModel, field_validator

from tsdaf.contracts._base import ContractModel


class SameMeasurementIntent(ContractModel):
    same_as: str = Field(min_length=1, description="The displayed name of this same measurement.")


class NewMeasurementIntent(ContractModel):
    new: Literal[True]

    @field_validator("new", mode="before")
    @classmethod
    def exact_boolean(cls, value: object) -> object:
        if value is not True:
            raise ValueError("new must be the boolean true")
        return value


class UnknownMeasurementIntent(ContractModel):
    unknown: Literal[True]

    @field_validator("unknown", mode="before")
    @classmethod
    def exact_boolean(cls, value: object) -> object:
        if value is not True:
            raise ValueError("unknown must be the boolean true")
        return value


class MeasurementIntent(
    RootModel[SameMeasurementIntent | NewMeasurementIntent | UnknownMeasurementIntent]
):
    """Choose same, distinct, or still unknown independently of experiment ownership."""

    model_config = ConfigDict(strict=True, frozen=True)

    @property
    def same_as(self) -> str | None:
        return self.root.same_as if isinstance(self.root, SameMeasurementIntent) else None

    @property
    def identity_known(self) -> bool:
        return not isinstance(self.root, UnknownMeasurementIntent)
