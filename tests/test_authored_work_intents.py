"""Advisory identities and exact cited operator text; no launch authority."""
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from looplab.core.external_work_state import normalize_authored
from looplab.events.replay import fold
from looplab.serve.server import make_app
from tests.test_external_work_state import _run, _work, _command


def _intent(**changes):
    return {'id': 'prepare-1', 'topic': 'next-method', 'operation': 'prepare',
            'subject_sha256': 'a' * 64, 'summary': 'Prepare the declared experiment inputs',
            'status': 'active', 'direction_refs': [], **changes}


def _ref(event, **changes):
    return {'event_seq': event.seq, 'text_sha256': sha256(event.data['text'].strip().encode()).hexdigest(),
            'disposition': 'deferred', 'summary': 'Previous intent is deferred while current work finishes', **changes}


def test_optional_absence_preserves_exact_legacy_normalized_payload():
    legacy = normalize_authored(_work())
    assert 'work_intents' not in legacy
    assert normalize_authored({**legacy}) == legacy
    assert normalize_authored(_work(work_intents=[])) == {**legacy, 'work_intents': []}


def test_real_public_checkpoint_records_exact_intent_and_deferred_historical_direction(tmp_path, monkeypatch):
    rd, store, gen = _run(tmp_path, monkeypatch)
    direction = store.append('hint', {'text': 'Earlier method must be prepared'})
    store.append('hint', {'text': 'Newer standing scope', 'replace': True})
    original_nodes = fold(store.read_all()).nodes
    with TestClient(make_app(tmp_path)) as client:
        receipt = _command(client, gen, _work(work_intents=[_intent(direction_refs=[_ref(direction)])]))
        assert receipt['status'] == 'succeeded'
        result = client.get('/api/runs/demo/harness-progress', params={'expected_generation': gen, 'brief': True}).json()
        own = result['external_work_state']
        assert own['checkpoint']['work_intents'][0]['direction_refs'][0] == _ref(direction)
        assert own['authority'] == 'none' and own['work_claims'] == 'authored_not_exclusive'
        assert fold(store.read_all()).nodes == original_nodes
        original = store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as restarted:
        result = restarted.get('/api/runs/demo/harness-progress', params={'expected_generation': gen}).json()
        assert result['external_work_state']['checkpoint']['work_intents'][0] == _intent(direction_refs=[_ref(direction)])
    assert store.path.read_bytes() == original


@pytest.mark.parametrize('change', [
    {'id': True}, {'id': []}, {'topic': ''}, {'operation': []}, {'operation': {}}, {'operation': True},
    {'operation': 'launch_unconditionally'}, {'subject_sha256': True}, {'status': []},
    {'summary': 'x' * 513}, {'reasoning': 'unsupported'}, {'direction_refs': [False]},
    {'direction_refs': [{'event_seq': True, 'text_sha256': 'a' * 64, 'disposition': 'following', 'summary': 'text'}]},
    {'direction_refs': [{'event_seq': 1, 'text_sha256': 'a' * 64, 'disposition': [], 'summary': 'text'}]},
])
def test_malformed_intent_is_rejected_without_native_mutation(tmp_path, monkeypatch, change):
    rd, store, gen = _run(tmp_path, monkeypatch); before = store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        receipt = _command(client, gen, _work(work_intents=[_intent(**change)]))
        assert receipt['status'] == 'rejected', receipt
    assert store.path.read_bytes() == before


@pytest.mark.parametrize('case', ['unknown', 'wrong_digest', 'model', 'mixed_digests_same_seq'])
def test_direction_reference_cannot_claim_unseen_or_model_text(tmp_path, monkeypatch, case):
    rd, store, gen = _run(tmp_path, monkeypatch)
    event = store.append('hint', {'text': 'Exact direction', **({'source': 'deep_research'} if case == 'model' else {})})
    ref = _ref(event)
    if case == 'unknown': ref['event_seq'] += 100
    if case == 'wrong_digest': ref['text_sha256'] = 'b' * 64
    intents = [_intent(direction_refs=[ref])]
    if case == 'mixed_digests_same_seq':
        intents = [_intent(direction_refs=[_ref(event, text_sha256='b' * 64)]), _intent(id='other', direction_refs=[_ref(event)])]
    before = store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        assert _command(client, gen, _work(work_intents=intents))['status'] == 'rejected'
    assert store.path.read_bytes() == before


