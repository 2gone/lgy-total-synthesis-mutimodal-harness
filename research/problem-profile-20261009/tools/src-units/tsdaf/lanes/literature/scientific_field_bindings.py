"""Mechanical input/public bindings shared by both scientific compilations.

Scientific types and constraints belong to fact_schema_v1_7. This table says where
an object lives and which fields transport identities, literal text or provenance.
No rule is inferred from a field-name suffix or a value's key presence.
"""
from dataclasses import dataclass
from functools import cache
from typing import Literal

from tsdaf.contracts.literature.schema import MachineProfile, fact_definition
from tsdaf.contracts.literature.objects import PUBLIC_TARGET_DEFINITIONS

Role = Literal['VALUE', 'LITERAL', 'REFERENCE', 'CHILDREN', 'HOST', 'RETAINED']


@dataclass(frozen=True)
class FieldBinding:
    role: Role
    targets: tuple[str, ...] = ()
    # Only CHILDREN fields specify their public wrapper representation.
    wrapped: bool = False
    codec: Literal['DIRECT', 'SIBLING_INDEX', 'SOURCE_UNIT'] = 'DIRECT'
    input_name: str | None = None


@dataclass(frozen=True)
class ObjectBinding:
    family: str
    definition: str
    identity: str
    # Actual public containers, not a search for matching labels or nearby ids.
    containers: tuple[tuple[str, str], ...]


OBJECTS = (
    ObjectBinding('COMPOUND','CompoundRecord','compound_id',(('SynthesisDomainPayload','compounds'),)),
    ObjectBinding('REACTION','ReactionBlock','reaction_block_id',(('SynthesisDomainPayload','reaction_blocks'),)),
    ObjectBinding('EXPERIMENT','ExperimentalAccount','account_id',(('SynthesisDomainPayload','experimental_accounts'),)),
    ObjectBinding('ROUTE','RouteRecord','route_id',(('SynthesisDomainPayload','routes'),)),
    ObjectBinding('NARRATIVE','PaperNarrative','narrative_id',(('SynthesisDomainPayload','paper_narratives'),)),
    ObjectBinding('CHARACTERIZATION','CharacterizationRecord','characterization_id',(('SynthesisDomainPayload','characterization_records'),)),
    ObjectBinding('BIOLOGICAL','BiologicalFinding','biological_finding_id',(('SynthesisDomainPayload','biological_findings'),)),
    ObjectBinding('OPERATION','Operation','operation_id',(('ReactionBlock','operations'),('ExperimentalAccount','operations'))),
    ObjectBinding('OUTCOME','ReportedOutcome','outcome_id',(('ReactionBlock','reported_outcomes'),('ExperimentalAccount','reported_outcomes'),('SynthesisDomainPayload','unassigned_outcomes'))),
    ObjectBinding('STAGE','OperationStage','stage_id',(('Operation','stages'),)),
    ObjectBinding('INTERMEDIATE','LocalIntermediate','local_intermediate_id',(('ReactionBlock','local_intermediates'),('ExperimentalAccount','local_intermediates'))),
    ObjectBinding('MATERIAL','Material','material_use_id',(('Operation','materials'),)),
    ObjectBinding('QUANTITY','ReportedQuantity','quantity_id',(('ReportedOutcome','quantities'),('Material','additional_reported_quantities'))),
    ObjectBinding('TARGET_PATH','TargetPath','target_path_id',(('RouteRecord','target_paths'),)),
    ObjectBinding('TARGET_ASSERTION','TargetAssertion','assertion_id',(('RouteRecord','target_assertions'),)),
)
BY_FAMILY = {o.family:o for o in OBJECTS}
BY_DEFINITION = {o.definition:o for o in OBJECTS}


