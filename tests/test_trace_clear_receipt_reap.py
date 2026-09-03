"""A finished trace clear stops leaving a receipt in the run root forever.

`plan_service_file_reap` classified four root-level populations — deletion receipts, their identity
sidecars, reset receipts and lifecycle locks — and not the fifth. A trace clear publishes
`.trace-clear.<run key>.<tc_…>.json` beside the run and nothing ever removed one.

That is worse than clutter, and the reason is in `trace_clear.py` itself: every NEW clear of a run
globs its siblings and strict-loads each, raising 503 on any that will not parse. One malformed
leftover therefore refuses every future clear of that run, permanently — and the directory it
accumulates in is the one the run list stats on every poll.

The refusals are the interesting half, and they are the deletion receipt's refusals: `pending` is
live state a retry resumes from, `superseded` is the only record that an operator's clear was
overtaken, a succeeded receipt still answers a retry idempotently until it goes cold, and an
UNREADABLE one is kept and said so — it is precisely the file breaking this run's clears, so the
sweep must not quietly make the symptom disappear.
"""
from __future__ import annotations

import json

import pytest

from looplab.serve.appstate import _TRACE_CLEAR_RECEIPT_PREFIX
from looplab.serve.service_reaper import plan_service_file_reap

_OP = "tc_" + "a" * 32
_KEY = "b" * 64


def _receipt(root, status="succeeded", *, operation=_OP, body=None, age_s=None):
    path = root / f"{_TRACE_CLEAR_RECEIPT_PREFIX}{_KEY}.{operation}.json"
    path.write_text(json.dumps(body if body is not None else {
        "version": 2, "status": status, "id": operation, "node_id": 3,
        "expected_generation": "c" * 64, "expected_trace_revision": "d" * 64,
    }), encoding="utf-8")
    if age_s is not None:
        import os
        old = path.stat().st_mtime - age_s
        os.utime(path, (old, old))
    return path


def _entry(plan, path):
    hits = [e for e in plan["entries"] if e["name"] == path.name]
    assert len(hits) == 1, f"expected one entry for {path.name}, got {hits}"
    return hits[0]


def test_a_cold_succeeded_receipt_is_reaped(tmp_path):
    """MUTATION: drop the branch -> the receipt is classified by nothing and lives forever."""
    path = _receipt(tmp_path, "succeeded", age_s=200_000)

    entry = _entry(plan_service_file_reap(tmp_path), path)

    assert entry["category"] == "trace_clear_receipt"
    assert entry["remove"] is True and "cold" in entry["rule"]


@pytest.mark.parametrize("status", ["pending", "superseded"])
def test_a_non_succeeded_receipt_is_never_reaped(tmp_path, status):
    """`pending` is live state a retry resumes from; `superseded` is the only record that an
    operator's clear was overtaken. Age does not enter into it.

    MUTATION: reap on age alone -> a retry loses the state it resumes from.
    """
    path = _receipt(tmp_path, status, age_s=10_000_000)

    entry = _entry(plan_service_file_reap(tmp_path), path)

    assert entry["remove"] is False and status in entry["rule"]


def test_a_succeeded_receipt_still_answers_a_retry_while_it_is_warm(tmp_path):
    """The grace is what keeps the operator's second click idempotent instead of a 404."""
    path = _receipt(tmp_path, "succeeded")

    entry = _entry(plan_service_file_reap(tmp_path), path)

    assert entry["remove"] is False and "old" in entry["rule"]


def test_an_unreadable_receipt_is_kept_and_named(tmp_path):
    """THE ONE THIS EXISTS FOR. A malformed sibling 503s every future clear of the run, so it must
    be findable — reaping it would make the symptom vanish and leave the operator with a clear that
    started working again for no stated reason.

    MUTATION: remove it as junk -> the sweep silently repairs a fault nobody diagnosed.
    """
    path = tmp_path / f"{_TRACE_CLEAR_RECEIPT_PREFIX}{_KEY}.{_OP}.json"
    path.write_text("{not json", encoding="utf-8")
    import os
    old = path.stat().st_mtime - 200_000
    os.utime(path, (old, old))

    entry = _entry(plan_service_file_reap(tmp_path), path)

    assert entry["remove"] is False
    assert "unreadable" in entry["rule"] and "inspection" in entry["rule"]


def test_a_name_the_writer_could_not_emit_is_not_swept(tmp_path):
    """`_split_operation`'s rule, applied here: a parser that accepts more shapes than the writer
    can emit hands unrelated files to a sweep with an `unlink` in it.

    MUTATION: match on the prefix alone -> a stray `.trace-clear.notes.json` becomes reapable.
    """
    path = _receipt(tmp_path, "succeeded", operation="notanoperation", age_s=200_000)

    entry = _entry(plan_service_file_reap(tmp_path), path)

    assert entry["remove"] is False and "unrecognised" in entry["rule"]


def test_the_other_four_populations_are_untouched(tmp_path):
    """The regression guard: a fifth rule must not reclassify the four that already worked."""
    _receipt(tmp_path, "succeeded", age_s=200_000)
    (tmp_path / "some-run").mkdir()

    plan = plan_service_file_reap(tmp_path)
    categories = {e["category"] for e in plan["entries"]}

    assert categories == {"trace_clear_receipt"}, (
        f"the sweep classified something it should not have: {categories}")
