"""Host side of recording by reference: every quantity the extraction rule finds in an entry gets a mark at its place in the text, the
model refers to marks instead of retyping numbers, and the record is reconciled by set arithmetic over marks, never by comparing strings.

The extraction is the one that drafted the reviewed fact sheets (build_review_pack.py); `mark_entry` recomputes it and refuses to go on
unless it reproduces the sheet row for row, so a mark, a sheet row and a place in the parse are always the same thing.
No model is called here."""
import importlib.util, json, re
from pathlib import Path

_PACK = Path(__file__).resolve().parents[2] / 'fullscore-handoff-4papers-20261011/tools/build_review_pack.py'


def _rule():
    """The extraction rule itself, read from the script that drafted the sheets (its definitions only, nothing of it is run)."""
    source = _PACK.read_text(encoding='utf-8')
    head = source.split('_TEX = ')[0]; body = '_TEX = ' + source.split('_TEX = ')[1].split('\ndef write(')[0]
    assert 'QUANTITY = re.compile' in body and 'def text_of' in body and 'DATA = re.compile' in body, 'the extraction rule moved'
    scope = {'re': re}; exec(body, scope)
    return scope


_R = _rule()
plain, text_of, QUANTITY, WORDS, DATA = _R['plain'], _R['text_of'], _R['QUANTITY'], _R['WORDS'], _R['DATA']

# Two kinds of quantity the drafting rule does not know, marked by the host as well: a ratio (of solvents, of isomers) and a pH. Their
# marks are the host's own and stand for no row of the drafted sheet. A ratio is numbers joined by colons; a gradient of ratios
# ("40:1 -> 20:1", "from 40:1 to PE/EtOAc = 20:1") is one quantity, the eluent changing, and gets one mark from its first ratio to its
# last; a volume basis printed right after it ("v/v") belongs to it.
_RATIO_ONE = r'(?<![\w.:/])\d+(?:\.\d+)?(?:[ \t]?:[ \t]?\d+(?:\.\d+)?)+(?![\w:]|\.\d)'
_STEP = r'[ \t]*(?:→|->|⟶|\\rightarrow|\\longrightarrow|\\to\b|\bto\b|then)[ \t]*(?:[A-Za-z][\w/+\-]*(?:[ \t]?/[ \t]?[\w+\-]+)*[ \t]*=?[ \t]*)?'
RATIO = re.compile(_RATIO_ONE + '(?:' + _STEP + _RATIO_ONE + ')*' + r'(?:,?[ \t]?(?:\([vV][ \t]?/[ \t]?[vV]\)|[vV][ \t]?/[ \t]?[vV]\b))?')
PH = re.compile(r'\bpH[ \t]*(?:=|≈|~|of|to|at|value of)?[ \t]*\d+(?:\.\d+)?(?:[ \t]?[-–][ \t]?\d+(?:\.\d+)?)?(?!\w|\.\d)')


# Spellings of units the drafting rule does not know (it reads mL, µL, h, min, equiv, wt% only so written), marked by the host too
# with the unit in its usual form. A lone "l" is not among them: after a number it is most often a garbled "1" in a formula or a
# compound label ("2l"), not a litre.
_SPELT = {'ml': 'mL', 'µl': 'µL', 'μl': 'µL', 'hr': 'h', 'hrs': 'h', 'mins': 'min', 'equivalent': 'equiv', 'equivalents': 'equiv', 'wt %': 'wt%',
          'torr': 'Torr', 'mmhg': 'mmHg', 'kpa': 'kPa', 'mpa': 'MPa'}
SPELT = re.compile(r'(?<![\w.])(-\s?)?(\d+(?:\.\d+)?)\s?(ml|[µμ]l|hrs?|mins|equivalents?|wt[ \t]%|Torr|mmHg|kPa|MPa)(?![A-Za-z])')
# "5 mol %": the drafting rule reads "5 mol"; the per cent sign printed after it is part of the quantity
_MOL_PER_CENT = re.compile(r'[ \t]?%')


