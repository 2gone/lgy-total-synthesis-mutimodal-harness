"""Offline host mechanics shared by recording and assembly; no provider, filesystem or credentials."""
import copy
import re
import unicodedata
from functools import lru_cache
import quantity_marks as qm


def figure_transcripts(paper, figure_index, rows):
    """Bind the saved reader records by canonical visual_id, never image basename."""
    good = [r for r in rows if str(r.get('file') or '').startswith(paper+'.') and not r.get('error') and r.get('transcript') is not None]
    by_file = {r['file']:r['transcript'] for r in good}
    by_block = {r['block']:r['transcript'] for r in good if r.get('block')}
    # an older figure index (block_id and file only, as on mfy-win) has no visual_id: its figures bind by block alone (2026-10-25)
    key = lambda r: paper+'.'+r['visual_id']+'.png' if r.get('visual_id') else None
    return {r['block_id']:by_block.get(r['block_id'], by_file.get(key(r)))
            for r in figure_index if r['block_id'] in by_block or key(r) in by_file}


def figure_text(block_id, transcript):
    """Carry already-recorded printed labels/conditions only, never infer structure."""
    label = {m['mark']: (m.get('label') or 'unlabelled drawing '+m['mark']) + (' ('+m['note']+')' if m.get('note') else '') for m in transcript.get('molecules') or []}
    lines = [f'[FIGURE {block_id}. Previously recorded figure-reader transcript; no image or new reading.]']
    if label:
        lines.append('drawings: '+'; '.join(label.values()))
    for arrow in transcript.get('arrows') or []:
        frm = ' + '.join(label.get(x,x) for x in arrow.get('from') or []) or '?'
        to = ' + '.join(label.get(x,x) for x in arrow.get('to') or []) or '?'
        kind = ' ['+arrow['kind']+']' if arrow.get('kind') not in (None,'reaction') else ''
        lines.append(frm+' → '+to+kind+': '+' | '.join(arrow.get('text') or []))
    if transcript.get('table'):
        lines += ['table:']+[' | '.join(map(str,row)) for row in transcript['table']]
    for field in ['legend','other_text']:
        if transcript.get(field):lines.append(field+': '+' / '.join(transcript[field]))
    return '\n'.join(lines)


def input_blocks(index, units):
    """Attach only adjacent data units explicitly labelled for this one procedure.

    Null, multi-object and mismatched labels are not identity evidence. The caller keeps
    all original units; these handles define a new input version, never an old replay.
    """
    unit = units[index]
    ids = list(unit['block_ids'])
    linked = []
    label = str(unit.get('compound_label') or '').strip()
    if not label or re.search(r'\band\b|,', label):
        return ids, linked
    for k in range(index + 1, len(units)):
        u = units[k]
        if u['kind'] != 'CHARACTERIZATION' or str(u.get('compound_label') or '').strip() != label:
            break
        linked.append(k)
        ids.extend(b for b in u['block_ids'] if b not in ids)
    return ids, linked


@lru_cache(maxsize=2048)
def _key(text):
    # Location only: removing corrupt glyphs or a degree sign is not quantity repair.
    return ''.join(ch for ch in qm.squeeze(text) if not unicodedata.category(ch).startswith('C') and ch != '°')


@lru_cache(maxsize=128)
def _location_text(text):
    compact, offsets = [], []
    # qm.squeeze is not characterwise (TeX/spacing), so do exact matching first below.
    for k, ch in enumerate(text):
        for c in _key(ch):
            compact.append(c)
            offsets.append(k)
    return ''.join(compact), offsets


def _positions(needle, text):
    joined, offsets = _location_text(text)
    key = _key(needle)
    if not key:
        return []
    return [(offsets[m.start()], offsets[m.end() - 1] + 1) for m in re.finditer(re.escape(key), joined)]