def _standalone(client, run_id, gen, intents, key):
    from tests.factories import post_command, command_terminal
    response = post_command(client, 'agent_checkpointed', {'work_intents': intents}, key, run_id=run_id, generation=gen)
    assert response.status_code in (200, 202), response.text
    return command_terminal(client, response.json())


def test_common_claim_is_metadata_only_on_internal_and_external_runs_and_cross_run_read(tmp_path, monkeypatch):
    import json
    from looplab.events.run_generation import run_generation_token
    root = tmp_path
    rd, store, gen = _run(root, monkeypatch, name='one')
    peer_rd, peer_store, peer_gen = _run(root, monkeypatch, name='two')
    config = json.loads((peer_rd / 'config.snapshot.json').read_text()); config['external_harness'] = False
    (peer_rd / 'config.snapshot.json').write_text(json.dumps(config))
    with TestClient(make_app(root)) as client:
        assert _standalone(client, 'one', gen, [_intent()], 'one-claim')['status'] == 'succeeded'
        assert _standalone(client, 'two', peer_gen, [_intent(id='peer')], 'peer-claim')['status'] == 'succeeded'
        originals = {path: path.read_bytes() for path in [store.path, peer_store.path]}
        response = client.get('/api/runs/one/work-claims', params={'expected_generation': gen, 'peer': 'two'})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['own']['claims'][0]['id'] == 'prepare-1'
        assert result['peers'][0]['claims'][0]['id'] == 'peer'
        assert len(result['possible_overlaps']) == 1 and result['atomic_across_runs'] is False
        assert result['peers'][0]['execution'] == 'not_attested'
        assert result['own']['exclusivity'] == 'not_claimed'
        assert response.headers['Cache-Control'] == 'no-store'
        for path, before in originals.items(): assert path.read_bytes() == before
        assert len(fold(store.read_all()).nodes) == len(fold(peer_store.read_all()).nodes) == 1
        compact = client.get('/api/runs/one/harness-progress', params={'expected_generation': gen, 'brief': True}).json()
        assert compact['work_intents']['claims'] == result['own']['claims']
        assert compact['candidate_blockers_if_expanding'] == client.get('/api/runs/one/harness-progress', params={'expected_generation': gen}).json()['candidate_blockers_if_expanding']


@pytest.mark.parametrize('event_id', [True, False, 1.0, '1'])
def test_optional_ack_event_identity_is_an_integer_not_a_coercible_scalar(tmp_path, monkeypatch, event_id):
    from looplab.serve.agent_events import append_events
    rd, store, gen = _run(tmp_path, monkeypatch)
    for i in range(2):
        append_events(tmp_path, [{'key': f'fixture:{i}', 'run_id': 'demo', 'run_uid': 'uid',
                                'kind': 'operator_direction', 'seq': i}])
    before = store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        response = client.post('/api/runs/demo/agent-event-ack', json={
            'expected_generation': gen, 'event_id': event_id, 'recipient': 'executor',
            'action_id': 'bad-identity', 'disposition': 'acknowledged'})
        assert response.status_code == 422, response.text
    assert store.path.read_bytes() == before
    assert not (tmp_path / '.agent-events/acknowledgements.jsonl').exists()


def test_optional_ack_receipt_is_visible_but_never_comprehension_or_new_permission(tmp_path, monkeypatch):
    from looplab.serve.agent_events import append_events, derive_log_events
    rd, store, gen = _run(tmp_path, monkeypatch)
    hint = store.append('hint', {'text': 'Prepare selected method before the next independent fit'})
    outbox = append_events(tmp_path, derive_log_events('demo', 'uid', [hint]))[0]
    with TestClient(make_app(tmp_path)) as client:
        url = '/api/runs/demo/work-claims'; params = {'expected_generation': gen}
        before = client.get(url, params=params).json()
        assert before['direction_receipts']['items'][0]['state'] == 'no_ACK_in_retained_sources'
        native = store.path.read_bytes()
        ack = client.post('/api/runs/demo/agent-event-ack', json={
            'expected_generation': gen, 'event_id': outbox['id'], 'recipient': 'executor',
            'action_id': 'intent-received', 'disposition': 'acknowledged'})
        assert ack.status_code == 200, ack.text
        after = client.get(url, params=params).json()
        view = after['direction_receipts']
        assert view['items'][0]['state'] == 'explicit_ACK_recorded'
        assert view['items'][0]['receipts'][0]['event_id'] == outbox['id']
        assert view['comprehension'] == 'not_measured' and view['execution_authority'] == 'none'
        assert after['own']['state'] == 'not_declared' and after['possible_overlaps'] == []
        assert store.path.read_bytes() == native


