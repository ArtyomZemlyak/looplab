"""A DISCARDED PROPOSAL IS RECEIPTED — the rule `invalid` already followed and `duplicate` did not.

THE MEASUREMENT (`runs/e5small-dr-unified-v8`, 2026-08-27)
---------------------------------------------------------
Eleven `propose` operations ran on that board. Ten produced a `card_added` or a `card_enriched`.
One did not:

    11:39:08  +24.1 min   81 provider calls   4,270,000 tokens   ->  NOTHING

Its last generation span (phase span `8a0373c14cabd6ec`, generation 81 of 81, 12:00:50) carries a
well-formed `emit`, 2,781 chars of arguments, a 13-key params map, and the hypothesis

    "Raising training max_seq_length from 128 to 256 (matching the eval's document truncation)
     improves test recall@100 on e5-small-en-ru by removing the train/eval length mismatch on
     long product documents."

with a rationale citing `config.yaml:276`, `build_trainer.py:144`, `config.yaml:229` and
`retriever.py:83`. A one-knob delta on the champion applied config. Between that propose closing at
12:03:12 and the next one starting at 13:11:47 the durable log records no `card_added`, no
`card_enriched`, no `hypothesis_added` — and the whole run contains zero `card_dropped` and zero
`card_auto_dropped`. The next propose spent 148 calls and returned a DIFFERENT hypothesis, so the
idea was not recovered; it was lost.

THE ASYMMETRY THAT CAUSED IT. `_plan_native_card`'s disposition vocabulary is exactly
{attach, duplicate, invalid, mint, reuse}. Three are accepted. `invalid` appends
`novelty_rejected{kind: card_contract}`. `duplicate` — the one a BUSY BOARD produces — returned None
with nothing written, two lines below it. So did the third branch, which fires when an ACCEPTED
disposition comes back with `card_id is None` or `idea is None`. All three unwind through
`audit._discard_node_build_telemetry`, which despite its name appends no event: its whole body nulls
`last_hyp_priority` / `last_foresight` / `last_foresight_pick` / `last_report` so a later build
cannot emit an abandoned build's predictions.

WHAT THIS CHANGE DOES AND DOES NOT DO. Refusing the mint is unchanged and right — a card whose owner
is in flight must not be minted twice, and this file asserts that the refusal still happens. What
changes is that the refusal is now countable and the idea recoverable.
"""
from __future__ import annotations

import inspect

from looplab.engine import card_reservation
from looplab.engine.card_reservation import (_DISCARDED_PROPOSAL_TEXT_MAX,
                                             _discarded_proposal_text)


# ------------------------------------------------------------------ the bounded receipt text
def test_the_receipt_carries_the_hypothesis_and_bounds_it():
    """The field a reader needs is "what was the idea", so it is the hypothesis. The bound matches
    the one `replay._on_hypothesis_added` applies to a `rationale`, so an audit row can never grow
    past what the fold keeps beside it."""
    idea = type("I", (), {"hypothesis": "x" * (_DISCARDED_PROPOSAL_TEXT_MAX + 500)})()
    out = _discarded_proposal_text(idea)
    assert len(out) == _DISCARDED_PROPOSAL_TEXT_MAX == 400
    assert _discarded_proposal_text(type("I", (), {"hypothesis": "  keep me  "})()) == "keep me"


def test_the_receipt_text_never_raises_into_the_reservation_path():
    """A receipt may not cost a build its refusal, so every unreadable shape degrades to "" and the
    row still carries the disposition — which is the part that makes the discard countable."""

    class _Explodes:
        @property
        def hypothesis(self):
            raise RuntimeError("boom")

    assert _discarded_proposal_text(_Explodes()) == ""
    assert _discarded_proposal_text(None) == ""
    assert _discarded_proposal_text(type("I", (), {"hypothesis": 17})()) == ""


# ------------------------------------------------------------------ where the receipt lives
def _commit_pass_source() -> str:
    return inspect.getsource(card_reservation.CardReservationMixin._reserve_node_build)


