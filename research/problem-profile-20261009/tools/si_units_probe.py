"""Only the reading-units step, part by part, on one supplement. Test-side loop over the lane's own
functions (build_reading_units_part, accept_reading_units_part, resolve_reading_units_answer).
Credentials from the process environment only."""
import json, os, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src-units'))
sys.path.insert(0, str(Path(__file__).resolve().parent)); import run_config, waits      # models and thinking levels: tools/run_config.json
import host_repairs as hr
import tsdaf.adapters.gemini.interactions as transport
from tsdaf.adapters.gemini.interactions import GeminiInteractions
from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
from tsdaf.lanes.document.reading_units import (ReadingUnitsAnswer, accept_reading_units_part, build_reading_units_part,
                                                build_reading_units_request, resolve_reading_units_answer)
from tsdaf.contracts.reading_units import ReadingUnits

paper, out = sys.argv[1], Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
FIX = ROOT.parents[2] / 'tests/fixtures/si_packages'
# SI_TEXT=__clean: the copy of the SI package whose text the pre-stage settled; everything else stays keyed by the paper
package = canonicalize_paper_package(FIX / (paper + os.environ.get('SI_TEXT', '')))
CONTRADICTIONS = [
    (", copies of spectra, and a screening table dumped as text.", ", and copies of spectra. A table of conditions screened for a reaction is not OTHER; it is PROCEDURE."),
    ("PROCEDURE is one compound's preparation.", "PROCEDURE is how something was made or tried: one compound's preparation, the conditions screened for a reaction, a protocol that prepares no labelled compound."),
    ("CHARACTERIZATION is one compound's data.", "CHARACTERIZATION is one compound's data, including a comparison of the natural and the synthetic sample of it."),
    ("copy the compound's label as the author prints it in the unit's title or first line -- 13, 14a, (±)-1, SI-6 -- and nothing else: not the name, not a label from the discussion.",
     "copy the label of the compound the unit is about, as the author prints it -- 13, 14a, (±)-1, SI-6 -- from the unit's own rows: its title or first line, the labels read from the figure row that opens it, or the procedure's own text saying what it gives. Copy the label and nothing else: not the name, and not a label taken from outside the unit."),
]
build = transport._chat_request
WINDOW_EFFORT = None
def wire(request, *, stream=False):
    w = build(request, stream=stream); w['model'] = run_config.model('workflow')
    if os.environ.get('PROBE_EFFORT'): w['reasoning_effort'] = os.environ['PROBE_EFFORT']
    if WINDOW_EFFORT: w['reasoning_effort'] = WINDOW_EFFORT      # this one part only, after it stalled twice as asked
    if os.environ.get('PROBE_HATCH'):   # a way out of a case no rule decides: say what was undecided instead of deliberating
        w['messages'] = [({**m, 'content': m['content'] + '\n\n' + (ROOT / 'si_hatch.txt').read_text()} if m['role'] == 'system' else m) for m in w['messages']]
        fmt = json.loads(json.dumps(w.get('response_format')))
        def add(node):
            if isinstance(node, dict):
                if 'first_block_id' in (node.get('properties') or {}):
                    node['properties']['open_question'] = {'anyOf': [{'type': 'string'}, {'type': 'null'}], 'description': 'What the rules left undecided for this unit, in one sentence; null when they decide it.'}
                    if 'required' in node: node['required'] = [*node['required'], 'open_question']
                for v in node.values(): add(v)
            elif isinstance(node, list):
                for v in node: add(v)
        add(fmt); w['response_format'] = fmt
    if os.environ.get('PROBE_PRINCIPLES'):   # principles that decide what the named rules do not (appended after them, below)
        w['_principles'] = True
    if os.environ.get('PROBE_RULES'):   # the open cases of the definition, decided (tools/si_rules_v2.txt)
        w['messages'] = [({**m, 'content': m['content'] + '\n\n' + (ROOT / 'si_rules_v2.txt').read_text()} if m['role'] == 'system' else m) for m in w['messages']]
    if w.pop('_principles', None):
        w['messages'] = [({**m, 'content': m['content'] + '\n\n' + (ROOT / 'si_principles.txt').read_text()} if m['role'] == 'system' else m) for m in w['messages']]
        if os.environ.get('PROBE_CONSISTENT'):   # the older wording that contradicted the principles, brought into line with them
            text = json.dumps(w, ensure_ascii=False); before = text
            for old, new in CONTRADICTIONS:
                assert old in text, old[:60]
                text = text.replace(old, new)
            w = json.loads(text)
    return w
