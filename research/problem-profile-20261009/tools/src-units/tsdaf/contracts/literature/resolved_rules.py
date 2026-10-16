"""State and hash checks shared by the runtime and contract-pack verifier.

Typed JSON schemas own value types, including nullable values. These checks do not
interpret values or infer scientific support from other fields or source modality.
"""
import hashlib
import json

AUTHORITY_KEYS = ('state', 'value', 'reason_code', 'coverage_scope_ref', 'evidence_uses', 'candidates')


def resolved_digest(value, *, faithful_mapping=False):
    authority = {key: value[key] for key in AUTHORITY_KEYS}
    if faithful_mapping:
        for key in ('model_record_refs','model_submission_refs'):
            if key in value:
                authority[key] = value[key]
    return hashlib.sha256(json.dumps(authority, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def resolved_errors(value, *, core=True, faithful_mapping=False, nullable=False):
    errors = []
    if 'resolved_value_sha256' in value:
        if value['resolved_value_sha256'] != resolved_digest(value, faithful_mapping=faithful_mapping):
            errors.append('RESOLVED_VALUE_HASH_MISMATCH')
    state = value['state']
    if state == 'PRESENT':
        if (value['value'] is None and not nullable) or value['reason_code'] is not None or value['coverage_scope_ref'] is not None:
            errors.append('RESOLVED_VALUE_STATE')
        if core and not faithful_mapping and not any(u.get('role') == 'SUPPORT' for u in value['evidence_uses']):
            errors.append('CORE_EVIDENCE_REQUIRED')
    elif state == 'NOT_FOUND':
        if value['value'] is not None or value['reason_code'] is not None or (value['coverage_scope_ref'] is None and not faithful_mapping):
            errors.append('RESOLVED_VALUE_STATE')
    elif state == 'UNRESOLVED':
        if value['value'] is not None or value['reason_code'] is None or value['coverage_scope_ref'] is not None:
            errors.append('RESOLVED_VALUE_STATE')
    elif state == 'UNSTATED':
        if not faithful_mapping or any(value[k] is not None for k in ('value', 'reason_code', 'coverage_scope_ref')) or any(value.get(k) for k in ('evidence_uses', 'candidates', 'model_record_refs','model_submission_refs')):
            errors.append('RESOLVED_VALUE_STATE')
    return errors


def validate_declared_precedence(nodes, relations, *, before_key, after_key, label):
    edges = {node: set() for node in nodes}
    for relation in relations:
        before, after = relation[before_key], relation[after_key]
        if before not in edges or after not in edges:
            raise ValueError(f'{label} precedence must stay within its ' + ('operation' if label == 'stage' else 'experiment'))
        if before == after:
            raise ValueError(f'{label} precedence cannot point to itself')
        edges[before].add(after)
    indegree = {node: 0 for node in nodes}
    for targets in edges.values():
        for node in targets:
            indegree[node] += 1
    ready = [node for node, degree in indegree.items() if degree == 0]
    count = 0
    while ready:
        node = ready.pop()
        count += 1
        for target in edges[node]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if count != len(nodes):
        raise ValueError(f'{label} precedence contains a cycle')


class PublicIdentityRegistry:
    """One birth may occur in alternative views of the same declared owner.

    Identity equality is supplied, never derived from values. Repeating an ID in
    one view, another owner, or another object family remains an integrity error.
    """
    def __init__(self):
        self._seen = {}

    def define(self, identifier, *, exclusive_owner=None, branch=None):
        if identifier in self._seen:
            owner, branches = self._seen[identifier]
            if (exclusive_owner is None or exclusive_owner != owner
                    or branch is None or branch in branches):
                raise ValueError("duplicate paper-global public object identity")
            branches.add(branch)
        else:
            self._seen[identifier] = (exclusive_owner, {branch})


def material_occurrences(field):
    """Enumerate explicitly stored views without selecting an interpretation."""
    if isinstance(field.get("value"), list):
        yield ("VALUE", None), field["value"]
    for candidate in field.get("candidates", ()):
        if isinstance(candidate.get("value"), list):
            yield ("CANDIDATE", candidate["candidate_id"]), candidate["value"]
