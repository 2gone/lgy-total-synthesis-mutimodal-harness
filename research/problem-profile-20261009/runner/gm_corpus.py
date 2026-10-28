"""A model behind an OpenAI-compatible gateway (October 2026: Gemini gemini-3.8-flash-high on lpzproxy) through the whole chain of a corpus
under one budget of calls in flight (2026-10-27, the 200
papers of ready200 on the v4 prompts). The settings of every call are gm_run.py's (shim, final4 + o3, the v4 rewordings, marks, medium
thinking, no streaming, NO_PROXY, every call's thinking kept in thoughts/<label>.jsonl); what differs is the scheduling:
  - the segmentation runs apart (si_units_probe.py with whole_paper.py's step-1 settings), one call at a time per paper, at most
    --seg-at-once papers together, the largest SI first (its windows are the longest serial part of a paper);
  - then whole_paper.py --reuse-units records the entries with RECORD_PARALLEL = min(entries the segmentation found, --max-parallel), so
    a paper takes RECORD_PARALLEL + 1 calls (its entries and the paper task) and waits, in the order papers finish segmenting, until
    that many fit in the budget;
  - a paper left with entries without a record or a failed paper task is repaired (whole_paper.py --repair) up to --repairs times.
Calls in flight never exceed --budget: one per segmenting paper, RECORD_PARALLEL + 1 per recording paper (each entry task and the paper
task ask one call at a time). --budget must stay within --hard-cap.
Resumable: a run with a complete result is left; one with units is repaired; segmentation already done in corpus-units/<label> is reused.
Portable (2026-10-28): paths are taken from where this file lies (runner/ beside tools/, the repository two levels up); the call shim and
the prompt rewordings are in runner/shim and runner/prompts. Credentials from the process environment (OPENAI_BASE_URL, OPENAI_API_KEY),
or from a file --cred names; they go into the children only and are never written anywhere.
usage: gm_corpus.py <round> --papers-file <one paper per line> [--cred <file>] [--model gemini-3.8-flash-high] [--budget 500] [--hard-cap 550]
       [--max-parallel 32] [--seg-at-once 80] [--repairs 2] [--si-text __clean_b] [--prefix gm1021]
Labels <prefix>-r<round>-<paper>; status in whole-paper/<prefix>-r<round>-status.json; every call's thinking in thoughts/<label>.jsonl."""
import json, os, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
RUNNER = Path(__file__).resolve().parent; HERE = RUNNER.parent; TOOLS = HERE / 'tools'; PROMPTS = RUNNER / 'prompts'
FIX = HERE.parents[1] / 'tests/fixtures/si_packages'
opt = lambda n, d=None: sys.argv[sys.argv.index(n) + 1] if n in sys.argv else d
rnd = sys.argv[1]; papers = [p.strip() for p in Path(opt('--papers-file')).read_text(encoding='utf-8').splitlines() if p.strip() and not p.startswith('#')]
BUDGET, CAP, MAX_RP = int(opt('--budget', '500')), int(opt('--hard-cap', '550')), int(opt('--max-parallel', '32'))
SEG_AT_ONCE, REPAIRS, SI_TEXT = int(opt('--seg-at-once', '80')), int(opt('--repairs', '2')), opt('--si-text', '__clean_b')
if BUDGET > CAP: raise SystemExit(f'--budget {BUDGET} is above the hard cap {CAP}')
MODEL = opt('--model', os.environ.get('TSDAF_WORKFLOW_MODEL', 'gemini-3.8-flash-high')); PREFIX = opt('--prefix', 'gm1021'); CRED = Path(opt('--cred')) if opt('--cred') else None
read_env = lambda path: {k.strip(): v.strip().strip('"').strip("'") for k, _, v in (l.strip().removeprefix('export ').partition('=') for l in Path(path).read_text(encoding='utf-8').splitlines()) if _}
cred = read_env(CRED) if CRED else {k: os.environ[k] for k in ('OPENAI_BASE_URL', 'OPENAI_API_KEY') if os.environ.get(k)}
if not cred.get('OPENAI_API_KEY') or not cred.get('OPENAI_BASE_URL'): raise SystemExit('no credentials: set OPENAI_BASE_URL and OPENAI_API_KEY, or --cred <file>')
DET = os.environ.get('TSDAF_DETECTOR_ENV') or r'D:\tsdaf\det\detector.env'
det = read_env(DET) if Path(DET).exists() else {}
base = {**os.environ, 'OPENAI_API_KEY': cred['OPENAI_API_KEY'], 'OPENAI_BASE_URL': cred['OPENAI_BASE_URL'], **det,
        'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1', 'PYTHONPATH': str(RUNNER / 'shim'), 'TSDAF_JSON_SCHEMA_SHIM': '1', 'SHIM_UNFENCE': '1',
        'SHIM_REPLACE': str(PROMPTS / 'replace-final4.json'), 'SHIM_ADDENDA': json.dumps({'Reaction blocks. A reaction block runs from': str(PROMPTS / 'addenda' / 'record-o3.txt')}),
        'REFERENCED_PROCEDURES': '1', 'QUANTITY_OF_WHAT': '1', 'OUTCOME_NOTE': '1', 'DATA_MARKS': '1', 'GENERAL_MARKS': '1', 'TSDAF_NO_STREAM': '1',
        'TSDAF_WORKFLOW_SEGMENT_EFFORT': 'medium', 'TSDAF_WORKFLOW_RECORD_EFFORT': 'medium', 'TSDAF_WORKFLOW_PAPER_TASK_EFFORT': 'medium', 'TSDAF_WORKFLOW_MODEL': MODEL,
        'SI_TEXT': SI_TEXT, **({'NO_PROXY': '*', 'no_proxy': '*'} if os.environ.get('TSDAF_DIRECT') == '1' else {}),      # TSDAF_DIRECT=1: bypass a system proxy (the corpus node's was dead)
        'SHIM_REPLACE_STEPS': json.dumps({s: str(PROMPTS / 'v4' / f'replace-{s}.json') for s in ('segment', 'record', 'paper_task')}), 'SHIM_REPLACE_STRICT': '1'}