transport._chat_request = wire
# PROBE_SAFE: a call the provider cut at the output ceiling was too large and is asked again at half the rows; any other failure is sent again
# unchanged, up to three times, and logged with its reason.
SAFE = os.environ.get('PROBE_SAFE')
# How long a stream may stay silent before it is given up. Traced on 2026-10-13 (48 calls): a healthy stream paused 89 s at most, and the
# seven that failed had simply stopped sending while the model was thinking -- under the old 900 s each of them cost fifteen minutes.
# 2026-10-14: the gateway now sends a keep-alive every 15 s, so a quiet stream is no longer cut at about 120 s and a real upstream error arrives
# as an error. A healthy call was then seen silent for 131 s before its answer; the limit leaves room for that. Keep-alive lines are not data
# and do not reset this clock.
SILENCE = float(os.environ.get('PROBE_SILENCE', '420' if SAFE else '900'))
# PROBE_FALLBACK=medium: a part whose stream stalled twice at the asked thinking level is asked once more at this level, that part only.
FALLBACK = os.environ.get('PROBE_FALLBACK')
# TSDAF_NO_STREAM=1 (2026-10-21): answered whole, not streamed -- lpzproxy gives Gemini's thinking text only in a whole answer (see
# record_actions.whole); with no keep-alives the read timeout holds, NO_STREAM_TIMEOUT seconds
NO_STREAM = os.environ.get('TSDAF_NO_STREAM') == '1'
client = GeminiInteractions.from_api_key(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['OPENAI_BASE_URL'], stream=not NO_STREAM,
                                         timeout=float(os.environ.get('NO_STREAM_TIMEOUT', '900')) if NO_STREAM else SILENCE, **({'max_transport_attempts': 1} if SAFE else {}))
