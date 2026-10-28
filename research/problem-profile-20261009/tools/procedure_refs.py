"""The procedures an experiment entry refers to by name (2026-10-18), found by the host so that the entry task can read them.

An entry that says it followed a procedure printed elsewhere ("General Procedure 1 was employed", "following Method B", "by the procedure
described for 12") is recorded from a text that leaves out what that procedure says. The host finds the explicit name in the entry and
the place in the SI that is headed by the same name (or, for a compound, the procedure unit the segmentation gave that compound), and
gives that text to the task to read. Only an explicit name is followed: nothing is inferred from the chemistry, and a unit is never
taken because it stands next to the entry. A name is followed only when exactly one other place of the SI is headed by it: headed at
several places (Method A and Method B numbered anew under each compound) or nowhere, it gives nothing, and the host does not choose. An
entry that heads a part of itself by the name refers to itself. A procedure taken from a compound needs the words that cite it
('following', 'according to', 'as described', 'by the same procedure ... for 12'); a title ('Procedure for the Preparation of 48') cites
nothing. No model is called here; nothing of what is found becomes part of the record.
usage (survey): procedure_refs.py <pack dir> [<pack dir> ...]"""
import json, re, sys
from pathlib import Path
from quantity_rule import text_of

_LEVEL = r'(?:general|typical|standard|representative)'
# the name's own mark is written as authors write it: a number, a capital letter or two (with a number: S1), a roman numeral -- never
# a word in lower case ('the procedure as described' names nothing)
_ID = r'(?-i:\d{1,2}[a-z]?|[A-Z]{1,2}\d{0,2}|[IVX]{1,4})'
NAMED = re.compile(r'\b(?:' + _LEVEL + r'\s+)?(?:experimental\s+)?(procedure|method|protocol)\s*\(?(' + _ID + r')\)?(?![\w])|\b(GP|GM)\s*-?\s*(' + _ID + r')\b', re.I)
FOR_COMPOUND = re.compile(r'\b(?:following|employing|according\s+to|as\s+described\s+(?:in|for)|analogous(?:ly)?\s+to|in\s+analogy\s+to|by|using|via|under)\s+(?:that\s+|those\s+|the\s+)?'
                          r'(?:same\s+|general\s+|analogous\s+|similar\s+|identical\s+)?(?:procedure|method|conditions|protocol)\s+'
                          r'(?:(?:already\s+|previously\s+)?(?:described|used|reported|given|outlined|employed)\s+)?(?:above\s+|previously\s+)?(?:as\s+for|as|for|of\s+synthesizing)\s+(?:the\s+(?:preparation|synthesis)\s+of\s+)?'
                          r'(?:compound\s+)?(\(?[+\-±]?\)?-?[0-9]{1,3}[a-z]?[\'′]*)'
                          r'|\bas\s+described\s+(?:above\s+|previously\s+)?for\s+(?:the\s+(?:preparation|synthesis)\s+of\s+)?(?:compound\s+)?(\(?[+\-±]?\)?-?[0-9]{1,3}[a-z]?[\'′]*)', re.I)
LIMIT = 6000


def _key(kind, ident):
    # 'general procedure GP1' and 'GP1' and 'general procedure 1' are one name
    kind = {'gp': 'procedure', 'gm': 'method'}.get(kind.lower(), kind.lower()); return f"{kind} {re.sub(r'^(?:gp|gm)-?(?=\d)', '', ident.lower())}"


def _external_reference(text, start, end):
    """An explicit external citation is not a same-package procedure name."""
    before = text[max(0, start - 60):start]
    after = re.split(r'[;\n]', text[end:end + 140], maxsplit=1)[0]
    return bool(re.search(r'\b(?:ref(?:erence)?s?\.?|literature|publication)\s*(?:\[?\d|procedure|method)', before + after, re.I))


def names_in(text):
    """Explicit same-package names; discovery never selects by chemistry."""
    cited = lambda m: re.search(r'\b(?:known|reported|literature|published|previous(?:ly)?|original|our)\s+(?:\w+\s+)?$', text[max(0, m.start() - 30):m.start()], re.I)
    names = {_key(m.group(1) or m.group(3), m.group(2) or m.group(4)) for m in NAMED.finditer(text)
             if not cited(m) and not _external_reference(text, m.start(), m.end())}
    compounds = set()
    for m in FOR_COMPOUND.finditer(text):
        if _external_reference(text, m.start(), m.end()): continue
        label = (m.group(1) or m.group(2)).strip()
        # A parenthesis around a plain number is typography. A printed
        # stereochemical sign is identity and is never silently stripped.
        if label.startswith('(') and ')' not in label: label = label[1:]
        compounds.add(label)
    return names, compounds


