"""Crash triage & self-repair context (the crash -> verdict -> informed-retry chain) for the
engine — extracted from orchestrator.py as a MIXIN: `class Engine(CrashRepairMixin, …)` inherits
these methods unchanged, so there is ZERO call-site churn and `self` here IS the engine. The
method bodies are verbatim moves and read engine attributes freely (`researcher`, `tracer`,
`_inline_repair_attempts`, `_deep_repair`, `_dep_lock`/`_dep_attempted`/`_dep_installer`,
`sandbox`), exactly as they did inside the class.

The cluster: `_triage_crash` (LLM crash-triage verdict — instance-monkeypatched by tests, which
a mixin preserves), `_repair_error_context` (ancestral repair chain + hint directives for the
repair prompt), `_prepare_env` (dependency self-prep on ModuleNotFoundError). The rule-based
fallback `_rule_triage` is imported from its canonical home (engine/triage.py); agents/digest
deps stay lazy, method-local imports."""
from __future__ import annotations

import sys
from typing import Optional

from looplab.core.llm import BudgetExceeded
from looplab.core.models import RunState
from looplab.engine.triage import _rule_triage


class CrashRepairMixin:
    """The engine's crash-triage/repair-context cluster. See the module docstring for the mixin
    convention (`self` is the Engine)."""

    def _triage_crash(self, state: RunState, node, error: str, attempt: int,
                      reason: str = "crash") -> dict:
        """Decide what to do with a just-failed node BEFORE spending another eval:
        {"action": "repair"|"abandon"|"reject_idea", "rationale": str}. Base mode: the unified
        agent decides (it can consult the run via its pilot tools — read_code / find_analogous —
        to judge whether nearby configs also fail, i.e. whether the IDEA is wrong vs the code).
        Falls back to a deterministic rule when no LLM triage agent is wired (unified_agent off),
        which never rejects an idea — so the feature is safe without an agent.

        `reason` (crash|timeout) is surfaced to both paths so a timeout is triaged as "too slow ->
        reduce compute" rather than mis-read as a wrong idea (a missing KNOWN lib never reaches here
        — env-prep installs it and re-runs first)."""
        # Tag the failure kind so the LLM agent (and the rule's marker scan) see crash vs timeout.
        tagged = f"[failure kind: {reason}]\n{error}"
        fn = getattr(self.researcher, "triage_crash", None)
        if callable(fn):
            try:
                from looplab.agents.roles import _state_brief
                from looplab.agents.hints import render_hint_directives
                try:
                    brief = _state_brief(state, None)
                except Exception:  # noqa: BLE001 - a brief is advisory; never block on it
                    brief = ""
                # Signal-delivery (§1): a standing directive (e.g. "prefer lighter models") is
                # relevant to the repair-vs-reject decision, so surface it to the triage agent too.
                brief += render_hint_directives(state.pending_hints)
                # Own span so the crash-triage LLM turns band as `triage`, NOT `evaluate`: triage runs
                # INSIDE the engine's `evaluate` span, so without this its (often many, agentic) turns
                # inherit phase=evaluate and inflate the "evaluate" band with failure-debugging that has
                # nothing to do with scoring — the exact "why is there a big eval when it never scored?"
                # confusion. (The repair itself already has its own `inline_repair` span.)
                with self.tracer.span("triage", attempt=attempt, reason=reason):
                    out = fn(node, tagged, attempt, state=state, brief=brief)
                if isinstance(out, dict) and out.get("action") in ("repair", "abandon", "reject_idea"):
                    return {"action": out["action"], "rationale": str(out.get("rationale", ""))[:300]}
            except BudgetExceeded:      # the hard budget stop must propagate, not degrade to the rule
                raise
            except Exception:  # noqa: BLE001 - agent triage is best-effort; fall through to the rule
                pass
        # 0 = unlimited attempts -> pass a large cap so the rule path keeps repairing mechanical
        # crashes (the anti-stuck guard, not a count, stops a genuinely stuck node).
        cap = self._inline_repair_attempts or 10**9
        return _rule_triage(reason, error, attempt, cap)

    def _repair_error_context(self, reason: str, error: str,
                              state: Optional[RunState] = None, node=None) -> str:
        """Error context handed to Developer.repair(). A timeout gets an explicit cost-reduction
        directive (the code was too slow, not wrong — shrink it to fit the budget). With deep_repair
        (C3) a crash is enriched with the failure taxonomy + a 'reproduce then fix' directive; else
        the raw tail. Shared by the inter-node debug operator and the inline (in-node) repair loop.

        M1/A0c: when `state`+`node` are given, the ANCESTRAL REPAIR CHAIN of the lineage is
        prepended (aira-dojo MEM_OPS `ancestral`) — prior fixes and what they hit — so a repair
        doesn't oscillate undo↔redo with an earlier one."""
        chain = ""
        if state is not None and node is not None:
            from looplab.events.digest import ancestral_repair_chain
            chain = ancestral_repair_chain(state, node)
            if chain:
                chain += "\n\n"
        error = chain + (error or "")
        if reason == "timeout":
            # Don't quote a specific budget here: the wall-clock varies by node kind (a sweep node gets
            # timeout×sweep_timeout_mult; a RepoTask uses its own per-profile timeout), so a hardcoded
            # self.timeout would be misleading. The directive — cut compute — is what matters.
            return ("[failure kind: timeout]\n" + error + "\n"
                    "The script exceeded its evaluation time budget and was killed before it produced a "
                    "metric. The IDEA is fine — it was just too slow. Return a corrected, complete script "
                    "that finishes WELL within the budget by reducing compute: fewer estimators/boosting "
                    "rounds, fewer epochs, fewer CV folds or seeds, early stopping, a smaller/lighter "
                    "model, capped n_jobs, or a subsample — keep the approach, cut the cost.")
        if reason == "oom":
            # The OOM-kill usually leaves NO Python traceback (the kernel SIGKILLs the process — that's
            # how _failure_reason recognised it), so a "diagnose the root cause" directive has nothing
            # to read. Give the actionable memory-reduction directive instead, mirroring the timeout one.
            return ("[failure kind: oom]\n" + error + "\n"
                    "The script was KILLED by the out-of-memory killer — it exceeded the available "
                    "RAM/VRAM (e.g. a JupyterHub pod's cgroup memory limit) before producing a metric, "
                    "typically with no Python traceback. The IDEA is fine — it was just too "
                    "memory-hungry. Return a corrected, complete script that fits in LESS memory: a "
                    "smaller batch size, a lighter/smaller model, fewer features or a subsample of the "
                    "rows, gradient accumulation instead of one large batch, lower precision "
                    "(float16/bfloat16), or freeing large intermediates — keep the approach, cut the "
                    "memory.")
        if self._deep_repair:
            return (f"[failure kind: {reason or 'unknown'}]\n{error}\n"
                    "Diagnose the root cause; if it's unclear, add a tiny reproduction/"
                    "assert near the failure, then return a corrected, complete script.")
        return error

    def _prepare_env(self, stderr: str) -> list[str]:
        """Environment self-prep: pip-install the KNOWN libraries a crash reports as missing, into
        the eval interpreter, so the engine can re-run instead of rejecting the idea. Returns the
        pip packages successfully installed (empty => nothing to do / install failed -> normal
        triage). Trusted_local only (gated by the caller via `self._auto_install_deps`).

        Per-package so a partial failure only stops the bad name; `_dep_attempted` + `_dep_lock`
        make it install-once-per-module and concurrency-safe (pip mutates one shared env)."""
        from looplab.runtime import deps
        # Parse the missing KNOWN libs BEFORE taking the lock — a crash with nothing to install (the
        # common case, and every non-dep crash) must not block on `_dep_lock` while another eval holds
        # it through a multi-minute pip install (max_parallel>1). Only contend for the lock when there
        # is real installable work.
        candidates = [m for m in deps.missing_modules(stderr) if deps.is_installable(m)]
        if not candidates:
            return []
        with self._dep_lock:
            mods = [m for m in candidates if m not in self._dep_attempted]  # re-check inside the lock
            if not mods:
                return []
            python = getattr(self.sandbox, "python", sys.executable)
            installer = self._dep_installer or deps.install
            installed: list[str] = []
            for mod in mods:
                self._dep_attempted.add(mod)    # one pip attempt per module per run (success or fail)
                pkg = deps.pip_package(mod)
                try:
                    with self.tracer.span("install_dep", package=pkg):
                        res = installer(pkg, python=python, timeout=self._dep_install_timeout)
                except Exception:  # noqa: BLE001 - a misbehaving installer must degrade to "not installed",
                    res = None     # not crash the eval; the node then flows to normal triage/repair.
                if getattr(res, "ok", False):
                    installed.append(pkg)
            return installed
