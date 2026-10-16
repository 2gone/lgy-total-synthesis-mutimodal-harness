"""The delivery of a corpus run: per paper its sources, what the chain read, and what it found, in one folder each.

  <out>/<position>_<paper>/
    source/        the paper's PDFs as delivered: main.pdf, si_001.pdf, ... (the consolidated corpus, article.json)
    processed/     model_input.md      the paper as the recording read it: the main text, the SI general methods, every experiment entry,
                                       figures as transcripts (the chain's package.md)
                   experimental.md     the whole experimental document, page by page, with its settled text (the pre-stage) and each
                                       figure and page picture in place, with what the figure reader read in it
                   main.md             the main text, page by page, the same way
                   experimental.pdf    the experimental document as joined from the paper's documents; documents.json says which pages
                                       came from which document (si_001, ..., main)
                   images/             the figures as read (cut from the PDF pages) and the pages without running text, rendered whole
                   figure_transcripts.json   what the figure reader read in each figure (marks, labels, arrows, tables)
    results/       result.json         the machine result: reaction records, paper-level record, characterization records (text, figure
                                       and page records), author disagreements, unresolved points, the structure step's summary
                   structure-binding.json   which drawn structure (box) belongs to which compound
                   entries_detail.json the recording of every experiment entry with its marks and checks
                   pages_characterizing.json  the pages without running text and the compounds each characterizes
  <out>/INDEX.csv, <out>/README_说明.md

Nothing in the workspace is changed. --results-only renews results/ and the index (after a binding is made again).
usage: corpus_pack.py <out dir> [--batch ready200] [--results-only] [--papers rid,rid]"""
import csv, json, shutil, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent; REPO = HERE.parents[1]; FIX = REPO / 'tests/fixtures'
option = lambda name, default: sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
out, BATCH, RESULTS_ONLY = Path(sys.argv[1]), option('--batch', 'ready200'), '--results-only' in sys.argv
imports = json.loads((HERE / 'batch/ready200-import.json').read_text(encoding='utf-8'))
ledger = json.loads((HERE / 'batch' / BATCH / 'status.json').read_text(encoding='utf-8'))
READY = Path(r'D:\SI_consolidation\ready_200_20261015_revised10')
index = {r['doi']: r for r in csv.DictReader(open(READY / 'INDEX.csv', encoding='utf-8-sig'))}
FURNITURE = {'header', 'footer', 'page_number', 'aside_text'}
readings = {}
for name in ('transcript-medium.json', 'transcript-fill.json'):
    path = HERE / 'figure-transcript' / name
    if path.exists(): readings.update({t['file']: t for t in json.loads(path.read_text(encoding='utf-8')) if not t.get('error')})


def transcript_lines(t):
    """What the figure reader read, as a few lines of markdown."""
    tr = t.get('transcript') or {}; lines = []
    labels = [m.get('label') or '(no label printed)' for m in tr.get('molecules') or [] if isinstance(m, dict)]
    if labels: lines.append('> drawings: ' + '; '.join(labels))
    for a in tr.get('arrows') or []:
        if isinstance(a, dict): lines.append(f"> {' + '.join(a.get('from') or []) or '?'} → {' + '.join(a.get('to') or []) or '?'}: {' | '.join(a.get('text') or []) or '(nothing printed on the arrow)'}")
    if tr.get('table'): lines += ['> table: ' + ' | '.join(map(str, row)) for row in tr['table'][:40]]
    if tr.get('other_text'): lines.append('> other text: ' + ' / '.join(map(str, tr['other_text']))[:600])
    return lines


def markdown(rid, package, doc, spans=None, pages_whole=()):
    """A package's blocks as markdown, page by page, figures in place with their transcripts; a page kept whole (a page without running
    text) is shown as its rendered picture."""
    rows = [json.loads(line) for line in (package / 'normalized/blocks.jsonl').read_text(encoding='utf-8').splitlines()]
    where = lambda p: next((f"{s['document']}, its page {p - s['pages'][0] + 1}" for s in spans or [] if s['pages'][0] <= p <= s['pages'][1]), None)
    lines, seen, page = [], {}, None
    for r in rows:
        if r['page_idx'] != page:
            page = r['page_idx']; lines += ['', f"## Page {page + 1}" + (f" ({where(page)})" if spans and where(page) else '')]
            whole = f'{rid}.SI-P{page:04d}-PAGE.png'
            if doc == 'SI' and whole in pages_whole: lines += [f'![page {page + 1}, rendered whole](images/{whole})']
        if r['type'] in FURNITURE: continue
        if r['type'] in ('image', 'chart', 'table') and r.get('asset_ref'):
            n = seen.get(page, 0); seen[page] = n + 1; file = f'{rid}.{doc}-P{page:04d}-A{n:04d}.png'
            if r['type'] == 'table' and (r.get('raw_text') or '').strip(): lines += ['', r['raw_text'].strip()]
            shown = (readings.get(file) or {}).get('same_as') or file      # a figure of the main text inside the joined document: the main text's picture
            if file in readings or (out_images / shown).exists():
                lines += ['', f'![{doc}-P{page:04d}-B{r["reading_order"]:04d}](images/{shown})'] + (transcript_lines(readings[file]) if file in readings else [])
            for c in r.get('captions') or []: lines.append(f'*{c.strip()}*')
            continue
        text = (r.get('raw_text') or '').strip()
        if text: lines += ['', text]
    return '\n'.join(lines).strip() + '\n'


