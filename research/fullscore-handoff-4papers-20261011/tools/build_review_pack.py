"""Build the review pack of one paper: the source files and the draft worksheets a reviewer confirms, corrects and completes against the PDF.
Everything here is mechanical (parse + reading units + extraction by rule). usage: build_review_pack.py <paper> <units.json> <out dir>"""
import collections, csv, json, re, shutil, sys
from pathlib import Path
paper, units_path, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
FIX = Path(__file__).resolve().parents[3] / 'tests/fixtures'
(out / 'inputs/main').mkdir(parents=True, exist_ok=True); (out / 'inputs/si').mkdir(parents=True, exist_ok=True); (out / 'worksheets').mkdir(exist_ok=True); (out / 'returned').mkdir(exist_ok=True)
import os
for kind, name in (('paper_packages', 'main'), ('si_packages', 'si')):
    # SI_TEXT=__clean: the SI text the pre-stage settled (the PDF is the same file)
    shutil.copy(FIX / kind / (paper + (os.environ.get('SI_TEXT', '') if kind == 'si_packages' else '')) / 'normalized/blocks.jsonl', out / 'inputs' / name / 'blocks.jsonl')
    pdfs = sorted((FIX / kind / paper / 'source').glob('*.pdf'))
    if pdfs: shutil.copy(pdfs[0], out / 'inputs' / name / 'paper.pdf')
shutil.copy(units_path, out / 'inputs/si/units.json')
blocks = {}
for line in (out / 'inputs/si/blocks.jsonl').read_text().splitlines():
    row = json.loads(line); blocks['SI-P%04d-B%04d' % (row['page_idx'], row['reading_order'])] = row
units = json.loads(units_path.read_text())['units']
_TEX = [(r'\\(?:text|mathrm|mathbf|textbf|mathit|operatorname)\s*\{([^{}]*)\}', r'\1'), (r'</?(?:sup|sub|b|i|em|strong)>', ''), (r'\^\s*\{?\s*(?:\\circ|o)\s*\}?\s*(?:\\mathrm\s*)?C', '°C'), (r'\^\s*\{?\s*\\circ\s*\}?', '°'),
        (r'\\mu\s*', 'µ'), (r'\\%', '%'), (r'\\times', '×'), (r'\\[,;! ]', ' '), (r'[$]', ''), (r'[{}]', ''), (r'μ', 'µ'), (r'[−–]', '-'), (r'~', ' ')]


def plain(text):
    for _ in range(3): text = re.sub(_TEX[0][0], _TEX[0][1], text)
    for pattern, to in _TEX[1:]: text = re.sub(pattern, to, text)
    return re.sub(r'[ \t]{2,}', ' ', text).replace('° C', '°C')


def text_of(row):
    # The parse sometimes hangs a whole paragraph on a figure as its caption (a procedure printed under its scheme): that is entry text.
    return plain('\n'.join([(row.get('raw_text') or '').strip(), *[c for c in (row.get('captions') or []) if len(c) > 200 or row['type'] == 'table']]).strip())


NUMBER = r'(?<![\w.])(-\s?)?(\d+(?:\.\d+)?)'
UNITS = r'(mmol|µmol|nmol|mol%|mol|mL|µL|L|mg|µg|kg|g|°C|%|equiv\.?|eq\.?|M|mM|µM|N|h|min|s|d|hours?|minutes?|seconds?|days?|drops?|mbar|bar|atm|psi|nm|W|rpm|wt%)'
QUANTITY = re.compile(NUMBER + r'\s?' + UNITS + r'(?![A-Za-z])')
WORDS = re.compile(r'\b(one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen|twenty|thirty|a few|several|an?|another|additional)\s+(hours?|minutes?|days?|drops?|times|portions?)\b|\b(overnight|room temperature|rt|r\.t\.|reflux|ambient temperature|twice|thrice)\b', re.I)
DATA = re.compile(r'(?:R\W*f\W*=?\W*0\W*\.|\[\W*(?:α|alpha)\W*\]|(?:1\W*H|13\W*C|19\W*F|31\W*P)\W{0,6}NMR|\bIR\W*\(|HRMS|HR-?ESI|\bm\.?p\.?\W|melting point)', re.I)
def write(name, rows):
    if not rows: return
    with (out / 'worksheets' / name).open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)


