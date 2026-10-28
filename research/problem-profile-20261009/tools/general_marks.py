"""General-method marks (2026-10-21): the SI general methods, as the entry package prints them, cut into sentences, each with a mark
⟦g1⟧, ⟦g2⟧, ... before it. An entry task names, for each reaction block, the marks of the sentences that state how that block's reaction
was run and that its entry does not override; the host copies those sentences verbatim into the record, with the block they are printed
in. The model only judges which sentences apply; it writes none of their words, and nothing of them goes into the actions. The whole
section is kept verbatim at the paper level as well (tools/whole_paper.py)."""
import re

SECTION = re.compile(r'## 2\. SI: GENERAL METHODS\n(.*?)(?=\n## 3\. |\Z)', re.S)
BLOCK = re.compile(r'^\[(SI-P\d+-B\d+)[^\]]*\]\s?(.*)$')
# a full stop ends a sentence where a capital (or a bracket) follows and no bracket is open; not after a single letter ('E. Merck'),
# a short abbreviation ('Art.', 'ca.', 'Fig.', 'No.', 'vs.', 'Co.') or a short capitalised word
ABBREVIATIONS = {'art', 'ca', 'fig', 'figs', 'no', 'nos', 'vs', 'co', 'inc', 'ltd', 'corp', 'eq', 'equiv', 'approx', 'ref', 'refs', 'e.g', 'i.e', 'cf', 'et al', 'al', 'dr', 'mr', 'st', 'ph.d'}


def _sentences(text):
    """[(start, end)] of the sentences of `text`, spaces after each left out."""
    out, depth, begin = [], 0, 0
    for k, ch in enumerate(text):
        if ch in '([{': depth += 1
        elif ch in ')]}': depth = max(0, depth - 1)
        elif ch in '.!?' and depth == 0 and k + 1 < len(text):
            following = re.match(r'(?:\d{1,3}(?:[,–-]\d{1,3})*)?\s+(?=[A-Z(\[])', text[k + 1:])
            if following is None:
                continue
            end = k + 1 + following.end()
            nxt = text[end:end + 1]
            word = re.search(r'([A-Za-z.]+)$', text[begin:k])
            if nxt and (nxt.isupper() or nxt in '(['):
                w = word.group(1) if word else ''
                # nor after a short capitalised word, as a journal is abbreviated ('J. Am. Chem. Soc.'): joining two sentences loses nothing
                # Company abbreviations can terminate a sentence. Ordinary short
                # journal abbreviations/initials remain protected.
                new_sentence = bool(re.match(r'(?:All|Other|Analytical|Preparative|Organic|The|Reactions|Reaction|Solvents|Yields)\b', text[end:]))
                if word and (len(w) == 1 or w.lower() in ABBREVIATIONS or (w[0].isupper() and len(w) <= 4)) and not (new_sentence and w.lower() in {'inc', 'ltd', 'corp', 'co'}): continue
                # Keep the printed footnote with the sentence it annotates.
                out.append((begin, end)); begin = end
                while begin < len(text) and text[begin] == ' ': begin += 1
    if text[begin:].strip(): out.append((begin, len(text.rstrip())))
    return out


def items(package):
    """[{'id': 'g1', 'block_id', 'text'}] in printed order: every sentence of the package's general methods, as printed there."""
    found = SECTION.search(package)
    if not found: return []
    out = []
    for line in found.group(1).split('\n'):
        hit = BLOCK.match(line.strip())
        if not hit or not hit.group(2).strip(): continue
        text = hit.group(2)
        for a, b in _sentences(text):
            z = b
            while z > a and text[z - 1].isspace(): z -= 1
            out.append({'block_id': hit.group(1), 'text': text[a:z], 'start': a, 'end': z})
    for n, item in enumerate(out, 1): item['id'] = f'g{n}'
    return out


def show(package, general_items):
    """The package with its general methods section printed with the marks: each block's sentences, each after its ⟦gN⟧."""
    found = SECTION.search(package)
    if not found or not general_items: return package
    lines, by_block = [], {}
    for item in general_items: by_block.setdefault(item['block_id'], []).append(item)
    for block_id, its in by_block.items(): lines += [f'[{block_id}] ' + ' '.join(f"⟦{i['id']}⟧{i['text']}" for i in its), '']
    return package[:found.start(1)] + '\n' + '\n'.join(lines) + package[found.end(1):]


def applied(record, general_items):
    """(the record's blocks each with general_methods resolved to [{'mark', 'text', 'printed_in'}], the marks it names that do not exist)."""
    by_id = {i['id']: i for i in general_items}; unknown = []
    for block in record.get('blocks') or []:
        named = [normal_mark(m, by_id) for m in block.get('general_methods') or []]
        unknown += [m for m in named if m not in by_id]
        block['general_methods'] = [{'mark': m, 'text': by_id[m]['text'], 'printed_in': by_id[m]['block_id']} for m in dict.fromkeys(named) if m in by_id]
    return record, unknown


def normal_mark(value, by_id):
    original = str(value).strip()
    hit = re.fullmatch(r'[⟦{ ]*(g\d+)[⟧} ]*(.*)', original, re.S)
    if not hit or hit.group(1) not in by_id:
        return original
    name, tail = hit.groups()
    if not tail.strip():
        return name
    # A decorated ID with copied source words is accepted only if those words
    # agree with this saved manifest. A wrong ID/body pair stays unknown.
    key = lambda s: re.sub(r'\s+', '', s).casefold()
    return name if len(key(tail)) >= 8 and key(by_id[name]['text']).startswith(key(tail)) else original


def source_mapping(old, new):
    """Map old source sentences to new slices, without selecting applicable clauses.

    Occurrence order disambiguates repeated text in one source block. All children
    of a split old sentence require a new semantic selection.
    """
    out, cursors = [], {}
    for item in old:
        b = item['block_id']
        children = [n for n in new if n['block_id'] == b]
        # New items tile the same method block except whitespace. Reconstruct the
        # offsets from its slices and find the old exact wording, not new g IDs.
        length = max((n['end'] for n in children), default=0)
        chars = [' '] * length
        for n in children: chars[n['start']:n['end']] = n['text']
        source = ''.join(chars)
        start = source.find(item['text'], cursors.get(b, 0))
        if start < 0:
            out.append({'old_id': item['id'], 'block_id': b, 'status': 'UNMAPPED_SOURCE', 'new_ids': []})
            continue
        end = start + len(item['text']); cursors[b] = end
        hits = [n['id'] for n in children if n['start'] < end and start < n['end']]
        out.append({'old_id': item['id'], 'block_id': b, 'old_span': [start, end], 'new_ids': hits,
                    'status': 'SPLIT_REQUIRES_SELECTION' if len(hits) > 1 else 'SOURCE_EQUIVALENT'})
    return out
