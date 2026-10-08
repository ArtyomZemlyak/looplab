"""Measured upstream equivalence, artifact regression and real trigger checks.

Uses the node pipeline and live timeouts, stall/divergence and subject policy.
Every repetition uses a fresh workspace; no node score is edited.
Observable scope is operator declared, never
an assertion of equivalence for unspecified recipes or environments.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

from looplab.core.errors import UpstreamRefusal
from looplab.core.node_evidence import read_bounded_regular_file
from looplab.core.scorer_boundary import BoundaryCapture
from looplab.engine.seed_base import selected_seed_base
from looplab.engine.upstream_state import digest, events_for, node_signature
from looplab.engine.shared import engine_fold as fold
from looplab.engine.upstream_workspace import checked_overlay, write_overlay, owned_path
from looplab.engine.workspace_seed import seed_candidate_workspace, seeded_base_revision
from looplab.runtime import command_eval


def boundary_at(work, declaration):
    capture = BoundaryCapture(declaration)
    for name in declaration["files"]:
        path = Path(work) / name
        data = read_bounded_regular_file(path, 4 * 1024 * 1024 + 1)
        if data is not None and len(data) <= 4 * 1024 * 1024:
            capture.add(name, data, path.stat().st_mode & 0o111)
    result = capture.receipt({"complete": True})
    if not result["complete"]:
        raise UpstreamRefusal("upstream_scorer_unavailable", "Declared scorer bytes are unavailable")
    return result["digest"]


def input_identity(task, settings, source, proposal):
    """Bind archives, inputs and interpreter, including the effective live eval spec."""
    spec = task.repo_spec()
    if settings.trust_mode != "trusted_local":
        raise UpstreamRefusal("upstream_scope_unsupported", "This upstream gate requires trusted_local execution; it never bypasses a sandbox")
    es = task.eval_spec()
    if es.get("host_scorer") or es.get("run_setup") or es.get("setup"):
        raise UpstreamRefusal("upstream_scope_unsupported", "Declare a self-contained evaluator and scorer boundary; external scorers and environment installation need a separate gate")
    inputs = []
    for name, row in list((spec.get("data") or {}).items()) + [(r["name"], r) for r in spec.get("references", [])]:
        if row.get("edit"):
            raise UpstreamRefusal("upstream_input_unavailable", "Mutable data cannot certify an immutable comparison")
        path = Path(row["path"])
        if path.is_dir():
            observed = seeded_base_revision(path)
            if not observed["complete"]:
                raise UpstreamRefusal("upstream_input_unavailable", f"Input {name} exceeds bounded regular-file evidence")
            inputs.append([name, observed["digest"]])
        else:
            body = read_bounded_regular_file(path, 64 * 1024 * 1024 + 1)
            if body is None or len(body) > 64 * 1024 * 1024:
                raise UpstreamRefusal("upstream_input_unavailable", f"Input {name} is missing or unsupported")
            inputs.append([name, hashlib.sha256(body).hexdigest()])
    for selector in (proposal["old_selector"], proposal["selector"]):
        selected_seed_base(selector)
    snapshots = {}
    for name in ("task.snapshot.json", "config.snapshot.json"):
        raw = read_bounded_regular_file(Path(proposal["selector"]["run_dir"]) / name, 2 * 1024 * 1024 + 1)
        if raw is None or len(raw) > 2 * 1024 * 1024:
            raise UpstreamRefusal("upstream_snapshot_unavailable", "Task/config snapshot is missing or unreadable")
        snapshots[name] = hashlib.sha256(raw).hexdigest()
    context = evaluation_context(task, settings, events_for(Path(proposal["selector"]["run_dir"])))
    env = environment_identity(spec, settings, context, proposal["selector"]["run_dir"])
    from looplab.engine.shared import effective_eval_spec
    return digest({"task": task.model_dump(), "settings": settings.model_dump(), "snapshots": snapshots,
                   "effective_eval_spec": effective_eval_spec(context),
                   "source": node_signature(source), "proposal": proposal,
                   "inputs": inputs, "environment": env, "process_env": dict(os.environ)})


def evaluation_env(settings, spec):
    """The same declared run/task environment for probing and gate execution."""
    from looplab.core.envsafe import merge_env
    return merge_env(settings.eval_env, spec.get("eval_env", {}), {"PYTHONDONTWRITEBYTECODE": "1"})


def environment_identity(spec, settings, context, rd):
    """Uncached selected-interpreter distributions under actual declared env layers.

    Stage environments are operator-only; use the pipeline's validated stages.
    Identical envs share a probe within this read, never across gate/CAS reads.
    The whole observation retains the previous 30-second bound.
    """
    from looplab.core.envsafe import is_secret_env, merge_env
    python = spec.get("task_python") or sys.executable
    base = evaluation_env(settings, spec)
    contexts = [("evaluation", base)] + [(s["name"], merge_env(base, s.get("env")))
        for s in context._operator_stages(context._eval_spec) or []]
    process = {k: v for k, v in os.environ.items() if not is_secret_env(k, v)}
    deadline, cache, observed = time.monotonic() + 30, {}, []
    for label, declared in contexts:
        identity = digest(declared)
        if identity not in cache:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UpstreamRefusal("upstream_environment_unavailable", "Evaluator environment observation exceeded its time bound")
            try:
                probe = subprocess.run([python, "-c", "import sys,json,platform,importlib.metadata as m;print(json.dumps([sys.executable,sys.version,platform.platform(),sorted((d.metadata['Name'],d.version) for d in m.distributions())]))"],
                    cwd=str(rd), env=merge_env(process, declared), capture_output=True, timeout=remaining)
                value = json.loads(probe.stdout) if probe.returncode == 0 and len(probe.stdout) <= 256 * 1024 else None
            except (OSError, ValueError, subprocess.SubprocessError):
                value = None
            if value is None:
                raise UpstreamRefusal("upstream_environment_unavailable", "Cannot read the evaluator interpreter's declared environment")
            cache[identity] = value
        observed.append([label, identity, cache[identity]])
    return observed


def evaluation_context(task, settings, events):
    """Use the node pipeline's single derivation, without engine setup or work."""
    from looplab.engine.eval_stages import EvalStagesMixin
    context = EvalStagesMixin()
    context._eval_spec = task.eval_spec()
    context._eval_timeout_override = command_eval.eval_timeout_override(fold(events).budget_overrides)
    context._strategy_fidelity = None
    context.max_eval_timeout = settings.max_eval_timeout
    context.metric_subject = settings.metric_subject
    return context


