"""Step 6 of the chain: the drawings of the paper bound to the compounds of its result.

The figures were already read once, before the chain: the molecule detector boxed every drawing and gave it a mark, and the figure reader
wrote the label printed beside each mark (figure-transcript/). After the record, that reading is reused here: each box is bound to the
compound of the result that carries its printed label, by the binding rule of the harness (tsdaf.workflow.structure_binding) --
exactly one compound carrying the label binds, several are AMBIGUOUS, none is UNBOUND, a box with no label is NO_LABEL. Nothing is guessed
and no model is called. The bound boxes are the drawings the result needs; their structures (SMILES) come from the OCSR side, returned per
box into ocsr-results/<paper>.ocsr.json, and are attached when they are there.

usage (offline check): structure_step.py <paper> <run dir>"""
import importlib.util, json, re, sys
from pathlib import Path
TOOLS = Path(__file__).resolve().parent; HERE = TOOLS.parent
sys.path.insert(0, str(TOOLS)); import run_config
_spec = importlib.util.spec_from_file_location('structure_binding', TOOLS / 'src-units/tsdaf/workflow/structure_binding.py')
binding = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(binding)


# the shape of a compound label: a number with an optional letter and primes, after optional prefixes joined by hyphens or written
# against it (14a, SI-4, S-1, S1, C5-epi-3, epi-20)
LABEL = re.compile(r"(?:[A-Za-z]{1,6}\d{0,2}-)*[A-Za-z]{0,3}\d{1,3}[a-z]?['′]*")


def readings(paper):
    """The figure reader's transcripts of this paper's figures: the boxed ones, then the ones read without boxes (later ones win)."""
    out = {}
    for name in ('transcript-medium.json', 'transcript-fill.json'):
        path = HERE / 'figure-transcript' / name
        if path.exists():
            # a copy of the main text's reading (same_as) is the same figure: its drawings are bound once, under the main text's name
            out.update({t['file']: t for t in json.loads(path.read_text(encoding='utf-8')) if t['file'].startswith(paper + '.') and not t.get('error') and not t.get('same_as')})
    pages = HERE / 'figure-labels' / f'{paper}.page-figures.json'      # the pages without running text, boxed whole (page_boxes.py)
    if pages.exists(): out.update({t['file']: t for t in json.loads(pages.read_text(encoding='utf-8'))})
    mended = HERE / 'figure-labels' / f'{paper}.rebox-figures.json'      # the figures whose boxes rebox.py mended or added to (rebox_apply.py)
    if mended.exists(): out.update({t['file']: t for t in json.loads(mended.read_text(encoding='utf-8'))})
    return out


def overlay(paper):
    """Our own reading in the shape the binding rule reads: one scheme per figure, one detection per boxed mark."""
    returned = HERE / run_config.CONFIG['ocsr']['results_dir'] / f'{paper}.ocsr.json'
    ocsr = json.loads(returned.read_text(encoding='utf-8')) if returned.exists() else {}
    schemes = []
    for file, t in sorted(readings(paper).items()):
        boxes = {m['mark']: m.get('bbox_px') for m in t.get('marks') or []}
        detections = []
        for molecule in (t.get('transcript') or {}).get('molecules') or []:
            if not isinstance(molecule, dict) or not molecule.get('mark'): continue
            detection_id = f"{file}#{molecule['mark']}"
            detections.append({'detection_id': detection_id, 'crop_sha256': None, 'bbox_px': boxes.get(molecule['mark']),
                               'printed_label': molecule.get('label'), 'ocsr': ocsr.get(detection_id) or {}})
        if detections: schemes.append({'logical_name': file, 'asset_sha256': None, 'detections': detections})
    provenance = {'boxes': 'molecule detector (run_config.json: detector), marks drawn on the figure before it was read',
                  'labels': f"figure reader ({run_config.model('vision')}), the label printed beside each mark",
                  'structures': str(returned.relative_to(HERE)) if returned.exists() else None}
    return {'schemes': schemes, 'provenance': provenance}, bool(ocsr)


