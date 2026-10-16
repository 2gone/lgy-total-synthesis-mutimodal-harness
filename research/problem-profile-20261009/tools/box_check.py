"""A check of the molecule detector's boxes, apart from the reading of the paper, before the boxes go to the OCSR side. A vision model
judges what it is shown; the host cuts, lays out, numbers and counts. Two looks, each at many things in one call, so that the check
covers every box at a small cost:
  boxes    each box cut with some of its surroundings and its own box line drawn, laid out as numbered tiles on one sheet (TILES a sheet);
           per tile: OK the box holds exactly one structure drawing, whole; CUT part of the drawing it holds runs past the box line;
           MERGED it holds more than one structure drawing; NOT_A_STRUCTURE what it holds is not a structure drawing.
  missed   whole figures with their boxes drawn, scaled down, FIGURES a call, each named F1, F2, ...; per figure the number of structure
           drawings no box holds and where. A figure the detector found nothing in is shown too.

Reads the transcripts (figure-transcript/) and the staged figures (figure-labels/<rid>.images/); a copy of the main text's reading
(same_as) is the same figure and is looked at once. Writes figure-labels/<rid>.boxcheck.json ({summary, figures: [{file, marks, boxes:
[{mark, verdict, note}], unboxed_drawings, unboxed_where}]}). Credentials from the process environment only (OPENAI_*).
--fill: only the boxes and figures the paper's last check left without an answer (a look that failed) are looked at again; the rest
is kept as it was.
usage: box_check.py [--fill] <rid> [<rid> ...]"""
import base64, collections, io, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont
sys.path.insert(0, str(Path(__file__).resolve().parent)); import figure_marks, run_config, waits
HERE = Path(__file__).resolve().parent.parent
TILES, COLUMNS, TILE, FIGURES, FIGURE_SIDE, MARGIN = 30, 6, 320, 6, 1024, 0.15
SYSTEM_BOXES = ("You are shown numbered tiles cut from figures of chemistry papers. In each tile a molecule detector's box is drawn as a "
                "coloured line, with some of the figure around it; the label printed beside a drawing does not need to be inside the box. "
                "For each tile say: OK when the box holds exactly one chemical structure drawing, whole; CUT when part of the drawing it holds "
                "runs past the box line; MERGED when it holds more than one structure drawing; NOT_A_STRUCTURE when what it holds is not a "
                'structure drawing. Answer as JSON: {"tiles": [{"tile": integer, "verdict": "OK" | "CUT" | "MERGED" | "NOT_A_STRUCTURE", '
                '"note": string}]}.')
SYSTEM_MISSED = ("You are shown figures from chemistry papers, each named before it. In each, a molecule detector drew a numbered box around "
                 "each chemical structure drawing it found; some figures have no box. For each figure count the structure drawings that no "
                 'box holds and say where they are; a drawing that a box holds in part is not counted. Answer as JSON: {"figures": [{"figure": string, "unboxed_drawings": integer, '
                 '"unboxed_where": string}]}.')
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['OPENAI_BASE_URL'], timeout=float(os.environ.get('BOXCHECK_TIMEOUT', '120')), max_retries=0)      # a sheet that keeps timing out is asked again with more time
font = ImageFont.load_default(size=22)


def figures(rid):
    """The figures of a paper as the figure reader read them: {file: marks}, later readings winning, copies and failed readings left out."""
    out = {}
    for name in ('transcript-medium.json', 'transcript-fill.json'):
        path = HERE / 'figure-transcript' / name
        if path.exists():
            out.update({t['file']: t.get('marks') or [] for t in json.loads(path.read_text(encoding='utf-8')) if t['file'].startswith(rid + '.') and not t.get('error') and not t.get('same_as')})
    pages = HERE / 'figure-labels' / f'{rid}.page-figures.json'      # the pages without running text, boxed whole (page_boxes.py)
    if pages.exists(): out.update({t['file']: t.get('marks') or [] for t in json.loads(pages.read_text(encoding='utf-8'))})
    if os.environ.get('BOXCHECK_MENDED'):      # the figures as rebox_apply.py made them (a check of the mended boxes), not as the detector gave them
        mended = HERE / 'figure-labels' / f'{rid}.rebox-figures.json'
        if mended.exists(): out.update({t['file']: t.get('marks') or [] for t in json.loads(mended.read_text(encoding='utf-8'))})
    return {f: m for f, m in sorted(out.items()) if (HERE / 'figure-labels' / f'{rid}.images' / f).exists()}


