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
    if path.endswith("/state"):
        return {"generation": GEN, "state": {"run_uid": "incarnation"}}
    if path.endswith("/harness-handoff"):
        return {"generation": GEN, "run_id": run_id, "mode": "external_harness",
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
        for suffix in ("state", "harness-handoff", "harness-progress")]
    assert all(r.method == "GET" and not r.content and "Idempotency-Key" not in r.headers for r in seen)
    assert all(r.headers["X-LoopLab-Token"] == "scoped" for r in seen)
    assert dict(seen[0].url.params)=={"observe_only":"true"}
    assert seen[1].url.params["expected_generation"] == seen[2].url.params["expected_generation"] == GEN
    assert seen[2].url.params["brief"] == "true"


@pytest.mark.parametrize("status,code", [(401,"credential_refused"),(403,"access_refused"),
    (404,"run_not_found"),(409,"run_context_changed"),(503,"source_unavailable"),(302,"api_read_failed")])
@pytest.mark.parametrize("at", ["state", "handoff", "progress"])
def test_connection_stops_at_first_refusal_without_echoing_sensitive_bodies(status, code, at):
    seen=[]
    suffix={"state":"state","handoff":"harness-handoff","progress":"harness-progress"}[at]
    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/"+suffix):
            return httpx.Response(status, json={"detail":"private-secret-url-or-token"})
        return httpx.Response(200,json=response(request.url.path))
    api=HarnessAPI("http://localhost", "scoped", transport=httpx.MockTransport(handler))
    result=api.connection_check("demo")
    assert result["ok"] is False and result["code"]==code and result["at"]==at
    assert "private-secret" not in str(result)
    assert len(seen)=={"state":1,"handoff":2,"progress":3}[at]


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
        if state_uppercase and request.url.path.endswith("/state"):
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


def test_connection_refuses_server_without_scoped_credential():
    seen=[]
    def handler(request):
        seen.append(request)
        value=response(request.url.path)
        if request.url.path.endswith("/harness-handoff"):value["credential_configured"]=False
        return httpx.Response(200,json=value)
    api=HarnessAPI("http://localhost",transport=httpx.MockTransport(handler))
    assert api.connection_check("demo")["code"]=="harness_credential_missing"
    assert len(seen)==2
