"""The recording stage of the whole-paper chain: one flash task per SI experiment entry, in parallel. Each task sees the main text and its own
entry (with the entry's scheme as a figure transcript) and returns the reaction blocks of that entry. The host then checks two things by rule and
hands the entry back once: printed quantities the record does not carry, and stages that hold what the definition keeps apart.
Credentials from the process environment only (OPENAI_*).
usage: record_entries.py <pack dir> <package.md> <out dir> [--effort medium]"""
import collections, csv, json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from pathlib import Path
from openai import OpenAI
sys.path.insert(0, str(Path(__file__).resolve().parent)); import run_config      # models and thinking levels: tools/run_config.json

pack, package, out = Path(sys.argv[1]), Path(sys.argv[2]).read_text(), Path(sys.argv[3]); out.mkdir(parents=True, exist_ok=True)
effort = sys.argv[sys.argv.index('--effort') + 1] if '--effort' in sys.argv else run_config.effort('workflow', 'record')
MODEL = run_config.model('workflow')
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['OPENAI_BASE_URL'], timeout=600, max_retries=0)
obj = lambda props: {'type': 'object', 'additionalProperties': False, 'required': list(props), 'properties': props}
strings = {'type': 'array', 'items': {'type': 'string'}}
Q = obj({'raw': {'type': 'string'}, 'value': {'type': ['string', 'null']}, 'unit': {'type': ['string', 'null']}, 'printed_in': {'type': 'string', 'enum': ['TEXT', 'SCHEME']}})
STEP = obj({'kind': {'type': 'string', 'enum': ['REACTION_STAGE', 'WORKUP', 'PURIFICATION']}, 'what_happens': {'type': 'string'},
            'materials': {'type': 'array', 'items': obj({'name': {'type': 'string'}, 'role': {'type': 'string', 'enum': ['SUBSTRATE', 'REAGENT', 'CATALYST', 'SOLVENT', 'QUENCH', 'WASH', 'DRYING_AGENT', 'ELUENT', 'OTHER']},
                                                         'quantities': {'type': 'array', 'items': Q}})},
            'conditions': {'type': 'array', 'items': obj({'kind': {'type': 'string', 'enum': ['TEMPERATURE', 'DURATION', 'ATMOSPHERE', 'REPEAT', 'OTHER']}, 'quantity': Q})}})
BLOCK = obj({'substrates': strings, 'products': strings, 'steps': {'type': 'array', 'items': STEP},
             'outcomes': {'type': 'array', 'items': obj({'product': {'type': 'string'}, 'quantities': {'type': 'array', 'items': Q}, 'appearance': {'type': ['string', 'null']}})}})
ENTRY = obj({'blocks': {'type': 'array', 'items': BLOCK},
             'differences': {'type': 'array', 'items': obj({'kind': {'type': 'string', 'enum': ['CONTRADICTION', 'SCHEME_VS_TEXT_IN_SI', 'SUMMARY_OR_OMISSION', 'DIFFERENT_KIND_OF_QUANTITY']}, 'statement': {'type': 'string'}})},
             'characterization_frames': {'type': 'array', 'items': obj({'compound': {'type': 'string'}, 'starts_with': {'type': 'string'}})},
             'unresolved': strings})
