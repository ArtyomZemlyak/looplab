from fastapi.testclient import TestClient

from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.serve.agent_events import append_events
from looplab.serve.server import make_app


def test_ack_is_idempotent_bound_to_incarnation_and_never_resolves_work(tmp_path, monkeypatch):
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir()
    store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'uid-r', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    event = append_events(tmp_path, [{'key': 'uid-r:ready:1', 'run_id': 'r', 'run_uid': 'uid-r',
                                      'kind': 'node_evaluated', 'summary': 'ready'}])[0]
    original = (rd / 'events.jsonl').read_bytes()
    body = {'expected_generation': gen, 'event_id': event['id'], 'recipient': 'researcher',
            'action_id': 'ack-1', 'disposition': 'acknowledged'}
    with TestClient(make_app(tmp_path)) as c:
        url = '/api/runs/r/agent-event-ack'
        first = c.post(url, json=body); assert first.status_code == 200, first.text
        assert c.post(url, json=body).json() == first.json()
        assert c.post(url, json={**body, 'disposition': 'resolved'}).status_code == 409
        assert c.post(url, json={**body, 'action_id': 'ack-2', 'expected_generation': '0' * 64}).status_code == 409
        assert c.post(url, json={**body, 'action_id': 'ack-3', 'event_id': 999}).status_code == 404
        page = c.get(url, params={'expected_generation': gen, 'recipient': 'researcher'}).json()
        assert len(page['items']) == 1
        assert 'only' in page['items'][0]['meaning']
    assert (rd / 'events.jsonl').read_bytes() == original


def test_manual_direction_is_a_distinct_wakeup_without_copying_its_text():
    from types import SimpleNamespace
    from looplab.serve.agent_events import derive_log_events
    events = [SimpleNamespace(seq=3, type='hint', data={'text': 'new instruction', 'replace': True})]
    row = derive_log_events('r', 'uid-r', events)[0]
    assert row['kind'] == 'operator_direction'
    assert row['owner'] == 'external_agent' and row['authority'] == 'notification_only'
    assert row['seq'] == 3 and row['details']['replace'] is True
    assert 'new instruction' not in str(row)


def test_ack_refuses_damaged_journal_instead_of_duplicate_on_retry(tmp_path, monkeypatch):
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir()
    store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'uid-r', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    event = append_events(tmp_path, [{'key': 'one', 'run_id': 'r', 'run_uid': 'uid-r', 'kind': 'node_evaluated'}])[0]
    (tmp_path / '.agent-events' / 'acknowledgements.jsonl').write_text('{broken\n')
    body = {'expected_generation': gen, 'event_id': event['id'], 'recipient': 'agent', 'action_id': 'a', 'disposition': 'acknowledged'}
    with TestClient(make_app(tmp_path)) as c:
        assert c.post('/api/runs/r/agent-event-ack', json=body).status_code == 503
        assert c.get('/api/runs/r/agent-event-ack', params={'expected_generation': gen}).status_code == 503


