"""The hand-off to the OCSR side: every box the molecule detector drew in the figures of the papers of a batch, cut out, with what each
box is, so that the structures come back under the box's own name and are bound to the compounds of our results again.

Per paper, for every figure read with boxes (a copy of the main text's reading, same_as, is the same figure and is given once):
  figures/<paper>/<figure file>          the figure as it was read (cut from the PDF page, widened to what is printed with the drawings), or a
                                         whole page without running text (<paper>.SI-P<page>-PAGE.png, page_boxes.py)
  figures_marked/<paper>/<figure file>   the same with its boxes and marks drawn, as the figure reader saw it
  crops/<paper>/<figure stem>__<mark>.png one box, cut with a margin of 4% of its larger side (at least 4 px), inside the figure
  returns/<paper>.ocsr.json              the form of the answer: one entry per box, under its detection_id, with the crop's sha256 filled in
A box is named detection_id = "<figure file>#<mark>", the name the structure step binds by (structure_step.py; ocsr-results/).
manifest.jsonl / manifest.csv: per box its detection_id, paper, DOI, figure and its sha256 and size, mark, the box (bbox_px, in the
figure's pixels) and the crop (crop_box_px), the crop file and its sha256, the label printed beside the drawing as our figure reader
read it, whether our result binds the box to a compound (needed_by_result, compound_label), and the box check's verdict when there is one.
Nothing in the workspace is changed. usage: ocsr_pack.py <out dir> [--batch ready200] [--papers rid,rid]"""
import csv, hashlib, json, shutil, sys, time
from pathlib import Path
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parent)); import figure_marks
HERE = Path(__file__).resolve().parent.parent
option = lambda name, default: sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
out, BATCH = Path(sys.argv[1]), option('--batch', 'ready200')
ledger = json.loads((HERE / 'batch' / BATCH / 'status.json').read_text(encoding='utf-8'))
papers = option('--papers', None)
papers = papers.split(',') if papers else sorted(p for p, e in ledger.items() if not p.startswith('_') and (e.get('chain') or {}).get('state') in ('done', 'gaps'))
imports = json.loads((HERE / 'batch/ready200-import.json').read_text(encoding='utf-8')) if (HERE / 'batch/ready200-import.json').exists() else {}
sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
readings = {}
for name in ('transcript-medium.json', 'transcript-fill.json'):
    path = HERE / 'figure-transcript' / name
    if path.exists(): readings.update({t['file']: t for t in json.loads(path.read_text(encoding='utf-8')) if not t.get('error') and not t.get('same_as')})
for path in sorted(HERE.glob('figure-labels/*.page-figures.json')):      # the pages without running text, boxed whole (page_boxes.py)
    readings.update({t['file']: t for t in json.loads(path.read_text(encoding='utf-8'))})
for path in sorted(HERE.glob('figure-labels/*.rebox-figures.json')):      # the figures whose boxes rebox.py mended or added to (rebox_apply.py)
    readings.update({t['file']: t for t in json.loads(path.read_text(encoding='utf-8'))})
FIELDS = ['detection_id', 'paper', 'doi', 'figure_file', 'figure_sha256', 'figure_width_px', 'figure_height_px', 'mark', 'bbox_px', 'crop_box_px', 'crop_file', 'crop_sha256',
          'printed_label', 'needed_by_result', 'compound_label', 'box_check', 'box_check_note', 'box_source', 'replaces_bbox_px']