def test_new_direction_and_explicit_conflict_stay_nonblocking_authored_context(tmp_path, monkeypatch):
    rd, store, gen = _run(tmp_path, monkeypatch)
    old = store.append('hint', {'text': 'Earlier selected experiment'})
    with TestClient(make_app(tmp_path)) as client:
        assert _standalone(client, 'demo', gen, [_intent(direction_refs=[_ref(old, disposition='following')])], 'first-intent')['status'] == 'succeeded'
        first = client.get('/api/runs/demo/work-claims', params={'expected_generation': gen}).json()
        assert first['own']['state'] == 'context_unchanged'
        assert first['own']['unaddressed_visible_direction_sequences'] == []
        latest = store.append('hint', {'text': 'Changed standing scope', 'replace': True})
        context = client.get('/api/runs/demo/work-claims', params={'expected_generation': gen}).json()
        assert context['own']['state'] == 'context_changed'
        assert context['own']['unaddressed_visible_direction_sequences'] == [latest.seq]
        prior = client.get('/api/runs/demo/harness-progress', params={'expected_generation': gen, 'brief': True}).json()
        conflict = _intent(direction_refs=[_ref(latest, disposition='conflict', summary='Existing healthy work follows a different already delegated intent')])
        assert _standalone(client, 'demo', gen, [conflict], 'explicit-interpretation')['status'] == 'succeeded'
        after = client.get('/api/runs/demo/harness-progress', params={'expected_generation': gen, 'brief': True}).json()
        assert after['work_intents']['claims'][0]['direction_refs'][0]['disposition'] == 'conflict'
        assert after['candidate_blockers_if_expanding'] == prior['candidate_blockers_if_expanding']
        assert after['recorded_lifecycle'] == prior['recorded_lifecycle']
        assert fold(store.read_all()).paused is False and len(fold(store.read_all()).nodes) == 1


@pytest.mark.parametrize('tail,expected_state', [(b'{broken\n', 'unavailable'), (b'{partial', 'not_declared')])
def test_other_task_and_damaged_peer_are_not_reported_as_absent_work(tmp_path, monkeypatch, tail, expected_state):
    rd, store, gen = _run(tmp_path, monkeypatch, name='one')
    other, other_store, other_gen = _run(tmp_path, monkeypatch, name='other')
    damaged, damaged_store, damaged_gen = _run(tmp_path, monkeypatch, name='damaged')
    event = other_store.read_all()[0]
    events = other_store.read_all()
    other_store.path.write_text(''.join((row.model_copy(update={'data': {**row.data, 'task_id': 'different-task'}}) if row.seq == event.seq else row).model_dump_json() + '\n' for row in events))
    # EventStore deliberately permits an unfinished final append. A malformed COMPLETE
    # record is damaged history; neither GET repairs or truncates either kind of tail.
    with damaged_store.path.open('ab') as handle: handle.write(tail)
    before = {path: path.read_bytes() for path in [store.path, other_store.path, damaged_store.path]}
    with TestClient(make_app(tmp_path)) as client:
        result = client.get('/api/runs/one/work-claims', params=[('expected_generation', gen), ('peer', 'other'), ('peer', 'damaged')])
        assert result.status_code == 200, result.text
        peers = {row['run_id']: row for row in result.json()['peers']}
        assert peers['other']['state'] == 'outside_task_scope' and peers['other']['claims'] is None
        assert peers['damaged']['state'] == expected_state
        assert peers['damaged']['claims'] == (None if expected_state == 'unavailable' else [])
        assert client.get('/api/runs/one/work-claims', params={'expected_generation': '0' * 64}).status_code == 409
    for path, raw in before.items(): assert path.read_bytes() == raw


