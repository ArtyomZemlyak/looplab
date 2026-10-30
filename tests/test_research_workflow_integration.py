"""Cross-surface CPU scenario over a real isolated native journal and synthetic arrays."""
import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient
from looplab.engine import resources
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.serve.server import make_app


def test_campaign_reconnect_and_replay_across_research_surfaces(tmp_path, monkeypatch):
    monkeypatch.setenv('LOOPLAB_BROWSER_FIXTURE_ROOT', str(tmp_path))
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    monkeypatch.setenv('LOOPLAB_AGENT_EVENTS', '0')
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    monkeypatch.delenv('LOOPLAB_UI_REQUIRE_AUTH', raising=False)
    monkeypatch.setattr(resources, 'default_gpu_host_lease_path', lambda: tmp_path / 'fixture-lease')
    fixture = Path(__file__).parent / 'browser' / 'research_fixture.py'
    spec = importlib.util.spec_from_file_location('isolated_research_fixture', fixture)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    rd, gen, store = module.rd, module.gen, module.store
    store.append('hypothesis_added', {'statement': 'Does a smaller model suffice?', 'source': 'deep_research'})
    store.append('node_created', {'node_id': 2, 'operator': 'score', 'parent_ids': [],
                                  'idea': {'operator': 'score', 'rationale': 'Saved model score'}})
    store.append('node_evaluated', {'node_id': 2, 'metric': None, 'extra_metrics': {'IoU': 0.42}})
    store.append('node_created', {'node_id': 3, 'operator': 'repair', 'parent_ids': [],
                                  'idea': {'operator': 'repair', 'rationale': 'Retry setup failure'}})
    store.append('node_failed', {'node_id': 3, 'reason': 'Synthetic setup failure'})
    store.append('node_reset', {'node_id': 3, 'generation': 0, 'from_stage': 'eval'})
    store.append('hint', {'text': 'Historical priority', 'replace': True})
    store.append('hint', {'text': 'Use the smaller-model direction next', 'replace': True})
    before = (rd / 'events.jsonl').read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        args = {'expected_generation': gen}
        brief = client.get('/api/runs/browser-campaign/research-briefing', params=args).json()
        assert brief['operator_directions']['items'][-1] == 'Use the smaller-model direction next'
        assert brief['metric_profile']['profile']['metrics'][0]['key'] == 'IoU'
        assert brief['research']['campaign_overview']['content']['summary'] == 'Context study overview'
        refs = brief['report_context']['comparison_refs']
        assert any(x['experiment_id'] == 'exp1' and x['node_ids'] == [0, 1] for x in refs)
        assert any(x['comparison_api'].endswith('/experiments/exp1/comparison') for x in refs)
        measurements = brief['research']['recent_profile_measurements']
        assert any(n['node_id'] == 2 and n['values'][0]['value'] == .42 for n in measurements['nodes'])
        assert measurements['caveat']
        page = client.get('/api/runs/browser-campaign/research-map', params=args).json()
        assert any(c['statement'] == 'Does a smaller model suffice?' for c in page['cards']['items'])
        missing_scalar = next(n for n in page['nodes']['items'] if n['node_id'] == 2)
        assert missing_scalar['metric_present'] is False and missing_scalar['extra_metric_keys'] == ['IoU']
        assert len(page['reports']['items']) == 2
        assert page['campaign_overview']['content']['summary'] == 'Context study overview'
        comparison = client.get('/api/runs/browser-campaign/experiments/exp1/comparison').json()
        assert comparison['contrasts'][0]['status'] == 'computed'
        assert comparison['contrasts'][0]['registered_before_result']
        assert client.get('/api/runs/browser-campaign/research-map', params={'expected_generation': '0' * 64}).status_code == 409
        review = client.post('/api/runs/browser-campaign/research-memo-review', json={
            'expected_generation': gen, 'purpose': 'scientific', 'memo': {}}).json()
        assert 'missing_conclusion' in {x['code'] for x in review['issues']}
        technical = client.post('/api/runs/browser-campaign/research-memo-review', json={
            'expected_generation': gen, 'purpose': 'technical', 'memo': {'summary': 'Retry diagnosed'}}).json()
        assert technical['issues'] == []
        handoff = {'expected_generation': gen, 'action_id': 'synthetic-ready', 'recipient_hook': 'fixture',
                   'reason': 'Synthetic next action ready', 'next_read': '/api/runs/browser-campaign/research-briefing'}
        first = client.post('/api/runs/browser-campaign/agent-handoff', json=handoff).json()
        again = client.post('/api/runs/browser-campaign/agent-handoff', json=handoff).json()
        assert first['event_id'] == again['event_id'] and again['replayed']
    assert (rd / 'events.jsonl').read_bytes() == before
    replayed = fold(EventStore(rd / 'events.jsonl').read_all())
    assert replayed.nodes[2].extra_metrics['IoU'] == 0.42 and replayed.nodes[2].metric is None
    with TestClient(make_app(tmp_path)) as restarted:
        reopened = restarted.get('/api/runs/browser-campaign/research-map', params={'expected_generation': gen}).json()
        assert reopened['campaign_overview']['content']['summary'] == 'Context study overview'
        events = restarted.get('/api/agent-events', params={'after': -1, 'timeout': 0}).json()['events']
        assert sum(e.get('id') == first['event_id'] for e in events) == 1