UPSTREAM_VERIFY_MODES = ("canary", "full")


def upstream_verify_setting(settings) -> str:
    """THE ONE READER of `Settings.upstream_verify` (doc 73 §4.2 G5). Anything unreadable is `full` —
    the strict, historical gate."""
    value = getattr(settings, "upstream_verify", "full")
    return value if value in UPSTREAM_VERIFY_MODES else "full"


def gate_profile(settings, task) -> str:
    """The equivalence profile a gate on this run MUST measure: `canary` when the setting asks for it
    AND the task declares an `eval.canary` (a cheap slice the operator vouched for), else `full`
    paired repetitions. Re-derived at advance time (`gate_matches_policy(profile=)`), so a result
    naming another profile grants nothing."""
    from looplab.engine.eval_canary import canary_spec
    if upstream_verify_setting(settings) != "canary":
        return "full"
    try:
        declared = canary_spec(task.eval_spec())
    except Exception:  # noqa: BLE001 — an unreadable eval spec declares no canary; the strict profile applies
        declared = None
    return "canary" if declared is not None else "full"


def waives_equivalence(declaration, proposal) -> bool:
    """The operator declared `repair_gate: probes` AND this proposal promotes ONLY a repair: it has
    a pending trigger to probe and every nominated hunk is repair-origin (`repair_only`, recorded on
    `upstream_proposed`; absent on an older proposal, which therefore keeps the full gate). A repair
    hunk nominated beside idea hunks would otherwise skip the repetitions for all of them."""
    return (declaration.get("repair_gate") == "probes"
            and bool(proposal.get("repair_trigger_nodes"))
            and proposal.get("repair_only") is True)


