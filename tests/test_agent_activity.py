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
