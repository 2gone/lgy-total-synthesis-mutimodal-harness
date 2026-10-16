"""Model input for explicit compilation, built from the public contract and bindings.

This surface is used only at skeleton/final compilation boundaries. It does not
change the middle layer's free scientific account into a database form.
"""
from copy import deepcopy
from decimal import Decimal
from functools import cache
import json
from typing import Any, ClassVar

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from tsdaf.contracts.literature.schema import _error_refusal
from tsdaf.contracts.literature.objects import registered_conflict_fields, registered_field_value_schema
from pydantic import RootModel, model_validator


from tsdaf.lanes.literature.scientific_field_bindings import (
    ATOMIC_ADAPTERS, BY_FAMILY, OBJECTS, FIELDS, EMPTY_SLOTS, ROOT_DECLARATIONS, authority_schema, public_definition,
)

_SCALAR_PROPERTIES = public_definition('ReportedScalarValue')['properties']
NUMBER = deepcopy(next(branch for branch in _SCALAR_PROPERTIES['numeric_value_decimal']['anyOf']
                       if branch.get('type') == 'string'))
# Space around a separately stated number carries no meaning; the digits do.
NUMBER['pattern'] = '^\\s*' + NUMBER['pattern'].removeprefix('^').removesuffix('$') + '\\s*$'
SCALAR_TEXT = deepcopy(_SCALAR_PROPERTIES['raw_text'])
SCALAR_UNIT = deepcopy(_SCALAR_PROPERTIES['unit'])
SCALAR_INPUT = {'oneOf':[
    {'type':'object','properties':{'value':NUMBER,'unit':SCALAR_UNIT,'text':SCALAR_TEXT},
     'required':['value'],'additionalProperties':False},
    {'type':'object','properties':{'range':{'type':'array','items':NUMBER,'minItems':2,'maxItems':2},
                                 'unit':SCALAR_UNIT,'text':SCALAR_TEXT},
     'required':['range'],'additionalProperties':False},
    {'type':'object','properties':{'text':SCALAR_TEXT},
     'required':['text'],'additionalProperties':False},
], 'description':(
    'Numeric, range, or intact qualitative value. The text-only form is a scientific '
    'value, not a quotation; do not invent a number. Preserve comparisons/ratios with '
    'no typed slot intact; never replace ≤ by =. Host performs arithmetic, not prose extraction.')}


LITERAL_DESCRIPTION = (
    'Author wording, not merely account prose. It does not replace values or relations '
    'in their scientific fields. A record basis does not verify a quotation.')
INTERPRETATION_DESCRIPTION = 'Model explanation, not an author quotation or a substitute for explicit values and relationships.'
REFERENCE_DESCRIPTION = 'Exact supplied or local object names; never labels matched by similarity. Host replaces the names with identities.'
UNSTATED_DESCRIPTION = 'Explicitly clear a claim without asserting absence or uncertainty.'
SHARED_REF_DESCRIPTION = 'An explicitly shared object defined once with a local label in this document.'
SKELETON_MEMBERS_DESCRIPTION = 'The complete ordered membership of this collection. Nest scientific content here; use ref for an explicitly shared local object.'
QUANTITY_KIND_NOTE = (
    'ReportedQuantity.kind: the listed kinds are the only named ones, not the only quantities. A stated '
    'quantity of any other kind is written the same way with kind omitted: reported_value holds the number '
    'and unit, basis says in the note\'s words what it measures.')
MATERIAL_AMOUNT_NOTE = (
    'Material.amount: one stated quantity of the material. Every other quantity the note states for that '
    'same material (its amount of substance, volume, concentration, purity) is a member of '
    'additional_reported_quantities. None is left only in text or dropped because another quantity is already given.')
MEMBERS_DESCRIPTION = (
    'Members of this collection. Name a baseline member with existing to add to or correct it; a baseline member '
    'you do not name is kept unchanged. A member without existing is new. Use ref for an explicitly shared local object.')
REMOVE_DESCRIPTION = 'Remove this baseline member: {"existing": its offered name, "remove": true}, with no other key. Nothing else removes a member.'


def resolved_schema(schema):
    """Expand a type reference and intersect explicit public state restrictions."""
    if '$ref' in schema:
        name=schema['$ref'].rsplit('/',1)[-1]
        return public_definition(name),name
    if 'allOf' in schema and 'properties' not in schema:
        result,name=resolved_schema(schema['allOf'][0])
        result=deepcopy(result)
        for constraint in schema['allOf'][1:]:
            for key,restriction in constraint.get('properties',{}).items():
                if key=='state' and authority_schema(result):
                    states=result['properties']['state'].get('enum',[])
                    allowed=restriction.get('enum',[restriction['const']] if 'const' in restriction else states)
                    result['properties']['state']['enum']=[v for v in states if v in allowed]
                # Full output constraints remain enforced by the public validator.
        return result,name
    return schema,None


