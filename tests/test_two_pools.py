"""Layer 2 (docs/23): the two decoupled concurrency axes eval_parallel / llm_parallel.

The canonical names win over the legacy max_parallel / parallel_build when set; unset (None) falls back
to the legacy field so a bare Engine is byte-identical to today. The Strategist steers the new names, and
either name keeps the runtime attr AND its read-through alias (_eval_parallel / _llm_parallel) in sync.
Layer 2 delivers NO throughput change (the spine still alternates) — this only locks the plumbing."""
from __future__ import annotations

import looplab.engine.orchestrator as _orch
from looplab.adapters.toytask import ToyTask
from looplab.agents.roles import ToyObjectiveDeveloper, ToyResearcher
from looplab.engine.orchestrator import Engine
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree


def _engine(tmp_path, **kw) -> Engine:
    task = ToyTask()
    return Engine(tmp_path, task=task,
                  researcher=ToyResearcher(task.bounds, seed=task.seed, step=task.step),
                  developer=ToyObjectiveDeveloper(), sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=2, max_nodes=3), **kw)


def test_defaults_are_byte_identical_to_legacy(tmp_path):
    e = _engine(tmp_path / "d")
    assert e.max_parallel == 1 and e.parallel_build == 1
    assert e._eval_parallel == 1 and e._llm_parallel == 1


def test_new_names_win_over_legacy(tmp_path):
    e = _engine(tmp_path / "w", eval_parallel=4, max_parallel=2, llm_parallel=3, parallel_build=1)
    # Concrete resolved values (not X==X): eval_parallel beat max_parallel, llm_parallel beat parallel_build.
    assert e.max_parallel == 4 and e._eval_parallel == 4
    assert e.parallel_build == 3 and e._llm_parallel == 3


def test_legacy_still_works_when_new_names_unset(tmp_path):
    e = _engine(tmp_path / "l", max_parallel=3, parallel_build=2)
    assert e.max_parallel == 3 and e._eval_parallel == 3          # None eval_parallel -> legacy
    assert e.parallel_build == 2 and e._llm_parallel == 2


def test_eval_parallel_auto_resolves_to_gpu_count(tmp_path, monkeypatch):
    # 0 = AUTO -> max(1, len(gpu_ids)). Inject a 3-GPU inventory so AUTO is DISTINGUISHED from
    # fall-through-to-1 (a CPU box would make eval_parallel=0 coincide with the default).
    monkeypatch.setattr(_orch, "_detect_gpu_ids", lambda: [0, 1, 2])
    e = _engine(tmp_path / "a", eval_parallel=0)
    assert e._gpu_ids == [0, 1, 2]
    assert e.max_parallel == 3 and e._eval_parallel == 3


def test_llm_parallel_auto_tracks_resolved_eval(tmp_path):
    # llm_parallel=0 -> AUTO -> the resolved eval width (build as many seeds as you can eval).
    e = _engine(tmp_path / "la", eval_parallel=4, llm_parallel=0)
    assert e.max_parallel == 4 and e.parallel_build == 4 and e._llm_parallel == 4


def test_strategist_steers_new_names_and_syncs_alias(tmp_path):
    e = _engine(tmp_path / "s")
    e._apply_strategy({"eval_parallel": 5})
    assert e.max_parallel == 5 and e._eval_parallel == 5          # runtime attr AND alias updated
    e._apply_strategy({"llm_parallel": 3})
    assert e.parallel_build == 3 and e._llm_parallel == 3
    e._apply_strategy({"eval_parallel": 0})                       # AUTO/0 must still clamp to >=1
    assert e.max_parallel == 1 and e._eval_parallel == 1


def test_strategist_legacy_name_still_applies(tmp_path):
    e = _engine(tmp_path / "sl")
    e._apply_strategy({"max_parallel": 6})
    assert e.max_parallel == 6 and e._eval_parallel == 6          # legacy alias keeps the new alias synced


def test_strategist_canonical_wins_when_both_present(tmp_path):
    # A strategy carrying BOTH names must land the CANONICAL value (matches __init__), not the legacy one.
    e = _engine(tmp_path / "b")
    e._apply_strategy({"eval_parallel": 8, "max_parallel": 4})
    assert e.max_parallel == 8 and e._eval_parallel == 8
    e._apply_strategy({"llm_parallel": 7, "parallel_build": 3})
    assert e.parallel_build == 7 and e._llm_parallel == 7


def test_steering_one_axis_leaves_the_other_untouched(tmp_path):
    # The central L2 promise: the two axes are INDEPENDENT. Steering evals must not move the LLM axis.
    e = _engine(tmp_path / "dec")
    e._apply_strategy({"eval_parallel": 9})
    assert e.max_parallel == 9 and e.parallel_build == 1 and e._llm_parallel == 1
    e._apply_strategy({"llm_parallel": 5})
    assert e.parallel_build == 5 and e.max_parallel == 9          # eval axis unchanged by the build steer
