"""Actual harness reads, not owner polling or old generations, update contact."""
from concurrent.futures import ThreadPoolExecutor
import json

from looplab.events.run_generation import run_generation_token
from looplab.serve.agent_activity import AgentActivity, MAX_RUNS
from looplab.serve.server import make_app
from fastapi.testclient import TestClient
from tests.test_harness_handoff import _run

OWNER = {"X-LoopLab-Token": "operator-secret-1234"}
AGENT = {"X-LoopLab-Token": "agent-secret-1234"}


def _read(client, generation, headers=OWNER, **params):
    return client.get("/api/runs/demo/harness-progress", headers=headers,
                      params={"expected_generation": generation, **params})


def test_owner_polling_never_implies_agent_contact_and_reads_write_no_files(tmp_path, monkeypatch):
    rd, store, client = _run(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    before = {str(p.relative_to(rd)): p.read_bytes() for p in rd.rglob('*') if p.is_file()}
    for brief in (False, True):
        activity = _read(client, generation, brief=brief).json()["agent_activity"]
        assert activity["status"] == "not_observed"
        assert activity["last_seen_at"] is None and activity["age_seconds"] is None
    contact = _read(client, generation, AGENT, brief=True).json()["agent_activity"]
    assert contact["status"] == "recent_request" and contact["last_seen_at"]
    assert contact["source"] == "authenticated_harness_progress_read"
    assert contact["scope"] == "this_ui_process"
    assert _read(client, generation).json()["execution"]["agent_connection"] == "not_measured"
    assert {str(p.relative_to(rd)): p.read_bytes() for p in rd.rglob('*') if p.is_file()} == before
    assert not any(secret in str(contact) for secret in ("operator-secret", "agent-secret"))


def test_quiet_contact_uses_monotonic_time_and_only_agent_can_refresh_it(tmp_path, monkeypatch):
    rd, store, client = _run(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    now = [0]
    stamp = ["2026-10-01T10:00:00+00:00"]
    client.app.state.looplab.agent_activity = AgentActivity(clock=lambda: now[0], timestamp=lambda: stamp[0])
    first = _read(client, generation, AGENT).json()
    now[0] = 119
    assert _read(client, generation).json()["agent_activity"]["status"] == "recent_request"
    now[0] = 120
    stamp[0] = "2026-10-01T09:00:00+00:00"  # wall-clock change cannot alter observed request age
    quiet = _read(client, generation, brief=True).json()
    assert quiet["agent_activity"]["status"] == "quiet"
    assert quiet["agent_activity"]["age_seconds"] == 120
    assert quiet["agent_activity"]["last_seen_at"] == first["agent_activity"]["last_seen_at"]
    assert quiet["event_seq"] == first["event_seq"]
    assert _read(client, generation, {"X-LoopLab-Token": "wrong"}).status_code == 401
    assert _read(client, "0" * 64, AGENT).status_code == 409
    assert _read(client, generation).json()["agent_activity"] == quiet["agent_activity"]
    refreshed = _read(client, generation, AGENT, brief=True).json()
    assert refreshed["agent_activity"]["status"] == "recent_request"
    assert refreshed["agent_activity"]["age_seconds"] == 0
    assert refreshed["event_seq"] == first["event_seq"]


def test_new_generation_and_new_ui_have_no_inherited_contact(tmp_path, monkeypatch):
    rd, store, client = _run(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    assert _read(client, generation, AGENT).json()["agent_activity"]["status"] == "recent_request"
    fresh_ui = TestClient(make_app(rd.parent))
    assert _read(fresh_ui, generation).json()["agent_activity"]["status"] == "not_observed"
    # Reset replaces the first event, rather than appending another run_started.
    row = json.loads((rd / "events.jsonl").read_text().splitlines()[0])
    row["ts"] += 1
    row["data"]["run_uid"] = "different-incarnation"
    (rd / "events.jsonl").write_text(json.dumps(row) + "\n")
    new_generation = run_generation_token(store.read_all())
    assert generation != new_generation
    assert _read(client, new_generation).json()["agent_activity"]["status"] == "not_observed"
    assert _read(client, generation, AGENT).status_code == 409
    assert _read(client, new_generation).json()["agent_activity"]["status"] == "not_observed"


def test_activity_cache_is_bounded_and_safe_for_concurrent_reads():
    clock = lambda: 0
    activity = AgentActivity(clock=clock)
    for i in range(MAX_RUNS + 1):
        activity.observe(str(i), "gen")
    assert activity.snapshot("0", "gen")["status"] == "not_observed"
    assert activity.snapshot(str(MAX_RUNS), "gen")["status"] == "recent_request"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: (activity.observe("one", "gen"), activity.snapshot("one", "gen")), range(64)))
    assert len(activity._reads) == MAX_RUNS
    assert activity.snapshot("one", "gen")["age_seconds"] == 0


def test_successful_scoped_training_checks_count_as_activity_without_domain_writes(tmp_path, monkeypatch):
    rd, store, client = _run(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    now = [0]
    client.app.state.looplab.agent_activity = AgentActivity(clock=lambda: now[0])
    before = (rd / 'events.jsonl').read_bytes()
    path = '/api/runs/demo/harness-checkpoints'
    args = {'expected_generation': generation}
    assert client.get(path, params=args, headers=OWNER).status_code == 200
    assert _read(client, generation).json()['agent_activity']['status'] == 'not_observed'
    reply = client.get(path, params=args, headers=AGENT)
    assert reply.status_code == 200 and reply.json() == {'pending': []}
    assert reply.headers['Cache-Control'] == 'no-store'
    activity = _read(client, generation).json()['agent_activity']
    assert activity['status'] == 'recent_request'
    assert activity['source'] == 'authenticated_harness_read'
    assert activity['read_kind'] == 'harness-checkpoints'
    now[0] = 121
    assert client.get(path, params={'expected_generation': '0' * 64}, headers=AGENT).status_code == 409
    assert _read(client, generation).json()['agent_activity']['status'] == 'quiet'
    assert (rd / 'events.jsonl').read_bytes() == before


def test_failed_checkpoint_read_never_refreshes_activity(tmp_path, monkeypatch):
    rd, store, client = _run(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    from looplab.harness import checkpoints
    def unavailable(*args):
        raise OSError('unreadable ledger')
    monkeypatch.setattr(checkpoints, 'pending', unavailable)
    response = client.get('/api/runs/demo/harness-checkpoints',
        params={'expected_generation': generation}, headers=AGENT)
    assert response.status_code == 503
    assert client.app.state.looplab.agent_activity.snapshot(rd, generation)['status'] == 'not_observed'


def test_any_successful_agent_request_counts_reads_and_writes_alike(tmp_path, monkeypatch):
    """Review 2026-10-14 §2: an agent busy POSTing (decisions, reviews, commands) or reading
    `/state` looked silent because only the two progress/checkpoint GETs were counted."""
    rd, store, client = _run(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    now = [0]
    client.app.state.looplab.agent_activity = AgentActivity(clock=lambda: now[0])
    # Owner reads of the same routes never count.
    assert client.get("/api/runs/demo/state", headers=OWNER).status_code == 200
    assert _read(client, generation).json()["agent_activity"]["status"] == "not_observed"
    assert client.get("/api/runs/demo/state", headers=AGENT).status_code == 200
    activity = _read(client, generation).json()["agent_activity"]
    assert activity["status"] == "recent_request"
    assert (activity["read_kind"], activity["request_kind"]) == ("state", "read")
    assert activity["source"] == "authenticated_harness_read"
    now[0] = 500
    assert _read(client, generation).json()["agent_activity"]["status"] == "quiet"
    decision = {"expected_generation": generation, "phase_id": "novelty",
                "action_id": "novelty-1", "idea": {"operator": "draft"},
                "decision": "submit", "reason": "The idea differs from previous trials"}
    response = client.post("/api/runs/demo/harness-decisions", json=decision, headers=AGENT)
    assert response.status_code == 200, response.text
    activity = _read(client, generation).json()["agent_activity"]
    assert activity["status"] == "recent_request" and activity["age_seconds"] == 0
    assert (activity["read_kind"], activity["request_kind"]) == ("harness-decisions", "write")
    # A refused write (stale generation / invalid body) is not contact with this run.
    now[0] = 1000
    stale = client.post("/api/runs/demo/harness-decisions", headers=AGENT,
                        json={**decision, "action_id": "novelty-2", "expected_generation": "0" * 64})
    assert stale.status_code >= 400
    assert client.post("/api/runs/demo/harness-decisions", json={}, headers=AGENT).status_code == 422
    assert _read(client, generation).json()["agent_activity"]["status"] == "quiet"
    # A request about ANOTHER run, or a non-run route, does not refresh this run.
    assert client.get("/api/runs", headers=AGENT).status_code == 200
    assert client.get("/api/runs/other/state", headers=AGENT).status_code >= 400
    assert _read(client, generation).json()["agent_activity"]["status"] == "quiet"


def test_silence_is_a_warning_only_while_looplab_waits_for_the_agent(tmp_path, monkeypatch):
    from looplab.serve.agent_activity import waiting_for_agent
    activity = AgentActivity(clock=lambda: 1000)
    # Never observed and nothing pending: neutral, not a warning.
    snap = activity.snapshot("rd", "gen")
    assert snap["status"] == "not_observed" and snap["attention"] == "nothing_required"
    assert snap["waiting_for"] is None and snap["request_kind"] is None
    question = {"pending_checkpoint_count": 2, "next_step": {"code": "answer_checkpoint"},
                "execution": {"engine_running": True}}
    assert waiting_for_agent(question) == {"reason": "evaluation_question", "count": 2}
    assert activity.snapshot("rd", "gen", waiting=waiting_for_agent(question))["attention"] == "waiting_on_agent"
    idle = {"pending_checkpoint_count": 0, "next_step": {"code": "choose_direction"},
            "execution": {"engine_running": True}}
    assert waiting_for_agent(idle) == {"reason": "next_experiment", "count": 1}
    # A stopped engine or a training run with no question waits on nobody the chip can name.
    assert waiting_for_agent({**idle, "execution": {"engine_running": False}}) is None
    assert waiting_for_agent({"pending_checkpoint_count": 0, "next_step": {"code": "inspect_pending"},
                              "execution": {"engine_running": True}}) is None
    assert waiting_for_agent({}) is None and waiting_for_agent({"pending_checkpoint_count": "x"}) is None
    clock = [0]
    activity = AgentActivity(clock=lambda: clock[0])
    activity.observe("rd", "gen", read_kind="commands", write=True)
    clock[0] = 30
    assert activity.snapshot("rd", "gen", waiting=waiting_for_agent(question))["attention"] == "agent_has_work"
    clock[0] = 400
    quiet = activity.snapshot("rd", "gen")
    assert quiet["status"] == "quiet" and quiet["attention"] == "nothing_required"
    assert activity.snapshot("rd", "gen", waiting=waiting_for_agent(question))["attention"] == "waiting_on_agent"
    # An unknown reason is ignored rather than trusted.
    assert activity.snapshot("rd", "gen", waiting={"reason": "anything"})["waiting_for"] is None


def test_progress_route_reports_the_idle_wait_for_the_next_experiment(tmp_path, monkeypatch):
    rd, store, client = _run(tmp_path, monkeypatch)
    generation = run_generation_token(store.read_all())
    import looplab.engine.run_lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "engine_liveness", lambda rd: True)
    body = _read(client, generation).json()
    assert body["next_step"]["code"] == "choose_direction"
    assert body["agent_activity"]["waiting_for"] == {"reason": "next_experiment", "count": 1}
    assert body["agent_activity"]["attention"] == "waiting_on_agent"
    body = _read(client, generation, AGENT, brief=True).json()
    assert body["agent_activity"]["attention"] == "agent_has_work"
