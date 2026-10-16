"""The recording stage, by reference: one flash task per SI experiment entry, in parallel. The host marks every quantity its rule finds
in the entry text; the task refers to marks instead of retyping numbers, and lists what was done as actions in the authors' order
(--form actions) or as stages (--form stages, the earlier wording, kept to measure the marks alone). The host reconciles by marks and
hands an entry back once, only for marks that do not close or wordings the entry does not print; stages are derived by the host.
Credentials from the process environment only (OPENAI_*).
usage: record_actions.py <pack dir> <package.md> <out dir> [--form actions|stages] [--effort medium]"""
import collections, csv, json, os, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from openai import OpenAI
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent)); import run_config, waits      # models and thinking levels: tools/run_config.json
import quantity_marks as qm

pack, package, out = Path(sys.argv[1]), Path(sys.argv[2]).read_text(encoding='utf-8'), Path(sys.argv[3]); out.mkdir(parents=True, exist_ok=True)
option = lambda name, default: sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
effort, FORM = option('--effort', run_config.effort('workflow', 'record')), option('--form', 'actions')
MODEL = run_config.model('workflow')
SILENCE = float(os.environ.get('RECORD_SILENCE', '420'))      # as in segmentation: the longest healthy silence traced there was 131 s
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['OPENAI_BASE_URL'], timeout=600, max_retries=0)
obj = lambda props: {'type': 'object', 'additionalProperties': False, 'required': list(props), 'properties': props}
strings = {'type': 'array', 'items': {'type': 'string'}}
Q = obj({'ref': {'type': ['string', 'null']}, 'raw': {'type': ['string', 'null']}, 'printed_in': {'type': 'string', 'enum': ['TEXT', 'SCHEME']}})
MATERIALS = {'type': 'array', 'items': obj({'name': {'type': 'string'}, 'role': {'type': 'string', 'enum': ['SUBSTRATE', 'REAGENT', 'CATALYST', 'SOLVENT', 'QUENCH', 'WASH', 'DRYING_AGENT', 'ELUENT', 'OTHER']},
                                            'quantities': {'type': 'array', 'items': Q}})}
CONDITIONS = {'type': 'array', 'items': obj({'kind': {'type': 'string', 'enum': ['TEMPERATURE', 'DURATION', 'ATMOSPHERE', 'REPEAT', 'OTHER']}, 'quantity': Q})}
ACTION = obj({'kind': {'type': 'string', 'enum': ['CHARGE', 'ADD', 'REMOVE', 'CONDITION', 'ATMOSPHERE', 'QUENCH', 'WORKUP', 'PURIFY', 'OTHER']}, 'what': {'type': 'string'}, 'begins_with': {'type': 'string'},
              'vessel': {'type': 'string'}, 'with_previous': {'type': 'boolean'}, 'materials': MATERIALS, 'conditions': CONDITIONS})
STEP = obj({'kind': {'type': 'string', 'enum': ['REACTION_STAGE', 'WORKUP', 'PURIFICATION']}, 'what_happens': {'type': 'string'}, 'materials': MATERIALS, 'conditions': CONDITIONS})
OUTCOMES = {'type': 'array', 'items': obj({'product': {'type': 'string'}, 'quantities': {'type': 'array', 'items': Q}, 'appearance': {'type': ['string', 'null']}})}
BLOCK = obj({'substrates': strings, 'products': strings, **({'actions': {'type': 'array', 'items': ACTION}} if FORM == 'actions' else {'steps': {'type': 'array', 'items': STEP}}), 'outcomes': OUTCOMES})
ENTRY = obj({'entry_kind': {'type': 'string', 'enum': ['SYNTHESIS', 'CONDITIONS_TRIED_OR_FAILED_ATTEMPT', 'NOT_A_PREPARATION']}, 'blocks': {'type': 'array', 'items': BLOCK},
             'not_recorded': {'type': 'array', 'items': obj({'ref': {'type': 'string'}, 'reason': {'type': 'string', 'enum': ['PART_OF_ANOTHER_QUANTITY', 'SAME_PASSAGE_PRINTED_TWICE', 'CHARACTERIZATION_DATA', 'NOT_A_QUANTITY']}})},
             'differences': {'type': 'array', 'items': obj({'kind': {'type': 'string', 'enum': ['CONTRADICTION', 'SCHEME_VS_TEXT_IN_SI', 'SUMMARY_OR_OMISSION', 'DIFFERENT_KIND_OF_QUANTITY']}, 'statement': {'type': 'string'}})},
             'characterization_frames': {'type': 'array', 'items': obj({'compound': {'type': 'string'}, 'starts_with': {'type': 'string'}})},
             'unresolved': strings})

