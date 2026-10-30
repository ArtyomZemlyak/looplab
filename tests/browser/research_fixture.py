"""Isolated CPU-only server fixture for research campaign browser smoke."""
import base64
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient
from looplab.core.config import Settings
from looplab.core.metric_profile import profile_digest
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.serve.projects import ProjectStore
from looplab.serve.server import make_app

root = Path(os.environ['LOOPLAB_BROWSER_FIXTURE_ROOT']); rd = root / 'browser-campaign'; rd.mkdir(parents=True, exist_ok=True)
(rd / 'config.snapshot.json').write_text(Settings(backend='toy', external_harness=True).model_dump_json())
task = json.loads((Path(__file__).resolve().parents[2] / 'examples' / 'toy_task.json').read_text())
(rd / 'task.snapshot.json').write_text(json.dumps(task))
store = EventStore(rd / 'events.jsonl')
store.append('run_started', {'run_id': rd.name, 'run_uid': 'browser-fixture', 'task_id': 'toy', 'goal': 'Study context on held-out examples', 'direction': 'max'})
store.append('hypothesis_added', {'statement': 'Does context improve the result?', 'source': 'deep_research'})
card = next(c for c in fold(store.read_all()).cards.values() if c.statement == 'Does context improve the result?')
gen = run_generation_token(store.read_all())
project_store = ProjectStore(root / 'projects.json')
project = project_store.create('Synthetic vision'); project_store.assign(rd.name, project.id)
project_store.put_metric_profile(project.id, {'name': 'Vision profile', 'metrics': [{'key': 'IoU', 'role': 'primary',
    'denominator': 'union pixels'}], 'slices': ['small objects']}, profile_digest(None))
os.environ['LOOPLAB_UI_ANONYMOUS'] = '1'; os.environ['LOOPLAB_AGENT_EVENTS'] = '0'
os.environ.pop('LOOPLAB_UI_TOKEN', None); os.environ.pop('LOOPLAB_UI_REQUIRE_AUTH', None)
app = make_app(root)
experiment = {'experiment_id': 'exp1', 'title': 'Context comparison', 'question': 'Does context help?',
    'question_card_id': card.id, 'hypothesis': 'Context improves score', 'primary_metric': 'IoU',
    'arms': {'control': {'node_id': 0}, 'treatment': {'node_id': 1}}, 'tests': [{'label': 'heldout'}],
    'metrics': [{'name': 'IoU', 'kind': 'mean', 'direction': 'max'}],
    'contrasts': [{'control': 'control', 'treatment': 'treatment', 'metric': 'IoU', 'test': 'heldout'}]}
with TestClient(app) as client:
    registration = client.post(f'/api/runs/{rd.name}/protocol/amendments', json={
        'expected_generation': gen, 'action_id': 'browser-registration', 'kind': 'experiment',
        'reason': 'Synthetic preregistration before results', 'experiment': experiment})
    if registration.status_code != 200:
        raise RuntimeError(registration.text)
    for node, score in [(0, 0.5), (1, 0.6)]:
        store.append('node_created', {'node_id': node, 'operator': 'score', 'parent_ids': [],
            'idea': {'operator': 'score', 'hypothesis': 'Context improves the score', 'parent_card_id': card.id}})
        store.append('node_evaluated', {'node_id': node, 'metric': score, 'extra_metrics': {'IoU': score}})
    related_seq = store.read_all()[-1].seq + 1
    old = store.append('report_generated', {'at_node': 2, 'content': {'headline': 'Earlier result',
        'body_markdown': 'Earlier detailed evidence.', 'campaign_overview': {'summary': 'Context study overview',
        'learnings': ['First measured outcome'], 'report_seqs': [related_seq]}}})
    store.append('report_generated', {'at_node': 2, 'content': {'headline': 'Later result', 'body_markdown': 'Later detailed evidence.'}})
    for node, arm, values in [(0, 'control', [0.2, 0.4, 0.6, 0.8]), (1, 'treatment', [0.3, 0.5, 0.7, 0.9])]:
        data = {'keys': ['q1', 'q2', 'q3', 'q4'], 'users': ['u1', 'u2', 'u3', 'u4'], 'score': values}
        body = {'expected_generation': gen, 'action_id': f'browser-upload-{node}', 'node_id': node, 'format': 'json',
                'content_b64': base64.b64encode(json.dumps(data).encode()).decode(),
                'metrics': {'IoU': {'kind': 'mean', 'array': 'score'}}, 'test_label': 'heldout',
                'experiment_id': 'exp1', 'arm': arm, 'measured_check': {'metric': 'IoU', 'node_metric': 'IoU'}}
        response = client.post(f'/api/runs/{rd.name}/per-query', json=body)
        if response.status_code != 200:
            raise RuntimeError(response.text)
    comparison = client.get(f'/api/runs/{rd.name}/experiments/exp1/comparison')
    if comparison.status_code != 200 or not comparison.json()['contrasts'][0].get('registered_before_result'):
        raise RuntimeError('Synthetic comparison was not genuinely preregistered: ' + comparison.text)
(root / 'fixture.json').write_text(json.dumps({'generation': gen, 'old_report_seq': old.seq}))