def fields(*, value='', literal='', host='', retained='', refs=None, children=None):
    result = {}
    for role, names in [('VALUE',value),('LITERAL',literal),('HOST',host),('RETAINED',retained)]:
        for name in names.split():
            if name in result:
                raise ValueError('duplicate field responsibility: '+name)
            result[name] = FieldBinding(role)
    for role, groups in [('REFERENCE',refs or {}),('CHILDREN',children or {})]:
        for name, target in groups.items():
            if name in result:
                raise ValueError('duplicate field responsibility: '+name)
            result[name] = FieldBinding(role, tuple(target) if role=='REFERENCE' else (target[0],),
                                        wrapped=target[1] if role=='CHILDREN' else False)
    return result


# Each definition lists ALL its fields: extending the public contract without a
# corresponding responsibility is a development-time error, not a model's puzzle.
FIELDS = {
 'CompoundRecord': fields(value='author_labels names structure model_interpretations', literal='source_statements',
     host='compound_id evidence_refs stable_key key_basis model_record_refs',
     refs={'predecessor_ids':('CompoundRecord',),'successor_ids':('CompoundRecord',)}),
 'ReactionBlock': fields(value='summary model_interpretations model_summary operation_precedence',
     host='reaction_block_id source_order evidence_refs stable_key key_basis model_record_refs',
     refs={'input_compound_ids':('CompoundRecord',),'output_compound_ids':('CompoundRecord',),
           'optimization_context_ids':('PaperNarrative',),'failed_attempt_context_ids':('PaperNarrative',),
           'mechanism_context_ids':('PaperNarrative',)},
     children={'operations':('Operation',False),'reported_outcomes':('ReportedOutcome',False),
               'local_intermediates':('LocalIntermediate',False)}),
 'ExperimentalAccount': fields(value='title model_interpretations preparation_uses operation_precedence',
     literal='source_parts', host='account_id stable_key evidence_refs model_record_refs',
     refs={'input_compound_ids':('CompoundRecord',),'output_compound_ids':('CompoundRecord',)},
     children={'operations':('Operation',False),'reported_outcomes':('ReportedOutcome',False),
               'local_intermediates':('LocalIntermediate',False)}),
 'Operation': fields(value='temperature_profile duration pressure concentration scale atmosphere key_addition key_workup reaction_class other_conditions description stage_coverage stage_precedence model_interpretations model_description procedure_sources unassigned_facts',
     host='operation_id sequence_index evidence_refs stable_key key_basis model_record_refs',
     children={'materials':('Material',True),'stages':('OperationStage',False)}),
 'ReportedOutcome': fields(value='model_interpretations assignment', literal='source_statements',
     host='outcome_id value_provenance evidence_refs stable_key key_basis model_record_refs',
     refs={'operation_ids':('Operation',),'product_compound_ids':('CompoundRecord',)},
     children={'quantities':('ReportedQuantity',False)}),
 'ReportedQuantity': fields(value='kind reported_value basis',host='quantity_id'),
 'Material': fields(value='role amount equivalents names model_interpretations reporting_level extent',
     literal='verbatim_name role_verbatim source_readings',
     host='sequence_index evidence_refs relation_evidence_refs material_use_id model_record_refs relation_record_refs',
     refs={'compound_id':('CompoundRecord',),'stage_id':('OperationStage',),'summary_material_index':('Material',)},
     children={'additional_reported_quantities':('ReportedQuantity',False)}),
 'OperationStage': fields(value='description model_interpretations',literal='source_parts',
     host='stage_id stable_key display_index verification boundary boundary_evidence_refs evidence_refs model_record_refs'),
 'LocalIntermediate': fields(value='author_label structure name produced_by consumed_by model_interpretations',
     literal='source_parts',host='local_intermediate_id evidence_refs model_record_refs'),
 'CharacterizationRecord': fields(value='method reported_value reported_formula reported_ion comparator sample_context accession_ids model_interpretations',
     literal='method_verbatim source_statements',host='characterization_id evidence_refs stable_key key_basis model_record_refs',
     refs={'compound_ids':('CompoundRecord',)}),
 'RouteRecord': fields(value='model_overview model_integration_note',host='route_id evidence_refs model_record_refs',
     refs={'compound_ids':('CompoundRecord',),'reaction_block_ids':('ReactionBlock',)},
     children={'target_paths':('TargetPath',False),'target_assertions':('TargetAssertion',False)}),
 'TargetPath': fields(value='reported_step_count reported_overall_yield reported_step_count_basis',
     host='target_path_id evidence_refs stable_key key_basis model_record_refs',
     refs={'target_compound_id':('CompoundRecord',),'reaction_block_ids':('ReactionBlock',)}),
 'TargetAssertion': fields(value='subject outcome',literal='author_statement',host='assertion_id evidence_refs stable_key key_basis model_record_refs',
     refs={'target_compound_id':('CompoundRecord',)}),
 'PaperNarrative': fields(value='kind title explanation subjects',literal='author_text source_parts kind_verbatim',
     host='narrative_id evidence_refs stable_key key_basis model_record_refs'),
 'NarrativeSubject': fields(value='object_type',refs={'object_id':('CompoundRecord','ReactionBlock','Operation','ReportedOutcome','RouteRecord','ExperimentalAccount')}),
 'ModelInterpretation': fields(value='text relation',host='evidence_refs model_record_refs',retained='primary'),
 'SourcePart': fields(literal='text',host='evidence_refs model_record_refs'),
 'SourceStatement': fields(value='model_interpretations',literal='statement source_parts',host='evidence_refs model_record_refs'),
 'CharacterizationSourceStatement': fields(value='sample_context',literal='statement source_parts',host='evidence_refs model_record_refs'),
 'OutcomeSourceStatement': fields(value='kind product_label model_interpretations',literal='statement source_parts kind_verbatim',
     host='evidence_refs model_record_refs',refs={'product_ids':('CompoundRecord',)}),
 'MaterialSourceReading': fields(value='role amount identity_relation model_interpretations reported_quantities',
     literal='verbatim_name source_parts role_verbatim',host='evidence_refs model_record_refs',refs={'compound_id':('CompoundRecord',)}),
 'ReportedQuantitySource': fields(value='quantity',literal='source_parts'),
 'ConditionNote': fields(value='category about extent sequence shared_unit_text value_relation adoption model_interpretations',
     literal='category_verbatim text source_parts',host='evidence_refs model_record_refs',refs={'stage_id':('OperationStage',)}),
 'ConditionSequenceMember': fields(value='values model_interpretations',literal='text source_parts',host='model_record_refs'),
 'ConditionSequenceValue': fields(value='category typed_value',literal='category_word text',host='model_record_refs'),
 'KnownOperationRelation': fields(value='completeness',refs={'known_operation_ids':('Operation',)}),
 'PreparationUse': fields(value='model_interpretations',host='evidence_refs model_record_refs',
     refs={'target_operation_id':('Operation',),'target_material_use_id':('Material',)}),
 'OutcomeAssignment': fields(value='experiment measurement_identity explanation',host='evidence_refs model_record_refs'),
 'ExperimentReference': fields(value='object_type',refs={'object_id':('ReactionBlock','ExperimentalAccount')}),
 'OperationPrecedence': fields(host='evidence_refs model_record_refs',refs={'before_operation_id':('Operation',),'after_operation_id':('Operation',)}),
 'StagePrecedence': fields(value='model_interpretations',host='evidence_refs model_record_refs',
     refs={'before_stage_id':('OperationStage',),'after_stage_id':('OperationStage',)}),
 'OperationStageCoverageValue': fields(value='status gaps'),
 'ProcedureSourceReading': fields(value='kind extent adoption relation model_interpretations',literal='source_parts',
     host='evidence_refs model_record_refs target_source_document_id target_source_revision_id',retained='ruling resolution_note',
     refs={'operation_id':('Operation',),'stage_id':('OperationStage',),'unit_block_id':('SourceUnit',)}),
 'UnassignedFact': fields(value='category about model_interpretations',literal='category_verbatim text',host='source evidence_refs model_record_refs'),
 'StageCoverageGap': fields(value='kind explanation',host='evidence_refs model_record_refs',refs={'stage_ids':('OperationStage',)}),
 'BiologicalFinding': fields(value='endpoint reported_value organism_or_system comparator',
     host='biological_finding_id evidence_refs model_record_refs', refs={'compound_ids':('CompoundRecord',)}),
 'MachineUnresolvedItem': fields(value='scope reason_code statement subjects',
     host='unresolved_item_id stable_key key_basis creation_key evidence_refs model_submission_refs',
     refs={'subject_id':('CompoundRecord','ReactionBlock','RouteRecord','Paper')}),
 'CoverageAssessment': fields(value='status source_phase explanation', host='evidence_refs model_submission_refs',
     refs={'subject_ids':('CompoundRecord','ReactionBlock','Operation','ReportedOutcome',
        'TargetPath','TargetAssertion','PaperNarrative','CharacterizationRecord','BiologicalFinding',
        'ExperimentalAccount','RouteRecord','Paper')}),
 'ReviewMarker': fields(value='target_object_type field_key reason',
     host='evidence_refs model_record_refs',
     refs={'target_object_id':tuple(PUBLIC_TARGET_DEFINITIONS.values()),'material_index':('Material',)}),
}
FIELDS['ReviewMarker']['target_object_id']=FieldBinding('REFERENCE',tuple(PUBLIC_TARGET_DEFINITIONS.values()),input_name='target')
FIELDS['ReviewMarker']['material_index']=FieldBinding('REFERENCE',('Material',),codec='SIBLING_INDEX',input_name='material')
FIELDS['MachineUnresolvedItem']['subject_id'] = FieldBinding('REFERENCE',
    ('CompoundRecord','ReactionBlock','RouteRecord','Paper'),input_name='subject')
