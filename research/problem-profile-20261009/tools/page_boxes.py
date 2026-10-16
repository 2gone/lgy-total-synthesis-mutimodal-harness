"""The molecule detector over the pages of the experimental document that have no running text -- the pages of reproduced spectra,
chromatograms and pictures the parser read nothing from. Their figures are not transcribed (prestage_paper.py reads the figures of the
pages with running text), so their drawings had no box; a spectrum page often carries the structure of the compound it characterizes.

The page is the one page_owners.py rendered whole from the PDF; the detector runs on it with the settings of run_config.json's detector,
as on every other figure. Each page becomes a figure of its own, <rid>.SI-P<page>-PAGE.png in figure-labels/<rid>.images/, so that the
box check, the hand-off to the OCSR side and the structure step take it as they take any figure. Which compound a box is: the page reader
already said which compound the page characterizes; a page that names exactly one compound gives that one to every box on it (the host's
part), and only a page that names several or none is read with its boxes, as the figures were (the figure reader's prompt), for the label
printed beside each.

Writes figure-labels/<rid>.page-detections.json and figure-labels/<rid>.page-figures.json (rows in the form of the figure transcripts:
file, block, marks, transcript.molecules[mark, label], and labels_from). Credentials and the detector from the process environment only
(OPENAI_*, TSDAF_DETECTOR_*). usage: page_boxes.py <rid> [<rid> ...]"""
import base64, io, json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from openai import OpenAI
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parent)); import detect_chunks, figure_marks, run_config, waits
HERE = Path(__file__).resolve().parent.parent
BOXED = re.search(r'SYSTEM = """(.*?)"""', (HERE / 'tools/figure_transcribe.py').read_text(encoding='utf-8'), re.S).group(1)      # as the figures were read
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['OPENAI_BASE_URL'], timeout=300, max_retries=0)
detector = run_config.CONFIG['detector']


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


for rid in sys.argv[1:]:
    began = time.monotonic(); images = HERE / 'figure-labels' / f'{rid}.images'; images.mkdir(parents=True, exist_ok=True)
    owners = json.loads((HERE / 'figure-labels' / f'{rid}.owners.json').read_text(encoding='utf-8'))
    pages = {}
    for o in owners:
        file = f"{rid}.SI-P{o['page']:04d}-PAGE.png"
        if not (images / file).exists(): Image.open(HERE / o['image']).convert('RGB').save(images / file)
        pages[file] = o
    found = HERE / 'figure-labels' / f'{rid}.page-detections.json'
    if pages and not found.exists():
        code, said = detect_chunks.detect([images / f for f in pages], found, detector['confidence_threshold'], detector['iou_threshold'], detector['imgsz'])
        if code != 0: print(rid, 'detector failed:', said); continue
    boxes = {i['image']: figure_marks.reading_order(i['detections']) for i in json.loads(found.read_text(encoding='utf-8'))['images']} if found.exists() else {}
    rows = []
    def make(item):
        file, o = item; ordered = boxes.get(file) or []
        if not ordered: return None
        marks = [{'mark': f'M{n:03d}', 'bbox_px': b['bbox_px']} for n, b in enumerate(ordered, 1)]
        if len(o['compounds']) == 1: labels, how, error = {m['mark']: o['compounds'][0] for m in marks}, 'the page (the compound the page characterizes)', None
        else: (labels, error), how = read(Image.open(images / file).convert('RGB'), marks), 'the figure reader'
        return {'file': file, 'block': f"SI-P{o['page']:04d}-PAGE", 'page': o['page'], 'document': o.get('document'), 'page_in_document': o.get('page_in_document'),
                'source': 'whole page rendered from the PDF, boxed by the detector', 'labels_from': how, 'marks': marks, 'error': error,
                'transcript': {'molecules': [{'mark': m['mark'], 'label': labels.get(m['mark'])} for m in marks]}}
    with ThreadPoolExecutor(max_workers=12) as pool: rows = [r for r in pool.map(make, pages.items()) if r]
    (HERE / 'figure-labels' / f'{rid}.page-figures.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
    print(rid, json.dumps({'pages without running text': len(pages), 'pages with boxes': len(rows), 'boxes': sum(len(r['marks']) for r in rows),
                           'labelled from the page': sum(1 for r in rows if r['labels_from'].startswith('the page')), 'read': sum(1 for r in rows if r['labels_from'] == 'the figure reader'),
                           'errors': sum(1 for r in rows if r['error']), 'seconds': round(time.monotonic() - began)}), flush=True)