TIMES = []      # (what, seconds, attempts, error) of every look, for the summary


def ask(system, content, what, effort=None):
    began = time.monotonic()
    for attempt in range(waits.TRIES):
        try:
            r = client.chat.completions.create(model=run_config.model('vision'), reasoning_effort=effort or os.environ.get('TSDAF_BOXCHECK_EFFORT') or run_config.effort('vision', 'box_check'), response_format={'type': 'json_object'},
                                               messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': content}])
            text = r.choices[0].message.content or ''
            answer = json.loads(text[text.index('{'):text.rindex('}') + 1]); TIMES.append((what, round(time.monotonic() - began, 1), attempt + 1, None)); return answer, r.model, None
        except Exception as error:
            last = f'{what}: {type(error).__name__}: {str(error)[:160]}'; time.sleep(waits.pause(error, attempt))
    TIMES.append((what, round(time.monotonic() - began, 1), waits.TRIES, last)); return None, None, last


def png(image):
    buffer = io.BytesIO(); image.save(buffer, format='PNG'); return {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()}}


def tile(rid, file, mark):
    """One box with some of its surroundings and its own box line, fitted into a TILE x TILE square."""
    image = Image.open(HERE / 'figure-labels' / f'{rid}.images' / file).convert('RGB'); w, h = image.size
    x0, y0, x1, y1 = mark['bbox_px']; m = MARGIN * max(x1 - x0, y1 - y0)
    box = (max(0, x0 - m), max(0, y0 - m), min(w, x1 + m), min(h, y1 + m)); cut = image.crop(tuple(round(v) for v in box))
    ImageDraw.Draw(cut).rectangle((x0 - box[0], y0 - box[1], x1 - box[0], y1 - box[1]), outline=figure_marks.MARK_COLOR, width=3)
    cut.thumbnail((TILE - 8, TILE - 34)); return cut