def locate(record, texts):
    actions, scopes = [], []
    for bi, block in enumerate(record.get('blocks') or []):
        for action in block.get('actions') or []:
            actions.append(action)
            scopes.append((bi, action.get('vessel')))
    order = {b: k for k, b in enumerate(texts)}
    options = []
    for a in actions:
        needle = a.get('begins_with') or ''
        # Include both clean and glyph-damaged occurrences; otherwise a clean later
        # occurrence can falsely win before context gets a chance to disambiguate.
        hits = [(order[b], at, b) for b, t in texts.items() for at, end in _positions(needle, t['plain'])]
        options.append(hits)
    chosen = [x[0] if len(x) == 1 else None for x in options]
    changed = True
    while changed:
        changed = False
        for k, hits in enumerate(options):
            if chosen[k] is not None or len(hits) < 2:
                continue
            low = next((chosen[j] for j in range(k - 1, -1, -1) if chosen[j] is not None and scopes[j] == scopes[k]), None)
            high = next((chosen[j] for j in range(k + 1, len(chosen)) if chosen[j] is not None and scopes[j] == scopes[k]), None)
            fits = [h for h in hits if (low is None or h[:2] >= low[:2]) and (high is None or h[:2] <= high[:2])]
            if len(fits) == 1 and (low is not None or high is not None):
                chosen[k] = fits[0]
                changed = True
    return [[c[2], c[0], c[1]] if c else None for c in chosen]


def order_checked(record, texts, places):
    actions, scopes = [], []
    for bi, block in enumerate(record.get('blocks') or []):
        for action in block.get('actions') or []:
            actions.append(action)
            scopes.append((bi, action.get('vessel')))
    places, review = list(places), []
    for k, here in enumerate(places):
        if here is None:
            continue
        # Shared table protocols are not a temporal ordering of table rows.
        if '<table' in texts[here[0]]['plain']:
            continue
        for j in range(k + 3, len(places)):
            if places[j] != here or scopes[j] != scopes[k]:
                continue
            middle = [places[m] for m in range(k + 1, j) if places[m] and scopes[m] == scopes[k]]
            if any(p[1:] < here[1:] for p in middle):
                review.append({'action': k, 'same_place_as': j, 'reaction_block': scopes[k][0] + 1,
                               'what': actions[k].get('what'), 'begins_with': actions[k].get('begins_with')})
                places[k] = None
                break
    return places, review


def quantity_safety(record, texts, marks):
    """A derivative with unsafe quantities quarantined; preserve every author's/model word.

    Source controls are not decoded. Explicit old marks are authoritative for replay.
    Block-level risk is used only when the quantity could not be located at all.
    """
    out = copy.deepcopy(record)
    by_ref = {m['ref']: m for m in marks}
    risks = []
    bad = {b: [i for i, c in enumerate(t['plain']) if unicodedata.category(c) == 'Cc' and c not in '\n\r\t'] for b, t in texts.items()}
    for path, q in qm.quantities_of(out.get('blocks') or []):
        ref = str(q.get('ref') or '').strip('{}? ')
        raw = q.get('raw') or ''
        mark = by_ref.get(ref)
        reasons, source = [], []
        if not ref and not raw.strip():
            reasons.append('EMPTY_QUANTITY')
        elif ref and mark is None:
            reasons.append('UNKNOWN_MARK')
        if mark:
            source = [(mark['block_id'], mark['start'], mark['end'])]
        elif raw.strip() and any(bad.values()):
            source = [(b, start, end) for b, t in texts.items() for start, end in _positions(raw, t['plain'])]
        if any(any(start - 2 <= at < end + 2 for at in bad.get(b, [])) for b, start, end in source):
            reasons.append('SOURCE_CONTROL_CHARACTER')
        if not source and any(bad.values()) and raw.strip():
            reasons.append('UNLOCATED_QUANTITY_IN_DAMAGED_ENTRY')
        if any(unicodedata.category(c) == 'Cc' and c not in '\n\r\t' for c in raw):
            reasons.append('RAW_CONTROL_CHARACTER')
        if reasons:
            q['host_validation'] = {'status': 'REVIEW_REQUIRED', 'reasons': reasons,
                                    'recorded_value': q.get('value'), 'recorded_unit': q.get('unit')}
            q.update(value=None, unit=None, usable=False)
            risks.append({'path': list(path), 'ref': ref or None, 'raw': raw or None,
                          'reasons': reasons, 'source_candidates': source})
    return out, risks


