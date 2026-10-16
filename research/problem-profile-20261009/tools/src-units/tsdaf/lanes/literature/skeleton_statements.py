"""The paper skeleton written as flat statements instead of one nested document.

The skeleton compile read its sources and then had to deliver the whole skeleton --
compounds, steps, routes, narratives: thirty to seventy thousand characters -- in one
reply. With reading fixed (2026-10-10) that one reply was what remained: 336 s on
principinol B, 701 s and a resend on hyperfirin, four broken streams in a row on
ineleganolide. The final compile had the same shape until its tasks were given one small
function per thing a note states (`block_statements`); these are the same for the skeleton.

This module turns the statements into the ordinary skeleton document; the same compiler
and contract then decide whether it can be saved. Nothing here reads meaning. In
particular it does not derive what the model did not state: checked on three accepted
skeletons, a compound's successors equal "the outputs of the steps it enters" for 16/18,
6/26 and 16/21 compounds, and no route lists exactly the compounds its steps use. Those
are the model's statements (main line and branch, by-product or successor), so they are
parameters here, not inferences.
"""
from copy import deepcopy

from tsdaf.lanes.literature.block_statements import (
    ROLES, _Q, _STATED, StatementError, _misshapen, _statement, scalar, stated,
)

NARRATIVE_KINDS = ('OBJECTIVE', 'AUTHOR_CONCLUSION', 'ROUTE_STRATEGY', 'SCOPE', 'MECHANISM',
                   'STRUCTURAL_CONTEXT', 'RELATED_FIGURE', 'OPTIMIZATION', 'FAILED_ATTEMPT')
ASSERTION_SUBJECTS = ('STRUCTURE', 'ABSOLUTE_CONFIGURATION', 'IDENTITY_MATCH')
ASSERTION_OUTCOMES = ('CONFIRMED', 'REVISED', 'DISPROVED', 'INCONCLUSIVE')
REASONS = ('CONFLICTING_CANDIDATES', 'UNPARSED_SOURCE', 'INSUFFICIENT_EVIDENCE', 'STAGE_PARTIAL', 'WAIT_FOR_SI',
           'SI_UNAVAILABLE', 'NO_EXPERIMENTAL_SOURCE', 'NOT_STATED_BY_AUTHOR', 'OUT_OF_SCOPE', 'NO_PUBLIC_SLOT')
SCOPES = ('COMPOUND', 'REACTION_BLOCK', 'ROUTE', 'PAPER')
COVERAGE_STATUSES = ('SUFFICIENT', 'NEEDS_READ', 'UNRESOLVED')
UNSTATED = 'NOT_STATED'

_S = {'type': 'string'}
_NAMES = {'type': 'array', 'items': _S}
_CITE = {'type': 'array', 'items': _S, 'description': 'Names of the sources this comes from, exactly as the input offers them.'}
_WORDING = {'type': 'string', 'description': "The author's own words that state this, copied from the source."}
_STEP = {'type': 'string', 'description': 'Name of the step this belongs to (the name you gave it).'}
_OPERATION = {'type': 'string', 'description': 'Name of the operation within the step, when the step has more than one.'}
_COMPOUNDS = {'type': 'array', 'items': _S, 'description': 'Names of the compounds (the names you gave them).'}
# In the skeleton a yield is often all the sources give; what it measures is then its kind.
_MEASURED = {**_STATED, 'required': ['quantity'], 'properties': {
    **_STATED['properties'], 'what': {'type': 'string', 'description': "In the source's words, what this quantity measures or is counted over (\"over two steps\"), when it says."},
    'cite': _CITE}}

