"""A reconstructed extra metric is legible as one in FOLDED state.

`maintenance/backfill_score_metrics.py` recovers objectives the score stage printed and the run
threw away (36 numbers computed, one kept). Its docstring argues that "the `backfilled` marker
beside it is what stops any surface calling this a live measurement" — and until 2026-09-02 there
was no such marker in folded state. The handler folded values plus a bare `declared` channel, and
the marker plus the per-key `precision_decimals` the writer argues a reader "must not have to
guess" lived only on the raw event row, which no surface reads.

THE COST IS A TIE READ AS MEASURED. The recovered suite is printed to TWO decimals while the
primary is read at six, so `e5small-dr-unified-v4` nodes 0 and 1 — which differ by 0.006 on
recall@100 — are identical on every recovered metric.

The sibling handler `_on_applied_params_backfilled` already had the right shape and is the
reference: every write it makes carries `backfilled: True` and the reason it was possible, because
"no surface may present a reconstruction as a measurement, and the flag is how a surface tells them
apart".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from looplab.core.models import extra_metric_is_backfilled, extra_metric_precision
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import EV_SCORE_METRICS_BACKFILLED


def _run(tmp_path, *, extras=None, channels=None):
    rd = tmp_path / "run"
    rd.mkdir(exist_ok=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "base"}, "code": "print(1)"})
    payload = {"node_id": 0, "metric": 0.79, "status": "ok"}
    if extras is not None:
        payload["extra_metrics"] = extras
    if channels is not None:
        payload["extra_metrics_provenance"] = channels
    store.append("node_evaluated", payload)
    return rd, store


def test_the_fold_stamps_the_marker_and_the_precision(tmp_path):
    """MUTATION: drop the `extra_metrics_backfill` stamp -> the values fold with a bare `declared`
    channel and every surface renders a reconstruction as a measurement."""
    rd, store = _run(tmp_path)
    store.append(EV_SCORE_METRICS_BACKFILLED, {
        "node_id": 0, "generation": 0, "read_at": 1234.5,
        "extra_metrics": {"ndcg@100": 0.46, "map@100": 0.34},
        "precision_decimals": {"ndcg@100": 2, "map@100": 2},
        "unrecoverable": ""})

    node = fold(EventStore(rd / "events.jsonl").read_all()).nodes[0]
    assert node.extra_metrics == {"ndcg@100": 0.46, "map@100": 0.34}
    # The channel stays `declared` — the operator's own scoring program printed these. The marker is
    # a SECOND, orthogonal fact, not a fourth channel value.
    assert node.extra_metrics_provenance == {"ndcg@100": "declared", "map@100": "declared"}
    assert extra_metric_is_backfilled(node) is True
    assert extra_metric_precision(node, "ndcg@100") == 2
    assert node.extra_metrics_backfill["backfilled_at"] == 1234.5


def test_a_live_record_is_never_marked(tmp_path):
    """Absent means MEASURED, and that is the safe direction here — the opposite of the channel
    map's. Every log written before the backfill tool existed folds unmarked."""
    rd, _store = _run(tmp_path, extras={"auc": 0.9}, channels={"auc": "auto"})
    node = fold(EventStore(rd / "events.jsonl").read_all()).nodes[0]
    assert node.extra_metrics_backfill == {}
    assert extra_metric_is_backfilled(node) is False
    assert extra_metric_precision(node, "auc") is None


def test_a_live_record_declines_the_backfill_and_stays_unmarked(tmp_path):
    """The handler's own idempotence: a measurement taken while the run was happening is never
    overwritten — so the marker must not land on it either. A stamped marker over a declined write
    would be the inversion with the sign flipped."""
    rd, store = _run(tmp_path, extras={"ndcg@100": 0.461234}, channels={"ndcg@100": "declared"})
    store.append(EV_SCORE_METRICS_BACKFILLED, {
        "node_id": 0, "generation": 0, "read_at": 1.0,
        "extra_metrics": {"ndcg@100": 0.46}, "precision_decimals": {"ndcg@100": 2},
        "unrecoverable": ""})

    node = fold(EventStore(rd / "events.jsonl").read_all()).nodes[0]
    assert node.extra_metrics == {"ndcg@100": 0.461234}   # the LIVE value, at its own precision
    assert extra_metric_is_backfilled(node) is False


