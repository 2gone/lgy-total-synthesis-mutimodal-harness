"""A reaction block written as flat statements instead of one nested document.

The block task of the final compilation has its notes and skeleton in the first message
and only writes. One nested document of ten thousand characters is where whole kinds of
content were silently left out (2026-10-09: a general agent given the same task wrote
itself small helpers -- one material, one condition -- before writing anything). These
statements are those helpers, offered as the tool: each says one thing the note states.

This module turns the statements into the ordinary final document; the same compiler
and contract then decide whether it can be saved. Nothing here reads meaning: a value
that is not one plain number is kept as written, a kind outside the contract's names is
left unnamed, and a name that matches nothing is refused with the names that exist.
"""
import re
from copy import deepcopy

KINDS = ('ISOLATED_YIELD', 'REPORTED_YIELD', 'ASSAY_YIELD', 'NMR_YIELD', 'BRSM_YIELD', 'EE', 'DR', 'RATIO',
         'RECOVERED_STARTING_MATERIAL', 'SCALE', 'MASS', 'AMOUNT_OF_SUBSTANCE', 'VOLUME')
ROLES = ('ROUTE_SUBSTRATE', 'REAGENT', 'CATALYST', 'ADDITIVE', 'SOLVENT', 'QUENCH_WORKUP')
CLEARABLE = ('temperature_profile', 'duration', 'pressure', 'concentration', 'scale')
UNSTATED_STAGE = 'NOT_STATED'
_PLAIN = re.compile(r'^-?(0|[1-9][0-9]*)(\.[0-9]+)?$')
_SPAN = re.compile(r'^(-?\d+(?:\.\d+)?)\s?(?:–|—|-|to)\s?(-?\d+(?:\.\d+)?)$')
# "2 x 5 mL", "3 × 10": a count of equal portions and the size of one.
_PORTIONS = re.compile(r'^\(?\s*(\d+)\s*[x×]\s*(-?\d+(?:\.\d+)?)\s*([^\s\d)][^)]*)?\)?$')

_Q = {'type': 'object', 'description': 'A stated quantity as the note writes it.',
      'properties': {
          'value': {'type': 'string', 'description': 'One plain number, as written.'},
          'range': {'type': 'array', 'items': {'type': 'string'}, 'description': 'For a stated range: its two plain numbers, low then high.'},
          'unit': {'type': 'string', 'description': 'The unit as the note writes it, whatever it is: mL, mmol, M, mol%, drops, ...'},
          'times': {'type': 'string', 'description': 'For equal portions written as "2 x 5 mL": the count (2); value and unit are then one portion (5, mL). Do not multiply.'},
          'text': {'type': 'string', 'description': 'The value as the note writes it when it is not one number or a range: a ratio such as 3:1, a qualitative value.'}}}
_STATED = {'type': 'object', 'required': ['quantity', 'what'], 'properties': {
    'quantity': _Q, 'what': {'type': 'string', 'description': "In the note's words, what this quantity measures."},
    'kind': {'type': 'string', 'description': 'Only when one of these names it exactly: ' + ', '.join(KINDS) + '. For any other kind of quantity (a concentration, a purity, ...) omit kind and say what it is in what.'}}}
_OP = {'type': 'string', 'description': 'Offered name of the operation.'}
_REPLACE = {'type': 'string', 'description': 'The id of an earlier call that this one replaces.'}


def _statement(kind, description, properties, required):
    return {'type': 'object', 'description': description, 'required': ['kind', *required],
            'properties': {'kind': {'type': 'string', 'enum': [kind]}, **properties, 'replace': _REPLACE}}