rows, missing = [], []
for rid in papers:
    run = HERE / 'whole-paper' / f'{BATCH}-{rid}'
    bound = json.loads((run / 'structure-binding.json').read_text(encoding='utf-8')) if (run / 'structure-binding.json').exists() else {}
    compound_of = {c['compound_id']: (c.get('author_labels') or [None])[0] for c in bound.get('compounds') or []}
    needed = {}      # a box standing for several compounds (a caption naming them all) is needed by each: their labels joined
    for n in bound.get('needed_for_ocsr') or []:
        needed[n['detection_id']] = '; '.join(x for x in (needed.get(n['detection_id']), compound_of.get(n.get('compound_id'))) if x)
    checked = {}
    if (HERE / 'figure-labels' / f'{rid}.boxcheck.json').exists():
        for f in json.loads((HERE / 'figure-labels' / f'{rid}.boxcheck.json').read_text(encoding='utf-8'))['figures']:
            for b in f['boxes']: checked[f"{f['file']}#{b['mark']}"] = (b.get('verdict'), b.get('note'))
    for file, t in sorted((f, t) for f, t in readings.items() if f.startswith(rid + '.') and t.get('marks')):
        source = HERE / 'figure-labels' / f'{rid}.images' / file
        if not source.exists(): missing.append(str(source)); continue
        for sub in ('figures', 'figures_marked', 'crops'): (out / sub / rid).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, out / 'figures' / rid / file)
        image = Image.open(source).convert('RGB'); w, h = image.size
        figure_marks.marked_image(image, [(m['mark'], m['bbox_px']) for m in t['marks']]).save(out / 'figures_marked' / rid / file)
        label = {m.get('mark'): m.get('label') for m in (t.get('transcript') or {}).get('molecules') or [] if isinstance(m, dict)}
        for m in t['marks']:
            x0, y0, x1, y1 = m['bbox_px']; pad = max(4, round(0.04 * max(x1 - x0, y1 - y0)))
            box = [max(0, round(x0 - pad)), max(0, round(y0 - pad)), min(w, round(x1 + pad)), min(h, round(y1 + pad))]
            crop = out / 'crops' / rid / f"{Path(file).stem}__{m['mark']}.png"; image.crop(box).save(crop)
            did = f"{file}#{m['mark']}"; verdict = tuple((t.get('checked') or {}).get(m['mark']) or checked.get(did, (None, None)))      # a mended box: its own check
            rows.append({'detection_id': did, 'paper': rid, 'doi': (imports.get(rid) or {}).get('doi'), 'figure_file': f'figures/{rid}/{file}', 'figure_sha256': sha(source),
                         'figure_width_px': w, 'figure_height_px': h, 'mark': m['mark'], 'bbox_px': m['bbox_px'], 'crop_box_px': box, 'crop_file': f'crops/{rid}/{crop.name}',
                         'crop_sha256': sha(crop), 'printed_label': label.get(m['mark']), 'needed_by_result': did in needed, 'compound_label': needed.get(did),
                         'box_check': verdict[0], 'box_check_note': verdict[1], 'box_source': (t.get('box_source') or {}).get(m['mark'], 'detector'),
                         'replaces_bbox_px': (t.get('replaced') or {}).get(m['mark'])})
    print(rid, sum(1 for r in rows if r['paper'] == rid), 'boxes', flush=True)
(out / 'returns').mkdir(parents=True, exist_ok=True)
for rid in papers:
    mine = [r for r in rows if r['paper'] == rid]
    if mine:
        (out / 'returns' / f'{rid}.ocsr.json').write_text(json.dumps({r['detection_id']: {'crop_sha256': r['crop_sha256'], 'smiles': None, 'inchi_key': None, 'formula': None, 'readable': None,
                                                                                          'is_abstract': None, 'confidence': None} for r in mine}, ensure_ascii=False, indent=1), encoding='utf-8')
with open(out / 'manifest.jsonl', 'w', encoding='utf-8') as f:
    for r in rows: f.write(json.dumps(r, ensure_ascii=False) + '\n')
with open(out / 'manifest.csv', 'w', encoding='utf-8-sig', newline='') as f:
    writer = csv.DictWriter(f, FIELDS); writer.writeheader()
    for r in rows: writer.writerow({**r, 'bbox_px': json.dumps(r['bbox_px']), 'crop_box_px': json.dumps(r['crop_box_px']), 'replaces_bbox_px': json.dumps(r['replaces_bbox_px']) if r['replaces_bbox_px'] else ''})
