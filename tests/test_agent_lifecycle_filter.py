"""Proven obsolete agent pauses are not fresh wakes; the outbox remains history."""
import json
from pathlib import Path
from types import SimpleNamespace

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.serve import agent_events as ae
from looplab.serve.agent_lifecycle_filter import superseded_agent_pause


def _run(tmp_path):
    root = tmp_path / 'runs'; rd = root / 'r'; rd.mkdir(parents=True)
    (rd / 'config.snapshot.json').write_text(Settings(backend='toy', external_harness=True).model_dump_json())
    store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'min'})
    return root, rd, store


def _pause(rd, store, suffix, submitted_by='agent_client'):
    cid = 'cmd_' + suffix * 32
    event = store.append('pause', {'reason': 'synthetic pause', '_command_id': cid})
    path = rd / '.commands'; path.mkdir(exist_ok=True)
    (path / f'{cid}.json').write_text(json.dumps({'id': cid, 'event_type': 'pause', 'event_seq': event.seq,
        'status': 'succeeded', 'submitted_by': submitted_by, 'run_generation': run_generation_token(store.read_all())}))
    return {'kind': 'run_paused', 'run_id': 'r', 'run_uid': 'u', 'seq': event.seq,
            'key': f'u:seq:{event.seq}', 'summary': 'synthetic pause'}


def test_filter_requires_newer_explicit_resume_and_exact_agent_receipt(tmp_path):
    _, rd, store = _run(tmp_path)
    agent = _pause(rd, store, 'a')
    assert not superseded_agent_pause(rd, agent, {})
    store.append('resume', {})
    assert superseded_agent_pause(rd, agent, {})
    # A current later pause must remain actionable; older pause is still superseded.
    current = _pause(rd, store, 'b')
    assert superseded_agent_pause(rd, agent, {})
    assert not superseded_agent_pause(rd, current, {})
    owner = _pause(rd, store, 'c', 'owner')
    store.append('resume', {})
    assert not superseded_agent_pause(rd, owner, {})
    assert not superseded_agent_pause(rd, {**agent, 'run_uid': 'another'}, {})
    assert not superseded_agent_pause(rd, {**agent, 'seq': 999}, {})
    (rd / '.commands' / ('cmd_' + 'a' * 32 + '.json')).unlink()
    assert not superseded_agent_pause(rd, agent, {})


def test_delivery_filters_obsolete_agent_pause_but_preserves_user_facts_and_history(tmp_path):
    root, rd, store = _run(tmp_path)
    old = _pause(rd, store, 'a')
    store.append('resume', {})
    user = _pause(rd, store, 'b', 'owner')
    store.append('resume', {})
    gen = run_generation_token(store.read_all())
    rows = [old, user,
            {'kind': 'operator_direction', 'run_id': 'r', 'run_uid': 'u', 'key': 'user-text', 'summary': 'operator message'},
            {'kind': 'agent_handoff', 'run_id': 'r', 'run_uid': 'u', 'generation': gen, 'key': 'handoff',
             'recipient_hook': 'default', 'summary': 'addressed handoff'},
            {'kind': 'node_failed', 'run_id': 'r', 'run_uid': 'u', 'key': 'failure', 'summary': 'actual failure'}]
    appended = ae.append_events(root, rows)
    ae.write_notify_config(root, {'command': ['wake', '{message}'], 'debounce_s': 0})
    calls = []
    pump = ae.AgentEventPump(SimpleNamespace(root=root), runner=lambda argv, timeout: calls.append(argv) or {'ok': True, 'exit_code': 0})
    pump.deliver()
    assert len(calls) == 1
    message = calls[0][1]
    assert 'operator message' in message and 'addressed handoff' in message and 'actual failure' in message
    assert f"#{appended[0]['id']} run_paused" not in message
    assert f"#{appended[1]['id']} run_paused" in message
    history = ae.read_outbox(root, after=-1)['events']
    assert {x['id'] for x in history} == {x['id'] for x in appended}
    assert any(x['status'] == 'dropped_stale' and appended[0]['id'] in x['event_ids'] for x in ae.ledger_rows(root))


