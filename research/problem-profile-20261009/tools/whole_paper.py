"""One paper end to end in the new shape, with the time of every stage:
  given      the parse, the figure transcripts and figure labels (pre-stage products, made once per paper, not timed here)
  1 segment  the SI is divided into reading units (flash, windows with memory)
  2 package  the host lays the paper out (entry package) and extracts the printed quantities by rule for reconciliation
  3 record   in parallel: one flash task per SI experiment entry (main text + its entry, host reconciliation) and one paper-level task
  4 frame    the host copies characterization data verbatim from the units; no model writes it
  5 assemble the host joins everything into one result; author disagreements are reported apart from the blocks
  6 structures the boxed and labelled drawings of the figures bound to the compounds by printed label; SMILES from the OCSR side when returned
Not the sealed chain: the result is a bench JSON, not a published contract artifact. Credentials from the process environment only.
usage: whole_paper.py <paper> <label> [--reuse-units <dir holding <paper>.units.json>] [--record v4|stages|actions] [--si-text __clean] [--model <model>]
       [--repair]: in the run directory <label> already there, ask again only what is missing -- the entries without a record (a lost connection,
       a rate limit), the paper task if it failed -- and assemble the result again; the segmentation and the package are kept."""
import collections, json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent; TOOLS = HERE / 'tools'; FIX = HERE.parents[1] / 'tests/fixtures'
paper, label = sys.argv[1], sys.argv[2]
reuse = Path(sys.argv[sys.argv.index('--reuse-units') + 1]) if '--reuse-units' in sys.argv else None
RECORD = sys.argv[sys.argv.index('--record') + 1] if '--record' in sys.argv else 'actions'
if '--model' in sys.argv: os.environ['TSDAF_WORKFLOW_MODEL'] = sys.argv[sys.argv.index('--model') + 1]      # overrides the workflow model of tools/run_config.json
if '--si-text' in sys.argv: os.environ['SI_TEXT'] = sys.argv[sys.argv.index('--si-text') + 1]      # '__clean': read the SI text the pre-stage settled
sys.path.insert(0, str(TOOLS)); from stages_from_actions import derive; import run_config, data_marks as dm, general_marks as gm; from quantity_rule import text_of
import host_repairs as hr
REPAIR = '--repair' in sys.argv
run = HERE / 'whole-paper' / label
if REPAIR and not (run / 'units').exists(): raise SystemExit(f'--repair: no run to repair at {run}')
run.mkdir(parents=True, exist_ok=REPAIR)
timings, python = {}, sys.executable


def step(name, command, env=None, log=None):
    began = time.monotonic()
    done = subprocess.run(command, env={**os.environ, **(env or {})}, stdout=open(run / (log or f'{name}.log'), 'w'), stderr=subprocess.STDOUT, cwd=HERE)
    timings[name] = round(time.monotonic() - began, 1); print(time.strftime('%H:%M:%S'), name, timings[name], 's', 'exit', done.returncode, flush=True)
    return done.returncode


began = time.monotonic()
units_dir = run / 'units'
if REPAIR: timings['1 segment'] = None
elif reuse is None:
    code = step('1 segment', [python, str(TOOLS / 'si_units_probe.py'), paper, str(units_dir)], {'PROBE_LANE': 'WINDOWS', 'PROBE_FIGURES': os.environ.get('PROBE_FIGURES', 'transcripts'), 'PROBE_MEMORY': '1', 'PROBE_ROWS': '60', 'PROBE_SAFE': '1', 'PROBE_EFFORT': os.environ.get('SEGMENT_EFFORT', run_config.effort('workflow', 'segment')), **({'PROBE_FALLBACK': 'medium'} if os.environ.get('SEGMENT_EFFORT') == 'high' else {})})
    if code != 0 or not (units_dir / f'{paper}.units.json').exists(): raise SystemExit('segmentation did not return units; see 1 segment.log')
else:
    units_dir.mkdir(); (units_dir / f'{paper}.units.json').write_bytes((reuse / f'{paper}.units.json').read_bytes()); timings['1 segment'] = None
