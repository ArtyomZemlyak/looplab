"""Metadata is cheap, payload is not: exercise real HTTP/MCP publication and its race/replay fences."""
from pathlib import Path
import base64

import httpx
import pytest

from looplab.events.replay import fold
from looplab.harness import composite, per_query
from looplab.harness.mcp_server import HarnessAPI
from test_campaign_protocol import (_client, _repo, _make_run, _experiment, _amend, _arrays,
                                    _gen, METRICS, AGENT)


def _fixture(tmp_path, monkeypatch):
    root, client = _client(tmp_path, monkeypatch)
    rd, store = _make_run(root, "camp", _repo(tmp_path), nodes=(0, 1), metrics=[0.5, 0.5])
    exp = _experiment(arms={"A0": {"node_id": 0}, "A1": {"node_id": 1}},
                      contrasts=[{"treatment": "A1", "control": "A0"}])
    reply = _amend(client, "camp", store, "experiment", experiment=exp)
    assert reply.status_code == 200, reply.text

    def forward(request):
        response = client.request(request.method, request.url.raw_path.decode(), content=request.content,
                                  headers={k: v for k, v in request.headers.items()
                                           if k.lower() in ("content-type", "x-looplab-token")})
        return httpx.Response(response.status_code, content=response.content,
                              headers={"content-type": "application/json"})

    api = HarnessAPI("http://testserver/", "agent-secret", transport=httpx.MockTransport(forward),
                     request_dir=tmp_path / "client-store")
    meta = {"expected_generation": _gen(store), "action_id": "up0", "node_id": 0,
            "metrics": METRICS, "experiment_id": "D4", "arm": "A0", "test_label": "original_ic"}
    return client, api, rd, store, meta


def test_preflight_is_read_only_and_does_not_claim_payload_or_scalar_attestation(tmp_path, monkeypatch):
    client, _api, rd, store, meta = _fixture(tmp_path, monkeypatch)
    prefix = (rd / "events.jsonl").read_bytes()
    monkeypatch.setattr(per_query, "_upload_bytes", lambda *_: pytest.fail("metadata read payload"))
    reply = client.post("/api/runs/camp/per-query/preflight", json=meta, headers=AGENT)
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["metadata_only"] is True and body["mode"] == "new_upload"
    assert body["expected_attempt"] == 0 and body["protocol_version"] == 1
    assert body["payload_validation"] == "not_checked"
    assert body["consistency"]["status"] == "unchecked"
    assert (rd / "events.jsonl").read_bytes() == prefix
    assert not (rd / "per_query").exists()


@pytest.mark.parametrize("changes,status", [
    ({"experiment_id": "missing"}, 404),
    ({"arm": "cold33"}, 400),
    ({"node_id": 1}, 409),
    ({"node_id": 99}, 404),
    ({"expected_generation": "0" * 64}, 409),
    ({"expected_attempt": 1}, 409),
    ({"metrics": {"m": {"kind": "median", "array": "x"}}}, 400),
    ({"metrics": {"m": {"array": ""}}}, 400),
    ({"metrics": {"m": {"array": "x", "columns": [True]}}}, 400),
    ({"measured_check": {"metric": "unknown"}}, 400),
])
def test_bad_metadata_refuses_before_payload_read_or_decode(tmp_path, monkeypatch, changes, status):
    client, _api, rd, _store, meta = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(per_query, "_upload_bytes", lambda *_: pytest.fail("bad metadata read payload"))
    monkeypatch.setattr(per_query, "_arrays_from_payload", lambda *_: pytest.fail("bad metadata decoded"))
    reply = client.post("/api/runs/camp/per-query", json={**meta, **changes, "content_b64": "INVALID"},
                        headers=AGENT)
    assert reply.status_code == status, reply.text
    assert not (rd / "per_query").exists()