OPENING = """You are given the main text of a total-synthesis paper and one experiment entry from its Supporting Information (SI), both as parsed text. Figures appear as transcripts: a figure reader copied what is printed in them, and you see no image. Write the entry down as a structured record. Someone who has never seen the paper must be able to repeat the experiment from the record alone, and the system checks every quantity of the record against the entry.

Marks. In the entry's text the system has put a mark in braces directly after every quantity that a mechanical rule recognised. The marks are not part of the paper. A marked quantity is recorded by giving its mark in ref; you do not retype it, because the system copies the wording from the text itself and a retyped number can be wrong. Every mark of the entry is used exactly once: either at the one place in the record where its quantity belongs, or under not_recorded. The rule knows nothing about chemistry and errs in three ways, and each has its answer:
- It marks only part of what is printed: one end of a range, a number without the words printed with it that say what the number is, a number the parse broke apart. Give the mark in ref, and in raw the complete wording as it is printed.
- It marks what is not a quantity of this experiment: a piece of a quantity that another mark already carries, a passage the parse printed twice, a number inside the characterization data, a number that is not a quantity at all. Give the mark under not_recorded with that reason.
- It misses quantities, and where the parse is poor it misses many: a quantity written in words, a count, a number whose unit or digits the parse spaced out or garbled, anything inside a table, anything printed only in the entry's own scheme. The marks are a help with what the rule found, not the list of what to record: read every sentence for its quantities yourself, and record each one that has no mark at its place, with ref null and raw copied as printed.
A marked wording is complete only when nothing the authors print with the number changes what it means: what a concentration or a strength is made up in, what a yield or a ratio covers and what it is counted against, where a range ends, whether a time or an amount was spread over the operation, given for each portion, added to an earlier one or counted in total. Where such words are printed with the number, raw is the whole of that printed wording; where none are, raw is null. A count of how many times an amount was used is not part of the amount's wording: it is a quantity of its own, recorded as told below. Wherever raw is given it is the wording as printed; where the parse has set its characters apart, close them up, and change nothing else.
A mark that ends in a question mark is an open one: digits or a sign stand apart just before the marked piece, so the parse may have broken one number in two and the rule caught only its end. For an open mark raw is always given: the complete quantity as it is printed; where the piece before it is something else (a compound label, another quantity), raw repeats the marked wording. In ref an open mark is named without its question mark.

Reaction blocks. A reaction block runs from what is charged to a product the authors isolate and report a mass or a yield for. Where the authors isolate something, report its mass or yield, and then carry on with it, or describe the same reaction again at another scale, the block ends there and the next begins. Everything isolated at the same point (the product, a side product, another isomer, recovered starting material) belongs to the one block that ends there. A reagent made on the spot belongs to the block that uses it. substrates and products are compound labels exactly as the authors print them."""

