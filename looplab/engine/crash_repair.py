"""Crash triage & self-repair context (the crash -> verdict -> informed-retry chain) for the
engine — extracted from orchestrator.py as a MIXIN: `class Engine(CrashRepairMixin, …)` inherits
these methods unchanged, so there is ZERO call-site churn and `self` here IS the engine. The
method bodies are verbatim moves and read engine attributes freely (`researcher`, `tracer`,
`_inline_repair_attempts`, `_deep_repair`, `_dep_lock`/`_dep_attempted`/`_dep_installer`,
`sandbox`), exactly as they did inside the class.

The cluster: `_triage_crash` (LLM crash-triage verdict — instance-monkeypatched by tests, which
a mixin preserves) over its single-ask half `_ask_triage` (one normalized ask; `_triage_crash` owns
the bounded re-ask of a non-answer, so patching `_triage_crash` still replaces the WHOLE decision as
every existing test expects), `_repair_error_context` (ancestral repair chain + hint directives for the
repair prompt), `_prepare_env` (dependency self-prep on ModuleNotFoundError) and its sibling
`_prepare_env_from_triage` (the same self-prep for a missing library the traceback never NAMES —
see its docstring), sharing one install tail in `_install_missing`. The rule-based fallback
`_rule_triage` and the repair-class coercion `coerce_repair_class` are imported from their
canonical home (engine/triage.py); agents/digest deps stay lazy, method-local imports."""
from __future__ import annotations

import sys
from typing import Optional

from looplab.core.llm import BudgetExceeded
from looplab.core.llm_broker import in_llm_lane
from looplab.core.models import RunState, normalize_researcher_footprint
from looplab.engine.triage import (AGENT_TRIAGE_ACTIONS, DEFAULT_TRIAGE_ACTION,
                                   UNANSWERABLE_TRIAGE_ACTION, _rule_triage, _TRIAGE_REASK_LIMIT,
                                   coerce_triage_action, is_transport_failure_verdict)
# The verification verdicts this module RENDERS (it never decides one). Two vocabularies live side
# by side in this file now and they are not the same kind of thing: `AGENT_TRIAGE_ACTIONS` above is
# what a model may answer, `REPAIR_*` below is what the engine measured. See
# `engine/repair_verify.py`'s docstring for why they must not be merged.
from looplab.engine.repair_verify import REPAIR_INERT, REPAIR_UNMET


def _accepted_kwargs(fn, candidates: dict) -> dict:
    """The subset of `candidates` that `fn` can actually be called with.

    Everything, when `fn` declares `**kwargs`; nothing it does not name otherwise. Signature
    introspection can fail on an exotic callable (a C function, a `functools.partial` chain, a
    proxy) — that answers "pass nothing", which is the safe direction: the callee keeps its
    historical prompt rather than the call blowing up and being mistaken for a provider outage."""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(candidates)
    return {k: v for k, v in candidates.items() if k in params}