if not REPAIR: step('2 package', [python, str(HERE.parent / 'fullscore-handoff-4papers-20261011/tools/build_review_pack.py'), paper, str(units_dir / f'{paper}.units.json'), str(run / 'pack')], log='2 package-quantities.log')
if not REPAIR: timings['2 package'] += step('2b', [python, str(TOOLS / 'entry_package.py'), paper, str(units_dir), str(run / 'package')], log='2 package-layout.log') and 0 or timings.pop('2b')
with ThreadPoolExecutor(max_workers=2) as pool:
    entries = pool.submit(step, '3 record: entry tasks', [python, str(TOOLS / 'record_entries.py'), str(run / 'pack'), str(run / 'package/package.md'), str(run / 'entries')] if RECORD == 'v4' else
                           [python, str(TOOLS / (sys.argv[sys.argv.index('--record-tool') + 1] if '--record-tool' in sys.argv else 'record_actions.py')), str(run / 'pack'), str(run / 'package/package.md'), str(run / 'entries'), '--form', RECORD] + (['--resume'] if REPAIR else []))
    paper_failed = not (run / 'paper.json').exists() or json.loads((run / 'paper.json').read_text(encoding='utf-8')).get('record') is None
    whole = pool.submit(step, '3 record: paper task', [python, str(TOOLS / 'paper_task.py'), str(run / 'package/package.md'), str(run / 'paper.json')]) if (paper_failed or not REPAIR) else None
    entries.result(); whole and whole.result()
timings['3 record (wall, both in parallel)'] = max(timings['3 record: entry tasks'], timings.get('3 record: paper task') or 0)

t = time.monotonic()
blocks = {}
for line in (FIX / 'si_packages' / (paper + os.environ.get('SI_TEXT', '')) / 'normalized/blocks.jsonl').read_text(encoding='utf-8').splitlines():
    row = json.loads(line); blocks['SI-P%04d-B%04d' % (row['page_idx'], row['reading_order'])] = row
units = json.loads((units_dir / f'{paper}.units.json').read_text())['units']
KINDS = [('RF', r'^\W*R\W*f\b'), ('1H_NMR', r'^\W*1\W*H\W*NMR'), ('13C_NMR', r'^\W*13\W*C\W*NMR'), ('OTHER_NMR', r'^\W*(?:19\W*F|31\W*P|11\W*B|29\W*Si|2D)\W*NMR'), ('HRMS', r'^\W*(?:HR-?MS|HR-?ESI|MS\b|LRMS|ESI-?MS)'),
         ('IR', r'^\W*(?:FT-?)?IR\b'), ('MP', r'^\W*(?:mp|m\.p\.|melting point)\b'), ('ROTATION', r'^\W*(?:\[\W*(?:α|alpha|a)\W*\]|optical rotation)'), ('UV', r'^\W*UV'), ('ELEMENTAL', r'^\W*(?:Anal|elemental)')]
DATA = re.compile(r'(?:R\W*f\W*=?\W*0\W*\.|\[\W*(?:α|alpha)\W*\]|(?:1\W*H|13\W*C|19\W*F|31\W*P)\W{0,6}NMR|\bIR\W*\(|HRMS|HR-?ESI|\bm\.?p\.?\W|melting point)', re.I)
flat = lambda text: re.sub(r'\s+', ' ', re.sub(r'[$^_{}\\]', ' ', re.sub(r'</?(?:sup|sub|b|i)>|\\(?:text|mathrm|mathbf)\b|\bbf\b', ' ', text or ''))).strip()
owners_file = HERE / 'figure-labels' / f'{paper}.owners.json'
owners = json.loads(owners_file.read_text(encoding='utf-8')) if owners_file.exists() else []
by_page = {o['page'] for o in owners}          # the pages kept whole (below); their pictures are not kept one by one
frames, entry_number = [], 0
for index, unit in enumerate(units):
    if unit['kind'] == 'PROCEDURE': entry_number += 1
    for block_id in unit['block_ids']:
        row = blocks[block_id]; text = '\n'.join([(row.get('raw_text') or '').strip(), *[c for c in (row.get('captions') or []) if len(c) > 200]]).strip()
        if unit['kind'] == 'CHARACTERIZATION' and row['type'] in ('image', 'chart') and row.get('asset_ref') and row['page_idx'] not in by_page:
            # a picture inside a compound's data is that compound's characterization as well: kept as the picture
            frames.append({'compound_label': unit['compound_label'], 'data_kind': 'FIGURE', 'si_unit': index, 'block_id': block_id, 'evidence_id': row['evidence_id'], 'page': row['page_idx'], 'asset_ref': row['asset_ref']})
            continue
        if not text: continue
        if unit['kind'] == 'CHARACTERIZATION':
            kind = next((name for name, pattern in KINDS if re.search(pattern, flat(text), re.I)), None)
            frames.append({'compound_label': unit['compound_label'], 'data_kind': kind, 'si_unit': index, 'block_id': block_id, 'evidence_id': row['evidence_id'], 'text': text})
        elif unit['kind'] == 'PROCEDURE' and row['type'] != 'table':
            cut = DATA.search(text)
            if cut and cut.start() > 80:   # the data printed after the procedure in the same paragraph
                frames.append({'compound_label': unit['compound_label'], 'data_kind': 'DATA_AFTER_PROCEDURE', 'si_unit': index, 'entry': entry_number, 'block_id': block_id, 'evidence_id': row['evidence_id'], 'text': text[cut.start():]})
            elif cut and block_id != unit['block_ids'][0]:
                # A later block of the entry that opens with data: the division left the data inside the procedure unit. It is framed all the
                # same, so that what the host keeps verbatim does not depend on where the units were cut.
                kind = next((name for name, pattern in KINDS if re.search(pattern, flat(text), re.I)), None)
                frames.append({'compound_label': unit['compound_label'], 'data_kind': kind or 'DATA_IN_PROCEDURE_UNIT', 'si_unit': index, 'entry': entry_number, 'block_id': block_id, 'evidence_id': row['evidence_id'], 'text': text})
