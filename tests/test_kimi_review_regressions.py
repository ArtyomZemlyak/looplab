"""Behavioral regressions found in the independent 5 November branch review."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from looplab.core.errors import ConfigRefusal
from looplab.core.run_deletion import publish_run_deletion_fence
from looplab.events.eventstore import EventStore
from looplab.harness.mcp_server import HarnessAPI
from looplab.maintenance.relocation import relocate_run
from looplab.serve.appstate import AppState


def _run(path):
    path.mkdir(parents=True)
    EventStore(path / 'events.jsonl').append('run_started', {
        'run_id': path.name, 'run_uid': 'uid-' + path.name,
        'task_id': 't', 'goal': 'g', 'direction': 'max'})
    return path


def test_surviving_deletion_fence_is_visible_after_quarantine_move(tmp_path):
    rd = _run(tmp_path / 'gone')
    publish_run_deletion_fence(rd, operation_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
                              expected_generation='b' * 64, expected_seq=0,
                              receipt_name='receipt.json')
    rd.rename(tmp_path / 'quarantine')
    with pytest.raises(HTTPException) as raised:
        AppState.run_dir(SimpleNamespace(root=tmp_path), 'gone')
    assert raised.value.status_code == 410
    assert raised.value.detail['code'] == 'run_deletion_in_progress'


def test_coalescing_never_promotes_old_direction_over_new_stop():
    rows = [{'id': 1, 'kind': 'operator_direction', 'run_id': 'a', 'summary': 'continue'},
            {'id': 2, 'kind': 'run_paused', 'run_id': 'a', 'summary': 'STOP'}]
    api = HarnessAPI('http://testserver', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'events': rows, 'next_after': 2})))
    body = api.wait_for_next(0, 0)['body']
    assert body['events'][0]['id'] == 2


def test_coalescing_does_not_discard_unrelated_run_payloads():
    rows = [{'id': 1, 'kind': 'agent_handoff', 'run_id': 'other',
             'summary': 'inspect', 'detail': {'receipt': 'needed-original-payload'}}]
    rows += [{'id': n, 'kind': 'operator_direction', 'run_id': 'main', 'summary': 'latest'}
             for n in range(2, 26)]
    api = HarnessAPI('http://testserver', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'events': rows, 'next_after': 25})))
    body = api.wait_for_next(0, 0)['body']
    assert any(row.get('run_id') == 'other' and row.get('detail') == rows[0]['detail']
               for row in body['events']), 'a global cursor must not consume another run silently'


def test_relocation_does_not_replace_a_raced_destination(tmp_path, monkeypatch):
    source = _run(tmp_path / 'old')
    target = tmp_path / 'new'
    import looplab.maintenance.relocation as relocation
    original = relocation._destination
    def race(src, dst):
        selected = original(src, dst)
        target.mkdir()  # another launch reserved this exact directory after validation
        return selected
    monkeypatch.setattr(relocation, '_destination', race)
    with pytest.raises(ConfigRefusal):
        relocate_run(source, target, apply=True)
    assert source.exists() and target.exists()
    assert not (target / 'events.jsonl').exists()


@pytest.mark.posix_only('symlink')
def test_relocation_refuses_source_symlink_before_resolving(tmp_path):
    source = _run(tmp_path / 'real')
    alias = tmp_path / 'alias'
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(ConfigRefusal):
        relocate_run(alias, tmp_path / 'new', apply=True)
    assert source.exists()


def test_real_prior_loading_works_without_optional_fastapi(tmp_path):
    import subprocess
    import sys
    script = '''
import importlib.abc
import json
import sys
from pathlib import Path
class NoFastAPI(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'fastapi' or fullname.startswith('fastapi.'):
            raise ModuleNotFoundError('optional fastapi deliberately unavailable')
sys.meta_path.insert(0, NoFastAPI())
sys.path.insert(0, 'tests')
from factories import make_engine
from looplab.engine.lessons import LessonMemory
from looplab.engine.operator_notes import scope_fingerprint
from looplab.adapters.toytask import ToyTask
base = Path(sys.argv[1]); base.mkdir()
task = ToyTask()
row = dict(record_kind='operator_note', note_id='on-test', note='Use the saved baseline.',
           task_family='toy', direction=task.direction, fingerprint=scope_fingerprint(task))
(base / 'operator_notes.jsonl').write_text(json.dumps(row) + '\\n')
engine = make_engine(base / 'other', task=task, reflection_priors=True, memory_dir=str(base))
text = LessonMemory(engine).load_reflection_priors(role='researcher')
assert 'Use the saved baseline.' in text
assert 'fastapi' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'memory')],
                            text=True, capture_output=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr


def test_nested_research_http_keeps_root_profile_and_links_with_leaf_decoy(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from looplab.adapters.toytask import ToyTask
    from looplab.core.config import Settings
    from looplab.core.run_ref import run_address
    from looplab.events.run_generation import run_generation_token
    from looplab.serve.server import make_app
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    rd = _run(tmp_path / 'campaign' / 'seed')
    _run(tmp_path / 'seed')  # a valid but unrelated root run must never receive the links
    (rd / 'config.snapshot.json').write_text(Settings(backend='toy', external_harness=True).model_dump_json())
    (rd / 'task.snapshot.json').write_text(ToyTask().model_dump_json())
    store = EventStore(rd / 'events.jsonl')
    generation = run_generation_token(store.read_all())
    store.append('node_created', {'node_id': 0, 'operator': 'score', 'idea': {'operator': 'score'}})
    store.append('node_evaluated', {'node_id': 0, 'metric': .5, 'extra_metrics': {'R200': .7}})
    report = store.append('report_generated', {'at_node': 1, 'content': {'headline': 'Nested result'}})
    address = run_address(tmp_path, rd)
    (tmp_path / 'projects.json').write_text(json.dumps({
        'assignments': {address: 'p'}, 'projects': [{'id': 'p', 'metric_profile': {
            'name': 'Correct nested profile', 'metrics': [{'key': 'R200', 'role': 'primary'}]}}]}))
    with TestClient(make_app(tmp_path)) as client:
        pages = {}
        for endpoint in ('research-map', 'research-briefing', 'harness-handoff',
                         'profile-measurements', 'research-export'):
            response = client.get(f'/api/runs/{address}/{endpoint}',
                                  params={'expected_generation': generation, 'report_seq': report.seq})
            assert response.status_code == 200, (endpoint, response.text)
            pages[endpoint] = response.json()
        assert pages['research-map']['run_id'] == address
        assert pages['harness-handoff']['run_id'] == address
        assert pages['harness-handoff']['server_paths']['run_root'] == str(tmp_path)
        assert pages['research-export']['run_id'] == address
        ref = pages['research-export']['reports'][0]['report_api']
        assert ref.startswith(f'/api/runs/{address}/')
        assert client.get(ref).status_code == 200
        for endpoint, page in pages.items():
            profile = page.get('metric_profile', page.get('profile'))
            if endpoint != 'harness-handoff' and endpoint != 'research-map':
                assert profile['state'] == 'configured', endpoint
        values = pages['profile-measurements']['nodes'][0]['values']
        assert values[0]['key'] == 'R200' and values[0]['value'] == .7
        context = pages['research-briefing']['report_context']
        assert all('/api/runs/seed/' not in str(value) for value in context.values())


def test_remote_submit_dry_run_does_not_require_ui_fastapi(tmp_path):
    import subprocess
    import sys
    script = '''
import importlib.abc
import sys
class NoFastAPI(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'fastapi' or fullname.startswith('fastapi.'):
            raise ModuleNotFoundError('optional fastapi deliberately unavailable')
sys.meta_path.insert(0, NoFastAPI())
from looplab.harness.composite import submit_experiment
class API:
    def run_progress(self, *args):
        return {'status': 200, 'body': {}}
result = submit_experiment(API(), 'run', 'a' * 64, key='preview',
    idea={'operator': 'fit'}, files={'train.py': 'print(1)'}, job_role='fit', dry_run=True)
assert result['ok'], result
assert 'fastapi' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', script], text=True,
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
