"""Read the printed compound label of every molecule the detector boxed, with the tuned prompt of the review overlay
(marked figure in, one label per mark out). The result is words a text-only task can use: which labels a figure prints, left to right.
Credentials from the process environment only."""
import base64, io, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from PIL import Image
from openai import OpenAI
sys.path.insert(0, str(Path(__file__).resolve().parent)); import run_config      # models and thinking levels: tools/run_config.json
sys.path.insert(0, '/Users/hululu/Documents/Codex/2026-09-02/mu/worktrees/e293-expression-review/tools/display_overlay')
sys.path.insert(0, '/Users/hululu/Documents/Codex/2026-09-02/mu/worktrees/e293-expression-review')
from build import BINDING_SYSTEM, extract_json, marked_image, png_bytes, reading_order
here = Path(__file__).resolve().parent.parent / 'figure-labels'
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['OPENAI_BASE_URL'], timeout=240, max_retries=3)

def read(job):
    block, image_path, boxes = job
    image = Image.open(image_path)
    ordered = reading_order([dict(b) for b in boxes])
    for n, b in enumerate(ordered, 1): b['mark'] = f'M{n:03d}'
    png = png_bytes(marked_image(image, [(b['mark'], b['bbox_px']) for b in ordered]))
    text = ("Report the printed compound label of each marked molecule in this figure.\n"
            f"Marks to report, exactly as drawn: {', '.join(b['mark'] for b in ordered)}\nCandidate compound labels: (none)\nFigure: (no caption)")
    t = time.monotonic()
    try:
        r = client.chat.completions.create(model=run_config.model('vision'), reasoning_effort=run_config.effort('vision', 'figure_labels'), messages=[
            {'role': 'system', 'content': BINDING_SYSTEM},
            {'role': 'user', 'content': [{'type': 'text', 'text': text}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(png).decode()}}]}])
        got = {b['mark']: b for b in extract_json(r.choices[0].message.content or '').get('bindings') or []}
        error = None
    except Exception as e:
        got, error = {}, type(e).__name__ + ': ' + str(e)[:200]
    return {'block_id': block, 'width_px': image.width, 'height_px': image.height, 'seconds': round(time.monotonic() - t, 1), 'error': error,
            'molecules': [{'mark': b['mark'], 'bbox_px': b['bbox_px'], 'confidence': b['confidence'],
                           'printed_label': (got.get(b['mark'], {}).get('compound_label') or '').strip() or None} for b in ordered]}

for p in sys.argv[1:]:
    blocks = {Path(r['file']).name: r['block_id'] for r in json.loads((here / f'{p}.blocks.json').read_text()) if r['file']}
    files = {Path(r['file']).name: r['file'] for r in json.loads((here / f'{p}.blocks.json').read_text()) if r['file']}
    jobs = [(blocks[i['image']], files[i['image']], i['detections']) for i in json.loads((here / f'{p}.detections.json').read_text())['images'] if i['detections']]
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=12) as pool: rows = list(pool.map(read, jobs))
    (here / f'{p}.labels.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print(p, 'figures', len(rows), 'errors', sum(bool(r['error']) for r in rows), 'molecules', sum(len(r['molecules']) for r in rows),
          'with a printed label', sum(bool(m['printed_label']) for r in rows for m in r['molecules']), f'{time.monotonic() - began:.0f}s', flush=True)
    for r in rows[:6]: print('   ', r['block_id'], [m['printed_label'] for m in r['molecules']])