STATEMENTS = {
    'compound': _statement('compound', 'A compound of the route: starting material, intermediate, product, or by-product.', {
        'name': {'type': 'string', 'description': 'A short name you choose; other calls use it to refer to this compound.'},
        'author_labels': {**_NAMES, 'description': "The author's printed labels for it, exactly as printed: 13, 14a, SI-6."},
        'names': {**_NAMES, 'description': 'The names the sources give it.'},
        'made_from': {**_COMPOUNDS, 'description': 'Names of the compounds the sources say it is made from.'},
        'leads_to': {**_COMPOUNDS, 'description': 'Names of the compounds the sources say it is carried on to.'},
        'note': {'type': 'string', 'description': 'What this compound is in the route, in a sentence.'},
        'wording': _WORDING, 'cite': _CITE},
        ['name']),
    'step': _statement('step', 'One transformation of the route as the sources report it: what goes in, what comes out, and how.', {
        'name': {'type': 'string', 'description': 'A short name you choose; other calls use it to place things in this step.'},
        'inputs': {**_COMPOUNDS, 'description': f'Names of the compounds it starts from. Write [{UNSTATED!r}] if the sources do not say.'},
        'outputs': {**_COMPOUNDS, 'description': f'Names of the compounds it gives. Write [{UNSTATED!r}] if the sources do not say.'},
        'summary': {'type': 'string', 'description': 'The transformation in one line.'},
        'description': {'type': 'string', 'description': 'What is done, as the sources describe it.'},
        'reaction_class': {'type': 'string', 'description': 'The kind of reaction, when the sources name it.'},
        'atmosphere': _S,
        'note': {'type': 'string', 'description': 'Anything else the sources say about this step as a whole.'},
        'cite': _CITE,
        'description_cite': {**_CITE, 'description': 'Sources of the description, when they differ from cite.'}},
        ['name', 'description']),
    'operation': _statement('operation', 'A further operation of a step that the sources report as more than one.', {
        'step': _STEP,
        'name': {'type': 'string', 'description': 'A short name you choose for this operation.'},
        'description': {'type': 'string', 'description': 'What is done, as the sources describe it.'},
        'reaction_class': _S, 'atmosphere': _S,
        'after': {**_NAMES, 'description': 'Names of the operations of this step it follows.'},
        'cite': _CITE},
        ['step', 'name', 'description']),
    'material': _statement('material', 'A substance the sources name in a step: substrate, reagent, catalyst, additive, solvent, with a quantity only where the sources you read state one.', {
        'step': _STEP, 'operation': _OPERATION,
        'names': {**_NAMES, 'description': 'The names the sources use for it.'},
        'role': {'type': 'string', 'enum': list(ROLES)},
        'compound': {'type': 'string', 'description': 'Name of the compound this material is, when it is one of the compounds you declared.'},
        'amount': _Q, 'equivalents': _Q,
        'wording': _WORDING,
        'printed_name': {'type': 'string', 'description': 'The name exactly as the author prints it there, when it differs from names.'},
        'role_wording': {'type': 'string', 'description': "The author's word for its role, when printed."},
        'cite': _CITE},
        ['step', 'names']),
    'condition': _statement('condition', 'A condition the sources give for a whole step: temperature, time, pressure, technique, or any other.', {
        'step': _STEP, 'operation': _OPERATION,
        'category': {'type': 'string', 'description': 'What kind of value this is, e.g. temperature, duration.'},
        'quantity': _Q},
        ['step', 'category', 'quantity']),
    'outcome': _statement('outcome', 'A measured result the sources report for a step: yield, ratio, selectivity, recovered material.', {
        'step': _STEP,
        'operations': {**_NAMES, 'description': 'Names of the operations it results from, when the step has more than one.'},
        'products': {**_COMPOUNDS, 'description': 'Names of the compounds it reports.'},
        'quantities': {'type': 'array', 'items': _MEASURED},
        'wording': _WORDING,
        'product_label': {'type': 'string', 'description': "The author's printed label for the product in that wording."},
        'kind_wording': {'type': 'string', 'description': "The author's own word for the measure (\"yield\", \"brsm\"), when printed."},
        'cite': _CITE},
        ['step', 'quantities']),
    'intermediate': _statement('intermediate', 'A species formed and consumed inside one step without being isolated.', {
        'step': _STEP,
        'name': {'type': 'string', 'description': 'A short name you choose.'},
        'printed_name': {'type': 'string', 'description': 'The name the sources give it.'},
        'author_label': {'type': 'string', 'description': "The author's printed label, if it has one."},
        'note': _S, 'wording': _WORDING, 'cite': _CITE},
        ['step', 'name']),
    'route': _statement('route', 'A route of the paper: which steps and compounds it consists of, and what it reaches.', {
        'name': {'type': 'string', 'description': 'A short name you choose.'},
        'steps': {**_NAMES, 'description': 'Names of its steps, in the order of the route.'},
        'compounds': {**_COMPOUNDS, 'description': 'Names of the compounds that belong to this route.'},
        'overview': {'type': 'string', 'description': 'The route in a few sentences.'},
        'note': {'type': 'string', 'description': 'How the sources fit together for this route, when that needs saying.'},
        'paths': {'type': 'array', 'description': 'One entry per target the route reaches.', 'items': {'type': 'object', 'required': ['target'], 'properties': {
            'target': {'type': 'string', 'description': 'Name of the target compound.'},
            'steps': {**_NAMES, 'description': 'Names of the steps on the way to it, in order.'},
            'step_count': {'type': 'integer', 'description': "The author's stated number of steps."},
            'step_count_basis': {'type': 'string', 'description': 'What the author counts it from and how.'},
            'overall_yield': _Q, 'cite': _CITE}}},
        'targets': {'type': 'array', 'description': 'What the authors assert about a target they reached.', 'items': {'type': 'object', 'required': ['target'], 'properties': {
            'target': {'type': 'string', 'description': 'Name of the target compound.'},
            'subject': {'type': 'string', 'enum': list(ASSERTION_SUBJECTS)},
            'outcome': {'type': 'string', 'enum': list(ASSERTION_OUTCOMES)},
            'wording': _WORDING, 'cite': _CITE}}},
        'cite': _CITE},
        ['name', 'steps']),
    'narrative': _statement('narrative', "A statement of the paper that is not a step: its objective, strategy, a mechanism, an optimization, a failed attempt, the authors' conclusion.", {
        'type': {'type': 'string', 'enum': list(NARRATIVE_KINDS), 'description': 'What kind of statement it is.'},
        'kind_wording': {'type': 'string', 'description': "The author's own word for what kind of statement this is, when printed."},
        'title': _S, 'wording': _WORDING,
        'explanation': {'type': 'string', 'description': 'What it says, in a sentence or two.'},
        'about': {**_NAMES, 'description': 'Names of the compounds, steps or routes it concerns.'},
        'cite': _CITE},
        ['type', 'title']),
    'characterization': _statement('characterization', 'A characterization the sources report for a compound.', {
        'compounds': _COMPOUNDS, 'method': _S,
        'method_wording': {'type': 'string', 'description': "The author's name for the method."},
        'wording': _WORDING,
        'sample': {'type': 'string', 'description': 'What was measured, when the sources say (a single crystal, the natural sample).'},
        'accession_ids': {**_NAMES, 'description': 'Deposition numbers, e.g. CCDC 2245070.'},
        'note': _S, 'cite': _CITE},
        ['compounds']),
    'biology': _statement('biology', 'A biological result the sources report for a compound.', {
        'compounds': _COMPOUNDS, 'endpoint': _S,
        'system': {'type': 'string', 'description': 'The organism, cell line or assay system.'},
        'comparator': {'type': 'string', 'description': 'A printed comparator such as < or >.'},
        'quantity': _Q, 'cite': _CITE},
        ['compounds', 'endpoint']),
    'coverage': _statement('coverage', 'Whether the sources read cover what they should for the route.', {
        'phase': {'type': 'string', 'enum': ['MAIN', 'SI']},
        'status': {'type': 'string', 'enum': list(COVERAGE_STATUSES), 'description': 'SUFFICIENT when the sources read cover it; NEEDS_READ when a source that should be read was not; UNRESOLVED when it cannot be told.'},
        'explanation': _S,
        'about': {**_NAMES, 'description': 'Names of the compounds, steps or routes it concerns.'},
        'cite': _CITE},
        ['phase', 'status', 'explanation', 'about']),
    'unresolved': _statement('unresolved', 'Something the sources leave open, state inconsistently, or that has no place among the other calls.', {
        'scope': {'type': 'string', 'enum': list(SCOPES)},
        'reason': {'type': 'string', 'enum': list(REASONS)},
        'subject': {'type': 'string', 'description': 'Name of the compound, step or route it concerns; "this paper" for the paper.'},
        'also_about': {**_NAMES, 'description': 'Names of further objects it concerns.'},
        'statement': _S, 'cite': _CITE},
        ['scope', 'reason', 'subject', 'statement']),
    'completeness': _statement('completeness', 'Whether the skeleton holds the whole forward route the sources report.', {
        'value': {'type': 'string', 'enum': ['COMPLETE', 'PARTIAL']}}, ['value']),
    'remove': {'type': 'object', 'description': 'Take back an earlier call.', 'required': ['kind', 'id'],
               'properties': {'kind': {'type': 'string', 'enum': ['remove']}, 'id': {'type': 'string'}}},
}
TOOLS = tuple(STATEMENTS)
# What a name may refer to, by the kind that declares it. A name is unique across all of them.
NAMED = ('compound', 'step', 'route', 'intermediate')
_PREFIX = {'compound': 'c', 'step': 's', 'operation': 'o', 'material': 'm', 'condition': 'k', 'outcome': 'r', 'intermediate': 'i',
           'route': 'p', 'narrative': 'n', 'characterization': 'h', 'biology': 'b', 'coverage': 'v', 'unresolved': 'u', 'completeness': 'z'}