ACTIONS = """Actions. Inside a block, list what the authors did, one action per thing done, in the order in which it was done. That is the order in which they tell it, except where one sentence names first a solution or mixture that was made up on its own and then put into something already prepared: that solution is made up in its own vessel and comes first, what the reaction vessel already held is charged after it, and bringing the solution over is an ADD that follows. You are not asked where a stage begins or ends: that is worked out later from your list. What the list must get right is which things were done, in what order, and which substances and quantities belong to each.
- kind says what the action is. CHARGE: what is in the vessel to begin with. ADD: something is put in, including the contents of another vessel. REMOVE: something is taken out and the reaction work continues with what is left (a solvent evaporated in order to go on in another, a solid filtered off before the next reagent); taking the solvent off the extracts at the end of a workup is part of that WORKUP. CONDITION: nothing goes in or out; the mixture is heated, cooled, stirred, kept, irradiated, electrolysed. ATMOSPHERE: the gas over the mixture is changed, the mixture is degassed, the vessel is sealed. QUENCH: the reaction is stopped. WORKUP: the crude product is got out of the finished reaction mixture (extraction, washing the extracts, drying, concentration). PURIFY: what is done to the crude product. OTHER: none of these. Choose by what the action is for, not by its verb: washing or drying a reagent before it is used is not WORKUP, it is the preparation of that reagent.
- A condition that says how an action was carried out belongs to that action: the temperature at which something was added, the time over which it was added, the number of portions. What is done to the mixture after that is an action of its own.
- with_previous is true when the authors name this action together with the one before it and do not say which came first. Their order is then not known and must not be invented: keep the printed order and set with_previous on every one after the first. It is false whenever the authors state an order.
- vessel is MAIN for the reaction vessel. What is done in another vessel before its contents join the reaction (a reagent made or mixed apart, a solution prepared separately) is SIDE1, and SIDE2 for a second such vessel; bringing its contents over is an ADD in MAIN.
- what tells the action in a few of the authors' own words. begins_with is the first few words of the passage that tells it, copied exactly from the entry text without marks, so that the system can find the place.
- materials are the substances of the action, each with every quantity printed for it. conditions are the temperatures, durations, atmosphere and repeat counts of the action; an atmosphere merely stated with an action (the gas it was done under) is a condition of that action, and only an act of changing the gas is an ATMOSPHERE action.
- A remark that applies to the whole entry is an action of kind OTHER at the place where it is printed.
outcomes: everything the authors isolate and report (the product, a side product, another isomer, recovered starting material), each with every quantity reported for it (mass, amount, yield, ratio) and its appearance."""

STAGES = """What a block holds:
- steps, in the authors' order. A stage is what is put into the flask at one point together with the conditions that hold until the next thing is put in, so that a reader can see what was present at each moment. The first charge is a stage ("14a in THF"); every later addition starts a new stage, even inside one sentence ("To 14a in THF was added DBU" is two stages); things the authors add together without saying which came first ("PdCl2 and CuCl were added") are one stage with several materials, because their order is not known and must not be invented; a change of temperature or a waiting time that the authors set apart ("then warmed to", "after one hour", "was cooled to -78 °C and then ...") is a stage of its own; heating or stirring stated right after an addition belongs to that addition's stage. A step's kind follows what it is for, not the verb: everything done before or between the reactions to get the reaction going is a REACTION_STAGE (washing or drying a reagent before use, making a reagent solution, removing the solvent to continue in another one); WORKUP is what is done to the reaction mixture once a reaction is over to get the crude product out (quench, extraction, washing the extracts, drying, concentration), and it can occur in the middle when the authors work up and then continue; PURIFICATION is what is done to the crude product (chromatography, distillation, recrystallisation). what_happens is one short sentence in the authors' words.
- materials of a step: every substance used in it, with every quantity the authors print for it. conditions of a step: each temperature, each duration, the atmosphere, each repeat count.
- outcomes: everything the authors isolate and report (the product, a side product, a second isomer, recovered starting material), each with every reported quantity (mass, amount, yield, ratio such as dr) and its appearance."""

