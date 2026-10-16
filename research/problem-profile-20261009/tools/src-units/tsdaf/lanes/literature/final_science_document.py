"""Compile one model document against one fixed skeleton, without a patch ledger.

Both scientific compilation boundaries use this input/codec. Names and source
bindings are exact, supplied capabilities. Prose is never interpreted here.
"""
from copy import deepcopy
from dataclasses import dataclass
import json

from tsdaf.contracts.literature.public_ids import public_id
from tsdaf.contracts.literature.objects import PUBLIC_TARGET_DEFINITIONS
from tsdaf.contracts.literature.schema import MachineProfile, validate_fact_definition
from tsdaf.contracts.literature.validation import validate_scientific_structure
from tsdaf.lanes.literature.scientific_field_bindings import (
    BY_DEFINITION, FIELDS, ROOT_DECLARATIONS, at_path, iter_bound_objects, iter_declarations, public_definition,
)
from tsdaf.lanes.literature.scientific_materialization import (
    LocatedError, ScientificValueCompiler, ValueBindings, exact_union,
)
from tsdaf.lanes.literature.structured_scientific_values import (
    FinalScienceDocument, SkeletonScienceDocument, ROOT_COLLECTIONS, conflict_fields, conflict_value_schema,
)

_METADATA = frozenset({'existing', 'label', 'records', 'citations'})
SCIENCE_KEYS = ('paper_id', 'domain_payload', 'completeness', 'unresolved_items',
                'coverage_assessments', 'blocking_conflicts')


def validate_mapping_scope(document, *, assigned, existing):
    """Check dispatched write ownership, never the scientific content of a task.

    None is the original whole-document task; () is the trailing paper task.
    The same check is used at acceptance and at independent transcript replay.
    """
    if assigned is None:
        return
    owner = assigned[0] if assigned else None
    description = (f'assigned reaction block {owner}' if owner else
                   'the paper task (no reaction blocks)')

    def refuse(reason):
        raise ValueError(f'This task writes {description}: {reason}')

    blocks = document.get('reaction_blocks', ())
    if not owner:
        if blocks:
            refuse('reaction blocks have their own tasks')
        return
    if 'completeness' in document:
        refuse('paper completeness belongs to the paper task')
    prefix = existing[owner]

    def check_members(value, *, own_block):
        if isinstance(value, dict):
            if 'existing' in value:
                path = existing.get(value['existing'])
                if path is None:
                    # Not a scope matter: the skeleton offers no object of this name.
                    raise ValueError(f"unknown baseline object: {value['existing']}; "
                                     'name an offered skeleton object, or omit existing for a new member')
                if not own_block or path[:len(prefix)] != prefix:
                    refuse(f"cannot edit existing member {value['existing']}")
            if 'remove' in value and not own_block:
                refuse('other root collections permit new members only')
            for child in value.values():
                check_members(child, own_block=own_block)
        elif isinstance(value, list):
            for child in value:
                check_members(child, own_block=own_block)

    if len(blocks) > 1:
        refuse('submit the assigned block once')
    for block in blocks:
        if block.get('existing') != owner or 'remove' in block:
            refuse('select the assigned block; do not create or remove reaction blocks')
        check_members(block, own_block=True)
    for field, value in document.items():
        if field not in {'reaction_blocks', 'records', 'citations'}:
            check_members(value, own_block=False)


def scientific_snapshot(machine):
    """Exact public scientific content, independent of an execution envelope."""
    return {key: deepcopy(machine[key]) for key in SCIENCE_KEYS if key in machine}


