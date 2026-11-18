"""Public no-spawn cancellation preserves queue identity, historical results and crash recovery."""
import json
import threading
from pathlib import Path

import anyio
import httpx
import pytest

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore, interprocess_lock
from looplab.events.replay import fold
from looplab.harness.mcp_server import HarnessAPI
from tests.test_run_command_service import _seed, _client, _Driver, _post, _terminal, _generation


def _fixture(tmp_path, monkeypatch, *, paused=True, queued=2):
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    monkeypatch.setenv('LOOPLAB_HARNESS_TOKEN', 'test-agent')
    root = tmp_path / 'runs'
    rd = root / 'demo'
    rd.mkdir(parents=True)
    seed = EventStore(rd / 'events.jsonl')
    seed.append('run_started', {'run_id': 'demo', 'run_uid': 'cpu-cancel-incarnation',
                                'task_id': 'task', 'goal': 'g', 'direction': 'min'})
    seed.append('node_created', {'node_id': 0, 'parent_ids': [], 'operator': 'draft',
                                 'idea': {'operator': 'draft'}, 'code': 'print(1)'})
    if paused:
        seed.append('pause', {})
    (rd / 'task.snapshot.json').write_text('{"kind":"quadratic","goal":"g","direction":"min"}')
    (rd / 'config.snapshot.json').write_text(json.dumps(Settings(
        backend='toy', external_harness=True, deep_research_every=-1, novelty_mode='off',
        foresight=False, track_hypotheses=False, concept_run_base=False).model_dump(mode='json')))
    store = EventStore(rd / 'events.jsonl')
    store.append('node_evaluated', {'node_id': 0, 'metric': .4, 'eval_seconds': .123})
    driver = _Driver(alive=not paused)
    client, srv = _client(root, driver, timeout=.05, observation=.4)
    commands = []
    for i in range(queued):
        submitted = _post(client, 'inject_node', {'idea': {'operator': 'manual', 'rationale': f'queued {i}', 'concepts': ['cpu/fixture']},
                                                  'code': f'print({i})'}, key=f'queued:{i}')
        assert submitted.status_code == 200, submitted.text
        command = _terminal(client, submitted.json())
        assert command['status'] == 'succeeded', command
        commands.append(command)
    assert not driver.calls

    def forward(request):
        response = client.request(request.method, request.url.raw_path.decode(), content=request.content,
                                  headers={k: v for k, v in request.headers.items()
                                           if k.lower() in ('content-type', 'x-looplab-token')})
        return httpx.Response(response.status_code, content=response.content,
                              headers={'content-type': 'application/json'})

    api = HarnessAPI('http://testserver/', 'test-agent', transport=httpx.MockTransport(forward),
                     request_dir=tmp_path / 'client-store')
    return root, rd, store, driver, client, srv, api, commands


def _body(client, command):
    generation = _generation(client)
    identity = client.get('/api/runs/demo/pending-inject', params={
        'command_id': command['id'], 'expected_generation': generation})
    assert identity.status_code == 200, identity.text
    row = identity.json()
    return {'expected_generation': generation, 'expected_run_uid': row['run_uid'],
            'command_id': command['id'], 'request_index': row['request_index'],
            'request_digest': row['request_digest'], 'action_id': 'cancel:first',
            'reason': 'This queued experiment was superseded before materialization.'}


def test_public_mcp_cancels_only_exact_head_and_replays_after_next_head(tmp_path, monkeypatch):
    root, rd, store, driver, client, srv, api, commands = _fixture(tmp_path, monkeypatch)
    body = _body(client, commands[0])
    before = (rd / 'events.jsonl').read_bytes()
    state = fold(store.read_all())
    nodes = {nid: node.model_dump(mode='json') for nid, node in state.nodes.items()}
    response = api.request('POST', '/api/runs/demo/pending-inject-cancellation', body)
    assert response['status'] == 200, response
    saved = response['body']['cancellation']
    assert saved['author'] == 'agent' and saved['outcome'] == 'operator_cancelled'
    current = fold(store.read_all())
    assert current.injects_done == 1 and len(current.inject_requests) == 2
    assert {nid: node.model_dump(mode='json') for nid, node in current.nodes.items()} == nodes
    assert current.budget_overrides == state.budget_overrides and current.total_eval_seconds == state.total_eval_seconds
    assert (rd / 'events.jsonl').read_bytes().startswith(before)
    assert [e.type for e in store.read_all()][-2:] == ['inject_failed', 'inject_done']
    assert not driver.calls
    reply = api.request('POST', '/api/runs/demo/pending-inject-cancellation', body)
    assert reply['status'] == 200 and reply['body']['replayed'] is True
    assert reply['body']['cancellation'] == saved
    assert fold(store.read_all()).injects_done == 1
    conflict = client.post('/api/runs/demo/pending-inject-cancellation', json={**body, 'reason': 'changed'})
    assert conflict.status_code == 409 and fold(store.read_all()).injects_done == 1
    # A fresh service re-derives cancellation and never drives the original inject on GET/retry.
    fresh, _srv = _client(root, driver)
    record = fresh.get(f"/api/runs/demo/commands/{commands[0]['id']}")
    assert record.status_code == 200 and record.json()['pending_inject_cancellation'] == saved
    retry = fresh.post(f"/api/runs/demo/commands/{commands[0]['id']}/retry")
    assert retry.status_code in (200, 409) and not driver.calls
    assert fold(store.read_all()).injects_done == 1


