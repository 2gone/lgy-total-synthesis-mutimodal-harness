"""The pre-stage: before segmentation, every place where the parser's text and the PDF text layer were not brought together is settled by
the page itself. The lines that hold such places are cut out of the rendered page and transcribed by the model, which is shown no
reading; the host then compares the transcription with the two readings at each place, mechanically:

  the transcription has one reading's characters there   -> that reading            (VERIFIED_PARSER / VERIFIED_LAYER)
  it has both (they are the same once spacing is set aside) -> unchanged             (UNCHANGED)
  it has neither                                         -> read again, larger; when the two readings of the image agree, the image's
                                                            characters                (IMAGE), otherwise the place is marked (FLAGGED)
  the image could not be read                            -> the rule's result stays, marked (NOT_READ)

A block that only the parser could read is transcribed whole and compared the same way. With --agree-check the other text blocks that
hold a letter o after a digit are also transcribed whole: that o is made a degree sign where two readings of the image show one; any
other difference two readings repeat is only marked (CHECK), the text is never changed by it.
The result is a copy of the package with the settled text (tests/fixtures/si_packages/<paper>__clean), and beside the workspace the
origin of every place and the timing. Credentials from the process environment only (OPENAI_*).
usage: prestage_resolve.py <paper> [--agree-check] [--parallel 16] [--model <vision model>] [--effort <prestage_read>] [--fallback-effort <prestage_read_fallback>] (defaults: tools/run_config.json) [--suffix <s>]"""
import base64, collections, io, json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pypdfium2 as pdfium
from openai import OpenAI
sys.path.insert(0, str(Path(__file__).resolve().parent)); import run_config, waits      # models and thinking levels: tools/run_config.json
sys.path.insert(0, str(Path(__file__).resolve().parent)); import clean_layer as cl

paper = sys.argv[1]; option = lambda name, default: sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
PARALLEL, MODEL, EFFORT, AGREE = int(option('--parallel', '16')), option('--model', run_config.model('vision')), option('--effort', run_config.effort('vision', 'prestage_read')), '--agree-check' in sys.argv
FALLBACK = option('--fallback-effort', run_config.effort('vision', 'prestage_read_fallback'))
SUFFIX = option('--suffix', '')      # a second pre-stage beside the first: prestage<suffix>/, <paper>__clean<suffix>, clean<suffix>/
OUT = cl.HERE / ('prestage' + SUFFIX) / paper; (OUT / 'crops').mkdir(parents=True, exist_ok=True)
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['OPENAI_BASE_URL'], timeout=180, max_retries=0)
PROMPT = ('The image is a piece of a page from a chemistry paper. Copy the text printed in it, line by line, exactly: every digit, letter, Greek letter, '
          'sign and punctuation mark as printed. Write subscripts and superscripts as plain characters in reading order. Where a line is cut off by the '
          'edge of the image, copy the part that is visible. Do not correct, complete or explain anything. Answer with the text only.')
squeeze = lambda t: re.sub(r'\s+', '', cl.fold(t or '').replace('̃', ''))
# what the model writes back, read as text: its own bold marks and any TeX it falls into are not characters of the page
heard = lambda t: squeeze(''.join(ch for ch, _ in cl.parser_chars(re.sub(r'\*\*|__', '', t or ''))))
# Letters an image reading cannot tell apart from their Latin twins: compared through this fold, a reading is not counted against the page
# for writing v where the page has ν. When the fold makes the two readings the same, the image cannot decide between them.
LOOK = str.maketrans({'ν': 'v', 'α': 'a', 'ο': 'o', 'κ': 'k', 'ρ': 'p', 'χ': 'x', 'ι': 'i', 'µ': 'u', 'υ': 'u', 'τ': 't', 'Α': 'A', 'Β': 'B', 'Ε': 'E', 'Η': 'H', 'Ι': 'I', 'Κ': 'K', 'Μ': 'M', 'Ν': 'N', 'Ο': 'O', 'Ρ': 'P', 'Τ': 'T', 'Χ': 'X', 'Υ': 'Y', 'Ζ': 'Z'})
began = time.monotonic(); calls = []; lock = __import__('threading').Lock()


