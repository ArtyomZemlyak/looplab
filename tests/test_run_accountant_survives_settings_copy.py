"""`llm_budget_usd` is a ceiling on the RUN, and "one accountant per run" must not depend on order.

Docs/57 `run-accountant-splits-on-settings-copy`. `core/llm.py::run_cost_accountant` caches the
run's one `CostAccountant` on the `Settings` object's `__dict__`, and pydantic's `model_copy`
copies that dict SHALLOWLY -- so a copy taken AFTER the attach shares the accountant and a copy
taken BEFORE mints its own. Reproduced here exactly as the marker recorded it: `Settings()`, copy,
attach on both -> two objects; attach first, copy after -> one. The default `run` path was saved
by ordering luck (preflight built the first client from the parent before the factory forked it);
the `wrap_up_only` path copied first and ran two ceilings on the one entry point that still spends.

The fix is that the ATTACH is explicit at every fork -- `cli/__init__.py::_engine` right after
settings resolve, and both fork sites in `agents/factory.py` on the PARENT before their
`model_copy` -- and that a deep copy shares the accountant instead of raising on its lock. The
fork sites are DRIVEN (real `make_developer_factory` / `build_unified_agent`, with only
`make_roles` replaced by a capture), because an AST check that the call is present could not tell
"before the copy" from "after it".
"""
from __future__ import annotations

import copy

import pytest

from looplab.core.config import Settings
from looplab.core.llm import CostAccountant, run_cost_accountant


def _settings(**kw) -> Settings:
    kw.setdefault("llm_budget_usd", 1.0)
    return Settings(**kw)


# ----------------------------------------------------------------- the mechanism, reproduced

def test_a_copy_taken_after_the_attach_shares_the_accountant():
    parent = _settings()
    accountant = run_cost_accountant(parent)
    child = parent.model_copy(update={"unified_agent": False})
    assert run_cost_accountant(child) is accountant
    assert accountant.limit == pytest.approx(1.0)


def test_the_copy_defect_is_real_which_is_why_every_fork_attaches_first():
    """The mechanism the fork-site rule rests on, stated so the rule has a falsifier: a settings
    object forked before any attach really does split the ceiling. Nothing in `run_cost_accountant`
    can repair that after the fact (the copy is an independent object with an empty slot), so the
    only place the property can be held is at the fork -- which the tests below drive."""
    parent = _settings()
    orphan = parent.model_copy(update={"unified_agent": False})
    assert run_cost_accountant(orphan) is not run_cost_accountant(parent)


# ---------------------------------------------------------------- the fork sites, DRIVEN

class _Captured(RuntimeError):
    """Raised by the `make_roles` double so a fork site stops right after forking."""


def _capturing_make_roles(seen: list):
    def make_roles(task, settings, *args, **kwargs):
        seen.append(settings)
        raise _Captured()
    return make_roles


def test_build_unified_agent_forks_a_settings_that_shares_the_parents_accountant(monkeypatch):
    import looplab.agents.factory as factory

    seen: list = []
    monkeypatch.setattr(factory, "make_roles", _capturing_make_roles(seen))
    parent = _settings()
    assert not hasattr(parent, "_looplab_run_accountant"), "the parent must start unattached"

    with pytest.raises(_Captured):
        factory.build_unified_agent(object(), parent)

    (split,) = seen
    assert split is not parent and split.unified_agent is False
    assert run_cost_accountant(split) is run_cost_accountant(parent), (
        "the unified_agent=False fork metered on its own ceiling")


def test_the_developer_factory_forks_a_settings_that_shares_the_parents_accountant(monkeypatch):
    import looplab.agents.factory as factory

    seen: list = []
    monkeypatch.setattr(factory, "make_roles", _capturing_make_roles(seen))
    parent = _settings()
    swap = factory.make_developer_factory(object(), parent)

    with pytest.raises(_Captured):
        swap("default")

    (forked,) = seen
    assert forked is not parent
    assert run_cost_accountant(forked) is run_cost_accountant(parent), (
        "a swapped-in Developer metered on its own ceiling")


def test_the_wrap_up_copy_inherits_once_the_entry_point_has_attached():
    """`_engine`'s own shape on the `wrap_up_only` path: the credential-free copy is the FIRST fork
    in that function, so the attach has to precede it. Driven through the real copy helper with
    the attach placed where `_engine` now places it."""
    from looplab.agents.preflight import credential_free_wrap_up_settings

    parent = _settings(llm_api_key="sk-test-not-real")
    accountant = run_cost_accountant(parent)                # what `_engine` does first
    degraded = credential_free_wrap_up_settings(parent)
    assert degraded is not parent and degraded.llm_api_key == ""
    assert run_cost_accountant(degraded) is accountant


def test_the_entry_point_attaches_before_its_first_fork():
    """The one site that cannot be driven without building an Engine: `_engine` must call
    `run_cost_accountant` before `credential_free_wrap_up_settings`, in source order. AST, never a
    substring (CLAUDE.md), and the order is asserted because the presence alone is not the rule."""
    from looplab.cli import _engine
    from tests._source_scan import called_names

    calls = called_names(_engine)
    assert "run_cost_accountant" in calls, "`_engine` no longer attaches the run accountant"
    assert calls.index("run_cost_accountant") < calls.index("credential_free_wrap_up_settings"), (
        "the attach must precede the wrap-up copy, or the wrap-up path runs two ceilings again")


# --------------------------------------------------------------------- the deep-copy half

def test_a_deep_copy_of_attached_settings_shares_the_accountant_instead_of_raising():
    """The latent half the marker named: `copy.deepcopy(settings)` raised `TypeError` on the
    accountant's lock once attached. A run's spend identity is not data to be duplicated."""
    parent = _settings()
    accountant = run_cost_accountant(parent)

    twin = copy.deepcopy(parent)

    assert twin is not parent
    assert run_cost_accountant(twin) is accountant
    assert run_cost_accountant(parent.model_copy(deep=True)) is accountant


def test_a_deep_copy_of_the_accountant_itself_is_the_same_object():
    accountant = CostAccountant(limit=2.0)
    accountant.add(0.5)
    assert copy.deepcopy(accountant) is accountant
    assert copy.deepcopy({"a": accountant})["a"] is accountant
