"""The refusal vocabulary for input the server could not read, and the reads that take no lock
(doc 50 SR-05; doc 52 §5.1 row 5, 2026-09-06).

Six sites answered `HTTPException(500, f"… unreadable: {exc}")` for a `config.snapshot.json` the
server could not read — where every sibling in `run_commands.py` answers 503 with a `code` — and two
of them reflected the `OSError` text, host path included, to the browser. `serve/http.py::REFUSALS`
is now the one table those sites read from, and the guard here is two-way: no literal 500 under
`serve/`, and the slugs the routers emit are exactly the table's.

The second half: three GET paths still took `srv.commands.sequence(rd)` — the EXCLUSIVE
cross-process per-run lock, which fails CLOSED with a 503 on its acquire timeout — so the Files
surface, the paid-lens recovery projection and every review read were refused whenever a writer
held the run. Each already fenced its read with a generation check before and after; that CAS is
what makes a read correct, and the lock bought nothing. Driven the way
`tests/test_read_fence_takes_no_write_lock.py` drives it: HOLD the sequencer from another thread
and ask for the read.
"""
from __future__ import annotations

import ast

from _source_scan import iter_sources, iter_trees
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from looplab.events.eventstore import EventStore
from looplab.serve.http import REFUSALS
from looplab.serve.run_commands import run_generation_token
from looplab.serve.server import make_app

SERVE = Path(__file__).resolve().parents[1] / "looplab" / "serve"
RUN = "demo"
OWNER = {"X-LoopLab-Token": "owner-secret"}


def _run(tmp_path, *, snapshot: bytes | None = b'{"timeout": 30.0}\n'):
    rd = tmp_path / RUN
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": RUN, "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "base"}, "code": "print(1)"})
    if snapshot is not None:
        (rd / "config.snapshot.json").write_bytes(snapshot)
    return rd


def _generation(rd):
    return run_generation_token(EventStore(rd / "events.jsonl").read_all())


# --------------------------------------------------------------------------- the table, two ways


def _serve_sources():
    return [path for path, _text in iter_sources(SERVE)]


# The hand-raised 500s that are FAULTS and not refusals, by enclosing function, each with its reason.
# A partial WRITE the client must retry is a fault; an input the server could not read is not.
FAULT_500_SITES = {
    "_put_run_config_locked": "the snapshot persisted and the durable trust-gate append did not — "
                              "a partial write the client must see as a fault and retry the same "
                              "PUT (tests/test_server.py pins the retry)",
}