# A page of the experimental document without running text that the page reader said characterizes one or more compounds
# (page_owners.py) is a characterization record of each of them too, beside their data from the text and from the units: the rendered
# page itself, with its page number in the joined document and in the paper's own document.
for o in owners:
    for compound in o.get('compounds') or []:
        frames.append({'compound_label': compound, 'data_kind': 'PAGE', 'page': o['page'], 'document': o.get('document'), 'page_in_document': o.get('page_in_document'), 'image': o['image']})
timings['4 frame'] = round(time.monotonic() - t, 2)

t = time.monotonic()
recorded = json.loads((run / 'entries/results.json').read_text()); paper_record = json.loads((run / 'paper.json').read_text())


def frames_by_record(frames, results):
    """Data printed inside a procedure unit belongs to the compound the entry's record says it is for: the record points at it
    (characterization_frames: the compound and the first words of its data). Each frame of the unit is cut where a pointed start of
    another compound than the one before is printed in it, and each piece goes to the compound pointed at; what comes before the first pointed start, and a frame where no start
    is printed, keep the compound the segmentation gave. compound_from says which of the two gave it. Nothing is read from the chemistry."""
    skip = lambda ch: ch.isspace() or ch in '$^_{}\\'
    def find(text, words):
        keep = [i for i, ch in enumerate(text) if not skip(ch)]; flat = ''.join(text[i] for i in keep).lower()
        needle = ''.join(ch for ch in words or '' if not skip(ch)).lower(); at = flat.find(needle) if needle else -1
        return keep[at] if at >= 0 else -1
    pointed = {r['n']: list((r.get('record') or {}).get('characterization_frames') or []) for r in results}
    out = []
    for f in frames:
        if 'compound_from' in f: out.append(f); continue      # cut at the record's data marks already (frames_by_marks)
        if f.get('entry') is None or 'text' not in f: out.append({**f, 'compound_from': 'segmentation'}); continue
        cuts = sorted({(at, p['compound'].strip()) for p in pointed.get(f['entry'], []) for at in [find(f['text'], p.get('starts_with'))] if at >= 0 and p.get('compound', '').strip()})
        cuts = [c for k, c in enumerate(cuts) if k == 0 or c[1] != cuts[k - 1][1]]      # cut only where the compound changes
        if not cuts: out.append({**f, 'compound_from': 'segmentation'}); continue
        if cuts[0][0] > 0 and f['text'][:cuts[0][0]].strip(): out.append({**f, 'text': f['text'][:cuts[0][0]], 'compound_from': 'segmentation'})
        for k, (at, compound) in enumerate(cuts):
            end = cuts[k + 1][0] if k + 1 < len(cuts) else len(f['text'])
            out.append({**f, 'compound_label': compound, 'compound_label_as_segmented': f['compound_label'], 'text': f['text'][at:end], 'compound_from': 'entry record'})
    return out


def frames_by_marks(frames, results):
    return hr.frames_by_marks(frames, results, units, blocks, text_of, dm, unassigned_characterization, frame_audit, KINDS, flat)


