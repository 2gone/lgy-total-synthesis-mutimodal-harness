"""Boxes for the structure drawings the molecule detector missed, in the figures the box check (box_check.py) says have drawings no box
holds. Two sources, kept apart so that what each covers can be measured:
  relook   the detector again, with a lower confidence threshold, on the figure set in the middle of a white page three times its size (a
           drawing touching the figure's edge is cut or missed there) and on four overlapping quarters of it, enlarged (small drawings and
           crowded figures); a box overlapping one it already gave is not new
  gemini   the vision model, shown the figure with the boxes it already has, gives a box for each structure drawing no box holds, drawn
           by the detector's own principle (BOX_RULE): the smallest rectangle holding every line, ring, atom symbol, charge, wedge and
           abbreviated group of one structure drawing -- not its label or name, not a caption, arrow or reagent, not another drawing
Boxes the check judged CUT (part of the drawing runs past the box) or MERGED (several drawings in one box) are mended from the same two
sources: the detector on the figure set on the white page; the vision model shown that one box, giving by BOX_RULE the box of every
drawing it touches. A mended box replaces the one it mends (replaces: mark).
Every candidate is then judged as the detector's boxes are (box_check.py's tiles): only a box judged OK is kept.

Writes figure-labels/<rid>.rebox.json: per figure the candidates {mark, bbox_px, source, verdict, note}; new marks go on after the
figure's last one (M0nn), so that a new box is named like any other. Credentials and the detector from the process environment only.
--cascade: the detector's candidates are judged first, and the vision model is asked only for the figures still short of boxes and the
boxes still not mended (OK boxes from the detector are drawn on the figure it is shown).
usage: rebox.py <rid> [<rid> ...] [--relook] [--gemini] [--cascade]   (both sources when neither is given)"""
import base64, collections, io, json, os, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from openai import OpenAI
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parent)); import detect_chunks, figure_marks, run_config, waits
HERE = Path(__file__).resolve().parent.parent
RELOOK_CONF, NEW_IOU = 0.10, 0.30
BOX_RULE = ("A box is the smallest rectangle that holds every line, ring, atom symbol, charge, wedge or hash and abbreviated group (Me, OBn, "
            "TBS, ...) of one chemical structure drawing. It does not hold the compound's printed label or name, a caption, an arrow, a "
            "reagent or condition, or any part of another drawing.")
# Two ways of a model's box not being tighter than the detector's (2026-10-16: its boxes cut atom symbols at the edge): WIDEN, the host
# widening every box it gives by this share of its larger side on each side; LOOSE, a sentence of the rule saying a box may be larger.
WIDEN = float(os.environ.get('REBOX_WIDEN', '0.05'))      # 2026-10-16, nine cut boxes of r025: none 6, 5% 7, 10% 7, the looser sentence 6 mended
LOOSE = ' A box may be a little larger than its drawing; it is never smaller.' if os.environ.get('REBOX_LOOSE') else ''
BOX_RULE = BOX_RULE + LOOSE
SYSTEM_GEMINI = ("You are shown a figure from a chemistry paper. A molecule detector drew a numbered box around each chemical structure drawing "
                 "it found; some drawings may have no box. " + BOX_RULE + " Give a box for each structure drawing that no drawn box holds, "
                 'and nothing for drawings that already have one. Answer as JSON: {"boxes": [{"box_2d": [ymin, xmin, ymax, xmax]}]} with '
                 "coordinates from 0 to 1000 over the whole image.")
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['OPENAI_BASE_URL'], timeout=120, max_retries=0)
detector = run_config.CONFIG['detector']
args = [a for a in sys.argv[1:] if not a.startswith('--')]
SOURCES = [s for s in ('relook', 'gemini') if f'--{s}' in sys.argv] or ['relook', 'gemini']
box_check_src = (HERE / 'tools/box_check.py').read_text(encoding='utf-8')
bc = {'__file__': str(HERE / 'tools/box_check.py'), '__name__': 'box_check'}; exec(compile(box_check_src, 'box_check.py', 'exec'), bc)      # its papers run only as __main__