def test_client_dry_run_needs_no_file_and_bad_arm_does_not_read_or_spool(tmp_path, monkeypatch):
    _client_, api, _rd, store, meta = _fixture(tmp_path, monkeypatch)
    path = tmp_path / "payload.npz"
    path.write_bytes(_arrays(0)[0])
    original = Path.read_bytes

    def bounded_read(p):
        if p == path:
            pytest.fail("invalid metadata opened the large payload")
        return original(p)

    monkeypatch.setattr(Path, "read_bytes", bounded_read)
    kwargs = dict(node_id=0, metrics=METRICS, action_id="dry", test_label="original_ic",
                  experiment_id="D4", arm="A0")
    dry = composite.upload_per_query(api, "camp", _gen(store), path=str(path / "missing"),
                                     dry_run=True, **kwargs)
    assert dry["status"] == 200 and dry["body"]["metadata_only"]
    refused = composite.upload_per_query(api, "camp", _gen(store), path=str(path),
                                         **{**kwargs, "arm": "cold33"})
    assert refused["status"] == 400 and refused["step"] == "metadata_preflight"
    assert api.uploads.pending() == []


def test_reset_between_client_preflight_and_upload_cannot_rebind_old_vectors(tmp_path, monkeypatch):
    _client_, api, rd, store, _meta = _fixture(tmp_path, monkeypatch)
    path = tmp_path / "payload.npz"
    path.write_bytes(_arrays(0)[0])
    original = api.post_large

    def reset_before_post(*args, **kwargs):
        store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
        store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.1})
        return original(*args, **kwargs)

    monkeypatch.setattr(api, "post_large", reset_before_post)
    reply = composite.upload_per_query(api, "camp", _gen(store), node_id=0, path=str(path),
                                       metrics=METRICS, action_id="race", experiment_id="D4", arm="A0")
    assert reply["status"] == 409, reply
    assert reply["body"]["detail"]["code"] == "node_attempt_changed"
    assert not (rd / "per_query").exists()


@pytest.mark.parametrize("fence", [{}, {"expected_attempt": 0}])
def test_server_rechecks_after_array_decode_before_publication(tmp_path, monkeypatch, fence):
    client, _api, rd, store, meta = _fixture(tmp_path, monkeypatch)
    original = per_query._arrays_from_payload

    def reset_during_payload(fmt, raw):
        arrays = original(fmt, raw)
        store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
        store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.1})
        return arrays

    monkeypatch.setattr(per_query, "_arrays_from_payload", reset_during_payload)
    reply = client.post("/api/runs/camp/per-query", json={**meta, **fence,
                        "content_b64": base64.b64encode(_arrays(0)[0]).decode()}, headers=AGENT)
    assert reply.status_code == 409 and reply.json()["detail"]["code"] == "node_attempt_changed"
    assert not (rd / "per_query").exists()


def test_exact_accepted_replay_survives_reset_but_changed_body_is_conflict(tmp_path, monkeypatch):
    _client_, api, rd, store, _meta = _fixture(tmp_path, monkeypatch)
    path = tmp_path / "payload.npz"
    path.write_bytes(_arrays(0)[0])
    kwargs = dict(node_id=0, path=str(path), metrics=METRICS, action_id="exact",
                  experiment_id="D4", arm="A0", measured_check={"metric": "FUR@200", "node_metric": "absent"})
    first = composite.upload_per_query(api, "camp", _gen(store), **kwargs)
    assert first["status"] == 200, first
    assert first["body"]["upload"]["consistency"]["status"] == "no_measured_value"
    saved = first["body"]["upload"]
    assert saved["expected_attempt"] == 0
    store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
    assert fold(store.read_all()).nodes[0].attempt == 1
    monkeypatch.setattr(per_query, "_arrays_from_payload", lambda *_: pytest.fail("exact replay decoded again"))
    again = composite.upload_per_query(api, "camp", _gen(store), **kwargs)
    assert again["status"] == 200 and again["body"]["replayed"] is True, again
    assert again["body"]["upload"] == saved
    changed = composite.upload_per_query(api, "camp", _gen(store), **{**kwargs, "arm": "A1"})
    assert changed["status"] == 409, changed
    assert len(per_query.read_manifest(rd)[0]) == 1