def test_no_route_answers_a_literal_500_for_input_it_could_not_read():
    """500 is the framework's word for a fault in the server's own code. A route that could not
    READ its input answers 503 with a code (`REFUSALS`); a route that raises 500 by hand is
    describing a client-visible condition in the vocabulary of a crash — unless it IS a fault, in
    which case it is listed in `FAULT_500_SITES` with its reason. Two-way: an unlisted site is named,
    and a listed site that no longer raises is stale. MUTATION: put one of the six sites back ->
    this names it."""
    offenders, found = [], set()
    for path in _serve_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "HTTPException"
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == 500):
                owner = node
                while owner in parents and not isinstance(
                        owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = parents[owner]
                name = getattr(owner, "name", "<module>")
                if name in FAULT_500_SITES:
                    found.add(name)
                    # …and a fault never reflects the exception's text (a host path) to the browser.
                    assert not any(isinstance(a, ast.JoinedStr) for a in node.args), (
                        f"{path.relative_to(SERVE)}:{node.lineno} interpolates into a 500 body")
                else:
                    offenders.append(f"{path.relative_to(SERVE)}:{node.lineno} in {name}")
    assert not offenders, "literal HTTPException(500) under serve/: " + ", ".join(offenders)
    assert found == set(FAULT_500_SITES), (found, set(FAULT_500_SITES))


def test_no_refusal_of_ANY_status_reflects_the_caught_exception(tmp_path):
    """THE SAME RULE, WITHOUT THE STATUS FILTER — and that filter is why this class survived.

    `refusal()`'s docstring states it plainly: "Never interpolate the exception: an `OSError`'s
    text carries the host path, and the body goes to the browser and into every export of it."
    The census above enforced it only for `HTTPException(500, …)`, so EIGHT sibling sites in
    `run_commands.py` raising 409/503 with an f-string of the caught `OSError` were invisible to
    the check that ended this class. Driven before the fix: a stray regular file where the server
    wants its lock directory answered

        {"detail": "run command-lock path cannot be validated:
                    [Errno 17] File exists: '/abs/host/path/.command-locks'"}

    …to the browser, while `test_no_route_answers_a_literal_500_for_input_it_could_not_read`
    stayed green. The status was never the property; the interpolation is.

    SCOPED TO `OSError`, which is the property and not a proxy for it. A `ValidationError` or an
    `EventStoreConcurrencyError` reflected into a 422/409 is the operator's own input or the run's
    own sequence number said back to them — that is what those refusals are FOR, and censoring
    them would make the server less useful without making it safer. What an `OSError` carries that
    those do not is the HOST PATH, put there by the kernel and never by the caller.

    AST, per handler: an `HTTPException` raised inside an `except OSError as <name>:` (or a tuple
    containing one, or a subclass the module names) may not interpolate `<name>` into its body. A
    `refusal("<slug>")` carries no exception by construction, which is why the fix is to route
    through the table rather than to reword eight strings.
    """
    oserrors = {"OSError", "IOError", "EnvironmentError", "FileNotFoundError", "PermissionError",
                "FileExistsError", "NotADirectoryError", "IsADirectoryError", "BlockingIOError",
                "InterruptedError", "TimeoutError", "ConnectionError", "BrokenPipeError"}

    def _catches_oserror(node) -> bool:
        names = ([node.type] if not isinstance(node.type, ast.Tuple) else list(node.type.elts))
        return any(getattr(n, "id", getattr(n, "attr", "")) in oserrors for n in names if n)

    offenders = []
    for path in _serve_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for handler in ast.walk(tree):
            if not (isinstance(handler, ast.ExceptHandler) and handler.name and handler.type):
                continue
            if not _catches_oserror(handler):
                continue
            caught = handler.name
            for node in ast.walk(handler):
                if not (isinstance(node, ast.Call)
                        and getattr(node.func, "id", "") == "HTTPException"):
                    continue
                for arg in list(node.args) + [k.value for k in node.keywords]:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Name) and sub.id == caught:
                            offenders.append(
                                f"{path.relative_to(SERVE)}:{node.lineno} interpolates the caught "
                                f"`{caught}` into a refusal body")
    assert not offenders, (
        "a refusal reflects a caught OSError's text — it carries the HOST PATH, and "
        "the body reaches the browser and every export of it. Route it through `REFUSALS` / "
        "`refusal(<slug>)` instead:\n  " + "\n  ".join(sorted(set(offenders))))


def test_a_bad_command_lock_path_answers_a_code_and_no_host_path(tmp_path):
    """THE DEFECT, driven: a stray regular file where the server wants its lock directory.

    A bad restore or an operator's mistake leaves `<run>/.command-locks` as a FILE. The handler
    caught the `OSError` and interpolated it, so the browser received

        {"detail": "run command-lock path cannot be validated:
                    [Errno 17] File exists: '/abs/host/path/.command-locks'"}

    — the absolute host path of the operator's run directory, in a body that is also written into
    every export of it. MUTATION: restore the f-string -> the run directory's path is in `detail`.
    """
    _run(tmp_path)
    # UNDER THE SERVER ROOT, which is where `_lock_directory` puts it — not under the run dir.
    (tmp_path / ".command-locks").write_text("not a directory\n", encoding="utf-8")
    client = TestClient(make_app(tmp_path))
    response = client.post(f"/api/runs/{RUN}/control",
                           json={"type": "run_abort", "data": {}})

    assert response.status_code in (409, 503), response.text
    body = response.json()["detail"]
    assert isinstance(body, dict), body
    assert body["code"] in REFUSALS and body["message"] and body["remediation"], body
    assert str(tmp_path) not in response.text, response.text
    assert "Errno" not in response.text, response.text


