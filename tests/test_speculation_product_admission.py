"""Positive `speculation_depth` on REAL workloads (2026-08-04 operator decision).

The public positive-depth path used to require `workload_scope == "quadratic_toy"` AND
`type(task) is ToyTask`, so every Dataset/Repo/Command workload was refused at `Engine.__init__` and
the shipped knob only ever replayed its own benchmark. The node-budget refund
(`core/models.py::is_unevaluated_speculative_discard`, re-exported from `search/card_selection.py`,
its home until 2026-08-05) removed the cost that fence was
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


def _engine(run_dir, task, *, llm_roles: bool = False, **kwargs) -> Engine:
    """One Engine over `task`'s own roles.

    `llm_roles=True` marks the Developer as code-generating, which is what a real RepoTask
    Developer declares under `backend="llm"`. It matters only for AUTO widths: since 2026-08-05
    `speculation_depth=-1` settles to 0 when NO role calls a provider
    (`Engine._build_calls_an_llm`), because a prefetch exists to overlap provider latency and a
    role pair built with no client has none. `task.build_roles()` here takes no client, so the
    default fixture is exactly that "nothing to overlap" case — the AUTO-WIDTH tests below have to
    opt in to be testing the width at all rather than the settle-to-off rule.
    """
    researcher, developer = task.build_roles()
    if llm_roles:
        developer.is_code_generating = True
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
    # The product lane pins the run's search TREATMENT and its LANE — never an evidence identity.
    # Neither a runtime-scope digest nor a whole-source implementation digest is minted: nothing was
    # measured for this workload, and a source digest would make the run unresumable after any edit.
    assert pinned["speculation_gate_receipt_digest"] == (
        quality.speculation_product_authority_digest(policy_scope="greedy", task_kind="repo"))
    assert "speculation_runtime_scope_sha256" not in pinned
    assert "speculation_implementation_digest" not in pinned


def test_a_real_run_stays_resumable_after_the_source_tree_changes(tmp_path, monkeypatch):
    """The product lane must not pin a whole-source digest.

    `speculation_implementation_digest` hashes every shipped .py file, so pinning it would make a
    half-finished Repo/GPU run permanently unresumable after any edit or `pip install -U` — measured
    on a real 12-node repo run, whose re-entry was refused minutes later because unrelated files had
    changed. The calibrated lanes keep that pin (it IS their evidence identity); this one must not.
    """
    task = _repo_task(tmp_path)
    run_dir = tmp_path / "upgrade"
    first = _engine(run_dir, task, card_driven_selection=True, speculation_depth=2)
    first.store.append("run_started", {
        "run_id": run_dir.name,
        "task_id": task.id,
        "direction": "max",
        **first._run_start_pinned_values(),
    })

    monkeypatch.setattr(
        quality, "speculation_implementation_digest", lambda: "sha256:" + "f" * 64)
    resumed = _engine(run_dir, task, card_driven_selection=True, speculation_depth=2)
    resumed._require_pinned_speculation_receipt(fold(resumed.store.read_all()))  # no raise
    assert resumed._speculation_enabled() is True


def test_the_product_lane_pins_survive_re_entry_and_bind_the_exact_treatment(tmp_path):
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

    # ...and a DIFFERENT treatment on the same durable prefix still fails closed, NAMING the pin that
    # moved. The old message listed every pin the check knows about and left the operator to guess.
    changed = _engine(run_dir, task, card_driven_selection=True, speculation_depth=2)
    with pytest.raises(SpeculationAuthorizationError,
                       match=r"speculation_depth was pinned at 3 and this process resolved 2"):
        changed._require_pinned_speculation_receipt(fold(changed.store.read_all()))


def test_a_calibrated_log_cannot_be_resumed_into_the_product_lane(tmp_path, monkeypatch):
    """The two lanes are disjoint: neither may reinterpret the other's speculative prefix.

    The receipt-authorized lane is the CALIBRATED one — the benchmark's own workload under its own
    receipt. That is the only place a receipt measured anything, so it is the only place a lane
    crossing is possible at all (see the test below for what a receipt does on a real workload).
    """
    monkeypatch.setattr(
        quality, "validated_speculation_gate_receipt", lambda _path: _toy_receipt())
    run_dir = tmp_path / "lane-crossing"
    task = ToyTask()
    settings = Settings(**{
        **SPECULATION_CALIBRATION_PROFILE_SETTINGS,
        "max_nodes": 3,
        "speculation_depth": 1,
        "speculation_gate_receipt": str(tmp_path / "receipt.json"),
    })

    def roles():
        return (
            ToyResearcher(task.bounds, seed=task.seed, step=task.step,
                          calibration_concepts=True),
            ToyObjectiveDeveloper(noise=0.0, calibration_gpu_probe=True),
        )

    attested = Engine(
        run_dir, task=task, researcher=roles()[0], developer=roles()[1],
        sandbox=SubprocessSandbox(),
        policy=GreedyTree(n_seeds=3, max_nodes=3, debug_depth=1),
        options=EngineOptions.from_settings(settings),
        role_factory=roles,
        _speculation_runtime_scope_sha256=speculation_runtime_scope_digest(
            settings.masked_snapshot()),
    )
    attested.store.append("run_started", {
        "run_id": run_dir.name,
        "task_id": task.id,
        "direction": "min",
        **attested._run_start_pinned_values(),
    })

    receiptless = _engine(run_dir, ToyTask(), card_driven_selection=True, speculation_depth=1)
    with pytest.raises(SpeculationAuthorizationError, match=r"calibrated lane .* product"):
        receiptless._require_pinned_speculation_receipt(fold(receiptless.store.read_all()))


def test_a_receipt_supplied_on_a_real_workload_binds_nothing_and_stays_resumable(
        tmp_path, monkeypatch):
    """A receipt that measured the toy benchmark must not decide a Repo/GPU run's re-entry.

    It used to: a receipt that was VALID at run start pinned `speculation_implementation_digest` —
    the whole-source digest — into `run_started` on any workload. The next source edit or
    `pip install -U` revoked the receipt, the resume declined it into the product lane with an empty
    digest, and the equality check refused the run FOREVER. Dropping the receipt from the resume
    command did not help, because that lands in the same product lane. So on a workload the receipt
    did not measure it now binds nothing in either direction, and the run resumes with or without it.
    """
    valid = _toy_receipt()
    monkeypatch.setattr(
        quality, "validated_speculation_gate_receipt", lambda _path: valid)
    run_dir = tmp_path / "receipt-on-a-real-workload"
    task = _repo_task(tmp_path)
    started = _engine(
        run_dir, task,
        card_driven_selection=True,
        speculation_depth=1,
        speculation_gate_receipt=str(tmp_path / "receipt.json"),
    )
    pinned = started._run_start_pinned_values()
    # The log records the PRODUCT lane, so it never claims an attestation that did not hold here.
    assert "speculation_implementation_digest" not in pinned
    assert "speculation_runtime_scope_sha256" not in pinned
    assert pinned["speculation_gate_receipt_digest"] == (
        quality.speculation_product_authority_digest(policy_scope="greedy", task_kind="repo"))
    started.store.append("run_started", {
        "run_id": run_dir.name, "task_id": task.id, "direction": "max", **pinned})
    entry = fold(started.store.read_all())

    # The source moved on, so the receipt is now DECLINED — the exact trap that used to strand a run.
    monkeypatch.setattr(quality, "validated_speculation_gate_receipt", lambda _path: None)
    with_receipt = _engine(
        run_dir, task, card_driven_selection=True, speculation_depth=1,
        speculation_gate_receipt=str(tmp_path / "receipt.json"))
    with_receipt._require_pinned_speculation_receipt(entry)          # no raise

    # ...and so does dropping the receipt from the resume command.
    without_receipt = _engine(run_dir, task, card_driven_selection=True, speculation_depth=1)
    without_receipt._require_pinned_speculation_receipt(entry)       # no raise


def test_a_legacy_receipt_pinned_log_is_adopted_instead_of_stranded(tmp_path):
    """The runs already stuck by the bug above are unstuck, and only that exact shape is.

    Invariant #6: the run_start record owns these values. A log with an implementation digest AND a
    receipt digest but NO runtime-scope pin and no calibration fields is precisely the old
    "receipt on a real workload" shape, whose whole-source digest no later process can reproduce.
    """
    task = _repo_task(tmp_path)
    run_dir = tmp_path / "legacy-adoption"
    engine = _engine(run_dir, task, card_driven_selection=True, speculation_depth=1)
    engine.store.append("run_started", {
        "run_id": run_dir.name, "task_id": task.id, "direction": "max",
        **engine._run_start_pinned_values(),
    })
    entry = fold(engine.store.read_all())
    legacy = entry.model_copy(update={
        "speculation_implementation_digest": _IMPLEMENTATION,
        "speculation_gate_receipt_digest": "sha256:" + "a" * 64,
    })

    resumed = _engine(run_dir, task, card_driven_selection=True, speculation_depth=1)
    resumed._require_pinned_speculation_receipt(legacy)              # no raise

    # A CALIBRATED log (it carries a runtime-scope pin) is never adopted away by a product-lane
    # process — the envelope its evidence was measured under still has to be re-established.
    calibrated = legacy.model_copy(update={
        "speculation_runtime_scope_sha256": "sha256:" + "d" * 64})
    refuser = _engine(run_dir, task, card_driven_selection=True, speculation_depth=1)
    with pytest.raises(SpeculationAuthorizationError, match="runtime-scope pin differs"):
        refuser._require_pinned_speculation_receipt(calibrated)


def test_the_product_lane_token_bumped_its_schema_without_stranding_a_live_run(tmp_path):
    """B3: `implementation_digest` left the hashed preimage; the schema id had to follow.

    Two different preimages under one schema id is the collision a schema id exists to prevent. The
    bump is real (the token changes), and re-entry accepts the superseded spelling so a run that was
    already underway when the bump landed is not refused.
    """
    tokens = quality.speculation_product_authority_digests(
        policy_scope="greedy", task_kind="repo")
    assert tokens[0] == quality.speculation_product_authority_digest(
        policy_scope="greedy", task_kind="repo")
    assert quality.SPECULATION_PRODUCT_AUTHORITY_SCHEMA.endswith("/v2")
    assert quality.SPECULATION_PRODUCT_AUTHORITY_LEGACY_SCHEMAS == (
        "looplab.speculation-product-authority/v1",)
    assert len(set(tokens)) == len(tokens) == 2      # the bump actually changed the token

    task = _repo_task(tmp_path)
    run_dir = tmp_path / "schema-bump"
    engine = _engine(run_dir, task, card_driven_selection=True, speculation_depth=1)
    engine.store.append("run_started", {
        "run_id": run_dir.name, "task_id": task.id, "direction": "max",
        **engine._run_start_pinned_values(),
    })
    entry = fold(engine.store.read_all())
    assert entry.speculation_gate_receipt_digest == tokens[0]        # only v2 is ever MINTED

    superseded = entry.model_copy(update={"speculation_gate_receipt_digest": tokens[1]})
    resumed = _engine(run_dir, task, card_driven_selection=True, speculation_depth=1)
    resumed._require_pinned_speculation_receipt(superseded)          # no raise

    # An unrelated token is still refused, with the cause named.
    foreign = entry.model_copy(update={
        "speculation_gate_receipt_digest": "sha256:" + "e" * 64})
    with pytest.raises(SpeculationAuthorizationError, match="lane token differs"):
        resumed._require_pinned_speculation_receipt(foreign)


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

@pytest.mark.parametrize("receipt", [
    None,                                          # revalidation failed outright
    _toy_receipt(require_gpu=False),               # measured without GPUs
    _toy_receipt(workload_scope="repo"),           # forged workload scope
    {**_toy_receipt(), "implementation_digest": ""},   # forged implementation digest
])
def test_a_stale_or_forged_receipt_is_declined_not_honoured_on_a_real_workload(
        tmp_path, monkeypatch, receipt):
    """Refused means NOT HONOURED — and on a real workload that must not be a hard raise.

    The receipt authorizes nothing here (the operator's setting is the authority), so raising at
    `Engine.__init__` would take a whole Repo/GPU run down over an attestation the run does not
    need — a trap now that `card_driven_selection` defaults True. The run proceeds in the product
    lane and `run_started` records the product-lane token, so the log never claims the attestation.
    """
    monkeypatch.setattr(
        quality, "validated_speculation_gate_receipt", lambda _path: receipt)
    engine = _engine(
        tmp_path / "bad-receipt",
        _repo_task(tmp_path),
        card_driven_selection=True,
        speculation_depth=1,
        speculation_gate_receipt=str(tmp_path / "receipt.json"),
    )
    assert engine._speculation_enabled() is True
    pinned = engine._run_start_pinned_values()
    assert pinned["speculation_gate_receipt_digest"] == (
        quality.speculation_product_authority_digest(policy_scope="greedy", task_kind="repo"))
    assert "speculation_implementation_digest" not in pinned    # no attestation was recorded


@pytest.mark.parametrize("receipt,match", [
    (None, "stale, invalid"),
    (_toy_receipt(require_gpu=False), "non-GPU"),
    (_toy_receipt(workload_scope="repo"), "policy-mismatched"),
])
def test_a_stale_or_forged_receipt_still_hard_refuses_the_calibrated_toy_lane(
        tmp_path, monkeypatch, receipt, match):
    """On the calibration toy the receipt IS the authority, so a bad one still raises."""
    monkeypatch.setattr(
        quality, "validated_speculation_gate_receipt", lambda _path: receipt)
    task = ToyTask()
    settings = Settings(**{
        **SPECULATION_CALIBRATION_PROFILE_SETTINGS,
        "max_nodes": 3,
        "speculation_depth": 1,
        "speculation_gate_receipt": str(tmp_path / "receipt.json"),
    })

    def roles():
        return (
            ToyResearcher(task.bounds, seed=task.seed, step=task.step,
                          calibration_concepts=True),
            ToyObjectiveDeveloper(noise=0.0, calibration_gpu_probe=True),
        )

    with pytest.raises(ValueError, match=match):
        Engine(
            tmp_path / "toy-bad-receipt",
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
        tmp_path / "auto-explicit-width", task, llm_roles=True,
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
        tmp_path / "auto-gpu-width", task, llm_roles=True,
        card_driven_selection=True, speculation_depth=-1, eval_parallel=0)
    assert gpu_auto._eval_parallel == 4 and gpu_auto.speculation_depth == 4

    monkeypatch.setattr(orchestrator, "_detect_gpu_ids", lambda: [])
    cpu_auto = _engine(
        tmp_path / "auto-cpu-width", task, llm_roles=True,
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
        tmp_path / "auto-clamped", task, llm_roles=True,
        card_driven_selection=True, speculation_depth=-1, eval_parallel=1024)
    assert wide.speculation_depth == 64                    # clamped to the field ceiling
    pinned = wide._run_start_pinned_values()
    assert pinned["speculation_depth"] == 64               # the RESOLVED integer is what is pinned


def test_auto_depth_adopts_the_pinned_depth_on_a_differently_sized_box(tmp_path):
    """Invariant #6: the run_started record wins over a live AUTO re-resolution."""
    task = _repo_task(tmp_path)
    run_dir = tmp_path / "auto-resume"
    first = _engine(run_dir, task, llm_roles=True, card_driven_selection=True,
                    speculation_depth=-1, eval_parallel=4)
    assert first.speculation_depth == 4
    first.store.append("run_started", {
        "run_id": run_dir.name,
        "task_id": task.id,
        "direction": "max",
        **first._run_start_pinned_values(),
    })

    # Same run, resumed on a two-lane box: AUTO re-resolves to 2, the log says 4, the log wins.
    resumed = _engine(run_dir, task, llm_roles=True, card_driven_selection=True,
                      speculation_depth=-1, eval_parallel=2)
    assert resumed.speculation_depth == 2
    resumed._require_pinned_speculation_receipt(fold(resumed.store.read_all()))
    assert resumed.speculation_depth == 4

    # An EXPLICIT depth is never adopted: a changed explicit treatment still fails closed.
    explicit = _engine(run_dir, task, card_driven_selection=True,
                       speculation_depth=2, eval_parallel=2)
    with pytest.raises(SpeculationAuthorizationError,
                       match=r"speculation_depth was pinned at 4 and this process resolved 2"):
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
    """A helper nobody calls is a hope, not an invariant — pin the call site itself.

    Pinned on the AST, not on the method NAME appearing in `_evaluate`'s source: `_evaluate` carries a
    why-comment naming this helper, so a substring test is satisfied with the call deleted outright,
    and satisfied again with the call moved AFTER the workdir — the one ordering it exists to forbid.
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(Engine._evaluate)))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "_assert_speculative_selection_confirmed"
             and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"]
    assert len(calls) == 1, (
        f"`_evaluate` must CALL the invariant exactly once (found {len(calls)}); "
        "a comment naming it is not a call site")
    assert [ast.unparse(arg) for arg in calls[0].args] == ["state", "node"], (
        f"the funnel calls it as `{ast.unparse(calls[0])}` — the folded state and node are what it judges")

    # It must run BEFORE anything that can start a sandbox: the workdir is the first such step.
    workdirs = [node for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and node.value == "nodes"]
    assert workdirs, "`_evaluate` no longer builds the node workdir under `nodes/`"
    assert calls[0].lineno < min(node.lineno for node in workdirs), (
        "the invariant is checked AFTER the workdir is derived — an unconfirmed prediction can "
        "already be on its way into a sandbox")

    # `assert` statements vanish under `python -O`; this one must not.
    body = inspect.getsource(Engine._assert_speculative_selection_confirmed)
    assert "raise SpeculativeEvaluationInvariantError" in body


def test_the_run_records_whether_speculation_cost_it_budget(tmp_path):
    """The precondition became an OBSERVATION: `charged_discards` is the regression signal.

    The refund shrank the measured harm ~10x (mean_normalized_regret 0.025643 -> 0.002590) but not
    to zero, so the cheap way to notice a regression has to survive dropping the pre-run receipt.
    """
    task = _repo_task(tmp_path)
    engine = _engine(tmp_path / "observed", task,
                     card_driven_selection=True, speculation_depth=2)
    node_id = _speculative_node_without_its_link(engine, task)
    state = fold(engine.store.read_all())

    # A prefetch that is still `pending` when the run finishes is ABANDONED, not discarded — and it
    # is charged by `card_budget_used` all the same. Reporting 0 here was the harm going unmeasured:
    # `_drop_stale_speculation` only terminalizes STALE prefetches, so every committed one the
    # consumer never admitted (eval-seconds budget crossed, wall deadline, operator stop) sat in this
    # blind spot — up to `speculation_depth` slots, one per GPU under AUTO.
    abandoned = quality.speculation_budget_observation(state)
    assert abandoned["abandoned"] == 1
    assert abandoned["discarded"] == 0
    assert abandoned["charged_discards"] == 1
    assert abandoned["depth"] == 2

    # An UNLINKED speculative node that failed is charged too, and used to be counted as neither
    # discarded nor refunded because the universe was keyed on the committed link alone.
    node = state.nodes[node_id]
    state.nodes[node_id] = node.model_copy(update={
        "status": node.status.__class__.failed,
        "error": "speculative node creation was rejected during replay",
        "error_reason": "superseded",
        "eval_seconds": 0.0,
        "never_evaluated": True,
    })
    assert node_id not in state.speculative_nodes
    unlinked = quality.speculation_budget_observation(state)
    assert unlinked["discarded"] == 1
    assert unlinked["refunded"] == 0            # no committed link => no refund
    assert unlinked["charged_discards"] == 1

    # A discarded speculative build that the log proves never ran is REFUNDED, not charged.
    state.speculative_nodes[node_id] = {"card_id": "card-1", "generation": 0}
    state.nodes[node_id] = state.nodes[node_id].model_copy(update={
        "eval_start_boundary": True})
    refunded = quality.speculation_budget_observation(state)
    assert refunded["discarded"] == 1
    assert refunded["refunded"] == 1
    assert refunded["charged_discards"] == 0

    # ...and one that DID consume an evaluation is charged — exactly the regression to notice.
    state.nodes[node_id] = state.nodes[node_id].model_copy(update={
        "eval_seconds": 12.5, "never_evaluated": False})
    charged = quality.speculation_budget_observation(state)
    assert charged["discarded"] == 1
    assert charged["refunded"] == 0
    assert charged["charged_discards"] == 1

    # ...including the one the log can only see through the eval-START boundary: a prefetch killed
    # mid-training, terminalized `superseded` by a resumed process whose in-memory in-flight set was
    # empty. Zero charged seconds, no stage row — and still not free.
    state.nodes[node_id] = state.nodes[node_id].model_copy(update={
        "eval_seconds": 0.0, "never_evaluated": True, "eval_started": True})
    interrupted = quality.speculation_budget_observation(state)
    assert interrupted["refunded"] == 0
    assert interrupted["charged_discards"] == 1


def test_the_budget_observation_never_raises_on_a_partial_state(tmp_path):
    """`finalize_run` swallows a raise here, taking the `finalize_step` marker with it.

    That leaves `requirements_complete` false and a run that never acknowledges its own
    finalization — from a PURE projection, which would fail identically on every retry. The
    docstring claimed totality; `refunded_card_budget_node_ids(state)` reached into `state.nodes`
    unguarded and did not deliver it.
    """

    class _Foreign:                     # no `.nodes` at all
        speculative_nodes: dict = {}
        card_build_outcomes: list = []
        card_build_requests: list = []
        speculation_depth = 1

    observation = quality.speculation_budget_observation(_Foreign())
    assert observation["depth"] == 1
    assert observation["charged_discards"] == 0
    # A state whose predicate blows up over-reports rather than handing out a false all-clear.
    task = _repo_task(tmp_path)
    engine = _engine(tmp_path / "hostile", task,
                     card_driven_selection=True, speculation_depth=1)
    node_id = _speculative_node_without_its_link(engine, task)
    hostile = fold(engine.store.read_all())
    object.__setattr__(hostile, "breed_excluded", _Unreadable())
    assert quality.speculation_budget_observation(hostile)["charged_discards"] == 1
    assert node_id == 0


class _Unreadable:
    """Every membership test raises — a stand-in for any partial/foreign state field."""

    def __contains__(self, _item):
        raise RuntimeError("unreadable")


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
