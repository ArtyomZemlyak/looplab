"""An abandoned prepared proposal is counted, with the reason from the path that knows.

MEASURED on `runs/e5small-dr-unified-v12`: node 2's card took FIVE propose phases. Four were
speculative, completed `ok: true`, and staged nothing — 604.765 + 317.696 + 139.759 + 524.508 s =
26.5 minutes of the card's 44.6-minute bill — before the fifth minted `card-2`. The run carries
ZERO `novelty_rejected` / `card_auto_dropped` / `proposal_discarded` rows, and `grep -c refused` on
its console log returns 0. Nothing anywhere said a proposal had been abandoned, or why.

THE REASON RIDES ON `_serve_raw_card_stage`'s OWN RETURN, not on `_card_stage_refusal`. Only
`_stage_prepared_card` writes that attribute, so the first cut — the caller re-reading it — held
whatever slug the LAST staging call anywhere recorded on the two paths that never reach the stager
(a producer fault, a proposal that formed no idea): a producer crash was warned as e.g.
`best_moved`, a specific-looking wrong cause an operator then greps the fences for. And the attach
HANDOFF (work handed to the serial spine and built) was warned as `abandoned: unrecorded`.

THE RECEIPT DROP IS DELIBERATE AND IS NOT WHAT THIS CHANGES. The serve republishes the audit
prefix on an ATTACH refusal only, because "on a stale-fence refusal the whole proposal is being
abandoned and re-made, so dropping them keeps the log honest" — republishing novelty/governance
rows for work about to be repeated would double-count it. A counted line carries no novelty rows.

`_stage_card_creates` has counted its refusals since 6262f3a1. That counter is on the CREATE lane;
the speculative lane reaches `_stage_prepared_card` by another route and had none.
"""
from __future__ import annotations

import ast
import logging

from looplab.engine import speculation
from looplab.engine.card_reservation import CARD_STAGE_REFUSALS
from looplab.engine.speculation import RAW_STAGE_PRE_STAGING_REASONS, SpecRawStageResult
from tests._source_scan import function_tree


class _Session:
    progressed = False


def _engine(consumed, staged, reason):
    eng = speculation.SpeculationMixin.__new__(speculation.SpeculationMixin)
    eng._serve_raw_card_stage = lambda: (consumed, staged, reason)
    # Everything after the abandonment branch is producer election, which this test is not about.
    eng._card_phase_elect_producer = lambda *a, **k: None
    return eng


def _serve(eng, session):
    try:
        speculation.SpeculationMixin._card_phase_serve_raw_stage(eng, session)
    except Exception:          # noqa: BLE001 - the tail is out of scope; the branch has already run
        pass


def _result(*, success, idea=None, audit_events=()):
    return SpecRawStageResult(
        generation=0, action={"kind": "draft"}, proposal_state=None,
        proposal_authority_seq=-1, proposal_node_ceiling=0, at_node=0,
        source="researcher", cue_fence=b"", success=success, idea=idea,
        audit_events=audit_events)


def _serving_engine(result, *, stale_slug, stager=None, attached=None):
    """A mixin instance around the REAL `_serve_raw_card_stage`, with the stager stubbed and the
    refusal attribute PRE-POISONED — the stale value the first cut misreported."""
    eng = speculation.SpeculationMixin.__new__(speculation.SpeculationMixin)
    eng._spec_raw_stage_result = result
    eng._card_stage_refusal = stale_slug
    eng._card_stage_attached_to = attached
    eng._published = []
    eng._publish_proposal_events = eng._published.append

    def _stage(*_a, **_k):
        if stager is None:
            return None
        return stager(eng)
    eng._stage_prepared_card = _stage
    return eng


def test_a_producer_fault_is_named_producer_failed_not_the_stale_fence_slug():
    """THE MISATTRIBUTION THIS EXISTS FOR. The create lane refused with `best_moved` turns ago;
    then the speculative producer crashes. The old read reported `best_moved` — a fence that never
    fired — and the operator debugged a nonexistent anchor race."""
    eng = _serving_engine(_result(success=False), stale_slug="best_moved")
    assert speculation.SpeculationMixin._serve_raw_card_stage(eng) == (
        True, False, "producer_failed")


def test_a_proposal_that_formed_no_idea_is_named_proposal_refused():
    eng = _serving_engine(_result(success=True, idea=None), stale_slug="cues_moved")
    assert speculation.SpeculationMixin._serve_raw_card_stage(eng) == (
        True, False, "proposal_refused")


def test_a_stager_refusal_carries_the_fence_slug_of_THIS_call():
    def _refuse(eng):
        eng._card_stage_refusal = "score_moved"
        return None
    eng = _serving_engine(_result(success=True, idea=object()), stale_slug=None, stager=_refuse)
    assert speculation.SpeculationMixin._serve_raw_card_stage(eng) == (
        True, False, "score_moved")


def test_a_stager_none_with_no_slug_is_unrecorded():
    """`unrecorded` is a legal answer for the same reason `unattributed` is on the run exit: a
    count with no reason still beats silence, and it is the signal that a refusal path forgot to
    name itself."""
    def _refuse(eng):
        eng._card_stage_refusal = None
        return None
    eng = _serving_engine(_result(success=True, idea=object()), stale_slug=None, stager=_refuse)
    assert speculation.SpeculationMixin._serve_raw_card_stage(eng) == (True, False, "unrecorded")