def test_bounded_read_omits_whole_peer_claims_and_names_continuations(tmp_path, monkeypatch):
    rd, store, gen = _run(tmp_path, monkeypatch, name='one')
    peer_rd, peer_store, peer_gen = _run(tmp_path, monkeypatch, name='two')
    with TestClient(make_app(tmp_path)) as client:
        assert _standalone(client, 'one', gen, [_intent()], 'small')['status'] == 'succeeded'
        claims = [_intent(id=f'peer{i}', summary='Descriptive declared work ' + 'x' * 450) for i in range(8)]
        assert _standalone(client, 'two', peer_gen, claims, 'large')['status'] == 'succeeded'
        response = client.get('/api/runs/one/work-claims', params={'expected_generation': gen, 'peer': 'two', 'max_bytes': 4096})
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(response.content) <= 4096 and body['response_budget']['limited']
        assert body['omitted_peers'] == ['two'] and body['peers'] == []
        full = client.get('/api/runs/one/work-claims', params={'expected_generation': gen, 'peer': body['omitted_peers'][0]})
        assert full.status_code == 200, full.text
        assert full.json()['peers'][0]['claims'] == claims
        assert full.json()['possible_overlaps_total'] == 8 and len(full.json()['possible_overlaps']) == 8


@pytest.mark.parametrize('source', ['acknowledgements.jsonl', 'outbox.jsonl'])
def test_unknown_ack_source_schema_is_unavailable_never_no_ACK(tmp_path, monkeypatch, source):
    import json
    rd, store, gen = _run(tmp_path, monkeypatch)
    store.append('hint', {'text': 'Current instruction'})
    directory = tmp_path / '.agent-events'; directory.mkdir(exist_ok=True)
    (directory / source).write_text(json.dumps({'unknown_shape': True}) + '\n')
    before = store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        response = client.get('/api/runs/demo/work-claims', params={'expected_generation': gen})
        assert response.status_code == 200, response.text
        view = response.json()['direction_receipts']
        assert view['state'] == 'unavailable' and not view['source_complete'] and view['items'] == []
    assert store.path.read_bytes() == before


def test_registered_read_tool_uses_one_GET_and_preserves_healthy_authority(tmp_path, monkeypatch):
    import anyio
    import httpx
    import json
    from looplab.harness.mcp_server import HarnessAPI, build_server
    rd, store, gen = _run(tmp_path, monkeypatch)
    peer_rd, peer_store, peer_gen = _run(tmp_path, monkeypatch, name='two')
    with TestClient(make_app(tmp_path)) as client:
        assert _standalone(client, 'demo', gen, [_intent()], 'own')['status'] == 'succeeded'
        assert _standalone(client, 'two', peer_gen, [_intent()], 'peer')['status'] == 'succeeded'
        before = [store.path.read_bytes(), peer_store.path.read_bytes()]
        requests = []
        def bridge(request):
            requests.append(request)
            response = client.request(request.method, str(request.url), content=request.content)
            return httpx.Response(response.status_code, content=response.content, headers={'Content-Type': 'application/json'})
        api = HarnessAPI('http://testserver', transport=httpx.MockTransport(bridge))
        server = build_server(api)
        tools = anyio.run(server.list_tools)
        hints = next(tool for tool in tools if tool.name == 'work_claims').annotations.model_dump(by_alias=True)
        assert hints['readOnlyHint'] and hints['idempotentHint'] and not hints['destructiveHint']
        result = anyio.run(server.call_tool, 'work_claims', {'run_id': 'demo', 'expected_generation': gen, 'peers': ['two']})
        envelope = result.model_dump(by_alias=True)
        result = envelope.get('structuredContent') or json.loads(envelope['content'][0]['text'])
        assert result['status'] == 200 and not result.get('code'), result
        body = result['body']
        assert body['own']['claims'] == [_intent()] and body['possible_overlaps_total'] == 1
        assert body['direction_receipts']['comprehension'] == 'not_measured'
        assert len(requests) == 1 and requests[0].method == 'GET'
        assert requests[0].url.params.get_list('peer') == ['two']
        assert [store.path.read_bytes(), peer_store.path.read_bytes()] == before
        api.client.close()


