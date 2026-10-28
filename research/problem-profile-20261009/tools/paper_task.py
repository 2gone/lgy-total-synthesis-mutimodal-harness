"""The paper-level record of one paper, from one task that sees the whole entry package: compounds (the targets among them), route, the totals the authors
state, screening and failed attempts, the points of the narrative. Runs beside the entry tasks. Credentials from the environment only.
usage: paper_task.py <package.md> <out.json> [--effort medium|high]"""
import json, os, re, sys, threading, time
from pathlib import Path
from openai import OpenAI
sys.path.insert(0, str(Path(__file__).resolve().parent)); import run_config, waits      # models and thinking levels: tools/run_config.json
package, out = Path(sys.argv[1]).read_text(), Path(sys.argv[2])
effort = sys.argv[sys.argv.index('--effort') + 1] if '--effort' in sys.argv else run_config.effort('workflow', 'paper_task')
MODEL = run_config.model('workflow')
SILENCE = float(os.environ.get('RECORD_SILENCE', '420'))      # see record_actions.streamed: keep-alives reset the read timeout
NO_STREAM = os.environ.get('TSDAF_NO_STREAM') == '1'; NO_STREAM_TIMEOUT = float(os.environ.get('NO_STREAM_TIMEOUT', '900'))      # see record_actions.whole
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['OPENAI_BASE_URL'], timeout=600, max_retries=0)
obj = lambda props: {'type': 'object', 'additionalProperties': False, 'required': list(props), 'properties': props}
strings = {'type': 'array', 'items': {'type': 'string'}}; numbers = {'type': 'array', 'items': {'type': 'integer'}}
SCHEMA = obj({
    'compounds': {'type': 'array', 'items': obj({'label': {'type': 'string'}, 'name': {'type': ['string', 'null']},
                                                 'role': {'type': 'string', 'enum': ['TARGET', 'INTERMEDIATE', 'STARTING_MATERIAL', 'SIDE_PRODUCT', 'NOT_ISOLATED', 'REFERENCE_COMPOUND', 'OTHER']},
                                                 'made_in_entries': numbers, 'printed_in': strings})},
    'route': {'type': 'array', 'items': obj({'entries': numbers, 'from': strings, 'to': strings, 'on_mainline': {'type': 'boolean'}, 'main_text_step': {'type': ['string', 'null']},
                                         'reported_by': {'type': 'string', 'enum': ['THIS_WORK', 'CITED']}})},
    # 2026-10-20: the paper's targets are its compounds of role TARGET (one or several), and each step count or overall yield the authors
    # state is kept with the compound it describes; before, one target string and one paper-wide count and yield held them all
    'reported_totals': {'type': 'array', 'items': obj({'compound': {'type': 'string'}, 'stated_step_count': {'type': ['string', 'null']},
                                                     'stated_overall_yield': {'type': ['string', 'null']}, 'printed_in': strings, 'reported_by': {'type': 'string', 'enum': ['THIS_WORK', 'CITED']}})},
    # 2026-10-21: reported_by says whether a route item or a statistic is the paper's own work or work the paper cites (a known conversion
    # of its last compound, an earlier synthesis it compares with); before, a cited one read as the paper's own
    'screening_and_failed_attempts': {'type': 'array', 'items': obj({'belongs_to_entries': numbers, 'where_printed': {'type': 'string'}, 'what': {'type': 'string'}})},
    'narrative': {'type': 'array', 'items': obj({'point': {'type': 'string'}, 'where_printed': {'type': 'string'}})},
    'unresolved': strings})