for k in ('TSDAF_EFFORT_MAP', 'RECORD_ONLY', 'HTTPS_PROXY', 'HTTP_PROXY', 'RECORD_PARALLEL'): base.pop(k, None)
# whole_paper.py's step 1 (segmentation), with the effort run_config gives it under TSDAF_WORKFLOW_SEGMENT_EFFORT=medium
SEG_ENV = {'PROBE_LANE': 'WINDOWS', 'PROBE_FIGURES': 'transcripts', 'PROBE_MEMORY': '1', 'PROBE_ROWS': '60', 'PROBE_SAFE': '1', 'PROBE_EFFORT': 'medium'}
UNITS, LOGS = HERE / 'corpus-units', HERE / 'chain-logs'; UNITS.mkdir(exist_ok=True); LOGS.mkdir(exist_ok=True); (HERE / 'whole-paper').mkdir(exist_ok=True); (HERE / 'thoughts').mkdir(exist_ok=True)
status_file = HERE / 'whole-paper' / f'{PREFIX}-r{rnd}-status.json'
status = json.loads(status_file.read_text(encoding='utf-8')) if status_file.exists() else {}
status['_settings'] = {'model': MODEL, 'gateway': base['OPENAI_BASE_URL'], 'credentials': CRED.name if CRED else 'process environment', 'budget': BUDGET, 'hard_cap': CAP, 'max_parallel': MAX_RP,
                       'seg_at_once': SEG_AT_ONCE, 'repairs': REPAIRS, 'si_text': SI_TEXT, 'prompts': base['SHIM_REPLACE_STEPS'], 'papers': len(papers)}
lock = threading.Lock()


def save():
    with lock: status_file.write_text(json.dumps(status, indent=1), encoding='utf-8')


def note(p, **kw):
    with lock: status.setdefault(p, {}).update(kw)
    save()


class Budget:
    """calls in flight; the recording papers are served in the order they ask, so a large one is not passed over by small ones"""
    def __init__(self, size): self.size, self.used, self.peak, self.cv, self.queue = size, 0, 0, threading.Condition(), []

    def take(self, n, fifo=True):
        n = min(n, self.size)
        with self.cv:
            me = object()
            if fifo: self.queue.append(me)
            while self.used + n > self.size or (fifo and self.queue[0] is not me): self.cv.wait()
            if fifo: self.queue.pop(0)
            self.used += n; self.peak = max(self.peak, self.used); self.cv.notify_all()
        return n

    def give(self, n):
        with self.cv: self.used -= n; self.cv.notify_all()


budget = Budget(BUDGET); seg_gate = threading.Semaphore(SEG_AT_ONCE); counts = {'segmenting': 0, 'recording': 0, 'done': 0, 'failed': 0}


def count(what, by):
    with lock: counts[what] += by


def run(cmd, env, log, hours):
    """a child and all of its children; stopped as a tree when it outlives its time"""
    with open(log, 'a', encoding='utf-8') as out:
        child = subprocess.Popen(cmd, env=env, stdout=out, stderr=subprocess.STDOUT, cwd=str(HERE), start_new_session=os.name != 'nt')
        try: return child.wait(timeout=hours * 3600)
        except subprocess.TimeoutExpired:
            if os.name == 'nt': subprocess.run(['taskkill', '/T', '/F', '/PID', str(child.pid)], capture_output=True)
            else:
                import signal; os.killpg(child.pid, signal.SIGKILL)
            return 'timeout'


def missing(rd):
    e, p = rd / 'entries' / 'results.json', rd / 'paper.json'
    if not (rd / 'result.json').exists() or not e.exists(): return True
    if any(not x.get('record') for x in json.loads(e.read_text(encoding='utf-8')).get('results') or []): return True
    return not p.exists() or json.loads(p.read_text(encoding='utf-8')).get('record') is None