CLOSING = """Quantities are only useful if they are faithful, so the record holds what is printed and nothing else.
- Never compute, convert or complete: no equivalents, totals or units that the authors did not print. A quantity written in words stays in words.
- A number that describes a substance is a quantity of that substance, whatever it measures: its concentration, its purity or strength, as much as its mass, volume or amount.
- A printed quantity is not left out because it looks minor. The same temperature stated for two moments is two conditions. A count printed together with an amount, saying how many times that amount was used, is a quantity of its own: it is a REPEAT condition of the action, recorded beside the amount and not folded into it.
- The entry's own scheme is part of the entry. A quantity printed only there is recorded where it belongs with printed_in SCHEME; everything from the procedure text has printed_in TEXT.

The main text is there to settle what the entry leaves unclear (which compound a label means, what a reagent is), never to add quantities to the blocks. Where the entry and another place disagree, the blocks carry what the procedure text of this entry prints, because the entry is the record of one actual run and a scheme or a summary was written afterwards. Each disagreement is reported under differences, quoting both sides and saying where each is printed, with its kind:
- CONTRADICTION: the main text or its scheme states something that cannot be true together with the SI entry.
- SCHEME_VS_TEXT_IN_SI: the entry's own scheme and its procedure text disagree.
- SUMMARY_OR_OMISSION: the other place only leaves something out, rounds it, or names a nominal scale; nothing is contradicted.
- DIFFERENT_KIND_OF_QUANTITY: the two numbers measure different things, such as two kinds of yield.
A number or label that looks misprinted is quoted as printed, not corrected.

Characterization data (Rf, NMR, HRMS, IR, melting point, optical rotation) is kept verbatim by the system and is not copied into the record: point at it in characterization_frames (the compound, the first few words), and give the marks that fall inside it under not_recorded.
entry_kind is a label and changes nothing about what is recorded: every entry is written down in full as described above, as faithfully as the fields allow. SYNTHESIS: the entry tells how a compound was made and isolated, whatever the means. CONDITIONS_TRIED_OR_FAILED_ATTEMPT: the entry as a whole is a set of conditions screened or an attempt reported as not having worked. NOT_A_PREPARATION: the entry makes no compound (an assay, a measurement protocol, a calculation, the preparation of a protein or a cell culture).
What cannot be decided from the text (a garbled number, an owner that is not clear) is said in unresolved, not guessed."""
SYSTEM = '\n\n'.join([OPENING, ACTIONS if FORM == 'actions' else STAGES, CLOSING])

blocks = {}
for line in (pack / 'inputs/si/blocks.jsonl').read_text(encoding='utf-8').splitlines():
    row = json.loads(line); blocks['SI-P%04d-B%04d' % (row['page_idx'], row['reading_order'])] = row
units = [(i, u) for i, u in enumerate(json.loads((pack / 'inputs/si/units.json').read_text(encoding='utf-8'))['units']) if u['kind'] == 'PROCEDURE']
sheet = collections.defaultdict(list)
if (pack / 'worksheets/A_procedure_quantities.csv').exists():
    for row in csv.DictReader(open(pack / 'worksheets/A_procedure_quantities.csv', encoding='utf-8-sig')): sheet[row['entry']].append(row)
main_text = package.split('## 2. SI: GENERAL METHODS')[0]
figures = {m.group(1): m.group(0) for m in re.finditer(r'\[FIGURE (SI-P\d+-B\d+).*?(?=\n\n|\Z)', package, re.S)}
entries = []
for n, (index, unit) in enumerate(units, 1):
    texts, marks = qm.mark_entry(unit['block_ids'], blocks, sheet[f'E{n:02d}'])
    parts = [f"### Entry {n} (compound label as segmented: {unit['compound_label']}; SI page {blocks[unit['block_ids'][0]]['page_idx'] + 1})"]
    for block_id in unit['block_ids']:
        if texts[block_id]['marked'].strip(): parts.append(f"[{block_id}] {texts[block_id]['marked']}")
        if block_id in figures: parts.append(figures[block_id])
    entries.append({'n': n, 'entry': f'E{n:02d}', 'si_unit': index, 'text': '\n\n'.join(parts), 'texts': texts, 'marks': marks, 'scheme': '\n'.join(figures[b] for b in unit['block_ids'] if b in figures)})
cost = collections.Counter(); calls = []