def _headings(text):
    """Literal heading occurrences, shared by discovery and uniqueness checks."""
    for m in NAMED.finditer(text):
        before = text[max(0, m.start() - 30):m.start()]
        citing = re.search(r'\b(?:following|followed|using|used|according\s+to|via|by|in|of|see|from|as|described|with|under|per)\s+(?:the\s+|general\s+|same\s+)*$', before, re.I)
        if not citing and re.match(r'\s*(?:[:.\n—–(]|-\s|$)', text[m.end():m.end() + 3]):
            yield _key(m.group(1) or m.group(3), m.group(2) or m.group(4)), m.start(), m.end()


def headed(text):
    """Names headed within the existing initial heading boundary."""
    return {name for name, start, _ in _headings(text) if start < 160}


def heads_anywhere(text):
    return {name for name, _, _ in _headings(text)}


def referred(entry_index, units, blocks):
    """[(name, unit index, text)] that the unit at entry_index refers to, in the order the names are found."""
    unit_text = lambda u: '\n\n'.join(t for t in (text_of(blocks[b]) for b in u['block_ids'] if b in blocks) if t.strip())
    own = unit_text(units[entry_index]); names, compounds = names_in(own)
    found = []
    for name in sorted(names - heads_anywhere(own)):     # a name the entry heads a part of itself by is its own
        places = []
        for k, u in enumerate(units):
            if k == entry_index or u['kind'] not in ('PROCEDURE', 'GENERAL_METHODS'): continue
            for b in u['block_ids']:
                text = text_of(blocks[b]) if b in blocks else ''
                for heading, start, _ in _headings(text):
                    if heading != name: continue
                    rest = '\n\n'.join(text_of(blocks[x]) for x in u['block_ids'][u['block_ids'].index(b):] if x in blocks)
                    places.append((name, k, rest[:LIMIT] if start < 160 else None))

        if len(places) == 1 and places[0][2] is not None: found += places
    own_label = str(units[entry_index].get('compound_label') or '').strip()
    for label in sorted(compounds - {own_label}):
        places = [(f'the procedure for {label}', k, unit_text(u)[:LIMIT]) for k, u in enumerate(units)
                  if k != entry_index and u['kind'] == 'PROCEDURE' and str(u.get('compound_label') or '').strip() == label]
        if len(places) == 1: found += places
    return found


def section(found):
    """The text given to the entry task, before the entry: empty when the entry refers to nothing the host found."""
    if not found: return ''
    parts = [f'[{name}, as printed at SI unit {k}]\n{text}' for name, k, text in found]
    return 'A PROCEDURE THE ENTRY REFERS TO (printed elsewhere in the SI, given for reading; it is not part of the entry)\n\n' + '\n\n'.join(parts) + '\n\n'


if __name__ == '__main__':
    total = with_ref = resolved = 0
    for pack in map(Path, sys.argv[1:]):
        blocks = {}
        for line in (pack / 'inputs/si/blocks.jsonl').read_text(encoding='utf-8').splitlines():
            row = json.loads(line); blocks['SI-P%04d-B%04d' % (row['page_idx'], row['reading_order'])] = row
        units = json.loads((pack / 'inputs/si/units.json').read_text(encoding='utf-8'))['units']
        for k, u in enumerate(units):
            if u['kind'] != 'PROCEDURE': continue
            total += 1; text = '\n'.join(text_of(blocks[b]) for b in u['block_ids'] if b in blocks); names, compounds = names_in(text)
            names -= heads_anywhere(text)
            if names or compounds:
                with_ref += 1; got = referred(k, units, blocks); resolved += bool(got)
                where = '' if got else ' | named elsewhere in: ' + str(sorted({f"{v['kind']}:{j}" for j, v in enumerate(units) if j != k for n in names
                                                                                   if n in names_in('\n'.join(text_of(blocks[b]) for b in v['block_ids'] if b in blocks))[0]})[:6])
                print(f"{pack.parent.name} unit {k}: names {sorted(names)} compounds {sorted(compounds)} -> {[(n, i, len(t)) for n, i, t in got]}{where}")
    print(f'procedure entries {total}, naming a procedure {with_ref}, of them given one {resolved}')
