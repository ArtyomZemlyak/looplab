"""Eval dispatch (run-setup gating, data binds, the eval entrypoint, sweep collapse) for the
engine — extracted from orchestrator.py as a MIXIN: `class Engine(EvalDispatchMixin, …)`
inherits these methods unchanged, so there is ZERO call-site churn and `self` here IS the
engine. The method bodies are verbatim moves and read engine attributes freely (`_eval_spec`,
`_repo_spec`, `_run_setup_lock`, `trust_mode`, `sandbox`, `store`, …), exactly as they did
inside the class. `_run_eval` is instance-monkeypatched by tests — a mixin preserves that seam.

Runtime deps (`command_eval`, `_run_argv`, `_to_float`) stay method-local so monkeypatching
through their source modules keeps working."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from looplab.events.replay import fold
from looplab.events.types import EV_RUN_SETUP_FINISHED, EV_RUN_SETUP_STARTED

# THE engine sentinel (engine/options.py): `_evaluate` passes it into `_run_eval` positionally
# (as `next_start`), so the identity check here MUST see the same object the orchestrator uses.
from looplab.engine.options import _UNSET


class EvalDispatchMixin:
    """The engine's eval-dispatch cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    def _agent_may(self, role: str, setting: str) -> bool:
        """Governance gate (Settings.agent_control): may `role` (strategist|boss|researcher) change
        `setting` at runtime? A setting absent from the map is LOCKED for everyone. Pure + cheap —
        called at each agent seam so the matrix is the single source of truth."""
        return role in (self._agent_control.get(setting) or ())

    def _ensure_run_setup(self) -> None:
        """Run the eval's RUN-LEVEL `run_setup` exactly ONCE, before the first eval — e.g. a one-time
        dependency install into the shared interpreter (the autonomy default when deps are stable
        across experiments). Distinct from per-node `setup`, which reinstalls before EVERY eval. Runs
        in the first editable repo's SOURCE dir so `-r requirements.txt` resolves; output streams to
        `run_setup.log`. A non-zero/timed-out run_setup ABORTS the run (the env would be unusable).
        Only in trusted_local (an untrusted/docker eval is a fresh container — use per-node `setup`).
        No-op when `run_setup` is unset. The guard is set BEFORE running so a crash can't retry-loop."""
        if self._run_setup_done:
            return
        # Serialize the check-then-set: parallel eval worker threads would otherwise all see
        # _run_setup_done == False and launch pip (not concurrency-safe) N times into one interpreter.
        with self._run_setup_lock:
            if self._run_setup_done:
                return
            cmd = list((self._eval_spec or {}).get("run_setup") or [])
            if not cmd or self.trust_mode != "trusted_local":
                self._run_setup_done = True
                return
            # Crash-safe exactly-once across resume (arch-review §5 P2): if THIS run_setup command
            # already completed successfully in a prior process (folded from `run_setup_finished`),
            # skip it — don't re-install deps on every resume. The in-memory flag above still guards
            # the concurrent case within one process; this closes the cross-process/resume gap.
            from looplab.core.models import run_setup_key
            if run_setup_key(cmd) in fold(self.store.read_all()).run_setup_done:
                self._run_setup_done = True
                return
            self._run_setup_done = True
            self._do_run_setup(cmd)

    def _do_run_setup(self, cmd: list) -> None:
        from looplab.runtime.sandbox import _run_argv
        eds = (self._repo_spec or {}).get("editables", [])
        cwd = eds[0]["path"] if eds else str(self.run_dir)
        to = float((self._eval_spec or {}).get("run_setup_timeout", 1800.0))
        self.store.append(EV_RUN_SETUP_STARTED, {"command": cmd, "cwd": cwd})
        log = str(Path(self.run_dir) / "run_setup.log")
        rc, out, err, timed = _run_argv(cmd, cwd, to, log_path=log)
        # Carry the command so the fold can key the exactly-once record on it (arch-review §5 P2).
        self.store.append(EV_RUN_SETUP_FINISHED,
                          {"command": cmd, "exit_code": rc, "timed_out": timed,
                           "stderr_tail": (err or "")[-2000:]})
        if rc != 0 or timed:
            raise RuntimeError(f"run_setup failed (exit={rc}, timed_out={timed}); see {log}\n"
                               + (err or out or "")[-500:])

    def _data_binds(self, workdir) -> Optional[list]:
        """(host_path, read_only) binds for the untrusted tier: every data/reference source that was
        actually materialized as a SYMLINK in `workdir`, bound at its own absolute path inside the
        container (the workspace bind carries only the symlink, which would otherwise dangle there).
        read_only unless the data source's `edit` permission grants in-place writes — so `edit:false`
        is enforced at the MOUNT layer for sandboxed runs, not just in the agent-facing write-tool
        gate. (The trusted_local tier runs on the host and keeps only the tool gate; documented in
        docs/guide/tasks.md.)

        Only ACTUAL symlinks are bound (arch-review §4 P1-8): a source that was copied IN — either
        `mount:false`, or a `mount:true` source on a host where `os.symlink` fell back to a copy
        (Windows without the symlink privilege) — already lives inside the /work bind, so re-binding
        it is redundant, and on Windows its drive-letter path has no valid Linux container target. `workdir`
        is already seeded (`_materialize` runs before `_run_eval`), so the symlink check is reliable."""
        wd = Path(workdir)

        def _linked(name) -> bool:
            try:
                return (wd / name).is_symlink()
            except OSError:
                return False

        binds: list = []
        for name, spec in (self._repo_spec or {}).get("data", {}).items():
            if isinstance(spec, dict):                    # DataSpec dict | bare path (back-compat)
                if spec.get("mount", True) and spec.get("path") and _linked(name):
                    binds.append((spec["path"], not spec.get("edit", False)))
            elif spec and _linked(name):
                binds.append((spec, True))
        for ref in (self._repo_spec or {}).get("references", []):
            if ref.get("mount") and ref.get("path") and _linked(ref["name"]):
                binds.append((ref["path"], True))         # references are read-only by definition
        return binds or None

    def _run_eval(self, node, workdir, env=None, profile=None, cancel=None, start_stage=_UNSET):
        """Eval dispatcher: RepoTask runs the operator's command + reads its metric;
        otherwise the classic solution.py sandbox path. Both return a `RunResult`, so all
        downstream metric/exit/timeout checks are identical.

        Phase 2: the command is built with an eval profile (smoke/full — `profile` arg, else
        the Researcher's `idea.eval_profile`) and, when params_style=cli_overrides, the
        node's params as `key=value` overrides.

        `start_stage`: which pipeline stage to run FROM (earlier stages reused). Default `_UNSET`
        derives it from `node.rerun_stage` (the operator node_reset seam). The inline-repair loop
        passes an EXPLICIT value (a stage name to reuse-into, or None for a full re-run) computed by
        its safe-reuse predicate — passing explicitly avoids the transient `rerun_stage` being reset
        by the loop's re-fold."""
        if self._eval_spec:
            from looplab.runtime import command_eval
            es = self._eval_spec
            self._ensure_run_setup()             # one-time run-level dep install (before the first eval)
            prof = profile or (node.idea.eval_profile if node is not None else None)
            # A7 Strategist fidelity override: when the active strategy pins smoke/full and the node
            # didn't request a profile, use the strategy's. An explicit `profile` arg (confirm=full)
            # always wins. "adaptive" leaves _strategy_fidelity None => the Idea's own profile.
            if prof is None and self._strategy_fidelity in ("smoke", "full"):
                prof = self._strategy_fidelity
            params = node.idea.params if node is not None else {}
            cmd, timeout = command_eval.build_command(es, params, prof)
            root = str(Path(workdir).resolve())               # repo/workdir root
            stages = self._resolve_stages(root, es, params,   # cmd-authoritative pipeline (+ %params% per stage)
                                          score_cmd=cmd, score_timeout=timeout)  # profile/timeout survive pipeline mode
            check_fn = (self._stage_check_fn(node)            # Phase 3: inter-stage verify (only if any stage asks)
                        if stages and any(s.get("check") for s in stages) else None)
            cwd = self._sandbox_cwd(workdir, es.get("cwd", "."))
            # untrusted tier (Phase 4): sandbox the eval in docker, mounting the workspace
            # root so the cwd subdir + host metric reading line up. Fails loudly w/o docker.
            # Symlink-mounted data/reference sources ride along as same-path binds (the /work
            # bind alone leaves their symlinks dangling in the container) — read-only unless the
            # source's `edit` permission grants writes (mount-layer enforcement of edit:false).
            wrap = (command_eval.make_docker_wrap(
                        root, self.docker_image,
                        mem=self.sandbox_memory or None, cpus=self.sandbox_cpus or None,
                        runtime=("runsc" if self.trust_mode == "hostile" else None),
                        binds=self._data_binds(workdir),
                        env=env)   # forward LOOPLAB_EVAL_SEED etc. into the container (per-eval env)
                    if self.trust_mode in ("untrusted", "hostile") else None)
            res = command_eval.run_command_eval(
                cmd, cwd, timeout, es["metric"], env,
                setup=es.get("setup") or None, setup_timeout=es.get("setup_timeout", 600.0),
                setup_cwd=root,                               # deps install at the repo root
                cross_check=es.get("cross_check"),            # Phase 4 drift cross-check …
                drift_tolerance=float(es.get("drift_tolerance", 1e-6)),
                enforce_drift=(self.eval_trust_mode == "ratify_freeze_drift"),
                wrap=wrap,
                metrics=es.get("metrics") or None,            # #5 multi-objective …
                constraints=es.get("constraints") or None,
                tracer=self.tracer,                           # child spans: setup/command/read
                cancel=cancel,                                # operator mid-eval node_abort
                log_dir=root,                                 # live setup.log/eval.log in the node workdir
                stages=stages,                                # multi-stage pipeline (Phase 1); None = single command
                start_stage=((node.rerun_stage if node is not None else None)
                             if start_stage is _UNSET else start_stage),  # Phase 2: re-run from a stage
                check_fn=check_fn)                            # Phase 3: optional inter-stage agentic verify
        else:
            # Intra-node sweep nodes run a whole grid in one process, so they need ~N× the
            # single-eval budget. `sweep_timeout_mult` scales the wall-clock for sweep nodes only;
            # _kill_tree + the mid-eval cancel watcher still bound a runaway. (The RepoTask path
            # gets its per-profile timeout from build_command above.)
            timeout = self.timeout
            if node is not None and node.idea.is_sweep:
                timeout = self.timeout * self.sweep_timeout_mult
            # Researcher-sized per-node budget (e.g. a neural-net / large-ensemble idea that needs longer
            # than the run default) — honored ONLY when the governance matrix grants the researcher the
            # `timeout` setting; otherwise the run-wide budget stands. This is the "auto" per-node mode.
            etv = getattr(node.idea, "eval_timeout", None) if node is not None else None
            if etv and etv > 0 and self._agent_may("researcher", "timeout"):
                timeout = float(etv)
            res = self.sandbox.run(node.code, str(workdir), timeout, env, cancel=cancel)
        # Intra-node sweep: if the solution reported a grid of trials, collapse them into the node's
        # scalar `metric` (the best feasible trial under the task direction) so fold/best-selection/
        # improve are untouched. Done BEFORE host grading so a host grader still has the final say on
        # the best trial's predictions file. The full trial list rides along on `res.trials`.
        if res.trials:
            self._apply_sweep_best(res)
        # Out-of-process host-side grading (general): override the (ignored) self-reported metric with
        # the HOST's score of the candidate's predictions. Applied for BOTH the command-eval and the
        # sandbox path, so a task that exposes host_grader() is always host-scored — and so EVERY
        # sandbox-path eval (normal AND the multi-seed confirm pass, both call _run_eval) is graded
        # the same way. host_grader takes precedence: its score replaces any self-reported metric.
        if self._host_grader is not None:
            res = self._apply_host_grade(res, workdir)
        return res

    def _apply_sweep_best(self, res):
        """Collapse an intra-node sweep's `res.trials` into the node's scalar `metric`: pick the
        best trial that produced a usable (finite) metric, under the task direction. Keeping
        `metric` a single number means fold, best-selection, confirm and `improve` treat a sweep
        node like any other; the trials are audit/UI only. No usable trial -> no metric (the node
        fails like an empty run, so a sweep where every config crashed can't pass)."""
        from looplab.runtime.sandbox import _to_float
        scored = [(t, _to_float(t.get("metric"))) for t in (res.trials or [])]
        scored = [(t, m) for t, m in scored if m is not None]
        if not scored:
            res.metric = None
            return
        chooser = min if self.task.direction == "min" else max
        best_t, best_m = chooser(scored, key=lambda tm: tm[1])
        res.metric = best_m
        extra = best_t.get("extra_metrics") or {}
        if extra:
            res.extra_metrics = {**(res.extra_metrics or {}), **extra}