def frames_by_marks(frames, results, units, blocks, text_of, dm, unassigned, audit, kinds=(), flatten=lambda s:s):
    """Use saved marks; no regenerated IDs, no unselected preparation refill.

    Unselected data is retained separately without asserting a compound identity.
    Model-selected coarse mixed spans stay as selected, explicitly pending review.
    """
    made = {}
    for r in results:
        if not r.get('record') or not r.get('data_marks'):
            continue
        unit = units[r['si_unit']]
        ids = r.get('input_block_ids') or unit['block_ids']
        texts = {b: {'plain': text_of(blocks[b])} for b in ids}
        if not dm.tiles(texts, r['data_marks']):
            audit.append({'entry': r['n'], 'status': 'STORED_MARKS_DO_NOT_TILE'})
            continue
        def piece(b, start, end, compound, origin, marks):
            row = blocks[b]
            source = '\n'.join([(row.get('raw_text') or '').strip(), *[c for c in row.get('captions') or [] if len(c) > 200 or (row['type'] == 'table' and r.get('input_block_ids'))]]).strip()
            at = dm.to_source(texts[b]['plain'], source)
            a, z = at(start), at(end)
            text = source[a:z].strip()
            kind = next((name for name, pattern in kinds if re.search(pattern, flatten(text), re.I)), None)
            return {'compound_label': compound, 'compound_label_as_segmented': unit.get('compound_label'),
                    'data_kind': kind or 'DATA_IN_PROCEDURE_UNIT', 'si_unit': r['si_unit'], 'entry': r['n'],
                    'block_id': b, 'evidence_id': row['evidence_id'], 'text': text,
                    'compound_from': origin, 'data_marks': marks, 'source_span': [a, z]}
        selected, covered = [], set()
        for compound, spans, marks in dm.spans(r['record'], r['data_marks']):
            covered.update(marks)
            for b, start, end in spans:
                f = piece(b, start, end, compound or unit.get('compound_label'), 'entry record', marks)
                if f['text']:
                    # Diagnostic only; never substitute freshly numbered boundaries.
                    fresh = dm.items({b: texts[b]})
                    if any(not x['data'] and x.get('boundary_basis') == 'operation_sentence' and start <= x['start'] < end for x in fresh):
                        f['host_review'] = 'MODEL_SELECTED_SPAN_CONTAINS_OPERATION_BOUNDARY'
                    selected.append(f)
        for m in r['data_marks']:
            if m['id'] in covered:
                continue
            if m['data']:
                f = piece(m['block_id'], m['start'], m['end'], None, 'unselected data; identity unresolved', [m['id']])
                if f['text']:
                    unassigned.append(f)
            else:
                audit.append({'entry': r['n'], 'block_id': m['block_id'], 'mark': m['id'],
                              'span': [m['start'], m['end']], 'status': 'NONDATA_NOT_REFILLED'})
        made[r['n']] = selected
    attached = {b for r in results if r['n'] in made for b in r.get('input_block_ids', []) if b not in units[r['si_unit']]['block_ids']}
    replaced_text = {f['block_id'] for fs in made.values() for f in fs} | {f['block_id'] for f in unassigned}
    out, done = [], set()
    for f in frames:
        n = f.get('entry')
        if n in made:
            if n not in done:
                out.extend(made[n])
                done.add(n)
        elif not (f.get('block_id') in attached and f.get('block_id') in replaced_text and f.get('text') and not f.get('asset_ref') and f.get('data_kind') not in ('FIGURE','PAGE')):
            out.append(f)
    return out + [f for n, fs in made.items() if n not in done for f in fs]


# ---- paper-level candidates the host lists for review (owner 2026-10-26); nothing in the model's record is changed -------------------
def _canon(text):
    """A wording compared without typography: dashes and minus signs alike, no spaces or hyphens, no case, a leading "compound" dropped.
    A sign of rotation or an ent-/rac- prefix is kept: (+)-X and (-)-X, X and ent-X are different compounds."""
    t = re.sub(r'[‐‑‒–—−]', '-', str(text or '')).strip()
    t = re.sub(r'^(?:compounds?|cmpd\.?)\s+', '', t, flags=re.I)
    return re.sub(r'[\s-]+', '', t).lower()


_STEREO_LEAD = re.compile(r'^\s*(?:\((?:[+\-−±]|[RSEZ](?:\s*,\s*\d*[RSEZ])*|\d+[RSEZ](?:\s*,\s*\d+[RSEZ])*)\)|rac|ent)\s*-\s*', re.I)      # as paper_task.py


