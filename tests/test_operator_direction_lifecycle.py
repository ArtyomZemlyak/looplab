"""Real command/replay recovery of scoped human directions, without engine/GPU work."""
import pytest
from fastapi.testclient import TestClient

from looplab.agents.hints import render_hint_directives
from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.serve.server import make_app
from tests.factories import post_command, command_terminal


def _run(tmp_path, monkeypatch):
    monkeypatch.setenv('LOOPLAB_UI_ANONYMOUS', '1')
    monkeypatch.delenv('LOOPLAB_UI_TOKEN', raising=False)
    monkeypatch.setenv('LOOPLAB_AGENT_EVENTS', '0')
    rd = tmp_path / 'demo'; rd.mkdir()
    (rd / 'config.snapshot.json').write_text(Settings(backend='toy', external_harness=True).model_dump_json())
    store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': 'demo', 'run_uid': 'uid', 'task_id': 't', 'direction': 'max'})
    store.append('node_created', {'node_id': 0, 'operator': 'draft', 'parent_ids': [], 'idea': {'operator': 'draft'}})
    store.append('node_evaluated', {'node_id': 0, 'metric': 0.5})
    return rd, store, run_generation_token(store.read_all())


def _direction(key='fit-v1', topic='fit', text='Use the saved control', **extra):
    return {'directive_id': key, 'topic': topic, 'text': text, **extra}


def _post(client, gen, data, key):
    response = post_command(client, 'hint', data, key, generation=gen)
    assert response.status_code in (200, 202), response.text
    return command_terminal(client, response.json())


def test_actual_commands_retire_only_one_topic_and_reopen_without_renewing_old_work(tmp_path, monkeypatch):
    rd, store, gen = _run(tmp_path, monkeypatch)
    with TestClient(make_app(tmp_path)) as client:
        first = _post(client, gen, _direction(), 'first')
        assert first['status'] == 'succeeded'
        assert _post(client, gen, _direction(), 'duplicate')['status'] == 'noop'
        assert _post(client, gen, _direction('report-v1', 'report', 'Publish the negative result'), 'independent')['status'] == 'succeeded'
        correction = _direction('fit-v2', text='Prepare the new size', supersedes=['fit-v1'])
        assert _post(client, gen, correction, 'correction')['status'] == 'succeeded'
        state = fold(store.read_all())
        assert [x['text'] for x in state.pending_hints] == ['Publish the negative result', 'Prepare the new size']
        assert state.operator_direction_ledger['fit-v1']['status'] == 'superseded'
        completed = _direction('fit-v2', text='Preparation finished', status='completed')
        assert _post(client, gen, completed, 'completed')['status'] == 'succeeded'
        changed_retirement = _post(client, gen, {**completed, 'text': 'Different history'}, 'changed-retirement')
        assert changed_retirement['status'] == 'rejected'
        assert changed_retirement['error']['code'] == 'direction_retirement_content_changed'
        stale = _post(client, gen, correction, 'late-notification')
        assert stale['status'] == 'rejected' and stale['error']['code'] == 'direction_already_retired'
        assert _post(client, gen, _direction('report-v1', 'report', status='cancelled'), 'cancelled')['status'] == 'succeeded'
        # Replaying an accepted command recovers its immutable receipt, not its old authority.
        count = len(store.read_all())
        assert _post(client, gen, _direction(), 'first')['id'] == first['id']
        assert len(store.read_all()) == count
    before = store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as restarted:
        progress = restarted.get('/api/runs/demo/harness-progress', params={'expected_generation': gen, 'brief': True}).json()
        directions = progress['operator_directions']
        assert directions['items'] == [] and directions['active_scoped_count'] == 0
        assert directions['retired_count'] == 3
        assert {x['status'] for x in directions['retired']} == {'completed', 'cancelled', 'superseded'}
    # A malformed historical duplicate cannot reactivate retired authority even outside HTTP.
    events_before_late = store.read_all()
    late_event = events_before_late[-1].model_copy(update={'type': 'hint', 'data': correction, 'seq': events_before_late[-1].seq + 1})
    assert fold([*events_before_late, late_event]).pending_hints == []
    replay = fold(store.read_all())
    assert render_hint_directives(replay.pending_hints) == ''
    assert replay.nodes[0].status == 'evaluated' and len(replay.nodes) == 1
    assert store.path.read_bytes() == before
    # A timestamp from a delayed reminder cannot outrank the durable event order.
    events = store.read_all()
    hints = [e for e in events if e.type == 'hint']
    reordered_clocks = [e.model_copy(update={'ts': 10**10 - e.seq}) if e in hints else e for e in events]
    assert fold(reordered_clocks).operator_direction_ledger == replay.operator_direction_ledger


