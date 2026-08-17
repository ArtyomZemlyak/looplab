"""A stage row a later inline repair superseded must not read as the node's current state.

THE DEFECT. `stage_finished` is appended once per stage per ATTEMPT of `engine/evaluate.py`'s
inline-repair loop, and `events/replay.py::_on_stage_finished` keeps the rows last-wins BY STAGE
NAME. An inline repair does NOT bump the lifecycle generation, so after a repair the surviving rows
still describe the attempt the repair replaced — and nothing in the record said so, in either
direction: `events/types.py` registers no stage-START counterpart and the `stage_finished` payload
(`{node_id, name, status, exit_code, seconds, generation}`) carries no repair counter. The
Inspector's TRACE was right about the same node at the same moment because it reads a DIFFERENT
source, the `stage_started` SPAN in `spans.jsonl` (24 of them on v9 against 21 folded events).

THE FIXTURES ARE THE REAL SHAPES. `_V9_NODE_5` / `_V9_NODE_6` are transcribed from
`runs/rubertlite-dr-unified-v9/events.jsonl` as it stood at 2026-08-17 12:48 UTC — the two nodes
the operator reported, with NINE and FIVE live `vectorsearch.train` processes in their workdirs and
their newest recorded stage statement a FAILURE from a superseded repair cycle, 177 and 56 minutes
old. `_V8_NODE_3` is the negative control that matters most and it is also real: its `mine` stage
ran ONCE and every later attempt REUSED it, so its row is older in wall-clock terms and is not
stale in any sense — a rule that convicted it would be worse than the defect.
"""
from __future__ import annotations

import json
from pathlib import Path

from looplab.core.models import Event, NodeStatus, stage_row_superseded, superseded_stage_rows
from looplab.events.replay import fold


def _ev(t, d, s):
    return Event(seq=s, ts=float(s), type=t, data=d)


def _node(nid, rows, *, created_seq=0):
    """`node_created` + the real row sequence, folded exactly as the engine wrote it."""
    evs = [_ev("run_started", {"task": {}, "settings": {}}, 0),
           _ev("node_created", {"node_id": nid, "operator": "draft", "parent_ids": [],
                                "idea": {"operator": "draft", "params": {}}, "code": "x"},
               created_seq + 1)]
    for i, (t, d) in enumerate(rows):
        evs.append(_ev(t, {"node_id": nid, **d}, created_seq + 2 + i))
    return fold(evs).nodes[nid]


def _sf(name, status, exit_code, secs):
    return ("stage_finished", {"name": name, "status": status, "exit_code": exit_code,
                               "seconds": secs, "generation": 0})


def _repair(attempt, reason, verified):
    return ("node_repaired", {"generation": 0, "attempt": attempt, "reason": reason,
                              "verified": verified, "code": "x%d" % attempt})


# rubertlite-dr-unified-v9 node 5, verbatim: three repair cycles, the last stage statement a
# failure from cycle 2, then a repair, then 177 minutes of training with nothing appended.
_V9_NODE_5 = [
    _sf("mine", "ok", 0, 3698.348), _sf("train", "fail", 1, 254.33),
    _repair(1, "crash", "inert"),
    _sf("mine", "reused", 0, 0.0), _sf("train", "fail", 1, 228.845),
    _repair(2, "crash", "unstated"),
    _sf("mine", "expect_failed", 0, 153.352),
    _repair(3, "expect_failed", "unmet"),
]
# node 6: one cycle, one repair, then 56 minutes.
_V9_NODE_6 = [
    _sf("mine", "ok", 0, 2216.976), _sf("train", "fail", 1, 297.349),
    _repair(1, "crash", "verified"),
]
# rubertlite-dr-unified-v8 node 3's shape: `mine` ran once and was REUSED by later attempts, which
# is the later attempt's own statement that its result stands.
_V8_NODE_3 = [
    _sf("mine", "ok", 0, 2304.0), _sf("train", "fail", 1, 100.0),
    _repair(1, "crash", "verified"),
    _sf("mine", "reused", 0, 0.0), _sf("train", "fail", 1, 120.0),
    _repair(2, "crash", "verified"),
    _sf("mine", "reused", 0, 0.0), _sf("train", "ok", 0, 19915.75), _sf("score", "ok", 0, 3130.3),
]


# ---------------------------------------------------------------- the property

