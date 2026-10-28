"""Tokens and time of a corpus run of gm_corpus.py (2026-10-27): from every call's line in thoughts/<label>.jsonl (the calls that returned;
a call that failed or timed out leaves no line and its tokens, if any were billed, are not counted) and from the run's status file.
Per step (segment, record, paper_task): calls, input, cached input, output (the answer), thinking, call seconds. lpzproxy's Gemini counts
the thinking apart (total_tokens = prompt + completion + reasoning), so the output billed is answer + thinking ('output with thinking'); per paper: segmentation and
recording wall, passes, entries, RECORD_PARALLEL; in total: wall of the run, peak calls in flight, papers complete.
A front-end batch run by batch_run.py --thought-shim is counted too (--batch <name>: thoughts/<name>-<paper>.<stage>.jsonl).
usage: corpus_stats.py --round v4c200 [--prefix gm1021] [--batch <front-end batch>] [--out stats-<round>.json]"""
import collections, json, statistics, sys
from datetime import datetime
from pathlib import Path
W = Path(__file__).resolve().parent.parent
opt = lambda n, d=None: sys.argv[sys.argv.index(n) + 1] if n in sys.argv else d
ROUND = opt('--round', 'v4c200'); PREFIX = opt('--prefix', 'gm1021'); BATCH = opt('--batch'); OUT = Path(opt('--out', str(W / f'stats-{ROUND}.json')))
status = json.loads((W / 'whole-paper' / f'{PREFIX}-r{ROUND}-status.json').read_text(encoding='utf-8'))
papers = [p for p in status if not p.startswith('_')]
steps = collections.defaultdict(collections.Counter); seconds = collections.defaultdict(list); per = {}; first = last = None
for p in papers:
    files = [W / 'thoughts' / f'{PREFIX}-r{ROUND}-{p}.jsonl'] + (sorted((W / 'thoughts').glob(f'{BATCH}-{p}.*.jsonl')) if BATCH else []); c = collections.Counter()
    for f, line in ((f, l) for f in files if f.exists() for l in f.read_text(encoding='utf-8').splitlines()):
        if not line.strip(): continue
        x = json.loads(line); u = x.get('usage') or {}; s = x.get('step') or 'other'
        if f.name.count('.') == 2: s = 'front-end ' + f.name.split('.')[1]      # a front-end stage's file: prepare, owners, prestage
        row = {'calls': 1, 'input': u.get('prompt_tokens') or 0, 'cached input': (u.get('prompt_tokens_details') or {}).get('cached_tokens') or 0,
               'output': u.get('completion_tokens') or 0, 'thinking': (u.get('completion_tokens_details') or {}).get('reasoning_tokens') or 0,
               'empty thinking': int(not (x.get('thought') or '').strip())}
        row['output with thinking'] = row['output'] + row['thinking'] if (u.get('total_tokens') or 0) >= row['input'] + row['output'] + row['thinking'] > row['input'] + row['output'] else row['output']
        steps[s].update(row); c.update(row); seconds[s].append(x.get('seconds') or 0)
        at = datetime.strptime(x['at'], '%Y-%m-%d %H:%M:%S'); first = min(first or at, at); last = max(last or at, at)
    st = status[p]; per[p] = {'state': st.get('state'), 'entries': st.get('entries'), 'record_parallel': st.get('record_parallel'), 'seg_seconds': st.get('seg_seconds'),
                              'record_seconds': sum(x['seconds'] for x in st.get('passes') or []), 'passes': [x['how'] for x in st.get('passes') or []], **c}
total = collections.Counter()
for s in steps: total.update(steps[s])
report = {'round': ROUND, 'settings': status.get('_settings'), 'started': status.get('_started'), 'finished': status.get('_finished'), 'wall_seconds': status.get('_wall_seconds'),
          'peak_in_flight': status.get('_peak_in_flight'), 'papers': len(papers), 'complete': sum(per[p]['state'] in ('complete', 'complete before this start') for p in papers),
          'not complete': {p: per[p]['state'] for p in papers if per[p]['state'] not in ('complete', 'complete before this start')},
          'repaired': sum('repair' in per[p]['passes'] for p in papers), 'first call returned': str(first), 'last call returned': str(last),
          'by step': {s: {**steps[s], 'call seconds sum': round(sum(seconds[s])), 'call seconds mean': round(statistics.mean(seconds[s]), 1),
                          'call seconds p90': round(sorted(seconds[s])[int(len(seconds[s]) * .9)], 1), 'call seconds max': round(max(seconds[s]), 1)} for s in steps},
          'total': dict(total), 'per paper': per}
wall = [per[p]['seg_seconds'] or 0 for p in papers]; rec = [per[p]['record_seconds'] for p in papers]
report['paper wall'] = {'segmentation mean': round(statistics.mean(wall)), 'segmentation max': max(wall), 'record mean': round(statistics.mean(rec)), 'record max': max(rec)}
OUT.parent.mkdir(exist_ok=True); OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1))
print(f"papers {report['papers']}  complete {report['complete']}  repaired {report['repaired']}  wall {report['wall_seconds']} s  peak in flight {report['peak_in_flight']}")
print(f"{'':12}{'calls':>8}{'input':>14}{'cached':>14}{'answer':>12}{'thinking':>13}{'out+think':>13}{'mean s':>9}{'p90 s':>8}{'max s':>8}")
for s, v in list(report['by step'].items()) + [('total', {**total, 'call seconds mean': '', 'call seconds p90': '', 'call seconds max': ''})]:
    print(f"{s:12}{v['calls']:>8}{v['input']:>14,}{v['cached input']:>14,}{v['output']:>12,}{v['thinking']:>13,}{v['output with thinking']:>13,}{v['call seconds mean']!s:>9}{v['call seconds p90']!s:>8}{v['call seconds max']!s:>8}")
print('paper wall', report['paper wall']); print('not complete', report['not complete']); print('written', OUT)