SYSTEM = """You are given the main text of a total-synthesis paper and one experiment entry from its Supporting Information (SI), both as parsed text with figures as transcripts (a figure reader copied what is printed; you see no image). Write the entry down as structured reaction blocks. Someone who has never seen the paper must be able to repeat the experiment from your record alone, and every number in it will be checked against the entry.

Reaction blocks. A reaction block runs from what is charged to a product the authors isolate and report a mass or a yield for. Most entries are one block. Where the authors isolate something, report its mass or yield, and then carry on with it (an intermediate that is weighed, or the same reaction described again at a second scale), the block ends there and the next begins: one block per isolation the authors report. A reagent made on the spot belongs to the block that uses it.

What a block holds:
- substrates, products: compound labels exactly as the authors print them (13, 14a, (±)-9, SI-4).
- steps, in the authors' order. A stage is what is put into the flask at one point together with the conditions that hold until the next thing is put in, so that a reader can see what was present at each moment. The first charge is a stage ("14a in THF"); every later addition starts a new stage, even inside one sentence ("To 14a in THF was added DBU" is two stages); things the authors add together without saying which came first ("PdCl2 and CuCl were added") are one stage with several materials, because their order is not known and must not be invented; a change of temperature or a waiting time that the authors set apart ("then warmed to", "after one hour", "was cooled to -78 °C and then ...") is a stage of its own; heating or stirring stated right after an addition belongs to that addition's stage. A step's kind follows what it is for, not the verb: everything done before or between the reactions to get the reaction going is a REACTION_STAGE (washing or drying a reagent before use, making a reagent solution, removing the solvent to continue in another one); WORKUP is what is done to the reaction mixture once a reaction is over to get the crude product out (quench, extraction, washing the extracts, drying, concentration), and it can occur in the middle when the authors work up and then continue; PURIFICATION is what is done to the crude product (chromatography, distillation, recrystallisation). what_happens is one short sentence in the authors' words.
- materials of a step: every substance used in it, with every quantity the authors print for it. conditions of a step: each temperature, each duration, the atmosphere, each repeat count.
- outcomes: everything the authors isolate and report (the product, a side product, a second isomer, recovered starting material), each with every reported quantity (mass, amount, yield, ratio such as dr) and its appearance.

How to write a quantity, and why: the record is only useful if it is faithful, so write only what is printed.
- raw is the authors' wording, copied exactly as printed ("1.17 g", "-78 °C", "one hour", "3 ×", "3 : 1 dr", "20:1 → 10:1"). value and unit are the number and unit when the authors print a number. When they write a quantity in words ("one hour", "room temperature", "overnight"), value and unit are null: do not turn words into numbers.
- Never compute, convert or complete: no equivalents the authors did not print, no totals. For "3 × 100 mL", the quantity is "100 mL" and the count "3 ×" is a REPEAT condition.
- A number that describes a substance is a quantity of that substance, whatever it measures: its concentration ("1.6 M in Et2O"), its purity or strength ("60% in mineral oil"), as well as its mass, volume and amount.
- Do not leave a printed quantity out because it looks minor. A temperature stated twice for two moments is two conditions. A remark that applies to the whole entry ("all solvents were degassed for ten minutes") is a step of its own.
- The entry's own scheme (the figure transcript inside the entry) is part of the entry. A quantity printed only there (a temperature or time on the arrow, a dr, a catalyst loading) is recorded where it belongs with printed_in SCHEME; everything from the procedure text has printed_in TEXT.

The main text is there to settle what the entry leaves unclear (a label, a reagent name), never to add quantities to the blocks. Where the entry and another place disagree, the blocks carry what the SI procedure text of this entry prints (amounts, conditions, solvent, reagents, the substrate actually used, its own yield), because the entry is the record of one actual run and a scheme is a later summary. Report each difference under differences, quoting both sides and where each is printed, with its kind:
- CONTRADICTION: the main text or its scheme states something that cannot both be true with the SI entry (a different solvent, substrate, temperature, reagent, or a different value of the same kind of yield).
- SCHEME_VS_TEXT_IN_SI: the entry's own scheme and its procedure text disagree.
- SUMMARY_OR_OMISSION: the main text or a scheme only leaves something out, rounds it, or gives a nominal scale ("10 g scale" for 9.78 g); nothing is contradicted.
- DIFFERENT_KIND_OF_QUANTITY: the two numbers are different things (an isolated yield and a yield based on recovered starting material, an optimized yield, an analytical yield).
Do not correct a number or label that looks misprinted; quote it.

Characterization data (Rf, NMR, HRMS, IR, melting point, optical rotation) is kept verbatim by the system, so do not copy it; if the entry contains such data only point at it in characterization_frames (compound, first few words).
If something cannot be decided from the text (a garbled number, an unclear owner), say so in unresolved; do not guess. If the entry is not a synthesis experiment (a screening table, a biological or computational protocol), write what it prints as faithfully as the fields allow and say what it is in unresolved."""

sheet = collections.defaultdict(list)
for row in csv.DictReader(open(pack / 'worksheets/A_procedure_quantities.csv', encoding='utf-8-sig')): sheet[row['entry']].append(row)
main_text = package.split('## 2. SI: GENERAL METHODS')[0]
entries = [{'n': int(m.group(1)), 'entry': f'E{int(m.group(1)):02d}', 'text': m.group(0)} for m in re.finditer(r'### Entry (\d+) \(.*?(?=\n### Entry |\n## 4\. )', package, re.S)]
ALIAS = {'minutes': 'min', 'minute': 'min', 'hours': 'h', 'hour': 'h', 'equiv.': 'equiv', 'eq': 'equiv', 'eq.': 'equiv'}
norm_unit = lambda u: ALIAS.get(re.sub(r'\s+', '', (u or '').replace('μ', 'µ')), re.sub(r'\s+', '', (u or '').replace('μ', 'µ')))
cost = collections.Counter(); calls = []


def decimal(value):
    try: return Decimal(str(value).replace('−', '-').replace('–', '-').replace(' ', ''))
    except (InvalidOperation, ValueError): return None


def quantities(node, found):
    if isinstance(node, dict):
        if {'raw', 'value', 'unit'} <= set(node): found.append(node)
        for value in node.values(): quantities(value, found)
    elif isinstance(node, list):
        for value in node: quantities(value, found)
    return found


def check(entry, record):
    """Mechanical reconciliation against the quantities extracted by rule from the entry text."""
    typed = quantities(record, []); text = re.sub(r'\s+', ' ', json.dumps(record, ensure_ascii=False)).lower(); rows = []
    for row in sheet[entry]:
        if row['unit'] == 'WORDS':
            state = 'TYPED' if any(row['raw'].lower() in (q['raw'] or '').lower() for q in typed) else 'TEXT' if row['raw'].lower() in text else 'ABSENT'
        else:
            want = decimal(row['value']); unit = norm_unit(row['unit'])
            hit = any(q['value'] is not None and decimal(q['value']) is not None and abs(decimal(q['value'])) == abs(want) and norm_unit(q['unit']) == unit for q in typed)
            state = 'TYPED' if hit else 'TEXT' if row['value'].lstrip('-') in text else 'ABSENT'
        rows.append((row['row'], row['raw'], state))
    return rows