@pytest.mark.parametrize('changes', [
    {'directive_id': True}, {'topic': ''}, {'status': 'pending'}, {'supersedes': [1]},
    {'supersedes': ['other']}, {'replace': True}, {'text': 'x' * 2001}, {'status': 'completed'},
])
def test_malformed_or_unknown_scope_has_no_authority(tmp_path, monkeypatch, changes):
    rd, store, gen = _run(tmp_path, monkeypatch)
    before = store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        result = _post(client, gen, _direction(**changes), 'invalid')
        assert result['status'] == 'rejected'
    assert store.path.read_bytes() == before


def test_legacy_replace_keeps_retired_id_tombstones_and_model_advisories_stay_advisory(tmp_path, monkeypatch):
    rd, store, gen = _run(tmp_path, monkeypatch)
    with TestClient(make_app(tmp_path)) as client:
        _post(client, gen, _direction(), 'scoped')
        _post(client, gen, {'text': 'New explicit global priority', 'replace': True}, 'legacy-replace')
        stale = _post(client, gen, _direction(), 'old-scope')
        assert stale['status'] == 'rejected'
    store.append('hint', _direction('forged', text='model text', source='deep_research'))
    store.append('hint', {'text': 'model advisory', 'source': 'deep_research'})
    state = fold(store.read_all())
    assert state.operator_direction_ledger['fit-v1']['status'] == 'superseded'
    assert render_hint_directives(state.pending_hints) == '\nOperator directive (follow it): New explicit global priority'


def test_same_text_in_independent_topics_does_not_deduplicate_and_id_is_immutable(tmp_path, monkeypatch):
    rd, store, gen = _run(tmp_path, monkeypatch)
    with TestClient(make_app(tmp_path)) as client:
        _post(client, gen, _direction(), 'first')
        assert _post(client, gen, _direction('different', 'report'), 'other-topic')['status'] == 'succeeded'
        changed = _post(client, gen, _direction(text='Changed content'), 'mutate-id')
        assert changed['status'] == 'rejected' and changed['error']['code'] == 'direction_id_content_changed'
        missing = _post(client, gen, _direction('fit-v2'), 'missing-supersedes')
        assert missing['status'] == 'rejected' and missing['error']['code'] == 'direction_topic_changed'
    assert len(fold(store.read_all()).pending_hints) == 2


@pytest.mark.parametrize('unrelated', [False, True])
def test_publication_race_refolds_exact_subject_without_losing_unrelated_tail(tmp_path, monkeypatch, unrelated):
    rd, store, gen = _run(tmp_path, monkeypatch)
    original = EventStore.append
    with TestClient(make_app(tmp_path)) as client:
        _post(client, gen, _direction(), 'first')
        raced = False
        def append(self, kind, data, *args, **kwargs):
            nonlocal raced
            if (not raced and self.path == store.path and kind == 'hint'
                    and data.get('directive_id') == 'fit-v2' and kwargs.get('expected_last_seq') is not None):
                raced = True
                if unrelated:
                    original(self, 'hint', {'text': 'Independent legacy direction'})
                else:
                    original(self, 'hint', _direction('winner', text='New actual priority', supersedes=['fit-v1']))
            return original(self, kind, data, *args, **kwargs)
        monkeypatch.setattr(EventStore, 'append', append)
        late = _post(client, gen, _direction('fit-v2', supersedes=['fit-v1']), 'racing')
        assert raced
        assert late['status'] == ('succeeded' if unrelated else 'rejected')
        rows = fold(store.read_all()).pending_hints
        assert any(row['text'] == ('Independent legacy direction' if unrelated else 'New actual priority') for row in rows)
        assert any(row.get('directive_id') == 'fit-v2' for row in rows) is unrelated


