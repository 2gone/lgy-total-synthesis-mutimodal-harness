"""Stages worked out by the host from a block's action list. The model lists what was done, in order, and says nothing about where a
stage begins; a principle applied here groups the actions. Changing the principle changes no model call.
  addition  a stage is what is put in at one point with what holds until the next thing is put in (the reviewed standard's reading)
  content   a stage ends only when what is in the vessel changes (something goes in or comes out)
  action    every action is a stage"""
import re

REACTION = {'CHARGE', 'ADD', 'REMOVE', 'CONDITION', 'ATMOSPHERE', 'OTHER'}
_KIND = {'S': 'REACTION_STAGE', 'W': 'WORKUP', 'P': 'PURIFICATION'}
_said = lambda q: re.sub(r'\s+', '', (q.get('raw') or q.get('ref') or '')).lower()


def _group(action): return 'W' if action['kind'] in ('QUENCH', 'WORKUP') else 'P' if action['kind'] == 'PURIFY' else 'S'


def _opens(action, stage, principle, before):
    """Does this reaction action open a new stage after `stage` (the actions gathered so far)? `before` is every earlier action of the block."""
    if action.get('with_previous'): return False            # named together with the one before: their order is not known
    if principle == 'action': return True
    if action['kind'] in ('CHARGE', 'ADD', 'REMOVE'): return True
    if action['kind'] in ('ATMOSPHERE', 'OTHER') or principle == 'content': return False
    # CONDITION under 'addition': what is held right after an addition is that addition's. A move to a temperature other than the one
    # last stated, once the stage has already held something, is a change the authors tell as a step of its own.
    new = [_said(c['quantity']) for c in action['conditions'] if c['kind'] == 'TEMPERATURE']
    stated = [_said(c['quantity']) for a in before for c in a['conditions'] if c['kind'] == 'TEMPERATURE']
    held = any(c['kind'] in ('TEMPERATURE', 'DURATION') for a in stage for c in a['conditions'])
    return held and bool(new) and (not stated or new[0] != stated[-1])


def derive(block, principle='addition'):
    """The block's actions as steps (REACTION_STAGE / WORKUP / PURIFICATION), each holding the materials and conditions of its actions."""
    groups, actions = [], block.get('actions') or []
    for index, action in enumerate(actions):
        g = _group(action); last = groups[-1] if groups else None
        # Only putting something in opens reaction work again. What merely continues after a workup or a purification (the extracts concentrated,
        # a remark on what was isolated) is still that workup or purification, whatever kind the action was given.
        if g == 'S' and last is not None and last['g'] in 'WP' and action['kind'] not in ('CHARGE', 'ADD'): g = last['g']
        if g == 'W' and last is not None and last['g'] == 'P' and action['kind'] == 'WORKUP': g = 'P'      # the purified fractions concentrated or dried
        if last is None or last['g'] != g or (g == 'S' and (action.get('vessel') != last['actions'][-1].get('vessel') or _opens(action, last['actions'], principle, actions[:index]))):
            groups.append({'g': g, 'actions': [], 'index': []}); last = groups[-1]
        last['actions'].append(action); last['index'].append(index)
    return [{'kind': _KIND[g['g']], 'what_happens': '; '.join(a['what'] for a in g['actions']), 'vessel': g['actions'][0].get('vessel'), 'actions': g['index'],
             'materials': [m for a in g['actions'] for m in a['materials']], 'conditions': [c for a in g['actions'] for c in a['conditions']]} for g in groups]