def extract(row, own=False):
    """(procedure text, [(start, end, raw, value, unit, own, drafted)]) of one parsed block: the quantities of the drafting rule, exactly as
    the sheet was drafted, and with own=True also those the host marks itself where no quantity of the drafting rule stands -- ratios
    (unit RATIO), pH values (pH) and the unit spellings above (own=True) -- and a drafted "mol" completed by its per cent sign (its
    drafted raw and unit kept in drafted, which is what the sheet holds)."""
    text = text_of(row)
    cut = DATA.search(text) if row['type'] not in ('table',) else None
    procedure = text[:cut.start()] if cut and cut.start() > 80 else text
    if row['type'] == 'table': return text, len(procedure), []
    found = [(m.start(), m.group(0), ((m.group(1) or '') + m.group(2)).replace(' ', ''), m.group(3).rstrip('.'), False, None) for m in QUANTITY.finditer(procedure)]
    found += [(m.start(), m.group(0), '', 'WORDS', False, None) for m in WORDS.finditer(procedure)]
    if own:
        for k, (at, raw, value, unit, _, _) in enumerate(found):
            more = _MOL_PER_CENT.match(procedure, at + len(raw)) if unit == 'mol' else None
            if more: found[k] = (at, raw + more.group(0), value, 'mol%', False, {'raw': raw, 'unit': unit})
        taken = [(at, at + len(raw)) for at, raw, *_ in found]
        def add(m, value, unit):
            if not any(m.start() < b and a < m.end() for a, b in taken): found.append((m.start(), m.group(0), value, unit, True, None)); taken.append((m.start(), m.end()))
        for m in SPELT.finditer(procedure): add(m, ((m.group(1) or '') + m.group(2)).replace(' ', ''), _SPELT[re.sub(r'[ \t]+', ' ', m.group(3)).lower()])
        for kind, pattern in (('pH', PH), ('RATIO', RATIO)):
            for m in pattern.finditer(procedure): add(m, re.sub(r'\s', '', m.group(0)), kind)
    return text, len(procedure), [(at, at + len(raw), raw, value, unit, mine, drafted) for at, raw, value, unit, mine, drafted in sorted(found, key=lambda f: f[0])]


def mark_entry(block_ids, blocks, sheet_rows):
    """The entry's text with a mark after every extracted quantity, and what each mark stands for.

    Returns (texts, marks): texts maps block_id -> {'plain': the block's text, 'marked': the same with marks, 'procedure_end': where its
    data begins}; marks is a list in reading order of {'ref', 'row', 'block_id', 'start', 'end', 'raw', 'value', 'unit', 'context'}."""
    texts, marks = {}, []
    for block_id in block_ids:
        text, procedure_end, found = extract(blocks[block_id], own=True)
        pieces, last = [], 0
        for start, end, raw, value, unit, mine, drafted in found:
            ref = f'q{len(marks) + 1}'
            # A digit or a sign standing apart just before the number: the parse may have broken one number in two ("- 7 8 °C" gives the
            # rule "8 °C"). The host cannot tell that from a label followed by a quantity, so it does not repair; it marks the place as open
            # and the record must state the complete wording there.
            open_ = unit not in ('WORDS', 'RATIO', 'pH') and bool(re.search(r'(?:\d|[-−–]|\d[ \t]?\.)[ \t]{1,2}$', text[:start]))
            marks.append({'ref': ref, 'block_id': block_id, 'start': start, 'end': end, 'raw': raw, 'value': value, 'unit': unit, 'open': open_, 'own': mine or unit in ('RATIO', 'pH'), **({'drafted': drafted} if drafted else {}),
                          'context': text[max(0, start - 70):end + 40].replace('\n', ' ')})
            # Marks go in at the END of each quantity; two finds that overlap keep their own ends, so the text between is never repeated.
            cut = max(end, last); pieces.append(text[last:cut] + '{' + ref + ('?' if open_ else '') + '}'); last = cut
        texts[block_id] = {'plain': text, 'marked': ''.join(pieces) + text[last:], 'procedure_end': procedure_end}
    drafted = [m for m in marks if not m['own']]
    as_drafted = lambda m: (m['block_id'], (m.get('drafted') or m)['raw'], m['value'], (m.get('drafted') or m)['unit'])
    if len(drafted) != len(sheet_rows) or any(as_drafted(m) != (r['block_id'], r['raw'], r['value'], r['unit']) for m, r in zip(drafted, sheet_rows)):
        raise AssertionError(f'the marks do not reproduce the sheet: {len(drafted)} marks, {len(sheet_rows)} sheet rows')
    for mark in marks: mark['row'] = None
    for mark, row in zip(drafted, sheet_rows): mark['row'] = row['row']
    for block_id, t in texts.items():       # removing the marks must give back the text exactly
        assert re.sub(r'\{q\d+\??\}', '', t['marked']) == t['plain'], block_id
    return texts, marks