def test_accepted_long_legacy_direction_is_a_visible_preview_with_exact_bounded_source(tmp_path, monkeypatch):
    """A successful hint must not look fully read when its overriding tail is absent."""
    import hashlib
    from urllib.parse import parse_qs, urlsplit
    import httpx
    from looplab.harness.mcp_server import HarnessAPI

    rd, store, gen = _run(tmp_path, monkeypatch)
    text = 'Контекст🙂' * 2200 + '\nIMPORTANT FINAL OVERRIDE: preserve the selected source.'
    assert 2000 < len(text) <= 20000
    with TestClient(make_app(tmp_path)) as client:
        result = _post(client, gen, {'text': text, 'replace': True}, 'long-direction')
        assert result['status'] == 'succeeded'
    before = store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        outputs = []
        for brief in (False, True):
            response = client.get('/api/runs/demo/harness-progress', params={'expected_generation': gen, 'brief': brief})
            assert response.status_code == 200, response.text
            outputs.append(response.json()['operator_directions'])
        directions = outputs[0]
        assert outputs[1] == directions
        assert directions['items'] == [text[:2000]] and directions['total'] == 1
        assert directions['truncated'] and directions['text_truncated']
        assert not directions['window_truncated'] and directions['read_warning']
        detail, = directions['item_details']
        assert detail['characters'] == len(text) and detail['shown_characters'] == 2000
        assert detail['text_truncated'] and detail['event_seq'] == result['event_seq']
        assert detail['text_sha256'] == hashlib.sha256(text.encode()).hexdigest()
        seen = []
        def bridge(request):
            seen.append(request)
            response = client.request(request.method, request.url.raw_path.decode())
            return httpx.Response(response.status_code, content=response.content)
        # Exercise the existing bounded transport via the actual external HTTP client.
        api = HarnessAPI('http://testserver', transport=httpx.MockTransport(bridge))
        recovered = api.request('GET', detail['full_ref'])
        assert recovered['status'] == 200 and not recovered.get('code'), recovered
        page = recovered['body']
        assert page['generation'] == gen and page['matched_seq'] == detail['event_seq']
        event, = page['events']
        assert event['seq'] == detail['event_seq'] and event['type'] == 'hint'
        assert event['data']['text'] == text
        assert hashlib.sha256(event['data']['text'].strip().encode()).hexdigest() == detail['text_sha256']
        assert 'IMPORTANT FINAL OVERRIDE' in event['data']['text']
        assert seen[0].method == 'GET' and seen[0].url.path.endswith('/log-page')
        params = parse_qs(urlsplit(detail['full_ref']).query)
        assert params['limit'] == ['1'] and params['generation'] == [gen]
        assert len(client.get(detail['full_ref']).content) < 100000
        research = client.get('/api/runs/demo/research-map', params={
            'expected_generation': gen, 'collections': 'operator_directions', 'max_bytes': 20000})
        assert research.status_code == 200, research.text
        assert research.json()['operator_directions'] == directions
    assert store.path.read_bytes() == before