def input_shape(schema, *, name=None, partial=False, concrete_override=None, skeleton=False):
    expanded,named=resolved_schema(schema)
    if expanded is not schema:
        return input_shape(expanded,name=named or name,partial=partial,concrete_override=concrete_override,skeleton=skeleton)
    if name=='ReportedScalarValue':
        return deepcopy(SCALAR_INPUT)
    if name=='StructureValue':
        return {'type':'object','properties':{k:deepcopy(schema['properties'][k])
                for k in ('input_format','raw_value')},'required':['input_format','raw_value'],
                'additionalProperties':False}
    if authority_schema(schema):
        states=schema['properties']['state']['enum']
        value_schema=schema['properties']['value']
        # Nullable scientific values retain a PRESENT null; it is distinct from
        # the absence of a claim. A nonnullable public value disallows it below.
        choices=value_schema.get('anyOf',[value_schema])
        nullable=any('Nullable' in v.get('$ref','') for v in choices)
        concrete=concrete_override or {'anyOf':[input_shape(v,partial=partial,skeleton=skeleton) for v in choices
                  if v.get('type')!='null' or nullable]}
        branches=[]
        if 'PRESENT' in states:
            branches.append(deepcopy(concrete))
        if 'NOT_FOUND' in states:
            branches.append({'type':'object','properties':{'claim':{'const':'NOT_FOUND'}},
                             'required':['claim'],'additionalProperties':False})
        if 'UNRESOLVED' in states:
            reason=deepcopy(schema['properties']['reason_code'])
            if 'anyOf' in reason:
                reason={'anyOf':[v for v in reason['anyOf'] if v.get('type')!='null']}
            branches.append({'type':'object','properties':{'claim':{'const':'UNRESOLVED'},'reason':reason,
                'alternatives':{'type':'array','items':concrete}},'required':['claim','reason'],
                'additionalProperties':False})
        if 'UNSTATED' in states:
            branches.append({'type':'object','properties':{'claim':{'const':'UNSTATED'}},
                             'required':['claim'],'additionalProperties':False,
                             'description':UNSTATED_DESCRIPTION})
        return {'anyOf':branches}
    if 'properties' in schema:
        if name not in FIELDS:
            raise ValueError('unclassified input object: '+str(name))
        props={}
        required=[]
        for key,binding in FIELDS[name].items():
            if binding.role in {'HOST','RETAINED'}:
                continue
            public=schema['properties'][key]
            public_name=binding.input_name or key
            if binding.codec in {'SIBLING_INDEX','SOURCE_UNIT'}:
                projected={'type':['string','null'],'minLength':1,
                           'description':'Select the exact supplied readable object/source name. Host transports its position and identity.'}
            elif binding.role=='CHILDREN':
                refs={'type':'array','items':position_parts(binding.targets[0],public,skeleton=skeleton)[0],
                      'description':SKELETON_MEMBERS_DESCRIPTION if skeleton else MEMBERS_DESCRIPTION}
                projected=input_shape(public,concrete_override=refs,skeleton=skeleton) if binding.wrapped else refs
            else:
                projected=input_shape(public,partial=partial,skeleton=skeleton)
            if binding.role=='REFERENCE' and binding.codec not in {'SIBLING_INDEX','SOURCE_UNIT'}:
                projected={**projected,'description':REFERENCE_DESCRIPTION}
            elif binding.role=='LITERAL':
                projected={**projected,'description':LITERAL_DESCRIPTION}
            if name=='ModelInterpretation' and key=='text':
                projected={**projected,'description':INTERPRETATION_DESCRIPTION}
            if name=='Material':
                from tsdaf.contracts.literature.material_relation_basis import (
                    RELATION_FIELDS, MATERIAL_OTHER_QUANTITIES_MEANING, material_relation_meanings,
                )
                if key in RELATION_FIELDS and binding.codec!='SIBLING_INDEX':
                    projected={'anyOf':[projected,{'type':'null'}],
                        'description':'Explicit relation, or null to remove this relation. Omission retains it.'}
                if key in RELATION_FIELDS:
                    projected['description'] = ' '.join((
                        material_relation_meanings()[key], projected.get('description', ''),
                    )).strip()
                elif key == 'additional_reported_quantities':
                    projected['description'] = ' '.join((
                        MATERIAL_OTHER_QUANTITIES_MEANING, projected.get('description', ''),
                    )).strip()
            props[public_name]=projected
            if not partial and key in schema.get('required',()) and key not in EMPTY_SLOTS.get(name,{}):
                required.append(public_name)
        result={'type':'object','properties':props,'required':required,'additionalProperties':False}
        conditions=[]
        for rule in schema.get('allOf',()):
            if 'if' not in rule or 'then' not in rule:
                continue
            restrictions={}
            demanded=[]
            for key,restriction in rule['then'].get('properties',{}).items():
                binding=FIELDS[name][key]
                if binding.role in {'HOST','RETAINED'}:
                    continue
                alias=binding.input_name or key
                restrictions[alias]=deepcopy(restriction)
                if not partial and binding.codec=='SOURCE_UNIT' and restriction.get('type')=='string':
                    demanded.append(alias)
            if restrictions:
                condition=deepcopy(rule['if'])
                condition['required']=list(condition.get('properties',{}))
                conditions.append({'if':condition,'then':{'properties':restrictions,
                                  **({'required':demanded} if demanded else {})}})
        if conditions:
            result['allOf']=conditions
        if name=='Material':
            from tsdaf.contracts.literature.material_relation_basis import (
                MATERIAL_OBJECT_MEANING, material_input_relation_rules,
            )
            result['description'] = MATERIAL_OBJECT_MEANING
            result.setdefault('allOf', []).extend(material_input_relation_rules(
                summary_name=FIELDS[name]['summary_material_index'].input_name or 'summary_material_index',
                stage_name=FIELDS[name]['stage_id'].input_name or 'stage_id',
            ))
        if name=='ConditionNote':
            from tsdaf.lanes.literature.condition_compilation_input import condition_input_shape
            leaf = input_shape(public_definition('ConditionSequenceValue'), name='ConditionSequenceValue', skeleton=skeleton)
            result = condition_input_shape(result, leaf, partial=partial)
        if name=='ReviewMarker':
            result['oneOf']=deepcopy(schema['oneOf'])
        return result
    result={k:deepcopy(v) for k,v in schema.items() if k in {
        'type','enum','const','pattern','minLength','maxLength','minimum','maximum','minItems','maxItems','uniqueItems','description'}}
    for key in ('anyOf','oneOf'):
        if key in schema:
            result[key]=[input_shape(v,partial=partial,skeleton=skeleton) for v in schema[key]]
    if 'items' in schema:
        result['items']=input_shape(schema['items'],skeleton=skeleton)
    return result


