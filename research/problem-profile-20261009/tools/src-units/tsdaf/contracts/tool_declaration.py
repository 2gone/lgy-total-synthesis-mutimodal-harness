"""The one rendering of a tool declaration's JSON Schema, for every place that builds one.

A declaration travels in every turn of every task, so what it repeats it pays for on
every turn. Pydantic writes two kinds of repetition into a model's schema, and neither
says anything a model can act on:

* ``title`` on every model and every field -- the field's own name, in title case.
* the item model's docstring on its ``$defs`` entry, when the property that references
  it already carries that same docstring as its description (which is how
  ``tool_write.write_request`` builds a collection's description).

Removing them is not a contract change: what a model may send, and what the executor
accepts, are unchanged. It is measured rather than assumed -- the experimental-detail
write went 25,146 -> 24,114 bytes on the day this landed, and its four-collection
block-scoped form 13,229 -> 12,197.

Generating the declaration and pinning it are two places (the lane's ``*_resources``
and ``schemas/generate.py``); both call this, or the snapshot gate would pin a
declaration no task is ever shown.

Canonical declarations retain definitions for internal contract readers. Provider
declarations inline local references before resources are hashed and sealed: the
current provider does not reliably expose referenced fields to the model. Router's
custom declaration uses the same inliner. Historical sealed resources stay immutable.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from typing import Any, NoReturn
from urllib.parse import unquote

from pydantic import BaseModel

__all__ = (
    "declaration_schema", "inline_local_schema", "name_repeated_shapes",
    "omit_stated_descriptions", "provider_declaration_schema",
)


# Maps whose keys are names the schema declares, not schema keywords: a field or a
# definition that is itself named title is part of the contract, not repetition.
_NAMED = frozenset({"properties", "$defs"})


def _strip_titles(node: Any, *, names: bool = False) -> None:
    if isinstance(node, dict):
        if not names:
            node.pop("title", None)
        for key, value in node.items():
            _strip_titles(value, names=not names and key in _NAMED)
    elif isinstance(node, list):
        for value in node:
            _strip_titles(value)


def _descriptions_holding(
    node: Any, inherited: str | None, found: dict[str, list[str | None]]
) -> None:
    """For each ``$defs`` name, the descriptions of the places that reference it."""
    if isinstance(node, dict):
        description = node.get("description")
        current = description if isinstance(description, str) else inherited
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            found.setdefault(reference.rsplit("/", 1)[-1], []).append(current)
        for key, value in node.items():
            if key != "$defs":
                _descriptions_holding(value, current, found)
    elif isinstance(node, list):
        for value in node:
            _descriptions_holding(value, inherited, found)


def declaration_schema(model: type[BaseModel], *, share_repeated: bool = False) -> dict[str, Any]:
    """Canonical tool schema, also used by internal contract and short-entry readers."""
    schema: dict[str, Any] = model.model_json_schema(mode="validation")
    _strip_titles(schema)
    definitions = schema.get("$defs")
    if isinstance(definitions, dict):
        holders: dict[str, list[str | None]] = {}
        _descriptions_holding(
            {key: value for key, value in schema.items() if key != "$defs"}, None, holders
        )
        # A definition is referenced from other definitions too, and those references
        # carry their own descriptions. Counting only the top level made the rule below
        # vacuously true for them: the fusion write's `FactChangeGroup.changes` says
        # something else entirely about a change, and the words it does not repeat were
        # dropped from `FactChange` because the top-level `changes` happened to repeat
        # them. Each definition's own description is not a holder of itself.
        for body in definitions.values():
            if isinstance(body, dict):
                _descriptions_holding(
                    {key: value for key, value in body.items() if key != "description"},
                    None,
                    holders,
                )
        for name, body in definitions.items():
            if not isinstance(body, dict):
                continue
            description = body.get("description")
            referencing = holders.get(name, [])
            # Drop it only where it is already said: every reference to this definition
            # must come from a description that contains these very words.
            if (
                isinstance(description, str)
                and referencing
                and all(isinstance(h, str) and description in h for h in referencing)
            ):
                body.pop("description")
    return _share_repeated(schema) if share_repeated else copy.deepcopy(schema)


def provider_declaration_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The actual tool parameters, generated before resource hashing and transport."""
    return inline_local_schema(declaration_schema(model))


