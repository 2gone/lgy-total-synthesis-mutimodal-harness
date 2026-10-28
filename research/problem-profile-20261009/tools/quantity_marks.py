"""Host side of recording by reference: every quantity the extraction rule finds in an entry gets a mark at its place in the text, the
model refers to marks instead of retyping numbers, and the record is reconciled by set arithmetic over marks, never by comparing strings.

The extraction rule is tools/quantity_rule.py, the rule that drafted the reviewed fact sheets. The recording does not depend on a sheet:
where the draft sheet of the run reproduces the marks row for row, each mark carries its sheet row (for the measurement), and otherwise
none does. Where a wording the model wrote itself is printed is told only when it is printed once in the entry; printed at several places,
it is given with those places and bound to none of them. No model is called here.

What value and unit mean (2026-10-19): the number and unit printed at the mark, or the number a wording of the model's own gives when it
holds exactly one. A bound, a range, an approximation or a qualifier printed with the number is carried by raw, and a raw that differs
from marked_raw says that more is printed with the number; the host does not judge whether a whole expression is one value. The host
never gives a digit or a sign the entry does not print."""
import collections, html, json, re
from quantity_rule import plain, text_of, QUANTITY, WORDS, DATA, NUMBER

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
SPELT = re.compile(NUMBER + r'\s?(ml|[µμ]l|hrs?|mins|equivalents?|wt[ \t]%|Torr|mmHg|kPa|MPa)(?![A-Za-z])')
# "5 mol %": the drafting rule reads "5 mol"; the per cent sign printed after it is part of the quantity
_MOL_PER_CENT = re.compile(r'[ \t]?%')


def glued_sign(text, at):
    """Whether the number starting at `at` carries a minus sign printed right before it, run on to a word ('at-78 °C', 'to-30 °C'):
    the drafting rule does not read a sign after a word character. A dash after a digit is a range ('20-30 °C') and is not a sign.
    The one source of this reading for the marks and for the model's own wordings (parse_quantity)."""
    return at >= 2 and text[at - 1] in '-−–' and text[at - 2].isalpha()


