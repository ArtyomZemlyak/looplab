"""A connected stdio process must not be confused with access to a live run."""
import httpx
import pytest

from looplab.harness.mcp_server import HarnessAPI

GEN = "a" * 64


def progress():
    """Critical fields of the server's compact progress, including empty gates."""
    return {"generation": GEN, "run_uid": "incarnation", "event_seq": 3, "at_node": 0,
        "evidence_revision": "b" * 64, "complete": True,
        "source_health": {name: {"read_complete": True} for name in
                          ("events", "decisions", "reviews", "checkpoints")},
        "next_step": {"code": "choose_direction", "title": "Choose", "detail": "Read evidence",
                      "owner": "external_agent", "reads": [], "action": None, "phase_id": None},
        "recorded_lifecycle": {"paused": False, "finished": False, "stop_requested": False},
        "execution": {"engine_running": False, "agent_connection": "not_measured",
                      "recorded_node_counts": {name: 0 for name in
                                               ("building", "queued", "evaluating", "pending")}},
        "candidate_blockers_if_expanding": [], "candidate_decisions_per_idea": {},
        "candidate_requirements": {"effective_concepts": False, "hypothesis_statement": False},
        "finish_reviews_due": [], "finish_report_due": False,
        "finish_pending_nodes": [], "finish_pending_node_count": 0,
        "finish_pending_nodes_truncated": False, "pending_checkpoints": [],
        "pending_checkpoint_count": 0, "pending_checkpoints_truncated": False}


def response(path, run_id="demo"):
    if path.endswith("/harness-identity"):
        return {"version": 1, "generation": GEN, "run_uid": "incarnation", "run_id": run_id,
                "event_seq": 3, "source_complete": True, "mode": "external_harness"}
    if path.endswith("/harness-handoff"):
        return {"generation": GEN, "run_uid": "incarnation", "run_id": run_id, "mode": "external_harness",
                "credential_configured": True,
                "server_paths": {"run_dir": "/runs/demo", "run_root": "/runs", "token":"must-not-be-exported"},
                "engine_running": False, "token": "must-not-be-exported"}
    return progress()


def test_connection_reads_only_fenced_context_preserving_obligations_and_stopped_engine():
    seen = []
    run_id = "mnist # %2F"
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=response(request.url.path, run_id))
    api = HarnessAPI("http://localhost/proxy", "scoped", transport=httpx.MockTransport(handler))
    result = api.connection_check(run_id, GEN)
    assert result["ok"] and result["code"] == "run_reads_succeeded"
    assert result["engine_running"] is False and result["agent_connection"] == "not_measured"
    assert result["next_step"]["code"] == "choose_direction"
    assert "must-not-be-exported" not in str(result)
    assert [r.url.path for r in seen] == [f"/proxy/api/runs/{run_id}/{suffix}"
        for suffix in ("harness-identity", "harness-handoff", "harness-progress")]
    assert all(r.method == "GET" and not r.content and "Idempotency-Key" not in r.headers for r in seen)
    assert all(r.headers["X-LoopLab-Token"] == "scoped" for r in seen)
    assert dict(seen[0].url.params)=={}
    assert seen[1].url.params["expected_generation"] == seen[2].url.params["expected_generation"] == GEN
    assert seen[2].url.params["brief"] == "true"


@pytest.mark.parametrize("status,code", [(401,"credential_refused"),(403,"access_refused"),
    (404,"run_not_found"),(409,"run_context_changed"),(503,"source_unavailable"),(302,"api_read_failed")])