def iou(a, b):
    w = max(0, min(a[2], b[2]) - max(a[0], b[0])); h = max(0, min(a[3], b[3]) - max(a[1], b[1])); inter = w * h
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter or 1)


def relook(images):
    """The detector on each image twice its size on a white page and on four overlapping quarters, at RELOOK_CONF: {name: [bbox_px]}."""
    work = Path(tempfile.mkdtemp()); jobs = {}
    for name, image in images.items():
        w, h = image.size; page = Image.new('RGB', (PAD * w, PAD * h), 'white'); page.paste(image, (w, h))
        page.save(work / f'{name}.page.png'); jobs[f'{name}.page.png'] = (name, w, h, 1.0)
        for i, (x, y) in enumerate([(0, 0), (w // 2 - w // 10, 0), (0, h // 2 - h // 10), (w // 2 - w // 10, h // 2 - h // 10)]):
            part = image.crop((x, y, min(w, x + w // 2 + w // 5), min(h, y + h // 2 + h // 5))); part = part.resize((part.width * 2, part.height * 2))
            part.save(work / f'{name}.q{i}.png'); jobs[f'{name}.q{i}.png'] = (name, -x, -y, 0.5)
    out = work / 'found.json'
    detect_chunks.detect([work / j for j in jobs], out, RELOOK_CONF, detector['iou_threshold'], detector['imgsz'])
    found = collections.defaultdict(list)
    for item in json.loads(out.read_text(encoding='utf-8'))['images'] if out.exists() else []:
        name, dx, dy, scale = jobs[Path(item['image']).name]; W, H = images[name].size
        for d in item['detections']:
            x0, y0, x1, y1 = d['bbox_px']
            box = [x0 * scale - dx, y0 * scale - dy, x1 * scale - dx, y1 * scale - dy] if scale != 1.0 else [x0 - dx, y0 - dy, x1 - dx, y1 - dy]
            box = [max(0, box[0]), max(0, box[1]), min(W, box[2]), min(H, box[3])]
            if box[2] - box[0] > 8 and box[3] - box[1] > 8: found[name].append([round(v, 1) for v in box])
    return found


def gemini(image, marks):
    picture = figure_marks.marked_image(image, [(m['mark'], m['bbox_px']) for m in marks]) if marks else image
    buffer = io.BytesIO(); picture.save(buffer, format='PNG'); url = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode(); W, H = image.size
    for attempt in range(waits.TRIES):
        try:
            r = client.chat.completions.create(model=run_config.model('vision'), reasoning_effort=run_config.effort('vision', 'box_check'), response_format={'type': 'json_object'},
                                               messages=[{'role': 'system', 'content': SYSTEM_GEMINI}, {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': url}}]}])
            text = r.choices[0].message.content or ''; answer = json.loads(text[text.index('{'):text.rindex('}') + 1])
            boxes = []
            for b in answer.get('boxes') or []:
                y0, x0, y1, x1 = [float(v) for v in (b.get('box_2d') or b.get('box') or b.get('bbox') or [])[:4]]      # the model names the key as it likes
                if x1 > x0 and y1 > y0:
                    box = [x0 * W / 1000, y0 * H / 1000, x1 * W / 1000, y1 * H / 1000]; m = WIDEN * max(box[2] - box[0], box[3] - box[1])
                    boxes.append([round(max(0, box[0] - m), 1), round(max(0, box[1] - m), 1), round(min(W, box[2] + m), 1), round(min(H, box[3] + m), 1)])
            return boxes, None
        except Exception as error:
            last = type(error).__name__ + ': ' + str(error)[:160]; time.sleep(waits.pause(error, attempt))
    return [], last


SYSTEM_FIX = ("You are shown a figure from a chemistry paper with one box drawn on it. It holds only part of a chemical structure drawing, or "
              "more than one drawing. " + BOX_RULE + " Give, by this rule, the box of every structure drawing the drawn box touches. Answer as "
              'JSON: {"boxes": [{"box_2d": [ymin, xmin, ymax, xmax]}]} with coordinates from 0 to 1000 over the whole image.')


PAD = 3      # the figure in the middle of a white page this many times its size: a drawing that touches the figure's edge is cut by the detector


def padded_detect(images):
    """The detector on each figure set in the middle of a white page PAD times its size, at RELOOK_CONF: {name: [bbox_px]} in the figure's
    own pixels. A drawing that touches the edge of its figure is boxed whole there, where at the edge the detector cuts it (2026-10-16:
    three such figures, cut at the edge, were boxed whole with confidence 0.86-0.91 on the white page; enlarged or shrunk they were cut)."""
    work = Path(tempfile.mkdtemp()); jobs = {}
    for name, image in images.items():
        w, h = image.size; page = Image.new('RGB', (PAD * w, PAD * h), 'white'); page.paste(image, (w, h))
        file = f'{len(jobs):05d}.png'; page.save(work / file); jobs[file] = (name, w, h)
    out = work / 'found.json'
    detect_chunks.detect([work / j for j in jobs], out, RELOOK_CONF, detector['iou_threshold'], detector['imgsz'])
    found = collections.defaultdict(list)
    for item in json.loads(out.read_text(encoding='utf-8'))['images'] if out.exists() else []:
        name, w, h = jobs[Path(item['image']).name]
        for d in item['detections']:
            x0, y0, x1, y1 = d['bbox_px']; box = [max(0, x0 - w), max(0, y0 - h), min(w, x1 - w), min(h, y1 - h)]
            if box[2] - box[0] > 8 and box[3] - box[1] > 8: found[name].append([round(v, 1) for v in box])
    return found


def relook_fix(images, wanted):
    """The detector on the white page around each figure with a box to mend: {(file, mark): [bbox_px]} (every box of that figure)."""
    found = padded_detect({f: images[f] for f in {w[0] for w in wanted}})
    return {(f, mark): found.get(f, []) for f, mark, _, _ in wanted}


def held(inner, outer):
    """The share of inner that lies inside outer."""
    w = max(0, min(inner[2], outer[2]) - max(inner[0], outer[0])); h = max(0, min(inner[3], outer[3]) - max(inner[1], outer[1]))
    return w * h / (((inner[2] - inner[0]) * (inner[3] - inner[1])) or 1)


def chosen(old, verdict, boxes):
    """The detector's mended boxes for one box: for CUT the largest box that holds most of the old one, for MERGED the boxes that lie
    mostly inside the old one (two or more)."""
    if verdict == 'CUT':
        fits = [b for b in boxes if held(old, b) >= 0.8 and (b[2] - b[0]) * (b[3] - b[1]) > 1.05 * (old[2] - old[0]) * (old[3] - old[1])]
        return [max(fits, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))] if fits else []
    inside = [b for b in boxes if held(b, old) >= 0.7 and (b[2] - b[0]) * (b[3] - b[1]) < 0.9 * (old[2] - old[0]) * (old[3] - old[1])]
    kept = []
    for b in sorted(inside, key=lambda b: -(b[2] - b[0]) * (b[3] - b[1])):
        if all(iou(b, k) < 0.5 for k in kept): kept.append(b)
    return kept if len(kept) >= 2 else []


def gemini_fix(image, mark, bbox):
    picture = figure_marks.marked_image(image, [(mark, bbox)])
    buffer = io.BytesIO(); picture.save(buffer, format='PNG'); url = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode(); W, H = image.size
    for attempt in range(waits.TRIES):
        try:
            r = client.chat.completions.create(model=run_config.model('vision'), reasoning_effort=run_config.effort('vision', 'box_check'), response_format={'type': 'json_object'},
                                               messages=[{'role': 'system', 'content': SYSTEM_FIX}, {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': url}}]}])
            text = r.choices[0].message.content or ''; answer = json.loads(text[text.index('{'):text.rindex('}') + 1]); boxes = []
            for b in answer.get('boxes') or []:
                y0, x0, y1, x1 = [float(v) for v in (b.get('box_2d') or b.get('box') or b.get('bbox') or [])[:4]]      # the model names the key as it likes
                if x1 > x0 and y1 > y0:
                    box = [x0 * W / 1000, y0 * H / 1000, x1 * W / 1000, y1 * H / 1000]; m = WIDEN * max(box[2] - box[0], box[3] - box[1])
                    boxes.append([round(max(0, box[0] - m), 1), round(max(0, box[1] - m), 1), round(min(W, box[2] + m), 1), round(min(H, box[3] + m), 1)])
            return boxes, None
        except Exception as error:
            last = type(error).__name__ + ': ' + str(error)[:160]; time.sleep(waits.pause(error, attempt))
    return [], last


def judge(rid, items):
    """box_check's tiles over candidate boxes: items are (key, file, {'mark', 'bbox_px'}); {key: (verdict, note)}."""
    out = {}
    def look(start):
        part = [(k + 1, f, m) for k, (_, f, m) in enumerate(items[start:start + bc['TILES']])]
        answer, _, error = bc['ask'](bc['SYSTEM_BOXES'], [{'type': 'text', 'text': f'Tiles 1 to {len(part)}.'}, bc['png'](bc['sheet'](rid, part))], 'candidates')
        said = {a.get('tile'): a for a in (answer or {}).get('tiles') or [] if isinstance(a, dict)}
        return [(items[start + n - 1][0], (said.get(n) or {}).get('verdict'), (said.get(n) or {}).get('note')) for n, _, _ in part]
    with ThreadPoolExecutor(max_workers=12) as pool:
        for chunk in pool.map(look, range(0, len(items), bc['TILES'])):
            for key, verdict, note in chunk: out[key] = (verdict, note)
    return out


CASCADE = '--cascade' in sys.argv      # the detector first; the vision model only for what the detector's boxes, judged, left undone


def named_and_judged(rid, marks_of, candidates, cands):
    """Marks for new candidates (after the figure's last mark and the candidates already named), then box_check's tiles over them."""
    items = []
    for f, c in cands:
        last = max([int(m['mark'][1:]) for m in marks_of[f]] + [int(x['mark'][1:]) for x in candidates[f] if x.get('mark')] or [0])
        c['mark'] = f'M{last + 1:03d}'; candidates[f].append(c); items.append(((f, c['mark']), f, c))
    verdicts = judge(rid, items) if items else {}
    for (f, mark), _, c in items: c['verdict'], c['note'] = verdicts.get((f, mark), (None, None))


for rid in args:
    began = time.monotonic(); check = json.loads((HERE / 'figure-labels' / f'{rid}.boxcheck.json').read_text(encoding='utf-8'))
    marks_of = bc['figures'](rid)
    said = {x['file']: x['unboxed_drawings'] for x in check['figures'] if isinstance(x.get('unboxed_drawings'), int) and x['unboxed_drawings'] > 0 and x['file'] in marks_of}
    flagged = list(said)
    images = {f: Image.open(HERE / 'figure-labels' / f'{rid}.images' / f).convert('RGB') for f in flagged}
    # the boxes to mend: those the check judged CUT or MERGED
    mend = [(x['file'], b['mark'], next(m['bbox_px'] for m in marks_of[x['file']] if m['mark'] == b['mark']), b['verdict'])
            for x in check['figures'] for b in x['boxes'] if b['verdict'] in ('CUT', 'MERGED') and x['file'] in marks_of]
    for f in {w[0] for w in mend}: images.setdefault(f, Image.open(HERE / 'figure-labels' / f'{rid}.images' / f).convert('RGB'))
    candidates, round_ = collections.defaultdict(list), []
    if 'relook' in SOURCES and flagged:
        for f, boxes in relook({f: images[f] for f in flagged}).items():
            kept = []
            for b in sorted(boxes, key=lambda b: -(b[2] - b[0]) * (b[3] - b[1])):
                if all(iou(b, m['bbox_px']) < NEW_IOU for m in marks_of[f]) and all(iou(b, k) < 0.5 for k in kept): kept.append(b)
            round_ += [(f, {'bbox_px': b, 'source': 'relook'}) for b in kept]
    if 'relook' in SOURCES and mend:
        around = relook_fix(images, mend)
        for f, mark, old, verdict in mend: round_ += [(f, {'bbox_px': b, 'source': 'relook', 'replaces': mark, 'mends': verdict}) for b in chosen(old, verdict, around.get((f, mark), []))]
    if CASCADE:
        named_and_judged(rid, marks_of, candidates, round_); round_ = []
        ok = collections.Counter((f, c.get('replaces')) for f, cs in candidates.items() for c in cs if c['verdict'] == 'OK')
        left_figs = [f for f in flagged if ok[(f, None)] < said[f]]
        left_mend = [w for w in mend if ok[(w[0], w[1])] < (1 if w[3] == 'CUT' else 2)]
    else: left_figs, left_mend = flagged, mend
    if 'gemini' in SOURCES and left_figs:
        with ThreadPoolExecutor(max_workers=12) as pool:
            for f, (boxes, error) in zip(left_figs, pool.map(lambda f: gemini(images[f], marks_of[f] + [{'mark': c['mark'], 'bbox_px': c['bbox_px']} for c in candidates[f] if c['verdict'] == 'OK' and not c.get('replaces')]), left_figs)):
                round_ += [(f, {'bbox_px': b, 'source': 'gemini'}) for b in boxes if all(iou(b, m['bbox_px']) < NEW_IOU for m in marks_of[f])]
    if 'gemini' in SOURCES and left_mend:
        with ThreadPoolExecutor(max_workers=12) as pool:
            for (f, mark, old, verdict), (boxes, error) in zip(left_mend, pool.map(lambda w: gemini_fix(images[w[0]], w[1], w[2]), left_mend)):
                round_ += [(f, {'bbox_px': b, 'source': 'gemini', 'replaces': mark, 'mends': verdict}) for b in boxes if iou(b, old) < 0.95]
    named_and_judged(rid, marks_of, candidates, round_)
    files = list(dict.fromkeys(flagged + [w[0] for w in mend]))
    rows = [{'file': f, 'unboxed_drawings_said': next((x['unboxed_drawings'] for x in check['figures'] if x['file'] == f), None), 'candidates': candidates.get(f, [])} for f in files]
    def mended(source, verdict):
        ok = collections.Counter((r['file'], c['replaces']) for r in rows for c in r['candidates'] if c.get('replaces') and c['source'] in source and c['verdict'] == 'OK')
        return sum(1 for f, mark, _, v in mend if v == verdict and ok[(f, mark)] >= (1 if verdict == 'CUT' else 2))
    summary = {'figures with drawings no box holds': len(flagged), 'drawings said to be without a box': sum(r['unboxed_drawings_said'] or 0 for r in rows if r['file'] in flagged),
               **{f'new boxes, {s}: candidates / judged OK': [sum(1 for r in rows for c in r['candidates'] if c['source'] == s and not c.get('replaces')),
                                                             sum(1 for r in rows for c in r['candidates'] if c['source'] == s and not c.get('replaces') and c['verdict'] == 'OK')] for s in SOURCES},
               'boxes to mend (CUT, MERGED)': [sum(1 for w in mend if w[3] == 'CUT'), sum(1 for w in mend if w[3] == 'MERGED')],
               **{f'mended, {name}': [mended(src, 'CUT'), mended(src, 'MERGED')] for name, src in (('relook', ('relook',)), ('gemini', ('gemini',)), ('either', ('relook', 'gemini'))) if set(src) <= set(SOURCES)},
               **({'cascade: asked of the vision model (figures, boxes to mend)': [len(left_figs), len(left_mend)]} if CASCADE else {}), 'seconds': round(time.monotonic() - began)}
    (HERE / 'figure-labels' / f'{rid}.rebox.json').write_text(json.dumps({'summary': summary, 'figures': rows}, ensure_ascii=False, indent=1), encoding='utf-8')
    print(rid, json.dumps(summary, ensure_ascii=False), flush=True)
