"""The whole flow for a set of standard paper packages, in one command (2026-10-28): what the corpus runs of October 2026 did, step by
step, with the same tools, prompts and settings.

  1 import     tools/import_ready200.py   each paper's main text and SI packages into tests/fixtures/ (main package + one experimental
                                          document: the SI documents one after another, the main text last, copies left out)
  2 front-end  tools/batch_run.py         figures cut and read (prepare), pages without running text assigned (owners), the SI text settled
                                          against the PDF text layer (prestage); --papers-at-once papers at a time, up to 16 calls each
  3 chain      runner/gm_corpus.py        segmentation, entry records, paper task, assembly, structure step; --budget calls in flight at most
  4 stats      runner/corpus_stats.py     calls, tokens (input, cached, answer, thinking) and time per step and per paper

Every step resumes: a paper or stage that finished is not run again; run the same command again after an interruption.
Credentials only from the process environment (OPENAI_BASE_URL, OPENAI_API_KEY: any OpenAI-compatible gateway that accepts
reasoning_effort and image input). Without a molecule detector (TSDAF_DETECTOR_ENV) the figures are read without boxes.

usage: run_pipeline.py --packages <package set: INDEX.csv + packages/> --name <run name>
                       [--model gemini-3.8-flash-high] [--vision-model gemini-3.8-flash-high]
                       [--papers-at-once 4] [--budget 60] [--hard-cap 70] [--max-parallel 32]
                       [--only import,front,chain,stats]"""
import os, subprocess, sys
from pathlib import Path
RUNNER = Path(__file__).resolve().parent; HERE = RUNNER.parent; TOOLS = HERE / 'tools'
opt = lambda n, d=None: sys.argv[sys.argv.index(n) + 1] if n in sys.argv else d
PACKAGES, NAME = opt('--packages'), opt('--name')
if not PACKAGES or not NAME: raise SystemExit(__doc__)
STEPS = opt('--only', 'import,front,chain,stats').split(',')
MODEL = opt('--model', os.environ.get('TSDAF_WORKFLOW_MODEL', 'gemini-3.8-flash-high'))
VISION = opt('--vision-model', os.environ.get('TSDAF_VISION_MODEL', MODEL))
AT_ONCE, BUDGET, CAP, MAX_RP = opt('--papers-at-once', '4'), opt('--budget', '60'), opt('--hard-cap', '70'), opt('--max-parallel', '32')
if not (os.environ.get('OPENAI_BASE_URL') and os.environ.get('OPENAI_API_KEY')): raise SystemExit('set OPENAI_BASE_URL and OPENAI_API_KEY in the environment')
env = {**os.environ, 'TSDAF_WORKFLOW_MODEL': MODEL, 'TSDAF_VISION_MODEL': VISION, 'PYTHONUTF8': '1'}
papers_file = HERE / 'batch' / f'{NAME}.txt'


def step(title, cmd):
    print(f'== {title}: {" ".join(str(c) for c in cmd)}', flush=True)
    code = subprocess.run([str(c) for c in cmd], env=env, cwd=str(HERE)).returncode
    if code != 0: raise SystemExit(f'{title} stopped with exit {code}; run the same command again to resume')


if 'import' in STEPS:
    step('1 import', [sys.executable, TOOLS / 'import_ready200.py', '--ready', PACKAGES, '--name', NAME, '--id-suffix', ''])
if not papers_file.exists(): raise SystemExit(f'{papers_file} is missing: run the import step first')
if 'front' in STEPS:
    step('2 front-end', [sys.executable, TOOLS / 'batch_run.py', NAME, papers_file, '--papers-at-once', AT_ONCE, '--tries', '3', '--suffix', '_b',
                         '--stages', 'prepare,owners,prestage', '--thought-shim', RUNNER / 'shim'])
if 'chain' in STEPS:
    step('3 chain', [sys.executable, RUNNER / 'gm_corpus.py', NAME, '--papers-file', papers_file, '--model', MODEL, '--budget', BUDGET, '--hard-cap', CAP,
                     '--max-parallel', MAX_RP, '--seg-at-once', BUDGET, '--si-text', '__clean_b', '--prefix', 'chain'])
if 'stats' in STEPS:
    step('4 stats', [sys.executable, RUNNER / 'corpus_stats.py', '--round', NAME, '--prefix', 'chain', '--batch', NAME, '--out', HERE / f'stats-{NAME}.json'])
print(f'done: results in whole-paper/chain-r{NAME}-<paper>/result.json, statistics in stats-{NAME}.json')
