"""Internal write repair and delivery controls, separate from scientific results."""

import json
from typing import Any, Literal, cast

from pydantic import Field, ValidationError, create_model

from tsdaf.contracts._base import ContractModel

# The one description of what `WriteRepair.item` holds, said once so `WriteRepair`
# itself and the `WriteControl.repairs` field that wraps it cannot say it two ways. E92
# renamed this field on `WriteRepair` but left `WriteControl.repairs`'s own description
# calling it "item key" -- a snake_case field named correctly beside a natural-language
# "item key" reads as two different names, and a live model completed the analogy by
# sending `item_key` (7 rejections, `lit-hyperfirin-RG001f07-detail-B000` turn 210).
_REPAIR_ITEM_DESCRIPTION = (
    "the corrected item's key in this write, spelled as receipts spell item: "
    "collection:index for a listed item, or the envelope's own name"
)


class WriteRepair(ContractModel):
    """Two keys copied from receipts: the rejected row's action_id, the corrected item's item."""

    action_id: str = Field(
        min_length=1,
        description=(
            "Copy the action_id of the rejected row from the receipt that listed it. The "
            "host assigns action_ids to receipt rows; items carry none."
        ),
    )
    item: str = Field(min_length=1, description=_REPAIR_ITEM_DESCRIPTION.capitalize() + ".")


class WriteControl(ContractModel):
    repairs: tuple[WriteRepair, ...] = Field(
        default=(),
        description=(
            "Optional. An accepted new version of the same item supersedes its rejected "
            "proposal; this does not prove the old scientific claim was saved. To declare "
            "a correction/replacement explicitly, give the rejected row's action_id and "
            + _REPAIR_ITEM_DESCRIPTION
            + "."
        ),
    )


PARTIAL_DELIVERY_GUIDANCE = (
    "Choose disposition PARTIAL to stop with unresolved writes; the host records them "
    "in the delivery without marking them repaired."
)


class WriteDeliveryControl(ContractModel):
    """Delivery apart from science: stop complete or leave unresolved writes recorded."""

    disposition: Literal["COMPLETE", "PARTIAL"] = Field(
        default="COMPLETE",
        description=(
            "Write delivery status, not scientific completeness: COMPLETE when no rejected "
            "write is left unresolved. " + PARTIAL_DELIVERY_GUIDANCE
        ),
    )


REPAIR_GUIDANCE = (
    " A rejected item blocks COMPLETE delivery: "
    "resend it under its collection; an accepted version of the same item supersedes "
    "the rejected proposal, not a proof that its old scientific claim was corrected. "
    "Use a different item identity for an additional fact. " + PARTIAL_DELIVERY_GUIDANCE
)


def repair_shape(entry: Any, *, example_key: str) -> WriteRepair:
    """One repair entry the way a receipt asks for it, or the sentence that says how.

    A live model tried replacement objects and inline items three turns running; the
    sentence names the shape and says that resending the corrected item is enough.
    """
    if isinstance(entry, dict):
        try:
            return WriteRepair.model_validate(entry)
        except ValidationError:
            pass
    failure = entry.get("action_id", entry.get("failure_id")) if isinstance(entry, dict) else None
    attempted_item = entry.get("item", entry.get("item_key")) if isinstance(entry, dict) else None
    shown = json.dumps(
        {
            "action_id": failure if isinstance(failure, str) and failure else "write:...",
            "item": attempted_item
            if isinstance(attempted_item, str) and attempted_item
            else example_key,
        }
    )
    collection = example_key.split(":", 1)[0]
    raise ValueError(
        "A repair names the corrected item by its key in this same write, e.g. "
        + shown
        + ", with the corrected item itself under "
        + collection
        + ". Simpler: resend the corrected item and leave repairs out; an accepted item "
        "that covers a failed one resolves it."
    )


def controlled_write(model: type[ContractModel]) -> type[ContractModel]:
    command = create_model(model.__name__ + "Command", __base__=(model, WriteControl))
    command.__doc__ = (model.__doc__ or "Save findings.") + REPAIR_GUIDANCE
    return cast(type[ContractModel], command)
