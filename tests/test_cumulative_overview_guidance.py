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
        # An updated overview alone is not a complete ordinary report: the exact problem observed
        # in production must reach the agent via both briefing and report guidance.
        missing = after['research']['quality']['report_fields_empty']
        assert {'verdict', 'champion_summary', 'key_metrics'} <= set(missing)
        assert after['report_context']['report_fields_empty'] == missing
        assert {'verdict', 'champion_summary', 'key_metrics'} <= {
            w['field'] for w in after['report_context']['warnings'] if w['code'] == 'report_field_empty'}
        version = c.get(f"/api/runs/r/report/versions/{response.json()['event_seq']}").json()
        assert not version['content'].get('verdict')
        corrected = dict(updated, verdict='No reliable winner yet', champion_summary='No justified champion: test identity unverified',
            what_worked=['Recorded Recall@20 after retry is 0.45'], what_didnt=['No matched comparison'],
            learnings=['The retry is measured but not independently matched'],
            next_directions=['Compare against a control on the same test'], caveats=['Cohort and policy remain unverified'],
            key_metrics={'metrics': [{'key': 'Recall@20', 'label': 'Recall@20', 'test_id': 'test'}],
                         'nodes': [{'node_id': 0, 'label': 'Retried model', 'node_generation': 1}]})
        corrected_response = c.post('/api/runs/r/commands', headers={'Idempotency-Key': 'report-update-corrected'}, json={
            'type': 'report_generated', 'data': {'content': corrected}, 'expected_generation': gen})
        assert corrected_response.status_code == 200 and corrected_response.json()['status'] == 'succeeded'
        final = c.get('/api/runs/r/research-briefing', params=args).json()
        assert final['report_context']['current_report_seq'] == corrected_response.json()['event_seq']
        assert final['research']['quality']['report_fields_empty'] == []
        assert final['report_context']['report_fields_empty'] == []
        assert final['research']['campaign_overview']['content']['report_seqs'] == [old.seq]
        assert final['research']['campaign_overview']['content']['summary'] == 'Earlier result and re-evaluation'
        saved = c.get(f"/api/runs/r/report/versions/{corrected_response.json()['event_seq']}").json()['content']
        assert saved['key_metrics']['metrics'][0]['key'] == 'Recall@20'
        assert saved['key_metrics']['nodes'][0]['node_generation'] == 1
        assert final['report_context']['recent_terminal_events'][0]['metric'] is None
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


def test_terminal_notification_and_phase_point_to_same_advisory_context():
    from looplab.serve.agent_events import compose_message
    row = {'id': 1, 'kind': 'node_evaluated', 'run_id': 'r', 'summary': 'node evaluated'}
    message = compose_message([row], limit_chars=2000)
    assert 'research_briefing.report_context' in message
    assert 'not an execution gate' in message
    phase = phase_detail('result_summary')
    assert any('research-briefing' in path for path in phase['reads'])
    assert any('research-workflow' in path for path in phase['reads'])
    ordinary = compose_message([{'id': 2, 'kind': 'stage_failed', 'run_id': 'r', 'summary': 'retry'}], limit_chars=2000)
    assert 'report_context' not in ordinary


def test_report_context_is_bounded_without_guessing_missing_attempt(tmp_path):
    from looplab.harness.report_guidance import snapshot
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    for n in range(15):
        store.append('node_created', {'node_id': n, 'operator': 'score', 'parent_ids': [], 'idea': {'operator': 'score'}})
        store.append('node_evaluated', {'node_id': n, 'metric': None, 'extra_metrics': {('m' * 300) + str(i): i for i in range(20)}})
    context = snapshot(rd, gen)
    assert context['terminal_event_count'] == 15 and context['terminal_events_truncated']
    assert len(context['recent_terminal_events']) == 10
    assert all(len(event['extra_metrics']) <= 8 and event['recorded_attempt'] is None for event in context['recent_terminal_events'])
    assert len(json.dumps(context).encode()) < 64 * 1024


def test_empty_metric_reference_envelope_is_not_reported_as_filled():
    from looplab.harness.research_quality import report_fields_empty
    assert 'key_metrics' in report_fields_empty({'key_metrics': {'metrics': [], 'nodes': [], 'baseline_node_id': None}})
    assert 'key_metrics' in report_fields_empty({'key_metrics': {'metrics': [{'key': 'm'}], 'nodes': []}})
    assert 'key_metrics' not in report_fields_empty({'key_metrics': {'metrics': [{'key': 'm'}], 'nodes': [{'node_id': 0}]}})


def test_authoring_contract_points_to_post_publication_readback():
    from looplab.harness.report_guidance import REPORT_INSTRUCTION
    from pathlib import Path
    assert 'report_fields_empty' in REPORT_INSTRUCTION and 'saved report' in REPORT_INSTRUCTION
    guide = (Path(__file__).parents[1] / 'looplab/harness/external-research-workflow.md').read_text()
    for field in ('verdict', 'champion_summary', 'what_worked', 'what_didnt', 'learnings', 'next_directions', 'caveats', 'key_metrics'):
        assert f'`{field}`' in guide
    assert 'extra_metrics' in guide and 'metric-bindings' in guide and 'report_fields_empty' in guide


def test_engineering_only_report_is_advisory_without_fabricated_science(tmp_path, monkeypatch):
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    monkeypatch.setenv('LOOPLAB_AGENT_EVENTS', '0')
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    monkeypatch.delenv('LOOPLAB_UI_REQUIRE_AUTH', raising=False)
    monkeypatch.setattr(resources, 'default_gpu_host_lease_path', lambda: tmp_path / 'fixture-lease')
    rd = tmp_path / 'r'; rd.mkdir()
    (rd / 'config.snapshot.json').write_text(Settings(backend='toy', external_harness=True).model_dump_json())
    store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'repair', 'direction': 'min'})
    gen = run_generation_token(store.read_all())
    store.append('node_created', {'node_id': 0, 'operator': 'repair', 'parent_ids': [], 'idea': {'operator': 'repair'}})
    store.append('node_failed', {'node_id': 0, 'reason': 'configuration error'})
    with TestClient(make_app(tmp_path)) as client:
        response = client.post('/api/runs/r/commands', headers={'Idempotency-Key': 'technical-report'}, json={
            'type': 'report_generated', 'data': {'content': {'headline': 'Configuration repair',
                'verdict': 'No scientific result or champion: setup failed',
                'caveats': ['No measurements were produced']}}, 'expected_generation': gen})
        assert response.status_code == 200 and response.json()['status'] == 'succeeded'
        body = client.get('/api/runs/r/research-briefing', params={'expected_generation': gen}).json()
        assert 'key_metrics' in body['report_context']['report_fields_empty']
        saved = client.get(f"/api/runs/r/report/versions/{response.json()['event_seq']}").json()['content']
        assert 'key_metrics' not in saved and 'no scientific result' in saved['verdict'].lower()
        assert not body['research']['experiments']['items']
