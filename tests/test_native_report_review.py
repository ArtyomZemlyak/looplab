"""Optional native paid review → exact citations → separately approved research nomination."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from looplab.core.llm import CostAccountant
from looplab.core.report_review import ReportReviewOut, bind_review, review_input, review_is_current
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.serve import report_review as service
from looplab.serve.server import make_app
from looplab.tools.run_control_tools import RunControlTools


AUTHORED = {'summary': 'One observed gain needs a matched explanation.', 'comments': [{
    'report_field': 'verdict', 'quote': 'The mechanism is proven.',
    'comment': 'A single outcome cannot establish the mechanism.', 'node_ids': [0, 1],
    'nomination': 'ablation', 'target_node_id': 1,
    'rationale': 'Consider a matched component ablation after confirming the allowed scientific scope.'}]}


def run_source(rd):
    rd.mkdir()
    store = EventStore(rd / 'events.jsonl')
    store.append('run_started', {'run_id': rd.name, 'run_uid': 'a' * 32,
        'goal': 'Minimize recorded error', 'direction': 'min'})
    for nid, metric in [(0, .5), (1, .4)]:
        store.append('node_created', {'node_id': nid, 'parent_ids': [] if nid == 0 else [0],
            'operator': 'draft' if nid == 0 else 'improve',
            'idea': {'operator': 'draft', 'params': {}}, 'code': 'print(1)'})
        store.append('node_evaluated', {'node_id': nid, 'metric': metric})
    store.append('report_generated', {'content': {'headline': 'Observed result',
        'verdict': 'The mechanism is proven.', 'caveats': ['Single recorded outcome.']}, 'at_node': 2})
    return store


def data_of(store):
    events = store.read_all()
    return review_input(fold(events), generation=run_generation_token(events))


class Client:
    def __init__(self, answer=None):
        self.answer = copy.deepcopy(answer or AUTHORED)
        self.accountant = CostAccountant()
        self.messages = []

    def complete_tool(self, messages, schema):
        self.messages.append(copy.deepcopy(messages))
        self.accountant.add(.01, {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30})
        return self.answer


def post_review(client, body, key='review-key'):
    result = client.post('/api/runs/r/report-review', json=body, headers={'Idempotency-Key': key})
    if result.status_code != 200:
        return result
    data = result.json()
    if data.get('status') == 'running':
        # Poll an explicit server job receipt, never resubmit a new paid identity.
        for _ in range(100):
            result = client.get('/api/jobs/' + data['job_id'])
            job = result.json()
            if job.get('status') == 'done':
                return SimpleNamespace(status_code=200, json=lambda: job['result'])
        pytest.fail('review worker did not settle')
    return result


def body_of(view):
    data = view['input']
    return {'expected_generation': view['generation'],
        **{key: data[key] for key in ('report_seq', 'report_digest', 'input_digest')}}


def test_independent_context_binds_only_native_outcomes_and_exact_report_quotes(tmp_path):
    store = run_source(tmp_path / 'r')
    data = data_of(store)
    fake = Client()
    result = service.generate_review(data, fake)
    assert len(fake.messages) == 1 and len(fake.messages[0]) == 2
    assert 'independent report reviewer' in fake.messages[0][0]['content']
    assert 'UNTRUSTED_REPORT_REVIEW_INPUT' in fake.messages[0][1]['content']
    assert result['comments'][0]['outcomes'][1]['metric'] == .4
    assert result['comments'][0]['outcomes'][1]['attempt'] == 0
    assert result['reviewer']['mutation_tools'] is False
    assert result['reviewer']['provider_independence'] == 'not_attested'
    assert review_is_current(data, result)
    assert 'stdout' not in json.dumps(data) and 'print(1)' not in json.dumps(data)


@pytest.mark.parametrize('damage', ['quote', 'field', 'node', 'bool_node', 'target', 'invented_metric'])
def test_fabricated_review_provenance_is_refused(tmp_path, damage):
    store = run_source(tmp_path / 'r')
    answer = copy.deepcopy(AUTHORED)
    comment = answer['comments'][0]
    if damage == 'quote': comment['quote'] = 'Unwritten claim'
    if damage == 'field': comment['report_field'] = 'private.notes'
    if damage == 'node': comment['node_ids'] = [999]
    if damage == 'bool_node': comment['node_ids'] = [True]
    if damage == 'target': comment['target_node_id'] = 999
    if damage == 'invented_metric': comment['outcomes'] = [{'metric': 100}]
    with pytest.raises(ValueError):
        bind_review(data_of(store), ReportReviewOut.model_validate(answer))


@pytest.mark.parametrize('change', ['report', 'metric', 'reset', 'code', 'protocol', 'new_node'])
def test_review_stales_on_real_native_subject_change(tmp_path, change):
    store = run_source(tmp_path / 'r')
    data = data_of(store)
    receipt = bind_review(data, ReportReviewOut.model_validate(AUTHORED))
    events = [e.model_copy(deep=True) for e in store.read_all()]
    if change == 'report': events[-1].data['content']['verdict'] = 'Changed report'
    if change == 'metric': events[4].data['metric'] = .3
    if change == 'code': events[3].data['code'] = 'print(2)'
    if change == 'protocol': events[3].data['protocol_version'] = 2
    if change == 'reset':
        store.append('node_reset', {'node_id': 1, 'from_stage': 'eval'})
        events = store.read_all()
    if change == 'new_node':
        store.append('node_created', {'node_id': 2, 'operator': 'draft', 'idea': {'operator': 'draft'}})
        events = store.read_all()
    assert not review_is_current(review_input(fold(events), generation=run_generation_token(events)), receipt)


def test_real_http_paid_receipt_replays_after_app_restart_without_repaying(tmp_path, monkeypatch):
    store = run_source(tmp_path / 'r')
    fake = Client()
    monkeypatch.setattr('looplab.serve.server.make_llm_client', lambda *a, **kw: fake)
    app = make_app(tmp_path)
    client = TestClient(app)
    response = client.get('/api/runs/r/report-review')
    assert response.status_code == 200, response.text
    body = body_of(response.json())
    result = post_review(client, body)
    assert result.status_code == 200
    value = result.json()
    assert value['ok'], value
    assert len(fake.messages) == 1 and fake.accountant.calls == 1
    types = [e.type for e in store.read_all()]
    assert types.count('report_review_started') == types.count('report_review_completed') == 1
    assert types.count('report_generated') == 1
    from looplab.events.types import EVENT_PAYLOAD_KEYS
    for event in store.read_all():
        if event.type.startswith('report_review_'):
            contract = EVENT_PAYLOAD_KEYS[event.type]
            assert set(contract.required) <= set(event.data) <= set(contract.keys)
    assert not set(types) & {'force_confirm', 'force_ablate', 'inject_node', 'resume'}
    restarted_app = make_app(tmp_path)
    restarted_app.state.looplab.llm_settings = lambda *a: pytest.fail('receipt replay read damaged config')
    restarted = TestClient(restarted_app)
    monkeypatch.setattr('looplab.serve.server.make_llm_client', lambda *a, **kw: pytest.fail('replay repaid'))
    assert post_review(restarted, body).json() == value
    assert restarted.get('/api/runs/r/report-review').json()['latest']['is_current'] is True
    changed = {**body, 'report_digest': 'b' * 64}
    assert post_review(restarted, changed).status_code == 409


def test_orphaned_claim_after_crash_never_repeats_paid_work_under_new_key(tmp_path, monkeypatch):
    store = run_source(tmp_path / 'r')
    fake = Client()
    monkeypatch.setattr('looplab.serve.server.make_llm_client', lambda *a, **kw: fake)
    app = make_app(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    body = body_of(client.get('/api/runs/r/report-review').json())
    def crash(*args, **kwargs):
        raise OSError('simulated process death after durable claim')
    monkeypatch.setattr(app.state.looplab.jobs, 'start_reserved', crash)
    assert post_review(client, body).status_code == 500
    assert len(fake.messages) == 0
    assert sum(e.type == 'report_review_started' for e in store.read_all()) == 1
    restarted = TestClient(make_app(tmp_path))
    replayed = post_review(restarted, body).json()
    assert replayed['code'] == 'report_review_uncertain' and replayed['ambiguous'] is True
    assert post_review(restarted, body, key='new-key').status_code == 409
    assert len(fake.messages) == 0


def test_no_durable_claim_means_no_provider_and_provider_error_preserves_report(tmp_path, monkeypatch):
    store = run_source(tmp_path / 'r')
    fake = Client()
    monkeypatch.setattr('looplab.serve.server.make_llm_client', lambda *a, **kw: fake)
    client = TestClient(make_app(tmp_path), raise_server_exceptions=False)
    body = body_of(client.get('/api/runs/r/report-review').json())
    real_claim = service.append_claim
    def broken_claim(*args, **kwargs):
        raise OSError('unavailable durable storage')
    monkeypatch.setattr(service, 'append_claim', broken_claim)
    assert post_review(client, body).status_code == 500
    assert not fake.messages and not any(e.type == 'report_review_started' for e in store.read_all())
    monkeypatch.setattr(service, 'append_claim', real_claim)
    def failed(messages, schema):
        fake.messages.append(messages)
        raise RuntimeError('private provider failure should never enter receipt')
    monkeypatch.setattr(fake, 'complete_tool', failed)
    result = post_review(client, body).json()
    assert result['code'] == 'report_review_failed'
    assert 'private provider failure' not in store.path.read_text()
    assert fold(store.read_all()).report['verdict'] == 'The mechanism is proven.'
    assert post_review(TestClient(make_app(tmp_path)), body).json() == result
    assert len(fake.messages) == 1


def test_result_that_became_stale_while_model_worked_is_saved_as_historical(tmp_path, monkeypatch):
    store = run_source(tmp_path / 'r')
    fake = Client()
    real = fake.complete_tool
    def changed(messages, schema):
        store.append('node_reset', {'node_id': 1, 'from_stage': 'eval'})
        return real(messages, schema)
    monkeypatch.setattr(fake, 'complete_tool', changed)
    monkeypatch.setattr('looplab.serve.server.make_llm_client', lambda *a, **kw: fake)
    app = make_app(tmp_path)
    client = TestClient(app)
    result = post_review(client, body_of(client.get('/api/runs/r/report-review').json())).json()
    assert result['ok']
    view = client.get('/api/runs/r/report-review').json()
    assert view['latest']['is_current'] is False
    with pytest.raises(ValueError, match='historical'):
        service.read_nomination(app.state.looplab, store.path.parent, result['seq'],
            result['content']['comments'][0]['nomination_id'])


def test_native_nomination_tail_race_rechecks_before_append(tmp_path, monkeypatch):
    store = run_source(tmp_path / 'r')
    fake = Client()
    monkeypatch.setattr('looplab.serve.server.make_llm_client', lambda *a, **kw: fake)
    app = make_app(tmp_path)
    host, client = app.state.looplab, TestClient(app)
    result = post_review(client, body_of(client.get('/api/runs/r/report-review').json())).json()
    args = {'run_id': 'r', 'review_seq': result['seq'],
        'nomination_id': result['content']['comments'][0]['nomination_id']}
    real_append, raced = EventStore.append, []
    def append_race(self, kind, data, **kwargs):
        if kind == 'hint' and 'review_nomination' in data and not raced:
            raced.append(True)
            real_append(self, 'report_generated', {'content': {'verdict': 'Changed after check.'}})
        return real_append(self, kind, data, **kwargs)
    monkeypatch.setattr(EventStore, 'append', append_race)
    tool = RunControlTools(tmp_path, mode='auto', approver=lambda a: 'allow_once',
        allow_inject=True, command_service=host.commands,
        report_review_reader=lambda rd: service.review_view(host, rd),
        report_nomination_reader=lambda rd, seq, nid: service.read_nomination(host, rd, seq, nid))
    output = tool.execute('nominate_report_review', args)
    assert raced and 'report_review_nomination_stale' in output, output
    assert not any(e.type == 'hint' for e in store.read_all())


@pytest.mark.parametrize('damage', ['invented_outcome', 'huge_summary', 'wrong_prefix'])
def test_corrupt_completed_receipt_is_neither_disclosed_applied_nor_repaid(tmp_path, monkeypatch, damage):
    store = run_source(tmp_path / 'r')
    fake = Client()
    monkeypatch.setattr('looplab.serve.server.make_llm_client', lambda *a, **kw: fake)
    app = make_app(tmp_path)
    client = TestClient(app)
    body = body_of(client.get('/api/runs/r/report-review').json())
    result = post_review(client, body).json()
    rows = [json.loads(line) for line in store.path.read_text().splitlines()]
    if damage == 'wrong_prefix':
        row = next(row for row in rows if row['type'] == 'report_review_started')
        row['data']['source_event_seq'] = result['seq']
    else:
        row = next(row for row in rows if row['type'] == 'report_review_completed')
        content = row['data']['content']
        if damage == 'invented_outcome': content['comments'][0]['outcomes'][1]['metric'] = 999
        if damage == 'huge_summary': content['summary'] = 'private forged text ' * 10000
    store.path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    restarted = TestClient(make_app(tmp_path))
    view = restarted.get('/api/runs/r/report-review').json()
    assert view['latest']['unavailable'] and view['latest']['content'] is None
    assert 'private forged text' not in json.dumps(view)
    assert post_review(restarted, body).json()['code'] == 'report_review_uncertain'
    assert post_review(restarted, body, key='another-key').json()['code'] == 'report_review_uncertain'
    assert len(fake.messages) == 1


def test_exact_captured_prefix_survives_native_append_between_snapshot_and_claim(tmp_path, monkeypatch):
    store = run_source(tmp_path / 'r')
    fake = Client()
    monkeypatch.setattr('looplab.serve.server.make_llm_client', lambda *a, **kw: fake)
    app = make_app(tmp_path)
    client = TestClient(app)
    body = body_of(client.get('/api/runs/r/report-review').json())
    original = service.append_claim
    def changed(*args, **kwargs):
        store.append('hint', {'text': 'New user scope after the captured snapshot.'})
        return original(*args, **kwargs)
    monkeypatch.setattr(service, 'append_claim', changed)
    result = post_review(client, body).json()
    assert result['ok'], result
    view = client.get('/api/runs/r/report-review').json()
    assert view['latest']['is_current'] is False and not view['latest']['unavailable']
    assert view['latest']['content'] == result['content']


@pytest.mark.parametrize('race', [False, True])
def test_approval_card_records_only_nomination_and_refuses_stale_during_approval(tmp_path, monkeypatch, race):
    store = run_source(tmp_path / 'r')
    fake = Client()
    monkeypatch.setattr('looplab.serve.server.make_llm_client', lambda *a, **kw: fake)
    app = make_app(tmp_path)
    host = app.state.looplab
    client = TestClient(app)
    body = body_of(client.get('/api/runs/r/report-review').json())
    result = post_review(client, body).json()
    nomination = result['content']['comments'][0]['nomination_id']
    args = {'run_id': 'r', 'review_seq': result['seq'], 'nomination_id': nomination}
    cards = []
    readers = {'report_review_reader': lambda rd: service.review_view(host, rd),
        'report_nomination_reader': lambda rd, seq, nid: service.read_nomination(host, rd, seq, nid)}
    def approve(action):
        cards.append(action)
        if race:
            store.append('node_reset', {'node_id': 1, 'from_stage': 'eval'})
        return 'allow_once'
    tool = RunControlTools(tmp_path, mode='auto', approver=approve, allow_inject=True,
        command_service=host.commands, **readers)
    host.commands.spawn_engine = lambda *a, **kw: pytest.fail('nomination spawned an engine')
    out = tool.execute('nominate_report_review', args)
    if race:
        assert 'report_review_nomination_stale' in out, out
        assert not any(e.type == 'hint' for e in store.read_all())
        return
    assert 'recorded' in out, out
    assert len(cards) == 1 and 'No evaluation' in cards[0]['preview']
    events = store.read_all()
    hint = next(e for e in events if e.type == 'hint')
    assert hint.data['review_nomination']['nomination_id'] == nomination
    assert 'research consideration only' in hint.data['text']
    assert not {e.type for e in events} & {'force_confirm', 'force_ablate', 'inject_node', 'resume'}
    assert len(fake.messages) == 1


def test_accepted_nomination_recovers_exact_command_after_evidence_changed(tmp_path, monkeypatch):
    store = run_source(tmp_path / 'r')
    fake = Client()
    monkeypatch.setattr('looplab.serve.server.make_llm_client', lambda *a, **kw: fake)
    app = make_app(tmp_path)
    host, client = app.state.looplab, TestClient(app)
    result = post_review(client, body_of(client.get('/api/runs/r/report-review').json())).json()
    args = {'run_id': 'r', 'review_seq': result['seq'],
        'nomination_id': result['content']['comments'][0]['nomination_id']}
    journal = tmp_path / 'turn-mutations.json'
    cards = []
    shared = dict(allow_inject=True, command_service=host.commands,
        command_key_namespace='session:review-lost-response', mutation_journal_path=journal,
        report_review_reader=lambda rd: service.review_view(host, rd),
        report_nomination_reader=lambda rd, seq, nid: service.read_nomination(host, rd, seq, nid))
    host.commands.spawn_engine = lambda *a, **kw: pytest.fail('nomination spawned an engine')
    original = RunControlTools(tmp_path, mode='auto',
        approver=lambda card: cards.append(card) or 'allow_once', **shared)
    first = original.execute('nominate_report_review', args)
    assert 'recorded' in first, first
    saved = json.loads(journal.read_text())
    assert len(saved['entries']) == 1
    # The accepted hint itself changes the operator context; another outcome also arrives.
    store.append('node_created', {'node_id': 2, 'operator': 'draft', 'idea': {'operator': 'draft'}})
    assert client.get('/api/runs/r/report-review').json()['latest']['is_current'] is False
    def forbidden(*a):
        pytest.fail('recovery reconstructed or reapproved an already accepted nomination')
    shared.update(report_review_reader=forbidden, report_nomination_reader=forbidden)
    recovered = RunControlTools(tmp_path, mode='plan', approver=forbidden,
        mutation_recovery=True, **shared)
    assert recovered.execute('nominate_report_review', args) == first
    assert len(cards) == 1 and sum(e.type == 'hint' for e in store.read_all()) == 1
    changed = RunControlTools(tmp_path, mode='auto', approver=forbidden,
        mutation_recovery=True, **shared)
    assert 'report_nomination_recovery_changed' in changed.execute('nominate_report_review',
        {**args, 'nomination_id': 'b' * 64})
    assert sum(e.type == 'hint' for e in store.read_all()) == 1 and len(fake.messages) == 1


@pytest.mark.parametrize('mode,decision', [('plan', 'allow_once'), ('auto', 'deny'), ('auto', None)])
def test_nomination_requires_its_separate_permission_even_in_auto(tmp_path, monkeypatch, mode, decision):
    store = run_source(tmp_path / 'r')
    monkeypatch.setattr('looplab.serve.server.make_llm_client', lambda *a, **kw: Client())
    app = make_app(tmp_path)
    host, client = app.state.looplab, TestClient(app)
    result = post_review(client, body_of(client.get('/api/runs/r/report-review').json())).json()
    args = {'run_id': 'r', 'review_seq': result['seq'],
        'nomination_id': result['content']['comments'][0]['nomination_id']}
    cards = []
    tool = RunControlTools(tmp_path, mode=mode,
        approver=None if decision is None else lambda card: cards.append(card) or decision,
        allow_inject=True, command_service=host.commands,
        report_review_reader=lambda rd: service.review_view(host, rd),
        report_nomination_reader=lambda rd, seq, nid: service.read_nomination(host, rd, seq, nid))
    assert 'recorded' not in tool.execute('nominate_report_review', args)
    assert len(cards) == (1 if mode == 'auto' and decision is not None else 0)
    assert not any(e.type == 'hint' for e in store.read_all())


@pytest.mark.parametrize('damage', ['bool_seq', 'extra', 'uid_not_digest', 'bool_generation'])
def test_unsupported_request_metadata_is_refused_before_claim_or_provider(tmp_path, monkeypatch, damage):
    store = run_source(tmp_path / 'r')
    monkeypatch.setattr('looplab.serve.server.make_llm_client',
        lambda *a, **kw: pytest.fail('invalid request constructed a provider'))
    client = TestClient(make_app(tmp_path))
    body = body_of(client.get('/api/runs/r/report-review').json())
    if damage == 'bool_seq': body['report_seq'] = True
    if damage == 'extra': body['untrusted_override'] = 'new budget'
    if damage == 'uid_not_digest': body['input_digest'] = 123
    if damage == 'bool_generation': body['expected_generation'] = True
    assert client.post('/api/runs/r/report-review', json=body,
        headers={'Idempotency-Key': 'bad-request'}).status_code in (400, 409)
    assert not any(e.type == 'report_review_started' for e in store.read_all())


def test_nonfinite_native_outcome_cannot_become_a_nomination(tmp_path):
    store = run_source(tmp_path / 'r')
    data = data_of(store)
    data['nodes'][1]['metric'] = float('nan')
    with pytest.raises(ValueError, match='measured feasible'):
        bind_review(data, ReportReviewOut.model_validate(AUTHORED))


def test_native_review_uses_current_prose_language_without_rewriting_original_evidence(tmp_path, monkeypatch):
    from looplab.core.config import Settings
    store = run_source(tmp_path / 'r')
    fake = Client()
    monkeypatch.setattr('looplab.serve.server.make_llm_client', lambda *a, **kw: fake)
    app = make_app(tmp_path)
    monkeypatch.setattr(app.state.looplab, 'llm_settings', lambda rd: Settings(output_language='ru'))
    client = TestClient(app)
    result = post_review(client, body_of(client.get('/api/runs/r/report-review').json())).json()
    assert result['ok'], result
    assert '[LoopLab output language: Russian (ru)]' in fake.messages[0][0]['content']
    assert result['content']['comments'][0]['quote'] == 'The mechanism is proven.'
    assert result['content']['comments'][0]['outcomes'][1]['metric'] == .4
    assert fold(store.read_all()).report['verdict'] == 'The mechanism is proven.'


def test_new_metric_behavior_observation_stales_prior_review_but_preserves_original_paid_receipt(tmp_path, monkeypatch):
    from looplab.core.metric_behavior import metric_behavior_scan
    store = run_source(tmp_path / 'r')
    fake = Client()
    monkeypatch.setattr('looplab.serve.server.make_llm_client', lambda *a, **kw: fake)
    client = TestClient(make_app(tmp_path))
    body = body_of(client.get('/api/runs/r/report-review').json())
    original = post_review(client, body).json()
    state = fold(store.read_all())
    store.append('metric_behavior_scan', metric_behavior_scan(state.nodes[1], state.nodes.values()))
    view = client.get('/api/runs/r/report-review').json()
    assert view['latest']['is_current'] is False and not view['latest']['unavailable']
    assert view['latest']['content'] == original['content']
    assert post_review(TestClient(make_app(tmp_path)), body).json() == original
    assert len(fake.messages) == 1