@pytest.mark.parametrize("at", ["identity", "handoff", "progress"])
def test_connection_stops_at_first_refusal_without_echoing_sensitive_bodies(status, code, at):
    seen=[]
    suffix={"identity":"harness-identity","handoff":"harness-handoff","progress":"harness-progress"}[at]
    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/"+suffix):
            return httpx.Response(status, json={"detail":"private-secret-url-or-token"})
        return httpx.Response(200,json=response(request.url.path))
    api=HarnessAPI("http://localhost", "scoped", transport=httpx.MockTransport(handler))
    result=api.connection_check("demo")
    expected_code = "connection_identity_unavailable" if status == 404 and at == "identity" else code
    assert result["ok"] is False and result["code"]==expected_code and result["at"]==at
    assert "private-secret" not in str(result)
    assert len(seen)=={"identity":1,"handoff":2,"progress":3}[at]


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_connection_unreachable_is_fixed_diagnostic_without_exception_details(error):
    def handler(request):
        raise error("private-secret-url-or-token",request=request)
    api=HarnessAPI("http://localhost",transport=httpx.MockTransport(handler))
    result=api.connection_check("demo")
    assert result["code"]=="api_unreachable" and result["status"] is None
    assert "private-secret" not in str(result)


@pytest.mark.parametrize("url", ["http://localhost:private-secret", "http://[private-secret"])
def test_invalid_server_url_refused_without_echoing_value(url):
    with pytest.raises(ValueError, match="valid HTTP") as caught:
        HarnessAPI(url)
    assert "private-secret" not in str(caught.value)


def test_connection_stale_handoff_never_reads_replacement_context():
    seen=[]
    def handler(request):
        seen.append(request)
        return httpx.Response(200,json=response(request.url.path))
    api=HarnessAPI("http://localhost",transport=httpx.MockTransport(handler))
    assert api.connection_check("demo","b"*64)["code"]=="generation_mismatch"
    assert len(seen)==1


@pytest.mark.parametrize("state_uppercase", [False, True])
def test_connection_hex_case_does_not_misidentify_the_same_generation(state_uppercase):
    seen = []
    def handler(request):
        seen.append(request)
        value = response(request.url.path)
        if state_uppercase and request.url.path.endswith("/harness-identity"):
            value["generation"] = GEN.upper()
        return httpx.Response(200, json=value)
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(handler))
    checked = api.connection_check("demo", GEN.upper())
    assert checked["ok"] and checked["generation"] == GEN
    assert len(seen) == 3
    assert all(r.url.params["expected_generation"] == GEN for r in seen[1:])


@pytest.mark.parametrize("run_id,generation", [("../other", ""), ("demo","stale"),("","")])
def test_connection_invalid_identity_never_opens_transport(run_id,generation):
    api=HarnessAPI("http://localhost",transport=httpx.MockTransport(lambda _:pytest.fail("unexpected HTTP")))
    with pytest.raises(ValueError):api.connection_check(run_id,generation)


@pytest.mark.parametrize("bad", [[], {"generation":"not-a-generation"},
    {"ok":False,"secret":"must-not-be-exported"}])
def test_connection_refuses_malformed_state(bad):
    api=HarnessAPI("http://localhost",transport=httpx.MockTransport(lambda _:httpx.Response(200,json=bad)))
    result=api.connection_check("demo")
    assert result["code"]=="invalid_response" and "must-not-be-exported" not in str(result)


def test_connection_rejects_mismatched_context():
    api=HarnessAPI("http://localhost",transport=httpx.MockTransport(
        lambda r:httpx.Response(200,json=response(r.url.path,"another-run"))))
    assert api.connection_check("demo")["code"]=="invalid_response"


def test_connection_keeps_incomplete_journal_evidence_explicit():
    def handler(request):
        value=response(request.url.path)
        if request.url.path.endswith("/harness-progress"):
            value["complete"] = False
            value["source_health"]["reviews"]["read_complete"] = False
            value["next_step"]["code"] = "inspect_sources"
        return httpx.Response(200,json=value)
    api=HarnessAPI("http://localhost",transport=httpx.MockTransport(handler))
    result=api.connection_check("demo")
    assert result["ok"] and result["evidence_complete"] is False
    assert result["next_step"]["code"]=="inspect_sources"
    assert result["source_health"]["reviews"]["read_complete"] is False


