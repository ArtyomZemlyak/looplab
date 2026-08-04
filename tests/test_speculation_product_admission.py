"""Positive `speculation_depth` on REAL workloads (2026-08-04 operator decision).

The public positive-depth path used to require `workload_scope == "quadratic_toy"` AND
`type(task) is ToyTask`, so every Dataset/Repo/Command workload was refused at `Engine.__init__` and
the shipped knob only ever replayed its own benchmark. The node-budget refund
(`search/card_selection.py::is_unevaluated_speculative_discard`) removed the cost that fence was
protecting, so the fence is gone. What these tests pin is what replaced it:

* any TaskAdapter admits positive depth, with no receipt at all;
* a supplied receipt is still revalidated, and a stale/forged one is still refused;
* AUTO depth resolves exactly as documented (the settled eval width), and an explicit value wins;
* the safety property the whole argument rests on — a speculative build must never consume an
  evaluation before its selection is confirmed — is ASSERTED at the single dispatch funnel.
"""
from __future__ import annotations

import inspect
import sys

import anyio
import pytest

import looplab.search.speculation_quality as quality
from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.adapters.toytask import ToyTask
from looplab.agents.roles import ToyObjectiveDeveloper, ToyResearcher
from looplab.core.config import Settings
from looplab.core.models import Idea
from looplab.engine.evaluate import SpeculativeEvaluationInvariantError
from looplab.engine.options import EngineOptions
from looplab.engine.orchestrator import Engine, SpeculationAuthorizationError
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree
from looplab.search.speculation_calibration import (
    SPECULATION_CALIBRATION_PROFILE_DIGEST,
    SPECULATION_CALIBRATION_PROFILE_SETTINGS,
    speculation_runtime_scope_digest,
)

_METRIC = {"kind": "stdout_json", "key": "metric"}
_IMPLEMENTATION = "sha256:" + "c" * 64


def _repo_task(tmp_path, *, body: str = 'import json; print(json.dumps({"metric": 1.0}))\n'):
    """A real (non-toy) `repo` TaskAdapter with a millisecond eval."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "run.py").write_text(body, encoding="utf-8")
    (repo / "params.txt").write_text("x=0\n", encoding="utf-8")
    return RepoTask(
        id="repo_admission", goal="raise the metric", direction="max",
        editable_path=str(repo), edit_surface=["*.txt"],
        eval=EvalSpec(command=[sys.executable, "run.py"], metric=_METRIC),
    )


def _engine(run_dir, task, **kwargs) -> Engine:
    researcher, developer = task.build_roles()
    return Engine(
        run_dir,
        task=task,
        researcher=researcher,
        developer=developer,
        sandbox=SubprocessSandbox(),
        policy=GreedyTree(n_seeds=1, max_nodes=4),
        role_factory=task.build_roles,
        **kwargs,
    )


def _toy_receipt(*, require_gpu: bool = True, admitted_depth: int = 1,
                 workload_scope: str = "quadratic_toy") -> dict:
    return {
        "self_digest": "sha256:" + "a" * 64,
        "implementation_digest": _IMPLEMENTATION,
        "require_gpu": require_gpu,
        "policy_scope": "greedy",
        "workload_scope": workload_scope,
        "task_profile_sha256": quality.speculation_task_profile_digest(ToyTask()),
        "admitted_depth": admitted_depth,
        "admitted_max_nodes": 3,
        "calibration_profile_digest": SPECULATION_CALIBRATION_PROFILE_DIGEST,
        "runtime_scope_sha256": speculation_runtime_scope_digest({
            **SPECULATION_CALIBRATION_PROFILE_SETTINGS,
            "max_nodes": 3,
            "speculation_depth": admitted_depth,
            "speculation_gate_receipt": "receipt.json",
        }),
        "gpu_inventory": [{
            "index": 0,
            "uuid": "GPU-" + "1" * 32,
            "pci_bus_id": "00000000:01:00.0",
            "name": "Synthetic GPU",
            "mem_total_mib": 24_576,
            "driver_version": "600.1",
            "cuda_driver_version": 13000,
        }] if require_gpu else [],
    }


# ---------------------------------------------------------------- the workload fence is gone

def test_a_real_repo_task_admits_positive_depth_without_any_receipt(tmp_path):
    engine = _engine(
        tmp_path / "repo-speculation",
        _repo_task(tmp_path),
        card_driven_selection=True,
        speculation_depth=2,
    )

    assert engine.speculation_gate_receipt is None
    assert engine._speculation_enabled() is True
    pinned = engine._run_start_pinned_values()
    assert pinned["speculation_depth"] == 2
    assert pinned["card_driven_selection"] is True
    assert pinned["speculation_policy_scope"] == "greedy"
    # The product lane pins the run's IDENTITY (code + policy + workload), never a calibrated
    # runtime-scope digest — none was measured for this workload, so minting one would be a lie.
    assert pinned["speculation_gate_receipt_digest"] == (
        quality.speculation_product_authority_digest(
            policy_scope="greedy",
            implementation_digest=pinned["speculation_implementation_digest"],
            task_kind="repo",
        ))
    assert "speculation_runtime_scope_sha256" not in pinned


def test_the_dataset_lane_is_admitted_the_same_way_and_replay_pins_survive(tmp_path):
    """Whole-lifecycle proof: admitted, pinned, and re-entrant on the same durable record."""
    task = _repo_task(tmp_path)
    run_dir = tmp_path / "reentry"
    first = _engine(run_dir, task, card_driven_selection=True, speculation_depth=3)
    first.store.append("run_started", {
        "run_id": run_dir.name,
        "task_id": task.id,
        "direction": "max",
        **first._run_start_pinned_values(),
    })

    resumed = _engine(run_dir, task, card_driven_selection=True, speculation_depth=3)
    resumed._require_pinned_speculation_receipt(fold(resumed.store.read_all()))  # no raise

    # ...and a DIFFERENT treatment on the same durable prefix still fails closed.
    changed = _engine(run_dir, task, card_driven_selection=True, speculation_depth=2)
    with pytest.raises(SpeculationAuthorizationError, match="exact validated"):
        changed._require_pinned_speculation_receipt(fold(changed.store.read_all()))


def test_a_receipt_authorized_log_cannot_be_resumed_into_the_product_lane(
        tmp_path, monkeypatch):
    """The two lanes are disjoint: neither may reinterpret the other's speculative prefix."""
    monkeypatch.setattr(
        quality, "validated_speculation_gate_receipt", lambda _path: _toy_receipt())
    run_dir = tmp_path / "lane-crossing"
    task = _repo_task(tmp_path)
    attested = _engine(
        run_dir, task,
        card_driven_selection=True,
        speculation_depth=1,
        speculation_gate_receipt=str(tmp_path / "receipt.json"),
    )
    attested.store.append("run_started", {
        "run_id": run_dir.name,
        "task_id": task.id,
        "direction": "max",
        **attested._run_start_pinned_values(),
    })

    receiptless = _engine(run_dir, task, card_driven_selection=True, speculation_depth=1)
    with pytest.raises(SpeculationAuthorizationError, match="exact validated"):
        receiptless._require_pinned_speculation_receipt(fold(receiptless.store.read_all()))