STATEMENTS = {
    'stage': _statement('stage', 'A phase of an operation that the note distinguishes.', {
        'operation': _OP,
        'name': {'type': 'string', 'description': 'A short name you choose; other calls use it to place things on this stage.'},
        'description': {'type': 'string', 'description': 'What the note says happens in this phase.'},
        'after': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Names of the stages the note places directly before this one.'},
        'note': {'type': 'string', 'description': 'Anything else the note says about this phase, in its wording.'}},
        ['operation', 'name', 'description']),
    'material': _statement('material', 'A substance the note reports in an operation, with every quantity the note states for it.', {
        'operation': _OP,
        'existing': {'type': 'string', 'description': 'Offered name of a material already in the skeleton, to add to or correct it.'},
        'names': {'type': 'array', 'items': {'type': 'string'}, 'description': 'The names the note uses for it. Required for a material not in the skeleton. For an existing one, give them when the note names it differently or corrects what the skeleton calls it.'},
        'compound': {'type': 'string', 'description': 'Offered name of the compound this material is, when the note identifies it; also corrects the skeleton when it names the wrong compound.'},
        'role': {'type': 'string', 'enum': list(ROLES)},
        'stage': {'type': 'string', 'description': 'Name of the stage in which the note uses it. Omit if the note gives it for the whole operation.'},
        'amount': _Q, 'equivalents': _Q,
        'other_quantities': {'type': 'array', 'items': _STATED, 'description': 'Every further quantity the note states for this material: amount of substance, volume, concentration, purity, and so on. A concentration is a number with its unit like any other (value "2.5", unit "M").'},
        'note': {'type': 'string', 'description': 'Anything else the note says about this material, in its wording.'},
        'remove_existing': {'type': 'boolean', 'description': 'True, with existing and nothing else, when the note says this skeleton material is not part of the operation.'}},
        ['operation']),
    'condition': _statement('condition', 'A condition the note states: temperature, time, rate, pressure, concentration, technique, eluent, observation, or any other.', {
        'operation': _OP,
        'stage': {'type': 'string', 'description': f'Name of the stage it belongs to. Omit if the note gives it for the whole operation. Write {UNSTATED_STAGE} if the note does not say which.'},
        'about': {'type': 'string', 'description': 'What the condition is about, when the note says (for example the internal temperature, the eluent).'},
        'steps': {'type': 'array', 'description': 'One step for values the note gives together (a temperature and the time held at it are one step). Several steps when the note gives them in succession, in its order.',
                  'items': {'type': 'array', 'items': {'type': 'object', 'required': ['category', 'quantity'], 'properties': {
                      'category': {'type': 'string', 'description': 'What kind of value this is, e.g. temperature, duration.'}, 'quantity': _Q}}}}},
        ['operation', 'steps']),
    'outcome': _statement('outcome', 'A measured result the note reports: yields, amounts obtained, ratios, recovered material.', {
        'existing': {'type': 'string', 'description': 'Offered name of an outcome already in the skeleton, to add its quantities.'},
        'operations': {'type': 'array', 'items': {'type': 'string'}, 'description': 'For a new outcome: offered names of the operations it results from.'},
        'products': {'type': 'array', 'items': {'type': 'string'}, 'description': 'For a new outcome: offered names of the compounds it reports.'},
        'quantities': {'type': 'array', 'items': _STATED}, 'note': {'type': 'string'}},
        []),
    'operation': _statement('operation', 'Something the note states about a whole operation.', {
        'operation': _OP,
        'description': {'type': 'string', 'description': 'Replaces the skeleton description when the note corrects it.'},
        'atmosphere': {'type': 'string'},
        'after': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Offered names of the operations the note places directly before this one.'},
        'clear': {'type': 'array', 'items': {'type': 'string', 'enum': list(CLEARABLE)}, 'description': 'Whole-operation values of the skeleton that the note gives for one stage instead; write them on that stage and clear them here.'},
        'note': {'type': 'string', 'description': 'A statement of the note about this operation, kept in its wording.'}},
        ['operation']),
    'note': _statement('note', 'A statement of the note that has no other place: about the block as a whole, or a reported fact that belongs to no material, condition or outcome.', {
        'operation': {'type': 'string', 'description': 'Offered name of the operation it concerns. Omit for a statement about the whole block.'},
        'category': {'type': 'string', 'description': 'For a reported fact inside an operation: what kind of fact it is (for example one entry of a screening table).'},
        'text': {'type': 'string', 'description': "The statement, in the note's wording."}},
        ['text']),
    'unresolved': _statement('unresolved', 'Something the note states that corrects or conflicts with the skeleton outside this block: a compound, the route, another block.', {
        'scope': {'type': 'string', 'enum': ['COMPOUND', 'REACTION_BLOCK', 'ROUTE', 'PAPER']},
        'subject': {'type': 'string', 'description': 'Offered name of the object it concerns.'},
        'statement': {'type': 'string', 'description': 'What the note says.'}},
        ['scope', 'subject', 'statement']),
    'characterization': _statement('characterization', 'A characterization the note reports for a compound of this block.', {
        'compounds': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Offered names of the compounds.'},
        'method': {'type': 'string'}, 'statement': {'type': 'string', 'description': 'What the note reports.'}},
        ['compounds', 'statement']),
    'remove': {'type': 'object', 'description': 'Take back an earlier call.', 'required': ['kind', 'id'],
               'properties': {'kind': {'type': 'string', 'enum': ['remove']}, 'id': {'type': 'string'}}},
}

