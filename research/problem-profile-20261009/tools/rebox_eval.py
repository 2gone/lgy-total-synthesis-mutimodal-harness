"""What each source of new boxes covers (rebox.py): on the figures the box check said have drawings no box holds, the missed look of
box_check.py is made again under four sets of boxes, in the same conditions -- the detector's boxes as they are; with the relook's
boxes judged OK; with the vision model's boxes judged OK; with both (a box of the one that overlaps one of the other is counted once).
What is left without a box under each is the measure. Writes rebox-eval.json in the workspace. usage: rebox_eval.py <rid> [<rid> ...]"""
import collections, json, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
src = (HERE / 'tools/box_check.py').read_text(encoding='utf-8')
bc = {'__file__': str(HERE / 'tools/box_check.py'), '__name__': 'box_check'}; exec(compile(src, 'box_check.py', 'exec'), bc)      # its papers run only as __main__


def iou(a, b):
    w = max(0, min(a[2], b[2]) - max(a[0], b[0])); h = max(0, min(a[3], b[3]) - max(a[1], b[1])); inter = w * h
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter or 1)


totals = collections.Counter(); per_paper = {}
for rid in sys.argv[1:]:
    rebox = json.loads((HERE / 'figure-labels' / f'{rid}.rebox.json').read_text(encoding='utf-8')); marks_of = bc['figures'](rid)
    flagged = [r['file'] for r in rebox['figures'] if r.get('unboxed_drawings_said')]
    ok = {r['file']: [c for c in r['candidates'] if c['verdict'] == 'OK' and not c.get('replaces')] for r in rebox['figures']}      # new boxes only; mending is counted in rebox.json
    def with_(sources):
        figs = {}
        for f in flagged:
            added = []
            for c in ok[f]:
                if c['source'] in sources and all(iou(c['bbox_px'], a['bbox_px']) < 0.5 for a in added): added.append(c)
            figs[f] = marks_of[f] + [{'mark': c['mark'], 'bbox_px': c['bbox_px']} for c in added]
        return figs
    left = {name: bc['missed'](rid, with_(sources)) for name, sources in (('detector only', ()), ('+ relook', ('relook',)), ('+ gemini', ('gemini',)), ('+ both', ('relook', 'gemini')))}
    row = {name: sum(v or 0 for v in got.values()) for name, got in left.items()}
    row['boxes added: relook'] = sum(1 for f in flagged for c in ok[f] if c['source'] == 'relook'); row['boxes added: gemini'] = sum(1 for f in flagged for c in ok[f] if c['source'] == 'gemini')
    row['figures'] = len(flagged); per_paper[rid] = row; totals.update(row)
    print(rid, row, flush=True)
base = totals['detector only'] or 1
report = {'totals': dict(totals), 'covered (share of the drawings without a box under the detector only)': {k: round(1 - totals[k] / base, 3) for k in ('+ relook', '+ gemini', '+ both')}, 'papers': per_paper}
(HERE / 'rebox-eval.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
print(json.dumps(report['totals'], ensure_ascii=False)); print(json.dumps(report['covered (share of the drawings without a box under the detector only)'], ensure_ascii=False))
