"""Every `Engine` member has ONE defining class, so a moved member cannot be shadowed by a leftover.

Review 2026-09-22, ENG1-04. `orchestrator.py` is being split into mixins one cluster at a time, and
no call site changes because in a mixin `self` IS the Engine. That same property is the risk of every
such move: a stale copy of a moved method left in the `Engine` body — a merge that resurrects the
deleted hunk, half a revert — SHADOWS the mixin's version by MRO. Every later edit to the real home
is then dead code, every test that drives the method still passes against the copy, and nothing goes
red. Two MIXINS defining one name is the same failure one step removed: base order picks a winner.

On this tree no member is defined by two classes of the family. `INTENDED_OVERRIDES` is where a
deliberate specialization must be written down with its reason (`orchestrator.py` keeps
`SharedEngineMixin` last in the bases precisely so a concern mixin CAN specialize a shared member);
anything else defined twice is red.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.engine.orchestrator import Engine
from looplab.engine.reentry import ReentryMixin
from looplab.engine.width_settling import WidthSettlingMixin

# name -> why a class earlier in `Engine.__mro__` deliberately re-defines a later class's member.
INTENDED_OVERRIDES: dict[str, str] = {}

# The live width settle, moved out of `orchestrator.py` in ENG1-04 step 1.
WIDTH_SETTLING_MEMBERS = ("_proposal_footprints", "_settle_proposal_width",
                          "_apply_control_overrides", "_reconfigure_llm_broker")

# The run-start pins and the re-entry checks that read them back, moved in ENG1-04 step 2.
REENTRY_MEMBERS = ("_run_start_pinned_values", "_run_start_settled_widths", "_repin_declared_env",
                   "_repin_settled_widths", "_recorded_settled_width",
                   "_require_pinned_speculation_receipt", "_reentry_repin")


def _homes(cls) -> dict[str, list[str]]:
    """member name -> every class of `cls.__mro__` inside `looplab.engine` whose OWN dict defines it."""
    out: dict[str, list[str]] = {}
    for klass in cls.__mro__:
        if not klass.__module__.startswith("looplab.engine"):
            continue
        for name in vars(klass):
            if name.startswith("__") and name.endswith("__"):
                continue
            out.setdefault(name, []).append(klass.__name__)
    return out


def test_no_engine_member_is_defined_by_two_classes_of_the_family():
    twice = {name: owners for name, owners in _homes(Engine).items() if len(owners) > 1}
    assert set(twice) == set(INTENDED_OVERRIDES), (
        f"defined by more than one class of the Engine family: {twice} — the first class listed "
        "SHADOWS the others. Delete the stale copy (a moved method's old body is the usual one), or "
        "register a deliberate specialization in INTENDED_OVERRIDES with its reason")


@pytest.mark.parametrize("name", WIDTH_SETTLING_MEMBERS)
def test_the_live_width_settle_resolves_to_its_mixin(name):
    assert WidthSettlingMixin in Engine.__mro__
    assert name not in vars(Engine), f"a copy of {name} in the Engine body shadows the mixin's"
    assert getattr(Engine, name) is vars(WidthSettlingMixin)[name]


@pytest.mark.parametrize("name", REENTRY_MEMBERS)
def test_the_re_entry_pins_resolve_to_their_mixin(name):
    assert ReentryMixin in Engine.__mro__
    assert name not in vars(Engine), f"a copy of {name} in the Engine body shadows the mixin's"
    # `getattr_static`, not `getattr`: `_recorded_settled_width` is a staticmethod, which a class-level
    # `getattr` unwraps to its bare function — never the object the mixin's own dict holds.
    assert inspect.getattr_static(Engine, name) is vars(ReentryMixin)[name]


def test_a_refusal_the_mixin_raises_is_caught_under_the_spelling_the_cli_imports():
    """The three re-entry refusals moved into `reentry.py` WITH the checks that raise them, and
    `orchestrator.py` imports them back — the spelling `cli/run_cmds.py::_drive_engine_to_terminal`
    catches by (`except RunStartPinError: raise`) and the tests import. A second class object under
    that spelling (a stale copy of the class left behind, or re-declared there) makes that `except`
    miss every refusal: the refused re-entry falls into the generic fatal-error branch, which writes
    `run_finished` and finalization receipts into the very log the engine refused to trust.

    DRIVEN through the real width check rather than asserted by name only: the refusal the MIXIN
    raises must be caught by the ORCHESTRATOR's class, and be exactly its subclass. Then pinned by
    identity for all three names, which is the whole property."""
    from looplab.core.models import RunState
    from looplab.engine import orchestrator, reentry

    eng = Engine.__new__(Engine)          # the width check reads these four attributes and no other
    eng._eval_parallel, eng._eval_parallel_startup_auto = 2, False      # an explicitly spelled 2...
    eng._llm_parallel, eng._llm_parallel_startup_auto = 1, False
    with pytest.raises(orchestrator.RunStartPinError) as caught:
        eng._repin_settled_widths(RunState(eval_parallel=1))           # ...against a log pinned at 1
    assert type(caught.value) is orchestrator.SettledWidthPinError
    for name in ("RunStartPinError", "SpeculationAuthorizationError", "SettledWidthPinError"):
        assert getattr(orchestrator, name) is getattr(reentry, name), (
            f"orchestrator.{name} is not the class `reentry.py` raises — a stale copy shadows it")


def test_the_census_sees_a_copy_left_behind():
    """Tier 1 for the guard itself: build the exact shape it refuses and watch it resolve wrongly."""
    class Mixin:
        def moved(self):
            return "the real home"

    class Host(Mixin):
        def moved(self):                  # the stale copy a botched merge leaves behind
            return "the leftover"

    Mixin.__module__ = Host.__module__ = "looplab.engine.synthetic"
    assert _homes(Host)["moved"] == ["Host", "Mixin"]
    assert Host().moved() == "the leftover", "the shadow this file exists to refuse"
