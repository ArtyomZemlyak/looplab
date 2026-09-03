"""A repeat of a request body is not a metering fault, and the ledger has to be able to say so.

Measured 2026-09-03: 224 of 6,232 `req_sha`-stamped rows repeat a body ($0.3932), and the repeat's
median latency is 25.1 ms against the original's 4,570.1 ms. Reproduced live through the meter with
two identical requests: 236.4 ms then 17.7 ms, same sha, same 4,012 prompt tokens, same charge. The
gateway serves a cached body and bills it in full, and there is no cached-token field anywhere in
the upstream `usage` to price differently.

Two ways to get this wrong are pinned below: counting the first send as a repeat, and dropping the
pre-§122 rows without saying how many were dropped.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import resent_bodies  # noqa: E402


def _ledger(tmp_path: Path) -> Path:
    rows = [
        # Pre-§122: no req_sha at all. Must be counted as skipped, not as a clean row.
        {"arm": "old", "cost": 1.0, "latency_ms": 500.0},
        # An original and a repeat served 100x faster -- the cache case.
        {"arm": "a", "cost": 0.10, "latency_ms": 5000.0, "req_sha": "aaaa"},
        {"arm": "a", "cost": 0.10, "latency_ms": 50.0, "req_sha": "aaaa"},
        # A repeat that took just as long: a real second generation of the same prompt.
        {"arm": "b", "cost": 0.20, "latency_ms": 4000.0, "req_sha": "bbbb"},
        {"arm": "b", "cost": 0.20, "latency_ms": 3900.0, "req_sha": "bbbb"},
        # A body sent once.
        {"arm": "c", "cost": 0.30, "latency_ms": 1000.0, "req_sha": "cccc"},
        {"arm": "c", "cost": 0.0, "latency_ms": 0.0, "req_sha": ""},   # empty sha is not a sha
    ]
    path = tmp_path / "meter.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n", encoding="utf-8")
    return path


def test_only_the_second_send_is_a_repeat(tmp_path):
    got = resent_bodies.scan(str(_ledger(tmp_path)))
    assert got["stamped"] == 5, "the two unstamped rows leaked into the denominator"
    assert got["repeats"] == 2, "an original counted as its own repeat"
    assert abs(got["repeat_cost"] - 0.30) < 1e-9


def test_the_skipped_rows_are_counted_out_loud(tmp_path):
    got = resent_bodies.scan(str(_ledger(tmp_path)))
    assert got["skipped_no_sha"] == 2, (
        "rows with no req_sha must be reported, not silently dropped -- otherwise the percentage "
        "is a share of a corpus the reader was never told about")


def test_a_repeat_that_took_as_long_was_generated_not_served(tmp_path):
    """The latency collapse is the whole evidence that the gateway cached it; b's repeat did not."""
    got = resent_bodies.scan(str(_ledger(tmp_path)))
    assert got["served_from_cache"] == 1, "arm b's equally-slow repeat was miscounted as cached"
    assert abs(got["cache_cost"] - 0.10) < 1e-9


def test_an_empty_ledger_says_nothing_rather_than_dividing_by_zero(tmp_path):
    empty = tmp_path / "meter.jsonl"
    empty.write_text("", encoding="utf-8")
    got = resent_bodies.scan(str(empty))
    assert got["stamped"] == 0 and got["repeats"] == 0
    assert resent_bodies.main([str(empty)]) == 0
