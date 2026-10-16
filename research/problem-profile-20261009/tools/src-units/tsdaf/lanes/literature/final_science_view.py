"""Readable fixed-skeleton input from the same field responsibilities as compilation.

The view omits storage wrappers and keeps every explicit scientific value. Exact
occurrence names select existing containers; ordinary references select identities.
"""
from copy import deepcopy

from jsonschema import Draft202012Validator
from tsdaf.contracts.literature.objects import PUBLIC_TARGET_DEFINITIONS
from tsdaf.contracts.literature.expressions import unstated

from tsdaf.lanes.literature.scientific_field_bindings import (
    EMPTY_SLOTS, FIELDS, authority_schema, iter_bound_objects, iter_declarations, public_definition,
)
from tsdaf.lanes.literature.structured_scientific_values import (
    ROOT_COLLECTIONS, resolved_schema, conflict_fields, conflict_value_schema,
)


class FinalScienceView:
    def __init__(self, machine, *, sources=None, compact=False):
        self.machine = machine
        self.compact = compact
        self.sources = sources or {}
        self.existing = {}
        self.by_path = {}
        self.references = {}
        # Names describe actual containment, including candidate position. No
        # title matching decides which scientific object a reference denotes.
        for row in iter_bound_objects(machine):
            parts = []
            for part in row.path[1:]:
                if isinstance(part, int):
                    parts[-1] += ' ' + str(part+1)
                elif part != 'value':
                    parts.append(part.replace('_', ' '))
            name = ' / '.join(parts)
            self.existing[name] = row.path
            self.by_path[row.path] = name
            if row.identity is not None:
                self.references.setdefault(row.identity, name)
        for field,definition,path,value in iter_declarations(machine):
            name=field.replace('_',' ')+' '+str(path[-1]+1)
            self.existing[name]=path
            self.by_path[path]=name
        definitions = {}
        def include(value):
            if isinstance(value, dict):
                if '$ref' in value:
                    name = value['$ref'].rsplit('/',1)[-1]
                    if name not in definitions:
                        definitions[name] = public_definition(name)
                        include(definitions[name])
                for child in value.values(): include(child)
            elif isinstance(value, list):
                for child in value: include(child)
        for name in FIELDS: include({'$ref':'#/$defs/'+name})
        self.validator = Draft202012Validator({'$defs':definitions})

    def document(self):
        domain = self.machine['domain_payload']
        return {field:[self.object(definition, value, ('domain_payload',field,i))
                       for i,value in enumerate(domain.get(field,()))]
                for field,definition in ROOT_COLLECTIONS.items() if field in domain}

    def root_declarations(self):
        """Keep original scientific declarations visible, apart from storage proof.

        These are display aliases of existing objects, not new scientific owners.
        The immutable input/public artifact still carries every proof and identity.
        """
        names={**self.references,self.machine['paper_id']:'this paper'}
        technical={'stable_key','key_basis','creation_key','unresolved_item_id','conflict_id',
            'candidate_id','evidence_refs','evidence_uses','coverage_scope_ref','model_record_refs',
            'model_submission_refs','relation_record_refs','relation_submission_refs',
            'resolved_value_sha256'}
        aliases={'subject_id':'subject','subject_ids':'subjects','target_object_id':'target','object_id':'object'}
        def display(value,reference=False):
            if isinstance(value,dict):
                return {aliases.get(key,key):display(child,key in aliases)
                        for key,child in value.items() if key not in technical}
            if isinstance(value,list):
                return [display(child,reference) for child in value]
            return names.get(value,value) if reference and isinstance(value,str) else deepcopy(value)
        result={key:display(self.machine[key]) for key in
            ('blocking_conflicts','unresolved_items','coverage_assessments','coverage','completeness')
            if key in self.machine}
        for field,definition,path,value in iter_declarations(self.machine):
            if field not in result:
                result[field]=[]
            if definition=='FieldConflict':
                target_type=PUBLIC_TARGET_DEFINITIONS[value['target_object_type']]
                binding=FIELDS[target_type].get(value['field_key'])
                row={'existing':self.by_path[path],
                    **{key:value[key] for key in ('target_object_type','field_key','blocking')},
                    'target':self.references[value['target_object_id']],
                    'candidate_values':[{'value':self.value(
                        conflict_value_schema(target_type,value['field_key']),candidate['value'],
                        (*path,'candidate_values',i,'value'),binding=binding)}
                        for i,candidate in enumerate(value['candidate_values'])]}
            elif definition=='ReviewMarker':
                row={'existing':self.by_path[path],
                     **{key:value[key] for key in ('target_object_type','field_key','reason')},
                     'target':self.references[value['target_object_id']]}
                if 'material_index' in value:
                    owner=self.existing[self.references[value['target_object_id']]]
                    row['material']=self.by_path[(*owner,'materials','value',value['material_index']-1)]
            else:
                row=self.object(definition,value,path)
            if path[-1]<len(result[field]):
                result[field][path[-1]]=row
            else:
                result[field].append(row)
        if 'field_conflicts' in result:
            result['blocking_conflicts']=[row for row in result['field_conflicts'] if row['blocking']]
        return result

    def object(self, definition, value, path):
        result = {'existing':self.by_path[path]} if path in self.by_path else {}
        schema = public_definition(definition)
        for key, binding in FIELDS[definition].items():
            if key not in value or binding.role in ('HOST','RETAINED'):
                continue
            alias = binding.input_name or key
            raw = value[key]
            # Only an exactly addressed existing container can inherit an omitted
            # field. Do not prune VALUE array members or PRESENT-null wrappers.
            if self.compact and path in self.by_path:
                empty = EMPTY_SLOTS.get(definition, {})
                if key in empty and raw == empty[key]:
                    continue
                expanded, _ = resolved_schema(schema['properties'][key])
                if authority_schema(expanded) and raw == unstated():
                    continue
            if binding.codec == 'SIBLING_INDEX':
                target = (*path[:-1],raw-1)
                result[alias] = self.by_path[target]
            elif binding.codec == 'SOURCE_UNIT':
                if raw is None:
                    result[alias] = None
                else:
                    matches = [name for name, source in self.sources.items()
                               if source['unit_block_id']==raw
                               and all(value.get(key)==source.get(key) for key in
                                       ('target_source_document_id','target_source_revision_id'))]
                    if len(matches)!=1:
                        raise ValueError('baseline source has no exact readable binding')
                    result[alias] = matches[0]
            else:
                result[alias] = self.value(schema['properties'][key],raw,(*path,key),binding=binding)
        if definition=='ConditionNote':
            from tsdaf.lanes.literature.condition_compilation_input import compact_condition_value
            return compact_condition_value(result, value)
        return result

    def value(self, schema, value, path, *, name=None, binding=None):
        expanded, named = resolved_schema(schema)
        if expanded is not schema:
            return self.value(expanded,value,path,name=named or name,binding=binding)
        if authority_schema(schema):
            state = value['state']
            if state=='PRESENT':
                return self.value(schema['properties']['value'],value['value'],(*path,'value'),binding=binding)
            claim = {'claim':state}
            if state=='UNRESOLVED':
                claim.update(reason=value['reason_code'], alternatives=[
                    self.value(schema['properties']['value'],row['value'],(*path,'candidates',i,'value'),binding=binding)
                    for i,row in enumerate(value['candidates'])])
            return claim
        if binding is not None and binding.role=='CHILDREN':
            return [self.object(binding.targets[0],row,(*path,i)) for i,row in enumerate(value)]
        if binding is not None and binding.role=='REFERENCE':
            if value is None: return None
            def name(v):
                return 'this paper' if v==self.machine['paper_id'] and 'Paper' in binding.targets else self.references[v]
            if isinstance(value,list): return [name(v) for v in value]
            return name(value)
        if name=='ReportedScalarValue':
            result = {'text':value['raw_text']}
            if value['numeric_value_decimal'] is not None:
                result['value'] = value['numeric_value_decimal']
            elif value['range_min_decimal'] is not None:
                result['range'] = [value['range_min_decimal'],value['range_max_decimal']]
            if value['unit'] is not None and ('value' in result or 'range' in result):
                result['unit'] = value['unit']
            return result
        if name=='StructureValue':
            return {key:deepcopy(value[key]) for key in ('input_format','raw_value')}
        if 'properties' in schema:
            return self.object(name,value,path)
        for key in ('anyOf','oneOf'):
            if key in schema:
                branches = [branch for branch in schema[key] if self.validator.evolve(schema=branch).is_valid(value)]
                if not branches:
                    raise ValueError('baseline violates its declared scientific type')
                rendered = [self.value(branch,value,path,binding=binding) for branch in branches]
                if any(item!=rendered[0] for item in rendered[1:]):
                    raise ValueError('baseline has ambiguous scientific representation')
                return rendered[0]
        if schema.get('type')=='array':
            return [self.value(schema['items'],row,(*path,i)) for i,row in enumerate(value)]
        return deepcopy(value)
