"""The cleaning layer: one text per block, made from two readings of the page, with the origin of every character kept.

Structure is the parser's (blocks, boxes, reading order). Which character is printed is the PDF text layer's. Whether digits, subscripts and
units stand together is the layer's; where one word ends and the next begins is the parser's. A sign the author typed in another way (a
degree sign set as a small raised o) is the sign when the parser reads it as that sign. Where the two readings cannot be brought together
by these rules nothing is chosen silently: the parser's text is kept and the stretch is marked.

Origins, per character of the output:
  AGREED    both readings have this character
  LAYER     the layer's character where the parser had another or none
  RULE      an equivalence the rules state (degree sign), or a space decided by rule
  PARSER    only the parser read this block or stretch (no text layer, or the box does not hold the block's text)
  DISPUTED  the readings differ and no rule decides: the parser's characters are kept and flagged

No model is called. Nothing in a package is changed in place: `write_clean_package` writes a copy with the reconciled text.
usage: clean_layer.py <paper> [--write]   (reports; with --write also writes tests/fixtures/si_packages/<paper>__clean and the origin file)"""
import collections, difflib, hashlib, json, re, shutil, sys, unicodedata
from pathlib import Path
import pdfplumber

HERE = Path(__file__).resolve().parent.parent; FIX = HERE.parents[1] / 'tests/fixtures/si_packages'
SYMBOLS = {'delta': 'δ', 'alpha': 'α', 'beta': 'β', 'gamma': 'γ', 'mu': 'µ', 'nu': 'ν', 'circ': '°', 'times': '×', 'pm': '±', 'rightarrow': '→', 'to': '→', 'cdot': '·', 'prime': "'", 'sim': '~', 'approx': '≈', 'Delta': 'Δ', 'lambda': 'λ',
           'geq': '≥', 'leq': '≤', 'ge': '≥', 'le': '≤', 'pi': 'π', 'sigma': 'σ', 'theta': 'θ', 'epsilon': 'ε', 'varepsilon': 'ε', 'omega': 'ω', 'Omega': 'Ω', 'AA': 'Å', 'infty': '∞', 'dag': '†', 'dagger': '†', 'ast': '*', 'bullet': '•', 'ell': 'l',
           'tau': 'τ', 'rho': 'ρ', 'degree': '°', 'textdegree': '°', 'micro': 'µ', 'eta': 'η', 'kappa': 'κ', 'phi': 'φ', 'chi': 'χ', 'psi': 'ψ', 'neq': '≠', 'equiv': '≡', 'ddagger': '‡', 'star': '*', 'percent': '%', 'zeta': 'ζ', 'xi': 'ξ', 'Sigma': 'Σ', 'Phi': 'Φ', 'Psi': 'Ψ', 'Gamma': 'Γ', 'Lambda': 'Λ', 'Pi': 'Π', 'Theta': 'Θ'}
MAP = str.maketrans({'−': '-', '–': '-', '—': '-', '‐': '-', '‑': '-', '‒': '-', '∘': '°', 'º': '°', '˚': '°', '̊': '°', '′': "'", '’': "'", '‘': "'", 'μ': 'µ', '­': '-', '​': '', '﻿': '', '∼': '~', '⋅': '·'})
_SYMBOL_HIGH = {0xB0: '°', 0xB1: '±', 0xB4: '×', 0xAE: '→', 0xD7: '·', 0xA2: "'", 0xBB: '≈', 0xA3: '≤', 0xB3: '≥', 0xB7: '•', 0xB9: '≠', 0xBA: '≡', 0xA5: '∞', 0xC6: '∅', 0xB6: '∂'}
_SYM = dict(zip('abgdezhqiklmnxoprstufcyw', 'αβγδεζηθικλµνξοπρστυφχψω')); _SYM.update(zip('GDQLXPSUFYW', 'ΓΔΘΛΞΠΣΥΦΨΩ'))
GAP = 0.10
_TOKEN = re.compile(r'\\begin\{[A-Za-z*]+\}(?:\{[lcr|@{}p0-9.]*\})?|\\end\{[A-Za-z*]+\}|\\\\|</?(?:sup|sub|b|i|em|strong|u|span)[^>]*>|\*\*|__|\\%|\\[#&_{}$~]|\\[A-Za-z]+|[$^_{}~\\]|\s+|.', re.S)