@cache
def family_shape(family):
    name=BY_FAMILY[family].definition
    return input_shape(public_definition(name),name=name,partial=True)


# Metadata is confined to scientific containers, not to every scalar or sentence.
_CONTAINER_METADATA = {
    'citations': {'type':'array','items':{'type':'string','minLength':1},'uniqueItems':True,
        'description':'Optional exact offered source names supporting the explicitly supplied contents of this container. Omission inherits an explicitly selected parent basis; [] selects none. The source directory itself never grants SUPPORT.'},
    'existing': {'type':'string','minLength':1, 'description':
        'Exact offered baseline object or candidate. Omission creates a distinct object; omitted fields retain baseline values and sources.'},
    'label': {'type':'string','minLength':1, 'description':
        'Optional local scientific name, only needed when another part of this document refers to this object.'},
    'records': {'type':'array','items':{'type':'string','minLength':1}, 'uniqueItems':True,
        'description':'Optional association with offered scientific notes. The Host records this submission origin without requiring a selection. Nested submitted content inherits an explicit selection unless overridden; never new source SUPPORT.'},
}
ROOT_COLLECTIONS = {field: obj.definition for obj in OBJECTS
                    for parent,field in obj.containers if parent=='SynthesisDomainPayload'}
# The first compilation settles paper-level objects. The final compilation task writes
# the experimental containers it was given accounts for, plus what it could not
# represent; compounds, routes, narratives, coverage and review conflicts stay as the
# skeleton settled them and are inherited unchanged. This is the scope of that task's
# tool. The compiler itself still compiles the complete document contract.
SKELETON_ONLY_ROOTS = frozenset({'compounds','routes','paper_narratives','biological_findings',
                                 'coverage_assessments','field_conflicts'})
REPEATED_SHAPE_NOTE = 'The {name} shape: given in full at its first position in this tool and in the representation entry.'


@cache
def conflict_fields():
    """Registered target/field pairs from the existing shared review contract."""
    return registered_conflict_fields(public_definition)


def conflict_value_schema(definition, field):
    return registered_field_value_schema(public_definition,definition,field)


