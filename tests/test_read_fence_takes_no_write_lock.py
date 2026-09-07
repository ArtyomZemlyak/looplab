"""A GET does not take the run's exclusive write sequencer.

`_assert_historical_generation` fenced historical node detail by taking `srv.commands.sequence(rd)` —
the EXCLUSIVE cross-process per-run lock, which fails CLOSED with a 503 on its acquire timeout. So
the read was refused whenever a writer held the run, which on a live run is most of the time.

The lock was never what made it correct. A read fence is made correct by a CAS ACROSS the read, and
`node_detail` already calls the fence before AND after its fold — its own comment says "the expensive
fold runs without the exclusive command sequencer" — so the first call holding the lock proved
nothing the second did not. Holding it across the fold would be a different and much worse design.

Driven by actually HOLDING the sequencer from another thread and asking for the read.
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _source_scan import function_tree

from looplab.events.eventstore import EventStore
from looplab.serve.run_commands import run_generation_token
from looplab.serve.server import make_app

RUN = "demo"


def _run(tmp_path):
    rd = tmp_path / RUN
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": RUN, "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "base"}, "code": "print(1)"})
    return rd


def _generation(rd):
    return run_generation_token(EventStore(rd / "events.jsonl").read_all())


def test_historical_detail_is_readable_WHILE_a_writer_holds_the_run(tmp_path):
    """THE DEFECT. MUTATION: restore `with srv.commands.sequence(rd):` -> this 503s (or blocks for
    the acquire budget) exactly when an engine is writing, i.e. whenever the run is alive."""
    rd = _run(tmp_path)
    app = make_app(tmp_path)
    srv = app.state.looplab
    client = TestClient(app)
    generation = _generation(rd)

    held = threading.Event()
    release = threading.Event()
    answered = threading.Event()
    box: dict = {}

    def _hold():
        with srv.commands.sequence(rd):
            held.set()
            release.wait(60)

    def _read():
        box["response"] = client.get(f"/api/runs/{RUN}/nodes/0",
                                     params={"seq": 1, "expected_generation": generation})
        answered.set()

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    assert held.wait(10), "the sequencer was never acquired — re-point this test"
    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    try:
        # THE ORDERING, not a duration: the read must finish while the writer STILL HOLDS the lock.
        # Asserting only "it eventually answered 200" is not enough and this test used to do exactly
        # that — under the mutation the request simply blocked for the holder's whole wait and then
        # succeeded, so the assertion held while the defect was fully present.
        assert answered.wait(10), "the read is blocked behind the write sequencer"
        assert not release.is_set(), "the read only completed after the writer let go"
        assert box["response"].status_code == 200, box["response"].text
        assert box["response"].json()["run_generation"] == generation
    finally:
        release.set()
        holder.join(10)
        reader.join(10)


def test_a_MOVED_generation_is_still_a_409(tmp_path):
    """The property the lock was mistaken for. Removing exclusivity must not remove the fence."""
    rd = _run(tmp_path)
    client = TestClient(make_app(tmp_path))
    response = client.get(f"/api/runs/{RUN}/nodes/0",
                          params={"seq": 1, "expected_generation": "b" * 64})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "run_generation_changed"


def test_a_missing_generation_is_still_a_400(tmp_path):
    rd = _run(tmp_path)
    client = TestClient(make_app(tmp_path))
    response = client.get(f"/api/runs/{RUN}/nodes/0", params={"seq": 1})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "historical_generation_required"


def test_the_fence_is_still_taken_TWICE_around_the_fold(tmp_path):
    """What actually makes the read safe. AST over the real handler, because the two calls are what
    the CAS IS — one of them is not a fence, it is a guess.

    MUTATION: drop either call -> a reset that wins during the fold publishes generation-A bytes
    under a generation-B label, which is exactly what this route exists to prevent.
    """
    from looplab.serve.routers import runs as runs_mod

    source = Path(runs_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    handler = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "node_detail")
    calls = [n for n in ast.walk(handler)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_assert_historical_generation"]
    assert len(calls) >= 2, "the before/after CAS is gone"


def test_the_fence_helper_takes_no_lock():
    """AST over the helper's own body: `sequence` must not appear. A comment saying so would satisfy
    a substring scan, and this is the one line the whole change is."""
    from looplab.serve.routers import runs as runs_mod

    source = Path(runs_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    helper = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "_assert_historical_generation")
    attrs = {n.func.attr for n in ast.walk(helper)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "sequence" not in attrs, "the read fence takes the exclusive write lock again"
    assert "generation_fence" in attrs, "it must go through the shared read-side primitive"


def test_generation_fence_returns_the_validated_dir_and_the_generation(tmp_path):
    """Driven directly. The path is RETURNED rather than discarded so a caller reads the same
    canonical directory the generation was taken from."""
    rd = _run(tmp_path)
    srv = make_app(tmp_path).state.looplab
    validated, generation = srv.commands.generation_fence(rd)
    assert generation == _generation(rd)
    assert Path(validated).resolve() == rd.resolve()


def test_generation_fence_does_not_serialise_two_readers(tmp_path):
    """Two concurrent reads must not queue behind each other — the whole reason a read fence is not
    a lock."""
    rd = _run(tmp_path)
    srv = make_app(tmp_path).state.looplab
    barrier = threading.Barrier(3, timeout=10)
    seen: list[str] = []

    def _reader():
        barrier.wait()
        seen.append(srv.commands.generation_fence(rd)[1])

    threads = [threading.Thread(target=_reader) for _ in range(2)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join(10)
    assert seen == [_generation(rd)] * 2
