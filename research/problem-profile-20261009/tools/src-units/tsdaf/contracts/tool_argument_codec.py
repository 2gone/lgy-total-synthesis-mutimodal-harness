"""Explicit transport spelling tolerance, separate from canonical scientific rules."""
from copy import deepcopy
import json

from jsonschema import Draft202012Validator

CODEC_DESCRIPTOR = {'version': 'property-spelling.v1'}
CODEC_DESCRIPTION = ('Property keys may have surrounding whitespace or one matched pair of outer quotes; '
    'the Host resolves them only to a unique declared key and reports the spelling change. '
    'Values and names are never corrected; duplicate or colliding keys are ambiguous.')


def decode_arguments(arguments):
    if not isinstance(arguments, str):
        return deepcopy(arguments)

    def unique(pairs):
        value = {}
        for key, child in pairs:
            if key in value:
                raise ValueError('Duplicate JSON property: '+repr(key))
            value[key] = child
        return value
    return json.loads(arguments, object_pairs_hook=unique)


def normalize_properties(value, schema):
    """Return a derived value and explicit spelling notes; never mutate the response.

    Branches must select one unambiguous representation. Unknown data maps are
    opaque, and unsuccessful scientific validation does not authorize guessing.
    The caller still validates the entire derived value against the canonical schema.
    """
    def spelling_needed(raw):
        if isinstance(raw, dict):
            return any(key != key.strip() or (len(key)>1 and key[0] == key[-1] and key[0] in ('"', "'"))
                       or spelling_needed(child) for key, child in raw.items())
        return isinstance(raw, list) and any(spelling_needed(child) for child in raw)

    def walk(raw, shape, path):
        if not spelling_needed(raw):
            return deepcopy(raw), []
        for keyword in ('oneOf', 'anyOf'):
            if keyword in shape:
                candidates = []
                failures = []
                for branch in shape[keyword]:
                    try:
                        result = walk(raw, branch, path)
                    except ValueError as error:
                        failures.append(error)
                        continue
                    if Draft202012Validator(branch).is_valid(result[0]):
                        candidates.append(result)
                if candidates:
                    first = candidates[0]
                    if any(row != first for row in candidates[1:]):
                        raise ValueError('Ambiguous property spelling at '+path)
                    return first
                if failures:
                    raise failures[0]
                return deepcopy(raw), []
        if isinstance(raw, dict) and 'properties' in shape:
            properties = shape['properties']
            result, notes = {}, []
            for key, child in raw.items():
                target = key
                if key not in properties:
                    candidate = key.strip()
                    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in ('"', "'"):
                        candidate = candidate[1:-1].strip()
                    if candidate in properties:
                        target = candidate
                if target in result or target != key and target in raw:
                    raise ValueError('Colliding JSON properties at '+path+': '+repr(key)+' -> '+repr(target))
                item_path = path+'/'+target.replace('~','~0').replace('/','~1')
                if target != key:
                    notes.append({'path':item_path,'original_key':key,'canonical_key':target})
                converted, nested = walk(child, properties.get(target, {}), item_path)
                result[target] = converted
                notes.extend(nested)
            return result, notes
        if isinstance(raw, list) and isinstance(shape.get('items'), dict):
            result, notes = [], []
            for index, child in enumerate(raw):
                converted, nested = walk(child, shape['items'], path+'/'+str(index))
                result.append(converted)
                notes.extend(nested)
            return result, notes
        return deepcopy(raw), []
    return walk(value, schema, '')