FIELDS['CoverageAssessment']['subject_ids'] = FieldBinding('REFERENCE',
    FIELDS['CoverageAssessment']['subject_ids'].targets,input_name='subjects')
FIELDS['Material']['summary_material_index'] = FieldBinding('REFERENCE',('Material',),codec='SIBLING_INDEX',input_name='summary_material')
FIELDS['ProcedureSourceReading']['unit_block_id'] = FieldBinding('REFERENCE',('SourceUnit',),codec='SOURCE_UNIT',input_name='source')
for _bindings in FIELDS.values():
    if 'model_record_refs' in _bindings:
        _bindings['model_submission_refs'] = FieldBinding('HOST')
FIELDS['Material']['relation_submission_refs'] = FieldBinding('HOST')
FIELDS['Material']['relation_field_bases'] = FieldBinding('HOST')

# Empty representation slots mean "no submitted claim", not NOT_FOUND. These
# defaults are explicit per carrier and are used by both the declaration and
# compiler; required substantive relationships/text are never defaulted.
EMPTY_SLOTS = {
    'CompoundRecord': {'author_labels':[], 'names':[], 'predecessor_ids':[], 'successor_ids':[]},
    'ReactionBlock': {'optimization_context_ids':[], 'failed_attempt_context_ids':[], 'mechanism_context_ids':[]},
    'Operation': {'other_conditions':[]},
    'Material': dict.fromkeys(('role','compound_id','verbatim_name','amount','equivalents')),
    'LocalIntermediate': {'author_label':None, 'produced_by':{'known_operation_ids':[]},
                          'consumed_by':{'known_operation_ids':[]}},
    'CharacterizationRecord': dict.fromkeys(('method_verbatim','method','reported_value',
        'reported_formula','reported_ion','comparator','sample_context')),
    'PaperNarrative': {'kind':None,'author_text':None},
    'CharacterizationSourceStatement': {'sample_context':None},
    'MaterialSourceReading': dict.fromkeys(('role','verbatim_name','amount','compound_id')),
    'ConditionNote': {'category':None,'about':None},
    'KnownOperationRelation': {'known_operation_ids':[]},
    'PreparationUse': {'target_material_use_id':None},
    'ProcedureSourceReading': {'operation_id':None,'unit_block_id':None},
    'UnassignedFact': {'category':None,'about':None},
    'OperationStageCoverageValue': {'gaps':[]},
    'StageCoverageGap': {'stage_ids':[]},
    'StagePrecedence': {'model_interpretations':[]},
    'BiologicalFinding': dict.fromkeys(('reported_value','organism_or_system','comparator')),
}