def fold(text):
    """One form for each dash, prime, degree and micro sign; compatibility forms folded (a subscript two is a two, a ligature is its letters)."""
    return unicodedata.normalize('NFKC', unicodedata.normalize('NFKC', text or '').translate(MAP)).translate(MAP).replace('μ', 'µ').replace('℃', '°C')


def parser_chars(raw):
    """The parser's text without its markup, as (character, offset in the raw text) pairs; a space where the raw text or its markup separates.
    Every output character points back to where it stands in the parser's own text."""
    out = []
    def put(text, at):
        for ch in fold(text):
            if ch.isspace():
                if out and out[-1][0] != ' ': out.append((' ', at))
            else: out.append((ch, at))
    for m in _TOKEN.finditer(raw or ''):
        t = m.group(0)
        if t.startswith('<') and t.endswith('>') and len(t) > 1 or t in ('**', '__'): continue
        if t.startswith(('\\begin{', '\\end{')) or t == '\\\\': put(' ', m.start()); continue
        if t == '\\%': put('%', m.start())
        elif len(t) == 2 and t[0] == '\\' and t[1] in '#&_{}$~': put(t[1], m.start())        # an escaped character is that character
        elif t[0] == '\\' and len(t) > 1: put(' ' + SYMBOLS.get(t[1:], '') + ' ', m.start())
        elif t in '$^_{}~\\' or t.isspace(): put(' ', m.start())
        else: put(t, m.start())
    while out and out[0][0] == ' ': out.pop(0)
    while out and out[-1][0] == ' ': out.pop()
    return out


def _glyph(c):
    """The character a glyph prints: a Symbol font stores Greek under Latin letters and symbols under private-use codes."""
    text = c['text']
    if 'symbol' in c['fontname'].lower() or any(0xF000 <= ord(ch) <= 0xF0FF for ch in text):
        # A private-use code F0xx stands for position xx of the Symbol font; that position, like a plain letter in the font, is Greek.
        plain = ''.join(chr(ord(ch) - 0xF000) if 0xF020 <= ord(ch) <= 0xF0FF else ch for ch in text)
        return ''.join(_SYM.get(ch, _SYMBOL_HIGH.get(ord(ch), ch)) for ch in plain) if 'symbol' in c['fontname'].lower() or plain != text else text
    return text