def test_an_unrecoverable_row_marks_nothing(tmp_path):
    """A row saying "this node could not be recovered" is not a reconstruction of anything."""
    rd, store = _run(tmp_path)
    store.append(EV_SCORE_METRICS_BACKFILLED, {
        "node_id": 0, "generation": 0, "read_at": 1.0, "extra_metrics": {},
        "precision_decimals": {}, "unrecoverable": "score_log_absent"})
    node = fold(EventStore(rd / "events.jsonl").read_all()).nodes[0]
    assert node.extra_metrics == {}
    assert extra_metric_is_backfilled(node) is False


def test_a_node_reset_clears_the_marker(tmp_path):
    """It describes the map the reset just cleared. Left set, a later LIVE measurement on the same
    node would be marked as a reconstruction — the inversion, with the sign flipped."""
    rd, store = _run(tmp_path)
    store.append(EV_SCORE_METRICS_BACKFILLED, {
        "node_id": 0, "generation": 0, "read_at": 1.0,
        "extra_metrics": {"ndcg@100": 0.46}, "precision_decimals": {"ndcg@100": 2},
        "unrecoverable": ""})
    assert extra_metric_is_backfilled(fold(EventStore(rd / "events.jsonl").read_all()).nodes[0])

    store.append("node_reset", {"node_id": 0, "stage": "eval"})
    node = fold(EventStore(rd / "events.jsonl").read_all()).nodes[0]
    assert node.extra_metrics == {}
    assert node.extra_metrics_backfill == {}


def test_the_marker_survives_a_json_round_trip(tmp_path):
    """The record travels to the browser and to the reviewer plane as JSON. A shape that only holds
    in-process is a marker the surfaces this exists for cannot read."""
    rd, store = _run(tmp_path)
    store.append(EV_SCORE_METRICS_BACKFILLED, {
        "node_id": 0, "generation": 0, "read_at": 1.0,
        "extra_metrics": {"ndcg@100": 0.46}, "precision_decimals": {"ndcg@100": 2},
        "unrecoverable": ""})
    node = fold(EventStore(rd / "events.jsonl").read_all()).nodes[0]
    revived = json.loads(json.dumps(node.model_dump()))
    assert revived["extra_metrics_backfill"]["backfilled"] is True
    assert revived["extra_metrics_backfill"]["precision_decimals"]["ndcg@100"] == 2


def test_the_reviewer_plane_carries_it(tmp_path):
    """A reviewer handed `nDCG 0.46` beside `0.46` cannot see the tie is a printing artifact
    without it — the same rule that put `extra_metrics_provenance` and `_direction` on that list."""
    from looplab.serve.routers.reviews import _REVIEW_NODE_KEYS
    assert "extra_metrics_backfill" in _REVIEW_NODE_KEYS


def test_the_writer_and_the_fold_agree_on_the_payload_keys():
    """The planner writes `precision_decimals` and `read_at`; the handler reads exactly those. A
    reader keyed on a field nothing writes is the dead branch `RunTools._research_memo` carried for
    six weeks — and this is the same join, one module over."""
    import ast
    import looplab.maintenance.backfill_score_metrics as writer
    from looplab.events import replay

    planner = ast.parse(Path(writer.__file__).read_text(encoding="utf-8"))
    written = {node.value for node in ast.walk(planner)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert {"precision_decimals", "read_at", "extra_metrics"} <= written

    handler = Path(replay.__file__).read_text(encoding="utf-8")
    start = handler.index("def _on_score_metrics_backfilled")
    body = handler[start:handler.index("def _on_applied_params_backfilled")]
    for key in ("precision_decimals", "read_at", "extra_metrics"):
        assert f'"{key}"' in body, f"the fold never reads {key}, which the writer records"
