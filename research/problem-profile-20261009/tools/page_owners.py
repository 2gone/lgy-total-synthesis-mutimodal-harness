"""Whose characterization each page of the experimental document without running text is: the pages of reproduced spectra and
chromatograms, and the pages the parser returned little or nothing for. Segmentation does not see these pages' pictures, and the
figure transcripts are made for the pages with running text (prestage_paper.py).

The division of labour: the host chose the pages and renders each whole page from the PDF (as the figure transcripts are cut from the
PDF page, not taken from the parser's pictures, which often split a spectrum from its structure and label); the model says only which
compound or compounds the page is a characterization of, the labels as printed. The assembly (whole_paper.py) keeps each such page as
a characterization record of each compound it names: the rendered page itself and its page number; nothing is read off it.

Writes figure-labels/<rid>.owners.json: [{page, document, page_in_document, image, compounds, answered_as, effort, error}] and the
renders in figure-labels/<rid>.pages/. An empty answer is asked once more at the fallback thinking level. Credentials from the process
environment only (OPENAI_*). usage: page_owners.py <rid>"""
import base64, collections, io, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from openai import OpenAI
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parent)); import run_config, waits
HERE = Path(__file__).resolve().parent.parent; FIX = HERE.parents[1] / 'tests/fixtures'
rid = sys.argv[1]; package = FIX / 'si_packages' / rid
SYSTEM = ("You are shown one page from the experimental part of a chemistry paper. Say which compound or compounds this page is a "
          "characterization of: each compound's label exactly as the page prints it, or its name as printed where no label is printed. When "
          'the page is not data measured on a compound, the list is empty. Answer as JSON: {"compounds": [string]}.')

rows = [json.loads(line) for line in (package / 'normalized/blocks.jsonl').read_text(encoding='utf-8').splitlines()]
# the pages with running text, as prestage_paper.py counts them
wordy = collections.Counter(r['page_idx'] for r in rows if (r['type'] == 'text' and len(r.get('raw_text') or '') > 300) or (r['type'] != 'text' and len(' '.join(r.get('captions') or [])) > 300))
pages = [p for p in sorted({r['page_idx'] for r in rows}) if not wordy[p]]
# which document of the paper and which of its pages, where the import joined several (batch/ready200-import.json)
spans = next((json.loads(f.read_text(encoding='utf-8')).get(rid, {}).get('pages') for f in (HERE / 'batch/ready200-import.json', FIX / 'ready200-import.json') if f.exists()), None) or []
where = lambda page: next(((s['document'], page - s['pages'][0]) for s in spans if s['pages'][0] <= page <= s['pages'][1]), (None, page))
renders = HERE / 'figure-labels' / f'{rid}.pages'; renders.mkdir(parents=True, exist_ok=True)
import fitz
pdf = fitz.open(next((package / 'source').glob('*.pdf'))); lock = __import__('threading').Lock()
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['OPENAI_BASE_URL'], timeout=240, max_retries=2)


def ask(page):
    target = renders / f'p{page:04d}.jpg'
    if not target.exists():
        with lock: pixmap = pdf[page].get_pixmap(dpi=150)
        image = Image.frombytes('RGB', (pixmap.width, pixmap.height), pixmap.samples); image.thumbnail((1800, 1800)); image.save(target, format='JPEG', quality=88)
    url = 'data:image/jpeg;base64,' + base64.b64encode(target.read_bytes()).decode()
    document, inside = where(page)
    out = {'page': page, 'document': document, 'page_in_document': inside, 'image': f'figure-labels/{rid}.pages/{target.name}', 'compounds': [], 'answered_as': None, 'effort': None, 'error': None}
    for effort in (run_config.effort('vision', 'figure_owner'), run_config.effort('vision', 'figure_owner_fallback')):
        for attempt in range(waits.TRIES):
            try:
                r = client.chat.completions.create(model=run_config.model('vision'), reasoning_effort=effort, response_format={'type': 'json_object'},
                                                   messages=[{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': url}}]}])
                text = r.choices[0].message.content or ''; out['answered_as'] = r.model
                if not text.strip(): break                      # an empty answer: the next thinking level
                answer = json.loads(text[text.index('{'):text.rindex('}') + 1])
                out.update(compounds=[c for c in answer.get('compounds') or [] if isinstance(c, str) and c.strip()], effort=effort, error=None); return out
            except Exception as error:
                out['error'] = type(error).__name__ + ': ' + str(error)[:160]
                time.sleep(waits.pause(error, attempt))
    return out


began = time.monotonic()
with ThreadPoolExecutor(max_workers=12) as pool: owners = list(pool.map(ask, pages))
(HERE / 'figure-labels' / f'{rid}.owners.json').write_text(json.dumps(owners, ensure_ascii=False, indent=1), encoding='utf-8')
print(f"{rid}: pages without running text {len(pages)} | characterizing a compound {sum(bool(o['compounds']) for o in owners)} | errors {sum(o['error'] is not None for o in owners)} | {time.monotonic() - began:.0f} s")
