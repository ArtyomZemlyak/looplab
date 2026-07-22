from __future__ import annotations

import anyio
import orjson
import pytest

import looplab.search.speculation_quality as quality
from looplab.adapters.toytask import ToyTask
from looplab.agents.roles import ToyObjectiveDeveloper, ToyResearcher
from looplab.cli.run_cmds import _run_engine_guarded
from looplab.core.config import Settings
from looplab.engine.options import EngineOptions
from looplab.engine.orchestrator import (
    Engine,
    SPECULATION_CALIBRATION_PROFILE_DIGEST,
    SPECULATION_CALIBRATION_PROFILE_SETTINGS,
    SPECULATION_CALIBRATION_VARIANT_FIELDS,
    SpeculationAuthorizationError,
)
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree
from looplab.search.speculation_calibration import speculation_runtime_scope_digest


_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_IMPLEMENTATION = "sha256:" + "c" * 64


def _runtime_scope(*, max_nodes: int = 3, depth: int = 1,
                   receipt: str | None = "receipt.json") -> str:
    return speculation_runtime_scope_digest({
        **SPECULATION_CALIBRATION_PROFILE_SETTINGS,
        "max_nodes": max_nodes,
        "speculation_depth": depth,
        "speculation_gate_receipt": receipt,
    })


def test_calibration_profile_covers_every_non_variant_setting_as_snapshot_json():
    assert set(SPECULATION_CALIBRATION_PROFILE_SETTINGS) == (
        set(Settings.model_fields) - set(SPECULATION_CALIBRATION_VARIANT_FIELDS))
    assert SPECULATION_CALIBRATION_VARIANT_FIELDS == {
        "max_nodes", "speculation_depth", "speculation_gate_receipt"}
    assert orjson.loads(orjson.dumps(
        SPECULATION_CALIBRATION_PROFILE_SETTINGS,
        option=orjson.OPT_SORT_KEYS,
    )) == SPECULATION_CALIBRATION_PROFILE_SETTINGS
    assert SPECULATION_CALIBRATION_PROFILE_SETTINGS["llm_api_key"] is None


def _engine(run_dir, **kwargs) -> Engine:
    task = ToyTask()
    if kwargs.get("speculation_gate_receipt") is not None:
        settings = Settings(**{
            **SPECULATION_CALIBRATION_PROFILE_SETTINGS,
            "max_nodes": 3,
            "speculation_depth": kwargs.get("speculation_depth", 0),
            "speculation_gate_receipt": kwargs["speculation_gate_receipt"],
        })

        def roles():
            return (
                ToyResearcher(
                    task.bounds, seed=task.seed, step=task.step,
                    calibration_concepts=True),
                ToyObjectiveDeveloper(noise=0.0, calibration_gpu_probe=True),
            )

        return Engine(
            run_dir,
            task=task,
            researcher=roles()[0],
            developer=roles()[1],
            sandbox=SubprocessSandbox(),
            policy=GreedyTree(n_seeds=3, max_nodes=3, debug_depth=1),
            options=EngineOptions.from_settings(settings),
            role_factory=roles,
            _speculation_runtime_scope_sha256=speculation_runtime_scope_digest(
                settings.masked_snapshot()),
            **kwargs,
        )
    return Engine(
        run_dir,
        task=task,
        researcher=ToyResearcher(task.bounds, seed=task.seed, step=task.step),
        developer=ToyObjectiveDeveloper(),
        sandbox=SubprocessSandbox(),
        policy=GreedyTree(n_seeds=1, max_nodes=3),
        **kwargs,
    )