def test_the_emitted_slugs_are_exactly_the_table():
    """Two-way: every `refusal("<slug>")` names a table row, and every row is emitted somewhere —
    a row nobody emits is a status nobody can be shown, and a slug outside the table is a status
    of its own."""
    emitted = set()
    for path in _serve_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "refusal"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                emitted.add(node.args[0].value)
    assert emitted == set(REFUSALS), (emitted, set(REFUSALS))
    # The STATUS is a closed set, not a constant. The table held only unreadable-snapshot rows when
    # this rule was written, and 503 was the only answer any of them could give. The 2026-09-08
    # rows are the command-lifecycle sites, where a run directory whose `.command-locks` or
    # `.commands` entry is a file or a symlink is the operator's own state conflicting with the
    # request — 409, which is the status those sites already answered before they were routed
    # through the table. What the rule is actually about is that a coded refusal never becomes a
    # 500 and never invents a status of its own, so the check is the SET.
    for slug, (status, message, remediation) in REFUSALS.items():
        assert status in (409, 503), (slug, status)
        assert message and remediation, slug


@pytest.mark.parametrize("snapshot, slug", [
    (b"\xff\xfe not utf-8", "config_snapshot_unreadable"),
    (b"{not json", "config_snapshot_unreadable"),
    (b"[]\n", "config_snapshot_not_object"),
])
def test_an_unreadable_snapshot_is_a_503_with_a_code_and_no_host_path(tmp_path, snapshot, slug):
    """THE DEFECT, driven on the owner's config GET and PUT. MUTATION: restore the f-string 500 ->
    the status is 500 and the body carries the run directory's path."""
    rd = _run(tmp_path, snapshot=snapshot)
    client = TestClient(make_app(tmp_path))
    generation = client.get(f"/api/runs/{RUN}/state").json()["generation"]
    for response in (client.get(f"/api/runs/{RUN}/config"),
                     client.put(f"/api/runs/{RUN}/config",
                                json={"settings": {"timeout": 45.0},
                                      "expected_generation": generation})):
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == slug, response.text
        assert "remediation" in response.json()["detail"]
        assert str(rd) not in response.text and str(tmp_path) not in response.text


def test_the_review_plane_answers_the_same_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    rd = _run(tmp_path)
    client = TestClient(make_app(tmp_path))
    created = client.post(f"/api/runs/{RUN}/reviews", headers=OWNER,
                          json={"ttl_seconds": 3600, "include_evidence": False})
    assert created.status_code == 200, created.text
    review = {"X-LoopLab-Review": created.json()["token"]}
    (rd / "config.snapshot.json").write_bytes(b"[]\n")
    response = client.get("/api/review/config", headers=review)
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "config_snapshot_not_object"
    assert str(rd) not in response.text


