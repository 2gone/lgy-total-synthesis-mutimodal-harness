"""The paper-level record of one paper, from one task that sees the whole entry package: target, compounds, route, the totals the authors
state, screening and failed attempts, the points of the narrative. Runs beside the entry tasks. Credentials from the environment only.
usage: paper_task.py <package.md> <out.json> [--effort medium|high]"""
import json, os, sys, threading, time
from pathlib import Path
from openai import OpenAI
sys.path.insert(0, str(Path(__file__).resolve().parent)); import run_config, waits      # models and thinking levels: tools/run_config.json
package, out = Path(sys.argv[1]).read_text(), Path(sys.argv[2])
effort = sys.argv[sys.argv.index('--effort') + 1] if '--effort' in sys.argv else run_config.effort('workflow', 'paper_task')
MODEL = run_config.model('workflow')
SILENCE = float(os.environ.get('RECORD_SILENCE', '420'))      # see record_actions.streamed: keep-alives reset the read timeout
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['OPENAI_BASE_URL'], timeout=600, max_retries=0)
obj = lambda props: {'type': 'object', 'additionalProperties': False, 'required': list(props), 'properties': props}
strings = {'type': 'array', 'items': {'type': 'string'}}; numbers = {'type': 'array', 'items': {'type': 'integer'}}
SCHEMA = obj({
    'target': {'type': 'string'},
    'compounds': {'type': 'array', 'items': obj({'label': {'type': 'string'}, 'name': {'type': ['string', 'null']},
                                                 'role': {'type': 'string', 'enum': ['TARGET', 'INTERMEDIATE', 'STARTING_MATERIAL', 'SIDE_PRODUCT', 'NOT_ISOLATED', 'REFERENCE_COMPOUND', 'OTHER']},
                                                 'made_in_entries': numbers, 'printed_in': strings})},
    'route': {'type': 'array', 'items': obj({'entries': numbers, 'from': strings, 'to': strings, 'on_mainline': {'type': 'boolean'}, 'main_text_step': {'type': ['string', 'null']}})},
    'stated_step_count': {'type': ['string', 'null']}, 'stated_overall_yield': {'type': ['string', 'null']},
    'screening_and_failed_attempts': {'type': 'array', 'items': obj({'belongs_to_entries': numbers, 'where_printed': {'type': 'string'}, 'what': {'type': 'string'}})},
    'narrative': {'type': 'array', 'items': obj({'point': {'type': 'string'}, 'where_printed': {'type': 'string'}})},
    'unresolved': strings})
