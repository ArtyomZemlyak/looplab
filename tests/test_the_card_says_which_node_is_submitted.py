"""§84's rule, in the card at last: the best EVALUATED node is submitted, not the last one.

A run sees its own nodes in order and has every reason to assume the newest is the answer. Measured
over all 17 multi-node probes in the corpus: ELEVEN ended on a node that was not their best, none on
a node better, paired sign test p = 1/2048. Median submitted TRAIN score 130.81 with the rule
against 18.38 without it. `remEE6` scored 234.89 and then finished by scoring 0.0 -- the rule is the
only reason that run has a number at all.

Shipped 2026-09-06, after the twelve-batch arm read out (§284). Not before: the card is read by both
sides of that comparison, and changing it mid-arm would have measured two things at once.

These tests read the GENERATED card, not the source, because the source is not what the model sees.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MAKE_TASK = REPO / "benchmarks" / "algotune" / "make_task.py"
ALGOTUNE = Path("/var/tmp/looplab-bench/AlgoTune")


def _card(tmp_path) -> dict:
    if not ALGOTUNE.is_dir():
        import pytest
        pytest.skip("no AlgoTune checkout on this box")
    subprocess.run([sys.executable, str(MAKE_TASK), "--algotune-root", str(ALGOTUNE),
                    "--task", "edge_expansion", "--out-dir", str(tmp_path)],
                   check=True, capture_output=True, timeout=600)
    return json.loads((tmp_path / "algotune_edge_expansion.json").read_text(encoding="utf-8"))


def test_the_card_says_the_best_evaluated_node_is_the_one_submitted(tmp_path):
    goal = _card(tmp_path)["goal"]
    assert "BEST **EVALUATED** ONE, NOT YOUR LAST" in goal, goal[-1200:]


def test_it_says_an_unevaluated_node_cannot_be_submitted(tmp_path):
    """`remPde` spent 74 % of its dollar before any node existed -- the same rule from the other
    end. Stating only "your best is kept" would leave that half unsaid."""
    goal = _card(tmp_path)["goal"]
    assert "never evaluated cannot be submitted" in goal, goal[-1200:]


def test_it_says_a_worse_later_node_does_not_replace_a_better_earlier_one(tmp_path):
    """This is the half the corpus actually punishes: eleven runs ended on a worse node."""
    goal = _card(tmp_path)["goal"]
    assert "does not replace an earlier one that scored better" in goal, goal[-1200:]


def test_it_draws_both_consequences_not_just_the_rule(tmp_path):
    """A rule with no consequence is a fact to nod at. The two that follow point opposite ways --
    take the late risk, and get things evaluated early -- and a card that gave only one of them
    would push the run off balance."""
    goal = _card(tmp_path)["goal"]
    assert "cannot cost you what you have already banked" in goal, goal[-1200:]
    # THE WHOLE SECOND CONSEQUENCE, not just its opening. A mutation that kept "worth exactly
    # nothing" and cut "get code evaluated early and often rather than perfecting one submission
    # you may not have the budget to grade" survived the first version of this test: the clause
    # that tells the run WHAT TO DO was gone and the assertion still passed.
    assert "get code evaluated early and often" in goal, goal[-1200:]
    assert "may not have the budget to grade" in goal, goal[-1200:]
    assert "worth exactly nothing" in goal, goal[-1200:]


def test_the_split_sentence_is_still_there(tmp_path):
    """The new clause sits beside the held-out-split one and must not have displaced it."""
    goal = _card(tmp_path)["goal"]
    assert "THE REPORTED SCORE IS ON A SPLIT YOU CANNOT SEE" in goal