def extract(row, own=False):
    """(procedure text, [(start, end, raw, value, unit, own, drafted)]) of one parsed block: the quantities of the drafting rule, exactly as
    the sheet was drafted, and with own=True also those the host marks itself where no quantity of the drafting rule stands -- ratios
    (unit RATIO), pH values (pH) and the unit spellings above (own=True) -- and a drafted "mol" completed by its per cent sign (its
    drafted raw and unit kept in drafted, which is what the sheet holds)."""
    text = text_of(row)
    cut = DATA.search(text) if row['type'] not in ('table',) else None
    procedure = text
    # Mark operation regions after spectra too; tables remain deliberately unmarked.
    import data_marks as dm
    regions = dm.items({'block': {'plain': text}})
    operational = lambda at: not any(i['data'] and i['start'] <= at < i['end'] for i in regions)
    if row['type'] == 'table': return text, len(procedure), []
    found = [(m.start(), m.group(0), ((m.group(1) or '') + m.group(2)).replace(' ', '').replace(',', ''), m.group(3).rstrip('.'), False, None) for m in QUANTITY.finditer(procedure) if operational(m.start())]
    found += [(m.start(), m.group(0), '', 'WORDS', False, None) for m in WORDS.finditer(procedure) if operational(m.start())]
    if own:
        for k, (at, raw, value, unit, _, _) in enumerate(found):
            more = _MOL_PER_CENT.match(procedure, at + len(raw)) if unit == 'mol' else None
            if more: found[k] = (at, raw + more.group(0), value, 'mol%', False, {'raw': raw, 'unit': unit})
        # "at-78 °C": the parse lost the space before a minus sign, and the drafting rule (no word character before a number) leaves the
        # sign out. The sign is part of the quantity; the drafted raw, value and unit are kept in drafted, which is what the sheet holds.
        for k, (at, raw, value, unit, mine, drafted) in enumerate(found):
            if unit != 'WORDS' and glued_sign(procedure, at):
                found[k] = (at - 1, procedure[at - 1] + raw, '-' + value, unit, mine, {**(drafted or {'raw': raw, 'unit': unit}), 'value': value})
        taken =[(at, at + len(raw)) for at, raw, *_ in found]
        def add(m, value, unit):
            if operational(m.start()) and not any(m.start() < b and a < m.end() for a, b in taken): found.append((m.start(), m.group(0), value, unit, True, None)); taken.append((m.start(), m.end()))
        for m in SPELT.finditer(procedure): add(m, ((m.group(1) or '') + m.group(2)).replace(' ', '').replace(',', ''), _SPELT[re.sub(r'[ \t]+', ' ', m.group(3)).lower()])
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
            # The same holds for the end of a range printed close up ("2-18 h", "2 to 18 h"): the mark catches the last number only, and
            # what the range is must be stated in raw (2026-10-20).
            open_ = unit not in ('WORDS', 'RATIO', 'pH') and bool(re.search(r'(?:\d|[-−–]|\d[ \t]?\.)[ \t]{1,2}$', text[:start]) or re.search(r'\d[ \t]?(?:[-−–]|to)[ \t]?$', text[:start]))
            marks.append({'ref': ref, 'block_id': block_id, 'start': start, 'end': end, 'raw': raw, 'value': value, 'unit': unit, 'open': open_, 'own': mine or unit in ('RATIO', 'pH'), **({'drafted': drafted} if drafted else {}),
                          'context': text[max(0, start - 70):end + 40].replace('\n', ' ')})
            # Marks go in at the END of each quantity; two finds that overlap keep their own ends, so the text between is never repeated.
            cut = max(end, last); pieces.append(text[last:cut] + '{' + ref + ('?' if open_ else '') + '}'); last = cut
        texts[block_id] = {'plain': text, 'marked': ''.join(pieces) + text[last:], 'procedure_end': procedure_end}
    drafted = [m for m in marks if not m['own']]
    as_drafted = lambda m: (m['block_id'], (m.get('drafted') or m)['raw'], (m.get('drafted') or m).get('value', m['value']), (m.get('drafted') or m)['unit'])
    for mark in marks: mark['row'] = None
    # the sheet row of each mark, for the measurement only: given where the draft sheet reproduces the marks row for row
    if len(drafted) == len(sheet_rows) and all(as_drafted(m) == (r['block_id'], r['raw'], r['value'], r['unit']) for m, r in zip(drafted, sheet_rows)):
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
    """For asking whether a wording is printed in a text: markup, spacing, dash and micro variants do not count, nor does an HTML escape
    the parse left in a table cell ('&lt;5' is the '<5' the model reads)."""
    return re.sub(r'\s+', '', plain(tidy(html.unescape(text or '')))).lower().replace('µ', 'u').replace('x', '×').replace('μ', 'u')


def parse_quantity(raw):
    """(value, unit) when the wording is a number with a unit the rule knows, else (None, None)."""
    closed = re.sub(r'(?<=\d)\s?([mµ])l\b', r' \1L', plain(tidy(raw)))
    for text in (closed, plain(raw or '')):
        m = QUANTITY.search(text)
        if m:
            value = ((m.group(1) or '') + m.group(2)).replace(' ', '').replace(',', ''); unit = m.group(3).rstrip('.')
            # "5 mol %": as for the marks, the per cent sign printed after "mol" is part of the unit
            if unit == 'mol' and _MOL_PER_CENT.match(text, m.end()): unit = 'mol%'
            return ('-' + value if not m.group(1) and glued_sign(text, m.start()) else value), unit
    return None, None


def quantities_of(node, path=()):
    """Every quantity object of a record (a dict that has 'ref' and 'printed_in'), with the path to it."""
    # a ref is compared as the bare mark name: the model may copy it with its braces or its question mark
    if isinstance(node, dict):
        if 'ref' in node and 'printed_in' in node: yield path, node
        for key, value in node.items(): yield from quantities_of(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node): yield from quantities_of(value, path + (index,))


