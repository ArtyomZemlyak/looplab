"""A paid proposal that a card planner refuses leaves a receipt — on ALL THREE lanes.

bd182357 added the receipt to the per-action funnel with a comment reading "THE ONLY PLACE A
DISCARDED PROPOSAL IS RECEIPTED ... and nowhere else". That overstated its own coverage. Two other
passes run a paid propose and then refuse one:

  * the BATCH draft lane (`novelty.py::_link_card`) fell through with nothing written — and a
    `duplicate` disposition is a busy board's ORDINARY answer, so this is not an edge case. It is
    byte-for-byte the loss bd182357 measured on `runs/e5small-dr-unified-v8`: a propose of 24.1 min
    / 81 provider calls / 4,270,000 tokens leaving no `card_added`, no `card_enriched`, no
    `hypothesis_added` and no `card_dropped`.
  * the Layer-5 SPECULATIVE producer was worse: it DOES emit the receipt under its buffered-intents
    sink, and `speculation.py::_serve_raw_card_stage` dropped `result.audit_events` on both failure
    returns — so the receipt was captured and then thrown away.

The row itself is now one constructor, because three hand-written copies of it are how the lanes
came to disagree about whether the row exists at all.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from looplab.engine import novelty as novelty_mod
from looplab.engine import orchestrator as orch_mod
from looplab.engine import speculation as spec_mod
from looplab.engine.card_reservation import discarded_proposal_receipt

from _source_scan import called_names, function_tree


class _Idea:
    def __init__(self, hypothesis=""):
        self.hypothesis = hypothesis


# --- the shape ----------------------------------------------------------------------------------

def test_a_duplicate_and_an_unplannable_are_DIFFERENT_facts():
    """They have different remedies: one says another card already owns the action, the other that
    the plan named no bounded action at all. Collapsing them makes the row uncountable."""
    dup = discarded_proposal_receipt("duplicate", 7, _Idea("try a bigger batch"), lane="planner")
    other = discarded_proposal_receipt("unplannable", 7, _Idea(""), lane="planner")

    assert dup["kind"] == "card_duplicate" and other["kind"] == "card_unplannable"
    assert dup["reason"] != other["reason"]
    assert dup["disposition"] == "duplicate"


def test_the_receipt_names_the_lane_that_discarded():
    """The whole reason this constructor exists is that the lanes were measurably not equivalent.
    A row that cannot say which pass wrote it cannot show that they have become equivalent."""
    lanes = {discarded_proposal_receipt("duplicate", 1, _Idea(), lane=lane)["pass"]
             for lane in ("planner", "batch_planner", "speculative")}
    assert lanes == {"planner", "batch_planner", "speculative"}


def test_the_receipt_carries_the_hypothesis_and_never_raises():
    assert discarded_proposal_receipt("duplicate", 1, _Idea("x" * 999), lane="planner"
                                      )["hypothesis"].startswith("x")
    for junk in (None, object(), _Idea(hypothesis=123)):
        row = discarded_proposal_receipt("duplicate", 1, junk, lane="planner")
        assert row["hypothesis"] == "", "an unreadable idea costs the text, never the row"


# --- all three lanes emit it ---------------------------------------------------------------------

def _calls_to(func, name: str) -> int:
    """How many REAL calls in *func* target *name*. AST via the shared scanner, not a substring: a
    commented-out call is not an `ast.Call`, so a lane cannot be covered by a comment."""
    return sum(1 for target in called_names(func)
               if target == name or target.endswith(f".{name}"))


def test_the_per_action_funnel_emits_it():
    assert _calls_to(orch_mod.Engine._prepare_node_idea, "discarded_proposal_receipt") >= 1


def test_the_batch_draft_lane_emits_it():
    """MUTATION: delete the branch -> a `duplicate` on the batch lane is silent again, which is the
    ordinary answer on a busy board."""
    source = inspect.getsource(novelty_mod)
    start = source.index("def _propose_batch_ideas") if "def _propose_batch_ideas" in source else 0
    assert "discarded_proposal_receipt(" in source, "the batch lane never builds the receipt"
    tree = ast.parse(source)
    emitters = [node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "discarded_proposal_receipt"]
    assert emitters, "no real call to the receipt constructor in novelty.py"
    lanes = {kw.value.value for call in emitters for kw in call.keywords
             if kw.arg == "lane" and isinstance(kw.value, ast.Constant)}
    assert lanes == {"batch_planner"}


def test_the_speculative_lane_publishes_its_buffered_receipts():
    """The third lane's defect was the inverse: the receipt WAS built, under
    `_capture_proposal_events`, and then dropped on the way out.

    MUTATION: remove either publish -> `producer_failed` / `proposal_refused` return with the
    buffered intents still in the discarded result, which is capture-then-throw-away.
    """
    tree = function_tree(spec_mod.SpeculationMixin._serve_raw_card_stage)
    published_before_return: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        body = node.body
        has_publish = any(
            isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute)
            and stmt.value.func.attr == "_publish_proposal_events" for stmt in body)
        returns = [stmt for stmt in body if isinstance(stmt, ast.Return)]
        if has_publish and returns:
            for ret in returns:
                for const in ast.walk(ret):
                    if isinstance(const, ast.Constant) and isinstance(const.value, str):
                        published_before_return.append(const.value)
    assert "producer_failed" in published_before_return
    assert "proposal_refused" in published_before_return


def test_the_stale_fence_refusal_still_DROPS_them():
    """The one case that is genuinely different, and its argument is in the source: a moved fence
    abandons a SUCCESSFUL proposal that will be re-made from the same state, so publishing would
    put two receipts in the log for one eventual card. The failure paths re-make nothing."""
    source = inspect.getsource(spec_mod.SpeculationMixin._serve_raw_card_stage)
    refusal_return = 'return True, False, str(getattr(self, "_card_stage_refusal", "") or "unrecorded")'
    assert refusal_return in source
    # ...and nothing publishes between the attach branch's own return and that one. (Anchored on the
    # RETURN, not on the first mention of `_card_stage_refusal`, which is in the docstring.)
    attach_return = "return True, False, None"
    between = source[source.index(attach_return) + len(attach_return):source.index(refusal_return)]
    assert "_publish_proposal_events" not in between


def test_no_lane_hand_writes_the_payload_any_more():
    """Three copies of one row is how they came to disagree about whether the row exists.

    MUTATION: re-inline the dict in any lane -> a later change to `kind`/`reason` lands on one lane
    and the other two keep the old spelling, which is the shape of the original defect.
    """
    for module in (novelty_mod, orch_mod, spec_mod):
        source = Path(module.__file__).read_text(encoding="utf-8-sig", errors="replace")
        assert '"kind": "card_duplicate"' not in source, (
            f"{Path(module.__file__).name} hand-writes the discarded-proposal payload")