def test_speculation_still_requires_the_greedy_policy_scope(tmp_path):
    """Not a workload fence: the freshness test asks the POLICY for the counterfactual action."""
    with pytest.raises(ValueError, match="requires policy='greedy'"):
        _engine(
            tmp_path / "wrong-policy",
            _repo_task(tmp_path),
            card_driven_selection=True,
            speculation_depth=1,
            policy_name="mcts",
        )


# ---------------------------------------------------------------- a supplied receipt still binds

@pytest.mark.parametrize("receipt,match", [
    (None, "stale, invalid"),                                   # revalidation failed outright
    (_toy_receipt(require_gpu=False), "non-GPU"),               # measured without GPUs
    (_toy_receipt(workload_scope="repo"), "policy-mismatched"),  # forged workload scope
])
def test_a_stale_or_forged_receipt_is_still_refused_on_a_real_workload(
        tmp_path, monkeypatch, receipt, match):
    monkeypatch.setattr(
        quality, "validated_speculation_gate_receipt", lambda _path: receipt)
    with pytest.raises(ValueError, match=match):
        _engine(
            tmp_path / "bad-receipt",
            _repo_task(tmp_path),
            card_driven_selection=True,
            speculation_depth=1,
            speculation_gate_receipt=str(tmp_path / "receipt.json"),
        )


def test_a_forged_implementation_digest_is_refused(tmp_path, monkeypatch):
    forged = {**_toy_receipt(), "implementation_digest": ""}
    monkeypatch.setattr(
        quality, "validated_speculation_gate_receipt", lambda _path: forged)
    with pytest.raises(ValueError, match="stale, invalid"):
        _engine(
            tmp_path / "forged-impl",
            _repo_task(tmp_path),
            card_driven_selection=True,
            speculation_depth=1,
            speculation_gate_receipt=str(tmp_path / "receipt.json"),
        )


