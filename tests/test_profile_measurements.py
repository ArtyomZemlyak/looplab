import json
from fastapi.testclient import TestClient
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.serve.server import make_app


def test_profile_values_respect_keys_missing_and_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    rd = tmp_path / 'r'; rd.mkdir()
    s = EventStore(rd / 'events.jsonl')
    s.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(s.read_all())
    s.append('node_created', {'node_id': 0, 'operator': 'model', 'parent_ids': [], 'idea': {'operator': 'model'}})
    s.append('node_evaluated', {'node_id': 0, 'metric': None, 'extra_metrics': {'Product@200': .5, 'repeat': .2}})
    (tmp_path / 'projects.json').write_text(json.dumps({'assignments': {'r': 'p'}, 'projects': [{'id': 'p', 'metric_profile': {'name': 'Recommendation', 'metrics': [{'key': 'Product@200', 'role': 'primary', 'denominator': 'all GT'}, {'key': 'missing', 'role': 'secondary'}], 'slices': ['repeat', 'explore']}}]}))
    with TestClient(make_app(tmp_path)) as c:
        url = '/api/runs/r/profile-measurements'
        result = c.get(url, params={'expected_generation': gen})
        assert result.status_code == 200, result.text
        body = result.json(); values = body['nodes'][0]['values']
        assert values[0]['value'] == .5 and values[0]['source'] == 'node.extra_metrics'
        assert values[1]['value'] is None and values[1]['source'] == 'not_measured'
        assert body['profile']['profile']['metrics'][0]['denominator'] == 'all GT'
        assert c.get(url, params={'expected_generation': '0' * 64}).status_code == 409
