"""The entry package: one paper laid out for a strong model that works in one context. Fixed order, fixed text, so that every later
turn can be served from the prompt cache. Text comes from the parse, figures from the Gemini transcripts (the strong model reads no image),
the split of the SI from the reading units. Nothing here is a model's judgment about the chemistry.
Also reports coverage: every parsed block is placed in the package, listed in its catalog, or dropped as page furniture.
usage: entry_package.py <paper> [units dir]"""
import collections, json, os, re, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent; FIX = HERE.parents[1] / 'tests/fixtures'
paper = sys.argv[1]; units_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / 'si-units/lanev2-r1'
FURNITURE = {'header', 'footer', 'page_number', 'aside_text'}; FIGURE = {'image', 'chart'}
transcripts = {t['file']: t for t in json.loads((HERE / 'figure-transcript/transcript-medium.json').read_text()) if t['file'].startswith(paper + '.') and not t['error']}
_fill = HERE / 'figure-transcript/transcript-fill.json'
if _fill.exists(): transcripts.update({t['file']: t for t in json.loads(_fill.read_text()) if t['file'].startswith(paper + '.') and not t['error']})


def load(kind):
    rows = [json.loads(line) for line in (FIX / kind / (paper + (os.environ.get('SI_TEXT', '') if kind == 'si_packages' else '')) / 'normalized/blocks.jsonl').read_text(encoding='utf-8').splitlines()]
    doc = 'MAIN' if kind == 'paper_packages' else 'SI'; seen = collections.Counter()
    for row in rows:
        row['id'] = '%s-P%04d-B%04d' % (doc, row['page_idx'], row['reading_order'])
        if row['type'] in FIGURE | {'table'} and row.get('asset_ref'):
            row['figure_file'] = '%s.%s-P%04d-A%04d.png' % (paper, doc, row['page_idx'], seen[row['page_idx']]); seen[row['page_idx']] += 1
    return rows


def figure_text(row):
    t = transcripts.get(row.get('figure_file')); caption = ' '.join(row.get('captions') or []); long = ''
    if len(caption) > 200:
        # The parse sometimes hangs a whole paragraph on a figure as its caption (a procedure printed under its scheme): that is text.
        long, caption = f"[{row['id']} text printed with the figure] {caption}\n", ''
    if t is None: return long + f"[FIGURE {row['id']}, not transcribed{': ' + caption if caption else ''}]", bool(long)
    tr = t['transcript']; label = {m['mark']: (m['label'] or f"unlabelled drawing {m['mark']}") + (f" ({m['note']})" if m.get('note') else '') for m in tr.get('molecules') or []}
    name = lambda x: label.get(x, x)
    lines = [long + f"[FIGURE {row['id']}{': ' + caption if caption else ''}. Transcribed from the image by a figure reader; structures are not described, only what is printed.]"]
    if label: lines.append('  drawings: ' + '; '.join(label.values()))
    for a in tr.get('arrows') or []:
        kind = '' if a.get('kind') in (None, 'reaction') else f" [{a['kind']}]"
        lines.append(f"  {' + '.join(map(name, a.get('from') or [])) or '?'} → {' + '.join(map(name, a.get('to') or [])) or '?'}{kind}: {' | '.join(a.get('text') or []) or '(nothing printed on the arrow)'}")
    if tr.get('table'): lines += ['  table:'] + ['    ' + ' | '.join(map(str, r)) for r in tr['table']]
    if tr.get('legend'): lines += ['  legend: ' + ' / '.join(tr['legend'])]
    if tr.get('other_text'): lines += ['  other printed text: ' + ' / '.join(tr['other_text'])]
    return '\n'.join(lines), True


def body(row):
    if row['type'] in FIGURE: return figure_text(row)
    text = (row.get('raw_text') or '').strip()
    if row['type'] == 'table' and row.get('captions'): text = ' '.join(row['captions']) + '\n' + text
    return (f"[{row['id']}] {text}" if text else ''), bool(text)


main, si = load('paper_packages'), load('si_packages'); by_id = {r['id']: r for r in si}
units = json.loads((units_dir / f'{paper}.units.json').read_text())['units']
place = {}; out = [f'# PAPER {paper}', '', '## 1. MAIN TEXT (reading order; figures as transcripts)', '']
references = 0
for row in main:
    if row['type'] in FURNITURE: place[row['id']] = 'furniture'; continue
    if row['type'] == 'ref_text': place[row['id']] = 'catalog: references'; references += 1; continue
    text, ok = body(row); place[row['id']] = 'package' if ok else ('package (figure, no transcript)' if row['type'] in FIGURE else 'empty')
    if text: out += [text, '']