def target_alias_candidates(record):
    """Targets that may be one compound under two labels (a main-text and an SI label, a name repeated): those printed with the same name,
    or made in the same SI entries, joined into groups. The model is told to give one compound once only where the paper says so (P2), so
    a paper that prints 2b in its main text and S2b in its SI without saying they are one leaves both; here they are listed with what
    joins them, for a person or a later structure match to decide. Two structures under one name (a proposed and a revised one) are joined
    too: the basis says only what is shared."""
    targets = [c for c in (record or {}).get('compounds') or [] if c.get('role') == 'TARGET' and (c.get('label') or c.get('name'))]
    parent = list(range(len(targets))); why = {}
    def root(i):
        while parent[i] != i: parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for i in range(len(targets)):
        for j in range(i + 1, len(targets)):
            a, b = targets[i], targets[j]; basis = []
            if a.get('name') and b.get('name') and _canon(a['name']) == _canon(b['name']): basis.append(f'the same name "{a["name"]}"')
            # one entry often makes several different compounds (isomers, a product and its by-product): sharing an entry joins two
            # targets only when their labels are also the same once a stereo prefix (ent-, rac-, (+)-, (S)-) is set aside
            shared = sorted(set(a.get('made_in_entries') or []) & set(b.get('made_in_entries') or []))
            bare = lambda c: _canon(_STEREO_LEAD.sub('', str(c.get('label') or '')))
            if shared and bare(a) and bare(a) == bare(b): basis.append('labels the same but for a stereo prefix, made in the same SI entries ' + ', '.join(map(str, shared)))
            if basis: parent[root(j)] = root(i); why[(i, j)] = basis
    groups = {}
    for i in range(len(targets)): groups.setdefault(root(i), []).append(i)
    out = []
    for members in groups.values():
        if len(members) < 2: continue
        out.append({'labels': [targets[i].get('label') for i in members],
                    'members': [{'label': targets[i].get('label'), 'name': targets[i].get('name'), 'made_in_entries': targets[i].get('made_in_entries') or [],
                                 'printed_in': targets[i].get('printed_in') or []} for i in members],
                    'basis': [{'labels': [targets[i].get('label'), targets[j].get('label')], 'shared': b} for (i, j), b in why.items() if i in members]})
    return out


def products_not_in_compounds(record, reaction_blocks):
    """Products the entry records report with an amount or a yield that the paper-level compounds do not hold, by label or by name: a
    compound the paper names without a label (an ent- target, a named precursor) is left out of a list defined as the labelled compounds.
    Listed with the entries and blocks that report it; the paper-level record is not changed."""
    keys = set()
    for c in (record or {}).get('compounds') or []:
        for v in (c.get('label'), c.get('name')):
            if v: keys.add(_canon(v))
    found = {}
    for b in reaction_blocks or []:
        rec = b.get('record') or {}
        # what the block makes: its products, and what its outcomes report an amount or a yield for (an outcome may be a side product)
        names = [str(p).strip() for p in rec.get('products') or []]
        names += [str(o.get('product') or '').strip() for o in rec.get('outcomes') or [] if any(q.get('value') is not None or q.get('raw') or q.get('marked_raw') for q in o.get('quantities') or [])]
        for name in dict.fromkeys(n for n in names if n):
            parts = [name] + [p.strip() for p in re.findall(r'\(([^()]+)\)', name)] + [re.sub(r'\s*\([^()]*\)\s*$', '', name).strip()]
            if any(_canon(p) in keys for p in parts if _canon(p)): continue
            # several labelled compounds named together ("9a and 9b") are already listed; a name that only points at another compound
            # (the starting material, the title compound) is no compound of its own
            pieces = [x for x in re.split(r'\s*(?:,|\band\b|\+|&|/)\s*', name) if x.strip()]
            if len(pieces) > 1 and all(_canon(x) in keys for x in pieces): continue
            if re.search(r'starting material|title compound', name, re.I): continue
            f = found.setdefault(_canon(name), {'product': name, 'entries': [], 'sources': []})
            if b['entry'] not in f['entries']: f['entries'].append(b['entry'])
            f['sources'] += [s for s in b.get('sources') or [] if s not in f['sources']]
    return list(found.values())