def test_v9_node_5_stage_rows_are_attributed_to_the_attempt_that_wrote_them():
    n = _node(5, _V9_NODE_5)
    assert n.status is NodeStatus.pending
    assert n.repairs == 3                      # three durable inline repairs on this lifecycle
    assert [(s["name"], s["status"], s["repairs"]) for s in n.stages] == [
        ("mine", "expect_failed", 2), ("train", "fail", 1)]
    # …and BOTH of the red chips the operator was shown are superseded.
    assert [s["name"] for s in superseded_stage_rows(n)] == ["mine", "train"]


def test_v9_node_6_stage_rows_are_superseded_by_its_one_repair():
    n = _node(6, _V9_NODE_6)
    assert n.status is NodeStatus.pending and n.repairs == 1
    assert [(s["name"], s["repairs"]) for s in n.stages] == [("mine", 0), ("train", 0)]
    assert [s["name"] for s in superseded_stage_rows(n)] == ["mine", "train"]


def test_a_reused_marker_advances_the_epoch_so_a_reused_success_is_never_superseded():
    """The one that decides the rule. v8 node 3's `mine ok` is two repairs older than the node and
    is CURRENT: every attempt since said `reused`, i.e. said this result stands."""
    n = _node(3, _V8_NODE_3)
    mine = next(s for s in n.stages if s["name"] == "mine")
    assert mine["status"] == "ok" and mine["seconds"] == 2304.0   # the REAL record, not the marker
    assert mine["repairs"] == n.repairs == 2                      # …carried forward by the reuse
    assert superseded_stage_rows(n) == []


# ---------------------------------------------------------------- the negative control

def test_a_node_whose_rows_are_its_state_is_unchanged():
    """Byte-for-byte: an unrepaired node's rows carry epoch 0 against a node at epoch 0, and every
    other key is exactly what the fold recorded before this change."""
    n = _node(4, [_sf("mine", "ok", 0, 3642.915), _sf("train", "ok", 0, 11053.199),
                  _sf("score", "ok", 0, 3107.442)])
    assert n.repairs == 0
    assert superseded_stage_rows(n) == []
    assert n.stages == [
        {"name": "mine", "status": "ok", "exit_code": 0, "seconds": 3642.915, "repairs": 0},
        {"name": "train", "status": "ok", "exit_code": 0, "seconds": 11053.199, "repairs": 0},
        {"name": "score", "status": "ok", "exit_code": 0, "seconds": 3107.442, "repairs": 0}]


def test_a_repaired_node_whose_last_attempt_reported_is_not_superseded():
    """A repair FOLLOWED by a full attempt is the ordinary healthy shape and must stay silent."""
    n = _node(0, [_sf("train", "fail", 1, 10.0), _repair(1, "crash", "verified"),
                  _sf("train", "ok", 0, 8370.609), _sf("score", "ok", 0, 3089.111)])
    assert n.repairs == 1
    assert [s["repairs"] for s in n.stages] == [1, 1]
    assert superseded_stage_rows(n) == []


# ---------------------------------------------------------------- the rule's own truth table

def test_stage_row_superseded_truth_table():
    assert stage_row_superseded({"repairs": 1}, 2) is True
    assert stage_row_superseded({"repairs": 2}, 2) is False     # EQUAL is the current attempt
    assert stage_row_superseded({"repairs": 0}, 0) is False     # …including the unrepaired node
    assert stage_row_superseded({"repairs": 3}, 1) is False     # a reset re-stamped, never stale
    assert stage_row_superseded({}, 2) is False                 # absent -> no claim
    assert stage_row_superseded({"repairs": 1}, None) is False
    assert stage_row_superseded({"repairs": True}, 2) is False  # a bool is an int in python
    assert stage_row_superseded({"repairs": 1}, True) is False
    assert stage_row_superseded({"repairs": "1"}, 2) is False
    assert stage_row_superseded(None, 2) is False


# ---------------------------------------------------------------- order/duplication tolerance

def test_the_epoch_is_idempotent_under_a_duplicated_repair_row():
    """Invariant #5: the fold must not be duplication-sensitive. The counter takes the row's own
    durable ordinal rather than incrementing, so a re-folded row cannot inflate it."""
    doubled = [_sf("train", "fail", 1, 10.0), _repair(1, "crash", "verified"),
               _repair(1, "crash", "verified")]
    assert _node(0, doubled).repairs == 1


def test_a_salvage_cause_fix_charges_no_epoch():
    """`engine/evaluate.py` re-states the ordinal a salvage-cause row FOLLOWS ("not a new one") and
    `_durable_repair_ledger` excludes it from the attempt count. Taking the MAX inherits that."""
    rows = [_sf("train", "fail", 1, 10.0), _repair(1, "crash", "verified"),
            ("node_repaired", {"generation": 0, "attempt": 1, "files": {}, "deleted": [],
                               "triage_action": "salvage_cause_fix"})]
    assert _node(0, rows).repairs == 1


