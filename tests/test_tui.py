"""The terminal control plane (`looplab tui`). The TUI is a thin HTTP client of the UI server, so the
pure rendering/gating helpers are unit-tested directly, and the client (Api) is exercised against a
real server via FastAPI's TestClient (no sockets) so the request/response contracts stay in lockstep
with the React `util.js` they mirror.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from looplab.serve import tui


GENERATION = "a" * 64

# ----------------------------------------------------------------------------- pure helpers

def test_fmt_metric():
    assert tui.fmt_metric(None) == "—"
    assert tui.fmt_metric(float("nan")) == "—"
    assert tui.fmt_metric(1.34289) == "1.343"
    assert tui.fmt_metric(0) == "0"
    assert tui.fmt_metric("pending") == "pending"
    # very small / very large -> exponential, like util.js fmt
    assert "e" in tui.fmt_metric(1e-9)
    assert "e" in tui.fmt_metric(5e8)


def test_fmt_ago():
    now = 1_000_000.0
    assert tui.fmt_ago(None) == "—"
    assert tui.fmt_ago(now - 5, now) == "just now"
    assert tui.fmt_ago(now - 120, now) == "2m ago"
    assert tui.fmt_ago(now - 7200, now) == "2h ago"
    assert tui.fmt_ago(now - 2 * 86400, now) == "2d ago"


def test_phase_meta_running_vs_stalled():
    # a searching run with a live engine reads as bright "running"
    g, colour, label = tui.phase_meta({"phase": "search", "engine_running": True})
    assert label == "running" and colour == "green"
    # not finished + no engine == stalled/zombie (the bug the web UI also surfaces)
    g, colour, label = tui.phase_meta({"phase": "search", "engine_running": False})
    assert "stalled" in label and colour == "red"
    # finished is finished regardless of engine flag
    _, colour, label = tui.phase_meta({"phase": "finished", "engine_running": False})
    assert label == "finished" and colour == "green"
    # a bare summary with only `finished` set still resolves
    _, _, label = tui.phase_meta({"finished": True})
    assert label == "finished"

    glyph, colour, label = tui.phase_meta({"phase": "finalizing", "engine_running": True})
    assert glyph == "◐" and colour == "yellow" and label == "finalizing"
    _, colour, label = tui.phase_meta({"phase": "finalizing", "engine_running": False})
    assert colour == "yellow" and label == "finalizing · engine stopped"


def test_sort_runs_recent_first():
    runs = [{"run_id": "a", "mtime": 1}, {"run_id": "b", "mtime": 3}, {"run_id": "c", "mtime": 2}]
    assert [r["run_id"] for r in tui.sort_runs(runs)] == ["b", "c", "a"]


def test_slug():
    assert tui.slug("My Run!") == "my-run"
    assert tui.slug("  spaces  and---dashes ") == "spaces-and-dashes"
    assert tui.slug("x" * 80) == "x" * 40


def test_spec_lines_catalogue_and_inline():
    lines = tui.spec_lines({"run_id": "demo", "task_file": "examples/toy_task.json",
                            "settings": {"max_nodes": 14, "policy": "greedy"}, "rationale": "toy smoke"})
    blob = "\n".join(lines)
    assert "demo" in blob and "toy_task.json" in blob and "catalogue" in blob
    assert "max_nodes=14" in blob and "policy=greedy" in blob and "toy smoke" in blob
    # inline mlebench_real shows the competition; setup steps are numbered
    lines = tui.spec_lines({"run_id": "k", "task": {"kind": "mlebench_real", "competition": "titanic"},
                            "settings": {}, "setup_steps": ["download data", "set the metric"]})
    blob = "\n".join(lines)
    assert "mlebench_real · titanic" in blob and "step 1." in blob and "step 2." in blob

    # a COMPOSABLE (kind-less) genesis task must still show goal / repo, not just run-name + settings
    lines = tui.spec_lines({"run_id": "c", "settings": {},
                            "task": {"goal": "beat the baseline", "direction": "max",
                                     "editable_path": "/repo/x"}})
    blob = "\n".join(lines)
    assert "beat the baseline" in blob and "/repo/x" in blob and "max" in blob


def test_spec_ready_gates():
    assert tui.spec_ready(None) is not None                 # no plan
    assert tui.spec_ready({"task": {"kind": "quadratic"}}) is not None    # missing name
    assert tui.spec_ready({"run_id": "x", "task": {}}) is not None        # no task kind
    # mlebench_real needs a competition
    assert tui.spec_ready({"run_id": "x", "task": {"kind": "mlebench_real"}}) is not None
    assert tui.spec_ready({"run_id": "x", "task": {"kind": "mlebench_real", "competition": "titanic"}}) is None
    # repo task needs a path AND (eval command or onboard)
    assert tui.spec_ready({"run_id": "x", "task": {"kind": "repo", "editable_path": "/r"}}) is not None
    assert tui.spec_ready({"run_id": "x", "task": {"kind": "repo", "editable_path": "/r", "onboard": True}}) is None
    assert tui.spec_ready({"run_id": "x", "task": {"kind": "repo", "editable_path": "/r",
                                                   "eval": {"command": ["python", "t.py"]}}}) is None
    # a catalogue task_file is always launchable once named
    assert tui.spec_ready({"run_id": "x", "task_file": "examples/toy_task.json"}) is None


def test_is_critical():
    assert tui.is_critical({"type": "run_abort"}) is True
    assert tui.is_critical({"type": "node_abort"}) is True
    assert tui.is_critical({"type": "node_reset"}) is True
    assert tui.is_critical({"type": "hint"}) is False
    assert tui.is_critical({}) is False


def test_parse_pick():
    # apply-all forms
    assert tui.parse_pick("", 3) == [0, 1, 2]
    assert tui.parse_pick("y", 3) == [0, 1, 2]
    assert tui.parse_pick("all", 2) == [0, 1]
    # cancel forms
    assert tui.parse_pick("n", 3) == []
    assert tui.parse_pick("cancel", 3) == []
    # explicit picks: 1-based in, 0-based out, deduped + ordered, out-of-range dropped
    assert tui.parse_pick("1,3", 3) == [0, 2]
    assert tui.parse_pick("3 1 3", 3) == [2, 0]
    assert tui.parse_pick("2", 3) == [1]
    assert tui.parse_pick("9", 3) == []          # out of range -> nothing
    # unrecognised -> None (caller re-asks)
    assert tui.parse_pick("huh?", 3) is None


def test_history_for_boss_cleans_turns():
    """Stored turns -> {role,content} the boss endpoints want: actions collapse to 'applied: …',
    summaries become recaps, contentless/unknown turns are dropped (no 'action: None' noise)."""
    hist = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hey"},
        {"role": "action", "action": {"type": "hint", "label": "hint: try MLPs"}, "status": "done"},
        {"role": "action", "action": {"type": "budget_extend"}},          # no label -> falls back to type
        {"role": "action", "action": {"type": "run_abort", "label": "finalize"}, "status": "pending"},
        {"role": "summary", "content": "we agreed to try neural nets"},
        {"role": "user", "content": ""},                                  # empty -> dropped
        {"role": "system", "content": "ignore"},                          # unknown role -> dropped
    ]
    out = tui.history_for_boss(hist)
    assert out == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hey"},
        {"role": "assistant", "content": "applied: hint: try MLPs"},
        {"role": "assistant", "content": "applied: budget_extend"},
        {"role": "assistant", "content": "requested (pending): finalize"},
        {"role": "assistant", "content": "Earlier recap: we agreed to try neural nets"},
    ]


def test_signatures_detect_change():
    # dashboard signature changes when a drawn field changes (nodes advanced), not on untouched data
    a = [{"run_id": "r", "phase": "search", "nodes": 3, "engine_running": True, "mtime": 1}]
    b = [{"run_id": "r", "phase": "search", "nodes": 4, "engine_running": True, "mtime": 2}]
    assert tui.dashboard_sig(a) == tui.dashboard_sig(list(a))
    assert tui.dashboard_sig(a) != tui.dashboard_sig(b)
    # run signature changes when an in-flight node appears or the phase flips
    s1 = {"phase": "search", "nodes": {"0": {"status": "done", "metric": 1.0}}}
    s2 = {"phase": "search", "nodes": {"0": {"status": "done", "metric": 1.0},
                                       "1": {"status": "pending"}}}
    assert tui.run_sig(s1) != tui.run_sig(s2)
    assert tui.run_sig(s1) == tui.run_sig(dict(s1))


# ----------------------------------------------------------------------------- Api job polling

def test_live_prompt_refreshes_then_reads(monkeypatch):
    """The live loop must redraw when the data signature changes while waiting, and still return the
    typed line. Drive it with a real OS pipe as stdin so select() behaves like a terminal."""
    import os
    import sys

    if not hasattr(__import__("select"), "select"):
        pytest.skip("no select()")

    api = tui.Api("http://x")
    app = tui.Tui.__new__(tui.Tui)                          # bypass __init__ (no server needed)
    app.api = api
    from rich.console import Console

    r_fd, w_fd = os.pipe()
    stdin = os.fdopen(r_fd, "r")
    devnull = open(os.devnull, "w")
    app.console = Console(file=devnull)                     # swallow drawing output
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(app, "_interactive", lambda: True)

    ticks = {"n": 0}
    renders = {"n": 0}

    def fetch():
        ticks["n"] += 1
        return ticks["n"]                                   # data changes every poll -> every poll redraws

    def render(_data):
        renders["n"] += 1
        # Deterministic (no wall-clock race): the initial draw is render #1; the first live refresh in
        # the wait loop is #2 — feed the input right then, so "at least one live refresh" is guaranteed
        # regardless of scheduling/load, and the very next select() returns the typed line.
        if renders["n"] == 2:
            os.write(w_fd, b"hello\n")

    try:
        line, data = app._live_prompt("» ", fetch=fetch, render=render, sig=lambda d: d, interval=0.02)
        assert line == "hello"
        assert renders["n"] >= 2                            # initial draw + at least one live refresh
    finally:
        stdin.close()                                      # closes r_fd
        devnull.close()
        try:
            os.close(w_fd)                                 # idempotent guard if the timer didn't fire
        except OSError:
            pass


def test_await_job_fast_path():
    """A result that is already the final dict (no job_id) is returned unchanged — no polling."""
    api = tui.Api("http://x")
    out = api._await_job({"ok": True, "reply": "hi"}, lambda j: f"/p/{j}", interval=0.01, deadline_s=1)
    assert out == {"ok": True, "reply": "hi"}


def test_await_job_polls_to_done(monkeypatch):
    """A {status:'running', job_id} is polled until the server reports done."""
    api = tui.Api("http://x")
    seq = [{"status": "running"}, {"status": "running"}, {"status": "done", "ok": True, "reply": "done!"}]
    calls = {"n": 0}

    def fake_get(path, timeout=None):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return seq[i]

    monkeypatch.setattr(api, "get", fake_get)
    out = api._await_job({"status": "running", "job_id": "abc"}, lambda j: f"/api/jobs/{j}",
                         interval=0.001, deadline_s=5)
    assert out["ok"] is True and out["reply"] == "done!"
    assert calls["n"] >= 3


def test_await_job_unknown_is_expired(monkeypatch):
    api = tui.Api("http://x")
    monkeypatch.setattr(api, "get", lambda path, timeout=None: {"status": "unknown"})
    out = api._await_job({"status": "running", "job_id": "abc"}, lambda j: f"/api/jobs/{j}",
                         interval=0.001, deadline_s=5)
    assert out["ok"] is False and "expired" in out["error"]


def test_await_job_bails_when_server_lost(monkeypatch):
    """A transport failure (status None = can't reach the server) should bail fast after a few misses,
    not spin out the whole deadline."""
    api = tui.Api("http://x")

    def dead(path, timeout=None):
        raise tui.ApiError("connection refused", status=None)

    monkeypatch.setattr(api, "get", dead)
    out = api._await_job({"status": "running", "job_id": "abc"}, lambda j: f"/api/jobs/{j}",
                         interval=0.001, deadline_s=60)        # huge deadline; must NOT spin it out
    assert out["ok"] is False and "lost contact" in out["error"]


def test_await_job_tolerates_5xx_then_done(monkeypatch):
    """A 5xx (status set) is transient — keep polling — and a later 'done' still wins."""
    api = tui.Api("http://x")
    seq = [tui.ApiError("boom", status=503), tui.ApiError("boom", status=503),
           {"status": "done", "ok": True, "reply": "ok"}]
    calls = {"n": 0}

    def get(path, timeout=None):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        v = seq[i]
        if isinstance(v, tui.ApiError):
            raise v
        return v

    monkeypatch.setattr(api, "get", get)
    out = api._await_job({"status": "running", "job_id": "abc"}, lambda j: f"/api/jobs/{j}",
                         interval=0.001, deadline_s=5)
    assert out["ok"] is True and out["reply"] == "ok"


# ----------------------------------------------------------------------------- authoritative run commands

def test_run_generation_reads_exact_lowercase_state_token(monkeypatch):
    api = tui.Api("http://x")
    calls = []
    monkeypatch.setattr(
        api, "_request",
        lambda method, path, **_kwargs: calls.append((method, path)) or {
            "state": {}, "generation": GENERATION})

    assert api.run_generation("run/with space?") == GENERATION
    assert calls == [("GET", "/api/runs/run%2Fwith%20space%3F/state")]


@pytest.mark.parametrize("generation", [None, "A" * 64, "a" * 63, 123])
def test_run_generation_and_submission_fail_closed_on_noncanonical_token(
        monkeypatch, generation):
    api = tui.Api("http://x")
    requests = []
    monkeypatch.setattr(
        api, "_request",
        lambda method, path, **_kwargs: requests.append((method, path)) or {
            "state": {}, "generation": generation})

    with pytest.raises(tui.ApiError, match="generation"):
        api.run_generation("demo")
    assert requests == [("GET", "/api/runs/demo/state")]
    requests.clear()
    with pytest.raises(tui.ApiError, match="generation"):
        api.run_command("demo", "resume", {}, expected_generation=generation)
    assert requests == []

def test_run_command_posts_idempotently_and_polls_terminal(monkeypatch):
    """One logical TUI command is one POST with an idempotency key; status refreshes are GET-only."""
    import uuid

    api = tui.Api("http://x")
    calls = []

    def request(method, path, body=None, timeout=None, headers=None):
        calls.append((method, path, body, headers or {}))
        if method == "POST":
            return {"id": "cmd-1", "status": "executing", "event_type": "resume"}
        return {"id": "cmd-1", "status": "succeeded", "event_type": "resume"}

    monkeypatch.setattr(api, "_request", request)
    monkeypatch.setattr("looplab.serve.tui_api.time.sleep", lambda _s: None)
    out = api.run_command(
        "demo", "resume", {}, wait_s=1, expected_generation=GENERATION)
    assert out["status"] == "succeeded"
    assert calls[0][0:3] == (
        "POST", "/api/runs/demo/commands",
        {"type": "resume", "data": {}, "expected_generation": GENERATION})
    assert uuid.UUID(calls[0][3]["Idempotency-Key"])
    assert calls[1][0:2] == ("GET", "/api/runs/demo/commands/cmd-1")
    assert "Idempotency-Key" not in calls[1][3]


def test_run_command_honors_caller_supplied_idempotency_key(monkeypatch):
    """A staged TUI identity must reach the server unchanged instead of being replaced in Api."""
    api = tui.Api("http://x")
    calls = []

    def request(method, path, body=None, timeout=None, headers=None):
        calls.append((method, path, body, headers or {}))
        return {"id": "cmd-staged", "status": "succeeded", "event_type": "resume"}

    monkeypatch.setattr(api, "_request", request)
    out = api.run_command(
        "demo", "resume", {}, idempotency_key="durable-tui-key",
        expected_generation=GENERATION)

    assert out["status"] == "succeeded"
    assert len(calls) == 1
    assert calls[0][3]["Idempotency-Key"] == "durable-tui-key"


def test_run_command_retry_existing_conflict_observes_without_reposting(monkeypatch):
    """A same-intent 409 names the durable command to GET; it must never trigger another POST."""
    api = tui.Api("http://x")
    command_id = "cmd_" + "a" * 32
    calls = []

    def request(method, path, body=None, timeout=None, headers=None):
        calls.append((method, path, body, headers or {}))
        if method == "POST":
            raise tui.ApiError({
                "code": "retry_existing_command",
                "existing_command_id": command_id,
                "message": "observe the already-durable command",
            }, status=409)
        return {"id": command_id, "status": "succeeded", "event_type": "resume"}

    monkeypatch.setattr(api, "_request", request)
    out = api.run_command(
        "demo", "resume", {}, idempotency_key="staged-key",
        expected_generation=GENERATION)

    assert out == {"id": command_id, "status": "succeeded", "event_type": "resume"}
    assert [(method, path) for method, path, _body, _headers in calls] == [
        ("POST", "/api/runs/demo/commands"),
        ("GET", f"/api/runs/demo/commands/{command_id}"),
    ]
    assert calls[0][3]["Idempotency-Key"] == "staged-key"


@pytest.mark.parametrize("status", [None, 408, 425, 429, 503])
def test_run_command_retries_transport_with_same_idempotency_key(monkeypatch, status):
    """A dropped response may hide a committed command; replay the logical request, not the action."""
    api = tui.Api("http://x")
    calls = []

    def request(method, path, body=None, timeout=None, headers=None):
        calls.append((method, path, body, headers or {}))
        if len(calls) == 1:
            raise tui.ApiError("connection reset", status=status)
        return {"id": "cmd-recovered", "status": "succeeded", "event_type": "resume"}

    monkeypatch.setattr(api, "_request", request)
    monkeypatch.setattr("looplab.serve.tui_api.time.sleep", lambda _s: None)
    out = api.run_command("demo", "resume", {}, expected_generation=GENERATION)
    assert out["status"] == "succeeded"
    assert len(calls) == 2
    assert calls[0][3]["Idempotency-Key"] == calls[1][3]["Idempotency-Key"]


def test_run_command_does_not_retry_authoritative_4xx(monkeypatch):
    api = tui.Api("http://x")
    calls = []

    def request(method, path, body=None, timeout=None, headers=None):
        calls.append((method, path, body, headers or {}))
        raise tui.ApiError("bad command", status=409)

    monkeypatch.setattr(api, "_request", request)
    with pytest.raises(tui.ApiError, match="bad command"):
        api.run_command("demo", "resume", {}, expected_generation=GENERATION)
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403, 404])
def test_run_command_poll_surfaces_authoritative_error(monkeypatch, status):
    """Auth/not-found polling failures must not be rewritten as an executing command."""
    api = tui.Api("http://x")

    def request(method, path, body=None, timeout=None, headers=None):
        if method == "POST":
            return {"id": "cmd-1", "status": "executing", "event_type": "resume"}
        raise tui.ApiError("authoritative poll failure", status=status)

    monkeypatch.setattr(api, "_request", request)
    monkeypatch.setattr("looplab.serve.tui_api.time.sleep", lambda _s: None)
    with pytest.raises(tui.ApiError, match="authoritative poll failure") as exc:
        api.run_command(
            "demo", "resume", {}, wait_s=1, expected_generation=GENERATION)
    assert exc.value.status == status


def test_run_command_url_encodes_run_and_command_ids(monkeypatch):
    api = tui.Api("http://x")
    calls = []

    def request(method, path, body=None, timeout=None, headers=None):
        calls.append((method, path))
        if method == "POST":
            return {"id": "cmd/with space?", "status": "executing", "event_type": "resume"}
        return {"id": "cmd/with space?", "status": "succeeded", "event_type": "resume"}

    monkeypatch.setattr(api, "_request", request)
    monkeypatch.setattr("looplab.serve.tui_api.time.sleep", lambda _s: None)
    assert api.run_command(
        "run/with space?", "resume", {}, wait_s=1,
        expected_generation=GENERATION)["status"] == "succeeded"
    assert calls == [
        ("POST", "/api/runs/run%2Fwith%20space%3F/commands"),
        ("GET", "/api/runs/run%2Fwith%20space%3F/commands/cmd%2Fwith%20space%3F"),
    ]


@pytest.mark.parametrize("status", [None, 408, 425, 429, 503])
def test_run_command_poll_tolerates_transient_failure(monkeypatch, status):
    api = tui.Api("http://x")
    polls = {"n": 0}

    def request(method, path, body=None, timeout=None, headers=None):
        if method == "POST":
            return {"id": "cmd-1", "status": "executing", "event_type": "resume"}
        polls["n"] += 1
        if polls["n"] == 1:
            raise tui.ApiError("temporary poll failure", status=status)
        return {"id": "cmd-1", "status": "succeeded", "event_type": "resume"}

    monkeypatch.setattr(api, "_request", request)
    monkeypatch.setattr("looplab.serve.tui_api.time.sleep", lambda _s: None)
    assert api.run_command(
        "demo", "resume", {}, wait_s=1,
        expected_generation=GENERATION)["status"] == "succeeded"
    assert polls["n"] == 2


def test_run_command_wait_expiry_is_executing_not_success(monkeypatch):
    api = tui.Api("http://x")
    monkeypatch.setattr(api, "_request", lambda *a, **k: {
        "id": "cmd-slow", "status": "accepted", "event_type": "run_abort"})
    out = api.run_command(
        "demo", "run_abort", {"reason": "finalized"}, wait_s=0,
        expected_generation=GENERATION)
    assert out == {"id": "cmd-slow", "status": "executing", "event_type": "run_abort"}


@pytest.mark.parametrize("record", [
    {"status": "succeeded", "event_type": "resume"},
    {"id": "cmd-1", "status": "surprise", "event_type": "resume"},
])
def test_run_command_rejects_malformed_envelope_instead_of_false_success(monkeypatch, record):
    api = tui.Api("http://x")
    monkeypatch.setattr(api, "_request", lambda *a, **k: record)
    with pytest.raises(tui.ApiError):
        api.run_command(
            "demo", "resume", {}, wait_s=0, expected_generation=GENERATION)


def test_run_command_rejects_poll_record_for_different_command(monkeypatch):
    api = tui.Api("http://x")

    def request(method, path, body=None, timeout=None, headers=None):
        if method == "POST":
            return {"id": "cmd-1", "status": "executing", "event_type": "resume"}
        return {"id": "cmd-2", "status": "succeeded", "event_type": "resume"}

    monkeypatch.setattr(api, "_request", request)
    monkeypatch.setattr("looplab.serve.tui_api.time.sleep", lambda _s: None)
    with pytest.raises(tui.ApiError, match="does not match"):
        api.run_command(
            "demo", "resume", {}, wait_s=1, expected_generation=GENERATION)


def _command_tui(fake_api):
    """Small no-server TUI harness for plan/control rendering tests."""
    import io
    from rich.console import Console

    app = tui.Tui.__new__(tui.Tui)
    if not callable(getattr(fake_api, "run_generation", None)):
        fake_api.run_generation = lambda _run_id: GENERATION
    app.api = fake_api
    app.console = Console(file=io.StringIO(), force_terminal=False, color_system=None)
    app._persist = lambda *_a, **_k: None
    return app


def test_staged_command_deep_snapshots_nested_intent_and_detects_later_mutation():
    key = "4e7e14ec-1959-4d67-bb55-643a2354c808"
    data = {"strategy": {"policy": "asha", "params": {"grace": 2}}}
    command = tui._staged_command("set_strategy", data, key, GENERATION)
    turn = {"action": {"type": "set_strategy", "data": data}, "command": command}

    assert tui._staged_replay(turn) == (
        key, "set_strategy", command["intent"]["data"], GENERATION)
    data["strategy"]["params"]["grace"] = 99
    assert command["intent"]["data"]["strategy"]["params"]["grace"] == 2
    assert tui._staged_replay(turn) is None


def test_apply_plan_durably_stages_deterministic_command_before_submit(monkeypatch):
    import copy
    import hashlib
    import uuid

    key = "4b5f7b14-4d5b-4fc8-9c2b-a1122cd231f7"
    expected_id = "cmd_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    timeline = []

    class FakeApi:
        def run_generation(self, run_id):
            timeline.append(("generation", run_id))
            return GENERATION

        def run_command(self, run_id, event_type, data, wait_s=8.0, **kwargs):
            timeline.append(("submit", run_id, event_type, copy.deepcopy(data), dict(kwargs)))
            return {"id": expected_id, "status": "succeeded", "event_type": event_type}

    app = _command_tui(FakeApi())
    app._persist = lambda run_id, row: timeline.append(
        ("persist", run_id, copy.deepcopy(row))) or True
    monkeypatch.setattr(tui.uuid, "uuid4", lambda: uuid.UUID(key))

    history = []
    app._apply_plan("demo", history, [{
        "type": "budget_extend", "data": {"add_nodes": 2}, "label": "extend",
    }])

    assert timeline[0] == ("generation", "demo")
    assert timeline[1] == ("persist", "demo", {
        "role": "action",
        "action": {"type": "budget_extend", "data": {"add_nodes": 2}, "label": "extend"},
        "status": "pending",
        "command": {
            "id": expected_id, "status": "accepted", "event_type": "budget_extend",
            "idempotency_key": key,
            "expected_generation": GENERATION,
            "intent": {"type": "budget_extend", "data": {"add_nodes": 2}},
            "submit_unconfirmed": True,
        },
    })
    assert timeline[2] == (
        "submit", "demo", "budget_extend", {"add_nodes": 2},
        {"idempotency_key": key, "expected_generation": GENERATION})
    assert timeline[3][0] == "persist" and timeline[3][2]["role"] == "command_status"


def test_control_durably_stages_deterministic_command_before_submit(monkeypatch):
    import copy
    import hashlib
    import uuid

    key = "839090cc-2134-43d8-9217-f94ff9195f93"
    expected_id = "cmd_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    timeline = []

    class FakeApi:
        def run_generation(self, run_id):
            timeline.append(("generation", run_id))
            return GENERATION

        def run_command(self, run_id, event_type, data, wait_s=8.0, **kwargs):
            timeline.append(("submit", run_id, event_type, copy.deepcopy(data), dict(kwargs)))
            return {"id": expected_id, "status": "executing", "event_type": event_type}

    app = _command_tui(FakeApi())
    app._persist = lambda run_id, row: timeline.append(
        ("persist", run_id, copy.deepcopy(row))) or True
    monkeypatch.setattr(tui.uuid, "uuid4", lambda: uuid.UUID(key))

    history = []
    result = app._control("demo", "resume", {}, history=history, label="resume")

    assert result["status"] == "executing"
    assert timeline[0] == ("generation", "demo")
    assert timeline[1] == ("persist", "demo", {
        "role": "action",
        "action": {"type": "resume", "data": {}, "label": "resume"},
        "status": "pending",
        "command": {
            "id": expected_id, "status": "accepted", "event_type": "resume",
            "idempotency_key": key,
            "expected_generation": GENERATION,
            "intent": {"type": "resume", "data": {}},
            "submit_unconfirmed": True,
        },
    })
    assert timeline[2] == (
        "submit", "demo", "resume", {},
        {"idempotency_key": key, "expected_generation": GENERATION})
    assert timeline[3][0] == "persist" and timeline[3][2]["role"] == "command_status"


def test_apply_plan_does_not_submit_when_durable_staging_fails():
    class FakeApi:
        def run_command(self, *_args, **_kwargs):
            raise AssertionError("run_command must not run without the durable pending row")

    app = _command_tui(FakeApi())
    app._persist = lambda *_args, **_kwargs: False
    history = []

    app._apply_plan("demo", history, [{"type": "resume", "data": {}, "label": "resume"}])

    assert len(history) == 1
    assert history[0]["status"] == "failed"
    assert "nothing was submitted" in history[0]["error"]


def test_control_does_not_submit_when_durable_staging_fails():
    class FakeApi:
        def run_command(self, *_args, **_kwargs):
            raise AssertionError("run_command must not run without the durable pending row")

    app = _command_tui(FakeApi())
    app._persist = lambda *_args, **_kwargs: False
    history = []

    result = app._control("demo", "resume", {}, history=history)

    assert result["status"] == "failed"
    assert len(history) == 1 and history[0]["status"] == "failed"
    assert "nothing was submitted" in result["error"]


def test_new_tui_reconciles_staged_id_after_response_is_lost(monkeypatch):
    """The durable pre-POST row is enough for a fresh process to observe the accepted command."""
    import copy
    import hashlib
    import uuid

    key = "6f587f6c-e7c4-4011-a915-a2162ed02f4b"
    expected_id = "cmd_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    class FakeApi:
        def __init__(self):
            self.rows = []
            self.record = None
            self.polls = []

        def post(self, _path, body=None, timeout=None, **_kwargs):
            self.rows.append(copy.deepcopy(body))
            return {"ok": True}

        def get(self, _path, timeout=None, **_kwargs):
            return copy.deepcopy(self.rows)

        def run_command(self, run_id, event_type, data, wait_s=8.0, **kwargs):
            assert kwargs["idempotency_key"] == key
            assert kwargs["expected_generation"] == GENERATION
            self.record = {"id": expected_id, "status": "succeeded", "event_type": event_type}
            raise SystemExit("client process died after the server accepted the POST")

        def get_run_command(self, run_id, command_id, **_kwargs):
            self.polls.append((run_id, command_id))
            return copy.deepcopy(self.record)

    api = FakeApi()
    first = _command_tui(api)
    first._persist = lambda run_id, row: bool(
        api.post(f"/api/runs/{run_id}/chat-log", row))
    monkeypatch.setattr(tui.uuid, "uuid4", lambda: uuid.UUID(key))

    with pytest.raises(SystemExit, match="client process died"):
        first._apply_plan(
            "demo", [], [{"type": "resume", "data": {}, "label": "resume"}])

    assert len(api.rows) == 1
    assert api.rows[0]["status"] == "pending"
    assert api.rows[0]["command"]["id"] == expected_id

    restarted = _command_tui(api)
    restarted._persist = lambda run_id, row: bool(
        api.post(f"/api/runs/{run_id}/chat-log", row))
    reloaded = restarted._load_chat("demo")
    assert restarted._reconcile_pending("demo", reloaded) is True
    assert api.polls == [("demo", expected_id)]
    assert reloaded[0]["status"] == "done"
    assert reloaded[0]["command"] == api.record


def test_uncertain_staged_404_resubmits_same_key_and_dedupes_delayed_first_arrival():
    """An early GET 404 cannot prove a timed-out POST will never reach the command service."""
    import hashlib

    key = "58dd3ba7-c7f8-450e-99e0-abdb1e3f43cf"
    expected_id = "cmd_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    class FakeApi:
        def __init__(self):
            self.record = None
            self.arrivals = []
            self.events = 0
            self.generation_reads = 0

        def run_generation(self, _run_id):
            self.generation_reads += 1
            return "b" * 64

        def get_run_command(self, run_id, command_id):
            assert (run_id, command_id) == ("demo", expected_id)
            raise tui.ApiError("not materialized yet", status=404)

        def _arrive(self, arrival_key, event_type):
            self.arrivals.append(arrival_key)
            if self.record is None:
                self.events += 1
                self.record = {
                    "id": expected_id, "status": "succeeded", "event_type": event_type,
                }
            return self.record

        def run_command(self, run_id, event_type, data, wait_s=8.0, **kwargs):
            assert (run_id, event_type, data) == ("demo", "budget_extend", {"add_nodes": 2})
            assert wait_s == 2.0 and kwargs["idempotency_key"] == key
            assert kwargs["expected_generation"] == GENERATION
            # The timed-out original reaches the backend immediately before recovery's same-key POST.
            self._arrive(key, event_type)
            return self._arrive(kwargs["idempotency_key"], event_type)

    api = FakeApi()
    app = _command_tui(api)
    updates = []
    app._persist = lambda _run_id, row: updates.append(row) or True
    history = [{
        "role": "action",
        "action": {"type": "budget_extend", "data": {"add_nodes": 2}, "label": "extend"},
        "status": "pending",
        "command": tui._staged_command(
            "budget_extend", {"add_nodes": 2}, key, GENERATION),
    }]

    assert app._reconcile_pending("demo", history) is True
    assert api.arrivals == [key, key] and api.events == 1
    assert api.generation_reads == 0
    assert history[0]["status"] == "done"
    assert history[0]["command"] == api.record
    assert updates[-1]["status"] == "done"


def test_apply_plan_uses_commands_and_never_legacy_reopen_resume():
    """Regression: [budget_extend, run_abort] must not append run_reopened and cancel finalization."""
    class FakeApi:
        def __init__(self):
            self.commands = []
            self.legacy = []

        def run_command(self, run_id, event_type, data, wait_s=8.0, **_kwargs):
            self.commands.append((run_id, event_type, data))
            return {"id": f"c{len(self.commands)}", "status": "succeeded", "event_type": event_type}

        def post(self, path, body=None, timeout=None):
            self.legacy.append((path, body))
            raise AssertionError("legacy control/resume path must not be used")

    api = FakeApi()
    app = _command_tui(api)
    history = []
    actions = [
        {"type": "budget_extend", "data": {"add_nodes": 3}, "label": "more budget"},
        {"type": "run_abort", "data": {"reason": "finalized"}, "label": "finalize"},
    ]
    app._apply_plan("demo", history, actions)
    assert api.commands == [
        ("demo", "budget_extend", {"add_nodes": 3}),
        ("demo", "run_abort", {"reason": "finalized"}),
    ]
    assert api.legacy == []
    assert all(t["status"] == "done" for t in history)
    assert not any(t["action"].get("type") == "run_reopened" for t in history)


def test_apply_plan_executing_is_pending_not_done():
    class FakeApi:
        def run_command(self, run_id, event_type, data, wait_s=8.0, **_kwargs):
            return {"id": "slow", "status": "executing", "event_type": event_type}

    app = _command_tui(FakeApi())
    history = []
    app._apply_plan("demo", history, [{"type": "run_abort", "data": {}, "label": "finalize"}])
    assert history[0]["status"] == "pending"
    assert history[0]["command"]["status"] == "executing"
    assert tui.history_for_boss(history) == [
        {"role": "assistant", "content": "requested (pending): finalize"}]


def test_apply_plan_stops_at_first_pending_command():
    """Later dependent plan steps are never submitted before the prior postcondition is observed."""
    class FakeApi:
        def __init__(self):
            self.commands = []

        def run_command(self, run_id, event_type, data, wait_s=8.0, **_kwargs):
            self.commands.append(event_type)
            return {"id": "slow", "status": "accepted", "event_type": event_type}

    api = FakeApi()
    app = _command_tui(api)
    history = []
    app._apply_plan("demo", history, [
        {"type": "budget_extend", "data": {"add_nodes": 3}, "label": "more budget"},
        {"type": "run_abort", "data": {"reason": "finalized"}, "label": "finalize"},
    ])
    assert api.commands == ["budget_extend"]
    assert len(history) == 1 and history[0]["status"] == "pending"
    assert "not submitted" in app.console.file.getvalue()


def test_apply_plan_stops_after_terminal_failure():
    class FakeApi:
        def __init__(self):
            self.commands = []

        def run_command(self, run_id, event_type, data, wait_s=8.0, **_kwargs):
            self.commands.append(event_type)
            return {"id": "bad", "status": "rejected", "event_type": event_type,
                    "error": {"message": "postcondition rejected"}}

    api = FakeApi()
    app = _command_tui(api)
    history = []
    app._apply_plan("demo", history, [
        {"type": "budget_extend", "data": {}, "label": "extend"},
        {"type": "run_abort", "data": {}, "label": "finalize"},
    ])
    assert api.commands == ["budget_extend"]
    assert history[0]["status"] == "failed"


def test_apply_plan_finalization_is_terminal_for_later_steps():
    class FakeApi:
        def __init__(self):
            self.commands = []

        def run_command(self, run_id, event_type, data, wait_s=8.0, **_kwargs):
            self.commands.append(event_type)
            return {"id": f"cmd-{len(self.commands)}", "status": "succeeded", "event_type": event_type}

    api = FakeApi()
    app = _command_tui(api)
    history = []
    app._apply_plan("demo", history, [
        {"type": "run_abort", "data": {"reason": "finalized"}, "label": "finalize"},
        {"type": "resume", "data": {}, "label": "resume"},
    ])
    assert api.commands == ["run_abort"]
    assert history[0]["status"] == "done"
    assert "finalized" in app.console.file.getvalue()


@pytest.mark.parametrize("status", [None, 408, 425, 429, 503])
def test_apply_plan_lost_response_keeps_staged_identity_pending(status):
    class FakeApi:
        def run_command(self, *_args, **_kwargs):
            raise tui.ApiError("connection lost after submit", status=status)

    app = _command_tui(FakeApi())
    persisted = []
    app._persist = lambda _run_id, row: persisted.append(dict(row)) or True
    app._apply_plan("demo", [], [
        {"type": "budget_extend", "data": {"add_nodes": 2}, "label": "extend"},
    ])

    assert persisted[0]["status"] == "pending"
    assert persisted[0]["command"]["id"].startswith("cmd_")
    assert "response lost" in app.console.file.getvalue()


@pytest.mark.parametrize("status", [None, 408, 425, 429, 503])
def test_quick_control_lost_response_returns_same_predicted_command_pending(status):
    class FakeApi:
        def run_command(self, *_args, **_kwargs):
            raise tui.ApiError("gateway response lost", status=status)

    app = _command_tui(FakeApi())
    app._persist = lambda *_args: True
    history = []
    result = app._control("demo", "budget_extend", {"add_nodes": 1}, history=history)

    assert result["status"] == "executing"
    assert result["id"] == history[0]["command"]["id"]
    assert history[0]["status"] == "pending"


def test_pending_action_reconciles_and_status_update_survives_reload():
    pending = {
        "role": "action",
        "action": {"type": "resume", "data": {}, "label": "resume"},
        "status": "pending",
        "command": {"id": "cmd-1", "status": "executing", "event_type": "resume"},
    }

    class FakeApi:
        def __init__(self):
            self.rows = [pending]
            self.polls = []

        def get(self, path, timeout=None):
            import copy
            return copy.deepcopy(self.rows)

        def get_run_command(self, run_id, command_id):
            self.polls.append((run_id, command_id))
            return {"id": command_id, "status": "succeeded", "event_type": "resume"}

        def post(self, path, body=None, timeout=None):
            self.rows.append(body)
            return {"ok": True}

    api = FakeApi()
    app = _command_tui(api)
    app._persist = lambda run_id, turn: api.post(f"/api/runs/{run_id}/chat-log", turn)
    history = app._load_chat("demo")
    assert app._reconcile_pending("demo", history) is True
    assert api.polls == [("demo", "cmd-1")]
    assert history[0]["status"] == "done"
    assert tui.history_for_boss(history) == [{"role": "assistant", "content": "applied: resume"}]

    reloaded = app._load_chat("demo")
    assert len(reloaded) == 1
    assert reloaded[0]["status"] == "done"
    assert reloaded[0]["command"]["status"] == "succeeded"


def test_pending_without_command_id_is_failed_once_and_folded_on_reload():
    import copy

    class FakeApi:
        def __init__(self):
            self.rows = [{
                "role": "action", "action": {"type": "resume", "label": "resume"},
                "status": "pending", "command": {"status": "executing", "event_type": "resume"},
            }]

        def get(self, path, timeout=None):
            return copy.deepcopy(self.rows)

        def get_run_command(self, *_a, **_k):
            raise AssertionError("a missing command id cannot be polled")

        def post(self, path, body=None, timeout=None):
            self.rows.append(copy.deepcopy(body))
            return {"ok": True}

    api = FakeApi()
    app = _command_tui(api)
    app._persist = lambda run_id, turn: api.post(f"/api/runs/{run_id}/chat-log", turn)
    history = app._load_chat("demo")
    assert app._reconcile_pending("demo", history) is True
    assert history[0]["status"] == "failed"
    assert api.rows[-1]["action_index"] == 0 and api.rows[-1]["command_id"] is None

    row_count = len(api.rows)
    reloaded = app._load_chat("demo")
    assert len(reloaded) == 1 and reloaded[0]["status"] == "failed"
    assert app._reconcile_pending("demo", reloaded) is True
    assert len(api.rows) == row_count


def test_command_status_index_fallback_never_overwrites_a_different_command():
    class FakeApi:
        def get(self, path, timeout=None):
            return [
                {"role": "action", "action": {"type": "resume"}, "status": "pending",
                 "command": {"id": "cmd-real", "status": "executing"}},
                {"role": "command_status", "command_id": "cmd-other", "action_index": 0,
                 "status": "done", "command": {"id": "cmd-other", "status": "succeeded"}},
            ]

    history = _command_tui(FakeApi())._load_chat("demo")
    assert len(history) == 1 and history[0]["status"] == "pending"
    assert history[0]["command"]["id"] == "cmd-real"


def test_command_status_staged_alias_folds_semantic_reattach_after_reload():
    class FakeApi:
        def get(self, path, timeout=None):
            return [
                {"role": "action", "action": {"type": "resume"}, "status": "pending",
                 "command": {"id": "cmd-staged", "status": "accepted"}},
                {"role": "command_status", "command_id": "cmd-existing", "action_index": 0,
                 "status": "done", "command": {
                     "id": "cmd-existing", "staged_id": "cmd-staged", "status": "succeeded",
                     "event_type": "resume",
                 }},
            ]

    history = _command_tui(FakeApi())._load_chat("demo")
    assert len(history) == 1 and history[0]["status"] == "done"
    assert history[0]["command"]["id"] == "cmd-existing"


def test_unresolved_persisted_command_blocks_next_boss_turn():
    class FakeApi:
        def __init__(self):
            self.boss_calls = 0

        def get_run_command(self, run_id, command_id):
            return {"id": command_id, "status": "executing", "event_type": "resume"}

        def command(self, *args, **kwargs):
            self.boss_calls += 1
            raise AssertionError("boss must not plan over an unresolved command")

    api = FakeApi()
    app = _command_tui(api)
    history = [{
        "role": "action", "action": {"type": "resume", "label": "resume"}, "status": "pending",
        "command": {"id": "cmd-1", "status": "executing", "event_type": "resume"},
    }]
    app._boss_turn("demo", "do something else", history)
    assert api.boss_calls == 0
    assert history[0]["status"] == "pending"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_reconciling_pending_blocks_new_plan_and_control(status):
    class FakeApi:
        def __init__(self):
            self.commands = []

        def get_run_command(self, run_id, command_id):
            raise tui.ApiError("denied", status=status)

        def run_command(self, run_id, event_type, data, wait_s=8.0, **_kwargs):
            self.commands.append(event_type)
            raise AssertionError("new commands must remain blocked")

    api = FakeApi()
    app = _command_tui(api)
    history = [{
        "role": "action", "action": {"type": "run_abort", "label": "finalize"}, "status": "pending",
        "command": {"id": "cmd-finalize", "status": "executing", "event_type": "run_abort"},
    }]
    app._apply_plan("demo", history, [{"type": "resume", "data": {}, "label": "resume"}])
    blocked = app._control("demo", "resume", {}, history=history)
    assert blocked["status"] == "executing"
    assert api.commands == [] and history[0]["status"] == "pending"


def test_reconcile_404_is_terminal_local_failure_not_fake_executing():
    class FakeApi:
        def get_run_command(self, run_id, command_id):
            raise tui.ApiError("missing", status=404)

    app = _command_tui(FakeApi())
    updates = []
    app._persist = lambda run_id, turn: updates.append(turn)
    history = [{
        "role": "action", "action": {"type": "resume", "label": "resume"}, "status": "pending",
        "command": {"id": "cmd-missing", "status": "executing", "event_type": "resume"},
    }]
    assert app._reconcile_pending("demo", history) is True
    assert history[0]["status"] == "failed"
    assert updates[0]["role"] == "command_status" and updates[0]["status"] == "failed"


def test_reconcile_malformed_200_is_terminal_protocol_failure():
    class FakeApi:
        def get_run_command(self, run_id, command_id):
            raise tui.ApiError("command response id does not match", status=200)

    app = _command_tui(FakeApi())
    updates = []
    app._persist = lambda run_id, turn: updates.append(turn)
    history = [{
        "role": "action", "action": {"type": "resume", "label": "resume"}, "status": "pending",
        "command": {"id": "cmd-expected", "status": "executing", "event_type": "resume"},
    }]
    assert app._reconcile_pending("demo", history) is True
    assert history[0]["status"] == "failed"
    assert "invalid command response" in history[0]["error"]
    assert updates[0]["role"] == "command_status" and updates[0]["status"] == "failed"


def test_quick_control_uses_command_and_surfaces_terminal_failure():
    class FakeApi:
        def __init__(self):
            self.calls = []

        def run_command(self, run_id, event_type, data, wait_s=8.0, **_kwargs):
            self.calls.append((run_id, event_type, data))
            return {"id": "bad", "status": "rejected", "event_type": event_type,
                    "error": {"code": "invalid_state", "message": "run is already finished"}}

    api = FakeApi()
    app = _command_tui(api)
    out = app._control("demo", "pause", {})
    assert api.calls == [("demo", "pause", {})]
    assert out["status"] == "rejected"
    assert "already finished" in app.console.file.getvalue()


def test_report_refresh_keeps_dedicated_endpoint():
    class FakeApi:
        def __init__(self):
            self.refreshed = []

        def refresh_report(self, run_id):
            self.refreshed.append(run_id)
            return {"ok": True}

        def run_command(self, *_a, **_k):
            raise AssertionError("report refresh is not a run command")

    api = FakeApi()
    app = _command_tui(api)
    history = []
    app._apply_plan("demo", history, [{"type": "__refresh_report__", "data": {}, "label": "refresh"}])
    assert api.refreshed == ["demo"]
    assert history[0]["status"] == "done"


def test_report_refresh_requires_explicit_success_and_stops_plan():
    class FakeApi:
        def refresh_report(self, run_id):
            return {}

        def run_command(self, *_a, **_k):
            raise AssertionError("later steps must not run after an unconfirmed report refresh")

    app = _command_tui(FakeApi())
    history = []
    app._apply_plan("demo", history, [
        {"type": "__refresh_report__", "data": {}, "label": "refresh"},
        {"type": "resume", "data": {}, "label": "resume"},
    ])
    assert len(history) == 1 and history[0]["status"] == "failed"
    assert "no confirmed success" in history[0]["error"]


def test_live_prompt_falls_back_when_select_unusable(monkeypatch):
    """If select()/readline can't be used (raises TypeError/ValueError/OSError) the loop must degrade to
    a plain blocking read instead of crashing the REPL."""
    import os
    import select as _select
    import sys

    api = tui.Api("http://x")
    app = tui.Tui.__new__(tui.Tui)
    app.api = api
    from rich.console import Console

    r_fd, w_fd = os.pipe()
    os.write(w_fd, b"typed\n")
    os.close(w_fd)
    stdin = os.fdopen(r_fd, "r")
    devnull = open(os.devnull, "w")
    app.console = Console(file=devnull)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(app, "_interactive", lambda: True)
    monkeypatch.setattr(_select, "select", lambda *a, **k: (_ for _ in ()).throw(TypeError("bad fd")))

    try:
        line, _ = app._live_prompt("» ", fetch=lambda: 1, render=lambda d: None, sig=lambda d: d, interval=0.01)
        assert line == "typed"
    finally:
        stdin.close()
        devnull.close()


# ----------------------------------------------------------------------------- Api ↔ real server

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import anyio  # noqa: E402

from looplab.engine.orchestrator import Engine  # noqa: E402
from looplab.search.policy import GreedyTree  # noqa: E402
from looplab.runtime.sandbox import SubprocessSandbox  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402
from looplab.adapters.toytask import ToyTask  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


def _build_run(root: Path, name: str = "demo"):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(root / name, task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4))
    return anyio.run(eng.run)


def _bind(api: tui.Api, client: TestClient) -> None:
    """Route the TUI's stdlib Api through the in-process TestClient (no real sockets) so the client's
    request building + error/detail unwrapping is exercised against the actual server routes."""
    def _request(method, path, body=None, timeout=None, headers=None):
        r = client.request(method, path, json=body, headers=headers)
        if r.status_code >= 400:
            detail = ""
            try:
                detail = (r.json() or {}).get("detail", "")
            except Exception:  # noqa: BLE001
                pass
            raise tui.ApiError(detail or f"{path}: HTTP {r.status_code}", status=r.status_code)
        return r.json() if r.content else None
    api._request = _request  # type: ignore[assignment]


def test_api_ping_and_runs(tmp_path):
    _build_run(tmp_path)
    api = tui.Api("http://test")
    _bind(api, TestClient(make_app(tmp_path)))
    assert api.ping() is True
    runs = tui.sort_runs(api.get("/api/runs"))
    assert runs and runs[0]["run_id"] == "demo" and runs[0]["finished"] is True


def test_api_state_payload(tmp_path):
    _build_run(tmp_path)
    api = tui.Api("http://test")
    _bind(api, TestClient(make_app(tmp_path)))
    state = api.get("/api/runs/demo/state")["state"]
    assert state["finished"] is True and state["nodes"]


def test_api_error_detail_unwrapped(tmp_path):
    """A 4xx from the server surfaces FastAPI's `detail` (human reason), not a bare status — same as
    util.js _throw, so the TUI shows "already exists …" not "409"."""
    _build_run(tmp_path)
    api = tui.Api("http://test")
    _bind(api, TestClient(make_app(tmp_path)))
    with pytest.raises(tui.ApiError) as ei:
        api.get("/api/runs/nope/state")
    assert ei.value.status == 404
    # starting a run that already exists -> 409 with a clear detail
    with pytest.raises(tui.ApiError) as ei:
        api.post("/api/start", {"run_id": "demo", "task_file": str(TASK)})
    assert ei.value.status == 409 and "exists" in ei.value.detail


def test_api_error_structured_command_detail_is_actionable():
    command_id = "cmd_" + "c" * 32
    error = tui.ApiError({
        "code": "retry_existing_command",
        "existing_command_id": command_id,
        "message": "An unresolved identical control intent already exists.",
        "remediation": f"GET /commands/{command_id}",
    }, status=409)
    assert error.code == "retry_existing_command"
    assert error.existing_command_id == command_id
    assert command_id in error.detail and "GET /commands/" in str(error)


def test_genesis_offline_soft_fails(tmp_path, monkeypatch):
    """With no LLM reachable the genesis boss soft-fails (ok:false) instead of throwing, so the TUI can
    keep the user's draft and show the reason (mirrors GenesisChat's offline handling)."""
    import looplab.serve.server as server_module

    def offline(*_args, **_kwargs):
        raise RuntimeError("offline for deterministic test")

    monkeypatch.setattr(server_module, "make_llm_client", offline)
    api = tui.Api("http://test")
    _bind(api, TestClient(make_app(tmp_path)))
    r = api.genesis([{"role": "user", "content": "a toy run"}], "a toy run", None)
    assert isinstance(r, dict) and r.get("ok") is False
