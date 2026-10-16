"""Take back what the OCSR side returns for a hand-off made by ocsr_pack.py, and bind the structures to the compounds of our results again.

The check, per returned <paper>.ocsr.json against the hand-off's manifest: every key is a detection_id of that paper's boxes; every box
of the paper is answered (an answer may be null fields: no structure read); the crop_sha256 sent with each box comes back unchanged, so
that an answer is known to be about the crop it names; smiles, inchi_key and formula are strings or null. A paper whose file passes is
installed as ocsr-results/<paper>.ocsr.json (the form structure_step.py reads: {detection_id: {smiles, inchi_key, formula, readable,
is_abstract, confidence}}) and its structure binding is made again (structure-binding.json and the summary in result.json).
Nothing is installed for a file that does not pass; the report says why. usage: ocsr_return.py <hand-off dir> <returned dir> [--install] [--batch ready200]"""
import json, subprocess, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent; TOOLS = HERE / 'tools'
sys.path.insert(0, str(TOOLS)); import run_config
option = lambda name, default: sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
handoff, returned, INSTALL, BATCH = Path(sys.argv[1]), Path(sys.argv[2]), '--install' in sys.argv, option('--batch', 'ready200')
manifest = [json.loads(line) for line in (handoff / 'manifest.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
by_paper = {}
for row in manifest: by_paper.setdefault(row['paper'], {})[row['detection_id']] = row
FIELDS = ('smiles', 'inchi_key', 'formula', 'readable', 'is_abstract', 'confidence')
report = {}
for path in sorted(returned.glob('*.ocsr.json')):
    paper = path.name[:-len('.ocsr.json')]; mine = by_paper.get(paper)
    if mine is None: report[paper] = {'passed': False, 'why': 'no such paper in the hand-off'}; continue
    try: answer = json.loads(path.read_text(encoding='utf-8'))
    except ValueError as error: report[paper] = {'passed': False, 'why': f'not JSON: {error}'}; continue
    unknown = sorted(set(answer) - set(mine)); unanswered = sorted(set(mine) - set(answer))
    changed = sorted(d for d, a in answer.items() if d in mine and (a or {}).get('crop_sha256') != mine[d]['crop_sha256'])
    badly_typed = sorted(d for d, a in answer.items() if any(not isinstance((a or {}).get(k), (str, type(None))) for k in ('smiles', 'inchi_key', 'formula')))
    read = sum(1 for a in answer.values() if (a or {}).get('smiles'))
    passed = not (unknown or unanswered or changed or badly_typed)
    report[paper] = {'passed': passed, 'boxes': len(mine), 'answered': len(answer), 'with a structure': read, 'unknown ids': unknown[:20], 'unanswered': unanswered[:20],
                     'crop hash not the one sent': changed[:20], 'fields of the wrong type': badly_typed[:20]}
    if passed and INSTALL:
        target = HERE / run_config.CONFIG['ocsr']['results_dir']; target.mkdir(exist_ok=True)
        (target / f'{paper}.ocsr.json').write_text(json.dumps({d: {k: (a or {}).get(k) for k in FIELDS} for d, a in answer.items()}, ensure_ascii=False, indent=1), encoding='utf-8')
        run = HERE / 'whole-paper' / f'{BATCH}-{paper}'
        done = subprocess.run([sys.executable, str(TOOLS / 'structure_step.py'), paper, str(run), '--write'], capture_output=True, text=True, encoding='utf-8', errors='replace')
        report[paper]['bound again'] = done.returncode == 0; report[paper]['binding'] = (done.stdout or done.stderr).strip().splitlines()[:1]
missing = sorted(set(by_paper) - set(report))
print(json.dumps({'papers returned': len(report), 'passed': sum(r['passed'] for r in report.values()), 'papers of the hand-off not returned': len(missing)}, ensure_ascii=False))
(returned / 'return-check.json').write_text(json.dumps({'report': report, 'not returned': missing}, ensure_ascii=False, indent=1), encoding='utf-8')
for paper, r in report.items():
    if not r['passed']: print('NOT PASSED', paper, {k: v for k, v in r.items() if v and k != 'passed'})
