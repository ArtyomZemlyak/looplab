from types import SimpleNamespace

from looplab.core.models import Event
from looplab.engine.node_build import NodeBuildMixin
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def test_real_emitter_preserves_role_and_old_rows_stay_unclassified(tmp_path):
    store = EventStore(tmp_path / 'events.jsonl')
    engine = SimpleNamespace(store=store)
    NodeBuildMixin._emit_node_created(
        engine, node_id=1, parent_ids=[], operator='fit',
        idea={'operator': 'fit', 'rationale': 'Train the candidate'},
        code='', files={'train.py': 'print(1)'}, node_kind='artifact', job_role='fit')
    row = store.read_all()[0]
    assert row.data['job_role'] == 'fit'
    state = fold(store.read_all())
    assert state.nodes[1].job_role == 'fit'
    assert state.nodes[1].kind == 'artifact'
    assert state.model_dump(mode='json')['nodes']['1']['job_role'] == 'fit'
    rebuilt = NodeBuildMixin._rebuild_identity(state.nodes[1], state)
    assert rebuilt['job_role'] == 'fit' and rebuilt['node_kind'] == 'artifact'

    legacy = [Event(seq=1, ts=100, type='node_created', data={
        'node_id': 0, 'parent_ids': [], 'operator': 'fit', 'idea': {'operator': 'fit'},
        'code': '', 'files': {}})]
    assert fold(legacy).nodes[0].job_role is None


def test_public_node_times_follow_current_lifecycle_and_reset():
    events = [
        Event(seq=1, ts=100, type='node_created', data={
            'node_id': 1, 'parent_ids': [], 'operator': 'fit',
            'idea': {'operator': 'fit'}, 'code': '', 'files': {}, 'job_role': 'fit'}),
        Event(seq=2, ts=120, type='node_eval_started', data={'node_id': 1, 'generation': 0}),
        Event(seq=3, ts=140, type='node_evaluated', data={
            'node_id': 1, 'generation': 0, 'metric': .5, 'extra_metrics': {}, 'eval_seconds': 20}),
        Event(seq=4, ts=160, type='node_reset', data={
            'node_id': 1, 'generation': 0, 'from_stage': 'eval'}),
        Event(seq=5, ts=200, type='node_created', data={
            'node_id': 1, 'generation': 1, 'parent_ids': [], 'operator': 'fit',
            'idea': {'operator': 'fit'}, 'code': '', 'files': {}, 'job_role': 'fit'}),
    ]
    completed = fold(events[:3]).nodes[1]
    assert (completed.created_at, completed.eval_started_at, completed.completed_at) == (100, 120, 140)
    assert not {'created_at', 'eval_started_at', 'completed_at'} & completed.model_dump(mode='json').keys()
    from looplab.serve.node_activity import public_node_times
    assert public_node_times(completed) == {'created_at': 100, 'eval_started_at': 120, 'completed_at': 140}
    reset = fold(events[:4]).nodes[1]
    assert reset.created_at == 100 and reset.completed_at is None and reset.eval_started_at is None
    rebuilt = fold(events).nodes[1]
    assert rebuilt.attempt == 1 and rebuilt.created_at == 200
    assert rebuilt.completed_at is None and rebuilt.eval_started_at is None
    assert rebuilt.job_role == 'fit'
    legacy = fold([events[0].model_copy(update={'data': {k: v for k, v in events[0].data.items() if k != 'job_role'}})]).nodes[1]
    assert legacy.created_at == 100 and legacy.completed_at is None and legacy.eval_started_at is None


def test_state_and_node_http_publish_times_without_replaying_their_projection(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    rd = tmp_path / 'r'; rd.mkdir()
    store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    created = store.append('node_created', {'node_id': 0, 'parent_ids': [], 'operator': 'fit',
                                          'idea': {'operator': 'fit'}, 'job_role': 'fit'})
    started = store.append('node_eval_started', {'node_id': 0, 'generation': 0})
    finished = store.append('node_evaluated', {'node_id': 0, 'generation': 0, 'metric': .5, 'extra_metrics': {}})
    with TestClient(make_app(tmp_path)) as client:
        public = client.get('/api/runs/r/state').json()['state']['nodes']['0']
        assert public['created_at'] == created.ts
        assert public['eval_started_at'] == started.ts
        assert public['completed_at'] == finished.ts
        detail = client.get('/api/runs/r/nodes/0').json()
        assert detail['created_at'] == created.ts
        assert detail['eval_started_at'] == started.ts
        assert detail['completed_at'] == finished.ts
    replayed = fold(store.read_all()).model_dump(mode='json')['nodes']['0']
    assert not {'created_at', 'eval_started_at', 'completed_at'} & replayed.keys()


import pytest

from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.harness import composite
from tests.factories import make_engine
from tests.test_campaign_composite import _setup


@pytest.mark.parametrize('job_role', ['score', None])
def test_api_injected_job_role_reaches_materialized_node(tmp_path, monkeypatch, job_role):
    _, run_dir, store, api, _, _ = _setup(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    result = composite.submit_experiment(
        api, 'camp', generation, key='materialize-role',
        idea={'operator': 'manual', 'rationale': 'exercise materialization'},
        code="print('ready')", job_role=job_role)
    assert result['ok'], result
    req = next(e.data for e in store.read_all() if e.type == 'inject_node')
    assert req.get('job_role') == job_role
    store.append('resume', {})
    engine = make_engine(run_dir, n_seeds=1, max_nodes=8)
    engine._create_injected_node(req)
    events = engine.store.read_all()
    created = [e for e in events if e.type == 'node_created' and e.data.get('source') == 'manual']
    assert len(created) == 1
    assert created[0].data.get('job_role') == job_role
    assert ('job_role' in created[0].data) == (job_role is not None)
    node = fold(events).nodes[created[0].data['node_id']]
    assert node.job_role == job_role
    assert not any(e.type == 'node_failed' and e.data.get('reason') == 'build_crash' for e in events)
    # An implement rebuild also forwards identity via the same emitter.
    engine.store.append('node_failed', {'node_id': node.id, 'generation': 0,
                                        'error': 'fixture failure', 'reason': 'crash',
                                        'eval_seconds': 0.0})
    engine.store.append('node_reset', {'node_id': node.id, 'generation': 0,
                                      'from_stage': 'implement'})
    state = fold(engine.store.read_all())
    engine._rerun_node(state.nodes[node.id], state)
    events = engine.store.read_all()
    rebuild = [e for e in events if e.type == 'node_created'
               and e.data.get('node_id') == node.id and e.data.get('generation') == 1]
    assert len(rebuild) == 1
    assert rebuild[0].data.get('job_role') == job_role
    assert ('job_role' in rebuild[0].data) == (job_role is not None)
    assert fold(events).nodes[node.id].job_role == job_role
