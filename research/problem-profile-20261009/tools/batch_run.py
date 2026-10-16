"""The corpus run: every paper of a list through the whole flow, resumably, with a ledger that says where each paper stands.

Per paper, one stage after another:
  prepare   prestage_paper.py   the packages, the figures cut from the PDF, boxed by the detector and read with their marks
  owners    page_owners.py      whose characterization each page without running text is (the assembly keeps those pages, rendered)
  prestage  prestage_resolve.py the SI text settled against the PDF text layer (written as <paper>__clean<suffix>)
  chain     whole_paper.py      segmentation, records, the paper task, assembly, and the structure step (structure-binding.json)

What a corpus of this size needs:
  - resume: a stage that finished is not run again (its exit was 0 and what it writes is there), so a rerun costs nothing;
  - retries: a stage that failed is tried again, up to --tries times, with a growing pause; its last lines are kept in the ledger;
  - a ledger: batch/<name>/status.json, per paper and stage: state, exit, seconds, tries, the end of the log, and for the chain what came out;
  - a report: --report prints the ledger without running anything;
  - models: from tools/run_config.json (the workflow and vision switches); --workflow-model overrides the workflow model for this batch.

Papers run --papers-at-once at a time (batch/<name>/at_once.txt, which can be changed while the run goes on); several runs of one batch
may go at once over different papers, each writing only its own papers into the ledger; inside a paper the stages run one after another, and the model calls inside a stage already run
concurrently. Credentials (D:\\tsdaf-lpz.env, or the file TSDAF_CREDENTIALS names) and the detector settings (D:\\tsdaf\\det\\detector.env) go into the children only.

usage: batch_run.py <name> <paper list file, one package id per line> [--papers-at-once 3] [--tries 3] [--suffix _b]
                    [--workflow-model <model>] [--report] [--plan]"""
import json, os, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
TOOLS = Path(__file__).resolve().parent; HERE = TOOLS.parent; FIX = HERE.parents[1] / 'tests/fixtures'
sys.path.insert(0, str(TOOLS)); import run_config
option = lambda name, default: sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
name, listing = sys.argv[1], Path(sys.argv[2])
AT_ONCE, TRIES, SUFFIX, WORKFLOW = int(option('--papers-at-once', '3')), int(option('--tries', '3')), option('--suffix', '_b'), option('--workflow-model', None)
papers = [line.strip() for line in listing.read_text(encoding='utf-8-sig').splitlines() if line.strip() and not line.startswith('#')]
root = HERE / 'batch' / name; logs = root / 'logs'; logs.mkdir(parents=True, exist_ok=True)
ledger_file = root / 'status.json'; lock = threading.Lock()
ledger = json.loads(ledger_file.read_text(encoding='utf-8')) if ledger_file.exists() else {}
STAGES = ('prepare', 'owners', 'prestage', 'chain')


RUN_ID = time.strftime('%Y%m%d-%H%M%S') + f'-{os.getpid()}'


def save():
    """Write this run's papers into the ledger. Several runs of one batch may go at once over different papers: each reads the ledger
    as it is on disk, under a lock file, and writes back only its own papers' entries and its own run record."""
    with lock:
        guard = root / 'status.lock'
        for _ in range(600):
            try: os.close(os.open(guard, os.O_CREAT | os.O_EXCL)); break
            except FileExistsError:
                if time.time() - guard.stat().st_mtime > 120: guard.unlink(missing_ok=True)      # left by a run that was killed
                time.sleep(0.2)
        try:
            on_disk = json.loads(ledger_file.read_text(encoding='utf-8')) if ledger_file.exists() else {}
            on_disk.update({p: ledger[p] for p in papers if p in ledger})
            if '_batch' in ledger: on_disk.setdefault('_runs', {})[RUN_ID] = ledger['_batch']; on_disk['_batch'] = ledger['_batch']
            tmp = ledger_file.with_suffix(f'.{os.getpid()}.tmp'); tmp.write_text(json.dumps(on_disk, ensure_ascii=False, indent=1), encoding='utf-8'); tmp.replace(ledger_file)
        finally: guard.unlink(missing_ok=True)


def at_once():
    """How many papers may run at once: batch/<name>/at_once.txt, read again before each paper starts, so that it can be lowered (a
    rate limit) or raised while the run goes on; --papers-at-once writes it when a run starts and it is not there yet."""
    try: return max(1, int((root / 'at_once.txt').read_text().strip()))
    except (OSError, ValueError): return AT_ONCE


gate, running = threading.Condition(), [0]


