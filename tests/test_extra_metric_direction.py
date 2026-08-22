"""WHICH WAY IS BETTER on a second objective, recorded rather than assumed.

`ui/src/panels.jsx::paretoFront` ranks the non-dominated set over the primary metric plus every
`extra_metrics` key, and until 2026-08-21 it read every one of those keys as cost-like. That was
harmless for exactly one reason, measured over every `*.jsonl` under `runs/` (131 files, 15 run
directories): the only extras any run has ever recorded are the engine's four CUDA-probe CONSTANTS
in the `specgate*` toys, and a constant dimension can neither create nor break a domination. All 8
evaluated nodes of the two real task families record `extra_metrics == {}`.

It stops being harmless the moment a run records a real second objective, and the ones queued to be
recorded are QUALITY metrics — one vecsearch score stage already prints nDCG@k, MAP@k, MRR@k,
Precision@k and Recall@k at seven cutoffs, ~35 higher-is-better numbers, of which the record keeps
one. Read as costs they invert, and the failure is silent: every number real, the ordering
backwards.

So the direction travels WITH the values, from the operator's own reader spec to the fold. These
tests drive that path; `ui/test/paretoFrontObjectiveSource.test.js` drives the consumer.
"""
from looplab.core.models import (
    DIRECTIONS, EXTRA_METRIC_DIRECTION_UNKNOWN, extra_metric_direction,
    normalize_extra_metric_directions, oriented_extra_metrics_only,
)


def test_an_unrecorded_direction_reads_unknown_and_never_a_direction():
    """The reader-side default, and the reason it is not `min`.

    Every log written before this shipped carries no direction map at all, and a key missing from a
    PRESENT map is the same fact — nobody recorded which way is better. Answering either direction
    for those states something that was never measured, which is the exact shape
    `EXTRA_METRIC_UNKNOWN` exists to refuse one field over."""
    assert extra_metric_direction(None, "nDCG_at_100") == EXTRA_METRIC_DIRECTION_UNKNOWN
    assert extra_metric_direction({}, "nDCG_at_100") == EXTRA_METRIC_DIRECTION_UNKNOWN
    assert extra_metric_direction({"other": "max"}, "nDCG_at_100") == EXTRA_METRIC_DIRECTION_UNKNOWN
    assert EXTRA_METRIC_DIRECTION_UNKNOWN not in DIRECTIONS


def test_a_malformed_direction_is_dropped_rather_than_coerced():
    """Same untrusted-input discipline as the channel map: this arrives from an old or hand-edited
    event log with assignment validation off. A dropped entry reads back `unknown`, which is the
    safe direction — a coerced one would publish an ordering nobody declared."""
    got = normalize_extra_metric_directions(
        {"ndcg": "max", "latency": "min", "typo": "mxa", "num": 1, 5: "max", None: "min"})
    assert got == {"ndcg": "max", "latency": "min"}
    assert normalize_extra_metric_directions("not a dict") == {}
    assert normalize_extra_metric_directions(None) == {}


def test_oriented_only_drops_the_axis_and_never_the_value():
    """The gate a ranking surface applies. It removes a DIMENSION from an ordering; the value stays
    in the record and on every reading surface, because an unorientable number is still a number."""
    extras = {"nDCG_at_100": 0.44, "alloc_bytes": 4096.0}
    kept, dirs = oriented_extra_metrics_only(extras, {"nDCG_at_100": "max"})
    assert kept == {"nDCG_at_100": 0.44} and dirs == {"nDCG_at_100": "max"}
    assert oriented_extra_metrics_only(extras, {}) == ({}, {})
    assert extras == {"nDCG_at_100": 0.44, "alloc_bytes": 4096.0}, "the source map is not mutated"


def test_the_node_folds_a_direction_map_and_an_old_log_folds_to_empty():
    """Additive with a reader-side default (invariant #5): a log written before this shipped has no
    such key, folds to `{}`, and every key answers `unknown`."""
    from looplab.core.models import Node
    base = {"id": 1, "operator": "seed",
            "idea": {"name": "x", "rationale": "y", "params": {}, "operator": "seed"}}
    n = Node.model_validate({**base, "extra_metrics": {"nDCG_at_100": 0.44},
                             "extra_metrics_direction": {"nDCG_at_100": "max", "typo": "mxa"}})
    assert n.extra_metrics_direction == {"nDCG_at_100": "max"}
    assert Node.model_validate(base).extra_metrics_direction == {}


def test_only_a_DECLARED_reader_can_orient_an_axis(tmp_path):
    """The writer's rule, DRIVEN rather than read off the source.

    `auto` keys are scraped off the candidate's own stdout with no declaration, so nothing said
    which way is better about them; the engine's probe constants are not objectives at all.
    Inventing a direction for those is the silent inversion this map exists to stop.

    Note the shape of the trap, borrowed from `test_auto_extra_metrics`: `latency` is BOTH a
    declared reader and another numeric key on the stdout line, so auto-capture also sees it. The
    declared channel wins the value; the direction must follow the DECLARATION and not the value,
    or an auto-captured number would inherit an orientation nobody gave it."""
    import json, sys
    from looplab.runtime.command_eval import run_command_eval
    (tmp_path / "p.py").write_text(
        "import json; print(json.dumps(%s))\n"
        % json.dumps({"metric": 0.5, "latency": 50.0, "ndcg": 0.44, "sneaky": 7.0}),
        encoding="utf-8")
    res = run_command_eval(
        [sys.executable, "p.py"], str(tmp_path), 60, {"kind": "stdout_json", "key": "metric"},
        metrics={"latency": {"kind": "stdout_json", "key": "latency", "direction": "min"},
                 "ndcg": {"kind": "stdout_json", "key": "ndcg", "direction": "max"},
                 "undirected": {"kind": "stdout_json", "key": "latency"}})
    assert res.extra_metrics["latency"] == 50.0 and res.extra_metrics["sneaky"] == 7.0
    assert res.extra_metrics_direction == {"latency": "min", "ndcg": "max"}, (
        "only the two DECLARED-with-a-direction readers orient an axis: `sneaky` was auto-captured "
        "and `undirected` declared no direction")


def test_a_declared_reader_that_MISSED_leaves_no_orphan_direction(tmp_path):
    """A direction for a key the record does not carry is a fact about nothing.

    The value dict already follows this rule — a missed reader returns None and is omitted — and the
    direction map must not drift from it, or a later reader joining the two would orient an axis
    that has no values on it."""
    import json, sys
    from looplab.runtime.command_eval import run_command_eval
    (tmp_path / "p.py").write_text(
        "import json; print(json.dumps(%s))\n" % json.dumps({"metric": 0.5}), encoding="utf-8")
    res = run_command_eval(
        [sys.executable, "p.py"], str(tmp_path), 60, {"kind": "stdout_json", "key": "metric"},
        metrics={"ndcg": {"kind": "stdout_json", "key": "ndcg", "direction": "max"}})
    assert res.extra_metrics in (None, {}), "the reader missed, so there is no value"
    assert res.extra_metrics_direction in (None, {}), "and therefore no direction either"
