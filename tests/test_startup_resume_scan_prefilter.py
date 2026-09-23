"""The server's startup resume scan folds only the logs that CAN hold an unserved resume.

Review 2026-09-22, SRV1-13. `engine_proc.py::install_resume_reconcile_hooks` recovers durable resume
intents before the server answers anything — synchronously, on purpose (its own note: "losing an
unserved resume is worse than a slow start"). But it built an `EventStore` (a full parse) and FOLDED
the complete log of EVERY run under the root to ask `resume_pending()`, so a large root delayed
uvicorn's bind by the sum of every run's fold.

`resume_pending()` is `last_resume_request_seq > last_resume_served_seq`, both zero by default, and
exactly two folded types raise the request seq: `resume_requested` and `restart`. A log whose bytes
contain neither type name cannot answer True, so a bounded streaming byte scan decides which logs
are worth the fold. Counted, never timed.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from looplab.events.eventstore import EventStore
from looplab.serve import engine_proc as ep
from looplab.serve.server import make_app


def _run(root, name, *, resume: bool):
    rd = root / name
    rd.mkdir()
    (rd / "task.snapshot.json").write_text("{}", encoding="utf-8")
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": name, "task_id": "t", "direction": "min"})
    for nid in range(3):
        store.append("node_created", {"node_id": nid, "operator": "draft",
                                      "idea": {"operator": "draft", "params": {},
                                               "rationale": "a restart of nothing"}})
    if resume:
        store.append("resume_requested", {})
    return rd


def test_startup_folds_only_the_run_that_can_hold_a_resume(tmp_path, monkeypatch):
    """MUTATION: drop the byte pre-filter in `_scan_startup` -> every run under the root is
    parsed and folded before the server answers, and `folded` names all six."""
    import looplab.events.replay as replay
    from looplab.engine import run_lifecycle

    for index in range(5):
        _run(tmp_path, f"quiet{index}", resume=False)
    _run(tmp_path, "resumable", resume=True)

    folded = []
    real_fold = replay.fold

    def _recording_fold(events, *args, **kwargs):
        events = list(events)
        if events:
            folded.append(events[0].data.get("run_id"))
        return real_fold(events, *args, **kwargs)

    spawns = []
    monkeypatch.setattr(replay, "fold", _recording_fold)
    monkeypatch.setattr(run_lifecycle, "RESUME_RECONCILE_GRACE_S", 0.0)
    monkeypatch.setattr(ep, "_engine_alive", lambda _rd: False)
    monkeypatch.setattr(ep, "_spawn_engine", lambda *a, **k: spawns.append((a, k)))

    with TestClient(make_app(tmp_path)) as client:
        assert client.get("/api/health").status_code == 200

    assert set(folded) == {"resumable"}, folded
    # ...and the one that could hold a resume still had it recovered before the server answered.
    assert len(spawns) == 1 and "resume" in spawns[0][0][0]


def test_the_filter_answers_maybe_for_every_shape_a_marker_can_take(tmp_path, monkeypatch):
    """The filter may only ever say "cannot" when the bytes PROVE it: a marker split across two read
    chunks, a `restart` rather than a `resume_requested`, a type name spelled with a JSON escape (a
    writer this tree does not have, but JSON allows), and a log the scan cannot read all answer
    "maybe" — the full fold decides those, exactly as before."""
    monkeypatch.setattr(ep, "_RESUME_SCAN_CHUNK_BYTES", 16)
    log = tmp_path / "events.jsonl"

    log.write_bytes(b'{"seq":0,"type":"run_started","data":{"note":"restarted twice"}}\n')
    assert ep._log_may_hold_resume_intent(log) is False

    log.write_bytes(b"x" * 13 + b'{"seq":1,"type":"resume_requested","data":{}}\n')
    assert ep._log_may_hold_resume_intent(log) is True        # straddles the 16-byte chunks

    log.write_bytes(b'{"seq":1,"type": "restart","data":{}}\n')
    assert ep._log_may_hold_resume_intent(log) is True

    log.write_bytes(b'{"seq":1,"type":"\\u0072estart","data":{}}\n')
    assert ep._log_may_hold_resume_intent(log) is True

    assert ep._log_may_hold_resume_intent(tmp_path / "missing" / "events.jsonl") is True


@pytest.mark.parametrize("event", ["resume_requested", "restart"])
def test_the_markers_are_the_fold_s_own_type_names(event):
    """The filter's vocabulary is DERIVED from the event-type constants the fold registers for
    those two handlers, so a renamed type cannot leave the filter matching a name nothing writes."""
    from looplab.events import types as event_types

    assert event in {event_types.EV_RESUME_REQUESTED, event_types.EV_RESTART}
    assert f'"{event}"'.encode() in ep._RESUME_INTENT_MARKERS