_WORD = re.compile(r'[A-Za-z][A-Za-z0-9\-]*|\d+(?:\.\d+)?')
_SENTENCE_END = re.compile(r'(?<=[.!?;])\s+(?=[A-Z(\[])|\n')


def printed_apart_at_mark(raw, mark, texts):
    """Whether a wording the model gave with a mark, not printed as one stretch, is the marked stretch with words of its own sentence that
    the print sets apart from it -- a parenthesis between ('a 0.4 M solution (31 mL, ...) of KHMDS in THF' given as '0.4 M solution of
    KHMDS in THF'), a header after it ('(29%) starting material'). Such a wording is kept as the model gave it and not handed back: the
    number is the mark's (the wording holds the marked stretch, not run on from a digit or a sign), the wording adds no number of its own,
    and every word it adds stands in the mark's sentence. The one source of this reading for reconcile and resolve (2026-10-20)."""
    whole, part = squeeze(raw), squeeze(mark['raw'])
    if not part or part not in whole or not _holds(whole, part): return False
    added = collections.Counter(_WORD.findall(plain(raw))) - collections.Counter(_WORD.findall(plain(mark['raw'])))
    if not added or any(w[0].isdigit() for w in added): return False
    text = texts[mark['block_id']]['plain']
    begin = max([m.end() for m in _SENTENCE_END.finditer(text, 0, mark['start'])], default=0)
    end = next((m.start() for m in _SENTENCE_END.finditer(text, mark['end'])), len(text))
    # each added word stands in the sentence as a word (a footnote number may follow it: 'bromide9'), not as a piece of another
    sentence = plain(text[begin:end])
    return all(re.search(r'(?<![A-Za-z])' + re.escape(w) + r'(?![A-Za-z])', sentence, re.I) for w in added)


def printed_apart_in_table(raw, texts):
    """The table block a wording without a mark is read from when the print sets its pieces apart -- a cell's number given with its
    column's unit ('45' under 't [min]' given as '45 min'), or None. The wording holds exactly one number; that number, with any sign or
    bound printed right before it ('-40', '&lt;5'), stands in one table block of the entry and in no other table (a paragraph of the
    entry may print the same number for something else: the table's footnote numbering its rows '(14) ca. 2:1', 2026-10-21); every word of the
    wording stands in that table as a word, and every other sign of it (%, °) is printed in it. A paragraph is not such a place: a unit
    found somewhere in it may belong to another quantity, so wordings there are still handed back (2026-10-20)."""
    words = _WORD.findall(plain(raw or '')); numbers = [w for w in words if w[0].isdigit()]
    if len(numbers) != 1 or len(_NUMBER.findall(plain(raw))) != 1: return None
    clean = {b: html.unescape(re.sub(r'<[^>]+>', ' ', t['plain'])) for b, t in texts.items()}
    lead = re.search(r'([<>≤≥≈~±+\-−–]?)\s*' + re.escape(numbers[0]), plain(raw)).group(1)
    # the sign is part of what must be printed: given without one, the cell must hold none either ('40' is not the '-40' printed)
    number = re.compile((r'(?<![\d.])' + re.escape(lead) + r'\s*' if lead else r'(?<![\d.<>≤≥≈~±+\-−–])(?<![<>≤≥≈~±+\-−–] )') + re.escape(numbers[0]) + r'(?![\d])')
    holding = [b for b, t in clean.items() if '<table' in texts[b]['plain'] and re.search(r'(?<![\d.])' + re.escape(numbers[0]) + r'(?![\d])', t)]
    if len(holding) != 1 or not number.search(clean[holding[0]]): return None
    # outside the table the number may stand for something else, but not with a sign or bound the wording leaves out ('-40 °C' in the
    # text, the wording '40 °C'): the wording may have been read there with its sign dropped; a dash after a digit joins a range ('0.5-2 mmol')
    signed = re.compile(r'(?<![\d.])([<>≤≥≈~±+\-−–])\s*' + re.escape(numbers[0]) + r'(?![\d])')
    if any(m.group(1) != lead for b, t in clean.items() if b != holding[0] for m in signed.finditer(t)): return None
    table = clean[holding[0]]
    if not all(re.search(r'(?<![A-Za-z])' + re.escape(w) + r'(?![A-Za-z])', table, re.I) for w in words if not w[0].isdigit()): return None
    signs = set(re.sub(r'[\sA-Za-z0-9.<>≤≥≈~±+\-−–]', '', plain(raw)))
    return holding[0] if all(c in table for c in signs) else None


