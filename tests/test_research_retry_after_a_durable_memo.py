"""A paid think is bought ONCE. A projection appended after the memo may not undo that.

`_record_deep_research` makes the memo durable (`research_completed`) and THEN appends the legacy
`hint` row and one `hypothesis_added` per admitted question. Those three appends were unguarded, so
a store that refused any of them raised out of `_research_attempt_step` into
`_research_overlap_loop`'s `except Exception` — which, until 2026-08-30, set `trig = "repeat"` at
the BOTTOM of its `try`. The retry therefore wore the ORIGINAL cadence/strategist label with
`last_sig` still unset, and bought:

  * a second `research_attempted` receipt for a gate already spent,
  * a second full PAID provider pass,
  * a second `research_completed` for a memo already on disk.

MEASURED over every event log on the box: 68 `research_attempted` / 176 `research_completed` and
**zero** duplicate `(trigger, at_node)` gates, so this window has never fired here. It is latent,
not recorded — which is why the fix is the cheap one (spend the in-process label where the durable
receipt is spent; let a projection failure be a projection failure) and not a redesign.

Two properties, driven from opposite ends: the RECORD must survive a refused projection, and the
LOOP must not re-wear a spent trigger on any of its three non-completing exits.
"""
from __future__ import annotations

import types

import anyio
import pytest

from looplab.core.llm import BudgetExceeded
from looplab.engine.orchestrator import Engine
from looplab.engine.research_cadence import ResearchCadenceMixin, research_memo_sig
from looplab.events.eventstore import EventStore
from looplab.events.types import EV_HINT, EV_HYPOTHESIS_ADDED, EV_RESEARCH_COMPLETED


# --------------------------------------------------------------- the record: the memo is the artifact

class _RefusingStore:
    """An `EventStore` that refuses exactly one event type, and remembers what it accepted."""

    def __init__(self, path, refuse: str):
        self._real = EventStore(path)
        self._refuse = refuse
        self.refused = 0

    def append(self, type, data, **kw):
        if type == self._refuse:
            self.refused += 1
            raise OSError(f"refusing {type}")
        return self._real.append(type, data, **kw)

    def read_all(self):
        return self._real.read_all()


def _engine(store):
    eng = Engine.__new__(Engine)
    eng.store = store
    eng._research_verify = False
    eng._track_hypotheses = True
    return eng


def _memo(**kw):
    from looplab.core.models import ResearchMemo
    return ResearchMemo(at_node=2, trigger="cadence", summary="loss plateaus",
                        recommended_directions=["try warmup"], **kw)


def _types(store):
    return [e.type for e in store.read_all()]


@pytest.mark.parametrize("refused", [EV_HINT, EV_HYPOTHESIS_ADDED])
def test_a_refused_projection_does_not_raise_out_of_the_record(tmp_path, refused):
    """THE PROPERTY. Before the split, this call raised — and every caller reads a raise as 'the
    paid pass failed', which is what re-bought the think."""
    store = _RefusingStore(tmp_path / "events.jsonl", refused)
    _engine(store)._record_deep_research(_memo(), trigger="cadence", manual=False,
                                         attempt_id="a1")
    assert store.refused >= 1, "the mutation must actually reach the refused append"
    assert EV_RESEARCH_COMPLETED in _types(store), "the paid artifact is durable"


def test_the_memo_append_itself_is_NOT_swallowed(tmp_path):
    """The boundary is 'is the memo on disk', not 'is this an append'. With nothing durable the
    trigger gate is still spent by the receipt, so a silent success here would discard a paid think
    with no record of it at all."""
    store = _RefusingStore(tmp_path / "events.jsonl", EV_RESEARCH_COMPLETED)
    with pytest.raises(OSError):
        _engine(store)._record_deep_research(_memo(), trigger="cadence", manual=False)


def test_a_refused_projection_is_logged_and_not_silent(tmp_path, caplog):
    store = _RefusingStore(tmp_path / "events.jsonl", EV_HINT)
    with caplog.at_level("WARNING", logger="looplab.engine.research_cadence"):
        _engine(store)._record_deep_research(_memo(), trigger="cadence", manual=False)
    said = [r.getMessage() for r in caplog.records]
    assert any("steering" in m and "memo recorded" in m for m in said), caplog.text


