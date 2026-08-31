"""An abandoned prepared proposal is counted, with the slug that refused it.

MEASURED on `runs/e5small-dr-unified-v12`: node 2's card took FIVE propose phases. Four were
speculative, completed `ok: true`, and staged nothing — 604.765 + 317.696 + 139.759 + 524.508 s =
26.5 minutes of the card's 44.6-minute bill — before the fifth minted `card-2`. The run carries
ZERO `novelty_rejected` / `card_auto_dropped` / `proposal_discarded` rows, and `grep -c refused` on
its console log returns 0. Nothing anywhere said a proposal had been abandoned, or why.

THE RECEIPT DROP IS DELIBERATE AND IS NOT WHAT THIS CHANGES. `_consume_prepared_raw_stage`
republishes the audit prefix on an ATTACH refusal only, because "on a stale-fence refusal the whole
proposal is being abandoned and re-made, so dropping them keeps the log honest" — republishing
novelty/governance rows for work about to be repeated would double-count it. A counted line carries
no novelty rows.

`_stage_card_creates` has counted its refusals since 6262f3a1. That counter is on the CREATE lane;
the speculative lane reaches `_stage_prepared_card` by another route and had none.
"""
from __future__ import annotations

import ast
import inspect
import logging
import pathlib

from looplab.engine import speculation
from looplab.engine.card_reservation import CARD_STAGE_REFUSALS

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Session:
    progressed = False


def _engine(consumed, staged, refusal):
    eng = speculation.SpeculationMixin.__new__(speculation.SpeculationMixin)
    eng._serve_raw_card_stage = lambda: (consumed, staged)
    eng._card_stage_refusal = refusal
    # Everything after the abandonment branch is producer election, which this test is not about.
    eng._card_phase_elect_producer = lambda *a, **k: None
    return eng


def _serve(eng, session):
    try:
        speculation.SpeculationMixin._card_phase_serve_raw_stage(eng, session)
    except Exception:          # noqa: BLE001 - the tail is out of scope; the branch has already run
        pass


def test_an_abandoned_proposal_is_counted_with_its_slug(caplog):
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
    eng = _engine(True, True, "best_anchor_moved")
    _serve(eng, _Session())
    assert getattr(eng, "_spec_raw_stage_abandoned", None) is None


def test_nothing_consumed_is_not_an_abandonment():
    """The early return. A turn with no prepared result has abandoned nothing."""
    eng = _engine(False, False, "best_anchor_moved")
    _serve(eng, _Session())
    assert getattr(eng, "_spec_raw_stage_abandoned", None) is None


def test_a_missing_slug_is_named_rather_than_dropped():
    """`unrecorded` is a legal answer for the same reason `unattributed` is on the run exit: a count
    with no reason still beats silence, and it is the signal that a refusal path forgot to name
    itself."""
    eng = _engine(True, False, "")
    _serve(eng, _Session())
    assert eng._spec_raw_stage_abandoned["unrecorded"] == 1


def test_the_slug_comes_from_the_SHARED_vocabulary():
    """Both lanes must name refusals from one tuple, or the two counters cannot be added up."""
    src = inspect.getsource(speculation.SpeculationMixin._card_phase_serve_raw_stage)
    assert "_card_stage_refusal" in src, (
        "the slug must come from the same attribute `_refuse` sets on the create lane, not from a "
        "second vocabulary this file invents")
    assert len(CARD_STAGE_REFUSALS) >= 2 and "" not in CARD_STAGE_REFUSALS


def test_the_duration_is_NOT_repeated_here():
    """One number in two places is how they drift. The seconds are already on this phase's
    `phase_progress` row; the warning says where to look instead of restating them."""
    fn = next(n for n in ast.walk(ast.parse(
        (ROOT / "looplab/engine/speculation.py").read_text()))
        if isinstance(n, ast.FunctionDef) and n.name == "_card_phase_serve_raw_stage")
    calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
             and getattr(c.func, "attr", None) in ("monotonic", "time", "perf_counter")]
    assert not calls, "this seam must not time anything — the span already did"
