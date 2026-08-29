"""What OTHER nodes had to repair — the one thing lineage cannot carry.

Files DO flow from parent to child (`repo_developer.implement_from`), and correctly. What cannot
flow is a fix a SIBLING found, because a node becomes a parent only by winning on metric. Measured
on `runs/e5small-dr-unified-v4`: nodes 4, 5, 6, 8 and 9 all improve from node 3 — a star, not a
chain. Node 6 repaired the mine stage, wrote the diagnosis into its own `prep.py`, and scored
0.781781 against node 3's 0.790898. It will never be a parent, so node 8 inherited node 3's six
files verbatim and hit the identical failure.

The ledger is INFORMATION, never files: nothing is inherited through it, so it cannot corrupt a
lineage. Its job is to rank what belongs in the SOURCE REPO, where every future node gets it
regardless of who won.
"""
from __future__ import annotations

from looplab.core.models import Event, RunState
from looplab.events.replay import fold


def _ev(seq: int, node_id, attempt, reason, paths, *, generation=0, rationale=None):
    data = {"node_id": node_id, "attempt": attempt, "generation": generation,
            "reason": reason, "changed": paths}
    if rationale is not None:
        data["rationale"] = rationale
    return Event(v=1, seq=seq, ts=1000.0 + seq, type="node_repaired", data=data)


def _fold(events) -> RunState:
    return fold(list(events))


def test_a_repair_is_recorded_with_its_paths_and_reason():
    st = _fold([_ev(0, 6, 1, "expect_failed", ["prep.py"], rationale="the stage skipped its write")])
    assert len(st.repair_ledger) == 1
    row = st.repair_ledger[0]
    assert row["node_id"] == 6 and row["reason"] == "expect_failed"
    assert row["paths"] == ["prep.py"]
    assert "skipped its write" in (row["rationale"] or "")


def test_a_duplicate_fold_does_not_double_count():
    """Replay must be a pure function of the log: the same row folded twice is one fact, and the
    (node, attempt, generation) key is what makes that true without a mutable guard."""
    e = _ev(0, 6, 1, "crash", ["a.py"])
    assert len(_fold([e, e]).repair_ledger) == 1


def test_a_row_with_no_declared_paths_falls_back_to_the_files_it_carried():
    """`changed` is newer than the event. A row written before that column existed must still name
    what it touched, or the whole history reads as "repaired nothing"."""
    ev = Event(v=1, seq=0, ts=1.0, type="node_repaired",
               data={"node_id": 3, "attempt": 1, "generation": 0, "reason": "oom",
                     "files": {"z.py": "...", "a.py": "..."}})
    assert _fold([ev]).repair_ledger[0]["paths"] == ["a.py", "z.py"]


def test_candidates_rank_by_DISTINCT_NODES_not_by_repair_count():
    """THE RANKING RULE, and it is the whole point. One node repairing a file four times is one
    node having a bad day; four nodes repairing it once each is a property of the REPO. Ranking by
    raw repair count would promote the first and bury the second."""
    st = _fold([
        _ev(0, 1, 1, "crash", ["lonely.py"]),
        _ev(1, 1, 2, "crash", ["lonely.py"]),
        _ev(2, 1, 3, "crash", ["lonely.py"]),
        _ev(3, 1, 4, "crash", ["lonely.py"]),
        _ev(4, 2, 1, "oom", ["shared.yaml"]),
        _ev(5, 3, 1, "oom", ["shared.yaml"]),
    ])
    ranked = st.repair_candidates()
    assert [c["path"] for c in ranked] == ["shared.yaml", "lonely.py"]
    assert ranked[0]["node_count"] == 2 and ranked[0]["nodes"] == [2, 3]
    assert ranked[1]["node_count"] == 1


def test_a_candidate_carries_why_it_was_repaired():
    """A path alone is not actionable. The reasons, counted, are what tell an operator whether this
    is one recurring defect or several unrelated ones that happen to share a file."""
    st = _fold([
        _ev(0, 1, 1, "oom", ["config.yaml"]),
        _ev(1, 2, 1, "oom", ["config.yaml"]),
        _ev(2, 3, 1, "timeout", ["config.yaml"]),
    ])
    reasons = st.repair_candidates()[0]["reasons"]
    assert list(reasons) == ["oom", "timeout"], reasons
    assert reasons["oom"] == 2


def test_the_ledger_is_bounded():
    """A long run repairs many times, and an unbounded ledger becomes a prompt that crowds out the
    code it annotates."""
    from looplab.events.replay import _REPAIR_LEDGER_MAX

    st = _fold(_ev(i, i, 1, "crash", [f"f{i}.py"]) for i in range(_REPAIR_LEDGER_MAX + 25))
    assert len(st.repair_ledger) == _REPAIR_LEDGER_MAX


def test_a_malformed_row_is_skipped_rather_than_folded_as_a_fact():
    st = _fold([
        Event(v=1, seq=0, ts=1.0, type="node_repaired",
              data={"node_id": "six", "attempt": 1, "reason": "crash", "changed": ["a.py"]}),
        _ev(1, 6, 1, "crash", ["a.py"]),
    ])
    assert [r["node_id"] for r in st.repair_ledger] == [6]
