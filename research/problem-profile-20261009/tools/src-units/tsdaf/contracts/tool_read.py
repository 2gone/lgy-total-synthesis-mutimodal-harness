"""Internal read action values. Content capabilities are explicitly assembled.

These are not public literature/document contracts. A new selector can be registered
without adding a content-kind or task-role branch to the read executor.
"""

import json
from typing import Annotated, Any, ClassVar, Literal, Self, get_args, get_origin

from pydantic import Field, ValidationError, create_model, model_validator

from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.tool_declaration import provider_declaration_schema

READ_DELIVERY_VERSION = "batch-record-capacity.v2"

MAX_READ_ITEMS = 8


class Selection(ContractModel):
    kind: str


class WholeSelection(Selection):
    """Request the complete bounded resource; exceeding the budget is an error."""

    kind: Literal["whole"]


class TextSelection(Selection):
    """Take sentences by position when the whole text is more than the question needs.

    Sentences are numbered by the handler's own segmentation, starting at 0, and end is
    exclusive. A read that returned a directory has already named the ranges it offers.
    """

    kind: Literal["text_range"]
    start_sentence: int = Field(default=0, ge=0)
    end_sentence: int | None = Field(default=None, ge=1)
    adjacent_blocks: int = Field(default=0, ge=0, le=2)

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        if self.end_sentence is not None and self.end_sentence <= self.start_sentence:
            raise ValueError("sentence range must be nonempty; end is exclusive")
        return self


class TextPartSelection(Selection):
    """Copy a host-offered part of oversized saved text, without computing offsets.

    Parts are transport slices of the pinned record, not source sentences or new
    evidence. Only locations that offer these entries support this selector.
    """

    kind: Literal["text_part"]
    part: int = Field(ge=0, description="Copy the part number from the offered read entry.")


Region = Annotated[
    tuple[Annotated[int, Field(ge=0, le=1000)], ...], Field(min_length=4, max_length=4)
]


class VisualReadBounds(ContractModel):
    region: Region | None = Field(
        default=None,
        description="Original-image bounds normalized to 1000. Omit for a bounded overview.",
    )

    @model_validator(mode="after")
    def positive_region(self) -> Self:
        if self.region is not None:
            x0, y0, x1, y1 = self.region
            if x0 >= x1 or y0 >= y1:
                raise ValueError("visual region must have positive area")
        return self


