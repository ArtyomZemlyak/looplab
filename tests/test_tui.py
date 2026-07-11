"""The terminal control plane (`looplab tui`). The TUI is a thin HTTP client of the UI server, so the
pure rendering/gating helpers are unit-tested directly, and the client (Api) is exercised against a
real server via FastAPI's TestClient (no sockets) so the request/response contracts stay in lockstep
with the React `util.js` they mirror.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from looplab.serve import tui


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


def test_action_needs_engine():
    assert tui.action_needs_engine({"type": "inject_node"}) is True
    assert tui.action_needs_engine({"type": "budget_extend"}) is True
    assert tui.action_needs_engine({"type": "hint"}) is False
    assert tui.action_needs_engine({}) is False


def test_is_critical():
    assert tui.is_critical({"type": "run_abort"}) is True
    assert tui.is_critical({"type": "node_abort"}) is True
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
    def _request(method, path, body=None, timeout=None):
        r = client.request(method, path, json=body)
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


def test_genesis_offline_soft_fails(tmp_path):
    """With no LLM reachable the genesis boss soft-fails (ok:false) instead of throwing, so the TUI can
    keep the user's draft and show the reason (mirrors GenesisChat's offline handling)."""
    api = tui.Api("http://test")
    _bind(api, TestClient(make_app(tmp_path)))
    r = api.genesis([{"role": "user", "content": "a toy run"}], "a toy run", None)
    assert isinstance(r, dict) and r.get("ok") is False
