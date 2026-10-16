"""Bring the consolidated corpus (D:\\SI_consolidation\\ready_200_20261015_revised10) to where the chain reads it: per paper a main package
(tests/fixtures/paper_packages/<rid>) and one experimental document (tests/fixtures/si_packages/<rid>) that holds every document of the
paper, so that segmentation, not the import, decides where the experimental part is. No model is called.

  paper id     r<legacy rank>_<first 8 hex of the main PDF's sha256>, as the papers of D:\\tsdaf\\packages are named (--id-suffix for a test).
  main         the parser's own blocks, as the reference and held-out papers were run: D:\\tsdaf\\packages\\<rid> when its blocks are the
               delivery's main_input_blocks_sha256, else the delivery's main package (the delivery may have folded block types together).
  experimental one package of the paper's documents one after another, page after page, with the PDFs joined in the same order: first
               the SI document that prints the most procedure quantities (amounts, masses, volumes), then the paper's other SI documents
               in the delivery's order, then the main text. A copy of a document already taken (the same number of pages and, over the
               first 20,000 characters, at least 0.8 of the same text: an SI deposited twice or in two versions) is left out; of two
               versions the one printing more procedure quantities is taken; a main PDF that is such a copy of an SI is left out too. The SI documents are the
               delivery's packages, which also carry, as page pictures, the pages the parser returned nothing for. The first document keeps
               its page numbers, so its block handles are what they were for a single SI. A paper without an SI PDF has its main text.
  kept         a package already there (the reference and held-out papers) is not touched; its blocks are only compared.

Writes batch/ready200-import.json (beside the fixtures instead when --fixtures is given: per paper the documents taken, their pages
in the joined document, and what to look at) and batch/ready200.txt (the paper list for batch_run.py, the largest experimental documents first).
--plan prints the choices and writes nothing.
usage: import_ready200.py [--ready D:\\SI_consolidation\\ready_200_20261015_revised10] [--fixtures <dir>] [--only rid,rid] [--id-suffix -m] [--plan]"""
import collections, csv, difflib, hashlib, json, re, shutil, sys
from pathlib import Path
TOOLS = Path(__file__).resolve().parent; HERE = TOOLS.parent; REPO = HERE.parents[1]
option = lambda name, default: sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
READY = Path(option('--ready', r'D:\SI_consolidation\ready_200_20261015_revised10')); FIX = Path(option('--fixtures', str(REPO / 'tests/fixtures')))
OLD_MAIN = Path(r'D:\tsdaf\packages'); PLAN = '--plan' in sys.argv; ONLY = set(filter(None, option('--only', '').split(','))); SUFFIX = option('--id-suffix', '')
QUANTITY = re.compile(r'\d\s?(?:mmol|mg|mL|µL|μL)\b')


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rows_of(package): return [json.loads(line) for line in (package / 'normalized/blocks.jsonl').read_text(encoding='utf-8').splitlines()]


def signal(package):
    """How many procedure quantities a document prints."""
    return len(QUANTITY.findall(' '.join(r.get('raw_text') or '' for r in rows_of(package))))


def text_head(package): return ' '.join(r.get('raw_text') or '' for r in rows_of(package))[:20000]


def artifact(path, relative, media_type):
    data = path.read_bytes(); return {'sha256': hashlib.sha256(data).hexdigest(), 'media_type': media_type, 'byte_size': len(data), 'relative_path': relative}


