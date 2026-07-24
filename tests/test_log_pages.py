from __future__ import annotations

import os
from pathlib import Path

import orjson
import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.serve import log_pages as log_pages_module  # noqa: E402
from looplab.serve.log_pages import EventLogPager  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402


OWNER = {"X-LoopLab-Token": "owner-secret"}


def _event(seq: int, *, run_id: str = "demo", payload: str = "") -> dict:
    return {
        "v": 1,
        "seq": seq,
        "ts": float(seq),
        "type": "run_started" if seq == 0 else "timeline_note",
        "data": ({"run_id": run_id, "task_id": "t", "goal": "g", "direction": "min"}
                 if seq == 0 else {"n": seq, "payload": payload}),
        "trace_id": None,
        "span_id": None,
    }


def _write_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(orjson.dumps(event) + b"\n" for event in events))


def _seed_run(root: Path, count: int = 8) -> Path:
    path = root / "demo" / "events.jsonl"
    _write_events(path, [_event(seq) for seq in range(count)])
    return path


def test_tail_older_newer_and_generation_fence(tmp_path):
    path = _seed_run(tmp_path, 12)
    pager = EventLogPager()

    tail = pager.page(path, direction="tail", limit=4)
    assert [event["seq"] for event in tail["events"]] == [8, 9, 10, 11]
    assert tail["generation"] and len(tail["generation"]) == 64
    assert tail["has_more"] == {"older": True, "newer": False}
    assert tail["cursors"]["newer"]  # live-edge cursor is retained for future delta polling
    assert tail["total_events"] == 12
    assert tail["range"] == {
        "start_index": 8, "end_index": 12, "first_seq": 8, "last_seq": 11}

    older = pager.page(
        path, direction="older", limit=4, cursor=tail["cursors"]["older"],
        generation=tail["generation"])
    assert [event["seq"] for event in older["events"]] == [4, 5, 6, 7]

    with open(path, "ab") as handle:
        handle.write(orjson.dumps(_event(12)) + b"\n")
        handle.write(orjson.dumps(_event(13)) + b"\n")
    newer = pager.page(
        path, direction="newer", limit=4, cursor=tail["cursors"]["newer"],
        generation=tail["generation"])
    assert [event["seq"] for event in newer["events"]] == [12, 13]
    assert newer["has_more"]["newer"] is False


def test_batch_members_are_logical_rows_for_pages_cursors_and_anchors(tmp_path):
    path = tmp_path / "batched" / "events.jsonl"
    store = EventStore(path)
    store.append("run_started", {
        "run_id": "batched", "task_id": "t", "goal": "g", "direction": "min"})
    store.append_many([
        ("timeline_note", {"n": 1}),
        ("timeline_note", {"n": 2}),
        ("timeline_note", {"n": 3}),
    ])
    assert len(path.read_bytes().splitlines()) == 2
    pager = EventLogPager()

    tail = pager.page(path, direction="tail", limit=2)
    assert [event["seq"] for event in tail["events"]] == [2, 3]
    assert all(event["type"] != "__looplab_event_batch_v1__" for event in tail["events"])
    assert tail["total_events"] == 4
    assert tail["range"]["start_index"] == 2

    older = pager.page(
        path, direction="older", limit=2, cursor=tail["cursors"]["older"],
        generation=tail["generation"])
    assert [event["seq"] for event in older["events"]] == [0, 1]
    newer = pager.page(
        path, direction="newer", limit=2, cursor=older["cursors"]["newer"],
        generation=tail["generation"])
    assert [event["seq"] for event in newer["events"]] == [2, 3]
    around = pager.page(path, direction="around", anchor_seq=1, limit=3)
    assert around["matched_seq"] == 1
    assert [event["seq"] for event in around["events"]] == [0, 1, 2]