def tidy(text):
    r"""A wording copied from a badly parsed passage, read as it was printed: typesetting commands dropped, a degree sign set as a raised
    "o" restored, and characters the parse spaced out one by one closed up ("0 . 2 e q ." -> "0.2 eq.", "- 7 8 ^ \mathrm o C" -> "-78 °C").
    Only spacing and markup change; no digit is added, removed or reordered."""
    text = re.sub(r'\\(?:rightarrow|to|longrightarrow)\b|->|⟶|➝', '→', text or '').replace('\\pm', '±').replace('\\cdot', '·').replace('\\circ', '°').replace('\\times', '×').replace('\\mu', 'µ').replace('\\%', '%').replace('℃', '°C')
    text = re.sub(r'\\[A-Za-z]+|[\\{}^_]', ' ', text)
    text = re.sub(r'(?<=\d)\s*(?:°|º|∘|o|O)\s*C\b', ' °C', text)
    tokens = text.split()
    out = []
    for token in tokens:       # a run of single characters is one word the parse pulled apart
        if out and len(token) == 1 and out[-1][1] and (token.isalnum() or token in '.,%:×°') : out[-1] = (out[-1][0] + token, True)
        else: out.append((token, len(token) == 1 and (token.isalnum() or token in '-−–.')))
    text = ' '.join(t for t, _ in out)
    text = re.sub(r'([-−–])\s+(?=\d)', r'\1', text)
    return re.sub(r'(\d)(°C)', r'\1 \2', re.sub(r'\s+', ' ', text)).strip()


def squeeze(text):
    """For asking whether a wording is printed in a text: markup, spacing, dash and micro variants do not count."""
    return re.sub(r'\s+', '', plain(tidy(text))).lower().replace('µ', 'u').replace('x', '×').replace('μ', 'u')


def parse_quantity(raw):
    """(value, unit) when the wording is a number with a unit the rule knows, else (None, None)."""
    closed = re.sub(r'(?<=\d)\s?([mµ])l\b', r' \1L', plain(tidy(raw)))
    m = QUANTITY.search(closed) or QUANTITY.search(plain(raw or ''))
    return (((m.group(1) or '') + m.group(2)).replace(' ', ''), m.group(3).rstrip('.')) if m else (None, None)


def quantities_of(node, path=()):
    """Every quantity object of a record (a dict that has 'ref' and 'printed_in'), with the path to it."""
    # a ref is compared as the bare mark name: the model may copy it with its braces or its question mark
    if isinstance(node, dict):
        if 'ref' in node and 'printed_in' in node: yield path, node
        for key, value in node.items(): yield from quantities_of(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node): yield from quantities_of(value, path + (index,))


def reconcile(record, texts, marks, scheme_text=''):
    """What the record does with the marks and whether its own wordings are printed in the entry.

    Returns {'placed': {ref: [paths]}, 'set_aside': {ref: reason}, 'unplaced': [ref], 'repeated': [ref], 'unknown': [ref],
    'not_printed': [raw], 'problems': n}. A mark is right when it is placed exactly once or set aside (never both); a wording the model
    wrote itself is right when the entry prints it (its text, or its scheme as transcribed)."""
    known = {m['ref'] for m in marks}; open_marks = {m['ref'] for m in marks if m.get('open')}; placed, unknown, not_printed, no_wording = {}, [], [], []
    body = squeeze(' '.join(t['plain'] for t in texts.values())); scheme = squeeze(scheme_text)
    for path, q in quantities_of(record.get('blocks') or []):
        ref, raw = (q.get('ref') or '').strip().strip('{}').rstrip('?') or None, q.get('raw')
        if ref:
            if ref in known: placed.setdefault(ref, []).append(path)
            else: unknown.append(ref)
            if ref in open_marks and not (raw or '').strip(): no_wording.append(ref)
        if raw and squeeze(raw) and squeeze(raw) not in body and squeeze(raw) not in scheme: not_printed.append(raw)
        if not ref and not (raw or '').strip(): not_printed.append('(a quantity with neither a mark nor a wording)')
    set_aside = {}
    for item in record.get('not_recorded') or []:
        ref = item['ref'].strip().strip('{}').rstrip('?')
        if ref in known: set_aside[ref] = item['reason']
        else: unknown.append(ref)
    repeated = sorted({r for r, paths in placed.items() if len(paths) > 1} | (set(placed) & set(set_aside)), key=lambda r: int(r[1:]))
    unplaced = [m['ref'] for m in marks if m['ref'] not in placed and m['ref'] not in set_aside]
    return {'placed': placed, 'set_aside': set_aside, 'unplaced': unplaced, 'repeated': repeated, 'unknown': sorted(set(unknown)), 'not_printed': not_printed, 'no_wording': no_wording,
            'problems': len(unplaced) + len(repeated) + len(set(unknown)) + len(not_printed) + len(no_wording)}


