"""Pre-stage of one new paper on mfy-win: put its two packages where the chain reads them, and have Gemini transcribe its figures.
  main package   copied from D:\\tsdaf\\packages\\<rid>
  SI package     built from the consolidated SI parse (D:\\SI_consolidation) with scripts/build_paper_package.py
  figures        every figure of the main text, and the SI figures on pages that also carry running text (not the spectra pages).
                 The molecule detector boxes their drawings first (figure-labels/<rid>.detections.json: run here when TSDAF_DETECTOR_PYTHON
                 and TSDAF_DETECTOR_WEIGHTS name a Python with ultralytics and the weights of run_config.json's detector, read when it is
                 already there), and each figure with boxes is read with its marks (M001, ...), as the five reference papers were, so that
                 every drawing keeps its printed label and its box for the structure step. A figure without boxes is read unmarked.
                 Figures are cut from the PDF page over the parser's box widened to what is printed with the drawings (figure_regions.py).
Writes figure-transcript/transcript-fill.json and figure-labels/<rid>.labels.json (what the segmentation step reads).
Credentials from the process environment only (OPENAI_*). usage: prestage_paper.py <rid>"""
import base64, collections, json, os, re, shutil, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from openai import OpenAI
sys.path.insert(0, str(Path(__file__).resolve().parent)); import run_config, waits      # models and thinking levels: tools/run_config.json
HERE = Path(__file__).resolve().parent.parent; REPO = HERE.parents[1]; FIX = REPO / 'tests/fixtures'
rid = sys.argv[1]; LIMIT = 1000      # a guard only: the largest experimental documents of the corpus hold about 250 figures
main_dir, si_dir = FIX / 'paper_packages' / rid, FIX / 'si_packages' / rid
if not main_dir.exists(): shutil.copytree(Path(r'D:\tsdaf\packages') / rid, main_dir)
if not si_dir.exists():
    match = next(h for h in json.load(open(r'D:\tsdaf\si_parsed_matches.json', encoding='utf-8')) if h['package'] == rid)      # only a paper without its SI package needs the parse list
    best = None
    for doc in {d['blocks']: d for d in match['si_docs']}.values():      # the SI document that carries the procedures
        text = Path(doc['blocks']).read_text(encoding='utf-8', errors='replace'); score = text.count('mmol')
        if best is None or score > best[0]: best = (score, doc)
    intake = Path(best[1]['blocks']).parents[1]
    done = subprocess.run([sys.executable, str(REPO / 'scripts/build_paper_package.py'), str(intake), str(si_dir), '--phase', 'SI', '--doi', match['doi'], '--source-pdf', best[1]['pdf']],
                          env={**os.environ, 'PYTHONPATH': str(HERE / 'tools/src-units'), 'PYTHONUTF8': '1'}, capture_output=True, text=True, encoding='utf-8', errors='replace')
    print('SI package:', intake.name, 'pages', best[1]['pages'], 'exit', done.returncode, (done.stdout or done.stderr)[-300:].replace('\n', ' '), flush=True)
    if done.returncode != 0: raise SystemExit('the SI package could not be built')