# Discriminator values already belong to ExperimentReference's public enum.
DISCRIMINATORS = {('ExperimentReference','object_id'):
    ('object_type', {'REACTION_BLOCK':'ReactionBlock','EXPERIMENTAL_ACCOUNT':'ExperimentalAccount'}),
    ('NarrativeSubject','object_id'): ('object_type', {
        'COMPOUND':'CompoundRecord','REACTION_BLOCK':'ReactionBlock','OPERATION':'Operation',
        'REPORTED_OUTCOME':'ReportedOutcome','ROUTE':'RouteRecord','EXPERIMENTAL_ACCOUNT':'ExperimentalAccount'}),
    ('MachineUnresolvedItem','subject_id'): ('scope', {
        'COMPOUND':'CompoundRecord','REACTION_BLOCK':'ReactionBlock','ROUTE':'RouteRecord','PAPER':'Paper'}),
    ('ReviewMarker','target_object_id'): ('target_object_type',PUBLIC_TARGET_DEFINITIONS)}

# Ordinary root declarations have no model-managed item lifecycle. Their arrays
# are explicit scientific statements in the same final document.
ROOT_DECLARATIONS = {'unresolved_items':'MachineUnresolvedItem',
                     'coverage_assessments':'CoverageAssessment'}
DECLARATION_COLLECTIONS = {
    **{field:(definition,(field,)) for field,definition in ROOT_DECLARATIONS.items()},
    'field_conflicts':('FieldConflict',('domain_payload','field_conflicts')),
    'review_markers':('ReviewMarker',('domain_payload','review_markers')),
}