def stage_flags(record):
    """Stages that hold what the definition keeps apart: the first charge with a later addition, or a set-apart temperature change with an addition."""
    flags = []
    for block in record.get('blocks') or []:
        for step in block['steps']:
            if step['kind'] != 'REACTION_STAGE': continue
            amounts = [m for m in step['materials'] if m['quantities'] and m['role'] in ('SUBSTRATE', 'REAGENT', 'CATALYST')]
            said = step['what_happens']
            if any(m['role'] == 'SUBSTRATE' for m in amounts) and any(m['role'] != 'SUBSTRATE' for m in amounts) and re.search(r'\b(was|were) added\b|\btreated with\b', said):
                flags.append(f'"{said}": the first charge and a later addition are in one stage')
            elif re.search(r'\b(cooled|warmed|heated|allowed to warm)\b[^.;]*\b(and then|then|before|after which)\b[^.;]*\b(added|treated)\b', said) and amounts:
                flags.append(f'"{said}": a temperature change the authors set apart and an addition are in one stage')
    return flags


def ask(messages, what):
    began = time.monotonic(); last = None; usage = {}
    for attempt in range(4):
        try:
            content, usage = '', {}
            for chunk in client.chat.completions.create(model=MODEL, messages=messages, reasoning_effort=effort, stream=True, stream_options={'include_usage': True},
                                                        response_format={'type': 'json_schema', 'json_schema': {'name': 'entry', 'strict': True, 'schema': ENTRY}}):
                if chunk.usage: usage = chunk.usage.model_dump(exclude_none=True)
                for choice in chunk.choices: content += choice.delta.content or ''
            answer = json.loads(content); break
        except Exception as error:
            last, answer = type(error).__name__ + ': ' + str(error)[:160], None; cost['failed_tries'] += 1; time.sleep(8 * (attempt + 1))
    seconds = round(time.monotonic() - began, 1)
    cost['calls'] += 1; cost['input'] += usage.get('prompt_tokens', 0); cost['output'] += usage.get('completion_tokens', 0)
    calls.append({'what': what, 'seconds': seconds, 'input': usage.get('prompt_tokens'), 'output': usage.get('completion_tokens'), 'error': None if answer is not None else last})
    print(time.strftime('%H:%M:%S'), what, seconds, 's', '' if answer is not None else last, flush=True)
    return answer


def run(item):
    messages = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': 'THE MAIN TEXT\n\n' + main_text + '\n\nTHE ENTRY TO WRITE\n\n' + item['text']}]
    first = record = ask(messages, f"entry {item['n']}")
    handed = None
    if record is not None:
        missing = [raw for _, raw, state in check(item['entry'], record) if state != 'TYPED']; flags = stage_flags(record)
        if missing or flags:
            note = 'The system compared your record with the entry by rule. Both checks can be wrong; look at the entry text again for each item and return the complete corrected record.'
            if missing: note += ('\n\nPrinted quantities that are not written as a quantity in the record (one that belongs to characterization data, or a parsing artefact, is not missing): ' + '; '.join(f'"{m}"' for m in missing) + '.')
            if flags: note += ('\n\nStages that may hold what the definition keeps apart (a stage is what is put in at one point; the first charge is its own stage; a set-apart temperature change is its own stage; things added together with no stated order stay together):\n- ' + '\n- '.join(flags))
            handed = {'missing': missing, 'stage_flags': flags}
            record = ask(messages + [{'role': 'assistant', 'content': json.dumps(record, ensure_ascii=False)}, {'role': 'user', 'content': note}], f"entry {item['n']} handed back") or record
    return {'entry': item['entry'], 'n': item['n'], 'record': record, 'first_record': first if handed else None, 'handed_back': handed,
            'check': check(item['entry'], record) if record else None, 'stage_flags_left': stage_flags(record) if record else None}


began = time.monotonic()
with ThreadPoolExecutor(max_workers=max(1, len(entries))) as pool: results = list(pool.map(run, entries))
wall = round(time.monotonic() - began, 1)
(out / 'results.json').write_text(json.dumps({'wall_seconds': wall, 'longest_call': max(c['seconds'] for c in calls), 'cost': dict(cost), 'calls': calls, 'results': results}, ensure_ascii=False, indent=1))
print(f"recording: entries {len(results)} (without a record {sum(r['record'] is None for r in results)}) | blocks {sum(len(r['record']['blocks']) for r in results if r['record'])} | handed back {sum(r['handed_back'] is not None for r in results)} "
      f"(for stages {sum(bool(r['handed_back'] and r['handed_back']['stage_flags']) for r in results)}) | wall {wall}s longest {max(c['seconds'] for c in calls)}s | calls {cost['calls']} input {cost['input']} output {cost['output']} failed tries {cost['failed_tries']}")