import tsdaf.lanes.document.reading_units as lane
# PROBE_TRACE: when each piece of a streamed answer arrived, and what it carried. Tells a connection closed for being silent from one closed
# for having run too long: the first shows a long gap before the end, the second ends while pieces are still arriving.
TRACE = []
if os.environ.get('PROBE_TRACE'):
    import collections
    _assemble = transport._assembled_chat_completion
    def _traced(stream, **kw):
        rec = {'chunks': 0, 'first': None, 'last': None, 'max_gap': 0.0, 'max_gap_at': None, 'kinds': collections.Counter(), 'by_30s': collections.defaultdict(collections.Counter), 'error': None}
        began = time.monotonic()
        def pieces():
            last = began
            try:
                for chunk in stream:
                    now = time.monotonic(); at = now - began; rec['chunks'] += 1
                    if rec['first'] is None: rec['first'] = round(at, 1)
                    if now - last > rec['max_gap']: rec['max_gap'], rec['max_gap_at'] = round(now - last, 1), round(at, 1)
                    last = now; rec['last'] = round(at, 1)
                    try:
                        for choice in chunk.choices or []:
                            delta = choice.delta.model_dump(exclude_none=True) if choice.delta is not None else {}
                            for key, value in delta.items():
                                if value not in ('', [], {}, None) and key != 'role': rec['kinds'][key] += 1; rec['by_30s'][int(at // 30) * 30][key] += 1
                        if getattr(chunk, 'usage', None): rec['kinds']['usage'] += 1
                    except Exception as error: rec['kinds']['unreadable:' + type(error).__name__] += 1
                    yield chunk
            except BaseException as error:
                rec['error'] = type(error).__name__ + ': ' + str(error)[:200]; raise
            finally:
                rec['ended'] = round(time.monotonic() - began, 1); rec['silent_before_end'] = round(time.monotonic() - last, 1)
                TRACE.append({**rec, 'kinds': dict(rec['kinds']), 'by_30s': {k: dict(v) for k, v in sorted(rec['by_30s'].items())}}); print(paper, 'TRACE', TRACE[-1], flush=True)
        return _assemble(pieces(), **kw)
    transport._assembled_chat_completion = _traced
if os.environ.get('PROBE_HEAD'):
    # Outline pass: each row shows only how it begins. Deciding where units begin rarely needs the peaks of a spectrum.
    _n, _full = int(os.environ['PROBE_HEAD']), lane._collapsed
    lane._collapsed = lambda text: (lambda c: c if len(c) <= _n else c[:_n] + f' ...[{len(c) - _n} more characters of this block not shown]')(_full(text))
if os.environ.get('PROBE_SINGLE'): lane.PART_CHARS = lane.PART_ROWS = 10**9
if os.environ.get('PROBE_ROWS'): lane.PART_ROWS = int(os.environ['PROBE_ROWS'])
# The lane refuses a whole document past 400,000 characters, the limit of reading it in one call. Here it is read window after window
# (PART_ROWS, PART_CHARS), each well under that, so the whole document may be of any length: a joined experimental document of
# several SI files and the main text can hold close to two million characters.
lane.MAX_REQUEST_CHARS = 10**12
if os.environ.get('PROBE_LONG'): lane.PART_LONG_ROWS = int(os.environ['PROBE_LONG'])
FIGURES = os.environ.get('PROBE_FIGURES')          # 'labels': what the OCSR branch read in each figure, as words; 'images': the figures themselves
figure_images = []
if FIGURES:
    import base64
    read = {r['block_id']: r for r in json.loads((ROOT.parent / 'figure-labels' / f'{paper}.labels.json').read_text())}
    figure_index = json.loads((ROOT.parent / 'figure-labels' / f'{paper}.blocks.json').read_text())
    files = {r['block_id']: r['file'] for r in figure_index if r['file']}
    transcripts, transcript_rows = {}, []
    if FIGURES == 'transcripts':
        for name in ['transcript-medium.json', 'transcript-fill.json']:
            path = ROOT.parent / 'figure-transcript' / name
            if path.exists():
                transcript_rows.extend(json.loads(path.read_text()))
        transcripts = hr.figure_transcripts(paper, figure_index, transcript_rows)
    def figure_row(block_id):
        if FIGURES == 'transcripts' and block_id in transcripts:
            return hr.figure_text(block_id, transcripts[block_id])
        labels = [m['printed_label'] or '(no label printed)' for m in read.get(block_id, {}).get('molecules', [])]
        if FIGURES in ('labels', 'transcripts'):
            return (f"[FIGURE, not text. A drawing with {len(labels)} chemical structure(s). The compound labels printed beside them, read from the figure "
                    f"left to right and top to bottom: {', '.join(labels)}. In a reaction drawing the product is usually the last.]" + (' [No recorded transcript available; conditions not supplied.]' if FIGURES == 'transcripts' else ''))
        return f"[FIGURE, not text: shown below as the image named {block_id}.]"
    package = package.model_copy(update={'blocks': tuple(b.model_copy(update={'raw_text': ((b.raw_text + '\n') if b.raw_text.strip() else '') + figure_row(b.block_id)}) if b.block_id in (set(read) | set(transcripts)) and (FIGURES == 'transcripts' or not b.raw_text.strip()) else b for b in package.blocks)})
    if FIGURES == 'images':
        for block_id in read:
            data = Path(files[block_id]).read_bytes()
            figure_images += [{'type': 'text', 'text': f'The image named {block_id}:'}, {'type': 'image', 'mime_type': 'image/jpeg', 'resolution': 'high', 'data': base64.b64encode(data).decode()}]
if os.environ.get('PROBE_LANE'): lane.READING_UNITS_FORM = os.environ['PROBE_LANE']     # the stage's own second form: its prompt, its definitions
prepared = build_reading_units_request(package, source_phase='SI')
units, rationale, start, scale, log = [], None, 0, 1, []
MEMORY, ADAPT = os.environ.get('PROBE_MEMORY'), os.environ.get('PROBE_ADAPT')
rows_limit = 40 if ADAPT else None       # sized from what the last part cost: about 22,000 thought tokens a part
# The whole flow arranged by the Host: one survey of the outline (sections + layout), then
# windows planned from the sections, each divided with the layout and the units so far.
FLOW, layout, plan = os.environ.get('PROBE_FLOW'), None, None
SIZE = {'PROCEDURES': 40, 'GENERAL_METHODS': 60}
if FLOW:
    all_rows = json.loads(prepared.call_kwargs['input'][0]['content'][0]['text'])['blocks']
    outline = [{'block_id': r['block_id'], 'block_type': r['block_type'], 'page_index': r['page_index'], 'characters': len(r['text']),
                'begins': r['text'] if r['text'].startswith('[FIGURE') else r['text'][:90]} for r in all_rows]
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['sections', 'layout'], 'properties': {
        'layout': {'type': 'string'},
        'sections': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'required': ['first_block_id', 'last_block_id', 'section'], 'properties': {
            'first_block_id': {'type': 'string'}, 'last_block_id': {'type': 'string'},
            'section': {'type': 'string', 'enum': ['FRONT', 'GENERAL_METHODS', 'PROCEDURES', 'SPECTRA', 'CRYSTALLOGRAPHY', 'REFERENCES', 'OTHER']}}}}}}
    t = time.monotonic()
    sp = client.prepare(input=[{'type': 'user_input', 'content': [{'type': 'text', 'text': json.dumps({'source_phase': 'SI', 'rows': outline}, ensure_ascii=False)}]}],
                        system_instruction=(ROOT / 'si_survey_prompt.txt').read_text(), response_format={'type': 'text', 'mime_type': 'application/json', 'schema': schema})
    si = client.execute_prepared(sp); survey = json.loads(si.output_text)
    at = {r['block_id']: n for n, r in enumerate(all_rows)}
    plan = ['OTHER'] * len(all_rows)
    for sec in survey['sections']:
        if sec['first_block_id'] in at and sec['last_block_id'] in at:
            for n in range(at[sec['first_block_id']], at[sec['last_block_id']] + 1): plan[n] = sec['section']
    layout = survey['layout']
    log.append({'survey': True, 'seconds': round(time.monotonic() - t, 1), 'thought': si.total_thought_tokens, 'out': si.total_output_tokens, 'in': si.total_input_tokens, 'attempts': si.transport_attempts,
                'outcome': 'sections ' + ' '.join(f"{s['section']}:{at.get(s['last_block_id'], -1) - at.get(s['first_block_id'], 0) + 1}" for s in survey['sections'])})
    print(paper, log[-1], flush=True); print(paper, 'LAYOUT', layout, flush=True)
    (out / f'{paper}.survey.json').write_text(json.dumps(survey, ensure_ascii=False, indent=1))
