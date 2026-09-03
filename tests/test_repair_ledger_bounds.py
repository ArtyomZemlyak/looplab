"""The cross-node repair ledger is bounded, and it now says what the bound dropped.

Two defects, one cause. The fold capped the ledger at 200 FIRST-COME rows with no receipt, so:

  * one node that repairs pathologically often consumed the whole ledger. A node with 2,345 repair
    rows fills all 200 slots before any other node records one — and the ledger's entire purpose is
    telling a LATER node what a SIBLING had to fix, which it cannot do if it is a transcript of the
    worst node's first two hundred attempts;
  * nothing recorded the truncation. `looplab inspect` printed the cap as a total ("repair rows:
    200") and `lessons_reconcile` read a dropped row as `no cause recorded`, generalizing over a
    population the fold had silently cut.

`repair_ledger_omitted` is additive and reader-defaulted (invariant #5): an old log folds to `{}`
and every consumer reads that as "nothing was dropped", which for a log written under the old cap is
the honest answer — nobody counted, and inventing a number would be worse.
"""
from __future__ import annotations

import pytest

from looplab.core.models import RunState
from looplab.events.replay import (_REPAIR_LEDGER_MAX, _REPAIR_LEDGER_MAX_PER_NODE,
                                   _record_repair_ledger)


def _repairs(st, node_id, count, *, start=0):
    for i in range(start, start + count):
        _record_repair_ledger(st, {"node_id": node_id, "attempt": i, "generation": 0,
                                   "reason": "crash", "changed": ["train.py"],
                                   "rationale": "fix the thing"})


def test_one_pathological_node_cannot_consume_the_whole_ledger():
    """THE INCIDENT SHAPE. MUTATION: drop the per-node bound -> node 1 takes all 200 slots and
    node 2 — a different experiment with a different fix — records nothing."""
    st = RunState()
    _repairs(st, 1, 2_345)
    _repairs(st, 2, 3)

    kept = [row["node_id"] for row in st.repair_ledger]
    assert kept.count(1) == _REPAIR_LEDGER_MAX_PER_NODE
    assert kept.count(2) == 3, "the sibling's rows are the whole point of the ledger"


def test_the_dropped_rows_are_counted_and_attributed():
    """MUTATION: return silently -> the CLI prints the cap as a total and a reader cannot tell a
    complete ledger from a truncated one."""
    st = RunState()
    _repairs(st, 1, 100)

    omitted = st.repair_ledger_omitted
    assert omitted["rows"] == 100 - _REPAIR_LEDGER_MAX_PER_NODE
    assert omitted["nodes"]["1"] == 100 - _REPAIR_LEDGER_MAX_PER_NODE, (
        "attribution matters: a reader needs to know WHICH node's history was cut")


def test_a_run_within_both_bounds_records_no_omission():
    """Absence must mean absence. MUTATION: always stamp a zero -> `{}` stops meaning 'nothing was
    dropped' and an old log becomes indistinguishable from a truncated one."""
    st = RunState()
    _repairs(st, 1, 3)
    _repairs(st, 2, 3)

    assert len(st.repair_ledger) == 6
    assert st.repair_ledger_omitted == {}


def test_the_global_bound_still_holds_across_many_nodes():
    """The per-node bound is a share of the global one, not a replacement for it: a run with
    hundreds of nodes must still not grow an unbounded prompt."""
    st = RunState()
    for node in range(1, 200):
        _repairs(st, node, _REPAIR_LEDGER_MAX_PER_NODE)

    assert len(st.repair_ledger) == _REPAIR_LEDGER_MAX
    assert st.repair_ledger_omitted["rows"] > 0


def test_the_node_key_is_a_string_so_replay_equals_a_round_trip():
    """This dict is serialized into every projection, where an integer key becomes a string. Folding
    to one spelling keeps a replayed state equal to a JSON round-tripped one.

    MUTATION: key by int -> `fold(log) != json.loads(json.dumps(fold(log)))` and a UI comparison
    that looked stable starts flapping.
    """
    import json

    st = RunState()
    _repairs(st, 7, _REPAIR_LEDGER_MAX_PER_NODE + 5)

    assert set(st.repair_ledger_omitted["nodes"]) == {"7"}
    assert json.loads(json.dumps(st.repair_ledger_omitted)) == st.repair_ledger_omitted


def test_idempotence_survives_the_bound():
    """A double-fold must collapse to the same state — the key check runs BEFORE the cap, so a
    repeated row is not counted as an omission.

    MUTATION: cap before de-duplicating -> replaying the same log twice inflates `rows`.
    """
    st = RunState()
    for _ in range(3):
        _record_repair_ledger(st, {"node_id": 1, "attempt": 0, "generation": 0,
                                   "reason": "crash", "changed": [], "rationale": ""})

    assert len(st.repair_ledger) == 1
    assert st.repair_ledger_omitted == {}, "a duplicate is not an omission"
