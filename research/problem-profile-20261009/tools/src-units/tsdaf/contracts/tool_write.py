"""Internal write action values, assembled from the collections a task registers.

These are not public literature/document contracts. A new collection can be
registered without adding a content-kind or task-role branch to the write
executor, the same way a new selector joins ``tool_read`` without one.
"""

from collections.abc import Callable, Sequence
from types import UnionType
from typing import Annotated, Any, ClassVar, Literal, Self, Union, cast, get_args, get_origin

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, create_model, model_validator
from pydantic.json_schema import SkipJsonSchema

from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.tool_delivery import REPAIR_GUIDANCE, WriteControl

# One rule for a scalar field's value; the declaration's field is built from the same type.
SCALAR_TEXT = Annotated[str, Field(min_length=1)]
_SCALAR = TypeAdapter(SCALAR_TEXT)


def scalar_text(value: Any) -> str:
    """Validate one written scalar the way the declaration declares it."""
    return _SCALAR.validate_python(value, strict=True)


# What sending an item again does is its collection's to say, with what identifies it
# (the ``identity`` of `write_request`); this said "replaces" for every collection, a
# fusion fact's sparse change included.
WRITE_CONTENT_GUIDANCE = (
    "Collections are optional arrays. Each write must carry at least one item in a "
    "nonempty collection or a non-null scalar value; controls alone save nothing."
)

WRITE_GUIDANCE = (
    "Save what this task has established. Each collection is independent: an item "
    "that fails does not discard the others, and each one is reported separately. "
    "Omission preserves what is already saved. Saving does not finish the task. "
    + WRITE_CONTENT_GUIDANCE
)


class NamesNothing(ValueError):
    """A write that carried no item: an object of this task's collections, each empty,
    or of controls alone. Nothing in it was refused that a later write could correct, so
    it leaves no obligation behind (`tool_execution`); every other caller reads it as the
    ValueError it is."""


class WriteShapeError(ValueError):
    """An envelope failed before individual items could be validated or saved."""

    def __str__(self) -> str:
        return (
            super().__str__()
            + ". Write envelope rejected: no items were saved or individually validated. "
            "Correct the envelope using the declared keys; this error does not require "
            "adding fields or rewriting item content."
        )


Reference = tuple[str, str, str, str]
"""(collection, field, target collection, target field): every id an item lists under
``field`` must be the ``target field`` of an item of this same write under ``target
collection``. A group naming its changes by id is the one case today."""


def write_shape(
    raw: Any,
    *,
    collections: Sequence[str],
    scalars: Sequence[str] = (),
    controls: Sequence[str] = (),
    references: Sequence[Reference] = (),
) -> None:
    """The one rule for the shape of a write; declaration and executor share it.

    A write is an object keyed by this task's collections, each a list of items, or by
    its scalar fields. Controls (repairs, a view) steer a write and save nothing, so a
    write carrying only those, or only empty lists, has not said what it saves. Refusing
    that with the shape teaches more than accepting it as a change of nothing. A
    reference from one item to another of the same write is shape too: what it names
    must be here.
    """
    if not isinstance(raw, dict):
        raise WriteShapeError("write arguments must be an object")
    unknown = sorted(set(raw) - set(collections) - set(scalars) - set(controls))
    if unknown:
        raise WriteShapeError(
            "unknown write collections "
            + ", ".join(unknown)
            + "; this task writes "
            + ", ".join((*collections, *scalars))
        )
    for key in collections:
        if key in raw and not isinstance(raw[key], list):
            raise WriteShapeError(f"{key} takes a list of items")
    if not any(raw.get(key) for key in collections) and all(
        raw.get(key) is None for key in scalars
    ):
        raise NamesNothing(
            'A write names what it saves: {"' + collections[0] + '": [{...}]}; '
            "this task writes " + ", ".join((*collections, *scalars)) + "."
        )
    for collection, field, target_collection, target_field in references:
        carried = {
            item[target_field]
            for item in raw.get(target_collection) or ()
            if isinstance(item, dict) and isinstance(item.get(target_field), str)
        }
        for index, item in enumerate(raw.get(collection) or ()):
            named = item.get(field) if isinstance(item, dict) else None
            if not isinstance(named, list):
                continue
            missing = [name for name in named if isinstance(name, str) and name not in carried]
            if missing:
                raise ValueError(
                    f"{collection}:{index} names {field} {', '.join(missing)} which this "
                    f"write does not carry under {target_collection}; a group names the "
                    f"{target_field} of items sent under {target_collection} in this same write"
                )