def _format_repair_log(repair_log) -> str:
    """Render this node's in-node repair history for the stop judge: one line per attempt, oldest
    first, newest last. Pure text assembly — the engine decides WHAT the judge sees, the agent
    decides how to ask about it.

    Rendered here rather than in `agents/unified_agent.py` because the rows are the ENGINE's
    record of what it did, and because the deterministic rule path must be able to ignore them
    without the agent module having an opinion. Empty history renders empty, so the first attempt's
    prompt is byte-identical to what it was before the history existed."""
    rows = [r for r in (repair_log or []) if isinstance(r, dict)]
    if not rows:
        return ""
    out = ["--- WHAT HAS ALREADY BEEN TRIED ON THIS NODE (oldest first) ---"]
    for r in rows:
        # A row rebuilt from a `node_repaired` written before the change-set column existed has no
        # `changed` key at all, which is NOT the same fact as "this fix changed nothing" — and the
        # difference is exactly the one the judge is being asked to read ("is this repair chain
        # rewriting the same lines?"). Distinguish the missing key from the empty list rather than
        # letting an old log look like a circling one. See
        # `engine/evaluate.py::_durable_repair_ledger`.
        changed = ("(not recorded — this attempt predates the change-set column)"
                   if "changed" not in r
                   else ", ".join(str(c) for c in (r.get("changed") or [])) or "nothing")
        # THE ENGINE'S OWN VERDICT ON THAT ROW, said in the engine's voice. `changed: nothing` is
        # already in front of the judge and a live model DID once read it correctly — v2 node 2
        # attempt 3's rationale opens "The 3 prior attempts never actually applied any file change"
        # — but it is a passive column three of its siblings ignored. So the two verdicts that mean
        # something get a sentence.
        #
        # Rendered ONLY for `inert`/`unmet`: a `verified` row, an `unstated` one and a row with no
        # verdict at all (a legacy log, a salvage marker) all render byte-identically to what this
        # prompt has always been. Prompt text is a contract (CLAUDE.md) — a new fact earns a new
        # sentence, it does not get to reword the existing ones.
        note = ""
        if r.get("verified") == REPAIR_INERT:
            note = ("\n    THE ENGINE COMPARED THE BYTES: this attempt changed no file at all, so "
                    "the evaluation after it re-ran inputs identical to the one before it.")
        elif r.get("verified") == REPAIR_UNMET and r.get("unmet"):
            note = ("\n    the engine could not find what this fix said it would change ("
                    + ", ".join(str(u) for u in (r.get("unmet") or [])[:6])
                    + ") anywhere in what it actually changed.")
        out.append(
            f"attempt {r.get('attempt')}: failed with — {' '.join(str(r.get('error', '')).split())}\n"
            f"    the fix claimed: {str(r.get('fix', '')).strip() or '(no rationale)'}\n"
            f"    it changed: {changed} | pipeline stages passed before the failure: "
            f"{r.get('stages_passed')}{note}")
    return "\n".join(out)


