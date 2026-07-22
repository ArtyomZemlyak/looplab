"""Synthetic acceptance and fail-closed coverage for the paired speculation quality receipt."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import orjson
import pytest

import looplab.search.speculation_quality as quality
from looplab.adapters.toytask import ToyTask
from looplab.agents.roles import (
    SPECULATION_CUDA_PROBE_CODE_PREFIX,
    SPECULATION_CUDA_PROBE_DEVICE_COUNT_METRIC,
    SPECULATION_CUDA_PROBE_STATIC_EXTRA_METRICS,
)
from looplab.core.models import (
    CARD_ACTION_DIGEST_V1_FIELDS,
    Idea,
    card_ownership_receipt,
    idea_proposal_ref,
)
from looplab.core.config import RUN_START_PINNED_FIELDS
from looplab.core.fitness import VERIFIER_SELECTION_CONTRACT
from looplab.engine.orchestrator import (
    SPECULATION_CALIBRATION_PROFILE_DIGEST,
    SPECULATION_CALIBRATION_PROFILE_SETTINGS,
    SPECULATION_POLICY_SCOPE,
)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.archive import DiversityArchive
from looplab.search.speculation_calibration import (
    SPECULATION_CALIBRATION_SEEDS,
    speculation_runtime_scope_digest,
)


_GPU = [{
    "index": 0,
    "uuid": "GPU-11111111-2222-3333-4444-555555555555",
    "pci_bus_id": "00000000:01:00.0",
    "name": "Synthetic GPU",
    "mem_total_mib": 24_576,
    "driver_version": "595.79",
    "cuda_driver_version": 13000,
}]
_GPU_PIN = [dict(_GPU[0])]
_OTHER_GPU = [{
    "index": 0,
    "uuid": "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "pci_bus_id": "00000000:02:00.0",
    "name": "Other Synthetic GPU",
    "mem_total_mib": 48_000,
    "driver_version": "595.80",
    "cuda_driver_version": 13000,
}]
_ENV = {"python": "test", "platform": "synthetic", "libs": {}}
_OTHER_ENV = {"python": "changed", "platform": "synthetic", "libs": {}}
_WORKSPACE = {}
_IMPL_A = lambda: "sha256:" + "a" * 64
_IMPL_B = lambda: "sha256:" + "b" * 64
_GPU_PROBE_CODE = SPECULATION_CUDA_PROBE_CODE_PREFIX + "print('synthetic objective')\n"
_GPU_PROBE_METRICS = {
    **dict(SPECULATION_CUDA_PROBE_STATIC_EXTRA_METRICS),
    SPECULATION_CUDA_PROBE_DEVICE_COUNT_METRIC: 1,
}


@pytest.fixture(autouse=True)
def _stable_scorer_gate(monkeypatch):
    calls: list[bool] = []

    def passed():
        calls.append(True)
        rows = [
            {"name": name, "passed": True, "expected": [], "actual": []}
            for name in quality.SCORER_FIDELITY_CASE_NAMES
        ]
        return {
            "schema": quality.SCORER_FIDELITY_SCHEMA,
            "passed": True,
            "cases": quality.SCORER_FIDELITY_CASE_COUNT,
            "mismatches": 0,
            "case_results": rows,
        }

    monkeypatch.setattr(quality, "scorer_fidelity_gate", passed)
    return calls


def _gate(pairs, **kwargs):
    kwargs.setdefault("gpu_inventory", _GPU)
    kwargs.setdefault("implementation_digest_fn", _IMPL_A)
    kwargs.setdefault("environment_fingerprint", _ENV)
    return quality.speculation_quality_gate(pairs, **kwargs)


def _write_receipt(path: Path, pairs, **kwargs):
    kwargs.setdefault("gpu_inventory", _GPU)
    kwargs.setdefault("implementation_digest_fn", _IMPL_A)
    kwargs.setdefault("environment_fingerprint", _ENV)
    return quality.write_speculation_gate_receipt(path, pairs, **kwargs)


def _validate(receipt, **kwargs) -> bool:
    kwargs.setdefault("gpu_inventory", _GPU)
    kwargs.setdefault("implementation_digest_fn", _IMPL_A)
    kwargs.setdefault("environment_fingerprint", _ENV)
    return quality.validate_speculation_gate_receipt(receipt, **kwargs)


def _validated(receipt, **kwargs):
    kwargs.setdefault("gpu_inventory", _GPU)
    kwargs.setdefault("implementation_digest_fn", _IMPL_A)
    kwargs.setdefault("environment_fingerprint", _ENV)
    return quality.validated_speculation_gate_receipt(receipt, **kwargs)


def _task_bytes(
    seed: int,
    *,
    task_id: str = "toy_quadratic",
    direction: str = "min",
    noise: float = 0.0,
    extra: dict | None = None,
) -> bytes:
    payload = {
        "bounds": {"x": [-10.0, 10.0], "y": [-10.0, 10.0]},
        "direction": direction,
        "goal": "minimize (x-3)^2 + (y+1)^2",
        "id": task_id,
        "kind": "quadratic",
        "noise": noise,
        "seed": seed,
        "step": 1.0,
    }
    payload.update(extra or {})
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _idea(
    card_id: str | None,
    concept: str | None,
    semantic_variant: int,
    *,
    operator: str = "draft",
) -> dict:
    statement = f"test {concept or 'empty-membership'}"
    return Idea(
        operator=operator,
        # Stand in for the seed-dependent candidate sampled by the real ToyResearcher.  Replicate
        # seeds must produce independent semantic trajectories; the seed itself is never hashed.
        params={"synthetic_sample": float((semantic_variant * 48_271) % 997)},
        rationale=statement,
        hypothesis=statement,
        card_id=card_id,
        concept_mode="full",
        concepts=[] if concept is None else [concept],
        footprint={"gpus": 1},
    ).model_dump(mode="json")


def _append_card_added(
    store: EventStore,
    node_id: int,
    idea_payload: dict,
    *,
    parent_ids: tuple[int, ...] = (),
) -> None:
    idea = Idea.model_validate(idea_payload)
    card_id = idea.card_id
    assert isinstance(card_id, str) and card_id
    statement = (idea.hypothesis or "").strip() or (idea.rationale or "").strip() \
        or f"{idea.operator} experiment"
    prefix = fold(store.read_all())
    score_id = prefix.best_node_id
    score_node = prefix.nodes.get(score_id) if score_id is not None else None
    assert score_id is None or score_node is not None
    parents = list(parent_ids)
    parent_generations = {str(parent): prefix.nodes[parent].attempt for parent in parents}
    action = {
        "operator": idea.operator,
        "params": dict(idea.params),
        "space": {key: list(values) for key, values in idea.space.items()},
        "eval_profile": idea.eval_profile,
        "eval_timeout": idea.eval_timeout,
        "parent_id": parents[0] if parents else None,
        "parent_ids": parents,
        "parent_generations": parent_generations,
        "scored_against": score_id,
        "scored_against_generation": score_node.attempt if score_node is not None else None,
        "scored_against_empty": score_id is None,
        "footprint": idea.footprint,
    }
    store.append("card_added", {
        "id": card_id,
        "statement": statement,
        "source": "engine" if idea.operator == "merge" else "researcher",
        "at_node": node_id,
        "rationale": (idea.rationale or "")[:400],
        "idea": {
            key: action[key]
            for key in ("operator", "params", "space", "eval_profile", "eval_timeout")
        },
        "parent_id": action["parent_id"],
        "parent_ids": action["parent_ids"],
        "parent_generations": action["parent_generations"],
        "scored_against": action["scored_against"],
        "scored_against_generation": action["scored_against_generation"],
        "scored_against_empty": action["scored_against_empty"],
        "footprint": idea.footprint,
        "steering_context": [],
        "ownership_receipt": card_ownership_receipt(card_id, statement, action),
        # Match the real two-phase writer: the staged Researcher proposal is rationale-only, while
        # Card materialization below adds the same text as the durable hypothesis join.
        "proposal_ref": idea_proposal_ref(
            idea.model_copy(deep=True, update={"hypothesis": None})
        ),
    })


def _append_card_enriched(store: EventStore, node_id: int, idea_payload: dict) -> None:
    idea = Idea.model_validate(idea_payload)
    assert isinstance(idea.card_id, str) and idea.card_id
    assert isinstance(idea.footprint, dict)
    store.append("card_enriched", {
        "id": idea.card_id,
        "node_id": node_id,
        "generation": 0,
        "proposal_ref": idea_proposal_ref(idea),
        "footprint": {
            **idea.footprint,
            "proposed_by": "researcher",
            "finalized_by": "developer",
        },
    })


def _created(
    store: EventStore,
    node_id: int,
    *,
    metric: float | None,
    concept: str | None,
    card_id: str | None = None,
    speculative: bool = False,
    violations: list[str] | None = None,
    semantic_variant: int = 0,
    probe_code: str = _GPU_PROBE_CODE,
    probe_metrics: dict | None = None,
    register_card: bool = True,
    enrich_card: bool = True,
    operator: str = "draft",
    parent_ids: tuple[int, ...] = (),
) -> None:
    effective_card_id = card_id or f"card-{node_id}"
    idea_payload = _idea(
        effective_card_id, concept, semantic_variant, operator=operator,
    )
    parents = list(parent_ids)
    data = {
        "node_id": node_id,
        "operator": operator,
        "parent_ids": parents,
        "idea": idea_payload,
        "code": probe_code,
        "footprint_finalized": True,
    }
    building = {
        "node_id": node_id,
        "operator": operator,
        "parent_ids": parents,
        "card_id": effective_card_id,
    }
    if parents:
        data["parent_generations"] = {str(parent): 0 for parent in parents}
    if speculative:
        data.update({"speculative": True, "card_build_generation": 0})
        building.update({"speculative": True, "card_build_generation": 0})
    if register_card:
        _append_card_added(store, node_id, idea_payload, parent_ids=parent_ids)
    store.append("node_building", building)
    store.append("node_created", data)
    if enrich_card:
        _append_card_enriched(store, node_id, idea_payload)
    if metric is not None:
        store.append("node_evaluated", {
            "node_id": node_id,
            "generation": 0,
            "metric": metric,
            "eval_seconds": 1.0,
            "extra_metrics": dict(
                _GPU_PROBE_METRICS if probe_metrics is None else probe_metrics),
            "violations": violations or [],
        })


def _make_run(
    path: Path,
    *,
    treatment: bool,
    seed: int,
    best_metric: float = 1.0,
    first_metric: float = 0.0,
    first_violations: list[str] | None = None,
    first_concept: str | None = "axis/a",
    second_concept: str | None = "axis/b",
    treatment_depth: int = 1,
    freshness_drop: bool = False,
    generic_unlinked_drop: bool = False,
    precommit_outcomes: tuple[str, ...] = (),
    open_queue: bool = False,
    pending_second: bool = False,
    fail_second: bool = False,
    max_nodes: int = 2,
    finish: bool = True,
    finish_reason: str | None = None,
    modern_finish: bool = True,
    finish_ack: bool = True,
    finish_complete: bool = True,
    reset_re_evaluate_speculative: bool = False,
    run_id: str | None = None,
    run_task_id: str = "toy_quadratic",
    run_direction: str = "min",
    task_snapshot_id: str = "toy_quadratic",
    task_direction: str = "min",
    task_noise: float = 0.0,
    task_bytes: bytes | None = None,
    env: dict | None = None,
    workspace: dict | None = None,
    dirty_inputs: list[dict] | None = None,
    data_provenance: dict | None = None,
    gpu_pin: list[dict] | None = None,
    omit_pins: tuple[str, ...] = (),
    pin_overrides: dict | None = None,
    drift_events: tuple[str, ...] = (),
    pre_finish_events: tuple[tuple[str, dict], ...] = (),
    extra_config: dict | None = None,
    semantic_variant: int | None = None,
    probe_code: str = _GPU_PROBE_CODE,
    probe_metrics: dict | None = None,
    greedy_fourth_parent: int | None = None,
    greedy_merge_seventh: bool = False,
) -> Path:
    path.mkdir(parents=True)
    depth = treatment_depth if treatment else 0
    config = dict(SPECULATION_CALIBRATION_PROFILE_SETTINGS)
    config.update({
        "speculation_depth": depth,
        "max_nodes": max_nodes,
        "speculation_gate_receipt": None,
    })
    config.update(extra_config or {})
    (path / "config.snapshot.json").write_text(
        json.dumps(config, sort_keys=True), encoding="utf-8")
    task_source = task_bytes if task_bytes is not None else _task_bytes(
        seed, task_id=task_snapshot_id, direction=task_direction, noise=task_noise)
    (path / "task.snapshot.json").write_bytes(task_source)

    task_model = ToyTask.model_validate(json.loads(task_source))
    task_payload = task_model.model_dump(mode="json")
    config_hash = hashlib.sha256(orjson.dumps(task_payload)).hexdigest()[:12]
    manifest_config_hash = hashlib.sha256(orjson.dumps(
        task_payload, option=orjson.OPT_SORT_KEYS,
    )).hexdigest()[:12]
    workspace_value = _WORKSPACE if workspace is None else workspace
    setup_manifest = hashlib.sha256(orjson.dumps(
        {"config": manifest_config_hash, "workspace": workspace_value, "provenance": {}},
        option=orjson.OPT_SORT_KEYS,
    )).hexdigest()[:16]

    started = {
        "run_id": path.name if run_id is None else run_id,
        "task_id": run_task_id,
        "goal": task_model.goal,
        "direction": run_direction,
        "config_hash": config_hash,
        "speculation_implementation_digest": _IMPL_A(),
        "speculation_calibration_profile_digest": SPECULATION_CALIBRATION_PROFILE_DIGEST,
        "speculation_calibration_gpu_inventory": _GPU_PIN if gpu_pin is None else gpu_pin,
        "speculation_calibration_seed": seed,
        "speculation_policy_scope": SPECULATION_POLICY_SCOPE,
        "speculation_runtime_scope_sha256": speculation_runtime_scope_digest(config),
        "env": _ENV if env is None else env,
        "workspace": workspace_value,
        "dirty_inputs": [] if dirty_inputs is None else dirty_inputs,
        "trust_gate": config["trust_gate"],
        "select_verifier_contract": VERIFIER_SELECTION_CONTRACT,
        **{field: config[field] for field in RUN_START_PINNED_FIELDS},
    }
    started.update(pin_overrides or {})
    for field in omit_pins:
        started.pop(field, None)

    store = EventStore(path / "events.jsonl")
    store.append("setup_started", {
        "phase": "task+data", "repo": False, "goal": task_model.goal,
    })
    store.append("setup_step", {
        "step": "workspace fingerprint",
        "sources": list(workspace_value) if isinstance(workspace_value, dict) else [],
    })
    store.append("run_started", started)
    store.append("setup_step", {"step": "wrote AGENTS.md"})
    store.append("setup_finished", {"seconds": 1.0, "manifest": setup_manifest})
    if data_provenance is not None:
        store.append("data_provenance", data_provenance)
    variant = seed if semantic_variant is None else semantic_variant
    if not treatment:
        _created(
            store,
            0,
            metric=first_metric,
            concept=first_concept,
            violations=first_violations,
            semantic_variant=variant,
            probe_code=probe_code,
            probe_metrics=probe_metrics,
        )
        _created(
            store,
            1,
            metric=None if pending_second or fail_second else best_metric,
            concept=second_concept,
            semantic_variant=variant,
            probe_code=probe_code,
            probe_metrics=probe_metrics,
        )
        if fail_second:
            store.append("node_failed", {
                "node_id": 1,
                "generation": 0,
                "reason": "crash",
                "error": "synthetic failure",
                "eval_seconds": 1.0,
            })
        if greedy_fourth_parent is not None or greedy_merge_seventh:
            _created(
                store,
                2,
                metric=best_metric + 1.0,
                concept="seed/third",
                semantic_variant=variant,
                probe_code=probe_code,
                probe_metrics=probe_metrics,
            )
            fourth_parent = 0 if greedy_merge_seventh else greedy_fourth_parent
            assert fourth_parent is not None
            _created(
                store,
                3,
                metric=-1.0,
                concept="operator/improve-1",
                semantic_variant=variant,
                probe_code=probe_code,
                probe_metrics=probe_metrics,
                operator="improve",
                parent_ids=(fourth_parent,),
            )
        if greedy_merge_seventh:
            _created(
                store,
                4,
                metric=-2.0,
                concept="operator/improve-2",
                semantic_variant=variant,
                probe_code=probe_code,
                probe_metrics=probe_metrics,
                operator="improve",
                parent_ids=(3,),
            )
            _created(
                store,
                5,
                metric=-3.0,
                concept="operator/improve-3",
                semantic_variant=variant,
                probe_code=probe_code,
                probe_metrics=probe_metrics,
                operator="improve",
                parent_ids=(4,),
            )
            _created(
                store,
                6,
                metric=-4.0,
                concept="operator/merge",
                semantic_variant=variant,
                probe_code=probe_code,
                probe_metrics=probe_metrics,
                operator="merge",
                parent_ids=(5, 4),
            )
    else:
        first_card_id = f"first-{path.name}"
        first_idea = _idea(first_card_id, first_concept, variant)
        _append_card_added(store, 0, first_idea)
        store.append("card_build_requested", {
            "card_id": first_card_id, "generation": 0,
        })
        _created(
            store,
            0,
            metric=None,
            concept=first_concept,
            card_id=first_card_id,
            speculative=True,
            semantic_variant=variant,
            probe_code=probe_code,
            probe_metrics=probe_metrics,
            register_card=False,
            enrich_card=False,
        )
        store.append("card_build_done", {
            "card_id": first_card_id,
            "generation": 0,
            "node_id": 0,
            "speculative": True,
        })
        _append_card_enriched(store, 0, first_idea)
        first_evaluated = False
        if first_metric is not None and not precommit_outcomes:
            store.append("node_evaluated", {
                "node_id": 0,
                "generation": 0,
                "metric": first_metric,
                "eval_seconds": 1.0,
                "extra_metrics": dict(
                    _GPU_PROBE_METRICS if probe_metrics is None else probe_metrics),
                "violations": first_violations or [],
            })
            first_evaluated = True
        for index, outcome in enumerate(precommit_outcomes):
            card_id = f"precommit-{index}-{path.name}"
            _append_card_added(
                store,
                1,
                _idea(card_id, second_concept, variant * 1_000 + index + 1),
            )
            store.append("card_build_requested", {"card_id": card_id, "generation": 0})
            if outcome == "stale" and not first_evaluated and first_metric is not None:
                # Model the real pre-commit race: this Card was elected while node 0 was still in
                # flight, then that evaluation moved the score/parent authority before the producer
                # could commit it.
                store.append("node_evaluated", {
                    "node_id": 0,
                    "generation": 0,
                    "metric": first_metric,
                    "eval_seconds": 1.0,
                    "extra_metrics": dict(
                        _GPU_PROBE_METRICS if probe_metrics is None else probe_metrics),
                    "violations": first_violations or [],
                })
                first_evaluated = True
            store.append("card_build_done", {
                "card_id": card_id,
                "generation": 0,
                "skipped": outcome,
            })
        if first_metric is not None and not first_evaluated:
            store.append("node_evaluated", {
                "node_id": 0,
                "generation": 0,
                "metric": first_metric,
                "eval_seconds": 1.0,
                "extra_metrics": dict(
                    _GPU_PROBE_METRICS if probe_metrics is None else probe_metrics),
                "violations": first_violations or [],
            })
        card_id = f"card-{path.name}"
        second_idea = _idea(card_id, second_concept, variant)
        _append_card_added(store, 1, second_idea)
        store.append("card_build_requested", {"card_id": card_id, "generation": 0})
        _created(
            store,
            1,
            metric=None,
            concept=second_concept,
            card_id=card_id,
            speculative=True,
            semantic_variant=variant,
            probe_code=probe_code,
            probe_metrics=probe_metrics,
            register_card=False,
            enrich_card=False,
        )
        if not open_queue:
            store.append("card_build_done", {
                "card_id": card_id,
                "generation": 0,
                "node_id": 1,
                "speculative": True,
            })
            _append_card_enriched(store, 1, second_idea)
        if pending_second:
            pass
        elif fail_second:
            store.append("node_failed", {
                "node_id": 1,
                "generation": 0,
                "reason": "crash",
                "error": "synthetic failure",
                "eval_seconds": 1.0,
            })
        elif not freshness_drop:
            store.append("node_evaluated", {
                "node_id": 1,
                "generation": 0,
                "metric": best_metric,
                "eval_seconds": 1.0,
                "extra_metrics": dict(
                    _GPU_PROBE_METRICS if probe_metrics is None else probe_metrics),
            })
            if reset_re_evaluate_speculative:
                store.append("node_reset", {
                    "node_id": 1,
                    "generation": 0,
                    "from_stage": "eval",
                })
                store.append("node_evaluated", {
                    "node_id": 1,
                    "generation": 1,
                    "metric": best_metric,
                    "eval_seconds": 1.0,
                    "extra_metrics": dict(
                        _GPU_PROBE_METRICS if probe_metrics is None else probe_metrics),
                })
        else:
            store.append("node_failed", {
                "node_id": 1,
                "generation": 0,
                "reason": "superseded",
                "error": quality.CARD_FRESHNESS_SUPERSEDED_ERROR,
                "eval_seconds": 0.0,
            })
    if generic_unlinked_drop:
        # Same marker for no exact folded link: a raw ignored terminal must invalidate evidence.
        store.append("node_failed", {
            "node_id": 999,
            "generation": 0,
            "reason": "superseded",
            "error": quality.CARD_FRESHNESS_SUPERSEDED_ERROR,
            "eval_seconds": 0.0,
        })
    for event_type in drift_events:
        store.append(event_type, {})
    for event_type, event_data in pre_finish_events:
        store.append(event_type, dict(event_data))
    if finish:
        finish_data = {} if finish_reason is None else {"reason": finish_reason}
        if not modern_finish:
            store.append("run_finished", finish_data)
        else:
            before = store.read_all()[-1].seq
            scope = "finalize:" + f"{seed:032x}"
            begun = store.append("finalize_step", {
                "scope": scope,
                "step": "begun",
                "finish_data": finish_data,
                "finish_report_planned": False,
                "after_seq": before,
            })
            finished = store.append("run_finished", {
                **finish_data,
                "after_seq": begun.seq,
                "finalization_required": True,
                "finalize_scope": scope,
            })
            terminal_state = fold(store.read_all())
            store.append("budget", {
                "elapsed_s": 1.0,
                "eval_s": round(terminal_state.total_eval_seconds, 3),
                "nodes": len(terminal_state.nodes),
                "finalize_scope": scope,
                "finish_seq": finished.seq,
            })
            store.append("finalize_step", {"scope": scope, "step": "budget"})
            store.append("diversity_archive", {
                **DiversityArchive(1.0).summary(terminal_state),
                "finalize_scope": scope,
                "finish_seq": finished.seq,
            })
            store.append("finalize_step", {"scope": scope, "step": "diversity"})
            store.append("finalize_step", {"scope": scope, "step": "case"})
            store.append("finalize_step", {
                "scope": scope,
                "step": "reflection_begun",
                "outcome": "disabled",
            })
            store.append("finalize_step", {
                "scope": scope,
                "step": "reflection",
                "outcome": "disabled",
            })
            store.append("finalize_step", {"scope": scope, "step": "llm_cost"})
            if finish_ack:
                store.append("finalization_finished", {"finish_seq": finished.seq})
            if finish_complete:
                store.append("finalize_step", {"scope": scope, "step": "complete"})
    return path


def _pairs(
    root: Path,
    *,
    seeds: tuple[int, int, int] = SPECULATION_CALIBRATION_SEEDS,
    treatment_depths: tuple[int, int, int] = (1, 1, 1),
    baseline_kwargs: dict | None = None,
    **treatment_kwargs,
) -> list[tuple[Path, Path]]:
    rows = []
    for index, seed in enumerate(seeds):
        baseline = _make_run(
            root / f"baseline-{index}",
            treatment=False,
            seed=seed,
            **(baseline_kwargs or {}),
        )
        treatment = _make_run(
            root / f"treatment-{index}",
            treatment=True,
            seed=seed,
            treatment_depth=treatment_depths[index],
            **treatment_kwargs,
        )
        rows.append((baseline, treatment))
    return rows


def _pair_errors(report: dict) -> list[str]:
    return [error for pair in report["pairs"] for error in pair["errors"]]


def _rewrite_first_event_data(
    run_dir: Path,
    event_type: str,
    mutate,
    where=lambda data: True,
) -> None:
    path = run_dir / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    row = next(
        row for row in rows
        if row.get("type") == event_type and where(row.get("data", {}))
    )
    mutate(row["data"])
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_green_gate_writes_and_revalidates_canonical_receipt(tmp_path, _stable_scorer_gate):
    pairs = _pairs(tmp_path / "runs")
    report = _gate(pairs)

    assert report["passed"] is True
    assert report["thresholds"] == quality.SPECULATION_QUALITY_THRESHOLDS
    assert report["policy_scope"] == "greedy"
    assert report["workload_scope"] == "quadratic_toy"
    assert report["calibration_seeds"] == [0, 1, 2]
    assert report["task_profile_sha256"] == quality.speculation_task_profile_digest(
        json.loads(_task_bytes(0)))
    assert report["admitted_depth"] == 1
    assert report["admitted_max_nodes"] == 2
    assert report["runtime_scope_sha256"] == speculation_runtime_scope_digest(
        json.loads((pairs[0][0] / "config.snapshot.json").read_text(encoding="utf-8"))
    )
    assert report["calibration_profile_digest"] == SPECULATION_CALIBRATION_PROFILE_DIGEST
    assert report["aggregates"] == {
        "pair_count": 3,
        "valid_metric_pairs": 3,
        "mean_normalized_regret": 0.0,
        "max_pair_normalized_regret": 0.0,
        "mean_hit_rate": 1.0,
        "max_pair_divergence_rate": 0.0,
        "min_pair_coverage_ratio": 1.0,
    }
    assert _stable_scorer_gate
    assert tuple(
        row["name"] for row in report["scorer_fidelity"]["case_results"]
    ) == quality.SCORER_FIDELITY_CASE_NAMES

    receipt_path = tmp_path / "speculation-quality.receipt.json"
    receipt = _write_receipt(receipt_path, pairs)
    assert receipt_path.read_bytes().endswith(b"\n")
    assert _validate(receipt)
    assert _validate(receipt_path)
    validated = _validated(receipt, gpu_inventory=_GPU_PIN)
    assert validated is not None and validated["self_digest"] == receipt["self_digest"]
    assert validated["gpu_inventory"] == _GPU_PIN
    assert not _validate(receipt, gpu_inventory=[{**_GPU[0], "mem_free_mib": 1}])


def test_real_scorer_receipt_round_trip_normalizes_integer_audit_keys(tmp_path, monkeypatch):
    from looplab.search.scorer_fidelity import scorer_fidelity_gate

    monkeypatch.setattr(quality, "scorer_fidelity_gate", scorer_fidelity_gate)
    pairs = _pairs(tmp_path / "real-scorer-runs")
    receipt_path = tmp_path / "real-scorer-receipt.json"
    receipt = _write_receipt(receipt_path, pairs)

    assert receipt["scorer_fidelity"]["cases"] == 15
    assert tuple(
        row["name"] for row in receipt["scorer_fidelity"]["case_results"]
    ) == quality.SCORER_FIDELITY_CASE_NAMES
    assert receipt["scorer_fidelity"]["mismatches"] == 0
    assert _validate(receipt_path)


def test_analysis_counts_exact_linked_zero_cost_freshness_marker(tmp_path):
    run = _make_run(
        tmp_path / "drop",
        treatment=True,
        seed=0,
        freshness_drop=True,
    )

    metrics = quality.analyze_speculation_run(run)["metrics"]
    assert metrics["accepted_requests"] == 2
    assert metrics["closed_requests"] == 2
    assert metrics["committed_exact_links"] == 2
    assert metrics["speculative_evaluated"] == 1
    assert metrics["freshness_dropped"] == 1
    assert metrics["hit_rate"] == pytest.approx(1 / 2)
    assert metrics["divergence_rate"] == pytest.approx(1 / 2)


@pytest.mark.parametrize(
    ("run_kwargs", "error"),
    [
        ({"probe_code": "print('no source-owned prefix')\n"}, "exact CUDA proof prefix"),
        ({"probe_metrics": {**_GPU_PROBE_METRICS, "untrusted": 1}}, "metric schema"),
        ({"probe_metrics": {**_GPU_PROBE_METRICS,
                            SPECULATION_CUDA_PROBE_DEVICE_COUNT_METRIC: 2}},
         "device count differs"),
        ({"probe_metrics": {**_GPU_PROBE_METRICS, "alloc_bytes": 1}},
         "invalid CUDA proof metric alloc_bytes"),
    ],
)
def test_analysis_requires_exact_source_owned_cuda_allocation_proof(
    tmp_path, run_kwargs, error,
):
    run = _make_run(
        tmp_path / error.replace(" ", "-"),
        treatment=False,
        seed=0,
        **run_kwargs,
    )
    with pytest.raises(ValueError, match=error):
        quality.analyze_speculation_run(run)


def test_ignored_unlinked_terminal_row_is_not_accepted_as_evidence(tmp_path):
    run = _make_run(
        tmp_path / "unlinked-drop",
        treatment=True,
        seed=0,
        generic_unlinked_drop=True,
    )
    with pytest.raises(ValueError, match="ignored or cross-generation node lifecycle row"):
        quality.analyze_speculation_run(run)


def test_stale_precommit_is_kept_in_the_request_denominator(tmp_path):
    run = _make_run(
        tmp_path / "denominator",
        treatment=True,
        seed=0,
        precommit_outcomes=("stale",),
    )
    metrics = quality.analyze_speculation_run(run)["metrics"]
    assert metrics["accepted_requests"] == 3
    assert metrics["closed_requests"] == 3
    assert metrics["precommit_stale"] == 1
    assert metrics["producer_failed"] == 0
    assert metrics["committed_exact_links"] == 2
    assert metrics["speculative_evaluated"] == 2
    assert metrics["hit_rate"] == pytest.approx(2 / 3)
    assert metrics["divergence_rate"] == pytest.approx(1 / 3)

    report = _gate(_pairs(
        tmp_path / "gate-denominator",
        precommit_outcomes=("stale",),
    ))
    assert report["passed"] is False
    assert report["aggregates"]["mean_hit_rate"] == pytest.approx(2 / 3)


def test_card_producer_failure_invalidates_the_lane(tmp_path):
    run = _make_run(
        tmp_path / "producer-failed",
        treatment=True,
        seed=0,
        precommit_outcomes=("producer_failed",),
    )
    with pytest.raises(ValueError, match="Card producer failure"):
        quality.analyze_speculation_run(run)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"treatment": True, "open_queue": True}, "open or inconsistent Card-build queue"),
        ({"treatment": False, "pending_second": True}, "not quiescent"),
        ({"treatment": False, "fail_second": True},
         "outside the clean calibration protocol"),
        ({"treatment": False, "finish": False}, "must be terminal"),
        ({"treatment": False, "finish_reason": "error"}, "non-qualifying terminal reason"),
        ({"treatment": False, "max_nodes": 3}, "complete physical node budget"),
    ],
)
def test_analysis_rejects_open_pending_error_or_short_evidence(tmp_path, kwargs, error):
    run = _make_run(tmp_path / error.replace(" ", "-"), seed=0, **kwargs)
    with pytest.raises(ValueError, match=error):
        quality.analyze_speculation_run(run)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"modern_finish": False}, "requires modern finalization"),
        ({"finish_ack": False}, "incomplete modern finalization"),
        ({"finish_complete": False}, "complete un-abandoned finalization scope"),
    ],
)
def test_analysis_requires_complete_modern_finalization(tmp_path, kwargs, error):
    run = _make_run(
        tmp_path / f"modern-{error.replace(' ', '-')}",
        treatment=False,
        seed=0,
        **kwargs,
    )
    with pytest.raises(ValueError, match=error):
        quality.analyze_speculation_run(run)


def test_additional_ignored_run_finished_row_fails_closed(tmp_path):
    run = _make_run(tmp_path / "duplicate-finish", treatment=False, seed=0)
    EventStore(run / "events.jsonl").append(
        "run_finished", {"after_seq": -10, "reason": "budget"})
    with pytest.raises(ValueError, match="exactly one raw accepted run_finished"):
        quality.analyze_speculation_run(run)


@pytest.mark.parametrize(
    "event_type",
    ["pause", "run_abort", "resume", "run_reopened", "budget_extend", "node_tombstoned"],
)
def test_calibration_lifecycle_controls_fail_closed_even_when_fold_ignores_them(
    tmp_path, event_type,
):
    run = _make_run(
        tmp_path / event_type,
        treatment=False,
        seed=0,
        drift_events=(event_type,),
    )
    with pytest.raises(ValueError, match=f"forbidden calibration lifecycle event: {event_type}"):
        quality.analyze_speculation_run(run)


@pytest.mark.parametrize(
    "event_type",
    ["set_strategy", "hint", "inject_node", "force_ablate", "card_edited"],
)
def test_every_nonprotocol_control_event_fails_closed(tmp_path, event_type):
    run = _make_run(
        tmp_path / f"control-{event_type}",
        treatment=False,
        seed=0,
        drift_events=(event_type,),
    )
    with pytest.raises(ValueError, match="outside the clean calibration protocol"):
        quality.analyze_speculation_run(run)


def test_calibration_rejects_non_footprint_card_enrichment(tmp_path):
    run = _make_run(
        tmp_path / "foreign-enrichment",
        treatment=False,
        seed=0,
        drift_events=("card_enriched",),
    )
    with pytest.raises(ValueError, match="exact footprint receipt"):
        quality.analyze_speculation_run(run)


def test_staged_and_materialized_proposal_refs_are_independently_exact(tmp_path):
    valid = _make_run(tmp_path / "two-phase-proposal-valid", treatment=True, seed=0)
    events = EventStore(valid / "events.jsonl").read_all()
    staged = next(event for event in events if event.type == "card_added")
    enriched = next(event for event in events if event.type == "card_enriched")

    assert staged.data["proposal_ref"] != enriched.data["proposal_ref"]
    quality.analyze_speculation_run(valid)

    forged = _make_run(tmp_path / "two-phase-proposal-forged", treatment=True, seed=0)
    _rewrite_first_event_data(
        forged,
        "card_added",
        lambda data: data["proposal_ref"].__setitem__("digest", "idea:v1:" + "0" * 64),
    )
    with pytest.raises(ValueError, match="materialized node action"):
        quality.analyze_speculation_run(forged)


def test_card_score_authority_is_bound_to_its_immediate_event_prefix(tmp_path):
    run = _make_run(tmp_path / "forged-score-prefix", treatment=False, seed=0)

    def forge_empty_score(data):
        data["scored_against"] = None
        data["scored_against_generation"] = None
        data["scored_against_empty"] = True
        action = {
            field: data["idea"][field] if field in data["idea"] else data[field]
            for field in CARD_ACTION_DIGEST_V1_FIELDS
        }
        data["ownership_receipt"] = card_ownership_receipt(
            data["id"], data["statement"], action,
        )

    _rewrite_first_event_data(
        run,
        "card_added",
        forge_empty_score,
        where=lambda data: data.get("id") == "card-1",
    )
    with pytest.raises(ValueError, match="immediate event prefix"):
        quality.analyze_speculation_run(run)


def test_merge_card_uses_engine_source_and_rejects_researcher_forgery(tmp_path):
    valid = _make_run(
        tmp_path / "merge-source-valid",
        treatment=False,
        seed=0,
        max_nodes=7,
        greedy_merge_seventh=True,
    )
    quality.analyze_speculation_run(valid)
    merge_card = fold(EventStore(valid / "events.jsonl").read_all()).cards["card-6"]
    assert merge_card.operator == "merge" and merge_card.source == "engine"

    forged = _make_run(
        tmp_path / "merge-source-forged",
        treatment=False,
        seed=0,
        max_nodes=7,
        greedy_merge_seventh=True,
    )
    _rewrite_first_event_data(
        forged,
        "card_added",
        lambda data: data.__setitem__("source", "researcher"),
        where=lambda data: data.get("id") == "card-6",
    )
    with pytest.raises(ValueError, match="ownership/proposal receipt is invalid"):
        quality.analyze_speculation_run(forged)


def test_calibration_rejects_self_consistent_improve_from_non_best_parent(tmp_path):
    run = _make_run(
        tmp_path / "non-best-greedy-parent",
        treatment=False,
        seed=0,
        max_nodes=4,
        # Node 0 is the unique minimum.  Every Card/Node/receipt below consistently names node 1,
        # so only immediate-prefix Greedy recomputation can detect the authority forgery.
        greedy_fourth_parent=1,
    )

    with pytest.raises(ValueError, match="canonical Greedy"):
        quality.analyze_speculation_run(run)


@pytest.mark.parametrize(
    ("event_type", "field", "value", "error"),
    [
        ("budget", "eval_s", 999.0, "budget finalization receipt differs"),
        ("diversity_archive", "niches", 999, "diversity finalization receipt differs"),
    ],
)
def test_terminal_effect_receipts_must_match_folded_state(
    tmp_path, event_type, field, value, error,
):
    run = _make_run(tmp_path / f"terminal-{event_type}", treatment=False, seed=0)
    _rewrite_first_event_data(run, event_type, lambda data: data.__setitem__(field, value))
    with pytest.raises(ValueError, match=error):
        quality.analyze_speculation_run(run)


def test_terminal_complete_must_be_the_last_event(tmp_path):
    run = _make_run(tmp_path / "post-complete", treatment=False, seed=0)
    EventStore(run / "events.jsonl").append("policy_decision", {"reason": "late"})
    with pytest.raises(ValueError, match="exact terminal finalization suffix"):
        quality.analyze_speculation_run(run)


def test_node_created_rejects_even_bool_attempt_alias(tmp_path):
    run = _make_run(tmp_path / "created-bool-generation", treatment=False, seed=0)
    _rewrite_first_event_data(
        run,
        "node_created",
        lambda data: data.__setitem__("generation", False),
    )
    with pytest.raises(ValueError, match="attempt-zero writer schema"):
        quality.analyze_speculation_run(run)


def test_reset_then_re_evaluated_speculative_node_is_not_a_hit(tmp_path):
    run = _make_run(
        tmp_path / "reset-hit",
        treatment=True,
        seed=0,
        reset_re_evaluate_speculative=True,
    )
    with pytest.raises(ValueError, match="forbidden calibration lifecycle event: node_reset"):
        quality.analyze_speculation_run(run)


def test_fail_closed_on_pair_shape_task_bytes_config_and_gpu(tmp_path):
    pairs = _pairs(tmp_path / "runs")
    assert not _gate(pairs[:2])["passed"]
    assert not _gate([*pairs, pairs[0]])["passed"]
    assert not _gate(pairs, gpu_inventory=[])["passed"]
    assert not _gate(pairs, gpu_inventory=[{**_GPU[0], "mem_free_mib": 1}])["passed"]
    assert not _gate(
        pairs,
        gpu_inventory=[{key: value for key, value in _GPU[0].items()
                        if key != "pci_bus_id"}],
    )["passed"]

    task_path = pairs[0][1] / "task.snapshot.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task_path.write_text(json.dumps(task, indent=2), encoding="utf-8")
    assert not _gate(pairs)["passed"]


def test_analysis_rejects_noncanonical_seed_placement_alias_receipt_and_scope_pin(tmp_path):
    noncanonical_seed = _make_run(
        tmp_path / "seed-outside-source-set", treatment=False, seed=3)
    with pytest.raises(ValueError, match="seed must be one of"):
        quality.analyze_speculation_run(noncanonical_seed)

    placement = _make_run(
        tmp_path / "placement-alias",
        treatment=False,
        seed=0,
        extra_config={"out": "synthetic"},
    )
    with pytest.raises(ValueError, match="config fields differ"):
        quality.analyze_speculation_run(placement)

    receipt = _make_run(
        tmp_path / "receipt-in-calibration",
        treatment=False,
        seed=0,
        extra_config={"speculation_gate_receipt": "borrowed.json"},
    )
    with pytest.raises(ValueError, match="must be null"):
        quality.analyze_speculation_run(receipt)

    wrong_scope = _make_run(
        tmp_path / "wrong-runtime-scope",
        treatment=False,
        seed=0,
        pin_overrides={"speculation_runtime_scope_sha256": _IMPL_B()},
    )
    with pytest.raises(ValueError, match="runtime scope pin"):
        quality.analyze_speculation_run(wrong_scope)

    borrowed_authority = _make_run(
        tmp_path / "borrowed-public-authority",
        treatment=False,
        seed=0,
        pin_overrides={"speculation_gate_receipt_digest": _IMPL_B()},
    )
    with pytest.raises(ValueError, match="public receipt authority"):
        quality.analyze_speculation_run(borrowed_authority)

    pairs = _pairs(tmp_path / "config-runs")
    config_path = pairs[0][1] / "config.snapshot.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["unpaired-behavior"] = True
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert not _gate(pairs)["passed"]


@pytest.mark.parametrize(
    "treatment_kwargs",
    [
        {"first_metric": 0.20, "best_metric": 0.80},  # normalized regret 0.20
        {"freshness_drop": True},              # hit=0 and divergence=1
        {"second_concept": "axis/a"},          # final trusted coverage 1/2
    ],
)
def test_fixed_quality_thresholds_cannot_be_loosened(tmp_path, treatment_kwargs):
    report = _gate(_pairs(tmp_path / "runs", **treatment_kwargs))
    assert report["passed"] is False
    assert report["thresholds"] == quality.SPECULATION_QUALITY_THRESHOLDS


def test_pair_regret_overflow_fails_closed_before_receipt_encoding(tmp_path):
    report = _gate(_pairs(
        tmp_path / "pair-overflow",
        baseline_kwargs={"first_metric": -1e308, "best_metric": -1e308},
        first_metric=1e308,
        best_metric=1e308,
    ))
    assert report["passed"] is False
    assert "pair regret overflowed" in _pair_errors(report)


def test_aggregate_overflow_returns_a_bounded_failed_report(tmp_path, monkeypatch):
    monkeypatch.setattr(quality, "_pair_quality", lambda *_args: {
        "normalized_regret": 1e308,
        "hit_rate": 1.0,
        "divergence_rate": 0.0,
        "coverage_ratio": 1.0,
    })
    report = _gate(_pairs(tmp_path / "aggregate-overflow"))
    assert report["passed"] is False
    assert report["aggregates"]["mean_normalized_regret"] is None
    assert any("aggregate quality metrics unavailable" in error for error in report["errors"])


@pytest.mark.parametrize(
    "run_kwargs",
    [
        {"task_snapshot_id": "other-task"},
        {"task_direction": "max"},
    ],
)
def test_task_snapshot_identity_and_direction_must_match_run_start(tmp_path, run_kwargs):
    run = _make_run(tmp_path / "task-mismatch", treatment=False, seed=0, **run_kwargs)
    with pytest.raises(ValueError, match="task snapshot"):
        quality.analyze_speculation_run(run)


def test_empty_and_directory_mismatched_run_ids_fail_closed(tmp_path):
    empty = _make_run(tmp_path / "empty", treatment=False, seed=0, run_id="")
    with pytest.raises(ValueError, match="run_id must be nonempty"):
        quality.analyze_speculation_run(empty)

    mismatched = _make_run(
        tmp_path / "mismatched-directory",
        treatment=False,
        seed=0,
        run_id="different-run-id",
    )
    with pytest.raises(ValueError, match="resolved run directory name"):
        quality.analyze_speculation_run(mismatched)


def test_cloned_event_and_complete_source_evidence_cannot_bypass_directory_identity(tmp_path):
    pairs = _pairs(tmp_path / "cloned")
    source, clone = pairs[0][0], pairs[1][0]
    for name in ("events.jsonl", "config.snapshot.json", "task.snapshot.json"):
        (clone / name).write_bytes((source / name).read_bytes())

    report = _gate(pairs)
    errors = _pair_errors(report)
    assert report["passed"] is False
    assert any("resolved run directory name" in error for error in errors)


@pytest.mark.parametrize(
    ("treatment", "event_type", "data", "error"),
    [
        (
            True,
            "card_build_requested",
            {"card_id": "hidden", "generation": -1},
            "card_build_requested payload is invalid",
        ),
        (
            True,
            "card_build_done",
            {
                "card_id": "orphan",
                "generation": 0,
                "node_id": 1,
                "speculative": True,
            },
            "does not exactly close its current request head",
        ),
    ],
)
def test_raw_card_queue_rows_cannot_hide_behind_fold_rejection(
    tmp_path, treatment, event_type, data, error,
):
    run = _make_run(
        tmp_path / event_type,
        treatment=treatment,
        seed=0,
        pre_finish_events=((event_type, data),),
    )

    with pytest.raises(ValueError, match=error):
        quality.analyze_speculation_run(run)


def test_duplicate_raw_done_cannot_hide_behind_a_closed_folded_queue(tmp_path):
    path = tmp_path / "duplicate-done"
    run = _make_run(
        path,
        treatment=True,
        seed=0,
        pre_finish_events=(("card_build_done", {
            "card_id": f"card-{path.name}",
            "generation": 0,
            "node_id": 1,
            "speculative": True,
        }),),
    )

    with pytest.raises(ValueError, match="does not exactly close its current request head"):
        quality.analyze_speculation_run(run)


def test_interleaved_double_request_cannot_replace_an_open_physical_head(tmp_path):
    run = _make_run(
        tmp_path / "interleaved-double-request",
        treatment=True,
        seed=0,
        pre_finish_events=(
            ("card_build_requested", {"card_id": "interleaved-a", "generation": 0}),
            ("card_build_requested", {"card_id": "interleaved-b", "generation": 0}),
        ),
    )
    with pytest.raises(ValueError, match="before closing its current head"):
        quality.analyze_speculation_run(run)


def test_unknown_event_type_is_not_quality_evidence(tmp_path):
    run = _make_run(tmp_path / "unknown-event", treatment=False, seed=0)
    EventStore(run / "events.jsonl").append("future_unknown_event", {"ignored": True})

    with pytest.raises(ValueError, match="unknown event type"):
        quality.analyze_speculation_run(run)


def test_identity_seed_and_diagnostic_changes_do_not_make_a_cloned_trajectory_unique(tmp_path):
    pairs = []
    for index, seed in enumerate(SPECULATION_CALIBRATION_SEEDS):
        # The first two baseline lanes execute the same candidates and outcomes.  Their run ids and
        # task seeds differ, so their raw sources differ, but neither is independent scientific
        # evidence once the semantic trajectory is canonicalized.
        baseline = _make_run(
            tmp_path / "semantic-clone" / f"baseline-{index}",
            treatment=False,
            seed=seed,
            semantic_variant=777 if index < 2 else seed,
        )
        treatment = _make_run(
            tmp_path / "semantic-clone" / f"treatment-{index}",
            treatment=True,
            seed=seed,
        )
        pairs.append((baseline, treatment))

    first = quality.analyze_speculation_run(pairs[0][0])
    clone = quality.analyze_speculation_run(pairs[1][0])
    assert first["sources"]["events"]["sha256"] != clone["sources"]["events"]["sha256"]
    assert (
        first["sources"]["semantic_trajectory_sha256"]
        == clone["sources"]["semantic_trajectory_sha256"]
    )

    report = _gate(pairs)
    assert report["passed"] is False
    assert (
        "semantic execution trajectory is cloned across evidence lanes"
        in _pair_errors(report)
    )


def test_replicate_seeds_are_unique_and_treatment_depth_is_one_common_value(tmp_path):
    duplicate_seed = _gate(_pairs(
        tmp_path / "duplicate-seeds", seeds=(0, 0, 2)))
    assert duplicate_seed["passed"] is False
    assert "calibration seed is reused across replicate pairs" in _pair_errors(duplicate_seed)

    mixed_depth = _gate(_pairs(
        tmp_path / "mixed-depth", treatment_depths=(1, 2, 1)))
    assert mixed_depth["passed"] is False
    assert "treatment speculation_depth differs across replicate pairs" in _pair_errors(mixed_depth)


def test_only_seed_may_drift_in_the_task_profile_across_replicate_pairs(tmp_path):
    pairs = []
    for index, seed in enumerate(SPECULATION_CALIBRATION_SEEDS):
        task_bytes = _task_bytes(
            seed,
            extra={"objective_variant": "different"} if index == 1 else None,
        )
        pairs.append((
            _make_run(
                tmp_path / "task-profile" / f"baseline-{index}",
                treatment=False,
                seed=seed,
                task_bytes=task_bytes,
            ),
            _make_run(
                tmp_path / "task-profile" / f"treatment-{index}",
                treatment=True,
                seed=seed,
                task_bytes=task_bytes,
            ),
        ))

    report = _gate(pairs)
    assert report["passed"] is False
    assert any("calibration task fields differ" in error for error in _pair_errors(report))


def test_calibration_config_rejects_non_settings_fields(tmp_path):
    pairs = []
    for index, seed in enumerate(SPECULATION_CALIBRATION_SEEDS):
        kwargs = {"extra_config": {"cross_pair_variant": "drift"}} if index == 1 else {}
        pairs.append((
            _make_run(
                tmp_path / "config-replicates" / f"baseline-{index}",
                treatment=False,
                seed=seed,
                **kwargs,
            ),
            _make_run(
                tmp_path / "config-replicates" / f"treatment-{index}",
                treatment=True,
                seed=seed,
                **kwargs,
            ),
        ))

    report = _gate(pairs)
    assert report["passed"] is False
    assert any("config fields differ" in error for error in _pair_errors(report))


@pytest.mark.parametrize(
    ("drift_kwargs", "error"),
    [
        (
            {"workspace": {"repo": "sha256:" + "d" * 64}},
            "calibration fresh Toy workspace must be exactly empty",
        ),
        (
            {"dirty_inputs": [{"path": "local.py", "sha256": "sha256:" + "d" * 64}]},
            "calibration fresh Toy dirty_inputs must be exactly empty",
        ),
    ],
)
def test_material_provenance_must_match_across_replicate_pairs(
    tmp_path, drift_kwargs, error,
):
    pairs = []
    for index, seed in enumerate(SPECULATION_CALIBRATION_SEEDS):
        kwargs = drift_kwargs if index == 1 else {}
        pairs.append((
            _make_run(
                tmp_path / "provenance-replicates" / f"baseline-{index}",
                treatment=False,
                seed=seed,
                **kwargs,
            ),
            _make_run(
                tmp_path / "provenance-replicates" / f"treatment-{index}",
                treatment=True,
                seed=seed,
                **kwargs,
            ),
        ))

    report = _gate(pairs)
    assert report["passed"] is False
    assert error in _pair_errors(report)


def test_data_provenance_is_outside_the_exact_toy_calibration_protocol(tmp_path):
    run = _make_run(
        tmp_path / "data-provenance",
        treatment=False,
        seed=0,
        data_provenance={"dataset": "sha256:" + "d" * 64},
    )
    with pytest.raises(ValueError, match="outside the clean calibration protocol: data_provenance"):
        quality.analyze_speculation_run(run)


@pytest.mark.parametrize("drift_event", ["env_changed", "workspace_changed"])
def test_explicit_environment_or_workspace_drift_invalidates_evidence(tmp_path, drift_event):
    run = _make_run(
        tmp_path / drift_event,
        treatment=False,
        seed=0,
        drift_events=(drift_event,),
    )
    with pytest.raises(ValueError, match="drift"):
        quality.analyze_speculation_run(run)


def test_current_environment_must_match(tmp_path):
    pairs = _pairs(tmp_path / "current-env")
    report = _gate(pairs, environment_fingerprint=_OTHER_ENV)
    assert report["passed"] is False
    assert any("current environment fingerprint" in error for error in _pair_errors(report))


def test_self_consistent_forged_workspace_and_setup_manifest_fail_closed(tmp_path):
    run = _make_run(
        tmp_path / "forged-workspace",
        treatment=False,
        seed=0,
        # _make_run also recomputes setup_step.sources and setup_finished.manifest from this value.
        workspace={"repo": "sha256:" + "d" * 64},
    )
    with pytest.raises(ValueError, match="fresh Toy workspace must be exactly empty"):
        quality.analyze_speculation_run(run)


def test_run_start_and_current_effective_gpu_inventory_must_match(tmp_path):
    assert not _gate(_pairs(tmp_path / "host-gpu"), gpu_inventory=_OTHER_GPU)["passed"]

    pairs = []
    for index, seed in enumerate(SPECULATION_CALIBRATION_SEEDS):
        pairs.append((
            _make_run(
                tmp_path / "pin-gpu" / f"baseline-{index}",
                treatment=False,
                seed=seed,
                gpu_pin=_OTHER_GPU if index == 0 else _GPU_PIN,
            ),
            _make_run(
                tmp_path / "pin-gpu" / f"treatment-{index}",
                treatment=True,
                seed=seed,
            ),
        ))
    report = _gate(pairs)
    assert report["passed"] is False
    assert any("effective GPU inventory differs" in error for error in _pair_errors(report))


def test_zero_baseline_concept_coverage_is_not_a_vacuous_success(tmp_path):
    report = _gate(_pairs(
        tmp_path / "zero-coverage",
        baseline_kwargs={"first_concept": None, "second_concept": None},
        first_concept=None,
        second_concept=None,
    ))
    assert report["passed"] is False
    assert any("coverage must be nonzero" in error for error in _pair_errors(report))


def test_infeasible_metric_path_invalidates_calibration_evidence(tmp_path):
    run = _make_run(
        tmp_path / "infeasible",
        treatment=False,
        seed=0,
        first_metric=-1_000_000_000.0,
        first_violations=["synthetic infeasible"],
    )
    with pytest.raises(ValueError, match="infeasible calibration node"):
        quality.analyze_speculation_run(run)


@pytest.mark.parametrize(
    ("field", "error"),
    [
        ("speculation_implementation_digest", "implementation digest"),
        ("speculation_calibration_profile_digest", "calibration profile digest"),
        ("speculation_calibration_gpu_inventory", "GPU inventory pin"),
        ("speculation_calibration_seed", "calibration seed"),
        ("speculation_policy_scope", "Greedy speculation policy scope"),
        ("speculation_runtime_scope_sha256", "runtime scope pin"),
    ],
)
def test_missing_run_start_calibration_profile_pins_fail_closed(tmp_path, field, error):
    run = _make_run(
        tmp_path / field,
        treatment=False,
        seed=0,
        omit_pins=(field,),
    )
    with pytest.raises(ValueError, match=error):
        quality.analyze_speculation_run(run)


def test_missing_immutable_profile_setting_fails_closed(tmp_path):
    run = _make_run(tmp_path / "missing-setting", treatment=False, seed=0)
    path = run / "config.snapshot.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config.pop("strategist_backend")
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="config fields differ"):
        quality.analyze_speculation_run(run)


def test_scorer_mismatch_and_malformed_or_oversized_sources_fail_closed(tmp_path, monkeypatch):
    pairs = _pairs(tmp_path / "runs")
    monkeypatch.setattr(quality, "scorer_fidelity_gate", lambda: {
        "schema": quality.SCORER_FIDELITY_SCHEMA,
        "passed": False,
        "cases": quality.SCORER_FIDELITY_CASE_COUNT,
        "mismatches": 1,
        "case_results": [
            {"name": name, "passed": False, "expected": [], "actual": []}
            for name in quality.SCORER_FIDELITY_CASE_NAMES
        ],
    })
    assert not _gate(pairs)["passed"]

    (pairs[0][1] / "events.jsonl").write_bytes(b"{not-json}\n")
    assert not _gate(pairs)["passed"]

    pairs = _pairs(tmp_path / "oversized")
    (pairs[0][1] / "config.snapshot.json").write_bytes(b"x" * (1024 * 1024 + 1))
    assert not _gate(pairs)["passed"]


def test_validator_recomputes_source_self_current_environment_and_implementation_digests(tmp_path):
    pairs = _pairs(tmp_path / "runs")
    receipt = _write_receipt(tmp_path / "receipt.json", pairs)

    assert not _validate(receipt, implementation_digest_fn=_IMPL_B)
    assert not _validate(receipt, environment_fingerprint=_OTHER_ENV)
    # Old evidence cannot simply be re-labelled by running the gate under later source bytes.
    regated = _gate(pairs, implementation_digest_fn=_IMPL_B)
    assert regated["passed"] is False
    assert all(
        any("current implementation digest" in error for error in pair["errors"])
        for pair in regated["pairs"]
    )

    tampered = json.loads(json.dumps(receipt))
    tampered["passed"] = False
    assert not _validate(tampered)

    for field, value in (
        ("workload_scope", "arbitrary"),
        ("task_profile_sha256", "sha256:" + "f" * 64),
        ("runtime_scope_sha256", "sha256:" + "e" * 64),
        ("admitted_max_nodes", 3),
        ("calibration_seeds", [0, 1, 3]),
    ):
        tampered = json.loads(json.dumps(receipt))
        tampered[field] = value
        tampered["self_digest"] = quality._self_digest(tampered)
        assert not _validate(tampered)

    # Semantically identical JSON with different bytes still violates the bound source receipt.
    config_path = pairs[0][0] / "config.snapshot.json"
    config_path.write_text(config_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert not _validate(receipt)

    oversized = {"schema": quality.SPECULATION_QUALITY_GATE_SCHEMA, "x": "z" * (1024 * 1024)}
    assert not _validate(oversized)