def entries_of(units_file):
    return sum(1 for u in json.loads(units_file.read_text(encoding='utf-8'))['units'] if u['kind'] == 'PROCEDURE')


def paper_job(p):
    label = f'{PREFIX}-r{rnd}-{p}'; rd = HERE / 'whole-paper' / label; ud = UNITS / label; uf = ud / f'{p}.units.json'
    env = {**base, 'TSDAF_THOUGHT_LOG': str(HERE / 'thoughts' / f'{label}.jsonl')}
    if rd.exists() and not missing(rd): note(p, state='complete before this start'); return
    # 1 segmentation
    if not (rd / 'units').exists() and not uf.exists():
        with seg_gate:
            for attempt in range(1, 3):
                budget.take(1, fifo=False); count('segmenting', 1); t = time.time(); note(p, seg_started=time.strftime('%H:%M:%S'))
                code = run([sys.executable, str(TOOLS / 'si_units_probe.py'), p, str(ud)], {**env, **SEG_ENV}, LOGS / f'{label}.segment.out', 3)
                count('segmenting', -1); budget.give(1)
                note(p, seg_seconds=round(time.time() - t), seg_exit=code, seg_attempts=attempt)
                if uf.exists(): break
                time.sleep(60)
        if not uf.exists(): count('failed', 1); note(p, state='segmentation failed'); return
    # 2 record (and assemble), then repairs
    n = entries_of(uf if uf.exists() else rd / 'units' / f'{p}.units.json'); rp = max(1, min(n, MAX_RP)); note(p, entries=n, record_parallel=rp)
    for attempt in range(0, REPAIRS + 1):
        repair = rd.exists()
        if repair and not missing(rd): break
        if repair and not (rd / 'units').exists():
            aside = HERE / 'whole-paper' / '_stopped' / f'{label}-{int(time.time())}'; aside.parent.mkdir(exist_ok=True); rd.rename(aside); repair = False
        cmd = [sys.executable, str(TOOLS / 'whole_paper.py'), p, label, '--record', 'actions', '--si-text', SI_TEXT, '--model', MODEL] + (['--repair'] if repair else ['--reuse-units', str(ud)])
        took = budget.take(rp + 1); count('recording', 1); t = time.time()
        code = run(cmd, {**env, 'RECORD_PARALLEL': str(rp)}, LOGS / f'{label}.{"repair" if repair else "run"}.out', 4)
        count('recording', -1); budget.give(took)
        with lock: status.setdefault(p, {}).setdefault('passes', []).append({'how': 'repair' if repair else 'run', 'exit': code, 'seconds': round(time.time() - t),
                                                                             'ended': time.strftime('%H:%M:%S')})
        save()
        if not rd.exists() or not missing(rd): break
        time.sleep(120)
    left = not rd.exists() or missing(rd); count('failed' if left else 'done', 1); note(p, state='still missing' if left else 'complete')


def heartbeat(stop):
    while not stop.wait(60):
        with budget.cv: used, peak, waiting = budget.used, budget.peak, len(budget.queue)
        print(time.strftime('%H:%M:%S'), f'in flight {used}/{BUDGET} (peak {peak})', f"segmenting {counts['segmenting']}", f"recording {counts['recording']}",
              f'waiting {waiting}', f"done {counts['done']}", f"failed {counts['failed']}", f'of {len(papers)}', flush=True)
        with lock: status['_progress'] = {'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'in_flight': used, 'peak': peak, **counts}
        save()


si_size = lambda p: sum(1 for _ in open(FIX / (p + SI_TEXT) / 'normalized' / 'blocks.jsonl', encoding='utf-8'))
order = sorted(papers, key=si_size, reverse=True)
status['_first_started'] = status.get('_first_started') or status.get('_started') or time.strftime('%Y-%m-%d %H:%M:%S')
status['_started'] = time.strftime('%Y-%m-%d %H:%M:%S'); status.setdefault('_starts', []).append(status['_started'])
save(); t0 = time.time()      # a restart (resumable) keeps the first start, so the run's wall is counted from it
stop = threading.Event(); threading.Thread(target=heartbeat, args=(stop,), daemon=True).start()
with ThreadPoolExecutor(max_workers=len(order)) as pool: list(pool.map(paper_job, order))
stop.set(); status['_finished'] = time.strftime('%Y-%m-%d %H:%M:%S'); status['_wall_seconds'] = round(time.time() - t0)
status['_wall_seconds_from_first_start'] = round(time.time() - time.mktime(time.strptime(status['_first_started'], '%Y-%m-%d %H:%M:%S')))
status['_peak_in_flight'] = max(budget.peak, status.get('_peak_in_flight') or 0); save()
print('ALLDONE', time.strftime('%H:%M:%S'), f"done {counts['done']} failed {counts['failed']} peak {budget.peak} wall {round(time.time() - t0)} s", flush=True)
