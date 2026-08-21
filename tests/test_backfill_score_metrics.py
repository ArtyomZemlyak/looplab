"""The objectives a score stage measured, recovered — and the four rules that keep the recovery honest.

Every scored vecsearch node printed 36 IR metrics and the record kept one. This module drives the
recovery; `looplab/maintenance/backfill_score_metrics.py` argues why it is worth doing.
"""
import json

import pytest

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import EV_SCORE_METRICS_BACKFILLED
from looplab.maintenance.backfill_score_metrics import (
    ALREADY_RECORDED, NO_SCORE_LOG, apply_run, parse_score_log, plan_run, readable_horizon,
)

_SUITE = "\n".join(
    f"2026-08-21 19:47:35.681 | INFO     | __main__:test_model:56 -   {name}: {value}"
    for name, value in (("Recall_at_1", "0.22"), ("Recall_at_100", "0.79"),
                        ("nDCG_at_100", "0.46"), ("MRR_at_100", "0.41")))


def test_the_parser_takes_the_metric_rows_and_nothing_else():
    """`Processing: 41` is the single largest source of numeric lines in these logs — 100 of them per
    file — and it is not an objective. The cutoff (`_at_K` / `@K`) is what separates them, and it is
    a property of the NAME rather than a list of names to keep."""
    text = _SUITE + "\n" + "\n".join(f"Processing: {i}" for i in range(100)) + "\nRECALL@100: 0.790898"
    found, decimals = parse_score_log(text)
    assert set(found) == {"Recall_at_1", "Recall_at_100", "nDCG_at_100", "MRR_at_100", "RECALL@100"}
    assert found["nDCG_at_100"] == 0.46 and found["RECALL@100"] == 0.790898
    assert "Processing" not in found


def test_precision_is_reported_PER_KEY_because_one_number_would_flatter_the_data():
    """These logs print the suite at 2 decimals and the primary at 6 — the same quantity twice. A
    file-level maximum would report '6 decimals' about a record that is 2 decimals wide almost
    everywhere, and two nodes 0.006 apart on the primary are IDENTICAL on every 2-decimal row."""
    _, decimals = parse_score_log(_SUITE + "\nRECALL@100: 0.790898")
    assert decimals["nDCG_at_100"] == 2 and decimals["RECALL@100"] == 6


def test_the_last_block_wins_when_a_stage_was_re_run():
    """Stage logs are opened append-mode, so a repaired run's block FOLLOWS its predecessor's. The
    same rule the trajectory readers apply, for the same reason: the final block is the one
    describing the artifact that survives."""
    found, _ = parse_score_log("nDCG_at_100: 0.11\n" + _SUITE)
    assert found["nDCG_at_100"] == 0.46


def _run(tmp_path, extras=None):
    """A one-node run whose node scored, with an optional LIVE extra_metrics record."""
    d = tmp_path / "r"
    (d / "nodes" / "node_0").mkdir(parents=True)
    (d / "nodes" / "node_0" / "score.log").write_text(_SUITE, encoding="utf-8")
    store = EventStore(str(d / "events.jsonl"))
    store.append("run_started", {"run_id": "r", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "operator": "seed",
                                  "idea": {"name": "n", "rationale": "r", "params": {},
                                           "operator": "seed"}})
    payload = {"node_id": 0, "generation": 0, "metric": 0.79, "eval_seconds": 1.0}
    if extras is not None:
        payload["extra_metrics"] = extras
    store.append("node_evaluated", payload)
    return d


def test_a_recovered_block_reaches_the_node_through_the_fold(tmp_path):
    d = _run(tmp_path)
    rows = plan_run(d)
    assert len(rows) == 1 and len(rows[0]["extra_metrics"]) == 4
    assert apply_run(d, rows) == 1
    node = fold(EventStore(str(d / "events.jsonl")).read_all()).nodes[0]
    assert node.extra_metrics["nDCG_at_100"] == 0.46
    assert node.extra_metrics_provenance["nDCG_at_100"] == "declared"


def test_no_direction_is_ever_written_so_the_axis_stays_unranked(tmp_path):
    """THE OMISSION IS THE POINT. Nobody declared which way was better when this eval ran, and
    orientation is a forward-looking declaration; asserting it here would present a reconstruction
    as a measurement. The consequence is intended — a ranking surface declines to order an axis it
    cannot orient, so these values are audit."""
    d = _run(tmp_path)
    apply_run(d, plan_run(d))
    node = fold(EventStore(str(d / "events.jsonl")).read_all()).nodes[0]
    assert node.extra_metrics, "the values landed"
    assert node.extra_metrics_direction == {}, "and not one of them is oriented"


def test_a_live_record_is_never_overwritten_and_a_second_pass_is_a_no_op(tmp_path):
    """The whole safety of the mechanism, in both directions. A measurement taken while the run was
    happening outranks a reconstruction read from a log afterwards — and because the fold declines
    on exactly the condition the planner skips on, re-running is idempotent BY CONSTRUCTION rather
    than by a check that could drift out of step with it."""
    d = _run(tmp_path, extras={"live_metric": 1.5})
    rows = plan_run(d)
    assert rows[0]["unrecoverable"] == ALREADY_RECORDED and not rows[0]["extra_metrics"]
    # ...and even if such a row were forced through, the fold refuses it.
    EventStore(str(d / "events.jsonl")).append(
        EV_SCORE_METRICS_BACKFILLED, {"node_id": 0, "extra_metrics": {"nDCG_at_100": 0.46}})
    node = fold(EventStore(str(d / "events.jsonl")).read_all()).nodes[0]
    assert node.extra_metrics == {"live_metric": 1.5}, "the live record survived"

    # Second pass over a run that WAS backfilled: the planner now sees the folded values and skips.
    d2 = _run(tmp_path / "second")
    apply_run(d2, plan_run(d2))
    again = plan_run(d2)
    assert again[0]["unrecoverable"] == ALREADY_RECORDED
    assert apply_run(d2, again) == 1, "the row is still written..."
    node2 = fold(EventStore(str(d2 / "events.jsonl")).read_all()).nodes[0]
    assert node2.extra_metrics["nDCG_at_100"] == 0.46, "...and changes nothing"


def test_a_node_with_no_score_log_gets_a_row_that_says_so(tmp_path):
    """'The log is gone' and 'this node had no second objective' are opposite statements, and a
    silent omission is read as the second."""
    d = _run(tmp_path)
    (d / "nodes" / "node_0" / "score.log").unlink()
    assert plan_run(d)[0]["unrecoverable"] == NO_SCORE_LOG


def test_the_readable_horizon_is_named_rather_than_silently_applied(tmp_path):
    """`EventStore.read_all` stops at the first logical-sequence GAP, deliberately. On
    `runs/rubertlite-dense-retrieval` that fence bites at event 20 of 1,624 lines, so a run with 81
    `node_created` rows folds to TWO nodes. Failing closed is right for a fold; reporting '1 scored
    node' about that run without saying what was bounded is not."""
    d = _run(tmp_path)
    served, lines = readable_horizon(d)
    assert served == lines and served > 0, "an intact log has no horizon to report"

    with open(d / "events.jsonl", "r+", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
        rows[-1]["seq"] = rows[-1]["seq"] + 5          # punch a gap
        fh.seek(0), fh.truncate()
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    served2, lines2 = readable_horizon(d)
    assert served2 < lines2, "the store stops at the gap and the tool can see that it did"