BOXED = re.search(r'SYSTEM = """(.*?)"""', (HERE / 'tools/figure_transcribe.py').read_text(encoding='utf-8'), re.S).group(1)      # the instruction the boxed figures were read with
SYSTEM = BOXED.replace('Every molecule drawing in it has been boxed and given a mark (M001, M002, ...). ', '') \
    + '\n\nNo marks are drawn in this figure: in molecules, give "mark" as D1, D2, ... in reading order, and use the same names in arrows.'
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['OPENAI_BASE_URL'], timeout=300, max_retries=2)
jobs = []
# The experimental document may hold the main text as one of its parts (import_ready200.py): its figures are those of the main package,
# read once there; the experimental document's copy takes that reading under its own name, marked same_as, and is not read again.
spans = next((json.loads(f.read_text(encoding='utf-8')).get(rid, {}).get('pages') for f in (HERE / 'batch/ready200-import.json', FIX / 'ready200-import.json') if f.exists()), None) or []
main_span = next((s['pages'] for s in spans if s['document'] == 'main'), None); aliases = []
for kind, doc in (('paper_packages', 'MAIN'), ('si_packages', 'SI')):
    rows = [json.loads(line) for line in (FIX / kind / rid / 'normalized/blocks.jsonl').read_text(encoding='utf-8').splitlines()]
    # a page with running text is a page of procedures, not of spectra; the parser sometimes hangs a procedure on a figure as its caption,
    # and that is running text too
    wordy = collections.Counter(r['page_idx'] for r in rows if (r['type'] == 'text' and len(r.get('raw_text') or '') > 300) or (r['type'] != 'text' and len(' '.join(r.get('captions') or [])) > 300))
    seen = collections.Counter()
    for row in rows:
        if row['type'] not in ('image', 'chart', 'table') or not row.get('asset_ref'): continue
        n = seen[row['page_idx']]; seen[row['page_idx']] += 1
        if row['type'] == 'table' or (doc == 'SI' and not wordy[row['page_idx']]): continue
        if doc == 'SI' and main_span and main_span[0] <= row['page_idx'] <= main_span[1]:
            aliases.append({'file': '%s.SI-P%04d-A%04d.png' % (rid, row['page_idx'], n), 'block': 'SI-P%04d-B%04d' % (row['page_idx'], row['reading_order']),
                            'same_as': '%s.MAIN-P%04d-A%04d.png' % (rid, row['page_idx'] - main_span[0], n)}); continue
        path = FIX / kind / rid / row['asset_ref']
        if path.exists() and path.stat().st_size > 2500:
            jobs.append({'file': '%s.%s-P%04d-A%04d.png' % (rid, doc, row['page_idx'], n), 'block': '%s-P%04d-B%04d' % (doc, row['page_idx'], row['reading_order']), 'path': path, 'doc': doc,
                         'row': row, 'page': [r for r in rows if r['page_idx'] == row['page_idx']], 'pdf': next((FIX / kind / rid / 'source').glob('*.pdf'), None)})
skipped = max(0, len(jobs) - LIMIT); jobs = jobs[:LIMIT]
import figure_marks, figure_regions
from PIL import Image
# Each figure is read from its PDF page over the parser's box widened to what is printed with the drawings (figure_regions.py): the parser's
# own image often leaves out the name and label printed under a structure. The parser's image is used only where there is no PDF.
staged = HERE / 'figure-labels' / f'{rid}.images'; staged.mkdir(parents=True, exist_ok=True); pages = {}
for job in jobs:
    target = staged / job['file']
    if not target.exists():
        if job['pdf'] is not None:
            if job['pdf'] not in pages: pages[job['pdf']] = figure_regions.Pages(job['pdf'])
            pages[job['pdf']].cut(job['row']['page_idx'], figure_regions.region(job['page'], job['row'])).save(target)
        else: Image.open(job['path']).convert('RGB').save(target)
    job['image'] = target
detections_file = HERE / 'figure-labels' / f'{rid}.detections.json'
covered = {i['image'] for i in json.loads(detections_file.read_text(encoding='utf-8'))['images']} if detections_file.exists() else set()
if any(j['file'] not in covered for j in jobs) and os.environ.get('TSDAF_DETECTOR_PYTHON') and os.environ.get('TSDAF_DETECTOR_WEIGHTS'):      # absent, or made for other figures
    detector = run_config.CONFIG['detector']
    done = subprocess.run([os.environ['TSDAF_DETECTOR_PYTHON'], str(HERE / 'tools/detect_molecules.py'), '--weights', os.environ['TSDAF_DETECTOR_WEIGHTS'], '--images', *[str(staged / j['file']) for j in jobs],
                           '--out', str(detections_file), '--imgsz', str(detector['imgsz']), '--conf', str(detector['confidence_threshold']), '--iou', str(detector['iou_threshold']),
                           '--device', os.environ.get('TSDAF_DETECTOR_DEVICE', 'cpu')], capture_output=True, text=True, encoding='utf-8', errors='replace', env={**os.environ, 'KMP_DUPLICATE_LIB_OK': 'TRUE'})
    print('detector exit', done.returncode, (done.stderr or '')[-300:].replace('\n', ' '), flush=True)