def canon(text, drop_sign=False):
    """A wording compared without its typography: dashes and minus signs alike, no spaces or hyphens, no case, a leading "compound"
    dropped; with drop_sign also a leading sign of rotation ((+)-, (-)-, (±)-), for names."""
    t = re.sub(r'[‐‑‒–—−]', '-', binding.normalize_label(text or '')).strip()
    t = re.sub(r'^(?:compounds?|cmpds?\.?)\s+', '', t, flags=re.I)
    if drop_sign: t = re.sub(r'^\((?:\+|-|±|\+/-)\)-?', '', t)
    return re.sub(r'[\s-]+', '', t).lower()


def readings_of(printed):
    """What a printed caption may stand for, by its form only: a list of parts (one per compound it names), each a list of wordings to
    try in turn. A list ("10, 11", "14a and 14b", "9a: Z = OH, 9b: Z = H") is split; a remark after a colon is dropped; "name (label)"
    and "label (remark)" try the bracket and the words around it; "3a/b" and "14a-c" are the labels they abbreviate."""
    text = re.sub(r'[‐‑‒–—−]', '-', binding.normalize_label(printed or '')).strip()
    text = re.sub(r'^(?:compounds?|cmpds?\.?)\s+', '', text, flags=re.I)
    parts, pieces = [], []
    # a list is split at a comma followed by a space, a semicolon, "and", "&" or a new line; a comma with no space after it ("2,3-cis")
    # splits only where every piece is a label ("10,11")
    for piece in re.split(r'\s*(?:,\s+|;|&|\band\b|\n)\s*(?![^()]*\))', text):
        bits = piece.split(',')
        pieces += bits if len(bits) > 1 and all(LABEL.fullmatch(b.strip()) for b in bits) else [piece]
    stem = None
    for part in pieces:
        part = re.split(r'\s*:\s*', part.strip())[0].strip()
        if not part: continue
        if re.fullmatch(r'[a-z]', part) and stem: part = stem + part       # "11a, b": the letter goes with the number before it
        stem = (re.fullmatch(r'(.*?\d+)[a-z]', part) or [None, None])[1]
        m = re.fullmatch(r'(.*?)\s*\(([^()]+)\)', part); tries = [part] + ([m.group(2).strip(), m.group(1).strip()] if m else [])
        expanded = []
        for t in tries:
            slash = re.fullmatch(r'(.*?\d+)([a-z])((?:\s*/\s*[a-z])+)', t); span = re.fullmatch(r'(.*?\d+)([a-zA-Z])-(?:\1)?([a-zA-Z])', t)
            if slash: parts += [[slash.group(1) + x] for x in [slash.group(2)] + re.findall(r'[a-z]', slash.group(3))]; expanded = None; break
            if span and span.group(2) < span.group(3): parts += [[span.group(1) + chr(c)] for c in range(ord(span.group(2)), ord(span.group(3)) + 1)]; expanded = None; break
            if '/' in t and all(LABEL.fullmatch(x.strip()) for x in t.split('/')): parts += [[x.strip()] for x in t.split('/')]; expanded = None; break
            expanded.append(t)
        if expanded: parts.append(expanded)
    return parts