def label(paper): return f'{name}-{paper}'


def outputs_there(paper, stage):
    """What a finished stage leaves behind, so that a stage marked done is also really done."""
    if stage == 'prepare': return (HERE / 'figure-labels' / f'{paper}.labels.json').exists() and (FIX / 'si_packages' / paper).exists()
    if stage == 'owners': return (HERE / 'figure-labels' / f'{paper}.owners.json').exists()
    if stage == 'prestage': return (FIX / 'si_packages' / f'{paper}__clean{SUFFIX}').exists() and (HERE / ('prestage' + SUFFIX) / paper / 'report.json').exists()
    return (HERE / 'whole-paper' / label(paper) / 'result.json').exists()


def command(paper, stage):
    if stage == 'prepare': return [sys.executable, str(TOOLS / 'prestage_paper.py'), paper]
    if stage == 'owners': return [sys.executable, str(TOOLS / 'page_owners.py'), paper]
    if stage == 'prestage': return [sys.executable, str(TOOLS / 'prestage_resolve.py'), paper, '--agree-check', '--parallel', '16', '--suffix', SUFFIX]
    # a chain that has a result is repaired in place: only what is missing is asked again
    return [sys.executable, str(TOOLS / 'whole_paper.py'), paper, label(paper), '--record', 'actions', '--si-text', '__clean' + SUFFIX] + (['--repair'] if outputs_there(paper, 'chain') else [])


def gaps(paper):
    """Entries of a chain that have no record because their calls failed (an entry the model judged not to be a preparation has one)."""
    try:
        recorded = json.loads((HERE / 'whole-paper' / label(paper) / 'entries/results.json').read_text(encoding='utf-8'))
        refused = {c['what'].removeprefix('entry ') for c in recorded.get('calls') or [] if (c.get('error') or '').startswith('Refused')}
        return sum(1 for r in recorded['results'] if not r.get('record') and str(r['n']) not in refused)      # a refusal by the provider is final
    except (OSError, ValueError, KeyError): return 0


def chain_summary(paper):
    result = json.loads((HERE / 'whole-paper' / label(paper) / 'result.json').read_text(encoding='utf-8'))
    blocks = result['reaction_blocks']; structures = (result.get('structures') or {}).get('summary') or {}
    return {'reaction blocks': len(blocks), 'without a record': sum(b['record'] is None for b in blocks), 'compounds': len((result.get('paper_record') or {}).get('compounds') or []),
            'characterization records': len(result.get('characterization_records') or []), 'of them pictures': sum(f.get('data_kind') in ('FIGURE', 'PAGE') for f in result.get('characterization_records') or []), 'author disagreements': len(result.get('author_disagreements') or []),
            'unresolved': len(result.get('unresolved') or []), 'drawings the result needs': structures.get('drawings the result needs'),
            'failed recording tries': (result.get('tokens') or {}).get('entry tasks', {}).get('failed_tries'), 'wall seconds': result.get('wall_seconds'), 'models': result.get('models')}


def env():
    def read(path, names=None):
        out = {}
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            key, sep, value = line.strip().removeprefix('export ').partition('=')
            if sep and (names is None or key.strip() in names): out[key.strip()] = value.strip().strip('"').strip("'")
        return out
    extra = {'TSDAF_WORKFLOW_MODEL': WORKFLOW} if WORKFLOW else {}
    return {**os.environ, **read(os.environ.get('TSDAF_CREDENTIALS', r'D:\tsdaf-lpz.env'), ('OPENAI_BASE_URL', 'OPENAI_API_KEY')), **read(r'D:\tsdaf\det\detector.env'), **extra,
            'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1'}


def run_paper(paper):
    with gate:
        while running[0] >= at_once(): gate.wait(15)
        running[0] += 1
    try: run_stages(paper)
    finally:
        with gate: running[0] -= 1; gate.notify_all()