def reconcile(record, texts, marks, scheme_text=''):
    """What the record does with the marks and whether its own wordings are printed in the entry.

    Returns {'placed': {ref: [paths]}, 'set_aside': {ref: reason}, 'unplaced': [ref], 'repeated': [ref], 'unknown': [ref],
    'not_printed': [raw], 'printed_apart': [raw], 'apart_ref': {raw: ref}, 'accepted_apart': [path], 'problems': n}. A mark is right when it is placed exactly once or set aside (never both); a wording the model
    wrote itself is right when the entry prints it (its text, or its scheme as transcribed), or when it is the marked stretch with words of
    its sentence printed apart from it (printed_apart_at_mark: kept, listed in accepted_apart, no problem). Of the wordings not printed,
    printed_apart lists those whose words the entry prints, only not together, for the note."""
    known = {m['ref'] for m in marks}; by_ref = {m['ref']: m for m in marks}; open_marks = {m['ref'] for m in marks if m.get('open')}
    placed, unknown, not_printed, no_wording, printed_apart, apart_ref, accepted_apart = {}, [], [], [], [], {}, []
    body = squeeze(' '.join(t['plain'] for t in texts.values())); scheme = squeeze(scheme_text)
    for path, q in quantities_of(record.get('blocks') or []):
        ref, raw = (q.get('ref') or '').strip().strip('{}').rstrip('?') or None, q.get('raw')
        if ref:
            if ref in known: placed.setdefault(ref, []).append(path)
            else: unknown.append(ref)
            if ref in open_marks and not (raw or '').strip(): no_wording.append(ref)
        if raw and squeeze(raw) and squeeze(raw) not in body and squeeze(raw) not in scheme:
            if ref in known and printed_apart_at_mark(raw, by_ref[ref], texts): accepted_apart.append(path); continue
            if not ref and printed_apart_in_table(raw, texts): accepted_apart.append(path); continue
            not_printed.append(raw)
            # for the note: every word and number of it is printed in the entry, only not as one stretch (a wording joining pieces)
            pieces = _WORD.findall(raw)
            if len(pieces) > 1 and all(squeeze(w) in body or squeeze(w) in scheme for w in pieces): printed_apart.append(raw); apart_ref.setdefault(raw, ref)
        if not ref and not (raw or '').strip(): not_printed.append('(a quantity with neither a mark nor a wording)')
    set_aside = {}
    for item in record.get('not_recorded') or []:
        ref = item['ref'].strip().strip('{}').rstrip('?')
        # a data mark (⟦d4⟧, tools/data_marks.py) only points at characterization; listed here it says nothing and is no unknown mark
        if re.fullmatch(r'⟦?d\d+⟧?', ref): continue
        if ref in known: set_aside[ref] = item['reason']
        else: unknown.append(ref)
    repeated = sorted({r for r, paths in placed.items() if len(paths) > 1} | (set(placed) & set(set_aside)), key=lambda r: int(r[1:]))
    unplaced = [m['ref'] for m in marks if m['ref'] not in placed and m['ref'] not in set_aside]
    return {'placed': placed, 'set_aside': set_aside, 'unplaced': unplaced, 'repeated': repeated, 'unknown': sorted(set(unknown)), 'not_printed': not_printed, 'printed_apart': printed_apart, 'apart_ref': apart_ref, 'accepted_apart': accepted_apart, 'no_wording': no_wording,
            'problems': len(unplaced) + len(repeated) + len(set(unknown)) + len(not_printed) + len(no_wording)}