RENDERED = {}; render_lock = __import__('threading').Lock()


def render(page, rect, zoom, name):
    """The rectangle (points, top-left origin) of a page as an image; each page is drawn once per zoom."""
    with render_lock:
        if (page, zoom) not in RENDERED:
            doc = pdfium.PdfDocument(str(next((cl.FIX / paper / 'source').glob('*.pdf')))); RENDERED[(page, zoom)] = doc[page].render(scale=zoom).to_pil(); doc.close()
        image = RENDERED[(page, zoom)]
    box = (int(max(0, rect[0]) * zoom), int(max(0, rect[1]) * zoom), int(min(image.width / zoom, rect[2]) * zoom), int(min(image.height / zoom, rect[3]) * zoom))
    path = OUT / 'crops' / f'{name}.png'; image.crop(box).save(path); return path


def read(path, what):
    data = base64.b64encode(path.read_bytes()).decode(); last = None
    for attempt in range(1, waits.TRIES + 1):
        # Measured 2026-10-14: at low effort the gateway answers some images with no choices and no output tokens, again and again,
        # while the same image at medium is answered every time; after two such answers the image is sent at medium.
        effort = EFFORT if attempt <= 2 else FALLBACK; t = time.monotonic()
        try:
            r = client.chat.completions.create(model=MODEL, reasoning_effort=effort, messages=[{'role': 'user', 'content': [{'type': 'text', 'text': PROMPT}, {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + data}}]}])
            if not r.choices: raise RuntimeError('the gateway returned no answer')
            text = r.choices[0].message.content or ''; u = r.usage.model_dump(exclude_none=True) if r.usage else {}
            with lock: calls.append({'what': what, 'seconds': round(time.monotonic() - t, 1), 'input': u.get('prompt_tokens'), 'output': u.get('completion_tokens'), 'attempt': attempt, 'effort': effort, 'answered_as': r.model})
            return text
        except Exception as error:
            last = type(error).__name__ + ': ' + str(error)[:120]
            with lock: calls.append({'what': what, 'seconds': round(time.monotonic() - t, 1), 'error': last, 'attempt': attempt, 'effort': effort})
            time.sleep(waits.LONG[attempt - 1] if waits.passing(error) else 5 * attempt)            # empty answers come in bursts; a rate limit or a lost connection lasts minutes: wait longer each time
    return None


def page_rect(block, size):
    b = block['block_box']; return (b[0] * size[0] / 1000, b[1] * size[1] / 1000, b[2] * size[0] / 1000, b[3] * size[1] / 1000)