class ImageRegion(ContractModel):
    left: int = Field(ge=0, le=1000, strict=True)
    top: int = Field(ge=0, le=1000, strict=True)
    right: int = Field(ge=0, le=1000, strict=True)
    bottom: int = Field(ge=0, le=1000, strict=True)

    @model_validator(mode="after")
    def positive_area(self) -> Self:
        if self.left >= self.right or self.top >= self.bottom:
            raise ValueError("image region requires left < right and top < bottom")
        return self

    def xyxy(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.right, self.bottom


class ImageSelection(Selection):
    """Select original-image coordinates, normalized to 1000; return exact pixels."""

    kind: Literal["image_region"]
    frame: Literal["source"] = Field(
        default="source",
        description="Use this location’s authorized original image, including for saved crops.",
    )
    region: ImageRegion = Field(
        description="Named original-image edges normalized to 1000; origin top-left. "
        "left/right increase rightwards; top/bottom increase downwards."
    )


class RecordSelection(Selection):
    """Read offered entries/fields or a JSON Pointer within the pinned resource."""

    kind: Literal["record_items"]
    item_ids: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    path: str = ""

    @model_validator(mode="after")
    def valid_path(self) -> Self:
        if self.path and not self.path.startswith("/"):
            raise ValueError("path must be an empty or slash-prefixed JSON Pointer")
        if self.item_ids and self.path:
            raise ValueError("choose offered item IDs or a JSON Pointer")
        return self


class DirectorySelection(Selection):
    """Read a bounded page of child locations, not the children's full contents."""

    kind: Literal["directory"]
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


SEARCH_MATCHING_DESCRIPTION = (
    "TERMS returns FULL blocks covering all query words when any exist; it does not fill "
    "remaining slots with weaker matches. Otherwise it returns only the best available "
    "PARTIAL coverage, with matched_terms naming the words actually found; a lone "
    "single-character fragment cannot support a partial multiword query. A standalone "
    "word, element or number remains searchable. Explicit superscripts on lexical words "
    "also permit finding their base word, while preserving the original full spelling; "
    "ordinary trailing digits are not removed. Double-quoted phrases must match a "
    "continuous string. LITERAL matches the normalized continuous query including symbols. "
    "Neither mode expands stems, prefixes, wildcards or scientific synonyms. "
)


class QuerySelection(Selection):
    __doc__ = (
        """Find authorized text by what it says, when its location is not known yet.

    In source investigations, TERMS containing only a known authorized source name,
    location or canonical page name returns its read entries as named_sources, not
    text matches or read evidence. A canonical package address not in the authorized
    catalog returns no navigation match; it is not split into content words.
    LITERAL always searches the words in source text.
    """
        + SEARCH_MATCHING_DESCRIPTION
        + """
    There are no operators, so OR and AND are ordinary words. A hit names a location and the
    sentences that matched in it, numbered as text_range counts them: sentence is the
    start; end_sentence, when present, is exclusive and covers a cross-sentence match.
    Read the location with that range, or a range around a single sentence for
    its context, or without select for the whole block. Long snippets marked search_preview
    use the index's rendered text around the match; read the source before quoting it.
    A hit whose matching range is covered by a known valid read says so
    (read: turns_ago, and whether it is still shown); no hint means coverage is unproven,
    and the location remains readable. A search that found exactly what
    an earlier one found says so (same_hits_as) and is not progress. Finding nothing
    means nothing matched *this* query within the authorized scope; it is never evidence
    that a concept is absent from the paper.
    """
    )

    kind: Literal["query"]
    terms: str = Field(min_length=1)
    mode: Literal["TERMS", "LITERAL"] = "TERMS"
    limit: int = Field(default=8, ge=1, le=20)


DEFAULT_SELECTORS = (
    WholeSelection,
    TextSelection,
    TextPartSelection,
    ImageSelection,
    RecordSelection,
    DirectorySelection,
    QuerySelection,
)


def select_shape(select: Any, selectors: tuple[type[Selection], ...] = DEFAULT_SELECTORS) -> None:
    """Refuse a selection that cannot be dispatched on, saying what arrived and what to write.

    The declaration refuses these too, as a discriminator complaint. A live model that had
    written its keys with quotation marks inside them was told about a discriminator four
    times running and never saw its own keys; this is the sentence it needed.
    """
    if select is None or isinstance(select, Selection):
        return
    kinds = ", ".join(get_args(model.model_fields["kind"].annotation)[0] for model in selectors)
    if isinstance(select, str):
        raise ValueError(
            "select is one flat object, not a string: write "
            + select
            + " as an object, not inside quotes."
        )
    if not isinstance(select, dict):
        raise ValueError(
            "select is one flat object with a required kind; choose kind from " + kinds + "."
        )
    quoted = [key for key in select if isinstance(key, str) and '"' in key]
    if "kind" in select and not quoted:
        return
    keys = ", ".join(json.dumps(key, ensure_ascii=False) for key in select)
    # The example reads best with kind first; the rest keep the order they arrived in.
    plain = {str(key).strip('"'): value for key, value in select.items()}
    meant = {**({"kind": plain["kind"]} if "kind" in plain else {}), **plain}
    if "kind" in select:
        raise ValueError(
            "select has keys with quotation marks inside them: "
            + keys
            + ". Write plain names, e.g. "
            + json.dumps(meant, ensure_ascii=False)
            + "."
        )
    if "kind" in meant:
        raise ValueError(
            "select needs a plain key named kind; this select has keys "
            + keys
            + ". Write the names without quotation marks inside them, e.g. "
            + json.dumps(meant, ensure_ascii=False)
            + "."
        )
    raise ValueError(
        "select needs a plain key named kind; this select has keys "
        + keys
        + " and no kind. Choose kind from "
        + kinds
        + "."
    )


def _derive_read_item_select(raw_item: Any) -> tuple[Any, str | None]:
    """The two literal shapes ``select`` arrives in when a model meant something else.

    A ``select`` sent as a JSON string is the object the model meant, quoted by a
    provider or a model that stringifies a nested argument; parsing it loses nothing
    (the probe corpus's own ``read_tool_calls.json`` carries this exact shape). A
    ``select`` that repeats the item's own ``location`` -- bare, or with the
    ``selection:`` prefix the field's name shares a root with -- names nothing beyond
    what ``location`` already named: read the whole passage. Neither guess fires when
    it would be ambiguous; an unparseable or unmatched string is left for the
    declaration's own "select is one flat object, not a string" refusal.
    """
    if not isinstance(raw_item, dict):
        return raw_item, None
    select_value = raw_item.get("select")
    if isinstance(select_value, dict) and isinstance(select_value.get("region"), dict):
        # A region whose keys arrive wrapped in their own quotation marks -- {"\"left\"": 0}
        # -- is the same region with a key spelled twice over by the transport; three of
        # hyperfirin's region reads were refused for it in one task (2026-10-10). Only the
        # marks are removed, and only when that leaves distinct keys.
        region = select_value["region"]
        unquoted = {str(key).strip('"\''): value for key, value in region.items()}
        if unquoted.keys() != region.keys() and len(unquoted) == len(region):
            return (
                {**raw_item, "select": {**select_value, "region": unquoted}},
                "region keys arrived inside quotation marks; read them without the marks",
            )
    terms = raw_item.get("terms")
    if (
        isinstance(terms, str)
        and terms.strip()
        and raw_item.get("location") == "search:text"
        and (
            select_value is None
            or (isinstance(select_value, dict) and select_value.get("kind") in (None, "whole")
                and not select_value.get("terms"))
        )
    ):
        # The words to search for written beside the location instead of inside select:
        # a search has nothing else those words could be (seen as the opening call of a
        # locating task, 2026-10-10, refused as an extra input).
        moved = {key: value for key, value in raw_item.items() if key != "terms"}
        return (
            {**moved, "select": {"kind": "query", "terms": terms}},
            "terms arrived beside the search location; read them as the query",
        )
    if not isinstance(select_value, str):
        return raw_item, None
    try:
        parsed_select = json.loads(select_value)
    except (ValueError, TypeError):
        parsed_select = None
    if isinstance(parsed_select, dict) and "kind" in parsed_select:
        return (
            {**raw_item, "select": parsed_select},
            "select arrived as a JSON string; read the object inside it",
        )
    location_value = raw_item.get("location")
    if isinstance(location_value, str) and (
        select_value == location_value or f"selection:{select_value}" == location_value
    ):
        return (
            {**raw_item, "select": None},
            "select repeated this item's own location; read it whole instead",
        )
    return raw_item, None


def canonical_read_item(raw: Any, *, item_model: type[ContractModel] | None = None):
    """The declared item, with only lossless selector transport forms decoded."""
    if isinstance(raw, ContractModel):
        raw = raw.model_dump(mode="json")
    working, derived = _derive_read_item_select(raw)
    select_shape(working.get("select") if isinstance(working, dict) else None)
    item = (item_model or ReadItem).model_validate_json(canonical_json_bytes(working))
    return item, derived


def _item_location(item: Any) -> Any:
    return item.get("location") if isinstance(item, dict) else getattr(item, "location", None)


def item_name(item: Any, *, item_model: type[ContractModel] | None = None) -> str:
    """The one spelling under which a read item is compared, stored and looked up again.

    The executor tags the content it delivers with this, and the content window finds
    the same item in the transcript by recomputing it from the recorded arguments; both
    sides must therefore spell it here and nowhere else.
    """
    try:
        parsed, _ = canonical_read_item(item, item_model=item_model)
        payload = parsed.model_dump(mode="json", exclude_defaults=True)
        if payload.get("select") == {"kind": "whole"}:
            payload.pop("select")
    except (ValueError, TypeError):
        # Invalid items keep every submitted field; they cannot replace a valid read.
        payload = item.model_dump(mode="json") if isinstance(item, ContractModel) else item
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def read_batch(
    request: Any, *, item_model: type[ContractModel] | None = None
) -> tuple[Any, ...]:
    """The one rule for how a read names what to read; declaration and executor share it.

    A batch is the shape. Naming a single location directly is the same request with one
    item, because that is what a model reaches for first and refusing it teaches nothing.
    Sending both is how a model hedges while it is learning the shape: when the two agree
    it is redundant and the batch stands; when they disagree the request means two things
    at once, and only then is it refused. The same item named twice in one batch is one
    item, read once.
    """
    if isinstance(request, ContractModel):
        request = request.model_dump(mode="json")
    if not isinstance(request, dict):
        raise ValueError("A read request must be an object")
    items, location, select = request.get("items"), request.get("location"), request.get("select")
    single_item = {key: request[key] for key in (item_model or ReadItem).model_fields if key in request}
    if location is None and select is not None:
        raise ValueError("select needs the location it applies to, or put both in items")
    batch = (
        tuple({item_name(item, item_model=item_model): item for item in items}.values())
        if items
        else ()
    )
    if not batch and location is None:
        raise ValueError('A read names what to read: {"items": [{"location": "..."}]}.')
    if location is None:
        return batch
    single = item_name(single_item, item_model=item_model)
    if not batch:
        return (single_item,)
    if any(item_name(item, item_model=item_model) == single for item in batch):
        return batch
    if select is None and any(_item_location(item) == location for item in batch):
        # A bare mention of a location the batch already selects says strictly less than
        # the batch does about it, so there is nothing here to disagree with.
        return batch
    raise ValueError(
        "This request reads two different things at once: items says "
        f"{', '.join(sorted({item_name(item, item_model=item_model) for item in batch}))} "
        "and the request "
        f"itself says {single}. Put every read in items."
    )


class ReadShape(ContractModel):
    """Carries the naming rule so a read is validated the same way wherever it arrives."""

    _item_model: ClassVar[type[ContractModel] | None] = None

    @model_validator(mode="after")
    def _names_what_to_read(self) -> Self:
        read_batch(self, item_model=self._item_model)
        return self


def read_models(
    selectors: tuple[type[Selection], ...] = DEFAULT_SELECTORS,
    *,
    reasons: tuple[str, ...] = (),
) -> tuple[type[ContractModel], type[ContractModel], type[ContractModel]]:
    """One schema/validator factory, also used for explicitly installed extensions.

    `reasons`, when a lane installs them, lets a read say why it is going back to a
    source it has already been given. A lane that installs none is unchanged to the
    byte, which is what keeps a pinned declaration pinned.

    Returns ``(item, envelope, request)``: the envelope carries every field name the
    request does -- including a lane-installed `reason`, which a hand-written envelope
    used to leave out and so refused as an unknown field -- built in the same call as
    the declaration, so the two field lists can never drift apart. There used to be a
    second, hand-written envelope class kept in step with this function by hand.
    """
    if not selectors:
        raise ValueError("a read tool requires at least one selector")
    kinds = []
    for model in selectors:
        tag = model.model_fields["kind"]
        values = get_args(tag.annotation)
        if (
            not tag.is_required()
            or get_origin(tag.annotation) is not Literal
            or len(values) != 1
            or not isinstance(values[0], str)
        ):
            raise ValueError("read selector kind must be a required single string Literal")
        kinds.append(values[0])
    if len(kinds) != len(set(kinds)):
        raise ValueError("read selectors require distinct kind values")
    # Pydantic generates both the declaration and the validator from these models.
    choice_type: Any = selectors[0]
    for model in selectors[1:]:
        choice_type = choice_type | model
    choice: Any = Annotated[choice_type, Field(discriminator="kind")]
    item_fields: dict[str, Any] = {
        "location": (str, Field(min_length=1)),
        "select": (choice | None, None),
    }
    if reasons:
        item_fields["reason"] = (
            Literal[reasons] | None,
            Field(
                default=None,
                description=(
                    "Why you are going back to a source you were already given, when "
                    "you are. The host answers the reason: a passage that breaks off "
                    "mid-sentence comes back with the passage beside it. Omit it for an "
                    "ordinary read."
                ),
            ),
        )
    item = create_model("ReadItem", __base__=ContractModel, **item_fields)
    request_fields: dict[str, Any] = {
        "items": (tuple[item, ...] | None, Field(default=None, max_length=MAX_READ_ITEMS)),
        "location": (str | None, Field(default=None, min_length=1)),
        "select": (choice | None, None),
    }
    if reasons:
        request_fields["reason"] = item_fields["reason"]
    # The envelope shares every field name and type with the request except `items`
    # and `select`, which it leaves as `Any`: a batch with one malformed item must
    # still let the executor read and reject that item on its own (`read_many`
    # validates each item against `item` itself, right after this), the same as a
    # top-level `select` shaped wrong on its own. Typing them as strictly as the
    # declaration does would make one bad item -- or a select a live provider had
    # stringified or quoted -- fail this whole call before either derivation or the
    # per-item diagnostic ever runs.
    envelope_fields: dict[str, Any] = {
        **request_fields,
        "items": (tuple[Any, ...] | None, Field(default=None, max_length=MAX_READ_ITEMS)),
        "select": (Any, None),
    }
    envelope = create_model("ReadEnvelope", __base__=ContractModel, **envelope_fields)
    request = create_model("ReadRequest", __base__=ReadShape, **request_fields)
    request._item_model = item
    request.__doc__ = (
        'Read an offered location unchanged: {"location": "the offered location"}. '
        "The host supplies the content and its reusable identity; do not construct a new "
        "selection name from a source name. Omit select for the default read; use an "
        "explicit selector only for a particular part. Returns actual content and pinned "
        "locations. Omitted selection reads bounded content (images: overview); large "
        "records return a directory. Source, derived and model work remain distinct. "
        "Use only selectors supported by that location. Reading never saves conclusions. "
        "select is one flat object with a required kind, not a string or a nested kind wrapper. "
        "Read several things at once with items; reading one thing may name its location "
        "and select directly instead. What you can still see is not sent twice: reading it "
        "again answers IN_CONTEXT and is not progress. Earlier reads that have left your "
        "context are listed under earlier_reads with how to read them again."
    )
    examples = (
        TextSelection(kind="text_range", start_sentence=0, end_sentence=2),
        TextPartSelection(kind="text_part", part=0),
        ImageSelection(
            kind="image_region",
            frame="source",
            region=ImageRegion(left=500, top=0, right=1000, bottom=400),
        ),
        RecordSelection(kind="record_items", fields=("text",)),
        DirectorySelection(kind="directory", offset=0, limit=5),
        WholeSelection(kind="whole"),
        QuerySelection(kind="query", terms="convergent fragment coupling"),
    )
    for example in examples:
        if type(example) in selectors:
            request.__doc__ += (
                " select example: "
                + json.dumps(
                    example.model_dump(mode="json", exclude_unset=True), separators=(",", ":")
                )
                + "."
            )
    return item, envelope, request


ReadItem, ReadEnvelope, ReadRequest = read_models()


def read_declaration(request: type[ContractModel] = ReadRequest) -> dict[str, Any]:
    return {
        "type": "function",
        "name": "read",
        "description": request.__doc__,
        "parameters": provider_declaration_schema(request),
    }