def hand_back_note(found, marks):
    """What the model is told when the record does not close over the marks. Only facts the host can state without reading the chemistry."""
    by_ref = {m['ref']: m for m in marks}; said = lambda ref: f'{{{ref}}} "{by_ref[ref]["raw"]}" in: ...{by_ref[ref]["context"]}...'
    lines = ['This is a mechanical source-and-mark check, not a judgment that the scientific record is complete. Return the complete record again with only what these points need changed; everything else must stay as it is. When correcting a quotation, retain its source-supported meaning in the appropriate existing fields, using structured fields where they apply, or in unresolved where none holds it: keeping that meaning is part of what the point needs.']
    if found['unplaced']: lines += ['', 'Marks that appear nowhere (place each where its quantity belongs, or list it under not_recorded with the reason):'] + ['- ' + said(r) for r in found['unplaced']]
    if found['repeated']: lines += ['', 'Marks used more than once (each mark is one place in the text and belongs to one place in the record; a quantity printed twice has two marks):'] + ['- ' + said(r) for r in found['repeated']]
    if found['unknown']: lines += ['', 'Marks that do not exist in this entry: ' + ', '.join('{' + r + '}' for r in found['unknown']) + '. Use only the marks in the entry text.']
    if found.get('no_wording'): lines += ['', 'Open marks (ending in ?) given without raw. Digits or a sign stand just before each of these, so the marked piece may be only the end of a number the parse broke apart or the end of a range; give in raw the complete quantity as printed, closed up:'] + ['- ' + said(r) for r in found['no_wording']]
    def rest(w):
        # the stretch printed at the wording's own mark, and what the wording says besides it (both as the model wrote them)
        mark = by_ref.get((found.get('apart_ref') or {}).get(w))
        at = w.find(mark['raw']) if mark else -1
        other = (w[:at] + ' ' + w[at + len(mark['raw']):]).strip(' ,;()') if at >= 0 else ''
        return f' -- printed at its mark: "{mark["raw"]}"; the rest, "{re.sub(r"[ ]{2,}", " ", other)}", is printed apart from it' if other else ''
    apart = [w for w in found['not_printed'] if w in (found.get('printed_apart') or [])]; elsewhere = [w for w in found['not_printed'] if w not in apart]
    if apart: lines += ['', 'Wordings given in raw whose words the entry prints, but not together (raw is one stretch copied exactly from the entry text or its scheme; what the rest of the wording said stays in the record, as said above):'] + [f'- "{w}"{rest(w)}' for w in apart]
    if elsewhere: lines += ['', 'Wordings given in raw that the entry does not print (raw must be copied exactly from the entry text or its scheme; a quantity from the main text or a computed one does not belong in the blocks):'] + [f'- "{w}"' for w in elsewhere]
    return '\n'.join(lines)


def places_of(needle, texts):
    """Every place [key, at] where `needle` stands in `texts` ({key: text}, already in the form the caller compares in), in reading order."""
    out = []
    for key, text in texts.items():
        at = text.find(needle) if needle else -1
        while at >= 0: out.append([key, at]); at = text.find(needle, at + len(needle))
    return out


# a number as printed, for counting the numbers of a wording (a digit run not part of a word or of a longer number)
_NUMBER = re.compile(r'(?<![\w.])\d+(?:\.\d+)?')


def _holds(whole, part):
    """Whether `part` stands in `whole` as a quantity of its own: not run on from a digit, a point or a sign before it."""
    return any(not (k and (whole[k - 1].isdigit() or whole[k - 1] in '.-−–+')) for k in (m.start() for m in re.finditer(re.escape(part), whole)))


def block_of_numbers(wording, texts):
    """The one block of the entry that prints every number of the wording (markup left out), or None: no number, or not one block."""
    numbers = _NUMBER.findall(plain(wording or ''))
    if not numbers: return None
    holding = [b for b, t in texts.items() if all(re.search(r'(?<![\d.])' + re.escape(n) + r'(?![\d])', re.sub(r'<[^>]+>', ' ', t['plain'])) for n in numbers)]
    return holding[0] if len(holding) == 1 else None


