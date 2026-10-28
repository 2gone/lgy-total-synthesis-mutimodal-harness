"""Which model and which thinking level a step uses, from run_config.json beside this file -- the one place they are set.
    run_config.model('workflow' | 'vision' | 'ocsr')        the group's model (an environment override wins: TSDAF_<GROUP>_MODEL)
    run_config.effort('workflow' | 'vision', step)          the thinking level of one step (an environment override wins: TSDAF_<GROUP>_<STEP>_EFFORT)
    run_config.effort('ocsr')                               the OCSR thinking level
    run_config.summary()                                    what a run records about the models it used"""
import json, os
from pathlib import Path

CONFIG = json.loads((Path(__file__).resolve().parent / 'run_config.json').read_text(encoding='utf-8'))
# TSDAF_MODEL was the chain's single switch before 2026-10-15; it still means the workflow model
_LEGACY = {'workflow': 'TSDAF_MODEL'}


def model(group):
    return os.environ.get(f'TSDAF_{group.upper()}_MODEL') or (os.environ.get(_LEGACY[group]) if group in _LEGACY else None) or CONFIG[group]['model']


def effort(group, step=None):
    value = CONFIG[group]['effort']; value = value if step is None else value[step]
    # TSDAF_WORKFLOW_RECORD_EFFORT=low: one step's level for one run (2026-10-20: the record step of DeepSeek at low, the others as set)
    if step is not None and os.environ.get(f'TSDAF_{group.upper()}_{step.upper()}_EFFORT'): return os.environ[f'TSDAF_{group.upper()}_{step.upper()}_EFFORT']
    # TSDAF_EFFORT_MAP: a model that has no such level takes the one named for it ('{"medium": "high"}'), for that run only
    return json.loads(os.environ['TSDAF_EFFORT_MAP']).get(value, value) if os.environ.get('TSDAF_EFFORT_MAP') and isinstance(value, str) else value


def summary():
    # the levels the run actually used (an override or a map applied), step by step
    used = lambda group: {k: effort(group, k) for k in CONFIG[group]['effort'] if not k.startswith('_')} if isinstance(CONFIG[group]['effort'], dict) else effort(group)
    return {group: {'model': model(group), 'effort': used(group)} for group in ('workflow', 'vision', 'ocsr')}
