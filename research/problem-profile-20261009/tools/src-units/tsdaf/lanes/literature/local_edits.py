"""Mechanical local corrections derived from the actual writer's field capabilities.

No scientific acceptance happens here. The assembled full proposal must still pass
its original writer, source checks, dependency group and publication validation.
"""

from __future__ import annotations

from copy import deepcopy
from functools import cache, reduce
from operator import or_
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import Field, TypeAdapter, create_model, model_validator
from pydantic_core import PydanticUndefined

from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.tool_write import SCALAR_TEXT

EDIT_MEANING = (
    "Correct only selected text/source fields of one saved or rejected proposal, named "
    "by its supplied action_id. Omitted fields keep the exact base. Objects update their "
    "named fields; scalars and arrays replace that field. A different value branch must "
    "be given completely. Only fields in this edit declaration can change; use a full "
    "item for other changes or removal of unknown fields. "
    "A stale or unprovable base is refused, never silently rebased. The full candidate "
    "still undergoes every original check. Use either the full item or edit, not both."
)


class PartialFields(ContractModel):
    model_config = {"json_schema_extra": {"minProperties": 1}}

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set:
            raise ValueError("changes must supply at least one editable field")
        return self


def capabilities(model: type[ContractModel]) -> tuple[frozenset[str], frozenset[str]]:
    allowed = model.local_edit_fields
    fixed = model.local_edit_fixed_fields
    if allowed & fixed or allowed | fixed != set(model.model_fields):
        raise ValueError(f"{model.__name__}: every field must declare its local-edit capability")
    return allowed, fixed


def supports_edit(model: type[ContractModel]) -> bool:
    """Declarations, live writes and history share the model's edit capability."""
    if not getattr(model, "local_edit_fields", None):
        return False
    capabilities(model)
    return True