def test_public_schema_and_local_cli_agree_with_packaged_workflow(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from looplab.cli import app
    monkeypatch.setenv('LOOPLAB_BROWSER_FIXTURE_ROOT', str(tmp_path))
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1'); monkeypatch.setenv('LOOPLAB_AGENT_EVENTS', '0')
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.delenv('LOOPLAB_UI_REQUIRE_AUTH', raising=False)
    monkeypatch.setattr(resources, 'default_gpu_host_lease_path', lambda: tmp_path / 'fixture-lease')
    spec = importlib.util.spec_from_file_location('isolated_research_fixture_schema', Path(__file__).parent / 'browser' / 'research_fixture.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    with TestClient(make_app(tmp_path)) as client:
        paths = client.get('/openapi.json').json()['paths']
        for path in ('/api/runs/{run_id}/research-workflow', '/api/runs/{run_id}/research-briefing',
                     '/api/runs/{run_id}/research-map', '/api/runs/{run_id}/research-export',
                     '/api/runs/{run_id}/readiness-bindings', '/api/projects/{pid}/metric-profile'):
            assert path in paths
    result = CliRunner().invoke(app, ['research-map', str(module.rd), '--generation', module.gen, '--briefing'])
    assert result.exit_code == 0, result.output
    import json
    assert json.loads(result.stdout)['metric_profile']['profile']['metrics'][0]['key'] == 'IoU'
    help_result = CliRunner().invoke(app, ['research-map', '--help'])
    assert help_result.exit_code == 0 and '--briefing' in help_result.stdout


def test_technical_comment_stays_out_of_research_memos(tmp_path, monkeypatch):
    import time
    monkeypatch.setenv('LOOPLAB_BROWSER_FIXTURE_ROOT', str(tmp_path))
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1'); monkeypatch.setenv('LOOPLAB_AGENT_EVENTS', '0')
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.delenv('LOOPLAB_UI_REQUIRE_AUTH', raising=False)
    monkeypatch.setattr(resources, 'default_gpu_host_lease_path', lambda: tmp_path / 'fixture-lease')
    spec = importlib.util.spec_from_file_location('isolated_research_fixture_comment', Path(__file__).parent / 'browser' / 'research_fixture.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    with TestClient(make_app(tmp_path)) as client:
        before = fold(EventStore(module.rd / 'events.jsonl').read_all())
        result = client.post('/api/runs/browser-campaign/commands', headers={'Idempotency-Key': 'technical-comment-1'}, json={
            'expected_generation': module.gen, 'type': 'comment_created',
            'data': {'node_id': 0, 'node_generation': 0, 'text': 'Resolved a technical setup issue.'}})
        assert result.status_code == 200, result.text
        for _ in range(50):
            receipt = client.get('/api/runs/browser-campaign/command-receipt', params={'expected_generation': module.gen},
                headers={'Idempotency-Key': 'technical-comment-1'}).json()
            if receipt['terminal']:
                break
            time.sleep(.02)
        assert receipt['command']['status'] == 'succeeded'
        after = fold(EventStore(module.rd / 'events.jsonl').read_all())
        assert len(after.research) == len(before.research)
        assert any(comment.text == 'Resolved a technical setup issue.' for comment in after.comments.values())


def test_briefing_project_language_and_explicit_run_override(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv('LOOPLAB_BROWSER_FIXTURE_ROOT', str(tmp_path))
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1'); monkeypatch.setenv('LOOPLAB_AGENT_EVENTS', '0')
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False); monkeypatch.delenv('LOOPLAB_UI_REQUIRE_AUTH', raising=False)
    monkeypatch.setattr(resources, 'default_gpu_host_lease_path', lambda: tmp_path / 'fixture-lease')
    spec = importlib.util.spec_from_file_location('isolated_language_fixture', Path(__file__).parent / 'browser' / 'research_fixture.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    projects = tmp_path / 'projects.json'
    data = json.loads(projects.read_text())
    data['projects'][0]['metric_profile']['authoring_language'] = 'ru'
    projects.write_text(json.dumps(data))
    with TestClient(make_app(tmp_path)) as client:
        url = '/api/runs/browser-campaign/research-briefing'; params = {'expected_generation': module.gen}
        language = client.get(url, params=params).json()['authoring_language']
        assert language['language'] == 'ru' and language['run_config'] == 'auto'
        config = module.rd / 'config.snapshot.json'; settings = json.loads(config.read_text())
        settings['output_language'] = 'en'; config.write_text(json.dumps(settings))
        language = client.get(url, params=params).json()['authoring_language']
        assert language['language'] == 'en' and language['project_preference'] == 'ru'
