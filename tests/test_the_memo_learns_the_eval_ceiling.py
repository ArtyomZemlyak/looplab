"""The eval's own ceilings reach the DEEP-RESEARCH role, and until 2026-09-18 they never had.

MEASURED over every memo on this box — 244 `research_completed` rows across eleven run dirs,
matched on the memo's own prose (summary / findings / recommended_directions / open_questions /
claims), median 11,405 chars each:

    the eval wall clock      0 of 244
    stage reuse              0 of 244
    the faiss CPU fallback   2 of 244
    the artifact contract   99 of 244  (41%)
    an OOM ceiling          74 of 244  (30%)

The split is not "the model ignores the harness". The two facts it reaches routinely are the two
its own tools and the run brief already carry; the three it never reaches are the ones nothing
STAMPED on it. `engine/proposal_cues.py::_set_complexity_hint` stamps `_gpu_budget_hint` and
`_time_budget_hint` on the researcher it is handed — the PROPOSE path's — and
`_compute_deep_research` calls a DIFFERENT object, `self.deep_researcher`.

WHAT THE OMISSION COST: `runs/e5small-dr-unified-v11` node 2 was SIGKILLed by its own 10-hour wall
at step 1764 of 2109 — 84%, 9h56m — discarding 10.0 GPU-hours, and the run then paid a full
retrain. The schedule measured 11.3 h against a 10 h wall and was decidable hours earlier. A memo
that never states the wall clock cannot size a schedule to fit it, and v14 is aimed at hard-negative
mining, whose pass pays ~51 minutes to the faiss CPU fallback on this card.
"""
from __future__ import annotations

import types

from looplab.agents import deep_research as dr
from looplab.core.models import RunState
from looplab.engine import proposal_cues as pc
from looplab.engine import research_cadence as rc


def _state() -> RunState:
    return RunState(goal="g", direction="max")


# ---------------------------------------------------------------------------------------------
# The brief carries them, and carries them LAST.

def test_the_brief_ends_on_the_ceilings():
    """`RESEARCHER_PROMPT_CUES`' ordering rule, applied to the brief the memo reads: each budget
    states the ceiling for its axis and the reader must end on the number it has to act on."""
    brief = dr.state_brief(_state(), prompt_cues=("GPU-CEILING-CUE", "WALL-CLOCK-CUE"))
    assert "GPU-CEILING-CUE" in brief and "WALL-CLOCK-CUE" in brief
    lines = [line for line in brief.splitlines() if line.strip()]
    assert lines[-1] == "WALL-CLOCK-CUE", (
        "the wall clock is the axis a proposal gets wrong LATEST, so it goes last")
    assert lines[-2] == "GPU-CEILING-CUE"


def test_an_empty_cue_adds_no_line():
    """Both stampers set their attribute UNCONDITIONALLY, empty included, so the brief must treat
    an empty cue as absent rather than emitting a blank line into the prompt."""
    plain = dr.state_brief(_state())
    assert dr.state_brief(_state(), prompt_cues=("", "   ", None)) == plain


def test_the_NEGATIVE_CONTROL_is_the_pre_fix_brief():
    """What the 0-of-244 measurement looked like from inside: no cues, no ceiling, no line."""
    brief = dr.state_brief(_state())
    assert "WALL-CLOCK-CUE" not in brief


# ---------------------------------------------------------------------------------------------
# The engine stamps them on THIS role — the half that was missing.

class _Researcher:
    """A role that records what the engine stamped and renders its own brief the way the real one
    does — `getattr(self, …, "")` on the two cue attributes."""

    def __init__(self):
        self.seen_brief = ""

    def research(self, state, trigger=""):
        self.seen_brief = dr.state_brief(
            state, prompt_cues=(getattr(self, "_gpu_budget_hint", ""),
                                getattr(self, "_time_budget_hint", "")))
        from looplab.core.models import ResearchMemo
        return ResearchMemo(at_node=0, trigger=trigger, summary="ok")


def _engine(researcher):
    """The engine surface `_compute_deep_research` touches, with the REAL stampers bound so the
    test drives the production path rather than a double of it."""
    # The two stampers live on `ProposalCuesMixin` and the consumer on `ResearchCadenceMixin`;
    # on the real `Engine` both are bases, so `self._stamp_*` resolves. Binding them from their OWN
    # class is what makes this drive the production methods rather than doubles of them — and the
    # first version of this test asserted them on the wrong mixin and went red, which is the check
    # working: the fix reaches ACROSS the two clusters, so a test may not assume one file's surface.
    eng = types.SimpleNamespace(deep_researcher=researcher, tracer=None)
    for name in ("_stamp_gpu_budget_hint", "_stamp_time_budget_hint"):
        setattr(eng, name, types.MethodType(getattr(pc.ProposalCuesMixin, name), eng))
    eng._gpu_budget_hint_text = lambda: "GPU-CEILING-CUE"
    # `_stamp_gpu_budget_hint` CONCATENATES the observed-footprint note onto the ceiling text, so a
    # stub missing it raises inside that stamper's own `except` and the cue silently does not land —
    # which is exactly what the first run of this test caught, and exactly how a real engine
    # attribute lost in a refactor would disappear from a prompt with nothing going red.
    eng._observed_footprint_note = lambda: ""
    eng._gpu_footprint_cue = True
    eng._time_budget_hint_text = lambda: "WALL-CLOCK-CUE"
    return eng


def test_the_engine_stamps_the_ceilings_on_the_DEEP_RESEARCHER():
    """The defect, driven. MUTATION: delete either stamp line in `_compute_deep_research` and the
    corresponding cue vanishes from the brief the memo was written from."""
    role = _Researcher()
    eng = _engine(role)
    memo = rc.ResearchCadenceMixin._compute_deep_research(eng, _state(), "repeat", trace=False)
    assert memo.summary == "ok", "the stamp must not cost the stage its memo"
    assert role._gpu_budget_hint == "GPU-CEILING-CUE"
    assert role._time_budget_hint == "WALL-CLOCK-CUE"
    assert "WALL-CLOCK-CUE" in role.seen_brief, (
        "the wall clock must be in the text the memo is written from, not only on the object")


def test_a_role_that_REFUSES_attribute_writes_still_gets_its_memo():
    """The swallowing contract the stampers already carry: a Toy role that rejects `setattr` must
    not cost the run its memo over a prompt cue."""

    class _Frozen(_Researcher):
        def __setattr__(self, name, value):
            if name.endswith("_budget_hint"):
                raise AttributeError(name)
            object.__setattr__(self, name, value)

    role = _Frozen()
    memo = rc.ResearchCadenceMixin._compute_deep_research(_engine(role), _state(), "repeat",
                                                          trace=False)
    assert memo.summary == "ok"


def test_a_stamper_that_RAISES_does_not_reach_the_memo():
    """Containment at the call site, not only inside the stampers — an engine whose cue TEXT
    builder raises (a retune mid-run, a missing spec) must still produce the memo."""
    role = _Researcher()
    eng = _engine(role)
    eng._time_budget_hint_text = lambda: (_ for _ in ()).throw(RuntimeError("no spec"))
    memo = rc.ResearchCadenceMixin._compute_deep_research(eng, _state(), "repeat", trace=False)
    assert memo.summary == "ok"