_ACCOUNT = {'type': 'string', 'description': 'Name of the account this belongs to (the name you gave it).'}


def _for_account(schema, *, drop=()):
    schema = deepcopy(schema)
    for key in drop:
        schema['properties'].pop(key, None)
    schema['properties']['operation'] = _ACCOUNT
    return schema


# The paper task writes what belongs to no reaction block. An experiment of that kind is an
# account; its stages, materials and conditions are the same statements, placed by its name.
PAPER_STATEMENTS = {
    'account': _statement('account', 'An experiment the notes report that belongs to no reaction block of the skeleton (for example the preparation of a starting material).', {
        'name': {'type': 'string', 'description': 'A short name you choose; other calls use it to place things in this account.'},
        'title': {'type': 'string', 'description': 'What the experiment is, in a few words.'},
        'description': {'type': 'string', 'description': 'What the note says is done.'},
        'atmosphere': {'type': 'string'},
        'inputs': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Offered names of the skeleton compounds it starts from, when they are in the skeleton.'},
        'outputs': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Offered names of the skeleton compounds it gives, when they are in the skeleton.'},
        'note': {'type': 'string', 'description': 'Anything else the note says about this experiment as a whole, in its wording.'}},
        ['name', 'description']),
    'stage': _for_account(STATEMENTS['stage']),
    'material': _for_account(STATEMENTS['material'], drop=('existing', 'remove_existing')),
    'condition': _for_account(STATEMENTS['condition']),
    'outcome': _statement('outcome', 'A measured result the notes report outside the reaction blocks: yields, amounts obtained, ratios.', {
        'account': {'type': 'string', 'description': 'Name of the account it results from. Omit for a result the notes tie to no experiment.'},
        'products': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Offered names of the skeleton compounds it reports, when they are in the skeleton.'},
        'quantities': {'type': 'array', 'items': _STATED}, 'note': {'type': 'string', 'description': "What the note says about this result, in its wording."}},
        ['quantities']),
    'characterization': STATEMENTS['characterization'],
    'unresolved': STATEMENTS['unresolved'],
    'completeness': _statement('completeness', 'Whether the notes say the paper\'s experimental record is complete. Also the one call of a task that finds nothing else to write.', {
        'value': {'type': 'string', 'enum': ['COMPLETE', 'PARTIAL']}}, ['value']),
    'remove': STATEMENTS['remove'],
}
PAPER_STATEMENTS['characterization'] = deepcopy(STATEMENTS['characterization'])
PAPER_STATEMENTS['characterization']['description'] = 'A characterization the notes report that no reaction-block task owns.'

TOOLS = tuple(STATEMENTS)
PAPER_TOOLS = tuple(PAPER_STATEMENTS)


def statement_kinds(paper=False):
    return PAPER_STATEMENTS if paper else STATEMENTS


