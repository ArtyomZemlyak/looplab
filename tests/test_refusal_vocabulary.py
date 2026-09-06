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
    for slug, (status, message, remediation) in REFUSALS.items():
        assert status == 503 and message and remediation, slug


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


def test_only_a_reconciling_GET_still_takes_the_sequencer():
    """The one GET handler whose body holds the exclusive lock is `start_status`, which RECONCILES a
    dead spawn's claim — a write, not a read. Derived from the routers' own AST in both directions,
    so a converted fence cannot quietly grow the lock back and a new sequenced GET must be listed
    here with its reason."""
    allowed = {"start_status": "reconciles a dead spawn's start claim under the lock"}
    sequenced_gets = set()
    for path, tree in iter_trees(SERVE / "routers"):
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_get = any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "get"
                         for d in node.decorator_list)
            holds = any(isinstance(sub, ast.Call) and getattr(sub.func, "attr", "") == "sequence"
                        for sub in ast.walk(node))
            if is_get and holds:
                sequenced_gets.add(node.name)
            if node.name in {"_assert_artifact_generation", "validate_bound_generation",
                             "recover_concept_lens_receipt"}:
                assert not holds, f"{node.name} took the exclusive sequencer back"
    assert sequenced_gets == set(allowed), sequenced_gets