def test_the_toy_replay_lane_keeps_its_full_narrow_envelope(tmp_path, monkeypatch):
    """Running the benchmark's OWN workload under its own receipt still binds every measured pin."""
    monkeypatch.setattr(
        quality, "validated_speculation_gate_receipt",
        lambda _path: _toy_receipt(admitted_depth=1))
    task = ToyTask()
    settings = Settings(**{
        **SPECULATION_CALIBRATION_PROFILE_SETTINGS,
        "max_nodes": 3,
        "speculation_depth": 2,                      # receipt admitted depth 1
        "speculation_gate_receipt": str(tmp_path / "receipt.json"),
    })

    def roles():
        return (
            ToyResearcher(task.bounds, seed=task.seed, step=task.step,
                          calibration_concepts=True),
            ToyObjectiveDeveloper(noise=0.0, calibration_gpu_probe=True),
        )

    with pytest.raises(ValueError, match="policy/depth-mismatched"):
        Engine(
            tmp_path / "toy-replay",
            task=task,
            researcher=roles()[0],
            developer=roles()[1],
            sandbox=SubprocessSandbox(),
            policy=GreedyTree(n_seeds=3, max_nodes=3, debug_depth=1),
            options=EngineOptions.from_settings(settings),
            role_factory=roles,
            _speculation_runtime_scope_sha256=speculation_runtime_scope_digest(
                settings.masked_snapshot()),
        )


# ---------------------------------------------------------------- AUTO depth

def test_auto_depth_follows_the_settled_eval_width(tmp_path, monkeypatch):
    task = _repo_task(tmp_path)
    # Explicit eval_parallel: AUTO depth is exactly that width.
    engine = _engine(
        tmp_path / "auto-explicit-width", task,
        card_driven_selection=True, speculation_depth=-1, eval_parallel=3)
    assert engine._eval_parallel == 3
    assert engine.speculation_depth == 3
    assert engine._speculation_depth_auto is True
    assert engine._speculation_enabled() is True

    # eval_parallel=0 is itself AUTO (one experiment per detected GPU, at least one), and AUTO depth
    # follows the RESOLVED width — never the raw setting.
    import looplab.engine.orchestrator as orchestrator
    monkeypatch.setattr(orchestrator, "_detect_gpu_ids", lambda: [0, 1, 2, 3])
    gpu_auto = _engine(
        tmp_path / "auto-gpu-width", task,
        card_driven_selection=True, speculation_depth=-1, eval_parallel=0)
    assert gpu_auto._eval_parallel == 4 and gpu_auto.speculation_depth == 4

    monkeypatch.setattr(orchestrator, "_detect_gpu_ids", lambda: [])
    cpu_auto = _engine(
        tmp_path / "auto-cpu-width", task,
        card_driven_selection=True, speculation_depth=-1, eval_parallel=0)
    assert cpu_auto._eval_parallel == 1 and cpu_auto.speculation_depth == 1

    # An explicit integer always overrides AUTO, and 0 stays the hard off-switch.
    explicit = _engine(
        tmp_path / "auto-override", task,
        card_driven_selection=True, speculation_depth=5, eval_parallel=2)
    assert explicit.speculation_depth == 5 and explicit._speculation_depth_auto is False
    off = _engine(
        tmp_path / "auto-off", task,
        card_driven_selection=True, speculation_depth=0, eval_parallel=2)
    assert off.speculation_depth == 0 and off._speculation_enabled() is False


def test_auto_depth_is_clamped_and_never_reaches_the_durable_log_unresolved(tmp_path):
    task = _repo_task(tmp_path)
    wide = _engine(
        tmp_path / "auto-clamped", task,
        card_driven_selection=True, speculation_depth=-1, eval_parallel=1024)
    assert wide.speculation_depth == 64                    # clamped to the field ceiling
    pinned = wide._run_start_pinned_values()
    assert pinned["speculation_depth"] == 64               # the RESOLVED integer is what is pinned


def test_auto_depth_adopts_the_pinned_depth_on_a_differently_sized_box(tmp_path):
    """Invariant #6: the run_started record wins over a live AUTO re-resolution."""
    task = _repo_task(tmp_path)
    run_dir = tmp_path / "auto-resume"
    first = _engine(run_dir, task, card_driven_selection=True,
                    speculation_depth=-1, eval_parallel=4)
    assert first.speculation_depth == 4
    first.store.append("run_started", {
        "run_id": run_dir.name,
        "task_id": task.id,
        "direction": "max",
        **first._run_start_pinned_values(),
    })

    # Same run, resumed on a two-lane box: AUTO re-resolves to 2, the log says 4, the log wins.
    resumed = _engine(run_dir, task, card_driven_selection=True,
                      speculation_depth=-1, eval_parallel=2)
    assert resumed.speculation_depth == 2
    resumed._require_pinned_speculation_receipt(fold(resumed.store.read_all()))
    assert resumed.speculation_depth == 4

    # An EXPLICIT depth is never adopted: a changed explicit treatment still fails closed.
    explicit = _engine(run_dir, task, card_driven_selection=True,
                       speculation_depth=2, eval_parallel=2)
    with pytest.raises(SpeculationAuthorizationError, match="exact validated"):
        explicit._require_pinned_speculation_receipt(fold(explicit.store.read_all()))


