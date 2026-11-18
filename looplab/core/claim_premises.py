"""Explicit logical dependencies and authored evidence reviews, never inherited scientific verdicts."""
from __future__ import annotations

import json
import math
import re

from looplab.core.jsonutil import canonical_json_digest
from looplab.core.redact import redact_persisted_text

_MAX_PREMISES = 4
_MAX_EVIDENCE = 16
_MAX_REVIEWS = 4096
_SHA = re.compile('[0-9a-f]{64}')


def _id(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError('invalid_claim_id')
    return value


def _sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _count(value):
    return type(value) is int and 0 <= value <= (1 << 63) - 1


def _subject(card):
    if card is None or getattr(card, 'merged_into', None):
        raise ValueError('claim_not_found_or_merged')
    return {'id': _id(card.id), 'seed_sha256': canonical_json_digest(card.seed_statement)}


def _node_stamp(state, card, node_id):
    node = state.nodes.get(node_id) if _count(node_id) else None
    if (node is None or node_id not in card.evidence or node_id in card.substituted_nodes
            or node_id in state.aborted_nodes or node_id in state.breed_excluded
            or node.status != 'evaluated' or not node.feasible
            or type(node.metric) not in (int, float) or not math.isfinite(node.metric)
            or not _count(node.attempt) or not _count(node.terminal_event_seq)):
        raise ValueError('claim_review_requires_own_usable_native_measurement')
    content = {'node_id': node.id, 'generation': node.attempt, 'terminal_seq': node.terminal_event_seq,
               'creation_seq': node.creation_event_seq, 'metric': node.metric, 'parent_ids': node.parent_ids,
               'source_sha256': canonical_json_digest({'code': node.code, 'files': node.files}),
               'metric_provenance': node.metric_provenance,
               'objective': {'direction': state.direction, 'key': state.objective_key}}
    return {'node_id': node.id, 'generation': node.attempt, 'terminal_seq': node.terminal_event_seq,
            'receipt_sha256': canonical_json_digest(content)}


def valid_record(value):
    """Strict bounded native reader. Unknown latest records cannot resurrect an older review."""
    if (not isinstance(value, dict) or set(value) != {'version', 'subject', 'premises', 'assessment', 'previous_sha256'}
            or type(value.get('version')) is not int or value['version'] != 1 or not _sha(value.get('previous_sha256'))):
        return False
    def subject(row):
        try:
            return isinstance(row, dict) and set(row) == {'id', 'seed_sha256'} and _id(row.get('id')) and _sha(row.get('seed_sha256'))
        except ValueError:
            return False
    if not subject(value.get('subject')) or not isinstance(value.get('premises'), list) or len(value['premises']) > _MAX_PREMISES:
        return False
    if not all(subject(row) for row in value['premises']) or len({row['id'] for row in value['premises']}) != len(value['premises']):
        return False
    if value['subject']['id'] in {row['id'] for row in value['premises']}:
        return False
    assessment = value['assessment']
    if assessment is not None:
        if (not isinstance(assessment, dict) or set(assessment) != {'judgment', 'reason', 'evidence'}
                or assessment.get('judgment') not in ('refuted', 'inconclusive')
                or not isinstance(assessment.get('reason'), str) or not assessment['reason'].strip() or len(assessment['reason']) > 512
                or not isinstance(assessment.get('evidence'), list) or not 1 <= len(assessment['evidence']) <= _MAX_EVIDENCE):
            return False
        evidence_ids = set()
        for row in assessment['evidence']:
            if (not isinstance(row, dict) or set(row) != {'node_id', 'generation', 'terminal_seq', 'receipt_sha256'}
                    or not all(_count(row.get(key)) for key in ('node_id', 'generation', 'terminal_seq'))
                    or row['node_id'] in evidence_ids or not _sha(row.get('receipt_sha256'))):
                return False
            evidence_ids.add(row['node_id'])
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()) <= 16384
    except (ValueError, TypeError, UnicodeError):
        return False