def _event_log_raises_eio(monkeypatch, rd) -> None:
    """`events.jsonl` EXISTS — so `run_dir` admits the run, it lstats and never opens — and every
    open of it fails with EIO: what a flaky network/FUSE mount produces, and what EACCES after a
    permission change looks like to a server running as root (which a chmod cannot demonstrate)."""
    import builtins
    import errno
    import os

    real_open = builtins.open
    target = os.path.realpath(rd / "events.jsonl")

    def _eio_open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and os.path.realpath(file) == target:
            raise OSError(errno.EIO, "Input/output error", os.fspath(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _eio_open)


@pytest.mark.parametrize("path", [
    f"/api/runs/{RUN}/state", f"/api/runs/{RUN}/nodes/0", f"/api/runs/{RUN}/cost",
    f"/api/runs/{RUN}/prov", f"/api/runs/{RUN}/config", f"/api/runs/{RUN}/comments",
    f"/api/runs/{RUN}/nodes/0/trace",
    # The legacy raw-envelope route reads `iter_event_jsonl` itself (its rows are the RAW envelopes,
    # which `AppState.events` re-validates into `Event`s), so it names the same refusal on its own.
    f"/api/runs/{RUN}/log",
])
def test_an_unreadable_event_log_is_a_coded_503_on_every_per_run_read(tmp_path, monkeypatch, path):
    """Review 2026-09-22, SRV2-04: an `events.jsonl` that exists but cannot be read answered a bare
    500 on every per-run GET — the framework's word for a crash in the server's own code, which a
    client reports and never retries. `AppState.events` is the read every fold on the HTTP path goes
    through, so it answers `event_log_unreadable` from the table. MUTATION: drop the `except OSError`
    in `AppState.events` -> 500."""
    rd = _run(tmp_path)
    client = TestClient(make_app(tmp_path), raise_server_exceptions=False)
    _event_log_raises_eio(monkeypatch, rd)
    response = client.get(path)
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "event_log_unreadable" and detail["remediation"], detail
    assert str(tmp_path) not in response.text and "Errno" not in response.text


def test_an_unreadable_event_log_keeps_its_run_in_the_list_and_says_why(tmp_path, monkeypatch):
    """The other half: the run list's per-run `except Exception: continue` swallowed the same error,
    so the run silently VANISHED from `/api/runs`. It stays, as a stub whose receipt says
    `unreadable` (the UI words that as "could not be read at all"), with EXACTLY the keys a folded
    row carries — and it is not cached: the first list after the fault clears is the real row.
    MUTATION: re-raise out of the new branch -> the run is missing from the list again."""
    _run(tmp_path)
    readable = tmp_path / "readable"
    readable.mkdir()
    EventStore(readable / "events.jsonl").append(
        "run_started", {"run_id": "readable", "task_id": "t", "goal": "g", "direction": "min"})
    client = TestClient(make_app(tmp_path))
    with monkeypatch.context() as scoped:
        _event_log_raises_eio(scoped, tmp_path / RUN)
        rows = {row["run_id"]: row for row in client.get("/api/runs").json()}
    assert set(rows) == {RUN, "readable"}, "a run whose log cannot be read vanished from the list"
    stub = rows[RUN]
    assert stub["source_integrity"]["unreadable"] is True
    assert stub["source_integrity"]["complete"] is False
    assert stub["nodes"] == 0 and stub["generation"] is None and stub["best_metric"] is None
    assert set(stub) == set(rows["readable"]), "the stub row's shape drifted from a folded row"

    healed = {row["run_id"]: row for row in client.get("/api/runs").json()}
    assert healed[RUN]["source_integrity"]["complete"] is True, "the stub was cached"
    assert healed[RUN]["task_id"] == "t" and healed[RUN]["nodes"] == 1


def test_a_log_that_is_not_a_regular_file_stays_unlisted(tmp_path):
    """The stub is for a log `run_dir` would OPEN. A directory where the log should be is a 404 per
    run, so listing it would publish a row that cannot be opened — it stays out, as it always was."""
    _run(tmp_path)
    odd = tmp_path / "odd"
    odd.mkdir()
    (odd / "events.jsonl").mkdir()
    client = TestClient(make_app(tmp_path))
    assert [row["run_id"] for row in client.get("/api/runs").json()] == [RUN]
    assert client.get("/api/runs/odd/state").status_code == 404


# --------------------------------------------------------------------------- reads take no lock


def _read_while_held(tmp_path, app, request):
    """Hold the run's exclusive sequencer from another thread; `request(client)` must answer
    WHILE it is held — the ordering, not a duration (the sibling test's own lesson)."""
    srv = app.state.looplab
    rd = tmp_path / RUN
    client = TestClient(app)
    held, release, answered = threading.Event(), threading.Event(), threading.Event()
    box: dict = {}

    def _hold():
        with srv.commands.sequence(rd):
            held.set()
            release.wait(60)

    def _read():
        box["response"] = request(client)
        answered.set()

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    assert held.wait(10), "the sequencer was never acquired — re-point this test"
    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    try:
        assert answered.wait(10), "the read is blocked behind the write sequencer"
        assert not release.is_set(), "the read only completed after the writer let go"
        return box["response"]
    finally:
        release.set()
        holder.join(10)
        reader.join(10)


def test_the_files_surface_is_readable_WHILE_a_writer_holds_the_run(tmp_path):
    """MUTATION: restore `with srv.commands.sequence(rd):` in `_assert_artifact_generation` ->
    blocks for the holder's whole wait, or 503s at the acquire budget."""
    _run(tmp_path)
    app = make_app(tmp_path)
    generation = _generation(tmp_path / RUN)
    response = _read_while_held(
        tmp_path, app,
        lambda c: c.get(f"/api/runs/{RUN}/artifacts", params={"expected_generation": generation}))
    assert response.status_code != 503, response.text
    assert response.status_code == 200, response.text


def test_paid_lens_recovery_is_readable_WHILE_a_writer_holds_the_run(tmp_path):
    _run(tmp_path)
    app = make_app(tmp_path)
    generation = _generation(tmp_path / RUN)
    response = _read_while_held(
        tmp_path, app,
        lambda c: c.get(f"/api/runs/{RUN}/concepts/lens/recovery",
                        params={"expected_generation": generation}))
    assert response.status_code != 503, response.text
    assert response.status_code == 200 and response.json()["state"] == "none", response.text


def test_a_review_read_is_readable_WHILE_the_owner_holds_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    _run(tmp_path)
    app = make_app(tmp_path)
    owner = TestClient(app)
    created = owner.post(f"/api/runs/{RUN}/reviews", headers=OWNER,
                         json={"ttl_seconds": 3600, "include_evidence": False})
    assert created.status_code == 200, created.text
    review = {"X-LoopLab-Review": created.json()["token"]}
    response = _read_while_held(tmp_path, app, lambda c: c.get("/api/review/state", headers=review))
    assert response.status_code != 503, response.text
    assert response.status_code == 200, response.text


def test_a_MOVED_generation_is_still_refused_after_the_read(tmp_path):
    """The property the lock was mistaken for. Removing exclusivity must not remove the fence."""
    _run(tmp_path)
    client = TestClient(make_app(tmp_path))
    stale = _generation(tmp_path / RUN)
    EventStore(tmp_path / RUN / "events.jsonl").append("pause", {})
    fresh = _generation(tmp_path / RUN)
    if fresh == stale:
        pytest.skip("the generation token does not move on this row; the fence has its own tests")
    response = client.get(f"/api/runs/{RUN}/artifacts", params={"expected_generation": stale})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "run_generation_changed"


def _publish_stuck_reset_marker(rd):
    """A reset marker whose receipt never landed — the "stuck Replay" leftover. Any marker will do:
    `_state_payload` reaches for the sequencer BEFORE it validates anything."""
    import uuid

    from looplab.core.run_reset import publish_run_reset_marker

    publish_run_reset_marker(rd, operation_id=str(uuid.uuid4()),
                             expected_generation=_generation(rd), receipt_name="x.json")


def test_state_is_readable_WHILE_a_writer_holds_a_run_that_carries_a_reset_marker(tmp_path):
    """Review 2026-09-22, SRV2-08: with a reset marker on disk, `routers/runs.py::_state_payload`
    reconciled it under `srv.commands.sequence(rd)` — the EXCLUSIVE lock, at the service's whole
    acquire budget (60 s by default) — before serving a GET. Six GETs reach that helper, and `/state`
    blocked for the full budget whenever a writer held the run. The reconcile is opportunistic
    cleanup the owning writer (or the next uncontended read) completes anyway, so a read TRIES the
    lock and never waits for it. The budget here is one the old path would have waited out in full.
    MUTATION: drop `timeout=0` from that acquire -> this read is still blocked after 10 s."""
    rd = _run(tmp_path)
    _publish_stuck_reset_marker(rd)
    app = make_app(tmp_path)
    app.state.looplab.commands.lock_acquire_timeout = 30.0
    timed: dict = {}

    def _timed_state(client):
        started = time.monotonic()
        response = client.get(f"/api/runs/{RUN}/state")
        timed["seconds"] = time.monotonic() - started
        return response

    response = _read_while_held(tmp_path, app, _timed_state)
    assert response.status_code == 200, response.text
    assert timed["seconds"] < 5.0, f"/state waited {timed['seconds']:.2f}s behind the writer"


def test_an_uncontended_state_read_still_reconciles_the_reset_marker(tmp_path, monkeypatch):
    """The other half: trying instead of waiting must not stop reconciling. With the lock free the
    read still takes it (non-blocking) and completes the observation, exactly as before; with a
    writer holding it, the read skips the cleanup rather than waiting for it."""
    import looplab.serve.routers.runs as runs_router

    rd = _run(tmp_path)
    _publish_stuck_reset_marker(rd)
    reconciled: list = []
    monkeypatch.setattr(runs_router, "reconcile_run_reset_observation",
                        lambda srv, run_dir: reconciled.append(run_dir) or False)
    app = make_app(tmp_path)
    assert TestClient(app).get(f"/api/runs/{RUN}/state").status_code == 200
    assert reconciled == [rd], "an uncontended read must still reconcile the marker"

    reconciled.clear()
    response = _read_while_held(tmp_path, app, lambda c: c.get(f"/api/runs/{RUN}/state"))
    assert response.status_code == 200 and reconciled == [], (
        "a contended read must skip the cleanup, not wait for or race the writer")


def _waits_on_the_sequencer(call: ast.Call) -> bool:
    """A `.sequence(...)` acquire that can WAIT: anything but an explicit literal-zero `timeout`,
    which is one non-blocking try (`RunCommandService.sequence` fails closed at once on contention)."""
    if getattr(call.func, "attr", "") != "sequence":
        return False
    return not any(k.arg == "timeout" and isinstance(k.value, ast.Constant)
                   and type(k.value.value) in (int, float) and k.value.value == 0
                   for k in call.keywords)


def test_only_a_reconciling_GET_still_takes_the_sequencer():
    """The one GET handler that can WAIT on the exclusive lock is `start_status`, which RECONCILES a
    dead spawn's claim — a write, not a read. Derived from the routers' own AST in both directions,
    so a converted fence cannot quietly grow the lock back and a new sequenced GET must be listed
    here with its reason.

    FOLLOWS SAME-MODULE HELPERS (review 2026-09-22, SRV2-08). This scanned handler BODIES only, and
    six GETs reached the lock through `routers/runs.py::_state_payload` — a closure the handler
    calls, or hands to `anyio.to_thread.run_sync` — so the rule this states was false while the test
    was green. Every function or closure of the same module that a GET REFERENCES by name is
    followed, transitively; a literal `timeout=0` acquire (one non-blocking try) is not a wait.
    MUTATION: drop `timeout=0` from `_state_payload`'s acquire -> `get_state`, `stream_events` and the
    node-evidence GETs are named."""
    allowed = {"start_status": "reconciles a dead spawn's start claim under the lock"}
    sequenced_gets, sequenced_helpers = set(), set()
    for path, tree in iter_trees(SERVE / "routers"):
        functions: dict[str, list] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.setdefault(node.name, []).append(node)

        def _waits(func, seen: set) -> bool:
            if id(func) in seen:
                return False
            seen.add(id(func))
            for sub in ast.walk(func):
                if isinstance(sub, ast.Call) and _waits_on_the_sequencer(sub):
                    return True
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    if any(_waits(helper, seen) for helper in functions.get(sub.id, ())):
                        return True
            return False

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_get = any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "get"
                         for d in node.decorator_list)
            holds = _waits(node, set())
            if is_get and holds:
                sequenced_gets.add(node.name)
            if holds and node.name in {"_assert_artifact_generation", "validate_bound_generation",
                                       "recover_concept_lens_receipt", "_state_payload",
                                       "_cached_node_attempt"}:
                sequenced_helpers.add(node.name)
    assert sequenced_gets == set(allowed), sequenced_gets
    assert not sequenced_helpers, f"{sorted(sequenced_helpers)} took the exclusive sequencer back"