@pytest.mark.parametrize("broken", [{}, {"ok": True}, {"v": 1, "metadata_only": True},
                                    {"expected_attempt": True}, {"generation": "0" * 64},
                                    {"mode": "unknown"}, {"run_uid": 123}, {"run_uid": ["uid"]},
                                    {"run_uid": True}, {"node_id": False},
                                    {"mode": "exact_replay_only", "metadata_validation": "prior_receipt_only",
                                     "upload_id": 123}])
def test_incomplete_successful_preflight_never_reads_or_spools_payload(tmp_path, monkeypatch, broken):
    _client_, api, _rd, store, meta = _fixture(tmp_path, monkeypatch)
    path = tmp_path / "payload.npz"
    path.write_bytes(_arrays(0)[0])
    real = api.request("POST", "/api/runs/camp/per-query/preflight", meta, read_only=True)
    body = broken if broken in ({}, {"ok": True}, {"v": 1, "metadata_only": True}) else {
        **real["body"], **broken}
    monkeypatch.setattr(api, "request", lambda *a, **k: {"status": 200, "body": body})
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("incomplete preflight read payload"))
    kwargs = dict(node_id=0, metrics=METRICS, action_id="incomplete", experiment_id="D4", arm="A0")
    for dry_run in (False, True):
        reply = composite.upload_per_query(api, "camp", _gen(store), path=str(path),
                                           dry_run=dry_run, **kwargs)
        assert reply["status"] == 503 and reply["code"] == "metadata_preflight_incomplete"
    assert api.uploads.pending() == []


def test_legacy_unfenced_receipt_replays_with_unchanged_request_digest(tmp_path, monkeypatch):
    client, api, _rd, store, meta = _fixture(tmp_path, monkeypatch)
    from looplab.serve.routers.campaign import PerQueryUploadBody
    path = tmp_path / "payload.npz"
    raw = _arrays(0)[0]
    path.write_bytes(raw)
    request = {**meta, "action_id": "legacy", "content_b64": base64.b64encode(raw).decode()}
    # The exact pre-addition digest has no expected_attempt key at all.
    import hashlib
    import orjson
    body = PerQueryUploadBody(**request)
    old_payload = body.model_dump(exclude={"expected_generation", "content_b64", "expected_attempt"})
    old_payload["source_sha256"] = hashlib.sha256(raw).hexdigest()
    old_digest = hashlib.sha256(orjson.dumps(old_payload, option=orjson.OPT_SORT_KEYS)).hexdigest()
    first = client.post("/api/runs/camp/per-query", json=request, headers=AGENT)
    assert first.status_code == 200, first.text
    saved = first.json()["upload"]
    assert saved["request_sha256"] == old_digest and saved["expected_attempt"] is None
    store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
    monkeypatch.setattr(per_query, "_arrays_from_payload", lambda *_: pytest.fail("legacy replay decoded"))
    replay = composite.upload_per_query(api, "camp", _gen(store), node_id=0, path=str(path),
                                        metrics=METRICS, action_id="legacy", experiment_id="D4",
                                        arm="A0", test_label="original_ic")
    assert replay["status"] == 200 and replay["body"]["replayed"] is True, replay
    assert replay["body"]["upload"] == saved