def join(parts, out, paper_id, doi):
    """One package of `parts` ((name, package directory) pairs) one after another. Every block keeps its text, type, boxes and picture; what names
    the document it came from (the paper id, the PDF's sha256, the page number, the order in the document, the parser's own index) is
    renumbered for the joined document, and a picture whose name an earlier part already uses is renamed. A picture that is not a JPEG
    (the delivery's page pictures are PNG) is stored as a JPEG, which is what segmentation reads. Returns the pages of each part."""
    import fitz
    tmp = out.with_name(out.name + '.joining'); shutil.rmtree(tmp, ignore_errors=True)
    for sub in ('source', 'normalized', 'images'): (tmp / sub).mkdir(parents=True)
    pdf = fitz.open()
    pdf_of = lambda part: next((part / 'source').glob('*.pdf'))
    for _, part in parts:
        with fitz.open(pdf_of(part)) as one: pdf.insert_pdf(one)
    pdf.save(tmp / 'source/paper.pdf'); pdf.close(); pdf_sha = sha(tmp / 'source/paper.pdf')
    rows, pages, spans, used, assets, native, warnings, counts = [], [], [], set(), [], 0, [], collections.Counter()
    first_doc = None
    for k, (label, part) in enumerate(parts):
        doc = json.loads((part / 'normalized/document.json').read_text(encoding='utf-8')); first_doc = first_doc or doc
        with fitz.open(pdf_of(part)) as one:
            if one.page_count != len(doc['pages']): raise ValueError(f'{part}: the PDF has {one.page_count} pages, its parse {len(doc["pages"])}')
        offset = len(pages); spans.append({'document': label, 'from': str(part), 'pages': [offset, offset + len(doc['pages']) - 1]})
        renamed = {}
        for page in doc['pages']: pages.append({**page, 'page_idx': page['page_idx'] + offset})
        top = -1
        for row in rows_of(part):
            ref = row.get('asset_ref')
            if ref:
                name = Path(ref).name; jpeg = (part / ref).read_bytes()[:3] == b'\xff\xd8\xff'
                if not jpeg: name = Path(name).stem + '.jpg'
                if name in used: renamed.setdefault(name, f'p{k}_{name}'); name = renamed[name]
                if name not in used:
                    used.add(name)
                    if jpeg: shutil.copyfile(part / ref, tmp / 'images' / name)
                    else:
                        from PIL import Image
                        Image.open(part / ref).convert('RGB').save(tmp / 'images' / name, format='JPEG', quality=95)
                    assets.append(artifact(tmp / 'images' / name, f'images/{name}', 'image/jpeg'))
                ref = f'images/{name}'
            page = row['page_idx'] + offset; top = max(top, row['mineru_native_ref']['native_index'])
            rows.append({**row, 'paper_id': paper_id, 'source_sha256': pdf_sha, 'page_idx': page, 'document_order': len(rows), 'asset_ref': ref,
                         'mineru_native_ref': {**row['mineru_native_ref'], 'native_index': row['mineru_native_ref']['native_index'] + native},
                         'source_locator': {**row['source_locator'], 'pdf_sha256': pdf_sha, 'page_idx': page}})
        native += top + 1; warnings += doc.get('warnings') or []
        for key, value in (doc.get('counts') or {}).items():
            if isinstance(value, int): counts[key] += value
    document = {**first_doc, 'paper_id': paper_id, 'source_sha256': pdf_sha, 'pages': pages, 'warnings': warnings, 'counts': {**dict(counts), 'blocks': len(rows), 'images': sum(r['type'] in ('image', 'chart') for r in rows)}}
    (tmp / 'normalized/document.json').write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    (tmp / 'normalized/blocks.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
    # the manifest names the pictures in the order the blocks first use them
    order = list(dict.fromkeys(r['asset_ref'] for r in rows if r.get('asset_ref'))); by_path = {a['relative_path']: a for a in assets}
    manifest = {'schema_version': 'tsdaf.paper-package.v1', 'paper_id': paper_id, 'display_name': paper_id, 'identity': {'doi': doi, 'source_phase': 'SI'},
                'source_pdf': artifact(tmp / 'source/paper.pdf', 'source/paper.pdf', 'application/pdf'),
                'normalized_document': artifact(tmp / 'normalized/document.json', 'normalized/document.json', 'application/json'),
                'normalized_blocks': artifact(tmp / 'normalized/blocks.jsonl', 'normalized/blocks.jsonl', 'application/x-ndjson'), 'assets': [by_path[p] for p in order]}
    (tmp / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    sys.path.insert(0, str(TOOLS / 'src-units')); from tsdaf.lanes.document.package_loader import load_paper_package
    load_paper_package(tmp)                     # the chain's own loader: the joined package is a package
    tmp.rename(out); return spans


index = list(csv.DictReader(open(READY / 'INDEX.csv', encoding='utf-8-sig')))
report, order = {}, []
for row in index:
    rid = 'r%03d_%s' % (int(row['legacy_rank']), row['main_sha256'][:8]) + SUFFIX; order.append(rid)
    if ONLY and rid not in ONLY and rid.removesuffix(SUFFIX) not in ONLY: continue
    paper = Path(row['package_path']); article = json.loads((paper / 'article.json').read_text(encoding='utf-8'))
    entry = {'position': int(row['position']), 'doi': row['doi'], 'year': row['year'], 'notes': []}
    old = OLD_MAIN / rid.removesuffix(SUFFIX)
    main_source = old if (old / 'normalized/blocks.jsonl').exists() and sha(old / 'normalized/blocks.jsonl') == article['main_input_blocks_sha256'] else paper / 'main'
    if main_source != old and article['main_block_type_mappings']: entry['notes'].append('main blocks as delivered, types folded: ' + json.dumps(article['main_block_type_mappings']))
    # the documents, the one printing the most procedure quantities first, copies left out, the main text last
    sis = [(paper / s['package'], s) for s in article.get('si') or []]
    signals = {s['package']: signal(p) for p, s in sis}; signals['main'] = signal(main_source)
    taken = []
    for p, s in sorted(sis, key=lambda x: -signals[x[1]['package']]) if sis else []:
        twin = next((t for t in taken if t[1]['pages'] == s['pages'] and difflib.SequenceMatcher(None, text_head(t[0]), text_head(p), autojunk=False).ratio() >= 0.8), None)
        if twin: entry['notes'].append(f"{s['package']} left out: a copy of {twin[1]['package']}"); continue
        taken.append((p, s))
    taken = taken[:1] + sorted(taken[1:], key=lambda x: x[1]['package'])
    # a main PDF that is itself a copy of an SI document taken (the delivery's main was the SI deposited again) is not joined a second time
    twin = next((t for t in taken if int(t[1]['pages']) == int(row['main_pages']) and difflib.SequenceMatcher(None, text_head(t[0]), text_head(main_source), autojunk=False).ratio() >= 0.8), None)
    if twin: entry['notes'].append(f"main left out: a copy of {twin[1]['package']} (the delivered main PDF is that SI)")
    parts = [(s['package'], p) for p, s in taken] + ([] if twin else [('main', main_source)])
    entry.update(documents=[label for label, _ in parts], signals=signals, pages_joined=sum(int(s['pages']) for _, s in taken) + (0 if twin else int(row['main_pages'])))
    if not sis: entry['notes'].append('no SI PDF: the experimental document is the main text')
    if max(signals.values()) < 10: entry['notes'].append('hardly any procedure quantity printed anywhere (the SI may hold only data): expect few or no reaction records')
    for p, s in taken:
        if s['block_type_mappings']: entry['notes'].append(f"{s['package']} as delivered, types folded: " + json.dumps(s['block_type_mappings']))
    report[rid] = entry
    main_dir, si_dir = FIX / 'paper_packages' / rid, FIX / 'si_packages' / rid
    if PLAN: continue
    if main_dir.exists():
        entry['main'] = 'kept' + ('' if sha(main_dir / 'normalized/blocks.jsonl') == sha(main_source / 'normalized/blocks.jsonl') else ' (its blocks differ from the source chosen now)')
    else:
        shutil.copytree(main_source, main_dir); entry['main'] = 'copied'
    if si_dir.exists(): entry['experimental'] = 'kept'
    else:       # a paper without an SI PDF too: a package of its own, named as an experimental document
        try: entry['pages'] = join(parts, si_dir, f'{rid}-experimental', row['doi']); entry['experimental'] = 'joined'
        except Exception as error: entry['experimental'] = f'FAILED: {type(error).__name__}: {str(error)[:240]}'
    print(rid, entry['documents'], entry.get('main'), '|', entry.get('experimental'), '|', '; '.join(entry['notes'])[:160], flush=True)

counts = collections.Counter()
for e in report.values():
    counts[f"documents joined: {len(e['documents'])}"] += 1
    for n in e['notes']: counts[re.sub(r'^si_\d+ ', 'si_NNN ', n.split(':')[0])] += 1
    if str(e.get('experimental', '')).startswith('FAILED'): counts['FAILED'] += 1
print(json.dumps(dict(sorted(counts.items())), indent=1))
if PLAN: raise SystemExit
out = FIX if '--fixtures' in sys.argv else HERE / 'batch'; out.mkdir(parents=True, exist_ok=True)
previous = json.loads((out / 'ready200-import.json').read_text(encoding='utf-8')) if (out / 'ready200-import.json').exists() else {}
(out / 'ready200-import.json').write_text(json.dumps({**previous, **report}, ensure_ascii=False, indent=1), encoding='utf-8')
if not ONLY:
    # the largest experimental documents first: segmentation reads a document window after window, so a paper of several hundred pages
    # takes an hour or more and should not be the last one running
    order.sort(key=lambda rid: -report[rid]['pages_joined'])
    (out / 'ready200.txt').write_text(f'# {READY}, the largest experimental documents first (import_ready200.py); notes per paper in ready200-import.json\n' + '\n'.join(order) + '\n', encoding='utf-8')