summary = {'made': time.strftime('%Y-%m-%d %H:%M:%S'), 'batch': BATCH, 'papers': len(papers), 'papers with boxes': len({r['paper'] for r in rows}), 'boxes': len(rows),
           'boxes our results bind to a compound': sum(r['needed_by_result'] for r in rows), 'boxes checked': sum(r['box_check'] is not None for r in rows),
           'box check verdicts': {v: sum(1 for r in rows if r['box_check'] == v) for v in ('OK', 'CUT', 'MERGED', 'NOT_A_STRUCTURE')}, 'figures missing on disk': missing}
(out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
(out / 'README_交付说明.md').write_text(f"""# OCSR 交付包（{summary['made']}）

本包是 {summary['papers with boxes']} 篇全合成论文的图中，分子检测器（YOLO）框出的全部结构式，共 {summary['boxes']} 个框，
其中 {summary['boxes our results bind to a compound']} 个是我们的抽取结果已绑定到化合物上的（`needed_by_result = true`，建议优先）。
请对每个框做 OCSR，按下面的格式交回；我们按框的编号把结构对应回化合物。

## 目录

- `crops/<论文>/<图名>__<标记>.png`：每个框的裁图（在框外留了框长边 4% 的边距，至少 4 像素）。**OCSR 的输入就是它。**
- `figures/<论文>/<图名>.png`：裁图所在的整张图（从 PDF 页面裁出）；`figures_marked/` 是同一张图画上框和标记（M001、M002……）。
- `manifest.jsonl` / `manifest.csv`：每个框一行。
  - `detection_id`：框的编号，形如 `<图名>#<标记>`，**交回时用它作键，不要改**；
  - `bbox_px`：框在整图中的像素坐标 [x0, y0, x1, y1]；`crop_box_px`：裁图在整图中的范围；
  - `crop_file`、`crop_sha256`：裁图文件及其 SHA-256；
  - `printed_label`：我们读到的、印在结构式旁的化合物编号（供参考，可能为空）；
  - `needed_by_result`、`compound_label`：我们的结果是否用到这个框，绑定到哪个化合物；
  - `box_check`、`box_check_note`：我们对框的质检结论（OK / CUT 切掉了一部分 / MERGED 框住了多个结构 / NOT_A_STRUCTURE 不是结构式），供参考。
  - `box_source`：框从哪里来：`detector` 分子检测器原框；`relook` 检测器在图四周加白边后重框（修补切掉的框或补上漏框）；`gemini` 视觉模型按检测器同样的原则给出的框（只用在检测器修不好的地方）。修补或新增的框都经同一质检判为 OK；
  - `replaces_bbox_px`：修补切掉的框时，原框的坐标（编号沿用原框，结构式没变）；拆开多结构框得到的框和补上的漏框是新编号。
- `returns/<论文>.ocsr.json`：**交回用的模板**，每篇一个，键已按 `detection_id` 填好。
- `summary.json`：本包的统计。

## 交回格式

每篇交回一个 `<论文>.ocsr.json`（直接在模板上填写），每个框一项：

```json
{{"<detection_id>": {{"crop_sha256": "（模板中已填，原样保留）", "smiles": "…或 null", "inchi_key": "…或 null",
  "formula": "…或 null", "readable": true, "is_abstract": false, "confidence": 0.0}}}}
```

- 每个框都要有一项；读不出结构就把 `smiles` 等留为 null，`readable` 写 false。
- `is_abstract`：结构里有 R 基、X、省略画法等，不是一个具体分子时写 true。
- `crop_sha256` 原样保留：我们用它确认每个答案对应的正是发出的那张裁图。
- 不要改文件名和 `detection_id`；如对框本身有意见（比如框切掉了一部分），可以在该项里加一个 `note` 字段。

交回后我们会逐项核对（编号齐全、裁图哈希一致、字段类型正确），核对通过的论文才会装入并重新绑定。
""", encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False))
