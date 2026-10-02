"""Measured upstream equivalence, artifact regression and real trigger checks.

Uses the evaluator's actual stage resolver. Every repetition is an explicit fresh
workspace; no node score is edited. Observable scope is operator declared, never
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
from looplab.engine.upstream_state import digest, node_signature
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
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
    """Bind actual archives, inputs and interpreter, plus all execution declarations."""
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
    python = spec.get("task_python") or sys.executable
    try:
        # Uncached: installing/changing a distribution invalidates the gate at CAS.
        probe = subprocess.run([python, "-c", "import sys,json,platform,importlib.metadata as m;print(json.dumps([sys.executable,sys.version,platform.platform(),sorted((d.metadata['Name'],d.version) for d in m.distributions())]))"],
            cwd=str(Path(rd) if (rd := proposal["selector"]["run_dir"]) else Path.cwd()),
            capture_output=True, timeout=30)
        env = json.loads(probe.stdout) if probe.returncode == 0 and len(probe.stdout) <= 256 * 1024 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        env = None
    if env is None:
        raise UpstreamRefusal("upstream_environment_unavailable", "Cannot read the evaluator interpreter's environment")
    for selector in (proposal["old_selector"], proposal["selector"]):
        selected_seed_base(selector)
    snapshots = {}
    for name in ("task.snapshot.json", "config.snapshot.json"):
        raw = read_bounded_regular_file(Path(proposal["selector"]["run_dir"]) / name, 2 * 1024 * 1024 + 1)
        if raw is None or len(raw) > 2 * 1024 * 1024:
            raise UpstreamRefusal("upstream_snapshot_unavailable", "Task/config snapshot is missing or unreadable")
        snapshots[name] = hashlib.sha256(raw).hexdigest()
    return digest({"task": task.model_dump(), "settings": settings.model_dump(), "snapshots": snapshots,
                   "source": node_signature(source), "proposal": proposal,
                   "inputs": inputs, "environment": env, "process_env": dict(os.environ)})


def execute_gate(rd, task, settings, source, proposal, manifest, action_id, charge):
    from looplab.core.envsafe import merge_env
    from looplab.engine.eval_stages import EvalStagesMixin
    spec, declaration = task.repo_spec(), task.upstream
    es = task.eval_spec()
    before = input_identity(task, settings, source, proposal)
    old_archive, _ = selected_seed_base(proposal["old_selector"])
    baseline_boundary = boundary_at(old_archive, spec["scorer_boundary"])
    executions, checks, index = [], [], 0
    root = owned_path(rd, "upstream/checks/" + digest(action_id)[:24])
    root.mkdir(parents=True, exist_ok=False)

    def run(label, selector, files, deleted=(), probe=None, failed_artifacts_ok=False):
        nonlocal index
        index += 1
        work = root / f"{index:03d}-{label}"
        effective = {**spec, "effective_seed_base": selector}
        seed_candidate_workspace(effective, work)
        checked_overlay(spec, files, deleted)
        write_overlay(work, files, deleted)
        if boundary_at(work, spec["scorer_boundary"]) != baseline_boundary:
            raise UpstreamRefusal("upstream_scorer_changed", "Gate workspace changed declared scorer bytes")
        env = merge_env(settings.eval_env, spec.get("eval_env", {}), {"PYTHONDONTWRITEBYTECODE": "1"})
        if probe:
            argv, timeout, metric, stages = probe["command"], probe["timeout"], {"kind": "stdout_regex", "pattern": "NEVER_A_SCORE=(.*)"}, None
            cwd = str(work)
        else:
            argv, timeout = command_eval.build_command(es, source.idea.params, "full")
            stages = EvalStagesMixin()._resolve_stages(work, es, source.idea.params, argv, timeout)
            metric, cwd = es["metric"], str(work / (es.get("cwd") or "."))
            if Path(es.get("cwd") or ".").is_absolute():
                raise UpstreamRefusal("upstream_scope_unsupported", "Upstream evaluator cwd must be workspace relative")
        state = fold(EventStore(Path(rd) / "events.jsonl").read_all())
        ceiling = state.budget_overrides.get("max_eval_seconds", settings.max_eval_seconds)
        if ceiling is not None:
            remaining = ceiling - state.total_eval_seconds
            required = sum(float(s.get("timeout", timeout)) for s in stages) if stages else timeout
            if remaining <= 0 or required > remaining:
                raise UpstreamRefusal("upstream_budget_exhausted", "Extend the explicit evaluation budget before buying the next gate execution")
        if timeout > settings.max_eval_timeout or any(float(s.get("timeout", timeout)) > settings.max_eval_timeout for s in stages or []):
            raise UpstreamRefusal("upstream_timeout_exceeded", "Gate execution exceeds the run's declared per-evaluation timeout ceiling")
        start = time.monotonic()
        result = command_eval.run_command_eval(argv, cwd, timeout, metric, env=env,
            stages=stages, log_dir=str(work / ".gate-logs"),
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
    for probe in declaration["regressions"] + (declaration["repair_probes"] if proposal["repair_trigger_nodes"] else []):
        repair = probe in declaration["repair_probes"]
        old = run("old-" + probe["name"], proposal["old_selector"], probe["files"], probe=probe, failed_artifacts_ok=repair)
        new = run("new-" + probe["name"], proposal["selector"], probe["files"], probe=probe)
        passed = old["valid"] and new["valid"] and new["exit_code"] == 0
        passed = passed and (old["exit_code"] != 0 if repair else old["exit_code"] == 0 and old["artifacts"] == new["artifacts"])
        checks.append({"kind": "repair" if repair else "regression", "name": probe["name"], "passed": bool(passed)})
    if proposal["repair_trigger_nodes"] and not declaration["repair_probes"]:
        checks.append({"kind": "repair", "passed": False, "reason": "Declare a real old-fail/new-pass trigger probe"})
    values = [[], []]
    for repeat in range(declaration["repeats"]):
        for side, selector, files, deleted in (
            (0, proposal["old_selector"], source.files, source.deleted),
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
            "scope": "declared scorer, old recipes, trigger probes and full source repetitions"}