def test_preview_metadata_distinguishes_window_from_text_and_keeps_authority_filter(tmp_path, monkeypatch):
    from looplab.harness.progress import _operator_directions
    rd, store, gen = _run(tmp_path, monkeypatch)
    for i in range(7):
        store.append('hint', {'text': ('x' * 2100) if i == 0 else f'direction {i}'})
    store.append('hint', {'text': 'y' * 3000, 'source': 'deep_research'})
    store.append('hint', {'text': 'deep-research directions: ' + 'z' * 3000})
    state = fold(store.read_all())
    directions = _operator_directions(state, events=store.read_all(), run_id='demo', generation=gen)
    assert directions['total'] == 7 and directions['window_truncated'] and directions['truncated']
    assert not directions['text_truncated'] and directions['read_warning'] is None
    assert directions['items'] == [f'direction {i}' for i in range(1, 7)]
    assert len(directions['item_details']) == 6
    assert all(not row['text_truncated'] and row['full_ref'] for row in directions['item_details'])
    # The old bounded context remains unchanged; omitted older directions are not shown as prefixes.
    assert render_hint_directives(state.pending_hints).splitlines()[-1].startswith('  6. direction 6')


def test_scoped_boundary_is_complete_and_stale_full_reference_refuses_reset(tmp_path, monkeypatch):
    rd, store, gen = _run(tmp_path, monkeypatch)
    with TestClient(make_app(tmp_path)) as client:
        assert _post(client, gen, _direction(text='a' * 2000), 'boundary')['status'] == 'succeeded'
        directions = client.get('/api/runs/demo/harness-progress', params={'expected_generation': gen}).json()['operator_directions']
        assert directions['items'] == ['a' * 2000] and not directions['truncated']
        assert directions['item_details'][0]['characters'] == 2000
        reference = directions['item_details'][0]['full_ref']
        assert client.get(reference).status_code == 200
        before = store.path.read_bytes()
        assert _post(client, gen, _direction('too-long', 'other', 'a' * 2001), 'refused')['status'] == 'rejected'
        assert store.path.read_bytes() == before
    # Imported/replaced incarnation: a surviving reference cannot authorize the new run.
    store.path.unlink()
    store = EventStore(store.path)
    store.append('run_started', {'run_id': 'demo', 'run_uid': 'new-uid', 'task_id': 't', 'direction': 'max'})
    with TestClient(make_app(tmp_path)) as client:
        assert client.get(reference).status_code == 409


def test_scoped_harness_can_follow_preview_reference_without_owner_rights(tmp_path, monkeypatch):
    from tests.test_harness_handoff import _run as protected_run
    rd, store, client = protected_run(tmp_path, monkeypatch)
    store.append('hint', {'text': 'Read all of this. ' * 200 + 'Final instruction.'})
    generation = run_generation_token(store.read_all())
    before = store.path.read_bytes()
    headers = {'X-LoopLab-Token': 'agent-secret-1234'}
    response = client.get('/api/runs/demo/harness-progress', params={'expected_generation': generation}, headers=headers)
    assert response.status_code == 200, response.text
    directions = response.json()['operator_directions']
    assert directions['text_truncated']
    full = client.get(directions['item_details'][0]['full_ref'], headers=headers)
    assert full.status_code == 200, full.text
    assert full.json()['events'][0]['data']['text'].endswith('Final instruction.')
    assert store.path.read_bytes() == before


def test_oversized_imported_hint_reference_remains_explicitly_unavailable(tmp_path, monkeypatch):
    rd, store, gen = _run(tmp_path, monkeypatch)
    # Imported historical rows may exceed today's intake bound. Do not lift the timeline cap.
    store.append('hint', {'text': 'large historical instruction ' * 40000})
    before = store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        response = client.get('/api/runs/demo/harness-progress', params={'expected_generation': gen})
        assert response.status_code == 200, response.text
        directions = response.json()['operator_directions']
        assert directions['text_truncated'] and 'unavailable' in directions['read_warning']
        page = client.get(directions['item_details'][0]['full_ref']).json()
        event, = page['events']
        assert event['seq'] == directions['item_details'][0]['event_seq']
        assert event['_log_page']['truncated'] is True
        assert event['_log_page']['reason'] == 'row_exceeds_byte_limit' and 'text' not in event['data']
    assert store.path.read_bytes() == before