def test_settings_carries_the_auto_sentinel_and_rejects_anything_below_it():
    assert Settings(speculation_depth=-1).speculation_depth == -1
    assert Settings(speculation_depth=0).speculation_depth == 0
    with pytest.raises(Exception):
        Settings(speculation_depth=-2)
    with pytest.raises(Exception):
        Settings(speculation_depth=65)


# ------------------------------------- the property the whole admission argument depends on

def _speculative_node_without_its_link(engine: Engine, task) -> int:
    """One speculative attempt-zero node whose `card_build_done` link never landed."""
    engine.store.append("run_started", {
        "run_id": engine.run_dir.name,
        "task_id": task.id,
        "direction": "max",
        **engine._run_start_pinned_values(),
    })
    idea = Idea(operator="draft", params={}, rationale="prefetch", hypothesis="prefetch",
                card_id="card-1")
    engine.store.append("node_created", {
        "node_id": 0,
        "generation": 0,
        "operator": "draft",
        "idea": idea.model_dump(mode="json"),
        "code": 'print("{\\"metric\\": 1.0}")',
        "parent_ids": [],
        "speculative": True,
        "card_build_generation": 0,
    })
    state = fold(engine.store.read_all())
    assert state.nodes[0].speculative is True
    assert 0 not in state.speculative_nodes      # the confirmation receipt is exactly what is absent
    return 0


def test_a_speculative_build_must_not_consume_an_evaluation_before_confirmation(tmp_path):
    """The safety property positive depth is admitted on: a miss is free ONLY if it never ran.

    A speculative node that reached the sandbox before its selection was confirmed would be real GPU
    time on a real training run — the cost the admission argument does NOT cover.
    """
    task = _repo_task(tmp_path)
    engine = _engine(tmp_path / "unconfirmed", task,
                     card_driven_selection=True, speculation_depth=1)
    node_id = _speculative_node_without_its_link(engine, task)

    with pytest.raises(SpeculativeEvaluationInvariantError, match="without a confirmed selection"):
        anyio.run(engine._evaluate, node_id, anyio.CapacityLimiter(1))

    # Nothing was spent: no terminal, no workdir.
    state = fold(engine.store.read_all())
    assert state.nodes[node_id].status.value == "pending"
    assert not (engine.run_dir / "nodes" / f"node_{node_id}").exists()


def test_the_invariant_is_wired_at_the_single_dispatch_funnel():
    """A helper nobody calls is a hope, not an invariant — pin the call site itself."""
    source = inspect.getsource(Engine._evaluate)
    assert "_assert_speculative_selection_confirmed" in source
    # It must run BEFORE anything that can start a sandbox: the workdir is the first such step.
    assert source.index("_assert_speculative_selection_confirmed") < source.index('"nodes"')
    # `assert` statements vanish under `python -O`; this one must not.
    body = inspect.getsource(Engine._assert_speculative_selection_confirmed)
    assert "raise SpeculativeEvaluationInvariantError" in body


def test_an_ordinary_node_and_a_confirmed_speculative_node_are_unaffected(tmp_path):
    """The invariant is narrow: only an attempt-zero speculative node without its link is refused."""
    task = _repo_task(tmp_path)
    engine = _engine(tmp_path / "confirmed", task,
                     card_driven_selection=True, speculation_depth=1)
    node_id = _speculative_node_without_its_link(engine, task)
    state = fold(engine.store.read_all())

    # An ordinary node: never consulted.
    ordinary = state.nodes[node_id].model_copy(update={"speculative": False})
    engine._assert_speculative_selection_confirmed(state, ordinary)

    # A confirmed speculative node: the durable link names this exact Card and request epoch.
    state.speculative_nodes[node_id] = {"card_id": "card-1", "generation": 0}
    engine._assert_speculative_selection_confirmed(state, state.nodes[node_id])

    # A link for a DIFFERENT request epoch is not a confirmation of this build.
    state.speculative_nodes[node_id] = {"card_id": "card-1", "generation": 7}
    with pytest.raises(SpeculativeEvaluationInvariantError):
        engine._assert_speculative_selection_confirmed(state, state.nodes[node_id])