unassigned_characterization, frame_audit = [], []
frames[:] = frames_by_marks(frames, recorded['results']) if RECORD != 'v4' else frames
frames[:] = frames_by_record(frames, recorded['results']) if RECORD != 'v4' else frames
# A page's compound as the page reader copied it may be the label, the name, or both ("name (label)", "label (a remark)"): the host
# finds it among the paper's compounds, by label first and then by name; what was copied stays beside it.
known = [c for c in (paper_record.get('record') or {}).get('compounds') or [] if c.get('label') or c.get('name')]
# the paper-level list can miss a compound; the label segmentation gave a procedure or its data is one as well
known += [{'label': l} for l in dict.fromkeys(str(u['compound_label']).strip() for u in units if u.get('compound_label') and u['kind'] in ('PROCEDURE', 'CHARACTERIZATION'))
          if not re.search(r'\band\b|,', l) and l not in {str(c.get('label')) for c in known}]
def canon(text, drop_sign=False):
    """A wording compared without its typography: dashes and minus signs alike, no spaces or hyphens, no case, a leading "compound"
    dropped; with drop_sign also a leading sign of rotation ((+)-, (-)-, (±)-), for names."""
    t = re.sub(r'[‐‑‒–—−]', '-', str(text or '')).strip()
    t = re.sub(r'^(?:compounds?|cmpd\.?)\s+', '', t, flags=re.I)
    if drop_sign: t = re.sub(r'^\((?:\+|-|±|\+/-)\)-?', '', t)
    return re.sub(r'[\s-]+', '', t).lower()
def compound_of(printed):
    """(the compound, the compounds it could be): by label first, then by name. Where one label is carried by several compounds of
    different names, the copied wording does not say which is meant: the first is given and all of them are listed."""
    plain = printed.strip(); inner = re.fullmatch(r'(.*?)\s*\(([^()]+)\)', plain); tries = [plain] + ([inner.group(2).strip(), inner.group(1).strip()] if inner else [])
    which = lambda hits: (hits[0].get('label') or hits[0].get('name'), [{'label': c.get('label'), 'name': c.get('name')} for c in hits] if len(hits) > 1 else None)
    for t in tries:      # by label: as printed, then without typography
        hits = [c for c in known if c.get('label') and str(c['label']).strip() == t] or [c for c in known if c.get('label') and canon(c['label']) == canon(t)]
        if hits: return which(hits)
    for t in tries:      # by name (a label may be a name too), the sign of rotation aside
        hits = [c for c in known if any(n and canon(n, True) == canon(t, True) for n in (c.get('name'), c.get('label')))]
        if hits: return which(hits)
    return None, None
for f in frames:
    if f['data_kind'] == 'PAGE':
        f['compound'], candidates = compound_of(f['compound_label'])
        if candidates: f['compound_candidates'] = candidates
procedure_units = [(i, u) for i, u in enumerate(units) if u['kind'] == 'PROCEDURE']
reaction_blocks = []
disagreements, entry_notes, nonpreparation_records = [], [], []
for (index, unit), result in zip(procedure_units, recorded['results']):
    record = (result['record'] if RECORD == 'v4' else result['resolved']) or {}
    if result.get('entry_kind') == 'NOT_A_PREPARATION':
        nonpreparation_records.append({'entry': result['n'], 'si_unit': index, 'source_block_ids': unit['block_ids'], 'record': record, 'original_record': result.get('record')})
        disagreements += [{'entry': result['n'], **d} for d in record.get('differences') or []]
        entry_notes += [{'entry': result['n'], 'what': u} for u in record.get('unresolved') or []]
        continue
    for at, block in enumerate(record.get('blocks') or [None]):
        actions = None
        if block is not None and RECORD == 'actions':     # stages are the host's reading of the action list; the list itself is kept beside them
            actions = block['actions']; block = {**{k: v for k, v in block.items() if k != 'actions'}, 'steps': derive(block, 'addition')}
        left = ([raw for _, raw, state in result['check'] or [] if state != 'TYPED'] if RECORD == 'v4' else {k: (result['check'] or {}).get(k) for k in ('unplaced', 'repeated', 'unknown', 'not_printed', 'printed_apart')}) if at == 0 else []
        reaction_blocks.append({'entry': result['n'], 'block_in_entry': at + 1, 'si_unit': index, 'compound_label_as_segmented': unit['compound_label'], 'sources': [blocks[b]['evidence_id'] for b in unit['block_ids']],
                                'record': block, 'actions': actions, 'entry_kind': result.get('entry_kind'), 'reconciliation_left': left,
                                # actions the host left unplaced for review: printed only where a later action is (record_actions.order_checked)
                                'order_review': (result.get('order_review') or []) if at == 0 else []})
    disagreements += [{'entry': result['n'], **d} for d in record.get('differences') or []]
    entry_notes += [{'entry': result['n'], 'what': u} for u in record.get('unresolved') or []]