def validate_scientific_snapshot(value):
    if set(value) - set(SCIENCE_KEYS):
        raise ValueError('scientific snapshot contains publication metadata')
    domain_type = public_definition('MachinePaperResult')['properties']['domain_payload']['$ref'].rsplit('/', 1)[-1]
    validate_fact_definition(domain_type, value['domain_payload'], MachineProfile.V1_7)
    if value['completeness'] not in public_definition('MachinePaperResult')['properties']['completeness']['enum']:
        raise ValueError('completeness must be an explicit public scientific status')
    for field, definition in ROOT_DECLARATIONS.items():
        for row in value.get(field, ()):
            validate_fact_definition(definition, row, MachineProfile.V1_7)
    expected = [row for row in value['domain_payload']['field_conflicts'] if row['blocking']]
    if value['blocking_conflicts'] != expected:
        raise ValueError('blocking conflicts differ from the explicitly declared conflicts')
    validate_scientific_structure(value, profile=MachineProfile.V1_7)


@dataclass(frozen=True)
class _Node:
    definition: str
    value: dict
    path: tuple
    records: tuple
    evidence: tuple
    before: dict | None
    identity: str


class FinalDocumentCompiler:
    """One ephemeral compilation, not an editor or an incremental current state.

    ``existing`` maps readable offered names to exact baseline paths, including
    candidate occurrences. ``records`` and ``sources`` are verified input tables.
    ``submission`` is the actual execution/action identity; it is never model text.
    The caller registers acceptance only after this entire compilation succeeds.
    """

    def __init__(self, baseline=None, *, submission, existing=None, records=None, sources=None,
                 evidence_refs=(), submission_refs=(), citations=None, paper_id=None,
                 check_offered_reference=None, offered_names=None):
        self.check_offered_reference = check_offered_reference
        self.original_baseline = deepcopy(baseline)
        if baseline is None:
            if not paper_id or existing or records:
                raise ValueError('first compilation needs a paper identity and no invented baseline or accounts')
            self.baseline = {'paper_id':paper_id, 'domain_payload':{
                'paper_id':paper_id, 'source_observation_refs':[],
                **{field:[] for field in ROOT_COLLECTIONS}, 'field_conflicts':[]},
                'unresolved_items':[], 'coverage_assessments':[], 'blocking_conflicts':[]}
        else:
            from tsdaf.lanes.literature.faithful_baseline import compilation_baseline
            self.baseline = compilation_baseline(baseline)
        self.submission = submission
        self.existing = dict(existing or {})
        # During sequential publication, the current result also contains names
        # for other tasks' additions. They retain members internally but were
        # never offered as reserved names to this model task.
        self.offered_names = set(self.existing if offered_names is None else offered_names)
        self.records = deepcopy(records or {})
        self.sources = deepcopy(sources or {})
        self.evidence = deepcopy(evidence_refs)
        self.submissions = deepcopy(submission_refs)
        self.citations = deepcopy(citations or {})
        self.baseline_nodes = {row.path: row for row in iter_bound_objects(self.baseline)}
        self.declarations={path:(definition,value) for _,definition,path,value in iter_declarations(self.baseline)}
        for name, path in self.existing.items():
            if path not in self.baseline_nodes and path not in self.declarations:
                raise ValueError(f'offered baseline name has no exact object: {name}')

    def compile(self, document):
        """Compatibility wrapper for existing Machine consumers; no fake first Machine."""
        science = self.compile_science(document)
        if self.original_baseline is None:
            return science
        result = deepcopy(self.baseline)
        result.update(science)
        validate_fact_definition('MachinePaperResult', result, MachineProfile.V1_7)
        return result

    def compile_science(self, document):
        model = SkeletonScienceDocument if self.original_baseline is None else FinalScienceDocument
        document = model.model_validate(document).root
        if self.original_baseline is None and 'completeness' not in document:
            raise ValueError('first compilation must explicitly declare scientific completeness')
        if self.original_baseline is not None:
            if self.check_offered_reference is not None:
                def check_explicit(value):
                    if isinstance(value, dict):
                        if 'existing' in value:
                            self.check_offered_reference(value['existing'])
                        for child in value.values():
                            check_explicit(child)
                    elif isinstance(value, list):
                        for child in value:
                            check_explicit(child)
                check_explicit(document)
            document = self._with_retained_members(document)
        # Reset on every complete proposal; the previous output is never a baseline.
        self.nodes = {}
        self.labels = {}
        self.compiled = {}
        self.compiling = set()
        self.definitions = {}
        inherited = self._record_refs(document.get('records', ()))
        evidence = self._citation_refs(document['citations']) if 'citations' in document else tuple(self.evidence)
        for field, definition in ROOT_COLLECTIONS.items():
            for index, value in enumerate(document.get(field, ())):
                self._index(definition, value, (field, index), inherited, evidence)
        for index, conflict in enumerate(document.get('field_conflicts',())):
            if 'existing' in conflict:
                continue
            definition=conflict_fields()[conflict['target_object_type'],conflict['field_key']]
            rule=FIELDS[definition][conflict['field_key']]
            records,refs=self._basis(conflict,inherited,evidence)
            if rule.role=='CHILDREN':
                for ordinal,candidate in enumerate(conflict['candidate_values']):
                    own_records,own_refs=self._basis(candidate,records,refs)
                    for member,value in enumerate(candidate['value']):
                        self._index(rule.targets[0],value,
                            ('field_conflicts',index,'candidate_values',ordinal,'value',member),own_records,own_refs)
        result = scientific_snapshot(self.baseline)
        for field in ROOT_COLLECTIONS:
            if field in document:
                result['domain_payload'][field] = [self._compile_node((field, i))
                                                  for i in range(len(document[field]))]
        for field, definition in ROOT_DECLARATIONS.items():
            if field in document:
                result[field]=[self._root_declaration(definition,value,(field,index),inherited,evidence)
                               for index,value in enumerate(document[field])]
        if 'field_conflicts' in document:
            result['domain_payload']['field_conflicts']=[self._conflict(value,('field_conflicts',i),inherited,evidence)
                for i,value in enumerate(document['field_conflicts'])]
            result['blocking_conflicts']=[deepcopy(row) for row in result['domain_payload']['field_conflicts'] if row['blocking']]
        if 'completeness' in document:
            result['completeness']=document['completeness']
        validate_scientific_snapshot(result)
        return result

    def _with_retained_members(self, document):
        """Give every supplied collection its complete membership before compiling.

        A baseline member the model did not name is kept exactly as it stands; only
        an explicit ``remove`` drops one. This adds no content and decides nothing
        scientific: it restores the unnamed members of collections the model touched,
        by their exact baseline position, so that adding to one member never deletes
        its siblings. Candidate lists inside an unresolved claim are left as written.
        """
        names = {}
        for name, path in self.existing.items():
            names.setdefault(path, name)

        def members(prefix):
            found = [(path[-1], names[path]) for path in names
                     if path[:-1] in (prefix, (*prefix, 'value')) and isinstance(path[-1], int)]
            return [name for _, name in sorted(found)]

        def merged(rows, baseline, where):
            named, removed = {}, set()
            for position, row in enumerate(rows):
                # Report an unknown name where the model wrote it, before members move.
                if 'existing' in row and row['existing'] not in self.existing:
                    raise ValueError(f"unknown baseline object at {(*where, position)}: {row['existing']}")
                if 'remove' in row:
                    if set(row) != {'existing', 'remove'} or row['existing'] not in baseline:
                        raise ValueError(f'remove names one existing member of this collection at {where}')
                    removed.add(row['existing'])
                elif 'existing' in row:
                    if row['existing'] in named:
                        raise ValueError(f'baseline object defined twice at {where}; define once and use its local label with ref')
                    named[row['existing']] = row
            kept = [row for row in rows if 'remove' not in row]
            if all(name in named or name in removed for name in baseline):
                return kept  # every baseline member is accounted for: the submitted order stands
            added = [row for row in kept if row.get('existing') not in baseline]
            return [*(named.get(name, {'existing': name}) for name in baseline if name not in removed), *added]

        def container(definition, value):
            if 'ref' in value or 'existing' not in value or value['existing'] not in self.existing:
                base = None
            else:
                base = self.existing[value['existing']]
            result = dict(value)
            for field, rule in FIELDS[definition].items():
                if rule.role != 'CHILDREN' or not isinstance(value.get(field), list):
                    continue
                rows = value[field]
                if base is not None:
                    rows = merged(rows, members((*base, field)), (value['existing'], field))
                result[field] = [container(rule.targets[0], row) for row in rows]
            return result

        result = dict(document)
        for field, definition in ROOT_COLLECTIONS.items():
            if field in document:
                rows = merged(document[field], members(('domain_payload', field)), (field,))
                result[field] = [container(definition, row) for row in rows]
        for field in ROOT_DECLARATIONS:
            if field in document:
                result[field] = merged(document[field], members((field,)), (field,))
        return result

    def _basis(self,value,records,evidence):
        return (self._record_refs(value['records']) if 'records' in value else records,
                self._citation_refs(value['citations']) if 'citations' in value else evidence)

    def _root_declaration(self, definition, value, path, records, evidence):
        before=self._existing_declaration(value['existing'],definition) if 'existing' in value else None
        if before is not None and set(value)=={'existing'}:
            return before
        records,evidence=self._basis(value,records,evidence)
        fields={key:child for key,child in value.items() if key not in _METADATA}
        host=({'unresolved_item_id':public_id('UNRESOLVED',self._key(definition,path))}
              if definition=='MachineUnresolvedItem' and before is None else {})
        compiler=ScientificValueCompiler(ValueBindings(
            reference=self._reference,
            child=lambda child,definition,child_path:self._compile_node(child_path),
            source=self._source, sibling=self._no_sibling,
            candidate=lambda location,index:public_id('CAND',self._key('candidate',(*location,index))),
            host_fields=lambda name,location:host if location==path else {},
        ),record_refs=records,evidence_refs=evidence,submission_refs=self.submissions)
        result=compiler.object(definition,fields,before=before,path=path)
        if not fields:
            # An explicit basis selection is still a submission when the model
            # keeps the statement itself unchanged. The bare-existing case above
            # remains a byte-for-byte inheritance.
            for key,new in (('evidence_refs',evidence),('model_record_refs',records),
                            ('model_submission_refs',self.submissions)):
                if key in FIELDS[definition] and new:
                    result[key]=exact_union(result.get(key,()),new)
        if before is not None and any(result.get(key)!=before.get(key)
                for key,binding in FIELDS[definition].items() if binding.role not in {'HOST','RETAINED'}):
            # Root declarations are one statement, not independently sourced field
            # facts. A changed statement must not inherit the old statement's proof.
            for key,new in (('evidence_refs',evidence),('model_record_refs',records),
                            ('model_submission_refs',self.submissions)):
                if key in FIELDS[definition]:
                    if new or key in result:
                        result[key]=deepcopy(list(new))
        return result

    def _existing_declaration(self, name, definition):
        entry=self.declarations.get(self.existing.get(name))
        if entry is None or entry[0]!=definition:
            raise ValueError('existing must select one supplied '+definition+' declaration')
        return deepcopy(entry[1])

    @staticmethod
    def _no_sibling(label):
        raise ValueError('a material-list reference requires its declared owning material list')

    def _conflict(self,value,path,records,evidence):
        if 'existing' in value:
            return self._existing_declaration(value['existing'],'FieldConflict')
        records,evidence=self._basis(value,records,evidence)
        definition=conflict_fields()[value['target_object_type'],value['field_key']]
        target=self._reference(value['target'],(definition,))
        binding=FIELDS[definition][value['field_key']]
        candidates=[]
        for index,candidate in enumerate(value['candidate_values']):
            own_records,own_refs=self._basis(candidate,records,evidence)
            compiler=ScientificValueCompiler(ValueBindings(
                reference=self._reference,
                child=lambda child,definition,location:self._compile_node(location),
                source=self._source,sibling=self._no_sibling,
                candidate=lambda location,i:public_id('CAND',self._key('candidate',(*location,i))),
                host_fields=lambda name,location:{},
            ),record_refs=own_records,evidence_refs=own_refs,submission_refs=self.submissions)
            location=(*path,'candidate_values',index)
            compiled=compiler.field_value(definition,value['field_key'],candidate['value'],path=(*location,'value'))
            candidates.append({'candidate_id':public_id('CAND',self._key('candidate',location)),
                'value':compiled,'evidence_uses':[{'evidence_ref':deepcopy(ref),'role':'SUPPORT'} for ref in own_refs],
                'model_record_refs':deepcopy(list(own_records)),
                'model_submission_refs':deepcopy(list(self.submissions))})
        result={'conflict_id':public_id('CONFLICT',self._key('FieldConflict',path)),
            'target_object_type':value['target_object_type'],'target_object_id':target,
            'field_key':value['field_key'],'candidate_values':candidates,'blocking':value['blocking'],
            'model_submission_refs':deepcopy(list(self.submissions))}
        validate_fact_definition('FieldConflict',result,MachineProfile.V1_7)
        return result

    def _record_refs(self, names):
        for name in names:
            if name not in self.records:
                raise ValueError(f'unknown scientific account: {name}')
        return tuple(deepcopy(self.records[name]) for name in names)

    def _key(self, definition, path):
        return json.dumps([self.submission, definition, path], ensure_ascii=False, separators=(',', ':'))

    def _citation_refs(self, names):
        result=[]
        for name in names:
            if name not in self.citations:
                raise ValueError(f'unknown offered source: {name}')
            for ref in self.citations[name]:
                if ref not in result:
                    result.append(deepcopy(ref))
        return tuple(result)

    def _index(self, definition, value, path, records, evidence):
        if 'ref' in value:
            self.nodes[path] = (definition, value['ref'])
            return
        if 'records' in value:
            records = self._record_refs(value['records'])
        if 'citations' in value:
            evidence = self._citation_refs(value['citations'])
        before = None
        if 'existing' in value:
            name = value['existing']
            if name not in self.existing:
                raise ValueError(f'unknown baseline object at {path}: {name}')
            located = self.baseline_nodes.get(self.existing[name])
            if located is None:
                raise ValueError(f'wrong existing object type at {path}: expected {definition}')
            if located.binding.definition != definition:
                raise ValueError(f'wrong object type at {path}: expected {definition}')
            before = located.value
            # One definition, plus explicit references if it is shared. Do not
            # merge competing mutations or infer that matching texts are one object.
            if located.path in self.definitions:
                raise ValueError(f'baseline object defined twice at {path}; define once and use its local label with ref')
            self.definitions[located.path] = path
        binding = BY_DEFINITION[definition]
        identity = before[binding.identity] if before is not None else public_id(binding.family, self._key(definition, path))
        self.nodes[path] = _Node(definition, value, path, records, evidence, before, identity)
        if 'label' in value:
            label = value['label']
            if label in self.labels or label in self.offered_names or label=='this paper':
                raise ValueError(f'local label is ambiguous at {path}: {label}')
            self.labels[label] = path
        for field, rule in FIELDS[definition].items():
            if rule.role != 'CHILDREN' or field not in value:
                continue
            contents = value[field]
            if rule.wrapped:
                if isinstance(contents, list):
                    arrays = [(contents, (*path, field, 'value'))]
                else:
                    arrays = [(rows, (*path, field, 'alternatives', i))
                              for i, rows in enumerate(contents.get('alternatives', ()))]
            else:
                arrays = [(contents, (*path, field))]
            for rows, base in arrays:
                for index, child in enumerate(rows):
                    self._index(rule.targets[0], child, (*base, index), records, evidence)

    def _node(self, path, seen=()):
        if path in seen:
            raise ValueError(f'cyclic local object reference at {path}')
        node = self.nodes[path]
        if isinstance(node, _Node):
            return node
        definition, label = node
        if label not in self.labels:
            raise ValueError(f'unknown shared local object at {path}: {label}')
        target = self._node(self.labels[label], (*seen, path))
        if target.definition != definition:
            raise ValueError(f'wrong shared object type at {path}: expected {definition}')
        return target

    @staticmethod
    def _described(node):
        """The submitted object in the model's own terms: its offered name or local label."""
        value = node.value
        if 'existing' in value:
            return f"{node.definition} {value['existing']!r}"
        if 'label' in value:
            return f"new {node.definition} labelled {value['label']!r}"
        shown = json.dumps({key: child for key, child in value.items()
                            if isinstance(child, (str, int, float)) or key == 'names'}, ensure_ascii=False)
        return f'new {node.definition} ' + (shown if len(shown) <= 90 else shown[:87] + '...')

    def _reference(self, label, allowed):
        if label=='this paper' and 'Paper' in allowed:
            return self.baseline['paper_id']
        if label in self.labels:
            node = self._node(self.labels[label])
            definition, identity = node.definition, node.identity
        elif label in self.existing:
            if self.check_offered_reference is not None:
                self.check_offered_reference(label)
            located = self.baseline_nodes.get(self.existing[label])
            if located is None:
                raise ValueError('a root declaration is not a scientific object reference: '+label)
            definition, identity = located.binding.definition, located.identity
        else:
            # Offer only names that exist and have the declared type; never pick one.
            local = sorted(name for name, path in self.labels.items() if self._node(path).definition in allowed)
            offered = sorted(name for name, path in self.existing.items()
                             if name in self.offered_names and path in self.baseline_nodes
                             and self.baseline_nodes[path].binding.definition in allowed)
            shown = [*local, *offered][:12]
            more = len(local) + len(offered) - len(shown)
            raise ValueError(f'unknown object reference: {label}. ' + (
                'Names of this kind: ' + '; '.join(shown) + (f'; and {more} more offered names.' if more else '.')
                if shown else 'This document declares no local name of this kind.'))
        if definition not in allowed or identity is None:
            raise ValueError(f'invalid reference type or unmaterialized object: {label}; expected {" or ".join(allowed)}')
        return identity

    def _sibling(self, path, *, label=None, baseline_path=None):
        if label is not None and label not in self.labels and label not in self.existing:
            # A summary names a material of this same list: offer those, not the paper's.
            here = [self._node(occurrence).value for occurrence in self.nodes if occurrence[:-1] == path[:-1]]
            names = [row.get('label') or row.get('existing') for row in here]
            raise ValueError(f'unknown object reference: {label}. Named materials of this list: '
                             + ('; '.join(name for name in names if name) or 'none') + '.')
        target = self._reference(label, ('Material',)) if label is not None else None
        matches = []
        for occurrence in self.nodes:
            if occurrence[:-1] != path[:-1]:
                continue
            member = self._node(occurrence)
            original = self.existing.get(member.value.get('existing'))
            if (target is not None and member.identity == target or
                    baseline_path is not None and original == baseline_path):
                matches.append(occurrence[-1]+1)
        if len(matches) != 1:
            raise ValueError(f'summary_material must select one member of this final material list at {path}')
        return matches[0]

    def _placed(self, node, value, path):
        """Only location-dependent codecs vary between explicit appearances.

        A public summary index is an address, not the scientific relationship.
        Recover its exact baseline target or explicit submitted name and encode
        it in THIS final list. Never reuse a cached index across candidates.
        """
        value = deepcopy(value)
        if node.definition == 'Operation':
            value['sequence_index'] = path[-1]+1
        if node.definition == 'Material':
            if 'summary_material' in node.value:
                if node.value['summary_material'] is not None:
                    value['summary_material_index'] = self._sibling(path, label=node.value['summary_material'])
            elif node.before is not None and 'summary_material_index' in node.before:
                original = self.existing[node.value['existing']]
                target = (*original[:-1], node.before['summary_material_index']-1)
                value['summary_material_index'] = self._sibling(path, baseline_path=target)
        return value

    def _source(self, name):
        if name not in self.sources:
            raise ValueError(f'unknown offered source: {name}')
        return deepcopy(self.sources[name])

    def _relation_equivalent(self,node,key,before,after):
        if key!='summary_material_index':
            return before==after
        if 'summary_material' not in node.value:
            return True
        original=self.existing[node.value['existing']]
        old_target=self.baseline_nodes[(*original[:-1],before-1)].identity
        return old_target==self._reference(node.value['summary_material'],('Material',))

    def _compile_node(self, path):
        node = self._node(path)
        if node.path in self.compiling:
            raise ValueError(f'cyclic scientific containment at {path}')
        if node.path in self.compiled:
            return self._placed(node, self.compiled[node.path], path)
        self.compiling.add(node.path)
        fields = {key: value for key, value in node.value.items() if key not in _METADATA}
        binding = BY_DEFINITION[node.definition]
        host = {binding.identity: node.identity}
        if node.before is None:
            if 'stable_key' in public_definition(node.definition).get('required', ()):
                host['stable_key'] = self._key(node.definition, node.path)
            if node.definition == 'ReportedOutcome':
                host['value_provenance'] = 'REPORTED'
        if node.definition == 'OperationStage':
            if node.before is None:
                host['display_index'] = path[-1]+1
            wording_changed = (node.before is None or
                'description' in fields and fields['description'] != node.before['description'])
            if wording_changed:
                # Boundary verification belongs to the exact verified wording.
                # Mapping a different description is a model declaration, not a
                # new quote check, and must not inherit an old VERIFIED result.
                host.update(verification='SUBMISSION_DECLARED' if self.submissions else 'RECORD_MAPPED',
                            boundary=None, boundary_evidence_refs=[])
        # Display order follows explicit containment. It does not infer chemical
        # precedence; the model's separate precedence relations remain untouched.
        if node.definition == 'Operation':
            host['sequence_index'] = path[-1]+1
        if node.definition == 'ReactionBlock' and node.before is None:
            host['source_order'] = path[-1]+1
        compiler = ScientificValueCompiler(ValueBindings(
            reference=self._reference,
            child=lambda child, definition, child_path: self._compile_node(child_path),
            source=self._source,
            sibling=lambda label: self._sibling(node.path, label=label),
            candidate=lambda location, index: public_id('CAND', self._key('candidate', (*location, index))),
            host_fields=lambda definition, location: host if location == node.path else {},
            relation_equivalent=lambda key, before, after: self._relation_equivalent(node,key,before,after),
        ), record_refs=node.records, evidence_refs=node.evidence, submission_refs=self.submissions)
        try:
            value = compiler.object(node.definition, fields, before=node.before, path=node.path)
            if node.definition == 'Operation':
                # The contract's own material rule, applied here as well only so that
                # a refusal can name this operation; the final validation still runs.
                from tsdaf.contracts.literature.validation import validate_material_relations
                materials = value.get('materials') or {}
                stages = {stage['stage_id'] for stage in value.get('stages', ())}
                for rows in (materials.get('value'), *(c.get('value') for c in materials.get('candidates', ()))):
                    if isinstance(rows, list):
                        validate_material_relations(rows, stages, faithful_mapping=True)
        except LocatedError as error:
            if error.where is not None:
                raise
            raise LocatedError(error.reason, fields=error.fields, where=self._described(node)) from error
        except ValueError as error:
            raise LocatedError(str(error), where=self._described(node)) from error
        # Identity is still installed for a pure existing reference with a legacy
        # optional ID. This does not attach records or alter any scientific field.
        value.setdefault(binding.identity, node.identity)
        self.compiling.remove(node.path)
        self.compiled[node.path] = deepcopy(value)
        return self._placed(node, value, path)
