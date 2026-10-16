"""What identifies an item of each write collection, said once.

An item sent again under the same identity is the same item: the host replaces the
saved one, or, for a fusion fact, changes only what the new item supplies. An action is
never saved; what it names only pairs a refused write with its correction.

This was written in five places -- the key tuples of each collection table, a
hand-written string in every handle's description, prompt prose, the host's refusal of
an unknown handle and the fusion executor's own tables -- and they had drifted: the
narrative handle said "title or kind" where its table keyed both, the fusion executor
compared an assessment's subject_ids in order where its registry and its state sorted
them. The declaration, which a model reads every turn, said none of it, and a4's
paragraph task invented paragraph_id because every collection beside paragraphs had an
id of its own. Now the tables, the executors, the refusal and the sentence each
declaration states are generated from this one.

Keyed by class name, so this module imports no model and any module may read it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tsdaf.contracts._base import ContractModel

# Where an identity field's value comes from: a name the model chooses, an id the task
# was given, or the item's own content.
Source = Literal["CHOSEN", "GIVEN", "CONTENT"]
# What an item sent again under a saved identity does.
OnRepeat = Literal["REPLACES", "SPARSE", "ACTION"]

# How the sentence glosses each source. An id the task was given needs none: its own
# field says where it comes from.
_GLOSS: dict[Source, str] = {
    "CHOSEN": " (your own name for it)",
    "GIVEN": "",
    "CONTENT": " (the item's own content)",
}


@dataclass(frozen=True)
class Identity:
    fields: tuple[tuple[str, Source], ...]
    on_repeat: OnRepeat = "REPLACES"
    # Fields compared as sets: the same subjects in another order are the same item.
    unordered: frozenset[str] = frozenset()
    handle_only: bool = False

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.fields)

    def key(self, item: object) -> tuple[object, ...]:
        """What two items of this collection share when they are the same item."""
        return tuple(
            tuple(sorted(value)) if name in self.unordered else value
            for name in self.keys
            for value in (getattr(item, name),)
        )

    def sentence(self, *, handle: bool) -> str | None:
        """The identity as the collection's declaration states it; an action has none."""
        if self.on_repeat == "ACTION":
            return None
        if self.handle_only:
            return ("Omit item_id to create a new account, even if its title is already used. "
                    "Select the supplied item_id to replace or rename that account, or its "
                    "action_id to edit it. The host maintains identity and versions.")
        named = " and ".join(
            name + _GLOSS[source] + (", in any order" if name in self.unordered else "")
            for name, source in self.fields
        )
        keys = " and ".join(self.keys)
        said = f"Identity: {named}. "
        if self.on_repeat == "SPARSE":
            said += f"An item sent for a {keys} already saved changes only what it supplies."
        elif handle:
            said += (
                f"An item sent again with the same {keys} replaces the saved one; to change "
                f"the {' or '.join(self.keys)} of a saved item, quote its item_id."
            )
        else:
            said += f"An item sent again with the same {keys} replaces the saved one."
        return said + " No other id is needed."


ITEM_IDENTITIES: dict[str, Identity] = {
    "ExperimentalRecord": Identity((), handle_only=True),
    # Paragraph location, whole or scoped to a few blocks or to the paper, and the
    # supplement. A paragraph is the passage it cites, in the order it cites it: the
    # same selections in another order are another delivery.
    "StepParagraph": Identity((("selection_ids", "CONTENT"),)),
    "InvestigationCoverage": Identity((("document_revision_id", "GIVEN"),)),
    "InvestigationLimitation": Identity((("scope", "GIVEN"),)),
    "SupplementalNarrative": Identity((("title", "CHOSEN"), ("kind", "GIVEN"))),
    # The middle layer.
    "Segment": Identity((("operation_id", "GIVEN"), ("segment_key", "CHOSEN"))),
    "Finding": Identity((("finding_id", "CHOSEN"),)),
    "ConflictRuling": Identity((("finding", "GIVEN"),)),
    "SameMeasurement": Identity((("finding", "GIVEN"),)),
    "NameRuling": Identity((("finding", "GIVEN"),)),
    "SkeletonRepair": Identity((("route_group_id", "GIVEN"), ("repair", "CONTENT"))),
    "TwoTransformations": Identity((("operation_id", "GIVEN"),)),
    # The fusion. A change names a fact as its fact_id says; a group is its changes.
    "FactChange": Identity((("fact_id", "GIVEN"),), on_repeat="SPARSE"),
    "FactChangeGroup": Identity((("changes", "CONTENT"),), on_repeat="ACTION"),
    "RouteSegment": Identity((("segment_id", "CHOSEN"),)),
    "FusionQuestion": Identity((("question_id", "CHOSEN"),)),
    "MainExperimentAssessment": Identity(
        (("subject_ids", "GIVEN"),), unordered=frozenset({"subject_ids"})
    ),
    # A withdrawal is an action, named by the location it withdraws.
    "WithdrawFinding": Identity((("location", "GIVEN"),), on_repeat="ACTION"),
    # The retired detail collections, read back only for sealed history.
    "ContainerPatch": Identity((("target_id", "GIVEN"), ("field_key", "GIVEN"))),
    "OutcomeAddition": Identity((("addition_id", "CHOSEN"),)),
    "CompoundAddition": Identity((("compound_key", "CHOSEN"),)),
    "ReactionBlockAddition": Identity((("block_key", "CHOSEN"),)),
    "OperationAddition": Identity((("operation_key", "CHOSEN"),)),
    "OperationStageAddition": Identity((("operation_id", "GIVEN"), ("stage_key", "CHOSEN"))),
    "ProcedureElision": Identity((("operation_id", "GIVEN"),)),
    "ConditionAddition": Identity((("condition_id", "CHOSEN"),)),
    "CompoundCharacterization": Identity((("characterization_id", "CHOSEN"),)),
}


def identity_of(model: type) -> Identity:
    for base in model.__mro__:
        if base.__name__ in ITEM_IDENTITIES:
            return ITEM_IDENTITIES[base.__name__]
    raise KeyError(model.__name__)


def identity_sentence(model: type[ContractModel]) -> str | None:
    """What a write declaration says of the collection whose items are ``model``."""
    return identity_of(model).sentence(handle="item_id" in model.model_fields)


def keyed(
    *collections: tuple[str, type[ContractModel]],
) -> dict[str, tuple[type[ContractModel], tuple[str, ...]]]:
    """A collection table, (name, item model) in receipt order, keyed from the table."""
    return {name: (model, identity_of(model).keys) for name, model in collections}