cost = recorded['cost']; assert len(procedure_units) == len(recorded['results']), 'entries recorded and procedure units differ'
result = {'paper': paper, 'label': label, 'si_text': os.environ.get('SI_TEXT', ''), 'models': run_config.summary(), 'record_tool': sys.argv[sys.argv.index('--record-tool') + 1] if '--record-tool' in sys.argv else 'record_actions.py', 'paper_record': paper_record['record'],
          # the SI general methods as the entry package prints them, sentence by sentence (tools/general_marks.py), kept whole at the paper level
          'si_general_methods': gm.items((run / 'package/package.md').read_text(encoding='utf-8')) if (run / 'package/package.md').exists() else [], 'reaction_blocks': reaction_blocks, 'characterization_records': frames, 'unassigned_characterization_records': unassigned_characterization, 'frame_audit': frame_audit, 'quantity_risks': [{'entry': r['n'], **x} for r in recorded['results'] for x in r.get('quantity_risks', [])], 'nonpreparation_records': nonpreparation_records, 'author_disagreements': disagreements,
          'unresolved': entry_notes + [{'entry': None, 'what': u} for u in ((paper_record['record'] or {}).get('unresolved') or [])],
          'timings_seconds': timings, 'wall_seconds': None,
          'tokens': {'entry tasks': {'calls': cost.get('calls'), 'input': cost.get('input'), 'output': cost.get('output'), 'failed_tries': cost.get('failed_tries', 0), 'longest_call': recorded['longest_call']},
                     'paper task': {'input': paper_record['input'], 'output': paper_record['output'], 'error': paper_record['error']}}}
# what the host lists for review at the paper level (owner 2026-10-26), the model's record unchanged: targets that may be one compound under
# two labels, and products the entry records report that the paper-level compounds do not hold (an unlabelled ent- target, a named precursor)
result['paper_level_candidates'] = {'target_alias_candidates': hr.target_alias_candidates(paper_record['record']),
                                    'products_not_in_compounds': hr.products_not_in_compounds(paper_record['record'], reaction_blocks)}
timings['5 assemble'] = round(time.monotonic() - t, 2)
# 6 structures: each drawing the detector boxed and the figure reader labelled before the chain is bound to a compound of this result by
# its printed label (tools/structure_step.py); the bound ones are the drawings the result needs, and their SMILES are attached once the OCSR
# side has returned them. No model is called here. Written beside the result as a sidecar: every structure in it is a model reading.
t = time.monotonic(); import structure_step
bound, structures = structure_step.bind(paper, label, result)
(run / 'structure-binding.json').write_text(json.dumps(bound, ensure_ascii=False, indent=1), encoding='utf-8')
result['structures'] = {'file': 'structure-binding.json', 'summary': structures}
timings['6 structures'] = round(time.monotonic() - t, 2); result['wall_seconds'] = round(time.monotonic() - began, 1)
(run / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1))
kinds = collections.Counter(f['data_kind'] or 'UNNAMED' for f in frames)
print(f"{label}: {paper} | wall {result['wall_seconds']}s | " + ' | '.join(f'{k} {v}' for k, v in timings.items()))
print(f"  reaction blocks {len(reaction_blocks)} (without a record {sum(b['record'] is None for b in reaction_blocks)}) | reaction stages {sum(s['kind'] == 'REACTION_STAGE' for b in reaction_blocks if b['record'] for s in b['record']['steps'])} | "
      f"characterization records {len(frames)} {dict(kinds)} | compounds {len((paper_record['record'] or {}).get('compounds', []))} | route items {len((paper_record['record'] or {}).get('route', []))} | "
      f"differences reported {dict(collections.Counter(d['kind'] for d in disagreements))} | unresolved {len(result['unresolved'])}")
