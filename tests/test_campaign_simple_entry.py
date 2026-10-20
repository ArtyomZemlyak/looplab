"""Doc 82 П7: preserve same-node arm identity and recover terminal sidecars idempotently."""
import json

from looplab.core.config import Settings
from looplab.core.models import Idea
from looplab.events.replay import fold
from looplab.harness.decisions import missing_decisions
from looplab.serve.per_query_auto import import_terminal_arrays
from tests.test_campaign_protocol import (_client, _repo, _make_run, _arrays, _upload,
                                          _experiment, _gen, AGENT, METRICS)


def test_two_policies_of_one_node_read_their_own_upload(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, 'policies', _repo(tmp_path), nodes=(0,), metrics={0: 0.4})
    exp = _experiment(arms={'A0': {'node_id': 0}, 'A1': {'node_id': 0}, 'A2': {'node_id': 0}})
    reply = client.post('/api/runs/policies/protocol/amendments', headers=AGENT, json={
        'expected_generation': _gen(store), 'action_id': 'reg', 'kind': 'experiment',
        'experiment': exp, 'reason': 'compare inference policies'})
    assert reply.status_code == 200, reply.text
    for arm, lift in [('A0', 0), ('A1', 0.2), ('A2', 0.4)]:
        payload, *_ = _arrays(42, lift)
        result = _upload(client, 'policies', store, 0, payload, action=arm,
                         experiment_id='D4', arm=arm)
        assert result.status_code == 200, result.text
    view = client.get('/api/runs/policies/per-query', headers=AGENT).json()
    assert len(view['uploads']) == 3
    comparison = client.get('/api/runs/policies/experiments/D4/comparison', headers=AGENT).json()
    rows = comparison.get('comparisons', comparison.get('contrasts', []))
    assert rows, comparison
    for row in rows:
        assert row['status'] == 'computed', row
        assert row['treatment_value'] > row['control_value'], row
        assert row['treatment_source']['upload_id'] != row['control_source']['upload_id'], row


def _sidecar(rd, payload, **options):
    directory = rd / 'nodes/node_0/per_query'
    directory.mkdir(parents=True)
    (directory / 'test.npz').write_bytes(payload)
    (directory / 'test.json').write_text(json.dumps({'metrics': METRICS,
                                                    'test_label': 'original_ic', **options}))
    return directory


def test_terminal_sidecars_import_once_and_manual_upload_wins(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, 'auto', _repo(tmp_path), nodes=(0,), metrics={0: 0.4})
    payload, *_ = _arrays(42)
    _sidecar(rd, payload)
    srv = client.app.state.looplab
    uploaded = import_terminal_arrays(srv, rd)
    assert len(uploaded) == 1
    assert uploaded[0]['source']['origin'] == 'node_workdir:per_query/test.npz'
    assert import_terminal_arrays(srv, rd) == []
    assert len(client.get('/api/runs/auto/per-query', headers=AGENT).json()['uploads']) == 1
    # Another terminal with an existing manual receipt consumes no second upload.
    (tmp_path / 'other').mkdir()
    rd2, store2 = _make_run(root, 'manual', _repo(tmp_path / 'other'), nodes=(0,), metrics={0: 0.4})
    _sidecar(rd2, payload)
    assert _upload(client, 'manual', store2, 0, payload).status_code == 200
    assert import_terminal_arrays(srv, rd2) == []


def test_no_declaration_and_linked_candidate_files_do_not_import(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, 'refuse', _repo(tmp_path), nodes=(0,), metrics={0: 0.4})
    payload, *_ = _arrays(42)
    directory = _sidecar(rd, payload)
    (directory / 'test.json').write_text('{}')
    assert import_terminal_arrays(client.app.state.looplab, rd) == []
    (directory / 'test.json').write_text(json.dumps({'metrics': METRICS}))
    (directory / 'test.npz').unlink()
    outside = tmp_path / 'outside.npz'
    outside.write_bytes(payload)
    (directory / 'test.npz').symlink_to(outside)
    assert import_terminal_arrays(client.app.state.looplab, rd) == []


def test_artifact_has_no_novelty_receipt_but_experiment_does(tmp_path):
    settings = Settings(backend='toy', external_harness=True, novelty_mode='algo', foresight=False,
                        best_of_n=1, strategist_every=0)
    from looplab.core.models import RunState
    state = RunState(run_uid='u')
    idea = Idea(operator='inject')
    assert missing_decisions(tmp_path, settings, state, idea, 'a' * 64,
                             node_kind='artifact') == []
    assert missing_decisions(tmp_path, settings, state, idea, 'a' * 64) == ['novelty']


def test_a_nonterminal_never_imports_and_a_reset_gets_a_new_receipt(tmp_path, monkeypatch):
    from looplab.harness.per_query import publish_upload
    from looplab.serve.routers.campaign import PerQueryUploadBody
    from fastapi import HTTPException
    import pytest
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, 'reset', _repo(tmp_path), nodes=(0,))
    payload, *_ = _arrays(42)
    _sidecar(rd, payload)
    srv = client.app.state.looplab
    assert import_terminal_arrays(srv, rd) == []
    store.append('node_evaluated', {'node_id': 0, 'metric': 0.4})
    first = import_terminal_arrays(srv, rd)
    assert len(first) == 1 and first[0]['node_generation'] == 0
    store.append('node_reset', {'node_id': 0, 'from_stage': 'eval'})
    assert import_terminal_arrays(srv, rd) == []
    store.append('node_evaluated', {'node_id': 0, 'metric': 0.4, 'generation': 1})
    # A worker that read attempt 0 before the reset cannot publish those bytes as attempt 1.
    body = PerQueryUploadBody(expected_generation=_gen(store), node_id=0, action_id='stale',
                             workdir_path='per_query/test.npz', metrics=METRICS)
    with pytest.raises(HTTPException, match='lifecycle changed'):
        publish_upload(srv, rd, body, agent_token=False, auto_attempt=0)
    second = import_terminal_arrays(srv, rd)
    assert len(second) == 1 and second[0]['node_generation'] == 1
    assert first[0]['upload_id'] != second[0]['upload_id']