only = set(filter(None, option('--papers', '').split(',')))
papers = sorted((e['position'], rid) for rid, e in imports.items() if rid.endswith('-c') and (not only or rid in only))
rows = []
for position, rid in papers:
    entry = imports[rid]; meta = index.get(entry['doi']) or {}; folder = out / f'{position:03d}_{rid.removesuffix("-c")}'
    run = HERE / 'whole-paper' / f'{BATCH}-{rid}'; chain = (ledger.get(rid) or {}).get('chain') or {}
    for sub in ('source', 'processed/images', 'results'): (folder / sub).mkdir(parents=True, exist_ok=True)
    out_images = folder / 'processed/images'
    if not RESULTS_ONLY:
        article = json.loads((Path(meta['package_path']) / 'article.json').read_text(encoding='utf-8')) if meta.get('package_path') else {}
        if article:
            shutil.copyfile(Path(meta['package_path']) / 'main/source/paper.pdf', folder / 'source/main.pdf')
            for si in article.get('si') or []: shutil.copyfile(Path(meta['package_path']) / si['package'] / 'source/paper.pdf', folder / f"source/{si['package']}.pdf")
        images = HERE / 'figure-labels' / f'{rid}.images'
        for p in images.glob('*.png') if images.exists() else []: shutil.copyfile(p, out_images / p.name)
        pages_whole = {p.name for p in out_images.glob('*-PAGE.png')}
        if (run / 'package/package.md').exists(): shutil.copyfile(run / 'package/package.md', folder / 'processed/model_input.md')
        clean = FIX / 'si_packages' / f'{rid}__clean_b'
        if clean.exists(): (folder / 'processed/experimental.md').write_text(markdown(rid, clean, 'SI', entry.get('pages'), pages_whole), encoding='utf-8')
        if (FIX / 'paper_packages' / rid).exists(): (folder / 'processed/main.md').write_text(markdown(rid, FIX / 'paper_packages' / rid, 'MAIN'), encoding='utf-8')
        joined = next((FIX / 'si_packages' / rid / 'source').glob('*.pdf'), None)
        if joined: shutil.copyfile(joined, folder / 'processed/experimental.pdf')
        (folder / 'processed/documents.json').write_text(json.dumps({'documents': entry.get('documents'), 'pages': entry.get('pages'), 'notes': entry.get('notes')}, ensure_ascii=False, indent=1), encoding='utf-8')
        mine = [t for f, t in readings.items() if f.startswith(rid + '.')]
        pagefig = HERE / 'figure-labels' / f'{rid}.page-figures.json'
        if pagefig.exists(): mine += json.loads(pagefig.read_text(encoding='utf-8'))
        mended = HERE / 'figure-labels' / f'{rid}.rebox-figures.json'      # figures whose boxes were mended or added to (rebox_apply.py): that reading wins
        if mended.exists():
            fixed = {t['file']: t for t in json.loads(mended.read_text(encoding='utf-8'))}; mine = [fixed.pop(t['file'], t) for t in mine] + list(fixed.values())
        (folder / 'processed/figure_transcripts.json').write_text(json.dumps(mine, ensure_ascii=False, indent=1), encoding='utf-8')
    for src, dst in ((run / 'result.json', 'result.json'), (run / 'structure-binding.json', 'structure-binding.json'), (run / 'entries/results.json', 'entries_detail.json'),
                     (HERE / 'figure-labels' / f'{rid}.owners.json', 'pages_characterizing.json')):
        if src.exists(): shutil.copyfile(src, folder / 'results' / dst)
    result = json.loads((run / 'result.json').read_text(encoding='utf-8')) if (run / 'result.json').exists() else {}
    blocks = result.get('reaction_blocks') or []; frames = result.get('characterization_records') or []
    bound = json.loads((run / 'structure-binding.json').read_text(encoding='utf-8')) if (run / 'structure-binding.json').exists() else {}
    notes = list(entry.get('notes') or [])
    if chain.get('state') == 'gaps': notes.append(f"{chain.get('gaps')} experiment entries without a record: the provider declined them")
    rows.append({'position': position, 'folder': folder.name, 'paper_id': rid, 'doi': entry['doi'], 'title': meta.get('title'), 'year': meta.get('year'), 'journal': meta.get('journal'),
                 'documents': '+'.join(entry.get('documents') or []), 'state': chain.get('state'), 'reaction_blocks': len(blocks),
                 'experiment_entries_without_a_record': sum(1 for b in blocks if b.get('record') is None),
                 'compounds': len(bound.get('compounds') or []), 'compounds_with_a_drawing': sum(1 for c in bound.get('compounds') or [] if c.get('structures')),
                 'characterization_records': len(frames), 'of_them_pictures': sum(1 for f in frames if f.get('data_kind') in ('FIGURE', 'PAGE')),
                 'author_disagreements': len(result.get('author_disagreements') or []), 'unresolved': len(result.get('unresolved') or []), 'notes': ' | '.join(notes)})
    print(folder.name, rows[-1]['reaction_blocks'], 'blocks', flush=True)