def test_a_budget_hard_stop_in_a_projection_still_propagates(tmp_path):
    """`BudgetExceeded` is the global hard stop and is never a projection hiccup."""
    class _BudgetStore(_RefusingStore):
        def append(self, type, data, **kw):
            if type == EV_HINT:
                raise BudgetExceeded("run budget spent")
            return self._real.append(type, data, **kw)

    store = _BudgetStore(tmp_path / "events.jsonl", EV_HINT)
    with pytest.raises(BudgetExceeded):
        _engine(store)._record_deep_research(_memo(), trigger="cadence", manual=False)


# ------------------------------------------------------- the loop: a spent trigger is not re-worn

class _LoopStub(ResearchCadenceMixin):
    """The overlap loop's host, mirroring `tests/test_research_overlap.py::_LoopStub`. Inherits
    `_research_attempt_step` — the real receipt→compute→record sequencing — over fakes."""

    def __init__(self, *, cap=2, cadence=0.001, fail_records=0):
        self._concurrent_research_max_calls = cap
        self._cadence = cadence
        self._fail_records = fail_records
        self.attempts: list[str] = []
        self.records: list[str] = []
        self.computes = 0
        self.store = types.SimpleNamespace(read_all=lambda: [])

    def _research_repeat_cadence(self):
        return self._cadence

    def _record_research_attempt(self, state, *, trigger, manual):
        if trigger == "repeat":
            return None
        self.attempts.append(trigger)
        return f"a{len(self.attempts)}"

    def _compute_deep_research(self, state, trigger, *, trace=True):
        self.computes += 1
        return types.SimpleNamespace(summary=f"memo-{self.computes}",
                                     recommended_directions=["d"])

    def _record_deep_research(self, memo, *, trigger, manual, attempt_id=None,
                              superseded=None):
        # `superseded=` is `_research_attempt_step`'s own kwarg on this branch (the
        # `_results_since_snapshot` stamp), and the stub has to accept it for the same reason
        # `tests/test_research_overlap.py::_LoopStub` does: this fake stands in for the real
        # method, so a signature it cannot be CALLED with turns every assertion below into a
        # TypeError the loop swallows as "the paid pass failed" — which is the exact shape this
        # file exists to catch, arriving as a false green rather than a red.
        if self._fail_records > 0:
            self._fail_records -= 1
            raise OSError("store refused a projection after the memo landed")
        self.records.append(trigger)


def test_a_failed_pass_does_not_re_receipt_the_gate_it_already_spent():
    """The regression. The first pass receipts `cadence` and then raises; the second pass must be a
    `repeat` — unreceipted, because that gate is spent — and must still think."""
    stub = _LoopStub(cap=2, fail_records=1)
    anyio.run(Engine._research_overlap_loop, stub, "cadence")
    assert stub.attempts == ["cadence"], "exactly one durable receipt for one durable gate"
    assert stub.computes == 2, "the loop still thinks on the next tick"
    assert stub.records == ["repeat"]


def test_the_healthy_path_still_receipts_once_and_labels_the_first_pass():
    """The negative control: nothing about a normal window moves. One receipt, and the memo's own
    `trigger` column still says which gate paid for it."""
    stub = _LoopStub(cap=2)
    anyio.run(Engine._research_overlap_loop, stub, "cadence")
    assert stub.attempts == ["cadence"] and stub.records == ["cadence", "repeat"]


def test_a_strategist_trigger_is_spent_exactly_like_a_cadence_one():
    stub = _LoopStub(cap=3, fail_records=1)
    anyio.run(Engine._research_overlap_loop, stub, "strategist")
    assert stub.attempts == ["strategist"]
    assert stub.records == ["repeat", "repeat"]


def test_every_failing_pass_in_a_window_costs_at_most_one_receipt():
    """Three consecutive refusals: the old loop wrote three receipts and paid for three thinks
    against one gate."""
    stub = _LoopStub(cap=4, fail_records=3)
    anyio.run(Engine._research_overlap_loop, stub, "cadence")
    assert stub.attempts == ["cadence"]
    assert stub.computes == 4 and stub.records == ["repeat"]