def declarations():
    """One function per kind of statement, each with its own parameter table."""
    out = []
    for kind, schema in STATEMENTS.items():
        properties = {key: value for key, value in schema['properties'].items() if key != 'kind'}
        out.append({'type': 'function', 'name': kind, 'description': schema['description'],
                    'parameters': {'type': 'object', 'properties': deepcopy(properties),
                                   'required': [key for key in schema['required'] if key != 'kind']}})
    return out


def _declared(entries, kind):
    return [e['args']['name'] for e in entries if e['kind'] == kind]


def _operations(entries, step):
    return [e['args']['name'] for e in entries if e['kind'] == 'operation' and e['args'].get('step') == step]


def accept(entries, statements, *, sources=None):
    """Earlier entries plus these statements, each with its id; nothing is changed in place.

    The statements of one reply are taken together, so one may name a compound or a step
    that a later one of the same reply declares. `sources` are the names the task offered
    for citing; a citation outside them is refused with the names that exist.
    """
    result, ids = deepcopy(list(entries)), []
    made = {}
    counter = max((int(e['id'][1:]) for e in result), default=0)
    for index, raw in enumerate(statements):
        where = f'call {index + 1} of this reply'
        if not isinstance(raw, dict) or raw.get('kind') not in STATEMENTS:
            raise StatementError(f'{where}: kind must be one of ' + ', '.join(STATEMENTS))
        kind, args = raw['kind'], {k: v for k, v in raw.items() if k not in ('kind', 'replace')}
        if kind == 'remove':
            kept = [e for e in result if e['id'] != args.get('id')]
            if len(kept) == len(result):
                raise StatementError(f"{where}: no saved call has the id {args.get('id')!r}")
            result = kept; ids.append(None)
            continue
        wrong = _misshapen({**args, 'kind': kind}, STATEMENTS[kind], '')
        if wrong:
            raise StatementError(f'{where} ({kind}): {wrong}')
        replaced = raw.get('replace')
        target = next((e for e in result if e['id'] == replaced), None) if replaced else None
        if replaced and target is None:
            raise StatementError(f'{where} ({kind}): no saved call has the id {replaced!r}')
        same = None if replaced else next((e for e in result if e['kind'] == kind and e['args'] == args), None)
        if same is not None:
            # Said again word for word: it is already held, and saying it twice adds nothing.
            ids.append(same['id']); made.setdefault(same['id'], index)
            continue
        if kind in NAMED or kind == 'operation':
            clash = next((e for e in result if e is not target and e['kind'] in (*NAMED, 'operation') and e['args'].get('name') == args['name']
                          and (kind != 'operation' or e['kind'] != 'operation' or e['args'].get('step') == args.get('step'))), None)
            if clash:
                raise StatementError(f"{where} ({kind}): call {clash['id']} already uses the name {args['name']!r} for a {clash['kind']}. "
                                     f"Give this one another name, or repeat that call with replace: {clash['id']!r} to change it.")
        if target is not None:
            target.update(kind=kind, args=args); ids.append(target['id'])
        else:
            counter += 1
            result.append({'id': f'{_PREFIX[kind]}{counter}', 'kind': kind, 'args': args}); ids.append(result[-1]['id'])
        made[ids[-1]] = index
    if result and not any(e['kind'] == 'completeness' for e in result):
        raise StatementError('The skeleton cannot be saved until completeness is stated: add a completeness call '
                             '(COMPLETE or PARTIAL) to this reply. Nothing else in the reply needs to change.')
    # References are checked against everything now held, so order within the reply is free.
    compounds, steps, routes = (set(_declared(result, k)) for k in ('compound', 'step', 'route'))
    intermediates = set(_declared(result, 'intermediate'))
    anything = compounds | steps | routes | intermediates

    def said(entry):
        # A call of this reply is named by its place in the reply; one saved earlier by its id.
        return (f"call {made[entry['id']] + 1} of this reply" if entry['id'] in made else f"call {entry['id']}, saved earlier,") + f" ({entry['kind']})"

    def need(entry, field, names, allowed, what, extra=()):
        for name in names or ():
            if name not in allowed and name not in extra:
                raise StatementError(
                    f"{said(entry)}: {field} names {name!r}, which is not a {what} you have declared. "
                    f"Declared: " + ('; '.join(sorted(allowed)) or '(none yet)'))

    for entry in result:
        a, kind = entry['args'], entry['kind']
        if sources is not None:
            cited = [*(a.get('cite') or ()), *(a.get('description_cite') or ()),
                     *(c for row in (a.get('paths') or []) + (a.get('targets') or []) for c in row.get('cite') or ())]
            for name in cited:
                if name not in sources:
                    raise StatementError(
                        f"{said(entry)}: cite names {name!r}, which is not a source you were given or have read in "
                        'this task. A source can be cited once you have it: read it first, or cite only the ones you have.')
        if kind == 'compound':
            need(entry, 'made_from', a.get('made_from'), compounds, 'compound'); need(entry, 'leads_to', a.get('leads_to'), compounds, 'compound')
        elif kind == 'step':
            need(entry, 'inputs', a.get('inputs'), compounds | intermediates, 'compound', (UNSTATED,))
            need(entry, 'outputs', a.get('outputs'), compounds | intermediates, 'compound', (UNSTATED,))
        elif kind in ('operation', 'material', 'condition', 'outcome', 'intermediate'):
            need(entry, 'step', [a.get('step')], steps, 'step')
            known = set(_operations(result, a.get('step')))
            if kind in ('material', 'condition') and a.get('operation'):
                need(entry, 'operation', [a['operation']], known, 'operation of that step')
            if kind == 'operation':
                need(entry, 'after', a.get('after'), known, 'operation of that step')
            if kind == 'outcome':
                need(entry, 'operations', a.get('operations'), known, 'operation of that step')
                need(entry, 'products', a.get('products'), compounds | intermediates, 'compound')
            if kind == 'material' and a.get('compound'):
                need(entry, 'compound', [a['compound']], compounds | intermediates, 'compound')
        elif kind == 'route':
            need(entry, 'steps', a.get('steps'), steps, 'step'); need(entry, 'compounds', a.get('compounds'), compounds, 'compound')
            for row in a.get('paths') or ():
                need(entry, 'paths/target', [row.get('target')], compounds, 'compound'); need(entry, 'paths/steps', row.get('steps'), steps, 'step')
            for row in a.get('targets') or ():
                need(entry, 'targets/target', [row.get('target')], compounds, 'compound')
        elif kind in ('characterization', 'biology'):
            need(entry, 'compounds', a.get('compounds'), compounds | intermediates, 'compound')
        elif kind in ('narrative', 'coverage', 'unresolved'):
            # The contract has no place for "about an intermediate": an intermediate lives
            # inside its step, so a statement about it is about that step.
            fields = {'about': a.get('about')} if kind != 'unresolved' else {
                'subject': [a.get('subject')] if a.get('scope') != 'PAPER' else [], 'also_about': a.get('also_about')}
            for field, names in fields.items():
                inside = next((n for n in names or () if n in intermediates), None)
                if inside is not None:
                    home = next(e['args']['step'] for e in result if e['kind'] == 'intermediate' and e['args']['name'] == inside)
                    raise StatementError(f"{said(entry)}: {field} names {inside!r}, an intermediate inside the step {home!r}. "
                                         f"A statement can be about a compound, a step or a route: name the step {home!r} instead.")
                need(entry, field, names, compounds | steps | routes, 'compound, step or route')
    return result, ids


