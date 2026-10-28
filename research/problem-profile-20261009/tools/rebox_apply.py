"""The boxes rebox.py found and the box check judged OK, made part of their figures. The figures' own readings (the transcripts,
page-figures.json) are not changed: figure-labels/<rid>.rebox-figures.json holds, per figure that changed, the whole figure again in
the form of the transcripts (file, marks, transcript.molecules[mark, label]), and the readers of the boxes (structure_step.py,
ocsr_pack.py, box_check.py) take it after the others, as a later reading.

  CUT       the mending box takes the place of the cut one under the same mark: the drawing is the same, so is its label
  MERGED    the box holding several drawings gives way to the mending boxes (two or more), each a new mark
  missed    a box of a drawing no box held is a new mark
When two sources mend one box, the detector's box is taken (the boxes the rest of the figure has are the detector's).
Which compound a new mark is: on a page that names exactly one compound (page_owners.py), that one (the host's part); elsewhere the
part of the figure around the new marks is read with those marks drawn, by the figure reader's own prompt (as the figures were read;
the whole figure when that part is most of it), and only the new marks take what it says -- the marks already read keep their labels.

Writes figure-labels/<rid>.rebox-figures.json: rows as above, with box_source {mark: detector | relook | gemini}, replaced {mark: the
cut box}, split {merged mark: [new marks]}, checked {mark: [verdict, note]} for the boxes that came from rebox, labels_from, error.
Credentials from the process environment only (OPENAI_*). usage: rebox_apply.py <rid> [<rid> ...]"""
import base64, io, json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from openai import OpenAI
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parent)); import figure_marks, run_config, waits
HERE = Path(__file__).resolve().parent.parent
BOXED = re.search(r'SYSTEM = """(.*?)"""', (HERE / 'tools/figure_transcribe.py').read_text(encoding='utf-8'), re.S).group(1)      # as the figures were read
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['OPENAI_BASE_URL'], timeout=300, max_retries=0)


def readings(rid):
    """The figures' readings as the binding takes them: the transcripts (copies and failed readings left out), then the pages."""
    out = {}
    for name in ('transcript-medium.json', 'transcript-fill.json'):
        path = HERE / 'figure-transcript' / name
        if path.exists(): out.update({t['file']: t for t in json.loads(path.read_text(encoding='utf-8')) if t['file'].startswith(rid + '.') and not t.get('error') and not t.get('same_as')})
    pages = HERE / 'figure-labels' / f'{rid}.page-figures.json'
    if pages.exists(): out.update({t['file']: t for t in json.loads(pages.read_text(encoding='utf-8'))})
    return out


def read(image, marks):
    buffer = io.BytesIO(); figure_marks.marked_image(image, [(m['mark'], m['bbox_px']) for m in marks]).save(buffer, format='PNG')
    url = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()
    for attempt in range(waits.TRIES):
        try:
            r = client.chat.completions.create(model=run_config.model('vision'), reasoning_effort=run_config.effort('vision', 'figure_transcribe'), response_format={'type': 'json_object'},
                messages=[{'role': 'system', 'content': BOXED}, {'role': 'user', 'content': [{'type': 'text', 'text': 'Marks drawn in this figure: ' + ', '.join(m['mark'] for m in marks) + '.'},
                                                                                             {'type': 'image_url', 'image_url': {'url': url}}]}])
            text = r.choices[0].message.content or ''; t = json.loads(text[text.index('{'):text.rindex('}') + 1])
            return {m.get('mark'): m.get('label') for m in t.get('molecules') or [] if isinstance(m, dict)}, None
        except Exception as error:
            last = type(error).__name__ + ': ' + str(error)[:160]; time.sleep(waits.pause(error, attempt))
    return {}, last


