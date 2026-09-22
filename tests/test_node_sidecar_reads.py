"""Files a candidate can write are read by polled GETs through ONE hardened reader.

Review 2026-09-22, SRV2-03. The node workdir is the candidate's own cwd, so anything in it can be a
FIFO, a symlink or a pathological tree the moment its code runs. Three polled readers opened those
names with blocking, link-following, unbounded opens:

  * `routers/runs.py::node_logs._tail` — `open(p, "rb")` on every stage log (and the Developer's
    `looplab_stages.json` via `read_text` just before it);
  * `core/node_evidence.py::metrics_attempt_receipt` — `.read_text()` of the attempt receipt;
  * `serve/metrics_adapters.py::TensorBoardAdapter.read` — a recursive `glob`, which follows
    directory symlinks (a loop, or a link into ANOTHER run's workdir).

A FIFO pins a request thread forever — `os.mkfifo('setup.log')` in the eval's cwd is enough, and
forty of them hang every sync route — and a symlink publishes whatever it points at, another run's
curves included (through the one-run review plane too). The properties below are driven through the
real routes with a bounded thread: each must ANSWER, empty, well inside the poll interval.
"""
from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient                          # noqa: E402

from looplab.core.node_evidence import (                            # noqa: E402
    METRICS_ATTEMPT_FILE, METRICS_ATTEMPT_RECEIPT_MAX_BYTES, begin_metrics_attempt,
    metrics_attempt_receipt, read_bounded_regular_file)
from looplab.events.eventstore import EventStore                    # noqa: E402
from looplab.serve.server import make_app                           # noqa: E402

pytestmark = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")

#: The answer budget. A blocked read never answers at all, so this only has to beat "never" by a
#: margin a contended CI box cannot eat; the poll interval it protects is 4 s.
_ANSWER_WITHIN_S = 2.0


def _run(root: Path, run_id: str = "demo") -> Path:
    rd = root / run_id
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": run_id, "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                                  "code": "print(1)"})
    node = rd / "nodes" / "node_0"
    node.mkdir(parents=True)
    return node


def _release_fifo_readers(fifos) -> None:
    """Unblock a reader stuck in `open()` on a FIFO (the pre-fix code), so a red run cannot strand a
    server thread for the rest of the session: open the write end non-blocking, then close it."""
    for fifo in fifos:
        try:
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass                     # ENXIO: nobody is blocked on it, which is the fixed behaviour


def _answer(client, path, *, fifos=()) -> tuple:
    client.get("/api/runs")          # app startup is not what is being timed
    box: dict = {}

    def _call():
        started = time.monotonic()
        box["response"] = client.get(path)
        box["seconds"] = time.monotonic() - started

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    thread.join(_ANSWER_WITHIN_S + 3.0)
    blocked = thread.is_alive()
    _release_fifo_readers(fifos)
    thread.join(10)
    assert not blocked, f"{path} is blocked on a candidate-planted file"
    assert box["seconds"] < _ANSWER_WITHIN_S, f"{path} took {box['seconds']:.2f}s"
    return box["response"]


# ------------------------------------------------------------------ the shared reader, directly

def test_the_reader_is_bounded_and_reads_a_tail_from_the_end(tmp_path):
    log = tmp_path / "eval.log"
    log.write_bytes(b"0123456789")
    assert read_bounded_regular_file(log, 4) == b"0123"
    assert read_bounded_regular_file(log, 4, tail=True) == b"6789"
    assert read_bounded_regular_file(log, 64, tail=True) == b"0123456789"
    assert read_bounded_regular_file(log, 0) == b""
    assert read_bounded_regular_file(tmp_path / "absent.log", 4) is None


def test_the_reader_refuses_a_fifo_a_symlink_and_a_directory_at_once(tmp_path):
    fifo = tmp_path / "setup.log"
    os.mkfifo(fifo)
    target = tmp_path / "secret.txt"
    target.write_bytes(b"not yours")
    link = tmp_path / "eval.log"
    link.symlink_to(target)
    started = time.monotonic()
    try:
        assert read_bounded_regular_file(fifo, 4) is None
    finally:
        _release_fifo_readers([fifo])
    assert time.monotonic() - started < _ANSWER_WITHIN_S
    assert read_bounded_regular_file(link, 64) is None, "a final-component link was followed"
    assert read_bounded_regular_file(tmp_path, 64) is None


def test_an_oversized_or_non_regular_receipt_is_no_receipt(tmp_path):
    begin_metrics_attempt(tmp_path, 2, started_at=5.0)
    assert metrics_attempt_receipt(tmp_path) == (2, 5.0)
    receipt = tmp_path / METRICS_ATTEMPT_FILE
    receipt.write_bytes(b'{"attempt": 2, "started_at": 5.0, "pad": "'
                        + b"x" * METRICS_ATTEMPT_RECEIPT_MAX_BYTES + b'"}')
    assert metrics_attempt_receipt(tmp_path) is None, "the receipt read is bounded"
    receipt.unlink()
    os.mkfifo(receipt)
    try:
        assert metrics_attempt_receipt(tmp_path) is None
    finally:
        _release_fifo_readers([receipt])


# ------------------------------------------------------------------ the polled routes

@pytest.mark.parametrize("planted", ["setup.log", "eval.log", "score.log", "looplab_stages.json"])
def test_node_logs_answers_past_a_planted_fifo(tmp_path, planted):
    """MUTATION: restore `open(p, "rb")` in `_tail` (or `read_text` for the manifest) -> the route
    never answers; the teardown opens the FIFO's write end to free the stuck thread."""
    node = _run(tmp_path)
    if planted != "eval.log":
        (node / "eval.log").write_text("real eval output\n")
    fifo = node / planted
    os.mkfifo(fifo)
    response = _answer(TestClient(make_app(tmp_path)), "/api/runs/demo/nodes/0/logs",
                       fifos=[fifo])
    assert response.status_code == 200, response.text
    body = response.json()
    if planted != "eval.log":
        assert body["eval"] == "real eval output\n", "the regular logs beside it still read"