def _cited(args, key='cite'):
    return {'citations': list(args[key])} if args.get(key) else {}


def _words(text):
    return [{'text': text}]


def assemble(entries):
    """The ordinary skeleton document these statements make, and where each entry landed."""
    kind_of = {e['args']['name']: e['kind'] for e in entries if e['kind'] in NAMED}
    type_of = {'compound': 'COMPOUND', 'intermediate': 'COMPOUND', 'step': 'REACTION_BLOCK', 'route': 'ROUTE'}
    document, where = {}, {}
    blocks, first_operation = {}, {}

    def subjects(names):
        return [{'object_type': type_of[kind_of[name]], 'object_id': name} for name in names]

    def operation(step, name=None):
        block = blocks[step]
        if name is None:
            return first_operation[step]
        return next(o for o in block['operations'] if o.get('label') == name)

    def ids(names):
        return {'claim': 'UNSTATED'} if list(names or ()) == [UNSTATED] else list(names or ())

    for entry in entries:
        a, kind = entry['args'], entry['kind']
        if kind == 'compound':
            row = {'label': a['name'], 'author_labels': list(a.get('author_labels') or ()), 'names': list(a.get('names') or ())}
            if a.get('note'): row['model_interpretations'] = [{'text': a['note']}]
            if 'made_from' in a: row['predecessor_ids'] = list(a['made_from'])
            if 'leads_to' in a: row['successor_ids'] = list(a['leads_to'])
            if a.get('wording'): row['source_statements'] = [{'statement': a['wording'], 'source_parts': _words(a['wording'])}]
            document.setdefault('compounds', []).append({**row, **_cited(a)})
        elif kind == 'step':
            made = {'description': a['description'], **_cited(a, 'description_cite' if a.get('description_cite') else 'cite')}
            if a.get('reaction_class'): made['reaction_class'] = a['reaction_class']
            if a.get('atmosphere'): made['atmosphere'] = a['atmosphere']
            block = {'label': a['name'], 'input_compound_ids': ids(a.get('inputs')), 'output_compound_ids': ids(a.get('outputs')),
                     'operations': [made], **_cited(a)}
            if a.get('summary'): block['summary'] = a['summary']
            if a.get('note'): block['model_interpretations'] = [{'text': a['note']}]
            blocks[a['name']], first_operation[a['name']] = block, made
            document.setdefault('reaction_blocks', []).append(block)
            where[('reaction_blocks', len(document['reaction_blocks']) - 1)] = entry['id']
    for entry in entries:
        a, kind = entry['args'], entry['kind']
        if kind == 'operation':
            block = blocks[a['step']]
            made = {'label': a['name'], 'description': a['description'], **_cited(a)}
            if a.get('reaction_class'): made['reaction_class'] = a['reaction_class']
            if a.get('atmosphere'): made['atmosphere'] = a['atmosphere']
            # A step that has named operations names its first one too, so order can refer to it.
            first_operation[a['step']].setdefault('label', a['step'] + ' / first operation')
            block['operations'].append(made)
            for before in a.get('after') or ():
                block.setdefault('operation_precedence', []).append({'before_operation_id': before, 'after_operation_id': a['name']})
    for entry in entries:
        a, kind = entry['args'], entry['kind']
        if kind == 'material':
            row = {'names': list(a['names'])}
            if a.get('role'): row['role'] = a['role']
            if a.get('compound'): row['compound_id'] = a['compound']
            if a.get('amount'): row['amount'] = scalar(a['amount'])
            if a.get('equivalents'): row['equivalents'] = scalar(a['equivalents'])
            if a.get('wording') or a.get('role_wording') or a.get('printed_name'):
                reading = {'identity_relation': 'SAME'}
                if a.get('role'): reading['role'] = a['role']
                if a.get('role_wording'): reading['role_verbatim'] = row['role_verbatim'] = a['role_wording']
                if a.get('wording'): reading['source_parts'] = _words(a['wording'])
                if a.get('printed_name'): reading['verbatim_name'] = a['printed_name']
                row['source_readings'] = [reading]
            operation(a['step'], a.get('operation')).setdefault('materials', []).append({**row, **_cited(a)})
        elif kind == 'condition':
            target, category = operation(a['step'], a.get('operation')), str(a['category']).strip().lower()
            value = scalar(a['quantity'])
            if category == 'temperature':
                target.setdefault('temperature_profile', []).append(value)
            elif category in ('duration', 'time') and 'duration' not in target:
                target['duration'] = value
            else:
                target.setdefault('other_conditions', []).append({'extent': 'WHOLE_OWNERS', 'category': a['category'], 'value': value})
        elif kind == 'outcome':
            block = blocks[a['step']]
            row = {'quantities': [{key: value for key, value in made.items() if key != 'basis' or value} | _cited(given)
                                  for made, given in zip(stated(a['quantities']), a['quantities'])]}
            if a.get('products'): row['product_compound_ids'] = list(a['products'])
            if a.get('operations'): row['operation_ids'] = list(a['operations'])
            if a.get('wording'):
                said = {'statement': a['wording'], 'source_parts': _words(a['wording'])}
                kinds = [q['kind'] for q in row['quantities'] if q.get('kind')]
                if kinds: said['kind'] = kinds[0]
                if a.get('products'): said['product_ids'] = list(a['products'])
                if a.get('product_label'): said['product_label'] = a['product_label']
                if a.get('kind_wording'): said['kind_verbatim'] = a['kind_wording']
                row['source_statements'] = [said]
            block.setdefault('reported_outcomes', []).append({**row, **_cited(a)})
        elif kind == 'intermediate':
            row = {'label': a['name']}
            if a.get('printed_name'): row['name'] = a['printed_name']
            if a.get('author_label'): row['author_label'] = a['author_label']
            if a.get('note'): row['model_interpretations'] = [{'text': a['note']}]
            if a.get('wording'): row['source_parts'] = _words(a['wording'])
            blocks[a['step']].setdefault('local_intermediates', []).append({**row, **_cited(a)})
        elif kind == 'route':
            row = {'label': a['name'], 'reaction_block_ids': list(a['steps'])}
            if 'compounds' in a: row['compound_ids'] = list(a['compounds'])
            if a.get('overview'): row['model_overview'] = a['overview']
            if a.get('note'): row['model_integration_note'] = a['note']
            for path in a.get('paths') or ():
                made = {'target_compound_id': path['target']}
                if 'steps' in path: made['reaction_block_ids'] = list(path['steps'])
                if path.get('step_count') is not None: made['reported_step_count'] = path['step_count']
                if path.get('step_count_basis'): made['reported_step_count_basis'] = path['step_count_basis']
                if path.get('overall_yield'): made['reported_overall_yield'] = scalar(path['overall_yield'])
                row.setdefault('target_paths', []).append({**made, **_cited(path)})
            for claim in a.get('targets') or ():
                made = {'target_compound_id': claim['target']}
                if claim.get('subject'): made['subject'] = claim['subject']
                if claim.get('outcome'): made['outcome'] = claim['outcome']
                if claim.get('wording'): made['author_statement'] = claim['wording']
                row.setdefault('target_assertions', []).append({**made, **_cited(claim)})
            document.setdefault('routes', []).append({**row, **_cited(a)})
        elif kind == 'narrative':
            row = {'kind': a['type'], 'title': a['title']}
            if a.get('kind_wording'): row['kind_verbatim'] = a['kind_wording']
            if a.get('wording'): row.update(author_text=a['wording'], source_parts=_words(a['wording']))
            if a.get('explanation'): row['explanation'] = a['explanation']
            if a.get('about'): row['subjects'] = subjects(a['about'])
            document.setdefault('paper_narratives', []).append({**row, **_cited(a)})
        elif kind == 'characterization':
            row = {'compound_ids': list(a['compounds'])}
            if a.get('method'): row['method'] = a['method']
            if a.get('method_wording'): row['method_verbatim'] = a['method_wording']
            if a.get('sample'): row['sample_context'] = a['sample']
            if a.get('accession_ids'): row['accession_ids'] = list(a['accession_ids'])
            if a.get('note'): row['model_interpretations'] = [{'text': a['note']}]
            if a.get('wording'):
                row['source_statements'] = [{'statement': a['wording'], 'source_parts': _words(a['wording']),
                                             **({'sample_context': a['sample']} if a.get('sample') else {})}]
            document.setdefault('characterization_records', []).append({**row, **_cited(a)})
        elif kind == 'biology':
            row = {'compound_ids': list(a['compounds']), 'endpoint': a['endpoint']}
            if a.get('system'): row['organism_or_system'] = a['system']
            if a.get('comparator'): row['comparator'] = a['comparator']
            if a.get('quantity'): row['reported_value'] = scalar(a['quantity'])
            document.setdefault('biological_findings', []).append({**row, **_cited(a)})
        elif kind == 'coverage':
            row = {'source_phase': a['phase'], 'status': a['status'], 'explanation': a['explanation']}
            if a.get('about'): row['subjects'] = list(a['about'])
            document.setdefault('coverage_assessments', []).append({**row, **_cited(a)})
        elif kind == 'unresolved':
            row = {'scope': a['scope'], 'reason_code': a['reason'], 'subject': a['subject'], 'statement': a['statement']}
            if a.get('also_about'): row['subjects'] = subjects([a['subject'], *a['also_about']] if a['scope'] != 'PAPER' else a['also_about'])
            document.setdefault('unresolved_items', []).append({**row, **_cited(a)})
        elif kind == 'completeness':
            document['completeness'] = a['value']
    return document, where