def settle(text, s, T, again):
    """Decide one place from the transcription T (and a second reading `again`, a callable, when the first has neither reading)."""
    left, right = squeeze(text[:s['start']]), squeeze(text[s['end']:]); here = squeeze(text[s['start']:s['end']])
    x, y = squeeze(s['parser']), squeeze(s['layer'] or '')
    other = x if here == y else y                      # the reading the text does not carry now
    if here.translate(LOOK) == other.translate(LOOK) and here != other:
        # the two readings differ only by a letter and its look-alike: an image cannot settle that, the character's own source does
        keep = y if s['kind'] in ('LAYER', 'DISPUTED') and y else here
        return 'LOOKALIKE_BY_RULE', (keep if keep != here else None), f'{x!r} or {y!r}: look-alikes, the text layer\'s character is kept'
    T = T.translate(LOOK); left, right = left.translate(LOOK), right.translate(LOOK); here_f, other_f = here.translate(LOOK), other.translate(LOOK)
    # The image speaks for a place only between two neighbours it shows: at a block's first or last characters the text may go on past
    # the crop (the paragraph runs into the next column or page), and an image that stops there is no evidence about what follows.
    if len(left) < 3 or len(right) < 3: return 'FLAGGED', None, 'the place is at the edge of the block; the image cannot show what lies past it'
    for n in (10, 7, 5, 3):
        L, R = left[-n:], right[:n]
        if len(L) < 3 or len(R) < 3: continue
        has_here, has_other = (L + here_f + R) in T, (L + other_f + R) in T
        if has_here and has_other: return 'UNCHANGED', None, f'{L}[{here}|{other}]{R}'
        if has_here: return ('VERIFIED_LAYER' if here == y and s['kind'] == 'LAYER' else 'VERIFIED_PARSER' if here == x else 'VERIFIED'), None, f'{L}[{here}]{R}'
        if has_other: return ('VERIFIED_PARSER' if other == x else 'VERIFIED_LAYER'), other, f'{L}[{other}]{R}'
    # neither reading: what does the image print between the same neighbours?
    def middle(T):
        for n in (8, 6, 4):
            L, R = left[-n:], right[:n]
            if len(L) >= 3 and len(R) >= 3 and T.count(L) == 1:
                a = T.find(L) + len(L); b = T.find(R, a)
                if b >= 0 and b - a <= max(12, 2 * len(here) + 4): return T[a:b]
        return None
    m = middle(T)
    if m is None: return 'FLAGGED', None, 'the place could not be found in the transcription'
    T2 = again()
    if T2 is None: return 'FLAGGED', None, f'image reads {m!r}; second reading failed'
    m2 = middle(heard(T2).translate(LOOK))
    if m2 == m:
        if m == other_f: return ('VERIFIED_PARSER' if other == x else 'VERIFIED_LAYER'), other, f'image, read twice, has {other!r} once look-alikes are set aside'
        if m == here_f: return 'UNCHANGED', None, f'image, read twice, has {here!r} once look-alikes are set aside'
        return 'IMAGE', m, f'both image readings: {m!r} (parser {x!r}, layer {y!r})'
    return 'FLAGGED', None, f'image readings differ: {m!r} / {m2!r} (parser {x!r}, layer {y!r})'


cleaned, rows = cl.clean_paper(paper); t_clean = round(time.monotonic() - began, 1)
doc = pdfium.PdfDocument(str(next((cl.FIX / paper / 'source').glob('*.pdf')))); sizes = {i: tuple(doc[i].get_size()) for i in range(len(doc))}; doc.close()
jobs = []                                   # (block id, [site indexes], page, rect, zoom)
decisions = collections.defaultdict(dict); crop_of = {}
for block_id, b in cleaned.items():
    if not b['sites']: continue
    if not b.get('block_box'): continue          # a caption the page was searched for in vain: no place to cut out; its places stay NOT_READ
    if b['sites'][0]['kind'] == 'PARSER':
        jobs.append((block_id, [0], b['page'], page_rect(b, sizes[b['page']]), 3.0)); continue
    groups = []
    rect = page_rect(b, sizes[b['page']])
    for k, s in enumerate(b['sites']):
        if not s['box'] or s['page'] is None: continue
        if squeeze(s['parser']) == '' and (s['page'] != b['page'] or s['box'][3] < rect[1] - 2 or s['box'][1] > rect[3] + 2):
            decisions[block_id][k] = ('PARSER_BY_RULE', '', 'characters the layer has outside the block (a page number or a running head), not read'); continue
        h = s['box'][3] - s['box'][1]; band = (s['box'][1] - 0.6 * h - 1, s['box'][3] + 0.6 * h + 1); page = s['page']
        g = next((g for g in groups if g['page'] == page and band[0] < g['band'][1] and band[1] > g['band'][0]), None)
        if g: g['sites'].append(k); g['band'] = (min(g['band'][0], band[0]), max(g['band'][1], band[1]))
        else: groups.append({'page': page, 'band': band, 'sites': [k]})
    for g in groups:
        x0, _, x1, _ = page_rect(b, sizes[g['page']]); jobs.append((block_id, g['sites'], g['page'], (x0 - 4, g['band'][0], x1 + 4, g['band'][1]), 4.5))