def test_connection_on_an_open_server_is_anonymous_not_refused():
    """doc 82 П1: a server with no harness credential is the open UI. The reads succeed without a
    token, so connection_check passes and says the connection is anonymous."""
    seen=[]
    def handler(request):
        seen.append(request)
        value=response(request.url.path)
        if request.url.path.endswith("/harness-handoff"):value["credential_configured"]=False
        return httpx.Response(200,json=value)
    api=HarnessAPI("http://localhost",transport=httpx.MockTransport(handler))
    result=api.connection_check("demo")
    assert result["ok"] and result["code"]=="run_reads_succeeded"
    assert result["credential"]=="anonymous"
    assert len(seen)==3
    assert all("X-LoopLab-Token" not in r.headers for r in seen)


def test_connection_on_a_protected_server_without_token_names_the_credential():
    api=HarnessAPI("http://localhost",transport=httpx.MockTransport(
        lambda request: httpx.Response(401, json={"detail": "unauthorized"})))
    result=api.connection_check("demo")
    assert result["ok"] is False and result["code"]=="credential_required"
    assert "LOOPLAB_HARNESS_TOKEN" in result["message"]




@pytest.mark.parametrize("field,value", [("version",True),("version",2),("run_uid",123),
    ("run_uid",[]),("run_uid",""),("event_seq",True),("event_seq",-1),
    ("source_complete",False),("mode","internal"),("run_id","other")])
def test_malformed_identity_is_refused_before_larger_context_reads(field,value):
    seen=[]
    def handler(request):
        seen.append(request)
        body=response(request.url.path);body[field]=value
        return httpx.Response(200,json=body)
    result=HarnessAPI("http://localhost",transport=httpx.MockTransport(handler)).connection_check("demo")
    assert result["code"] == "invalid_response" and result["at"] == "identity"
    assert len(seen) == 1


@pytest.mark.parametrize("endpoint", ["harness-handoff","harness-progress"])
def test_same_generation_context_cannot_change_the_run_uid(endpoint):
    seen=[]
    def handler(request):
        seen.append(request)
        body=response(request.url.path)
        if request.url.path.endswith('/'+endpoint):body['run_uid']='other-incarnation'
        return httpx.Response(200,json=body)
    result=HarnessAPI("http://localhost",transport=httpx.MockTransport(handler)).connection_check("demo")
    assert result["code"] == "invalid_response"
    assert len(seen) == (2 if endpoint == "harness-handoff" else 3)


def test_real_bootstrap_never_fetches_broad_state_and_identity_stays_small(tmp_path,monkeypatch):
    import json
    from tests.test_harness_handoff import _run
    rd,store,client=_run(tmp_path,monkeypatch)
    store.append('hint',{'text':'large prior direction '*20000})
    before=store.path.read_bytes()
    seen=[]
    def bridge(request):
        seen.append(request.url.path)
        assert not request.url.path.endswith('/state'), 'bootstrap requested the broad projection'
        result=client.request(request.method,request.url.raw_path.decode(),headers=request.headers)
        if request.url.path.endswith('/harness-identity'):
            assert len(result.content) < 2048
            assert set(result.json()) == {'version','run_id','run_uid','generation',
                'event_seq','source_complete','mode'}
        return httpx.Response(result.status_code,content=result.content)
    api=HarnessAPI('http://testserver','agent-secret-1234',transport=httpx.MockTransport(bridge))
    result=api.connection_check('demo')
    assert result['ok'],result
    assert [x.rsplit('/',1)[1] for x in seen] == ['harness-identity','harness-handoff','harness-progress']
    assert store.path.read_bytes() == before
    assert 'large prior' not in json.dumps(result)


def test_identity_refuses_torn_source_without_repair(tmp_path,monkeypatch):
    from tests.test_harness_handoff import _run
    rd,store,client=_run(tmp_path,monkeypatch)
    with store.path.open('ab') as handle:handle.write(b'{"torn":')
    before=store.path.read_bytes()
    response=client.get('/api/runs/demo/harness-identity',headers={'X-LoopLab-Token':'agent-secret-1234'})
    assert response.status_code == 503
    assert store.path.read_bytes() == before
