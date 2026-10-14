"""Review 2026-10-14 §7: a MiniOneRec eval printed ~615 numeric keys on its metric line, the
`extra_metrics` bound kept the first 256 in map order — all auto-captured — and three of the four
DECLARED key metrics vanished from the record with nothing saying so."""
from __future__ import annotations

from looplab.core.models import (EXTRA_METRICS_MAX_ITEMS, Node, extra_metrics_overflow,
                                 normalize_extra_metrics)
from looplab.events.replay import _extra_metrics_omitted
from looplab.runtime.command_eval import captured_result_fields

DECLARED = ["InCart_FilteredUnseenRecall@20", "FilteredUnseenRecall@200",
            "FilteredUnseenRecall@20"]


def _noisy_map():
    out = {f"auto_{i}": float(i) for i in range(615)}
    out.update({name: 0.5 for name in DECLARED})
    return out


def test_declared_names_survive_the_bound_whatever_their_position():
    value = _noisy_map()
    kept = normalize_extra_metrics(value, priority=DECLARED)
    assert len(kept) == EXTRA_METRICS_MAX_ITEMS
    assert all(name in kept for name in DECLARED)
    omitted = extra_metrics_overflow(value, priority=DECLARED)
    assert len(omitted) == len(value) - EXTRA_METRICS_MAX_ITEMS
    assert not set(omitted) & set(kept)


def test_a_map_under_the_bound_normalizes_exactly_as_before():
    value = {"b": 1, "a": 2.5, "bad": "x", "nan": float("nan"), "flag": True}
    assert normalize_extra_metrics(value, priority=["a"]) == {"b": 1.0, "a": 2.5}
    assert list(normalize_extra_metrics(value, priority=["a"])) == ["b", "a"]
    assert extra_metrics_overflow(value) == []


def test_the_capture_puts_declared_metrics_first():
    line = "{" + ", ".join(f'"auto_{i}": {i}' for i in range(300)) + \
        ', "metric": 0.9, "FilteredUnseenRecall@20": 0.25}'
    fields = captured_result_fields(
        line, ".", {"kind": "stdout_json", "key": "metric"}, 0.9,
        metrics={"FilteredUnseenRecall@20": {"kind": "stdout_json",
                                              "key": "FilteredUnseenRecall@20"}})
    assert next(iter(fields["extra_metrics"])) == "FilteredUnseenRecall@20"
    assert "FilteredUnseenRecall@20" in normalize_extra_metrics(fields["extra_metrics"])


def test_the_omitted_receipt_folds_bounded_and_defaults_empty():
    assert _extra_metrics_omitted(None) == {}
    assert _extra_metrics_omitted({"count": 0, "names": []}) == {}
    assert _extra_metrics_omitted({"count": 359, "names": ["a", 3, "b"]}) == {
        "count": 359, "names": ["a", "b"]}
    assert Node.model_fields["extra_metrics_omitted"].default_factory() == {}
