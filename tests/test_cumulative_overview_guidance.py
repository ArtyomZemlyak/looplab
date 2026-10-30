"""External agent receives context and authors an update, without automatic prose or gates."""
import json
from fastapi.testclient import TestClient
from looplab.engine import resources
from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.harness.phases import phase_detail
from looplab.serve.server import make_app


def test_report_context_finds_new_terminal_event_and_real_reference_errors_then_update(tmp_path, monkeypatch):
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1'); monkeypatch.setenv('LOOPLAB_AGENT_EVENTS', '0')
    monkeypatch.setattr(resources, 'default_gpu_host_lease_path', lambda: tmp_path / 'fixture-lease')
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.delenv('LOOPLAB_UI_REQUIRE_AUTH', raising=False)
    rd = tmp_path / 'r'; rd.mkdir()
    (rd / 'config.snapshot.json').write_text(Settings(backend='toy', external_harness=True).model_dump_json())
    (rd / 'task.snapshot.json').write_text(json.dumps({'kind': 'quadratic', 'goal': 'g', 'direction': 'min'}))
    store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'compare', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    store.append('node_created', {'node_id': 0, 'operator': 'fit', 'parent_ids': [], 'idea': {'operator': 'fit'}})
    store.append('node_evaluated', {'node_id': 0, 'metric': None, 'extra_metrics': {'Recall@20': .4}})
    old = store.append('report_generated', {'at_node': 1, 'content': {'headline': 'First', 'campaign_overview': {
        'summary': 'Earlier study', 'recommendation': 'Keep control', 'recommendation_limits': 'One test',
        'next_directions': ['Check heldout'], 'report_seqs': [999]}}})
    # The same node count can get a later terminal event after a retry: at_node alone misses it.
    store.append('node_reset', {'node_id': 0, 'generation': 0, 'from_stage': 'eval'})
    outcome = store.append('node_evaluated', {'node_id': 0, 'generation': 1, 'metric': None, 'extra_metrics': {'Recall@20': .45}})
    with TestClient(make_app(tmp_path)) as c:
        args = {'expected_generation': gen}
        brief = c.get('/api/runs/r/research-briefing', params=args)
        assert brief.status_code == 200, brief.text
        context = brief.json()['report_context']
        assert context['previous_overview_seq'] == old.seq
        assert context['recent_terminal_events'][0]['event_seq'] == outcome.seq
        assert context['recent_terminal_events'][0]['extra_metrics'][0]['value'] == .45
        assert {w['code'] for w in context['warnings']} >= {'terminal_after_overview', 'invalid_report_reference'}
        assert 'no scientific truth' in context['meaning']
        assert c.get(context['previous_overview_api']).json()['content']['campaign_overview']['summary'] == 'Earlier study'
        assert c.get('/api/runs/r/harness-contract').json()['report_authoring']['canonical_workflow'].endswith('/research-workflow')
        progress = c.get('/api/runs/r/harness-progress', params=args).json()
        assert 'cumulative scientific overview' in progress['report_authoring']['instruction']
        assert progress['report_authoring']['context'].endswith('/research-briefing')
        assert 'cumulative' in phase_detail('report')['authoring_guidance']['instruction']
        updated = {'headline': 'Re-evaluation', 'summary': 'Recall was 0.45 on the recorded test.',
                   'campaign_overview': {'summary': 'Earlier result and re-evaluation', 'recommendation': 'No decision without matched test',
                      'recommendation_limits': 'Test identity unverified', 'next_directions': ['Compare same test and decide'], 'report_seqs': [old.seq]}}
        response = c.post('/api/runs/r/commands', headers={'Idempotency-Key': 'report-update'}, json={
            'type': 'report_generated', 'data': {'content': updated}, 'expected_generation': gen})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'succeeded'
        after = c.get('/api/runs/r/research-briefing', params=args).json()
        assert after['report_context']['previous_overview_seq'] == response.json()['event_seq']
        assert not {w['code'] for w in after['report_context']['warnings']} & {'terminal_after_overview', 'invalid_report_reference'}
        assert after['research']['campaign_overview']['content']['summary'] == 'Earlier result and re-evaluation'
        assert c.get('/api/runs/r/research-briefing', params={'expected_generation': '0' * 64}).status_code == 409


def test_old_report_without_overview_still_loads_and_advice_is_not_gate(tmp_path, monkeypatch):
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1'); monkeypatch.setenv('LOOPLAB_AGENT_EVENTS', '0')
    monkeypatch.setattr(resources, 'default_gpu_host_lease_path', lambda: tmp_path / 'fixture-lease')
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.delenv('LOOPLAB_UI_REQUIRE_AUTH', raising=False)
    rd = tmp_path / 'r'; rd.mkdir()
    (rd / 'config.snapshot.json').write_text(Settings(backend='toy', external_harness=True).model_dump_json())
    store = EventStore(rd / 'events.jsonl'); store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    old = store.append('report_generated', {'at_node': 0, 'content': {'headline': 'Legacy report'}})
    with TestClient(make_app(tmp_path)) as c:
        before = c.get('/api/runs/r/research-briefing', params={'expected_generation': gen}).json()
        assert before['report_context']['warnings'][0]['code'] == 'overview_missing'
        assert c.get(f'/api/runs/r/report/versions/{old.seq}').status_code == 200
        result = c.post('/api/runs/r/commands', headers={'Idempotency-Key': 'old-shape'}, json={
            'type': 'report_generated', 'data': {'content': {'headline': 'New legacy-shaped report'}}, 'expected_generation': gen})
        assert result.status_code == 200 and result.json()['status'] == 'succeeded'
