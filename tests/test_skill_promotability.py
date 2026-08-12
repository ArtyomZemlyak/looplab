"""A SKILL is a technique a later run could apply — not a fact about one node.

"The auto classifier is rubbish, it needs improving." It is not bad at ranking; it never asked the
question. The promotion gate tested three things — supported, positive delta, non-empty statement —
and none of them is "does this generalize". Measured 2026-08-12 over the 27 auto-skills the shipped
store had accumulated, EVERY ONE was instance-specific:

    perturb best node 8 (metric=5.4404437)
    perturb node 9 (params={'x': 3.7898})
    mean-merge of nodes 0,1

…including the same operation five times under five different node numbers, as five separate
"skills". This file is the truth table for the predicate that stops it, driven by that exact corpus.
"""
from __future__ import annotations

import pytest

from looplab.engine.memory import promotable_skill_statement

# Verbatim from `looplab-memory/skills.quarantined-2026-08-12/`. If a change lets any of these
# through again, the store fills back up with the same 27 rows.
SHIPPED_JUNK = [
    "perturb best node 8 (metric=5.4404437)",
    "perturb best node 5 (metric=6.03485842) [novelty-gate: nudged off a near-duplicate]",
    "perturb best node 0 (metric=53.054167369999995)",
    "perturb node 9 (params={'x': 3.7898})",
    "perturb node 4 (params={'x': 5.0})",
    "mean-merge of nodes 0,1",
    "mean-merge of nodes 1,3",
]


@pytest.mark.parametrize("statement", SHIPPED_JUNK)
def test_every_skill_the_store_actually_accumulated_is_refused(statement):
    assert promotable_skill_statement(statement) is False


# Real technique statements from the same corpus and from the rubert run's lessons. A false negative
# here silently loses procedural memory, which is the whole point of the tier — so the predicate is
# deliberately conservative and these must all survive.
TRANSFERABLE = [
    "ensemble/recombine the top solutions into one stronger pipeline",
    "two-stage temperature was the decisive factor",
    "R-Drop was the only structural change that improved over DCL+threshold",
    "Keep the calibrated InfoNCE loss temperature fixed; deviating from it regresses recall",
    "Initializing the distributed process group before any collective call fixes the crash",
    "Adding hard-negative mining raises test recall above the in-batch-only plateau",
]


@pytest.mark.parametrize("statement", TRANSFERABLE)
def test_a_real_technique_still_promotes(statement):
    assert promotable_skill_statement(statement) is True


def test_it_refuses_what_cannot_transfer_and_names_why():
    """The three shapes, each on its own, so a loosened pattern fails one case rather than none."""
    assert promotable_skill_statement("perturb node 3 then re-evaluate") is False   # a node id
    assert promotable_skill_statement("raise the batch size, metric=0.88 after") is False  # a value
    assert promotable_skill_statement("apply params={'lr': 0.001} to the head") is False   # a literal
    assert promotable_skill_statement("set the config to {'lr': 0.001}") is False          # bare dict


def test_a_claim_too_short_to_hold_a_technique_is_refused():
    # The store derives titles and file names from this text, so an empty-ish claim also mints an
    # unreadable file.
    assert promotable_skill_statement("x") is False
    assert promotable_skill_statement("") is False
    assert promotable_skill_statement(None) is False
    assert promotable_skill_statement("   use it   ") is False
    assert promotable_skill_statement("use dropout on the head") is True


def test_refusing_a_skill_does_not_refuse_the_LESSON():
    """This is a judgement about whether a sentence generalizes, not about whether it is true. The
    lesson tier keeps the claim with its evidence either way — only the procedural tier is gated."""
    import inspect

    from looplab.engine import lessons_distill

    source = inspect.getsource(lessons_distill)
    gate = source[source.index("for h in final.research_cards():"):]
    gate = gate[:gate.index("skills.append")]
    assert "promotable_skill_statement(h.statement)" in gate
    # …and the gate sits AFTER the lessons are appended, so the append is not inside it.
    assert source.index("self._e._append_lessons") < source.index("promotable_skill_statement")


def test_the_word_node_alone_is_not_disqualifying():
    """Only a node ID is instance-specific. "node" as a common noun appears in real techniques, and
    rejecting those would quietly empty the tier the other way."""
    assert promotable_skill_statement("prefer a shallower node expansion order") is True
    assert promotable_skill_statement("re-evaluate the champion node before merging") is True