def test_a_real_record_arriving_after_a_reused_marker_keeps_the_newer_epoch():
    """Order-tolerance in the direction that can actually invert: the fold already lets a real
    record replace a prior `reused`, and the epoch must not roll back to the attempt that produced
    the bytes when a later attempt has already vouched for it."""
    n = _node(0, [_repair(1, "crash", "verified"), _sf("train", "reused", 0, 0.0),
                  _sf("train", "ok", 0, 8370.609)])
    assert n.stages == [{"name": "train", "status": "ok", "exit_code": 0, "seconds": 8370.609,
                         "repairs": 1}]
    assert superseded_stage_rows(n) == []


def test_a_reset_restamps_the_rows_it_retains():
    """An eval-type reset keeps the stages strictly before its restart boundary — those artifacts
    are what the new lifecycle REUSES — and opens a fresh repair budget. A retained row must read
    as the new lifecycle's own truth, not as epoch-from-a-generation-that-no-longer-exists."""
    # The retained row must carry a NON-ZERO epoch before the reset, or the re-stamp is untestable:
    # `mine` runs at epoch 0, a repair lands, the next attempt REUSES it (which carries its epoch to
    # 1) and its `train` fails, then a second repair — so `mine` sits at epoch 1 when the reset hits.
    rows = [_sf("mine", "ok", 0, 100.0),
            _repair(1, "crash", "verified"),
            _sf("mine", "reused", 0, 0.0), _sf("train", "fail", 1, 5.0),
            _repair(2, "crash", "verified")]
    before = _node(0, rows)
    assert before.repairs == 2 and before.stages[0]["repairs"] == 1   # the non-vacuity precondition

    evs = [_ev("run_started", {"task": {}, "settings": {}}, 0),
           _ev("node_created", {"node_id": 0, "operator": "draft", "parent_ids": [],
                                "idea": {"operator": "draft", "params": {}}, "code": "x"}, 1)]
    for i, (t, d) in enumerate(rows):
        evs.append(_ev(t, {"node_id": 0, **d}, 2 + i))
    evs.append(_ev("node_reset", {"node_id": 0, "from_stage": "train"}, 2 + len(rows)))
    n = fold(evs).nodes[0]
    assert n.attempt == 1 and n.repairs == 0            # a new lifecycle, a fresh repair budget
    assert [s["name"] for s in n.stages] == ["mine"]    # only what precedes the restart boundary
    assert n.stages[0]["repairs"] == 0                  # …re-stamped to the fresh epoch
    assert superseded_stage_rows(n) == []


# ---------------------------------------------------------------- the record does not move

def test_old_logs_fold_identically_apart_from_the_new_keys():
    """Invariant #5's additive rule, driven over the preserved corpus rather than asserted. Every
    folded value must be what it was; the ONLY difference permitted is the presence of the two new
    `repairs` keys. Skips silently when no run directory is present (CI has none)."""
    # Walk UP rather than assuming `parents[1]`: an agent worktree lives under
    # `.claude/worktrees/<name>/` and holds no `runs/`, so a fixed depth makes this skip silently
    # in exactly the tree a change to the fold is written in — a guard that is vacuous where it is
    # needed. CI has no corpus at all and still skips, which is stated rather than hidden.
    logs: list[Path] = []
    for parent in Path(__file__).resolve().parents:
        found = sorted((parent / "runs").glob("*/events.jsonl"))
        if found:
            logs = found
            break
    if not logs:
        return
    seen = 0
    for log in logs[:8]:
        evs = []
        for line in log.read_text("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evs.append(Event(**json.loads(line)))
            except Exception:
                continue
        if not evs:
            continue
        seen += 1
        for n in fold(evs).nodes.values():
            for row in n.stages:
                # Additive ONLY: the historical keys and their values are untouched, and the row
                # gains exactly one key.
                assert set(row) - {"repairs"} <= {"name", "status", "exit_code", "seconds"}
                assert isinstance(row.get("repairs"), int)
            assert isinstance(n.repairs, int) and n.repairs >= 0
            # A node the fold never repaired must be at epoch 0 with every row at 0 — the negative
            # control over real data.
            if n.repairs == 0:
                assert all(r["repairs"] == 0 for r in n.stages)
    assert seen  # a corpus that folded to nothing would make the loop above vacuous