def _cycle_nodes(graph):
    """Bounded iterative DFS; a deep imported chain cannot consume Python's call stack."""
    color, cycles = {}, set()
    for start in graph:
        if color.get(start):
            continue
        path, positions = [start], {start: 0}
        stack = [(start, iter(graph.get(start, ())))]
        color[start] = 1
        while stack:
            key, edges = stack[-1]
            child = next(edges, None)
            if child is None:
                stack.pop(); path.pop(); positions.pop(key); color[key] = 2
            elif color.get(child) == 1:
                cycles.update(path[positions[child]:])
            elif not color.get(child):
                color[child] = 1; positions[child] = len(path); path.append(child)
                stack.append((child, iter(graph.get(child, ()))))
    return cycles


def build_review(state, data):
    if not isinstance(data, dict) or set(data) != {'id', 'premise_ids', 'assessment'}:
        raise ValueError('claim_review_requires_exact_authored_fields')
    subject = _subject(state.cards.get(_id(data['id'])))
    premises = data['premise_ids']
    if (not isinstance(premises, list) or len(premises) > _MAX_PREMISES
            or any(not isinstance(key, str) for key in premises) or len(set(premises)) != len(premises)):
        raise ValueError('invalid_claim_premises')
    refs = [_subject(state.cards.get(_id(key))) for key in premises]
    if subject['id'] in premises:
        raise ValueError('claim_dependency_cycle')
    # These are LOGICAL edges; parent_card_id is a research filing and is never read here.
    graph = {}
    reviews = state.claim_reviews
    if subject['id'] not in reviews and len(reviews) >= _MAX_REVIEWS:
        raise ValueError('claim_review_capacity')
    for key, row in reviews.items():
        if key == subject['id']:
            continue
        value = row.get('review')
        if not valid_record(value):
            raise ValueError('claim_review_history_unavailable')
        graph[key] = [ref['id'] for ref in value['premises']]
    graph[subject['id']] = list(premises)
    if _cycle_nodes(graph):
        raise ValueError('claim_dependency_cycle')
    assessment = data['assessment']
    if assessment is not None:
        if (not isinstance(assessment, dict) or set(assessment) != {'judgment', 'reason', 'node_ids'}
                or assessment.get('judgment') not in ('refuted', 'inconclusive')
                or not isinstance(assessment.get('reason'), str) or not assessment['reason'].strip() or len(assessment['reason']) > 512):
            raise ValueError('invalid_authored_claim_assessment')
        node_ids = assessment['node_ids']
        if (not isinstance(node_ids, list) or not 1 <= len(node_ids) <= _MAX_EVIDENCE
                or any(not _count(key) for key in node_ids) or len(set(node_ids)) != len(node_ids)):
            raise ValueError('invalid_claim_evidence_ids')
        assessment = {'judgment': assessment['judgment'], 'reason': redact_persisted_text(
            assessment['reason'].strip(), max_chars=512, entropy=False, single_line=False),
            'evidence': [_node_stamp(state, state.cards[subject['id']], key) for key in node_ids]}
    review = {'version': 1, 'subject': subject, 'premises': refs, 'assessment': assessment,
              'previous_sha256': canonical_json_digest(reviews.get(subject['id']))}
    if not valid_record(review):
        raise ValueError('invalid_claim_review')
    return review


def authored_input(review):
    return {'id': review['subject']['id'], 'premise_ids': [row['id'] for row in review['premises']],
            'assessment': None if review['assessment'] is None else {
                'judgment': review['assessment']['judgment'], 'reason': review['assessment']['reason'],
                'node_ids': [row['node_id'] for row in review['assessment']['evidence']]}}


def review_is_current(state, value):
    if not valid_record(value):
        return False
    try:
        if _subject(state.cards.get(value['subject']['id'])) != value['subject']:
            return False
        if [_subject(state.cards.get(row['id'])) for row in value['premises']] != value['premises']:
            return False
        assessment = value['assessment']
        if assessment is not None:
            return [_node_stamp(state, state.cards[value['subject']['id']], row['node_id'])
                    for row in assessment['evidence']] == assessment['evidence']
        return True
    except (ValueError, TypeError):
        return False