def declarations(paper=False):
    """One function per kind of statement.

    Each kind keeps its own parameter table in front of the model. One tool taking a list of
    statements of any kind was tried in production on 2026-10-09: about half of its calls
    were refused, because the model guessed at shapes it could no longer see one by one.
    """
    out = []
    for kind, schema in statement_kinds(paper).items():
        properties = {key: value for key, value in schema['properties'].items() if key != 'kind'}
        out.append({'type': 'function', 'name': kind, 'description': schema['description'],
                    'parameters': {'type': 'object', 'properties': deepcopy(properties),
                                   'required': [key for key in schema['required'] if key != 'kind']}})
    return out


def statements_of(calls):
    """The write calls of one reply as statements, in the order the reply gives them."""
    return [{**(arguments if isinstance(arguments, dict) else {}), 'kind': name} for name, arguments in calls]


def scalar(quantity):
    """The contract's scalar. A value that is not one plain number is never repaired into
    one: a written range becomes the range form, anything else is kept as text."""
    if not isinstance(quantity, dict):
        return {'text': str(quantity)}
    count, one, one_unit = portions(quantity)
    if count is not None:
        # Both numbers stay as written and neither is multiplied: the typed value is one
        # portion, and the written form, count included, is kept beside it. Leaving
        # "2 x 5 mL" as text only lost the 5 mL from every result that had one.
        return {'value': one, **({'unit': one_unit} if one_unit else {}),
                'text': quantity.get('text') if _PORTIONS.match(str(quantity.get('text') or '').strip()) else f"{count} x {one}" + (f' {one_unit}' if one_unit else '')}
    out, unit, text = {}, quantity.get('unit'), quantity.get('text')
    value = str(quantity['value']).strip().replace('−', '-') if quantity.get('value') not in (None, '') else None
    span = quantity.get('range')
    span = [str(v).strip().replace('−', '-') for v in span] if isinstance(span, list) and len(span) == 2 else None
    if span is None and value is not None and not _PLAIN.match(value) and _SPAN.match(value):
        span, value = list(_SPAN.match(value).groups()), None
    if span is not None and all(_PLAIN.match(v) for v in span):
        if float(span[0]) > float(span[1]):
            # "from 0 °C to -78 °C": the contract's range runs low to high, so the bounds are
            # put in that order and the direction the note wrote stays in the text.
            text, span = text or f"{span[0]} to {span[1]}" + (f' {unit}' if unit else ''), [span[1], span[0]]
        out['range'] = span
    elif value is not None and _PLAIN.match(value):
        out['value'] = value
    elif value is not None or span is not None:
        written = value if value is not None else ' – '.join(span)
        text, unit = text or (written + (' ' + unit if unit else '')), None
    if unit and ('value' in out or 'range' in out):
        out['unit'] = unit
    if text:
        out['text'] = text
    return out or {'text': ''}


def portions(quantity):
    """(count, size of one portion, unit) when a quantity is written as equal portions, else Nones.

    Said with `times`, or written in the fixed form "2 x 5 mL" in value or text. Reading that
    form is not arithmetic: nothing is multiplied.
    """
    if not isinstance(quantity, dict):
        return None, None, None
    value = str(quantity.get('value') or '').strip().replace('−', '-')
    times = str(quantity.get('times') or '').strip()
    if times.isdigit() and _PLAIN.match(value):
        return times, value, quantity.get('unit')
    for written in (value, str(quantity.get('text') or '').strip() if not value else ''):
        found = _PORTIONS.match(written) if written else None
        if found:
            return found.group(1), found.group(2), (found.group(3) or quantity.get('unit') or '').strip() or None
    return None, None, None


def portion_count(quantity):
    """The count of equal portions as a stated quantity of its own, or None."""
    count, one, unit = portions(quantity)
    if count is None:
        return None
    return {'reported_value': {'value': count}, 'basis': 'number of equal portions of ' + one + (f' {unit}' if unit else '') + ' each'}


def stated(items):
    out = []
    for item in items or ():
        kind = re.sub(r'[\s-]+', '_', str(item.get('kind') or '').strip().upper())
        out.append({'reported_value': scalar(item.get('quantity')), 'basis': item.get('what') or '',
                    **({'kind': kind} if kind in KINDS else {})})
    return out