@pytest.mark.parametrize('changes', [
    {'expected_generation': '0' * 64}, {'expected_run_uid': 'wrong'},
    {'command_id': 'cmd_' + 'f' * 32}, {'request_digest': '0' * 64},
    {'request_index': 1}, {'request_index': True}, {'node_id': 0},
])
def test_wrong_identity_refuses_without_native_history_or_command_mutation(tmp_path, monkeypatch, changes):
    _root, rd, _store, driver, client, _srv, _api, commands = _fixture(tmp_path, monkeypatch)
    body = _body(client, commands[0])
    before = (rd / 'events.jsonl').read_bytes()
    records = {p.name: p.read_bytes() for p in (rd / '.commands').glob('cmd_*.json')}
    reply = client.post('/api/runs/demo/pending-inject-cancellation', json={**body, **changes})
    assert reply.status_code in (404, 409, 422), reply.text
    assert (rd / 'events.jsonl').read_bytes() == before
    assert {p.name: p.read_bytes() for p in (rd / '.commands').glob('cmd_*.json')} == records
    assert not driver.calls


def test_non_head_reserved_live_unknown_and_direct_cli_lock_refuse(tmp_path, monkeypatch):
    _root, rd, store, driver, client, srv, _api, commands = _fixture(tmp_path, monkeypatch)
    future = _body(client, commands[1])
    assert client.post('/api/runs/demo/pending-inject-cancellation', json=future).status_code == 409
    body = _body(client, commands[0])
    before = (rd / 'events.jsonl').read_bytes()
    driver.alive = True
    assert client.post('/api/runs/demo/pending-inject-cancellation', json=body).json()['detail']['code'] == 'engine_running'
    driver.alive = False
    real_liveness = srv.commands.engine_liveness
    srv.commands.engine_liveness = lambda _rd: None
    assert client.post('/api/runs/demo/pending-inject-cancellation', json=body).json()['detail']['code'] == 'engine_liveness_unknown'
    srv.commands.engine_liveness = real_liveness
    with interprocess_lock(rd / 'engine.lock', required=True):
        locked = client.post('/api/runs/demo/pending-inject-cancellation', json=body)
        assert locked.status_code == 409 and locked.json()['detail']['code'] == 'engine_running'
    assert (rd / 'events.jsonl').read_bytes() == before
    store.append('node_building', {'node_id': 90, 'operator': 'manual', 'parent_ids': []})
    reserved = client.post('/api/runs/demo/pending-inject-cancellation', json=body)
    assert reserved.status_code == 409 and reserved.json()['detail']['code'] == 'inject_reservation_present'
    assert fold(store.read_all()).injects_done == 0 and not driver.calls


def test_atomic_commit_lost_metadata_ack_reconciles_without_consuming_next_head(tmp_path, monkeypatch):
    root, rd, store, driver, client, srv, _api, commands = _fixture(tmp_path, monkeypatch)
    body = _body(client, commands[0])
    before = (rd / 'events.jsonl').read_bytes()
    original_save = srv.commands._save
    monkeypatch.setattr(srv.commands, '_save', lambda *_: (_ for _ in ()).throw(OSError('injected lost metadata ACK')))
    failed = client.post('/api/runs/demo/pending-inject-cancellation', json=body)
    assert failed.status_code == 503
    assert fold(store.read_all()).injects_done == 1
    assert (rd / 'events.jsonl').read_bytes().startswith(before)
    monkeypatch.setattr(srv.commands, '_save', original_save)
    fresh, _srv = _client(root, driver)
    receipt = fresh.get(f"/api/runs/demo/commands/{commands[0]['id']}").json()
    assert receipt['pending_inject_cancellation']['request']['action_id'] == body['action_id']
    again = fresh.post('/api/runs/demo/pending-inject-cancellation', json=body)
    assert again.status_code == 200 and again.json()['replayed']
    assert fold(store.read_all()).injects_done == 1 and not driver.calls



