"""Data marks (2026-10-20): the places in an entry's text where characterization data can begin, numbered for the model to point at, so
that the host never has to search the print for a few words and guess where data ends. One source for the entry task (record_actions.py:
the marks shown in the text, the check of what the record points at) and for the assembly (whole_paper.py: the pieces of print cut at
the marks the record names).

A data mark stands at the start of every block of the entry and at the start of every piece of data the drafting rule's DATA pattern
finds (Rf, an NMR, IR, HRMS, a rotation, a melting point) or another data heading (DATA_START), moved back to the start of its clause so that a heading printed with it
("50: Rf = ...") stays with it. The record says for each compound the mark where its data begin (from) and the mark of its last piece
(to); a piece runs to the next mark of its block or to the end of the block. No model is called here; the host reads no chemistry."""
import difflib, re
from quantity_rule import DATA
import quantity_marks as qm

# where a piece of data begins: the drafting rule's DATA (Rf, NMR, IR, HRMS, a rotation, a melting point) and the other headings data is
# printed under (low-resolution and other mass spectra, GC, HPLC, UV, elemental analysis), so that every piece can be pointed at (2026-10-20)
DATA_START = re.compile(DATA.pattern + r'|\bLRMS\b|\bLC-MS\b|\b(?:EI|CI|ESI|FAB|MALDI|APCI)-?MS\b|\bMS\s*\((?:EI|CI|ESI|FAB|MALDI|APCI)|\bGC\s*\(|\bHPLC\s*\(|\bUV\s*[(:]|\bAnal\.|\belemental analysis', re.I)

# These are visible operation cues, not a classification of all chemical prose.
# Every inferred return to operations is labelled for audit, not used to rewrite
# the stored model response or its old data marks.
OPERATION = re.compile(r'^(?:To\s+(?:a|the)\b|(?:The|A|An)\s+(?:reaction|mixture|solution|residue|crude|combined|solvent|filtrate|flask|resultant)\b|After\b|Upon\b|Next\b|Potassium acetate\b|(?:A\s+)?solution of\b|.*?\b(?:was|were)\s+(?:added|dissolved|stirred|heated|cooled|purified|evaporated|washed|dried|concentrated)\b)', re.I)
SENTENCE_START = re.compile(r'(?:[.;]\s+|\n+)(?=[A-Z(])')
PREPARATIVE = re.compile(r'\b(?:purif\w*|preparative|prep[. -]*HPLC|chromatograph\w*|afford\w*|isolat\w*|TLC|monitor\w*)\b', re.I)


def data_starts(text):
    for m in DATA_START.finditer(text):
        at = m.start()
        # Ir(COD) is a reagent name, not an IR spectrum.
        if re.match(r'Ir\s*\(\s*COD', text[at:], re.I):
            continue
        begin = _clause_start(text, at)
        prefix = text[begin:at]
        if re.match(r'(?:HPLC|GC|R\W*f)', m.group(), re.I) and PREPARATIVE.search(prefix):
            continue
        # Preserve a short author label, never an entire preparation clause.
        label = re.fullmatch(r'\s*(?:\(?[+−±-]\)?-?)?[\w-]{1,18}:\s*', prefix)
        yield begin if label else at

MARK = re.compile(r'⟦(d\d+)⟧')


_CLAUSE_END = re.compile(r';|\n|\.\s|(?<=[A-Za-z)\]])\.\d{1,3}(?:[,–-]\d{1,3})*(?=\s)')


def _clause_start(text, at):
    """The start of the clause holding position `at`: just after the last ';', line break or '. ' before it, spaces skipped. A footnote
    number may stand between a word's full stop and the space ('according to Butsugan's method.3 LRMS (CI)'); a decimal ('1.5 mL') is
    no such end, its full stop following a digit (2026-10-21)."""
    k = max((m.end() for m in _CLAUSE_END.finditer(text, 0, at)), default=0)
    while k < at and text[k].isspace(): k += 1
    return k


def items(texts):
    """[{'id': 'd1', 'block_id', 'start', 'end', 'data': bool}] in reading order over `texts` ({block_id: {'plain': ...}}): a mark at each
    block start and at each piece of data; `end` is the next mark of the block or the block's end; `data` says the piece begins with data."""
    out = []
    continuing = False
    for block_id, t in texts.items():
        text = t['plain']
        if not text.strip(): continue
        data_at = set(data_starts(text))
        operations = {m.end() for m in SENTENCE_START.finditer(text) if OPERATION.match(text[m.end():])}
        if OPERATION.match(text):
            operations.add(0)
        starts = sorted({0} | data_at | operations)
        state = continuing and not OPERATION.match(text)
        for k, s in enumerate(starts):
            if s in data_at:
                state = True
            elif s in operations:
                state = False
            out.append({'block_id': block_id, 'start': s, 'end': starts[k + 1] if k + 1 < len(starts) else len(text), 'data': bool(state),
                        'boundary_basis': 'data_heading' if s in data_at else 'operation_sentence' if s in operations else 'block_start',
                        'continuation_candidate': bool(state and s not in data_at)})
        continuing = bool(state)
    for n, item in enumerate(out, 1): item['id'] = f'd{n}'
    return out