def test_an_attach_is_a_handoff_with_no_abandon_reason_and_its_audit_prefix_published():
    def _attach(eng):
        eng._card_stage_refusal = None
        eng._card_stage_attached_to = "card-0"
        return None
    rows = (("novelty_rejected", {"reason": "kept"}, None, None),)
    eng = _serving_engine(_result(success=True, idea=object(), audit_events=rows),
                          stale_slug=None, stager=_attach)
    assert speculation.SpeculationMixin._serve_raw_card_stage(eng) == (True, False, None)
    assert eng._published == [rows], "the handoff commits its audit prefix (the paid call's record)"


def test_an_abandoned_proposal_is_counted_with_its_reason(caplog):
    """Mutation: delete the branch and 26.5 minutes of paid work goes back to leaving no trace."""
    eng = _engine(True, False, "best_anchor_moved")
    with caplog.at_level(logging.WARNING):
        _serve(eng, _Session())
    assert eng._spec_raw_stage_abandoned["best_anchor_moved"] == 1
    # `getMessage()`, not `.message`: the record holds the FORMAT string and its args separately,
    # and `.message % r.args` raises once the args are already applied. My first spelling did.
    said = [r.getMessage() for r in caplog.records]
    assert any("abandoned a prepared proposal: best_anchor_moved" in m for m in said), said


def test_a_STAGED_proposal_is_not_counted():
    """NON-VACUITY: the branch must not fire on the success path, or the count is just a call
    counter and says nothing about abandonment."""
    eng = _engine(True, True, None)
    _serve(eng, _Session())
    assert getattr(eng, "_spec_raw_stage_abandoned", None) is None


def test_nothing_consumed_is_not_an_abandonment():
    """The early return. A turn with no prepared result has abandoned nothing."""
    eng = _engine(False, False, None)
    _serve(eng, _Session())
    assert getattr(eng, "_spec_raw_stage_abandoned", None) is None


def test_a_handoff_is_not_an_abandonment(caplog):
    """The attach case: consumed, not staged, NO reason — handed to the serial spine and built.
    Warning it as abandoned announced a loss that never happened, on every repair/debug proposal
    whose question a live Card already owned."""
    eng = _engine(True, False, None)
    with caplog.at_level(logging.WARNING):
        _serve(eng, _Session())
    assert getattr(eng, "_spec_raw_stage_abandoned", None) is None
    assert not [r for r in caplog.records if "abandoned" in r.getMessage()]


def test_the_pre_staging_reasons_are_registered_and_disjoint_from_the_fence_slugs():
    """`CARD_STAGE_REFUSALS` may only carry slugs `_stage_prepared_card` itself emits
    (`tests/test_card_stage_refusals.py` pins that in both directions), so the two pre-staging
    words live in their own tuple — and the two vocabularies must stay disjoint or the summed
    counters stop being attributable."""
    assert set(RAW_STAGE_PRE_STAGING_REASONS).isdisjoint(CARD_STAGE_REFUSALS)
    assert len(CARD_STAGE_REFUSALS) >= 2 and "" not in CARD_STAGE_REFUSALS
    assert all(isinstance(s, str) and s for s in RAW_STAGE_PRE_STAGING_REASONS)


def _getattr_constants(func) -> set[str]:
    """Names read via `getattr(self, "<name>", ...)` — AST, so a comment cannot satisfy it."""
    return {
        call.args[1].value
        for call in ast.walk(function_tree(func))
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        and call.func.id == "getattr" and len(call.args) >= 2
        and isinstance(call.args[1], ast.Constant) and isinstance(call.args[1].value, str)
    }


def test_the_slug_is_read_where_the_stager_ran_and_nowhere_upstream():
    """The serve — the one frame that just called `_stage_prepared_card` — reads the fence
    attribute; the CALLER must not, because between its read and the stager's write sit the two
    pre-staging paths that made the old read stale. AST on both sides: the first spelling of this
    guard was `assert "_card_stage_refusal" in src`, which the caller's own comment satisfies with
    the read deleted (CLAUDE.md: a guard test must not be satisfiable by a COMMENT)."""
    serve = speculation.SpeculationMixin._serve_raw_card_stage
    caller = speculation.SpeculationMixin._card_phase_serve_raw_stage
    assert "_card_stage_refusal" in _getattr_constants(serve)
    assert "_card_stage_refusal" not in _getattr_constants(caller), (
        "the caller re-reading the fence attribute is the stale-slug defect coming back")


def test_the_duration_is_NOT_repeated_here():
    """One number in two places is how they drift. The seconds are already on this phase's
    `phase_progress` row; the warning says where to look instead of restating them."""
    fn = function_tree(speculation.SpeculationMixin._card_phase_serve_raw_stage)
    calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
             and getattr(c.func, "attr", None) in ("monotonic", "time", "perf_counter")]
    assert not calls, "this seam must not time anything — the span already did"
