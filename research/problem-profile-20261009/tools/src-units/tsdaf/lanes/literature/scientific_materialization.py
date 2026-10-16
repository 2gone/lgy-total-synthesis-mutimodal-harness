"""Compile explicit scientific values without reading meaning from their prose.

Both compilation boundaries supply exact object/source bindings and actual accepted
births. This module has no search, scientific parser, or ownership heuristic.
"""
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable

from jsonschema import Draft202012Validator

from tsdaf.contracts.literature.expressions import unstated
from tsdaf.contracts.literature.schema import MachineProfile, validate_fact_definition, _refusal
from tsdaf.lanes.literature.scientific_field_bindings import (
    ATOMIC_ADAPTERS, DISCRIMINATORS, EMPTY_SLOTS, FIELDS, authority_schema, public_definition,
)
from tsdaf.lanes.literature.structured_scientific_values import (
    explicit_scalar, input_shape, resolved_schema,
)


@dataclass(frozen=True)
class ValueBindings:
    # Callbacks resolve supplied names against a frozen naming table. Neither
    # similarity matching nor selecting the first matching occurrence is allowed.
    reference: Callable[[str, tuple[str, ...]], str]
    child: Callable[[dict, str, tuple], dict]
    source: Callable[[str], dict]
    sibling: Callable[[str], int]
    candidate: Callable[[tuple, int], str]
    # Host-only representation fields for this exact object occurrence.
    host_fields: Callable[[str, tuple], dict]
    relation_equivalent: Callable[[str, object, object], bool] = lambda key, before, after: before == after


def exact_union(before, after):
    result=deepcopy(list(before))
    for value in after:
        if value not in result:
            result.append(deepcopy(value))
    return result


class LocatedError(ValueError):
    """A refusal that already says which submitted object and field it concerns."""

    def __init__(self, reason, *, fields=(), where=None):
        self.reason, self.fields, self.where = reason, tuple(fields), where
        place = '/'.join(self.fields)
        super().__init__(': '.join(part for part in (where, place, reason) if part))


