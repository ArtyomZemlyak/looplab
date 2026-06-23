"""I12 integration: the orchestrator-wired multi-seed confirmation phase."""
from __future__ import annotations

import anyio

from autornd.eventstore import EventStore
from autornd.orchestrator import Engine
from autornd.policy import GreedyTree
from autornd.replay import fold
from autornd.roles import ToyObjectiveDeveloper, ToyResearcher
from autornd.sandbox import SubprocessSandbox
from autornd.toytask import ToyTask


def _noisy_engine(run_dir, *, confirm_top_k, confirm_seeds, max_nodes=10):
    task = ToyTask(id="toy_noisy", goal="noisy quadratic", direction="min",
                   bounds={"x": (-10.0, 10.0), "y": (-10.0, 10.0)},
                   seed=3, step=1.5, noise=0.8)
    researcher = ToyResearcher(task.bounds, seed=task.seed, step=task.step)
    developer = ToyObjectiveDeveloper(noise=task.noise)
    return Engine(run_dir, task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=3, max_nodes=max_nodes),
                  confirm_top_k=confirm_top_k, confirm_seeds=confirm_seeds)


def test_confirmation_phase_runs_and_picks_robust_best(tmp_path):
    rd = tmp_path / "run"
    state = anyio.run(_noisy_engine(rd, confirm_top_k=3, confirm_seeds=6).run)
    assert state.finished

    confirmed = [n for n in state.nodes.values() if n.confirmed_mean is not None]
    assert 1 <= len(confirmed) <= 3                      # only top-k get confirmed

    # node_confirmed events were actually written to the log.
    events = list(EventStore(rd / "events.jsonl").read_all())
    assert sum(1 for e in events if e.type == "node_confirmed") == len(confirmed)

    # The final best is chosen from the confirmed pool by its robust mean.
    best = state.best()
    assert best is not None and best.confirmed_mean is not None
    assert best.confirmed_std is not None


def test_no_confirmation_by_default_is_unchanged(tmp_path):
    """confirm disabled (default) -> no node_confirmed events, best ranks by metric."""
    state = anyio.run(_noisy_engine(tmp_path / "run", confirm_top_k=0, confirm_seeds=0).run)
    assert state.finished
    assert all(n.confirmed_mean is None for n in state.nodes.values())
    best = state.best()
    assert best is not None and best.confirmed_mean is None


def test_confirmation_survives_replay(tmp_path):
    """Re-folding the log reproduces the confirmed best exactly (determinism)."""
    rd = tmp_path / "run"
    s1 = anyio.run(_noisy_engine(rd, confirm_top_k=2, confirm_seeds=5).run)
    s2 = fold(EventStore(rd / "events.jsonl").read_all())
    assert s2.best_node_id == s1.best_node_id
    assert s2.model_dump() == s1.model_dump()
