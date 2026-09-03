"""The legacy `/control` route ANNOUNCES its deprecation and COUNTS who still uses it.

The route has no durable request identity and no mandatory generation fence, so a lost-response
retry re-appends an ADDITIVE intent — `budget_extend`'s `add_nodes` is a documented delta, and
inject/fork/deep_research each queue another PAID unit of work. Requiring `expected_seq` was tried
and reverted: it is the correct end state and it breaks the contract this route exists to preserve.

So the route stays and the deprecation becomes REAL rather than a comment nobody can read:

  * a caller is TOLD (`Deprecation`, `Link; rel="successor-version"`, and a `Warning` naming the
    exact hazard and the fix), which is the one channel a client can actually observe;
  * the port is COUNTED, so "41 unfenced call sites" is a number that can go down instead of a
    sentence in a comment.

There is deliberately no `Sunset`: RFC 8594's field carries a DATE and nobody has committed to one.
Emitting an invented date would be a schedule this project has not agreed to — the same rule that
makes a `DECLINED[…]` marker here carry a number rather than a plausible sentence.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from looplab.events.eventstore import EventStore
from looplab.serve.routers import control as control_router
from looplab.serve.server import make_app


def _run(tmp_path, run_id="demo"):
    rd = tmp_path / run_id
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": run_id, "task_id": "t", "goal": "g", "direction": "min"})
    return rd


@pytest.fixture(autouse=True)
def _clean_tally():
    control_router._legacy_control_callers.clear()
    yield
    control_router._legacy_control_callers.clear()


def _post(client, run_id="demo", body=None, **kw):
    return client.post(f"/api/runs/{run_id}/control",
                       json=body or {"type": "pause", "data": {}}, **kw)


def test_the_response_tells_the_caller_it_is_deprecated(tmp_path):
    """MUTATION: drop the headers -> the deprecation exists only in a source comment, where no
    client can read it, which is exactly the state this closed."""
    _run(tmp_path)
    client = TestClient(make_app(tmp_path))
    r = _post(client)
    assert r.status_code == 200, r.text
    assert r.headers.get("Deprecation") == "true"
    assert 'rel="successor-version"' in r.headers.get("Link", "")
    assert "/api/runs/demo/commands" in r.headers.get("Link", "")


def test_the_warning_names_the_hazard_and_the_fix(tmp_path):
    """A deprecation notice a caller cannot act on is a worse comment. The hazard here is specific
    — a retried request re-appends rather than resolving to the same record — and so is the fix."""
    _run(tmp_path)
    client = TestClient(make_app(tmp_path))
    warning = _post(client).headers.get("Warning", "")
    assert warning.startswith("299 "), "an RFC 9111 miscellaneous persistent warning"
    assert "Idempotency-Key" in warning
    assert "/commands" in warning


def test_there_is_no_invented_sunset_date(tmp_path):
    """RFC 8594's `Sunset` carries a DATE. Nobody has committed to one, and a plausible date is a
    schedule this project has not agreed to."""
    _run(tmp_path)
    client = TestClient(make_app(tmp_path))
    assert "Sunset" not in _post(client).headers


def test_the_route_still_works_exactly_as_before(tmp_path):
    """The whole reason the fence was reverted. Announcing a deprecation must not become the silent
    409 that was already rejected."""
    rd = _run(tmp_path)
    client = TestClient(make_app(tmp_path))
    r = _post(client)
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["type"] == "pause"
    assert [e.type for e in EventStore(rd / "events.jsonl").read_all()][-1] == "pause"


def test_a_successful_append_is_counted_by_type_and_agent(tmp_path):
    """The port needs a number. MUTATION: drop the tally -> "41 unfenced call sites" stays a
    sentence in a comment and nobody can tell whether anything outside the suite still calls it."""
    _run(tmp_path)
    client = TestClient(make_app(tmp_path))
    _post(client, headers={"User-Agent": "looplab-tui/1"})
    _post(client, headers={"User-Agent": "looplab-tui/1"})
    _post(client, body={"type": "resume", "data": {}}, headers={"User-Agent": "curl/8"})

    tally = control_router.legacy_control_callers()
    assert tally["pause"]["looplab-tui/1"] == 2
    assert tally["resume"]["curl/8"] == 1


def test_a_REFUSED_call_is_not_counted(tmp_path):
    """A 400/409 is a caller the route refused, not a migration blocker. Counting it would inflate
    the number the port is tracked against with requests that never appended anything."""
    _run(tmp_path)
    client = TestClient(make_app(tmp_path))
    refused = _post(client, body={"type": "not_a_control_event", "data": {}})
    assert refused.status_code == 400
    assert control_router.legacy_control_callers() == {}


def test_the_agent_map_is_bounded(tmp_path):
    """Untrusted input on a hot path. A caller varying its User-Agent per request must not grow the
    map without bound — the overflow bucket keeps the COUNT honest and drops the distinction."""
    for i in range(control_router._LEGACY_CONTROL_MAX_AGENTS + 5):
        control_router._note_legacy_control_caller("pause", f"agent-{i}")

    agents = control_router.legacy_control_callers()["pause"]
    assert len(agents) == control_router._LEGACY_CONTROL_MAX_AGENTS + 1  # + the overflow bucket
    assert agents["(other)"] == 5
    assert sum(agents.values()) == control_router._LEGACY_CONTROL_MAX_AGENTS + 5, (
        "the total must survive the bucketing")


def test_a_missing_user_agent_is_named_not_dropped(tmp_path):
    """"unknown" and "nobody called" are different facts."""
    control_router._note_legacy_control_caller("pause", "")
    assert control_router.legacy_control_callers()["pause"]["unknown"] == 1


def test_the_fenced_commands_route_carries_no_deprecation(tmp_path):
    """It is the SUCCESSOR. A deprecation header on it would send a migrating client nowhere."""
    _run(tmp_path)
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/runs/demo/commands",
                    headers={"Idempotency-Key": "k" * 16},
                    json={"type": "pause", "data": {}})
    assert "Deprecation" not in r.headers