class ScientificValueCompiler:
    def __init__(self, bindings: ValueBindings, *, record_refs=(), evidence_refs=(), submission_refs=()):
        self.bindings=bindings
        self.records=deepcopy(list(record_refs))
        # Only explicitly bound sources for THIS submission, never all sources in
        # its recording task. Record provenance is not original source SUPPORT.
        self.evidence=deepcopy(list(evidence_refs))
        self.submissions=deepcopy(list(submission_refs))

    def object(self, definition, fields, *, before=None, path=()):
        schema=public_definition(definition)
        refusal = _refusal(Draft202012Validator(input_shape(schema,name=definition,partial=True)), fields)
        if refusal:
            raise ValueError(f'{definition}: {refusal}')
        value=self._object(definition,schema,fields,before,path)
        validate_fact_definition(definition,value,MachineProfile.V1_7)
        return value

    def field_value(self, definition, field, value, *, path=()):
        """An explicit conflict alternative has the exact target field's value type."""
        from tsdaf.lanes.literature.structured_scientific_values import conflict_value_schema
        return self._value(conflict_value_schema(definition,field),value,path=path,
                           binding=FIELDS[definition][field])

    def _value(self, schema, value, *, name=None, before=None, path=(), binding=None, owner=None):
        expanded,named=resolved_schema(schema)
        if expanded is not schema:
            return self._value(expanded,value,name=named or name,before=before,
                               path=path,binding=binding,owner=owner)
        if authority_schema(schema):
            return self._authority(schema,value,before,path,binding,owner)
        if binding is not None and binding.role=='CHILDREN':
            return [deepcopy(self.bindings.child(node,binding.targets[0],(*path,index)))
                    for index,node in enumerate(value)]
        if binding is not None and binding.role=='REFERENCE':
            if value is None:
                return None
            targets=binding.targets
            if owner is not None:
                definition,key,parent=owner
                discriminator=DISCRIMINATORS.get((definition,key))
                if discriminator:
                    field,types=discriminator
                    targets=(types[parent[field]],)
            if binding.codec=='SIBLING_INDEX':
                return self.bindings.sibling(value)
            if isinstance(value,list):
                return [self.bindings.reference(v,targets) for v in value]
            return self.bindings.reference(value,targets)
        if name=='ReportedScalarValue':
            return explicit_scalar(value)
        if name=='StructureValue':
            # No chemistry normalization is guessed or inherited from an older
            # structure. Optional derived representations are explicitly absent.
            return {key:deepcopy(value.get(key)) for key in ATOMIC_ADAPTERS[name]}
        if 'properties' in schema:
            return self._object(name,schema,value,before,path)
        for choice_key in ('anyOf','oneOf'):
            if choice_key in schema:
                choices=[choice for choice in schema[choice_key]
                         if Draft202012Validator(input_shape(choice,partial=before is not None)).is_valid(value)]
                if not choices or choice_key=='oneOf' and len(choices)!=1:
                    raise ValueError('scientific type is ambiguous or invalid at '+str(path))
                outputs=[self._value(choice,value,before=before,path=path,binding=binding,owner=owner)
                         for choice in choices]
                if any(output!=outputs[0] for output in outputs[1:]):
                    raise ValueError('overlapping types have different representations at '+str(path))
                return outputs[0]
        if schema.get('type')=='array':
            # A submitted list replaces its members. Array position must never
            # attach a previous member's provenance to a different statement.
            return [self._value(schema['items'],v,path=(*path,i)) for i,v in enumerate(value)]
        return deepcopy(value)

    def _authority(self, schema, value, before, path, binding, owner):
        result=unstated()
        claim=value.get('claim') if isinstance(value,dict) else None
        if claim=='UNSTATED':
            return result
        result['model_record_refs']=deepcopy(self.records)
        if self.submissions:
            result['model_submission_refs']=deepcopy(self.submissions)
        result['evidence_uses']=[{'evidence_ref':deepcopy(ref),'role':'SUPPORT'} for ref in self.evidence]
        if claim in ('NOT_FOUND','UNRESOLVED'):
            result['state']=claim
            if claim=='UNRESOLVED':
                result['reason_code']=value['reason']
                for index,alternative in enumerate(value.get('alternatives',())):
                    result['candidates'].append({
                        'candidate_id':self.bindings.candidate(path,index),
                        'value':self._value(schema['properties']['value'],alternative,
                            path=(*path,'alternatives',index),binding=binding,owner=owner),
                        'evidence_uses':deepcopy(result['evidence_uses']),
                        'model_record_refs':deepcopy(self.records),
                        **({'model_submission_refs':deepcopy(self.submissions)} if self.submissions else {})})
        else:
            result['state']='PRESENT'
            # A changed authority has exactly this submission's provenance. No
            # old SUPPORT is silently attached to its replacement value.
            result['value']=self._value(schema['properties']['value'],value,
                before=before.get('value') if isinstance(before,dict) else None,
                path=(*path,'value'),binding=binding,owner=owner)
        return result

    def _object(self, name, schema, fields, before, path):
        if name=='ConditionNote':
            from tsdaf.lanes.literature.condition_compilation_input import expand_condition_value
            fields = expand_condition_value(fields, before)
        if name not in FIELDS:
            raise ValueError('object has no explicit responsibility: '+str(name))
        result=deepcopy(before) if before is not None else {}
        if before is not None and not fields:
            return result
        responsibilities=FIELDS[name]
        effective={**deepcopy(EMPTY_SLOTS.get(name,{})),**result}
        # Only discriminator values (not source IDs or inferred source meaning)
        # select a declared codec branch. Use inherited values for sparse edits.
        for key,binding in responsibilities.items():
            alias=binding.input_name or key
            if alias in fields:
                effective[key]=fields[alias]
        for key,binding in responsibilities.items():
            if binding.role in ('HOST','RETAINED'):
                continue
            alias=binding.input_name or key
            source_changed=(binding.codec=='SOURCE_UNIT' and before is not None
                            and 'kind' in fields and fields['kind']!=before.get('kind'))
            if alias not in fields and not source_changed:
                continue
            value=fields.get(alias)
            if name=='Material':
                from tsdaf.contracts.literature.material_relation_basis import RELATION_FIELDS
                if key in RELATION_FIELDS and value is None:
                    result.pop(key,None)
                    continue
            if binding.codec=='SIBLING_INDEX' and value is None:
                # This optional relationship has no public null representation.
                result.pop(key,None)
                continue
            if binding.codec=='SOURCE_UNIT':
                source=self.bindings.source(value) if value is not None else {}
                result[key]=source.get('unit_block_id')
                # These are derived location fields, not retained scientific claims.
                for technical in ('target_source_document_id','target_source_revision_id'):
                    result.pop(technical,None)
                for condition in schema.get('allOf',()):
                    if Draft202012Validator(condition.get('if',False)).is_valid(effective):
                        for technical in condition.get('then',{}).get('required',()):
                            if technical in ('target_source_document_id','target_source_revision_id'):
                                if technical not in source:
                                    raise ValueError('explicit source required at '+str((*path,alias)))
                                result[technical]=source[technical]
                continue
            try:
                result[key]=self._value(schema['properties'][key],value,
                    before=result.get(key),path=(*path,key),binding=binding,owner=(name,key,effective))
            except LocatedError as error:
                if error.where is not None:
                    raise  # a nested container already named itself
                raise LocatedError(error.reason, fields=(alias, *error.fields)) from error
            except ValueError as error:
                raise LocatedError(str(error), fields=(alias,)) from error
        # Mechanical host values are supplied by actual accepted history/placement,
        # never invented from scientific contents or matched by a field-name suffix.
        host=self.bindings.host_fields(name,path)
        for key,value in host.items():
            if key not in responsibilities or responsibilities[key].role!='HOST':
                raise ValueError('host attempted to replace a scientific field: '+key)
            result[key]=deepcopy(value)
        if 'evidence_refs' in responsibilities:
            result['evidence_refs']=exact_union(result.get('evidence_refs',()),self.evidence)
        if 'model_record_refs' in responsibilities and (self.records or 'model_record_refs' in result):
            result['model_record_refs']=exact_union(result.get('model_record_refs',()),self.records)
        if fields and 'model_submission_refs' in responsibilities and self.submissions:
            result['model_submission_refs']=exact_union(result.get('model_submission_refs',()),self.submissions)
        if name=='Material':
            from tsdaf.contracts.literature.material_relation_basis import transport_relation_bases
            transport_relation_bases(before,result,evidence=self.evidence,records=self.records,
                submissions=self.submissions,equivalent=self.bindings.relation_equivalent)
        for key in schema.get('required',()):
            if key in result:
                continue
            binding=responsibilities[key]
            expanded,_=resolved_schema(schema['properties'][key])
            if key in EMPTY_SLOTS.get(name,{}):
                result[key]=deepcopy(EMPTY_SLOTS[name][key])
            elif authority_schema(expanded) and 'UNSTATED' in expanded['properties']['state']['enum']:
                result[key]=unstated()
            elif binding.role=='CHILDREN' and not binding.wrapped:
                result[key]=[]
            elif binding.codec=='SOURCE_UNIT':
                result[key]=None
            else:
                raise ValueError('explicit field required at '+str((*path,key)))
        return result
