"""Three seams where a name or a rule was written twice (doc 25 AG-08, AG-10, RA-10).

* AG-08 — two unrelated `run_phase` functions in one package, disambiguated only by signature.
* AG-10 — the prompt-cue tuple as an inline literal in BOTH researchers, under a docstring promising
  they honour the same cues and a registry test that scans the write side, not these reads.
* RA-10 — mle-bench registry/data-dir resolution written three times, in a module claiming to be
  "the single place the registry/data-dir resolution lives".
"""
from __future__ import annotations

import inspect

import pytest

from looplab.agents import agent as agent_mod
from looplab.agents import roles as roles_mod
from looplab.agents import strategist as strategist_mod
from looplab.agents.roles import RESEARCHER_HINT_ATTRS, RESEARCHER_PROMPT_CUES


# ------------------------------------------------------------------ AG-10: one prompt-cue tuple

def test_the_prompt_cues_are_a_strict_subset_of_the_hint_registry():
    """A cue not in the registry would never be SET by anyone, so the prompt would splice an
    always-empty string and the nudge would silently never reach the model."""
    assert set(RESEARCHER_PROMPT_CUES) <= set(RESEARCHER_HINT_ATTRS)
    assert len(RESEARCHER_PROMPT_CUES) < len(RESEARCHER_HINT_ATTRS), (
        "the prompt cues are a SUBSET by design; the rest are consumed structurally")


def test_the_subset_is_exactly_the_prose_cues():
    """`_digest_cap` is a numeric cap, `_hyp_order` orders the board inside `_state_brief`, and
    `_novelty_stance` / `_steering_context` / `_cross_run_advisory_receipt` are read structurally —
    none of them is concatenated into the user turn as prose."""
    assert RESEARCHER_PROMPT_CUES == (
        "_complexity_hint", "_sweep_hint", "_novelty_feedback", "_novelty_hint")


@pytest.mark.parametrize("module,holder,method", [
    ("roles", "LLMResearcher", "propose"),
    ("agent", "ToolUsingResearcher", "propose"),
])
def test_both_researchers_read_the_SAME_cue_tuple(module, holder, method):
    """The promise in the registry docstring — "both researchers honor the same cues" — was
    unchecked. A cue added to one inline literal and not the other makes the agentic path and the
    plain path ask the model different questions, with no test to notice."""
    owner = {"roles": roles_mod, "agent": agent_mod}[module]
    source = inspect.getsource(getattr(getattr(owner, holder), method))
    assert "RESEARCHER_PROMPT_CUES" in source, f"{holder}.{method} re-derives the cue tuple"
    assert '"_complexity_hint"' not in source, f"{holder}.{method} still inlines the cue literal"


def test_the_tuple_is_declared_in_exactly_one_place():
    # Matched at the START of a tuple line, so the same names appearing inside the broader
    # RESEARCHER_HINT_ATTRS registry (prefixed by `_digest_cap`) are not counted as a second copy.
    combined = inspect.getsource(roles_mod) + inspect.getsource(agent_mod)
    assert combined.count(
        '\n    "_complexity_hint", "_sweep_hint", "_novelty_feedback", "_novelty_hint")') == 1


# ------------------------------------------------------------------ AG-08: the run_phase collision

def test_the_strategist_classifier_no_longer_shares_the_tool_loop_seam_name():
    """`agents/agent.py::run_phase` is the tool-loop-with-handoff wrapper and a documented patch
    seam that MUST keep its name; the strategist's run classifier moved instead."""
    assert hasattr(strategist_mod, "classify_run_phase")
    assert not hasattr(strategist_mod, "run_phase"), (
        "a back-compat alias would preserve exactly the collision the rename removes")
    assert callable(agent_mod.run_phase), "the tool-loop seam must keep its name"


def test_the_classifier_still_answers_the_four_phases():
    from looplab.core.models import RunState
    from looplab.agents.strategist import classify_run_phase

    state = RunState()
    assert classify_run_phase(state, 3) == "seed", "an empty run is seeding"
    state.confirmed_done = True
    assert classify_run_phase(state, 3) == "confirm", "confirm wins over every other phase"


def test_the_only_importer_follows_the_rename():
    from looplab.engine import strategy

    source = inspect.getsource(strategy)
    assert "classify_run_phase(state, self.n_seeds)" in source
    assert "run_phase(state, self.n_seeds)" not in source.replace("classify_run_phase", "X")


# ------------------------------------------------------------------ RA-10: one registry resolution

def test_every_mlebench_module_resolves_the_registry_through_one_helper():
    """The `.resolve()` is the part that matters. `registry.set_data_dir` keys the whole competition
    layout off that path, so a relative `--data-dir` resolved by one caller and left relative by
    another points at two different trees whenever the process cwd differs — and the symptom is
    "not prepared" or a grade against the wrong answers, never an error."""
    from looplab.adapters import mlebench_grade, mlebench_prep, mlebench_real

    for module in (mlebench_prep, mlebench_grade):
        source = inspect.getsource(module)
        assert "set_data_dir" not in source, f"{module.__name__} resolves the registry itself"
        assert "_competition(" in source, f"{module.__name__} does not use the shared resolver"
    # The CALL, not the word: the helper's docstring explains what `set_data_dir` does.
    assert inspect.getsource(mlebench_real).count("registry.set_data_dir(") == 1


def test_the_prepared_check_is_defined_once_and_shared():
    from looplab.adapters import mlebench_prep, mlebench_real

    assert mlebench_prep.is_prepared is mlebench_real.is_prepared, (
        "two `is_prepared` definitions would let prep and the task disagree about which tree is "
        "prepared")


def test_the_single_place_comment_is_now_true():
    from looplab.adapters import mlebench_real

    doc = mlebench_real._competition.__doc__ or ""
    assert "single place the registry/data-dir resolution lives" in doc
    assert "doc 25 RA-10" in doc, "the claim needs its own provenance now that it is enforced"


def test_the_shared_resolver_resolves_a_relative_data_dir(monkeypatch, tmp_path):
    """Driven, not asserted from source: a relative path must reach `set_data_dir` absolute."""
    import sys
    import types

    seen = {}

    class _Registry:
        def set_data_dir(self, path):
            seen["data_dir"] = path
            return self

        def get_competition(self, competition_id):
            seen["competition"] = competition_id
            return "competition"

    module = types.ModuleType("mlebench.registry")
    module.registry = _Registry()
    package = types.ModuleType("mlebench")
    monkeypatch.setitem(sys.modules, "mlebench", package)
    monkeypatch.setitem(sys.modules, "mlebench.registry", module)
    monkeypatch.chdir(tmp_path)

    from looplab.adapters.mlebench_real import _competition

    assert _competition("spaceship-titanic", "data") == "competition"
    assert seen["data_dir"].is_absolute(), "a relative --data-dir reached the registry unresolved"
    assert seen["data_dir"] == (tmp_path / "data").resolve()


def test_no_data_dir_leaves_the_default_registry_untouched(monkeypatch):
    import sys
    import types

    calls = []

    class _Registry:
        def set_data_dir(self, path):
            calls.append(path)
            return self

        def get_competition(self, competition_id):
            return "competition"

    module = types.ModuleType("mlebench.registry")
    module.registry = _Registry()
    monkeypatch.setitem(sys.modules, "mlebench", types.ModuleType("mlebench"))
    monkeypatch.setitem(sys.modules, "mlebench.registry", module)

    from looplab.adapters.mlebench_real import _competition

    assert _competition("spaceship-titanic") == "competition"
    assert calls == [], "no --data-dir must not re-point the registry at anything"
