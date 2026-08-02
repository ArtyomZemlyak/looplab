"""Three read-side contracts the owner and reviewer surfaces must answer identically (doc 25 SR-09, SR-10).

Each was maintained by comment discipline instead of shared code — the reviewer route's own comment
said "exactly as the owner route does" — which means a future change to the receipt format or the
cursor codes has to be made twice or the two surfaces quietly diverge on what evidence they serve.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import HTTPException

from looplab.core.node_evidence import METRICS_ATTEMPT_FILE
from looplab.serve.http import comment_cursor_error, comment_filter_invalid
from looplab.serve.metrics_adapters import fenced_node_metrics


# --------------------------------------------------------------- the attempt-fenced metrics read

class _Series:
    """Stand in for the adapters: record what window was asked for, answer with a marker."""

    def __init__(self):
        self.calls = []

    def __call__(self, node_dir, *, since_wall_time=None):
        self.calls.append(since_wall_time)
        return {"loss": [{"step": 0, "wall_time": since_wall_time or 0.0, "value": 1.0}]}


@pytest.fixture
def reader(monkeypatch):
    series = _Series()
    monkeypatch.setattr("looplab.serve.metrics_adapters.read_node_metrics", series)
    return series


def _receipt(node_dir, attempt: int, started_at: float = 1000.0) -> None:
    (node_dir / METRICS_ATTEMPT_FILE).write_text(
        json.dumps({"attempt": attempt, "started_at": started_at}), encoding="utf-8")


def test_a_receipt_for_this_attempt_windows_the_read_to_its_start(tmp_path, reader):
    """The window is what drops reset-era points. Reading unbounded here would serve the previous
    attempt's curve under the current attempt's label."""
    _receipt(tmp_path, 3, started_at=1234.5)
    assert fenced_node_metrics(tmp_path, 3)
    assert reader.calls == [1234.5]


def test_a_receipt_for_another_attempt_yields_nothing_and_reads_nothing(tmp_path, reader):
    _receipt(tmp_path, 2)
    assert fenced_node_metrics(tmp_path, 5) == {}
    assert reader.calls == [], "a superseded lifecycle must not even be read"


def test_a_legacy_run_with_no_receipt_is_readable_only_at_attempt_zero(tmp_path, reader):
    """Receipts postdate some runs, so attempt zero stays readable. A LATER attempt without its
    marker is unknown, not old-but-fine — and unknown fails closed."""
    assert fenced_node_metrics(tmp_path, 0)
    assert reader.calls == [None], "the legacy read is unwindowed, by design"
    reader.calls.clear()
    assert fenced_node_metrics(tmp_path, 1) == {}
    assert reader.calls == []


def test_a_torn_receipt_is_treated_as_absent_not_as_a_match(tmp_path, reader):
    """The receipt is an observability accelerator, not run truth. A hand-edited or half-written one
    must not be able to authorize serving another attempt's series."""
    (tmp_path / METRICS_ATTEMPT_FILE).write_text('{"attempt": ', encoding="utf-8")
    assert fenced_node_metrics(tmp_path, 4) == {} and reader.calls == []


def test_a_reader_that_raises_yields_an_empty_series_not_a_failed_request(tmp_path, monkeypatch):
    """Observability must never take down the owner's request or the reviewer's page."""
    def _boom(node_dir, *, since_wall_time=None):
        raise RuntimeError("event file half written")

    monkeypatch.setattr("looplab.serve.metrics_adapters.read_node_metrics", _boom)
    _receipt(tmp_path, 0)
    assert fenced_node_metrics(tmp_path, 0) == {}


def test_the_real_reader_survives_a_node_dir_that_does_not_exist(tmp_path):
    assert fenced_node_metrics(tmp_path / "missing", 0) == {}


def test_a_receipt_written_now_windows_to_now(tmp_path, reader):
    """Guards the tuple ORDER — (attempt, started_at). Swapping them would pass a plausible int as
    a wall-time and silently return an empty window for every live node."""
    now = time.time()
    _receipt(tmp_path, 7, started_at=now)
    fenced_node_metrics(tmp_path, 7)
    assert reader.calls == [now]


# --------------------------------------------------------------- the comment error contracts

def test_a_partial_lifecycle_filter_is_refused_with_the_shared_code():
    """One half of (node_id, node_generation) would widen the filter to every attempt of that node —
    the opposite of what a caller pinning a lifecycle asked for."""
    exc = comment_filter_invalid()
    assert exc.status_code == 400
    assert exc.detail["code"] == "comment_filter_invalid"
    assert "supplied together" in exc.detail["message"]


class _Cursor(Exception):
    def __init__(self, message: str, *, stale: bool):
        super().__init__(message)
        self.stale = stale


def test_a_malformed_cursor_is_400_and_a_superseded_one_is_409():
    """The split is what a client acts on: 400 says this cursor was never valid, 409 says it was
    valid for a run state that has since moved — only the second is worth re-fetching page one for."""
    invalid = comment_cursor_error(_Cursor("comment cursor is invalid", stale=False))
    assert invalid.status_code == 400
    assert invalid.detail["code"] == "invalid_comment_cursor"

    superseded = comment_cursor_error(
        _Cursor("comment cursor belongs to another run generation", stale=True))
    assert superseded.status_code == 409
    assert superseded.detail["code"] == "comment_cursor_stale"


def test_the_cursor_error_carries_the_projection_reason_verbatim():
    """The message is the only thing telling a reader WHICH invariant the cursor broke."""
    exc = comment_cursor_error(_Cursor("comment cursor belongs to another filter scope", stale=True))
    assert exc.detail["message"] == "comment cursor belongs to another filter scope"
    assert exc.detail["remediation"] == "refresh comments from the first page"


def test_both_comment_surfaces_raise_the_same_builders():
    """The point of sharing them: a reviewer that could filter or paginate more loosely than the
    owner would surface comments the owner's own view excludes."""
    from looplab.serve.routers import collaboration, reviews

    for module in (collaboration, reviews):
        source = module.__file__
        text = open(source, encoding="utf-8").read()
        assert "comment_filter_invalid()" in text and "comment_cursor_error(exc)" in text
        assert '"code": "comment_cursor_stale"' not in text, (
            f"{source} re-inlines the cursor envelope instead of sharing it")