class StatementError(ValueError):
    """A statement that cannot be placed; says which one and what names exist."""


_TYPES = {'object': dict, 'array': list, 'string': str, 'boolean': bool}


def _misshapen(value, schema, path):
    """Where a call's arguments leave the shape its function declares, or None."""
    expected = schema.get('type')
    if expected == 'string' and isinstance(value, (int, float)) and not isinstance(value, bool):
        return None  # a bare number where its written form was asked for; scalar() keeps it as written
    if expected in _TYPES and not isinstance(value, _TYPES[expected]):
        return f"{path or 'the arguments'} must be {'an' if expected[0] in 'ao' else 'a'} {expected}, not {type(value).__name__ if not isinstance(value, list) else 'a list'}"
    if 'enum' in schema and value not in schema['enum']:
        return f"{path} must be one of: " + ', '.join(schema['enum'])
    if expected == 'object' and str(schema.get('description', '')).startswith('A stated quantity') and not any(
            value.get(key) not in (None, '', []) for key in ('value', 'range', 'text')):
        return f"{path} states no quantity: give a value with its unit, a range, or text -- or leave {path.rsplit('/', 1)[-1]} out"
    if expected == 'object':
        for key in schema.get('required', ()):
            if key != 'kind' and value.get(key) in (None, '', []):
                return f"{path + '/' if path else ''}{key} is needed"
        for key, child in schema.get('properties', {}).items():
            if key in value and value[key] is not None:
                found = _misshapen(value[key], child, f"{path + '/' if path else ''}{key}")
                if found:
                    return found
    if expected == 'array':
        for index, member in enumerate(value):
            found = _misshapen(member, schema.get('items', {}), f'{path}/{index}')
            if found:
                return found
    return None


def _steps(steps):
    """A step is the values given together; a list wrapped around them inside a step adds nothing."""
    def flat(step):
        for member in step if isinstance(step, list) else [step]:
            if isinstance(member, list):
                yield from flat(member)
            else:
                yield member
    return [list(flat(step)) for step in steps] if isinstance(steps, list) else steps


def compound_aliases(skeleton):
    """What the skeleton itself calls each compound, mapped to its offered name.

    A note says "enone 13"; the skeleton offers that compound as "compounds 6" and lists
    "enone 13" and "13" as its names. A call that names the compound the way the note and
    the skeleton do means that compound; only a name two compounds share is left alone.
    """
    found, shared = {}, set()
    for compound in (skeleton or {}).get('compounds') or ():
        offered = compound.get('existing')
        if not offered:
            continue
        for name in [*(compound.get('names') or ()), *(compound.get('author_labels') or ())]:
            key = str(name).strip().casefold()
            if key and found.setdefault(key, offered) != offered:
                shared.add(key)
    return {key: offered for key, offered in found.items() if key not in shared}


_COMPOUND_FIELDS = {'material': ('compound',), 'outcome': ('products',), 'characterization': ('compounds',), 'account': ('inputs', 'outputs')}


def _offered(args, kind, aliases):
    def one(name):
        return aliases.get(name.strip().casefold(), name) if isinstance(name, str) else name
    for field in _COMPOUND_FIELDS.get(kind, ()):
        value = args.get(field)
        if isinstance(value, list):
            args[field] = [one(name) for name in value]
        elif isinstance(value, str):
            args[field] = one(value)
    if kind == 'unresolved' and args.get('scope') == 'COMPOUND':
        args['subject'] = one(args.get('subject'))