def by_form(bound, named):
    """Boxes the binding rule left UNBOUND, bound by the form of their printed caption (readings_of): to the compound whose label it holds
    once its typography is set aside, or once a sign of rotation or configuration printed before the label is ((+)-1 for 1, when the
    paper has no compound labelled (+)-1), or else whose name it is. A caption that names several compounds binds its box to each of them
    (BOUND_SHARED, with the others named): one drawing standing for several. What was printed is kept in printed_as."""
    rows = bound['compounds']; ids = [r['compound_id'] for r in rows]
    by_label = {}; by_name = {}
    for r, c in zip(rows, named):
        for l in r['author_labels']: by_label.setdefault(canon(l), set()).add(r['compound_id'])
        for n in (c.get('name'),):
            if n: by_name.setdefault(canon(n, True), set()).add(r['compound_id'])
    row_of = dict(zip(ids, rows)); still = []; counts = {'bound by form': 0, 'bound shared': 0}
    for item in bound['unresolved']:
        if item['binding'] != 'UNBOUND': still.append(item); continue
        found = []
        for tries in readings_of(item['printed_label']):
            hit = next((by_label[canon(t)] for t in tries if len(by_label.get(canon(t), ())) == 1), None) or \
                  next((by_label[canon(bare)] for t in tries for bare in [re.sub(r'^(?:\((?:\+|-|±|\+/-|R|S|rac)\)-?|rac-|ent-)', '', re.sub(r'[‐‑‒–—−]', '-', t.strip()))] if bare != t.strip() and len(by_label.get(canon(bare), ())) == 1), None) or \
                  next((by_name[canon(t, True)] for t in tries if len(by_name.get(canon(t, True), ())) == 1), None)
            if hit and next(iter(hit)) not in found: found.append(next(iter(hit)))
        if not found: still.append(item); continue
        shared = len(found) > 1
        for cid in found:
            row_of[cid]['structures'].append({**item, 'binding': 'BOUND_SHARED' if shared else 'BOUND', 'printed_as': item.get('printed_as') or item['printed_label'],
                                              **({'shared_with': [row_of[x]['author_labels'][0] for x in found if x != cid]} if shared else {})})
        counts['bound shared' if shared else 'bound by form'] += 1
    bound['unresolved'] = still
    s = bound['summary']; moved = counts['bound by form'] + counts['bound shared']
    s['unbound'] -= moved; s['bound'] += moved; s.update({k.replace(' ', '_'): v for k, v in counts.items()})
    s['compounds_with_a_drawing'] = sum(1 for r in rows if r['structures'])