def _receipt(digest: str = _DIGEST_A, *, require_gpu: bool = True,
             admitted_depth: int = 1) -> dict:
    return {
        "self_digest": digest,
        "implementation_digest": _IMPLEMENTATION,
        "require_gpu": require_gpu,
        "policy_scope": "greedy",
        "workload_scope": "quadratic_toy",
        "task_profile_sha256": quality.speculation_task_profile_digest(ToyTask()),
        "admitted_depth": admitted_depth,
        "admitted_max_nodes": 3,
        "runtime_scope_sha256": _runtime_scope(depth=admitted_depth),
        "calibration_profile_digest": SPECULATION_CALIBRATION_PROFILE_DIGEST,
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


def _calibration_engine(run_dir, monkeypatch, *, depth: int) -> Engine:
    task = ToyTask()
    settings = Settings(**{
        **SPECULATION_CALIBRATION_PROFILE_SETTINGS,
        "max_nodes": 3,
        "speculation_depth": depth,
    })
    researcher = ToyResearcher(
        task.bounds, seed=task.seed, step=task.step, calibration_concepts=True)
    developer = ToyObjectiveDeveloper(noise=0.0, calibration_gpu_probe=True)

    def roles():
        return (
            ToyResearcher(
                task.bounds, seed=task.seed, step=task.step,
                calibration_concepts=True),
            ToyObjectiveDeveloper(noise=0.0, calibration_gpu_probe=True),
        )

    import looplab.core.hardware as hardware
    monkeypatch.setattr(hardware, "effective_gpu_inventory", lambda: [{
        "index": 0, "uuid": "GPU-" + "1" * 32,
        "pci_bus_id": "00000000:01:00.0", "name": "Synthetic GPU",
        "mem_total_mib": 24_576, "driver_version": "600.1",
        "cuda_driver_version": 13000,
        "mem_free_mib": 20_000,
    }])
    monkeypatch.setattr(
        quality, "speculation_implementation_digest", lambda: _IMPLEMENTATION)
    return Engine(
        run_dir,
        task=task,
        researcher=researcher,
        developer=developer,
        sandbox=SubprocessSandbox(),
        policy=GreedyTree(n_seeds=3, max_nodes=3, debug_depth=1),
        options=EngineOptions.from_settings(settings),
        role_factory=roles,
        _speculation_gate_calibration=True,
        _speculation_runtime_scope_sha256=speculation_runtime_scope_digest(
            settings.masked_snapshot()),
    )


def test_public_positive_depth_requires_a_current_gpu_quality_receipt(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="requires speculation_gate_receipt"):
        _engine(
            tmp_path / "missing",
            card_driven_selection=True,
            speculation_depth=1,
        )

    monkeypatch.setattr(quality, "validated_speculation_gate_receipt", lambda _path: None)
    with pytest.raises(ValueError, match="stale, invalid"):
        _engine(
            tmp_path / "invalid",
            card_driven_selection=True,
            speculation_depth=1,
            speculation_gate_receipt=str(tmp_path / "receipt.json"),
        )

    monkeypatch.setattr(
        quality,
        "validated_speculation_gate_receipt",
        lambda _path: _receipt(require_gpu=False),
    )
    with pytest.raises(ValueError, match="non-GPU"):
        _engine(
            tmp_path / "cpu-only",
            card_driven_selection=True,
            speculation_depth=1,
            speculation_gate_receipt=str(tmp_path / "receipt.json"),
        )


def test_valid_receipt_admits_and_pins_exact_digest(tmp_path, monkeypatch):
    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setattr(
        quality,
        "validated_speculation_gate_receipt",
        lambda path: _receipt(admitted_depth=2) if str(path) == str(receipt_path) else None,
    )
    engine = _engine(
        tmp_path / "admitted",
        card_driven_selection=True,
        speculation_depth=2,
        speculation_gate_receipt=str(receipt_path),
    )

    assert engine._speculation_enabled() is True
    assert engine.speculation_gate_receipt == str(receipt_path)
    assert engine._speculation_gate_receipt_digest == _DIGEST_A
    assert engine._run_start_pinned_values() == {
        "holdout_fraction": 0.0,
        "holdout_select": False,
        "select_verifier": False,
        "select_verifier_samples": 3,
        "verifier_ci_tie": False,
        "card_driven_selection": True,
        "speculation_depth": 2,
        "speculation_implementation_digest": _IMPLEMENTATION,
        "speculation_runtime_scope_sha256": _runtime_scope(depth=2),
        "speculation_gate_receipt_digest": _DIGEST_A,
        "speculation_policy_scope": "greedy",
    }


def test_resume_rejects_missing_or_different_recorded_receipt(tmp_path, monkeypatch):
    receipt_path = tmp_path / "receipt.json"
    current = {_DIGEST_A}
    monkeypatch.setattr(
        quality,
        "validated_speculation_gate_receipt",
        lambda _path: _receipt(next(iter(current))),
    )

    missing_dir = tmp_path / "missing-record"
    missing = _engine(
        missing_dir,
        card_driven_selection=True,
        speculation_depth=1,
        speculation_gate_receipt=str(receipt_path),
    )
    missing.store.append("run_started", {
        "run_id": "missing-record",
        "task_id": "toy",
        "direction": "min",
        "card_driven_selection": True,
        "speculation_depth": 1,
    })
    with pytest.raises(RuntimeError, match="exact validated"):
        missing._reentry_repin()

    changed_dir = tmp_path / "changed-record"
    first = _engine(
        changed_dir,
        card_driven_selection=True,
        speculation_depth=1,
        speculation_gate_receipt=str(receipt_path),
    )
    first.store.append("run_started", {
        "run_id": "changed-record",
        "task_id": "toy",
        "direction": "min",
        **first._run_start_pinned_values(),
    })
    current.clear()
    current.add(_DIGEST_B)
    resumed = _engine(
        changed_dir,
        card_driven_selection=True,
        speculation_depth=1,
        speculation_gate_receipt=str(receipt_path),
    )
    with pytest.raises(RuntimeError, match="exact validated"):
        resumed._reentry_repin()


def test_run_rejects_receipt_mismatch_before_recovery_ack_setup_or_log_write(
        tmp_path, monkeypatch):
    receipt_path = tmp_path / "receipt.json"
    current = {_DIGEST_A}
    monkeypatch.setattr(
        quality,
        "validated_speculation_gate_receipt",
        lambda _path: _receipt(next(iter(current))),
    )
    run_dir = tmp_path / "ordered-reentry"
    first = _engine(
        run_dir,
        card_driven_selection=True,
        speculation_depth=1,
        speculation_gate_receipt=str(receipt_path),
    )
    first.store.append("run_started", {
        "run_id": "ordered-reentry",
        "task_id": "toy",
        "direction": "min",
        **first._run_start_pinned_values(),
    })
    before = (run_dir / "events.jsonl").read_bytes()

    current.clear()
    current.add(_DIGEST_B)
    resumed = _engine(
        run_dir,
        card_driven_selection=True,
        speculation_depth=1,
        speculation_gate_receipt=str(receipt_path),
    )
    for name in ("_recover_interrupted_builds", "_ack_commands", "_setup_phase"):
        monkeypatch.setattr(
            resumed,
            name,
            lambda *_args, _name=name, **_kwargs: (_ for _ in ()).throw(
                AssertionError(f"{_name} crossed the receipt gate")
            ),
        )

    with pytest.raises(RuntimeError, match="exact validated"):
        anyio.run(resumed.run)
    assert (run_dir / "events.jsonl").read_bytes() == before


def test_inert_depth_needs_no_receipt_and_calibration_has_exact_envelope(
        tmp_path, monkeypatch):
    inert = _engine(
        tmp_path / "inert",
        card_driven_selection=False,
        speculation_depth=3,
    )
    assert inert._speculation_enabled() is False
    assert "speculation_depth" not in inert._run_start_pinned_values()
    assert "speculation_gate_receipt_digest" not in inert._run_start_pinned_values()

    with pytest.raises(ValueError, match="calibration profile mismatch"):
        _engine(
            tmp_path / "unscoped-calibration",
            card_driven_selection=True,
            speculation_depth=1,
            _speculation_gate_calibration=True,
        )

    baseline = _calibration_engine(tmp_path / "baseline", monkeypatch, depth=0)
    treatment = _calibration_engine(tmp_path / "treatment", monkeypatch, depth=1)
    assert baseline._speculation_enabled() is False
    assert treatment._speculation_enabled() is True
    for engine, depth in ((baseline, 0), (treatment, 1)):
        pinned = engine._run_start_pinned_values()
        assert "speculation_gate_receipt_digest" not in pinned
        assert pinned["speculation_depth"] == depth
        assert pinned["speculation_implementation_digest"] == _IMPLEMENTATION
        assert pinned["speculation_runtime_scope_sha256"] == _runtime_scope(depth=depth)
        assert pinned["speculation_calibration_profile_digest"] == (
            SPECULATION_CALIBRATION_PROFILE_DIGEST)
        assert pinned["speculation_calibration_gpu_inventory"] == [{
            "index": 0, "uuid": "GPU-" + "1" * 32,
            "pci_bus_id": "00000000:01:00.0", "name": "Synthetic GPU",
            "mem_total_mib": 24_576, "driver_version": "600.1",
            "cuda_driver_version": 13000,
        }]
        assert pinned["speculation_calibration_seed"] == 0
        assert pinned["speculation_policy_scope"] == "greedy"


def test_receipt_scope_binds_exact_depth_policy_and_task(tmp_path, monkeypatch):
    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setattr(
        quality, "validated_speculation_gate_receipt",
        lambda _path: _receipt(admitted_depth=1),
    )
    with pytest.raises(ValueError, match="policy/depth-mismatched"):
        _engine(
            tmp_path / "wrong-depth",
            card_driven_selection=True,
            speculation_depth=2,
            speculation_gate_receipt=str(receipt_path),
        )
    with pytest.raises(ValueError, match="policy/depth-mismatched"):
        task = ToyTask(step=2.0)
        settings = Settings(**{
            **SPECULATION_CALIBRATION_PROFILE_SETTINGS,
            "max_nodes": 3,
            "speculation_depth": 1,
            "speculation_gate_receipt": str(receipt_path),
        })

        def roles():
            return (
                ToyResearcher(
                    task.bounds, seed=task.seed, step=task.step,
                    calibration_concepts=True),
                ToyObjectiveDeveloper(noise=0.0, calibration_gpu_probe=True),
            )

        Engine(
            tmp_path / "wrong-task",
            task=task,
            researcher=roles()[0],
            developer=roles()[1],
            sandbox=SubprocessSandbox(),
            policy=GreedyTree(n_seeds=3, max_nodes=3),
            options=EngineOptions.from_settings(settings),
            role_factory=roles,
            _speculation_runtime_scope_sha256=speculation_runtime_scope_digest(
                settings.masked_snapshot()),
            card_driven_selection=True,
            speculation_depth=1,
            speculation_gate_receipt=str(receipt_path),
        )


@pytest.mark.parametrize("run_id,include_impl", [("", True), ("missing-impl", False)])
def test_positive_prefix_missing_identity_fails_before_any_write(
        tmp_path, monkeypatch, run_id, include_impl):
    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setattr(
        quality, "validated_speculation_gate_receipt", lambda _path: _receipt())
    run_dir = tmp_path / (run_id or "empty-run-id")
    engine = _engine(
        run_dir,
        card_driven_selection=True,
        speculation_depth=1,
        speculation_gate_receipt=str(receipt_path),
    )
    payload = {
        "run_id": run_id,
        "task_id": "toy_quadratic",
        "direction": "min",
        "card_driven_selection": True,
        "speculation_depth": 1,
        "speculation_gate_receipt_digest": _DIGEST_A,
        "speculation_policy_scope": "greedy",
    }
    if include_impl:
        payload["speculation_implementation_digest"] = _IMPLEMENTATION
    engine.store.append("run_started", payload)
    before = (run_dir / "events.jsonl").read_bytes()
    # The CLI guard must not translate an authorization refusal into run_finished(error).
    with pytest.raises(SpeculationAuthorizationError, match="exact validated"):
        _run_engine_guarded(engine)
    assert (run_dir / "events.jsonl").read_bytes() == before


@pytest.mark.parametrize(
    "marker",
    [
        {"speculation_depth": 1},
        {"speculation_gate_receipt_digest": _DIGEST_A},
        {"speculation_runtime_scope_sha256": _runtime_scope()},
        {"speculation_implementation_digest": _IMPLEMENTATION},
        {"speculation_policy_scope": "greedy"},
    ],
)
def test_any_speculation_marker_with_card_false_fails_before_mutation(
        tmp_path, marker):
    run_dir = tmp_path / next(iter(marker))
    engine = _engine(run_dir, card_driven_selection=False, speculation_depth=0)
    engine.store.append("run_started", {
        "run_id": run_dir.name,
        "task_id": "toy_quadratic",
        "direction": "min",
        "card_driven_selection": False,
        **marker,
    })
    before = (run_dir / "events.jsonl").read_bytes()
    with pytest.raises(RuntimeError, match="exact validated"):
        anyio.run(engine.run)
    assert (run_dir / "events.jsonl").read_bytes() == before


def test_public_receipt_keeps_explicit_operator_strategy_control(tmp_path, monkeypatch):
    monkeypatch.setattr(
        quality, "validated_speculation_gate_receipt", lambda _path: _receipt())
    engine = _engine(
        tmp_path / "strategy",
        card_driven_selection=True,
        speculation_depth=1,
        speculation_gate_receipt=str(tmp_path / "receipt.json"),
    )
    treatment = {"stance": "explore", "novelty_weight": 0.8, "coverage_weight": 0.2}
    engine._apply_strategy({
        "card_scoring": treatment,
        "_pinned": ["card_scoring"],
        "source": "operator",
    })
    assert engine._card_scoring == treatment


def test_calibration_bootstrap_blocks_strategy_and_budget_mutation(tmp_path, monkeypatch):
    engine = _calibration_engine(tmp_path / "immutable-calibration", monkeypatch, depth=1)
    before = getattr(engine, "_card_scoring", None)
    engine._apply_strategy({
        "card_scoring": {
            "stance": "explore", "novelty_weight": 0.8, "coverage_weight": 0.2,
        },
        "_pinned": ["card_scoring"],
        "source": "operator",
    })
    assert getattr(engine, "_card_scoring", None) == before

    engine.store.append("budget_extend", {"add_nodes": 1})
    with pytest.raises(RuntimeError, match="calibration forbids"):
        engine._apply_control_overrides(fold(engine.store.read_all()))


@pytest.mark.parametrize("event_type,data", [
    ("budget_extend", {"add_nodes": 1}),
    ("budget_extend", {"timeout": 99.0, "eval_parallel": 2}),
    ("set_strategy", {"strategy": {"policy": "mcts"}}),
])
def test_public_receipt_authorizes_audited_stage6_controls(
        tmp_path, monkeypatch, event_type, data):
    monkeypatch.setattr(
        quality, "validated_speculation_gate_receipt", lambda _path: _receipt())
    run_dir = tmp_path / f"control-{event_type}-{len(data)}"
    engine = _engine(
        run_dir,
        card_driven_selection=True,
        speculation_depth=1,
        speculation_gate_receipt=str(tmp_path / "receipt.json"),
    )
    engine.store.append("run_started", {
        "run_id": run_dir.name,
        "task_id": "toy_quadratic",
        "direction": "min",
        **engine._run_start_pinned_values(),
    })
    engine.store.append(event_type, data)
    state = fold(engine.store.read_all())
    # Receipt authority covers the calibrated launch profile. Explicit Stage-6 interventions stay
    # available and auditable; the quality evidence reader excludes these runs from recalibration.
    engine._require_pinned_speculation_receipt(state)