def accept(entries, statements, *, operations, aliases=None):
    """Earlier entries plus these statements, each with its id; nothing is changed in place.

    The statements of one reply are taken together, so one may name a stage that a later one
    of the same reply declares.
    """
    paper = operations is None
    kinds = statement_kinds(paper)
    result, ids = deepcopy(list(entries)), []
    if paper:
        operations = [e['args']['name'] for e in result if e['kind'] == 'account'] + [
            s.get('name') for s in statements if isinstance(s, dict) and s.get('kind') == 'account']
    counter = max((int(e['id'][1:]) for e in result), default=0)
    stages = lambda operation: {e['args']['name'] for e in result if e['kind'] == 'stage' and e['args'].get('operation') == operation}
    pending = [s for s in statements if isinstance(s, dict) and s.get('kind') == 'stage']
    for index, raw in enumerate(statements):
        if not isinstance(raw, dict) or raw.get('kind') not in kinds:
            raise StatementError(f'call {index + 1} of this reply: kind must be one of ' + ', '.join(kinds))
        args = {k: v for k, v in raw.items() if k not in ('kind', 'replace')}
        kind = raw['kind']
        if kind == 'remove':
            kept = [e for e in result if e['id'] != args.get('id')]
            if len(kept) == len(result):
                raise StatementError(f"call {index + 1} of this reply: no saved call has the id {args.get('id')!r}")
            result, _ = kept, ids.append(None)
            continue
        if kind == 'condition':
            args['steps'] = _steps(args.get('steps'))
        if aliases:
            _offered(args, kind, aliases)
        wrong = _misshapen({**args, 'kind': kind}, kinds[kind], '')
        if wrong:
            raise StatementError(f'call {index + 1} of this reply ({kind}): {wrong}')
        amount = args.get('amount') if kind == 'material' else None
        if isinstance(amount, dict) and (amount.get('value') not in (None, '') or amount.get('range')) and not amount.get('unit'):
            raise StatementError(f'call {index + 1} of this reply (material): amount gives a number without its unit; '
                                 'write the unit the note gives (equivalents go in equivalents).')
        if kind == 'material' and args.get('existing') and not raw.get('replace'):
            earlier = next((e['id'] for e in result if e['kind'] == 'material' and e['args'].get('existing') == args['existing']
                            and e['args'].get('operation') == args.get('operation')), None)
            if earlier:
                raise StatementError(f'call {index + 1} of this reply (material): call {earlier} already writes the existing material '
                                     f'{args["existing"]!r}. Say everything about it in one call: repeat it with replace: {earlier!r}. '
                                     'A second use of the same substance in another stage is a new material with names, not existing.')
        if kind in ('stage', 'material', 'condition') and not args.get('operation'):
            raise StatementError(f'call {index + 1} of this reply ({kind}): operation is needed')
        placed = args.get('account') if paper and kind == 'outcome' else args.get('operation')
        if placed is not None and placed not in operations:
            raise StatementError(f'call {index + 1} of this reply ({kind}): ' + (
                f'no account is named {placed!r}. The accounts are: ' + ('; '.join(operations) or '(none yet: declare it with account)') if paper else
                f'no operation of this block is named {placed!r}. Its operations are: ' + '; '.join(operations)))
        if kind in ('material', 'condition') and args.get('stage') and args['stage'] != UNSTATED_STAGE:
            known = stages(args['operation']) | {s.get('name') for s in pending if s.get('operation') == args['operation']}
            if args['stage'] not in known:
                raise StatementError(f'call {index + 1} of this reply ({kind}): this operation has no stage named {args["stage"]!r}. Its stages are: ' + ('; '.join(sorted(known)) or '(none yet)'))
        replaced = raw.get('replace')
        target = next((e for e in result if e['id'] == replaced), None) if replaced else None
        if replaced and target is None:
            raise StatementError(f'call {index + 1} of this reply ({kind}): no saved call has the id {replaced!r}')
        same = None if replaced else next((e for e in result if e['kind'] == kind and e['args'] == args), None)
        if same is not None:
            # Said again word for word: it is already held, and saying it twice adds nothing.
            ids.append(same['id'])
            continue
        if target is not None:
            target.update(kind=kind, args=args); ids.append(target['id'])
        else:
            counter += 1
            result.append({'id': f'{kind[0]}{counter}', 'kind': kind, 'args': args}); ids.append(result[-1]['id'])
    return result, ids


