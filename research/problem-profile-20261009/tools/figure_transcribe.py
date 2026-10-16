"""Transcribe each selected figure once, whatever task may later need it: the molecules the detector boxed (with their
printed labels), every reaction arrow with the text printed on it, and any legend or note. Marked figure in, JSON out.
Credentials from the process environment only. usage: figure_transcribe.py <effort> <out.json> [name-filter]"""
import base64, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from PIL import Image
from openai import OpenAI
sys.path.insert(0, str(Path(__file__).resolve().parent)); import run_config      # models and thinking levels: tools/run_config.json
sys.path.insert(0, '/Users/hululu/Documents/Codex/2026-09-02/mu/worktrees/e293-expression-review/tools/display_overlay')
sys.path.insert(0, '/Users/hululu/Documents/Codex/2026-09-02/mu/worktrees/e293-expression-review')
from build import extract_json, marked_image, png_bytes, reading_order
here = Path(__file__).resolve().parent.parent / 'figure-transcript'
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['OPENAI_BASE_URL'], timeout=300, max_retries=2)
SYSTEM = """You are shown one figure from a chemistry paper. Every molecule drawing in it has been boxed and given a mark (M001, M002, ...). Transcribe what the figure prints. You are a copyist: do not interpret, do not summarise, do not describe what a structure looks like, and do not expand an abbreviation.

Answer with one JSON object:

molecules: one entry per mark, {"mark", "label", "note"}. label is the compound label printed beside that drawing (13, 14a, (±)-1, SI-6), or null when none is printed. note is any other text printed with the drawing that is not part of the structure itself -- a name, "(dr = 5:1)", "not isolated", "X-ray", whether it stands in square brackets -- or null.

arrows: one entry per reaction arrow, in reading order, {"from", "to", "text", "yield", "kind"}. from and to are lists of marks (a mark, or the label when the species is printed only as text, e.g. a reagent written out). text is everything printed over, under or beside that arrow, copied exactly, one printed line per list item, in the order printed; keep a letter or number that keys the arrow to a legend ("a", "b)"). yield is the yield text printed for that arrow ("72%", "56% (2 steps)") or null. kind is "reaction" for an ordinary arrow, or what the figure itself marks it as: "crossed out", "dashed", "retrosynthetic", "equilibrium", "mechanism".

legend: the text of any legend or footnote printed inside the figure ("Reagents and conditions: a) ...", "a Isolated yield"), copied exactly, one item per line; an empty list when there is none.

table: when the figure is or contains a table, its rows as lists of cell texts, header first; otherwise null.

other_text: any other printed words not covered above (a panel letter, a title, a remark such as "failed epimerization at C12"), one item each. Atom labels and numbering inside a structure are not text to report.

Copy characters exactly: digits, decimal points, signs, units, degree signs, Greek letters. Write subscripts and superscripts inline (CH2Cl2, 23 °C). Where a character cannot be read, write [?] in its place rather than guessing."""

def read(job):
    name, boxes = job
    image = Image.open(here / 'crops' / name)
    ordered = reading_order([dict(b) for b in boxes])
    for n, b in enumerate(ordered, 1): b['mark'] = f'M{n:03d}'
    png = png_bytes(marked_image(image, [(b['mark'], b['bbox_px']) for b in ordered])) if ordered else png_bytes(image)
    text = 'Marks drawn in this figure: ' + (', '.join(b['mark'] for b in ordered) or '(none: the detector found no molecule drawing)') + '.'
    t = time.monotonic(); usage = None
    try:
        r = client.chat.completions.create(model=run_config.model('vision'), reasoning_effort=EFFORT, messages=[
            {'role': 'system', 'content': SYSTEM},
            {'role': 'user', 'content': [{'type': 'text', 'text': text}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(png).decode()}}]}])
        got, error = extract_json(r.choices[0].message.content or ''), None
        usage = r.usage.model_dump() if r.usage else None
    except Exception as e:
        got, error = {}, type(e).__name__ + ': ' + str(e)[:200]
    return {'file': name, 'seconds': round(time.monotonic() - t, 1), 'error': error, 'thought': ((usage or {}).get('completion_tokens_details') or {}).get('reasoning_tokens'),
            'completion': (usage or {}).get('completion_tokens'), 'marks': [{'mark': b['mark'], 'bbox_px': b['bbox_px']} for b in ordered], 'transcript': got}

EFFORT, out = (run_config.effort('vision', 'figure_transcribe') if sys.argv[1] == 'config' else sys.argv[1]), sys.argv[2]      # 'config': the level set in run_config.json
jobs = [(i['image'], i['detections']) for i in json.loads((here / 'detections.json').read_text())['images'] if len(sys.argv) < 4 or sys.argv[3] in i['image']]
began = time.monotonic()
with ThreadPoolExecutor(max_workers=16) as pool: rows = list(pool.map(read, jobs))
(here / out).write_text(json.dumps(rows, ensure_ascii=False, indent=1))
secs = sorted(r['seconds'] for r in rows)
print('figures', len(rows), 'errors', sum(bool(r['error']) for r in rows), f'wall {time.monotonic() - began:.0f}s', 'model seconds total', round(sum(secs)), 'median', secs[len(secs) // 2], 'max', secs[-1],
      'thought total', sum(r['thought'] or 0 for r in rows), flush=True)