def conflict_shape(*, skeleton=False):
    props=public_definition('FieldConflict')['properties']
    branches=[]
    for (target,field),definition in conflict_fields().items():
        binding=FIELDS[definition][field]
        if binding.role=='CHILDREN':
            value={'type':'array','items':container_shape(binding.targets[0],skeleton=skeleton)}
        else:
            value=input_shape(conflict_value_schema(definition,field),skeleton=skeleton)
        branches.append({'type':'object','properties':{
            'target_object_type':{'const':target},'field_key':{'const':field},
            'target':{'type':'string','minLength':1},'blocking':deepcopy(props['blocking']),
            'candidate_values':{'type':'array','minItems':props['candidate_values']['minItems'],
                'items':{'type':'object','properties':{
                    'value':value, **{key:deepcopy(_CONTAINER_METADATA[key]) for key in (('citations',) if skeleton else ('records','citations'))}},
                    'required':['value'],'additionalProperties':False}},
            **{key:deepcopy(_CONTAINER_METADATA[key]) for key in (('citations',) if skeleton else ('records','citations'))},
        },'required':['target_object_type','field_key','target','blocking','candidate_values'],
            'additionalProperties':False})
    return {'oneOf':branches}


def _input_predicate(public):
    """A public position rule on plain or authority-wrapped fields, in input terms.

    Only the two forms the contract uses are translated. Anything else stops the
    build, so a rule is never silently left out of what the model is shown.
    """
    branches = []
    for branch in public['anyOf']:
        (field, rule), = branch['properties'].items()
        if set(rule) == {'const'}:
            shown = {'const': rule['const']}
        elif set(rule) == {'properties'} and set(rule['properties']) == {'state'} and set(rule['properties']['state']) == {'const'}:
            shown = {'type': 'object', 'properties': {'claim': {'const': rule['properties']['state']['const']}}, 'required': ['claim']}
        else:
            raise ValueError('position rule has no input form: ' + json.dumps(branch, sort_keys=True))
        branches.append({'properties': {field: shown}, 'required': [field]})
    return {'anyOf': branches}


def _build_position(definition, position, skeleton):
    """``container_shape`` as one public position refines it, with what that adds.

    The same object family can stand in positions with different rules: an outcome
    inside its experiment carries no assignment; an unassigned outcome at the root needs
    one, with something about it still undecided. ``position`` is the public schema of
    the collection. Returns ``(shape, forbidden keys, required keys, undecided rule)``.
    """
    shape = deepcopy(container_shape(definition, skeleton=skeleton))
    member = shape['oneOf'][0]
    items = position.get('items', {})
    forbidden = [key for part in items.get('allOf', ()) for key in part.get('not', {}).get('required', ())]
    for key in forbidden:
        del member['properties'][FIELDS[definition][key].input_name or key]
    required, undecided = [], None
    target = items.get('$ref', '').rsplit('/', 1)[-1]
    if target and target != definition:
        base, refinement = public_definition(target)['allOf']
        if base != {'$ref': '#/$defs/' + definition} or set(refinement) - {'required', 'properties'}:
            raise ValueError('position refinement has no input form: ' + target)
        required = list(refinement.get('required', ()))
        needed = {'required': required}
        member.setdefault('allOf', []).append(
            needed if skeleton else {'if': {'not': {'required': ['existing']}}, 'then': needed})
        for key, rule in refinement.get('properties', {}).items():
            inner = member['properties'][key]
            owner = public_definition(definition)['properties'][key]['$ref'].rsplit('/', 1)[-1]
            # The refined value is written whole: its own public required keys apply.
            inner['required'] = [name for name in public_definition(owner)['required']
                                 if FIELDS[owner][name].role not in {'HOST', 'RETAINED'}]
            undecided = _input_predicate(rule)
            inner.setdefault('allOf', []).append(undecided)
    return shape, tuple(forbidden), tuple(required), undecided


def position_parts(definition, position, *, skeleton=False):
    """``(shape, forbidden, required, undecided)`` for one public collection position."""
    items = position.get('items', {})
    if not items or items == {'$ref': '#/$defs/' + definition}:
        return container_shape(definition, skeleton=skeleton), (), (), None
    return _position_parts(definition, json.dumps(position, sort_keys=True), skeleton)


@cache
def _position_parts(definition, position, skeleton):
    return _build_position(definition, json.loads(position), skeleton)


def new_member_alternatives(definition):
    """The public contract's "at least one of these" for a new container, in input names.

    Read from the public definition, never restated: a new ExperimentalAccount needs its
    source parts or a model interpretation. An existing member inherits what it has.
    """
    branches = public_definition(definition).get('anyOf', ())
    if not branches or any(set(branch) != {'required'} for branch in branches):
        return []
    table = FIELDS[definition]
    if any(table[key].role in {'HOST', 'RETAINED'} for branch in branches for key in branch['required']):
        return []
    return [[table[key].input_name or key for key in branch['required']] for branch in branches]