def test_unknown_or_damaged_sources_are_not_silently_suppressed(tmp_path, monkeypatch):
    root, rd, store = _run(tmp_path)
    pause = _pause(rd, store, 'a'); store.append('resume', {})
    import looplab.harness.journals as journals
    from fastapi import HTTPException
    monkeypatch.setattr(journals, 'read_event_source', lambda path: (_ for _ in ()).throw(HTTPException(503, 'unknown')))
    assert not superseded_agent_pause(rd, pause, {})
    pump = ae.AgentEventPump(SimpleNamespace(root=root))
    assert pump._still_actionable([pause]) == ([pause], [])


def test_open_checkpoint_is_preserved_when_old_pause_is_filtered(tmp_path):
    from looplab.events.replay import fold
    from looplab.harness import checkpoints
    root, rd, store = _run(tmp_path)
    store.append('node_created', {'node_id': 0, 'parent_ids': [], 'operator': 'draft', 'idea': {'operator': 'draft'}})
    old = _pause(rd, store, 'a'); store.append('resume', {})
    q = checkpoints.ask(rd, 0, 0, 'train_monitor', stage='train', observation='step 5 loss 2')
    pump = ae.AgentEventPump(SimpleNamespace(root=root))
    state = fold(store.read_all())
    conditions = pump._conditions(rd, None, state, store.read_all(), state.run_uid)
    checkpoint = next(row for row in conditions if row['kind'] == 'monitor_question')
    keep, stale = pump._still_actionable([old, checkpoint])
    assert stale == [old] and keep == [checkpoint]
    assert checkpoint['details']['checkpoint_id'] == q['checkpoint_id']


def test_restart_reminder_discards_only_pause_superseded_since_initial_delivery(tmp_path):
    from tests.test_agent_events import _Activity, _Clock, _rewrite_outbox_ts
    root, rd, store = _run(tmp_path)
    pause = _pause(rd, store, 'a')
    rows = ae.append_events(root, [pause, {'kind': 'node_failed', 'run_id': 'r', 'run_uid': 'u',
                                          'key': 'failure', 'summary': 'retained failure'}])
    clock = _Clock(); activity = _Activity(); calls = []
    srv = SimpleNamespace(root=root, agent_activity=activity)
    runner = lambda argv, timeout: calls.append(argv) or {'ok': True, 'exit_code': 0}
    ae.write_notify_config(root, {'command': ['wake', '{message}'], 'debounce_s': 0})
    _rewrite_outbox_ts(root, clock() - 1)
    ae.AgentEventPump(srv, clock=clock, runner=runner).deliver()
    assert len(calls) == 1 and 'run_paused' in calls[0][1]
    store.append('resume', {})
    clock.t += ae.REACTION_WINDOW_S + 1
    ae.AgentEventPump(srv, clock=clock, runner=runner).deliver()
    assert len(calls) == 2 and 'retained failure' in calls[1][1]
    assert 'run_paused' not in calls[1][1]
    assert any(row['status'] == 'dropped_stale' and rows[0]['id'] in row['event_ids'] for row in ae.ledger_rows(root))
    assert len(ae.read_outbox(root, after=-1)['events']) == 2


def test_generation_changed_after_receipt_read_keeps_notification(tmp_path, monkeypatch):
    from looplab.harness import journals
    root, rd, store = _run(tmp_path)
    pause = _pause(rd, store, 'a'); store.append('resume', {})
    original = journals.read_event_source; calls = []
    def racing(path):
        calls.append(path)
        if len(calls) == 2:
            from fastapi import HTTPException
            raise HTTPException(409, 'run replaced during read')
        return original(path)
    monkeypatch.setattr(journals, 'read_event_source', racing)
    assert not superseded_agent_pause(rd, pause, {})
