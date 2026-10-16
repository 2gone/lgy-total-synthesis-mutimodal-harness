"""Which model and which thinking level a step uses, from run_config.json beside this file -- the one place they are set.
    run_config.model('workflow' | 'vision' | 'ocsr')        the group's model (an environment override wins: TSDAF_<GROUP>_MODEL)
    run_config.effort('workflow' | 'vision', step)          the thinking level of one step
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
    value = CONFIG[group]['effort']
    return value if step is None else value[step]


def summary():
    return {group: {'model': model(group), 'effort': CONFIG[group]['effort']} for group in ('workflow', 'vision', 'ocsr')}
