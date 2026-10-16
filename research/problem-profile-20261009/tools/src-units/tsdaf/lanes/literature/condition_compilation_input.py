"""Direct condition values use the existing public sequence representation.

This is a storage-shape adapter, shared by declarations, both compilers and reads.
The model still supplies category, scope and stage; no prose is interpreted.
"""
from copy import deepcopy

from tsdaf.lanes.literature.scientific_field_bindings import FIELDS


DIRECT_VALUE_DESCRIPTION = (
    "The condition's scientific value, including qualitative text without a number or unit. "
    'Use value for that meaning even if wording or explanation is also recorded. '
    'Supply category and the explicit stage/scope; Host packages the storage nesting. '
    'When values go together or follow one another, use sequence instead: each member holds '
    'the values given together, each with its own category; members follow the stated order, '
    'and value_relation ORDERED_POINTS states that they are successive.'
)


def condition_input_shape(shape, leaf_shape, *, partial):
    result = deepcopy(shape)
    result['properties']['value'] = {
        **deepcopy(leaf_shape['properties']['typed_value']),
        'description': DIRECT_VALUE_DESCRIPTION,
    }
    result.setdefault('allOf', []).extend([
        {'not': {'required': ['value', 'sequence']}},
        {'if': {'required': ['value']}, 'then': {
            'properties': {'category': deepcopy(leaf_shape['properties']['category'])},
            **({} if partial else {'required': ['category']}),
        }},
    ])
    return result


def expand_condition_value(fields, before=None):
    if 'value' not in fields:
        return fields
    if 'sequence' in fields:
        raise ValueError('ConditionNote: choose value or sequence, not both in one submission')
    category = fields.get('category', (before or {}).get('category'))
    if category is None:
        raise ValueError('ConditionNote.category is required for a direct value on a new condition')
    result = deepcopy(fields)
    value = result.pop('value')
    result['category'] = category
    # Explicit value replaces this collection, never borrows old member provenance.
    result['sequence'] = [{'values': [{'category': category, 'typed_value': value}]}]
    return result


def compact_condition_value(fields, public):
    sequence = public.get('sequence')
    if not isinstance(sequence, list) or len(sequence) != 1:
        return fields
    member = sequence[0]
    host = lambda definition: {key for key, binding in FIELDS[definition].items() if binding.role == 'HOST'}
    if set(member) - host('ConditionSequenceMember') != {'values'}:
        return fields
    values = member['values']
    if len(values) != 1:
        return fields
    leaf = values[0]
    if set(leaf) - host('ConditionSequenceValue') != {'category', 'typed_value'}:
        return fields
    if leaf['category'] != public.get('category'):
        return fields
    result = deepcopy(fields)
    result['value'] = result.pop('sequence')[0]['values'][0]['typed_value']
    return result