def run(job):
    block_id, idx, page, rect, zoom = job; b = cleaned[block_id]; name = f"{block_id}-{idx[0]}"
    for k in idx: crop_of[(block_id, k)] = name
    T = read(render(page, rect, zoom, name), 'place' if zoom > 3 else 'block read by the parser alone')
    if T is None:
        for k in idx: decisions[block_id][k] = ('NOT_READ', None, 'the image could not be read')
        return
    again = lambda: read(render(page, rect, zoom * 1.5, name + '-again'), 'second reading')
    if b['sites'][0]['kind'] == 'PARSER':
        # the whole block: the parser's text is checked against the image, difference by difference
        P, Ts = squeeze(b['text']).translate(LOOK), heard(T).translate(LOOK)
        if P == Ts: decisions[block_id][0] = ('VERIFIED_PARSER', None, 'the whole block'); return
        # the block's box may not hold the whole block (that is often why only the parser could read it): an image that holds less than
        # the parser's text speaks for nothing beyond itself, so the parser's text stays, marked
        import difflib
        if len(Ts) < 0.9 * len(P) or difflib.SequenceMatcher(None, P, Ts, autojunk=False).ratio() < 0.8:
            decisions[block_id][0] = ('FLAGGED', None, f'the image holds {len(Ts)} of the parser\'s {len(P)} characters; the box does not show the whole block'); return
        T2 = again(); Ts2 = heard(T2).translate(LOOK) if T2 else None
        decisions[block_id][0] = ('IMAGE', T, 'two image readings agree with each other, not with the parser') if Ts2 == Ts else ('FLAGGED', None, 'parser and image differ; the two image readings differ too' if T2 else 'second reading failed')
        return
    Ts = heard(T); cache = {}
    def again_once():
        if 'T2' not in cache: cache['T2'] = again()
        return cache['T2']
    for k in idx: decisions[block_id][k] = settle(b['text'], b['sites'][k], Ts, again_once)


t0 = time.monotonic()
with ThreadPoolExecutor(max_workers=PARALLEL) as pool: list(pool.map(run, jobs))
t_read = round(time.monotonic() - t0, 1)
# apply
final, outcome = {}, collections.Counter(); places = []
for block_id, b in cleaned.items():
    text = b['text']; d = decisions.get(block_id, {})
    if b['sites'] and b['sites'][0]['kind'] == 'PARSER':
        what, new, why = d.get(0, ('NOT_READ', None, 'no job'))
        if what == 'IMAGE': text = re.sub(r'[ \t]*\n[ \t]*', ' ', new).strip()
        outcome['whole block read by the parser alone: ' + what] += 1; places.append({'block': block_id, 'site': 0, 'kind': 'PARSER', 'decision': what, 'why': why, 'crop': crop_of.get((block_id, 0))})
    else:
        for k in sorted(d, key=lambda k: -b['sites'][k]['start']):
            what, new, why = d[k]; s = b['sites'][k]; outcome[s['kind'] + ' -> ' + what] += 1
            places.append({'block': block_id, 'site': k, 'kind': s['kind'], 'parser': s['parser'], 'layer': s['layer'], 'decision': what, 'why': why, 'crop': crop_of.get((block_id, k))})
            if new is not None and what in ('VERIFIED_PARSER', 'VERIFIED_LAYER', 'IMAGE', 'LOOKALIKE_BY_RULE', 'PARSER_BY_RULE'): text = text[:s['start']] + new + text[s['end']:]
        for k, s in enumerate(b['sites']):
            if k not in d: outcome[s['kind'] + ' -> NOT_READ (no box)'] += 1; places.append({'block': block_id, 'site': k, 'kind': s['kind'], 'decision': 'NOT_READ', 'why': 'no position on the page'})
    final[block_id] = {**b, 'text': text}