def _unwrap(annotation: Any) -> Any:
    """The type behind Annotated and Optional wrappers, containers and unions kept."""
    while True:
        origin = get_origin(annotation)
        if origin is Annotated:
            annotation = get_args(annotation)[0]
        elif origin in (UnionType, Union) and type(None) in get_args(annotation):
            members = [m for m in get_args(annotation) if m is not type(None)]
            annotation = members[0] if len(members) == 1 else Union[tuple(members)]  # noqa: UP007
        else:
            return annotation


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _tag(member: Any) -> str | None:
    """A union member's discriminator value, the way pydantic names it in an error's loc."""
    if _is_model(member) and "kind" in member.model_fields:
        options = get_args(_unwrap(member.model_fields["kind"].annotation))
        if len(options) == 1 and isinstance(options[0], str):
            return options[0]
    return None


def item_holder(
    model: type[BaseModel], loc: Sequence[str | int]
) -> tuple[str | None, str | None, tuple[str, ...]] | None:
    """Where an error's path lands, described the way a model reads it.

    Walks ``loc`` from the item's model: a field name steps into that field's type, an
    index into a container's element, a union tag into that member. Returns the field
    path walked, the union tag reached (if any) and the fields the reached model takes;
    None when the path does not describe a model the caller can be told about.
    """
    current: Any = model
    path: list[str] = []
    tag: str | None = None
    for segment in loc:
        current = _unwrap(current)
        if isinstance(segment, int):
            if get_origin(current) in (tuple, list) and get_args(current):
                current = get_args(current)[0]
                continue
            return None
        if _is_model(current) and segment in current.model_fields:
            current, tag = current.model_fields[segment].annotation, None
            path.append(segment)
            continue
        if get_origin(current) in (UnionType, Union):
            member = next(
                (
                    m
                    for m in get_args(current)
                    if _tag(m) == segment or getattr(m, "__name__", None) == segment
                ),
                None,
            )
            if member is not None:
                current, tag = member, segment
                continue
        return None
    current = _unwrap(current)
    if not _is_model(current):
        return None
    return (".".join(path) or None, tag, declared_fields(current))


# `SkipJsonSchema` is a generic alias to a type checker; the marker pydantic stores in a
# field's metadata is an instance of its runtime class.
_SKIPPED_FROM_SCHEMA = cast(type, SkipJsonSchema)


def declared_fields(model: type[BaseModel]) -> tuple[str, ...]:
    """The fields a model's declaration lists, which are the ones to name to a model.

    A field kept out of the schema (`SkipJsonSchema`) is accepted as null so an old
    record still reads, and it is not in what the model was told it may write: naming
    it in "takes ..." invited the model to write a field the declaration never had
    (`FusionQuestion.reading_plan`, retired with the fusion's handoffs, E172).
    """
    return tuple(
        name
        for name, field in model.model_fields.items()
        if not any(isinstance(item, _SKIPPED_FROM_SCHEMA) for item in field.metadata)
    )


# The most a Host sentence handed to the model as an item's diagnostic may run to. A
# diagnostic is a reason, not a document: a refusal that quoted the fact root it was
# refusing ran to 191 KB, three of them went into one tool result, and the request that
# carried them was refused by the provider (f05, ledger E141). The sentence is cut here,
# at the one place every tool renders a rejected item, so no producer can do it again.
DIAGNOSTIC_CHARS = 2_000


def item_diagnostic(
    error: BaseException, *, model: type[BaseModel] | None = None, collection: str | None = None
) -> Any:
    """The one rendering of a rejected item's error, for every tool that reports items.

    Unknown fields on the same object are one cause, with a count and examples, not
    one repeated declaration per field. Other validation errors remain actionable
    loc/msg/type entries. The sealed model response retains every submitted field.
    """
    if not isinstance(error, ValidationError):
        if not isinstance(error, ValueError):
            return type(error).__name__
        said = str(error)
        if len(said) > DIAGNOSTIC_CHARS:
            said = said[: DIAGNOSTIC_CHARS - 1] + "…"
        return said
    entries = error.errors(include_url=False, include_context=False, include_input=False)
    grouped: dict[tuple[str | int, ...], list[Any]] = {}
    for entry in entries:
        if entry.get("type") == "extra_forbidden":
            grouped.setdefault(tuple(entry["loc"][:-1]), []).append(entry)
    rendered: list[Any] = []
    for entry in entries:
        loc = list(entry.get("loc", ()))
        if entry.get("type") != "extra_forbidden" or not loc:
            if (
                entry.get("type") in {"model_type", "model_attributes_type", "dict_type"}
                and model is not None
            ):
                found = item_holder(model, loc)
                if found is not None:
                    path, _, fields = found
                    entry["msg"] = f"{path or 'item'} requires an object with fields: " + ", ".join(
                        fields
                    )
            rendered.append(entry)
            continue
        group = grouped[tuple(loc[:-1])]
        if entry is not group[0]:
            continue
        rendered.append(entry)
        if len(group) > 1:
            entry["count"] = len(group)
            entry["examples"] = [e["loc"][-1] for e in group[:3]]
            entry["loc"] = tuple(loc[:-1])
            entry["msg"] = (
                f"{len(group)} unknown fields on this object; remove undeclared keys "
                "rather than adding or enumerating fields"
            )
        found = item_holder(model, loc[:-1]) if model is not None else None
        if found is None or collection is None:
            continue
        path, tag, fields = found
        where = (
            f"on a {collection} item" if path is None else f"under {path} of a {collection} item"
        )
        holder = (
            f"a {collection} item"
            if path is None
            else f"a {tag} {path.rsplit('.', 1)[-1]}"
            if tag is not None
            else f"a {path.rsplit('.', 1)[-1]} entry"
        )
        unknown = (
            f"unknown field {loc[-1]}"
            if len(group) == 1
            else f"{len(group)} unknown fields (examples: "
            + ", ".join(map(str, entry["examples"]))
            + "); remove undeclared keys"
        )
        entry["msg"] = f"{unknown} {where}; {holder} takes " + ", ".join(fields)
    return rendered