@cache
def container_shape(definition, *, skeleton=False):
    shape=input_shape(public_definition(definition),name=definition,partial=True,skeleton=skeleton)
    alternatives = new_member_alternatives(definition)
    if alternatives:
        needed = {'anyOf': [{'required': keys} for keys in alternatives]}
        # Shown where the model writes it, so a new member is not refused only afterwards.
        shape.setdefault('allOf', []).append(
            needed if skeleton else {'if': {'not': {'required': ['existing']}}, 'then': needed})
    shape['properties'].update({key:deepcopy(value) for key,value in _CONTAINER_METADATA.items()
                                if not skeleton or key not in {'existing','records'}})
    if definition == 'Material':
        from tsdaf.contracts.literature.material_relation_basis import material_input_relation_rules
        shape.setdefault('allOf', []).extend(material_input_relation_rules(
            summary_name=FIELDS[definition]['summary_material_index'].input_name or 'summary_material_index',
            stage_name=FIELDS[definition]['stage_id'].input_name or 'stage_id', container=True,
        ))
    return {'oneOf':[shape, {'type':'object','properties':{
        'ref':{'type':'string','minLength':1,'description':SHARED_REF_DESCRIPTION}},
        'required':['ref'],'additionalProperties':False},
        *([] if skeleton else [removed_member_shape()])]}


def removed_member_shape():
    return {'type':'object','properties':{'existing':{'type':'string','minLength':1},
            'remove':{'const':True,'description':REMOVE_DESCRIPTION}},
            'required':['existing','remove'],'additionalProperties':False}


def root_position(field):
    """The public schema of one root collection of the paper's scientific payload."""
    payload = public_definition('MachinePaperResult')['properties']['domain_payload']['$ref'].rsplit('/', 1)[-1]
    return public_definition(payload)['properties'][field]


def positions():
    """Every (owner, field, definition, public position) a container can stand in."""
    for field, definition in ROOT_COLLECTIONS.items():
        yield 'document', field, definition, root_position(field)
    for owner, table in FIELDS.items():
        for field, binding in table.items():
            if binding.role == 'CHILDREN' and not binding.wrapped:
                yield owner, field, binding.targets[0], public_definition(owner)['properties'][field]


@cache
def final_document_schema(*, skeleton=False):
    unchanged={'type':'object','properties':{'existing':deepcopy(_CONTAINER_METADATA['existing'])},
               'required':['existing'],'additionalProperties':False}
    def entries(shape):
        return {'type':'array','items':shape if skeleton else {'oneOf':[shape,deepcopy(unchanged)]}}
    declarations={}
    for field, definition in ROOT_DECLARATIONS.items():
        shape=input_shape(public_definition(definition),name=definition,skeleton=skeleton)
        shape['properties'].update({key:deepcopy(_CONTAINER_METADATA[key]) for key in (('citations',) if skeleton else ('records','citations'))})
        if skeleton:
            declarations[field]={'type':'array','items':shape}
        else:
            inherited=input_shape(public_definition(definition),name=definition,partial=True)
            inherited['properties'].update({key:deepcopy(_CONTAINER_METADATA[key])
                                             for key in ('existing','records','citations')})
            inherited['required']=['existing']
            declarations[field]={'type':'array','items':{'oneOf':[shape,inherited,removed_member_shape()]}}
    return {'type':'object','properties':{
        **{field:{'type':'array','items':position_parts(definition,root_position(field),skeleton=skeleton)[0]}
           for field,definition in ROOT_COLLECTIONS.items()},
        **declarations,
        'field_conflicts':entries(conflict_shape(skeleton=skeleton)),
        'completeness':deepcopy(public_definition('MachinePaperResult')['properties']['completeness']),
        **({} if skeleton else {'records':deepcopy(_CONTAINER_METADATA['records'])}),
        'citations':deepcopy(_CONTAINER_METADATA['citations']),
    },'additionalProperties':False,
    **({'required':['completeness']} if skeleton else {}),
    'description':('The complete paper skeleton with explicit scientific completeness. No prior skeleton exists.' if skeleton else (
        'One final scientific document against the fixed supplied skeleton. A later complete submission '
        'replaces this proposal; it is not a patch on a previous proposal. Omit what is unchanged: omitted '
        'fields, collections and baseline members inherit the fixed skeleton, not a previous submission. In a '
        'collection, name only the baseline members you add to or correct, with {"existing":"offered name"}, '
        'and add new members without existing. Value arrays such as other_conditions have no member identity: '
        'omit the whole field to inherit it, or give its complete final values.'))}


# What a dispatched task does not write. A reaction-block task leaves paper completeness
# to the paper task; the paper task leaves reaction blocks to their own tasks. The scope
# check refuses the same things, so the declaration must not offer them.
TASK_EXCLUDED_ROOTS = {'WHOLE': frozenset(), 'BLOCK': frozenset({'completeness'}),
                       'PAPER': frozenset({'reaction_blocks'})}