out += ['## 2. SI: GENERAL METHODS', '']
catalog = collections.defaultdict(list); entries = 0; section3 = ['## 3. SI: EXPERIMENT ENTRIES (one per procedure unit of the SI, in SI order)', '']
for index, unit in enumerate(units):
    rows = [by_id[b] for b in unit['block_ids']]
    if unit['kind'] in ('GENERAL_METHODS', 'PROCEDURE'):
        target = out if unit['kind'] == 'GENERAL_METHODS' else section3
        if unit['kind'] == 'PROCEDURE':
            entries += 1; section3 += [f"### Entry {entries} (SI unit {index}; compound label as segmented: {unit['compound_label']}; SI page {rows[0]['page_idx'] + 1})", '']
        for row in rows:
            if row['type'] in FURNITURE: place[row['id']] = 'furniture'; continue
            text, ok = body(row); place[row['id']] = 'package' if ok else ('package (figure, no transcript)' if row['type'] in FIGURE else 'empty')
            if text: target += [text, '']
    else:
        numbers = sum(len(re.findall(r'\d+\.\d+', r.get('raw_text') or '')) for r in rows)
        first = next((re.sub(r'\s+', ' ', r.get('raw_text') or '')[:70] for r in rows if (r.get('raw_text') or '').strip()), '(figures only)')
        catalog[unit['kind']].append(f"SI unit {index}, pages {rows[0]['page_idx'] + 1}-{rows[-1]['page_idx'] + 1}, compound {unit['compound_label']}, {len(rows)} blocks, {numbers} decimal numbers; begins: {first}")
        for row in rows: place[row['id']] = 'furniture' if row['type'] in FURNITURE else f"catalog: {unit['kind']}"
outside = collections.Counter()
for row in si:
    if row['id'] in place: continue
    # Blocks the reading units do not hold: the SI reference list and the spectra pages are listed, anything else with text is a gap.
    kind = 'furniture' if row['type'] in FURNITURE else 'catalog: references' if row['type'] == 'ref_text' else 'catalog: spectra and other figures outside the units' if row['type'] in FIGURE | {'table'} \
        else 'catalog: headings outside the units' if len(row.get('raw_text') or '') < 120 else 'NOT PLACED'
    place[row['id']] = kind; outside[kind] += 1
out += section3 + ['## 4. NOT INCLUDED, HELD BY THE SYSTEM (ask for a unit by its number if you need its text)', '',
                   'Characterization data is copied into the result verbatim by the system; you do not transcribe it.', '']
for kind in ('CHARACTERIZATION', 'CRYSTALLOGRAPHY', 'OTHER', 'BIBLIOGRAPHY'):
    if catalog[kind]: out += [f'{kind} ({len(catalog[kind])} units):'] + ['- ' + line for line in catalog[kind]] + ['']
out += [f'Main-text reference list: {references} entries, not included.'] + [f'SI, {k.split(": ")[1]}: {v} blocks, not included.' for k, v in outside.items() if k.startswith('catalog')]
text = '\n'.join(out); target = Path(sys.argv[3]) if len(sys.argv) > 3 else HERE / 'entry-package' / paper; target.mkdir(parents=True, exist_ok=True)
(target / 'package.md').write_text(text); (target / 'coverage.json').write_text(json.dumps(place, indent=0))
count = collections.Counter(place.values()); figs = [r for r in main + si if r['type'] in FIGURE]
inside = [r for r in figs if place[r['id']].startswith('package')]
print(f"{paper}: package {len(text)} chars (~{len(text) // 4000}k tokens) | entries {entries} | blocks: {dict(count)}")
print(f"  figures inside the package {len(inside)}, with a transcript {sum(place[r['id']] == 'package' for r in inside)}, without {sum(place[r['id']] != 'package' for r in inside)} | NOT PLACED {count['NOT PLACED']}")
sizes = {s.split('\n')[0][:40]: len(s) // 4000 for s in re.split(r'\n(?=## )', text)}; print('  sections ~k tokens:', sizes)
