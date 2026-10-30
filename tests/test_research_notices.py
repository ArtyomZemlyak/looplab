import json
from fastapi.testclient import TestClient
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.serve.server import make_app


def test_saved_notices_have_optional_idempotent_disposition(tmp_path, monkeypatch):
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1'); monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all()); cid = 'cmd_' + 'a' * 32
    commands = rd / '.commands'; commands.mkdir()
    (commands / f'{cid}.json').write_text(json.dumps({'id': cid, 'status': 'succeeded', 'event_type': 'research_completed', 'event_seq': 1, 'run_generation': gen,
        'intake_notices': [{'kind': 'truncated', 'field': 'summary', 'count': 3}, {'kind': 'report_unchanged', 'current_seq': 1}]}))
    with TestClient(make_app(tmp_path)) as client:
        url = '/api/runs/r/research-notices'; params = {'expected_generation': gen}
        page = client.get(url, params=params)
        assert page.status_code == 200, page.text
        item = page.json()['items'][0]
        assert item['notice']['kind'] == 'truncated' and item['disposition'] == 'unreviewed'
        assert page.json()['items'][1]['notice'] == {'kind': 'report_unchanged', 'current_seq': 1}
        body = {'expected_generation': gen, 'command_id': cid, 'notice_id': item['notice_id'], 'action_id': 'review-1', 'disposition': 'resolved', 'reason': 'published corrected summary'}
        first = client.post(url, json=body)
        assert first.status_code == 200, first.text
        assert not first.json()['replayed'] and client.post(url, json=body).json()['replayed']
        assert client.post(url, json={**body, 'disposition': 'reviewed'}).status_code == 409
        refreshed = client.get(url, params=params).json()['items'][0]
        assert refreshed['disposition'] == 'resolved' and refreshed['reason'] == body['reason']
        assert client.get(url, params={'expected_generation': '0' * 64}).status_code == 409


def test_notice_read_refuses_symlinked_command_directory(tmp_path, monkeypatch):
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1'); monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    rd = tmp_path / 'r'; rd.mkdir(); store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'r', 'run_uid': 'u', 'task_id': 't', 'goal': 'g', 'direction': 'max'})
    gen = run_generation_token(store.read_all())
    target = tmp_path / 'other'; target.mkdir(); (rd / '.commands').symlink_to(target, target_is_directory=True)
    with TestClient(make_app(tmp_path)) as client:
        response = client.get('/api/runs/r/research-notices', params={'expected_generation': gen})
        assert response.status_code == 409