def _partial_type(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is Annotated:
        base, *metadata = get_args(annotation)
        return Annotated[_partial_type(base), *metadata]
    if origin in (Union, UnionType):
        return reduce(or_, (_partial_type(a) for a in get_args(annotation)))
    if isinstance(annotation, type) and issubclass(annotation, ContractModel):
        return partial_model(annotation)
    # Arrays replace completely: their element types and constraints stay unchanged.
    return annotation


def field_type(model: type[ContractModel], name: str, aspect: str | None = None) -> Any:
    if name == "words" and hasattr(model, "word_model"):
        from tsdaf.lanes.literature.middle_layer import READING_KINDS

        if aspect is not None:
            if aspect not in READING_KINDS:
                raise ValueError("unknown base aspect; submit the full item")
            return model.word_model(READING_KINDS[aspect])
        # The actual aspect-specific input types exclude the internal omitted-word
        # sentinel. No parallel editable words table or newly nullable fields.
        return reduce(or_, dict.fromkeys(model.word_model(k) for k in READING_KINDS.values()))
    return model.model_fields[name].annotation


@cache
def partial_model(model: type[ContractModel]) -> type[ContractModel]:
    allowed, _ = capabilities(model)
    fields = {}
    for name, field in model.model_fields.items():
        if name not in allowed:
            continue
        definition = deepcopy(field)
        definition.default_factory = lambda: None
        definition.validate_default = False
        definition.default = PydanticUndefined
        fields[name] = (_partial_type(field_type(model, name)), definition)
    result = create_model(model.__name__ + "Changes", __base__=PartialFields, **fields)
    result.__doc__ = "Selected fields of the verified base proposal."
    return result


@cache
def edit_model(model: type[ContractModel]) -> type[ContractModel]:
    payload = create_model(
        model.__name__ + "Edit",
        __base__=ContractModel,
        action_id=(
            SCALAR_TEXT,
            Field(description="Supplied action name of the exact base proposal."),
        ),
        changes=(partial_model(model), Field(description=EDIT_MEANING)),
    )
    wrapper = create_model(
        model.__name__ + "EditItem",
        __base__=ContractModel,
        edit=(
            payload,
            Field(description="Select one recorded proposal and supply its corrections."),
        ),
    )
    wrapper.__doc__ = EDIT_MEANING
    return wrapper


def _branches(annotation: Any) -> tuple[Any, ...]:
    if get_origin(annotation) is Annotated:
        return _branches(get_args(annotation)[0])
    if get_origin(annotation) in (Union, UnionType):
        return tuple(b for a in get_args(annotation) for b in _branches(a))
    return (annotation,)


def _object_model(annotation: Any, value: Any) -> type[ContractModel] | None:
    if not isinstance(value, dict):
        return None
    objects = [
        b for b in _branches(annotation) if isinstance(b, type) and issubclass(b, ContractModel)
    ]
    if len(objects) == 1:
        return objects[0]
    matches = []
    for model in objects:
        try:
            model.model_validate_json(canonical_json_bytes(value))
        except ValueError:
            continue
        matches.append(model)
    if len(matches) == 1:
        return matches[0]
    if objects:
        raise ValueError("ambiguous object branch; submit the complete item")
    return None


def merge_edit(
    model: type[ContractModel], base: dict[str, Any], changes: dict[str, Any]
) -> dict[str, Any]:
    """Sparse objects, whole arrays, explicit branch replacement, no inferred links."""
    allowed, fixed = capabilities(model)
    if set(base) - set(model.model_fields):
        raise ValueError("base has unknown fields; use the full item to remove them")
    if not changes or set(changes) - allowed:
        raise ValueError("changes must contain only this base's editable fields")
    result = deepcopy(base)
    for key, change in changes.items():
        field = model.model_fields[key]
        annotation = field_type(model, key, base.get("aspect"))
        before = base.get(key)
        old_model = _object_model(annotation, before)
        new_model = _object_model(annotation, change)
        if old_model is not None and old_model is new_model:
            result[key] = merge_edit(old_model, before, change)
        elif old_model is not None or new_model is not None:
            # Changing a value branch replaces it entirely, never mixes its keys.
            TypeAdapter(annotation).validate_json(canonical_json_bytes(change), strict=True)
            protected_before = (
                {name: before.get(name) for name in capabilities(old_model)[1]} if old_model else {}
            )
            protected_after = (
                {name: change.get(name) for name in capabilities(new_model)[1]} if new_model else {}
            )
            if protected_before != protected_after:
                raise ValueError("branch change alters protected relationships; submit a full item")
            result[key] = deepcopy(change)
        else:
            # This also validates array members in full and never invents nullable fields.
            constrained = Annotated[annotation, *field.metadata] if field.metadata else annotation
            TypeAdapter(constrained).validate_json(
                canonical_json_bytes(change),
                strict=True,
            )
            result[key] = deepcopy(change)
    if any(result.get(name) != base.get(name) for name in fixed):
        raise ValueError("edit changes a protected field; submit a full item")
    return result


def semantic_state(value: Any) -> Any:
    """Host-only presence/private state that JSON defaults cannot reconstruct."""
    if isinstance(value, ContractModel):
        return {
            "model": type(value).__name__,
            "fields_set": sorted(value.model_fields_set),
            "private": deepcopy(value.__pydantic_private__ or {}),
            "children": {
                name: semantic_state(getattr(value, name)) for name in type(value).model_fields
            },
        }
    if isinstance(value, (tuple, list)):
        return [semantic_state(item) for item in value]
    return None


def edited_semantics(state: Any, changes: dict[str, Any]) -> Any:
    """Keep omitted-field/private evidence only for branches retained by the edit."""
    if not isinstance(state, dict):
        return None
    result = deepcopy(state)
    result["fields_set"] = sorted(set(state["fields_set"]) | changes.keys())
    for name, change in changes.items():
        result["children"][name] = (
            edited_semantics(state["children"].get(name), change)
            if isinstance(change, dict)
            else None
        )
    return result


def restore_semantic_state(value: Any, state: Any, *, exact: bool = True) -> None:
    """Restore host-only semantics, never values; edits may replace child branches."""
    if isinstance(value, ContractModel) and isinstance(state, dict):
        if state.get("model") != type(value).__name__:
            if exact:
                raise ValueError("recorded semantic state belongs to another value branch")
            return
        fields = set(type(value).model_fields)
        if set(state.get("children", {})) != fields or set(state.get("fields_set", ())) - fields:
            raise ValueError("recorded semantic fields differ from the current domain model")
        private = state.get("private", {})
        if set(private) - set(value.__private_attributes__):
            raise ValueError("private edit state differs from the current domain model")
        # Models without declared private fields use None, not an empty dictionary.
        # Their serialized semantic state is empty in both cases, but Pydantic's
        # equality (used by verified history replay) distinguishes the two.
        object.__setattr__(value, "__pydantic_private__",
                           deepcopy(private) if value.__private_attributes__ else None)
        object.__setattr__(value, "__pydantic_fields_set__", set(state["fields_set"]))
        for name, child in state.get("children", {}).items():
            restore_semantic_state(getattr(value, name), child, exact=exact)
    elif isinstance(value, (tuple, list)) and isinstance(state, list):
        if len(value) != len(state):
            raise ValueError("recorded sequence semantics have the wrong length")
        for member, member_state in zip(value, state, strict=True):
            restore_semantic_state(member, member_state, exact=exact)
    elif exact and state is not None:
        raise ValueError("recorded semantic state differs from the current value")