@pytest.mark.parametrize('path,value', [
    [('version',), True], [('run_uid',), True], [('event_seq',), True], [('own', 'run_uid'), 'different'],
    [('own', 'claims'), None], [('own', 'claims'), False], [('own', 'authority'), 'exclusive'],
    [('own', 'claims', 0, 'operation'), []], [('own', 'checkpoint_seq'), True],
    [('peers',), None], [('peers', 0, 'claims'), None], [('possible_overlaps_total',), True], [('possible_overlaps_total',), 0],
    [('peers', 0, 'claims', 0, 'subject_sha256'), 'b' * 64],
    [('possible_overlaps', 0, 'own_intent_id'), []], [('possible_overlaps', 0, 'peer_intent_id'), 'unknown'],
    [('direction_receipts', 'items'), None], [('direction_receipts', 'comprehension'), 'confirmed'],
    [('response_budget', 'limited'), 0], [('omitted_peers',), [True]],
])
def test_typed_reader_refuses_malformed_200_without_body_or_extra_HTTP(tmp_path, monkeypatch, path, value):
    import httpx
    from looplab.harness.mcp_server import HarnessAPI
    rd, store, gen = _run(tmp_path, monkeypatch)
    peer_rd, peer_store, peer_gen = _run(tmp_path, monkeypatch, name='two')
    with TestClient(make_app(tmp_path)) as client:
        _standalone(client, 'demo', gen, [_intent()], 'own')
        _standalone(client, 'two', peer_gen, [_intent()], 'peer')
        page = client.get('/api/runs/demo/work-claims', params={'expected_generation': gen, 'peer': 'two'}).json()
    target = page
    for key in path[:-1]: target = target[key]
    target[path[-1]] = value
    requests = []
    api = HarnessAPI('http://testserver', transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(200, json=page)))
    result = api.work_claims('demo', gen, ['two'])
    assert result['reason'] == 'invalid_work_claims' and result['outcome'] == 'unavailable'
    assert 'body' not in result and len(requests) == 1 and requests[0].method == 'GET'
    api.client.close()


@pytest.mark.parametrize('event_id', [True, False, 1.0, '1'])
def test_MCP_ACK_refuses_coercible_id_before_HTTP(event_id):
    import anyio
    import httpx
    from looplab.harness.mcp_server import HarnessAPI, build_server
    requests = []
    api = HarnessAPI('http://testserver', transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(200, json={'acknowledged': True})))
    from mcp.server.mcpserver.exceptions import ToolError
    with pytest.raises(ToolError, match='valid integer'):
        anyio.run(build_server(api).call_tool, 'acknowledge_agent_event',
            {'run_id': 'demo', 'expected_generation': 'a' * 64, 'event_id': event_id,
             'recipient': 'executor', 'action_id': 'coercion-must-not-ACK'})
    assert requests == []
    api.client.close()


def test_only_explicit_withdrawal_clears_claims_and_malformed_latest_does_not_fall_back(tmp_path, monkeypatch):
    rd, store, gen = _run(tmp_path, monkeypatch)
    with TestClient(make_app(tmp_path)) as client:
        assert _standalone(client, 'demo', gen, [_intent()], 'declare')['status'] == 'succeeded'
        assert _command(client, gen, _work(), key='unrelated')['status'] == 'succeeded'
        own = client.get('/api/runs/demo/work-claims', params={'expected_generation': gen}).json()['own']
        assert own['claims'] == [_intent()]
        assert _standalone(client, 'demo', gen, [], 'withdraw')['status'] == 'succeeded'
        own = client.get('/api/runs/demo/work-claims', params={'expected_generation': gen}).json()['own']
        assert own['claims'] == [] and own['state'] == 'context_unchanged'
        store.append('agent_checkpointed', {'work_intents': {'unsupported_record': True}})
        before = store.path.read_bytes()
        own = client.get('/api/runs/demo/work-claims', params={'expected_generation': gen}).json()['own']
        assert own['claims'] is None and own['state'] == 'unavailable'
        assert store.path.read_bytes() == before