def sheet(rid, items):
    """Numbered tiles on one sheet: items are (n, file, mark)."""
    rows = -(-len(items) // COLUMNS); page = Image.new('RGB', (COLUMNS * TILE, rows * TILE), 'white'); draw = ImageDraw.Draw(page)
    for k, (n, file, mark) in enumerate(items):
        x, y = (k % COLUMNS) * TILE, (k // COLUMNS) * TILE; draw.rectangle((x, y, x + TILE - 1, y + TILE - 1), outline=(160, 160, 160))
        draw.text((x + 6, y + 4), str(n), fill=(0, 0, 0), font=font); page.paste(tile(rid, file, mark), (x + 4, y + 30))
    return page


def look_figures(rid, figs, start):
    """The missed look over FIGURES figures of figs ({file: marks}) from start: (part, answer, model, error)."""
    part = list(figs.items())[start:start + FIGURES]; content = []
    for k, (file, marks) in enumerate(part, 1):
        image = Image.open(HERE / 'figure-labels' / f'{rid}.images' / file).convert('RGB')
        image = figure_marks.marked_image(image, [(m['mark'], m['bbox_px']) for m in marks]) if marks else image; image.thumbnail((FIGURE_SIDE, FIGURE_SIDE))
        content += [{'type': 'text', 'text': f'F{k}' + ('' if marks else ' (no box)')}, png(image)]
    # the missed look counts the same at low effort as at medium, four times faster (2026-10-16, three papers: 20 and 20); the tiles stay at
    # the box check's effort, where low passes cut boxes as whole
    answer, model, error = ask(SYSTEM_MISSED, content, f'figures {start + 1}-{start + len(part)}', run_config.effort('vision', 'box_check_missed'))
    return part, answer, model, error


def missed(rid, figs):
    """The missed look over figs ({file: marks}): {file: number of structure drawings no box holds, or None when not answered}."""
    out = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        for part, answer, _, error in pool.map(lambda start: look_figures(rid, figs, start), range(0, len(figs), FIGURES)):
            said = {str(a.get('figure')).strip(): a for a in (answer or {}).get('figures') or [] if isinstance(a, dict)} if not error else {}
            for k, (file, _) in enumerate(part, 1): out[file] = (said.get(f'F{k}') or {}).get('unboxed_drawings')
    return out


def check_paper(rid):
    began = time.monotonic(); figs = figures(rid)
    boxes = [(file, m) for file, marks in figs.items() for m in marks]
    verdict, errors, models, unboxed = {}, [], collections.Counter(), {}
    last = HERE / 'figure-labels' / f"{rid}.boxcheck{os.environ.get('BOXCHECK_AS', '')}.json"
    if FILL and last.exists():      # what the last check answered is kept; only what it left open is asked
        for r in json.loads(last.read_text(encoding='utf-8'))['figures']:
            verdict.update({(r['file'], b['mark']): (b['verdict'], b['note']) for b in r['boxes'] if b['verdict']})
            if r['unboxed_drawings'] is not None: unboxed[r['file']] = {'unboxed_drawings': r['unboxed_drawings'], 'unboxed_where': r['unboxed_where']}
    every, boxes = boxes, [(f, m) for f, m in boxes if (f, m['mark']) not in verdict]
    open_figs = {f: m for f, m in figs.items() if f not in unboxed}
    def look_boxes(start):
        part = [(k + 1, f, m) for k, (f, m) in enumerate(boxes[start:start + TILES])]
        answer, model, error = ask(SYSTEM_BOXES, [{'type': 'text', 'text': f'Tiles 1 to {len(part)}.'}, png(sheet(rid, part))], f'tiles {start + 1}-{start + len(part)}')
        return part, answer, model, error
    with ThreadPoolExecutor(max_workers=12) as pool:
        sheets = pool.map(look_boxes, range(0, len(boxes), TILES)); looks = pool.map(lambda start: look_figures(rid, open_figs, start), range(0, len(open_figs), FIGURES))
        for part, answer, model, error in sheets:
            if error: errors.append(error); continue
            models[model] += 1; said = {a.get('tile'): a for a in (answer or {}).get('tiles') or [] if isinstance(a, dict)}
            for n, file, m in part: verdict[(file, m['mark'])] = (said.get(n) or {}).get('verdict'), (said.get(n) or {}).get('note') if n in said else 'not judged'
        for part, answer, model, error in looks:
            if error: errors.append(error); continue
            models[model] += 1; said = {str(a.get('figure')).strip(): a for a in (answer or {}).get('figures') or [] if isinstance(a, dict)}
            for k, (file, _) in enumerate(part, 1): unboxed[file] = said.get(f'F{k}') or {}
    rows = [{'file': file, 'marks': len(marks), 'boxes': [{'mark': m['mark'], 'verdict': verdict.get((file, m['mark']), (None, None))[0], 'note': verdict.get((file, m['mark']), (None, 'not looked at'))[1]} for m in marks],
             'unboxed_drawings': (unboxed.get(file) or {}).get('unboxed_drawings'), 'unboxed_where': (unboxed.get(file) or {}).get('unboxed_where')} for file, marks in figs.items()]
    counts = collections.Counter(b['verdict'] for r in rows for b in r['boxes'])
    summary = {'figures': len(rows), 'figures without a box': sum(1 for r in rows if not r['marks']), 'boxes': len(every), 'verdicts': dict(counts),
               'drawings no box holds': sum(r['unboxed_drawings'] or 0 for r in rows if isinstance(r['unboxed_drawings'], int)),
               'figures with such drawings': sum(1 for r in rows if isinstance(r['unboxed_drawings'], int) and r['unboxed_drawings'] > 0),
               **({'filled': {'boxes asked again': len(boxes), 'figures asked again': len(open_figs)}} if FILL else {}), 'calls': sum(models.values()), 'failed calls': errors, 'answered_as': dict(models), 'effort': run_config.effort('vision', 'box_check'), 'seconds': round(time.monotonic() - began),
               'looks': [{'what': w, 'seconds': t, 'attempts': n, 'error': e} for w, t, n, e in TIMES]}; TIMES.clear()
    # BOXCHECK_AS: write beside the result under another name (a comparison of settings), not over it
    last.write_text(json.dumps({'summary': summary, 'figures': rows}, ensure_ascii=False, indent=1), encoding='utf-8')
    print(rid, json.dumps(summary, ensure_ascii=False), flush=True)


FILL = '--fill' in sys.argv
if __name__ == '__main__':
    for rid in [a for a in sys.argv[1:] if not a.startswith('--')]: check_paper(rid)