def run_stages(paper):
    entry = ledger.setdefault(paper, {})
    for stage in STAGES:
        state = entry.setdefault(stage, {'state': 'waiting', 'tries': 0})
        if state.get('state') == 'done' and outputs_there(paper, stage) and not (stage == 'chain' and gaps(paper)): continue
        if state.get('state') in ('failed', 'running', 'gaps'):      # a new run of the batch tries a stage that failed (or was cut off) afresh
            state['earlier_tries'] = state.get('earlier_tries', 0) + state['tries']; state['tries'] = 0
        if stage == 'chain' and (HERE / 'whole-paper' / label(paper)).exists() and not outputs_there(paper, stage):
            # a chain that stopped half way leaves its directory; it is kept beside, not overwritten
            (HERE / 'whole-paper' / label(paper)).rename(HERE / 'whole-paper' / f'{label(paper)}.stopped-{time.strftime("%Y%m%d-%H%M%S")}')
        while state['tries'] < TRIES:
            state.update(state='running', tries=state['tries'] + 1, started=time.strftime('%Y-%m-%d %H:%M:%S')); save()
            log = logs / f'{paper}.{stage}.{state["tries"]}.log'; t = time.time()
            with open(log, 'w', encoding='utf-8') as out:
                code = subprocess.run(command(paper, stage), env=ENV, stdout=out, stderr=subprocess.STDOUT, cwd=str(HERE), creationflags=FLAGS).returncode
            tail = log.read_text(encoding='utf-8', errors='replace').strip().splitlines()[-3:]
            state.update(exit=code, seconds=round(time.time() - t), log=log.name, tail=tail)
            if code == 0 and outputs_there(paper, stage):
                missing = gaps(paper) if stage == 'chain' else 0
                state['state'] = 'done' if not missing else 'gaps'; state['gaps'] = missing
                if stage == 'chain': state['summary'] = chain_summary(paper)
                save()
                if not missing: break
                time.sleep(60 * state['tries']); continue      # entries left without a record: the next try repairs the chain in place
            state['state'] = 'failed'; save()
            if stage == 'chain' and (HERE / 'whole-paper' / label(paper)).exists() and not outputs_there(paper, stage):
                (HERE / 'whole-paper' / label(paper)).rename(HERE / 'whole-paper' / f'{label(paper)}.failed-{state["tries"]}-{time.strftime("%Y%m%d-%H%M%S")}')      # never the name of an earlier run's
            time.sleep(60 * state['tries'])           # the gateway's limits pass in minutes
        if state['state'] not in ('done', 'gaps'): return       # the later stages need this one


def report():
    if ledger_file.exists(): ledger.update(json.loads(ledger_file.read_text(encoding='utf-8')))      # every run of the batch, not only this one
    counts = {stage: {} for stage in STAGES}
    for paper in papers:
        for stage in STAGES:
            s = (ledger.get(paper) or {}).get(stage, {}).get('state', 'waiting'); counts[stage][s] = counts[stage].get(s, 0) + 1
    print(f'batch {name}: {len(papers)} papers | workflow {WORKFLOW or run_config.model("workflow")}' + (' (this batch)' if WORKFLOW else ' (run_config)') + f', vision {run_config.model("vision")}')
    for stage in STAGES: print(f'  {stage:9s}', counts[stage])
    failed = [(p, st, (ledger[p][st].get('tail') or [''])[-1][:120]) for p in papers for st in STAGES if (ledger.get(p) or {}).get(st, {}).get('state') == 'failed']
    for p, st, tail in failed: print(f'  FAILED {p} {st}: {tail}')
    done = [ledger[p]['chain']['summary'] for p in papers if (ledger.get(p) or {}).get('chain', {}).get('state') == 'done' and ledger[p]['chain'].get('summary')]
    if done:
        walls = sorted(s['wall seconds'] or 0 for s in done)
        print(f"  chains done {len(done)}: median wall {walls[len(walls) // 2]:.0f}s | reaction blocks {sum(s['reaction blocks'] for s in done)} | drawings the results need {sum(s['drawings the result needs'] or 0 for s in done)}")


ENV, FLAGS = None, getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
if '--report' in sys.argv: report(); raise SystemExit
if '--plan' in sys.argv:
    for paper in papers:
        todo = [st for st in STAGES if not ((ledger.get(paper) or {}).get(st, {}).get('state') == 'done' and outputs_there(paper, st))]
        print(paper, 'to run:', todo or 'nothing')
    raise SystemExit
ENV = env()
ledger.setdefault('_batch', {}).update(started=time.strftime('%Y-%m-%d %H:%M:%S'), papers=len(papers), suffix=SUFFIX, models=run_config.summary(), workflow_override=WORKFLOW); save()
if not (root / 'at_once.txt').exists(): (root / 'at_once.txt').write_text(str(AT_ONCE))      # a second run of the batch (a few papers to repair) does not change the limit the first one runs under
with ThreadPoolExecutor(max_workers=min(len(papers), 64)) as pool: list(pool.map(run_paper, papers))
ledger['_batch']['finished'] = time.strftime('%Y-%m-%d %H:%M:%S'); save()
report()