def iter_declarations(machine):
    for field,(definition,path) in DECLARATION_COLLECTIONS.items():
        parent=machine
        for part in path[:-1]:
            parent=parent[part]
        for index,value in enumerate(parent.get(path[-1],())):
            yield field,definition,(*path,index),value



@cache
def public_definition(name):
    return fact_definition(name, MachineProfile.V1_7)


def check_bindings():
    """No scientific field can silently inherit a generic storage interpretation."""
    for name, table in FIELDS.items():
        actual=set(public_definition(name)['properties'])
        if actual!=table.keys():
            raise ValueError(f'{name}: missing={sorted(actual-table.keys())}; obsolete={sorted(table.keys()-actual)}')
    for obj in OBJECTS:
        if FIELDS[obj.definition][obj.identity].role!='HOST':
            raise ValueError('object identity must be Host-owned: '+obj.definition)
        for parent, field in obj.containers:
            if parent=='SynthesisDomainPayload':
                # Machine's schema names its domain in this one public slot.
                domain=public_definition('MachinePaperResult')['properties']['domain_payload']
                if '$ref' in domain:
                    domain=public_definition(domain['$ref'].split('/')[-1])
                if field not in domain['properties']:
                    raise ValueError('missing paper collection: '+field)
            elif FIELDS[parent][field].targets!=(obj.definition,):
                raise ValueError('child/container responsibilities disagree')
    return True


@dataclass(frozen=True)
class BoundObject:
    binding: ObjectBinding
    path: tuple[str | int, ...]
    value: dict
    parent_path: tuple[str | int, ...] | None
    field: str
    # None is a plain array, 'value' is the adopted authority value; candidate_id
    # means a retained alternative. It is never silently promoted to 'value'.
    branch: str | None = None

    @property
    def identity(self):
        return self.value.get(self.binding.identity)


