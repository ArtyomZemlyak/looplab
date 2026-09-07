"""A server that pins nine-day-old code must SAY SO, because its answers look healthy.

THE CASE THIS DRIVES, measured on 2026-09-03. The operator reported the question ladder showing
twelve questions with nothing attached and every row "not measured yet". Four layers were checked
and all four were correct: the fold on disk gave 17 `parent_card_id` edges and 7 questions with
children; `public_cards()` run in-process over that same log published all 17; the shipped
`ui/dist` bundle contained the fixed reader (`child_concept_tags` present, `child_card_ids` absent);
and the lattice model, driven in node over the real wire, drew experiments under 10 of 15 rows.

The payload the RUNNING SERVER returned carried `parent_card_id` on 0 of 34 cards, `child_rollup` on
0 of 12 questions, and no `child_concept_tags` key at all — a 30-field DTO against the tree's 55.
That process had been up 9 days 5 hours, since before the fold learned to keep the edge. Restarting
it restored 17/7/7 in one step, with no code change.

So the defect was never in a layer that a unit test can reach: the code under test was RIGHT, and the
process serving it was OLD. The only guard that could have caught it is one that makes the server
publish its own code identity, which is what these tests hold in place. Every assertion below was
mutation-checked — each has an input that makes it fail.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from looplab.serve import code_freshness as cf


def _tree(root: Path, names=("a.py", "sub/b.py")) -> Path:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n")
    return root


def _touch(path: Path, text: str = "x = 2\n") -> None:
    """Rewrite with an mtime that is provably different, without sleeping for a filesystem tick."""
    path.write_text(text)
    stat = path.stat()
    import os
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def test_a_tree_that_has_not_moved_is_not_stale(tmp_path):
    boot = cf.snapshot(_tree(tmp_path))
    report = cf.code_freshness(tmp_path, boot=boot)
    assert report["stale"] is False
    assert report["changed"] == []
    assert report["changed_count"] == 0
    assert report["files_at_boot"] == report["files_now"] == 2


def test_a_rewritten_module_makes_the_process_stale(tmp_path):
    """The shipped case: a merge rewrites `events/card_ledger.py` under a live server."""
    boot = cf.snapshot(_tree(tmp_path))
    _touch(tmp_path / "sub" / "b.py")
    report = cf.code_freshness(tmp_path, boot=boot)
    assert report["stale"] is True
    assert report["changed"] == [str(Path("sub") / "b.py")]
    assert report["changed_count"] == 1


def test_a_module_added_after_boot_counts(tmp_path):
    """A merge that only ADDS a file (a new event type, a new projection) still means restart."""
    boot = cf.snapshot(_tree(tmp_path))
    (tmp_path / "c.py").write_text("y = 1\n")
    report = cf.code_freshness(tmp_path, boot=boot)
    assert report["stale"] is True
    assert report["changed"] == ["c.py"]
    assert report["files_now"] == report["files_at_boot"] + 1


def test_a_module_deleted_after_boot_counts(tmp_path):
    boot = cf.snapshot(_tree(tmp_path))
    (tmp_path / "a.py").unlink()
    report = cf.code_freshness(tmp_path, boot=boot)
    assert report["stale"] is True
    assert report["changed"] == ["a.py"]


def test_a_non_python_file_is_not_code(tmp_path):
    """Runs, logs and notes live under the tree too. A written event log is not a stale server."""
    boot = cf.snapshot(_tree(tmp_path))
    (tmp_path / "events.jsonl").write_text('{"seq": 1}\n')
    assert cf.code_freshness(tmp_path, boot=boot)["stale"] is False


def test_pycache_cannot_make_a_server_report_itself_stale(tmp_path):
    """`__pycache__` mtimes move on IMPORT — including this process's own. Tracking them would make
    every server permanently stale and the notice worthless."""
    _tree(tmp_path)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "a.cpython-312.py").write_text("compiled = 1\n")
    boot = cf.snapshot(tmp_path)
    _touch(cache / "a.cpython-312.py")
    (cache / "later.py").write_text("compiled = 2\n")
    report = cf.code_freshness(tmp_path, boot=boot)
    assert report["stale"] is False
    assert report["files_at_boot"] == 2                    # a.py and sub/b.py only


def test_the_count_is_exact_while_the_list_is_a_bounded_sample(tmp_path):
    """The notice must not become the payload. The COUNT is what the operator acts on."""
    names = tuple("m%02d.py" % i for i in range(cf.MAX_REPORTED_CHANGES + 5))
    boot = cf.snapshot(_tree(tmp_path, names))
    for name in names:
        _touch(tmp_path / name)
    report = cf.code_freshness(tmp_path, boot=boot)
    assert report["changed_count"] == len(names)
    assert len(report["changed"]) == cf.MAX_REPORTED_CHANGES
    assert report["changed_truncated"] is True


def test_a_walk_that_hit_the_bound_does_not_claim_completeness(tmp_path, monkeypatch):
    """`stale: false` off a truncated walk means 'nothing changed in the part I looked at'."""
    monkeypatch.setattr(cf, "MAX_TRACKED_FILES", 2)
    boot = cf.snapshot(_tree(tmp_path, ("a.py", "b.py", "c.py")))
    report = cf.code_freshness(tmp_path, boot=boot)
    assert report["complete"] is False
    assert report["files_now"] == 2


def test_the_cache_answers_within_its_window_and_re_reads_after_it(tmp_path, monkeypatch):
    """A 2.5 s poll per open browser must not walk the tree every tick — and must not go blind.

    The clock is a settable NOW rather than a fixed sequence of ticks: how many times the
    implementation reads the clock is not the property, and pinning it made the single-flight
    re-check (one extra read per miss) look like a regression.
    """
    calls = []
    now = [0.0]
    monkeypatch.setattr(cf, "code_freshness", lambda: calls.append(1) or {"stale": False})
    monkeypatch.setattr(cf, "_cached", None)
    tick = lambda: now[0]

    cf.cached_code_freshness(clock=tick)                  # miss: reads, stamps at 0.0
    now[0] = 1.0
    cf.cached_code_freshness(clock=tick)                  # inside the window: hit
    assert len(calls) == 1
    now[0] = cf.CACHE_SECONDS + 1.0
    cf.cached_code_freshness(clock=tick)                  # past the window: reads again
    assert len(calls) == 2


def test_ONE_WALK_PER_WINDOW_even_when_every_reader_misses_together(monkeypatch):
    """THE THUNDERING HERD. The walk ran with `_cache_lock` RELEASED — correctly, since holding it
    would serialize every reader — but with nothing else in its place, so at each 30-second boundary
    every open SSE stream and every polling tab walked the tree simultaneously. On this deployment
    the package sits on the same network mount as the run dirs (~0.4 ms per present-file `lstat`,
    ~135 ms per walk), so that is K x the cost of the one answer they all end up sharing.

    MUTATION: drop `_compute_lock` -> `calls` is 8, one per thread.
    """
    import threading

    started = threading.Barrier(8)
    calls: list[int] = []

    def _slow_walk():
        calls.append(1)
        time.sleep(0.05)                                  # long enough for the others to arrive
        return {"stale": False}

    monkeypatch.setattr(cf, "code_freshness", _slow_walk)
    monkeypatch.setattr(cf, "_cached", None)

    got: list[dict] = []

    def _reader():
        started.wait()
        got.append(cf.cached_code_freshness())

    threads = [threading.Thread(target=_reader) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1, f"eight simultaneous misses must produce one walk, got {len(calls)}"
    assert len(got) == 8 and all(g == {"stale": False} for g in got), (
        "and every reader still gets the answer")


def test_the_payload_reads_the_freshness_ONCE(tmp_path, monkeypatch):
    """It is a fact about THIS payload, and `state_payload` asked for it twice on both the hit and
    the miss path — so the `state` mirror and the envelope could disagree, and each ask is a cache
    lookup that becomes a tree walk at the window boundary."""
    import inspect

    from looplab.serve import appstate

    body = inspect.getsource(appstate.AppState.state_payload)
    assert body.count("cached_code_freshness()") == 2, (
        "once per branch (hit and miss), never once per USE — got "
        f"{body.count('cached_code_freshness()')}")
    assert body.count('"server_code": server_code') == 2, "both envelopes reuse that one read"


# --- the shipped HTTP surface ---------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    fastapi = pytest.importorskip("fastapi")              # [ui] extra
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    del fastapi
    return TestClient(make_app(tmp_path)), tmp_path


def _log(rd: Path) -> None:
    rd.mkdir(parents=True, exist_ok=True)
    rows = [{"v": 1, "seq": i, "ts": 1783555825.0 + i, "type": t, "data": {},
             "trace_id": None, "span_id": None}
            for i, t in enumerate(["run_started", "generation_started"])]
    (rd / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_the_state_payload_carries_the_servers_own_code_identity(client):
    api, root = client
    _log(root / "demo")
    body = api.get("/api/runs/demo/state").json()
    assert body["server_code"]["stale"] is False
    # Mirrored into `state` as well: `useRunState` publishes the folded snapshot, not the frame
    # around it, so a receipt left only on the envelope reaches no browser surface.
    assert body["state"]["server_code"]["stale"] is False


def test_the_stamp_survives_the_state_payload_cache(client, monkeypatch):
    """The SSE hot path serves from a file-identity-keyed cache. A stamp that appeared only on the
    MISS would go silent on every frame after the first — which is exactly the window in which a
    server goes stale, since nothing re-folds when the log has not moved."""
    api, root = client
    _log(root / "demo")
    api.get("/api/runs/demo/state")                       # populate the cache
    from looplab.serve import appstate
    monkeypatch.setattr(appstate, "cached_code_freshness",
                        lambda: {"stale": True, "changed_count": 3, "changed": ["a.py"],
                                 "changed_truncated": False, "files_at_boot": 3, "files_now": 3,
                                 "complete": True})
    body = api.get("/api/runs/demo/state").json()         # cache hit: same file identity
    assert body["server_code"]["stale"] is True
    assert body["state"]["server_code"]["changed_count"] == 3