SYSTEM = """You are given one total-synthesis paper laid out in full: the main text with its figures as transcripts (a figure reader copied what is printed; you see no image), the SI general methods, every SI experiment entry, and a catalog of what is held elsewhere. Other tasks write down each experiment entry in detail. Your work is what only someone who sees the whole paper can write: the paper-level record.

- target: the compound the synthesis ends in, by the label and name the authors print.
- compounds: every compound the paper gives a label to, with the label exactly as printed, its name when one is printed, what it is in this paper, the SI entries that make it, and where its label is printed (block ids). A compound that appears only in a figure transcript still counts. Do not describe structures.
- route: one item per SI experiment entry that is a synthesis experiment, in the order of the synthesis: its substrates and products as the SI entry prints them, whether it lies on the main line to the target, and the step of the main text or scheme it corresponds to (quote the arrow or sentence) when there is one. When the main text shows a different substrate or grouping than the SI, the route follows the SI entry; quote the main text under main_text_step so that the difference can be seen.
- stated_step_count, stated_overall_yield: only what the authors state, in their words. Do not count or multiply.
- screening_and_failed_attempts: every condition table, optimisation, failed route or abandoned approach the paper reports, with the entries it belongs to and where it is printed.
- narrative: the points the authors themselves make about strategy, key steps, mechanism and comparison with the natural product, each with where it is printed. Their claims, not your assessment.

Write only what is printed. Where the paper leaves something open (a label used for two things, an entry that fits no step), say so under unresolved; do not guess."""
def ask(text):
    """(record or None, usage, finish reason, error) of one call on `text`."""
    last, record, usage, finish = None, None, {}, None
    for attempt in range(waits.TRIES):
        try:
            content, usage, finish = '', {}, None
            stream = client.chat.completions.create(model=MODEL, reasoning_effort=effort, stream=True, stream_options={'include_usage': True},
                    messages=[{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': text}], response_format={'type': 'json_schema', 'json_schema': {'name': 'paper', 'strict': True, 'schema': SCHEMA}})
            heard, done, stalled = [time.monotonic()], threading.Event(), []
            def watch():
                while not done.wait(5):
                    if time.monotonic() - heard[0] > SILENCE: stalled.append(True); stream.close(); return
            threading.Thread(target=watch, daemon=True).start()
            try:
                for chunk in stream:
                    heard[0] = time.monotonic(); usage.setdefault('answered_as', getattr(chunk, 'model', None))
                    if chunk.usage: usage = {**chunk.usage.model_dump(exclude_none=True), 'answered_as': usage.get('answered_as')}
                    for choice in chunk.choices: content += choice.delta.content or ''; finish = choice.finish_reason or finish
            except Exception:
                if not stalled: raise
            finally: done.set()
            if stalled: raise TimeoutError(f'no chunk from the model for {SILENCE:.0f} s; the stream was closed')
            if finish in ('length', 'max_tokens'): return None, usage, 'length', 'the answer reached the output limit'      # the same input would end the same way (the gateway says either)
            return json.loads(content), usage, finish, None
        except Exception as error:
            last = type(error).__name__ + ': ' + str(error)[:160]; time.sleep(waits.pause(error, attempt))      # a rate limit lasts minutes
    return None, usage, finish, last


def parts_of(text, n):
    """The package in n parts: every part has the main text, the general methods and the catalog, and one n-th of the experiment entries
    (whole entries, in order, under their own numbers)."""
    at = text.index('\n## 3. '); head, rest = text[:at], text[at:]
    tail_at = rest.find('\n## 4. '); body, tail = (rest, '') if tail_at < 0 else (rest[:tail_at], rest[tail_at:])
    title, *entries = body.split('\n### Entry ')
    entries = ['\n### Entry ' + e for e in entries]; size = -(-len(entries) // n)
    return [head + title + ''.join(entries[k:k + size]) + tail for k in range(0, len(entries), size)]


def merged(records):
    """One paper-level record from the records of the parts: compounds joined by their label, the route and the per-entry lists in the
    parts' order, the paper-wide fields from the first part that states them, repeated items once."""
    first = lambda key: next((r[key] for r in records if r.get(key)), None)
    compounds = {}
    for r in records:
        for c in r.get('compounds') or []:
            had = compounds.setdefault(c['label'], {**c, 'made_in_entries': [], 'printed_in': []})
            had['name'] = had.get('name') or c.get('name')
            if c.get('role') == 'TARGET': had['role'] = 'TARGET'
            had['made_in_entries'] = sorted(set(had['made_in_entries']) | set(c.get('made_in_entries') or []))
            had['printed_in'] = list(dict.fromkeys(had['printed_in'] + list(c.get('printed_in') or [])))
    once = lambda key: list({json.dumps(x, sort_keys=True, ensure_ascii=False): x for r in records for x in r.get(key) or []}.values())
    return {'target': first('target') or '', 'compounds': list(compounds.values()), 'route': [x for r in records for x in r.get('route') or []],
            'stated_step_count': first('stated_step_count'), 'stated_overall_yield': first('stated_overall_yield'),
            'screening_and_failed_attempts': once('screening_and_failed_attempts'), 'narrative': once('narrative'), 'unresolved': once('unresolved')}


# A paper whose record does not fit in one answer (its answer reaches the output limit) is asked in parts, each with the whole main text
# and part of the experiment entries, twice as many parts each time up to eight; the host joins the parts' records.
began = time.monotonic(); split = int(os.environ.get('PAPER_PARTS', '1'))       # PAPER_PARTS: start in that many parts (a test of the joining)
if split == 1: record, usage, finish, last = ask(package); calls = [{'parts': 1, 'output': usage.get('completion_tokens'), 'finish': finish}]
else: record, usage, finish, last, split, calls = None, {}, 'length', None, split // 2, []
while record is None and finish == 'length' and split < 8 and '\n### Entry ' in package:
    split *= 2; got = [ask(part) for part in parts_of(package, split)]
    calls.append({'parts': len(got), 'output': [g[1].get('completion_tokens') for g in got], 'finish': [g[2] for g in got]})
    usage = {'prompt_tokens': sum(g[1].get('prompt_tokens') or 0 for g in got), 'completion_tokens': sum(g[1].get('completion_tokens') or 0 for g in got), 'answered_as': got[0][1].get('answered_as')}
    if all(g[0] is not None for g in got): record, last = merged([g[0] for g in got]), None
    else: finish = 'length' if any(g[2] in ('length', 'max_tokens') for g in got) else None; last = next(g[3] for g in got if g[0] is None)
seconds = round(time.monotonic() - began, 1)
out.write_text(json.dumps({'record': record, 'seconds': seconds, 'input': usage.get('prompt_tokens'), 'output': usage.get('completion_tokens'), 'answered_as': usage.get('answered_as'), 'parts': split, 'calls': calls, 'error': None if record else last}, ensure_ascii=False, indent=1))
print('paper task', seconds, 's', 'in', usage.get('prompt_tokens'), 'out', usage.get('completion_tokens'), 'compounds', len(record['compounds']) if record else None, 'route', len(record['route']) if record else None, last if not record else '')
