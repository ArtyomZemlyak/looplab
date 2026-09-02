"""One fold per (run, file identity, request) — and none across requests.

Measured on the routers: two folds per `GET .../config`, three plus a full read per PUT, two per
artifact read and two per 4 s metrics poll. Each re-read and re-folded the whole `events.jsonl`,
which on a repo run embeds full file sets in every `node_created`.

THE RULE THIS DOES NOT BREAK. Invariant #4 forbids caching derived state across ENGINE LOOP
ITERATIONS; the engine is a different process and never reaches `AppState`. What makes the memo
sound is invariant #5: `fold` is deterministic and order-tolerant with no I/O, so two folds of the
same BYTES are equal — and the key is `file_identity`, so an append inside the request (the config
PUT writes an event and re-folds) misses and gets the new state.

WITHIN a request, never across, because a folded `RunState` is mutable. Nothing in `serve/` mutates
one today — re-derived below — but a cross-request cache would make that a property every future
route has to preserve silently, with the failure appearing in ANOTHER request's state.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from looplab.events.eventstore import EventStore
from looplab.serve import appstate as appstate_mod
from looplab.serve.appstate import request_fold_scope
from looplab.serve.server import make_app

SERVE = Path(appstate_mod.__file__).resolve().parent


def _run(tmp_path, run_id="demo"):
    rd = tmp_path / run_id
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": run_id, "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "base"}, "code": "print(1)"})
    return rd


def _srv(tmp_path):
    return make_app(tmp_path).state.looplab


def _counting(srv, calls):
    real = appstate_mod.fold

    def _fold(events):
        calls.append(1)
        return real(events)
    return _fold


def test_without_a_scope_every_call_folds(monkeypatch, tmp_path):
    """The NEGATIVE CONTROL, and the byte-identical behaviour outside HTTP: background jobs, the
    CLI and a test constructing `AppState` directly all still get a fresh fold."""
    srv, calls = _srv(tmp_path), []
    rd = _run(tmp_path)
    monkeypatch.setattr(appstate_mod, "fold", _counting(srv, calls))

    srv.state(rd)
    srv.state(rd)
    srv.state(rd)
    assert len(calls) == 3


def test_inside_one_scope_the_same_run_folds_ONCE(monkeypatch, tmp_path):
    """THE FIX. MUTATION: drop the memo lookup -> three folds of a multi-MB log per request."""
    srv, calls = _srv(tmp_path), []
    rd = _run(tmp_path)
    monkeypatch.setattr(appstate_mod, "fold", _counting(srv, calls))

    with request_fold_scope():
        first = srv.state(rd)
        second = srv.state(rd)
        third = srv.state(rd)
    assert len(calls) == 1
    assert first is second is third


def test_two_scopes_do_not_share(monkeypatch, tmp_path):
    """A request-scoped memo, not a cache. MUTATION: hoist it onto `AppState` -> two requests share
    one mutable `RunState`, and "no route mutates it" becomes a property every future route has to
    preserve silently."""
    srv, calls = _srv(tmp_path), []
    rd = _run(tmp_path)
    monkeypatch.setattr(appstate_mod, "fold", _counting(srv, calls))

    with request_fold_scope():
        srv.state(rd)
    with request_fold_scope():
        srv.state(rd)
    assert len(calls) == 2


def test_an_append_inside_the_scope_MISSES(monkeypatch, tmp_path):
    """The key is `file_identity`, so a writer that appends and re-folds gets the new state. This is
    exactly what the config PUT does, and a memo keyed on the path alone would hand it back the
    state from before its own write."""
    srv, calls = _srv(tmp_path), []
    rd = _run(tmp_path)
    monkeypatch.setattr(appstate_mod, "fold", _counting(srv, calls))

    with request_fold_scope():
        before = srv.state(rd)
        assert 1 not in before.nodes
        EventStore(rd / "events.jsonl").append("node_created", {
            "node_id": 1, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {}, "rationale": "x"}, "code": "print(2)"})
        after = srv.state(rd)

    assert len(calls) == 2, "the append must not be served from the memo"
    assert 1 in after.nodes


def test_two_runs_in_one_scope_keep_their_own_entries(monkeypatch, tmp_path):
    """The key carries the run directory. MUTATION: key on identity alone -> two runs whose logs
    happen to share a `file_identity` answer about each other."""
    srv, calls = _srv(tmp_path), []
    first, second = _run(tmp_path, "one"), _run(tmp_path, "two")
    monkeypatch.setattr(appstate_mod, "fold", _counting(srv, calls))

    with request_fold_scope():
        a = srv.state(first)
        b = srv.state(second)
        srv.state(first)
    assert len(calls) == 2
    assert a.run_id == "one" and b.run_id == "two"


def test_a_missing_log_is_not_shared_between_runs(monkeypatch, tmp_path):
    """Both fold to nothing today, so sharing would give the right ANSWER for the wrong reason — and
    the memo would be claiming something about a file it never read."""
    srv, calls = _srv(tmp_path), []
    monkeypatch.setattr(appstate_mod, "fold", _counting(srv, calls))
    with request_fold_scope():
        srv.state(tmp_path / "gone-a")
        srv.state(tmp_path / "gone-b")
    assert len(calls) == 2


def test_the_memo_is_bounded(monkeypatch, tmp_path):
    """An SSE generator or a fan-out route must not accumulate one folded state per run. Past the
    bound it stops answering, which degrades to the previous behaviour rather than to a wrong one."""
    srv, calls = _srv(tmp_path), []
    runs = [_run(tmp_path, f"r{i}") for i in range(appstate_mod._REQUEST_FOLD_MEMO_MAX + 2)]
    monkeypatch.setattr(appstate_mod, "fold", _counting(srv, calls))

    with request_fold_scope():
        for rd in runs:
            srv.state(rd)
        overflowed = runs[-1]
        srv.state(overflowed)                       # past the bound: folds again
        srv.state(runs[0])                          # inside the bound: memoized
    assert len(calls) == len(runs) + 1


def test_the_live_server_installs_the_scope(tmp_path):
    """END TO END: a real request through the real middleware.

    It observes the ContextVar AT FOLD TIME, which is the only thing that proves the scope reached
    the route — including across the threadpool hop a sync handler takes. Asserting merely that the
    request folded is vacuous: it folds either way.

    MUTATION: drop the middleware -> every fold runs with no memo installed, which is the shape of a
    fix that measures nothing.
    """
    scoped: list[bool] = []
    _run(tmp_path)
    app = make_app(tmp_path)
    original = appstate_mod.fold

    def _fold(events):
        scoped.append(appstate_mod._REQUEST_FOLD_MEMO.get() is not None)
        return original(events)

    appstate_mod.fold = _fold
    try:
        with TestClient(app) as client:
            response = client.get("/api/runs/demo/state")
        assert response.status_code == 200
    finally:
        appstate_mod.fold = original

    assert scoped, "the request never folded at all — re-point this test"
    assert any(scoped), "no fold in a real request saw the memo: the middleware did not run"


def test_a_streaming_response_releases_the_memo(tmp_path):
    """The cost of the PURE-ASGI shape, paid back. A `BaseHTTPMiddleware` wrapper would end at the
    response's headers; a plain ASGI one stays active for the whole body, so an SSE connection open
    for hours would otherwise hold up to `_REQUEST_FOLD_MEMO_MAX` folded states — on a 64 MB log
    that is real memory held for as long as a browser tab is open.

    MUTATION: drop the `memo.clear()` in the send wrapper -> the entries survive every chunk.
    """
    from looplab.serve.server import _RequestFoldMemoMiddleware

    seen: list[int] = []

    async def _app(scope, receive, send):
        memo = appstate_mod._REQUEST_FOLD_MEMO.get()
        assert memo is not None, "the scope must be installed for the handler"
        memo[("run", "identity")] = object()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"chunk", "more_body": True})
        seen.append(len(memo))                       # after the first streamed chunk
        await send({"type": "http.response.body", "body": b""})

    async def _drive():
        await _RequestFoldMemoMiddleware(_app)(
            {"type": "http", "path": "/api/runs/demo/events"},
            None, lambda message: _noop())

    async def _noop():
        return None

    import anyio
    anyio.run(_drive)
    assert seen == [0], "a streaming response must not hold folded states for its lifetime"


def test_a_non_http_scope_is_passed_straight_through(tmp_path):
    """Lifespan and websocket scopes have no request to memoize and must not pay for one."""
    from looplab.serve.server import _RequestFoldMemoMiddleware

    called: list[str] = []

    async def _app(scope, receive, send):
        called.append(scope["type"])
        assert appstate_mod._REQUEST_FOLD_MEMO.get() is None

    async def _drive():
        await _RequestFoldMemoMiddleware(_app)({"type": "lifespan"}, None, None)

    import anyio
    anyio.run(_drive)
    assert called == ["lifespan"]


def test_no_serve_route_mutates_a_folded_state():
    """The property the request scope means nobody has to preserve forever — but which must hold
    TODAY, or even a within-request memo would hand a mutated state to the next reader.

    AST over `serve/`: an assignment to an attribute of a name bound from `.state(`/`fold(`. Not a
    substring scan, because a commented-out mutation is not an `ast.Assign`.
    """
    offenders: list[str] = []
    for path in sorted(SERVE.rglob("*.py")):
        if "__pycache__" in path.parts or ".ipynb_checkpoints" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
        folded: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                func = node.value.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else func.id if isinstance(func, ast.Name) else "")
                if name in {"state", "fold"}:
                    folded.update(t.id for t in node.targets if isinstance(t, ast.Name))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                        and target.value.id in folded):
                    offenders.append(f"{path.name}:{node.lineno} {target.value.id}.{target.attr}")
    assert not offenders, (
        "a folded RunState is mutated in serve/, so it may not be shared even within one request:\n  "
        + "\n  ".join(offenders))