# agree check: whole blocks the two readings agree on, transcribed; a difference that two image readings repeat is only marked
checks = []
if AGREE:
    def check(block_id):
        b = cleaned[block_id]; page = b['page']; path = render(page, page_rect(b, sizes[page]), 3.0, block_id + '-agree'); T = read(path, 'agree check')
        if T is None: return {'block': block_id, 'result': 'NOT_READ'}
        mine = squeeze(final[block_id]['text']).translate(LOOK)
        if len(heard(T)) < 0.6 * len(mine): return {'block': block_id, 'result': 'NOT_READ', 'why': 'the reading is much shorter than the block'}
        if heard(T).translate(LOOK) == mine: return {'block': block_id, 'result': 'THREE_WAY'}
        T2 = read(render(page, page_rect(b, sizes[page]), 4.5, block_id + '-agree-again'), 'agree check, second reading')
        if T2 is None: return {'block': block_id, 'result': 'NOT_READ'}
        import difflib
        ops = lambda t: {(x, y) for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, mine, heard(t).translate(LOOK), autojunk=False).get_opcodes() if tag != 'equal' for x, y in [(mine[i1:i2], heard(t).translate(LOOK)[j1:j2])] if set(x + y) != {'-'}}
        same = ops(T) & ops(T2)
        if not ops(T): return {'block': block_id, 'result': 'THREE_WAY', 'why': 'only hyphens at line ends differ'}
        # A letter o right after a digit that both image readings show as a degree sign: the author typed the degree sign as a letter, and
        # both readings of the text kept the letter. This one pattern is corrected; every other difference is only marked (2026-10-14).
        degree = []
        for t in (T, T2):
            got = heard(t).translate(LOOK); idx = [k for k, ch in enumerate(final[block_id]['text']) if not ch.isspace()]
            found = set()
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, mine, got, autojunk=False).get_opcodes():
                if tag == 'replace' and mine[i1:i2] == 'o' and got[j1:j2] == '°' and i1 > 0 and mine[i1 - 1].isdigit(): found.add(idx[i1])
            degree.append(found)
        fixed = sorted(degree[0] & degree[1])
        return {'block': block_id, 'result': 'CHECK' if same else 'IMAGE_UNSTABLE', 'differences repeated by both image readings': sorted(same)[:20], 'degree signs typed as o': fixed}
    # The check changes a text only where a letter o stands after a digit as a degree sign would ("25 oC", "-78 o"); a block without
    # such a place is not read again, since nothing in it could be corrected (2026-10-16: reading every block cost about one reading per
    # block for the same corrections).
    DEGREE_LIKE = re.compile(r'\d[ \t]?o(?:[ \t]?[CF]\b|(?![A-Za-z]))')
    targets = [k for k, b in cleaned.items() if len(final[k]['text']) >= 20 and b.get('block_box') and DEGREE_LIKE.search(final[k]['text'])]; t1 = time.monotonic()
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool: checks = list(pool.map(check, targets))
    t_agree = round(time.monotonic() - t1, 1)
    for chk in checks:
        for at in reversed(chk.get('degree signs typed as o') or []):
            text = final[chk['block']]['text']; final[chk['block']] = {**final[chk['block']], 'text': text[:at] + '°' + text[at + 1:]}
else: t_agree = None
dst = cl.write_clean_package(paper, final, rows, suffix=SUFFIX)
ok = [c for c in calls if not c.get('error')]
report = {'paper': paper, 'model': MODEL, 'effort': EFFORT, 'parallel': PARALLEL, 'seconds': {'merge': t_clean, 'reading the places': t_read, 'agree check': t_agree, 'total': round(time.monotonic() - began, 1)},
          'jobs': len(jobs), 'calls': len(calls), 'failed calls': len(calls) - len(ok), 'tokens': {'input': sum(c.get('input') or 0 for c in ok), 'output': sum(c.get('output') or 0 for c in ok)},
          'places': dict(outcome), 'agree check': dict(collections.Counter(c['result'] for c in checks)) if checks else None}
(OUT / 'report.json').write_text(json.dumps({**report, 'decisions': places, 'checks': checks, 'calls': calls}, ensure_ascii=False, indent=1), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False)); print('written', dst)