def task_document_schema(*, skeleton=False, task='WHOLE'):
    """The document a compilation task submits: the final task's experimental part."""
    document = final_document_schema(skeleton=skeleton)
    if skeleton:
        return document
    hidden = SKELETON_ONLY_ROOTS | TASK_EXCLUDED_ROOTS[task]
    return {**document, 'properties': {key: value for key, value in document['properties'].items()
                                      if key not in hidden}}


def entry_shapes(*, skeleton=False, task='WHOLE'):
    """``(names, shapes, shared, document)``: the named shapes this boundary can reach."""
    containers = {obj.definition for obj in OBJECTS}
    shapes = {name: container_shape(name, skeleton=skeleton) if name in containers
              else input_shape(public_definition(name), name=name, skeleton=skeleton)
              for name in FIELDS}
    shapes['ReportedScalarValue'] = SCALAR_INPUT
    # A position that refines its family gets its own name: the public one when the
    # contract names it, otherwise the family name where every position agrees.
    for owner, field, definition, position in positions():
        shape, forbidden, required, _ = position_parts(definition, position, skeleton=skeleton)
        if forbidden:
            shapes[definition] = shape
        elif required:
            shapes[position['items']['$ref'].rsplit('/', 1)[-1]] = shape
    signature = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'))
    names = {signature(shape): name for name, shape in shapes.items()}
    document = task_document_schema(skeleton=skeleton, task=task)
    reachable = set()
    pending = [document]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if name := names.get(signature(value)):
                reachable.add(name)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    shapes = {name: shape for name, shape in shapes.items() if name in reachable}
    # Display names for shapes the document repeats verbatim. They are read from the
    # actual shapes, so a change to either shape changes or removes the shared name.
    shared = {'FieldConflict': conflict_shape(skeleton=skeleton)} if 'field_conflicts' in document['properties'] else {}
    if 'Operation' in shapes:
        shared['Interpretations'] = shapes['Operation']['oneOf'][0]['properties']['model_interpretations']
    for name, shape in shared.items():
        names.setdefault(signature(shape), name)
    shared = {name: shape for name, shape in shared.items() if names[signature(shape)] == name}
    return names, shapes, shared, document


def repeated_object_shapes(*, skeleton=False, task='WHOLE'):
    """Named object and list shapes of the entry: the only ones a declaration may name instead of repeat."""
    def is_object(shape):
        return shape.get('type') == 'object' or any(
            key in shape and all(is_object(branch) for branch in shape[key]) for key in ('oneOf', 'anyOf'))

    # Naming a small shape costs as much as repeating it.
    _, shapes, shared, _ = entry_shapes(skeleton=skeleton, task=task)
    return {name: shape for name, shape in (*shapes.items(), *shared.items())
            if (is_object(shape) or shape.get('type') == 'array')
            and len(json.dumps(shape, separators=(',', ':'))) > 600}


def position_notes(*, skeleton=False):
    """One sentence for each position rule, from the same parts the shapes are built from."""
    lines = []
    for owner, field, definition, position in positions():
        _, forbidden, required, undecided = position_parts(definition, position, skeleton=skeleton)
        where = field if owner == 'document' else owner + '.' + field
        if forbidden:
            line = definition + ' in ' + where + ': no ' + ', '.join(forbidden) + '; it already belongs to that ' + owner + '.'
        elif required:
            choices = []
            for branch in (undecided or {}).get('anyOf', ()):
                (key, rule), = branch['properties'].items()
                choices.append(key + ' ' + (json.dumps(rule['const']) if 'const' in rule
                                             else 'as Claim_' + rule['properties']['claim']['const']))
            line = ('A new member of ' + where + ' needs ' + ', '.join(required) + ' written in full'
                    + (', with at least one of these undecided: ' + '; '.join(choices) if choices else '') + '.')
        else:
            continue
        if line not in lines:
            lines.append(line)
    return lines