def resolve(record, texts, marks):
    """The record with every quantity filled in by the host: its wording, value and unit from the parse, and where it is printed.
    The model's record is not changed; a new one is returned."""
    by_ref = {m['ref']: m for m in marks}; out = json.loads(json.dumps(record))
    # the printed places of the marks each action uses: a wording the model wrote itself that is printed at several places is bound to
    # the one of them that a mark of the same action stands at, if exactly one is (the model retyped a marked quantity)
    marked_at = {}
    for path, q in quantities_of(out.get('blocks') or []):
        mark = by_ref.get((q.get('ref') or '').strip().strip('{}').rstrip('?'))
        if mark: marked_at.setdefault(path[:3], set()).add((mark['block_id'], mark['start']))
    flat = {block_id: plain(t['plain']) for block_id, t in texts.items()}; body = squeeze(' '.join(t['plain'] for t in texts.values()))
    for path, q in quantities_of(out.get('blocks') or []):
        q['ref'] = (q.get('ref') or '').strip().strip('{}').rstrip('?') or None
        mark = by_ref.get(q['ref']); reading = (q.get('raw') or '').strip() or None
        if mark:
            value, unit = (mark['value'] or None, None if mark['unit'] == 'WORDS' else mark['unit'])
            # A fuller wording that holds the marked one (the qualifier printed with it) keeps the marked number; only a wording that
            # repairs the marked one (a number the parse broke apart) gives the number itself, and only when it holds exactly one number.
            # A wording that does not hold the mark and holds more than one number ("20 - 30%", "0 4 °C") gives no value: neither its
            # first number nor the mark's (whose sign may be a range's dash) is the number printed. A ratio or a pH is read as before.
            if reading and squeeze(reading) != squeeze(mark['raw']) and not _holds(squeeze(reading), squeeze(mark['raw'])):
                rv, ru = parse_quantity(reading)
                if mark['unit'] in ('RATIO', 'pH'): value, unit = (rv, ru) if rv is not None else (value, unit)
                else: value, unit = (rv, ru) if rv is not None and len(_NUMBER.findall(plain(reading))) == 1 else (None, None)
            # a wording kept although the entry does not print it as one stretch (reconcile): the model's wording; the print is marked_raw at span
            if reading and squeeze(reading) not in body and printed_apart_at_mark(reading, mark, texts): q['raw_printed_apart'] = True
            q.update(raw=reading or mark['raw'], marked_raw=mark['raw'], value=value, unit=unit, row=mark['row'], block_id=mark['block_id'], span=[mark['start'], mark['end']])
        else:
            value, unit = parse_quantity(reading); found = places_of(plain(reading or ''), flat)
            # several places: the one a mark of the same action stands at -- in the block's own text, where the marks are placed
            at_mark = [p for p in found if any(b == p[0] and abs(len(texts[b]['plain'][:s]) - p[1]) <= 2 for b, s in marked_at.get(path[:3], ()))] if len(found) > 1 else []
            where = found[0] if len(found) == 1 else at_mark[0] if len(at_mark) == 1 else None
            q.update(raw=reading, marked_raw=None, value=value, unit=unit, row=None, block_id=where[0] if where else None, span=[where[1], where[1] + len(plain(reading))] if where else None,
                     **({'printed_times': len(found), 'printed_at': found[:6]} if len(found) > 1 and where is None else {}))
            if where is None and squeeze(reading or '') not in body and printed_apart_in_table(reading, texts): q['raw_printed_apart'] = True
            if where is None:
                # not one place: when its numbers are printed in one block of the entry only (a table whose cells repeat them, a cell
                # number given with its column's unit), that block is where it is printed -- the block, not a place in it
                block = block_of_numbers(reading, texts)
                if block: q.update(block_id=block, located_in_block=True)
    return out