def apply(rid):
    began = time.monotonic(); found = HERE / 'figure-labels' / f'{rid}.rebox.json'
    if not found.exists(): print(rid, 'no rebox.json'); return
    figs = readings(rid); owners = {}
    if (HERE / 'figure-labels' / f'{rid}.owners.json').exists():
        owners = {f"{rid}.SI-P{o['page']:04d}-PAGE.png": o.get('compounds') or [] for o in json.loads((HERE / 'figure-labels' / f'{rid}.owners.json').read_text(encoding='utf-8'))}
    rows, to_read = [], []
    # labels read for new marks before (an earlier run of this tool): the same mark with the same box is not read again
    before = {}
    out_file = HERE / 'figure-labels' / f'{rid}.rebox-figures.json'
    if out_file.exists():
        for r in json.loads(out_file.read_text(encoding='utf-8')):
            boxes = {m['mark']: [round(v) for v in m['bbox_px']] for m in r.get('marks') or []}
            for x in (r.get('transcript') or {}).get('molecules') or []:
                if x.get('mark') in (r.get('new') or []): before[(r['file'], x['mark'], tuple(boxes.get(x['mark']) or []))] = (x.get('label'), r.get('labels_from'))
    for f in json.loads(found.read_text(encoding='utf-8'))['figures']:
        file, ok = f['file'], [c for c in f['candidates'] if c.get('verdict') == 'OK']
        if not ok or file not in figs: continue
        t = figs[file]; marks = [dict(m) for m in t.get('marks') or []]; labels = {m.get('mark'): m.get('label') for m in (t.get('transcript') or {}).get('molecules') or [] if isinstance(m, dict)}
        source, replaced, split, checked, new = {m['mark']: 'detector' for m in marks}, {}, {}, {}, []
        menders = {}
        for c in ok:
            if c.get('replaces'): menders.setdefault(c['replaces'], []).append(c)
        for mark, cs in menders.items():
            old = next((m for m in marks if m['mark'] == mark), None)
            if old is None: continue
            kind = cs[0].get('mends'); by = [c for c in cs if c['source'] == 'relook'] or cs      # the detector's box first
            if kind == 'CUT':
                c = max(by, key=lambda c: (c['bbox_px'][2] - c['bbox_px'][0]) * (c['bbox_px'][3] - c['bbox_px'][1]))
                replaced[mark] = old['bbox_px']; old['bbox_px'] = c['bbox_px']; source[mark] = c['source']; checked[mark] = [c['verdict'], c.get('note')]
            elif kind == 'MERGED' and len(by) >= 2:
                marks = [m for m in marks if m['mark'] != mark]; labels.pop(mark, None); source.pop(mark, None); split[mark] = [c['mark'] for c in by]
                for c in by: marks.append({'mark': c['mark'], 'bbox_px': c['bbox_px']}); source[c['mark']] = c['source']; checked[c['mark']] = [c['verdict'], c.get('note')]; new.append(c['mark'])
        for c in ok:
            if not c.get('replaces'):
                marks.append({'mark': c['mark'], 'bbox_px': c['bbox_px']}); source[c['mark']] = c['source']; checked[c['mark']] = [c['verdict'], c.get('note')]; new.append(c['mark'])
        if not (replaced or split or new): continue
        marks = sorted(marks, key=lambda m: m['mark'])
        row = {**{k: v for k, v in t.items() if k not in ('marks', 'transcript')}, 'file': file, 'marks': marks, 'box_source': source, 'replaced': replaced, 'split': split,
               'checked': checked, 'labels': labels, 'new': new, 'labels_from': None, 'error': None}
        if new and len(owners.get(file) or []) == 1:
            for mark in new: row['labels'][mark] = owners[file][0]
            row['labels_from'] = 'the page (the compound the page characterizes)'
        elif new:
            known = {mark: before.get((file, mark, tuple(round(v) for v in next(m['bbox_px'] for m in marks if m['mark'] == mark)))) for mark in new}
            if all(known.values()):
                for mark, (label, how) in known.items(): row['labels'][mark] = label
                row['labels_from'] = next(iter(known.values()))[1]
            else: to_read.append(row)
        rows.append(row)
    def label(row):
        # only the new marks are read, on the part of the figure around them (wide enough for a label printed under or beside a
        # drawing); the rest of the figure was read already. Where that part is most of the figure, the whole figure is read.
        image = Image.open(HERE / 'figure-labels' / f'{rid}.images' / row['file']).convert('RGB'); w, h = image.size
        new = [m for m in row['marks'] if m['mark'] in row['new']]
        x0, y0 = min(m['bbox_px'][0] for m in new), min(m['bbox_px'][1] for m in new); x1, y1 = max(m['bbox_px'][2] for m in new), max(m['bbox_px'][3] for m in new)
        pad = max(60, 0.6 * max(x1 - x0, y1 - y0)); cut = [max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)]
        if (cut[2] - cut[0]) * (cut[3] - cut[1]) < 0.7 * w * h:
            part = image.crop(tuple(round(v) for v in cut)); shown = [{'mark': m['mark'], 'bbox_px': [m['bbox_px'][0] - round(cut[0]), m['bbox_px'][1] - round(cut[1]), m['bbox_px'][2] - round(cut[0]), m['bbox_px'][3] - round(cut[1])]} for m in new]
            said, error = read(part, shown); how = 'the figure reader, on the part of the figure around the new marks'
        else: said, error = read(image, row['marks']); how = 'the figure reader (new marks only)'
        for mark in row['new']: row['labels'][mark] = said.get(mark)
        row['labels_from'], row['error'] = how, error
    with ThreadPoolExecutor(max_workers=12) as pool: list(pool.map(label, to_read))
    for row in rows:
        labels = row.pop('labels'); row['transcript'] = {**(figs[row['file']].get('transcript') or {}), 'molecules': [{'mark': m['mark'], 'label': labels.get(m['mark'])} for m in row['marks']]}
    (HERE / 'figure-labels' / f'{rid}.rebox-figures.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
    print(rid, json.dumps({'figures changed': len(rows), 'cut boxes replaced': sum(len(r['replaced']) for r in rows), 'merged boxes split': sum(len(r['split']) for r in rows),
                           'new marks': sum(len(r['new']) for r in rows), 'figures read': len(to_read), 'read errors': sum(1 for r in rows if r['error']),
                           'new marks with a label': sum(1 for r in rows for m in r['new'] for x in r['transcript']['molecules'] if x['mark'] == m and x['label']),
                           'seconds': round(time.monotonic() - began)}), flush=True)


for rid in sys.argv[1:]: apply(rid)
