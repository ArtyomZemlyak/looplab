"""Eval dispatch (run-setup gating, data binds, the eval entrypoint, sweep collapse) for the
engine — extracted from orchestrator.py as a MIXIN: `class Engine(EvalDispatchMixin, …)`
inherits these methods unchanged, so there is ZERO call-site churn and `self` here IS the
engine. The method bodies are verbatim moves and read engine attributes freely (`_eval_spec`,
`_repo_spec`, `_run_setup_lock`, `trust_mode`, `sandbox`, `store`, …), exactly as they did
inside the class. `_run_eval` is instance-monkeypatched by tests — a mixin preserves that seam.

Runtime deps (`command_eval`, `_run_argv`, `_to_float`) stay method-local so monkeypatching
through their source modules keeps working."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import anyio

from looplab.core.errors import RunSetupRefusal
from looplab.core.llm import BudgetExceeded
from looplab.core.models import (EXTRA_METRIC_AUTO, NodeStatus, RunState,
                                 apply_engine_extra_metric_channels,
                                 normalize_extra_metric_channels, normalize_extra_metrics)
from looplab.engine.evaluate import _redacted_tail, handed_admission_fold
from looplab.engine.resources import eval_time_admission_blocked
from looplab.engine.shared import effective_researcher_eval_timeout
from looplab.engine.speculation_gate import engine_authored_artifacts
# Through the ENGINE's fold seam, not `replay.fold` directly — see `shared.py::engine_fold`.
from looplab.engine.shared import engine_fold as fold
from looplab.events.types import (EV_NODE_FAILED, EV_RUN_SETUP_FINISHED, EV_RUN_SETUP_STARTED,
                                  SETUP_THREAD_APPENDABLE)
from looplab.runtime import applied_params, effective_batch, metric_inputs

# THE engine sentinel (engine/options.py): `_evaluate` passes it into `_run_eval` positionally
# (as `next_start`), so the identity check here MUST see the same object the orchestrator uses.
from looplab.engine.options import _UNSET

_LOG = logging.getLogger(__name__)

# How many declared requirement lines the durable `deps_declared` receipt carries. Well past any
# real file (the live dense-retrieval repo declares 21) and small enough that a pathological
# generated requirements.txt cannot bloat every reader of the event log. Overflow is COUNTED, never
# silently dropped — see `_record_declared_deps`.
_DECLARED_PINS_CAP = 400

# How many DISTINCT dependency declarations one run may install (the run's own baseline counts as
# the first). The digest gate and the seen-set already stop repetition; this bounds VARIETY, which
# is what an agent that rewrites requirements.txt every node produces. A module constant rather than
# a `Settings` field, matching its sibling bound `engine/triage.py::_MAX_DEP_ROUNDS`: both are
# runaway guards on the same shared interpreter, and neither is a knob an operator tunes — the knob
# is `auto_install_deps`, which turns the whole capability off.
_MAX_NODE_DEP_SYNCS = 8


class _DeferredBudgetStop:
    """A task-group facade that DEFERS a background task's `BudgetExceeded` instead of letting it
    cancel that group's siblings -- used by `_dispatch_evals` for exactly one caller,
    `_spawn_research`, and by the two parallel-build fan-outs (the chunked barrier in
    `_handle_create_actions` and `_steady_state_build_lane`), whose pooled builds now let the
    ceiling propagate out of `_create_node_guarded` instead of recording it as one node's
    `build_crash` (review 2026-09-22, ENG1-01). Each owner re-raises the held stop on the MAIN task
    the moment its group has joined, and each admission loop tests the sink before starting more
    work — the same clauses (a) and (b) below.

    THE DEFECT IT CLOSES is the one `Engine._drain_inflight_evaluation` documents from the campaign
    artefacts, seen on the OTHER dispatch path.  Under Card speculation the evaluation lives in the
    run-scoped `eval_tg`, so the ceiling reaches it only at `Engine.run` and a drain there is enough.
    Under `_dispatch_evals` (speculation off -- a supported configuration) the evaluation is awaited
    INSIDE the same `bg_tg` the overlapped research runs in, so a research task that raises cancels
    the evaluation directly, at the first checkpoint after its shielded worker thread returns: the
    score is on disk and its `node_evaluated` is never written.  Same loss, one frame lower.

    IT DOES NOT WEAKEN THE STOP, and the three things that make that true are all outside this
    class.  (a) `_dispatch_evals` re-raises the captured exception the instant its evaluations have
    joined -- unconditionally, unwrapped, with its own message, so the CLI still records
    `run_finished {"reason": "budget_exhausted"}`.  (b) Both admission loops test the sink BEFORE
    starting another evaluation, so a deferred stop starts no new work; it only finishes work
    already paid for.  (c) Nothing in the drain window can spend anyway: `CostAccountant.add` is
    already over the ceiling, so the very next priced call raises again.

    Only `start_soon` is intercepted.  Everything else -- `cancel_scope` above all, which
    `_dispatch_evals`'s `finally` uses to stop the repeating research loop -- is the real group's.

    THE SAME LOSS FROM INSIDE AN EVALUATION (`eval-raised-ceiling-still-cancels-sibling-terminals`,
    doc 57). A ceiling crossed by an evaluation's OWN paid bookkeeping -- its triage, repair, stage
    check or critic; clause (c) above is also the mechanism, since a repeat-research capture leaves
    the run pinned over ceiling for hours of eval -- leaves an eval CHILD task, not the research, and
    this facade never saw it. A note here recorded that seam as closed on 2026-09-08 by two changes,
    and it was not (review 2026-09-22, ENG2-02): `accountant_over_ceiling` lets
    `Engine._drain_inflight_evaluation` answer for an escaping exception that carries no ceiling, and
    `evaluate.py::_land_terminal_before_ceiling` lands the RAISING node's own terminal -- but a child's
    raise cancels the eval group ITSELF, so every sibling is cancelled at its next checkpoint and the
    drain, entered inside that cancelled scope, cannot wait for one of them. The test that proved the
    closure raised from the HOST body, where the scope is still live; driven in the child shape, the
    sibling's score was on disk and its terminal was gone.

    WHAT CLOSES IT is this facade's own move, one level down: an eval-child wrapper DEFERS a pure
    spend ceiling instead of raising it into its group (`core/errors.py::deferrable_budget_stop`) --
    `_dispatch_evals`' `_eval_in_slot` into this same `budget_stop` sink, the Card path's
    `speculation.py::_card_eval_one` into the run-level `_eval_budget_stop` -- admission refuses
    while one is held, and the owner re-raises it after the siblings have landed: `_dispatch_evals`
    after its join, `_run_with_llm_broker` after `_drain_adopted_evals`
    (`speculation.py::_raise_deferred_eval_budget_stop`). Driven by
    `tests/test_budget_ceiling_drains_the_inflight_eval.py` (the child shape through the real
    wrappers) and `tests/test_card_eval_ceilings.py` (a real Card-mode `Engine.run`). The two lesser
    hardenings that landed on 2026-09-08 stand: `start_soon` forwards anyio's `name=`, and `start()`
    is REFUSED rather than passed through uncaptured.
    """

    __slots__ = ("_tg", "_sink")

    def __init__(self, tg, sink: list):
        self._tg = tg
        self._sink = sink

    def start_soon(self, func, *args, name=None) -> None:
        # `name=` is FORWARDED and not dropped: anyio puts it on the task object and every stall
        # report, `anyio` traceback and task dump reads it, so a facade that silently swallowed it
        # renamed the one deferred task in the group to `None` exactly when the run is ending and
        # somebody is reading the dump to find out why.
        sink = self._sink

        async def _capture() -> None:
            try:
                await func(*args)
            except BudgetExceeded as exc:
                sink.append(exc)         # re-raised by `_dispatch_evals` once the evals have joined

        self._tg.start_soon(_capture, name=name)

    async def start(self, *args, **kwargs):
        """REFUSED, loudly. `__getattr__` used to forward this to the real group, so a caller that
        needed the started-value handshake got a task whose `BudgetExceeded` was NOT captured --
        the whole defect this facade exists to close, re-introduced by a one-word change at a call
        site and invisible at this one. Capturing it here is not the answer either: `start()`'s
        contract is that the task calls `task_status.started()` before the await returns, and a
        raise BEFORE that point has to reach the caller to unblock it, so a facade cannot both
        defer the exception and honour the handshake. Say so instead of choosing one silently."""
        raise TypeError(
            "_DeferredBudgetStop cannot defer a `tg.start()` task (its started-value handshake and "
            "a deferred BudgetExceeded are mutually exclusive) -- use start_soon")

    def __getattr__(self, name):
        return getattr(self._tg, name)


# Bounded aging for continuous eval dispatch (`_dispatch_evals`). After this many consecutive
# bypasses the queue head gets exclusive claim on GPU releases, so a wide request stops losing every
# partial release to the small jobs behind it. The claim ends the moment the head is admitted, or —
# see the scan — when the pool has fully drained and the head STILL does not fit, which proves it
# wants more than the box physically has and must not be allowed to wedge the batch.
_HEAD_BYPASS_LIMIT = 3


def budget_stop_recheck(budget_stop: list) -> bool:
    """Did the spend ceiling fire while the SERIAL dispatcher was waiting for resources?

    `_dispatch_evals`' serial branch tests its deferred-stop sink at the top of the queue loop and
    nowhere else, and the resource wait below that test can last HOURS: the host GPU-pool lease
    waits on every co-hosted run (CLAUDE.md: "potentially for hours"), re-folding every 0.5 s and
    re-checking the LIFECYCLE gates — never the ceiling. A `BudgetExceeded` the overlapped research
    captured during that wait was therefore never consulted, and one more evaluation was admitted
    and STARTED on a run that was already over (docs/57 `serial-dispatch-admits-one-eval-past-the-
    ceiling`): GPU burnt for nothing, and its first paid call then raised the ceiling from INSIDE
    the eval, the terminal-losing shape the drain does not cover. The parallel branch gates at its
    refill point for exactly this reason; this is that gate for the serial wait, asked at the two
    instants a captured stop can first be seen — the head of each wait tick, and the moment a
    reservation comes back — because the sink is appended by a task on the same event loop, so it
    can only move across an `await`.

    A named predicate rather than an inline `if budget_stop:` so the rule has a home a test can
    point at; the dispatch test could not see the window before, because its host had no
    `_wait_reserve_node_resources` and the `hasattr` skipped the whole wait.
    """
    return bool(budget_stop)


def _run_terminal_gate(state) -> bool:
    """Whether the RUN has stopped accepting new eval work (doc 25 ES-06).

    Written out at three eval-dispatch sites. `getattr` defaults are kept: two of the three read a
    state the caller re-folded mid-loop, and a folded `RunState` always carries these three, so the
    defaults cannot change a live decision — they only keep a hand-built test stub from raising.

    On a folded state it IS `RunState.halted` (review 2026-09-22, ENG1-11), the one spelling every
    other gate now reads; it stays spelled out here only for those stubs, and
    `tests/test_run_state_halted.py` pins the two to the same truth table.
    """
    return bool(
        getattr(state, "paused", False)
        or getattr(state, "finished", False)
        or getattr(state, "stop_requested", None)
    )


def _eval_admission_current(state, node, generation, max_es) -> bool:
    """Whether *node* may still be handed to an eval, re-checked after a bounded wait.

    The same eight clauses were spelled at three dispatch sites — twice affirmatively and once
    NEGATED inline — so the serial and parallel branches could drift on what "still admissible"
    means while every test stayed green (doc 25 ES-06). Each clause is load-bearing:

    * `node is None` / `attempt != generation` — the node was rebuilt while we waited, so the
      reservation belongs to a lifecycle that no longer exists.
    * `status is not pending` — something already terminated it; a second eval would breach the
      one-terminal-event-per-node invariant.
    * `tombstoned` / `id in aborted_nodes` — operator or engine withdrew it mid-wait.
    * the run terminal gate — pause/stop/finish landed during the wait.
    * `total_eval_seconds >= max_es` — the run's eval budget was spent while we waited.

    Returns True only when ALL hold; callers keep their own refusal handling, which genuinely
    differs per branch (skip / drop the candidate / stop admitting entirely).
    """
    return bool(
        node is not None
        and node.attempt == generation
        and getattr(node, "status", NodeStatus.pending) is NodeStatus.pending
        and not getattr(node, "tombstoned", False)
        and node.id not in state.aborted_nodes
        and not _run_terminal_gate(state)
        and not (max_es is not None and state.total_eval_seconds >= max_es)
    )


def _eval_time_admission_refused(engine, state, node, max_es) -> bool:
    """Whether the run's eval-second allowance refuses ONE MORE evaluation lane right now.

    The dispatcher's own half of `resources.py::eval_time_admission_blocked` (doc 27
    `eval-lanes-admit-without-reserving-time`): it reads the three numbers the rule needs off the
    live fold, off the in-flight reservation ledger and off the lane's own worst case, so every
    admission site asks the SAME question rather than each re-spelling `total_eval_seconds >=
    max_es` — which is how the three sites came to enforce a per-lane ceiling while the ledger they
    all charge is per-RUN. `node` is the candidate when the site has already chosen one and None at
    the sites that gate before the scan; the estimate is then the run-level per-eval budget.

    THE LEDGER IS READ THROUGH A BOUND-METHOD LOOKUP, and the reason is the same one the two
    `hasattr(self, "_…_reserve_node_resources")` guards below carry: `_dispatch_evals` is driven
    in the suite by stub hosts that own none of `ResourceSchedulingMixin`, and on such a host there
    is no ledger to read — `reserved` is 0.0 and the rule degrades to exactly the historical
    completed-time gate. What keeps that fallback from silently becoming the PRODUCT behaviour (the
    `getattr`-default drift `engine/attribute_sites.py` is a census of) is that the property is
    driven over a real `Engine` rather than pinned in source:
    `tests/test_eval_time_reservation.py` admits one lane and asserts the next is refused while it
    is still running, which no stub can satisfy.
    """
    reserved_seconds = getattr(engine, "_reserved_eval_seconds", None)
    estimate_seconds = getattr(engine, "_eval_seconds_estimate", None)
    return eval_time_admission_blocked(
        float(getattr(state, "total_eval_seconds", 0.0) or 0.0),
        float(reserved_seconds()) if callable(reserved_seconds) else 0.0,
        float(estimate_seconds(node)) if callable(estimate_seconds) else 0.0,
        max_es,
    )


def _reserve_eval_time(engine, node_id, generation, node) -> None:
    """Commit one lane's worst-case eval charge against the run's allowance (the ledger half of
    `_eval_time_admission_refused`, reached the same way and for the same stub-host reason)."""
    reserve = getattr(engine, "_reserve_eval_seconds", None)
    estimate = getattr(engine, "_eval_seconds_estimate", None)
    if callable(reserve) and callable(estimate):
        reserve(node_id, generation, estimate(node))


def _release_eval_time(engine, node_id, generation) -> None:
    """Hand the unused portion of that commitment back. Always paired with `_reserve_eval_time` in
    a `finally`, because a leaked reservation is a run that stops admitting lanes forever."""
    release = getattr(engine, "_release_eval_seconds", None)
    if callable(release):
        release(node_id, generation)


class EvalDispatchMixin:
    """The engine's eval-dispatch cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    # THE LATCHED REFUSAL of this process's run-level setup (review 2026-09-22, ENG2-08): the
    # `RunSetupRefusal` `_do_run_setup` raised, or None. Class-level, like `_eval_budget_stop` beside
    # it in `speculation.py`, so the engine doubles the suite builds without `Engine.__init__`
    # (`tests/test_events_replay.py::_fake_eval_engine`) read the same default. Held by the ENGINE
    # OBJECT, exactly like `_run_setup_done`, and never durable on purpose: a resume (a new process,
    # a new Engine) is the operator's "I fixed it", and the fold records `run_setup_done` only for a
    # SUCCESSFUL command, so the resumed run runs the setup again.
    _run_setup_refusal: Optional[BaseException] = None

    def _ensure_run_setup(self) -> None:
        """Run the eval's RUN-LEVEL `run_setup` exactly ONCE, before the first eval — e.g. a one-time
        dependency install into the shared interpreter (the autonomy default when deps are stable
        across experiments). Distinct from per-node `setup`, which reinstalls before EVERY eval. Runs
        in the first editable repo's SOURCE dir so `-r requirements.txt` resolves; output streams to
        `run_setup.log`. A non-zero/timed-out run_setup ABORTS the run (the env would be unusable).
        Only in trusted_local (an untrusted/docker eval is a fresh container — use per-node `setup`).
        When `run_setup` is unset the command is DERIVED from what the repo declares
        (`_settle_declared_deps`) — LoopLab preparing its own environment is the product promise, and
        an operator who has to know their repo ships a `requirements.txt` is the bug. No-op only when
        the repo declares nothing we act on. The in-process guard is set only AFTER a successful install, so
        a concurrent worker on the lock-free fast path blocks on the lock rather than racing ahead to
        evaluate against a half-installed interpreter.

        Honest delivery contract: EXACTLY-once for a command that reported success, AT-LEAST-once
        across a kill that lands between `run_setup_started` and `run_setup_finished`. LoopLab cannot
        make an arbitrary operator command transactional, and refusing to re-run would leave the
        interpreter permanently half-installed with no way forward, so the repeat happens — but it is
        stamped `after_interrupted_attempt` in the log instead of being indistinguishable from a first
        attempt. Give `run_setup` an idempotent command (a plain `pip install` is) whenever the repeat
        would otherwise cost money or mutate shared state.

        A FAILURE IS LATCHED, so the setup runs at most once per Engine (review 2026-09-22,
        ENG2-08). The in-process guard below is set only on SUCCESS, which is right for the
        evaluations queued on `_run_setup_lock` while the install runs — and was wrong for them the
        moment it FAILED: each one took the lock in turn, found the flag still False and launched the
        same failing install again, serially, into the shared interpreter. The refusal is now kept
        on `_run_setup_refusal` and every later caller — a waiter on the lock or an evaluation that
        arrives afterwards — raises it without running anything. It is a `RunSetupRefusal`, which
        `_evaluate` treats as a deliberate stop and the eval-child wrappers defer to their owner, so
        the run ENDS on it rather than terminalizing and pausing (see `core/errors.py`)."""
        if self._run_setup_done:
            return
        # Serialize the check-then-set: parallel eval worker threads would otherwise all see
        # _run_setup_done == False and launch pip (not concurrency-safe) N times into one interpreter.
        with self._run_setup_lock:
            if self._run_setup_done:
                return
            # …and a waiter that queued here while the install FAILED — or any evaluation that
            # arrives after it — stops on that failure without running anything.
            self._raise_latched_run_setup_refusal()
            cmd = self._settle_declared_deps(list((self._eval_spec or {}).get("run_setup") or []))
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
            # Set the in-memory guard only AFTER the install SUCCEEDS. The install runs while THIS lock
            # is held, so a concurrent worker that reached the lock-free fast path (top) still sees the
            # flag False mid-install and blocks on `_run_setup_lock` here — waiting for the install to
            # finish instead of racing ahead and evaluating against an unprepared interpreter. A failed
            # install leaves the flag False and LATCHES its refusal (the docstring's last
            # paragraph): the waiters stop on it and the run aborts, and since the fold records
            # run_setup_done only for exit_code==0, the resume the refusal asks for runs it again.
            # The Declaration travels only when the command is OURS: an operator's own `run_setup`
            # is run exactly as written or not at all, so it is never rewritten into a reduced form.
            try:
                self._do_run_setup(cmd, declared=(self._declared_deps()
                                                  if getattr(self, "_deps_setup_derived", False)
                                                  else None))
            except RunSetupRefusal as exc:
                self._run_setup_refusal = exc
                raise
            self._run_setup_done = True

    def _raise_latched_run_setup_refusal(self) -> None:
        """Stop on this process's failed run-level setup, if it failed — never run it again.

        A FRESH `RunSetupRefusal` per caller, chained `from` the latched one, rather than the latched
        object itself: several evaluation workers stop on it concurrently, and re-raising one
        exception object from many threads interleaves their frames into a single traceback. The
        sentence is the same one, so the CLI boundary prints the refusal once, and the original —
        with the frames of the install that actually failed — is its `__cause__`.
        """
        latched = self._run_setup_refusal
        if latched is not None:
            raise RunSetupRefusal(*latched.args) from latched

    # The closed vocabulary of what a run DID about its repo's declared dependencies. A bare string
    # is what `deps_declared.action` carries into the durable log, so a typo'd literal here would be
    # invisible to every reader; `tests/test_declared_deps.py` drives the table from this tuple.
    DECLARED_DEPS_ACTIONS: tuple[str, ...] = (
        "installed",             # nothing operator-authored; we derived and ran the `-r` install
        "operator_run_setup",    # the operator wrote their own run_setup — it wins, untouched
        "auto_install_disabled",  # auto_install_deps=false: the operator owns this environment
        "refused_untrusted_tier",  # a declaration exists but this tier runs --network none
        "nothing_declared",      # nothing we act on (`observed` may still name what we saw)
        "installed_partial",     # …minus lines no index serves; a REVERSIBLE policy, see _do_run_setup
        "node_resync",           # a node's declaration differs from the run's — the Developer's ask
        "node_resync_failed",    # …and installing it did not succeed (the node evals unchanged)
        "node_resync_capped")    # …and this run has spent `_MAX_NODE_DEP_SYNCS`; refused

    def _repo_deps_root(self) -> str:
        """The directory whose dependency declaration governs this run: the first editable repo's
        SOURCE dir. The SAME rule `_do_run_setup` uses for its cwd, and deliberately so — the derived
        command is `-r <basename>`, which only resolves if it runs where the file was found.

        EMPTY when there is no editable repo, and `find_declaration` answers "nothing" for an empty
        root. `_do_run_setup` falls back to the run dir instead, because a cwd must exist for an
        operator's arbitrary command — but a run DIRECTORY is not a repo, and reading a declaration
        out of one would let a stray `requirements.txt` beside `events.jsonl` govern installs."""
        # `getattr`, not attribute access: this is reached from `_ensure_run_setup`, whose callers
        # include non-repo tasks and the test doubles that stand in for them — an Engine without a
        # repo spec is a legitimate shape here, and reading it unguarded turned "this task declares
        # no dependencies" into an AttributeError from inside a setup thread.
        eds = (getattr(self, "_repo_spec", None) or {}).get("editables", [])
        return eds[0]["path"] if eds else ""

    def _node_deps_root(self, workdir) -> str:
        """Where the SAME repo's declaration lives inside a node's workdir — the mirror of
        `_repo_deps_root`. The single-repo shorthand mounts at the workspace root (`name == "."`);
        an `editables` entry mounts under its own subdir, so the file the agent edited is at
        `<workdir>/<name>/requirements.txt`, not at the workspace root."""
        eds = (self._repo_spec or {}).get("editables", [])
        name = (eds[0].get("name") or ".") if eds else "."
        base = Path(str(workdir))
        return str(base if name in (".", "") else base / str(name).rstrip("/"))

    def _declared_watchlist(self) -> list:
        """The distributions whose versions a receipt reports on: exactly the ones the repo
        declares. Not "everything installed" — that is thousands of rows per event and most of it is
        transitive noise; not the `run_started.env` list either, which is a FIXED 17-package
        calibration identity that happens to contain none of this repo's pins."""
        decl = self._declared_deps()
        return sorted(decl.pins) if decl.found else []

    def _env_versions(self, watch) -> dict:
        from looplab.runtime import deps
        return deps.installed_versions(watch, python=getattr(self.sandbox, "python", None)) if watch else {}

    def _env_delta_since(self, watch, before) -> dict:
        """What moved, measured against `before`. Bounded by construction — one entry per DECLARED
        distribution at most, and the declaration itself is capped at read time."""
        from looplab.runtime import deps
        return deps.version_delta(before, self._env_versions(watch))

    def _sync_node_deps(self, workdir) -> None:
        """Install this NODE's dependency declaration when the Developer changed it.

        THE DELIBERATE-CHANGE PATH. The owner's requirement is that a Developer wanting a new
        library or a different version rewrites the requirements file and the deps get reinstalled.
        The write half already worked — `requirements.txt` is an ordinary file in the edit surface
        (under the composable `repo:` default `["**/*"]`; NOT under the direct `RepoTask` default
        `["**/*.py"]` — see `_record_declared_deps`, which reports which). The read half did not
        exist: nothing re-read the file, so an edit was inert and the agent's only feedback was the
        same ImportError it started with.

        WHAT STOPS IT THRASHING THE ENVIRONMENT, in order:
          1. **A content DIGEST, not a timestamp** — the fast path. A node whose declaration is
             byte-identical to the run's does nothing at all: no set, no subprocess, no event, no
             lock. That is the overwhelmingly common case (every node inherits the file unchanged),
             so the steady-state cost of this hook is one small read and a sha256. It is deliberately
             NOT an independent CORRECTNESS bound: rule 2 below reaches the same answer for the same
             input, which is why deleting the comparison is behaviour-preserving and no test catches
             it (checked by mutation, 2026-08-07). What it buys is that the common path never
             contends for `_dep_lock` — a node that did not touch the file must not wait behind
             another node's multi-minute pip, which is the same reason `crash_repair._prepare_env`
             parses its candidates before taking that lock.
          2. **A per-run SET of digests already installed**, seeded with the run's own baseline. This
             is the rule; 1 is its shortcut. It additionally covers a node that REVERTS to an earlier
             file, so an agent oscillating between two dependency sets pays once for each rather than
             once per node. Checked and extended UNDER `_dep_lock` with 3 and the install: it is a
             check-then-act over run-global state, and two eval workers reach it concurrently.
          3. **A hard per-run cap** (`_MAX_NODE_DEP_SYNCS`). 1 and 2 bound repetition, not variety:
             an agent that appends a distinct comment line each node produces a new digest every
             time. The cap is what makes that terminate, and the refusal is RECORDED so it reads as
             a bound being hit rather than the edit being ignored.
          4. **`_dep_lock`.** pip is not concurrency-safe and this is a shared interpreter; the
             crash-time installer already serializes on this lock, so the two cannot interleave.

        WHAT THIS IS NOT: a per-node environment. The install lands in the interpreter EVERY
        concurrent eval shares, so a node that changes a version changes it under its siblings.
        That is not a new hazard — `crash_repair` has pip-installed into this same shared
        interpreter from a worker thread since it was written — but it is the reason the honest
        answer here is a per-node venv, which is deliberately out of scope. The bound above plus the
        `deps_declared` receipt make the exposure visible and finite rather than silent.

        Best-effort throughout: a failed install is recorded and the node evaluates against the
        unchanged environment (its own crash then drives the normal repair path). It must not abort
        the RUN the way a failed run-setup does — a run-setup failure means the operator's
        environment never came up, while this one means one node's speculative dependency edit did
        not take."""
        # Read BARE, as `evaluate.py` and `setup_phase.py` read it: `__init__` always assigns it. The
        # `getattr(..., False)` it replaced answered a double with a switch no real Engine settles to
        # (True on the trusted tier), and the settled default would be the one wrong fix here — a
        # double that never declared it would go on to pip-install (review 2026-09-22, ENG1-03).
        if not self._auto_install_deps:
            return
        from looplab.runtime import deps
        base = self._declared_deps()
        node_decl = deps.find_declaration(self._node_deps_root(workdir))
        if not node_decl.found or node_decl.digest == base.digest:
            return                       # gate 1: unchanged — the common path, and it costs nothing
        argv = deps.declaration_argv(node_decl, python=getattr(self.sandbox, "python", None))
        from looplab.runtime.sandbox import _run_argv
        log = str(Path(self.run_dir) / "run_setup.log")   # same log as run-setup: one install story
        # Watch the UNION of what the run declares and what this node declares: a node that DROPS a
        # pin moves that distribution too, and watching only the node's list would miss it.
        watch = sorted(set(self._declared_watchlist()) | set(node_decl.pins))
        # Gates 2 and 3 are CHECK-THEN-ACT over run-global state and share `_dep_lock` with the
        # install, so two eval workers whose nodes both changed the file cannot both pass the cap or
        # both install. Gate 1 above is what keeps this lock off the common path — a node that did
        # not touch the file must never wait behind a multi-minute pip, which is the same reason
        # `crash_repair._prepare_env` parses its candidates before contending for this lock.
        with self._dep_lock:                              # gate 4: serialize with the crash installer
            seen = self._deps_synced_digests
            if not seen and base.digest:
                seen.add(base.digest)     # the run's own baseline is the first of the allowance
            if node_decl.digest in seen:
                return                    # gate 2: this exact file was already installed this run
            seen.add(node_decl.digest)
            if len(seen) > _MAX_NODE_DEP_SYNCS:
                self._record_declared_deps(node_decl, "node_resync_capped", [])
                return                    # gate 3: variety bound
            before = self._env_versions(watch)
            rc, out, err, timed = _run_argv(argv, node_decl.root, self._dep_install_timeout,
                                            log_path=log)
            delta = self._env_delta_since(watch, before)
        if rc != 0 or timed:
            _LOG.warning("node dependency re-sync failed (exit=%s, timed_out=%s); the node evaluates "
                         "against the unchanged environment — see %s", rc, timed, log)
        # The declaration we CACHE stays the run's baseline: the node's file is what a node asked
        # for, not what the repo declares, and adopting it would make the next node's diff read
        # against a sibling's speculative edit rather than against the source tree.
        self._record_declared_deps(node_decl, "node_resync" if (rc == 0 and not timed)
                                   else "node_resync_failed", argv, delta=delta)

    def _declared_deps(self):
        """This run's `runtime.deps.Declaration`, read once and cached.

        Cached because it is consulted twice for different reasons and must give the same answer to
        both: here, to decide what to install, and at CRASH time
        (`crash_repair.py::_install_missing`), to keep a bare-name install from moving a package the
        repo pinned. If the two reads disagreed — because the Developer rewrote the file mid-run —
        the run would install one set of pins and enforce another, which is a worse failure than
        either policy alone. Lazily initialized rather than set in `Engine.__init__`: this is a
        filesystem read, ~170 test call sites construct the Engine directly, and a run whose
        `_ensure_run_setup` never fires should never pay for it."""
        decl = getattr(self, "_deps_declaration", None)
        if decl is None:
            from looplab.runtime import deps
            decl = deps.find_declaration(self._repo_deps_root())
            self._deps_declaration = decl
        return decl

    def _settle_declared_deps(self, operator_cmd: list) -> list:
        """Decide what this run installs from the repo's own declaration, record the decision, and
        return the argv `_ensure_run_setup` should run.

        THE COMPOSITION RULE: an explicit operator `run_setup` WINS ENTIRELY. Not prepended, not
        merged, not appended. Someone who spelled a setup command meant it, and all three ways of
        combining are wrong in a way the operator cannot undo — a prepend double-installs for the
        (common) operator whose command already IS `pip install -r requirements.txt`, and an operator
        who deliberately curated an environment against the repo's pins has no spelling left that
        means "do not install these". The derived command is a DEFAULT, and a default that overrides
        an explicit value is not a default.

        The three gates, in the order they are asked:
          * `auto_install_deps` (default true) is the operator's one switch for "LoopLab may change
            my environment". It already gates the crash-time installer; gating this the same way is
            what makes the switch mean one thing. Note it is ALSO where the operator says "run
            nothing" now that an empty `run_setup` no longer means that.
          * the tier: installs are `trusted_local` only, because the Docker tiers run
            `--network none` and must not mutate a shared image. A detected declaration there is a
            STATED refusal — the receipt says `refused_untrusted_tier` — rather than a silent skip,
            because "we found your pins and could not honour them" is exactly the thing an operator
            debugging a version mismatch needs to be told.
          * whether the tree declares anything we act on at all.

        Failure is LOUD by design and inherits `_do_run_setup`'s existing contract: a non-zero
        `-r` install raises and aborts the run. Degrading to "the agent will work it out from
        tracebacks" is what produced 2,345 repair attempts on `runs/rubert-dr-0804`."""
        from looplab.runtime import deps
        decl = self._declared_deps()
        if not decl.found and not operator_cmd:
            action, cmd = "nothing_declared", []          # nothing to do at all
        elif self.trust_mode != "trusted_local":
            # There IS setup to do and this tier cannot do it. Asked BEFORE "who authored the
            # command", because the answer does not depend on that: `_ensure_run_setup` has always
            # skipped an operator's own `run_setup` here too, and recording that as
            # `operator_run_setup` would claim we ran a command we did not.
            action, cmd = "refused_untrusted_tier", []
        elif operator_cmd:
            action, cmd = "operator_run_setup", list(operator_cmd)
        elif not self._auto_install_deps:
            # Reached only on a trusted tier, so `_auto_install_deps` being False here can only mean
            # the operator switched `auto_install_deps` off — a different fact from the tier refusal
            # above, and it leads to a different fix. (Read bare for `_sync_node_deps`' reason: a
            # missing attribute is no longer recorded as the operator's `auto_install_disabled`.)
            action, cmd = "auto_install_disabled", []
        else:
            action = "installed"
            cmd = deps.declaration_argv(decl, python=getattr(self.sandbox, "python", None))
        assert action in self.DECLARED_DEPS_ACTIONS
        # Whether the command about to run is OURS. Only a derived command may be retried in reduced
        # form (`_do_run_setup`); an operator's own is run exactly as written or not at all.
        self._deps_setup_derived = (action == "installed")
        self._record_declared_deps(decl, action, cmd)
        return cmd

    def _record_declared_deps(self, decl, action: str, cmd: list, *, delta=None, dropped=None) -> None:
        """Append the once-per-run `deps_declared` receipt: what the repo asked for, what we did.

        This is the row that would have made `runs/rubert-dr-0807` legible. Its log records that
        `pytorch-lightning` was installed and nothing else; the repo pinned 1.5.1, the container held
        2.6.5, and no artifact of that run carries either number. `run_started.env` does not help —
        its `libs` map is a FIXED 17-package list owned by the speculation-quality receipt validator,
        and pytorch-lightning is not in it (nor sentence-transformers, faiss, tensorboard,
        accelerate, datasets). Changing THAT list is not an option: it is the calibration identity a
        published receipt is revalidated against, so widening it silently revokes every issued
        receipt. Hence a separate, purpose-built row.

        DIAGNOSTIC (fold-ignored) and appended from the eval WORKER THREAD outside `_write_lock`,
        beside the `SETUP_THREAD_APPENDABLE` pair below. Invariant #1's question for a non-folded
        event is "does any reader key on its position?", and the answer here is no by construction:
        `speculation.py::_proposal_authority_seq` excludes `DIAGNOSTIC_EVENTS` wholesale, and
        `evaluate.py::_durable_dep_rounds` keys on `deps_installed` alone. Membership is asserted at
        the append site the way the train-monitor asserts it.

        Idempotent across resume ON THE FACTS: a re-append is skipped only when an existing row
        already carries this digest AND this action. A resume that finds a DIFFERENT digest has
        genuinely re-observed a rewritten declaration and must say so — that row is the audit trail
        for the Developer-requested change."""
        from looplab.events.types import DIAGNOSTIC_EVENTS, EV_DEPS_DECLARED
        # Bounded: a declaration is operator/agent-authored text and this row is durable. The pins
        # are the load-bearing part (they are what a version dispute is settled against), so they are
        # carried whole up to a cap, with the overflow COUNTED rather than silently dropped.
        pins = dict(list(decl.pins.items())[:_DECLARED_PINS_CAP])
        data = {
            "root": decl.root, "file": decl.filename, "digest": decl.digest, "action": action,
            "pins": pins, "pin_count": len(decl.pins),
            "pins_truncated": len(decl.pins) > _DECLARED_PINS_CAP,
            # The directives (`-e .`, `--index-url …`, `-r other.txt`) are the part of the file we
            # do NOT follow. Naming them is what stops the receipt reading as "these 21 lines are
            # everything the repo asked for" when a `-r base.txt` pointed somewhere we never looked.
            "directives": [str(d)[:200] for d in decl.directives[:_DECLARED_PINS_CAP]],
            # Declarations we saw and deliberately left alone (pyproject.toml, environment.yml …).
            "observed": list(decl.observed),
            "command": list(cmd),
            # Only the node-resync path measures a delta here; the run-setup install reports its own
            # on `run_setup_finished`, which is where its exit code lives — one install, one row.
            "env_delta": dict(delta or {}),
            # Declarations we could not honour, per line, verbatim from pip. Written from the SAME
            # list `run_setup_finished.dropped_requirements` carries (one producer,
            # `_retry_without_unsatisfiable`) — it appears here too because this row is the one that
            # NAMES the decision (`installed_partial`), and a decision that does not carry its own
            # evidence sends the reader to another event to find out what it means.
            "dropped": list(dropped or []),
        }
        try:
            prior = [e for e in self.store.read_all() if e.type == EV_DEPS_DECLARED]
        except Exception:  # noqa: BLE001 - a receipt must never be what takes a run down
            prior = []
        if prior:
            last = prior[-1].data or {}
            if last.get("digest") == decl.digest and last.get("action") == action:
                return
        assert EV_DEPS_DECLARED in DIAGNOSTIC_EVENTS
        self.store.append(EV_DEPS_DECLARED, data)

    def _do_run_setup(self, cmd: list, declared=None) -> None:
        from looplab.core.models import run_setup_key
        from looplab.runtime import deps
        from looplab.runtime.sandbox import _run_argv
        # ONE root rule, shared with the declaration reader — but an arbitrary operator command
        # needs a cwd that EXISTS, so a repo-less run falls back to the run dir (see the docstring
        # there for why the declaration reader must NOT make that fallback).
        cwd = self._repo_deps_root() or str(self.run_dir)
        to = float((self._eval_spec or {}).get("run_setup_timeout", 1800.0))
        # A prior process appended this command's `run_setup_started` and never appended its finish:
        # its side effects may be complete, partial, or absent and no receipt can say which. The
        # attempt receipt is what makes that ambiguity SURVIVE the crash — see the delivery contract
        # in `_ensure_run_setup`. Stamping the repeat keeps `run_setup.log` and the event log honest
        # about how many times an arbitrary command actually ran.
        interrupted = run_setup_key(cmd) in fold(self.store.read_all()).run_setup_open
        if interrupted:
            _LOG.warning("run_setup %r is being re-executed after an interrupted attempt: its side "
                         "effects may be applied twice", cmd)
        # These are FOLDED events appended from an eval WORKER THREAD (`_run_eval` runs under
        # `anyio.to_thread` from `_evaluate`), so they sit outside `_write_lock` and outside
        # BACKGROUND_APPENDABLE / DIAGNOSTIC_EVENTS / the per-node parallel-build seam. That is
        # invariant #1's remaining thread-side exception, and it is now a REGISTRY rather than a
        # prose claim: `SETUP_THREAD_APPENDABLE` states the conditions (append serializes bytes,
        # `_run_setup_lock` makes this once-per-run, the fold keys run_setup_open/done purely BY
        # COMMAND), the asserts below pin membership at the append sites the way the train-monitor
        # pins DIAGNOSTIC_EVENTS, and tests/test_setup_thread_appendable.py guards both ends.
        assert EV_RUN_SETUP_STARTED in SETUP_THREAD_APPENDABLE
        assert EV_RUN_SETUP_FINISHED in SETUP_THREAD_APPENDABLE
        self.store.append(EV_RUN_SETUP_STARTED,
                          {"command": cmd, "cwd": cwd, "after_interrupted_attempt": interrupted})
        log = str(Path(self.run_dir) / "run_setup.log")
        # What the environment held for the DECLARED distributions before this command ran. The
        # `-r` install honours the repo's pins, and on the live testbed honouring them DOWNGRADES a
        # package (`pytorch_lightning==1.5.1` into a container carrying 2.6.5). That is a policy
        # question the operator owns, and one they cannot even be asked if no artifact records that
        # it happened — so the delta is measured here rather than inferred later. Measured around
        # an OPERATOR's command too: their `pip install -r …` moves the same versions.
        watch = self._declared_watchlist()
        before = self._env_versions(watch)
        rc, out, err, timed = _run_argv(cmd, cwd, to, log_path=log)
        # ONE DEAD LINE IN A REAL REPO'S requirements.txt MUST NOT STOP THE LAB.
        #
        # "The install could not run" and "one declared line has no distribution anywhere" are
        # different failures, and only the first is a reason to refuse to start. Live and measured
        # 2026-08-07: the dense-retrieval testbed declares `ecom-mlflow` at requirements.txt:24,
        # nothing on this box serves it (no PIP_INDEX_URL, no pip.conf), and it is imported only from
        # `test*.py` files the eval pipeline never executes — so under a whole-file contract every
        # rubert run aborts at run start over a package no node would have imported.
        #
        # So: retry ONCE without exactly the unresolvable lines, and record per line which and why.
        # WHAT STAYS FATAL, deliberately — a pip that crashed, a dead or missing index, any non-zero
        # exit with no per-requirement complaint, a declaration too tangled to rewrite faithfully
        # (`deps.can_reduce`), an unresolvable name the declaration does not even contain (a
        # TRANSITIVE dep: dropping the line that pulled it is not what the operator asked), and the
        # reduced set failing in its turn. `unsatisfied_requirements` returning {} is what makes all
        # of those fall straight through to the raise below, unchanged.
        #
        # THIS IS A BEHAVIOUR CHOICE AND IT IS THE OWNER'S TO OVERRULE — recorded as such in the
        # receipt vocabulary (`installed_partial`) and in docs/guide/configuration.md. It is not a
        # silent degrade: if a dropped package IS needed, the run fails at the point of use with a
        # traceback naming it, and `run_setup_finished.dropped_requirements` is the run-start record
        # explaining exactly why it was absent — which is the thing that was missing before.
        dropped: list = []
        if rc != 0 and not timed and declared is not None:
            dropped, retried = self._retry_without_unsatisfiable(declared, out, err, cwd, to, log)
            if retried is not None:
                rc, out, err, timed = retried
        # Carry the command so the fold can key the exactly-once record on it (arch-review §5 P2).
        # Both output tails go through `self._redact`, like every sibling tail (evaluate.py's
        # stdout_tail/stderr, train_monitor's reason). This one is persisted to the DURABLE event
        # log, so under `redact_output=True` — the recommended untrusted-tier posture — a run_setup
        # command's stderr (pip echoing an index URL with an inline token, a git error carrying URL
        # userinfo, a traceback quoting an env var) used to land verbatim and stay there; the
        # RuntimeError below re-leaked the same bytes into the failure message.
        # `env_delta` is an ADDITIVE data field on a FOLDED event (invariant 5): old logs simply do
        # not carry it and every reader defaults it, and the fold has never read this payload beyond
        # `command`/`exit_code`. Empty dict = "nothing the repo declares moved", which is a real and
        # useful answer (a re-run of an already-satisfied install), distinct from an absent field.
        # `dropped_requirements` sits beside `env_delta` on purpose: both answer "what environment
        # did this run actually get?", both are per-item and machine-readable, and an operator must
        # be able to see "we honoured 20 of 21 declarations, `ecom-mlflow` has no distribution"
        # without opening a log. A stderr tail cannot be read by a tool.
        self.store.append(EV_RUN_SETUP_FINISHED,
                          {"command": cmd, "exit_code": rc, "timed_out": timed,
                           "stderr_tail": _redacted_tail(self._redact, err, 2000),
                           "env_delta": self._env_delta_since(watch, before),
                           "dropped_requirements": dropped})
        if rc != 0 or timed:
            # Name the requirements pip refused, when it named any. "run_setup failed, see the log"
            # and "your repo declares ecom-mlflow and nothing serves it" are the same event and not
            # the same message — and this branch is now reached only when the reduced retry did not
            # apply or did not help, which is exactly when the operator needs the name most.
            unsat = deps.unsatisfied_requirements((err or "") + (out or ""))
            named = ("; no distribution for: " + ", ".join(sorted(unsat))) if unsat else ""
            # An `EnvironmentRefusal` (doc 52 §5.1 row 6), not a bare RuntimeError: this is the
            # operator's own setup command failing on the operator's own box — a refusal about the
            # environment, which the CLI boundary prints as one sentence naming the fix instead of
            # the 42-frame traceback that reads as an engine crash. Same base class, so every
            # `except RuntimeError` on the way up still catches it. Its `RunSetupRefusal` subclass
            # is what makes it the RUN's stop rather than one node's fault (review 2026-09-22,
            # ENG2-08) — see `core/errors.py::RunSetupRefusal`.
            raise RunSetupRefusal(
                f"run_setup failed (exit={rc}, timed_out={timed}){named}; see {log}. Fix the "
                f"`eval.setup` command or the declared requirements it installs, then resume.\n"
                + _redacted_tail(self._redact, err or out, 500))

    def _retry_without_unsatisfiable(self, declared, out, err, cwd, to, log):
        """Retry the DERIVED declaration install once, minus the lines pip could not resolve.

        Returns `(dropped records, retry result | None)`. `None` means no retry was attempted and
        the caller's original failure stands — see the conditions in `_do_run_setup`, which is where
        the policy is stated. The dropped records are returned even when the retry then fails, so
        the log says what was tried.

        The reduced file is written into the RUN DIR, never into the operator's repo: a run must not
        mutate the source tree it is reading, and `run_setup.log` records the file pip actually read.
        It is only safe to move the file because `deps.can_reduce` has already refused any
        declaration whose directives name relative paths."""
        from looplab.runtime import deps
        from looplab.runtime.sandbox import _run_argv
        unsat = deps.unsatisfied_requirements((err or "") + (out or ""))
        if not unsat or not deps.can_reduce(declared):
            return [], None
        # Only lines the DECLARATION itself carries. An unresolvable TRANSITIVE dependency names a
        # distribution the operator never wrote down, and the only way to drop it would be to drop
        # whichever declared line pulled it in — a guess about intent, so we refuse and stay fatal.
        droppable = [n for n in unsat if n in declared.pins]
        if not droppable or len(droppable) >= len(declared.pins):
            return [], None            # nothing to drop, or nothing would be left to install
        records = [{"name": n, "line": declared.pins[n],
                    "reason": "no distribution found", "pip": unsat[n]} for n in sorted(droppable)]
        try:
            src = Path(declared.root) / declared.filename
            reduced = Path(self.run_dir) / "requirements.reduced.txt"
            reduced.write_text(
                deps.reduced_requirements(src.read_text(encoding="utf-8", errors="replace"),
                                          droppable), encoding="utf-8")
        except OSError as e:           # unreadable source / unwritable run dir -> the original stands
            _LOG.warning("could not write a reduced declaration (%s); the original failure stands", e)
            return [], None
        _LOG.warning("dependency install retrying without %d unresolvable declaration(s): %s",
                     len(droppable), ", ".join(sorted(droppable)))
        argv = deps.declaration_argv(declared, python=getattr(self.sandbox, "python", None))
        argv = argv[:-1] + [str(reduced)]      # same command, pointed at the reduced copy
        result = _run_argv(argv, cwd, to, log_path=log)
        if result[0] == 0 and not result[3]:
            self._record_declared_deps(declared, "installed_partial", argv, dropped=records)
        return records, result

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

    def _declared_eval_env(self, env, es) -> Optional[dict]:
        """Fold the run- and task-level DECLARED ENVIRONMENT into the child env for one eval.

        Returns the argument UNCHANGED — including `None` — when nothing is declared, which keeps
        every run that declares no environment byte-identical: `None` is the sentinel three call
        sites and `tests/test_gpu_resources.py` read as "unpinned, whole box, legacy behaviour", and
        materializing it into `{}` for a run with no declaration would rewrite that for nothing.

        Both layers were validated where they were WRITTEN (`Settings.eval_env`'s field validator and
        `EvalSpec.env`'s), by the one shared rule. They are re-read from a snapshot here rather than
        re-validated, on the same reasoning as `_resolve_stages`: an old or hand-edited snapshot must
        stay resumable. What that costs is bounded — a hand-edited snapshot can name a variable the
        validator would have refused — and it is the same trust an operator already has over
        `cmd.command` itself, which is an argv the eval executes.
        """
        from looplab.runtime.command_eval import merge_env
        declared = merge_env(getattr(self, "_eval_env", {}), (es or {}).get("env"))
        if not declared:
            return env
        return {**(env or {}), **declared}

    def _run_eval(self, node, workdir, env=None, profile=None, cancel=None, start_stage=_UNSET,
                  canary=None):
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
        by the loop's re-fold.

        `canary`: the EVAL CANARY's declaration (`engine/eval_canary.py::canary_spec`) — None, the
        default, is the full eval, byte-identical. Given, the SAME chain runs with `canary["env"]`
        overlaid LAST on the declared environment and every timeout capped at `canary["timeout"]`,
        and the paid/recording hooks that would describe the node's REAL run (stage checks, the
        deadline judge, the stage-progress beacons, the stage-identity recorder) are left off. The
        caller owns `workdir` being a scratch directory and discards the result's metric."""
        # THE DECLARED ENVIRONMENT, composed ONCE and BEFORE anything spawns (F1d). Run level
        # (`Settings.eval_env`) then task level (`cmd.env`), most specific last; the per-stage layer
        # is applied by `_run_stages`, which is the only layer that differs per child.
        #
        # It goes in HERE, above the branch, rather than at any spawn site, because `env` is the
        # single dict this method hands to EVERY execution path: the command-eval tiers below
        # (`make_docker_wrap(env=…)` turns it into the container's `-e` pairs, `run_command_eval(env=…)`
        # overlays it on the subprocess tier's secret-filtered host base) AND `self.sandbox.run(…)` on
        # the solution.py path. Composing it once is what makes them agree by construction rather than
        # by three matching edits — and it is what makes `eval_env`'s promise ("every eval of every
        # node") true for a non-repo task too. It lands on top of the engine's own env (the GPU pin,
        # the read-fence marker), which is safe because `validate_env_map` refuses every name the
        # engine owns, so a declaration can never overwrite one.
        env = self._declared_eval_env(env, self._eval_spec)
        if canary is not None:
            # The canary's env (`LOOPLAB_CANARY=1` + the task's `eval.canary.env`) wins over every
            # declared layer: it is what makes this run tiny.
            env = {**(env or {}), **dict(canary.get("env") or {})}
        if self._eval_spec:
            from looplab.runtime import command_eval
            es = self._eval_spec
            self._ensure_run_setup()             # one-time run-level dep install (before the first eval)
            root = str(Path(workdir).resolve())               # repo/workdir root
            # The Developer's deliberate-change path: if THIS node's dependency declaration differs
            # from the one the run installed, install it before the eval. Digest-gated, so the
            # unchanged case (every node that did not touch the file) costs one read — see
            # `_sync_node_deps` for the four bounds and for why this is not a per-node environment.
            self._sync_node_deps(root)
            # The profile -> `build_command` -> `_resolve_stages` derivation, asked of the ONE owner
            # (`eval_stages.py::_eval_pipeline`) rather than spelled out here. The planners ask the
            # same function through `_resolved_stages`, which is what stops them planning a chain
            # this line would not run — they used to re-implement it and had already lost `profile`.
            # It is the derivation only: `_ensure_run_setup`/`_sync_node_deps` above are the
            # dispatcher's own side effects and stay here, where a planner can never reach them.
            cmd, timeout, stages, _protocol = self._eval_pipeline(node, workdir, profile)
            if canary is not None:
                from looplab.engine.eval_canary import capped_pipeline
                timeout, stages = capped_pipeline(timeout, stages, float(canary["timeout"]))
            # Phase 3: inter-stage verify (only if any stage asks). `root` and `stages` are what let
            # the checker's `loss_unchanged_from_first_step` verdict be checked against the whole of
            # this attempt's stage log instead of the 4,000-char tail it is shown — the log lives in
            # `log_dir` (== `root`, below) and the plan comes from the SAME resolved list this eval
            # runs, so neither is derived from anything a model said. See `_stage_check_fn`.
            check_fn = (self._stage_check_fn(node, root, stages)
                        if stages and any(s.get("check") for s in stages) and canary is None
                        else None)
            cwd = self._sandbox_cwd(workdir, es.get("cwd", "."))
            # PREFLIGHT the resolved chain for a PROTECTED script the workdir doesn't hold, BEFORE any
            # stage runs. The one failure the repair loop structurally cannot fix (the agent may not
            # write a protected path) was also the most expensive to discover: the `score` stage is
            # LAST, so a node paid for its whole train and then died in 0.06s on
            # `python: can't open file '…/looplab_eval.py'`. Reported as a plain failed RunResult
            # rather than raised: an exception here escapes the eval worker and leaves the node with
            # NO terminal event (invariant 2), re-raising on every resume.
            _unrunnable = self._unrunnable_protected_scripts(
                [(s.get("name"), s.get("command")) for s in stages] if stages
                else [("score", cmd)], root, cwd)
            if _unrunnable:
                return command_eval.RunResult(
                    exit_code=2, stdout="", metric=None, timed_out=False,
                    stderr="\n".join(self.PROTECTED_SCRIPT_MISSING.format(stage=st, script=sc)
                                     for st, sc in _unrunnable))
            # untrusted tier (Phase 4): sandbox the eval in docker, mounting the workspace
            # root so the cwd subdir + host metric reading line up. Fails loudly w/o docker.
            # Symlink-mounted data/reference sources ride along as same-path binds (the /work
            # bind alone leaves their symlinks dangling in the container) — read-only unless the
            # source's `edit` permission grants writes (mount-layer enforcement of edit:false).
            # The Settings -> container translation (image, mem/cpus, readonly rootfs, and what
            # "hostile" means) comes from `sandbox.docker_tier_kwargs`, the ONE derivation all three
            # Docker surfaces share — the solution tier via `make_sandbox`, this command tier, and
            # the operator assistant's shell. `self` IS the source: the Engine copies those five
            # `Settings` fields onto itself under the same names. Only the per-EVAL arguments
            # (`binds`, `env`) are spelled here, because only this tier has them.
            from looplab.runtime.sandbox import docker_tier_kwargs
            wrap = (command_eval.make_docker_wrap(
                        root, **docker_tier_kwargs(self),
                        binds=self._data_binds(workdir),
                        env=env)   # forward LOOPLAB_EVAL_SEED etc. into the container (per-eval env)
                    if self.trust_mode in ("untrusted", "hostile") else None)
            # The operator's declared metric SUBJECT, filtered to strings HERE rather than trusted:
            # `_grandfathered` reloads a recorded `task.snapshot.json` WITHOUT re-validating it, so
            # the pydantic guard on `EvalSpec.metric` is not total over what reaches this line, and a
            # non-string entry becomes `Path(workdir) / 123` -> an uncaught TypeError out of the eval
            # worker (no node terminal, re-dying on every resume).
            _mspec = es.get("metric") if isinstance(es.get("metric"), dict) else {}
            _subject = [s for s in (_mspec.get("subject") or []) if isinstance(s, str) and s.strip()]
            # …and the PATTERN half, filtered by the same rule for the same reason. A `subject_glob`
            # reaches `Path.glob(pattern)`, which raises `TypeError` on a non-string just as readily.
            _subject_glob = [g for g in (_mspec.get("subject_glob") or [])
                             if isinstance(g, str) and g.strip()]
            # THE ATTEMPT'S OWN FLOOR, taken on the engine's clock immediately before the eval.
            # `run_command_eval` derives its subject floor from a `_eval_started` set AFTER setup, so
            # this one is at most a few seconds EARLIER — the permissive direction, which is the
            # right one for a record that must never refuse spuriously. It goes through the SAME
            # `attempt_freshness_floor` so a stage-scoped re-run drops the floor here exactly as it
            # does for the subject: the engine's own reuse must not make the reused stage's config
            # read as `stale`.
            _attempt_started = time.time()
            # HOST-SIDE SCORING (doc 52 row 10a): when the task declares a host scorer, the final
            # stage's stdout is the HOST's, read with the host scorer's own reader (the task's when
            # it declares none), and the task's reader is handed over as `self_metric` to read the
            # candidate's own number off the stage before it. Without one, byte-identical.
            _host = es.get("host_scorer") if isinstance(es.get("host_scorer"), dict) else None
            _primary = ((_host.get("metric") if isinstance(_host.get("metric"), dict) else None)
                        or es["metric"]) if _host else es["metric"]
            res = command_eval.run_command_eval(
                cmd, cwd, timeout, _primary, env,
                self_metric=(es["metric"] if _host else None),
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
                stall_cap=self.eval_stall_timeout_s,          # #6: operator-set silence-before-kill cap (0 = off)
                # The single-command path's deterministic divergence stop (Settings; see the field's
                # comment for the 0-of-110 scorer measurement that decided the default). Read off the
                # DECLARED attribute: until 2026-09-06 this was `getattr(self,
                # "single_command_divergence_watch", False)` on a name no `__init__` ever assigned,
                # so the watchdog never armed on this path whatever the setting said.
                divergence_watch=bool(self._single_command_divergence_watch),
                check_fn=check_fn,                            # Phase 3: optional inter-stage agentic verify
                # THE STAGE IDENTITY INSTRUMENT. Derives each stage's reuse key before it runs and
                # its outputs' content identity when its artifact contract passes; both ride on the
                # stage row into `stage_finished`. Injected because the import closure it needs lives
                # in the engine and `runtime` imports nothing above `core`. It records only — see
                # `runtime/stage_identity.py` for why a cross-node cache was measured and refused.
                # `workdir`, not `cwd`: `_stage_reachable_files` resolves script paths against the
                # workdir ROOT, and the declared relative `cwd` rides separately as the fail-closed
                # clause it is on `_safe_reuse_start` — the two arguments are the same pair that
                # predicate takes, deliberately.
                stage_key_fn=(self._stage_key_fn(workdir, cwd=es.get("cwd") or None)
                              if canary is None else None),
                # THE LIVE STAGE CURSOR. `stage_finished` lands only at a stage's COMPLETION, so
                # between two rows nothing could say which of `mine`/`train`/`score` was running:
                # `train_monitor.resolve_stage_log` guessed it from freshest-mtime (its own docstring
                # concedes the cursor "genuinely is unobservable from here") and every UI status
                # surface simply called the whole multi-hour pipeline "Training / evaluating".
                # Diagnostic beacons only — nothing here folds, decides, kills or selects.
                on_stage_event=(self._stage_progress_fn(
                    node.id if node is not None else None,
                    node.attempt if node is not None else 0, stages)
                    if canary is None else None),
                # The one-shot deadline judge (doc 39 site #2). Both are needed and are separate on
                # purpose: the callback may be None (no client) while the cap is set, and the cap is
                # the OPERATOR'S number — `sandbox._granted_grace` clamps to it in the runtime, so a
                # judge cannot name its own extension even if a future caller lets it try.
                on_deadline=(self._deadline_grace_fn(node) if canary is None else None),
                deadline_grace_max_s=self.eval_deadline_grace_s,
                # METRIC PROVENANCE: what the number is a claim ABOUT. Gated on the rung so `off` is
                # byte-identical to the behaviour before this shipped — and so a RESUMED pre-2026-08-13
                # run (which `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` pins to `off`) does not acquire a
                # different record mid-log. The binding is a READ; whether an unbound metric is a
                # violation is decided at the terminal, in `engine/evaluate.py`.
                subject=(_subject if str(getattr(self, "metric_subject", "audit") or "audit") != "off"
                         else None),
                subject_glob=(_subject_glob
                              if str(getattr(self, "metric_subject", "audit") or "audit") != "off"
                              else None))
            # AN ABSENT DECLARATION IS ITSELF THE FINDING, and it has to be recorded HERE.
            #
            # `run_command_eval` records nothing when no subject is declared, deliberately: it is the
            # library boundary, and "the operator declared nothing" is a fact about the TASK, not
            # about the run. But `not_declared` is the state 82 of 83 corpus metrics are in — it is
            # the whole reason this exists — so leaving it unrecorded would make `require` a rung
            # that fires on a mis-declared subject and never on a missing one, i.e. on the rare case
            # and not the universal one.
            #
            # Scoped to THIS branch, which is `if self._eval_spec:` — a task with an operator eval
            # spec, the only place a `subject` could have been declared. A toy/dataset/sweep eval has
            # no such field and must not be told it forgot one.
            if (str(getattr(self, "metric_subject", "audit") or "audit") != "off"
                    and getattr(res, "metric_subject", None) is None):
                res.metric_subject = command_eval.absent_metric_subject()
            # THE INPUT SIDE — what this number was measured AGAINST (`runtime/metric_inputs.py`).
            # Bound HERE and not inside `run_command_eval` for two reasons that both point the same
            # way: the workdir is unambiguously alive at this line (the subject binding needs the
            # SCORE STAGE's instant and therefore has to live inside the stage loop, an input does
            # not), and `run_command_eval` is the library boundary — a caller outside the engine has
            # no `eval.inputs` and must not grow an argument for one.
            #
            # AT THE METRIC READ, deliberately, not before the command. A staged pipeline may BUILD
            # the corpus it then scores against, and a record bound before the first stage would
            # identify an index that did not exist yet. "As it stands when the number was read" is
            # the same instant `command_eval` binds a single-command SUBJECT at, and for the same
            # reason: it is the only instant that is a fact about this number.
            #
            # NOT gated on the `metric_subject` rung. That rung decides whether an unbound SUBJECT is
            # a violation — an enforcement question — and this is a pure record that gates nothing,
            # excludes nothing and cannot fail a node. Gating it on `off` would silence the input
            # record for exactly the RESUMED runs whose comparability is hardest to establish.
            _declared_inputs = metric_inputs.input_declaration(es)
            if _declared_inputs:
                try:
                    res.eval_inputs = metric_inputs.bind_inputs(_declared_inputs, str(workdir))
                except Exception:  # noqa: BLE001 - a record may never cost a node its terminal
                    res.eval_inputs = {"inputs_bound": False, "inputs": [],
                                       "unbound_reason": "unreadable"}
            # THE COORDINATE SIDE — what the CONFIGURATION that ran said the node's declared
            # `Idea.params` were worth (`runtime/applied_params.py`).
            #
            # AT THE METRIC READ, and the same instant argument the subject side makes: a staged
            # pipeline REWRITES its configuration between stages (the training stage and the scoring
            # stage each resolve their own on this box), so a record bound before the first stage
            # would describe a document that no longer decided anything by the time the number was
            # read. "As it stands when the number was read" is the only instant that is a fact about
            # this number.
            #
            # HERE AND NOT INSIDE `run_command_eval`, for `metric_inputs`' two reasons: the workdir is
            # unambiguously alive at this line, and `run_command_eval` is the library boundary — a
            # caller outside the engine has no `Idea.params` and must not grow an argument for one.
            #
            # THE CARRIERS ARE `node.files`' OWN KEYS, i.e. exactly what the engine committed and
            # nothing it discovered. That is what keeps this a record about the node's declared
            # configuration and not a search of the workdir for a file that looks convenient; the
            # STRONGER source — the config the eval process itself wrote — is elected only by the
            # operator's `applied_config_glob`, and only on a unique match.
            #
            # NOT GATED, on any rung. It records what a number's coordinates are; it does not decide
            # whether the number is sound, so it mints no violation, excludes nothing and cannot cost
            # a node its terminal. A node that adjusted for a real constraint must still be allowed
            # to win — what must not survive is a record attributing its number to parameters it
            # never used.
            try:
                _declared_params = (dict(node.idea.params or {})
                                    if node is not None and node.idea is not None else {})
                res.applied_params = applied_params.bind_applied_params(
                    _declared_params, str(workdir),
                    carriers=sorted((node.files or {}) if node is not None else {}),
                    applied_config_glob=_mspec.get("applied_config_glob"),
                    since=command_eval.attempt_freshness_floor(
                        _attempt_started, stages,
                        (node.rerun_stage if node is not None else None)
                        if start_stage is _UNSET else start_stage),
                    # The pipeline the coordinates are claimed about. A `merge` + `score` node
                    # averages two parents' weights and trains nothing, so every training coordinate
                    # the rung compares for it governed nothing — 4 of the 12 records on this box,
                    # one of them a champion. The engine holds this here and used to discard it.
                    pipeline_stages=[s.get("name") for s in (stages or ())])
            except Exception:  # noqa: BLE001 - a record may never cost a node its terminal
                res.applied_params = None
            # THE EXECUTION SIDE of that same coordinate (`runtime/effective_batch.py`): what the
            # trainer's OWN state file says it ran at, which is the one thing a reader of the
            # committed or resolved configuration cannot see (`auto_find_batch_size` writes the
            # reduced batch to `trainer_state.json` and leaves `args` declaring the original).
            #
            # HERE, at the metric read, for the two reasons its siblings are: the workdir is
            # unambiguously alive at this line, and `run_command_eval` is the library boundary. The
            # SAME freshness floor as the resolved tier, and for the same reason — a state file that
            # predates this attempt belongs to the previous one — so it is re-derived from the same
            # call rather than remembered across the `except` above, which could have swallowed it.
            try:
                res.effective_train_batch = effective_batch.bind_effective_train_batch(
                    str(workdir),
                    since=command_eval.attempt_freshness_floor(
                        _attempt_started, stages,
                        (node.rerun_stage if node is not None else None)
                        if start_stage is _UNSET else start_stage))
            except Exception:  # noqa: BLE001 - a record may never cost a node its terminal
                res.effective_train_batch = None
            # THE PROTOCOL SIDE — which profile's overrides the score command ran under, as
            # `_eval_pipeline` resolved them for THIS dispatch (`command_eval.eval_protocol`). A pure
            # record: it folds into `metric_provenance.comparability` at the terminal, where it can
            # only refuse ranking a smoke-scored node against a full-scored one (doc 68 §1).
            res.eval_protocol = _protocol
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
            # The same governed canonicalization feeds native-batch identity and execution. Keeping
            # this as the single consumer prevents admission from fingerprinting a timeout we ignore.
            etv = effective_researcher_eval_timeout(
                self, node.idea if node is not None else None)
            if etv is not None:
                timeout = etv
            res = self.sandbox.run(node.code, str(workdir), timeout, env, cancel=cancel)
            # WHO WROTE THE PRINT STATEMENT, asserted here because here is the only place that can.
            # The sandbox tags every scraped stdout number `auto` — it is handed an opaque string and
            # runs it, so authorship is exactly the question `runtime` cannot ask (and answering it
            # from the string's own BYTES is what let a candidate forge the `engine` tag: see
            # `core/calibration.py::engine_declared_extra_metric_keys`). The engine knows which
            # artifacts it spliced itself, from its own role wiring, so the upgrade is applied here
            # over the code that actually ran. It only ever raises `auto` -> `engine` on the probe's
            # own key names, and only in a run whose Developer IS the engine's probe splicer.
            #
            # BEFORE the terminal payload, deliberately: `evaluate.py`'s
            # `authenticated_extra_metrics_only` gate is expressed over this tag, so a tag settled
            # after it would be a second, drift-prone answer to the same question.
            res.extra_metrics_provenance = apply_engine_extra_metric_channels(
                res.extra_metrics_provenance, node.code,
                engine_authored=engine_authored_artifacts(self))
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
        # Candidate stdout is untrusted input. A syntactically valid `{"trials":[1]}` must fail the
        # node, not raise AttributeError in the host engine. Keep only object-shaped trial records;
        # replay independently validates them into Trial models later.
        trials = [t for t in (res.trials or []) if isinstance(t, dict)]
        scored = [(t, _to_float(t.get("metric"))) for t in trials]
        scored = [(t, m) for t, m in scored if m is not None]
        if not scored:
            res.metric = None
            return
        chooser = min if self.task.direction == "min" else max
        best_t, best_m = chooser(scored, key=lambda tm: tm[1])
        res.metric = best_m
        extra = normalize_extra_metrics(best_t.get("extra_metrics"))
        if extra:
            res.extra_metrics = {**normalize_extra_metrics(res.extra_metrics), **extra}
            # AUTO, unambiguously: a trial's extras come off the `{"trials": [...]}` line the
            # candidate itself printed — there is no reader spec behind them and no operator
            # declaration they could satisfy. Tagged here rather than left absent so they read as
            # `auto` and not as the weaker `unknown`, which is reserved for logs written before the
            # channel was recorded at all.
            res.extra_metrics_provenance = {
                **normalize_extra_metric_channels(res.extra_metrics_provenance),
                **{k: EXTRA_METRIC_AUTO for k in extra}}

    def _skip_if_aborted(self, a: dict, cur: RunState) -> bool:
        # Both explicit stop affordances close not-yet-started work at zero cost. A mid-eval abort/drop
        # is handled by EvaluateMixin's watcher and records the time already spent.
        node_id = a["node_id"]
        n = cur.nodes.get(node_id)
        node_aborted = node_id in cur.aborted_nodes
        card_dropped = bool(
            n is not None and self._operator_card_dropped_for_node(cur, n))
        if node_aborted or card_dropped:
            if n is not None and n.status is NodeStatus.pending:
                reason = "aborted" if node_aborted else "card_dropped"
                error = "aborted by operator" if node_aborted else "Card dropped by operator"
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node_id, "generation": n.attempt,
                    "error": error, "reason": reason, "eval_seconds": 0.0})
            return True
        return False

    def _fold_if_tail_moved(self, cached: Optional[tuple]) -> tuple:
        """`(tail_seq, RunState)` for the log as it is now, re-folding ONLY if the tail moved.

        The LOOP-LOCAL half of doc 25 ES-12, and deliberately not the shared `fold_cached` the
        finding asked for. A resource wait can last as long as another run holds the host GPU pool
        (hours), and every bounded tick of it re-folded the WHOLE log — an O(total-events) busy-poll
        the wait's own comment had been asking to gate since it was written. `EventStore.read_all`
        is already incrementally cached, so an unchanged log costs a stat and the fold is what the
        gate removes.

        WHY THE TAIL IS A SOUND KEY here and a shared memo is not. Soundness: the wait's stated
        reason for re-folding is a GPU->CPU Card re-pin, which does not bump the pool epoch — but it
        does APPEND (`EV_CARD_RESOURCE_PINNED`), as does every operator gate the loop re-checks
        (pause/stop/abort/reset) and the terminal `_skip_if_aborted` writes itself. Nothing this
        loop reacts to can land without moving the tail, and seqs are strictly monotonic, so an
        equal tail means the fold would be the same value. Isolation: the returned state is CACHED
        BY ONE LOOP, and its one hand-off is a MOVE, never a share — the serial dispatch gives its
        last fold to the evaluation it starts, reads it no more and folds afresh on its next
        iteration, and that `_evaluate` keeps it as its own admission state
        (`evaluate.py::handed_admission_fold`, EVT-04). That is the whole difference from the
        rejected `fold_cached` on the store, which would have handed ONE `RunState` to a build
        worker thread and to `evaluate.py`'s `node.rerun_stage = None` at the same time. Pass
        `None` on the first tick — or the `(tail_seq, state)` this same loop has just folded, which
        is what the serial dispatch seeds it with (review 2026-09-22, EVT-04) — and hand back what
        this returned.
        """
        events = self.store.read_all()
        tail = events[-1].seq if events else -1
        if cached is not None and cached[0] == tail:
            return cached
        return tail, fold(events)

    async def _dispatch_evals(self, evals: list, state: RunState,
                              max_es: Optional[float]) -> None:
        # Single experiment at a time is the base mode: run evals sequentially and
        # deterministically. Concurrent fan-out (the task-group below) is a backlog
        # seam — opt in with max_parallel > 1. Deep research overlaps + records immediately
        # in BOTH modes (see _spawn_research), independent of max_parallel.
        #
        # Nested groups: the repeating research (`_spawn_research`, when
        # `concurrent_research_repeat` is on) lives in the OUTER `bg_tg` and never finishes on its
        # own; the evals run in / under it and, once they JOIN, the `finally` cancels `bg_tg` to stop
        # the loop. The one-shot path (repeat off, == today) is NOT cancelled — `bg_tg` waits for the
        # single memo to finish exactly as the pre-refactor single group did (byte-identical).
        #
        # The ceiling belongs before STARTING an evaluation, not before RECORDING one: the sink
        # below holds a background `BudgetExceeded` until the evaluations it overlaps have landed
        # their terminals, and this method re-raises it the moment they have.  See
        # `_DeferredBudgetStop` for why that does not weaken the hard stop.
        #
        # The SAME sink holds a ceiling crossed INSIDE a parallel lane (ENG2-02, `_eval_in_slot`),
        # so one set of admission gates and one re-raise serve both producers. Imported here, not at
        # module level, to keep this change inside the method it guards. It holds a refused RUN
        # SETUP too (ENG2-08): the run's other ending that surfaces inside one evaluation, deferred
        # by the same lane on the same terms — see `core/errors.py::deferrable_run_stop`.
        from looplab.core.errors import deferrable_run_stop
        budget_stop: list[BaseException] = []
        async with anyio.create_task_group() as bg_tg:
            self._spawn_research(_DeferredBudgetStop(bg_tg, budget_stop), state)
            try:
                if self._eval_parallel <= 1:
                    limiter = anyio.CapacityLimiter(1)
                    for a in evals:
                        if budget_stop:
                            break        # the ceiling fired: finish what is running, start nothing
                        cur_events = self.store.read_all()
                        cur = fold(cur_events)
                        # This lane's `(tail_seq, state)`: moved by the resource wait's tail gate
                        # below, and HANDED to the evaluation's ADMIT when it starts (EVT-04).
                        waited_fold = (cur_events[-1].seq if cur_events else -1, cur)
                        if self._skip_if_aborted(a, cur):
                            continue
                        # Re-check the eval-compute budget BEFORE each eval (not just per loop
                        # iteration), so a multi-eval batch can't overshoot by a whole batch (#2/#25).
                        # Through the reservation rule, like the parallel branch: on this branch the
                        # ledger is empty at this point (the previous eval was awaited and its
                        # `finally` released), so the answer is the historical completed-time one —
                        # but the QUESTION is now asked in one place for both branches.
                        if _eval_time_admission_refused(self, cur, None, max_es):
                            break
                        node = cur.nodes.get(a["node_id"])
                        reservation = None
                        generation = None
                        skip_eval = False
                        # The wait below is the one place on this branch where a deferred ceiling
                        # can land between the loop-top `break` and `_evaluate` — see
                        # `budget_stop_recheck` for the hours it cost. It is asked at the head of
                        # every tick and again the instant a reservation is granted; a stop found
                        # there skips the eval, and the loop-top test then ends the queue.
                        if node is not None and hasattr(self, "_wait_reserve_node_resources"):
                            generation = node.attempt
                            # The wait's own tail-gated fold, loop-local and handed to nobody: with
                            # the cross-run host lease this wait lasts as long as ANOTHER run holds
                            # the pool (hours of training), and an unconditional re-fold per 0.5s
                            # tick was an O(total-events) busy-poll — the cost confirm F26 documents.
                            # SEEDED with the loop-top fold above, whose tail nothing has moved
                            # since (review 2026-09-22, EVT-04): `None` here re-folded that identical
                            # prefix on EVERY eval's first tick, 60 times in a 60-node toy run. The
                            # gate still re-folds the moment anything appends.
                            while True:
                                if budget_stop_recheck(budget_stop):
                                    skip_eval = True
                                    break
                                # Resource waits must not pin a stale fold forever.  A GPU->CPU Card
                                # re-pin does not release a GPU (and therefore does not bump the pool
                                # epoch), so re-fold after every bounded condition tick and fence the
                                # exact lifecycle plus run-level operator gates before retrying.
                                # The tail gate keeps that promise exactly: a re-pin APPENDS
                                # (`EV_CARD_RESOURCE_PINNED`), as does every operator gate this loop
                                # re-checks, so nothing it reacts to can land without moving the tail
                                # — see `_fold_if_tail_moved` for why the key is sound and why this
                                # stays loop-local rather than a memo on the store (ES-12).
                                waited_fold = self._fold_if_tail_moved(waited_fold)
                                waiting = waited_fold[1]
                                live = waiting.nodes.get(node.id)
                                if self._skip_if_aborted(a, waiting):
                                    skip_eval = True
                                    break
                                lifecycle_current = _eval_admission_current(
                                    waiting, live, generation, max_es)
                                if not lifecycle_current:
                                    if live is not None and live.id in waiting.aborted_nodes:
                                        self._skip_if_aborted(a, waiting)
                                    skip_eval = True
                                    break
                                cur, node = waiting, live
                                reservation = await self._wait_reserve_node_resources(
                                    node,
                                    resource_pin=self._card_resource_pin_for_node(cur, node),
                                    wait_once=True,
                                )
                                if reservation is None:
                                    continue
                                # The ceiling may have fired during the `await` that just granted
                                # these devices. Hand them back and start nothing: the eval would
                                # spend GPU on a run that is over and raise from inside itself.
                                if budget_stop_recheck(budget_stop):
                                    self._release_gpus(reservation.get("gpu_ids"))
                                    reservation = None
                                    skip_eval = True
                                    break
                                # The admission re-check reads the log through the SAME gate
                                # (EVT-04): everything it refuses on — abort, drop, reset, tombstone,
                                # pause/stop/finish, a spent budget, a re-pin — lands by APPEND, so an
                                # unmoved tail is the fold the tick just took. It used to re-fold
                                # unconditionally, 60 identical prefixes in a 60-node toy run.
                                waited_fold = self._fold_if_tail_moved(waited_fold)
                                admitted = waited_fold[1]
                                live = admitted.nodes.get(node.id)
                                if self._skip_if_aborted(a, admitted):
                                    self._release_gpus(reservation.get("gpu_ids"))
                                    reservation = None
                                    skip_eval = True
                                    break
                                if not _eval_admission_current(
                                        admitted, live, generation, max_es):
                                    self._release_gpus(reservation.get("gpu_ids"))
                                    reservation = None
                                    if live is not None and live.id in admitted.aborted_nodes:
                                        self._skip_if_aborted(a, admitted)
                                    skip_eval = True
                                    break
                                if not self._node_resource_reservation_is_current(
                                    admitted, live, reservation,
                                ):
                                    self._release_gpus(reservation.get("gpu_ids"))
                                    cur, node = admitted, live
                                    continue
                                node = live
                                self._register_eval_resource_reservation(
                                    node.id, generation, reservation)
                                break
                        if skip_eval:
                            continue
                        # …and the TIME this lane will charge, committed against the run allowance
                        # for exactly as long as the lane holds it. Reserved beside the devices and
                        # released in the same `finally`, so no path can leak an allowance nobody is
                        # spending (`resources.py::eval_time_admission_blocked`).
                        _reserve_eval_time(self, a["node_id"], generation, node)
                        try:
                            # ADMIT's fold IS this one while the tail stays put (review 2026-09-22,
                            # EVT-04): it re-folded the identical prefix for every evaluation, 60 of
                            # 60 in a 60-node toy run. The state is given up here — nothing below
                            # reads it, and the next iteration folds afresh — see
                            # `evaluate.py::handed_admission_fold` for why that is not a shared memo.
                            with handed_admission_fold(a["node_id"], *waited_fold):
                                await self._evaluate(a["node_id"], limiter, max_es)
                        finally:
                            _release_eval_time(self, a["node_id"], generation)
                            if reservation is not None and generation is not None:
                                self._settle_eval_resource_reservation(a["node_id"], generation, reservation)
                else:
                    # G3 distributed/parallel eval: CONTINUOUS dispatch. A pool of `max_parallel` slots
                    # is kept FULL — the instant any eval finishes and the dispatcher worker returns
                    # its lifecycle reservation to `_free_gpus`, the producer admits the NEXT
                    # queued eval into that slot. This closes the head-of-line gap the old
                    # `started >= max_parallel: break` left: that break capped the batch at max_parallel
                    # STARTED and deferred the rest to a FUTURE spine iteration, so a short eval that
                    # freed its GPU left it idle for the whole remaining life of a long sibling (the
                    # 10h-vs-1h case). The semaphore bounds concurrency to max_parallel AND refills a
                    # freed slot; each eval gets its own no-op CapacityLimiter(1) so `_evaluate`'s
                    # internal `async with limiter` is inert and the semaphore is the SOLE bound.
                    # fast_acquire: when a slot is already free the admit takes no checkpoint, so a batch
                    # that fits in the pool behaves like the old tight loop (all started before any child
                    # runs); the checkpoint only happens on the genuine refill wait.
                    #
                    # STILL A BARRIER: the inner task group joins the WHOLE batch before returning, so
                    # `bg_tg`'s lifecycle is unchanged. It no longer says anything about a
                    # "`pending_nodes()`-keyed guarantee", and that clause was stale from F1f: the
                    # run-scoped eval group means a node stays `pending` across outer-loop turns this
                    # barrier never sees, and since F1i the node-count cadences no longer key on that
                    # predicate at all (`engine/cadence.py::at_creation_boundary`).
                    # CAPTURE the width beside the semaphore. `anyio.Semaphore` cannot be resized, so
                    # its token TOTAL is this batch's real concurrency for the batch's whole life —
                    # while `self._eval_parallel` is live and has three writers that move it mid-batch
                    # (the Strategist, an operator `budget_extend`, and since docs/29 F1 the
                    # proposal-derived re-pin). The drained-pool test below asks "are all my tokens but
                    # one free?", which is a question about THIS semaphore and must be asked of its own
                    # total. Against the live attribute it broke in both directions: LOWERED mid-batch
                    # (4 -> 2) the test fires with 3 siblings still holding GPUs, so a wide head is
                    # declared unsatisfiable while the devices it wants are merely busy — and
                    # `head_unsatisfiable` is sticky until the head changes, so it starves for the rest
                    # of its queue lifetime, which is the exact defect `_HEAD_BYPASS_LIMIT` exists to
                    # prevent. RAISED mid-batch (2 -> 4) the test needs `slots.value >= 3` from a
                    # semaphore whose maximum is 2, so it can never fire and a genuinely unsatisfiable
                    # head wedges the batch on `_wait_for_gpu_change` with nothing running to move the
                    # epoch. `_settle_proposal_width` only re-pins between batches, so this is defence
                    # in depth for that writer — but it is the ONLY thing holding the line for the
                    # other two, which have always been able to fire mid-batch.
                    batch_width = max(1, int(self._eval_parallel))
                    slots = anyio.Semaphore(batch_width, fast_acquire=True)

                    async def _eval_in_slot(nid: int, generation: Optional[int],
                                            reservation: Optional[dict]) -> None:
                        try:
                            # A private single-token limiter -> `_evaluate`'s `async with limiter` is a
                            # no-op; the outer semaphore is what bounds fan-out and drives the refill.
                            await self._evaluate(nid, anyio.CapacityLimiter(1), max_es)
                        except BaseException as exc:  # noqa: BLE001 — re-raised unless a run stop
                            # A SPEND CEILING THIS EVALUATION CROSSED is deferred into the same sink
                            # the overlapped research uses (review 2026-09-22, ENG2-02), never raised
                            # into `tg`, where it cancelled every sibling lane mid-score. The refill
                            # gate below then admits nothing more, and the method re-raises the stop
                            # once the batch has joined. See `core/errors.py::deferrable_budget_stop`.
                            # A refused run setup (ENG2-08) rides the same sink, so a batch whose
                            # lanes all stopped on it ends the run on ONE refusal, not a group.
                            stop = deferrable_run_stop(exc)
                            if stop is None:
                                raise
                            budget_stop.append(stop)
                        finally:
                            # The eval-second allowance this lane committed at admission goes back
                            # BEFORE the slot does: the producer wakes on `slots.release()` and
                            # immediately asks whether one more lane fits, and it must ask that with
                            # this lane's worst case already handed back — its REAL cost is in the log
                            # by now and `total_eval_seconds` charges it.
                            _release_eval_time(self, nid, generation)
                            if reservation is not None and generation is not None:
                                self._settle_eval_resource_reservation(nid, generation, reservation)
                            slots.release()          # free the slot -> wakes the producer to admit next

                    async with anyio.create_task_group() as tg:
                        pending = list(evals)
                        # Bounded aging state for the scan below (see _HEAD_BYPASS_LIMIT).
                        head_id: Optional[int] = None
                        head_bypasses = 0
                        head_unsatisfiable = False
                        while pending:
                            # Fresh fold PER ADMISSION (like the serial branch, unlike the old fold-once):
                            # continuous dispatch means earlier evals in THIS batch complete mid-loop, so
                            # the abort-skip and the eval-budget guard both act on LIVE state — strictly
                            # stricter than the dead fold-once check the old comment flagged.
                            cur = fold(self.store.read_all())
                            # Budget guard (parallel path): now that `cur` reflects mid-batch completions,
                            # this actually enforces the eval-second cap — admit no more once spent. The
                            # overshoot is bounded to the ONE evaluation the ceiling has always allowed,
                            # not to the ~max_parallel that could each enter under the same remaining
                            # balance: the annotation this line used to carry — "a 'hard cumulative'
                            # budget cannot count only completed charges: every lane can enter under the
                            # same remaining balance and each timeout may exceed it. Reserve the
                            # worst-case/time-bounded charge atomically at admission, then release the
                            # unused portion when the evaluation settles." — is what
                            # `resources.py::eval_time_admission_blocked` and the reservation below now
                            # do. The first lane still enters on the completed-time rule alone (see that
                            # function's second clause: an empty ledger may never refuse, or a ceiling
                            # under one eval's timeout would admit nothing at all).
                            if _eval_time_admission_refused(self, cur, None, max_es):
                                break
                            await slots.acquire()     # blocks only when the pool is full -> the refill point
                            # the pre-check above may be minutes old after a genuine refill
                            # wait. Re-fold while owning the freed slot so a sibling that crossed the hard
                            # eval budget (or an operator abort) cannot be followed by one more admission.
                            cur = fold(self.store.read_all())
                            if _eval_time_admission_refused(self, cur, None, max_es):
                                slots.release()
                                break
                            # …and the SPEND ceiling, for the same reason.  THE ONLY gate this
                            # branch needs, and deliberately not a second one at the top of the
                            # producer loop: `slots.acquire()` sits unconditionally between that top
                            # and the scan below, so EVERY route to an admission — a fresh turn, a
                            # `continue` after an unsatisfiable head, a genuine refill wait — passes
                            # through here.  A top-of-loop copy would be unreachable-first and
                            # therefore untestable, which is how a gate rots.  It is also the place
                            # the stop actually lands: the refill wait is where this loop spends
                            # nearly all of its time, and the overlapped research crosses the ceiling
                            # while a sibling is mid-eval.
                            if budget_stop:
                                slots.release()
                                break
                            # Scan for the first candidate whose complete footprint fits *now*.  A
                            # GPU-heavy head may wait while an explicit CPU node (gpus=0) behind it
                            # starts; reservation and release both use the condition-protected pool.
                            epoch = (self._gpu_pool_epoch()
                                     if hasattr(self, "_gpu_pool_epoch") else 0)
                            chosen_index = None
                            chosen_node = None
                            chosen_reservation = None
                            kept = []
                            for a in pending:
                                if self._skip_if_aborted(a, cur):
                                    continue
                                kept.append(a)
                            pending = kept
                            # BOUNDED AGING. First-fit is work-conserving and right almost always — an
                            # explicit CPU node behind a GPU-heavy head should start — but a steady
                            # stream of small jobs consumes every PARTIAL release, so a wide head can
                            # wait forever for all of its GPUs to be free at the same instant. Once the
                            # head has been passed over `_HEAD_BYPASS_LIMIT` times in a row, scan ONLY
                            # the head: releases then accumulate toward it instead of being eaten. The
                            # claim is dropped below if the pool drains completely and it still does not
                            # fit, so an impossible request waits its turn instead of wedging the batch.
                            current_head = pending[0]["node_id"] if pending else None
                            if current_head != head_id:
                                head_id, head_bypasses, head_unsatisfiable = current_head, 0, False
                            scan = pending
                            if head_bypasses >= _HEAD_BYPASS_LIMIT and not head_unsatisfiable:
                                scan = pending[:1]
                            for pos, a in enumerate(scan):
                                node = cur.nodes.get(a["node_id"])
                                if node is None or not hasattr(self, "_try_reserve_node_resources"):
                                    chosen_index = pos
                                    chosen_node = node
                                    break
                                candidate = self._try_reserve_node_resources(
                                    node,
                                    resource_pin=self._card_resource_pin_for_node(cur, node),
                                )
                                if candidate is not None:
                                    chosen_index = pos
                                    chosen_node = node
                                    chosen_reservation = candidate
                                    break
                            # A bypass is what ages the head; picking the head itself clears the debt.
                            if chosen_index is not None:
                                head_bypasses = head_bypasses + 1 if chosen_index > 0 else 0
                            elif scan is not pending and slots.value >= batch_width - 1:
                                # The pool was reserved for the head and drained to empty (this task
                                # holds the only taken slot) and it STILL does not fit: it wants more
                                # than the box has. Release the claim so the queue behind it can move.
                                head_unsatisfiable = True
                            if chosen_index is None:
                                slots.release()
                                if not pending:
                                    break
                                # A release between the scan and this wait changes the epoch, so the
                                # condition returns immediately rather than losing the wake-up.
                                await anyio.to_thread.run_sync(
                                    self._wait_for_gpu_change, epoch, abandon_on_cancel=True)
                                continue
                            if chosen_node is not None and chosen_reservation is not None:
                                admitted = fold(self.store.read_all())
                                live = admitted.nodes.get(chosen_node.id)
                                if self._skip_if_aborted(pending[chosen_index], admitted):
                                    self._release_gpus(chosen_reservation.get("gpu_ids"))
                                    pending.pop(chosen_index)
                                    slots.release()
                                    continue
                                terminal_gate = _run_terminal_gate(admitted)
                                lifecycle_current = _eval_admission_current(
                                    admitted, live, chosen_node.attempt, max_es)
                                if (
                                    not lifecycle_current
                                    or not self._node_resource_reservation_is_current(
                                        admitted, live, chosen_reservation,
                                    )
                                ):
                                    self._release_gpus(chosen_reservation.get("gpu_ids"))
                                    if terminal_gate:
                                        # A pause/stop can land during the bounded resource wait.  The
                                        # reservation was formed from the old turn, so release it and end
                                        # admission instead of spinning or scheduling work past the gate.
                                        slots.release()
                                        break
                                    if not lifecycle_current:
                                        pending.pop(chosen_index)
                                    slots.release()
                                    continue
                                chosen_node = live
                            chosen = pending.pop(chosen_index)
                            generation = (chosen_node.attempt if chosen_node is not None else None)
                            if chosen_reservation is not None and generation is not None:
                                self._register_eval_resource_reservation(
                                    chosen["node_id"], generation, chosen_reservation)
                            # THE TIME RESERVATION IS TAKEN HERE, before the lane starts and while
                            # the producer still owns the decision — the whole point is that the NEXT
                            # turn of this loop sees it. Its release is `_eval_in_slot`'s `finally`,
                            # which the failed-spawn path below cannot rely on (nothing ran), so that
                            # path releases it beside the devices.
                            _reserve_eval_time(self, chosen["node_id"], generation, chosen_node)
                            try:
                                tg.start_soon(_eval_in_slot, chosen["node_id"], generation,
                                              chosen_reservation)
                            except BaseException:
                                _release_eval_time(self, chosen["node_id"], generation)
                                if chosen_reservation is not None and generation is not None:
                                    self._clear_eval_resource_reservation(
                                        chosen["node_id"], generation)
                                    self._release_gpus(chosen_reservation.get("gpu_ids"))
                                slots.release()
                                raise
            finally:
                # Evals have joined (or errored out) — stop the repeating research loop. One-shot
                # research (repeat off) leaves `bg_tg` uncancelled so its single memo still records,
                # preserving the pre-refactor behaviour byte-for-byte. Defensive getattr: a partial
                # test Engine defaults to one-shot (no cancel), == today.
                if getattr(self, "_concurrent_research_repeat", False):
                    bg_tg.cancel_scope.cancel()
        # THE HARD STOP, paid in full and one join later than it used to be.  Reached only when the
        # body above did not already raise something of its own, and unconditional when it is: the
        # run ends here with the same exception object the accountant raised, so its class, its
        # sentence and the `run_finished {"reason": "budget_exhausted"}` derived from it are
        # byte-identical to before.  What changed is that the evaluations it overlapped are now IN
        # the log it stops over.
        if budget_stop:
            raise budget_stop[0]