def bind(paper, label, result):
    """(the binding written beside the result, its summary for result.json). The compounds bound to are those of the paper-level record
    and every further label the reaction records use (a substrate, a product, something isolated): the paper-level list can miss one."""
    # the paper-level list can name one label twice (two forms of one compound, an entry listed twice): one compound, its names and
    # entries joined -- two compounds under one label would make every drawing of it AMBIGUOUS
    named, at = [], {}
    for c in ((result.get('paper_record') or {}).get('compounds')) or []:
        key = binding.normalize_label(str(c.get('label') or ''))
        if not key: continue
        if key in at:
            kept = at[key]; kept['name'] = kept.get('name') or c.get('name')
            kept['made_in_entries'] = sorted(set(kept.get('made_in_entries') or []) | set(c.get('made_in_entries') or []), key=str)
        else: at[key] = dict(c); named.append(at[key])
    seen = set(at)
    for block in result.get('reaction_blocks') or []:
        record = block.get('record') or {}
        for value in (record.get('substrates') or []) + (record.get('products') or []) + [o.get('product') for o in record.get('outcomes') or []]:
            # a record may name the compound around its label ("compound 12", "enone 9"): the label is the last word that holds a digit;
            # a value with none ("substrate", a mixture described in words) adds nothing
            words = [w.strip('()[],;.:') for w in str(value or '').split()]
            found = next((w for w in reversed(words) if LABEL.fullmatch(w)), None)
            if found and binding.normalize_label(found) not in seen:
                seen.add(binding.normalize_label(found)); named.append({'label': found, 'name': str(value).strip() if found != str(value).strip() else None, 'role': 'FROM_THE_REACTION_RECORDS', 'made_in_entries': [block.get('entry')]})
    # the label segmentation gave each procedure is a compound the paper prepares, also when the paper-level list missed it
    for block in result.get('reaction_blocks') or []:
        found = str(block.get('compound_label_as_segmented') or '').strip()
        if found and not re.search(r'\band\b|,', found) and binding.normalize_label(found) not in seen:
            seen.add(binding.normalize_label(found)); named.append({'label': found, 'name': None, 'role': 'FROM_THE_SEGMENTATION', 'made_in_entries': [block.get('entry')]})
    machine = {'paper_id': paper, 'machine_result_id': label, 'domain_payload': {'compounds': [{'compound_id': f'C{i + 1:03d}', 'author_labels': [c['label']]} for i, c in enumerate(named)]}}
    reading, has_structures = overlay(paper)
    # A drawing is often captioned with the compound's name and its number in brackets ("principinol B (1)", "(1)"). When the printed
    # words are not a label of the result as they stand, the bracketed part is, if it is one: the box binds by it, the printed words stay.
    known = binding.label_index(machine['domain_payload']['compounds'])
    for scheme in reading['schemes']:
        for d in scheme['detections']:
            printed = str(d.get('printed_label') or '').strip(); m = re.fullmatch(r'(.*?)\s*\(([^()]+)\)', printed)
            if printed and binding.normalize_label(printed) not in known and m and binding.normalize_label(m.group(2)) in known and len(readings_of(printed)) == 1:
                d['printed_as'] = printed; d['printed_label'] = m.group(2).strip()
    bound = binding.bind_structures(machine, reading)
    printed_as = {d['detection_id']: d['printed_as'] for sc in reading['schemes'] for d in sc['detections'] if 'printed_as' in d}
    for item in [x for row in bound['compounds'] for x in row['structures']] + bound['unresolved']:
        if item['detection_id'] in printed_as: item['printed_as'] = printed_as[item['detection_id']]
    by_form(bound, named)
    if not has_structures:      # before the OCSR side returns, there is nothing to agree or disagree
        for row in bound['compounds']: row['readings_agree'] = None
        bound['summary']['compounds_whose_drawings_disagree'] = None
    for row, c in zip(bound['compounds'], named): row.update(name=c.get('name'), role=c.get('role'), made_in_entries=c.get('made_in_entries'))
    for scheme in reading['schemes']:
        for d in scheme['detections']:
            if d['bbox_px'] is None: d['note'] = 'read without a box: there is no crop for the OCSR side'
    needed = [{'detection_id': s['detection_id'], 'figure': s['figure'], 'bbox_px': s['bbox_px'], 'printed_label': s['printed_label'], 'compound_id': row['compound_id']}
              for row in bound['compounds'] for s in row['structures']]
    bound['needed_for_ocsr'] = needed
    summary = {**bound['summary'], 'figures read': len(reading['schemes']), 'drawings the result needs': len(needed),
               'of them without a box': sum(1 for n in needed if n['bbox_px'] is None), 'structures returned by OCSR': has_structures}
    return bound, summary


if __name__ == '__main__':
    paper, run = sys.argv[1], Path(sys.argv[2])
    result = json.loads((run / 'result.json').read_text(encoding='utf-8'))
    bound, summary = bind(paper, run.name, result)
    if '--write' in sys.argv:      # the binding made again (structures returned by the OCSR side): written where the chain writes it
        (run / 'structure-binding.json').write_text(json.dumps(bound, ensure_ascii=False, indent=1), encoding='utf-8')
        result['structures'] = {'file': 'structure-binding.json', 'summary': summary}
        (run / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
    print('unbound labels:', sorted({u['printed_label'] for u in bound['unresolved'] if u['binding'] == 'UNBOUND'}), '| ambiguous:', sorted({u['printed_label'] for u in bound['unresolved'] if u['binding'] == 'AMBIGUOUS'}))
    labelled = [c['author_labels'] for c in bound['compounds'] if c['author_labels'] and not c['structures']]
    print('labelled compounds with no drawing bound:', labelled)
