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

import pytest

from looplab.engine.orchestrator import Engine
from looplab.engine.width_settling import WidthSettlingMixin

# name -> why a class earlier in `Engine.__mro__` deliberately re-defines a later class's member.
INTENDED_OVERRIDES: dict[str, str] = {}

# The live width settle, moved out of `orchestrator.py` in ENG1-04 step 1.
WIDTH_SETTLING_MEMBERS = ("_proposal_footprints", "_settle_proposal_width",
                          "_apply_control_overrides", "_reconfigure_llm_broker")


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
