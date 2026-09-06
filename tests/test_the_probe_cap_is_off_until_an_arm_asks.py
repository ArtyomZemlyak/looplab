"""A count cap on `run_probe`, off by default, because §189 found the only lever worth an arm.

MEASURED over the 69 `edge_expansion` runs with a champion: of eleven process variables, only
`run_probe` separates the top thirteen from the bottom thirteen — 20 calls against 29, p = 0.037 —
while evaluated nodes (3 vs 3), `eval_train` calls (12 vs 12), file reads, generations and every
phase share are flat. Split at the corpus median of 24 probes, the champion is 221.81 against
177.84: **+43.97, two-sided p = 0.0077**, and **+50.03 (p = 0.0097)** restricted to the fifty runs
that evaluated exactly three nodes.

It is a correlation — a run that probes twenty-nine times may be probing BECAUSE it is lost — and
the cap is the only way to tell that apart from probing making it lost. §187 prices the arm at 48
probes for power 0.83, so the cap must exist before the arm and must change nothing until then.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from looplab.tools.dev_probe import DevProbeTools  # noqa: E402
from looplab.runtime import landlock  # noqa: E402


# Same rule as `tests/test_dev_probe.py`: on a kernel without Landlock the confined probe FAILS
# CLOSED (`exit=3`, by design), so any test that actually launches one is skipped here rather than
# red. Derivation-only tests keep running.
_NO_LANDLOCK = landlock.unavailable_reason()


@pytest.fixture(autouse=True)
def _skip_when_the_kernel_cannot_confine(monkeypatch):
    if not _NO_LANDLOCK:
        return
    original = DevProbeTools.execute_result

    def _guarded(self, name, args, **kw):
        code = str((args or {}).get("code") or "") if isinstance(args, dict) else ""
        if name == "run_probe" and code.strip() and getattr(self, "confine_reads", True):
            pytest.skip(f"the probe's kernel read rung fails closed here: {_NO_LANDLOCK}")
        return original(self, name, args, **kw)

    monkeypatch.setattr(DevProbeTools, "execute_result", _guarded)


def test_the_default_is_uncapped():
    """Every probe in the corpus ran uncapped; the default has to keep doing that."""
    tools = DevProbeTools()
    assert tools.max_calls == 0
    for _ in range(6):
        got = tools.execute_result("run_probe", {"code": "print(1)"})
        assert "refused" not in (got.content or ""), got.content


def test_the_cap_refuses_only_after_it_is_reached():
    tools = DevProbeTools(max_calls=2)
    first = [tools.execute_result("run_probe", {"code": "print(1)"}) for _ in range(2)]
    assert all(not r.is_error for r in first), [r.content for r in first]
    blocked = tools.execute_result("run_probe", {"code": "print(1)"})
    assert blocked.is_error and "run_probe refused" in blocked.content
    assert blocked.structured.get("refused") == "probe_cap"
    assert blocked.structured.get("cap") == 2


def test_the_refusal_names_the_cheaper_instrument():
    """A cap that only says no teaches nothing; the card's own answer is `eval_train`."""
    tools = DevProbeTools(max_calls=1)
    tools.execute_result("run_probe", {"code": "print(1)"})
    text = tools.execute_result("run_probe", {"code": "print(1)"}).content
    assert 'run_dev_command("eval_train")' in text, text
    assert "graded number" in text, text


def test_a_refused_probe_does_not_consume_more_of_the_cap():
    """The counter must not run away past the cap -- the message quotes it."""
    tools = DevProbeTools(max_calls=1)
    tools.execute_result("run_probe", {"code": "print(1)"})
    a = tools.execute_result("run_probe", {"code": "print(1)"}).content
    b = tools.execute_result("run_probe", {"code": "print(1)"}).content
    assert a == b, (a, b)


def test_an_unknown_tool_is_still_unknown_under_a_cap():
    tools = DevProbeTools(max_calls=1)
    got = tools.execute_result("nope", {})
    assert got.is_error and "unknown tool" in got.content


def _dev(**kw):
    """A bare developer object with just the probe wiring set, no task or engine needed."""
    from looplab.adapters.repo_developer import LLMRepoDeveloper
    obj = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    obj._probe = True
    obj._probe_repo_spec = None
    obj._probe_timeout_s = 60.0
    obj._probe_confine = True
    obj._probe_max_calls = kw.get("probe_max_calls", 0)
    obj._dev_commands = None
    return obj


def test_the_setting_defaults_to_uncapped():
    """`Settings.developer_probe_max_calls` exists for §190's arm and changes nothing until set."""
    from looplab.core.config import Settings
    assert Settings().developer_probe_max_calls == 0
    assert Settings(developer_probe_max_calls=12).developer_probe_max_calls == 12


def test_the_factory_threads_the_setting_to_the_role():
    """One place turns a setting into behaviour; if this stops passing it, the arm silently runs
    uncapped and its control and treatment become the same thing."""
    import inspect

    from looplab.agents import factory
    src = inspect.getsource(factory)
    assert "probe_max_calls=getattr(settings, \"developer_probe_max_calls\", 0)" in src, (
        "make_roles no longer passes the cap; an arm setting it would measure nothing")


def test_the_role_hands_the_cap_to_the_tool():
    import inspect

    from looplab.adapters import repo_developer as rd
    src = inspect.getsource(rd.LLMRepoDeveloper._tools_for_build) if hasattr(
        rd.LLMRepoDeveloper, "_tools_for_build") else inspect.getsource(rd)
    assert 'max_calls=getattr(self, "_probe_max_calls", 0)' in src, (
        "the developer builds DevProbeTools without the cap")


def test_the_cap_is_per_RUN_not_per_phase():
    """`_scout_tools` builds a fresh provider every phase, so a per-instance counter caps nothing.

    Measured live on 2026-09-04 with the first version: `capA1` ran 12 probes inside one phase span,
    was refused on the 13th, and the NEXT phase span started again at zero — 15 calls under a cap of
    12. §189's effect is measured per RUN (corpus median 24 probes), so a per-phase cap of 12 across
    three or four phases is not the treatment the arm registered. The owner passes ONE dict.
    """
    shared = {"n": 0}
    first = DevProbeTools(max_calls=3, counter=shared)
    for _ in range(3):
        assert not first.execute_result("run_probe", {"code": "print(1)"}).is_error
    second = DevProbeTools(max_calls=3, counter=shared)          # a new phase, same run
    blocked = second.execute_result("run_probe", {"code": "print(1)"})
    assert blocked.is_error and "this run has already made 3 probes" in blocked.content, blocked.content


def test_an_unshared_provider_still_counts_for_itself():
    """Without a counter the provider owns one, so a direct construction still honours its cap."""
    solo = DevProbeTools(max_calls=1)
    assert not solo.execute_result("run_probe", {"code": "print(1)"}).is_error
    assert solo.execute_result("run_probe", {"code": "print(1)"}).is_error


def test_the_developer_hands_one_counter_to_every_provider_it_builds():
    import inspect

    from looplab.adapters import repo_developer as rd
    src = inspect.getsource(rd)
    assert "counter=self._probe_call_counter()" in src, (
        "the developer builds probe providers without sharing a counter, so the cap resets per phase")
    dev = rd.LLMRepoDeveloper.__new__(rd.LLMRepoDeveloper)
    a = dev._probe_call_counter()
    a["n"] = 5
    assert dev._probe_call_counter() is a, "the counter is rebuilt per call, so nothing accumulates"
