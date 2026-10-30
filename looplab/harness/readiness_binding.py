"""Evidence-bound late manifest binding, independent of execution admission.

The experiment remains immutable. A receipt records a manifest match at one instant and,
when explicitly requested, a streamed artifact match; it never grants GPU work.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from fastapi import HTTPException
from looplab.core.atomicio import append_jsonl_bytes_locked
from looplab.core.jsonutil import canonical_json_digest
from looplab.events.eventstore import interprocess_lock
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.harness.campaign import experiments_of, read_journal
from looplab.harness.journals import read_event_source, read_source

JOURNAL = 'readiness_bindings.jsonl'


def _validate_scope(rd: Path, body, events):
    generation = run_generation_token(events)
    if generation != body.expected_generation:
        raise HTTPException(409, 'run generation changed')
    state = fold(events)
    rows, health = read_journal(rd)
    if not health.get('read_complete'):
        raise HTTPException(503, 'protocol journal incomplete')
    exp = experiments_of(rows, state.run_uid).get(body.experiment_id)
    if not exp:
        raise HTTPException(404, 'experiment not registered')
    scope = {k: v for k, v in exp.items() if k not in ('registration', 'superseded')}
    if canonical_json_digest(scope) != body.scope_sha256:
        raise HTTPException(409, 'scientific scope changed')
    relation = next((r for r in exp.get('relationships', []) if r.get('kind') == 'model_producer'
                     and r.get('subject_arm') == body.subject_arm and r.get('node_id') == body.node_id
                     and r.get('run_id') in (None, rd.name)), None)
    if not relation:
        raise HTTPException(409, 'no matching registered producer relationship')
    if relation.get('node_generation') is not None and relation['node_generation'] != body.attempt:
        raise HTTPException(409, 'registered producer attempt differs')
    if body.artifact_sha256 and relation.get('artifact_sha256') and relation['artifact_sha256'] != body.artifact_sha256:
        raise HTTPException(409, 'registered producer artifact digest differs')
    node = state.nodes.get(body.node_id)
    if not node or str(node.status) not in ('evaluated', 'NodeStatus.evaluated') or node.attempt != body.attempt:
        raise HTTPException(409, 'producer is not evaluated at this attempt')
    return state


def _prior(path, state, body, digest):
    saved, health = read_source(path)
    if not health.get('read_complete'):
        raise HTTPException(503, 'binding journal incomplete')
    old = next((r for r in saved if r.get('run_uid') == state.run_uid and r.get('action_id') == body.action_id), None)
    if old and old.get('request_sha256') != digest:
        raise HTTPException(409, 'binding action_id reused with different content')
    return old


def publish(srv, rd: Path, body, read_manifest, read_artifact) -> dict:
    # The potentially large explicit artifact hash happens outside the command sequencer.
    # Both the protocol and producer attempt are rechecked under the sequencer afterward.
    request = body.model_dump(exclude_none=True)
    digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    path = rd / JOURNAL
    if bool(body.artifact_path) != bool(body.artifact_sha256):
        raise HTTPException(400, 'artifact_path and artifact_sha256 must be supplied together')
    with srv.commands.sequence(rd):
        events = read_event_source(rd)
        if run_generation_token(events) != body.expected_generation:
            raise HTTPException(409, 'run generation changed')
        state = fold(events)
        old = _prior(path, state, body, digest)
        if old:
            return {'replayed': True, 'binding': old}
        state = _validate_scope(rd, body, events)
        uid = state.run_uid
    prefix = f'nodes/node_{body.node_id}/'
    for field, label in ((body.path, 'manifest'), (body.artifact_path, 'artifact')):
        if field and (not field.startswith(prefix) or '..' in field.split('/')):
            raise HTTPException(400, f'{label} must be inside the producer node directory')
    actual, identity = read_manifest(body.path, body.node_id, body.attempt)
    if identity.get('node_id') != body.node_id or identity.get('attempt') != body.attempt:
        raise HTTPException(409, 'manifest is not bound to the producer attempt')
    if actual != body.manifest_sha256:
        raise HTTPException(409, 'manifest hash mismatch')
    if body.artifact_path:
        actual, identity = read_artifact(body.artifact_path, body.node_id, body.attempt)
        if identity.get('node_id') != body.node_id or identity.get('attempt') != body.attempt:
            raise HTTPException(409, 'artifact is not bound to the producer attempt')
        if actual != body.artifact_sha256:
            raise HTTPException(409, 'artifact hash mismatch')
    with srv.commands.sequence(rd):
        after = read_event_source(rd)
        current = _validate_scope(rd, body, after)
        if current.run_uid != uid:
            raise HTTPException(409, 'run incarnation changed')
        with interprocess_lock(Path(str(path) + '.lock'), required=True):
            old = _prior(path, current, body, digest)
            if old:
                return {'replayed': True, 'binding': old}
            verified = bool(body.artifact_path)
            receipt = {**request, 'run_uid': uid, 'event_seq': after[-1].seq, 'request_sha256': digest,
                       'status': 'artifact_and_manifest_verified' if verified else 'manifest_verified',
                       'meaning': ('Manifest and artifact bytes matched at read time; execution authority is not established.' if verified
                                   else 'Manifest bytes matched at read time; checkpoint content and execution authority are not established.')}
            append_jsonl_bytes_locked(path, json.dumps(receipt, sort_keys=True).encode() + b'\n')
        return {'replayed': False, 'binding': receipt}


def snapshot(rd: Path, generation: str) -> dict:
    events = read_event_source(rd)
    if run_generation_token(events) != generation:
        raise HTTPException(409, 'run generation changed')
    state = fold(events)
    protocol, health = read_journal(rd)
    if not health.get('read_complete'):
        raise HTTPException(503, 'protocol journal incomplete')
    experiments = experiments_of(protocol, state.run_uid)
    rows, health = read_source(rd / JOURNAL)
    if not health.get('read_complete'):
        raise HTTPException(503, 'binding journal incomplete')
    result = []
    for row in rows:
        if row.get('run_uid') != state.run_uid or row.get('expected_generation') != generation:
            continue
        exp = experiments.get(row.get('experiment_id'))
        scope = {k: v for k, v in (exp or {}).items() if k not in ('registration', 'superseded')}
        node = state.nodes.get(row.get('node_id'))
        current = bool(exp and canonical_json_digest(scope) == row.get('scope_sha256') and node
                       and node.attempt == row.get('attempt') and str(node.status) in ('evaluated', 'NodeStatus.evaluated'))
        result.append({**row, 'scope_and_attempt_current': current,
                       'manifest_current': 'not_rechecked', 'artifact_current': 'not_rechecked',
                       'authority': 'none'})
    return {'generation': generation, 'run_uid': state.run_uid, 'event_seq': events[-1].seq,
            'items': result[-100:], 'truncated': len(result) > 100,
            'meaning': 'Receipts report a prior manifest read. Refresh and recheck manifest, source, protocol, budget and GPU admission before execution.'}