def test_node_logs_never_follows_a_log_symlink_into_another_run(tmp_path):
    """The cross-run leak: a candidate links its `eval.log` to a sibling run's log (or any file the
    server can read). The old containment check resolved the link and accepted any target that sat
    in the resolved node directory; the link itself is what must be refused."""
    node = _run(tmp_path)
    other = _run(tmp_path, "other")
    (other / "eval.log").write_text("OTHER RUN'S PRIVATE OUTPUT\n")
    (node / "eval.log").symlink_to(other / "eval.log")
    (node / "setup.log").symlink_to(node / "eval.log")        # even a link inside the node dir
    response = _answer(TestClient(make_app(tmp_path)), "/api/runs/demo/nodes/0/logs")
    assert response.status_code == 200, response.text
    assert "OTHER RUN" not in response.text
    assert response.json()["eval"] == "" and response.json()["setup"] == ""


def test_node_logs_refuses_a_node_directory_that_is_a_link(tmp_path):
    """The directory is candidate-reachable too: `nodes/node_0 -> ../../other/nodes/node_0` must
    not turn this run's log panel into a window on another run's workdir."""
    _run(tmp_path)
    other = _run(tmp_path, "other")
    (other / "eval.log").write_text("OTHER RUN'S PRIVATE OUTPUT\n")
    node = tmp_path / "demo" / "nodes" / "node_0"
    node.rmdir()
    node.symlink_to(other, target_is_directory=True)
    response = _answer(TestClient(make_app(tmp_path)), "/api/runs/demo/nodes/0/logs")
    assert response.status_code == 200, response.text
    assert "OTHER RUN" not in response.text


def test_node_metrics_answers_past_a_planted_receipt_fifo(tmp_path):
    """MUTATION: restore `.read_text()` in `metrics_attempt_receipt` -> never answers."""
    node = _run(tmp_path)
    fifo = node / METRICS_ATTEMPT_FILE
    os.mkfifo(fifo)
    response = _answer(TestClient(make_app(tmp_path)), "/api/runs/demo/nodes/0/metrics",
                       fifos=[fifo])
    assert response.status_code == 200, response.text
    assert response.json()["metrics"] == {}


# ------------------------------------------------------------------ the TensorBoard discovery walk

def _event_file(directory: Path, name: str = "events.out.tfevents.1.host") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"\x00" * 16)
    return path


def test_event_discovery_never_follows_a_link_loop_or_leaves_the_node(tmp_path):
    """The reviewer's two shapes: `a -> .`/`b -> .` (a loop a recursive glob walks to the path
    limit) and `link -> ../other_run` (another run's event file served as this node's curves).
    MUTATION: restore the recursive `glob` -> the other run's file is returned."""
    from looplab.serve.metrics_adapters import _event_files

    node = tmp_path / "node"
    own = _event_file(node / "lightning_logs" / "version_0")
    (node / "a").symlink_to(node, target_is_directory=True)
    (node / "b").symlink_to(node, target_is_directory=True)
    foreign = _event_file(tmp_path / "other_run" / "tb")
    (node / "link").symlink_to(tmp_path / "other_run", target_is_directory=True)
    box: dict = {}
    # In a bounded thread: two loop links make a link-following walk EXPONENTIAL in depth, so the
    # defect this pins is a walk that does not come back — which must fail here, not hang the suite.
    walker = threading.Thread(target=lambda: box.setdefault("found", _event_files(str(node))),
                              daemon=True)
    walker.start()
    walker.join(_ANSWER_WITHIN_S + 3.0)
    assert not walker.is_alive(), "event discovery is still walking a link loop"
    found = box["found"]
    assert found == [str(own)], found
    assert str(foreign) not in found


def test_event_discovery_skips_a_directory_holding_any_non_regular_event_entry(tmp_path):
    """The accumulator opens EVERY `*tfevents*` name in a directory it is handed, by path, blocking.
    A directory whose event entries are not all regular files is therefore never handed to it."""
    from looplab.serve.metrics_adapters import _event_files

    node = tmp_path / "node"
    clean = _event_file(node / "train")
    _event_file(node / "val")
    os.mkfifo(node / "val" / "events.out.tfevents.2.host")
    _event_file(node / "linked")
    (node / "linked" / "x.tfevents.y").symlink_to(clean)
    assert _event_files(str(node)) == [str(clean)]


def test_event_discovery_is_bounded_by_an_entry_cap(tmp_path, monkeypatch):
    from looplab.serve import metrics_adapters

    node = tmp_path / "node"
    for index in range(30):
        (node / f"d{index:02d}").mkdir(parents=True)
    _event_file(node / "zz_last")
    monkeypatch.setattr(metrics_adapters, "_EVENT_WALK_ENTRY_CAP", 10)
    assert metrics_adapters._event_files(str(node)) == [], "the walk ran past its entry cap"


def test_event_discovery_refuses_a_node_directory_that_is_itself_a_link(tmp_path):
    from looplab.serve.metrics_adapters import _event_files

    real = tmp_path / "other_run_node"
    _event_file(real / "tb")
    link = tmp_path / "node"
    link.symlink_to(real, target_is_directory=True)
    assert _event_files(str(link)) == []
    assert stat.S_ISLNK(os.lstat(link).st_mode)