def tiles(texts, entry_items):
    """Whether `entry_items` (data marks stored with a record) cover each block of `texts` with text from its start to its end, piece
    after piece, with no other block: the print they were made on, whatever rule placed them."""
    by_block = {}
    for i in entry_items: by_block.setdefault(i['block_id'], []).append(i)
    if set(by_block) != {b for b, t in texts.items() if t['plain'].strip()}: return False
    for b, pieces in by_block.items():
        ends = [0] + [i['end'] for i in pieces]
        if [i['start'] for i in pieces] != ends[:-1] or ends[-1] != len(texts[b]['plain']): return False
    return True


def show(texts, entry_items):
    """{block_id: marked text with the data marks}: each block's 'marked' text (with its quantity marks) and a ⟦dN⟧ before each piece."""
    shown = {}
    for block_id, t in texts.items():
        marked = t['marked']; at = {i['start']: i['id'] for i in entry_items if i['block_id'] == block_id}
        out, plain_at = [], 0
        for token in re.split(r'(\{q\d+\??\})', marked):
            if re.fullmatch(r'\{q\d+\??\}', token): out.append(token); continue
            for ch in token:
                if plain_at in at: out.append(f'⟦{at.pop(plain_at)}⟧')
                out.append(ch); plain_at += 1
        if plain_at in at: out.append(f'⟦{at.pop(plain_at)}⟧')
        shown[block_id] = ''.join(out)
        assert MARK.sub('', re.sub(r'\{q\d+\??\}', '', shown[block_id])) == t['plain'], block_id
    return shown


def frame_issues(record, entry_items, texts):
    """What the record's characterization frames point at that the host cannot cut: a mark that is not one of this entry's data marks, a
    last mark before the first, first words that do not stand at the first mark's piece. [(compound, what is wrong)]."""
    order = {i['id']: k for k, i in enumerate(entry_items)}; by_id = {i['id']: i for i in entry_items}; issues = []
    for f in record.get('characterization_frames') or []:
        a, b = str(f.get('from') or '').strip('⟦⟧ '), str(f.get('to') or '').strip('⟦⟧ ')
        compound = f.get('compound') or ''
        if a not in order or b not in order: issues.append((compound, f"{a or '(none)'} or {b or '(none)'} is not a data mark of this entry")); continue
        if order[b] < order[a]: issues.append((compound, f'its last mark {b} stands before its first mark {a}')); continue
        words = qm.squeeze(f.get('starts_with'))
        piece = texts[by_id[a]['block_id']]['plain'][by_id[a]['start']:by_id[a]['end']]
        if words and words not in qm.squeeze(piece): issues.append((compound, f'the words "{f.get("starts_with")}" are not at {a}'))
    return issues


def issue_note(issues):
    if not issues: return ''
    return '\n'.join(['', 'Characterization frames whose data marks the system cannot follow (give the compound, the data mark where its data begin in from, the mark of its last piece in to, and the first words after the from mark):'] +
                     [f'- "{c}": {w}' for c, w in issues])


def spans(record, entry_items):
    """[(compound, [(block_id, start, end)], [ids])]: the plain-text pieces each characterization frame of the record names, contiguous
    pieces of one block joined."""
    order = {i['id']: k for k, i in enumerate(entry_items)}; out = []
    ends = lambda f: (str(f.get('from') or '').strip('⟦⟧ '), str(f.get('to') or '').strip('⟦⟧ '))
    bounds = [(order[a], order[b]) for a, b in map(ends, record.get('characterization_frames') or []) if a in order and b in order and order[b] >= order[a]]
    for f in record.get('characterization_frames') or []:
        a, b = ends(f)
        if a not in order or b not in order or order[b] < order[a]: continue
        # a frame whose last mark runs into the next frame's data (that frame begins inside it and ends after it) ends before that frame's
        # first mark: the first mark is the one the model anchors with its first words; a frame held wholly inside another is left as given
        last = min([order[b]] + [s - 1 for s, e in bounds if order[a] < s <= order[b] < e])
        chosen = entry_items[order[a]:last + 1]; pieces = []
        # a frame that holds no piece of data (a screening table, a remark on the compound's stability) is not characterization: the
        # print stays with the entry, and no frame is cut (2026-10-21)
        if not any(i['data'] for i in chosen): continue
        # a frame from the entry's first mark when that piece is no data (the entry's heading and its procedure, '(E)-4-Homogeranyltoluene
        # (4). This compound was synthesized by ...') begins at the first piece of data after it: the heading and the procedure stay with
        # the entry. Only the first block's: a later block may begin with data run on from the page before (2026-10-21)
        if chosen[0] is entry_items[0] and not chosen[0]['data']: chosen = chosen[1:]
        for i in chosen:
            if pieces and pieces[-1][0] == i['block_id'] and pieces[-1][2] == i['start']: pieces[-1] = (i['block_id'], pieces[-1][1], i['end'])
            else: pieces.append((i['block_id'], i['start'], i['end']))
        out.append((f.get('compound', '').strip(), pieces, [i['id'] for i in chosen]))
    return out


def to_source(plain, source):
    """A function from offsets in the plain text the marks were made on to offsets in the block's source text (what the assembly keeps
    verbatim): the two differ only by the parse's typesetting cleaned in the plain one, aligned character by character."""
    ops = difflib.SequenceMatcher(None, plain, source, autojunk=False).get_opcodes()
    def at(i):
        for tag, i1, i2, j1, j2 in ops:
            if i1 <= i < i2 or (i == i2 == len(plain) and tag == 'equal'): return j1 + (i - i1) if tag == 'equal' else j1
        return len(source)
    return lambda i: len(source) if i >= len(plain) else at(i)