def streamed(messages):
    """The answer of one streamed call. The gateway sends a keep-alive every 15 s while the model is silent, and a keep-alive also resets
    the client's read timeout, so a stream whose model has stopped would be waited on forever: a watch closes the stream when no chunk of
    the model's own (thinking or answer) has come for SILENCE seconds."""
    stream = client.chat.completions.create(model=MODEL, messages=messages, reasoning_effort=effort, stream=True, stream_options={'include_usage': True},
                                            response_format={'type': 'json_schema', 'json_schema': {'name': 'entry', 'strict': True, 'schema': ENTRY}})
    heard, done, stalled = [time.monotonic()], threading.Event(), []
    def watch():
        while not done.wait(5):
            if time.monotonic() - heard[0] > SILENCE: stalled.append(True); stream.close(); return
    threading.Thread(target=watch, daemon=True).start()
    content, usage, finish = '', {}, None
    try:
        for chunk in stream:
            heard[0] = time.monotonic(); usage.setdefault('answered_as', getattr(chunk, 'model', None))      # the model the gateway says answered
            if chunk.usage: usage = {**chunk.usage.model_dump(exclude_none=True), 'answered_as': usage.get('answered_as')}
            for choice in chunk.choices: content += choice.delta.content or ''; finish = choice.finish_reason or finish
    except Exception:
        if not stalled: raise
    finally: done.set()
    if stalled: raise TimeoutError(f'no chunk from the model for {SILENCE:.0f} s; the stream was closed')
    if not content.strip():      # say why, so that an answer that never comes can be told from one that was cut
        raise ValueError(f"empty answer (finish {finish}, output tokens {usage.get('completion_tokens')}, of them thought {(usage.get('completion_tokens_details') or {}).get('reasoning_tokens')})")
    usage['finish'] = finish
    return content, usage


class Refused(Exception):
    """The provider declined the request; asking again gives the same."""


def parsed(content, usage):
    """The record in an answer. An answer whose JSON is wrapped (a code fence, a sentence before it) is read from its first brace to its
    last; one that still does not read says how it begins and why it ended, for the log."""
    try: return json.loads(content)
    except ValueError:
        try: return json.loads(content[content.index('{'):content.rindex('}') + 1])
        except ValueError:
            if 'request was blocked' in content: raise Refused(content[:120]) from None      # the provider's own refusal: final, not tried again
            raise ValueError(f"no JSON in the answer (finish {usage.get('finish')}, output tokens {usage.get('completion_tokens')}): {content[:160]!r}") from None


def ask(messages, what):
    began = time.monotonic(); last = None; usage = {}
    for attempt in range(waits.TRIES):
        try:
            content, usage = streamed(messages)
            answer = parsed(content, usage); break
        except Exception as error:
            last, answer = type(error).__name__ + ': ' + str(error)[:160], None; cost['failed_tries'] += 1
            if isinstance(error, Refused): break
            time.sleep(waits.pause(error, attempt))      # a rate limit lasts minutes
    seconds = round(time.monotonic() - began, 1)
    cost['calls'] += 1; cost['input'] += usage.get('prompt_tokens', 0); cost['output'] += usage.get('completion_tokens', 0)
    calls.append({'what': what, 'seconds': seconds, 'input': usage.get('prompt_tokens'), 'output': usage.get('completion_tokens'), 'answered_as': usage.get('answered_as'), 'error': None if answer is not None else last})
    print(time.strftime('%H:%M:%S'), what, seconds, 's', '' if answer is not None else last, flush=True)
    return answer


def located(record, texts):
    """Where each action's begins_with is printed (block, offset), found by the host; None when the entry does not print those words."""
    flat = [(block_id, qm.squeeze(t['plain'])) for block_id, t in texts.items()]; found = []
    for block in record.get('blocks') or []:
        for action in block.get('actions') or []:
            words = qm.squeeze(action.get('begins_with')); place = None
            for order, (block_id, text) in enumerate(flat):
                at = text.find(words) if words else -1
                if at >= 0: place = [block_id, order, at]; break
            found.append(place)
    return found


def summary(found): return {k: (v if k in ('unplaced', 'repeated', 'unknown', 'not_printed', 'no_wording', 'problems') else len(v)) for k, v in found.items()}