# A figure that is one drawing filling its whole frame (the structure printed above each SI procedure) is often not found: the
# detector is made for drawings inside a page. Such figures are looked at again set on a white page twice their size, and the boxes
# found there are taken back to the figure's own pixels.
detector = run_config.CONFIG['detector']
if detections_file.exists() and os.environ.get('TSDAF_DETECTOR_PYTHON') and os.environ.get('TSDAF_DETECTOR_WEIGHTS') and 'second_look' not in json.loads(detections_file.read_text(encoding='utf-8')):
    found = json.loads(detections_file.read_text(encoding='utf-8')); empty = [i for i in found['images'] if not i['detections']]
    found['second_look'] = {'figures without a box at first': len(empty), 'found on a white page': 0}
    if empty:
        padded = staged / 'padded'; padded.mkdir(exist_ok=True); offset = {}
        for i in empty:
            image = Image.open(staged / i['image']).convert('RGB'); w, h = image.size
            page = Image.new('RGB', (2 * w, 2 * h), 'white'); page.paste(image, (w // 2, h // 2)); page.save(padded / i['image']); offset[i['image']] = (w // 2, h // 2, w, h)
        again = staged / 'padded.detections.json'
        subprocess.run([os.environ['TSDAF_DETECTOR_PYTHON'], str(HERE / 'tools/detect_molecules.py'), '--weights', os.environ['TSDAF_DETECTOR_WEIGHTS'], '--images', *[str(padded / name) for name in offset],
                        '--out', str(again), '--imgsz', str(detector['imgsz']), '--conf', str(detector['confidence_threshold']), '--iou', str(detector['iou_threshold']),
                        '--device', os.environ.get('TSDAF_DETECTOR_DEVICE', 'cpu')], capture_output=True, text=True, encoding='utf-8', errors='replace', env={**os.environ, 'KMP_DUPLICATE_LIB_OK': 'TRUE'})
        if again.exists():
            back = {}
            for i in json.loads(again.read_text(encoding='utf-8'))['images']:
                dx, dy, w, h = offset[i['image']]
                back[i['image']] = [{**d, 'bbox_px': [round(min(max(v - (dx if k % 2 == 0 else dy), 0), w if k % 2 == 0 else h), 1) for k, v in enumerate(d['bbox_px'])], 'found_on_a_white_page': True} for d in i['detections']]
            for i in found['images']:
                if not i['detections'] and back.get(i['image']): i['detections'] = back[i['image']]
            found['second_look'] = {'figures without a box at first': len(empty), 'found on a white page': sum(bool(v) for v in back.values())}
            detections_file.write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding='utf-8')
            print('second look on a white page:', found['second_look'], flush=True)
boxes = {}
if detections_file.exists():
    found = json.loads(detections_file.read_text(encoding='utf-8'))
    if found.get('weights_sha256') != run_config.CONFIG['detector']['weights_sha256']: print('WARNING: the detections were made with other weights than run_config.json names', flush=True)
    boxes = {i['image']: figure_marks.reading_order(i['detections']) for i in found['images'] if i['detections']}
else:
    print(f'WARNING: no detector boxes for {rid} (figure-labels/{rid}.detections.json): its figures are read unmarked and their drawings cannot be bound to compounds', flush=True)


def read(job):
    began = time.monotonic(); ordered = boxes.get(job['file']) or []
    marks = [{'mark': f'M{n:03d}', 'bbox_px': box['bbox_px']} for n, box in enumerate(ordered, start=1)]
    if marks:
        import io
        buffer = io.BytesIO(); figure_marks.marked_image(Image.open(job['image']), [(m['mark'], m['bbox_px']) for m in marks]).save(buffer, format='PNG')
        image, system, source = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode(), BOXED, 'figure, boxed by the detector'
        ask = 'Marks drawn in this figure: ' + ', '.join(m['mark'] for m in marks) + '.'      # as the reference papers' figures were asked
    else:
        image, system, source = 'data:image/png;base64,' + base64.b64encode(job['image'].read_bytes()).decode(), SYSTEM, 'figure, no marks'; ask = 'Transcribe this figure.'
    for attempt in range(waits.TRIES):
        try:
            r = client.chat.completions.create(model=run_config.model('vision'), reasoning_effort=run_config.effort('vision', 'figure_transcribe'), response_format={'type': 'json_object'}, messages=[{'role': 'system', 'content': system},
                {'role': 'user', 'content': [{'type': 'text', 'text': ask}, {'type': 'image_url', 'image_url': {'url': image}}]}])
            text = r.choices[0].message.content or ''; transcript, error = json.loads(text[text.index('{'):text.rindex('}') + 1]), None; break
        except Exception as e:
            transcript, error = {}, type(e).__name__ + ': ' + str(e)[:160]
            if not waits.passing(e) and attempt >= 1: break        # a reading that fails twice for another reason is left (it is reported)
            time.sleep(waits.pause(e, attempt))
    return {'file': job['file'], 'block': job['block'], 'source': source + (', read from the PDF page' if job['pdf'] is not None else ', the parser\'s image'), 'marks': marks or None, 'seconds': round(time.monotonic() - began, 1), 'error': error, 'transcript': transcript}


began = time.monotonic()
with ThreadPoolExecutor(max_workers=12) as pool: rows = list(pool.map(read, jobs))
read_as = {r['file']: r for r in rows}
rows += [{**read_as[a['same_as']], 'file': a['file'], 'block': a['block'], 'same_as': a['same_as']} for a in aliases if a['same_as'] in read_as]
lock = HERE / 'figure-transcript/.lock'; (HERE / 'figure-transcript').mkdir(exist_ok=True); (HERE / 'figure-labels').mkdir(exist_ok=True)
while True:      # several papers are pre-staged at once and share the transcript file
    try: os.close(os.open(lock, os.O_CREAT | os.O_EXCL)); break
    except FileExistsError: time.sleep(0.5)
try:
    fill = HERE / 'figure-transcript/transcript-fill.json'; old = json.loads(fill.read_text(encoding='utf-8')) if fill.exists() else []
    fill.write_text(json.dumps([r for r in old if not r['file'].startswith(rid + '.')] + rows, ensure_ascii=False, indent=1), encoding='utf-8')
    medium = HERE / 'figure-transcript/transcript-medium.json'
    if not medium.exists(): medium.write_text('[]', encoding='utf-8')
finally:
    os.remove(lock)
labels = [{'block_id': r['block'], 'molecules': [{'printed_label': m.get('label')} for m in (r['transcript'].get('molecules') or []) if isinstance(m, dict)]} for r in rows if r['file'].startswith(rid + '.SI-') and not r['error']]
(HERE / 'figure-labels' / f'{rid}.labels.json').write_text(json.dumps(labels, ensure_ascii=False), encoding='utf-8')
(HERE / 'figure-labels' / f'{rid}.blocks.json').write_text(json.dumps([{'block_id': r['block'], 'file': None} for r in rows], ensure_ascii=False), encoding='utf-8')
print(f"{rid}: figures transcribed {len(rows) - sum('same_as' in r for r in rows)}, taken from the main text's reading {sum('same_as' in r for r in rows)} (main {sum(j['doc'] == 'MAIN' for j in jobs)}, SI {sum(j['doc'] == 'SI' for j in jobs)}; read with boxes {sum(bool(r['marks']) for r in rows)}), errors {sum(bool(r['error']) for r in rows)}, over the limit and skipped {skipped}, wall {time.monotonic() - began:.0f}s", flush=True)
