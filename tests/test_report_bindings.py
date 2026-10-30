import json
from fastapi.testclient import TestClient
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.serve.server import make_app


def test_report_binding_requires_correct_test_metric_and_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1'); monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    store.append('node_created', {'node_id': 0, 'operator': 'score', 'parent_ids': [], 'idea': {'operator': 'score'}})
    store.append('node_evaluated', {'node_id': 0, 'metric': None, 'extra_metrics': {'Product@200': .5}})
    report = store.append('report_generated', {'content': {'key_metrics': {'metrics': [{'key': 'Product@200', 'test_id': 'test1'}], 'nodes': [{'node_id': 0, 'node_generation': 0}]}}})
    directory = rd / 'per_query'; directory.mkdir()
    uploads = [{'upload_id': 'ok', 'run_uid': 'u', 'generation': gen, 'node_id': 0, 'node_generation': 0, 'test_id': 'test1', 'n_queries': 3, 'metrics': {'product_array': {'value': .5}}, 'consistency': {'status': 'matches_measured', 'metric': 'product_array', 'node_metric': 'Product@200'}},
               {'upload_id': 'wrong-test', 'run_uid': 'u', 'generation': gen, 'node_id': 0, 'node_generation': 0, 'test_id': 'test2', 'metrics': {'Product@200': {'value': .5}}}]
    (directory / 'manifest.jsonl').write_text(''.join(json.dumps(u) + '\n' for u in uploads))
    with TestClient(make_app(tmp_path)) as c:
        url = f'/api/runs/r/report/versions/{report.seq}/metric-bindings'
        response = c.get(url, params={'expected_generation': gen})
        assert response.status_code == 200, response.text
        b = response.json()['bindings'][0]
        assert len(b['uploads']) == 1 and b['uploads'][0]['upload_id'] == 'ok'
        assert b['uploads'][0]['consistency'] == 'matches_measured' and b['uploads'][0]['attempt_verified']
        assert c.get(url, params={'expected_generation': '0' * 64}).status_code == 409
        uploads[0]['node_generation'] = 7
        (directory / 'manifest.jsonl').write_text(''.join(json.dumps(u) + '\n' for u in uploads))
        u = c.get(url, params={'expected_generation': gen}).json()['bindings'][0]['uploads'][0]
        assert not u['attempt_verified'] and u['consistency'] == 'unchecked'