def test_reset_invalidates_generation_and_cursor(tmp_path):
    path = _seed_run(tmp_path, 5)
    pager = EventLogPager()
    first = pager.page(path, direction="tail", limit=3)

    replacement = path.with_suffix(".replacement")
    rows = [_event(0, run_id="replacement"), _event(1, run_id="replacement")]
    rows[0]["ts"] = 987654.0
    _write_events(replacement, rows)
    os.replace(replacement, path)

    with pytest.raises(HTTPException) as caught:
        pager.page(path, direction="older", cursor=first["cursors"]["older"])
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "run_generation_changed"

    with pytest.raises(HTTPException) as caught_generation:
        pager.page(path, direction="tail", generation=first["generation"])
    assert caught_generation.value.status_code == 409
    assert caught_generation.value.detail["actual_generation"] != first["generation"]


def test_same_generation_atomic_and_same_size_rewrites_invalidate_cursor_revision(tmp_path):
    path = tmp_path / "demo" / "events.jsonl"
    original = [_event(0), *[_event(seq, payload="a") for seq in range(1, 7)]]
    _write_events(path, original)
    pager = EventLogPager()
    first = pager.page(path, direction="tail", limit=2)

    # Atomic repair can preserve the canonical first event, row count, seqs, and byte length while
    # changing later evidence. The old boundary must not be reinterpreted against that new file.
    replacement = path.with_suffix(".replacement")
    repaired = [_event(0), *[_event(seq, payload="b") for seq in range(1, 7)]]
    _write_events(replacement, repaired)
    assert replacement.stat().st_size == path.stat().st_size
    os.replace(replacement, path)

    with pytest.raises(HTTPException) as atomic_conflict:
        pager.page(path, direction="older", cursor=first["cursors"]["older"])
    assert atomic_conflict.value.status_code == 409
    assert atomic_conflict.value.detail["actual_generation"] == first["generation"]

    fresh = pager.page(path, direction="tail", limit=2, generation=first["generation"])
    assert [event["data"]["payload"] for event in fresh["events"]] == ["b", "b"]
    assert fresh["cursors"] != first["cursors"]

    # A same-identity, same-size rewrite is ambiguous too. Force a distinct metadata signature so
    # coarse/fast filesystems exercise the conservative rebuild path deterministically.
    before = path.stat()
    rewritten = path.read_bytes().replace(b'"payload":"b"', b'"payload":"c"', 1)
    assert len(rewritten) == before.st_size
    with open(path, "r+b") as handle:
        handle.write(rewritten)
        handle.flush()
        os.fsync(handle.fileno())
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
    after = path.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert after.st_size == before.st_size

    with pytest.raises(HTTPException) as in_place_conflict:
        pager.page(path, direction="older", cursor=fresh["cursors"]["older"])
    assert in_place_conflict.value.status_code == 409
    assert in_place_conflict.value.detail["actual_generation"] == first["generation"]


def test_same_identity_shrink_invalidates_cursor_even_when_boundary_remains_in_range(tmp_path):
    path = _seed_run(tmp_path, 8)
    pager = EventLogPager()
    first = pager.page(path, direction="tail", limit=3)
    before = path.stat()

    # The old `older` boundary is row 5. Keep six rows so it remains numerically in range; only the
    # index revision can distinguish the rebuilt file from the snapshot that issued the cursor.
    shortened = b"".join(path.read_bytes().splitlines(keepends=True)[:6])
    with open(path, "r+b") as handle:
        handle.write(shortened)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
    after = path.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert after.st_size < before.st_size

    with pytest.raises(HTTPException) as conflict:
        pager.page(path, direction="older", cursor=first["cursors"]["older"])
    assert conflict.value.status_code == 409
    assert conflict.value.detail["actual_generation"] == first["generation"]

    fresh = pager.page(path, direction="tail", limit=3, generation=first["generation"])
    assert [event["seq"] for event in fresh["events"]] == [3, 4, 5]


