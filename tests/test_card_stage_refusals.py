"""Every staging fence names WHICH half of it moved, and the registry cannot rot.

`_stage_prepared_card._plan` compares a FRESH fold against the snapshot the proposal was authored
against. Any of eight conjuncts refuses, and the refusal is the DESIGNED answer to moved authority —
a proposal authored against an old search state must never be relabelled as current work. None of
that changes here. What changes is that until 2026-08-31 every refusal returned a bare `None`, so
the loss was unattributable.

THE BATCH LANE IS WHY IT MATTERS. Since `56764cbd` moved the paid batch propose off the event-loop
thread there is a minutes-long SUSPENSION between the authority fold and the staging loop, so one
best-IMPROVING eval terminal or one `research_completed`/`hint`/strategy row — all
BACKGROUND_APPENDABLE, all hashed by `_proposal_cue_fence` — refuses EVERY idea of the batch at
once. Pre-offload the loop was frozen and no fence input could move mid-propose, so this was
unreachable.

Guarded the way `CARD_BUILD_SKIP_REASONS` is one module over, and for the same reason: a typo'd slug
does not fail at runtime. It lands on an in-process seam a caller reads and on a log line an
operator greps, i.e. it reads as a refusal nobody can look up.
"""
from __future__ import annotations

import ast

from looplab.engine.card_reservation import CARD_STAGE_REFUSALS, CardReservationMixin
from tests._source_scan import function_tree


def _plan_tree() -> ast.AST:
    """The `_plan` closure INSIDE `_stage_prepared_card`, and nothing else.

    Scoped rather than module-wide on purpose: `card_reservation.py` defines THREE functions called
    `_plan`, and a file-wide search would report about a fence nobody asked it to check — the same
    trap `tests/test_offload_lane_writes_no_folded_events.py` records for `run_sync`.
    """
    for node in ast.walk(function_tree(CardReservationMixin._stage_prepared_card)):
        if isinstance(node, ast.FunctionDef) and node.name == "_plan":
            return node
    raise AssertionError("`_plan` is gone from `_stage_prepared_card` — re-point this guard")


def _emitted_slugs() -> set[str]:
    """Every literal handed to `_refuse(...)` inside that `_plan`, by AST."""
    out = set()
    for node in ast.walk(_plan_tree()):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_refuse"):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    out.add(arg.value)
    return out


def test_no_BARE_none_survives_in_the_staging_FENCE():
    """The defect itself, as an assertion. Mutation: revert any conjunct to `return None` and its
    refusal goes back to being one silent drop among eight.

    SCOPED TO THE FENCE, and the two exclusions are named rather than positional. `_plan` also
    refuses on its DISPOSITION — `attach` and non-`mint` — and those are a different class that
    already names itself through its own seams (`_card_stage_attached_to` and
    `_card_claim_refusal`, both read by `speculation.py`). The fence conjuncts are the ones whose
    `if` tests the FRESH fold against the authored snapshot, so the rule here is "a refusal under a
    branch that does not read `plan` must be named" — non-positional, so a conjunct added anywhere
    is still covered.
    """
    plan_tree = _plan_tree()
    def _reads_plan(test) -> bool:
        return any(isinstance(n, ast.Name) and n.id == "plan" for n in ast.walk(test))

    dispositional = set()
    for node in ast.walk(plan_tree):
        if isinstance(node, ast.If) and _reads_plan(node.test):
            dispositional.update(id(n) for n in ast.walk(node)
                                 if isinstance(n, ast.Return))
    bare = [n for n in ast.walk(plan_tree)
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
            and n.value.value is None and id(n) not in dispositional]
    assert not bare, (
        f"{len(bare)} fence conjunct(s) still refuse with a bare `None` — name the refusal and "
        "register it, or a moved fence discards N paid ideas with nothing on the record")


def test_every_slug_the_fence_emits_is_REGISTERED():
    unregistered = _emitted_slugs() - set(CARD_STAGE_REFUSALS)
    assert not unregistered, f"unregistered staging refusals: {sorted(unregistered)}"


def test_every_REGISTERED_slug_is_actually_EMITTED():
    """The other direction, which is what stops the registry rotting into a list of words nothing
    produces. Mutation: add a member nothing returns."""
    unemitted = set(CARD_STAGE_REFUSALS) - _emitted_slugs()
    assert not unemitted, f"registered but emitted nowhere: {sorted(unemitted)}"


def test_the_registry_has_no_duplicates_and_no_blanks():
    assert len(set(CARD_STAGE_REFUSALS)) == len(CARD_STAGE_REFUSALS)
    assert all(isinstance(s, str) and s.strip() for s in CARD_STAGE_REFUSALS)


def test_the_EIGHT_conjuncts_are_still_eight_distinct_facts():
    """The fence was ONE compound `if` and is now eight; splitting it must not have dropped a
    comparison. Mutation: delete any conjunct — its slug stops being emitted and this goes red
    through the registry test above, and the count here says which."""
    assert len(_emitted_slugs()) == len(CARD_STAGE_REFUSALS) == 8, (
        f"the fence emits {sorted(_emitted_slugs())}; a conjunct was added or lost without the "
        "registry moving with it")


def test_the_seam_is_CLEARED_at_entry_so_a_reader_reads_THIS_call():
    """`_card_stage_refusal` is read by the caller right after the call, exactly like
    `_card_stage_attached_to`. A stale value from a previous idea would attribute one idea's
    refusal to the next. Mutation: delete the reset line."""
    fn = next(n for n in ast.walk(function_tree(CardReservationMixin._stage_prepared_card))
              if isinstance(n, ast.FunctionDef) and n.name == "_stage_prepared_card")
    resets = [n for n in fn.body
              if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Attribute) and t.attr == "_card_stage_refusal"
                      for t in n.targets)
              and isinstance(n.value, ast.Constant) and n.value.value is None]
    assert resets, "`_card_stage_refusal` must be cleared at entry, beside its sibling seam"


def test_the_caller_COUNTS_the_refusals_and_says_so():
    """A named refusal nobody reads is the same silence one layer up. Mutation: drop the `else`
    branch or the warning, and a batch whose fence moved is unattributable again."""
    tree = function_tree(CardReservationMixin._stage_card_creates)
    reads = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "getattr"
             and any(isinstance(a, ast.Constant) and a.value == "_card_stage_refusal"
                     for a in n.args)]
    assert reads, "the staging loop must read which fence refused"
    warned = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "warning"]
    assert warned, "the loss must be said out loud, not merely counted into a local"
