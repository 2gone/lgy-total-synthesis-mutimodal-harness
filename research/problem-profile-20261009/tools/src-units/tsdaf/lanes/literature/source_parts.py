"""Host copies already delivered source fragments; the model supplies their owner."""

import json
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from pydantic import Field, model_validator

from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.tool_write import DIAGNOSTIC_CHARS
from tsdaf.lanes.literature.author_words import quoted_source_candidates, quoted_source_fragments
from tsdaf.lanes.literature.citation_policy import SOURCE_SELECTION_MEANING


class SourceSelection(ContractModel):
    """Select delivered text; no offsets, evidence hashes or normalized values."""

    selection_id: str = Field(
        min_length=1, description="Meaningful name of a source text selection. " + SOURCE_SELECTION_MEANING
    )
    quote: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Optional short continuous author words locating the intended fragment in "
            "this selection. Omit to use the complete selected text. For repeated words, "
            "choose an offered quote/within pair; within locates but is not saved as content."
        ),
    )

    within: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Optional continuous context uniquely locating quote in this selection. "
            "Used only for locating: only the quote fragment is saved and parsed. "
            "Choose an offered quote/within pair for repeated text; requires quote."
        ),
    )

    @model_validator(mode="after")
    def context_requires_quote(self) -> "SourceSelection":
        if self.within is not None and self.quote is None:
            raise ValueError("within requires a quote to identify the content to save")
        return self

    @classmethod
    def __get_pydantic_json_schema__(cls, core: Any, handler: Any) -> dict[str, Any]:
        schema = handler(core)
        schema["allOf"] = [
            {
                "if": {"required": ["within"], "properties": {"within": {"type": "string"}}},
                "then": {"required": ["quote"], "properties": {"quote": {"type": "string"}}},
            }
        ]
        return schema


def selected_source_part(
    choice: SourceSelection,
    selections: Mapping[str, Mapping[str, Any]],
    *,
    field_path: str,
) -> dict[str, Any]:
    """One exact source rule for write checking and public compilation."""
    from tsdaf.lanes.literature.model_names import UnknownReference

    sid = choice.selection_id
    if sid not in selections:
        raise UnknownReference(
            "selection_id", sid, sorted(selections), field_path + " cites an unavailable source"
        )
    selected = selections[sid]
    if not isinstance(selected.get("evidence_ref"), Mapping):
        raise ValueError(field_path + ": the selected source lacks its exact evidence binding")
    text = selected.get("exact_text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(field_path + ": the selected source cannot supply author text")
    if choice.within is not None:
        contexts = quoted_source_fragments(text, choice.within)
        if len(contexts) != 1:
            raise ValueError(
                field_path + ": within must locate one continuous context in the named source"
            )
        text = contexts[0]
    if choice.quote is not None:
        matches = quoted_source_fragments(text, choice.quote)
        if not matches:
            raise ValueError(
                field_path + ": quote is not continuous in the named source; "
                "select its separate fragments without joining them"
            )
        if len(matches) != 1:
            prefix = (
                field_path + ": quote has multiple occurrences in the named source. "
                "Choose the intended occurrence using its context; keep the same selection_id "
                "and use its offered quote/within pair, or select a narrower delivered passage. "
                "Context is for locating, not an instruction to copy all its quantities. "
                "No occurrence was selected. Candidates: "
            )
            offered = []
            total = len(quoted_source_fragments(selected["exact_text"], choice.quote))
            for candidate in quoted_source_candidates(selected["exact_text"], choice.quote):
                trial = [*offered, candidate]
                suffix = f" Showing {len(trial)} of {total} occurrences."
                if len(prefix + json.dumps(trial, ensure_ascii=False) + suffix) > DIAGNOSTIC_CHARS:
                    break
                offered = trial
            suffix = f" Showing {len(offered)} of {total} occurrences."
            raise ValueError(prefix + json.dumps(offered, ensure_ascii=False) + suffix)
        text = matches[0]
    return {"text": text, "evidence_refs": [dict(selected["evidence_ref"])]}


def fact_source_ids(fact: Any) -> tuple[str, ...]:
    """Source references actually expressed by a fact/change, in source order."""
    def get(value, name, default=()):
        return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)

    return tuple(dict.fromkeys((
        *(get(fact, "source_ids") or ()),
        *get(fact, "add_source_ids"),
        *(get(part, "selection_id") for part in (get(fact, "source_parts") or ())),
    )))


@lru_cache(maxsize=64)
def source_selection_entries(
    model: type[ContractModel],
    aspect: str | None = None,
    *, root_path: str = "words",
) -> dict[str, str]:
    """Find source-array paths in the actual writer, including nested sequence members."""
    schema = model.model_json_schema()
    selector = SourceSelection.model_json_schema()
    shape = {
        name + ("" if name in selector["required"] else "?"): " | ".join(
            part["type"] for part in field.get("anyOf", [field])
        )
        for name, field in selector["properties"].items()
    }
    result: dict[str, str] = {}

    def visit(field: Mapping[str, Any], path: str, active: tuple[str, ...] = ()) -> None:
        ref = field.get("$ref")
        if ref:
            if ref in active:
                return
            visit(schema["$defs"][ref.rsplit("/", 1)[-1]], path, (*active, ref))
            return
        if field.get("type") == "array":
            item = field.get("items", {})
            if item.get("$ref", "").endswith("/SourceSelection"):
                result[path] = (
                    field.get("description", "")
                    + " Source-selection array shape: "
                    + json.dumps([shape], ensure_ascii=False)
                    + ". "
                    + SOURCE_SELECTION_MEANING + " quote is optional, as defined by the source selector."
                )
            else:
                visit(item, path + "[*]", active)
        for name, child in field.get("properties", {}).items():
            visit(child, (path + "." if path else "") + name, active)
        for keyword in ("anyOf", "oneOf"):
            for child in field.get(keyword, ()):
                visit(child, path, active)

    root = schema
    if aspect is not None:
        root = next(
            branch
            for branch in schema["oneOf"]
            if branch["properties"]["aspect"]["const"] == aspect
        )
    visit(root, "" if aspect is not None else root_path)
    return result