def test_torn_or_invalid_tail_is_excluded_and_can_be_completed(tmp_path):
    path = _seed_run(tmp_path, 2)
    partial = orjson.dumps(_event(2))
    split = len(partial) - 4
    with open(path, "ab") as handle:
        handle.write(partial[:split])
    pager = EventLogPager()

    torn = pager.page(path, direction="tail")
    assert [event["seq"] for event in torn["events"]] == [0, 1]
    assert torn["torn_tail"] is True
    with open(path, "ab") as handle:
        handle.write(partial[split:] + b"\n")
    healed = pager.page(
        path, direction="newer", cursor=torn["cursors"]["newer"],
        generation=torn["generation"])
    assert [event["seq"] for event in healed["events"]] == [2]
    assert healed["torn_tail"] is False

    # A complete JSON object that is not an Event stops the same recoverable prefix as EventStore.
    with open(path, "ab") as handle:
        handle.write(b'{"seq":3,"data":{}}\n')
        handle.write(orjson.dumps(_event(4)) + b"\n")
    stopped = pager.page(path, direction="tail")
    assert [event["seq"] for event in stopped["events"]] == [0, 1, 2]
    assert stopped["torn_tail"] is True


def test_strict_row_and_byte_caps_with_oversize_progress(tmp_path):
    path = tmp_path / "events.jsonl"
    _write_events(path, [_event(0), _event(1, payload="x" * 20_000), _event(2)])
    page = EventLogPager().page(path, direction="tail", limit=2, byte_limit=1024)

    assert len(page["events"]) == 2
    assert page["events"][0]["seq"] == 1
    assert page["events"][0]["_log_page"] == {
        "truncated": True,
        "raw_bytes": len(orjson.dumps(_event(1, payload="x" * 20_000))) + 1,
        "reason": "row_exceeds_byte_limit",
    }
    assert page["events"][1]["seq"] == 2
    assert page["bytes"] == sum(len(orjson.dumps(event)) for event in page["events"])
    assert page["bytes"] <= 1024
    assert len(page["events"]) <= page["limit"]

    huge_type = _event(1)
    huge_type["type"] = "T" * 20_000
    _write_events(path, [_event(0), huge_type, _event(2)])
    typed = EventLogPager().page(path, direction="tail", limit=2, byte_limit=1024)
    assert [event["seq"] for event in typed["events"]] == [1, 2]
    assert typed["events"][0]["_log_page"]["truncated"] is True
    assert len(typed["events"][0]["type"]) == 256
    assert typed["bytes"] <= 1024


