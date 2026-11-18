"""Selected-peer advisory work visibility over native checkpoints, never a distributed lease."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import HTTPException

from looplab.core.external_work_state import context_digest
from looplab.core.work_intents import normalize_intents, validate_direction_refs
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.harness.journals import read_event_source
from looplab.core.run_ref import resolve_run_address

_SHA = re.compile(r'[0-9a-f]{64}')


def _last_record(events):
    for event in reversed(events):
        if event.type != 'agent_checkpointed':
            continue
        body = event.data
        if 'work_intents' in body:
            return event, body['work_intents']
        work = body.get('external_work_state')
        if isinstance(work, dict) and 'work_intents' in work:
            return event, {**{key: work.get(key) for key in ('version', 'run_uid', 'run_generation', 'context_digest')},
                           'intents': work['work_intents']}
    return None, None


def project(events, state, generation, run_id):
    base = {'run_id': run_id, 'run_uid': state.run_uid, 'generation': generation,
            'event_seq': events[-1].seq, 'authority': 'none', 'exclusivity': 'not_claimed',
            'execution': 'not_attested', 'subject_identity': 'author_declared_not_verified'}
    event, record = _last_record(events)
    if not isinstance(state.run_uid, str) or not state.run_uid:
        return {**base, 'state': 'unavailable', 'claims': None, 'checkpoint_seq': None}
    if event is None:
        return {**base, 'state': 'not_declared', 'claims': [], 'checkpoint_seq': None}
    try:
        if (not isinstance(record, dict)
                or set(record) != {'version', 'run_uid', 'run_generation', 'context_digest', 'intents'}
                or type(record.get('version')) is not int or record['version'] != 1
                or record.get('run_uid') != state.run_uid or record.get('run_generation') != generation
                or not isinstance(record.get('context_digest'), str) or _SHA.fullmatch(record['context_digest']) is None):
            raise ValueError('invalid_work_record')
        claims = normalize_intents(record['intents'])
        validate_direction_refs(claims, events[:next(i for i, row in enumerate(events) if row.seq == event.seq)])
    except (ValueError, TypeError, UnicodeError, StopIteration):
        return {**base, 'state': 'unavailable', 'claims': None, 'checkpoint_seq': event.seq}
    from looplab.harness.progress import _operator_directions
    directions = _operator_directions(state, events=events, run_id=run_id, generation=generation)
    known = {(row['event_seq'], row['text_sha256']) for row in directions['item_details']}
    current_refs = {(ref['event_seq'], ref['text_sha256']) for claim in claims
                    if claim['status'] == 'active' for ref in claim['direction_refs']}
    source = '/api/runs/' + quote(run_id, safe='') + '/log-page?' + urlencode({
        'direction': 'around', 'anchor_seq': event.seq, 'limit': 1, 'byte_limit': 65536, 'generation': generation})
    return {**base, 'state': 'context_unchanged' if record['context_digest'] == context_digest(state) else 'context_changed',
            'claims': claims, 'checkpoint_seq': event.seq, 'full_ref': source,
            'unaddressed_visible_direction_sequences': sorted(seq for seq, digest in known - current_refs),
            'direction_window_truncated': directions['window_truncated'],
            'meaning': 'Latest explicit declaration, not a lease, measured completion or approval. New operator context is unresolved until the author records its interpretation; it does not block execution. Historical references and authored deferred/conflict dispositions are not proof of reading, disobedience or scientific truth.'}


def snapshot(root: Path, rd: Path, run_id: str, expected_generation: str, peers=(), max_bytes=65536):
    if (not isinstance(peers, (list, tuple)) or len(peers) > 8
            or any(not isinstance(peer, str) or len(peer) > 256 for peer in peers)
            or type(max_bytes) is not int or not 4096 <= max_bytes <= 65536):
        raise HTTPException(400, {'code': 'invalid_work_claim_read'})
    events = read_event_source(rd); generation = run_generation_token(events)
    if generation != expected_generation.lower():
        raise HTTPException(409, 'run generation changed')
    state = fold(events)
    if not isinstance(state.task_id, str) or not state.task_id:
        raise HTTPException(409, {'code': 'work_claim_task_scope_unknown'})
    if not isinstance(state.run_uid, str) or not state.run_uid:
        raise HTTPException(503, {'code': 'work_claim_identity_unavailable'})
    own = project(events, state, generation, run_id)
    from looplab.harness.progress import _operator_directions
    from looplab.harness.direction_receipts import project as project_receipts
    directions = _operator_directions(state, events=events, run_id=run_id, generation=generation)
    direction_receipts = project_receipts(root, run_id, state.run_uid, generation,
                                          [row['event_seq'] for row in directions['item_details'] if row['event_seq'] is not None])
    results, seen = [], {rd.resolve()}
    for peer in peers:
        address = resolve_run_address(root, peer)
        if address.defect is not None:
            results.append({'run_id': peer, 'state': 'unavailable', 'claims': None}); continue
        if address.path.resolve() in seen:
            continue
        seen.add(address.path.resolve())
        try:
            peer_events = read_event_source(address.path)
            peer_state = fold(peer_events)
            if peer_state.task_id != state.task_id:
                results.append({'run_id': address.run_id, 'state': 'outside_task_scope', 'claims': None}); continue
            results.append(project(peer_events, peer_state, run_generation_token(peer_events), address.run_id))
        except (HTTPException, OSError, ValueError):
            results.append({'run_id': peer, 'state': 'unavailable', 'claims': None})
    if run_generation_token(read_event_source(rd)) != generation:
        raise HTTPException(409, 'run generation changed')
    payload = {'version': 1, 'generation': generation, 'run_uid': state.run_uid, 'event_seq': events[-1].seq,
               'task_scope': 'same_native_task_id_only_inputs_and_scientific_equivalence_not_verified',
               'discovery': 'caller_selected_peers_only', 'atomic_across_runs': False,
               'own': own, 'direction_receipts': direction_receipts, 'peers': results, 'omitted_peers': [], 'possible_overlaps': [], 'possible_overlaps_total': 0, 'overlaps_truncated': False,
               'response_budget': {'max_bytes': max_bytes, 'limited': False}}
    def size():
        return len(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode())
    while size() > max_bytes and payload['peers']:
        removed = payload['peers'].pop(); payload['omitted_peers'].insert(0, removed['run_id'])
        payload['response_budget']['limited'] = True
    # Whole rows only: a shortened scientific declaration must not masquerade as complete.
    if size() > max_bytes:
        raise HTTPException(503, {'code': 'own_work_claims_exceed_response_budget', 'message': 'Request a larger bounded response; no claim was shortened.'})
    own_active = [row for row in own.get('claims') or [] if row['status'] == 'active']
    def matches():
        values = []
        for peer in payload['peers']:
            for left in own_active:
                for right in peer.get('claims') or []:
                    if (right['status'] == 'active' and (left['operation'], left['subject_sha256']) == (right['operation'], right['subject_sha256'])):
                        values.append({'peer_run_id': peer['run_id'], 'own_intent_id': left['id'], 'peer_intent_id': right['id'],
                                       'basis': 'exact_author_declared_subject_and_operation_not_execution_proof'})
        return values
    overlaps = matches()
    payload['possible_overlaps_total'] = len(overlaps)
    while size() > max_bytes and payload['peers']:
        removed = payload['peers'].pop(); payload['omitted_peers'].insert(0, removed['run_id'])
        payload['response_budget']['limited'] = True
        overlaps = matches(); payload['possible_overlaps_total'] = len(overlaps)
    if size() > max_bytes:
        raise HTTPException(503, {'code': 'own_work_claims_exceed_response_budget'})
    for match in overlaps:
        payload['possible_overlaps'].append(match)
        if size() > max_bytes:
            payload['possible_overlaps'].pop()
            payload['overlaps_truncated'] = payload['response_budget']['limited'] = True
    return payload
