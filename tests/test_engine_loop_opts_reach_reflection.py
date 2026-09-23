"""The operator's tool-loop options reach the agent loops the ENGINE runs itself.

Review 2026-09-22, found by ENG1-03's knob census. `engine/lessons_distill.py::_reflect_loop_opts`
built the run-end reflection loop's options from `getattr(engine, "settings", None)`, and no real
`Engine` has a `settings` attribute — only one test double ever set it. So every run-end reflection
ran on the DEFAULT options: under the shipped Settings the one difference is `context_budget_chars`
(1,000,000 configured against the loop's built-in fallback), and an operator who lowered it for a
small-context model still got the fallback at finalize. The CLI, which holds the Settings, now hands
`loop_opts_from_settings(settings)` to `Engine(loop_opts=...)`.

DRIVEN through the only production constructor (`cli/__init__.py::_engine`) and the real
`_reflect_loop_opts`, not pinned: the defect was a read that type-checked and answered a default.
"""
from __future__ import annotations

from looplab.core.config import Settings


def test_the_configured_options_reach_the_run_end_reflection_loop(tmp_path):
    """MUTATION: drop `loop_opts=` from the CLI's `Engine(...)` call (or restore the `settings`
    read) -> the reflection loop's budget is the loop's unset default again, not 12,345."""
    import looplab.cli as cli
    from looplab.adapters.toytask import ToyTask

    engine = cli._engine(tmp_path / "run", ToyTask(),
                         Settings(backend="toy", context_budget_chars=12_345, agent_stuck_repeat=7),
                         None)
    opts = engine.lessons._reflect_loop_opts()
    assert opts["context_budget_chars"] == 12_345
    assert opts["stuck_repeat"] == 7
    # The tight cap still WINS over the configured `agent_max_turns` (the method's own contract).
    assert opts["max_turns"] == 15


def test_a_bare_engine_keeps_the_defaults_its_reflection_always_got(tmp_path):
    """No CLI, no options: what every directly-constructed Engine (and every run before this fix)
    handed the loop — the defaults plus the 15-turn cap."""
    from looplab.agents.loop_options import LoopOptions
    from tests.factories import make_engine

    engine = make_engine(tmp_path / "run")
    assert dict(engine.lessons._reflect_loop_opts()) == dict(LoopOptions().replace(max_turns=15))