class WriteShape(ContractModel):
    """Carries the shape rule so a write is validated the same way wherever it arrives."""

    write_collections: ClassVar[tuple[str, ...]] = ()
    write_scalars: ClassVar[tuple[str, ...]] = ()
    write_references: ClassVar[tuple[Reference, ...]] = ()

    @model_validator(mode="after")
    def _names_what_it_saves(self) -> Self:
        cls = type(self)
        controls = tuple(
            name
            for name in cls.model_fields
            if name not in cls.write_collections and name not in cls.write_scalars
        )
        write_shape(
            self.model_dump(mode="json", exclude_unset=True),
            collections=cls.write_collections,
            scalars=cls.write_scalars,
            controls=controls,
            references=cls.write_references,
        )
        return self


def enumerated_values(model: type[ContractModel]) -> str:
    """The values an item's enumerated fields may take, in the sentence a model reads.

    A live model invented a kind that did not exist: the real values sat in the schema's
    $defs, which is where a model does not look. The collection's description is.
    """
    parts: list[str] = []
    for name, field in model.model_fields.items():
        annotation = field.annotation
        options: tuple[Any, ...] = ()
        if get_origin(annotation) is Literal:
            options = get_args(annotation)
        elif get_origin(annotation) in (UnionType, Union):
            members = get_args(annotation)
            literals = [member for member in members if get_origin(member) is Literal]
            if literals and len(members) == 2 and type(None) in members:
                options = get_args(literals[0])
        if options and all(isinstance(option, str) for option in options):
            parts.append(f"{name}: " + ", ".join(options) + ".")
    return " ".join(parts)


def write_request(
    *,
    name: str,
    collections: Sequence[tuple[str, type[ContractModel]]],
    scalars: Sequence[tuple[str, str]] = (),
    controls: Sequence[type[ContractModel]] = (WriteControl,),
    doc: str = "",
    references: Sequence[Reference] = (),
    identity: Callable[[type[ContractModel]], str | None] = lambda model: None,
    alternatives: dict[str, type[ContractModel]] | None = None,
) -> type[ContractModel]:
    """One write tool covering every collection this task may change.

    ``collections`` is (payload key, item model) in the order receipts appear.
    Every collection becomes its own array, so an item is validated, accepted and
    reported on its own. ``scalars`` are (payload key, description) single values
    that are set or left alone. ``controls`` are the bases whose fields steer the
    write without saving anything; ``doc`` is appended to the shared guidance;
    ``references`` are the ids one collection's items may list of another's, checked by
    the same shape rule at both arrival points. ``identity`` says, per item model, what
    identifies an item and what sending it again does; a lane passes its one table, so
    this contract imports none.
    """
    if not collections:
        raise ValueError("a write tool requires at least one registered collection")
    fields: dict[str, Any] = {}
    for key, model in collections:
        # A registered collection's model is only known at runtime; pydantic still
        # validates every item against it.
        alternate = (alternatives or {}).get(key)
        item_type = model | alternate if alternate is not None else model
        items = tuple[item_type, ...]  # type: ignore[valid-type]
        description = " ".join(
            part
            for part in ((model.__doc__ or "").strip(), identity(model), enumerated_values(model))
            if part
        )
        fields[key] = (items, Field(default=(), description=description or None))
    for key, description in scalars:
        fields[key] = (SCALAR_TEXT | None, Field(default=None, description=description))
    shape = type(
        name + "Shape",
        (WriteShape,),
        {
            "write_collections": tuple(key for key, _ in collections),
            "write_scalars": tuple(key for key, _ in scalars),
            "write_references": tuple(references),
        },
    )
    request: type[ContractModel] = create_model(name, __base__=(shape, *controls), **fields)
    request.__doc__ = WRITE_GUIDANCE + (" " + doc if doc else "") + REPAIR_GUIDANCE
    return cast(type[ContractModel], request)
