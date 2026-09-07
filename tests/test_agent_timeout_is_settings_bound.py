"""The external coding agent's wall is the operator's, not the constructor's.

`CliAgentDeveloper.__init__` carries `timeout: float = 600.0`, and `agents/factory.py` never passed
the argument. So on every composed run that constructor default WAS the value: no config file, no
`LOOPLAB_*` env var and no UI field could move it. Ten minutes is a launch-time constant nobody chose
for a repo task, where the agent seeds a whole worktree, reads it and writes several files.

Driven through `make_roles`, the real composition root — a signature check would pass on a factory
that accepted the setting and dropped it, which is one character away from the state this fixed.
"""
from __future__ import annotations

import pytest

from looplab.core.config import Settings


def _built(monkeypatch, **over):
    """Build the roles with the CLI-agent Developer selected, capturing its constructor kwargs."""
    from looplab.agents import cli_agent as cli_mod
    from looplab.agents import factory as factory_mod
    from looplab.adapters.tasks import validate_task

    #  imports the class INSIDE the branch, so the seam is its home module.
    seen: dict = {}
    real = cli_mod.CliAgentDeveloper

    class _Spy(real):
        def __init__(self, **kwargs):
            seen.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(cli_mod, "CliAgentDeveloper", _Spy)

    settings = Settings(developer="agent", developer_backend="opencode", **over)
    task = validate_task({"kind": "quadratic", "task_id": "t", "goal": "g", "direction": "min",
                          "expr": "(x-3)**2"})
    factory_mod.make_roles(task, settings)
    return seen


def test_the_operators_timeout_reaches_the_agent(monkeypatch):
    """THE DEFECT. MUTATION: drop the `timeout=` argument -> this is 600.0 whatever the operator
    configured, and no surface in the product can say otherwise."""
    seen = _built(monkeypatch, agent_timeout=5400.0)
    assert seen.get("timeout") == pytest.approx(5400.0)


def test_an_unset_config_is_the_historical_value(monkeypatch):
    """The default is the constructor's own, so a run that configures nothing is byte-identical to
    what shipped."""
    assert _built(monkeypatch).get("timeout") == pytest.approx(600.0)
    assert Settings().agent_timeout == pytest.approx(600.0)


def test_it_is_bounded_like_every_other_wall():
    """An agent that never exits is a build slot held forever. MUTATION: make it unbounded -> a
    typo'd config can hang one node's build for the life of the process."""
    for bad in (0, -1, 48 * 3600.0):
        with pytest.raises(Exception):
            Settings(agent_timeout=bad)
    assert Settings(agent_timeout=24 * 3600.0).agent_timeout == pytest.approx(86_400.0)


def test_the_env_var_reaches_it(monkeypatch):
    """Settings are flat so `LOOPLAB_<FIELD>` maps 1:1 (CLAUDE.md); this is the surface an operator
    on a box actually uses."""
    monkeypatch.setenv("LOOPLAB_AGENT_TIMEOUT", "1800")
    assert Settings().agent_timeout == pytest.approx(1800.0)


def test_it_is_NOT_the_eval_clock():
    """Two different walls on two different processes, and conflating them is the mistake this
    setting's name invites: `max_eval_timeout` bounds a node's EVALUATION, this bounds ONE Developer
    invocation. They must not share a default or a ceiling by accident."""
    assert Settings().agent_timeout != Settings().max_eval_timeout