def iter_bound_objects(machine):
    """Walk ONLY declared public containers, including explicit alternatives.

    The locator is Host-owned. Existing legacy children without their optional ID
    remain addressable by an exact snapshot location; no identity is guessed here.
    """
    def visit(parent_name, parent, parent_path):
        for binding in OBJECTS:
            for container, field in binding.containers:
                if container!=parent_name or field not in parent:
                    continue
                wrapped = parent_name!='SynthesisDomainPayload' and FIELDS[parent_name][field].wrapped
                value=parent[field]
                array_path=(*parent_path,field)
                arrays=[]
                if wrapped:
                    if isinstance(value.get('value'),list):
                        arrays.append(((*array_path,'value'),value['value'],'value'))
                    for i,candidate in enumerate(value.get('candidates',())):
                        if isinstance(candidate.get('value'),list):
                            arrays.append(((*array_path,'candidates',i,'value'),candidate['value'],candidate['candidate_id']))
                elif isinstance(value,list):
                    arrays.append((array_path,value,None))
                for path,rows,branch in arrays:
                    for i,row in enumerate(rows):
                        located=BoundObject(binding,(*path,i),row,
                            None if parent_name=='SynthesisDomainPayload' else parent_path,field,branch)
                        yield located
                        yield from visit(binding.definition,row,located.path)
    yield from visit('SynthesisDomainPayload',machine['domain_payload'],('domain_payload',))
    from tsdaf.contracts.literature.objects import registered_conflict_fields
    pairs=registered_conflict_fields(public_definition)
    for index,conflict in enumerate(machine['domain_payload'].get('field_conflicts',())):
        definition=pairs.get((conflict['target_object_type'],conflict['field_key']))
        if definition is None:
            continue
        rule=FIELDS[definition][conflict['field_key']]
        if rule.role!='CHILDREN':
            continue
        binding=BY_DEFINITION[rule.targets[0]]
        owner=('domain_payload','field_conflicts',index)
        for ordinal,candidate in enumerate(conflict['candidate_values']):
            for member,row in enumerate(candidate['value']):
                path=(*owner,'candidate_values',ordinal,'value',member)
                yield BoundObject(binding,path,row,owner,conflict['field_key'],candidate['candidate_id'])
                yield from visit(binding.definition,row,path)


def at_path(value,path):
    for part in path:
        value=value[part]
    return value


ATOMIC_ADAPTERS = {
    'ReportedScalarValue': frozenset({'raw_text','numeric_value_decimal','range_min_decimal',
        'range_max_decimal','unit','normalized_value_decimal','normalized_range_min_decimal',
        'normalized_range_max_decimal','normalized_unit'}),
    'StructureValue': frozenset({'input_format','raw_value','canonical_smiles','isomeric_smiles',
        'inchi','inchi_key','normalizer_name','normalizer_version'}),
}


def authority_schema(schema):
    from tsdaf.contracts.literature.resolved_rules import AUTHORITY_KEYS
    return set(AUTHORITY_KEYS) <= schema.get('properties',{}).keys()


def check_type_closure():
    """Audit every model-writable reachable definition, including inline authority."""
    visited=set()
    def visit(schema,name=None):
        if '$ref' in schema:
            target=schema['$ref'].rsplit('/',1)[-1]
            if target in visited:
                return
            visited.add(target)
            visit(public_definition(target),target)
            return
        if name in ATOMIC_ADAPTERS:
            if set(schema['properties'])!=ATOMIC_ADAPTERS[name]:
                raise ValueError('atomic adapter contract drift: '+name)
            return
        if authority_schema(schema):
            visit(schema['properties']['value'])
            return
        if 'properties' in schema:
            if name not in FIELDS:
                # An allOf patch restricts an already visited authority/definition;
                # it is not a new object shape to construct independently.
                raise ValueError('unclassified writable object definition: '+str(name))
            for field,binding in FIELDS[name].items():
                if binding.role in {'HOST','RETAINED','CHILDREN'}:
                    continue
                visit(schema['properties'][field])
            return
        if 'items' in schema:
            visit(schema['items'])
        if 'allOf' in schema:
            # First component carries the actual type; remaining components only
            # constrain it, and the input adapter preserves those state limits.
            visit(schema['allOf'][0])
        for key in ('anyOf','oneOf'):
            for branch in schema.get(key,()):
                visit(branch)
    check_bindings()
    for obj in OBJECTS:
        visit({'$ref':'#/$defs/'+obj.definition})
    return frozenset(visited)