def layer_chars(page, box, margin=1.5):
    """The text layer's characters whose centre lies in the box, in reading order, as dicts {ch, raised, small, box}; {ch: ' '} where the
    geometry shows a gap. Lines come from the full-size characters; small ones join the line whose band holds their centre; a run of small
    characters holding a subscript and a superscript stacked at one place is read lowered first, then raised."""
    W, H = float(page.width), float(page.height); x0, top, x1, bottom = box[0] * W / 1000 - margin, box[1] * H / 1000 - margin, box[2] * W / 1000 + margin, box[3] * H / 1000 + margin
    # Only upright characters: text set turned on the page (a download stamp running up the margin) is not part of any block, and read
    # across the page its letters would fall into the lines beside it.
    chars = [c for c in page.chars if x0 <= (c['x0'] + c['x1']) / 2 <= x1 and top <= (c['top'] + c['bottom']) / 2 <= bottom and c['text'].strip() and c.get('upright', True)]
    if not chars: return []
    seen, kept = set(), []
    for c in chars:      # a glyph painted twice (bold by overprint) is one character
        key = (c['text'], round(c['x0'], 1), round(c['top'], 1))
        if key not in seen: seen.add(key); kept.append(c)
    chars = kept; sizes = sorted(c['size'] for c in chars); main = sizes[len(sizes) // 2]
    # Lines are chains of characters that stand side by side: a character joins the chain of a neighbour less than an em away along the line. A raised or lowered character is therefore always on the line of the
    # character it is attached to, never on the line before or after, whatever its size.
    order = sorted(range(len(chars)), key=lambda k: chars[k]['x0']); parent = list(range(len(chars)))
    def root(k):
        while parent[k] != k: parent[k] = parent[parent[k]]; k = parent[k]
        return k
    mids = [(c['top'] + c['bottom']) / 2 for c in chars]
    for a_pos, a in enumerate(order):
        for b in order[a_pos + 1:]:
            if chars[b]['x0'] - chars[a]['x1'] > 1.0 * main: break
            # side by side means the two overlap in height by at least a third of the smaller one: a raised or lowered character overlaps
            # the one it is attached to, and does not reach the line above or below
            overlap = min(chars[a]['bottom'], chars[b]['bottom']) - max(chars[a]['top'], chars[b]['top'])
            if overlap >= 0.34 * min(chars[a]['bottom'] - chars[a]['top'], chars[b]['bottom'] - chars[b]['top']) and chars[b]['x0'] >= chars[a]['x0'] - 0.3 * main: parent[root(b)] = root(a)
    groups = collections.defaultdict(list)
    for k in range(len(chars)): groups[root(k)].append(chars[k])
    lines = []
    for members in groups.values():
        full = [c for c in members if c['size'] >= 0.8 * main] or members; mid = sorted((c['top'] + c['bottom']) / 2 for c in full)[len(full) // 2]
        lines.append({'mid': mid, 'chars': members, 'x0': min(c['x0'] for c in members)})
    # chains that share a line (a gap wider than an em split them) are put back side by side, top to bottom, then left to right
    lines.sort(key=lambda l: (l['mid'], l['x0'])); merged = []
    for l in lines:
        if merged and abs(l['mid'] - merged[-1]['mid']) < 0.4 * main: merged[-1]['chars'] += l['chars']
        else: merged.append(l)
    lines = merged
    out = []
    for n, line in enumerate(lines):
        row = sorted(line['chars'], key=lambda c: c['x0']); ordered, run = [], []
        def flush():
            low = [c for c in run if (c['top'] + c['bottom']) / 2 >= line['mid']]; high = [c for c in run if (c['top'] + c['bottom']) / 2 < line['mid']]
            if low and high and min(c['x0'] for c in high) < max(c['x1'] for c in low) and min(c['x0'] for c in low) < max(c['x1'] for c in high): ordered.extend(low + high)
            else: ordered.extend(run)
            run.clear()
        for c in row:
            if c['size'] < 0.8 * main: run.append(c)
            else: flush(); ordered.append(c)
        flush()
        # A line that ends in a hyphen goes on without a space. Whether the hyphen belongs to the word (a compound name) or only to the line
        # break the layer cannot say; it is marked, and the parser's reading decides it.
        if n and out and out[-1]['ch'] == '-': out[-1]['line_end_hyphen'] = True
        elif n: out.append({'ch': ' '})
        for a, b in zip([None] + ordered, ordered):
            # Measured on five supplements (2026-10-14): characters of one token stand less than 0.04 of the type size apart, a printed space
            # is 0.16 or more, and almost nothing lies between. A tenth of the type size separates the two.
            if a is not None and b['x0'] - a['x1'] > GAP * max(a['size'], b['size']) and b['x0'] > a['x0']: out.append({'ch': ' '})
            small = b['size'] < 0.8 * main; raised = small and (b['top'] + b['bottom']) / 2 < line['mid'] - 0.08 * main
            for ch in fold(_glyph(b)):
                if not ch.isspace(): out.append({'ch': ch, 'small': small, 'raised': raised, 'box': (round(b['x0'], 1), round(b['top'], 1), round(b['x1'], 1), round(b['bottom'], 1))})
    return out


def _squeeze(chars, get=lambda c: c):
    idx = [i for i, c in enumerate(chars) if get(c) != ' ']; return ''.join(get(chars[i]) for i in idx), idx


def _cls(ch): return 'digit' if ch.isdigit() else 'letter' if ch.isalpha() else 'other'


def _rule_text(P):
    """Cleaning by rule alone, for a block no layer speaks for: a run of three or more single characters is closed up. Flagged by the caller."""
    text = ''.join(ch for ch, _ in P); tokens = text.split(' '); out = []; single = [len(t) == 1 for t in tokens]
    for n, t in enumerate(tokens):
        in_run = single[n] and ((n > 0 and single[n - 1] and ((n > 1 and single[n - 2]) or (n + 1 < len(tokens) and single[n + 1]))) or (n + 2 < len(tokens) and single[n + 1] and single[n + 2]))
        prev_in_run = n > 0 and single[n - 1] and in_run
        out.append(t if not out else ('' if prev_in_run else ' ') + t)
    return ''.join(out)


def reconcile(raw, layer, page_layer=None):
    """(text, spans, report) for one block. `layer` is layer_chars of the block's box; `page_layer`, when given, is a callable returning the
    layer of the whole page and of the next, used when the box does not hold the block's text."""
    P = parser_chars(raw); Ps, pi = _squeeze(P, lambda c: c[0]); report = collections.Counter(); notes = []
    def parser_only(why):
        text = _rule_text(P); report[why] += 1; return text, [[0, len(text), 'PARSER']], {'counts': dict(report), 'notes': notes, 'status': why, 'sites': [{'start': 0, 'end': len(text), 'parser': text, 'layer': None, 'kind': 'PARSER', 'box': None}]}
    if not Ps: return '', [], {'counts': {}, 'notes': [], 'status': 'empty', 'sites': []}
    empty_box = not layer
    if empty_box and page_layer is None: return parser_only('no text layer in the box')
    Ls, li = _squeeze(layer, lambda c: c['ch'])
    def far(a, b): return not b or difflib.SequenceMatcher(None, a, b, autojunk=False).quick_ratio() < 0.75 or not (0.7 <= len(b) / max(1, len(a)) <= 1.4)
    if far(Ps, Ls) and page_layer is not None:
        # The box and the block do not cover the same text (the paragraph runs on to the next page, or the parser cut the block elsewhere):
        # look for the block's own beginning and end in the layer of the page and the next.
        whole = page_layer(); Ws, wi = _squeeze(whole, lambda c: c['ch']); start = end = None
        for k in (0, 8, 20):
            key = Ps[k:k + 18]
            if len(key) == 18 and Ws.count(key) == 1: start = Ws.find(key) - k; break
        for k in (0, 8, 20):
            key = Ps[len(Ps) - k - 18:len(Ps) - k]
            if len(key) == 18 and Ws.count(key) == 1: end = Ws.find(key) + 18 + k; break
        if (start is None or end is None) and 8 <= len(Ps) < 40 and Ws.count(Ps) == 1: start = Ws.find(Ps); end = start + len(Ps)      # short: the whole of it, once on the page
        if start is not None and end is not None and start >= 0 and 0.7 <= (end - start) / len(Ps) <= 1.4:
            layer = whole[wi[start]:wi[end - 1] + 1]; Ls, li = _squeeze(layer, lambda c: c['ch']); report['found on the page instead of in the box'] += 1
    if far(Ps, Ls): return parser_only('no text layer in the box' if empty_box and not Ls else 'box does not hold the block\'s text')
    single = {}; n = 0; tokens = ''.join(ch for ch, _ in P).split(' ')
    for t_n, t in enumerate(tokens):
        run = len(t) == 1 and ((t_n > 0 and len(tokens[t_n - 1]) == 1 and ((t_n > 1 and len(tokens[t_n - 2]) == 1) or (t_n + 1 < len(tokens) and len(tokens[t_n + 1]) == 1))) or (t_n + 2 < len(tokens) and len(tokens[t_n + 1]) == 1 and len(tokens[t_n + 2]) == 1))
        for _ in t: single[n] = run; n += 1
    p_gap = lambda k: k > 0 and pi[k] != pi[k - 1] + 1          # a space stands before squeezed character k in the parser's text
    l_gap = lambda k: k > 0 and li[k] != li[k - 1] + 1
    out = []                                                     # (character, origin, parser gap before, layer gap before, in a spaced-out run)
    raised, small = set(), set()                                 # indexes in out of characters the layer shows raised; set smaller than the line
    sites = []                                                   # (first out index, last out index + 1, parser piece, layer piece, kind, layer index range)
    def site(kind, x, y, j1, j2, start): sites.append((start, len(out), x, y, kind, j1, j2))
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, Ps, Ls, autojunk=False).get_opcodes():
        x, y = Ps[i1:i2], Ls[j1:j2]
        if tag == 'equal':
            for a, b in zip(range(i1, i2), range(j1, j2)):
                # both read a letter o, the layer shows it raised, and it stands between a number and the letter of a temperature scale:
                # the author set the degree sign as a raised o (convention), so it is the degree sign. Not a place for the image to settle:
                # the page itself prints an o, and the origin RULE keeps the change visible.
                if Ps[a] in 'oO' and layer[li[b]].get('raised') and a > 0 and Ps[a - 1].isdigit() and Ps[a + 1:a + 2] in ('C', 'F'):
                    report['rule: degree sign typed as a raised o, both readings o'] += 1
                    out.append(('°', 'RULE', p_gap(a), l_gap(b), single.get(a, False))); continue
                out.append((Ps[a], 'AGREED', p_gap(a), l_gap(b), single.get(a, False)))
                if layer[li[b]].get('raised'): raised.add(len(out) - 1)
                if layer[li[b]].get('small'): small.add(len(out) - 1)
            continue
        ctx = Ps[max(0, i1 - 12):i1] + '[' + x + '|' + y + ']' + Ps[i2:i2 + 10]
        if len(x) > 6 or len(y) > 6:
            report['disputed: a long stretch'] += 1; notes.append(('DISPUTED', ctx[:120]))
            start = len(out)
            for a in range(i1, i2): out.append((Ps[a], 'DISPUTED', p_gap(a), None, single.get(a, False)))
            site('DISPUTED', x, y, j1, j2, start); continue
        degree = '°' in x and y and all(c in 'oO°' for c in y) and all(layer[li[b]].get('raised') or layer[li[b]]['ch'] == '°' for b in range(j1, j2))
        if degree:                                               # the author typed the degree sign as a raised o and the parser read a degree sign
            report['rule: degree sign typed as a raised o'] += 1
            start = len(out)
            for a in range(i1, i2): out.append((Ps[a], 'RULE', p_gap(a), None, single.get(a, False)))
            site('RULE', x, y, j1, j2, start); continue
        if set(x + y) == {'-'} and all(layer[li[b]].get('line_end_hyphen') for b in range(j1, j2)):
            report['rule: hyphen at a line end, as the parser reads it'] += 1          # the layer has it, the parser joined the word: the parser's
            continue
        if y == '' and any(c.isalnum() for c in x):              # the parser has letters or digits the layer does not: the layer may be incomplete here
            report['disputed: parser has characters the layer lacks'] += 1; notes.append(('DISPUTED', ctx[:120]))
            start = len(out)
            for a in range(i1, i2): out.append((Ps[a], 'DISPUTED', p_gap(a), None, single.get(a, False)))
            site('DISPUTED', x, y, j1, j2, start); continue
        kind = 'digit' if re.search(r'\d', x + y) else 'greek' if re.search(r'[α-ωΑ-Ω]', x + y) else 'letter' if re.search(r'[A-Za-z]', x + y) else 'unit or symbol' if re.search(r'[°%µ×±→·~≈<>=+/-]', x + y) else 'punctuation'
        report['layer over parser: ' + kind] += 1; notes.append(('LAYER', ctx[:120]))
        start = len(out)
        for b in range(j1, j2):
            out.append((Ls[b], 'LAYER', p_gap(i1) if b == j1 and i1 < len(Ps) else None, l_gap(b), False))
            if layer[li[b]].get('raised'): raised.add(len(out) - 1)
            if layer[li[b]].get('small'): small.add(len(out) - 1)
        site('LAYER', x, y, j1, j2, start)
    text, origins, pos = [], [], []
    for n, (ch, origin, pg, lg, in_run) in enumerate(out):
        space = False
        if n:
            prev = out[n - 1][0]
            if pg is None: space, why = bool(lg), origin                       # only one reading speaks for this place
            elif lg is None: space, why = bool(pg) and not (in_run and out[n - 1][4]), origin
            elif pg == lg: space, why = pg, None
            elif lg: space, why = True, 'RULE'; report['space: the layer shows a gap the parser closed'] += 1
            elif in_run and out[n - 1][4]: space, why = False, 'RULE'; report['space: closed inside a run of single characters'] += 1
            elif _cls(prev) == 'letter' and _cls(ch) == 'letter': space, why = True, 'RULE'; report['space: kept between two words (the parser\'s)'] += 1
            else: space, why = False, 'RULE'; report['space: closed between digits, subscripts or units (the layer\'s)'] += 1
            if space: text.append(' '); origins.append(why or 'AGREED')
        # A raised digit or sign right after a digit set on the line is an exponent or a reference mark: written plainly it would run into
        # the number before it, so a caret marks where the raised part begins.
        if n in raised and (ch.isdigit() or ch in '+-−') and n - 1 not in small and out[n - 1][1] in ('AGREED', 'LAYER') and not space and text and text[-1].isdigit():
            text.append('^'); origins.append('RULE'); report['raised digits after a number, marked with ^'] += 1
        pos.append(len(text)); text.append(ch); origins.append(origin)
    pos.append(len(text)); spans = []
    for k, o in enumerate(origins):
        if spans and spans[-1][2] == o: spans[-1][1] = k + 1
        else: spans.append([k, k + 1, o])
    status = 'reconciled'; placed = []
    for start, stop, x, y, kind, j1, j2 in sites:
        # the place itself: its own layer characters, or for a place the layer has nothing at, the characters on either side of it
        near = [layer[li[b]] for b in range(j1, min(len(li), j2)) if layer[li[b]].get('box')] or [layer[li[b]] for b in (j1 - 1, j1) if 0 <= b < len(li) and layer[li[b]].get('box')]
        box = (min(q['box'][0] for q in near), min(q['box'][1] for q in near), max(q['box'][2] for q in near), max(q['box'][3] for q in near)) if near else None
        page = near[0].get('page') if near else None
        placed.append({'start': pos[start] if start < len(pos) else len(text), 'end': pos[stop] if stop < len(pos) else len(text), 'parser': x, 'layer': y, 'kind': kind, 'box': box, 'page': page})
    # where on the page the reconciled text lies (points, per page), from the layer characters it was read from
    region = {}
    for q in layer:
        if q.get('box') and q.get('page') is not None:
            b = region.setdefault(q['page'], [q['box'][0], q['box'][1], q['box'][2], q['box'][3], 0])
            b[0], b[1], b[2], b[3], b[4] = min(b[0], q['box'][0]), min(b[1], q['box'][1]), max(b[2], q['box'][2]), max(b[3], q['box'][3]), b[4] + 1
    return ''.join(text), spans, {'counts': dict(report), 'notes': notes, 'status': status, 'sites': placed, 'region': region}


def clean_paper(paper, kind='si_packages'):
    """Reconcile every text block of a package, and every caption (keyed <block_id>#c<n>). Returns {key: {'text', 'spans', 'status', 'counts', 'notes', 'parser', ...}}."""
    root = HERE.parents[1] / 'tests/fixtures' / kind / paper
    rows = [json.loads(l) for l in (root / 'normalized/blocks.jsonl').read_text(encoding='utf-8').splitlines()]; pdf = pdfplumber.open(next((root / 'source').glob('*.pdf'))); out = {}; cache = {}
    def whole(i):
        if i not in cache: cache[i] = [{**q, 'page': i} for q in layer_chars(pdf.pages[i], [0, 0, 1000, 1000], margin=0)] if 0 <= i < len(pdf.pages) else []
        return cache[i]
    doc = 'SI' if kind == 'si_packages' else 'MAIN'
    for r in rows:
        if r['type'] != 'text' or not (r.get('raw_text') or '').strip() or r['page_idx'] >= len(pdf.pages): continue
        block_id = '%s-P%04d-B%04d' % (doc, r['page_idx'], r['reading_order']); page = r['page_idx']
        text, spans, report = reconcile(r['raw_text'], [{**q, 'page': page} for q in layer_chars(pdf.pages[page], r['bbox_norm_1000'])], lambda: whole(page) + [{'ch': ' '}] + whole(page + 1))
        out[block_id] = {'text': text, 'spans': spans, 'status': report['status'], 'counts': report['counts'], 'notes': report['notes'][:40], 'sites': report['sites'], 'parser': r['raw_text'], 'page': page, 'block_box': r['bbox_norm_1000']}
    # The text the parser gave as a figure's or a table's caption is printed text like any other -- a whole procedure is sometimes set
    # under a drawing and read as its caption. Its own box is not given, so it is looked for on the page (and the next) by its first and
    # last characters, and where it is found its box is the box of the layer characters it was read from.
    for r in rows if not __import__('os').environ.get('CLEAN_TEXT_ONLY') else []:      # CLEAN_TEXT_ONLY=1: text blocks only, as before 2026-10-14
        if r['type'] == 'text' or r['page_idx'] >= len(pdf.pages): continue
        block_id = '%s-P%04d-B%04d' % (doc, r['page_idx'], r['reading_order']); page = r['page_idx']
        for i, caption in enumerate(r.get('captions') or []):
            if len(caption.strip()) < 20: continue
            text, spans, report = reconcile(caption, [{**q, 'page': page} for q in layer_chars(pdf.pages[page], r['bbox_norm_1000'])], lambda: whole(page) + [{'ch': ' '}] + whole(page + 1))
            region = report.get('region') or {}; at = max(region, key=lambda k: region[k][4]) if region else None
            box = None
            if at is not None and report['status'] == 'reconciled':
                w, h = float(pdf.pages[at].width), float(pdf.pages[at].height); x0, y0, x1, y1, _ = region[at]
                box = [round(1000 * x0 / w), round(1000 * y0 / h), round(1000 * x1 / w), round(1000 * y1 / h)]
            out[f'{block_id}#c{i}'] = {'text': text, 'spans': spans, 'status': report['status'], 'counts': report['counts'], 'notes': report['notes'][:40], 'sites': report['sites'],
                                       'parser': caption, 'page': at if at is not None else page, 'block_box': box, 'caption_of': block_id, 'caption_index': i}
    return out, rows


def write_clean_package(paper, cleaned, rows, kind='si_packages', suffix=''):
    """A copy of the package with the reconciled text in place of the parser's, so that every later step reads it unchanged. The origin of the
    characters is written beside the workspace, not into the package (a package holds only what its manifest lists)."""
    src = HERE.parents[1] / 'tests/fixtures' / kind / paper; dst = src.parent / f'{paper}__clean{suffix}'
    if dst.exists(): shutil.rmtree(dst)
    shutil.copytree(src, dst); doc = 'SI' if kind == 'si_packages' else 'MAIN'; lines = []
    for r in rows:
        block_id = '%s-P%04d-B%04d' % (doc, r['page_idx'], r['reading_order'])
        if block_id in cleaned and cleaned[block_id]['text']: r = {**r, 'raw_text': cleaned[block_id]['text']}
        if r.get('captions'):
            r = {**r, 'captions': [cleaned[f'{block_id}#c{i}']['text'] if cleaned.get(f'{block_id}#c{i}', {}).get('text') else c for i, c in enumerate(r['captions'])]}
        lines.append(json.dumps(r, ensure_ascii=False))
    data = ('\n'.join(lines) + '\n').encode('utf-8'); (dst / 'normalized/blocks.jsonl').write_bytes(data)
    manifest = json.loads((dst / 'manifest.json').read_text(encoding='utf-8')); manifest['normalized_blocks'].update(byte_size=len(data), sha256=hashlib.sha256(data).hexdigest())
    (dst / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (HERE / ('clean' + suffix)).mkdir(exist_ok=True); (HERE / ('clean' + suffix) / f'{paper}.{doc}.origin.json').write_text(json.dumps(cleaned, ensure_ascii=False), encoding='utf-8')
    return dst


if __name__ == '__main__':
    paper = sys.argv[1]; cleaned, rows = clean_paper(paper); total = collections.Counter(); status = collections.Counter(); chars = collections.Counter()
    for b in cleaned.values():
        status[b['status']] += 1; total.update(b['counts'])
        for s, e, o in b['spans']: chars[o] += e - s
    print(paper, '| blocks', dict(status)); print('  characters by origin', dict(chars)); print('  ', dict(sorted(total.items())))
    if '--write' in sys.argv: print('  written', write_clean_package(paper, cleaned, rows))