def run(item):
    messages = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': 'THE MAIN TEXT\n\n' + main_text + '\n\nTHE ENTRY TO WRITE\n\n' + item['text']}]
    first = record = ask(messages, f"entry {item['n']}")
    base = {'entry': item['entry'], 'n': item['n'], 'si_unit': item['si_unit'], 'marks': item['marks']}
    if record is None: return {**base, 'record': None, 'first_record': None, 'handed_back': None, 'check': None, 'resolved': None, 'kept': None}
    found = first_found = qm.reconcile(record, item['texts'], item['marks'], item['scheme']); handed = None; kept = 'first'
    if found['problems']:
        handed = summary(found)
        second = ask(messages + [{'role': 'assistant', 'content': json.dumps(record, ensure_ascii=False)}, {'role': 'user', 'content': qm.hand_back_note(found, item['marks'])}], f"entry {item['n']} handed back")
        if second is not None:
            again = qm.reconcile(second, item['texts'], item['marks'], item['scheme'])
            # The second record replaces the first only when it closes better; a rewrite that loses more than it repairs is not accepted.
            if again['problems'] < found['problems']: record, found, kept = second, again, 'second'
            handed['second_problems'] = again['problems']
    return {**base, 'record': record, 'first_record': first if handed else None, 'handed_back': handed, 'kept': kept, 'entry_kind': record.get('entry_kind'), 'check': summary(found),
            'resolved': qm.resolve(record, item['texts'], item['marks']), 'action_places': located(record, item['texts']) if FORM == 'actions' else None}


began = time.monotonic()
if not entries:      # a supplement with no procedure entry is a result, not a failure
    (out / 'results.json').write_text(json.dumps({'form': FORM, 'wall_seconds': 0, 'longest_call': 0, 'cost': {}, 'calls': [], 'results': []}), encoding='utf-8'); print('recording: the SI has no procedure entry'); raise SystemExit(0)
# at most RECORD_PARALLEL entries at once (default 12): a joined experimental document can hold hundreds of entries, and several papers
# run at once share the gateway's rate limit
# --resume: an entry the last recording of this run already wrote (it has a record, and the host marks its text the same way) is kept;
# only the others are asked again -- the entries a lost connection or a rate limit left without a record.
previous = {}
if '--resume' in sys.argv and (out / 'results.json').exists():
    earlier = json.loads((out / 'results.json').read_text(encoding='utf-8')); calls[:0] = earlier.get('calls') or []
    previous = {r['n']: r for r in earlier['results'] if r.get('record')}
same_marks = lambda a, b: [(m['block_id'], m['raw'], m['unit']) for m in a] == [(m['block_id'], m['raw'], m['unit']) for m in b]
def run_or_keep(item):
    kept = previous.get(item['n'])
    return {**kept, 'resumed': True} if kept and same_marks(kept['marks'], item['marks']) else run(item)
with ThreadPoolExecutor(max_workers=max(1, min(len(entries), int(os.environ.get('RECORD_PARALLEL', '12'))))) as pool: results = list(pool.map(run_or_keep, entries))
wall = round(time.monotonic() - began, 1); done = [r for r in results if r['record']]
(out / 'results.json').write_text(json.dumps({'form': FORM, 'wall_seconds': wall, 'longest_call': max((c['seconds'] for c in calls), default=0), 'cost': dict(cost), 'resumed': sum(bool(r.get('resumed')) for r in results), 'calls': calls, 'results': results}, ensure_ascii=False, indent=1), encoding='utf-8')
total = sum(len(r['marks']) for r in results); left = sum(r['check']['problems'] for r in done); print('entry kinds:', dict(collections.Counter(r.get('entry_kind') for r in results)))
print(f"recording ({FORM}): entries {len(results)} (without a record {len(results) - len(done)}) | blocks {sum(len(r['record']['blocks']) for r in done)} | marks {total}, placed {sum(r['check']['placed'] for r in done)}, set aside {sum(r['check']['set_aside'] for r in done)}, "
      f"problems left {left} | handed back {sum(r['handed_back'] is not None for r in results)} (second kept {sum(r['kept'] == 'second' for r in results)}) | wall {wall}s longest {max((c['seconds'] for c in calls), default=0)}s | kept from before {sum(bool(r.get('resumed')) for r in results)} | calls {cost['calls']} input {cost['input']} output {cost['output']} failed tries {cost.get('failed_tries', 0)}")
