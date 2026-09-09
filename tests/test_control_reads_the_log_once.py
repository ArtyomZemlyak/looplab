"""The control plane reads the event log ONCE, not once per POST (CODE_REVIEW `eventstore.py:81`).

Two costs were stacked on top of each other and both were invisible:

  * `EventStore.__init__` walked the whole log TWICE — `_scan_last_seq()` (a `read_all()`) to learn
    the tail seq, and then an unconditional `log_divergence(self.path)`, a second `read_bytes()` plus
    a re-decode of every complete line, to seed the corruption receipt;
  * `routers/control.py` built a fresh `EventStore` per `POST /control`, so a session of N control
    appends paid N of those double walks over a log that grew by one record each time.

Both halves are measured here with an ACCOUNTANT rather than a clock. `eventstore._decode_event_
record` is the one function every event-shaped read goes through — `read_all`'s incremental parse
and `log_divergence`'s walk both call it through the module global — so counting its invocations
counts records re-parsed, exactly, and is immune to how fast the machine is. (`iter_event_jsonl`
deliberately calls the PUBLIC `decode_event_record`, so `AppState.events` is not counted here; this
file is about the control-plane WRITE path.)

The properties that must not regress while the cost goes down: a mid-file corruption is still seen
at construction with the same detail, and a log we could not READ still refuses rather than reading
as a healthy empty one.
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

from looplab.events import eventstore as eventstore_mod
from looplab.events.eventstore import EventStore
from looplab.serve.server import make_app


def _seed_run(tmp_path, run_id="demo", records: int = 40):
    rd = tmp_path / run_id
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": run_id, "task_id": "t", "goal": "g", "direction": "min"})
    for i in range(records - 1):
        store.append("phase_progress", {"phase": "search", "note": f"row {i}"})
    return rd


class _Accountant:
    """Counts records decoded, and whole-file divergence walks, through the module globals."""

    def __init__(self, monkeypatch):
        self.records = 0
        self.divergence_walks = 0
        real_decode = eventstore_mod._decode_event_record
        real_divergence = eventstore_mod.log_divergence

        def decode(obj, **kw):
            self.records += 1
            return real_decode(obj, **kw)

        def divergence(path):
            self.divergence_walks += 1
            return real_divergence(path)

        monkeypatch.setattr(eventstore_mod, "_decode_event_record", decode)
        monkeypatch.setattr(eventstore_mod, "log_divergence", divergence)

    def reset(self) -> None:
        self.records = 0
        self.divergence_walks = 0


# ---------------------------------------------------------------- one walk, not two


def test_a_healthy_log_is_walked_once_at_construction(tmp_path, monkeypatch):
    """MUTATION: restore the `log_divergence` seed line -> `divergence_walks` is 1 and every record
    is decoded TWICE, which is the second full pass this closed."""
    rd = _seed_run(tmp_path, records=25)
    books = _Accountant(monkeypatch)

    store = EventStore(rd / "events.jsonl")

    assert store.divergence is None
    assert books.divergence_walks == 0, "a healthy log must not pay the exact-detail walk at all"
    assert books.records == 25, "each record is decoded exactly once, by the tail-seq scan"


def test_a_mid_file_corruption_is_still_seen_at_construction(tmp_path):
    """The cheap path may not cost the fail-closed receipt. `read_all` stops at the corrupt line and
    the tail behind it is durable-but-invisible to every fold; the store has to say so."""
    rd = tmp_path / "demo"
    rd.mkdir(parents=True)
    log = rd / "events.jsonl"
    log.write_bytes(
        b'{"v":1,"seq":0,"ts":1.0,"type":"a","data":{}}\n'
        b'{"v":1,"seq":1,"ts":1.0,"type":"b","data":{}}\n'
        b'{corrupt\n'
        b'{"v":1,"seq":2,"ts":1.0,"type":"c","data":{}}\n')

    store = EventStore(log)

    assert store.divergence == {"good_records": 2, "corrupt_line": 3, "dropped_lines": 1}
    assert [e.type for e in store.read_all()] == ["a", "b"]


def test_the_exact_detail_walk_still_runs_on_the_corrupt_branch(tmp_path, monkeypatch):
    """The saving is "a healthy log pays nothing", not "the detail is gone". `corrupt_line` and
    `dropped_lines` count records BEHIND the reader's stop, so only a walk that continues past the
    bad line can produce them."""
    rd = tmp_path / "demo"
    rd.mkdir(parents=True)
    log = rd / "events.jsonl"
    log.write_bytes(
        b'{"v":1,"seq":0,"ts":1.0,"type":"a","data":{}}\n'
        b'{corrupt\n'
        b'{"v":1,"seq":1,"ts":1.0,"type":"b","data":{}}\n'
        b'{"v":1,"seq":2,"ts":1.0,"type":"c","data":{}}\n')
    books = _Accountant(monkeypatch)

    store = EventStore(log)

    assert books.divergence_walks == 1
    assert store.divergence == {"good_records": 1, "corrupt_line": 2, "dropped_lines": 2}


def test_a_torn_final_line_is_not_a_divergence_and_still_costs_nothing(tmp_path, monkeypatch):
    """A final line without a newline is a normal crash mid-append, which `append` heals. It must
    not be promoted to a corruption receipt by the cheaper seeding."""
    rd = tmp_path / "demo"
    rd.mkdir(parents=True)
    log = rd / "events.jsonl"
    log.write_bytes(
        b'{"v":1,"seq":0,"ts":1.0,"type":"a","data":{}}\n'
        b'{"v":1,"seq":1,"ts":1.0,"type":"b","dat')
    books = _Accountant(monkeypatch)

    store = EventStore(log)

    assert store.divergence is None
    assert [e.type for e in store.read_all()] == ["a"]
    assert books.divergence_walks == 0


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX mode bits")
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file anyway")
def test_a_log_we_could_not_read_refuses_instead_of_reading_as_healthy(tmp_path):
    """`read_all` treats an OSError on the region read as "no new bytes", so an unreadable log looks
    exactly like an empty one from the cache alone — and `cli::log_integrity_from` would then print
    `complete: True` for a file nobody read. The dropped second pass used to raise here; it still
    does, from the narrow probe that only a zero-byte read can reach."""
    rd = tmp_path / "demo"
    rd.mkdir(parents=True)
    log = rd / "events.jsonl"
    log.write_bytes(b'{"v":1,"seq":0,"ts":1.0,"type":"a","data":{}}\n')
    log.chmod(0o000)
    try:
        with pytest.raises(OSError):
            EventStore(log)
    finally:
        log.chmod(0o644)


# ---------------------------------------------------------------- one store, not one per POST


def _post(client, run_id="demo", body=None, **kw):
    return client.post(f"/api/runs/{run_id}/control", json=body or {"type": "pause", "data": {}}, **kw)


def test_the_control_route_appends_THROUGH_the_shared_store(tmp_path):
    """The store is the object built to make a tail read O(new bytes); a fresh one per POST throws
    that away before it can pay for itself.

    Held from BEFORE the POST on purpose: a store built after the fact would scan the log and read
    correct either way. Only an append that went through THIS object advances its `_seq` here."""
    rd = _seed_run(tmp_path, records=10)
    app = make_app(tmp_path)
    srv = app.state.looplab
    client = TestClient(app)

    shared = srv.event_store(rd)
    assert shared._seq == 9

    assert _post(client).status_code == 200

    assert srv.event_store(rd) is shared, "the run's reader must survive the request that used it"
    assert shared._seq == 10, "the route appended through some OTHER store"


def test_n_control_posts_do_not_re_parse_the_log_n_times(tmp_path, monkeypatch):
    """THE accountant. MUTATION: put `EventStore(local_rd / "events.jsonl")` back at the append and
    each POST re-decodes the whole log — with a 40-record seed and 12 posts that is ~550 records
    instead of the handful below, and it grows with the square of a session's length."""
    seed = 40
    posts = 12
    rd = _seed_run(tmp_path, records=seed)
    app = make_app(tmp_path)
    client = TestClient(app)
    # Warm the reader with one POST first: the FIRST construction legitimately walks the log once,
    # and that cost is not what this measures.
    assert _post(client).status_code == 200

    books = _Accountant(monkeypatch)
    for _ in range(posts):
        assert _post(client).status_code == 200

    assert books.divergence_walks == 0
    assert books.records < seed, (
        f"{posts} further control appends re-parsed {books.records} records — more than ONE full "
        f"scan of a {seed}-record log, so the reader is being rebuilt per request")


def test_the_reused_store_still_sees_a_log_replaced_underneath_it(tmp_path):
    """The reason a store may cross requests when a folded `RunState` may not: it re-validates
    against disk. A Replay archives `events.jsonl` and starts a new one, and the cached reader must
    rebase on the replacement rather than answer from the generation that is gone."""
    rd = _seed_run(tmp_path, records=6)
    app = make_app(tmp_path)
    srv = app.state.looplab
    client = TestClient(app)
    assert _post(client).status_code == 200
    store = srv.event_store(rd)
    assert len(store.read_all()) == 7

    (rd / "events.jsonl").rename(rd / "events.archived.jsonl")
    fresh = EventStore(rd / "events.jsonl")
    fresh.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})

    assert [e.type for e in store.read_all()] == ["run_started"]
    assert store._seq == 0, "the cached tail seq must rebase, or an old CAS token would still pass"