def assemble(entries, *, assigned, operations):
    """The ordinary final document for the assigned block, and where each entry landed.

    With no assigned block this is the paper task: each account is an experiment outside the
    reaction blocks, holding one operation that its stages, materials and conditions belong to.
    """
    built, where, outcomes, extra = {}, {}, [], {}
    paper = assigned is None
    accounts, account_outcomes = {}, {}
    if paper:
        operations = [e['args']['name'] for e in entries if e['kind'] == 'account']
    several = len(operations) > 1

    def op(name):
        return built.setdefault(name, {'label': name + ' / operation'} if paper else {'existing': name})

    def label(operation, stage):
        # A stage name is the model's, local to its operation; the document needs it unique in the block.
        if paper:
            return f'{operation}: {stage}' if several else stage
        return f"{operation.rsplit(' ', 1)[-1]}. {stage}" if several else stage

    block = {'existing': assigned}
    for entry in entries:
        a, kind = entry['args'], entry['kind']
        if kind == 'stage':
            o = op(a['operation'])
            stage = {'label': label(a['operation'], a['name']), 'description': a['description']}
            if a.get('note'):
                stage['model_interpretations'] = [{'text': a['note']}]
            o.setdefault('stages', []).append(stage)
            where[('stages', a['operation'], len(o['stages']) - 1)] = entry['id']
            for before in a.get('after') or ():
                o.setdefault('stage_precedence', []).append({'before_stage_id': label(a['operation'], before), 'after_stage_id': stage['label']})
        elif kind == 'material':
            o = op(a['operation'])
            if a.get('remove_existing'):
                material = {'existing': a.get('existing'), 'remove': True}
            else:
                names = [n for n in a.get('names') or () if n]
                material = {'existing': a['existing'], **({'names': names} if names else {})} if a.get('existing') else {'names': names}
                if not a.get('existing') and not names:
                    raise StatementError(f"call {entry['id']} (material): a material not in the skeleton needs names")
                if a.get('compound'): material['compound_id'] = a['compound']
                if a.get('role'): material['role'] = a['role']
                if a.get('amount'): material['amount'] = scalar(a['amount'])
                if a.get('equivalents'): material['equivalents'] = scalar(a['equivalents'])
                if a.get('stage'): material.update(extent='WITHIN_OWNER', stage_id=label(a['operation'], a['stage']))
                more = [*stated(a.get('other_quantities')), *filter(None, [portion_count(a.get('amount'))])]
                if more: material['additional_reported_quantities'] = more
                if a.get('note'): material['model_interpretations'] = [{'text': a['note']}]
            o.setdefault('materials', []).append(material)
            where[('materials', a['operation'], len(o['materials']) - 1)] = entry['id']
        elif kind == 'condition':
            o, steps = op(a['operation']), a['steps']
            if a.get('stage') == UNSTATED_STAGE:
                condition = {'extent': 'UNRESOLVED'}
            elif a.get('stage'):
                condition = {'extent': 'WITHIN_OWNER', 'stage_id': label(a['operation'], a['stage'])}
            else:
                condition = {'extent': 'WHOLE_OWNERS'}
            if a.get('about'): condition['about'] = a['about']
            if len(steps) == 1 and len(steps[0]) == 1:
                condition.update(category=steps[0][0]['category'], value=scalar(steps[0][0]['quantity']))
            else:
                condition['sequence'] = [{'values': [{'category': v['category'], 'typed_value': scalar(v['quantity'])} for v in step]} for step in steps]
                if len(steps) > 1: condition['value_relation'] = 'ORDERED_POINTS'
            o.setdefault('other_conditions', []).append(condition)
            where[('other_conditions', a['operation'], len(o['other_conditions']) - 1)] = entry['id']
        elif kind == 'operation':
            o = op(a['operation'])
            if a.get('description'): o['description'] = a['description']
            if a.get('atmosphere'): o['atmosphere'] = a['atmosphere']
            for field in a.get('clear') or ():
                if field in CLEARABLE: o[field] = {'claim': 'UNSTATED'}
            if a.get('note'): o.setdefault('model_interpretations', []).append({'text': a['note']})
            for before in a.get('after') or ():
                block.setdefault('operation_precedence', []).append({'before_operation_id': before, 'after_operation_id': a['operation']})
        elif kind == 'note':
            if a.get('operation') and a.get('category'):
                op(a['operation']).setdefault('unassigned_facts', []).append({'category': a['category'], 'model_interpretations': [{'text': a['text']}]})
            elif a.get('operation'):
                op(a['operation']).setdefault('model_interpretations', []).append({'text': a['text']})
            else:
                block.setdefault('model_interpretations', []).append({'text': a['text']})
        elif kind == 'account':
            account = accounts.setdefault(a['name'], {'label': a['name']})
            if a.get('title'): account['title'] = a['title']
            # The contract wants the account itself stated, not only its operation.
            account['model_interpretations'] = [{'text': a.get('note') or a['description']}]
            if a.get('inputs'): account['input_compound_ids'] = list(a['inputs'])
            if a.get('outputs'): account['output_compound_ids'] = list(a['outputs'])
            o = op(a['name'])
            o['description'] = a['description']
            if a.get('atmosphere'): o['atmosphere'] = a['atmosphere']
        elif kind == 'completeness':
            extra['completeness'] = a['value']
        elif kind == 'outcome' and paper:
            outcome = {'quantities': stated(a['quantities'])}
            if a.get('products'): outcome['product_compound_ids'] = list(a['products'])
            if a.get('note'): outcome['model_interpretations'] = [{'text': a['note']}]
            if a.get('account'):
                outcome['operation_ids'] = [op(a['account'])['label']]
                account_outcomes.setdefault(a['account'], []).append(outcome)
            else:
                extra.setdefault('unassigned_outcomes', []).append(outcome)
        elif kind == 'outcome':
            outcome = {'existing': a['existing']} if a.get('existing') else {}
            if not a.get('existing'):
                if a.get('operations'): outcome['operation_ids'] = list(a['operations'])
                if a.get('products'): outcome['product_compound_ids'] = list(a['products'])
            if a.get('quantities'): outcome['quantities'] = stated(a['quantities'])
            if a.get('note'): outcome['model_interpretations'] = [{'text': a['note']}]
            outcomes.append(outcome); where[('reported_outcomes', None, len(outcomes) - 1)] = entry['id']
        elif kind == 'unresolved':
            extra.setdefault('unresolved_items', []).append({'scope': a['scope'], 'reason_code': 'CONFLICTING_CANDIDATES', 'subject': a['subject'], 'statement': a['statement']})
        elif kind == 'characterization':
            extra.setdefault('characterization_records', []).append({
                'compound_ids': list(a['compounds']), **({'method': a['method']} if a.get('method') else {}),
                'model_interpretations': [{'text': a['statement']}]})
    order = list(built)
    if paper:
        rows = [{**accounts.get(name, {'label': name}), 'operations': [built[name]],
                 **({'reported_outcomes': account_outcomes[name]} if name in account_outcomes else {})} for name in order]
        return {**({'experimental_accounts': rows} if rows else {}), **extra}, where, order
    if built: block['operations'] = [built[name] for name in order]
    if outcomes: block['reported_outcomes'] = outcomes
    return {'reaction_blocks': [block], **extra}, where, order


def locate(message, where, order):
    """Add, to the Host's reason, which statement each document position came from."""
    def position(match):
        index = int(match.group(1))
        found = where.get((match.group(2), order[index] if index < len(order) else None, int(match.group(3))))
        return match.group(0) + (f' (your call {found})' if found else '')
    message = re.sub(r"operations/(\d+)/(materials|other_conditions|stages)/(\d+)", position, message)
    message = re.sub(r"\('reaction_blocks', \d+, 'operations', (\d+), '(materials|other_conditions|stages)', (\d+)", position, message)
    return re.sub(r'reported_outcomes/(\d+)', lambda m: m.group(0) + (f" (your call {where[('reported_outcomes', None, int(m.group(1)))]})" if ('reported_outcomes', None, int(m.group(1))) in where else ''), message)