def planned(start):
    """How many rows the window at `start` shows: to the end of its section, at most that section's size; a short tail goes with the next section."""
    def run(n):
        e = n
        while e < len(plan) and plan[e] == plan[n]: e += 1
        return e - n
    size, rest = SIZE.get(plan[start], 120), run(start)
    if rest > size // 3 or start + rest >= len(plan): return min(size, rest)
    return rest + min(SIZE.get(plan[start + rest], 120), run(start + rest))
began = time.monotonic(); unchanged_tries = rate_tries = 0
while True:
    if FLOW: rows_limit = planned(start)
    kwargs, shown, final = build_reading_units_part(prepared, start, scale=scale, rows_limit=rows_limit, before=units if MEMORY else None, layout=layout)
    if figure_images:
        kwargs = {**kwargs, 'input': [{**kwargs['input'][0], 'content': [*kwargs['input'][0]['content'], *figure_images]}]}
    history, accepted = list(kwargs['input']), None
    for turn in (1, 2):
        t = time.monotonic(); p = client.prepare(**{**kwargs, 'input': history})
        try: i = client.execute_prepared(p)
        except transport.GeminiAdapterError as error:
            if ADAPT and error.failure.code.value == 'PROVIDER_OUTPUT_TRUNCATED' and len(shown) > 8:
                log.append({'start': start, 'rows': len(shown), 'outcome': 'TRUNCATED, halved', 'seconds': round(time.monotonic() - t, 1)}); print(paper, log[-1], flush=True)
                rows_limit = max(8, len(shown) // 2); accepted = 'again'; break
            if SAFE:
                spent, code = round(time.monotonic() - t, 1), error.failure.code.value
                audit = getattr(error, 'attempt_audit_bytes', None) or getattr(error.failure, 'attempt_audit_bytes', None)
                try: last = json.loads(audit)['attempts'][-1] if audit else {}
                except Exception: last = {}
                said = {k: last.get(k) for k in ('phase', 'reason', 'http_status')} if last else str(getattr(error.failure, 'message', ''))[:200]
                # Only the output ceiling says a part was too large. A stream cut after minutes is not a size signal: on 2026-10-13 parts halved
                # after such a cut took as much thought as before (21 rows, 51K) and the division got worse, so those are sent again unchanged.
                too_large = code == 'PROVIDER_OUTPUT_TRUNCATED'
                if too_large and len(shown) > 8:
                    log.append({'start': start, 'rows': len(shown), 'outcome': f'{code} after {spent}s, halved', 'seconds': spent, 'said': said}); print(paper, log[-1], flush=True)
                    rows_limit = max(8, len(shown) // 2); accepted = 'again'; break
                # The gateway's rate limit lasts minutes, not seconds (2026-10-15: every window of one quarter hour refused), and so does a
                # lost connection of the node (2026-10-16: the calls of several minutes failed one after another): both are waited out
                # with growing pauses and do not use up the resends kept for other failures.
                if code in ('PROVIDER_RATE_LIMIT', 'PROVIDER_TRANSIENT') and rate_tries < waits.TRIES:
                    rate_tries += 1; pause = waits.LONG[rate_tries - 1]
                    log.append({'start': start, 'rows': len(shown), 'outcome': f'{code} after {spent}s, sent again after {pause}s', 'seconds': spent, 'said': said}); print(paper, log[-1], flush=True)
                    time.sleep(pause); accepted = 'again'; break
                unchanged_tries += 1
                if unchanged_tries <= 3:
                    lower = FALLBACK and unchanged_tries >= 2 and not WINDOW_EFFORT
                    if lower: WINDOW_EFFORT = FALLBACK
                    log.append({'start': start, 'rows': len(shown), 'outcome': f'{code} after {spent}s, sent again' + (f' at {FALLBACK}' if lower else ''), 'seconds': spent, 'said': said}); print(paper, log[-1], flush=True)
                    time.sleep(10 * unchanged_tries); accepted = 'again'; break
            raise
        row = {'start': start, 'rows': len(shown), 'scale': scale, 'final': final, 'turn': turn, **({'effort': WINDOW_EFFORT} if WINDOW_EFFORT else {}), 'seconds': round(time.monotonic() - t, 1),
               'thought': i.total_thought_tokens, 'out': i.total_output_tokens, 'in': i.total_input_tokens, 'attempts': i.transport_attempts}
        try:
            raw = json.loads(i.output_text or '')
            asked = [(u.get('first_block_id'), u.pop('open_question', None)) for u in raw.get('units', [])]
            row['open_questions'] = [f'{b}: {q}' for b, q in asked if q]
            answer = ReadingUnitsAnswer.model_validate_json(json.dumps(raw)); outcome = accept_reading_units_part(shown, answer, final=final)
        except Exception as error:
            outcome = 'shape: ' + type(error).__name__
        row['outcome'] = outcome if isinstance(outcome, str) else ('WIDEN' if outcome is None else f'{len(outcome[0])} units, settles {outcome[1]} rows')
        log.append(row); print(paper, row, flush=True)
        if not isinstance(outcome, str):
            accepted = outcome; unchanged_tries = rate_tries = 0      # the resends are counted per window, not over the whole document
            if outcome is not None and rationale is None: rationale = answer.rationale
            break
        if turn == 2: raise SystemExit(f'{paper}: refused twice: {outcome}')
        history += [dict(s) for s in json.loads(i.response_bytes).get('steps', [])] + [{'type': 'user_input', 'content': [{'type': 'text', 'text': outcome}]}]
    if accepted == 'again': continue
    if accepted is None: scale *= 2; continue
    if ADAPT and log[-1].get('thought'):
        rows_limit = max(12, min(160, 2 * len(shown), round(len(shown) * 22000 / log[-1]['thought'])))
    units += accepted[0]
    if SAFE: rows_limit, unchanged_tries, WINDOW_EFFORT = None, 0, None
    if final: break
    start, scale = start + accepted[1], 1
resolved = resolve_reading_units_answer(package, source_phase='SI', answer=ReadingUnitsAnswer(units=units, rationale=rationale), request_sha256=prepared.request_sha256,
                                        model_profile=f"{run_config.model('workflow')}-chat-completions-{os.environ.get('PROBE_EFFORT') or 'default'}", resources=prepared.resources, not_shown_block_ids=prepared.not_shown_block_ids)
assert isinstance(resolved, ReadingUnits), resolved
(out / f'{paper}.units.json').write_text(json.dumps(resolved.model_dump(mode='json'), ensure_ascii=False))
(out / f'{paper}.log.json').write_text(json.dumps({'seconds': round(time.monotonic() - began, 1), 'calls': log, 'trace': TRACE}, ensure_ascii=False, indent=1))
print(paper, 'DONE', round(time.monotonic() - began), 's', len(log), 'calls', len(resolved.units), 'units', 'uncovered', len(resolved.uncovered_block_ids), flush=True)
