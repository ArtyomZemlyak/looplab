"""A reader is shown the coordinates that RAN, with the proposal in brackets where they differ.

`Idea.params` is a proposal. Under `params_style: "none"` the engine applies nothing and the
Developer realises it by editing the repo, so a repair that fits a run into memory moves the numbers
while the proposal stays frozen at what was asked for.

Measured on `runs/e5small-dr-unified-v4` node 3 — the run's champion for four days: proposed
`batch_size 8192 / accum 2 / n_epochs 15`, APPLIED `4096 / 4 / 3` after six repairs. A quarter of the
effective batch and a fifth of the schedule. `_state_brief` printed the proposal as "Best so far:
… params=…" on every proposal cycle, so every "one knob off the champion" idea was sized against a
recipe that never ran.
"""
from __future__ import annotations

from types import SimpleNamespace

from looplab.core.param_carriers import node_params_brief


def _node(declared, applied=None, diverged=None):
    prov = None
    if applied is not None:
        record = {"applied": applied}
        if diverged is not None:
            record["diverged"] = diverged
        prov = {"applied_params": record}
    return SimpleNamespace(idea=SimpleNamespace(params=dict(declared)), metric_provenance=prov)


def test_the_applied_value_comes_first_and_the_proposal_is_bracketed():
    """THE ORDER IS THE POINT — node 3's real numbers, verbatim."""
    out = node_params_brief(_node(
        {"train.training.batch_size": 8192.0, "train.training.n_epochs": 15.0},
        applied={"train.training.batch_size": 4096.0, "train.training.n_epochs": 3.0},
        diverged=[{"param": "train.training.batch_size", "declared": 8192.0, "applied": 4096.0},
                  {"param": "train.training.n_epochs", "declared": 15.0, "applied": 3.0}]))
    assert "train.training.batch_size=4096.0 (proposed 8192.0)" in out
    assert "train.training.n_epochs=3.0 (proposed 15.0)" in out
    assert out.index("4096.0") < out.index("8192.0"), "the fact must precede the wish"


def test_a_param_that_did_not_move_is_not_bracketed():
    """Bracketing everything would train the reader to ignore the brackets."""
    out = node_params_brief(_node({"a": 1.0}, applied={"a": 1.0}, diverged=[]))
    assert out == "a=1.0"


def test_the_divergence_COUNT_is_stated_even_when_the_rows_fall_outside_the_cap():
    """'These are the numbers that ran' is only trustworthy if the reader is told how many moved —
    and with a small cap the moved ones can be the ones clipped away."""
    applied = {f"p{i:02d}": float(i) for i in range(20)}
    diverged = [{"param": "p19", "declared": 99.0, "applied": 19.0}]
    out = node_params_brief(_node({}, applied=applied, diverged=diverged), cap=3)
    assert "+17 more" in out
    assert "[1 of 20 moved from the proposal]" in out


def test_no_applied_record_falls_back_to_the_declaration_UNMARKED():
    """Absent evidence is not evidence of agreement. A pre-2026-08-20 node, or one whose metric was
    never bound, must read exactly as it did before — nothing bracketed, nothing implied."""
    out = node_params_brief(_node({"a": 1.0}))
    assert out == repr({"a": 1.0})
    assert "proposed" not in out and "moved" not in out


def test_an_empty_or_malformed_record_is_the_same_fallback():
    for prov in ({}, {"applied_params": None}, {"applied_params": {}},
                 {"applied_params": {"applied": None}}, {"applied_params": {"applied": {}}}):
        node = SimpleNamespace(idea=SimpleNamespace(params={"a": 1.0}), metric_provenance=prov)
        assert node_params_brief(node) == repr({"a": 1.0}), prov


def test_a_node_with_nothing_at_all_says_so_rather_than_printing_an_empty_dict():
    assert node_params_brief(SimpleNamespace(idea=None, metric_provenance=None)) == "(none recorded)"


def test_a_malformed_diverged_row_cannot_break_the_render():
    """The record is persisted data; a row missing its `param` must cost that row's bracket and
    nothing else."""
    out = node_params_brief(_node(
        {"a": 1.0}, applied={"a": 2.0},
        diverged=[{"declared": 1.0}, "junk", {"param": "a", "declared": 1.0}]))
    assert "a=2.0 (proposed 1.0)" in out


def test_the_live_champion_reads_correctly_end_to_end():
    """Against the real run, not a fixture: node 3's brief must show 4096/3 and bracket 8192/15."""
    from pathlib import Path

    import pytest

    log = Path("/home/jovyan/data/looplab/runs/e5small-dr-unified-v4/events.jsonl")
    if not log.exists():
        pytest.skip("the live run is not on this box")
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    node = fold(EventStore(log).read_all()).nodes[3]
    out = node_params_brief(node)
    assert "train.training.batch_size=4096.0 (proposed 8192.0)" in out
    assert "train.training.n_epochs=3.0 (proposed 15.0)" in out


# ---------------------------------------------------------------------------------------------
# THE WIRING. Every assertion above tests the renderer; none would redden if `_state_brief` still
# printed `idea.params`, which is the whole defect.
# ---------------------------------------------------------------------------------------------

def test_the_researcher_brief_shows_the_applied_coordinates(tmp_path):
    """Through `_state_brief`, the line the Researcher reads on every proposal cycle."""
    from looplab.agents.roles import _state_brief
    from looplab.core.models import RunState

    log = "/home/jovyan/data/looplab/runs/e5small-dr-unified-v4/events.jsonl"
    from pathlib import Path

    import pytest
    if not Path(log).exists():
        pytest.skip("the live run is not on this box")
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    state: RunState = fold(EventStore(log).read_all())
    # Pin the PARENT line, not the champion line: which node is best moves while the run runs, and a
    # test that hard-codes it measures the clock. Node 3 is terminal and its record is frozen.
    brief = _state_brief(state, state.nodes[3])
    refine_line = next(l for l in brief.splitlines() if l.startswith("Refine from node 3:"))
    assert "train.training.batch_size=4096.0 (proposed 8192.0)" in refine_line, refine_line
    assert "train.training.n_epochs=3.0 (proposed 15.0)" in refine_line, refine_line
    # Scoped to the line under test, deliberately. A FAILED node has no applied record and falls
    # back to its declaration UNMARKED — documented behaviour — so the raw proposal dict legitimately
    # appears elsewhere in the digest. Asserting its absence from the whole brief would be asserting
    # the fallback away.
    assert "'train.training.batch_size': 8192.0" not in refine_line