@cache
def compilation_write_entry(*, skeleton=False, task='WHOLE'):
    """A compact index of actual input shapes, shared by both compilation tasks.

    Type names organize this display only; the model sends the exact property keys.
    Shared claim syntax is derived from those same shapes, including required keys.
    """
    names, shapes, shared, document = entry_shapes(skeleton=skeleton, task=task)
    signature = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'))

    claim_templates = {}

    def describe(shape, *, expand=False, template=False):
        # Share only identical claim shapes. The parameter is display notation for
        # the actual alternatives item shape, never a new accepted input field.
        claim = shape.get('properties', {}).get('claim', {}).get('const')
        if claim is not None and not template:
            base = deepcopy(shape)
            alternatives = base['properties'].get('alternatives')
            parameter = None
            if alternatives and alternatives.get('type') == 'array':
                parameter = alternatives['items']
                alternatives['items'] = {'type': 'Value'}
            alias = 'Claim_' + str(claim)
            previous = claim_templates.get(alias)
            if previous is None or previous == base:
                claim_templates[alias] = base
                return alias + ('<' + describe(parameter) + '>' if parameter is not None else '')
            # A future branch with different required keys/rules is shown in full,
            # never silently merged into a superficially similar common template.
        name = names.get(signature(shape))
        if name and not expand:
            return name
        if 'properties' in shape:
            required = shape.get('required', ())
            return '{' + ', '.join(key + ('' if key in required else '?') + ': ' + describe(value)
                                  + (' [AuthorWording]' if value.get('description') == LITERAL_DESCRIPTION else '')
                                  + (' [ObjectName]' if value.get('description') == REFERENCE_DESCRIPTION else '')
                                  for key, value in shape['properties'].items()) + '}'
        for keyword in ('oneOf', 'anyOf'):
            if keyword in shape:
                options = list(dict.fromkeys(describe(branch) for branch in shape[keyword]))
                return options[0] if len(options) == 1 else '(' + ' | '.join(options) + ')'
        if 'const' in shape:
            return json.dumps(shape['const'])
        if 'enum' in shape:
            return '(' + ' | '.join(json.dumps(v) for v in shape['enum']) + ')'
        if shape.get('type') == 'array':
            bounds = ', '.join(key + '=' + str(shape[key]) for key in ('minItems', 'maxItems') if key in shape)
            return '[' + describe(shape['items']) + ']' + (' (' + bounds + ')' if bounds else '')
        kind = shape.get('type', 'declared value')
        if isinstance(kind, list):
            kind = ' | '.join(kind)
        return str(kind) + (' matching ' + shape['pattern'] if 'pattern' in shape else '')

    from tsdaf.lanes.literature.condition_compilation_input import DIRECT_VALUE_DESCRIPTION
    lines = ['Representation entry generated from the actual input contract. This is syntax, not an extraction checklist.',
             'ReportedScalarValue: ' + SCALAR_INPUT['description'],
             'ConditionNote.value: ' + DIRECT_VALUE_DESCRIPTION,
             'AuthorWording: ' + LITERAL_DESCRIPTION,
             'ModelInterpretation.text: ' + INTERPRETATION_DESCRIPTION,
             'ObjectName: ' + REFERENCE_DESCRIPTION,
             'Names before = identify shapes in this guide, not fields to submit. ? marks optional keys; | separates alternatives.',
             'Claim names expand to the exact object shapes below. <Value> is a display type parameter, not a submitted key; use the listed value type for alternatives. Additional constraints remain in the tool declaration.',
             'The tool declaration gives each named shape in full at its first position; a later position that takes the same shape names it instead of repeating it.',
             'Send the exact keys. Nested references use the offered names or the local name declared by the object.',
             'document: ' + document['description'],
             'Inside an operation, stages name phases; other_conditions belongs to the operation and uses stage_id for phase ownership. '
             'stage_precedence states scientific order; array placement alone does not.'
             # The first compilation reasons about the paper; the final one only transfers accounts.
             + (' Explain reasoning in model_interpretations too.' if skeleton else ''),
             'document = ' + describe(document)]
    if 'Material' in shapes:
        material = shapes['Material']['oneOf'][0]
        lines.append('Material: ' + material['description'])
        from tsdaf.contracts.literature.material_relation_basis import RELATION_FIELDS
        for field in (*RELATION_FIELDS, 'additional_reported_quantities'):
            key = FIELDS['Material'][field].input_name or field
            lines.append('Material.' + key + ': ' + material['properties'][key]['description'])
        if not skeleton:
            lines.append(QUANTITY_KIND_NOTE)
            lines.append(MATERIAL_AMOUNT_NOTE)
    for name, shape in (*shapes.items(), *shared.items()):
        lines.append(name + ' = ' + describe(shape, expand=True))
    lines.append('Collections of named shapes: ' + (SKELETON_MEMBERS_DESCRIPTION if skeleton else MEMBERS_DESCRIPTION))
    for name in shapes:
        if name in {obj.definition for obj in OBJECTS} and (alternatives := new_member_alternatives(name)):
            lines.append('A new ' + name + ' needs at least one of: ' + '; '.join(' and '.join(keys) for keys in alternatives) + '.')
    lines.extend(position_notes(skeleton=skeleton))
    lines.append('ref: ' + SHARED_REF_DESCRIPTION)
    if not skeleton:
        lines.append('remove: ' + REMOVE_DESCRIPTION)
    lines.extend(key + ': ' + value['description'] for key, value in _CONTAINER_METADATA.items()
                 if not skeleton or key not in {'existing', 'records'})
    lines.extend(name + ('<Value>' if 'alternatives' in shape['properties'] else '') + ' = ' +
                 describe(shape, expand=True, template=True) for name, shape in list(claim_templates.items()))
    lines.extend(name + ': ' + shape['description'] for name, shape in claim_templates.items()
                 if 'description' in shape)
    return '\n'.join(lines)


