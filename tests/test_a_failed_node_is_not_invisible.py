"""Every `node_failed` in the corpus is wrapped in a crash-atomic packet, and every naive reader missed it.

MEASURED 2026-09-03 over the 80 run logs in the probe corpus: 29,571 physical rows, 11 of them
packets, in 11 different runs, each holding exactly one `node_failed` and one `pause` — so the
count of node failures visible to a reader that keys on `type` is **zero out of eleven**. Six of
the eight tools under `benchmarks/` that read `events.jsonl` were in that state, and so was the
sweep line that has been printing `fails=[]`.

The packet has two spellings: the corpus writes `"type": ["__looplab_event_batch_v1__"]`, a
one-element list, and the engine's own tests exercise the bare string. Both are pinned below,
because handling one and not the other is how this comes back.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import events_read  # noqa: E402
import probe_summary  # noqa: E402

SENT = events_read.SENTINEL


def _log(tmp_path: Path, rows) -> Path:
    p = tmp_path / "events.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows) + '{"torn": ', encoding="utf-8")
    return p


def _packet(kind, inner):
    return {"v": 1, "seq": 9, "ts": 3.0, "type": kind,
            "data": {"schema": 1, "count": len(inner), "events": inner}}


FAIL = {"v": 1, "seq": 8, "ts": 3.0, "type": "node_failed",
        "data": {"node_id": 2, "error": "(developer stuck: wrote nothing)"}}
PAUSE = {"v": 1, "seq": 9, "ts": 3.0, "type": "pause", "data": {"node_id": 2}}
PLAIN = {"v": 1, "seq": 1, "ts": 1.0, "type": "run_started", "data": {}}


def test_both_spellings_of_the_packet_are_expanded(tmp_path):
    for kind in ([SENT], SENT):
        got = events_read.read(_log(tmp_path, [PLAIN, _packet(kind, [FAIL, PAUSE])]))
        types = [r.get("type") for r in got]
        assert types == ["run_started", "node_failed", "pause"], (kind, types)


def test_a_row_that_only_mentions_the_sentinel_is_kept_whole(tmp_path):
    """A packet is a container plus contents. Named-but-empty is a row, and swallowing it loses it."""
    impostor = {"v": 1, "seq": 2, "ts": 2.0, "type": SENT, "data": {"note": "no events here"}}
    got = events_read.read(_log(tmp_path, [PLAIN, impostor]))
    assert len(got) == 2 and got[1] is not None
    assert got[1]["data"] == {"note": "no events here"}


def test_the_torn_last_line_is_skipped_not_fatal(tmp_path):
    got = events_read.read(_log(tmp_path, [PLAIN]))
    assert [r["type"] for r in got] == ["run_started"]


def test_a_missing_file_is_empty_rather_than_an_exception(tmp_path):
    assert events_read.read(tmp_path / "nope.jsonl") == []


def test_the_summary_reports_the_node_it_lost(tmp_path):
    """The point of all of the above: a run that lost a node must not summarise as a clean run."""
    run = tmp_path / "runs" / "t" / "run"
    run.mkdir(parents=True)
    rows = [PLAIN,
            {"v": 1, "seq": 2, "ts": 2.0, "type": "node_evaluated",
             "data": {"node_id": 0, "metric": 12.5}},
            _packet([SENT], [FAIL, PAUSE])]
    (run / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (run / "spans.jsonl").write_text("", encoding="utf-8")

    got = probe_summary.summarise(run)
    assert got is not None
    assert got["node_failed"] == [(2, "(developer stuck: wrote nothing)")], got["node_failed"]
