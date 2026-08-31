"""Two rules the goal card never stated, both invisible from inside a run.

Every other clause in this card describes something the model can eventually observe: the score
formula, what the warm-up absorbs, what `eval_train` costs. These two cannot be observed at all.

  (a) THE PER-INSTANCE CEILING. In isolated mode the harness gives each instance's subprocess
      `(1 + WARMUP_MULTIPLIER) * reference_time * TARGET_TIME_MULTIPLIER` seconds -- 6 runs, each
      allowed 10x the reference. Cross it and the instance is killed, which scores INVALID, not
      slow. A model only ever sees the runs that survived; `eval_train` never reports the ceiling
      they survived under, so no amount of measuring reveals it.

  (b) THE BEST EVALUATED NODE IS SUBMITTED, not the last one. Measured 2026-08-30 on `remDL2`:
      node_0 at 14.29, node_1 at 13.98, submitted node_0. Before that run every probe's last node
      happened to also be its best, so the rule was invisible even in the corpus. A model that
      does not know it has every reason to protect a working solver instead of attacking it.

Both are DERIVED from the arena's own constants where they can be, for the reason every other
derived clause in `make_task.py` is: a hand-typed 10 keeps being quoted after the constant moves.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MAKE = REPO / "benchmarks" / "algotune" / "make_task.py"
ARENA = Path("/var/tmp/looplab-bench/AlgoTune")

pytestmark = pytest.mark.skipif(
    not ARENA.exists(), reason="AlgoTune checkout not on this box"
)


def _goal(tmp_path, *flags):
    out = tmp_path / "ws"
    r = subprocess.run(
        [sys.executable, str(MAKE), "--algotune-root", str(ARENA),
         "--task", "pde_heat1d", "--out", str(out), *flags],
        capture_output=True, text=True, timeout=900,
    )
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    spec = next(out.rglob("algotune_*.json"))
    return json.loads(spec.read_text())["goal"]


def test_a_the_card_states_the_per_instance_ceiling(tmp_path):
    g = _goal(tmp_path, "--full-context", "--deliver", "--one-card", "--enforce-rules")
    assert "CEILING ON HOW SLOW YOUR SOLVER MAY BE" in g, (
        "the card still does not tell the model there is a per-instance timeout at all"
    )
    assert "KILLED" in g and "INVALID" in g, (
        "the card mentions a ceiling but not that crossing it scores invalid rather than slow"
    )


def test_a_the_ceiling_moves_when_the_arena_moves(tmp_path):
    """A fake arena with DIFFERENT constants must produce a card quoting those.

    The first version of this test compared per_instance_cap's output against the live arena's
    constants -- which is satisfied by `return (10.0, 5.0, 10.0)` typed by hand. Mutation caught it
    (2026-08-31): hardcoding the numbers left the whole file green. The only way to tell derived
    from typed is to change the source and watch the answer change.
    """
    sys.path.insert(0, str(MAKE.parent))
    try:
        import make_task
    finally:
        sys.path.pop(0)

    fake = tmp_path / "fake-arena"
    (fake / "AlgoTuner" / "utils" / "evaluator").mkdir(parents=True)
    (fake / "AlgoTuner" / "utils" / "evaluator" / "runner.py").write_text(
        "TARGET_TIME_MULTIPLIER = 7.0  # not the real one\n")
    (fake / "AlgoTuner" / "utils" / "timing_config.py").write_text(
        "WARMUP_MULTIPLIER: float = 3.0\n")

    cap = make_task.per_instance_cap(fake)
    assert cap is not None, "could not read constants out of a well-formed arena tree"
    assert (cap[0], cap[1]) == (7.0, 3.0), (
        f"per_instance_cap returned {cap[:2]} for an arena whose constants are (7.0, 3.0) -- the "
        "numbers are typed into the card, not read from the arena it is built against"
    )


def test_a_the_card_quotes_the_constants_the_harness_enforces(tmp_path):
    """And the derived numbers must actually reach the text."""
    sys.path.insert(0, str(MAKE.parent))
    try:
        import make_task
    finally:
        sys.path.pop(0)

    cap = make_task.per_instance_cap(ARENA)
    assert cap is not None, "per_instance_cap could not read the arena's constants"
    mult, warm, _floor = cap

    sys.path.insert(0, str(ARENA))
    try:
        from AlgoTuner.utils.evaluator.runner import TARGET_TIME_MULTIPLIER
        from AlgoTuner.utils.timing_config import WARMUP_MULTIPLIER
    finally:
        sys.path.pop(0)

    assert (mult, warm) == (float(TARGET_TIME_MULTIPLIER), float(WARMUP_MULTIPLIER)), (
        "the card's ceiling no longer matches the constants the harness enforces"
    )

    g = _goal(tmp_path, "--full-context", "--deliver", "--one-card", "--enforce-rules")
    assert f"* {mult:.0f}`" in g or f"{mult:.0f}x the reference" in g, (
        f"the multiplier {mult} the harness uses does not appear in the card:\n"
        + g[g.find("CEILING"):g.find("CEILING") + 500]
    )


def test_a_no_arena_means_no_claim(tmp_path):
    """Where the constants cannot be read, the card must say nothing rather than invent a number."""
    sys.path.insert(0, str(MAKE.parent))
    try:
        import make_task
    finally:
        sys.path.pop(0)
    assert make_task.per_instance_cap(tmp_path / "not-an-arena") is None, (
        "per_instance_cap invented a ceiling for a root with no AlgoTune in it"
    )


def test_b_the_card_says_the_best_evaluated_node_is_the_one_submitted(tmp_path):
    g = _goal(tmp_path, "--full-context", "--deliver", "--one-card", "--enforce-rules")
    assert "BEST EVALUATED SOLVER IS WHAT GETS SUBMITTED" in g, (
        "the card still lets the model assume its LAST version is the one that counts"
    )
    # NOT a bare "costs you nothing": mutation on 2026-08-31 showed that phrase already appears in
    # an unrelated clause of the same card, so the assertion passed with KEEP_BEST's consequence
    # deleted. Anchor inside the clause itself.
    i = g.index("BEST EVALUATED SOLVER IS WHAT GETS SUBMITTED")
    clause = g[i:i + 900]
    assert "A change that turns out WORSE costs you nothing" in clause, (
        "the card states the rule but not its consequence -- that a worse attempt is free, which "
        "is the whole reason the rule is worth telling:\n" + clause[:600]
    )
    assert "never evaluated cannot be submitted" in clause or "never got around to EVALUATING" in clause, (
        "the card states the upside but not the other half: an unevaluated improvement is worth zero"
    )


def test_b_it_is_tied_to_one_card_not_to_full_context(tmp_path):
    """It is a fact about how THIS loop scores, so it travels with the loop's own clause."""
    with_flag = _goal(tmp_path / "a", "--deliver", "--one-card")
    without = _goal(tmp_path / "b", "--deliver")
    assert "BEST EVALUATED SOLVER" in with_flag
    assert "BEST EVALUATED SOLVER" not in without, (
        "the clause leaks into runs that did not ask for the one-card contract, which would change "
        "the measurement for an arm that never adopted it"
    )