def test_addressed_handoff_is_idempotent_and_reaches_only_target_hook(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace
    from looplab.serve import agent_events as ae
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir()
    store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'uid-r', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    calls = []
    pump = ae.AgentEventPump(SimpleNamespace(root=tmp_path), clock=lambda: time.time() + 10,
        runner=lambda argv, timeout: calls.append(argv) or {'ok': True, 'exit_code': 0})
    for hook in ('researcher', 'executor'):
        ae.write_notify_config(tmp_path, {'command': ['notify', hook, '{message}'], 'debounce_s': 0}, hook=hook)
    body = {'expected_generation': gen, 'action_id': 'ready-1', 'recipient_hook': 'researcher',
            'reason': 'Source review ready', 'next_read': '/api/runs/r/harness-progress', 'urgency': 'high'}
    with TestClient(make_app(tmp_path)) as c:
        first = c.post('/api/runs/r/agent-handoff', json=body); assert first.status_code == 200, first.text
        repeat = c.post('/api/runs/r/agent-handoff', json=body); assert repeat.json()['replayed']
        assert repeat.json()['event_id'] == first.json()['event_id']
        assert c.post('/api/runs/r/agent-handoff', json={**body, 'reason': 'changed'}).status_code == 409
        assert c.post('/api/runs/r/agent-handoff', json={**body, 'action_id': 'new', 'expected_generation': '0' * 64}).status_code == 409
    pump.deliver()
    assert len(calls) == 1 and calls[0][1] == 'researcher'
    assert 'Source review ready' in calls[0][2]
    assert len([r for r in ae.read_outbox(tmp_path)['events'] if r['kind'] == 'agent_handoff']) == 1


def test_acknowledgement_closes_handoff_reaction_without_extra_delivery(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace
    from looplab.serve import agent_events as ae
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all()); calls = []
    activity = SimpleNamespace(clock_now=lambda: 1.0, seen_since=lambda run, since: False)
    pump = ae.AgentEventPump(SimpleNamespace(root=tmp_path, agent_activity=activity), clock=lambda: time.time()+1000,
        runner=lambda argv, timeout: calls.append(argv) or {'ok': True, 'exit_code': 0})
    ae.write_notify_config(tmp_path, {'command': ['notify', '{message}'], 'debounce_s': 0}, hook='researcher')
    body = {'expected_generation': gen, 'action_id': 'ready', 'recipient_hook': 'researcher',
            'reason': 'ready for review', 'next_read': '/api/runs/r/harness-progress'}
    with TestClient(make_app(tmp_path)) as c:
        eid = c.post('/api/runs/r/agent-handoff', json=body).json()['event_id']
        pump.deliver()
        ack = {'expected_generation': gen, 'event_id': eid, 'recipient': 'researcher',
               'action_id': 'ack-ready', 'disposition': 'acknowledged'}
        assert c.post('/api/runs/r/agent-event-ack', json=ack).status_code == 200
        pump.deliver()
    assert len(calls) == 1
    assert any(row['status'] == 'acknowledged' for row in ae.ledger_rows(tmp_path))


def test_outbox_id_recovers_after_crash_between_append_and_state(tmp_path):
    import json
    from looplab.serve import agent_events as ae
    ae.append_events(tmp_path, [{'key': 'first', 'run_id': 'r', 'kind': 'node_evaluated'}])
    state_path = tmp_path / '.agent-events' / 'state.json'
    state = json.loads(state_path.read_text()); state['next_id'] = 0
    state_path.write_text(json.dumps(state))
    row = ae.append_events(tmp_path, [{'key': 'second', 'run_id': 'r', 'kind': 'node_failed'}])[0]
    assert row['id'] == 1


def test_persisted_ack_closes_recovered_handoff_without_reminder(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace
    from looplab.serve import agent_events as ae
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all()); calls = []; now = [time.time()+1000]
    activity = SimpleNamespace(clock_now=lambda: 1.0, seen_since=lambda run, since: False)
    srv = SimpleNamespace(root=tmp_path, agent_activity=activity)
    runner = lambda argv, timeout: calls.append(argv) or {'ok': True, 'exit_code': 0}
    ae.write_notify_config(tmp_path, {'command': ['notify', '{message}'], 'debounce_s': 0}, hook='researcher')
    with TestClient(make_app(tmp_path)) as c:
        handoff = {'expected_generation': gen, 'action_id': 'ready', 'recipient_hook': 'researcher',
                   'reason': 'ready for review', 'next_read': '/api/runs/r/harness-progress'}
        eid = c.post('/api/runs/r/agent-handoff', json=handoff).json()['event_id']
        ae.AgentEventPump(srv, clock=lambda: now[0], runner=runner).deliver()
        ack = {'expected_generation': gen, 'event_id': eid, 'recipient': 'researcher',
               'action_id': 'ack-ready', 'disposition': 'acknowledged'}
        assert c.post('/api/runs/r/agent-event-ack', json=ack).status_code == 200
        now[0] += ae.REACTION_WINDOW_S + 1
        ae.AgentEventPump(srv, clock=lambda: now[0], runner=runner).deliver()
    assert len(calls) == 1
    assert any(row['status'] == 'acknowledged' for row in ae.ledger_rows(tmp_path))


def test_recovered_handoff_for_replaced_generation_does_not_wake(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace
    from looplab.serve import agent_events as ae
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all()); calls = []; now = [time.time()+1000]
    activity = SimpleNamespace(clock_now=lambda: 1.0, seen_since=lambda run, since: False)
    srv = SimpleNamespace(root=tmp_path, agent_activity=activity)
    runner = lambda argv, timeout: calls.append(argv) or {'ok': True, 'exit_code': 0}
    ae.write_notify_config(tmp_path, {'command': ['notify', '{message}'], 'debounce_s': 0}, hook='researcher')
    with TestClient(make_app(tmp_path)) as c:
        body = {'expected_generation': gen, 'action_id': 'ready', 'recipient_hook': 'researcher',
                'reason': 'ready for review', 'next_read': '/api/runs/r/harness-progress'}
        assert c.post('/api/runs/r/agent-handoff', json=body).status_code == 200
        ae.AgentEventPump(srv, clock=lambda: now[0], runner=runner).deliver()
        (rd / 'events.jsonl').unlink()
        EventStore(rd / 'events.jsonl').append('run_started', {'run_id': 'r', 'run_uid': 'new', 'task_id': 't', 'goal': 'new', 'direction': 'max'})
        now[0] += ae.REACTION_WINDOW_S + 1
        ae.AgentEventPump(srv, clock=lambda: now[0], runner=runner).deliver()
    assert len(calls) == 1
    assert any(row['status'] == 'dropped_stale' for row in ae.ledger_rows(tmp_path))


def test_any_run_read_does_not_ack_addressed_handoff(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace
    from looplab.serve import agent_events as ae
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all()); calls = []; now = [time.time()+1000]
    activity = SimpleNamespace(clock_now=lambda: 1.0, seen_since=lambda run, since: True)
    srv = SimpleNamespace(root=tmp_path, agent_activity=activity)
    runner = lambda argv, timeout: calls.append(argv) or {'ok': True, 'exit_code': 0}
    ae.write_notify_config(tmp_path, {'command': ['notify', '{message}'], 'debounce_s': 0}, hook='researcher')
    with TestClient(make_app(tmp_path)) as c:
        body = {'expected_generation': gen, 'action_id': 'ready', 'recipient_hook': 'researcher',
                'reason': 'ready for review', 'next_read': '/api/runs/r/harness-progress'}
        assert c.post('/api/runs/r/agent-handoff', json=body).status_code == 200
        pump = ae.AgentEventPump(srv, clock=lambda: now[0], runner=runner); pump.deliver()
        now[0] += ae.REACTION_WINDOW_S + 1; pump.deliver()
    assert len(calls) == 2
    assert ae.ledger_rows(tmp_path)[0]['status'] == 'resent_no_reaction'


def test_addressed_handoff_cannot_be_acknowledged_under_different_recipient(tmp_path, monkeypatch):
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    with TestClient(make_app(tmp_path)) as client:
        body = {'expected_generation': gen, 'action_id': 'ready', 'recipient_hook': 'researcher',
                'reason': 'ready', 'next_read': '/api/runs/r/harness-progress'}
        event_id = client.post('/api/runs/r/agent-handoff', json=body).json()['event_id']
        ack = {'expected_generation': gen, 'event_id': event_id, 'recipient': 'other',
               'action_id': 'ack', 'disposition': 'acknowledged'}
        assert client.post('/api/runs/r/agent-event-ack', json=ack).status_code == 409
        assert client.post('/api/runs/r/agent-event-ack', json={**ack, 'recipient': 'researcher'}).status_code == 200