if only and (out / 'INDEX.csv').exists():      # some papers packed again: the index keeps the other papers' rows
    done = {r['paper_id'] for r in rows}
    kept = [{k: (int(v) if isinstance(v, str) and v.isdigit() else v) for k, v in r.items()} for r in csv.DictReader(open(out / 'INDEX.csv', encoding='utf-8-sig')) if r['paper_id'] not in done]
    rows = sorted(rows + kept, key=lambda r: int(r['position']))
with open(out / 'INDEX.csv', 'w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, list(rows[0])); w.writeheader(); w.writerows(rows)
total = lambda k: sum(r[k] or 0 for r in rows)
(out / 'README_说明.md').write_text(f"""# 全合成 200 篇：机器解析交付包（{time.strftime('%Y-%m-%d')}）

本包是 {len(rows)} 篇全合成论文的原文、前置处理后的文本和图片、以及机器解析结果，每篇一个文件夹，`INDEX.csv` 是总索引。
共 {total('reaction_blocks')} 个反应记录块，{total('compounds')} 个化合物（其中 {total('compounds_with_a_drawing')} 个已对应到图中的结构式），
{total('characterization_records')} 条表征记录（其中 {total('of_them_pictures')} 条是图片或整页谱图）。

**说明**：这些都是模型和规则生成的机器结果，没有经过人工核对；结构式的 SMILES 来自另一份 OCSR 交付包，交回后才会填进结构绑定。

## 每篇文件夹（`<序号>_<论文编号>`）

- `source/`：原始 PDF。`main.pdf` 是正文，`si_001.pdf`、`si_002.pdf`… 是交付时的每一份 SI（包括重复的副本）。
- `processed/`：模型读到的内容。
  - `model_input.md`：记录步骤实际读到的全文：正文、SI 的一般方法、每个实验条目，图以转录文字的形式给出。
  - `experimental.md`：完整的实验文档，按页展开，正文是前置处理核对过的文字，每张图在原位插入图片和读图结果；没有成段文字的页面（谱图等）以整页图给出。
  - `main.md`：正文，按页展开，格式同上。
  - `experimental.pdf`：把这篇论文的各份文档（步骤最多的 SI、其他 SI、正文；重复的副本只留一份）按顺序拼成的一个 PDF；`documents.json` 说明每页来自哪份文档。
  - `images/`：读过的图（从 PDF 页面切出）和没有成段文字页面的整页渲染图；`figure_transcripts.json` 是读图结果（框的标记、印刷编号、箭头、表格）。
- `results/`：机器解析结果。
  - `result.json`：反应记录（每个步骤的物料、用量、条件、产物）、论文级记录（靶标、化合物表、路线）、表征记录（文字数据、图片和整页谱图的归属）、作者之间不一致之处、未解决项。
  - `structure-binding.json`：图中的结构式（框）对应到哪个化合物。
  - `entries_detail.json`：每个实验条目的记录过程（数量标记、核对结果）。
  - `pages_characterizing.json`：没有成段文字的页面，以及每页是哪个化合物的表征。

## INDEX.csv 的列

序号、文件夹、论文编号、DOI、标题、年份、期刊、合并的文档、状态、反应记录块数、没有记录的条目数、化合物数、有结构图的化合物数、表征记录数（其中图片数）、作者不一致处、未解决项、备注。
状态为 `gaps` 的论文，有少数实验条目被模型服务的内容过滤拒绝处理，这些条目没有记录，备注里写明了数量。
""", encoding='utf-8')
print(json.dumps({'papers': len(rows), 'reaction blocks': total('reaction_blocks'), 'compounds': total('compounds')}, ensure_ascii=False))
