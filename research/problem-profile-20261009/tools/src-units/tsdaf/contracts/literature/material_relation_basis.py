"""Current material relation provenance, preserving legacy shared precision.

This is Host transport at compilation, never another model input or edit ledger.
The shared legacy bucket did not say which source supported which relation; its
original field set and precision therefore remain explicit after transport.
"""
from copy import deepcopy

RELATION_FIELDS = ('reporting_level', 'extent', 'stage_id', 'summary_material_index')
MATERIAL_USE_LEVEL = 'USE'
MATERIAL_TOTAL_LEVEL = 'TOTAL'
MATERIAL_STAGE_EXTENT = 'WITHIN_OWNER'

MATERIAL_OBJECT_MEANING = (
    'A material row represents a reported material object. Missing name or amount means '
    'that information is unstated, not that the material is absent. Express an explicit '
    'absence as a scoped scientific condition with its explanation; do not create a '
    'material object solely to express its absence.')
MATERIAL_OTHER_QUANTITIES_MEANING = (
    'Other reported quantities about this material. Give each quantity its kind, '
    'reported_value and basis explaining what the quantity measures or qualifies. '
    'These quantities do not declare actual material use or a stage assignment.')


def material_relation_meanings():
    """Meaning of the existing relations, shared with their effective-result validator."""
    return {
        'reporting_level': (
            f'{MATERIAL_TOTAL_LEVEL} is a reported material total; {MATERIAL_USE_LEVEL} '
            'is an actual use. A total is not itself an addition or consumption.'),
        'extent': (
            'The declared scope of this material statement. A stage reference requires '
            f'{MATERIAL_STAGE_EXTENT}.'),
        'stage_id': (
            'A stage of this same operation. The material statement must declare '
            f'extent {MATERIAL_STAGE_EXTENT}; naming a stage does not infer that extent.'),
        'summary_material_index': (
            f'Links an actual {MATERIAL_USE_LEVEL} to a {MATERIAL_TOTAL_LEVEL} material '
            'in this same operation and final materials collection. The target must be '
            'another material and cannot itself link to a summary. This is not a '
            'general relationship between any two quantities; do not declare actual use '
            'unless that is the intended meaning.'),
    }


def material_input_relation_rules(*, summary_name, stage_name, container=False, existing_name='existing'):
    """Only input-local constraints; existing objects can inherit omitted relation fields.

    Only the container boundary knows whether a supplied existing object is selected.
    Field-only validation cannot require inherited values after identity is stripped.
    Target identity, membership and target level need the final effective objects and
    remain with the shared result validator. No scientific relationship is inferred.
    """
    rules = []
    for reference, field, value in (
        (summary_name, 'reporting_level', MATERIAL_USE_LEVEL),
        (stage_name, 'extent', MATERIAL_STAGE_EXTENT),
    ):
        declared = {'required': [reference], 'properties': {reference: {'type': 'string'}}}
        if not container:
            rules.append({'if': declared, 'then': {'properties': {field: {'const': value}}}})
        else:
            rules.append({'if': {**declared, 'not': {'required': [existing_name]}},
                          'then': {'required': [field]}})
    return rules

LEGACY_BASIS_KEYS = {
    'relation_evidence_refs':'evidence_refs',
    'relation_record_refs':'model_record_refs',
    'relation_submission_refs':'model_submission_refs',
}


def relation_basis_schema():
    properties={key:{'type':'array','items':{'$ref':'#/$defs/'+definition}}
        for key,definition in (('evidence_refs','EvidenceRef'),
            ('model_record_refs','ModelRecordRef'),('model_submission_refs','ModelSubmissionRef'))}
    properties.update(precision={'enum':['EXPLICIT_FIELD','LEGACY_SHARED']},
        original_fields={'type':'array','items':{'enum':list(RELATION_FIELDS)},
                         'uniqueItems':True,'minItems':1})
    basis={'type':'object','properties':properties,'required':['precision'],
        'additionalProperties':False,
        'allOf':[{'if':{'properties':{'precision':{'const':'LEGACY_SHARED'}}},
            'then':{'required':['original_fields']},
            'else':{'not':{'required':['original_fields']}}}]}
    return {'type':'object','properties':{key:deepcopy(basis) for key in RELATION_FIELDS},
            'additionalProperties':False}


def material_relation_rules():
    """Generated public schema and runtime use exactly these presence rules."""
    return {'allOf':[{'if':{'required':['relation_field_bases']},'then':{
        'not':{'anyOf':[{'required':[key]} for key in LEGACY_BASIS_KEYS]},
        'properties':{'relation_field_bases':{'properties':{
            key:{'if':{'properties':{'precision':{'const':'LEGACY_SHARED'}}},
                 'then':{'properties':{'original_fields':{'contains':{'const':key}}}}}
            for key in RELATION_FIELDS}}},
        'allOf':[{'if':{'required':[key]},
            'then':{'properties':{'relation_field_bases':{'required':[key]}}},
            'else':{'properties':{'relation_field_bases':{'not':{'required':[key]}}}}}
            for key in RELATION_FIELDS]}}]}


def current_relation_bases(row):
    if 'relation_field_bases' in row:
        return deepcopy(row['relation_field_bases'])
    fields=[key for key in RELATION_FIELDS if key in row]
    if not fields:
        return {}
    shared={'precision':'LEGACY_SHARED','original_fields':fields,
        **{target:deepcopy(row[key]) for key,target in LEGACY_BASIS_KEYS.items() if row.get(key)}}
    return {key:deepcopy(shared) for key in fields}


def transport_relation_bases(before, after, *, evidence, records, submissions, equivalent):
    """Preserve unchanged relations; replace only explicitly changed values.

    ``equivalent`` is an address codec comparison: a reordered summary index is
    compared by its actual target identity. It never interprets scientific prose.
    """
    old=current_relation_bases(before or {})
    result={}
    for key in RELATION_FIELDS:
        if key not in after:
            continue
        if before is not None and key in before and equivalent(key,before[key],after[key]):
            result[key]=old[key]
        else:
            result[key]={'precision':'EXPLICIT_FIELD',
                'evidence_refs':deepcopy(list(evidence)),
                'model_record_refs':deepcopy(list(records)),
                'model_submission_refs':deepcopy(list(submissions))}
    for key in LEGACY_BASIS_KEYS:
        after.pop(key,None)
    if result or 'relation_field_bases' in after:
        after['relation_field_bases']=result


def has_material_relation_basis(row):
    from jsonschema import Draft202012Validator
    if 'relation_field_bases' in row:
        return (Draft202012Validator(material_relation_rules()).is_valid(row)
            and all(any(row['relation_field_bases'][key].get(slot) for slot in LEGACY_BASIS_KEYS.values())
                    for key in RELATION_FIELDS if key in row))
    return any(row.get(key) for key in LEGACY_BASIS_KEYS)
