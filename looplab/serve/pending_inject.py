"""Exact idle cancellation of an admitted, unmaterialized inject head.

The existing inject_failed/inject_done batch is the authority. Command metadata is a recoverable
projection of that batch, not an independent cancellation queue or permission to launch an engine.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import threading
import time
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from looplab.core.jsonutil import canonical_json
from looplab.core.node_evidence import read_bounded_regular_file
from looplab.events.eventstore import EventStore, EventStoreConcurrencyError, EventStoreLockError
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.events.types import EV_INJECT_DONE, EV_INJECT_FAILED, EV_INJECT_NODE
from looplab.serve.engine_proc import engine_write_lock_http, run_lifecycle_lock_http


class PendingInjectCancellationBody(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    expected_generation: str = Field(pattern=r'^[0-9a-fA-F]{64}$')
    expected_run_uid: str = Field(min_length=1, max_length=160)
    command_id: str = Field(pattern=r'^cmd_[0-9a-f]{32}$')
    request_index: int = Field(ge=0, strict=True)
    request_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    action_id: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=1000)


def _refuse(code, message, status=409):
    raise HTTPException(status, {'code': code, 'message': message})


def _hash(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


# One immutable observation revision is scanned once, not on every command watcher poll. Cache only
# small validated receipts; source events and payloads are not retained here.
_receipts = OrderedDict()
_receipts_lock = threading.Lock()


def _receipt_index(observation):
    key = (str(observation.path), observation.revision)
    with _receipts_lock:
        if key in _receipts:
            return _receipts[key]
    events = observation.events_view()
    by_action = {}
    for i, event in enumerate(events):
        receipt = event.data.get('cancellation')
        if receipt is None:
            continue
        if event.type == EV_INJECT_DONE:
            if i == 0 or events[i - 1].type != EV_INJECT_FAILED or events[i - 1].data.get('cancellation') != receipt:
                _refuse('inject_cancellation_history_unavailable', 'Cancellation history is incomplete.', 503)
            continue
        if event.type != EV_INJECT_FAILED or not isinstance(receipt, dict):
            _refuse('inject_cancellation_history_unavailable', 'Cancellation history is invalid.', 503)
        try:
            request = PendingInjectCancellationBody(**receipt['request']).model_dump()
            request['expected_generation'] = request['expected_generation'].lower()
            done = events[i + 1]
            valid = (type(receipt.get('v')) is int and receipt['v'] == 1
                     and receipt.get('outcome') == 'operator_cancelled'
                     and type(receipt.get('failure_seq')) is int and type(receipt.get('done_seq')) is int
                     and type(event.data.get('idx')) is int and type(done.data.get('idx')) is int
                     and event.data.get('reason') == 'operator_cancelled'
                     and done.type == EV_INJECT_DONE and done.data.get('cancellation') == receipt
                     and event.data.get('idx') == request['request_index'] == done.data.get('idx')
                     and receipt.get('failure_seq') == event.seq and receipt.get('done_seq') == done.seq
                     and type(receipt.get('original_event_seq')) is int
                     and receipt.get('request_sha256') == _hash(request)
                     and request['action_id'] not in by_action)
        except (KeyError, ValueError, TypeError, IndexError):
            valid = False
        if not valid:
            _refuse('inject_cancellation_history_unavailable', 'Cancellation history is inconsistent.', 503)
        by_action[request['action_id']] = receipt
    with _receipts_lock:
        _receipts[key] = by_action
        while len(_receipts) > 32:
            _receipts.popitem(last=False)
    return by_action


def receipt_for_record(record, observation):
    """Read-only authority used by admission, retry and respawn recovery, including a lost save."""
    if record.get('event_type') != EV_INJECT_NODE:
        return None
    if observation.torn_tail:
        _refuse('inject_cancellation_history_unavailable', 'Event history is incomplete.', 503)
    for receipt in _receipt_index(observation).values():
        request = receipt['request']
        if (request['command_id'] == record.get('id')
                and request['expected_generation'] == record.get('run_generation')
                and receipt['original_event_seq'] == record.get('event_seq')):
            return receipt
    return None


def reconcile_record(service, rd, path, record, observation):
    receipt = receipt_for_record(record, observation)
    if receipt is None:
        return None
    updated = dict(record)
    updated['pending_inject_cancellation'] = receipt
    if record.get('status') in ('succeeded', 'noop'):
        # A successful admission ACK remains historical success. Its later cancellation is distinct.
        if updated != record:
            service._save(path, updated)
        return updated
    error = {'code': 'operator_cancelled', 'message': 'The unmaterialized inject was explicitly cancelled.',
             'remediation': 'Read the cancellation receipt; a new experiment requires a new command.',
             'retryable': False}
    if updated == record and record.get('status') == 'failed' and record.get('error') == error:
        return record
    return service._terminal(path, updated, 'failed', error=error)


def settle_worker(service, rd, path, record, observation):
    """Stop an old watcher before timeout/postcondition/respawn, even if metadata save was lost."""
    if receipt_for_record(record, observation) is None:
        return False
    with service.sequence(rd):
        current = service._load(path)
        if current is not None:
            reconcile_record(service, rd, path, current, service._observe(rd))
    return True


def _context(service, rd, expected_generation):
    from looplab.harness.journals import read_event_source

    rd = service.validate_paths(rd)
    events = read_event_source(rd)
    observation = service._observe(rd)
    if observation.torn_tail:
        _refuse('inject_cancellation_history_unavailable', 'Event history is incomplete.', 503)
    if not events or observation.latest_seq != events[-1].seq:
        _refuse('inject_source_changed', 'Run history changed during the read; refresh the request identity.')
    generation = run_generation_token(events)
    if generation != expected_generation.lower():
        _refuse('run_generation_changed', 'Run generation changed.')
    state = fold(events)
    if not isinstance(state.run_uid, str) or not state.run_uid:
        _refuse('run_uid_unavailable', 'Cancellation needs an explicit recorded run UID; legacy identity is unavailable.')
    return rd, events, observation, state, generation


def _record(service, rd, command_id):
    path = service._path(rd, command_id)
    if not path.exists():
        _refuse('inject_command_missing', 'Read the original command receipt; this command is absent.', 404)
    raw = read_bounded_regular_file(path, 2 * 1024 * 1024)
    try:
        record = json.loads(raw) if raw is not None else None
    except (ValueError, UnicodeDecodeError, RecursionError):
        record = None
    if not isinstance(record, dict) or record.get('id') != command_id or record.get('event_type') != EV_INJECT_NODE:
        _refuse('inject_command_unavailable', 'The original inject command record is unavailable.', 503)
    return path, record


def _identity(service, rd, record, observation, state, generation):
    if record.get('run_generation') != generation:
        _refuse('inject_command_generation_changed', 'The original command belongs to another run generation.')
    intent = service._find_intent(rd, record['id'], record, observation)
    if intent is None or not record.get('semantic_payload_digest'):
        _refuse('inject_intent_unavailable', 'The exact original inject intent cannot be verified.', 503)
    marker = service._intent_marker(record, record['id'])
    indices = [i for i, request in enumerate(state.inject_requests) if request.get('_command_id') == marker]
    if len(indices) != 1:
        _refuse('inject_request_unavailable', 'The original command does not identify exactly one queued request.', 503)
    index = indices[0]
    digest = _hash({'run_uid': state.run_uid, 'generation': generation, 'command_id': record['id'],
                    'request_index': index, 'intent_seq': intent.seq,
                    'semantic_payload_digest': record['semantic_payload_digest']})
    return index, digest, intent


def pending_identity(service, rd: Path, command_id: str, expected_generation: str):
    rd, _events, observation, state, generation = _context(service, rd, expected_generation)
    _path, record = _record(service, rd, command_id)
    index, digest, intent = _identity(service, rd, record, observation, state, generation)
    prior = receipt_for_record(record, observation)
    status = ('operator_cancelled' if prior else 'already_served' if index < state.injects_done
              else 'pending_head' if index == state.injects_done else 'pending_not_head')
    return {'v': 1, 'generation': generation, 'run_uid': state.run_uid, 'command_id': command_id,
            'request_index': index, 'request_digest': digest, 'original_event_seq': intent.seq,
            'queue_head_index': state.injects_done, 'pending_count': len(state.inject_requests) - state.injects_done,
            'status': status, 'cancellation': prior, 'authority': 'identity_only'}


def cancel(service, rd: Path, body: PendingInjectCancellationBody, *, agent_token: bool):
    from looplab.serve.control_validation import run_is_external
    if agent_token and not run_is_external(rd):
        _refuse('agent_token_refused', 'The agent may cancel injects only in an external_harness run.', 403)
    request = body.model_dump()
    request['expected_generation'] = request['expected_generation'].lower()
    digest = _hash(request)
    try:
        with service.sequence(rd):
            rd, events, observation, state, generation = _context(service, rd, body.expected_generation)
            service._reject_unresolved_reset(rd, 'cancel this pending inject')
            if state.run_uid != body.expected_run_uid:
                _refuse('run_uid_changed', 'Run UID does not match the original request.')
            prior = _receipt_index(observation).get(body.action_id)
            if prior is not None:
                if prior['request_sha256'] != digest:
                    _refuse('inject_cancellation_action_conflict', 'Cancellation action_id was reused with different content.')
                path, record = _record(service, rd, body.command_id)
                reconcile_record(service, rd, path, record, observation)
                return {'ok': True, 'replayed': True, 'cancellation': prior}
            path, record = _record(service, rd, body.command_id)
            index, request_digest, intent = _identity(service, rd, record, observation, state, generation)
            if index != body.request_index or request_digest != body.request_digest:
                _refuse('inject_request_changed', 'The exact queued request identity does not match.')
            if index != state.injects_done:
                _refuse('inject_not_pending_head', 'Only the current unmaterialized inject head can be cancelled.')
            if state.building or state.buildings:
                _refuse('inject_reservation_present', 'A build reservation exists; its ownership must be resolved separately.')
            with run_lifecycle_lock_http(rd):
                liveness = service._engine_state(rd)
                if liveness is not False:
                    _refuse('engine_running' if liveness is True else 'engine_liveness_unknown',
                            'Cancellation requires positively stopped engine ownership.')
                if service._spawn_claim_path(rd).exists():
                    _refuse('engine_spawn_unresolved', 'An engine spawn claim is unresolved; cancellation starts no recovery.')
                directory = service._directory(rd)
                for claim in directory.glob('.activity_*.json'):
                    _refuse('command_activity_unresolved', 'Another activity claim is unresolved.')
                for candidate, active in service._scan_command_records(rd, on_symlink='unreadable'):
                    if candidate != path and (active is None or active.get('status') not in
                                               ('succeeded', 'noop', 'failed', 'rejected', 'timed_out')):
                        _refuse('command_in_progress', 'Another command is active or unreadable.')
                for claim in directory.glob('.cmd_*.executing'):
                    if claim.name != f'.{body.command_id}.executing':
                        _refuse('command_activity_unresolved', 'Another command execution claim is unresolved.')
                with engine_write_lock_http(rd):
                    # Direct CLI writers are excluded here; the tail CAS still catches any other
                    # event publisher. No reservation, engine spawn or materializer is invoked.
                    receipt = {'v': 1, 'outcome': 'operator_cancelled',
                               'author': 'agent' if agent_token else 'operator',
                               'request': request, 'request_sha256': digest,
                               'original_event_seq': intent.seq, 'failure_seq': events[-1].seq + 1,
                               'done_seq': events[-1].seq + 2, 'recorded_at': time.time()}
                    EventStore(rd / 'events.jsonl').append_many([
                        (EV_INJECT_FAILED, {'idx': index, 'reason': 'operator_cancelled',
                                            'error': body.reason, 'cancellation': receipt}),
                        (EV_INJECT_DONE, {'idx': index, 'cancellation': receipt})],
                        expected_last_seq=events[-1].seq, require_lock=True, require_durable=True)
                    reconcile_record(service, rd, path, record, service._observe(rd))
            return {'ok': True, 'replayed': False, 'cancellation': receipt}
    except EventStoreConcurrencyError as exc:
        raise HTTPException(409, {'code': 'inject_source_changed', 'message': 'Run history changed; refresh identity.'}) from exc
    except (OSError, EventStoreLockError) as exc:
        raise HTTPException(503, {'code': 'inject_cancellation_unavailable',
                                  'message': 'Cancellation acknowledgement is unavailable. Read the original receipt and retry only this exact action.'}) from exc
