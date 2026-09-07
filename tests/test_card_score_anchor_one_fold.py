"""The card score fence's two halves must be read from ONE fold.

`_reserve_node_build` takes `scored_against` from whatever its CALLER folded, then re-folds fresh
inside `_plan` under the log-tail CAS and — until 2026-08-31 — read the anchor's ATTEMPT from that
fresh fold. The receipt therefore carried an (old id, new attempt) pair whenever the anchor re-ran
in between.

That was unreachable by construction while the paid propose ran on the loop thread: the loop was
frozen, so the two folds were the same log. `_await_batch_proposal` offloading the propose to a
worker opened the window to the propose's whole duration (minutes, and on this box the batch lane's
proposes run while evals do).

The harm is precise and is NOT the stale id. `cards.py::card_score_fence_state` answers `stale`
exactly on `scored_against_generation != anchor_attempt`, so the mixed pair reads `current` in the
one case the generation is in the receipt to catch — "the metric the proposal was scored against no
longer exists even though the id does". The stale ID is deliberately kept: the ladder narrowed
champion-equality away on 2026-08-13 because it killed cards permanently on an unrelated node's win,
and `search/card_selection.py` asks only that the anchor be live.
"""
from __future__ import annotations

import ast
import pathlib

from looplab.core.cards import card_score_fence_state
from looplab.core.models import Event
from looplab.engine.card_reservation import CardReservationMixin, scored_anchor
from looplab.events.replay import fold

ROOT = pathlib.Path(__file__).resolve().parents[1]

_PROPOSAL = [
    ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
    ("node_created", {"node_id": 1, "operator": "draft",
                      "idea": {"operator": "draft", "hypothesis": "a baseline"}}),
    ("node_evaluated", {"node_id": 1, "metric": 0.80}),
]
# What lands while the offloaded propose is still running: the champion is re-run, so its metric —
# the number the proposal was scored against — is gone even though the node id is not.
_MID_PROPOSE = [
    ("node_reset", {"node_id": 1, "from_stage": "eval", "reason": "code changed"}),
    # …and it re-scores, so the board has a champion again — at a DIFFERENT number from the one the
    # proposal was scored on. That is the whole hazard: same id, same "best", new metric.
    ("node_evaluated", {"node_id": 1, "metric": 0.85, "generation": 1}),
]


def _fold(pairs):
    return fold([Event(type=t, data=d) for t, d in pairs])


def test_the_anchor_pair_comes_from_one_fold():
    """Both halves, from the same state object. A helper that returned only the id would leave the
    attempt to be re-read somewhere else, which is the whole defect."""
    proposal = _fold(_PROPOSAL)
    assert scored_anchor(proposal) == (1, 0)

    after_reset = _fold(_PROPOSAL + _MID_PROPOSE)
    assert after_reset.nodes[1].attempt == 1, "the fixture must actually move the attempt"
    assert scored_anchor(after_reset) == (1, 1)


def test_an_anchor_that_reran_mid_propose_records_the_attempt_it_was_SCORED_on():
    """The defect, driven at the seam that had it.

    `state` is the fresh CAS-time fold; `requested` is the caller's older id. Without the attempt
    the snapshot mixes the two folds.
    """
    proposal = _fold(_PROPOSAL)
    fresh = _fold(_PROPOSAL + _MID_PROPOSE)

    anchor_id, anchor_attempt = scored_anchor(proposal)
    fixed = CardReservationMixin._card_score_snapshot(fresh, anchor_id, anchor_attempt)
    assert fixed == (1, 0, False), (
        "the receipt must name the attempt the proposal was scored on, not the one the anchor "
        f"reached while the propose was in flight; got {fixed}")

    mixed = CardReservationMixin._card_score_snapshot(fresh, anchor_id)
    assert mixed == (1, 1, False), "guard fixture: the un-named form still reads the fresh attempt"


def test_the_mixed_pair_reads_CURRENT_and_the_one_fold_pair_reads_STALE():
    """Why it matters: the fence verdict flips, in the direction that hides the staleness."""
    fresh = _fold(_PROPOSAL + _MID_PROPOSE)
    live_attempt = fresh.nodes[1].attempt

    mixed_id, mixed_gen, mixed_empty = CardReservationMixin._card_score_snapshot(fresh, 1)
    assert card_score_fence_state(
        mixed_id, mixed_gen, mixed_empty,
        anchor_live=True, anchor_attempt=live_attempt) == "current", (
        "the two-fold pair is what made a stale card look selectable")

    one_fold_id, one_fold_gen, one_fold_empty = CardReservationMixin._card_score_snapshot(
        fresh, *scored_anchor(_fold(_PROPOSAL)))
    assert card_score_fence_state(
        one_fold_id, one_fold_gen, one_fold_empty,
        anchor_live=True, anchor_attempt=live_attempt) == "stale", (
        "with both halves from the proposal's fold the ladder can finally answer honestly")


