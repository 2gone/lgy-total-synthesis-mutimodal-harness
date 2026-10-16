"""The corpus run laid out for the SI review service (tsdaf-review, /si/): one folder per paper, named as in the corpus package
(corpus_pack.py: <position>_<paper>), which the review page shows as the paper's id. No model is called; the workspace is not changed.

  pdf/si.pdf                    the experimental document the chain read (every document of the paper joined, import_ready200.py):
                                the block handles of the results (SI-P....-B....) are its pages
  pdf/main.pdf                  the main text
  parse/si.blocks.jsonl         the experimental document's text as settled against the PDF (the pre-stage's clean package)
  parse/si.blocks.parser.jsonl  the same document as the parser gave it
  parse/main.blocks.jsonl       the main text's blocks
  parse/si.origin.json          per block, where the settled text came from; parse/documents.json the documents joined and their pages
  result/result.json            the paper's result (whole_paper.py); beside it structure-binding.json, entries_detail.json,
                                units.json (segmentation), pages_characterizing.json

A paper without pdf/si.pdf, parse/si.blocks.jsonl or a readable result/result.json, or whose name the service would not take, is
reported and not staged. usage: si_review_stage.py <out dir> [--batch ready200] [--papers rid,rid]"""
import json, re, shutil, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent; FIX = HERE.parents[1] / 'tests/fixtures'
option = lambda name, default: sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
out, BATCH = Path(sys.argv[1]), option('--batch', 'ready200'); only = set(filter(None, option('--papers', '').split(',')))
imports = json.loads((HERE / 'batch/ready200-import.json').read_text(encoding='utf-8'))
ledger = json.loads((HERE / 'batch' / BATCH / 'status.json').read_text(encoding='utf-8'))
NAME = re.compile(r'^[A-Za-z0-9_.-]{1,120}$')      # the service's paper id

staged, refused = [], []
for rid in sorted((r for r in imports if r.endswith('-c') and (not only or r in only)), key=lambda r: imports[r]['position']):
    entry = imports[rid]; name = f"{entry['position']:03d}_{rid.removesuffix('-c')}"; run = HERE / 'whole-paper' / f'{BATCH}-{rid}'
    if ((ledger.get(rid) or {}).get('chain') or {}).get('state') not in ('done', 'gaps'): refused.append((name, 'chain not finished')); continue
    joined = next((FIX / 'si_packages' / rid / 'source').glob('*.pdf'), None); main = next((FIX / 'paper_packages' / rid / 'source').glob('*.pdf'), None)
    files = {'pdf/si.pdf': joined, 'pdf/main.pdf': main,
             'parse/si.blocks.jsonl': FIX / 'si_packages' / f'{rid}__clean_b/normalized/blocks.jsonl',
             'parse/si.blocks.parser.jsonl': FIX / 'si_packages' / rid / 'normalized/blocks.jsonl',
             'parse/main.blocks.jsonl': FIX / 'paper_packages' / rid / 'normalized/blocks.jsonl',
             'parse/si.origin.json': HERE / 'clean_b' / f'{rid}.SI.origin.json',
             'result/result.json': run / 'result.json', 'result/structure-binding.json': run / 'structure-binding.json',
             'result/entries_detail.json': run / 'entries/results.json', 'result/units.json': run / 'units' / f'{rid}.units.json',
             'result/pages_characterizing.json': HERE / 'figure-labels' / f'{rid}.owners.json'}
    missing = [k for k in ('pdf/si.pdf', 'parse/si.blocks.jsonl', 'result/result.json') if not (files[k] and files[k].exists())]
    if missing or not NAME.match(name): refused.append((name, 'missing ' + ', '.join(missing) if missing else 'name')); continue
    try: json.loads(files['result/result.json'].read_text(encoding='utf-8'))
    except Exception as error: refused.append((name, f'result.json: {error}')); continue
    folder = out / name
    if folder.exists(): shutil.rmtree(folder)
    for k, src in files.items():
        if src and src.exists(): (folder / k).parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(src, folder / k)
    (folder / 'parse/documents.json').write_text(json.dumps({'paper': rid, 'doi': entry['doi'], 'documents': entry.get('documents'), 'pages': entry.get('pages'),
                                                             'notes': entry.get('notes')}, ensure_ascii=False, indent=1), encoding='utf-8')
    staged.append(name); print(name, flush=True)
print(json.dumps({'staged': len(staged), 'not staged': refused}, ensure_ascii=False))
