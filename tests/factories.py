"""One canonical `Engine` construction for the test suite (doc 25 XP-11).

29 test files each defined a private `_engine(...)` around `Engine(...)`, and there are 150+ direct
`Engine(` constructions across the suite. Every one of them is a call site of the engine's keyword
API, which CLAUDE.md calls out as needing to stay stable precisely BECAUSE so many tests construct it
directly — so the cost of ever evolving that API is paid once per file rather than once.

`make_engine` is the shared shape those factories all had: load the toy task, build its roles, hand
the engine a subprocess sandbox and a `GreedyTree`. Everything else passes through as keyword
overrides, so a test that needs a different policy, a scripted role or an extra knob keeps saying so
locally instead of inheriting a default it did not choose.

Deliberately NOT a fixture: these constructions happen inside helper functions and parametrized
bodies as often as at test top level, and a fixture would force the call sites that need two engines
(resume/crash-recovery tests build a second one over the same run dir) to work around it.

Migration is opportunistic, as the finding says — this exists so new tests have one obvious way, and
so the next engine-API change has one place to start. It does not require a suite-wide rewrite.
"""
from __future__ import annotations

from pathlib import Path

from looplab.adapters.toytask import ToyTask
from looplab.engine.orchestrator import Engine
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

ROOT = Path(__file__).resolve().parents[1]
TOY_TASK = ROOT / "examples" / "toy_task.json"


def make_engine(run_dir, *, task_file=None, task=None, researcher=None, developer=None,
                sandbox=None, policy=None, n_seeds: int = 3, max_nodes: int = 8,
                **overrides) -> Engine:
    """Build an `Engine` over the toy task, overriding only what a test actually cares about.

    `n_seeds`/`max_nodes` shape the DEFAULT `GreedyTree`; pass `policy=` to replace it wholesale
    (the ablation/merge tests do). `task`/`researcher`/`developer`/`sandbox` accept scripted stubs.
    Anything else in `**overrides` goes straight to `Engine`, so this never becomes a second, lagging
    spelling of its keyword API.
    """
    task = task if task is not None else ToyTask.load(task_file or TOY_TASK)
    if researcher is None or developer is None:
        built_researcher, built_developer = task.build_roles()
        researcher = researcher if researcher is not None else built_researcher
        developer = developer if developer is not None else built_developer
    return Engine(
        run_dir,
        task=task,
        researcher=researcher,
        developer=developer,
        sandbox=sandbox if sandbox is not None else SubprocessSandbox(),
        policy=policy if policy is not None else GreedyTree(n_seeds=n_seeds, max_nodes=max_nodes),
        **overrides,
    )