def apply_undercut(state):
    """Pure advisory overlay after Card derivation: no verdict, measurement, rank or parent change."""
    current = {}
    for key, row in state.claim_reviews.items():
        card = state.cards.get(key)
        if card is None:
            continue
        value = row.get('review')
        good = valid_record(value)
        fresh = good and review_is_current(state, value)
        card.premise_review = {'state': 'current' if fresh else 'stale' if good else 'unavailable',
            'review_seq': row['seq'], 'actor_kind': row.get('actor_kind', 'unknown'),
            'authority': 'authored_interpretation', 'verdict_changed': False,
            'premises': value['premises'] if good else None,
            'assessment': value['assessment'] if good else None, 'premise_status': [], 'undercut': []}
        if fresh:
            current[key] = (card, value, row)
    cycles = _cycle_nodes({key: [ref['id'] for ref in value['premises']]
                           for key, (_, value, _) in current.items()})
    for key in cycles:
        card, _, _ = current.pop(key)
        card.premise_review.update(state='unavailable', premises=None, assessment=None)
    for key, (card, value, row) in current.items():
        for ref in value['premises']:
            premise = current.get(ref['id'])
            observed = state.cards.get(ref['id'])
            review = getattr(observed, 'premise_review', None)
            card.premise_review['premise_status'].append({'premise_id': ref['id'],
                'state': review['state'] if isinstance(review, dict) else 'not_reviewed',
                'review_seq': review['review_seq'] if isinstance(review, dict) else None,
                'assessment': review['assessment']['judgment'] if isinstance(review, dict)
                    and review['state'] == 'current' and isinstance(review['assessment'], dict) else None})
            if premise is None:
                continue
            _, assessed, native = premise
            assessment = assessed['assessment']
            if assessment is not None and assessment['judgment'] == 'refuted':
                card.premise_review['undercut'].append({'premise_id': ref['id'], 'review_seq': native['seq'],
                    'reason': assessment['reason'], 'node_ids': [row['node_id'] for row in assessment['evidence']],
                    'meaning': 'Explicit logical premise has a current authored refutation review; reassess this explanation. The child may remain true for another reason; its measurements and verdict are unchanged.'})


def valid_public_view(value):
    """Keep the optional Card wire notice whole and closed; never truncate its scientific meaning."""
    if (not isinstance(value, dict) or set(value) != {'state', 'review_seq', 'actor_kind', 'authority',
            'verdict_changed', 'premises', 'assessment', 'premise_status', 'undercut'}
            or value.get('state') not in ('current', 'stale', 'unavailable') or not _count(value.get('review_seq'))
            or value.get('actor_kind') not in ('local_operator', 'deployment_owner', 'harness_agent', 'unknown')
            or value.get('authority') != 'authored_interpretation' or value.get('verdict_changed') is not False):
        return False
    if value['state'] == 'unavailable':
        return value['premises'] is None and value['assessment'] is None and value['premise_status'] == [] and value['undercut'] == []
    test = {'version': 1, 'subject': {'id': 'shape-only', 'seed_sha256': '0' * 64},
            'premises': value['premises'], 'assessment': value['assessment'], 'previous_sha256': '0' * 64}
    if not valid_record(test):
        return False
    for field in ('premise_status', 'undercut'):
        if not isinstance(value[field], list) or len(value[field]) > _MAX_PREMISES:
            return False
    try:
        for row in value['premise_status']:
            if (not isinstance(row, dict) or set(row) != {'premise_id', 'state', 'review_seq', 'assessment'}
                    or not _id(row['premise_id']) or row['state'] not in ('current', 'stale', 'unavailable', 'not_reviewed')
                    or (row['review_seq'] is not None and not _count(row['review_seq']))
                    or row['assessment'] not in (None, 'refuted', 'inconclusive')):
                return False
        for row in value['undercut']:
            if (not isinstance(row, dict) or set(row) != {'premise_id', 'review_seq', 'reason', 'node_ids', 'meaning'}
                    or not _id(row['premise_id']) or not _count(row['review_seq'])
                    or not isinstance(row['reason'], str) or not row['reason'].strip() or len(row['reason']) > 512
                    or not isinstance(row['node_ids'], list) or not 1 <= len(row['node_ids']) <= _MAX_EVIDENCE
                    or any(not _count(key) for key in row['node_ids'])
                    or not isinstance(row['meaning'], str) or len(row['meaning']) > 512):
                return False
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()) <= 16384
    except (ValueError, TypeError, UnicodeError):
        return False