def test_the_empty_board_answer_is_unchanged():
    """`(None, None)` is an honest anchor, not a refusal — the `scored_against_empty` triple still
    has to come out of it, or every bootstrap card becomes unscorable."""
    empty = _fold([("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})])
    assert scored_anchor(empty) == (None, None)
    assert CardReservationMixin._card_score_snapshot(empty, None, None) == (None, None, True)


def test_every_reservation_that_NAMES_an_anchor_names_its_attempt():
    """The rule, over the tree, so a fifth call site cannot reintroduce the split.

    `_plan_native_card`'s direct callers are deliberately NOT in scope: they hand it the same
    `state` object they read `best_node_id` from, so there is only one fold to begin with. This is
    about `_reserve_node_build`, which re-folds internally.
    """
    offenders = []
    for rel in ("looplab/engine/orchestrator.py", "looplab/engine/ablation.py",
                "looplab/engine/card_reservation.py", "looplab/engine/novelty.py",
                "looplab/engine/speculation.py"):
        path = ROOT / rel
        if not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "_reserve_node_build":
                continue
            kwargs = {kw.arg for kw in node.keywords}
            if "scored_against" in kwargs and "scored_against_attempt" not in kwargs:
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "these reservations name an anchor id without its attempt, so the receipt's two halves "
        "come from two folds again:\n  " + "\n  ".join(offenders))


def test_the_reservation_FORWARDS_the_attempt_to_the_plan():
    """The plumbing between the two, which the seam tests above cannot see.

    `_reserve_node_build` takes the attempt and `_plan_native_card` consumes it; sever the one call
    between them and every caller above still names it, the receipt still mixes two folds, and
    nothing else in this file notices.
    """
    src = (ROOT / "looplab/engine/card_reservation.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_reserve_node_build")
    forwarding = [
        call for call in ast.walk(fn)
        if isinstance(call, ast.Call)
        and getattr(call.func, "attr", None) == "_plan_native_card"
        and any(kw.arg == "scored_against_attempt" for kw in call.keywords)
    ]
    assert forwarding, (
        "`_reserve_node_build` accepts `scored_against_attempt` and does not hand it to "
        "`_plan_native_card` — the parameter is decorative and the two folds are back")


def test_the_ast_rule_can_actually_fail():
    """NON-VACUITY: the walk must flag a call that names the id alone, or it is checking nothing."""
    tree = ast.parse("self._reserve_node_build(a, b, scored_against=s.best_node_id)")
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    kwargs = {kw.arg for kw in call.keywords}
    assert "scored_against" in kwargs and "scored_against_attempt" not in kwargs


def test_the_anchor_APPLIES_THE_SAME_REFUSAL_the_snapshot_does():
    """One rule, one spelling. `scored_anchor` re-derived the first half of `_card_score_snapshot`
    and dropped its `tombstoned` / `aborted_nodes` refusal, so a champion the snapshot refuses came
    back from here as a live anchor.

    MUTATION: re-derive it as `(state.best_node_id, node.attempt)` -> this is red on both cases.
    """
    tombstoned = _fold(_PROPOSAL)
    tombstoned.nodes[1].tombstoned = True     # set directly: the property is the REFUSAL, not the
    assert CardReservationMixin._card_score_snapshot(tombstoned, None) is None, (   # event shape
        "the fixture must actually reach the snapshot's refusal")
    assert scored_anchor(tombstoned) == (None, None), (
        "a tombstoned champion is not something a proposal can be scored against")

    aborted = _fold(_PROPOSAL)
    aborted.aborted_nodes.append(1)
    assert CardReservationMixin._card_score_snapshot(aborted, None) is None
    assert scored_anchor(aborted) == (None, None)


def test_the_anchor_never_returns_the_OVERLOADED_attempt_sentinel():
    """`attempt=None` is not "this anchor has no attempt" — one function down it means "the caller
    has no opinion", and `_card_score_snapshot` then takes the attempt from ITS OWN fresh fold. So
    an id from the caller's fold arrived beside an attempt from a later one: the exact two-fold
    mismatch this pair exists to close, reintroduced by the helper that closes it.

    MUTATION: `return node_id, (None if node is None else node.attempt)` -> the pair here is
    `(1, None)` and the mismatch is back.
    """
    missing = _fold(_PROPOSAL)
    missing.nodes.pop(1)                                   # a best_node_id with no node behind it
    node_id, attempt = scored_anchor(missing)
    assert (node_id, attempt) == (None, None), (
        "an anchor with no node is no anchor; it must never be an id beside a `no opinion` attempt")

    live_id, live_attempt = scored_anchor(_fold(_PROPOSAL))
    assert live_attempt is not None, "and a real anchor still names its own attempt"
    assert (live_id, live_attempt) == (1, 0)