class CrashRepairMixin:
    """The engine's crash-triage/repair-context cluster. See the module docstring for the mixin
    convention (`self` is the Engine)."""

    @in_llm_lane("build")
    def _triage_crash(self, state: RunState, node, error: str, attempt: int,
                      reason: str = "crash", *, repair_log=None,
                      depth: Optional[int] = None, attempts_left: Optional[int] = None) -> dict:
        """Decide what to do with a just-failed node BEFORE spending another eval:
        {"action": "repair"|"abandon"|"reject_idea"|"unanswerable"|"unreadable", "rationale": str}.

        THIS IS THE STOPPING RULE for the inline-repair loop, not merely a repair-vs-reject
        classifier. The unified agent decides (it can consult the run via its pilot tools —
        read_code / find_analogous — to judge whether nearby configs also fail, i.e. whether the
        IDEA is wrong vs the code), and `abandon` is its "I no longer know how to fix this". It is
        asked once per attempt, which costs no extra calls: the loop already made exactly one triage
        call per attempt. What changed is the EVIDENCE — `repair_log` is this node's whole repair
        history (what failed, what each fix claimed, which files it actually touched, how deep the
        pipeline got), so the model judges a trajectory instead of one traceback in isolation.

        `reason` (crash|timeout|oom) is surfaced to both paths so a timeout is triaged as "too slow
        -> reduce compute" rather than mis-read as a wrong idea (a missing KNOWN lib never reaches
        here — env-prep installs it and re-runs first). `attempts_left` is the remaining hard budget,
        told to the model so a stop and a cap-out are not the same surprise; it is advisory to the
        model and enforced by the caller regardless. It is a real number even for a run with no
        OPERATOR cap (`inline_repair_attempts = 0`), because there is still a bound in that case —
        `engine/evaluate.py::_UNLIMITED_REPAIR_CEILING` — and telling the judge `None` on exactly the
        runs carrying the loosest bound was the least useful place to be coy. `None` remains
        meaningful for a caller that genuinely has no cap to report.

        FAIL CLOSED, IN TWO DIFFERENT DIRECTIONS. A wired judge that produces no usable verdict never
        degrades to a permissive "repair" and never falls through to the deterministic rule — but the
        two ways it can fail are NOT the same condition and no longer share an answer:

          * the TRANSPORT failed (the call raised, `resilient` caught an unreachable endpoint — the
            request never completed) -> `unanswerable`, which the caller routes to the run-level circuit
            breaker. This is how the 2345-repair incident began and it is a RUN-level fact: every
            other node reaches the same endpoint.
          * the model ANSWERED something outside the vocabulary -> `unreadable`, a per-NODE stop with
            no pause. The provider is demonstrably alive, so halting the run (and, under
            `eval_parallel > 1`, every healthy sibling with it) on one bad emit is a strictly wrong
            diagnosis — it used to hand the operator the MODEL's own rationale under a "check your
            credits, key and base URL" banner.

        Both are re-asked `_TRIAGE_REASK_LIMIT` times before they are acted on: one non-answer is not
        a diagnosis, and stopping a node on the first one costs a whole node for a single flapped
        socket (measured: `developer.repair` calls = 0). The rule path stays reserved for the
        genuinely different case of no judge being wired at all."""
        # Tag the failure kind so the LLM agent (and the rule's marker scan) see crash vs timeout.
        tagged = f"[failure kind: {reason}]\n{error}"
        fn = getattr(self.researcher, "triage_crash", None)
        if callable(fn):
            verdict = None
            # THE RE-ASK. `_TRIAGE_REASK_LIMIT` extra rounds, and only ever for a non-answer: a real
            # verdict (repair/abandon/reject_idea) returns from inside the loop on the first pass, so
            # the healthy path still costs EXACTLY one call per attempt, which is what "the stop
            # decision is free" depends on. The extra call is charged only when the alternative is
            # ending the node on an answer nobody could read.
            for _round in range(1 + _TRIAGE_REASK_LIMIT):
                verdict = self._ask_triage(fn, state, node, tagged, attempt, reason,
                                           repair_log, depth, attempts_left)
                if verdict["action"] in AGENT_TRIAGE_ACTIONS:
                    return verdict
            return verdict
        # NO judge wired (`unified_agent` off) — a configuration, not a failure. The deterministic
        # rule keeps repairing mechanical crashes, bounded ONLY by the operator's hard cap, because
        # it has no way to form the stop judgement the model makes. 0 = unlimited -> a large cap.
        cap = self._inline_repair_attempts or 10**9
        return _rule_triage(reason, error, attempt, cap)

    def _ask_triage(self, fn, state: RunState, node, tagged: str, attempt: int, reason: str,
                    repair_log, depth, attempts_left) -> dict:
        """ONE ask of the wired judge, normalized to a `TRIAGE_ACTIONS` verdict.

        Split out of `_triage_crash` so the re-ask above is a loop over a single, total function
        rather than a duplicated forty-line try block — and so the three ways an ask can end
        (a real verdict, a transport failure, an answer nobody can read) have one place each instead
        of being reachable only by driving a whole sandboxed eval against a broken endpoint."""
        try:
            from looplab.agents.roles import _state_brief
            from looplab.agents.hints import render_hint_directives
            try:
                # NOT a proposal: this asks for a `TRIAGE_ACTIONS` verdict, not an `Idea`, so the
                # board's claim contracts are instructions it cannot follow (see `_state_brief`).
                brief = _state_brief(state, None, for_proposal=False)
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
            # `triage_crash` is a DUCK-TYPED seam (any object wired as `researcher` may
            # implement it), and this change added three keyword arguments to it. Passing them
            # unconditionally makes an implementation written against the old signature raise
            # TypeError — which the fail-closed handler below would then read as a dead provider
            # and use to stop the node and pause the RUN. That is the worst possible way for a
            # signature change to land, so the call is narrowed to what the callee actually
            # accepts. A `**kwargs` implementation gets everything, as before.
            extra = {"history": _format_repair_log(repair_log),
                     "stages_passed": depth, "attempts_left": attempts_left}
            with self.tracer.span("triage", attempt=attempt, reason=reason):
                out = fn(node, tagged, attempt, state=state, brief=brief,
                         **_accepted_kwargs(fn, extra))
            if isinstance(out, dict) and out.get("action") in AGENT_TRIAGE_ACTIONS:
                # `missing_dependency` (a library the agent says is absent) is part of the
                # verdict, so it is carried here rather than re-derived downstream. It fails
                # closed to "" = no install, and the engine never acts on it alone (see
                # runtime/deps.py::triage_install_candidates).
                return {"action": out["action"], "rationale": str(out.get("rationale", ""))[:300],
                        "missing_dependency": str(out.get("missing_dependency", ""))[:100]}
            # A TRANSPORT FAILURE OBSERVED ONE LAYER DOWN. `UnifiedAgent.triage_crash`'s `_fallback`
            # runs when `resilient` contained an unreachable endpoint / a 401 / a loop that never
            # emitted, and it stamps `TRIAGE_TRANSPORT_FAILURE_KEY` on the verdict it returns. That
            # marker — never the action string — is what admits `unanswerable` from a return value,
            # so a live model echoing the word cannot claim its own unreachability and trip the
            # run-level breaker. Its rationale passes through instead of being re-wrapped in
            # "returned no usable verdict (…)", because it is a real report, not a malformed one.
            if is_transport_failure_verdict(out):
                return {"action": UNANSWERABLE_TRIAGE_ACTION,
                        "rationale": (str(out.get("rationale", ""))[:300]
                                      or "the triage model did not return a verdict"),
                        "missing_dependency": ""}
            # A WIRED, LIVE JUDGE THAT ANSWERED SOMETHING OUTSIDE THE VOCABULARY. Coerced through the
            # registry, which fails closed to `unreadable` rather than inventing "repair" — tonight's
            # watchdog verification found the mirror-image bug (an unparseable verdict read as
            # transparent) and it was a real break. `unreadable` can also arrive ALREADY SPELLED,
            # from `UnifiedAgent._finalize`'s own coercion of an out-of-enum emit; that one is not
            # malformed, it is the same verdict reached one layer down, so its rationale (the model's
            # own words about the node) passes through rather than being re-wrapped.
            _raw = (out or {}).get("action") if isinstance(out, dict) else None
            _why = (str(out.get("rationale", ""))[:300] if _raw == DEFAULT_TRIAGE_ACTION
                    else f"the triage model returned no usable verdict ({out!r})"[:300])
            return {"action": coerce_triage_action(_raw),
                    "rationale": _why or "no verdict returned", "missing_dependency": ""}
        except BudgetExceeded:      # the hard budget stop must propagate, not degrade to a verdict
            raise
        except Exception as exc:  # noqa: BLE001 - a WIRED judge whose CALL failed is a provider
            # outage, not a licence to keep repairing. This used to fall through to `_rule_triage`,
            # which answers "repair" for any mechanical crash while attempts remain — so a dead
            # endpoint kept the loop running at full speed with no LLM in it at all. This is the
            # engine's own observation of the transport, so it is `unanswerable` directly rather
            # than through the coercion (which rejects that word on purpose).
            return {"action": UNANSWERABLE_TRIAGE_ACTION,
                    "rationale": f"{type(exc).__name__}: {exc}"[:300],
                    "missing_dependency": ""}

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
        # Repair-side twin of the Layer-4 proposal cue. Explicit footprints own the device count;
        # an unspecified footprint retains the historical parallel single-device rule.
        footprint = normalize_researcher_footprint(
            getattr(getattr(node, "idea", None), "footprint", None))
        declared_gpus = (footprint.get("gpus") if footprint is not None
                         and "gpus" in footprint else None)
        if getattr(self, "_repo_spec", None) and declared_gpus is not None:
            if declared_gpus == 0:
                error += ("\n[hardware: this node declares `footprint.gpus=0` and runs CPU-only. "
                          "Keep CUDA/GPU requirements disabled in every repaired command.]")
            else:
                error += (f"\n[hardware: this node reserved exactly {declared_gpus} GPU(s). Every "
                          f"training/eval command must target exactly {declared_gpus} device(s); keep "
                          "that count unchanged across repairs.]")
        elif (getattr(self, "_repo_spec", None) and self._eval_parallel > 1
              and getattr(self, "_gpu_ids", None)):
            error += ("\n[hardware: this legacy unspecified-footprint node is pinned to exactly ONE "
                      "GPU for parallel eval. Keep every training/eval command at one device.]")
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
        if reason == "diverged":
            # The directive that must be said FIRST is the negative one. This kill and an OOM kill are
            # the same SIGKILL with the same absent traceback, so a model reading only the exit code
            # reaches for the memory playbook — which is what happened for three rounds on v6 node 5,
            # each one halving a batch size that was never the problem.
            return ("[failure kind: diverged]\n" + error + "\n"
                    "LoopLab's own training health-check KILLED this stage: the live log reported a "
                    "non-finite loss or grad_norm (NaN/inf) repeatedly, so the model could no longer "
                    "learn and the rest of the budget would have been wasted. This is NOT an "
                    "out-of-memory kill and NOT a timeout — do not reduce batch size, model size or "
                    "sequence length to fix it; that changes nothing about the instability and costs "
                    "another full run to find out. Return a corrected, complete change that makes the "
                    "OBJECTIVE numerically stable: lower the learning rate or lengthen the warmup, "
                    "clip gradients, add an epsilon inside every log/sqrt/division, compute the loss "
                    "in float32 even under mixed precision, guard a masked softmax against an "
                    "all-masked row and a 0*log(0), and reduce or ramp the weight of any newly-added "
                    "auxiliary/regularisation term. Keep the idea; make its arithmetic survivable.")
        if reason == "stalled":
            # Same misclassification hazard as `diverged`, opposite fix: the process was ALIVE and
            # silent, so there is nothing to read in stderr and nothing memory-shaped to cut.
            return ("[failure kind: stalled]\n" + error + "\n"
                    "LoopLab's stall watchdog KILLED this stage: it stayed alive but produced no "
                    "output for the whole stall window — a hung distributed barrier, a wedged CUDA "
                    "op, a deadlocked dataloader or a lock nobody releases. This is NOT an "
                    "out-of-memory kill and NOT a timeout; reducing batch or model size does not "
                    "unblock a deadlock. Return a corrected, complete change that either removes the "
                    "hang (a barrier every rank reaches, a bounded timeout on the blocking call, "
                    "`num_workers=0` / no fork under a multithreaded runtime, no lock held across a "
                    "collective) or makes progress observable — print or flush a heartbeat line at "
                    "least once per inner loop so the next run reports WHERE it stopped.")
        if reason == "needs_failed":
            # The one directive that must NOT say "diagnose the crash": there was no crash. The stage
            # was refused before it started, so its code is not evidence of anything yet, and the two
            # candidate repairs are both in DECLARATIONS — either the earlier stage's `expect.files`
            # or this stage's `needs`. The error text already names both sides where it can.
            return ("[failure kind: needs_failed]\n" + error + "\n"
                    "This stage never ran. It DECLARES an input file (`needs` in the stage manifest) "
                    "that was not present in the eval workdir when its turn came, so LoopLab refused "
                    "to start it rather than spend the stage's runtime on a pipeline that cannot "
                    "succeed. Do not debug this stage's code — it did not execute. Find which of the "
                    "two declarations is wrong: either an EARLIER stage writes that file somewhere "
                    "other than where it declares it (fix that stage's code or its `expect.files`), "
                    "or THIS stage's `needs` names a path the pipeline never produces (fix the "
                    "`needs` entry to the real path). Removing the `needs` entry is not a fix — it "
                    "only moves the same failure into the stage's own loader, later and more "
                    "expensively.")
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
        # A MISSING SUBMODULE OF AN INSTALLED DISTRIBUTION IS NOT A MISSING DISTRIBUTION, and pip
        # cannot tell the difference because it is never asked about the submodule. Live 2026-08-07
        # (`runs/rubert-dr-0807` node 0, round 1): the repo's `utils.py` does
        # `from pytorch_lightning.utilities.cloud_io import get_filesystem`, a module Lightning 2.x
        # DELETED, so the traceback read `No module named 'pytorch_lightning.utilities.cloud_io'`.
        # `missing_modules` reduced that to `pytorch_lightning` — installed at 2.6.5 — and
        # `pip install pytorch-lightning` answered "Requirement already satisfied" with returncode 0
        # in 2.19 s. `InstallResult.ok` is `returncode == 0`, so the engine recorded
        # `deps_installed {"packages": ["pytorch-lightning"], "round": 1}`, spent one of
        # `_MAX_DEP_ROUNDS`, and re-ran the node's whole stage pipeline into the byte-identical
        # exception (the next `node_repaired.error_in` is that same traceback). Worse than the wasted
        # eval: the receipt says the environment was just fixed, so the failure that follows reads as
        # the agent's code being wrong.
        #
        # The probe is confined to the DOTTED-ONLY names on purpose. A bare `No module named 'torch'`
        # keeps today's behaviour byte-for-byte, no extra interpreter spawn and no new way to fail:
        # `is_present` fails closed to "present" = "do not install", and failing closed on the fresh-box
        # case (nothing installed yet, which is the reason this module exists at all) would be a
        # regression dressed as a safety check. Here the direction is right — doubt means "leave it to
        # the repair path", which is where a version mismatch belongs anyway.
        suspects = [m for m in candidates if m in deps.submodule_only_modules(stderr)]
        if suspects:
            python = getattr(self.sandbox, "python", sys.executable)
            present = {m for m in suspects if deps.is_present(m, python=python)}
            candidates = [m for m in candidates if m not in present]
        return self._install_missing(candidates)

    def _install_missing(self, candidates: list[str]) -> list[str]:
        """Install the ALLOWLISTED import names in `candidates` that this run has not already tried,
        returning the pip packages that installed cleanly. The shared tail of both env-prep entry
        points — the traceback-driven `_prepare_env` and the triage-driven
        `_prepare_env_from_triage` — so `_dep_lock`, the once-per-module `_dep_attempted` cache, the
        injected installer seam and the install span have exactly one implementation."""
        from looplab.runtime import deps
        if not candidates:
            return []
        with self._dep_lock:
            mods = [m for m in candidates if m not in self._dep_attempted]  # re-check inside the lock
            if not mods:
                return []
            python = getattr(self.sandbox, "python", sys.executable)
            installer = self._dep_installer or deps.install
            # What the REPO says about these distributions. Read once per call, from the same cached
            # Declaration `_ensure_run_setup` installed from, so the run cannot install one set of
            # pins and enforce another. `None` for a non-repo task — then this whole block is inert
            # and the behaviour below is byte-for-byte what it was.
            decl = self._declared_deps()
            installed: list[str] = []
            for mod in mods:
                self._dep_attempted.add(mod)    # one pip attempt per module per run (success or fail)
                pkg = deps.pip_package(mod)
                # AN INSTALL MUST NOT SILENTLY MOVE A PACKAGE THE REPO PINNED. `pip install
                # pytorch-lightning` against a repo pinning `pytorch_lightning==1.5.1` is how
                # `runs/rubert-dr-0804` acquired Lightning 2.6.5, and the Lightning-2.x SHAPE is then
                # all over `rubert-dr-0807`'s repairs — read off its event log 2026-08-07, BOTH failing
                # nodes carry it: a `pytorch_lightning/trainer/configuration_validator.py ...
                # NotImplementedError` repair each, node 0's terminal is the DDP `_rebuild_buckets`
                # RuntimeError, and node 2 carries a second "It looks like your Lightning ..." one.
                # (An earlier note put it at 7 of that run's 12 repairs; that count is NOT re-derived
                # here — the durable `error_in` tails are truncated, so treat it as the shape being
                # pervasive rather than as a figure.) So when the declaration names this distribution,
                # pip is handed the operator's own line VERBATIM rather than a bare name. Verbatim
                # matters twice: LoopLab never has to parse a specifier grammar, and the receipt below
                # quotes exactly what the repo wrote. Measured 2026-08-07 in a tmpfs venv: the declared
                # 1.5.1 imports against this container's torch 2.4.0 and HAS
                # `pytorch_lightning.utilities.cloud_io.get_filesystem` — the module 2.x deleted and
                # the one node 0 died on in round 1.
                #
                # WHY NOT `-c requirements.txt`. A constraints file would bind the transitive
                # resolution too, which is strictly stronger — and pip cannot read this repo's file
                # at all. Measured 2026-08-07 against the live testbed's own requirements.txt:
                #     ERROR: Constraints cannot have extras
                # because it declares `bm25s[full]`. A mechanism that fails closed on the very file
                # it exists to honour is not a mechanism; per-package resolution degrades to today's
                # behaviour for a line it cannot use, which is the right direction.
                pin = deps.declared_requirement(mod, decl)
                req = pin or pkg
                before = deps.installed_version(pkg, python=python)
                try:
                    with self.tracer.span("install_dep", package=pkg, requirement=req):
                        res = installer(req, python=python, timeout=self._dep_install_timeout)
                except Exception:  # noqa: BLE001 - a misbehaving installer must degrade to "not installed",
                    res = None     # not crash the eval; the node then flows to normal triage/repair.
                if getattr(res, "ok", False):
                    installed.append(pkg)
                    # The before/after pair is the whole reportability story for an install: without
                    # it the log says `pytorch-lightning` was installed and an operator still cannot
                    # tell whether that was a no-op or a two-major-version move. Recorded on the
                    # engine for `_evaluate` to stamp onto `deps_installed` — the append site is
                    # there because that is where the write lock and the node/generation key are.
                    self._dep_receipts[pkg] = {
                        "package": pkg, "requirement": req, "declared": pin,
                        "before": before, "after": deps.installed_version(pkg, python=python)}
            return installed

    def _drain_dep_receipts(self, packages: list) -> list:
        """The per-package install receipts for `packages`, REMOVED from the pending map.

        `_install_missing` runs in a worker thread and records `{requirement, declared, before,
        after}` per package; `_evaluate` stamps them onto `deps_installed` under `_write_lock`.
        Draining rather than reading means a later round in the same node cannot re-report an
        install that already has a receipt on the log — the rows would then disagree about how many
        times a package moved.

        Ordered by `packages` (the caller's own order) so the receipt list lines up index-for-index
        with the `packages` field beside it. A package with no receipt is simply absent rather than a
        `None` hole: the only way to get here without one is an injected test installer, and a list
        of nulls would read as "we looked and found nothing" rather than "we did not look"."""
        pending = getattr(self, "_dep_receipts", None)
        if not pending:
            return []
        out = [pending.pop(p) for p in (packages or []) if p in pending]
        return out

    def _prepare_env_from_triage(self, triage: dict, stderr: str) -> list[str]:
        """Environment self-prep for a missing library the TRACEBACK NEVER NAMES.

        `_prepare_env` above can only install what the traceback reports as missing, which misses
        the whole class of failures where a library DEGRADES an absent optional dependency into
        something else. Live 2026-08-05 (`runs/rubert-dr-0805` node 0): `transformers` guards
        `init_empty_weights`/`find_tied_parameters` behind `is_accelerate_available()`, so an absent
        `accelerate` — already on the install allowlist — surfaced as a bare
        `NameError: name 'init_empty_weights' is not defined` with the word "accelerate" nowhere in
        the exception. Env-prep saw nothing installable, and the node spent TWO of its six repair
        attempts hand-patching symbols instead. The crash-triage agent named the cause correctly
        both times and had no way to act on it.

        So the agent's verdict becomes an admissible SOURCE of the name — never on its own:
        `deps.triage_install_candidates` requires the traceback to be unresolved-name shaped and the
        triage to point at that same traceback (its docstring is the contract), and we additionally
        install only what is provably ABSENT from the eval interpreter. Returns the pip packages
        installed (empty => nothing to do, and the caller proceeds to a normal code repair)."""
        from looplab.runtime import deps
        named = deps.triage_install_candidates(
            str(triage.get("missing_dependency", "") or ""),
            str(triage.get("rationale", "") or ""), stderr or "")
        if not named:
            return []
        python = getattr(self.sandbox, "python", sys.executable)
        # The decisive filter, and the one the agent cannot influence: a package that is already
        # importable is not the cause of anything, so naming it installs nothing. Probed against the
        # EVAL interpreter (`find_spec`, no import) and fail-closed to "present" on any doubt.
        # `_dep_attempted` is consulted FIRST (an unlocked read; `_install_missing` re-checks it
        # under `_dep_lock`) purely to skip the probe for a module this run already tried — an agent
        # that keeps naming the same failed install would otherwise spawn one per repair.
        absent = [m for m in named
                  if m not in self._dep_attempted and not deps.is_present(m, python=python)]
        return self._install_missing(absent)