A, B, C, entries, n, k = [], [], [], [], 0, 0
for index, unit in enumerate(units):
    rows = [blocks[b] for b in unit['block_ids']]
    if unit['kind'] == 'PROCEDURE':
        n += 1; entry = f'E{n:02d}'; row_no = 0
        entries += [f"## {entry}  compound label as segmented: {unit['compound_label']}  (SI unit {index}, PDF page {rows[0]['page_idx'] + 1})", '']
        C.append({'entry': entry, 'si_unit': index, 'compound_label_as_segmented': unit['compound_label'], 'pdf_page': rows[0]['page_idx'] + 1, 'begins': re.sub(r'\s+', ' ', ' '.join(text_of(r) for r in rows))[:90],
                  'entry_type': '', 'segmentation_ok': '', 'reaction_block': '', 'substrate_labels': '', 'product_labels': '', 'on_mainline': '', 'printed_yield_raw': '', 'operation_order': '', 'required_stage_boundaries': '', 'note': ''})
        for block_id, row in zip(unit['block_ids'], rows):
            text = text_of(row); entries += [f"**{block_id}** · `{row['evidence_id']}` · PDF page {row['page_idx'] + 1} · type {row['type']}", '', text or '(no text: figure or empty block)', '']
            cut = DATA.search(text) if row['type'] not in ('table',) else None
            procedure = text[:cut.start()] if cut and cut.start() > 80 else text
            if cut and cut.start() > 80:
                k += 1; B.append({'row': f'C{k:03d}', 'compound_label': unit['compound_label'], 'data_kind_guess': 'DATA_AFTER_PROCEDURE', 'block_id': block_id, 'evidence_id': row['evidence_id'], 'pdf_page': row['page_idx'] + 1,
                                  'numbers_in_parsed_text': len(re.findall(r'\d+(?:\.\d+)?', text[cut.start():])), 'parsed_text': text[cut.start():], 'verdict': '', 'data_kind': '', 'correct_text': '', 'note': ''})
            found = [(m.start(), m.group(0), ((m.group(1) or '') + m.group(2)).replace(' ', ''), m.group(3).rstrip('.')) for m in QUANTITY.finditer(procedure)] if row['type'] != 'table' else []
            found += [(m.start(), m.group(0), '', 'WORDS') for m in WORDS.finditer(procedure)] if row['type'] != 'table' else []
            for at, raw, value, kind in sorted(found):
                row_no += 1
                A.append({'row': f'{entry}-{row_no:03d}', 'entry': entry, 'compound_label': unit['compound_label'], 'block_id': block_id, 'evidence_id': row['evidence_id'], 'pdf_page': row['page_idx'] + 1, 'raw': raw, 'value': value, 'unit': kind,
                          'context': procedure[max(0, at - 70):at + len(raw) + 40].replace('\n', ' '), 'verdict': '', 'correct_raw': '', 'role': '', 'owner': '', 'stage': '', 'note': ''})
    elif unit['kind'] == 'CHARACTERIZATION':
        for block_id, row in zip(unit['block_ids'], rows):
            text = text_of(row)
            if not text.strip(): continue
            k += 1; B.append({'row': f'C{k:03d}', 'compound_label': unit['compound_label'], 'data_kind_guess': 'TABLE' if row['type'] == 'table' else ('DATA' if DATA.search(text) else 'OTHER_OR_CONTINUATION'), 'block_id': block_id,
                              'evidence_id': row['evidence_id'], 'pdf_page': row['page_idx'] + 1, 'numbers_in_parsed_text': len(re.findall(r'\d+(?:\.\d+)?', text)), 'parsed_text': text, 'verdict': '', 'data_kind': '', 'correct_text': '', 'note': ''})
(out / 'worksheets/A_entries.md').write_text(f'# {paper}: SI procedure entries as parsed (markup flattened)\n\n' + '\n'.join(entries))
write('A_procedure_quantities.csv', A); write('B_characterization.csv', B); write('C_entries_and_blocks.csv', C)
write('E_author_disagreements.csv', [{'row': 'X01', 'entries': '', 'kind': '', 'si_states': '', 'si_where': '', 'main_states': '', 'main_where': '', 'same_kind_of_statement': '', 'note': '(example row: replace or delete)'}])
print(f"{paper}: entries {n} | A rows {len(A)} | B rows {len(B)} ({dict(collections.Counter(r['data_kind_guess'] for r in B))}) | SI pages {max(r['page_idx'] for r in blocks.values()) + 1}")