def test_atomic_batch_failure_before_commit_leaves_queue_and_command_unchanged(tmp_path, monkeypatch):
    _root, rd, store, driver, client, _srv, _api, commands = _fixture(tmp_path, monkeypatch)
    body = _body(client, commands[0])
    before = (rd / 'events.jsonl').read_bytes()
    real = EventStore.append_many
    monkeypatch.setattr(EventStore, 'append_many', lambda *a, **k: (_ for _ in ()).throw(OSError('before commit')))
    assert client.post('/api/runs/demo/pending-inject-cancellation', json=body).status_code == 503
    assert (rd / 'events.jsonl').read_bytes() == before and fold(store.read_all()).injects_done == 0
    monkeypatch.setattr(EventStore, 'append_many', real)
    assert client.post('/api/runs/demo/pending-inject-cancellation', json=body).status_code == 200
    assert fold(store.read_all()).injects_done == 1 and not driver.calls


def test_live_original_worker_cannot_respawn_after_cancel_commit_loses_record_save(tmp_path, monkeypatch):
    _root, rd, store, driver, client, srv, _api, _commands = _fixture(tmp_path, monkeypatch, paused=False, queued=0)
    admitted, release, finished = threading.Event(), threading.Event(), threading.Event()
    monitor = srv.commands._monitor

    def delayed_monitor(*args, **kwargs):
        admitted.set()
        assert release.wait(5)
        try:
            return monitor(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(srv.commands, '_monitor', delayed_monitor)
    command = _post(client, 'inject_node', {'idea': {'operator': 'manual', 'concepts': ['cpu/fixture']},
                                          'code': 'print(2)'}, key='active-before-cancel').json()
    try:
        assert admitted.wait(3)
        body = _body(client, command)
        driver.alive = False
        real_save = srv.commands._save
        monkeypatch.setattr(srv.commands, '_save', lambda *_: (_ for _ in ()).throw(OSError('after cancellation commit')))
        reply = client.post('/api/runs/demo/pending-inject-cancellation', json=body)
        assert reply.status_code == 503 and fold(store.read_all()).injects_done == 1
        monkeypatch.setattr(srv.commands, '_save', real_save)
    finally:
        release.set()
    assert finished.wait(5)
    record = srv.commands._load(srv.commands._path(rd, command['id']))
    assert record['status'] == 'failed' and record['error']['code'] == 'operator_cancelled', record
    assert record['pending_inject_cancellation']['request']['action_id'] == body['action_id']
    assert not driver.calls
    retry = client.post(f"/api/runs/demo/commands/{command['id']}/retry")
    assert retry.status_code in (200, 409) and not driver.calls


def test_torn_atomic_cancel_batch_keeps_cursor_unconsumed_and_gates_recovery(tmp_path, monkeypatch):
    _root, rd, store, driver, client, _srv, _api, commands = _fixture(tmp_path, monkeypatch)
    body = _body(client, commands[0])
    path = rd / 'events.jsonl'
    before = path.read_bytes()
    real = EventStore.append_many

    def interrupted_append(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        with path.open('r+b') as stream:
            stream.truncate(len(before) + 50)  # disposable torn final envelope, never a production repair
        raise OSError('torn publication')

    monkeypatch.setattr(EventStore, 'append_many', interrupted_append)
    assert client.post('/api/runs/demo/pending-inject-cancellation', json=body).status_code == 503
    damaged = path.read_bytes()
    assert damaged.startswith(before)
    assert fold(EventStore(path).read_all()).injects_done == 0
    assert client.post('/api/runs/demo/pending-inject-cancellation', json=body).status_code == 503
    assert client.get(f"/api/runs/demo/commands/{commands[0]['id']}").status_code == 503
    assert path.read_bytes() == damaged and not driver.calls


def test_unresolved_spawn_or_other_command_is_not_silently_retired(tmp_path, monkeypatch):
    _root, rd, _store, driver, client, srv, _api, commands = _fixture(tmp_path, monkeypatch)
    body = _body(client, commands[0])
    path = srv.commands._spawn_claim_path(rd)
    path.write_text(json.dumps({'command_id': 'old-unknown', 'pid': 99999999}))
    saved = path.read_bytes()
    reply = client.post('/api/runs/demo/pending-inject-cancellation', json=body)
    assert reply.status_code == 409 and reply.json()['detail']['code'] == 'engine_spawn_unresolved'
    assert path.read_bytes() == saved
    path.unlink()  # fixture-only removal of its own injected unknown claim
    monkeypatch.setattr(srv.commands, '_start_worker', lambda *a, **k: None)
    other = _post(client, 'resume', {}, key='other-active')
    assert other.status_code == 200 and other.json()['status'] == 'accepted'
    reply = client.post('/api/runs/demo/pending-inject-cancellation', json=body)
    assert reply.status_code == 409 and reply.json()['detail']['code'] == 'command_in_progress'
    assert not driver.calls


def test_registered_mcp_api_tool_uses_supported_cancellation_and_saved_action(tmp_path, monkeypatch):
    from looplab.harness.mcp_server import build_server
    _root, _rd, store, driver, client, _srv, api, commands = _fixture(tmp_path, monkeypatch)
    body = _body(client, commands[0])
    result = anyio.run(build_server(api).call_tool, 'api_request', {
        'method': 'POST', 'path': '/api/runs/demo/pending-inject-cancellation', 'body': body})
    assert not getattr(result, 'isError', False), result
    assert 'operator_cancelled' in str(result)
    assert fold(store.read_all()).injects_done == 1 and not driver.calls


def test_scoped_agent_cannot_cancel_internal_run_inject(tmp_path, monkeypatch):
    _root, rd, store, driver, client, _srv, api, commands = _fixture(tmp_path, monkeypatch)
    body = _body(client, commands[0])
    settings = json.loads((rd / 'config.snapshot.json').read_text())
    settings['external_harness'] = False
    (rd / 'config.snapshot.json').write_text(json.dumps(settings))
    before = (rd / 'events.jsonl').read_bytes()
    result = api.request('POST', '/api/runs/demo/pending-inject-cancellation', body)
    assert result['status'] == 403 and result['body']['detail']['code'] == 'agent_token_refused'
    assert (rd / 'events.jsonl').read_bytes() == before and fold(store.read_all()).injects_done == 0
    assert not driver.calls


def test_served_request_is_not_a_cancel_target_and_legacy_uid_is_explicitly_unavailable(tmp_path, monkeypatch):
    root, rd, store, driver, client, _srv, _api, commands = _fixture(tmp_path, monkeypatch)
    body = _body(client, commands[0])
    store.append('inject_done', {'idx': 0})  # the engine's existing served-receipt semantics
    store.append('node_created', {'node_id': 1, 'parent_ids': [], 'operator': 'manual',
                                  'idea': {'operator': 'manual'}, 'code': 'print(0)'})
    before = (rd / 'events.jsonl').read_bytes()
    refused = client.post('/api/runs/demo/pending-inject-cancellation', json=body)
    assert refused.status_code == 409 and refused.json()['detail']['code'] == 'inject_not_pending_head'
    assert (rd / 'events.jsonl').read_bytes() == before and fold(store.read_all()).injects_done == 1
    legacy = _seed(root, run_id='legacy')
    legacy_generation = client.get('/api/runs/legacy/state?observe_only=true').json()['generation']
    missing = client.get('/api/runs/legacy/pending-inject', params={
        'command_id': commands[0]['id'], 'expected_generation': legacy_generation})
    assert missing.status_code == 409 and missing.json()['detail']['code'] == 'run_uid_unavailable'
    assert not driver.calls


def test_live_mcp_schema_exposes_identity_fences_for_supported_semantic_action(tmp_path, monkeypatch):
    _root, _rd, _store, _driver, _client_, _srv, api, _commands = _fixture(tmp_path, monkeypatch)
    schema = api.schema('/api/runs/{run_id}/pending-inject-cancellation')
    assert 'PendingInjectCancellationBody' in str(schema)
    components = schema['components']
    body = components['PendingInjectCancellationBody']
    assert set(body['required']) == {'expected_generation', 'expected_run_uid', 'command_id',
                                    'request_index', 'request_digest', 'action_id', 'reason'}