def hand_back_note(found, marks):
    """What the model is told when the record does not close over the marks. Only facts the host can state without reading the chemistry."""
    by_ref = {m['ref']: m for m in marks}; said = lambda ref: f'{{{ref}}} "{by_ref[ref]["raw"]}" in: ...{by_ref[ref]["context"]}...'
    lines = ['The system checked the record against the marks of the entry. Return the complete record again with only these points changed; everything else in it was accepted and must stay as it is.']
    if found['unplaced']: lines += ['', 'Marks that appear nowhere (place each where its quantity belongs, or list it under not_recorded with the reason):'] + ['- ' + said(r) for r in found['unplaced']]
    if found['repeated']: lines += ['', 'Marks used more than once (each mark is one place in the text and belongs to one place in the record; a quantity printed twice has two marks):'] + ['- ' + said(r) for r in found['repeated']]
    if found['unknown']: lines += ['', 'Marks that do not exist in this entry: ' + ', '.join('{' + r + '}' for r in found['unknown']) + '. Use only the marks in the entry text.']
    if found.get('no_wording'): lines += ['', 'Open marks (ending in ?) given without raw. Digits or a sign stand apart just before each of these, so the marked piece may be only the end of the number; give in raw the complete quantity as printed, closed up:'] + ['- ' + said(r) for r in found['no_wording']]
    if found['not_printed']: lines += ['', 'Wordings given in raw that the entry does not print (raw must be copied exactly from the entry text or its scheme; a quantity from the main text or a computed one does not belong in the blocks):'] + [f'- "{w}"' for w in found['not_printed']]
    return '\n'.join(lines)


def _holds(whole, part):
    """Whether `part` stands in `whole` as a quantity of its own: not run on from a digit, a point or a sign before it."""
    return any(not (k and (whole[k - 1].isdigit() or whole[k - 1] in '.-−–+')) for k in (m.start() for m in re.finditer(re.escape(part), whole)))


def resolve(record, texts, marks):
    """The record with every quantity filled in by the host: its wording, value and unit from the parse, and where it is printed.
    The model's record is not changed; a new one is returned."""
    by_ref = {m['ref']: m for m in marks}; out = json.loads(json.dumps(record))
    for _, q in quantities_of(out.get('blocks') or []):
        q['ref'] = (q.get('ref') or '').strip().strip('{}').rstrip('?') or None
        mark = by_ref.get(q['ref']); reading = (q.get('raw') or '').strip() or None
        if mark:
            value, unit = (mark['value'] or None, None if mark['unit'] == 'WORDS' else mark['unit'])
            # A fuller wording that holds the marked one (the qualifier printed with it) keeps the marked number; only a wording that
            # repairs the marked one (a number the parse broke apart) gives the number itself.
            if reading and squeeze(reading) != squeeze(mark['raw']) and not _holds(squeeze(reading), squeeze(mark['raw'])):
                rv, ru = parse_quantity(reading); value, unit = (rv, ru) if rv is not None else (value, unit)
            q.update(raw=reading or mark['raw'], marked_raw=mark['raw'], value=value, unit=unit, row=mark['row'], block_id=mark['block_id'], span=[mark['start'], mark['end']])
        else:
            value, unit = parse_quantity(reading); where = None
            for block_id, t in texts.items():
                at = plain(t['plain']).find(plain(reading or '\0'))
                if at >= 0: where = (block_id, at); break
            q.update(raw=reading, marked_raw=None, value=value, unit=unit, row=None, block_id=where[0] if where else None, span=[where[1], where[1] + len(plain(reading))] if where else None)
    return out