def test_source_line_read_has_an_independent_hard_memory_cap(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    _write_events(path, [_event(0), _event(1, payload="x" * 5_000), _event(2)])
    monkeypatch.setattr(log_pages_module, "MAX_SOURCE_ROW_BYTES", 1024)
    page = EventLogPager().page(path, direction="tail")
    assert [event["seq"] for event in page["events"]] == [0]
    assert page["torn_tail"] is True
    assert page["source_tail_limited"] is True


def test_seq_gaps_are_preserved_and_around_uses_nearest_existing_event(tmp_path):
    path = tmp_path / "events.jsonl"
    _write_events(path, [_event(seq) for seq in (0, 10, 20, 40)])
    page = EventLogPager().page(path, direction="around", anchor_seq=14, limit=3)
    assert page["matched_seq"] == 10
    assert [event["seq"] for event in page["events"]] == [0, 10, 20]

    with open(path, "ab") as handle:
        handle.write(orjson.dumps(_event(40)) + b"\n")  # duplicate seq ends the canonical prefix
        handle.write(orjson.dumps(_event(50)) + b"\n")
    stopped = EventLogPager().page(path, direction="tail")
    assert [event["seq"] for event in stopped["events"]] == [0, 10, 20, 40]
    assert stopped["torn_tail"] is True

    with pytest.raises(HTTPException) as negative:
        EventLogPager().page(path, direction="around", anchor_seq=-1)
    assert negative.value.status_code == 400


def test_around_50k_uses_cached_seq_index_and_append_only_delta(tmp_path, monkeypatch):
    """After one index build, repeated scrubber anchors parse zero rows and bisect cached seqs.

    Appending one event parses exactly that one row; this prevents an 11fps scrubber from moving the
    old browser-wide O(50k) scan into the server.
    """
    path = tmp_path / "events.jsonl"
    _write_events(path, [_event(seq) for seq in range(50_000)])
    pager = EventLogPager()
    tail = pager.page(path, direction="tail", limit=100)
    assert [event["seq"] for event in tail["events"]] == list(range(49_900, 50_000))

    calls = 0
    original = log_pages_module._row_from

    def counted(raw, start, end):
        nonlocal calls
        calls += 1
        return original(raw, start, end)

    monkeypatch.setattr(log_pages_module, "_row_from", counted)
    around = pager.page(
        path, direction="around", anchor_seq=25_001, limit=101,
        generation=tail["generation"])
    assert around["matched_seq"] == 25_001
    assert 25_001 in [event["seq"] for event in around["events"]]
    assert len(around["events"]) <= 101
    assert calls == 0

    with open(path, "ab") as handle:
        handle.write(orjson.dumps(_event(50_000)) + b"\n")
    delta = pager.page(
        path, direction="newer", cursor=tail["cursors"]["newer"],
        generation=tail["generation"])
    assert [event["seq"] for event in delta["events"]] == [50_000]
    assert calls == 1


def test_index_cache_is_lru_bounded_across_runs(tmp_path):
    pager = EventLogPager(max_indexed_runs=2)
    paths = []
    first_page = None
    for run in range(3):
        path = tmp_path / f"run-{run}" / "events.jsonl"
        _write_events(path, [_event(0, run_id=f"run-{run}")])
        paths.append(path)
        page = pager.page(path)
        if run == 0:
            first_page = page
    assert len(pager._indexes) == 2
    assert str(paths[0]) not in pager._indexes

    # A cursor is meaningful only while its concrete index revision exists. Rebuilding after LRU
    # eviction returns the ordinary self-healing generation conflict even though the file is intact.
    with pytest.raises(HTTPException) as conflict:
        pager.page(paths[0], direction="newer", cursor=first_page["cursors"]["newer"])
    assert conflict.value.status_code == 409
    assert conflict.value.detail["actual_generation"] == first_page["generation"]
    recovered = pager.page(paths[0], direction="tail", generation=first_page["generation"])
    assert [event["seq"] for event in recovered["events"]] == [0]


def test_log_page_route_validation_auth_no_store_and_review_denial(tmp_path, monkeypatch):
    _seed_run(tmp_path, 5)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    client = TestClient(make_app(tmp_path))

    denied = client.get("/api/runs/demo/log-page")
    assert denied.status_code == 401
    assert denied.headers["Cache-Control"] == "no-store"
    assert "X-LoopLab-Token" in denied.headers["Vary"]

    allowed = client.get("/api/runs/demo/log-page", headers=OWNER)
    assert allowed.status_code == 200
    assert allowed.headers["Cache-Control"] == "no-store"
    assert [event["seq"] for event in allowed.json()["events"]] == list(range(5))
    legacy = client.get("/api/runs/demo/log", headers=OWNER)
    assert legacy.status_code == 200
    assert [event["seq"] for event in legacy.json()] == list(range(5))

    assert client.get(
        "/api/runs/demo/log-page", headers=OWNER,
        params={"direction": "older"}).status_code == 400
    assert client.get(
        "/api/runs/demo/log-page", headers=OWNER,
        params={"direction": "around"}).status_code == 400
    assert client.get(
        "/api/runs/demo/log-page", headers=OWNER,
        params={"direction": "tail", "limit": 501}).status_code == 422

    created = client.post(
        "/api/runs/demo/reviews", headers=OWNER,
        json={"ttl_seconds": 3600, "include_evidence": True})
    assert created.status_code == 200
    review = {"X-LoopLab-Review": created.json()["token"]}
    forbidden = client.get("/api/runs/demo/log-page", headers=review)
    assert forbidden.status_code == 403
    assert forbidden.headers["Cache-Control"] == "no-store"


def test_log_page_rejects_event_log_symlink_outside_run(tmp_path):
    outside = tmp_path / "outside.jsonl"
    _write_events(outside, [_event(0)])
    rd = tmp_path / "demo"
    rd.mkdir()
    try:
        (rd / "events.jsonl").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    response = TestClient(make_app(tmp_path)).get("/api/runs/demo/log-page")
    assert response.status_code == 404


def test_log_page_explicitly_rejects_symlink_candidate(tmp_path, monkeypatch):
    _seed_run(tmp_path, 1)
    client = TestClient(make_app(tmp_path))
    original = Path.is_symlink

    def looks_like_symlink(path):
        return path.name == "events.jsonl" or original(path)

    monkeypatch.setattr(Path, "is_symlink", looks_like_symlink)
    assert client.get("/api/runs/demo/log-page").status_code == 404


def test_eventstore_fixture_generation_matches_state_contract(tmp_path):
    """The page generation is the exact canonical first-event token used by /state/commands."""
    rd = tmp_path / "demo"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {
        "run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    client = TestClient(make_app(tmp_path))
    assert (client.get("/api/runs/demo/log-page").json()["generation"]
            == client.get("/api/runs/demo/state").json()["generation"])


def test_legacy_log_route_is_row_bounded(tmp_path, monkeypatch):
    """The legacy /log route returns the oldest-first envelopes past `since`, but bounded to
    _LEGACY_LOG_MAX_ROWS so a pathological multi-GB events.jsonl can't be materialized whole into
    one response (OOM). An incremental caller pages forward by advancing `since`."""
    import looplab.serve.routers.runs as runs_mod
    monkeypatch.setattr(runs_mod, "_LEGACY_LOG_MAX_ROWS", 4)   # tiny cap so the bound is observable
    _seed_run(tmp_path, 10)                                    # seq 0..9
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    client = TestClient(make_app(tmp_path))

    first = client.get("/api/runs/demo/log", headers=OWNER)
    assert first.status_code == 200
    assert [event["seq"] for event in first.json()] == [0, 1, 2, 3]   # capped, oldest-first
    # advancing `since` pages forward — no event is permanently lost across the cap
    second = client.get("/api/runs/demo/log", headers=OWNER, params={"since": 3})
    assert [event["seq"] for event in second.json()] == [4, 5, 6, 7]


def test_legacy_log_route_rejects_symlinked_events_log(tmp_path, monkeypatch):
    """Defence-in-depth parity with /log-page: a symlinked events.jsonl (e.g. inside an imported run
    bundle) that could escape the run dir is rejected with 404, not followed."""
    _seed_run(tmp_path, 5)
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    client = TestClient(make_app(tmp_path))
    original = Path.is_symlink

    def looks_like_symlink(path):
        return path.name == "events.jsonl" or original(path)

    monkeypatch.setattr(Path, "is_symlink", looks_like_symlink)
    assert client.get("/api/runs/demo/log", headers=OWNER).status_code == 404


def test_legacy_log_route_is_byte_bounded(tmp_path, monkeypatch):
    """A row cap is not a memory cap: /log also bounds the aggregate serialized bytes, so a handful of
    very large events can't be materialized whole into one response (OOM)."""
    import looplab.serve.routers.runs as runs_mod
    monkeypatch.setattr(runs_mod, "_LEGACY_LOG_MAX_ROWS", 10_000)    # rows are not the limit here
    monkeypatch.setattr(runs_mod, "_LEGACY_LOG_MAX_BYTES", 2_000)    # tiny byte budget forces the cut
    big = "x" * 400
    _write_events(tmp_path / "demo" / "events.jsonl",
                  [_event(0)] + [_event(s, payload=big) for s in range(1, 20)])
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    client = TestClient(make_app(tmp_path))
    got = client.get("/api/runs/demo/log", headers=OWNER)
    assert got.status_code == 200
    seqs = [event["seq"] for event in got.json()]
    assert seqs == list(range(len(seqs)))           # oldest-first, contiguous prefix
    assert 0 < len(seqs) < 20                        # byte budget stopped it before all 20 rows