def test_fit_reference_is_not_score_owner_and_explicit_superseding_declaration_preserves_both(tmp_path,monkeypatch):
    from looplab.harness.campaign import read_journal,experiments_of
    client,_api,rd,store,meta=_fixture(tmp_path,monkeypatch)
    events_before=store.path.read_bytes()
    with monkeypatch.context() as blocked:
        blocked.setattr(per_query,'_upload_bytes',lambda *_:pytest.fail('ownership refusal read payload'))
        blocked.setattr(per_query,'_arrays_from_payload',lambda *_:pytest.fail('ownership refusal decoded payload'))
        for suffix,extra in [('/preflight',{}),('',{'content_b64':'INVALID'})]:
            response=client.post('/api/runs/camp/per-query'+suffix,json={**meta,'node_id':1,**extra},headers=AGENT)
            assert response.status_code==409,response.text
            detail=response.json()['detail']
            assert detail['code']=='arm_bound_elsewhere' and detail['ownership_source']=='registered_arm'
            assert detail['owner']['node_id']==0 and detail['submitted_node_id']==1
            assert detail['remediation']['supersedes']=='D4'
            assert 'model_producer' in detail['remediation']['message']
    assert store.path.read_bytes()==events_before and not (rd/'per_query').exists()
    # The prior declaration is immutable even under a new action. Correct it explicitly using
    # the existing supersedes protocol, without moving the saved vectors to the fit node.
    corrected=_experiment(experiment_id='D4-score-owner',supersedes='D4',
        arms={'A0':{'node_id':1}},contrasts=[],
        relationships=[{'kind':'model_producer','subject_arm':'A0','node_id':0,'node_generation':0}])
    registered=_amend(client,'camp',store,'experiment',action='explicit-score-owner',
                      reason='Correct score-vector owner; preserve checkpoint lineage',experiment=corrected)
    assert registered.status_code==200,registered.text
    payload=base64.b64encode(_arrays(0)[0]).decode()
    uploaded=client.post('/api/runs/camp/per-query',json={**meta,'node_id':1,
        'experiment_id':'D4-score-owner','action_id':'correct-score-owner-upload','content_b64':payload},headers=AGENT)
    assert uploaded.status_code==200,uploaded.text
    assert uploaded.json()['upload']['node_id']==1
    rows,health=read_journal(rd);assert health['read_complete']
    declarations=experiments_of(rows,fold(store.read_all()).run_uid)
    assert declarations['D4']['arms']['A0']['node_id']==0
    assert declarations['D4-score-owner']['arms']['A0']['node_id']==1
    assert declarations['D4-score-owner']['relationships'][0]['node_id']==0
    # Superseding never turns the old wrong-owner request into an alias for the score node.
    again=client.post('/api/runs/camp/per-query/preflight',json={**meta,'node_id':1},headers=AGENT)
    assert again.status_code==409 and again.json()['detail']['code']=='arm_bound_elsewhere'
    assert store.path.read_bytes()==events_before



def test_first_genuine_upload_binds_unknown_arm_owner_and_later_other_node_stays_refused_before_payload(tmp_path,monkeypatch):
    client,_api,rd,store,meta=_fixture(tmp_path,monkeypatch)
    exp=_experiment(experiment_id='future-owner',arms={'A0':{}},contrasts=[],
        relationships=[{'kind':'model_producer','subject_arm':'A0','node_id':1}])
    assert _amend(client,'camp',store,'experiment',action='future-owner-registration',experiment=exp).status_code==200
    body={**meta,'experiment_id':'future-owner','action_id':'first-vector-owner',
          'content_b64':base64.b64encode(_arrays(0)[0]).decode()}
    first=client.post('/api/runs/camp/per-query',json=body,headers=AGENT)
    assert first.status_code==200,first.text
    assert first.json()['upload']['node_id']==0
    before_manifest=per_query.manifest_path(rd).read_bytes();before_events=store.path.read_bytes()
    monkeypatch.setattr(per_query,'_upload_bytes',lambda *_:pytest.fail('owner conflict read payload'))
    monkeypatch.setattr(per_query,'_arrays_from_payload',lambda *_:pytest.fail('owner conflict decoded payload'))
    second=client.post('/api/runs/camp/per-query',json={**body,'node_id':1,
        'action_id':'other-vector-owner','content_b64':'INVALID'},headers=AGENT)
    assert second.status_code==409
    detail=second.json()['detail']
    assert detail['ownership_source']=='accepted_upload' and detail['owner']['node_id']==0
    assert detail['submitted_node_id']==1 and detail['remediation']['supersedes']=='future-owner'
    assert per_query.manifest_path(rd).read_bytes()==before_manifest and store.path.read_bytes()==before_events
