import json

from fastapi.testclient import TestClient

from looplab.adapters.toytask import ToyTask
from looplab.core.config import Settings
from looplab.engine.lessons import LessonMemory
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.serve.server import make_app
from factories import make_engine


def _run(tmp_path):
    rd = tmp_path / 'r'; rd.mkdir()
    (rd / 'config.snapshot.json').write_text(Settings(backend='toy', memory_dir=str(tmp_path / 'memory')).model_dump_json())
    (rd / 'task.snapshot.json').write_text(ToyTask().model_dump_json())
    store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'uid-r', 'task_id': 'toy_quadratic',
                                 'goal': ToyTask().goal, 'direction': 'min'})
    return rd, run_generation_token(store.read_all())


def test_operator_note_is_idempotent_and_reaches_prior_by_scope_not_task_id(tmp_path):
    rd, generation = _run(tmp_path)
    with TestClient(make_app(tmp_path)) as client:
        body = {'expected_generation': generation, 'action_id': 'note-1',
                'task_family': 'offline-toy-family', 'note': 'Measure the baseline before tuning.'}
        response = client.post('/api/runs/r/operator-notes', json=body)
        assert response.status_code == 200, response.text
        assert client.post('/api/runs/r/operator-notes', json=body).json()['replayed'] is True
        assert client.post('/api/runs/r/operator-notes', json={**body, 'note': 'changed'}).status_code == 409
    rows = (tmp_path / 'memory' / 'operator_notes.jsonl').read_text().splitlines()
    assert len(rows) == 1
    engine = make_engine(tmp_path / 'other', task=ToyTask(id='renamed-task'), reflection_priors=True,
                         memory_dir=str(tmp_path / 'memory'))
    memory = LessonMemory(engine)
    text = memory.load_reflection_priors(role='researcher')
    assert 'Measure the baseline before tuning.' in text
    assert 'offline-toy-family' in text
    receipt = memory.prior_receipts['researcher']
    assert receipt['operator_notes'] == [json.loads(rows[0])['note_id']]


def test_operator_note_does_not_cross_direction_or_task_fingerprint(tmp_path):
    rd, generation = _run(tmp_path)
    with TestClient(make_app(tmp_path)) as client:
        assert client.post('/api/runs/r/operator-notes', json={
            'expected_generation': generation, 'action_id': 'note-1',
            'task_family': 'offline-toy-family', 'note': 'Use this only for the toy family.'}).status_code == 200
    other = ToyTask(id='other-task', goal='classify images', direction='max')
    engine = make_engine(tmp_path / 'other', task=other, reflection_priors=True,
                         memory_dir=str(tmp_path / 'memory'))
    memory = LessonMemory(engine)
    text = memory.load_reflection_priors(role='researcher')
    assert 'Use this only for the toy family.' not in text
    assert memory.prior_receipts['researcher']['operator_notes'] == []