def document_refusal(schema, value):
    """The one failure to correct, at its path, or None when the document fits.

    Where the contract offers alternatives, the keys the model actually wrote say which
    one it used: an object with ``ref`` is a shared reference, one with ``remove`` is a
    removal, one with ``range`` is a range. Only when exactly one alternative has all of
    its required keys present is that alternative's own failure reported; otherwise the
    alternatives are listed as before. Nothing about the science is chosen.
    """
    error = best_match(Draft202012Validator(schema).iter_errors(value))
    if error is None:
        return None
    while error.validator in ('oneOf', 'anyOf') and error.context and isinstance(error.instance, dict):
        written = [index for index, branch in enumerate(error.validator_value)
                   if branch.get('type') == 'object'
                   and all(key in error.instance for key in branch.get('required', ()))]
        # The plain object alternative requires nothing, so a more specific one wins.
        specific = [index for index in written if error.validator_value[index].get('required')]
        chosen = specific if len(specific) == 1 else written
        if len(chosen) != 1:
            break
        failures = [child for child in error.context if next(iter(child.schema_path), None) == chosen[0]]
        if not failures:
            break
        error = best_match(failures)
    said = _error_refusal(error)
    if (error.validator == 'type' and error.validator_value == 'string'
            and isinstance(error.instance, (int, float)) and not isinstance(error.instance, bool)):
        # A JSON number cannot keep the reported digits (1.10 becomes 1.1).
        said += '; write the number as a string with its reported digits, for example "1.10"'
    return said


class FinalScienceDocument(RootModel[dict[str,Any]]):
    """The model's final scientific document; technical storage fields are Host-owned."""

    skeleton: ClassVar[bool] = False
    task: ClassVar[bool] = False
    scope: ClassVar[str] = 'WHOLE'

    @classmethod
    def _schema(cls):
        if cls.task:
            return task_document_schema(skeleton=cls.skeleton, task=cls.scope)
        return final_document_schema(skeleton=cls.skeleton)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        return deepcopy(cls._schema())

    @model_validator(mode='after')
    def _shape(self):
        refusal = document_refusal(self._schema(), self.root)
        if refusal:
            raise ValueError(refusal)
        return self


class FinalTaskDocument(FinalScienceDocument):
    """What the final compilation task submits: the experimental part of the document."""
    task: ClassVar[bool] = True


class BlockTaskDocument(FinalTaskDocument):
    """One reaction block's task: paper completeness is not its to write."""
    scope: ClassVar[str] = 'BLOCK'


class PaperTaskDocument(FinalTaskDocument):
    """The paper task after the block tasks: reaction blocks are not its to write."""
    scope: ClassVar[str] = 'PAPER'


class SkeletonScienceDocument(FinalScienceDocument):
    """The same scientific representation, with no fictional prior objects/accounts."""
    skeleton: ClassVar[bool] = True


def explicit_scalar(value):
    from tsdaf.lanes.literature.reported_scalars import _converted, stated_unit_token
    Draft202012Validator(SCALAR_INPUT).validate(value)
    number=value.get('value')
    limits=value.get('range')
    number=number.strip() if number is not None else None
    limits=[limit.strip() for limit in limits] if limits else limits
    if limits and Decimal(limits[0])>Decimal(limits[1]):
        raise ValueError('range lower bound exceeds upper bound')
    unit=value.get('unit')
    raw=value.get('text')
    if raw is None:
        raw=(number if number is not None else ' to '.join(limits))+(' '+unit.strip() if unit and unit.strip() else '')
    result={key:None for key in ATOMIC_ADAPTERS['ReportedScalarValue']}
    result.update(raw_text=raw,numeric_value_decimal=number,unit=unit,
                  range_min_decimal=limits[0] if limits else None,range_max_decimal=limits[1] if limits else None)
    for token,field in [(number,'normalized_value_decimal'),
                       (limits[0] if limits else None,'normalized_range_min_decimal'),
                       (limits[1] if limits else None,'normalized_range_max_decimal')]:
        # The stated unit is matched as written. An unknown unit keeps its number and
        # spelling and derives no normalized value in some other dimension.
        key=stated_unit_token(unit) if unit else None
        if token is not None and key is not None:
            converted=_converted(Decimal(token),key)
            if converted is not None:
                result['normalized_unit'],normalized=converted
                result[field]=format(normalized,'f')
    return result