_SCHEMA_MAPS = frozenset({
    'properties', '$defs', 'definitions', 'patternProperties', 'dependentSchemas',
})
_SCHEMA_VALUES = frozenset({
    'items', 'contains', 'not', 'if', 'then', 'else', 'additionalProperties',
    'unevaluatedProperties', 'propertyNames', 'additionalItems', 'unevaluatedItems',
    'contentSchema',
})
_SCHEMA_ARRAYS = frozenset({'anyOf', 'oneOf', 'allOf', 'prefixItems'})
_SCOPED = frozenset({'$id', '$anchor', '$dynamicAnchor', '$dynamicRef',
                     '$recursiveRef', '$recursiveAnchor'})
_REF_ANNOTATIONS = frozenset({
    'title', 'description', '$comment', 'default', 'examples', 'deprecated',
    'readOnly', 'writeOnly',
})


def inline_local_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline acyclic local schema pointers without changing validation rules.

    Only schema positions are visited. Property names and literal example/default
    data are never interpreted as keywords. Unsupported references fail preparation
    with their technical location; they are never deleted or left for the model.
    """
    def pointer(path: tuple[str, ...]) -> str:
        return '#' + ''.join('/' + p.replace('~', '~0').replace('/', '~1') for p in path)

    def fail(path: tuple[str, ...], reason: str) -> NoReturn:
        raise ValueError(f'provider schema at {pointer(path)}: {reason}')

    def visit(node: Any, path: tuple[str, ...], active: tuple[str, ...]) -> Any:
        if not isinstance(node, (dict, bool)):
            fail(path, 'expected a schema object or boolean')
        if isinstance(node, bool):
            return node
        scoped = _SCOPED & node.keys()
        if scoped:
            fail(path, f'unsupported scoped schema keywords: {sorted(scoped)}')
        if '$ref' in node:
            ref = node['$ref']
            if not isinstance(ref, str) or not ref.startswith('#/'):
                fail(path, 'only local JSON Pointer references are supported')
            ref = unquote(ref)
            if ref in active:
                fail(path, f'recursive reference {ref}')
            # A schema reference must target a schema position, not a literal value.
            target = locations.get(ref)
            if target is None:
                fail(path, f'unresolved schema reference {ref}')
            siblings = set(node) - {'$ref'}
            result = visit(target[0], target[1], (*active, ref))
            if not siblings:
                return result
            if (siblings <= _REF_ANNOTATIONS and isinstance(result, dict)
                    and all(key not in result or result[key] == node[key] for key in siblings)):
                result.update({key: copy.deepcopy(node[key]) for key in siblings})
                return result
            # $ref and its siblings both apply to the same instance. Keep that
            # conjunction, including both descriptions, instead of overwriting one
            # side. Wrapping the target in allOf preserves evaluated-property
            # annotations for sibling unevaluatedProperties too.
            adjacent = visit({key: value for key, value in node.items() if key != '$ref'},
                             path, active)
            adjacent.setdefault('allOf', []).insert(0, result)
            result = adjacent
            return result
        result = copy.deepcopy(node)
        for key, value in node.items():
            if key in {'$defs', 'definitions'}:
                result.pop(key)
            elif key in _SCHEMA_MAPS:
                result[key] = {name: visit(child, (*path, key, name), active)
                               for name, child in value.items()}
            elif key in _SCHEMA_ARRAYS or (key == 'items' and isinstance(value, list)):
                result[key] = [visit(child, (*path, key, str(i)), active)
                               for i, child in enumerate(value)]
            elif key in _SCHEMA_VALUES:
                result[key] = visit(value, (*path, key), active)
        return result

    locations: dict[str, tuple[Any, tuple[str, ...]]] = {}

    def index(node: Any, path: tuple[str, ...]) -> None:
        locations[pointer(path)] = (node, path)
        if not isinstance(node, dict):
            return
        scoped = _SCOPED & node.keys()
        if scoped:
            fail(path, f'unsupported scoped schema keywords: {sorted(scoped)}')
        for key, value in node.items():
            if key in _SCHEMA_MAPS:
                for name, child in value.items():
                    index(child, (*path, key, name))
            elif key in _SCHEMA_ARRAYS or (key == 'items' and isinstance(value, list)):
                for i, child in enumerate(value):
                    index(child, (*path, key, str(i)))
            elif key in _SCHEMA_VALUES:
                index(value, (*path, key))

    index(schema, ())
    return visit(schema, (), ())


def _schema_children(schema: Any, transform: Any) -> Any:
    """Transform schema positions only, never example/default values or property maps."""
    if not isinstance(schema, dict):
        return copy.deepcopy(schema)
    result = copy.deepcopy(schema)
    for key, value in schema.items():
        if key in _SCHEMA_MAPS:
            result[key] = {name: transform(child) for name, child in value.items()}
        elif key in _SCHEMA_ARRAYS or (key == 'items' and isinstance(value, list)):
            result[key] = [transform(child) for child in value]
        elif key in _SCHEMA_VALUES:
            result[key] = transform(value)
    return result


def _share_repeated(schema: dict[str, Any]) -> dict[str, Any]:
    """Factor identical sub-schemas without changing a rule or its description.

    Existing definitions/references remain rooted in the same document. Scoped
    identifiers and anchors opt out: moving their bodies could change resolution.
    This remains an opt-in canonical rendering for contract comparisons. Actual
    provider declarations use the inline representation; sealed history is unchanged.
    """
    counts: Counter[str] = Counter()
    scoped = False
    def signature(node: Any) -> str:
        return json.dumps(node, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    def count(node: Any) -> Any:
        nonlocal scoped
        if isinstance(node, dict):
            scoped |= bool({'$id', '$anchor', '$dynamicAnchor', '$dynamicRef'} & node.keys())
            encoded = signature(node)
            if len(encoded) >= 1024 and '$defs' not in node:
                counts[encoded] += 1
        return _schema_children(node, count)
    count(schema)
    if scoped:
        return copy.deepcopy(schema)
    existing = set(schema.get('$defs', {}))
    names: dict[str, str] = {}
    generated: dict[str, Any] = {}
    def replace(node: Any) -> Any:
        encoded = signature(node)
        if counts[encoded] > 1:
            if encoded not in names:
                ordinal = len(names) + 1
                while (name := f'SharedShape{ordinal}') in existing or name in names.values():
                    ordinal += 1
                names[encoded] = name
                generated[name] = _schema_children(node, replace)
            return {'$ref': '#/$defs/' + names[encoded]}
        return _schema_children(node, replace)
    result = _schema_children(schema, replace)
    if generated:
        result.setdefault('$defs', {}).update(generated)
    return result


def omit_stated_descriptions(schema: Any, stated: str) -> Any:
    """Drop field descriptions the same request already states word for word.

    ``stated`` is the text sent beside this declaration in the same request. A
    description is omitted only when that text contains it verbatim, so nothing the
    model is told is lost and nothing depends on an earlier request. Types, required
    keys, enums, bounds and every other rule are untouched; a property that happens
    to be named ``description`` is a name, not an annotation, and is never visited.
    """
    def visit(node: Any) -> Any:
        result = _schema_children(node, visit)
        if isinstance(result, dict):
            description = result.get('description')
            if isinstance(description, str) and description and description in stated:
                del result['description']
        return result

    return visit(schema)


def _without_descriptions(schema: Any) -> Any:
    def visit(node: Any) -> Any:
        result = _schema_children(node, visit)
        if isinstance(result, dict):
            result.pop('description', None)
        return result

    return visit(schema)


def name_repeated_shapes(schema: Any, named: dict[str, Any], *, note: str) -> Any:
    """Declare each named object or list shape in full once; later positions name it.

    The provider cannot use local references, so an inlined declaration repeats a
    large shape at every position that accepts it. ``named`` maps the names the same
    request's prompt gives these shapes to the shapes themselves. The first position
    in document order keeps the complete shape; a later position whose rules are
    exactly that shape keeps only its type and ``note`` with its name. Nothing is
    matched by resemblance: a position that differs in any rule stays complete.
    The Host still validates every position against the complete contract.
    """
    signature = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'))  # noqa: E731
    names = {signature(_without_descriptions(shape)): name for name, shape in named.items()}
    seen: set[str] = set()

    def visit(node: Any) -> Any:
        name = names.get(signature(_without_descriptions(node))) if isinstance(node, dict) else None
        if name is not None:
            if name in seen:
                kind = 'array' if node.get('type') == 'array' else 'object'
                return {'type': kind, 'description': note.format(name=name)}
            seen.add(name)
        return _schema_children(node, visit)

    return visit(schema)