SYSTEM = """You are given one total-synthesis paper laid out in full: the main text with its figures as transcripts (a figure reader copied what is printed; you see no image), the SI general methods, every SI experiment entry, and a catalog of what is held elsewhere. Other tasks write down each experiment entry in detail. Your work is what only someone who sees the whole paper can write: the paper-level record.

- compounds: every compound the paper gives a label to, with the label exactly as printed, its name when one is printed, what it is in this paper, the SI entries that make it, and where its label is printed (block ids). A compound that appears only in a figure transcript still counts. Do not describe structures. Record every synthesis target the authors report reaching: the compounds marked TARGET are the authoritative target list; do not select one final target. A reached target may also be used in a later synthesis. A compound the authors make as their way to a natural product they do not make themselves, the rest of the way being earlier work they cite, is a target they report reaching; the cited conversion is in route with reported_by CITED. Keep prepared intermediates and previously reported reference compounds in their stated roles.
- route: the synthesis relationships of the SI experiment entries, in the order of the synthesis, keeping every synthesis branch the authors report: for each, its substrates and products as the SI entry prints them, whether it lies on the main line to a target, and the step of the main text or scheme it corresponds to (quote the arrow or sentence) when there is one. A route item describes one actual synthesis relationship, not necessarily one SI entry: independent or alternative preparations in the same entry use separate items with that same entry reference; do not combine their starting materials as co-reactants. A sourced main-text step without its own SI preparation may have entries=[], with its source retained in main_text_step; do not invent an SI entry. When the main text shows a different substrate or grouping than the SI, the route follows the SI entry; quote the main text under main_text_step so that the difference can be seen. A conversion the paper cites from earlier work instead of carrying it out is a route item too, with entries=[] and reported_by CITED; what the paper itself does is THIS_WORK.
- reported_totals: each author-stated step count or overall yield, with the compound it describes, in the authors' words, preserving the stated starting point and route qualifiers, and where it is printed. A statistic about an intermediate or catalyst does not by itself make it a synthesis target. Do not count steps or multiply yields. reported_by says whether the statistic describes this paper's work or work it cites. compound is the label of one of the compounds above.
- screening_and_failed_attempts: every condition table, optimisation, failed route or abandoned approach the paper reports, with the entries it belongs to and where it is printed.
- narrative: the points the authors themselves make about strategy, key steps, mechanism and comparison with the natural product, each with where it is printed. Their claims, not your assessment.

Write only what is printed. Where the paper leaves something open (a label used for two things, an entry that fits no step), say so under unresolved; do not guess."""
def ask(text, then=()):
    """(record or None, usage, finish reason, error) of one call on `text` (`then`: the turns after it, for a hand-back)."""
    last, record, usage, finish = None, None, {}, None
    for attempt in range(waits.TRIES):
        try:
            content, usage, finish = '', {}, None
            if NO_STREAM:      # one call answered whole (see record_actions: lpzproxy gives Gemini's thinking text only in a whole answer)
                r = client.with_options(timeout=NO_STREAM_TIMEOUT).chat.completions.create(model=MODEL, reasoning_effort=effort,
                        messages=[{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': text}, *then], response_format={'type': 'json_schema', 'json_schema': {'name': 'paper', 'strict': True, 'schema': SCHEMA}})
                choice = r.choices[0] if r.choices else None; content = (choice.message.content if choice else None) or ''; finish = choice.finish_reason if choice else None
                usage = {**(r.usage.model_dump(exclude_none=True) if r.usage else {}), 'answered_as': getattr(r, 'model', None)}
                if finish in ('length', 'max_tokens'): return None, usage, 'length', 'the answer reached the output limit'
                return json.loads(content), usage, finish, None
            stream = client.chat.completions.create(model=MODEL, reasoning_effort=effort, stream=True, stream_options={'include_usage': True},
                    messages=[{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': text}, *then], response_format={'type': 'json_schema', 'json_schema': {'name': 'paper', 'strict': True, 'schema': SCHEMA}})
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


# a label with a sign of rotation, a configuration or rac-/ent- before it, compared without them (P1, 2026-10-21)
STEREO_LEAD = re.compile(r'^\s*(?:\((?:[+\-−±]|[RSEZ](?:\s*,\s*\d*[RSEZ])*|\d+[RSEZ](?:\s*,\s*\d+[RSEZ])*)\)|rac|ent)\s*-\s*', re.I)


def issues(record):
    """What the record names that the host cannot join: a statistic whose compound is not a label of the record's compounds, and targets
    that differ only by a stereo prefix and are made in the same entries (perhaps one compound listed twice). The model decides."""
    compounds = record.get('compounds') or []; labels = {str(c.get('label') or '').strip() for c in compounds}
    out = [f'reported_totals gives the compound "{t.get("compound")}", which is not the label of any compound in compounds'
           for t in record.get('reported_totals') or [] if str(t.get('compound') or '').strip() not in labels]
    same = {}
    for c in compounds:
        if c.get('role') == 'TARGET' and c.get('made_in_entries'):
            same.setdefault((STEREO_LEAD.sub('', str(c.get('label') or '')).strip(), tuple(sorted(c['made_in_entries']))), []).append(str(c.get('label')))
    out += [f'the targets {", ".join(chr(34) + x + chr(34) for x in ls)} are made in the same entries ({", ".join(map(str, key[1]))}); if they are one compound, give it once'
            for key, ls in same.items() if len(ls) > 1]
    return list(dict.fromkeys(out))


def own_work(record):
    """A route item the model gives as CITED whose product is a starting material of a route item with SI entries is the paper's own
    work: the paper's experiments go on to use what it made, whether its procedure is printed or cited (owner 2026-10-21; two wordings in
    the task changed nothing, the model keeping to the SI's 'synthesized previously by ...'). Set in place to THIS_WORK, the model's answer
    and the entries that use the product kept with it; a cited conversion whose product the paper does not use stays CITED. The changes."""
    used = {}
    for x in record.get('route') or []:
        if x.get('entries'):
            for label in x.get('from') or []: used.setdefault(str(label).strip(), set()).update(x['entries'])
    changed = []
    for x in record.get('route') or []:
        if x.get('reported_by') != 'CITED': continue
        by = {str(l).strip(): sorted(used[str(l).strip()]) for l in x.get('to') or [] if str(l).strip() in used}
        if not by: continue
        x.update(reported_by='THIS_WORK', reported_by_model='CITED',
                 reported_by_basis='; '.join(f'its product {l} is a starting material of entries {", ".join(map(str, e))}' for l, e in by.items()))
        changed.append({'from': x.get('from'), 'to': x.get('to'), 'basis': x['reported_by_basis']})
    return changed


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
    parts' order, every author statistic (reported_totals) as each part gives it, repeated items once."""
    # One compound is one label with one name. A label the parts give with two different names is two compounds sharing the label (the
    # record says so); both are kept. A compound with no label is joined by its name only; one with neither is kept as it is.
    # names compared without typography (spaces, dashes, case); a sign of rotation is part of the name: (+)-X and (±)-X are not one compound
    canon = lambda t: re.sub(r'[\s\-‐‑‒–—−]+', '', str(t or '').strip()).lower()
    compounds = {}
    for r in records:
        for c in r.get('compounds') or []:
            label = str(c.get('label') or '').strip()
            # compared with the name the joined compound has by now: once a nameless one took a name, another name is another compound
            keys = [k for k in compounds if k[0] == label and (canon(compounds[k].get('name')) == canon(c.get('name')) or not canon(compounds[k].get('name')) or not canon(c.get('name')))] if label else \
                   [k for k in compounds if not k[0] and canon(c.get('name')) and k[1] == canon(c.get('name'))]
            key = keys[0] if keys else (label, canon(c.get('name')), len(compounds))
            had = compounds.setdefault(key, {**c, 'made_in_entries': [], 'printed_in': []})
            had['name'] = had.get('name') or c.get('name')
            if c.get('role') == 'TARGET': had['role'] = 'TARGET'
            had['made_in_entries'] = sorted(set(had['made_in_entries']) | set(c.get('made_in_entries') or []))
            had['printed_in'] = list(dict.fromkeys(had['printed_in'] + list(c.get('printed_in') or [])))
    once = lambda key: list({json.dumps(x, sort_keys=True, ensure_ascii=False): x for r in records for x in r.get(key) or []}.values())
    return {'compounds': list(compounds.values()), 'route': [x for r in records for x in r.get('route') or []], 'reported_totals': once('reported_totals'),
            'screening_and_failed_attempts': once('screening_and_failed_attempts'), 'narrative': once('narrative'), 'unresolved': once('unresolved')}


# A paper whose record does not fit in one answer (its answer reaches the output limit) is asked in parts, each with the whole main text
# and part of the experiment entries, twice as many parts each time up to eight; the host joins the parts' records.
began = time.monotonic(); split = int(os.environ.get('PAPER_PARTS', '1'))       # PAPER_PARTS: start in that many parts (a test of the joining)
first, handed_back = None, []
if split == 1:
    record, usage, finish, last = ask(package); calls = [{'parts': 1, 'output': usage.get('completion_tokens'), 'finish': finish}]
    handed_back = issues(record) if record else []
    if handed_back:      # once: the points listed, the rest of the record kept as it was
        note = '\n'.join(['The system checked the record:'] + [f'- {x}' for x in handed_back] + ['Answer with the whole record again, changing only what these points concern.'])
        again, usage2, finish2, last2 = ask(package, [{'role': 'assistant', 'content': json.dumps(record, ensure_ascii=False)}, {'role': 'user', 'content': note}])
        calls.append({'parts': 1, 'handed_back': True, 'output': usage2.get('completion_tokens'), 'finish': finish2})
        if again is not None: first, record = record, again
        usage = {'prompt_tokens': (usage.get('prompt_tokens') or 0) + (usage2.get('prompt_tokens') or 0), 'completion_tokens': (usage.get('completion_tokens') or 0) + (usage2.get('completion_tokens') or 0), 'answered_as': usage.get('answered_as')}
else: record, usage, finish, last, split, calls = None, {}, 'length', None, split // 2, []
while record is None and finish == 'length' and split < 8 and '\n### Entry ' in package:
    split *= 2; got = [ask(part) for part in parts_of(package, split)]
    calls.append({'parts': len(got), 'output': [g[1].get('completion_tokens') for g in got], 'finish': [g[2] for g in got]})
    usage = {'prompt_tokens': sum(g[1].get('prompt_tokens') or 0 for g in got), 'completion_tokens': sum(g[1].get('completion_tokens') or 0 for g in got), 'answered_as': got[0][1].get('answered_as')}
    if all(g[0] is not None for g in got): record, last = merged([g[0] for g in got]), None
    else: finish = 'length' if any(g[2] in ('length', 'max_tokens') for g in got) else None; last = next(g[3] for g in got if g[0] is None)
seconds = round(time.monotonic() - began, 1)
own = own_work(record) if record else []       # the host's reading of 'what the paper itself does' on the final record
out.write_text(json.dumps({'record': record, 'first_record': first, 'handed_back': handed_back, 'reported_by_by_host': own, 'issues_left': issues(record) if record else [], 'seconds': seconds, 'input': usage.get('prompt_tokens'), 'output': usage.get('completion_tokens'), 'answered_as': usage.get('answered_as'), 'parts': split, 'calls': calls, 'error': None if record else last}, ensure_ascii=False, indent=1))
print('paper task', seconds, 's', 'in', usage.get('prompt_tokens'), 'out', usage.get('completion_tokens'), 'compounds', len(record['compounds']) if record else None, 'route', len(record['route']) if record else None, last if not record else '')