def old_side_overlay(source, proposal, manifest):
    """The SOURCE the gate's old side runs on the current base: the node's own overlay, or — for a
    proposal whose source was measured on an older base and REBASED (doc 73 §4.3) — the merged overlay
    the manifest carries, bound to the proposal row by its hash. A manifest whose merged overlay does
    not match that hash refuses: the old side would not be the source the lane admitted."""
    rebase = manifest.get("rebase") if isinstance(manifest, dict) else None
    if not proposal.get("rebased_from"):
        return source.files, source.deleted
    if (not isinstance(rebase, dict) or rebase.get("from_digest") != proposal["rebased_from"]
            or digest({"files": rebase.get("files"), "deleted": rebase.get("deleted")})
            != proposal.get("source_overlay_hash")):
        raise UpstreamRefusal("upstream_rebase_changed", "The proposal's merged source overlay changed; propose again")
    return rebase["files"], rebase["deleted"]


def execute_gate(rd, task, settings, source, proposal, manifest, action_id, charge, *, extra_env=None):
    from looplab.engine.shared import effective_eval_spec, effective_max_eval_timeout
    spec, declaration = task.repo_spec(), task.upstream
    # THE OLD SIDE (doc 73 §4.3): the source as the lane admitted it — rebased onto the current base
    # when the base moved past its measurement. Under `full` it must still reproduce the source's own
    # measured score, so a merge that changed the source's behaviour fails the gate rather than
    # comparing two things the source never measured.
    source_files, source_deleted = old_side_overlay(source, proposal, manifest)
    rebased = bool(proposal.get("rebased_from"))
    before = input_identity(task, settings, source, proposal)
    old_archive, _ = selected_seed_base(proposal["old_selector"])
    baseline_boundary = boundary_at(old_archive, spec["scorer_boundary"])
    executions, checks, index = [], [], 0
    root = owned_path(rd, "upstream/checks/" + digest(action_id)[:24])
    root.mkdir(parents=True, exist_ok=False)

    def run(label, selector, files, deleted=(), probe=None, failed_artifacts_ok=False, canary=None):
        nonlocal index
        index += 1
        work = root / f"{index:03d}-{label}"
        effective = {**spec, "effective_seed_base": selector}
        seed_candidate_workspace(effective, work)
        checked_overlay(spec, files, deleted)
        write_overlay(work, files, deleted)
        if boundary_at(work, spec["scorer_boundary"]) != baseline_boundary:
            raise UpstreamRefusal("upstream_scorer_changed", "Gate workspace changed declared scorer bytes")
        # `extra_env`: the devices a LIVE engine leased for this gate (`engine/upstream_serve.py`) —
        # only `CUDA_VISIBLE_DEVICES`; None on the stopped lane, which owns the box.
        env = {**evaluation_env(settings, spec), **(extra_env or {})}
        events = events_for(Path(rd))
        context = evaluation_context(task, settings, events)
        es = effective_eval_spec(context)
        if probe:
            argv, timeout, metric, stages = probe["command"], probe["timeout"], {"kind": "stdout_regex", "pattern": "NEVER_A_SCORE=(.*)"}, None
            cwd = str(work)
        else:
            argv, timeout, stages, _ = context._eval_pipeline(source, work, "full")
            if canary is not None:
                # The task's declared canary (`eval_canary.py`): its env last, every stage and the
                # single command capped at its timeout — the slice a node's own canary runs.
                from looplab.engine.eval_canary import capped_pipeline
                timeout, stages = capped_pipeline(timeout, stages, canary["timeout"])
                env = {**env, **canary["env"]}
            metric, cwd = es["metric"], str(work / (es.get("cwd") or "."))
            if Path(es.get("cwd") or ".").is_absolute():
                raise UpstreamRefusal("upstream_scope_unsupported", "Upstream evaluator cwd must be workspace relative")
        state = fold(events)
        ceiling = state.budget_overrides.get("max_eval_seconds", settings.max_eval_seconds)
        if ceiling is not None:
            remaining = ceiling - state.total_eval_seconds
            required = sum(float(s.get("timeout", timeout)) for s in stages) if stages else timeout
            if remaining <= 0 or required > remaining:
                raise UpstreamRefusal("upstream_budget_exhausted", "Extend the explicit evaluation budget before buying the next gate execution")
        ceiling = effective_max_eval_timeout(context)
        if timeout > ceiling or any(float(s.get("timeout", timeout)) > ceiling for s in stages or []):
            raise UpstreamRefusal("upstream_timeout_exceeded", "Gate execution exceeds the run's declared per-evaluation timeout ceiling")
        start = time.monotonic()
        result = command_eval.run_command_eval(argv, cwd, timeout, metric, env=env,
            stages=stages, log_dir=str(work / ".gate-logs"),
            stall_cap=settings.eval_stall_timeout_s,
            divergence_watch=settings.single_command_divergence_watch,
            subject=es["metric"].get("subject") if not probe and settings.metric_subject != "off" else None,
            subject_glob=es["metric"].get("subject_glob") if not probe and settings.metric_subject != "off" else None,
            cross_check=es.get("cross_check") if not probe else None,
            drift_tolerance=float(es.get("drift_tolerance", 1e-6)),
            enforce_drift=settings.eval_trust_mode == "ratify_freeze_drift" and not probe,
            metrics=es.get("metrics") if not probe else None,
            constraints=es.get("constraints") if not probe else None)
        seconds = time.monotonic() - start
        # Charge immediately: later failure never erases a real execution.
        row = {"label": label, "exit_code": result.exit_code, "timed_out": result.timed_out,
               "metric": result.metric if result.metric is not None and math.isfinite(result.metric) else None,
               "seconds": seconds, "workdir": str(work.relative_to(rd)).replace("\\", "/"),
               "stages": result.stages, "artifacts": {}, "valid": False}
        executions.append(row)
        valid = not (result.timed_out or result.stalled or result.diverged or result.drift or result.violations or result.failed_stage)
        if not probe and settings.metric_subject != "off":
            from looplab.engine.metric_salvage import unbound_subject_violation_rows
            row["metric_subject"] = result.metric_subject or command_eval.absent_metric_subject()
            valid = valid and not unbound_subject_violation_rows(row["metric_subject"], result.metric, settings.metric_subject)
        try:
            if boundary_at(work, spec["scorer_boundary"]) != baseline_boundary:
                raise UpstreamRefusal("upstream_scorer_changed", "Execution changed the declared scorer boundary")
            for name in (probe or {}).get("artifacts", []):
                body = read_bounded_regular_file(work / name, 4 * 1024 * 1024 + 1)
                if body is None or len(body) > 4 * 1024 * 1024:
                    if not (failed_artifacts_ok and result.exit_code != 0):
                        valid = False
                else:
                    row["artifacts"][name] = hashlib.sha256(body).hexdigest()
            row["valid"] = valid and (probe is not None or row["metric"] is not None)
        finally:
            charge(row)  # even a scorer/artifact refusal retains actual execution cost
        return row

    for probe in declaration["tests"]:
        r = run("test-" + probe["name"], proposal["selector"], probe["files"], probe=probe)
        checks.append({"kind": "test", "name": probe["name"], "passed": r["valid"] and r["exit_code"] == 0})
    probes = [("regression", p) for p in declaration["regressions"]]
    if proposal["repair_trigger_nodes"]:
        probes += [("repair", p) for p in declaration["repair_probes"]]
    for kind, probe in probes:
        repair = kind == "repair"
        old = run("old-" + probe["name"], proposal["old_selector"], probe["files"], probe=probe, failed_artifacts_ok=repair)
        new = run("new-" + probe["name"], proposal["selector"], probe["files"], probe=probe)
        passed = old["valid"] and new["valid"] and new["exit_code"] == 0
        passed = passed and (old["exit_code"] != 0 if repair else old["exit_code"] == 0 and old["artifacts"] == new["artifacts"])
        checks.append({"kind": kind, "name": probe["name"], "passed": bool(passed)})
    if proposal["repair_trigger_nodes"] and not declaration["repair_probes"]:
        checks.append({"kind": "repair", "passed": False, "reason": "Declare a real old-fail/new-pass trigger probe"})
    if waives_equivalence(declaration, proposal):
        # doc 73 §2.3 (track 1): the operator declared `repair_gate: probes`, and this proposal
        # promotes a REPAIR — no source metric existed before it to reproduce. The waiver is a check
        # row of its own, never an `equivalence` row claiming samples that were not taken.
        checks.append({"kind": "equivalence_waived", "passed": True,
                       "reason": "repair_gate=probes: the source recipe had no metric before the fix"})
        after = input_identity(task, settings, source, proposal)
        return {"passed": before == after and all(c["passed"] for c in checks),
                "input_identity": before, "inputs_unchanged": before == after, "checks": checks,
                "executions": executions, "eval_seconds": sum(r["seconds"] for r in executions),
                "scope": "declared scorer, old recipes and trigger probes (repair_gate=probes)"}
    if gate_profile(settings, task) == "canary":
        # doc 73 §4.2 G5: ONE old/new pair on the declared canary instead of the paired full
        # repetitions — hours on a GPU task become minutes. A different slice than the source's own
        # measurement, so its score is not asked to reproduce; the tolerance is the operator's.
        from looplab.engine.eval_canary import canary_spec
        declared = canary_spec(task.eval_spec())
        values = [[], []]
        for side, selector, files, deleted in (
            (0, proposal["old_selector"], source_files, source_deleted),
            (1, proposal["selector"], manifest["recipe_files"], manifest.get("recipe_deleted", []))):
            r = run(("old-canary" if side == 0 else "new-canary") + "0", selector, files, deleted,
                    canary=declared)
            if r["valid"] and r["exit_code"] == 0:
                values[side].append(r["metric"])
        equivalence = {"kind": "equivalence", "passed": False, "values": values, "profile": "canary"}
        if all(len(v) == 1 for v in values):
            means = [values[0][0], values[1][0]]
            tolerance = declaration["atol"] + declaration["rtol"] * max(map(abs, means))
            equivalence.update(means=means, tolerance=tolerance, delta=means[1] - means[0],
                               passed=abs(means[1] - means[0]) <= tolerance)
        checks.append(equivalence)
        after = input_identity(task, settings, source, proposal)
        return {"passed": before == after and all(c["passed"] for c in checks),
                "input_identity": before, "inputs_unchanged": before == after, "checks": checks,
                "executions": executions, "eval_seconds": sum(r["seconds"] for r in executions),
                "scope": "declared scorer, old recipes, trigger probes and one canary pair"
                         + (" (source rebased onto the current base)" if rebased else "")}
    values = [[], []]
    for repeat in range(declaration["repeats"]):
        for side, selector, files, deleted in (
            (0, proposal["old_selector"], source_files, source_deleted),
            (1, proposal["selector"], manifest["recipe_files"], manifest.get("recipe_deleted", []))):
            r = run(("old-source" if side == 0 else "new-source") + str(repeat), selector, files, deleted)
            if r["valid"] and r["exit_code"] == 0:
                values[side].append(r["metric"])
    equivalence = {"kind": "equivalence", "passed": False, "values": values, "profile": "full"}
    if all(len(v) == declaration["repeats"] for v in values):
        means = [statistics.mean(v) for v in values]
        sem = [statistics.stdev(v) / math.sqrt(len(v)) for v in values]
        tolerance = declaration["atol"] + declaration["rtol"] * max(map(abs, means)) + declaration["sigma"] * math.hypot(*sem)
        original = source.task_metric if source.task_metric is not None else source.metric
        source_tolerance = declaration["atol"] + declaration["rtol"] * abs(original) + declaration["sigma"] * sem[0]
        equivalence.update(means=means, sem=sem, tolerance=tolerance, delta=means[1] - means[0],
            source_reproduced=abs(means[0] - original) <= source_tolerance,
            passed=abs(means[1] - means[0]) <= tolerance and abs(means[0] - original) <= source_tolerance)
    checks.append(equivalence)
    after = input_identity(task, settings, source, proposal)
    return {"passed": before == after and all(c["passed"] for c in checks),
            "input_identity": before, "inputs_unchanged": before == after, "checks": checks,
            "executions": executions, "eval_seconds": sum(r["seconds"] for r in executions),
            "scope": "declared scorer, old recipes, trigger probes and full source repetitions"
                     + (" (source rebased onto the current base)" if rebased else "")}