def test_the_commit_pass_stays_SILENT_because_it_is_also_the_batch_entry_point():
    """The receipt is deliberately NOT here, and this test is why.

    `_reserve_node_build` is also the batch pre-reservation entry point: it is called with a
    ready-made Idea and no paid propose behind it, so calling it twice with the same idea is the
    idempotent retry of ONE action.
    `test_card_writer_lifecycle::test_batch_prereservations_mint_on_main_thread_and_dedupe_exact_active_work`
    pins that such a re-reservation appends nothing at all, and a discard row here would count a
    phantom loss on every exact twin. A first version of this change did append here; that test
    caught it.
    """
    src = _commit_pass_source()
    head, _, tail = src.partition('if plan.disposition == "duplicate":')
    assert tail, "the duplicate branch moved — re-point this test at it"
    branch = tail.split("return None")[0]
    assert "_append_proposal_event" not in branch
    assert "return None" in tail          # …and the refusal itself is unchanged


def test_the_planner_pass_receipts_the_discard_and_names_itself():
    """`_prepare_node_idea._link` runs immediately after the proposal call, so it is a pass that can
    know a PAID proposal was refused. Before this it returned None and the caller unwound through
    `_discard_node_build_telemetry`, which appends nothing.

    RE-POINTED 2026-09-02, and the reason matters more than the edit. This asserted the literal
    fields of an inline dict, and that dict was one of THREE hand-written copies: the batch draft
    lane in `novelty.py::_link_card` and the Layer-5 speculative producer each run a paid propose
    and refuse one too, and both lost it in silence — while this branch's own comment claimed to be
    "THE ONLY PLACE ... and nowhere else". The payload is now
    `card_reservation.py::discarded_proposal_receipt`, so what this file can still pin is that THIS
    branch calls it and names ITSELF; the row's fields are driven directly in
    `tests/test_discarded_proposal_receipt.py`, which also holds the other two lanes.
    """
    from looplab.engine import orchestrator

    src = inspect.getsource(orchestrator.Engine._prepare_node_idea)
    _, _, tail = src.partition('elif plan.disposition not in {"mint", "reuse", "attach"}:')
    assert tail, "the planner receipt moved — re-point this test at it"
    branch = tail.split("return plan.idea")[0]
    assert "_append_proposal_event(EV_NOVELTY_REJECTED" in branch
    assert "discarded_proposal_receipt(" in branch
    assert 'lane="planner"' in branch, "the row must name the pass that wrote it"
    assert "plan.disposition" in branch, "the disposition decides duplicate vs unplannable"
    assert "linked" in branch, "the hypothesis comes off the idea this pass just paid for"

    # ...and the constructor really produces what this branch stopped spelling.
    from looplab.engine.card_reservation import discarded_proposal_receipt

    row = discarded_proposal_receipt("duplicate", 3, None, lane="planner")
    assert row["pass"] == "planner" and row["kind"] == "card_duplicate"
    assert row["disposition"] == "duplicate" and row["action"] == "dropped"
    assert discarded_proposal_receipt("stale", 3, None, lane="planner")["kind"] == "card_unplannable"


def test_an_accepted_disposition_is_never_receipted_as_a_discard():
    """A false receipt is worse than none: it would make a healthy mint countable as a loss. The
    planner branch is an `elif` under the `invalid` check and is guarded by the SAME accepted set the
    return uses, so mint/reuse/attach cannot reach it."""
    from looplab.engine import orchestrator

    src = inspect.getsource(orchestrator.Engine._prepare_node_idea)
    assert 'elif plan.disposition not in {"mint", "reuse", "attach"}:' in src
    # the guard and the return must name the SAME accepted set, or one of them is wrong
    assert src.count('{"mint", "reuse", "attach"}') == 2


def test_the_invalid_receipt_is_unchanged_and_still_the_only_card_contract_row():
    """`invalid` already had a receipt and its wording is a contract other readers key on; this
    change adds kinds beside it and must not reword or duplicate it."""
    commit = _commit_pass_source()
    assert commit.count('"kind": "card_contract"') == 1
    assert "proposal cannot form a bounded native Card action" in commit