def test_generation_replacement_during_peer_observation_refuses_whole_view(tmp_path, monkeypatch):
    from looplab.harness import work_claims
    rd, store, gen = _run(tmp_path, monkeypatch)
    read = work_claims.read_event_source
    calls = []
    def replaced(path):
        events = read(path); calls.append(path)
        if len(calls) == 2:
            # In-place replacement is modeled at the independent source-read seam;
            # no fixture or real native journal is rewritten by this GET.
            events[0] = events[0].model_copy(update={'ts': events[0].ts + 1})
        return events
    monkeypatch.setattr(work_claims, 'read_event_source', replaced)
    before = store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        result = client.get('/api/runs/demo/work-claims', params={'expected_generation': gen})
        assert result.status_code == 409 and len(calls) == 2
    assert store.path.read_bytes() == before


def test_scoped_read_and_metadata_do_not_grant_owner_workflows(tmp_path, monkeypatch):
    rd, store, gen = _run(tmp_path, monkeypatch)
    monkeypatch.setenv('LOOPLAB_HARNESS_TOKEN', 'fixture-scoped-token')
    headers = {'X-LoopLab-Token': 'fixture-scoped-token'}
    from tests.factories import post_command, command_terminal
    with TestClient(make_app(tmp_path)) as client:
        result = post_command(client, 'agent_checkpointed', {'work_intents': [_intent()]},
            'scoped-intent', generation=gen, headers=headers)
        assert result.status_code in (200, 202), result.text
        assert command_terminal(client, result.json())['status'] == 'succeeded'
        before = store.path.read_bytes()
        assert client.get('/api/runs/demo/work-claims', params={'expected_generation': gen}, headers=headers).status_code == 200
        assert client.post('/api/start', json={}, headers=headers).status_code == 403
        assert store.path.read_bytes() == before


def test_registered_MCP_ACK_keeps_exact_integer_identity_and_idempotent_receipt(tmp_path, monkeypatch):
    import anyio
    import httpx
    import json
    from looplab.harness.mcp_server import HarnessAPI, build_server
    from looplab.serve.agent_events import append_events, derive_log_events
    rd, store, gen = _run(tmp_path, monkeypatch)
    hints = [store.append('hint', {'text': f'Observed direction {i}'}) for i in range(2)]
    records = append_events(tmp_path, derive_log_events('demo', 'uid', hints))
    event_id = records[-1]['id']; assert type(event_id) is int and event_id != records[0]['id']
    native = store.path.read_bytes(); requests = []
    with TestClient(make_app(tmp_path)) as client:
        def bridge(request):
            requests.append(request)
            response = client.request(request.method, str(request.url), content=request.content,
                                      headers={'Content-Type': 'application/json'})
            return httpx.Response(response.status_code, content=response.content, headers={'Content-Type': 'application/json'})
        api = HarnessAPI('http://testserver', transport=httpx.MockTransport(bridge)); server = build_server(api)
        args = {'run_id': 'demo', 'expected_generation': gen, 'event_id': event_id,
                'recipient': 'executor', 'action_id': 'exact-integer-ACK'}
        results = []
        for _ in range(2):
            envelope = anyio.run(server.call_tool, 'acknowledge_agent_event', args).model_dump(by_alias=True)
            result = envelope.get('structuredContent') or json.loads(envelope['content'][0]['text'])
            assert result['status'] == 200, result
            results.append(result['body'])
        assert results[0] == results[1]
        assert all(json.loads(request.content)['event_id'] == event_id for request in requests)
        view = client.get('/api/runs/demo/work-claims', params={'expected_generation': gen}).json()['direction_receipts']
        assert view['items'][0]['receipts'] == []
        assert view['items'][1]['receipts_total'] == 1 and view['items'][1]['receipts'][0]['event_id'] == event_id
        assert view['comprehension'] == 'not_measured'
        assert len((tmp_path / '.agent-events/acknowledgements.jsonl').read_text().splitlines()) == 1
        api.client.close()
    assert store.path.read_bytes() == native
