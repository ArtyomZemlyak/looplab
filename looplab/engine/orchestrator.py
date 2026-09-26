"""Engine / control loop (I6, ADR-12/18). anyio structured concurrency:
node *creation* is sequential & deterministic; node *evaluation* fans out under a
CapacityLimiter. State is always a fresh fold of the log (files-as-truth); resume
is just re-entering this loop on an existing run dir — pending nodes get re-evaluated
idempotently, and node ids are a monotonic count so reruns never duplicate.

A crash can be injected (for the resume test) via `crash_after`: hard-exit after N
node_evaluated events have been written, simulating `kill -9` mid-run.
"""
from __future__ import annotations

import dataclasses
import functools
import logging
import os
import secrets
import threading
import time
from collections.abc import Mapping
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
from typing import Optional

import anyio

from looplab.core.errors import budget_stop_leaf
from looplab.events.eventstore import EventStore, EventStoreConcurrencyError, retry_tail_cas
from looplab.events.types import (BACKGROUND_APPENDABLE, DIAGNOSTIC_EVENTS,
                                  EV_RUN_LOOP_EXITED, EV_TRACE_EXPORT_HEALTH,
                                  trace_export_unhealthy, trace_export_health_signature,
                                  run_exit_reason,
    EV_APPROVAL_REQUESTED,
    EV_COMMAND_ACK,
    EV_PLAN,
    EV_DRIFT_UNAVAILABLE,
    EV_FINALIZE_STEP,
    EV_NODE_CREATED,
    EV_PAUSE,
    EV_POLICY_DECISION,
    EV_REPORT_GENERATED,
    EV_RESUME_SERVED, EV_RUN_ABORT, EV_RUN_FINISHED,
    EV_RUNG_PROMOTED,
    EV_SPEC_APPROVAL_REQUESTED,
    EV_SPEC_APPROVED, EV_SPEC_PROPOSED)
from looplab.engine.ablation import AblationMixin
from looplab.engine.metric_salvage import settle_mode as settle_metric_salvage_mode
from looplab.engine.widths import LLM_WIDTH_MAX
# The live width settle (the proposals' re-pin, the operator's `budget_extend`, the broker ceiling
# that follows them) is a mixin of its own since review 2026-09-22, ENG1-04 step 1.
from looplab.engine.width_settling import WidthSettlingMixin
# The run-start pins and the re-entry checks that read them back (invariant #6) are a mixin of their
# own since ENG1-04 step 2. The `RunStartPinError` family moved WITH the checks that raise it and is
# imported back here: the SAME class objects, under the spelling `cli/run_cmds.py` and the tests use
# (`tests/test_engine_member_homes.py` drives a refusal through that spelling).
from looplab.engine.reentry import (ReentryMixin, SpeculationAuthorizationError,
                                    RunStartPinError, SettledWidthPinError)  # noqa: F401
# The run's one-time setup phase (`run_started`, provenance, profiling, the leakage stop) is a mixin
# of its own since ENG1-04 step 3; `_dirty_inputs` and the two constants it reads went with it.
from looplab.engine.setup_phase import SetupPhaseMixin
# The operator's forced steering, served from its durable queues (ENG1-04 step 4b).
from looplab.engine.forced_requests import ForcedRequestsMixin
from looplab.engine.audit import AuditMixin
from looplab.engine.cadence import occupancy_due
from looplab.engine.card_reservation import (CardReservationMixin, _BuildReservation,
                                             scored_anchor)
from looplab.engine.speculation_gate import CalibrationRuntime, admit_speculation_lane
from looplab.engine.confirm_phase import ConfirmPhaseMixin
from looplab.engine.noise_floor import NoiseFloorMixin
from looplab.engine.costs import bind_cost_accountants, find_cost_accountants, seed_prior_spend
from looplab.engine.crash_repair import CrashRepairMixin
# `_DeferredBudgetStop` moved with `_dispatch_evals`, its first user (ENG1-04 step 4d); the two
# parallel-build fan-outs below reach the SAME class through this import.
from looplab.engine.eval_dispatch import EvalDispatchMixin, _DeferredBudgetStop
from looplab.engine.eval_stages import EvalStagesMixin
from looplab.engine.evaluate import EvaluateMixin
from looplab.engine.node_build import NodeBuildMixin
from looplab.engine.proposal_cues import ProposalCuesMixin
from looplab.engine.resources import (ResourceSchedulingMixin, cuda_visible_device_tokens,
                                      default_gpu_host_lease_path, detect_gpu_inventory,
                                      schedulable_cuda_tokens)
from looplab.engine.speculation import SpeculationMixin
from looplab.engine.train_monitor import TrainingMonitorMixin
from looplab.engine.asha_monitor import AshaMonitorMixin
from looplab.engine.shared import SharedEngineMixin
from looplab.engine.novelty import NoveltyGateMixin
from looplab.engine.strategy import StrategyCadenceMixin
from looplab.engine.concept_cadence import ConceptCadenceMixin
from looplab.engine.verifier_tiebreak import VerifierTiebreakMixin
from looplab.engine.value_estimate import ValueEstimateMixin
from looplab.engine.research_cadence import ResearchCadenceMixin
from looplab.engine.finalize import (
    ensure_finish_report,
    finalize_run,
    finalize_scope_quiescent,
    incomplete_finalize_scope,
    is_guarded_abort,
    mark_finish_report_complete,
    scoped_finish_report,
)
from looplab.events.finalize_protocol import FINALIZE_STEP_BEGUN
from looplab.engine.holdout import HoldoutGrader
from looplab.engine.lessons import LessonMemory
from looplab.engine.options import EngineOptions
from looplab.engine.knobs import EngineKnobs, settle_knobs
from looplab.engine.workspace import WorkspaceSeeder
# Pure triage/fingerprint helpers extracted to looplab/engine/triage.py, imported back under
# their original names so `looplab.engine.orchestrator._rule_triage`, `._holdout_indices`
# (& friends) stay importable — tests import them from this module path. (`_normalize_error_sig`
# was re-exported here too until 2026-08-05; the error-signature guard it served was replaced by
# the triage model's own stop decision — see `engine/triage.py`'s module docstring.)
# `_MECHANICAL_MARKERS` was re-exported here too until 2026-08-20, when the stderr marker scan that
# chose the no-judge path's repair budget was DELETED — a bound on the text quality of a program's
# error output, i.e. the very thing the paragraph above says was already retired once. The name is
# gone rather than aliased: a test still importing it must go red, because what it guarded no longer
# exists in any spelling. See `engine/triage.py`'s obituary for that constant.
from looplab.engine.triage import (_MAX_DEP_ROUNDS,  # noqa: F401
                                   _dir_fingerprint, _failure_reason, _holdout_indices,
                                   _rule_triage, _shallow_fingerprint)
from looplab.core.models import BENIGN_TERMINAL_REASONS, Event, NodeStatus, RunState
from looplab.core.errors import ConfigRefusal, EnvironmentRefusal
from looplab.core.llm_budget import RunBudget
from looplab.core.phase_events import phase_sink_scope
from looplab.core.llm_broker import (LLMConcurrencyBroker,
                                     default_llm_lane_limits, in_llm_lane, llm_broker_scope,
                                     llm_lane_scope)
from looplab.search.card_selection import (
    META_CARD_ID, SpeculativeSelectionContext, card_budget_used, card_next_actions,
    refunded_node_reservations, speculative_card_actions, speculative_raw_actions,
)
from looplab.search.speculation_calibration import (
    # Re-exported too since ENG1-04 step 2 moved its two readers (the run-start pin and the re-entry
    # check) to `reentry.py`: `cli/run_cmds.py` and the tests import it from this module.
    SPECULATION_CALIBRATION_PROFILE_DIGEST,  # noqa: F401
    # Re-exported, not used here since doc 25 ES-01 moved the envelope to engine/speculation_gate.py:
    # the engine, the CLI and the tests all spell this on `engine.orchestrator`, and
    # tests/test_calibration_profile_home.py pins that the name did not move out from under them.
    SPECULATION_CALIBRATION_PROFILE_SETTINGS,  # noqa: F401
    SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS,
    SPECULATION_POLICY_SCOPE,
)
from looplab.search.policy import SearchPolicy, exploit_forced_action
# The strategist-cadence cluster (StrategyContext / make_policy / validate_strategy / coverage_signal
# / run_phase / operator_yields / NOVELTY_STANCES …) moved to engine/strategy.py (StrategyCadenceMixin),
# which imports those symbols from their canonical sources — so they are no longer imported here.
from looplab.events.replay import fold
from looplab.agents.roles import (Developer, Researcher, is_researcher_fallback,
                                  researcher_fallback_cause)
from looplab.runtime.sandbox import Sandbox
from looplab.core.tracing import (
    TRACE_EXPORT_FLUSH_TIMEOUT_MILLIS, AsyncJsonlSpanExporter, Tracer, current_ids)

# Re-export (back-compat): the engine sentinel lives in engine/options.py since the F3 knob
# collapse (the signature takes **knobs now, so the orchestrator itself no longer needs it);
# kept importable from this module path for pre-collapse importers.
from looplab.engine.options import _UNSET  # noqa: F401

_LOG = logging.getLogger(__name__)

# Back-compatible export: the source-owned definition lives beside the shared runtime-scope digest.
SPECULATION_CALIBRATION_VARIANT_FIELDS = SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS
# The immutable calibration profile and its digest live in `search/speculation_calibration.py`, which
# exists to own exactly this source-scoped identity (doc 25 SE-07). They are re-exported here because
# the engine, the CLI and the tests all spell them on this module.


# The reasons `looplab resume --drain-only` pauses with (doc 68 68.3a, `Engine._drain_only_turn`):
# nothing it owes is left, or a dispatch admitted none of what it owes. Module constants so the
# tests read the one spelling.
DRAIN_ONLY_PAUSE_REASON = ("drain-only resume: every reset or interrupted evaluation finished; "
                           "`looplab resume` (without --drain-only) continues the search")
DRAIN_ONLY_STUCK_REASON = ("drain-only resume: no evaluation could be admitted for node(s) {ids}; "
                           "`looplab resume` (without --drain-only) continues the search")


def drain_owed(state: RunState, node) -> bool:
    """Whether `looplab resume --drain-only` owes `node` an evaluation (doc 68 68.3a).

    Pending, not withdrawn (tombstoned, aborted), not waiting on the loop head's rebuild (a reset
    from `implement`/`propose`), and its CURRENT lifecycle either opened by a reset —
    `Node.attempt > 0`: a `node_reset`, or the epoch requeue a reset after holdout disclosure
    causes — or started an evaluation that never landed a terminal (`Node.eval_started`). A node
    the SEARCH built and has not dispatched is not owed: whether it runs at all is a search
    decision (a Card's freshness gate may yet discard it), so it waits for the next plain resume."""
    return bool(node.status is NodeStatus.pending and not node.tombstoned
                and node.id not in state.aborted_nodes
                and node.rerun_from not in ("implement", "propose")
                and (node.attempt > 0 or node.eval_started))


# ------------------------------------------------------------------ THE CADENCE OFFLOAD (F1i / EM-01)
#
# `_run_cadences` is the run's paid periodic block — the Strategist consult, the concept
# re-tag/consolidation pass, the verifier tie-break, the report refresh, the deep-research step and
# the lesson distillations. It was a plain `def` with no `await` in it, called as
# `state = self._run_cadences(state)` from the async spine, so nothing in it could ever yield: DRIVEN
# 2026-09-08, a tick-counter task reading from INSIDE a blocking stub at each site counted 177->177
# (strategist), 38->38 (report), 36->36 (concept) and 34->34 (verifier) — zero ticks, all four. Since
# `cadence.at_creation_boundary` those gates come due WHILE evaluations run, so the hold lands on top
# of a live GPU: no eval watcher tick, no operator abort/reset detection, no train-monitor kill and no
# control ACK for as long as the block spends.
#
# THE OFFLOAD IS THEREFORE A SINK, NOT A `to_thread`. All nine row types those cadences write are
# FOLDED and none is in `DIAGNOSTIC_EVENTS`, and the load-bearing one is `verifier_group_scored`: it
# MOVES the champion tie-break, so a worker-thread append landing inside a Card reservation's window
# is exactly the `score_moved` conjunct `card_reservation.py::_proposal_receipt_fence` discards an
# already-paid proposal on. Invariant #1 is the rule and this is its shape: the worker BUFFERS its
# folded intents and the MAIN TASK publishes them after the await, at the same point in the loop the
# cadence block always wrote at.
#
# WHY NOT `novelty.py::_offload_under_proposal_sink`. Its sink intercepts `_append_proposal_event`
# only — one funnel, four call sites — while the cadence cluster writes through `self.store.append`
# from eleven modules, and hoisting all of them onto a new funnel would be a rename across the
# cluster whose one forgotten site is a silent breach. The interception therefore goes where the
# writes already converge: `Engine.store` itself, which every cadence reads through, resolved per
# CONTEXT so only the worker's copied context sees the buffer. It is the same ContextVar discipline
# the proposal sink uses (set on the calling task immediately before a non-abandonable
# `to_thread.run_sync` hop, which copies the context into the worker), and its safety rests on the
# same two facts: the main task is suspended at that await, and every sibling task carries the
# context it was spawned with, so none of them can see this buffer.
_CADENCE_STORE_SINK: "ContextVar[Optional[_BufferedCadenceStore]]" = ContextVar(
    "looplab_cadence_store_sink", default=None)

# ONE thread, because there is one caller: `_run_cadences` has exactly one call site, on the loop
# task, awaited. A shared pool would be wrong in the other direction — anyio's default 40 tokens are
# held by every in-flight `_run_eval` for its whole multi-hour duration, so a cadence queued on it
# could wait behind the evaluations it is supposed to run BESIDE. Process-wide and lazily built so
# importing this module never touches the loop (`novelty.py::proposal_limiter`'s rule).
_CADENCE_THREADS = 1
_CADENCE_LIMITER = None


def cadence_limiter():
    """The dedicated pool the offloaded cadence block rides. One object per process."""
    global _CADENCE_LIMITER
    if _CADENCE_LIMITER is None:
        _CADENCE_LIMITER = anyio.CapacityLimiter(_CADENCE_THREADS)
    return _CADENCE_LIMITER


class _BufferedCadenceStore:
    """The store view a cadence worker sees: FOLDED rows are BUFFERED for the main task to publish,
    the two already-registered thread-side seams pass straight through, and reads see both.

    WHAT PASSES THROUGH AND WHY IT IS NOT A WIDENING. `DIAGNOSTIC_EVENTS` and
    `BACKGROUND_APPENDABLE` are invariant #1's own registries for exactly this situation — the first
    is fold-ignored AND excluded wholesale from every seq-equality fence, the second is the
    concurrent-research task's allow-list with a splice-neutrality proof
    (`tests/test_background_appendable.py`). Both are already appended from worker threads today, by
    the research task and by `core/phase_events.py`'s sink. Buffering them would be a REGRESSION in
    observability: `phase_progress` and the `agent_phase_*` moments are how the UI shows that a
    multi-minute cadence is alive, and `llm_usage` is the durable spend ledger a ceiling is read off.
    Everything else — every folded, authority-bearing row — is buffered.

    THE ORDER THIS PRODUCES is buffered-after-passthrough on the real log, and that is precisely what
    those two registries assert is safe: neither set's position is load-bearing. Nothing else moves,
    because the main task publishes at the same loop point the block always wrote at.

    A CAS APPEND IS REFUSED, not degraded. `expected_last_seq` is a promise about the tail of the
    REAL log at the instant of the append, and a buffered row cannot keep it — the publish happens
    later, against a tail that has moved by construction. No cadence uses one today; a future one
    that does gets a loud `TypeError` here instead of a silently unfenced write. `require_durable`
    goes the same way and for the same reason: it is a claim about bytes that are not written yet.
    """

    __slots__ = ("_store", "rows", "after_publish")

    def __init__(self, store):
        self._store = store
        # `(Event, trace_id, span_id)` per buffered row: the Event is what the worker's own reads
        # fold, the pair beside it is what the publish re-appends with. Kept together so a publish
        # cannot drift from what the worker was shown.
        self.rows: list = []
        # SIDE EFFECTS THAT MUST FOLLOW THEIR GATE (review 2026-09-22, ENG3-05): callables a
        # cadence registered through `Engine._after_durable`, run by `_offload_cadence` on the main
        # task only once every buffered row above is published. See that method for why.
        self.after_publish: list = []

    def __getattr__(self, name):
        # Everything that is not an append or a read is the real store's — `path`, the locks, the
        # repair helpers. A proxy that re-implemented any of them would be a second EventStore.
        return getattr(self._store, name)

    def _refuse(self, kwargs: dict) -> None:
        for key in ("expected_last_seq", "require_durable"):
            if kwargs.get(key) not in (None, False):
                raise TypeError(
                    f"a cadence worker cannot honour `{key}`: its folded rows are buffered and "
                    "published by the main task after the await, so the tail it would fence "
                    "against has already moved (see _BufferedCadenceStore)")

    def append(self, type: str, data: dict, **kwargs):
        if type in DIAGNOSTIC_EVENTS or type in BACKGROUND_APPENDABLE:
            return self._store.append(type, data, **kwargs)
        self._refuse(kwargs)
        trace_id, span_id = current_ids()
        # `deepcopy` for the same reason `novelty.py::_capture_proposal_events` deep-copies: the
        # caller keeps its dict and may go on mutating it, and what is published must be what the
        # cadence decided at the moment it decided it.
        event = Event(seq=self._next_seq(), ts=time.time(), type=type,
                      data=deepcopy(data), trace_id=trace_id, span_id=span_id)
        self.rows.append((event, trace_id, span_id))
        return event

    def append_many(self, records, **kwargs):
        # The atomic multi-row envelope degrades to per-row buffering, which is sound HERE and only
        # here: the whole buffer is published by one main-task call, so the group still lands as a
        # contiguous run of rows. `_refuse` above still rejects the tail fence that would make the
        # atomicity load-bearing.
        return [self.append(rtype, rdata, **kwargs) for rtype, rdata in records]

    def _next_seq(self) -> int:
        tail = self._store.read_all()
        highest = tail[-1].seq if tail else -1
        return max(highest, self.rows[-1][0].seq if self.rows else -1) + 1

    def read_all(self):
        """The real log with this worker's own buffered rows appended.

        The worker MUST see its own writes: `_run_cadences` threads one `state` through eleven
        consumers and several of them re-fold after writing (`_maybe_deep_research`,
        `_sync_card_enrichments`). A view that hid the buffer would hand the next consumer a state
        missing the row the previous one just decided — a stale chain, not a delayed one. The
        synthesized seqs are view-only: the publish assigns the real ones.
        """
        rows = self._store.read_all()
        if not self.rows:
            return rows
        return list(rows) + [event for event, _tid, _sid in self.rows]


def accountant_over_ceiling(engine: object) -> bool:
    """Is this run ALREADY at or past a spend ceiling — asked of the LEDGERS, not of an exception?

    THE FACT A CANCELLED SIBLING CANNOT CARRY. `_drain_inflight_evaluation` used to key entirely on
    `budget_stop_leaf(escaping)`, i.e. on the exception that reached `Engine.run`. That is exactly
    right when the ceiling was raised by the overlapped RESEARCH task, and it is blind in the case
    the marker above described: a `BudgetExceeded` raised INSIDE an evaluation (the repair path
    re-raises it; stage checks, triage and the repair critic are all paid calls) comes out of an
    `eval_tg` CHILD, so the group cancels `_run_with_llm_broker`, the outer handler catches a
    Cancelled whose leaf is None, and the drain no-ops — while every SIBLING evaluation, mid-score,
    loses the terminal for compute the run has already bought. Same five-runs-measured loss the
    drain was built for, one seam over.

    So ask the question the escaping exception cannot answer. The ceiling is a property of the RUN
    and both halves of it are held out of band: the `CostAccountant` family (`spent` against
    `limit`, the ceiling `core/llm.py::CostAccountant.add` raises on) and the reserve-commit
    `RunBudget` the broker meters at `borrow()` (`committed_cost`/`committed_tokens` against
    `cost_limit`/`llm_token_limit`). At-or-over on ANY of them is the same predicate those two
    classes refuse on, so this can only be true where the next paid call would raise anyway — which
    is also why it costs nothing: the drain it enables starts no new work and cannot spend.

    TOTAL AND NEVER RAISING. It runs on the teardown path, one frame from the `raise` that ends the
    run, and a run must not lose its budget receipt to an introspection error over a foreign role
    graph. An unreadable ledger answers False, i.e. exactly today's behaviour.
    """
    try:
        for accountant in find_cost_accountants(engine):
            limit = getattr(accountant, "limit", None)
            spent = getattr(accountant, "spent", None)
            if (isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0
                    and isinstance(spent, (int, float)) and not isinstance(spent, bool)
                    and spent >= limit):
                return True
        budget = getattr(engine, "_llm_budget", None)
        if budget is not None:
            cost_limit = getattr(budget, "cost_limit", None)
            committed = getattr(budget, "committed_cost", 0.0)
            if (isinstance(cost_limit, (int, float)) and not isinstance(cost_limit, bool)
                    and cost_limit > 0 and float(committed) >= float(cost_limit)):
                return True
            token_limit = getattr(budget, "token_limit", None)
            committed_tokens = getattr(budget, "committed_tokens", 0)
            if (isinstance(token_limit, (int, float)) and not isinstance(token_limit, bool)
                    and token_limit > 0 and int(committed_tokens) >= int(token_limit)):
                return True
    except Exception as exc:  # noqa: BLE001 — a teardown probe may never replace the run's own stop
        _LOG.debug("could not read this run's spend ledgers for the drain gate: %r", exc)
    return False


def _sole_task_group_error(group: BaseException) -> BaseException:
    """Unwrap a task group's LONE exception, so a failure's TYPE survives the group boundary.

    Backlog F1f put an `anyio` task group around the whole run so evaluations can outlive the Card
    session that admitted them.  anyio collapses even a single exception into a `BaseExceptionGroup`,
    and `Engine.run`'s failure type is a contract in two places: `_RefusalBoundaryGroup` in the CLI
    prints an `OperatorRefusal` as one line at `REFUSAL_EXIT_CODE` and everything else with a full
    traceback, and the suite asserts real types through `pytest.raises`.  A group of MORE than one is
    a genuine multi-failure and is re-raised unchanged — flattening that would drop failures.
    """

    while isinstance(group, BaseExceptionGroup) and len(group.exceptions) == 1:
        group = group.exceptions[0]
    return group


def _detect_gpu_ids() -> list[int]:
    """Best-effort list of usable GPU ordinals for the per-eval GPU pinning + `max_parallel=0` AUTO
    (evaluate.py). Honors an existing `CUDA_VISIBLE_DEVICES` (respect an operator/scheduler that already
    fenced the box), else asks torch, else `core/hardware.detect_gpus`. Returns [] when there is no GPU
    (CPU box / detection unavailable) — the caller then simply never pins and AUTO collapses to 1.
    Never raises.

    The last step used to count `nvidia-smi -L` output lines itself, which made this the SECOND
    nvidia-smi parser in the tree (doc 25 ES-10). `core/hardware` owns that probe: `query_nvidia_smi`
    is documented as the one launcher+CSV-splitter, and `detect_gpus` adds the comma-in-a-GPU-name
    repair the `-L` counter never needed but every other reader of the same binary does. Two parsers
    for one fact is how a box comes to report different GPU COUNTS to the pinning code and to the
    admission envelope, which is a discrepancy `engine/resources.py::detect_gpu_inventory` has a
    fail-closed guard for.
    """
    cvd = os.environ.get("CUDA_VISIBLE_DEVICES")
    if cvd is not None:
        # `schedulable_cuda_tokens` applies CUDA's OWN left-to-right truncation of an ordinal fence,
        # so this count is what a child process will actually see rather than how many ids were typed.
        # That matters because the count is a WIDTH: AUTO derives `eval_parallel` from it and
        # `run_started` pins the resolved integer permanently, so `CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7`
        # on a two-GPU box used to make a transient env typo the run's durable treatment — one every
        # later resume on the real box then ADOPTS (invariant #6). UUID/MIG fences and un-probeable
        # boxes are left exactly as spelled; see the helper for why it fails open everywhere else.
        ids = schedulable_cuda_tokens(cuda_visible_device_tokens(cvd)) or []
        # Ordinals INSIDE this fenced view are 0..n-1 regardless of the physical ids named in the var.
        return list(range(len(ids)))
    try:
        import torch  # optional
        n = int(torch.cuda.device_count())
        if n > 0:
            return list(range(n))
    except Exception:  # noqa: BLE001 — torch missing / driver error -> fall through
        pass
    try:
        from looplab.core.hardware import detect_gpus
        return list(range(len(detect_gpus())))
    except Exception:  # noqa: BLE001 — capability detection is best-effort by contract
        return []


# The confirm phase (engine/confirm_phase.py) and ablation (engine/ablation.py) clusters are
# MIXINS — pure file-level moves inherited unchanged, so every `self._confirm_phase(...)` /
# `self._ablate(...)` call site (and every test poking those names on Engine) is untouched.


class CreationRunawayCounters:
    """The two creation-level bounds the run loop carries from one turn to the next.

    Lifted out of `Engine._run_with_llm_broker` (doc 25 XP-06), where these were four loop-carried
    locals and the charging rule was thirteen lines in the middle of a 389-line function: no test
    could reach the RULE, only the whole simulated spin around it. Every comment below is the loop's
    own, unchanged apart from the counters losing their leading underscore.
    `tests/test_creation_runaway_guard.py` still drives both bounds end to end through a real run.
    """

    def __init__(self) -> None:
        # Creation-level runaway guard: if the loop keeps CREATING nodes while NO node reaches a
        # terminal (evaluated/failed), it is spinning — e.g. `fold` returning empty `nodes` makes
        # `_create_node` re-mint id 0 forever (the 184MB node_created(0) spin). The eval loop bounds
        # its own inline-repair runaway (the triage model's stop verdict + `inline_repair_attempts`),
        # but node CREATION had nothing. Local counters (not replayed) → on trip we
        # append run_finished (which IS replayed), so resume sees a cleanly-finished run.
        #
        # It charges nodes actually MINTED, counted from the LOG (`node_created` rows), not from the
        # planned `len(creates)` and not from `len(state.nodes)`. Both alternatives were wrong, in
        # opposite directions:
        #   * planned creates over-charge a lane that plans work and mints nothing — the Card lane
        #     stages/elects per turn, so a Card-side stall was reported as "node creation not
        #     converging" when not one node had been created. That is the misdiagnosis this counter
        #     caused for the whole speculation-depth defect, and the reason the message is now split;
        #   * folded `nodes` under-charges to zero in the exact spin the guard exists for: the
        #     empty-nodes fold that re-mints id 0 forever leaves `len(state.nodes)` at 0 every turn.
        # The log is the one view that sees both. `no_mint_turns` is the companion bound for the
        # other half — a create lane that keeps planning work and minting nothing — because a
        # mint-only charge on its own would turn that stall into an unbounded loop.
        self.created_no_terminal = 0
        self.prev_terminal = -1
        # `None` until the first observation: on RESUME the log already holds every earlier
        # `node_created`, and charging that history to this process's guard would false-trip a long
        # healthy run on its first loop turn. Only rows minted from here on are this loop's spin.
        self.minted_charged: Optional[int] = None
        # The REACH of that companion bound, which is narrower than "the loop is bounded".
        # `no_mint_turns` is incremented in exactly ONE place, `_handle_create_actions`,
        # which the loop reaches only through the `if creates:` branch. Every `continue` above it is
        # outside its reach, and at least two are real lanes: the speculation head-request/`buildings`
        # session and `_drop_stale_speculation` both restart the turn before `_select_actions` runs.
        # A loop confined to those advances NEITHER counter — `created_no_terminal` does not cover
        # the gap either, because it is charged only when the log gains `node_created` rows and BOTH
        # counters reset on any node reaching terminal, so a request → build → discard cycle (which
        # mints and terminalizes every pass) resets them every pass. What bounds that lane is
        # elsewhere: the refund cap (`search/card_selection.py::refunded_node_reservations`, one whole
        # operator budget) and the monotonic id ceiling (`_node_id_ceiling`, which never reuses an id).
        # A new `continue` above the create branch is an unbounded turn unless it carries its own
        # bound. The AUTO depth ratchet below carries one by being ONE-WAY: it settles the depth to 0,
        # which switches off `_speculation_enabled()` and with it the branch it returns through.
        self.no_mint_turns = 0

    def charge(self, *, minted_now: int, terminal_now: int) -> None:
        """Observe one loop turn: `minted_now` is the LOG's `node_created` count, `terminal_now`
        the folded count of nodes past `pending`.

        Three rules, and each one is load-bearing in a direction the other two are not.  The FIRST
        observation only calibrates (a resume inherits the whole log's history and must not be
        charged for it).  A mint is creation progress, so it clears the no-mint bound but not the
        mint bound.  ANY node reaching terminal is real progress and clears both.
        """
        if self.minted_charged is None:
            self.minted_charged = minted_now
        elif minted_now != self.minted_charged:
            self.created_no_terminal += max(0, minted_now - self.minted_charged)
            self.minted_charged = minted_now
            self.no_mint_turns = 0                   # a mint IS creation progress
        if terminal_now != self.prev_terminal:       # a node reached terminal (progress) -> reset
            self.created_no_terminal = 0
            self.no_mint_turns = 0
            self.prev_terminal = terminal_now


# Failures that are not evidence about the run. `superseded` is a node RESET (the operator or the
# engine replaced the node's generation) and an aborted node is an operator cancellation: charging
# either to a no-progress bound would let ordinary steering end the run.
# DERIVED, NOT SPELLED (review 2026-09-22, ENG1-08): this was `{"superseded"}` alone, a third
# hand-written copy of `core/models.py::BENIGN_TERMINAL_REASONS` that had already drifted — an
# operator's `card_dropped` (and a speculative build's `frozen`, a `proxy_skipped` candidate, a
# materialize-time `aborted`) counted as environment failures, so dropping three Cards before
# anything evaluated stopped the run blaming "the environment, dependencies or data". The registry
# is the ONE statement of "ended for a reason saying nothing about the experiment", and the
# failure-spike filter and the owner alert already derive from it
# (`tests/test_engine_terminal_reasons.py` pins all three readers).
_NON_EVIDENCE_FAILURE_REASONS = BENIGN_TERMINAL_REASONS


def systemic_failure_stop_reason(state, threshold: int) -> Optional[str]:
    """Should the whole run stop because nothing has EVER worked? The reason, or None.

    `CreationRunawayCounters` is the loop's only run-level no-progress bound and it resets on any
    TERMINAL — but `node_failed` is a terminal, so a run in which every node fails reads as
    progress and grinds on unbounded. Measured on `runs/rubertlite-dr-unified-v2` (2026-08-11):
    26 hours, 1,705 provider calls, 6 failed nodes, 0 evaluated, no stop. Every one of those
    failures was the SAME environment defect, re-diagnosed from scratch by a fresh Developer each
    time, because nothing in the loop was allowed to conclude "this is not about the idea".

    The distinction that matters is not how many nodes failed but WHETHER ANYTHING HAS EVER
    WORKED:

      * At least one evaluated node — the environment, the libraries and the data are PROVEN. A
        later failure is about that one idea, so only that node and its direction stop and the
        search continues. This bound is off entirely, whatever the failure count.
      * No evaluated node, ever — nothing is proven, and the N-th failure is evidence about the
        RUN rather than about the N-th idea. That is the systemic case ("we only ever start node
        zero and the environment/library/data is broken"), and the run should stop and say so
        instead of buying the same diagnosis N more times.

    Counted in DISTINCT nodes that ended failed, not in attempts: a node repaired five times and
    failed is one failed idea, and the inline-repair limit is what bounds that. Resets and
    operator aborts are excluded — see `_NON_EVIDENCE_FAILURE_REASONS`.

    `threshold <= 0` disables the bound, matching every other interval knob in the engine.
    """
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold <= 0:
        return None
    if state.evaluated_nodes():
        return None
    aborted = set(getattr(state, "aborted_nodes", None) or [])
    failed = [n for n in state.nodes.values()
              if n.status is NodeStatus.failed and not n.tombstoned and n.id not in aborted
              and str(getattr(n, "error_reason", "") or "") not in _NON_EVIDENCE_FAILURE_REASONS]
    if len(failed) < threshold:
        return None
    # Name the shape so the operator can act. The reasons are what the triage already recorded, so
    # this adds a diagnosis rather than a new opinion.
    reasons = sorted({str(getattr(n, "error_reason", "") or "unknown") for n in failed})
    return ("systemic failure: {n} node(s) failed and none has ever produced a metric — "
            "the environment, dependencies or data are the likely cause rather than any one idea "
            "({why})").format(n=len(failed), why=", ".join(reasons[:4]))


class Engine(ConfirmPhaseMixin, NoiseFloorMixin, AblationMixin, NoveltyGateMixin,
             StrategyCadenceMixin,
             ConceptCadenceMixin, VerifierTiebreakMixin, ValueEstimateMixin,
             ResearchCadenceMixin, EvalStagesMixin, CrashRepairMixin, EvalDispatchMixin,
             AuditMixin, ResourceSchedulingMixin, SpeculationMixin, EvaluateMixin, NodeBuildMixin,
             CardReservationMixin,
             ProposalCuesMixin,
             TrainingMonitorMixin, AshaMonitorMixin,
             WidthSettlingMixin, ReentryMixin, SetupPhaseMixin, ForcedRequestsMixin,
             # The pure-config knobs as non-data descriptors: an instance value always wins, and an
             # object that never ran `__init__` reads the library default (ENG1-03 step 4b).
             EngineKnobs,
             # Last: the cross-cluster members every other mixin may call (doc 25 ES-14). Kept at the
             # END of the MRO so a concern mixin that ever needs to specialize one can, exactly as it
             # could when they lived on the Engine body.
             SharedEngineMixin):
    @property
    def max_parallel(self) -> int:
        """Deprecated read-through alias for the canonical evaluation width.

        Keep the descriptor instead of a second instance attribute: integrations may continue to
        read or assign ``max_parallel``, but there is only one live value and new runtime code cannot
        observe a stale legacy copy.
        """
        return self._eval_parallel

    @max_parallel.setter
    def max_parallel(self, value: int) -> None:
        self._eval_parallel = value

    @property
    def parallel_build(self) -> int:
        """Deprecated read-through alias for the canonical LLM/build width."""
        return self._llm_parallel

    @parallel_build.setter
    def parallel_build(self, value: int) -> None:
        self._llm_parallel = value

    def __init__(
        self,
        run_dir: str | os.PathLike,
        *,
        task,
        researcher: Researcher,
        developer: Developer,
        sandbox: Sandbox,
        policy: SearchPolicy,
        options: Optional[EngineOptions] = None,
        crash_after: Optional[int] = None,
        # The setting NAMES the operator spelled explicitly at launch (`looplab run` `-s`/typed flags,
        # a Web/API launch's `settings`). Written into `run_started` ONCE, at first start, and read
        # back from there (never from here) — an explicitly launched width axis is an operator pin
        # the Strategist cannot override (`engine/widths.py::operator_width_axes`). A launch-surface
        # fact, not a Settings value, which is why it is a caller kwarg like `crash_after`.
        explicit_settings=(),
        # `looplab resume --drain-only` (doc 68 68.3a): evaluate what is pending, then pause — no
        # node is created, no forced request served, no cadence run (`_drain_only_turn`). A fact
        # about THIS invocation, never the run's, which is why it is a caller kwarg like
        # `crash_after` and is not recorded: the next plain `resume` searches as before.
        drain_only: bool = False,
        onboarder=None,
        # --- A7 Strategist + richer-operator knobs (config-first; defaults == today's behavior) ---
        strategist=None,            # Optional[Strategist]; None => static config policy (default)
        deep_researcher=None,       # Optional[DeepResearcher]; None => Deep-Research stage off
        report_writer=None,         # Optional[ReportWriter]; None => agent report off (deterministic only)
        developer_factory=None,     # Optional[Callable[[str], Developer]] for live backend swap
        developer_name="default",   # backend actually represented by the initial Developer object
        role_factory=None,          # Variant-1: Optional[Callable[[], (Researcher, Developer)]] building a
        #                             FRESH wired role pair for a parallel build worker (None => no pool =>
        #                             parallel_build clamps to 1). Typically `lambda: make_roles(task, settings)`.
        proxy_scorer=None,          # A6: Optional[ProxyScorer] early-signal candidate gate
        dep_installer=None,                  # Optional[Callable] install hook (test seam; default = deps.install)
        # D1 holdout-gated promotion (B6): reserve a fraction of host-held labels as a FINAL
        # holdout partition the search never sees; at finish, re-score the val-top-k on it and
        # (when holdout_select) let the unseen signal pick the champion. Host-graded tasks only
        # (label-partition holdout is free — the predictions already exist); 0.0 = off.
        # Phase 2 (D3/D4/T10/P4) knobs — kept on the engine so strategist-driven policy swaps
        # rebuild policies with the same run-wide settings.
        embedder=None,                       # text→vector callable (default: zero-dep hash_embed)
        lesson_abstractor=None,              # Memora synergy: harmonic recall over cross-run lessons
        loop_opts=None,                      # the operator's tool-loop options, for the engine's OWN
        #                                      agent loops (run-end reflection); None => the defaults
        _speculation_gate_calibration: bool = False,  # private mechanics-test/bootstrap seam
        _speculation_runtime_scope_sha256: Optional[str] = None,
        # Private CLI→Engine provenance seam. Narrow calibration/receipt paths independently
        # reconstruct this digest from their source-owned full Settings profile before trusting it.
        # BACKLOG §4 (docs/15 F3): every PURE-CONFIG knob — one per EngineOptions field — is
        # accepted via **knobs and validated against EngineOptions. Adding one is THREE edits on
        # this side, not the two this used to claim (review 2026-09-22, ENG1-03): the Settings
        # field, the EngineOptions field, and its `Knob` in `engine/knobs.py::EngineKnobs` — or,
        # for a knob that reads more than its own field, an `_opt` + `self.<attr>` below and an
        # `EXPLICIT_IN_INIT` reason (where each lands is derived,
        # `tests/test_engine_options.py::attr_by_field`). Each knob's type/default/why lives on
        # EngineOptions (engine/options.py), which mirrors the old signature comments.
        # Resolution per knob (unchanged): explicitly passed kwarg > `options` field > default.
        **knobs,
    ):
        # Resolve each pure-config knob ONCE, up front — explicit kwarg > options field > default —
        # so the assignment/validation body below is exactly the pre-EngineOptions code operating on
        # plain locals (no behavior change, no re-plumbing of the ~100 keyword call sites).
        if options is None:
            options = EngineOptions()
        # Unknown knob -> TypeError, exactly like a real keyword (a typo'd knob must not silently
        # fall back to the default). The field set IS EngineOptions — verified 1:1 by
        # tests/test_engine_options.py + tests/test_options_divergence.py.
        _fields = {f.name for f in dataclasses.fields(EngineOptions)}
        _bad = set(knobs) - _fields
        if _bad:
            raise TypeError(f"Engine() got unexpected keyword argument(s): {sorted(_bad)}")
        # THE LAUNCH RECORD (review 2026-09-22, ENG1-03 step 4a): every knob this Engine was asked
        # for, resolved once — explicit kwarg > `options` field > default — into ONE frozen
        # `EngineOptions`. It was a closure over two dicts, so the question "what was this engine
        # LAUNCHED with?" had no answer once `__init__` returned: 17 knob attributes are rewritten
        # after construction (a Strategist, a control override, a re-entry pin), and the launch value
        # survived only as whatever each attribute had not been overwritten with. `self.options` is
        # never rewritten. A duck-typed `options` still resolves field by field, as `_opt` did.
        self.options = (dataclasses.replace(options, **knobs) if dataclasses.is_dataclass(options)
                        else EngineOptions(**{f: knobs[f] if f in knobs else getattr(options, f)
                                              for f in _fields}))

        def _opt(field: str):
            return getattr(self.options, field)

        # EVERY PURE-CONFIG KNOB LANDS HERE, in one step (review 2026-09-22, ENG1-03 step 4c): each
        # attribute that is a function of ONE `EngineOptions` field is declared once, with its settle
        # rule and its why-comment, in `engine/knobs.py::EngineKnobs`, and this writes all 130 into the
        # instance dict before anything below reads one. They were 130 `_opt` locals and 130
        # assignments spread over this body, which a double could not see and a reader could not
        # enumerate. What stays below is only what reads more than its own field — the box, the task,
        # the roles, another knob — listed with its reason in `knobs.py::EXPLICIT_IN_INIT`.
        settle_knobs(self)

        # Layer-2 decoupling (docs/23): the CANONICAL `eval_parallel`/`llm_parallel` win over the legacy
        # `max_parallel`/`parallel_build` when set; None => fall back to the legacy field => byte-identical.
        _eval_parallel_opt = _opt("eval_parallel")
        _eval_parallel_value = (_eval_parallel_opt if _eval_parallel_opt is not None
                                else _opt("max_parallel"))
        _llm_parallel_opt = _opt("llm_parallel")
        _llm_parallel_value = (_llm_parallel_opt if _llm_parallel_opt is not None
                               else _opt("parallel_build"))
        max_nodes = _opt("max_nodes")
        merge_mode = _opt("merge_mode")
        novelty_mode = _opt("novelty_mode")
        novelty_gate = _opt("novelty_gate")
        agent_drives_actions = _opt("agent_drives_actions")
        speculation_depth = _opt("speculation_depth")
        speculation_gate_receipt = _opt("speculation_gate_receipt")
        metric_salvage = _opt("metric_salvage")
        metric_salvage_repair = _opt("metric_salvage_repair")
        auto_install_deps = _opt("auto_install_deps")
        digest_char_cap = _opt("digest_char_cap")

        self.run_dir = Path(run_dir)
        self.task = task
        self.researcher = researcher
        # P1: propagate the hypothesis-tracking knob to the researcher (LLMResearcher reads it;
        # UnifiedAgent forwards it to its inner researcher). Default-on already via the constructor;
        # this makes an explicit OFF reach the prompt. Best-effort (toy researchers ignore it).
        try:
            setattr(self.researcher, "track_hypotheses", self._track_hypotheses)
        except Exception:  # noqa: BLE001
            pass
        self.developer = developer
        self.sandbox = sandbox
        self.policy = policy
        # A7 Strategist: the policy is now hot-swappable, so the engine keeps the knobs needed to
        # rebuild it (n_seeds/max_nodes/ablate_every) + the meta-controller + operator-mix state.
        self.max_nodes = max_nodes
        # The policy's OWN node budget is the base a live add_nodes override extends — NOT self.max_nodes
        # (the engine default can differ from a passed-in policy's, e.g. in tests). Tracked separately so
        # the override is applied idempotently (absolute set per iteration) without compounding, and
        # re-captured on a strategy-driven policy swap below.
        self._base_max_nodes = getattr(policy, "max_nodes", max_nodes)
        self.strategist = strategist
        # In-process memo for `_maybe_consult_strategist`: the operator pin (plus the two live inputs
        # its whitelist consults) that last validated down to NO surviving fields. An invalid pin
        # "drifts" forever, and without this the strategy path rebuilt the whole StrategyContext on
        # every loop pass to re-derive the same no-op. Nothing durable keys off it — see there.
        self._invalid_pin_verdict: Optional[tuple] = None
        # In-process abstention memo for the value-estimate cadence (docs/BACKLOG.md §0.1 row 17):
        # the `(node_id, attempt)` pairs whose estimate came back unusable. Declared HERE rather
        # than minted on first use so it takes no row in `engine/attribute_sites.py`'s shrink-only
        # backlog — a read of a name nothing assigns answers a default instead of raising, which is
        # the whole reason that registry exists. Nothing durable keys off it: an abstention is
        # live-only and a resumed process may retry each node once, which is bounded.
        self._value_estimate_attempted: set[tuple[int, int]] = set()
        self.deep_researcher = deep_researcher
        self.report_writer = report_writer
        self.developer_factory = developer_factory
        self._developer_name = str(developer_name or "default")
        # Variant-1 parallel BUILD: a pool of fresh (researcher, developer) pairs so N drafts research +
        # code CONCURRENTLY without clobbering each other's role state (developer.last_files, researcher
        # hints). The settled canonical LLM width is the fan-out; the pool is built lazily on
        # the first parallel batch and clamped to what `role_factory` can supply (None => stays serial).
        self.role_factory = role_factory
        # NB: draft builds fan out via anyio.to_thread, whose default capacity limiter is 40 threads;
        # a `parallel_build` above that (le=64) just queues the excess (no deadlock — workers never
        # re-enter the loop), so effective build concurrency silently caps near 40. The value is a raw
        # opt here (0 = AUTO); it is resolved against the settled `self._eval_parallel` further down.
        # Layer-2: the canonical `llm_parallel` wins over the legacy `parallel_build` when set.
        self._llm_parallel_startup_opt = _llm_parallel_value
        self._llm_parallel = max(1, self._llm_parallel_startup_opt)  # provisional; re-resolved below
        self._role_pool: Optional[list] = None
        # A successful live Developer swap owns every subsequent build worker too. None means the
        # CLI factory's configured backend is still authoritative; a string means pooled developers
        # must be rebuilt through developer_factory under that exact Strategist-selected backend.
        self._pool_developer_override: Optional[str] = None
        # A0b/T8: "auto" resolves by Developer capability — code recombination is the verified
        # strongest merge (removing it costs ~9 pp), so it is the default wherever the Developer
        # actually GENERATES code (LLM/agent backends declare `is_code_generating`); templated/toy
        # developers keep the legacy mean-param merge (a code ensemble is meaningless there).
        if merge_mode == "auto":
            merge_mode = ("ensemble" if getattr(developer, "is_code_generating", False)
                          else "mean")
        self._merge_mode = merge_mode
        # doc 52 row 18: the plan's endgame reserve and whether its reserve sweeps the champion (a
        # Strategist may switch the sweep off through `operators.endgame_sweep`).
        self._endgame_sweep = True
        self._endgame_surrogate = None
        self._prefer_sweep = False   # A7: Strategist-set bias toward intra-node sweeps (audit-driven)
        # METRIC SALVAGE — settled through the same `_opt` ladder as every other policy, so a
        # snapshot/resume carries the operator's choice (invariant 6) instead of the class default.
        self.metric_salvage = settle_metric_salvage_mode(metric_salvage)
        self.metric_salvage_repair = bool(metric_salvage_repair)
        # Environment self-prep (deps.py): auto-install a missing KNOWN library and re-run, instead
        # of letting the crash-triage agent reject the idea. Trusted_local tier ONLY — the Docker
        # tiers run --network none and must not mutate a shared image. `_dep_attempted` records every
        # module we've already run pip for THIS run (one attempt per module: success => now present
        # forever; failure => won't change on retry), so an offline/misnamed package can't loop.
        # `_dep_lock` serializes pip + that set across parallel evals (pip is not concurrency-safe).
        self._auto_install_deps = bool(auto_install_deps) and self.trust_mode == "trusted_local"
        self._dep_installer = dep_installer        # None => deps.install (real pip)
        self._dep_attempted: set[str] = set()
        # Per-package install RECEIPTS ({pip name -> {requirement, declared, before, after}}), filled
        # by `_install_missing` and drained onto the `deps_installed` event by `_evaluate`. They are
        # produced under `_dep_lock` in a worker thread and consumed under `_write_lock` on the main
        # task, which is why they land here rather than being returned: `_install_missing` returns the
        # package NAMES its two callers already key on, and widening that return type would change
        # both call sites plus the injected-installer seam ~10 tests drive.
        self._dep_receipts: dict[str, dict] = {}
        # The repo's own dependency declaration, read once and cached by
        # `eval_dispatch.py::_declared_deps` (None until first asked). Both the run-setup install and
        # the crash-time pin lookup read THIS object, so a run cannot install one set of pins and
        # enforce another.
        self._deps_declaration = None
        # Declaration digests this run has already installed (the run's own baseline seeds it on
        # first use). Read and mutated under `_dep_lock` by `_sync_node_deps` — a check-then-act over
        # run-global state that two eval workers can reach at once — so it is created HERE rather
        # than lazily, which would itself be the race.
        self._deps_synced_digests: set[str] = set()
        import threading as _threading
        self._dep_lock = _threading.Lock()
        self.proxy_scorer = proxy_scorer
        if self.trust_gate not in ("audit", "gate", "block"):
            # A security control must fail LOUDLY: silently coercing a typo ("Gate") to "audit"
            # would run with no enforcement while the caller believes the gate is on.
            raise ConfigRefusal(
                f"trust_gate must be 'audit', 'gate' or 'block', got {self.trust_gate!r}")
        # novelty_mode is the primary selector; a legacy novelty_gate=True forces the "algo" path.
        # Read ONCE, here: it used to be relayed to `self._novelty_gate` as well, which nothing read
        # (review 2026-09-22, CORE-08).
        self._novelty_mode = str(novelty_mode or "llm") if not novelty_gate else "algo"
        # T5 semantic novelty (Phase 2): reject a proposal whose idea TEXT is a near-duplicate of
        # an existing node's — with one informed re-propose when the duplicate FAILED (the
        # ShinkaEvolve lever: novelty rejection before evaluation, ablation-ranked above model
        # routing). hash_embed is the zero-dep default; T4 wires a real embedder from config.
        if embedder is None:
            from looplab.tools.vectorstore import hash_embed as _he
            embedder = _he
        self._embedder = embedder
        self._idea_vecs: dict[tuple, list] = {}  # (len, prefix) of idea text -> embedding (in-memory)
        # M5: the Researcher's always-on digest budget (0 = auto-scale with run size).
        try:
            setattr(researcher, "_digest_cap", int(digest_char_cap))
        except Exception:  # noqa: BLE001 — toy researchers without attrs are fine
            pass
        # The researcher's copies of two prompt cues, threaded exactly like `_digest_cap` above (why
        # each has TWO deliveries — this one and the engine attribute — is beside its Knob in
        # `engine/knobs.py`; registry `roles.RESEARCHER_HINT_ATTRS`, so every wrapper mirrors them).
        try:
            setattr(researcher, "_memo_verdict_cue", self._memo_verdict_cue)
        except Exception:  # noqa: BLE001 — toy researchers without attrs are fine
            pass
        try:
            setattr(researcher, "_gpu_footprint_cue", self._gpu_footprint_cue)
        except Exception:  # noqa: BLE001 — toy researchers without attrs are fine
            pass
        # Novelty stance (Strategist-owned dial): how hard the proposer / foresight ranker / novelty
        # gate push for NEW directions. "balanced" == today's behavior; the Strategist raises it to
        # "explore" when coverage shows narrowing, or "exploit" to converge. Set by _apply_strategy.
        self._novelty_stance = "balanced"
        # Memora synergy: the SAME abstractor Memora uses for the case/KB index, applied to the
        # cross-run LESSONS tier so lesson retrieval gains anchor-expansion (harmonic recall)
        # instead of fingerprint-Jaccard alone. None (memora off) => the legacy Jaccard-only path.
        self._lesson_abstractor = lesson_abstractor
        # The operator's configured tool-loop options (`agents/tool_loop.py::loop_opts_from_settings`)
        # for the agent loops the ENGINE runs itself rather than a role — the run-end reflection and
        # skill distillation (`lessons_distill.py::_reflect_loop_opts`). The CLI holds the Settings
        # and hands them in; None (a bare Engine) keeps the defaults those loops always got. Review
        # 2026-09-22 (found by ENG1-03's knob census): they read `getattr(engine, "settings", None)`,
        # an attribute no real Engine has, so no operator's options ever reached them.
        self._loop_opts = loop_opts
        self._exploit_suite = None   # 4.3 hardened ruleset; loaded once memory_dir is set (below)
        # Cross-run memory / lessons / reflection cluster (looplab/engine/lessons.py). The Engine
        # keeps thin delegators under the original `_`-names below (tests call/monkeypatch them);
        # the lessons-owned mutable state (seen stamp, prior note) lives on LessonMemory.
        self.lessons = LessonMemory(self)
        # Unified self-driving agent: in unified mode `researcher is developer` (one object plays
        # both roles); `agent_drives_actions` additionally lets it pick the next macro action.
        self.agent_drives_actions = self.unified_agent and agent_drives_actions
        # GPU pool + max_parallel=0 AUTO. Multi-GPU boxes were used at 1/N: a single-command eval pins
        # itself to one GPU (or DataParallel-deadlocks on cleanup), leaving the others idle. To actually
        # parallelize, each concurrent eval is pinned to a DISTINCT GPU via CUDA_VISIBLE_DEVICES (see
        # evaluate.py::_evaluate); `max_parallel=0` means AUTO — run one experiment per detected GPU.
        # Settled HERE, ahead of the Layer-5 admission block below, because `speculation_depth = -1`
        # (AUTO) resolves off the settled eval width and the resolved integer is what the admission
        # envelope, the runtime-scope pin and `run_started` all have to agree on.
        self._gpu_ids: list[int] = _detect_gpu_ids()
        self._gpu_physical_ids, self._gpu_mem = detect_gpu_inventory(self._gpu_ids)
        # Which axes were spelled AUTO. Only an AUTO axis may ADOPT the width `run_started` pinned on
        # re-entry (`_repin_settled_widths`); an explicitly spelled width that disagrees with the pin
        # is a changed treatment and fails closed there. Same rule, same rationale as
        # `_speculation_depth_auto` below — AUTO is a request to let the BOX decide, and on re-entry
        # the run's own log outranks a different box.
        # Each flag mirrors its own resolver's AUTO test EXACTLY (the `== 0` branch below for evals,
        # `_resolve_llm_parallel`'s post-`int()` test for builds), so the two can never disagree about
        # whether this launch asked for AUTO.
        self._eval_parallel_startup_auto = (_eval_parallel_value == 0)
        try:
            self._llm_parallel_startup_auto = (int(self._llm_parallel_startup_opt) == 0)
        except (TypeError, ValueError):
            self._llm_parallel_startup_auto = False   # unparseable -> `_resolve_llm_parallel` returns 1
        if _eval_parallel_value == 0:                    # AUTO: the agent/operator lets the box decide
            # ...but only where the box is the constraint. AUTO means "one experiment per detected
            # GPU", so a task that declares itself CPU-locked has no GPU-derived width: `len(_gpu_ids)`
            # is then a coincidence, not a capacity estimate. `_task_gpu_capable` is the same signal,
            # with the same "absent means capable" rule, that already keeps such a task out of the
            # per-eval device reservation and the pool-wide host lease — "`_eval_parallel` and
            # `_gpu_ids` describe the BOX, not the work" (engine/resources.py). Deriving the WIDTH
            # from the box for work the box's GPUs cannot serve is that same category error one layer
            # up, and it costs determinism: two concurrent toy evals finish in wall-clock order, so
            # the documented offline smoke produced a different `node_evaluated` order run to run.
            # An explicitly spelled width is still honoured — an operator who wants CPU-parallel evals
            # asks for them by number.
            _eval_parallel_value = (max(1, len(self._gpu_ids))
                                    if self._task_gpu_capable() else 1)
        self._eval_parallel = max(1, int(_eval_parallel_value))
        # Now that eval_parallel is settled, resolve llm_parallel (0 = AUTO = eval_parallel), so a build
        # fan-out never exceeds what we can concurrently evaluate.
        self._llm_parallel = self._resolve_llm_parallel(self._llm_parallel_startup_opt)
        # docs/29 F1 — the LAUNCH treatment, kept because `_eval_parallel` stops being it. A
        # proposal-derived re-pin, a `budget_extend` and `_repin_settled_widths` all write the live
        # attribute, so by the time `_settle_proposal_width` needs a CEILING ("never widen past what
        # this run was authorized to run at") the live value is the last thing that moved it, and
        # using it would ratchet the run monotonically downward — one wide proposal could never be
        # undone. On a resumed run the ceiling comes from `run_started` instead (invariant #6: the log
        # outranks this box); this is the fallback for the first process and for a legacy log that
        # pinned nothing.
        self._eval_parallel_launched = self._eval_parallel
        self._llm_parallel_launched = self._llm_parallel
        # AUTO (-1) follows the same settled width; every other value is used as spelled. Resolving
        # BEFORE the local is read again keeps one settled integer flowing into the envelope checks,
        # the runtime-scope digest and the run_started pin — a hardware-derived depth must never reach
        # the durable log, or replay on another box would rebuild a different search treatment.
        speculation_depth, self._speculation_depth_auto = self._resolve_speculation_depth(
            speculation_depth)
        # Keep a settled, bounded scalar for the Layer-5 producer/consumer seam. Zero is a hard
        # off-switch; no task group/request event is allowed to infer a non-zero depth from hardware.
        self.speculation_depth = max(0, min(LLM_WIDTH_MAX, int(speculation_depth or 0)))
        self.speculation_gate_receipt = (
            str(Path(speculation_gate_receipt).expanduser().resolve())
            if speculation_gate_receipt is not None else None
        )
        self._speculation_gate_calibration = bool(_speculation_gate_calibration)
        # True only on the receiptless positive-depth lane: the operator's setting is the authority,
        # so no evidence identity (implementation digest / runtime scope) is minted or required.
        self._speculation_product_lane = False
        self._speculation_gate_admitted = False
        self._speculation_gate_receipt_digest = ""
        # Every spelling of THIS run's product-lane identity that re-entry accepts (the mintable one
        # plus superseded schema ids). Empty off the product lane: no other lane has an alternative.
        self._speculation_product_authority_tokens: frozenset[str] = frozenset()
        self._speculation_implementation_digest = ""
        self._speculation_policy_scope = ""
        self._speculation_calibration_profile_digest = ""
        self._speculation_calibration_gpu_inventory: list[dict] = []
        self._speculation_calibration_seed: Optional[int] = None
        self._speculation_runtime_scope_sha256 = ""
        _gate_receipt = None
        # The narrow calibrated envelope's inputs, snapshotted where its closures used to be
        # defined.  Safe to build once: none of these names is rebound after this point in
        # __init__, so both call sites below see exactly what the closures would have read.
        _calibration_runtime = CalibrationRuntime(
            option_fields=frozenset(_fields), read_option=_opt,
            recorded_runtime_scope=_speculation_runtime_scope_sha256,
            card_driven_selection=self.options.card_driven_selection,
            max_nodes=max_nodes,
            speculation_depth=speculation_depth,
            task=task,
            researcher=researcher,
            developer=developer,
            policy=policy,
            sandbox=sandbox,
            crash_after=crash_after,
            strategist=strategist,
            deep_researcher=deep_researcher,
            report_writer=report_writer,
            developer_factory=developer_factory,
            onboarder=onboarder,
            proxy_scorer=proxy_scorer,
            lesson_abstractor=lesson_abstractor,
            dep_installer=dep_installer,
        )

        # The lane decision itself lives in engine/speculation_gate.py beside the envelope it
        # consults (doc 25 XP-06); it stamps every `_speculation_*` identity this run re-enters on.
        admit_speculation_lane(self, _calibration_runtime, _gate_receipt)
        self._strategy_fidelity: Optional[str] = None   # None => use the Idea's own profile
        # Layer-2 compatibility lives solely in the two descriptors above. New runtime logic reads the
        # canonical attributes; legacy Engine(...) callers and direct assignments transparently feed them.
        # The canonical field is also the opt-in switch for the SHARED provider-call budget. An
        # unset field (including legacy-only parallel_build) and startup AUTO preserve historical
        # unbounded FOREGROUND overlap; only a positive canonical value activates a finite total.
        # The background lane caps are NOT part of that opt-in — `default_llm_lane_limits` applies them
        # with or without a total, because the producers they bound (both live-log watchdogs, per eval)
        # multiply with the eval width, which AUTO is precisely what derives from the box.
        try:
            _startup_llm_total = (min(LLM_WIDTH_MAX, int(_llm_parallel_opt))
                                  if _llm_parallel_opt is not None
                                  and int(_llm_parallel_opt) > 0 else None)
        except (TypeError, ValueError, OverflowError):
            _startup_llm_total = None
        # THE RUN'S SPEND BUDGET, one object every role's provider call reserves against at the
        # broker's permit (`core/llm_budget.py`, doc 52 row 15). Built before the broker so the
        # broker can carry it; fed by the durable ledger's sink (`engine/costs.py`) and seeded from
        # the `llm_usage` rows on a resume, so the cap holds across restarts.
        self._llm_budget = RunBudget(cost_limit=self._llm_cost_limit,
                                     token_limit=self._llm_token_limit)
        self._llm_broker = LLMConcurrencyBroker(
            total=_startup_llm_total,
            lane_limits=default_llm_lane_limits(_startup_llm_total),
            budget=self._llm_budget,
        )
        self._llm_lane_limits_explicit = False
        self._free_gpus: list[int] = list(self._gpu_ids)   # free-list handed out per concurrent eval
        # Every local Engine process otherwise sees the same physical devices as independently free.
        # Hold one crash-released OS lease while this Engine has any GPU reservation. It intentionally
        # serializes separate Runs at pool granularity because ordinal/UUID/MIG aliases are not safely
        # comparable across independently configured CUDA_VISIBLE_DEVICES environments.
        self._gpu_host_lease_path = (
            default_gpu_host_lease_path() if self._gpu_ids else None)
        self._gpu_host_lease_handle = None
        self._gpu_lock = threading.Lock()
        self._gpu_condition = threading.Condition(self._gpu_lock)
        self._gpu_epoch = 0
        self._eval_gpu_reservations: dict[tuple[int, int], dict] = {}
        # The eval-SECOND half of the same lifecycle reservation: what the lanes now running are
        # still going to charge against `max_eval_seconds`, so an admission gate can subtract it
        # instead of comparing only completed charges (`resources.py::eval_time_admission_blocked`).
        self._eval_time_reservations: dict[tuple[int, object], float] = {}
        self.timeout = _opt("timeout")
        self.crash_after = crash_after
        self._drain_only = bool(drain_only)
        self._explicit_settings = tuple(sorted({str(k) for k in (explicit_settings or ())}))
        # The width axes an OPERATOR owns (`engine/widths.py::operator_width_axes`), refreshed from the
        # fold before any Strategist width can be applied (`_apply_control_overrides`,
        # `_maybe_consult_strategist`, `_reentry_repin`); empty until the first of those runs.
        self._operator_width_axes: frozenset = frozenset()
        # 4.3: load the hardened exploit ruleset grown by `looplab harden` (hacker-fixer-solver)
        # from <memory_dir>/exploits.jsonl — merged into the reward-hack scan so every
        # previously-discovered exploit stays guarded on later runs. None => built-in detector only.
        if self.memory_dir and self.reward_hack_detect:
            _ep = Path(self.memory_dir) / "exploits.jsonl"
            if _ep.exists():
                try:
                    from looplab.trust.harden import ExploitSuite
                    self._exploit_suite = ExploitSuite.load(_ep)
                except Exception:  # noqa: BLE001
                    self._exploit_suite = None
        # RepoTask onboarding (Phase 3): `onboarder()` -> a proposed {eval_spec,
        # adapter_files, goal}; ratified per `eval_trust_mode` then frozen+trusted.
        self.onboarder = onboarder
        self._run_setup_done = False             # run-level (once) dependency setup guard
        self._run_setup_lock = _threading.Lock()   # _run_eval runs on parallel worker threads; the
        #   check-then-set on _run_setup_done races without this, launching run_setup (pip) N times
        self._drift_warned = False   # one-shot guard for the #8 drift-coverage warning
        # Serial Card-claim refusal ledger (see `_refuse_card_claim` / `_note_card_claim_refusal`):
        # the last refusal's reason, the exact lane it refused, and how many CONSECUTIVE turns it has
        # refused that lane. Local, not replayed — the retirement it drives IS durable.
        self._card_claim_refusal: Optional[str] = None
        self._card_claim_refusal_lane: Optional[tuple] = None
        self._card_claim_refusal_turns = 0
        # Fail loud at START, not mid-sweep: the untrusted tier needs docker, so verify it once
        # here instead of re-discovering (and re-scanning PATH) on every eval's make_docker_wrap.
        if self.trust_mode in ("untrusted", "hostile"):
            import shutil as _sh
            if not _sh.which("docker"):
                raise EnvironmentRefusal(
                    f"trust_mode={self.trust_mode!r} needs the docker CLI to sandbox evals, but it "
                    "was not found on PATH. Install Docker or use trust_mode='trusted_local'.")
            # Same rule, same place, for the OTHER value that can silently un-harden this tier: a
            # `sandbox_readonly_rootfs` docker cannot mount refuses HERE rather than in the first
            # node's first stage (`readonly_rootfs_argv` raises ConfigRefusal; "" is a no-op).
            from looplab.runtime.sandbox import readonly_rootfs_argv as _ro_argv
            _ro_argv(self.sandbox_readonly_rootfs)
        self._spec_activated = False
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # Declared HERE, not only by the `store` property's setter: `engine/attribute_sites.py`'s
        # rule is that every attribute the family reads has one declaring site, and
        # `__init__` is it.
        self._event_store = None
        self.store = EventStore(self.run_dir / "events.jsonl")
        # Bind after EventStore exists and before any role can make an LLM call. Paid usage now
        # survives process restarts in the same append-only source of truth as the run itself.
        #
        # And the CEILING survives them too, which it did not: a fresh process gets a fresh
        # `CostAccountant` at zero, so `looplab resume` handed an exhausted run a second full
        # budget (§213: `freeB3` resumed at $1.03 of a $1.00 ceiling and was stopped by pid at
        # $1.1056). Seeded BEFORE the bind so the tracker's baseline already contains it.
        seed_prior_spend(self)
        bind_cost_accountants(self)
        self._write_lock = anyio.Lock()
        # Node-id reservation lock (Variant-1 parallel build): serialises the CHEAP build prefix (fold ->
        # id=max(nodes)+1 -> parent-check -> node_building append) so PARALLEL `_create_node` threads get
        # DISTINCT monotonic ids. A threading.Lock (not the anyio _write_lock) because parallel builds run
        # in worker THREADS (anyio.to_thread). Uncontended on the serial path -> byte-identical.
        self._id_lock = threading.Lock()
        # Variant-1 parallel build: serialises the cross-run advisory-text computation + its receipt
        # capture in `_set_complexity_hint` so two concurrent draft builds can't clobber the shared
        # `self._cross_run_advisory_receipt` between one build's write and its per-build capture.
        # Uncontended on the serial path and no-op unless `cross_run_advisory` is on.
        self._advisory_lock = threading.Lock()
        # Tracing (I14): nested, correlated spans -> spans.jsonl (files-as-truth), bridged to
        # OpenTelemetry when the SDK is configured. Diagnostics only; never drives state.
        self.tracer = Tracer(AsyncJsonlSpanExporter(
                                 self.run_dir / "spans.jsonl", run_id=self.run_dir.name,
                                 lifecycle_fence=True),
                             run_id=self.run_dir.name,
                             capture_llm_io=self._trace_llm_io)
        # Who ends that lifetime. False => `Engine.run`'s own `finally`, which is right whenever the
        # coroutine returning IS the end of the run. A caller that still traces afterwards -- the
        # CLI's guarded-abort handler, which buys the finish report AFTER `run()` has raised -- calls
        # `defer_trace_retirement()` and owns `retire_tracer()` instead.
        self._trace_retirement_deferred = False
        # Last exporter-health snapshot PUBLISHED (not merely observed): the row is a state
        # change, so an exporter that stays broken is recorded once, not once per loop turn.
        self._trace_export_health_seen: tuple = ()
        # Task assets (e.g. the dataset) materialized into each node's sandbox workdir.
        assets = getattr(task, "assets", None)
        self._assets: dict = assets() if callable(assets) else {}
        self.task_has_columns = callable(getattr(task, "columns", None))   # I1: tabular task?
        # Out-of-process / host-side grading (B1+, general): a task may expose `host_grader()` ->
        # {"predictions": <file>, "scorer": <name>, "labels": <held-out answer key>, "key"?: ...}. When
        # present, the candidate (a separate sandbox process) writes ONLY predictions; the host (this
        # engine process) scores them — the labels live in engine memory and never touch the candidate
        # FS or the event log. Works for ANY solution.py-path task, not just MLEBench.
        hg = getattr(task, "host_grader", None)
        self._host_grader: Optional[dict] = hg() if callable(hg) else None
        # Host-grading/holdout cluster (looplab/engine/holdout.py) and workspace-seeding cluster
        # (looplab/engine/workspace.py). Like `self.lessons` above, the Engine keeps thin
        # delegators under the original `_`-names (tests + internal callers use them); both
        # wrappers read engine state live through their engine handle, so construction order
        # only matters relative to the first CALL (`_build_holdout_idx` just below needs
        # `self.holdout`; the first workspace call is in run()).
        self.holdout = HoldoutGrader(self)
        self.workspace = WorkspaceSeeder(self)
        # D1 holdout partition: a deterministic subset of the host-held labels reserved as the
        # final unseen signal. Every search/confirm eval is scored on the COMPLEMENT only; the
        # holdout rows are touched exactly once, at finish, to re-score the val-top-k. The
        # partition is a pure function of (n_labels, fraction) — identical across resume/replay,
        # no state to persist. Real MLE-bench (kind="mlebench") is graded by the official
        # out-of-process grader, which the engine cannot partition — skipped.
        # THE MLE-BENCH SEARCH SPLIT (doc 52 §5.1 row 3, `engine/holdout.py::apply_search_split`):
        # the original assets are kept aside so every (re)build of the partition carves from them.
        self._assets_public: Optional[dict] = None
        self._search_answers: Optional[str] = None
        self._search_hidden_ids: frozenset = frozenset()
        self._holdout_idx: frozenset = self._build_holdout_idx(self._holdout_fraction)
        # `refuse=False`: this construction reads the LIVE fraction, which `_reentry_repin` is about
        # to overwrite with the one `run_started` pinned (invariant #6). The refusal is made there,
        # against the value that actually decides the protocol — see `apply_search_split`.
        self._apply_search_split(refuse=False)
        self._holdout_epoch = 0
        # RepoTask (ADR-7): an existing repo the agent edits + a command-based eval.
        rs = getattr(task, "repo_spec", None)
        self._repo_spec: dict = rs() if callable(rs) else {}
        es = getattr(task, "eval_spec", None)
        self._eval_spec: dict = es() if callable(es) else {}
        # The operator's LIVE per-eval budget (`budget_extend{eval_timeout}`), re-read off the fold by
        # `_apply_control_overrides` on every turn — None until one is recorded. `_eval_spec` itself
        # stays the task's own recorded spec; every budget/timeout reader asks
        # `shared.py::effective_eval_spec`, which applies this to a copy.
        self._eval_timeout_override: Optional[float] = None
        # The on-the-fly control watcher (`forced_requests.py::_control_watch_loop`): armed by the
        # run loop's head, idle while the head keeps turning; `_inject_lanes_inflight` counts the
        # Card session's concurrent inject builds (`_card_phase_serve_operator_inject`).
        self._control_watch_armed: bool = False
        self._control_watch_scope = None
        self._loop_head_monotonic: float = 0.0
        self._inject_lanes_inflight: int = 0
        # Ablation probes run via the solution.py sandbox path, which is wrong for a repo/eval-spec
        # run (the repo tree is absent) — so `_ablate` no-ops there. Tell the policy not to PROPOSE
        # ablate on such runs: the skip creates no refine_block node, so the ablate cadence would
        # never clear and the loop would spin forever (re-stamped on every policy rebuild, see
        # strategy.py::_apply_strategy). The flag is read via getattr so any policy object is safe.
        self._ablation_capable: bool = not (bool(self._repo_spec) or bool(self._eval_spec))
        self.policy.ablation_capable = self._ablation_capable
        # Fail loudly: a repo task with no trusted eval AND no onboarder would silently
        # evaluate every node via the empty solution.py path. Require one or the other.
        if self._repo_spec and not self._eval_spec and onboarder is None:
            raise ConfigRefusal(
                "RepoTask has no eval and no onboarder: set `onboard: true` with "
                "backend=llm (so an onboarder is built), or provide `eval` in the task.")

    # --------------------- workspace materialization (extracted to engine/workspace.py)
    # The workspace seeding / materialization cluster lives in looplab/engine/workspace.py
    # (`WorkspaceSeeder`, constructed as `self.workspace` in __init__). These thin delegators
    # keep the ORIGINAL method names on the Engine — tests call e.g. `engine._write_node_files`
    # / `engine._seed_workspace` directly — and WorkspaceSeeder routes its internal cross-calls
    # back through them, so an instance-level monkeypatch intercepts every path.
    def _write_assets(self, workdir) -> None:
        return self.workspace.write_assets(workdir)

    def _write_node_files(self, node, workdir) -> None:
        return self.workspace.write_node_files(node, workdir)

    def _materialize(self, node, workdir) -> None:
        return self.workspace.materialize(node, workdir)

    # ------------------------------------------------------------ loop control
    def _ack_commands(self, events) -> None:
        """Causally acknowledge every marked server command this engine has folded.

        The ack is replay-neutral diagnostics.  It names both command id and exact intent sequence,
        so an unrelated engine/background event can never be mistaken for command observation. The
        caller passes the exact snapshot used for ``fold``: a second read here could include a command
        appended after the fold and falsely acknowledge an intent this iteration never observed.

        A long-running engine calls this at every decision boundary.  Keep a local cursor over the
        exact ``EventStore`` snapshot: the first call bootstraps the historical acknowledgement set,
        while later calls inspect only the appended suffix.  ``EventStore.read_all`` retains Event
        object identity across ordinary appends and rebuilds the cache on replacement/rewrite, so a
        changed first object (or a shorter snapshot) safely invalidates the cursor.  The attributes
        are initialized lazily because a few focused tests construct ``Engine`` with
        ``object.__new__``.
        """
        total = len(events)
        initialized = bool(getattr(self, "_command_ack_initialized", False))
        cursor = int(getattr(self, "_command_ack_cursor", 0)) if initialized else 0
        first = events[0] if total else None
        cached_first = getattr(self, "_command_ack_first_event", None)
        invalidated = initialized and (
            cursor > total or (cursor > 0 and (first is None or first is not cached_first)))
        if invalidated:
            cursor = 0
            acked: set[tuple[str, object]] = set()
        else:
            # Copy, not alias: the dedup passes below mutate ``acked`` in place, but the durable
            # seen-set must not advance until every ack row is appended — otherwise a failed append
            # marks an unwritten ack as seen and it is lost for the process lifetime.
            acked = set(getattr(self, "_command_ack_seen", set()))

        # Two passes over the *new suffix* matter: an already-durable ack later in that same suffix
        # must suppress its intent even when the intent row appears first.
        for index in range(cursor, total):
            event = events[index]
            if event.type == EV_COMMAND_ACK:
                acked.add((str((event.data or {}).get("command_id")),
                           (event.data or {}).get("event_seq")))

        pending: list[tuple[str, int]] = []
        for index in range(cursor, total):
            event = events[index]
            command_id = (event.data or {}).get("_command_id")
            identity = (str(command_id), event.seq)
            if command_id and identity not in acked:
                acked.add(identity)
                pending.append(identity)

        # Append the diagnostics FIRST, then commit the process-local cursor/seen against the exact
        # folded snapshot. A crash before the commit is harmless (a restart re-bootstraps from cursor
        # 0); a NON-fatal append failure is now also harmless — because the cursor and seen-set stay
        # unadvanced, the next call re-scans this suffix and re-attempts the un-acked intents (the
        # already-appended acks are re-observed and deduped in the first pass). A subsequent call sees
        # the new ack rows in its suffix.
        for command_id, event_seq in pending:
            self.store.append(EV_COMMAND_ACK, {
                "command_id": command_id, "event_seq": event_seq,
            })
        self._command_ack_initialized = True
        self._command_ack_cursor = total
        self._command_ack_first_event = first
        self._command_ack_seen = acked

    def _begin_finalize(
            self, data: dict, *, scope: str | None = None,
            finish_report_planned: bool = False, after_seq: int | None = None) -> str:
        """Durably stage one exact terminal payload and return its stable wrap-up scope.

        ``after_seq`` is the natural-finish decision CAS. The EventStore check prevents even an
        invalid marker from landing when a control won before the claim; replay also validates the
        physical adjacency for defense in depth.
        """
        scope = scope or f"finalize:{secrets.token_hex(16)}"
        already_begun = any(
            event.type == EV_FINALIZE_STEP and (event.data or {}).get("scope") == scope
            and (event.data or {}).get("step") == FINALIZE_STEP_BEGUN
            for event in self.store.read_all())
        if not already_begun:
            payload = {
                "scope": scope,
                "step": FINALIZE_STEP_BEGUN,
                "finish_data": dict(data),
                "finish_report_planned": bool(finish_report_planned),
            }
            kwargs = {}
            if after_seq is not None:
                payload["after_seq"] = after_seq
                kwargs["expected_last_seq"] = after_seq
            self.store.append(EV_FINALIZE_STEP, payload, **kwargs)
        return scope

    def _finish_run(self, data: dict, *, scope: str | None = None) -> None:
        """Open one durable finalization scope, then publish its terminal run event.

        The begun marker precedes ``run_finished``. A hard kill after the terminal event is therefore
        distinguishable from a fully projected run, and re-entry can finish the same scope without
        reopening search or repeating already-gated paid wrap-up work.
        """
        scope = self._begin_finalize(data, scope=scope)
        self.store.append(EV_RUN_FINISHED, {**data, "finalize_scope": scope})

    def _refuse_finish_over_adopted_evals(self) -> bool:
        """QUIESCENCE now includes running evaluations, not just a still log (backlog F1f).

        Before the eval task group was hoisted to run scope, every finish decision was structurally
        preceded by a session join, so "the log has not moved since `after_seq`" was the whole of
        quiescence.  A session may now return with GPUs still burning, so the same `after_seq` CAS
        can succeed while a node is mid-training — and `_finish_with_report_if_quiescent` would then
        buy a paid report, name a champion and publish a budget summary over a metric that does not
        exist yet.  Doc 33 calls this out as the dangerous failure of option 1 ("finalization races a
        running eval and finishes the run over live work"), which is why it lands in the SAME change.

        A REFUSAL plus a drain request, not an inline wait: the two finish helpers are sync and are
        reached from five gates, so the loop drains on its next turn and the gate then succeeds —
        one extra turn, no busy spin, and the finish contract itself is untouched.
        """

        if not self._evals_inflight():
            return False
        self._eval_drain_requested = True
        return True

    def _finish_if_quiescent(self, data: dict, *, after_seq: int) -> bool:
        """CAS-claim a scoped terminal intent and publish it only while the log stays quiescent.

        The begin marker is the first adjacency claim. ``run_finished`` then names that marker as its
        immediate predecessor and opts into the exact-finish crash handshake.
        """
        if self._refuse_finish_over_adopted_evals():
            return False
        scope = f"finalize:{secrets.token_hex(16)}"
        try:
            self._begin_finalize(data, scope=scope, after_seq=after_seq)
        except EventStoreConcurrencyError:
            return False
        events = self.store.read_all()
        begun = next(
            event for event in reversed(events)
            if event.type == EV_FINALIZE_STEP
            and (event.data or {}).get("scope") == scope
            and (event.data or {}).get("step") == FINALIZE_STEP_BEGUN
        )
        try:
            finished = self.store.append(
                EV_RUN_FINISHED,
                {
                    **data,
                    "after_seq": begun.seq,
                    "finalization_required": True,
                    "finalize_scope": scope,
                },
                expected_last_seq=begun.seq,
            )
        except EventStoreConcurrencyError:
            return False
        return finished.seq == begun.seq + 1

    def _finish_with_report_if_quiescent(
            self, state: RunState, data: dict, *, after_seq: int) -> bool:
        """Write one scoped paid report and finish as an adjacency-checked CAS chain.

        The provider attempt is guarded by ``report_begun``. A crash retry can reuse the durable
        report or record an ambiguous attempt, but can never buy it again. The successful report event
        remains immediately before ``run_finished`` as required by replay.
        """
        report_planned = self.report_writer is not None and self.report_every > 0
        if not report_planned:
            return self._finish_if_quiescent(data, after_seq=after_seq)

        if self._refuse_finish_over_adopted_evals():
            return False
        scope = f"finalize:{secrets.token_hex(16)}"
        try:
            self._begin_finalize(
                data,
                scope=scope,
                finish_report_planned=True,
                after_seq=after_seq,
            )
        except EventStoreConcurrencyError:
            return False
        if not ensure_finish_report(self, self.store.read_all(), scope, state=state):
            return False

        events = self.store.read_all()
        if not finalize_scope_quiescent(events, scope):
            self.store.append(EV_FINALIZE_STEP, {
                "scope": scope,
                "step": "abandoned",
                "outcome": "decision_snapshot_changed_during_report",
            })
            return False

        report = scoped_finish_report(events, scope)
        tail_seq = events[-1].seq if events else -1
        if report is not None and report.seq != tail_seq:
            # Only diagnostics may have followed; clone the durable content without another provider
            # call so report->finish is adjacent again. A background-appendable event (an `llm_usage`
            # from a cost sink) can splice in between this tail read and the CAS, exactly like the
            # finish CAS below — abandon the scope on a lost race instead of crashing the finish path.
            try:
                report = self.store.append(
                    EV_REPORT_GENERATED,   # the registry constant, not a literal (invariant #7: a typo'd literal silently no-ops)
                    dict(report.data or {}),
                    expected_last_seq=tail_seq,
                )
            except EventStoreConcurrencyError:
                self.store.append(EV_FINALIZE_STEP, {
                    "scope": scope,
                    "step": "abandoned",
                    "outcome": "event_won_report_clone_cas",
                })
                return False
            tail_seq = report.seq
        try:
            finished = self.store.append(
                EV_RUN_FINISHED,
                {
                    **data,
                    "after_seq": tail_seq,
                    "finalization_required": True,
                    "finalize_scope": scope,
                },
                expected_last_seq=tail_seq,
            )
        except EventStoreConcurrencyError:
            self.store.append(EV_FINALIZE_STEP, {
                "scope": scope,
                "step": "abandoned",
                "outcome": "event_won_report_to_finish_cas",
            })
            return False
        mark_finish_report_complete(self, scope)
        return finished.seq == tail_seq + 1

    def _record_trace_export_health(self) -> bool:
        """Publish the span exporter's own health while it is failing. Returns whether a row landed.

        The exporter's loss receipt is written BY the exporter, so a worker that has stopped
        reports nothing at all: v12 wrote its last span at 18:20 and kept appending events for the
        next ten and a half hours with no receipt, no warning and no surface — `metrics()` carried
        the whole story (`worker_alive`, `shutdown`, per-reason drops) and was read by NOTHING in
        the product. The engine is the one writer that outlives a dead exporter, so it publishes
        that snapshot here, on the run's own log.

        DIAGNOSTIC and dedup'd: `trace_export_unhealthy` keeps a healthy run's log untouched, and
        `trace_export_health_signature` keeps a permanently-dead exporter to one row per distinct
        state — the fields that DECIDE health, because the snapshot's counters are monotonic and
        keying on them makes "distinct state" mean "another turn happened".
        """
        exporter = getattr(getattr(self, "tracer", None), "exporter", None)
        snapshot_fn = getattr(exporter, "metrics", None)
        if not callable(snapshot_fn):
            return False
        try:
            snapshot = dict(snapshot_fn())
        except Exception:  # noqa: BLE001 - a diagnostic read must never stop the run loop
            return False
        if not trace_export_unhealthy(snapshot):
            return False
        # The DECIDING fields only — never the whole snapshot. `metrics()` also carries
        # `accepted_spans`/`exported_spans`/`queued_spans`/`buffered_bytes`, which move with
        # essentially every span, while `trace_export_unhealthy` LATCHES on cumulative counters that
        # are never reset. Keyed on the whole dict, "one row per distinct state" therefore became
        # one fsync'd row per outer-loop turn for the rest of the run after a single transient drop.
        # The derivation lives beside the predicate so the two cannot come apart.
        fingerprint = trace_export_health_signature(snapshot)
        if fingerprint == self._trace_export_health_seen:
            return False
        self._trace_export_health_seen = fingerprint
        self.store.append(EV_TRACE_EXPORT_HEALTH, snapshot)
        return True

    async def run(self) -> RunState:
        """Run under one shared broker context inherited by anyio tasks and worker threads."""
        # The engine's OWN main-loop thread. The concurrent build fan-out dispatches `_create_node` to
        # `anyio.to_thread` WORKER threads; comparing a caller's thread against THIS ident (not the
        # process `main_thread()`) lets board-wide emitters tell a real fan-out worker from a serial
        # main-task build even when a host embeds a serial Engine in its own worker thread (peer review).
        import threading
        self._main_loop_thread_ident = threading.get_ident()
        broker = getattr(self, "_llm_broker", None)
        if broker is None:  # defensive for test/library engines constructed through __new__
            broker = self._llm_broker = LLMConcurrencyBroker(
                budget=getattr(self, "_llm_budget", None))
        try:
            with llm_broker_scope(broker), llm_lane_scope("engine"), \
                    phase_sink_scope(self._append_phase_event):
                # THE RUN-SCOPED EVAL TASK GROUP (backlog F1f, doc 33 option 1 — "adopting
                # sessions").  Evaluation children used to belong to whichever `_run_card_session`
                # admitted them, and that session could not return until the LAST of them drained.
                # So the run stopped STARTING work at the FIRST terminal and still reached the outer
                # loop no sooner: 115.6 GPU-h of idle second slot across the six width-2 runs on this
                # box, against 164.4 GPU-h of work actually done.  Owning the group HERE makes a
                # session turn a DECISION boundary instead of a QUIESCENCE one — it returns, the
                # outer loop takes its turn (cadences, acks, control overrides, forced requests,
                # budget refresh, runaway charge, Card inventory), and the next session ADOPTS
                # whatever is still burning.
                #
                # Two things make this a LIFETIME change and not a WRITER change, which is why it
                # needs no new exception to engine invariant #1: the children are anyio tasks on
                # this same event loop (never threads), and every one of the nine node-terminal
                # appends in `engine/evaluate.py` is lexically inside `async with self._write_lock`.
                # `_record_eval_start_boundary` stays on the main task at the dispatch decision,
                # exactly where the invariant says to keep it.
                #
                # It is opened HERE rather than around `_run_with_llm_broker`'s turn loop only to
                # avoid re-indenting ~300 lines of that loop for a structural change; the lifetime
                # is the same either way.  `_run_with_llm_broker` drains adopted evals itself before
                # `finalize_run`, so this group's join is a backstop, not the quiescence rule.
                # `_eval_inflight` must exist before the first turn: the loop's own freshness drain
                # and its terminal gates read it whether or not a session has been entered yet.
                self._ensure_speculation_state()
                try:
                    async with anyio.create_task_group() as eval_tg:
                        self._eval_task_group = eval_tg
                        # Operator controls applied ON THE FLY while the loop is inside a long
                        # step (`forced_requests.py::_control_watch_loop`). Looked up defensively:
                        # `Engine.run` is borrowed by host stubs that are not Engines.
                        _start_watch = getattr(self, "_start_control_watch", None)
                        if callable(_start_watch):
                            _start_watch(eval_tg)
                        try:
                            return await self._run_with_llm_broker()
                        except BaseException as escaping:
                            if callable(_start_watch):
                                self._stop_control_watch()
                            # THE CEILING MUST NOT DISCARD WORK IT HAS ALREADY PAID FOR.  See
                            # `_drain_inflight_evaluation` for the measurement and the whole
                            # argument; the raise below is unconditional, so the hard stop is
                            # unchanged in class, message and timing-relative-to-finalization.
                            await self._drain_inflight_evaluation(escaping)
                            raise
                        finally:
                            if callable(_start_watch):
                                self._stop_control_watch()
                            self._eval_task_group = None
                except BaseExceptionGroup as group:
                    # A task group collapses even a LONE exception into a group, and `Engine.run`'s
                    # failure TYPE is a contract: `cli/__init__.py::_RefusalBoundaryGroup` prints an
                    # `OperatorRefusal` as one line at exit code 2 and gives everything else a
                    # traceback at exit 1 (CLAUDE.md — "a deliberate refusal is a TYPE, not a
                    # message"), and ~40 tests assert the type through `pytest.raises`. Wrapping a
                    # `ConfigRefusal` in an ExceptionGroup would put every operator refusal back in
                    # the 42-lines-of-frames presentation that split removed. Unwrap the single-
                    # exception case and let a genuine multi-failure group through as itself.
                    raise _sole_task_group_error(group) from None
        finally:
            # The raising exits' half of the run-loop exit receipt (see `_record_run_loop_exit`):
            # a no-op when the fall-through already recorded it or the loop was never entered. It runs
            # BEFORE the exporter is retired, so the receipt still reaches an open trace.
            self._record_run_loop_exit()
            # ONE exporter lifetime per run, and it must end before the lifecycle lock may be
            # released: a background span that closes after that point would append behind a
            # reset/clear instead of being rejected.  `retire_tracer` is that terminal barrier.
            #
            # It is DEFERRED when the caller will still trace after this coroutine returns
            # (`defer_trace_retirement`).  `cli/run_cmds.py::_run_engine_guarded` is exactly that
            # caller: its outer handler writes the terminal AND buys the finish report, several
            # frames above this `finally`.  See `defer_trace_retirement` for the measurement.
            # BOTH lookups are defensive, and the second is not paranoia: `Engine.run` is borrowed
            # by host stubs that are not Engines at all -- `_RunHost` in
            # `tests/test_budget_ceiling_drains_the_inflight_eval.py` -- and by
            # `Engine.__new__(Engine)` probes that never ran `__init__`.
            # The code this replaced was defensive for exactly that reason
            # (`getattr(getattr(self, "tracer", None), "shutdown", None)`); moving the guard inside
            # `retire_tracer` left the METHOD lookup itself unguarded, and those three drain tests
            # caught it. `hasattr` rather than a local alias, so the call site keeps the literal the
            # source pin in `tests/test_async_trace_exporter.py` reads.
            if not getattr(self, "_trace_retirement_deferred", False) \
                    and hasattr(self, "retire_tracer"):
                self.retire_tracer()

    def defer_trace_retirement(self) -> None:
        """Hand this run's exporter lifetime to the caller, which MUST call `retire_tracer`.

        THE DEFECT, measured on the 2026-08-24 campaign (docs/53 §2c). Fifteen `report_generated`
        rows across the 30-run corpus (eleven of them under `runs-B`) carry a `span_id` whose span
        is in NO artifact -- not `spans.jsonl`, not `.spans-append.jsonl`, not `trace.json` -- and
        no `looplab.exporter.loss` receipt anywhere names the loss. The corpus splits cleanly: every
        one of them is the `trigger="finish"` report of a run that ended on the spend ceiling, and
        every run that ended otherwise kept its report span.

        THE CAUSE, and it is NOT "a span vanished between close and flush" as the item was filed.
        The span never reached the exporter's queue at all. A ceiling hit escapes `Engine.run`, so
        this `finally` retires the exporter; the CLI's guarded handler THEN opens
        `tracer.span("report")` on the way to `run_finished`. `AsyncJsonlSpanExporter.export`
        refuses a post-shutdown row and records the drop with `durable=False` -- deliberately, so a
        terminal exporter cannot be resurrected as a receipt writer behind a trace reset. Refusing
        AND leaving no receipt is right for a straggler from a background thread; it is wrong for
        the run's own terminal report, which is synchronous, on the main thread, and still inside
        the engine lock.

        SO THE LIFETIME MOVES, NOT THE FENCE. The owner that writes the terminal owns the trace, and
        it still retires it inside the same lock scope `Engine.run` held. Nothing about the barrier,
        the abandon-on-timeout or the writer guard changes; a span that closes after the OWNER is
        done is refused exactly as before.
        """
        self._trace_retirement_deferred = True

    def retire_tracer(self) -> None:
        """Make the exporter's final barrier terminal. Idempotent: `shutdown` is one-shot.

        Drains accepted work and, on its bounded timeout, atomically abandons anything that has not
        crossed the lifecycle writer fence. Python still cannot interrupt an in-progress filesystem
        call; a crossed writer keeps the fence until it is done.
        """
        _trace_shutdown = getattr(getattr(self, "tracer", None), "shutdown", None)
        if not callable(_trace_shutdown):
            return
        try:
            _stopped = bool(_trace_shutdown(
                timeout_millis=TRACE_EXPORT_FLUSH_TIMEOUT_MILLIS))
        except Exception:  # noqa: BLE001 - never mask cancellation/domain failure in finally
            _stopped = False
        if not _stopped:
            _LOG.warning(
                "trace exporter did not stop before lifecycle release; pending rows were "
                "abandoned behind the trace-writer fence")

    async def _drain_inflight_evaluation(self, escaping: BaseException) -> None:
        """Let an evaluation that is ALREADY BURNING land its terminal before the spend ceiling
        tears the run-scoped eval task group down.  No-op for every other failure.

        THE DEFECT, measured on the 2026-08-24 campaign (`runs-B`).  Five of the twenty task-arms
        finished with one more `score.log` on disk than they had `node_evaluated` events:
        `integer_factorization` 4 node dirs / 4 score.logs / **3** events (the lost score was
        4.0958), `spectral_clustering` 2/2/**1**, `max_clique_cpsat` 7/7/**6**,
        `min_dominating_set` 3/3/**2** (1.0804), `multi_dim_knapsack` 5/5/**4** (2.8004, against a
        champion of 2.8586 -- the closest call in the corpus).  The event tails are identical in all
        five: `node_eval_started` -> `workspace_seeded` -> `research_attempted` -> ONE research
        `llm_usage` -> a long gap while the evaluation runs -> the ceiling.  The evaluation FINISHED
        and wrote its score to disk; the loop never saw a result it had already paid for.

        THE MECHANISM.  `_spawn_research`'s task raises `BudgetExceeded` out of the CardSession's
        `bg_task_group`, so it reaches `Engine.run` while the evaluation -- which lives in the
        RUN-scoped `eval_tg`, a strictly outer group -- is still in its worker thread.  That thread
        hop is `abandon_on_cancel=False`, i.e. shielded, so the eval is not abandoned: it runs to
        completion and writes `score.log`.  The cancellation is delivered at the NEXT checkpoint,
        which is inside `engine/evaluate.py` between the eval returning and its single
        `EV_NODE_EVALUATED` append.  Nothing was saved by cancelling -- the compute was already
        spent -- and the one durable record of it was lost.

        THE CASE THE ESCAPING EXCEPTION CANNOT NAME (closed 2026-09-08).  When the ceiling is raised
        INSIDE an evaluation rather than by the overlapped research -- the repair path re-raises it,
        and stage checks, triage and the repair critic are paid calls -- the raise comes out of an
        `eval_tg` CHILD.  The group then cancels `_run_with_llm_broker`, this hook is entered with a
        Cancelled whose `budget_stop_leaf` is None, and the drain no-opped while every SIBLING
        evaluation lost its terminal: the same measured loss, one seam over.  So the gate asks
        `accountant_over_ceiling` as well -- the run's own ledgers, which hold the ceiling out of
        band and can therefore answer for an exception that carries nothing.  The in-flight test
        moved ABOVE both, because "is there anything to drain" is the cheap half and neither
        predicate is worth asking of a run with no evaluation running.

        THE FIX IS THE ORDERING, NOT THE STOP.  Draining here happens BEFORE `async with eval_tg`
        exits, which is the only instant at which the children are neither cancelled nor already
        gone.  `Engine.run`'s `raise` is unconditional and untouched, so the run still stops with
        the same exception, the same message and the same `run_finished
        {"reason": "budget_exhausted"}`; `finalize_run` then computes its champion and its budget
        summary over a log that includes the node instead of one that silently omits it.

        WHY THIS COSTS NOTHING IT SHOULD NOT.  The drain starts NO new work: `_drain_adopted_evals`
        is a poll over `_eval_inflight`, the sessions have already returned, and no LLM call is
        reachable from it -- so the ceiling cannot be crossed by a further dollar while it waits.
        The wait is bounded by the evaluation the run had already committed to, which is the same
        barrier the clean finish at `_run_with_llm_broker`'s exit and the abort gate at
        `_settle_terminal_gate` both already pay, for the same stated reason (the run is ending,
        there is no GPU left to idle).  Deliberately NOT shielded: an operator Ctrl-C is a real
        cancellation and must still cut the wait short, and if this task is somehow entered with a
        cancellation already pending the first `anyio.sleep` re-raises it and we fall through to
        `raise` -- i.e. exactly today's behaviour, never worse.
        """
        if not self._evals_inflight():
            return                            # nothing paid for is in flight -- no barrier to pay
        if budget_stop_leaf(escaping) is None and not accountant_over_ceiling(self):
            return                            # an ordinary crash keeps today's teardown, untouched
        await self._drain_adopted_evals()

    def _enter_run(self) -> bool:
        """Authorize re-entry, recover, ACK and set up: everything before the first loop turn.

        An EXACT cut out of `_run_with_llm_broker` (doc 25 XP-06).  Measured rather than assumed:
        nothing after this block reads either of its two locals — `events` is dead after the setup
        gate and `state` is re-folded at the top of every loop turn — so its entire output is the
        one `entry_finished` flag finalization needs.

        It stays in THIS module because it is the run loop's own prologue — it sequences recovery,
        command acknowledgement and setup in one order (ENG1-04 kept it with the spine, review
        2026-09-22). It folds twice through the module-global `fold`, the monkeypatch seam
        (`tests/test_creation_runaway_guard.py` and friends replace it). That used to be the reason
        it could not move — a method elsewhere bound a different object — but since ENG1-04 step 0
        every engine file reaches the same seam at call time through `engine/shared.py::engine_fold`,
        so the fold no longer pins anything here.
        """
        # NO PROGRESS BEACON IN THIS PROLOGUE, and the reason is worth recording because it looks
        # like the obvious place for one. A resume IS one of the operator-reported blank waits: every
        # line of `_enter_run` runs before the loop's first turn, so no node, marker or pending count
        # has moved and the run looks dead. Beacons were added here and REVERTED — measured, they
        # broke thirteen tests across four files, and each break was a real property, not a stale pin:
        #   * `tests/test_speculation_runtime_gate.py` pins the log BYTES as unchanged when the
        #     receipt gate rejects a run. That is an authorization property — a run that fails
        #     authorization must not have mutated its log — and the gate sits BELOW the read a
        #     `read_log` beacon would have to bracket, so no ordering fixes it.
        #   * `tests/test_report.py` and `tests/test_stop_finalize_resume.py` broke on finalize
        #     RECOVERY: the wrap-up handshake reconciles a crashed finalize against the log, and rows
        #     appended here changed which branch it took, minting a fresh paid scope where it should
        #     have resumed the existing one. A diagnostic row moved a PAID-work decision.
        #   * `tests/test_end_to_end.py` and `tests/test_settled_width_pins.py` pin exact event
        #     counts across a resume (98 vs 94, 44 vs 42).
        # This is invariant #1's own warning arriving in practice: the question is never "does the
        # fold read it?" but "does any reader key on it?", and the prologue is where the
        # authorization fences, the finalize-scope reconciliation and the width pins all read the raw
        # log. Making a resume visible needs a channel that is NOT the event log — see the note in
        # `events/types.py::PROGRESS_STAGES`, which is why that vocabulary has one stage and not two.
        events = self.store.read_all()
        state = fold(events)
        # Re-entry authorization is the first semantic boundary.  Recovery, command ACK and setup all
        # append events, so a stale/missing/different receipt must fail before any of them can mutate a
        # positive-depth run.  `_reentry_repin` repeats this after setup to guard a concurrent tail edit.
        # The settled widths are the same kind of boundary and are restored first, so every later
        # decision in this invocation runs at the width the run's own log was written under.
        self._repin_settled_widths(state)
        self._repin_declared_env(state)
        self._require_pinned_speculation_receipt(state)
        if self._speculation_gate_calibration and events:
            # The hidden bootstrap is launch-only.  Even an exact prior calibration envelope cannot be
            # resumed/reused as another sample; every evidence lane starts from an exactly empty log.
            raise SpeculationAuthorizationError(
                "speculation gate calibration requires exactly zero prior events at run start")
        if self._recover_interrupted_builds(state):
            # Recovery appends terminal evidence. Re-fold before setup or any policy work so this
            # invocation cannot resurrect the abandoned marker or reuse its reserved id.
            events = self.store.read_all()
            state = fold(events)
        self._ack_commands(events)
        # A hard kill can land after the durable terminal intent (`finalize_step:begun`) but before
        # `run_finished`. Never run setup/search in that gap; finalization restores the exact terminal
        # payload from the begun marker and resumes only the same wrap-up scope.
        if (incomplete_finalize_scope(events) is None
                and not state.finalization_pending()):
            self._setup_phase(state)

        return self._reentry_repin()

    async def _run_with_llm_broker(self) -> RunState:
        entry_finished = self._enter_run()
        # Only an ENTERED loop owes an exit receipt: `_enter_run` raising (e.g. the speculation
        # receipt gate refusing re-entry) must keep the log byte-identical —
        # `tests/test_speculation_runtime_gate.py` pins those bytes.
        self._run_loop_exit_owed = True
        start = time.time()
        # The creation-level runaway guard's two bounds and the rule that charges them — see
        # `CreationRunawayCounters`, which carries the whole argument for why they read the LOG.
        runaway = CreationRunawayCounters()
        while True:
            # A terminal gate on the previous turn refused to finish over adopted evaluations
            # (`_refuse_finish_over_adopted_evals`). Pay the drain here, once, before re-deriving the
            # decision prefix — the run is stopping, so there is no GPU left to idle, and the gate
            # below then reaches its CAS over a log with no evaluation in flight.
            if self._eval_drain_requested:
                await self._drain_adopted_evals()
            # A SPEND CEILING AN ADOPTED EVALUATION DEFERRED is paid HERE, at the head of the turn
            # and after any drain above (review 2026-09-22, ENG2-02): every sibling still burning
            # lands its terminal first, then the run stops with the accountant's own exception.
            # See `speculation.py::_raise_deferred_eval_budget_stop` and `_card_eval_one`.
            await self._raise_deferred_eval_budget_stop()
            # Before the decision prefix is read, so a published row is part of THIS turn's fold
            # and cannot move the tail under the seq recheck below.
            self._record_trace_export_health()
            decision_events = self.store.read_all()
            state = fold(decision_events)
            decision_seq = decision_events[-1].seq if decision_events else -1
            # A control can arrive after initial re-entry. Re-check the calibrated authority on every
            # stable decision prefix before ACKs, recovery or any budget/strategy application.
            self._require_pinned_speculation_receipt(state)
            # A command ACK is a durable observation boundary. If it (or any concurrent writer)
            # extends the log after this fold, refold before doing domain work so neither a stale
            # reset nor a stale natural-finish decision can cross the newly-observed intent.
            self._ack_commands(decision_events)
            self._mark_loop_head()
            observed_tail = self.store.read_all()
            if (observed_tail[-1].seq if observed_tail else -1) != decision_seq:
                continue
            # A background consolidation can land immediately before any terminal/operator/budget gate.
            # Mirror it while this decision prefix is stable so an early exit cannot leave the durable
            # Card board permanently behind the Hypothesis board.
            state = self._mirror_hypothesis_card_merges(state)
            reconciled_tail = self.store.read_all()
            if (reconciled_tail[-1].seq if reconciled_tail else -1) != decision_seq:
                continue
            if state.search_epoch != self._holdout_epoch:
                # A reset/new candidate can win the finish race AFTER holdout disclosure while this
                # same Engine process stays alive. Rebuild immediately; waiting for a CLI re-entry
                # would stamp epoch-N events while still scoring the epoch-(N-1) partition.
                self._holdout_epoch = state.search_epoch
                self._holdout_idx = self._build_holdout_idx(
                    self._holdout_fraction, self._holdout_epoch)
                self._apply_search_split()
            # A scoped terminal intent is itself a work gate. Finalize/recover that exact scope
            # below; never reopen setup/search while a paid-report or terminal append is in flight.
            pending_scope = incomplete_finalize_scope(decision_events)
            self._pending_finalize_scope = pending_scope
            if pending_scope is not None:
                break
            # A durable resume request can land while this process is already alive (a `restart`;
            # the retired legacy `/resume` route recorded one unconditionally). A live
            # loop acknowledges it only when it can actually re-enter work; terminal/HITL/pause gates
            # leave it pending so the post-exit waiter (or on-load reconciler) spawns a fresh CLI,
            # whose normal resume path lifts the appropriate gate.
            if state.resume_pending() and not state.finished and not state.paused:
                self.store.append(EV_RESUME_SERVED, {})
                continue
            # Terminal/operator gates precede ALL work, including reset rebuilds. An explicit pause
            # must freeze a queued rerun; a scoped developer-crash pause must stop a stale reset batch.
            # A prior invocation guard may have appended a guarded-abort run_finished (`error`, or
            # the ceiling's `budget_exhausted`) after a durable abort. That is a retryable failed
            # wrap-up, not the abort's terminal result; republish the stable abort scope and let
            # scoped finalization deduplicate every completed side effect. `is_guarded_abort` and
            # never the literal: the ceiling is the ORDINARY terminal of a budgeted campaign, and a
            # literal `"error"` here made it read as a clean finish nothing needed to retry.
            if (state.finished and state.stop_requested
                    and is_guarded_abort(state.stop_reason)):
                abort = next(
                    (event for event in reversed(decision_events)
                     if event.type == EV_RUN_ABORT),
                    None,
                )
                abort_scope = f"abort:{abort.seq}" if abort is not None else None
                # The one finisher that does NOT go through a quiescence CAS — it republishes a
                # stable abort scope unconditionally — so its drain is spelled out here rather than
                # delegated to `_refuse_finish_over_adopted_evals`.
                await self._drain_adopted_evals()
                # …and a ceiling an evaluation deferred DURING that drain still ends the run as the
                # ceiling, before the abort's terminal — the disposition it had when the child's
                # raise cancelled this drain outright (ENG2-02), minus the siblings it cost.
                await self._raise_deferred_eval_budget_stop()
                self._finish_run({"reason": "aborted"}, scope=abort_scope)
                break
            if state.finished:
                break
            if isinstance(state.leakage, dict) and state.leakage.get("leak"):
                if self._settle_terminal_gate(state, "leakage", decision_seq=decision_seq) == "break":
                    break
                continue
            if state.stop_requested:
                if self._settle_terminal_gate(state, "aborted", decision_seq=decision_seq) == "break":
                    break
                continue
            if state.paused:
                if self._close_card_build_before_terminal_gate(state):
                    continue
                break
            # node_reset (operator "re-run this node from a stage"): a reset from implement/propose
            # re-develops the SAME node id IN PLACE before any other loop work, so it never mints a new
            # node. (An eval-reset needs no help here — the fold left it pending-with-code and the normal
            # eval dispatch below re-scores it.)
            _resets = [n for n in state.nodes.values()
                       if n.rerun_from in ("implement", "propose")
                       and n.status is NodeStatus.pending and not n.tombstoned
                       and n.id not in state.aborted_nodes]
            if _resets:
                # One rebuild per fold. A developer crash can auto-pause the first node, and a reset/
                # abort can change the rest while it is building; never process a stale whole batch.
                # OFF the loop thread (doc 52 row 12), like every other build: the rebuild is a paid
                # Developer call and its own-node appends are the worker seam's.
                # A FRESH GATE PER REBUILD, exactly as `_handle_create_actions` opens every create
                # turn (review 2026-09-22, ENG1-02). `_refuse_degraded_proposal` answers "already
                # gated" without queuing anything while `_create_paused` is set, so a flag left True
                # by an earlier invocation of this engine would make a dead provider's re-proposal
                # refuse SILENTLY — no pause, the reset still pending, and this branch proposing
                # (paid) again every turn.
                self._create_paused = False
                self._pending_create_pause = []
                await self._offload_build(functools.partial(self._rerun_node, _resets[0], state))
                self._drain_create_pause()
                continue
            # Charge the runaway guard for what the log says was MINTED since the previous turn. Read
            # off the events rather than the fold so the empty-nodes spin (which folds to no nodes at
            # all while appending a `node_created` per turn) is still counted — see `minted_charged`.
            runaway.charge(
                minted_now=sum(1 for _e in decision_events if _e.type == EV_NODE_CREATED),
                terminal_now=sum(1 for _n in state.nodes.values()
                                 if _n.status is not NodeStatus.pending),
            )
            # …and the bound the charge above cannot express: `node_failed` IS a terminal, so a run
            # where every node fails resets that guard every time and never stops. This one asks
            # whether anything has EVER worked — see `systemic_failure_stop_reason` for why that is
            # the line between "the environment is broken, stop the run" and "this idea is broken,
            # stop the node". Off once any node has been evaluated, and off entirely at threshold 0.
            _systemic = systemic_failure_stop_reason(state, self.systemic_failure_stop)
            if _systemic is not None:
                # Through the SAME ladder as every other terminal gate, not a bare finish. This gate
                # sits BEFORE the speculation block below, so unlike the `_finish_with_report_if_
                # quiescent` call sites further down it has no structural guarantee that no Card
                # build head is open — and finishing over an open head leaves the run's own durable
                # request unacknowledged. See `_settle_terminal_gate`: the order IS the rule.
                if self._settle_terminal_gate(state, _systemic, decision_seq=decision_seq) == "break":
                    break
                continue
            _signal = self._run_spec_gates(state)
            if _signal == "break":
                break
            if _signal == "continue":
                continue
            max_s, max_es = self._apply_control_overrides(state)
            # Budget (I13): per-invocation wall-clock ceiling (resets on each resume).
            if max_s is not None and (time.time() - start) >= max_s:
                if self._settle_terminal_gate(state, "time_budget", decision_seq=decision_seq,
                                       max_es=max_es, drain_forced_request=True) == "break":
                    break
                continue
            # Eval-compute budget (#2): cumulative time spent inside evals across the whole run
            # (persisted via the event log, so it survives resume — unlike wall-clock). Stops
            # the silent multi-hour sweep that real training runs can produce.
            if (max_es is not None
                    and state.total_eval_seconds >= max_es):
                if self._settle_terminal_gate(state, "eval_budget", decision_seq=decision_seq,
                                       max_es=max_es, drain_forced_request=True) == "break":
                    break
                continue

            # DRAIN ONLY (doc 68 68.3a): after every terminal and budget gate above — a pause, a stop
            # or a ceiling still wins — and BEFORE anything that builds, serves a forced request,
            # consults, researches or proposes.
            if self._drain_only:
                if await self._drain_only_turn(state, max_es) == "break":
                    break
                continue

            # docs/29 F1 — the run's WIDTH re-pins HERE, from what the research proposed, for the same
            # reason the AUTO depth re-resolves below: a stable decision prefix, no PRODUCER in flight
            # (an evaluation may be — review 2026-09-22, ES1-03; `_settle_proposal_width` says why),
            # and a durable event the caller must re-fold after. AFTER `_apply_control_overrides` so
            # an operator's live `budget_extend` is already in force and the axis it owns is visibly
            # theirs; BEFORE the speculation block so the depth settle and every gate under it read
            # one width rather than two.
            if self._settle_proposal_width(state):
                continue

            if await self._serve_forced_requests(state):
                continue

            if self._speculation_enabled():
                # AUTO depth re-resolves HERE, on a stable decision prefix with no head request and
                # no build in flight yet, so a settle can never land between a prefetch's request and
                # its commit. It appends a durable event and returns True; re-enter so every gate
                # below reads the new treatment from a fresh fold rather than from this stale one.
                if self._settle_speculation_depth(state, events=decision_events):
                    continue
                # Crash-prefix cleanup and the durable Card-build queue both precede cadences and
                # empty-action finalization. Otherwise request->node_building->crash can finish the run
                # with its exact request head still unacknowledged.
                if await self._close_developer_sentinel_once():
                    continue
                speculative_state = fold(self.store.read_all())
                # A result whose request another path closed releases its role telemetry here too:
                # an adopted build (width > 1) can finish with no session left to sweep it.
                self._discard_orphaned_spec_results(speculative_state)
                # …ONCE the boundary a session handed back for has been paid. A session that returns
                # with requests still open (a result to commit, an adopted build still running) sets
                # `_card_boundary_debt`; without this the open head would send the loop straight back
                # into a session and the cadences below — the turn it returned FOR — would never run.
                # COMMIT FIRST, THEN PAY IT (2026-09-25, critic review of the boundary wait). A build
                # that finished while the boundary was owed is committed here, before the cadences:
                # 8d9952a1's rule was about STARTING work across the boundary, and the claim re-checks
                # epoch, freshness, budget and the Card itself. Not while the run is stopping or an
                # operator's fork/inject waits for the slot.
                if self._commit_ready_builds_before_cadence(speculative_state, max_es):
                    speculative_state = fold(self.store.read_all())
                if (((self._head_request(speculative_state) is not None or speculative_state.buildings)
                        and not getattr(self, "_card_boundary_debt", False))
                        # A run-ahead proposal that finished between sessions (width > 1) is staged by a
                        # session too, or the outer loop would propose the same next Card itself.
                        or getattr(self, "_spec_raw_stage_result", None) is not None):
                    await self._run_card_session(
                        [],
                        speculative_state,
                        max_es,
                        None if max_s is None else start + max_s,
                    )
                    continue

            # The translated Card denominator changes whenever an attempt becomes tombstoned/gated or
            # a speculative freshness drop lands.  Refresh it BEFORE the Strategist reads
            # ``node_budget_frac``; the post-cadence refresh below is still required because a live
            # policy swap rebuilds ``policy.max_nodes`` from its unextended base.
            self._refresh_speculation_budget(state, events=decision_events)
            # OFF THE LOOP THREAD (the whole block, one worker hop): the cadences below spend,
            # and `at_creation_boundary` makes them due while an evaluation is burning. Their
            # folded rows are buffered by the sink and published by THIS task inside the
            # helper, so the tail read on the next line already carries them.
            state = await self._offload_cadence(functools.partial(self._run_cadences, state))
            self._card_boundary_debt = False     # the boundary a Card session returned for is paid
            post_cadence_events = self.store.read_all()
            post_cadence_seq = post_cadence_events[-1].seq if post_cadence_events else -1
            if post_cadence_seq != decision_seq:
                # Re-enter every gate after either an internal cadence append or a concurrent control.
                continue

            # Refresh after any in-loop policy swap so a live `add_nodes` extension is never lost. Card
            # mode translates the raw hard ceiling into the effective policy view: gated/tombstoned
            # Nodes stay hidden from ranking, but their already-reserved slots cannot be spent again.
            self._refresh_speculation_budget(state, events=post_cadence_events)

            if self._speculation_enabled():
                # Layer 5 freshness is live engine policy, never fold semantics. Drain one stale Node
                # and restart the turn; only a fully-clean fresh prefix may reach Card scoring.
                # This site used to pass no `eval_inflight` because it "runs between batches, with
                # every eval task already joined". That argument was already wrong across a CRASH —
                # a node this process never dispatched may have been mid-training when the PREVIOUS
                # process died, which is why `_drop_stale_speculation` also reads the durable
                # eval-start boundary — and since F1f it is wrong IN-PROCESS too: the outer loop now
                # turns while adopted evaluations run. Passing the live set is the in-memory half;
                # without it this call would terminalize a node whose sandbox is burning GPU minutes
                # right now, and `_evaluate` would then write a SECOND terminal for it.
                if await self._drop_stale_speculation(eval_inflight=self._eval_inflight):
                    continue
                fresh_events = self.store.read_all()
                fresh_seq = fresh_events[-1].seq if fresh_events else -1
                if fresh_seq != post_cadence_seq:
                    continue
                state = fold(fresh_events)

            # THE PLAN (doc 52 row 18): written / re-cut on the main task at this creation boundary,
            # then read back off the fold so the reserve below is the durable row's, never a local's.
            if self._ensure_plan(state):
                state = fold(self.store.read_all())
            actions = self._select_actions(state)
            actions = self._plan_gate(state, actions)
            if not actions:
                if await self._handle_no_actions(state, decision_seq=decision_seq) == "break":
                    break
                continue

            ablates = [a for a in actions if a["kind"] == "ablate"]
            if ablates:
                for a in ablates:
                    if "_scores" in a:   # surface "why this node" for ablates too (was dropped: this
                        self.store.append(EV_POLICY_DECISION,   # branch continues before the create loop)
                                          {"scores": a["_scores"], "chosen": a.get("_chosen"),
                                           "reason": a.get("_reason")})
                    await self._ablate(a["parent_id"])
                continue

            evals = [a for a in actions if a["kind"] == "evaluate"]
            # `debug` is deliberately NOT in this tuple any more (F5). Nothing in `search/`
            # produces one, so the only ways an action of that kind reaches here are a third-party
            # policy and a stale plugin — and this loop is the one place both funnel through. A
            # failure is repaired inside the node that failed; opening a fresh node to have another
            # go at the same experiment is the thing the operator deleted.
            creates = [a for a in actions
                       if a["kind"] in ("draft", "improve", "merge")]
            # OCCUPANCY-PACED PRODUCTION (backlog F1g, doc 33 §10).  `_select_actions` answers
            # "what should happen next" over the folded board, and a Node that is ALREADY being
            # evaluated is still `pending` there — so for the whole of a multi-hour evaluation the
            # selector returns an evaluate action naming a node this turn cannot start, `creates` is
            # empty, and the branch below is skipped.  `_stage_card_creates`, the ONLY writer of Card
            # INVENTORY, is therefore reachable only in the instants when NOTHING is running: measured
            # on a toy-backend run of this shape it fired ONCE in a whole 12-node run, at node 0.
            # Production was gated on occupancy ZERO, which is exactly backwards, and it is why F1f's
            # fix — the outer loop now turns while evaluations burn — could reach the boundary and
            # still find nothing to build.
            if not creates:
                creates = self._occupancy_paced_creates(state, evals)

            if creates:
                # doc 25 ES-05: the 220-line branch that used to live here is now a §4 phase
                # helper. It always continued or broke the loop, never fell through, so the
                # signal is acted on unconditionally.
                _signal, state, runaway.no_mint_turns = await self._handle_create_actions(
                    creates, state, created_no_terminal=runaway.created_no_terminal,
                    no_mint_turns=runaway.no_mint_turns,
                    decision_seq=decision_seq, max_es=max_es, max_s=max_s, start=start)
                # Any run-global pause a build QUEUED must become durable here, on the main task,
                # before the next fold. The branch has nine exits and only two of them drained,
                # which was adequate while the only producer was `_create_node`'s developer-crash
                # breaker (a worker-thread queue that always returns through one of those two). The
                # proposal-path breaker (`_refuse_degraded_proposal`) queues from
                # `_prepare_node_idea`, reachable from exits that never drained — and the branch
                # RESETS the queue on its next entry, so the pause would be silently dropped and the
                # run would keep paying for proposals against a dead provider. `_drain_create_pause`
                # empties the queue, so the inner drains stay exactly as they were.
                if getattr(self, "_pending_create_pause", None):
                    self._drain_create_pause()
                if _signal == "break":
                    break
                continue

            if self._speculation_enabled():
                await self._run_card_session(
                    evals,
                    state,
                    max_es,
                    None if max_s is None else start + max_s,
                )
            else:
                await self._dispatch_evals(evals, state, max_es)

        # The loop is over: nothing below is a step a control may be drained beside, and the wrap-up
        # suffix is positional, so the on-the-fly watcher stands down before the drain/finalize.
        self._control_watch_armed = False
        # Every `break` above can leave adopted evaluations running (the eval task group is owned by
        # `Engine.run`, not by this loop), and finalization reads the FOLD: champion, budget summary,
        # diversity archive, case store. Draining here — not at the task group's join, which happens
        # after `finalize_run` has already returned — is what keeps that read complete.
        await self._drain_adopted_evals()
        # A ceiling an evaluation deferred during that drain (ENG2-02) must not be swallowed into a
        # clean finish: before this, the child's raise cancelled the drain and the run ended on the
        # ceiling, and it still does — after the siblings have landed, before `finalize_run`.
        await self._raise_deferred_eval_budget_stop()
        # WHY THE LOOP STOPPED, exactly once — the receipt rule, the `finished` skip and the
        # exactly-once latch all live on `_record_run_loop_exit`. This fall-through covers every
        # `break` above; `Engine.run`'s outer `finally` calls the same helper so the RAISING
        # exits (the BudgetExceeded hard stop, a provider/store error, cancellation) get the same
        # receipt — the previous inline append sat only here and silently skipped every one of
        # them, i.e. exactly the exit classes the motivating v11 chase had to rule out by hand.
        self._record_run_loop_exit()
        # Finalize (extracted to looplab/engine/finalize.py, a pure move): budget summary,
        # diversity archive, LLM cost roll-up, case store + reflection note, read-model,
        # trace.json + tree.html. Event emission order is preserved exactly.
        return finalize_run(self, entry_finished=entry_finished, start_time=start)

    def _record_run_loop_exit(self) -> None:
        """Append the run loop's exit receipt exactly once per entered loop, wherever the exit is.

        WHY THE LOOP STOPPED, DERIVED FROM THE FINAL FOLD rather than from a hand-set local per
        exit: a derivation cannot disagree with the state a reader reconstructs from the same
        log. `unattributed` is a legal answer and the reason this exists — measured 2026-08-31,
        three runs of eight (v6, v9, v11) ended with NO pause and NO `run_finished` row; v11's
        last event is a `trust_scan`, so anyone folding its log sees a run still in flight,
        forever. A row saying the engine could not name its own exit is infinitely more than that
        silence, and it is the input a second rung would need to turn `unattributed` into a
        specific reason per exit.

        THE `finished` EXIT WRITES NO ROW, deliberately: `run_finished` already names that exit on
        the log, and the terminal gate appends it immediately after `finalize_step(begun)` — the
        head of `events/finalize_protocol.py::QUIET_FINALIZATION_SUFFIX`, whose readers
        (`search/speculation_quality.py::_validate_calibration_terminal` and the real-run half of
        `tests/test_finalize_protocol.py`) demand that exact contiguous terminal shape. The first
        cut appended `run_loop_exited: finished` between `run_finished` and `budget`, which made
        the speculation gate refuse every calibration run recorded at that commit.

        CALLED FROM TWO PLACES because the exits are of two kinds: `_run_with_llm_broker`'s
        fall-through covers every `break` of its loop (no count: the one this carried had drifted —
        review 2026-09-22, ES1-08), and `Engine.run`'s outer `finally` covers the
        raising exits — BudgetExceeded, a provider/store error, cancellation. `_run_loop_exit_owed`
        makes the pair exactly-once: latched only after `_enter_run` returns (a refused re-entry
        must keep the log byte-identical) and cleared on the first receipt. Errors are contained
        because the receipt must never break a shutdown — and a raise from `Engine.run`'s
        `finally` would REPLACE the exception already unwinding (`shared.py::_append_progress_row`
        documents that shape).
        """
        if not getattr(self, "_run_loop_exit_owed", False):
            return
        self._run_loop_exit_owed = False
        try:
            events = self.store.read_all()
            # A staged terminal boundary that is still OPEN owns this exit. The loop is stopping
            # precisely so the (resumed) finalization can finish that scope, and a row spliced
            # after `finalize_step(begun)` is an event `events/finalize_scope.py`'s recovery
            # projection does not recognize — measured by `tests/test_stop_finalize_resume.py` and
            # `tests/test_report.py`: it turned a begun-only crash recovery into a silent
            # no-recovery and a paid finish report into a re-bill. The receipt yields to the
            # protocol here exactly as it yields to `run_finished` on the finished path.
            from looplab.events.finalize_scope import incomplete_finalize_scope
            if incomplete_finalize_scope(events) is not None:
                return
            reason = run_exit_reason(fold(events))
            if reason != "finished":
                self.store.append(EV_RUN_LOOP_EXITED, {"reason": reason})
        except Exception:  # noqa: BLE001 - contain: never mask the exception already in flight
            _LOG.warning("the run loop's exit receipt could not be appended", exc_info=True)

    def _settle_terminal_gate(self, state, reason: str, *, decision_seq: int,
                              max_es: Optional[float] = None,
                              drain_forced_request: bool = False) -> str:
        """One terminal gate: settle what is in flight, then finish only if the log is quiescent.

        Every terminal gate of the run loop settles through here: leakage, aborted, the
        systemic-failure stop, time_budget and eval_budget (this said "four gates" and missed the
        fifth caller — review 2026-09-22, ES1-08). The ORDER is the whole rule — a Card build or a
        forced Node creator still in flight must be settled BEFORE finalization can win, or the run
        finishes with its own durable request head unacknowledged. Returns the outer loop's signal:
        "break" once the run is durably finished, "continue" to re-enter every gate on a fresh fold.
        An in-flight head also yields "continue", because the settle attempt churns the tail either
        way and its return value means "a head existed", not "this CAS succeeded".

        `state.paused` deliberately does NOT come through here even though it settles the same
        in-flight build: it then breaks WITHOUT finishing, which is a different terminal.

        Named for the `_close_*_before_terminal_gate` family it drives, and deliberately NOT
        `_terminal_gate`: this module already has a `_run_terminal_gate` PREDICATE ("has the run
        stopped accepting eval work"), and the two would read as the same thing.
        """
        if self._close_card_build_before_terminal_gate(state, max_es):
            return "continue"
        if drain_forced_request and self._close_node_creating_forced_request_before_terminal_gate(
            state, reason=reason,
        ):
            return "continue"
        if self._finish_with_report_if_quiescent(
                state, {"reason": reason}, after_seq=decision_seq):
            return "break"
        return "continue"

    def _run_spec_gates(self, state) -> Optional[str]:
        """The eval-spec onboarding pre-phase and its drift warning (doc 25 XP-06 phase helper).

        Lifted verbatim out of the run loop. Unlike `_handle_create_actions` this block CAN fall
        through — the activation and drift-warning steps run and the turn carries on — so the
        signal is three-valued and `None` means "keep going", not "nothing happened".
        """
        # Onboarding pre-phase (Phase 3, ADR-7): the agent proposes a trusted eval
        # spec + metric adapter; a human ratifies it once (or autonomous auto-confirms);
        # then it's frozen + protected and the optimization loop trusts it.
        if self.onboarder is not None and not state.spec_confirmed:
            if state.proposed_spec is None:
                with self.tracer.span("onboard", new_trace=True), \
                        llm_lane_scope("enrichment"):
                    proposal = self.onboarder()
                self.store.append(EV_SPEC_PROPOSED, proposal)
                return "continue"
            if self.eval_trust_mode == "autonomous":
                self.store.append(EV_SPEC_APPROVED, {})   # no human gate
                return "continue"
            if not state.spec_approval_requested:
                self.store.append(EV_SPEC_APPROVAL_REQUESTED,
                                  {"eval": state.proposed_spec.get("eval_spec")})
            return "break"  # pause for `LoopLab approve` (ratify_freeze)
        if self.onboarder is not None and not self._spec_activated:
            self._activate_spec(state.proposed_spec)
        # Drift coverage (#8): ratify_freeze_drift only corroborates the metric if a
        # cross_check reader exists. An adapter metric (agent-authored reader) with no
        # cross_check would make the drift guard a SILENT no-op exactly where it matters
        # most — surface it loudly once instead of pretending the metric is corroborated.
        if (self.eval_trust_mode == "ratify_freeze_drift" and self._eval_spec
                and not self._drift_warned):
            self._drift_warned = True
            _m = self._eval_spec.get("metric", {})
            if _m.get("kind") == "adapter" and not self._eval_spec.get("cross_check"):
                self.store.append(EV_DRIFT_UNAVAILABLE, {
                    "reason": "ratify_freeze_drift selected but the adapter metric has no "
                              "cross_check; the agent-authored reader is trusted WITHOUT "
                              "independent corroboration. Add eval.cross_check (a built-in "
                              "reader) to enable the drift guard."})
        return None

    async def _drain_only_turn(self, state, max_es) -> str:
        """One turn of `looplab resume --drain-only` (doc 68 68.3a): evaluate what is OWED, and
        when nothing is, pause and hand back — "finish only the pending evaluations and stop".

        WHY. A rescore — `node_reset {from_stage: "score"}` — could not run without resuming the
        whole search: a resumed run evaluates the reset node and then goes on creating nodes,
        consulting the Strategist and researching, spending the budget the operator meant to keep.
        Here nothing but evaluation runs. Owed nodes are evaluated through the ordinary dispatch
        (`_dispatch_evals`: the admission rules, the `node_eval_started` boundary, the repair loop
        — an evaluation's own repairs are part of finishing it) with NO research overlap; nothing
        is created, no forced request is served (they stay queued, durably, for the next resume),
        no cadence runs, and the end-of-search ladder (confirm, noise floor, finalize) is never
        reached. A reset from `implement`/`propose` is still rebuilt at the loop head: the operator
        asked for exactly that node.

        WHAT IT OWES is `drain_owed`: a lifecycle a reset opened, or an evaluation that started and
        never landed a terminal. A build the search made and has not dispatched stays pending.

        When nothing owed is left the run PAUSES with a stated reason, through the same control
        event an operator's pause is — the engine's own precedent is the confirm phase's auto-pause
        — so it ends as it began (a rescore starts from a paused run), and the next plain `resume`
        lifts it and searches on.

        NEVER A SPIN. A dispatch can admit none of what it was handed — the eval budget's
        reservation rule refuses a lane before the spent seconds reach the ceiling the loop head
        tests — and the same nodes would then be handed to it again on every turn, forever. So a
        turn in which no owed lifecycle moved pauses instead, naming the nodes it could not admit."""
        await self._drain_adopted_evals()
        await self._raise_deferred_eval_budget_stop()
        owed = {node.id: node.attempt for node in state.nodes.values() if drain_owed(state, node)}
        reason = DRAIN_ONLY_PAUSE_REASON
        if owed:
            await self._dispatch_evals([{"kind": "evaluate", "node_id": node_id}
                                        for node_id in sorted(owed)], state, max_es, research=False)
            after = fold(self.store.read_all())
            # Still owed, on the lifecycle it was handed on: nothing moved it. An abort or a reset
            # landing meanwhile is a move — the next turn re-derives what is owed.
            stuck = sorted(node_id for node_id, generation in owed.items()
                           if (node := after.nodes.get(node_id)) is not None
                           and node.attempt == generation and drain_owed(after, node))
            if len(stuck) < len(owed):
                return "continue"
            reason = DRAIN_ONLY_STUCK_REASON.format(ids=", ".join(map(str, stuck)))
        async with self._write_lock:
            if self._run_halt_intent():
                return "break"
            self.store.append(EV_PAUSE, {"reason": reason})
        return "break"

    async def _handle_no_actions(self, state, *, decision_seq) -> str:
        """The empty-action ladder: noise floor -> confirm -> holdout -> HITL approval -> finish
        (doc 25 XP-06).

        Lifted verbatim out of the run loop's `if not actions:` branch, which — like the ES-05
        `creates` branch before it — always continued or broke and never fell through. Its six
        outer-loop `break`/`continue` statements cannot cross a function boundary, so they return a
        signal instead; the caller acts on it unconditionally.

        It stays in THIS module because it folds: `fold` is the module-global monkeypatch seam.
        """
        # THE EVAL NOISE FLOOR (doc 52 row 11), BEFORE confirmation and for the reason the two are
        # different instruments: the floor is measured on the SEARCH's champion under the SEARCH's
        # own protocol, and confirm may demote that champion on a different (disjoint-seed, full-
        # profile) signal. Off by default (`Settings.eval_noise_seeds`), and off means this ladder
        # step is one comparison. It records and decides nothing; a completed pass gates itself.
        if self._noise_floor_due(state):
            await self._noise_floor_phase(state)
            return "continue"
        # Optional multi-seed confirmation pass (I12) before finishing:
        # re-evaluate the top-k under several seeds and record robust metrics.
        if (self.confirm_top_k > 0 and self.confirm_seeds > 0
                and not self._already_confirmed(state)):
            await self._confirm_phase(state)
            return "continue"
        # D1 holdout-gated promotion: AFTER the confirm pass (so confirmed means pick the
        # top-k), re-score the val-leaders' predictions on the reserved holdout partition.
        # Free (no re-training) and replay-safe (gated per node). The fold then lets the
        # unseen signal pick the champion (holdout_select) + surfaces the gap.
        if self._holdout_pending(state):
            await self._holdout_phase(state)
            return "continue"
        # HITL gate (I21, ADR-11): pause for human approval of the final best.
        # Approval flows through the event log (a UI/human appends
        # `approval_granted` through the allow-listed control writer); the engine reads and applies it.
        if self.require_approval and not state.approved:
            best = state.best()
            # No real candidate can ever be approved. Do not create an impossible HITL gate;
            # fall through to the normal report/finalization path with an explicit reason.
            if best is not None and not state.awaiting_approval:
                self.store.append(EV_APPROVAL_REQUESTED, {
                    "node_id": best.id, "generation": best.attempt,
                    "metric": best.metric, "after_seq": decision_seq})
                # An abort/reset can win between the stale loop snapshot and this append. Fold
                # again and stop only if the exact lifecycle request actually landed; otherwise
                # keep the engine alive to select/confirm the remaining candidate set.
                requested = fold(self.store.read_all())
                if (not requested.awaiting_approval
                        or requested.approval_subject != best.id
                        or requested.approval_generation != best.attempt):
                    return "continue"
            if best is not None:
                return "break"  # awaiting approval -> stop without finishing
        finish_data = ({"reason": "no_eligible_candidate"}
                       if state.best() is None else {})
        if self._finish_with_report_if_quiescent(
                state, finish_data, after_seq=decision_seq):
            return "break"
        return "continue"

    def _running_eval_node_ids(self) -> set[int]:
        """The Nodes whose evaluation is burning a slot RIGHT NOW, from the live adopted set.

        Deliberately the LIVE set and not the durable `node_eval_started` boundary. The two answer
        different questions and both are right for their own consumer: the durable row is what a
        RESUMED process reads to tell a prefetch that never ran from one whose sandbox burned GPU
        minutes (`_drop_stale_speculation`), and it stays true across the crash by design. What the
        production pace needs is "is a device busy in THIS process", and after a crash the answer is
        no — every one of those sandboxes died with the process that owned them. So a resumed run
        reads occupancy 0 here, takes the ordinary create turn, and is right to.
        """

        return {node_id for node_id, _generation in (getattr(self, "_eval_inflight", None) or ())}

    def _occupancy_paced_creates(self, state: RunState, evals: list[dict]) -> list[dict]:
        """Production the run may do BECAUSE a GPU is busy and the board behind it is empty (F1g).

        The turn reaching this point has no create action, and the reason is almost always that the
        selector answered with an evaluate action for a Node that is already in flight — which this
        turn cannot start, so the turn does nothing and the outer loop hands straight back to a
        session that also has nothing to do. Meanwhile the device stays busy for hours, the board
        stays empty, and the build latency that could have hidden behind the evaluation gets paid
        serially after it (167.7 GPU-h across this box's corpus, backlog F1g).

        THREE gates, and each of them is load-bearing.

        1. `occupancy_due` (`engine/cadence.py`) — the pace itself: an evaluation is running and the
           supply behind it does not cover the width. `queued` counts pending Nodes that are NOT in
           flight, i.e. work already built and waiting for a slot; producing more of that is not what
           an empty board means.
        2. Nothing this turn could have STARTED instead. Every evaluate action names an already-
           running Node, and no build/request is outstanding. If any of those is false the run is not
           starved — it is about to admit or about to commit — and minting here would be inventory
           bought against a decision that has not landed yet.
        3. The masked SELECTION decides what, in the same two-lane order `card_next_actions` uses —
           a durable Card that owns the next action first (`speculative_card_actions`), and only if
           there is none, the counterfactual raw lane that mints one (`speculative_raw_actions`).
           Both are the session's own producer queries, asked with the running Nodes hidden
           (`ignored_pending_node_ids`), and reusing them is what keeps this from becoming a SECOND
           selection authority — doc 33 option 4 is the write-up of why that is the expensive
           mistake in this area. Both lanes are needed, and the first one is the half that actually
           moves a GPU: minting a Card while the board is empty leaves inventory nobody builds,
           because the forced evaluate action for the running Node masks it from `_select_actions`
           on the very next turn too. Measured — with only the raw lane the card lands mid-eval and
           the node is still built serially after the terminal, i.e. no change at all.

        Bounded to the free slots, because the point is to fill them and not to run the search ahead
        on unproven ideas: everything past that is the prefetch's job, which owns a calibrated depth
        and a freshness drain and is reachable mid-evaluation since F1f.

        NO NEW EVENT, and that is a decision rather than an omission. "An evaluation is running and
        the board has nothing selectable" is already derivable — the live half from
        `Engine._eval_inflight`, the durable half from the folded board — and a row saying so would
        be a new writer for a fact nobody has to be told. A FOLDED row would move
        `_proposal_authority_seq` (which since 2026-08-20 costs a `_reserve_node_build` CAS retry, no
        longer a paid proposal) and the node-slot ceiling nothing; a DIAGNOSTIC row is excluded from
        that fence today, but it would still be an append per poll turn for the whole of a multi-hour
        evaluation, i.e. an unbounded log written to record that nothing happened. The condition is
        also its own idempotence (see `occupancy_due`), so there is nothing for a receipt to fence.
        """

        if not self._card_inventory_enabled():
            # Inventory is a Card-mode concept, and so is `_eval_inflight`: only the Card session
            # populates it, so with the selector off this predicate is vacuous anyway. Saying so
            # explicitly keeps the non-Card spine byte-identical rather than accidentally so.
            return []
        running = self._running_eval_node_ids()
        queued = {node.id for node in state.pending_nodes()} - running
        # Bare: `__init__` always settles it (ENG1-03 — the `getattr(..., 0)` here was never a sentinel).
        width = max(1, int(self._eval_parallel or 1))
        if not occupancy_due(inflight=len(running), queued=len(queued), width=width):
            return []
        if any(action.get("node_id") not in running for action in evals):
            return []                     # a slot could be filled from the board; do that instead
        # An open request means "a build is already answering this" only while it holds every
        # producer: with a build width above one (`_speculative_producer_width`) a free producer is
        # exactly what this mid-eval production exists to feed. At width one that is any request.
        if state.buildings or (self._head_request(state) is not None
                               and self._busy_producers(state) >= self._producer_capacity(state)):
            return []                     # a build is already answering this
        # >= 1 by `occupancy_due`, which is the same arithmetic: it is due only while the supply is
        # short of the width. Spelled out rather than hard-coded to 1 because filling EVERY freed
        # slot from one turn is what keeps `boundary_owed`'s bool honest at width > 1.
        free = width - len(running) - len(queued)
        context = SpeculativeSelectionContext(
            scoring=getattr(self, "_card_scoring", None),
            ignored_pending_node_ids=running,
            resource_envelope=self._resource_envelope(),
        )
        owned = speculative_card_actions(
            state, self.policy, self.policy.max_nodes, context=context)
        lane = owned or speculative_raw_actions(
            state, self.policy, self.policy.max_nodes, context=context)
        return [action for action in lane
                if action.get("kind") in ("draft", "improve", "merge")][:free]

    # CLOSED 2026-09-06 (doc 52 row 12): the serial lane's build, the fork's build and the node-reset
    # rebuild leave the loop thread through `_offload_build` / `_offload_node_build` (the proposal
    # pool, the own-node worker seam, the pause drained on the main task), and every build site
    # reads its outputs off the `DeveloperResult` envelope instead of the shared instance.
    # `tests/test_developer_result.py` drives the loop's own counter through a blocking build.
    #
    # MEASURED 2026-09-04 — the largest loop hold, but the HARM is not established and the
    # difference decides whether the offload is worth its risk. `card_build` wall, and how much
    # overlaps a live evaluation (union of eval windows, NOT a per-pair sum: the first cut summed
    # pairs and reported 149% of the build wall, which is impossible on one thread, and is retracted):
    #     v11  13 builds  608.6 min   93% overlaps an eval,  7% with none
    #     v4   16 builds  621.6 min   42% overlaps an eval, 58% with none (362 min)
    # v11's nine evaluations collapse into ONE continuous union window, so "93%" there says the box
    # was always evaluating — not that a build displaced anything.
    #
    # WHAT IS STILL MISSING, and it is exactly what would justify the change: overlapping an eval is
    # not a cost. The eval runs in a SUBPROCESS and a busy loop does not slow it. The cost is a FREE
    # GPU with BUILDABLE WORK while the loop is held, which needs board state per instant and cannot
    # be read from spans alone. Until that is measured this carries a cost CEILING, not a cost.
    async def _steady_state_build_lane(self, creates, state, pairs) -> tuple[RunState, bool]:
        """AIRA₂'s shape: propose and dispatch the next build the moment a LANE frees (doc 52 row 33).

        The chunked fan-out above is a bulk-synchronous barrier — `_fan` builds start together and
        NOTHING moves until the slowest finishes, so a fast worker cannot propose from a completed
        sibling's evidence and the loop pays the maximum of every chunk instead of its mean. This is
        the same work with the join moved: one lane per free (researcher, developer) pair, and the
        next proposal happens when a lane opens, against a fold that already contains everything
        that finished.

        WHAT DOES NOT MOVE, because these are the invariants the barrier was protecting:

        * the PROPOSAL and the RESERVATION stay on the MAIN task, serially, exactly as before
          (engine invariant #1): a worker still only implements an id the main task has already
          reserved durably, and ids are still minted under `_id_lock` in a fixed order;
        * the re-fold happens before EVERY proposal rather than before every chunk, which is
          strictly more of what the re-fold existed to buy — the novelty gate now also sees the
          `card_added` / `node_building` receipts of the lanes still running, so it cannot re-propose
          an idea another lane is building at this moment;
        * the pause circuit breaker still stops before starting new work, and the group still joins
          before the turn ends, so no build outlives the turn that started it;
        * NO LANE HOLDS THE PRIMARY ROLE PAIR while this method keeps proposing on it. This fourth
          one was MISSING from the list, and from the lane, between 2026-09-07 and 2026-09-08:
          `_build_role_pairs` returns `[(self.researcher, self.developer)] + pool`, so pair 0 is
          the primary pair, and the barrier made leasing it safe only because propose-all /
          build-all / join meant no proposal ran while a build held it. Removing the join is
          precisely what makes it unsafe. Driven: a build's `last_foresight` read back as None
          because a concurrent proposal's `finally` had nulled it — `foresight_selected` silently
          never written — and, in the mirror order, one node's ranking stamped onto the next, which
          is the mis-attribution the per-build pooled roles exist to prevent. The caller now hands
          this method non-primary pairs only, minting one extra POOLED pair so the operator's width
          survives (`speculation.py::_producer_role_pair` states the same rule where it was already
          obeyed).

        WHAT DOES MOVE, and is why this ships behind a flag: the researcher is asked for ONE idea per
        lane instead of `_fan` ideas per chunk. That is more provider calls of a smaller shape, and
        the diversity that came from asking for `_fan` distinct ideas at once now comes from
        proposing against a fold that holds the siblings — a different (and, on the field's own
        account, stronger) way to get it, but not the same bytes.
        """
        # THE NODE-OPEN FLOOR, before anything is proposed or reserved. Every other create path
        # asks it — the Card lane at `_handle_create_actions`, the chunked path per chunk, the
        # serial path per node — and this lane, added later, asked it nowhere: with
        # `node_open_budget_floor_usd` set and `steady_state_build` on, the floor was simply inert
        # and the run kept opening nodes until the CEILING raised `BudgetExceeded` from inside a
        # worker, where `_create_node_guarded` then turned it into one node's terminal (it is held
        # to the join and re-raised since review 2026-09-22, ENG1-01). Asked ONCE here
        # rather than per iteration because the loop below runs inside a task group, where a raise
        # would tear down lanes that are already building; the same "a lane of N node(s)" shape the
        # Card lane uses.
        self._refuse_node_open_below_floor(f"a steady-state build lane of {len(creates)} node(s)")
        # A SEMAPHORE and not a `CapacityLimiter`: the limiter is BORROWER-scoped — the task
        # that acquires must be the one that releases — and the whole point here is that the
        # MAIN task takes the slot (so it blocks before proposing) while the LANE gives it
        # back when its build ends. Driven: the limiter raises
        # `this borrower isn't holding any of this CapacityLimiter's tokens` on the first release.
        limiter = anyio.Semaphore(len(pairs))
        free_pairs = list(pairs)
        started = 0
        # The spend ceiling raised inside a lane's build is HELD until the lanes join and then
        # re-raised on this MAIN task (review 2026-09-22, ENG1-01) — the chunked path's rule, and
        # `_DeferredBudgetStop`'s clause (b): the admission below tests the sink before it proposes,
        # so a held stop starts no new work (the next proposal is itself a paid call).
        budget_stop: list[BaseException] = []
        async with anyio.create_task_group() as tg:
            deferred = _DeferredBudgetStop(tg, budget_stop)
            for action in creates:
                # BLOCKS UNTIL A LANE IS FREE — this is the whole difference from the barrier, and
                # it is why the fold below sees completions the chunked path could not.
                await limiter.acquire()
                if self._create_paused or budget_stop:
                    limiter.release()
                    break
                state = fold(self.store.read_all())
                ideas, telemetry, dropped = await self._await_batch_proposal(state, 1)
                if not ideas:
                    self._record_dropped_batch_cards(dropped)
                    limiter.release()
                    continue
                idea, telemetry_row = ideas[0], (telemetry[0] if telemetry else None)
                if budget_stop or self._refuse_degraded_proposal(idea, main_task=True):
                    # A dead provider hands back a degraded FALLBACK; the barrier path breaks the
                    # whole batch on one, and so does this — the next turn re-plans.
                    # …and a lane that hit the spend ceiling WHILE this proposal was in flight
                    # (review 2026-09-22, ENG1-01): the proposal is already paid, but reserving a
                    # node for it is new work the held stop forbids — its build's first paid call
                    # would only raise the same stop — so it takes the same exit.
                    self._record_dropped_batch_cards(dropped)
                    limiter.release()
                    break
                if "_scores" in action:
                    self.store.append(EV_POLICY_DECISION,
                                      {"scores": action["_scores"], "chosen": action.get("_chosen"),
                                       "reason": action.get("_reason")})
                self._append_rung_promotion(action)
                anchor_id, anchor_attempt = scored_anchor(state)
                reservation = self._reserve_node_build(
                    action, idea, scored_against=anchor_id,
                    scored_against_attempt=anchor_attempt, source="researcher",
                    steering_context=((telemetry_row or {}).get("_steering_context", [])
                                      if isinstance(telemetry_row, dict) else []))
                # THE REJECTS GET THEIR NODE-LESS CARDS HERE, on the SUCCESS path too — exactly as
                # the chunked path does after its reservations are durable. Without this a lane
                # that proposed one idea and rejected three recorded only the one: the three drops
                # never reached the Card board at all (the two failure paths above record them, so
                # the loss showed only when the proposal SUCCEEDED). The two batch capabilities
                # this also used to spend are gone with the attributes that carried them (review
                # 2026-09-22, ENG1-12): an already-reserved Idea can no longer ride into the next
                # iteration as a live gate bypass, because nothing of this proposal outlives it.
                self._record_dropped_batch_cards(dropped)
                if reservation is None:
                    limiter.release()
                    continue
                pair = free_pairs.pop()
                started += 1

                async def _lane(action=action, pair=pair, reservation=reservation, idea=idea,
                                telemetry_row=telemetry_row):
                    # The span is per LANE, not per batch: a barrier's cost is one number for the
                    # slowest member, and the thing this exists to make visible is that the lanes
                    # no longer wait for each other.
                    try:
                        with self.tracer.span("parallel_build_lane", fan=len(pairs),
                                              parallel_build=self._llm_parallel):
                            await anyio.to_thread.run_sync(
                                functools.partial(self._create_node_guarded, action, pair,
                                                  reservation, idea, telemetry_row))
                    finally:
                        # RELEASED IN A FINALLY, and the pair goes back before the slot does: a lane
                        # that raises must free its pair or the pool leaks a worker per crash, and
                        # `_create_node_guarded` already turns an unexpected exception into that
                        # node's own `node_failed` terminal rather than tearing down the group.
                        free_pairs.append(pair)
                        limiter.release()

                deferred.start_soon(_lane)
        if self._create_paused:
            self._drain_create_pause()
        if budget_stop:
            raise budget_stop[0]
        return fold(self.store.read_all()), started > 0

    async def _handle_create_actions(self, creates, state, *, created_no_terminal,
                                     no_mint_turns, decision_seq, max_es, max_s, start):
        """The `creates` branch of the run loop, lifted verbatim (doc 25 ES-05).

        220 inline lines: runaway-counter arithmetic, the speculation receipt-owned/raw split,
        parallel-build chunking, card-lane claiming and batch-drop bookkeeping. The §4 comment
        below claims the loop reads as "a table of guarded steps"; this branch was the one place
        it did not.

        Structural only — every append, fold, `_write_lock` point and gate stays exactly where it
        was. The one unavoidable change is control flow: nine `break`/`continue` statements here
        targeted the OUTER `while`, and those cannot cross a function boundary, so they return a
        signal instead. The five that target loops INSIDE this branch are untouched. The branch
        never fell through to the code after it — it always continued or broke — so the caller
        acts on the signal unconditionally.

        Returns `(signal, state, no_mint_turns)` where signal is `"break"` or `"continue"`.
        `state` is returned because this branch re-folds it and the loop reads the newer value.

        `no_mint_turns` round-trips for a sharper reason: it is the second of the two runaway
        bounds — consecutive turns that PLANNED creates and minted nothing — and this branch is the
        only place that increments it. Passed by value it would lose every mutation, the no-mint
        guard would never trip, and a create lane that elects work forever without ever building it
        would loop forever instead of finishing. That hazard is the one this lift's first draft
        actually hit (for `created_no_terminal`, which then still round-tripped); it moved rather
        than went away.

        `created_no_terminal` no longer needs the round-trip: the mint charge moved to the top of
        the loop, where it is read off the log's `node_created` rows instead of `len(creates)`, and
        the compensating decrements that existed only to undo that over-charge are gone with it. So
        this branch now only READS the counter, to test the trip. Both are rebound to the original
        local names below so the lifted lines stay byte-identical.
        """
        _created_no_terminal = created_no_terminal
        _no_mint_turns = no_mint_turns
        # Runaway trip: MINTED too many nodes with ZERO reaching terminal since the last
        # progress (charged at the top of the loop from the log). A healthy run creates a batch
        # then evaluates it (which resets the counter); only a spin (empty-nodes fold re-minting
        # the same id) grows this unbounded. Cap generously so operator injects / wide seed
        # batches never false-trip.
        _runaway_cap = max(self.policy.max_nodes, 4) * 3 + 50
        if _created_no_terminal > _runaway_cap:
            if self._finish_with_report_if_quiescent(state, {
                    "reason": "stuck: node creation not converging (no node reached terminal)"},
                    after_seq=decision_seq):
                return "break", state, _no_mint_turns
            return "continue", state, _no_mint_turns
        # …and its companion: a create lane that keeps PLANNING work and minting nothing. The
        # Card lane can legitimately spend a turn without a node (authoring a work item, losing
        # a build CAS, refusing a mixed-authority batch), so this cannot be one turn — but it
        # must still be bounded, or the same stall that used to end the run with the wrong
        # message would simply never end it at all. Same generous cap, counted in CONSECUTIVE
        # turns and reset by any mint or any terminal, so only a genuine no-progress spin
        # reaches it. The reason names what actually happened instead of blaming node creation.
        _no_mint_turns += 1
        if _no_mint_turns > _runaway_cap:
            # The reason names the CAUSE, not just the symptom. "N action(s) planned for M turns
            # without creating a node" describes what the counter saw; an operator cannot act on it,
            # and the same log's `budget.speculation` already recorded `producer_failed: 1`. The
            # diagnosis reads that same folded state, so the terminal and the budget summary agree.
            _why = self._create_stall_diagnosis(creates, state)
            if self._finish_with_report_if_quiescent(state, {
                    "reason": (
                        f"stuck: {len(creates)} action(s) planned for "
                        f"{_no_mint_turns} consecutive loop turns without creating a node"
                        + (f" — {_why}" if _why else ""))},
                    after_seq=decision_seq):
                return "break", state, _no_mint_turns
            return "continue", state, _no_mint_turns
        self._create_paused = False   # set by _create_node's developer_crash circuit-breaker
        self._pending_create_pause = []   # …and its worker-side request queue (see _request_create_pause)
        # TWO GATES, not one. INVENTORY (minting selection-ready Cards) belongs to
        # `card_driven_selection`; PREFETCH (electing an isolated producer to build the next Card
        # ahead of time) belongs to `speculation_depth`. Both halves used to sit under
        # `_speculation_enabled()`, which made the queue's only writer reachable only through the
        # prefetch lane — see `_card_inventory_enabled` for what that measured.
        if self._card_inventory_enabled():
            receipt_owned = [META_CARD_ID in action for action in creates]
            # One turn has one authority. A mixed lane could stage new work while claiming a
            # stale selection snapshot, so retain the serial spine's existing fail-closed rule.
            if any(receipt_owned) and not all(receipt_owned):
                return "continue", state, _no_mint_turns

            if not any(receipt_owned):
                # Raw policy actions do not yet name executable work. Author their concrete
                # Ideas and durable Cards now, but deliberately leave every Node slot unowned;
                # the next fresh fold must select them before a producer can be requested.
                # (`speculative_raw_actions` keeps its name: it is the COUNTERFACTUAL raw lane —
                # "what would the policy do if no durable Card owned this?" — and it is pure
                # selection over folded state with no dependency on the prefetch lane at all.)
                #
                # The running evaluations are masked (backlog F1g). Without it this re-derivation
                # disagrees with the one that decided the turn: a Node under evaluation is still
                # `pending`, so the raw lane's own fallback is that Node's evaluate action, which is
                # not a create — `speculative_raw_actions` then returns nothing and an occupancy-paced
                # turn falls through to the serial compatibility path with a lane it never staged.
                # The mask is exactly the in-flight set, never `_acknowledged_pending_ids`' whole
                # pending board: a pending Node NOT in flight is real work the consumer is about to
                # admit, and hiding it would mint inventory against a slot that is already spoken for.
                stageable = speculative_raw_actions(
                    state,
                    self.policy,
                    self.policy.max_nodes,
                    context=SpeculativeSelectionContext(
                        scoring=getattr(self, "_card_scoring", None),
                        ignored_pending_node_ids=self._running_eval_node_ids(),
                        resource_envelope=self._resource_envelope(),
                    ),
                )
                if stageable:
                    # WITH PREFETCH: author one work item at a time. The live depth is filled by the
                    # isolated steady-state proposer while eval runs; staging an unreserved wide seed
                    # batch here only creates stale inventory if the first fast eval moves best.
                    #
                    # WITHOUT PREFETCH: stage the WHOLE lane. There is no steady-state proposer to
                    # fill the depth, and the argument against a wide batch does not apply — it needs
                    # an eval to finish between the staging and the selection, and on this path
                    # nothing is in flight (`_dispatch_evals` is awaited, and a turn with `creates`
                    # dispatches nothing). Truncating to one here would instead SERIALIZE every batch
                    # the run would otherwise have built at once: the rung-0/seed width, and the
                    # population lane of `evolutionary`/`mcts`/`asha` — none of which ever reached
                    # this code before, because AUTO settles the depth to 0 for a non-greedy policy
                    # and a spelled depth is refused there. `_stage_card_creates` already has the
                    # multi-draft lane (one shared-Researcher diversity pass), `forced_card_actions`
                    # hands back up to `width` ready drafts, and `_claim_existing_card_builds` claims
                    # the complete lane in one tail-CAS group — so the batch shape survives the queue
                    # rather than being flattened by it.
                    lane = stageable if not self._speculation_enabled() else stageable[:1]
                    if await self._stage_card_creates(lane, state):
                        return "continue", state, _no_mint_turns
                    if self._create_paused:
                        # …but a staging attempt that GATED the run is not a "rejected" one. The
                        # serial compatibility try below would propose again against the same dead
                        # provider and pay for a second identical refusal. Hand the loop back so it
                        # re-folds, sees `paused`, and stops.
                        return "continue", state, _no_mint_turns
                    # A rejected staging attempt gets one ordinary serial compatibility try;
                    # it must not poll the same paid proposal outside the runaway accounting.
                # Unsupported/custom scorer semantics retain the exact serial compatibility
                # path below; a Card it cannot score must never be staged/reused in a loop.

            if self._speculation_enabled():
                # A positive depth is useful only with a genuinely isolated role pair. If the
                # configured factory cannot provide one, fall through to the safe serial Card
                # claim below. Otherwise request/session is the sole build path: a lost selection
                # CAS restarts from a fresh fold and never silently converts to serial execution.
                serial_fallback = any(
                    self._card_requires_serial_fallback(action.get(META_CARD_ID))
                    for action in creates
                )
                if (all(receipt_owned)
                        and self._producer_role_pair() is not None
                        and not serial_fallback):
                    # The mask travels with this election too (backlog F1g), for the same reason it
                    # travels into `speculative_raw_actions` above and `_claim_existing_card_builds`
                    # below. `_speculation_depth_used` states the rule: a Node already admitted to
                    # the consumer is no longer prefetch inventory, and "retaining it in the count
                    # makes depth=1 strictly serial". This call site could not observe a running
                    # evaluation before F1f — the outer loop only turned between batches — so the
                    # default empty mask was correct then and is not now: an occupancy-paced turn
                    # reaches it WITH a GPU busy, and charging that node against the depth refuses
                    # the election, appends nothing and leaves the freed slot dark for the whole
                    # evaluation. `_card_phase_request_build` has always passed the session's own
                    # copy of this set; it is the same object.
                    if self._request_card_build(
                            consumed_inflight=frozenset(
                                getattr(self, "_eval_inflight", None) or ())):
                        await self._run_card_session(
                            [],
                            fold(self.store.read_all()),
                            max_es,
                            None if max_s is None else start + max_s,
                        )
                    return "continue", state, _no_mint_turns
            # With prefetch off, a receipt-owned lane falls through to `_claim_existing_card_builds`
            # below — the SAME serial Card claim the prefetch path already falls back to whenever the
            # role factory cannot isolate a pair. So "Cards are minted, selected, then built" is one
            # code path with or without speculation; only who builds them differs.
        # Variant-1 parallel BUILD: seed/explore DRAFTS are independent, so build (research + code)
        # up to `parallel_build` at once, each on its OWN pooled (researcher, developer) pair + its
        # own pre-reserved id (reserved serially under _id_lock, then fanned out in a task-group of
        # worker threads). Non-draft creates (improve/merge/debug depend on a parent's result and
        # use role helpers not yet pool-threaded) and the no-pool config fall through to the serial
        # loop below — byte-identical to before.
        _card_reservations: Optional[list[_BuildReservation]] = None
        if any(META_CARD_ID in action for action in creates):
            # A Card lane is one authority decision. Mixing receipt-owned and proposer-owned
            # work in it would make the score-to-claim fence ambiguous, so fail closed.
            if not all(META_CARD_ID in action for action in creates):
                return "continue", state, _no_mint_turns
            # The mask travels with the lane (backlog F1g): a lane selected while an evaluation runs
            # was selected with that Node hidden, and the claim must revalidate the SAME question or
            # it retires the Card as unclaimable. Empty whenever nothing is in flight, which is every
            # ordinary create turn — that path is byte-identical.
            # The node-OPEN floor, asked BEFORE the claim mints durable reservations: a refusal
            # here leaves nothing reserved and nothing owed (`_refuse_node_open_below_floor`).
            self._refuse_node_open_below_floor(f"a Card lane of {len(creates)} node(s)")
            _card_reservations = self._claim_existing_card_builds(
                creates, ignored_pending_node_ids=self._running_eval_node_ids())
            if _card_reservations is None:
                # A refused claim used to be an unconditional retry, which is right for a transient
                # refusal and a SPIN for a permanent one. Count it; the ledger retires a lane that has
                # answered the same way for `_CARD_CLAIM_RETIRE_AFTER` turns so selection can move on.
                self._note_card_claim_refusal(
                    [self._canonical_card_id(a.get(META_CARD_ID)) or "" for a in creates])
                return "continue", state, _no_mint_turns
            self._card_claim_refusal_lane = None      # a claim landed: this lane is not stalled
            self._card_claim_refusal_turns = 0
        _card_reservation_by_id = {
            reservation.card_id: reservation
            for reservation in (_card_reservations or [])
        }
        # Never while adopted Card builds run (width > 1): they hold pooled pairs from the same
        # `_role_pool` this fan leases, so a pooled Developer would build two nodes at once and cross
        # their `last_files`. With one producer the session is joined before this path runs.
        _pb_pairs = (self._build_role_pairs(min(self._llm_parallel, len(creates)))
                     if (self._llm_parallel > 1 and len(creates) > 1
                         and all(a.get("kind") == "draft" for a in creates)
                         and not any(META_CARD_ID in a for a in creates)
                         and not getattr(self, "_spec_build_inflight", None)) else None)
        if _pb_pairs and len(_pb_pairs) > 1 and self._steady_state_build:
            # NEVER THE PRIMARY PAIR, and this is the fourth invariant the barrier was protecting.
            # `_build_role_pairs` returns `[(self.researcher, self.developer)] + pool`, so pair 0 IS
            # the primary pair — and under the barrier that was safe, because propose-all then
            # build-all then join means no proposal ever ran while a build held those objects. The
            # steady lane's whole purpose is to remove that join, so from the moment pair 0 is
            # leased the main task's next `_await_batch_proposal` calls `self.researcher.propose()`
            # on the same object a lane is building with (and under the shipped `unified_agent` the
            # same object is the developer too).
            #
            # Driven: with `parallel_build=2` and this flag on, two of eight proposals overlapped a
            # build holding the shared pair, and the build's own `last_foresight` was read back as
            # None because the proposal's `finally` had nulled it — `foresight_selected` silently
            # never written for that node. The mirror direction cross-wires one node's ranking onto
            # the next, which is the mis-attribution the per-build pooled roles exist to prevent.
            #
            # `speculation.py::_producer_role_pair` already states this rule where it is obeyed
            # ("`_build_role_pairs(1)` is intentionally not used: it returns the primary roles whose
            # per-build output slots are shared with repairs and ordinary builds") and refuses a
            # pair equal to the primary. This lane, added later, did not.
            #
            # ONE MORE PAIR, not one fewer lane. Simply dropping pair 0 would leave a single lane at
            # the common `parallel_build=2` and silently turn the flag into the barrier it replaces
            # — a fix that removes the feature is not a fix. `_build_role_pairs(n + 1)` mints one
            # extra POOLED pair (the pool is built lazily from `role_factory`, so the cost is one
            # more role construction, once), and `[1:]` is then exactly `n` pairs none of which is
            # the primary. Falling back to the BARRIER below when the factory cannot supply two is
            # the honest alternative to proposing onto a role a lane is holding.
            _steady_pairs = list(self._build_role_pairs(
                min(self._llm_parallel, len(creates)) + 1)[1:])
            _steady_pairs = [_p for _p in _steady_pairs
                             if (isinstance(_p, tuple) and len(_p) == 2
                                 and _p[0] is not getattr(self, "researcher", None)
                                 and _p[1] is not getattr(self, "developer", None))]
            if len(_steady_pairs) > 1:
                # The barrier's replacement (doc 52 row 33), opt-in: propose and dispatch as each
                # lane frees instead of chunk-join-chunk. Same reservations, same worker, same join
                # before the turn ends — see `_steady_state_build_lane` for what moves and what does
                # not.
                state, _built = await self._steady_state_build_lane(creates, state, _steady_pairs)
                return "continue", state, _no_mint_turns
        if _pb_pairs and len(_pb_pairs) > 1:
            _fan = len(_pb_pairs)
            for _i in range(0, len(creates), _fan):
                _chunk = creates[_i:_i + _fan]
                # Phase 2: ONE shared-researcher pass produces the DISTINCT seed ideas for this
                # chunk (avoidance-driven diversity + novelty gate); the fan-out below then only
                # IMPLEMENTS them per-developer, so we never pay N independent research rolls that
                # collide. If the researcher can't diversify to the full width, build only as many
                # nodes as we got distinct ideas — the loop re-plans the rest next iteration.
                # RE-FOLD before each chunk (review finding #6): a batch WIDER than the fan-out is
                # built in multiple chunks; earlier chunks' nodes are now in the log, so re-folding
                # lets THIS chunk's vs-history novelty gate see them and not re-propose their ideas
                # (the serial path gets this for free — each node lands before the next proposes).
                if _i:
                    state = fold(self.store.read_all())
                # MAIN TASK, before the paid batch proposal and before any reservation: the
                # node-OPEN floor for this whole chunk (`_refuse_node_open_below_floor`).
                self._refuse_node_open_below_floor(f"a build chunk of {len(_chunk)} node(s)")
                # Per-idea FOREAGENT telemetry snapshots captured by _propose_batch (aligned
                # 1:1 with _ideas), so each build emits ITS OWN
                # hypothesis_ranked/foresight_selected.
                _ideas, _telem, _dropped_batch = await self._await_batch_proposal(
                    state, len(_chunk))
                if not _ideas:
                    self._record_dropped_batch_cards(_dropped_batch)
                    continue
                # Third and last lane that reserves a node from a proposal without crossing
                # `_prepare_node_idea`'s `_link` funnel. A dead provider hands the shared batch
                # researcher N degraded FALLBACKS at once, which is how the same non-proposal used to
                # become several byte-identical nodes in one chunk. MAIN TASK — this is the loop task,
                # before any `start_soon`.
                if any(self._refuse_degraded_proposal(_idea, main_task=True) for _idea in _ideas):
                    self._record_dropped_batch_cards(_dropped_batch)
                    break
                _chunk = _chunk[:len(_ideas)]
                for _a in _chunk:               # surface the audit events only for what we build
                    if "_scores" in _a:
                        self.store.append(EV_POLICY_DECISION,
                                          {"scores": _a["_scores"], "chosen": _a.get("_chosen"),
                                           "reason": _a.get("_reason")})
                    self._append_rung_promotion(_a)
                # Proposal is complete before durable reservation: a native Card receipt must
                # bind the exact immutable statement/action.  The MAIN TASK serially commits
                # card_added -> node_building for each idea, then workers only implement.
                # ONE FOLD FOR BOTH HALVES of the score fence. `state` here was folded before the
                # minutes-long awaited propose above, and `_reserve_node_build._plan` re-folds fresh
                # under the CAS — so reading the id here and the ATTEMPT there recorded a pair the
                # proposal was never scored against, and the card then read `current` in exactly the
                # case the generation is in the receipt to catch. The stale ID is not the defect and
                # is deliberately kept: see `scored_anchor`.
                _anchor_id, _anchor_attempt = scored_anchor(state)
                _reserved = [
                    # `retry_attach` stays off (default): these Ideas came from the shared batch
                    # proposal and never crossed `_prepare_node_idea._link`, so no earlier pass
                    # planned an attach for this pass to agree with.
                    #
                    # The score fence's two halves are bound ABOVE, from one fold, since
                    # 2026-08-31. `state` here is the pre-propose fold and `_plan` re-folds fresh
                    # under the CAS, so reading the id here and the ATTEMPT there recorded a pair
                    # the proposal was never scored against. Half of the original finding was
                    # WRONG and is deliberately not fixed: the stale ID is the correct record —
                    # `cards.py::card_score_fence_state` narrowed champion-equality away on
                    # 2026-08-13 because it killed cards permanently on an unrelated node's win,
                    # and `card_selection` asks only that the anchor be live. Both readers want
                    # the champion the proposal was scored under.
                    self._reserve_node_build(
                        _a, _idea, scored_against=_anchor_id,
                        scored_against_attempt=_anchor_attempt,
                        source="researcher",
                        steering_context=(
                            (_tel or {}).get("_steering_context", [])
                            if isinstance(_tel, dict) else []),
                    )
                    for _a, _idea, _tel in zip(_chunk, _ideas, _telem)
                ]
                # Accepted preplanned ids are durable first. Node-less rejects then receive fresh
                # closed Card ids without shifting any reservation the workers are about to use.
                self._record_dropped_batch_cards(_dropped_batch)
                # Cost guardrail (Phase 4): surface the concurrent build fan-out width in the
                # trace (spans.jsonl / OTel). `built` is structurally bounded by `fan` (=len of
                # the role pool) which is bounded by `parallel_build`, so a batch can never exceed
                # the configured fan-out — this span makes the actual per-batch cost observable.
                # THIS JOIN IS A BULK-SYNCHRONOUS BUILD BARRIER, not independent adaptive research
                # threads: a fast worker cannot select or propose from a completed sibling's
                # evidence until the slowest build of the chunk and the eval batch after it finish.
                # The lane that refills a freed worker instead exists, opt-in
                # (`_steady_state_build_lane`, `Settings.steady_state_build`); flipping the DEFAULT
                # is the indexed open item `parallel-build-is-a-bulk-synchronous-barrier` (doc 52),
                # which is owed a multi-GPU measurement. This was an un-indexed `CODEX AGENT` note
                # until review 2026-09-22 (ES1-08).
                # The spend ceiling, raised inside one build, is HELD here until the chunk joins
                # (review 2026-09-22, ENG1-01): `_create_node_guarded` no longer turns it into a
                # `build_crash`, and letting it cancel the group would cost the siblings their
                # already-paid builds. Re-raised below, after the join, on this MAIN task.
                _budget_stop: list[BaseException] = []
                with self.tracer.span("parallel_build_batch", fan=_fan, built=len(_chunk),
                                      parallel_build=self._llm_parallel):
                    async with anyio.create_task_group() as _tg:
                        _deferred = _DeferredBudgetStop(_tg, _budget_stop)
                        for _a, _res, _pair, _idea, _tel in zip(
                                _chunk, _reserved, _pb_pairs, _ideas, _telem):
                            if _res is None:
                                continue
                            # _create_node_guarded: an UNEXPECTED exception in one build becomes a
                            # node_failed terminal for its already-reserved id (node_building was
                            # appended up front) instead of tearing down the task group and killing
                            # the whole run — the rest of the concurrent batch still finishes.
                            _deferred.start_soon(anyio.to_thread.run_sync,
                                                 functools.partial(self._create_node_guarded,
                                                                   _a, _pair, _res, _idea, _tel))
                # Circuit breaker under concurrency: `start_soon` does not yield, so no worker runs
                # until the task group JOINS above — the pause flag can only be observed HERE, after
                # the whole chunk finishes. So a developer/build crash pauses after AT MOST this one
                # chunk (bounded by the fan-out width), not mid-chunk; stop before the next chunk.
                # The pause a sibling requested is appended BEFORE the budget stop is re-raised: it
                # records a hard fault this chunk really had, and the stop starts no new work.
                if self._create_paused:
                    self._drain_create_pause()
                if _budget_stop:
                    raise _budget_stop[0]
                if self._create_paused:
                    break
            return "continue", state, _no_mint_turns
        for _create_index, a in enumerate(creates):
            reservation = (_card_reservation_by_id.get(a.get(META_CARD_ID))
                           if META_CARD_ID in a else None)
            if META_CARD_ID in a and reservation is None:
                continue
            if "_scores" in a:   # policy exposed candidate scores -> surface "why this node"
                self.store.append(EV_POLICY_DECISION,
                                  {"scores": a["_scores"], "chosen": a.get("_chosen"),
                                   "reason": a.get("_reason")})
            self._append_rung_promotion(a)
            if META_CARD_ID in a:
                # The complete Card lane was claimed atomically above, before the first slow
                # build could make its siblings ineligible through the evaluate-all prefix.
                try:
                    await self._offload_node_build(a, reserved=reservation)
                except BaseException:
                    for later in (_card_reservations or [])[_create_index + 1:]:
                        self._fail_reserved_build(
                            node_id=later.node_id,
                            card_id=later.card_id,
                            generation=0,
                            error="Card build batch stopped by an unexpected build error",
                            reason="build_batch_cancelled",
                        )
                    raise
            else:
                # One node per iteration on this path, so the floor is asked per node — the
                # decision `Settings.node_open_budget_floor_usd` is about, on the main task.
                self._refuse_node_open_below_floor(f"a new {a.get('kind')} node")
                # sequential -> deterministic ids/proposals; OFF the loop thread since 2026-09-06
                await self._offload_node_build(a)
            if self._create_paused:
                self._drain_create_pause()
                for later in (_card_reservations or [])[_create_index + 1:]:
                    self._fail_reserved_build(
                        node_id=later.node_id,
                        card_id=later.card_id,
                        generation=0,
                        error="Card build batch stopped after a Developer crash",
                        reason="build_batch_cancelled",
                    )
                # A developer_crash auto-PAUSED the run (LLM unreachable / hard error). STOP the
                # rest of the batch instead of building every seed and paying the full within-call
                # retry/backoff on each — honouring the "PAUSE on the FIRST developer_crash"
                # guarantee the crash branch documents. The loop re-folds paused=True at the top
                # and finalizes; a plain `resume` continues once the cause is fixed.
                break
        return "continue", state, _no_mint_turns


    # -------------------------------------------------- run() phase helpers (§4 decomposition)
    # Pure structural decomposition of run(): each method is a cohesive span lifted verbatim so the
    # loop body reads as a table of guarded steps. No behavior/ordering/gating change — every event
    # emission, _write_lock point, and fold site stays exactly where it was in the original run().

    def _hard_node_reservation_limit(self, state: RunState) -> int:
        """Return the operator-owned ceiling for distinct durable Node reservations.

        The ceiling is extended by exactly the reservations the L3 accounting has already REFUNDED
        (``refunded_node_reservations`` — a speculative build proven by the event log to have been
        discarded before it consumed any evaluation).  Without that term the two halves of the budget
        disagreed and the refund was inert: ``card_budget_used`` stopped charging the slot, but
        ``_node_id_ceiling`` — the monotonic id ALLOCATOR, which can never reuse an id — kept it
        spent, so a run that discarded three predictions simply ran three fewer experiments on the
        same budget. Both halves now read one predicate, and this stays a pure function of the folded
        log, so replay reaches the identical number.
        """

        base_limit = getattr(self, "_base_max_nodes", None)
        if base_limit is None:
            # Compatibility for narrowly-constructed Engine test doubles and older embedders. A fully
            # initialized Engine always owns ``_base_max_nodes``; only the partial-object seam falls
            # back to the policy/configured value, and an unconfigured object fails closed at zero.
            base_limit = getattr(getattr(self, "policy", None), "max_nodes", None)
        if base_limit is None:
            base_limit = getattr(self, "max_nodes", 0)
        try:
            base_limit = int(base_limit)
        except (TypeError, ValueError, OverflowError):
            base_limit = 0
        operator_limit = max(
            0,
            base_limit + int(state.budget_overrides.get("add_nodes", 0) or 0),
        )
        # The refund is bounded by the operator ceiling itself (see `refunded_node_reservations`), so
        # a freshness loop can never mint unbounded builds off its own discards.
        return operator_limit + refunded_node_reservations(state, operator_limit)

    def _unmaterialized_card_request_indices(self, state: RunState) -> set[int]:
        """Return exact outstanding request indexes that still own a future Node slot.

        Materialized ownership is matched as a multiset: one strict speculative ``node_building``
        marker or one not-yet-linked speculative Node can discharge only one request with the exact
        ``(card_id, card_build_generation)`` identity. Ordinary Card build markers, mismatched
        generations, and Nodes already linked by an accepted ``card_build_done`` cannot discharge a
        later duplicate request. The returned absolute indexes make conversion credit head-specific.
        """

        done = max(0, min(int(state.card_builds_done), len(state.card_build_requests)))
        materialized: dict[tuple[str, int], int] = {}

        def _add_materialized(key: tuple[str, int]) -> None:
            materialized[key] = materialized.get(key, 0) + 1

        linked_node_ids = {
            node_id for node_id in state.speculative_nodes
            if type(node_id) is int
        }
        # A valid node_created clears its build marker. If a corrupt prefix leaves both projections,
        # count the physical node id once, preferring the created Node below.
        for node_id, marker in state.buildings.items():
            if (
                type(node_id) is not int
                or node_id in state.nodes
                or node_id in linked_node_ids
                or not isinstance(marker, Mapping)
                or marker.get("node_id") != node_id
                or marker.get("speculative") is not True
            ):
                continue
            card_id = marker.get("card_id")
            generation = marker.get("card_build_generation")
            if isinstance(card_id, str) and card_id and type(generation) is int and generation >= 0:
                _add_materialized((card_id, generation))

        for node in state.nodes.values():
            if (
                node.id in linked_node_ids
                or node.speculative is not True
                or not isinstance(node.idea.card_id, str)
                or not node.idea.card_id
                or type(node.card_build_generation) is not int
            ):
                continue
            _add_materialized((node.idea.card_id, node.card_build_generation))

        unmaterialized: set[int] = set()
        closed_ahead = set(state.card_builds_done_ahead)
        for request_index in range(done, len(state.card_build_requests)):
            if request_index in closed_ahead:
                continue
            request = state.card_build_requests[request_index]
            key = self._request_key(request)
            if key is None:
                continue
            available = materialized.get(key, 0)
            if available:
                materialized[key] = available - 1
            else:
                unmaterialized.add(request_index)
        return unmaterialized

    def _unmaterialized_card_reservations(
        self,
        state: RunState,
        *,
        consume_request: bool = False,
        request_index: Optional[int] = None,
    ) -> int:
        """Count durable requests not yet represented by a distinct physical Node reservation.

        ``consume_request`` leaves out the request whose own slot is being converted right now (the
        queue position ``request_index`` names, else the head): it already owns that slot and must
        not be charged twice. ONE spelling for the three counts that charge requests: the strict
        slot count (`_node_reservation_slots_remaining`), the policy denominator
        (`_refresh_speculation_budget`) and the pure selector's limit
        (`speculation.py::_speculative_selection_node_limit`). Only the first credited anything
        until 2026-09-26; `_refresh_speculation_budget` says what the other two cost.
        """

        unmaterialized = self._unmaterialized_card_request_indices(state)
        credited = (request_index if request_index is not None else max(
            0, min(int(state.card_builds_done), len(state.card_build_requests)),
        ))
        return len(unmaterialized) - int(bool(consume_request) and credited in unmaterialized)

    def _node_reservation_slots_remaining(
        self,
        state: RunState,
        *,
        events=None,
        consume_request: bool = False,
        request_index: Optional[int] = None,
    ) -> int:
        """Return strict remaining physical slots at every new-Node append boundary.

        ``consume_request`` is used only while converting an exact outstanding speculative request
        into ``node_building``; that request already owns one slot and must not be charged twice.
        ``request_index`` names WHICH request (its queue position) when several producers hold
        requests at once; omitted, it is the head, as it always was with one producer.
        """

        if events is None:
            events = self.store.read_all()
        raw_used = self._node_id_ceiling(events, state)
        request_used = self._unmaterialized_card_reservations(
            state, consume_request=consume_request, request_index=request_index)
        return max(0, self._hard_node_reservation_limit(state) - raw_used - request_used)

    def _refresh_speculation_budget(
        self,
        state: RunState,
        *,
        events=None,
        consume_request: bool = False,
        request_index: Optional[int] = None,
    ) -> None:
        """Refresh the live policy denominator without refunding the hard Node admission ceiling.

        Card selection ranks an effective view that excludes tombstoned and currently gated Nodes. The
        configured ``max_nodes + add_nodes`` limit, however, bounds physical Node reservations. Translate
        its remaining raw slots into the effective denominator so policy intent keeps the filtered view
        while every slot already reserved — including a failed reservation gap — remains spent. This
        overrides the SpeculationMixin helper so serial and speculative Card admission share one limit.

        ``consume_request`` / ``request_index`` are the speculative CLAIM's
        (`speculation.py::_claim_requested_card_build`): the request it converts is credited,
        exactly as the strict slot count credits it, so the claim hands the policy the denominator
        its ELECTION saw as far as its OWN request is concerned — and the election runs before its
        own request exists. (A request elected AFTER it is charged, as at any refresh; that cannot
        flip the last slot, because that election itself needed a free slot with this one charged.)
        The credit is true once the request becomes a node; `_serve_card_builds` re-derives the
        strict value after every other outcome. Charged, the LAST
        slot could never be claimed: the claim's `policy.max_nodes` fell to
        `card_budget_used(state)`, so `GreedyTree.next_actions` answered "budget spent" where the
        election had seen one free slot and a due merge; another Card won, the build closed
        `stale:not_selected_now`, and the election chose the same Card again. MEASURED 2026-09-26
        on the toy driver (`max_nodes=12, eval_parallel=2, speculation_depth=2`): at build width 4,
        22 of 50 runs spun at ~97 % CPU until killed — the kept result made each cycle free, so the
        Card session never handed back; at width 1, 20 of 40 bought the same build 84-85 times and
        ended `stuck`, skipping confirmation and the noise floor. With the credit: 0 of 90
        (`tests/test_last_slot_claim_asks_the_election_question.py`).
        """
        hard_limit = self._hard_node_reservation_limit(state)
        if events is None:
            events = self.store.read_all()
        raw_used = self._node_id_ceiling(events, state)
        request_used = self._unmaterialized_card_reservations(
            state, consume_request=consume_request, request_index=request_index)
        effective_used = (
            card_budget_used(state) if self.card_driven_selection else len(state.nodes)
        )
        self.policy.max_nodes = effective_used + max(
            0, hard_limit - raw_used - request_used,
        )

    def _append_rung_promotion(self, action: dict) -> bool:
        """Durably append one row per exact ASHA halving receipt, including across resume.

        Widened Card lanes stamp the same rung/survivor decision on every chosen parent.  Speculation
        commits those parents in separate turns, so an in-memory per-lane set cannot deduplicate them.
        The append-only log is the authority: retry tail races, but suppress an exact receipt already
        recorded by an ordinary or speculative path.  A changed rung or survivor set remains distinct.
        """
        if action.get("_rung") is None:
            return False
        payload = {"rung": action["_rung"], "survivors": action.get("_promoted", [])}

        def _plan(events, tail) -> bool:
            if any(
                event.type == EV_RUNG_PROMOTED
                and event.data.get("rung") == payload["rung"]
                and event.data.get("survivors", []) == payload["survivors"]
                for event in events
            ):
                return False
            with self._id_lock:
                self.store.append(EV_RUNG_PROMOTED, payload, expected_last_seq=tail)
            return True

        # A receipt this run could not land is not a receipt: report "not appended" and let the next
        # turn re-decide, rather than claiming a halving decision the log does not carry.
        return retry_tail_cas(self.store, _plan, on_exhaust=lambda: False)

    def _ensure_plan(self, state: RunState) -> bool:
        """Write the plan row when none exists, or a re-cut one when due (`engine/plan.py`);
        True when a row was appended. Main task only; a 0 reserve fraction writes nothing."""
        from looplab.engine.plan import build_plan, replan
        if self._endgame_reserve_frac <= 0.0 or getattr(self, "_speculation_gate_calibration", False):
            return False
        # 3 is what a real Engine settles `n_seeds` to; a double's 0 cut a plan with no seed phase.
        n_seeds = int(getattr(self.policy, "n_seeds", getattr(self, "n_seeds", 3)) or 0)
        max_nodes = int(getattr(self.policy, "max_nodes", 0) or 0)
        if max_nodes <= 0:
            return False
        if state.plan is None:
            row = build_plan(max_nodes=max_nodes, n_seeds=n_seeds,
                             reserve_frac=self._endgame_reserve_frac, at_node=len(state.nodes),
                             endgame_sweep=self._endgame_sweep)
        else:
            from looplab.agents.strategist import stall_rung, strategist_stall_window
            rung, _started = stall_rung(
                state, strategist_stall_window(getattr(self, "strategist", None)))
            row = replan(state.plan, max_nodes=max_nodes, n_seeds=n_seeds,
                         reserve_frac=self._endgame_reserve_frac, at_node=len(state.nodes),
                         stall_rung=rung, endgame_sweep=self._endgame_sweep)
        if row is None:
            return False
        self.store.append(EV_PLAN, row)
        return True

    def _plan_gate(self, state: RunState, actions: list[dict]) -> list[dict]:
        """The reserve the dispatcher honours (`engine/plan.py::endgame_actions`): inside the
        endgame, breadth is replaced by the top-2 ensemble and champion sweeps."""
        from looplab.engine.plan import endgame_actions
        if state.plan is None:
            return actions
        return endgame_actions(state, state.plan, actions, sweep=self._endgame_sweep)

    def _sweep_researcher(self, researcher):
        """The k-NN surrogate the endgame's champion sweep proposes with (doc 52 row 18): bounds
        inferred from the run's own evaluated params, the run's Researcher as its fallback below
        warm-up, the run's `surrogate_explore` weight. Built once, and again only when the handle it
        wraps changes (a mid-run BOHB switch re-wraps the primary).

        NOT `search/researcher_stack.py::with_surrogate`, BY DECISION (review 2026-09-22, W5-5
        follow-up). That rule builds the surrogate LAYER of a researcher HANDLE the run keeps; this
        is a PROPOSER for one `META_SWEEP` action, which `_prepare_node_idea` asks instead of
        `researcher` and then drops — no handle is ever replaced. Each of the rule's three
        differences from this constructor would turn the sweep into something else:

        * R1 (`shares_one_agent`): the rule returns a unified facade UNWRAPPED, because re-wrapping
          one handle would split it from the developer. Nothing is re-wrapped here, so R1 has
          nothing to protect — and under the shipped `unified_agent=True` the rule would make every
          champion sweep a plain LLM improve.
        * Idempotence: the rule returns a chain that already holds a surrogate unchanged, so on a
          `surrogate_proposer` / `policy=bohb` run the sweep would be the primary's own surrogate —
          an ordinary surrogate improve.
        * Bounds: the rule adopts bounds a link DECLARES; the sweep passes `{}` with
          `infer_bounds=True` on purpose, searching the region the run has evaluated (its observed
          range padded by 10 %), not the task's whole declared space.

        `tests/test_endgame_plan.py` pins all three."""
        from looplab.search.surrogate import SurrogateResearcher
        if self._endgame_surrogate is None or self._endgame_surrogate.fallback is not researcher:
            self._endgame_surrogate = SurrogateResearcher(
                {}, fallback=researcher, explore=getattr(self, "_surrogate_explore", 0.1),
                infer_bounds=True)
        return self._endgame_surrogate

    def _select_actions(self, state: RunState) -> list[dict]:
        """Apply the explicit macro-selection authority order for one fresh fold."""
        # Receipt-backed Card selection is the narrowest authority and therefore wins when both opt-in
        # selectors are enabled. The default false flag takes the exact historical branches below.
        forced = exploit_forced_action(
            state, self.policy, max_nodes=self.policy.max_nodes,
            quantile=self.exploit_strong_node_quantile)
        if forced is not None:
            # NO SELECTOR IS CONSULTED THIS TURN, and that is the point: the card-clause version of
            # this instruction was declined fourteen times in twenty-eight (doc 56 §108, §137).
            return forced
        if self.card_driven_selection:
            return card_next_actions(
                state, self.policy, self.policy.max_nodes,
                scoring=getattr(self, "_card_scoring", None),
            )
        if self.agent_drives_actions:
            return self._agent_next_actions(state)
        return self.policy.next_actions(state)

    def _request_create_pause(self, node_id: Optional[int], reason: str, *,
                              generation: int = 0) -> None:
        """Ask the MAIN task to append the run-global auto-pause gate.

        Called from a build worker thread, where appending EV_PAUSE directly would put a FOLDED,
        run-global, selection-affecting event outside invariant #1's worker seam. `list.append` is
        atomic under the GIL, so several crashing siblings in one chunk queue safely; only the FIRST
        is appended — they are the same "a build crashed, stop the batch" gate and one pause is what
        the run needs.

        `node_id=None` QUEUES A NODE-LESS PAUSE, and a caller whose build never reached
        `node_created` must use it (review 2026-09-22, ENG1-01). `replay.py::_on_pause` reads a pause
        that NAMES a node as the scoped developer-crash breaker and DROPS it unless that node is
        folded, `failed`, with `error_reason == "developer_crash"` — so a pause naming a bare
        reservation is a gate the fold never applies. `_create_node_guarded` queued exactly that:
        driven, three `pause` rows, `fold().paused` False, and six reservations spent on
        `build_crash` against a dead provider. The node-less form is the run-global gate, the same
        payload `_refuse_degraded_proposal` queues for the same reason.

        `generation` is the named node's CURRENT attempt. `_on_pause` binds the pause to the
        generation it names, so an in-place rebuild (`_rerun_node`, attempt >= 1) that queued the
        historical hard-coded 0 would be dropped exactly the same way.
        """
        # Lazily initialised: the run loop resets the queue each iteration, but a build can crash
        # on a path that has not reached that reset yet.
        if not isinstance(getattr(self, "_pending_create_pause", None), list):
            self._pending_create_pause = []
        self._pending_create_pause.append(
            {"reason": reason} if node_id is None
            else {"node_id": node_id, "generation": generation, "reason": reason})
        self._create_paused = True   # tell the create-batch loop to STOP after this node

    def _drain_create_pause(self) -> None:
        """Append any worker-requested auto-pause. MAIN TASK ONLY — this is the seam's whole point."""
        pending = getattr(self, "_pending_create_pause", None) or []
        self._pending_create_pause = []
        if pending:
            self.store.append(EV_PAUSE, pending[0])

    # ---- the proposal path's provider circuit breaker (the twin of `developer_crash`) ----------
    #
    # A dead provider is handled correctly on the REPAIR path and in `_create_node`'s
    # developer_crash breaker: the node is FAILED and the run is PAUSED with a reason naming the
    # provider, so `looplab resume` picks it up once the endpoint is back. The RESEARCHER/proposal
    # path had no equivalent, and every role degrades on purpose, so the failure was invisible:
    # `/tmp/ll-s4b/run` (provider killed after node 0 evaluated) built three more nodes with
    # byte-identical bounds-midpoint params, spliced the transport error into the hypothesis board,
    # the node rationale, the research memo and the DURABLE CROSS-RUN CASE, declared a champion over
    # them, and finished with no reason at all and exit 0.
    #
    # PAUSE ON THE FIRST ONE, exactly like developer_crash, and for the identical argument: the
    # fallback is produced only after the role's own retries (the plain Researcher re-prompts with the
    # parse error, the agentic one runs a whole tool loop and then a forced emit), so a Researcher that
    # still cannot state a hypothesis has hit something a NEW node cannot fix. Proposing again just
    # mints more identical dead nodes. Freeze rather than finish, so a plain `resume` continues once
    # the cause is resolved — and so the run cannot report a champion or write a cross-run case over
    # experiments that were never proposed.
    _PROPOSAL_CRASH_PAUSE = (
        "auto-paused: the Researcher's LLM provider failed, so it returned a degraded FALLBACK "
        "instead of a proposal — {cause}. Nothing was proposed, so no node was built. Fix the "
        "endpoint/credentials and `looplab resume`; the run keeps every experiment it already has.")

    def _degraded_proposal_pause(self, idea) -> Optional[str]:
        """The operator-facing pause reason for a degraded proposal, or None if this is a real one."""
        if not is_researcher_fallback(idea):
            return None
        return self._PROPOSAL_CRASH_PAUSE.format(
            cause=researcher_fallback_cause(idea) or "no cause was captured")

    def _refuse_degraded_proposal(self, idea, *, main_task: bool) -> bool:
        """Refuse a role's degraded FALLBACK as a proposal, and gate the run. True when refused.

        ``main_task`` picks the append discipline, and the choice is load-bearing in the same way
        `node_build.py::developer_crash_records` documents for its five sites. The staging lane runs
        on the MAIN task and appends EV_PAUSE directly, so the very next fold sees `paused` and no
        further paid proposal is attempted. `_prepare_node_idea` can run in a build WORKER thread
        (the `llm_parallel` fan-out), where EV_PAUSE is a run-global FOLDED event outside invariant
        #1's own-node worker seam — it queues and the main task appends it after the join.

        NODE-LESS on purpose, both ways. `replay.py::_on_pause` reads a pause that NAMES a node as the
        scoped developer-crash breaker and DROPS it unless that node is already `failed` with
        `error_reason == "developer_crash"` — so a node id here (there is no node: the proposal was
        refused before any reservation) would append a pause the fold silently ignores, which is the
        same class of invisible failure as the defect itself. A node-less pause is the run-global gate,
        exactly like an operator STOP, which is what a dead provider actually is.
        """
        reason = self._degraded_proposal_pause(idea)
        if reason is None:
            return False
        if getattr(self, "_create_paused", False):
            # ONE gate per turn. A single turn can reach this twice — the staging lane refuses the
            # proposal, and the create branch then falls through to its "one ordinary serial
            # compatibility try", which proposes again and refuses again. Both are correct refusals;
            # two identical `pause` rows for one dead provider are just noise in the log the operator
            # reads. Measured on the live reproduction (`/tmp/ll-fixb/run`): seq 10 and 11, identical.
            return True
        self._create_paused = True     # stop the rest of any create batch, like developer_crash
        # REDACTED, because this `reason` is provider text. It is built from the raw `LLMError`
        # (`agents/roles.py::researcher_fallback_cause`), and a provider that quotes the request's
        # own `Authorization` header back in its error body -- an ordinary shape -- then puts the
        # operator's credential verbatim into `events.jsonl`, the file `export-bundle` copies and
        # the UI renders. Driven 2026-09-08: `sk-...` landed in the pause row while the SAME text
        # was masked to `bearer ******` two rows above, in `research_completed`. Three screens
        # already mask it (`redact_secrets`, `redact_persisted_text`, `redact_output_tail`); this
        # row simply reached none of them, because `_redact` had 7 call sites and no `EV_PAUSE`.
        reason = self._redact(reason)
        if main_task:
            if not fold(self.store.read_all()).paused:
                self.store.append(EV_PAUSE, {"reason": reason})
        else:
            # Same queue and same drain as `_request_create_pause`; only the payload differs.
            if not isinstance(getattr(self, "_pending_create_pause", None), list):
                self._pending_create_pause = []
            self._pending_create_pause.append({"reason": reason})
        return True

    def _recover_interrupted_builds(self, state: RunState) -> bool:
        """Terminalize build reservations left in-flight by a dead engine invocation.

        ``node_building`` is intentionally transient in the fold, but its id is a durable reservation.
        If the process dies before ``node_created``/``node_failed``, replay alone cannot know that no
        worker still owns it and the UI keeps rendering a live build forever. Entering ``run`` under the
        run lock is that proof: no prior engine worker can still be authoritative. Append one ordinary
        failure per surviving marker before setup/search; bare first-build reservations clear without
        fabricating a Node, while an interrupted in-place rebuild closes its current generation.
        """
        markers = getattr(state, "buildings", None) or {}
        recovered = False
        for node_id, marker in sorted(markers.items()):
            node = state.nodes.get(node_id)
            raw_generation = marker.get("generation") if isinstance(marker, dict) else None
            generation = (raw_generation if isinstance(raw_generation, int)
                          and not isinstance(raw_generation, bool) and raw_generation >= 0
                          else node.attempt if node is not None else 0)
            # every durable reservation gets a terminal outcome before any new work. Merely
            # ignoring the transient projection resurrects its breathing card on every subsequent replay.
            card_id = (marker.get("card_id") if isinstance(marker, dict)
                       and isinstance(marker.get("card_id"), str) else None)
            current_card_id = (node.idea.card_id if node is not None and node.idea is not None
                               else None)
            self._fail_reserved_build(
                node_id=node_id,
                card_id=card_id,
                generation=generation,
                reason="build_interrupted",
                error="node build was interrupted before it committed",
                # An implement-reset reuses the Node's existing Card. A propose-reset owns a newly
                # minted Card whose marker id differs until node_created lands, so it must close just
                # like a bare first build.
                #
                # `node is None` is NOT proof of a newly minted card, and reading it as one was the
                # worst bug the attach disposition shipped: an interrupted repair has no Node either,
                # and its marker names the PARENT's card. The intent below is unchanged and still
                # right for what it can see; ownership is settled in `_fail_reserved_build` against
                # the raw journal, which is the only place that CAN see it
                # (`card_reservation.py::_reservation_minted_card`). This site deliberately does not
                # re-derive that — one authority, one spelling.
                drop_card=(node is None or (card_id is not None and card_id != current_card_id)),
            )
            recovered = True
        return recovered

    # *Closed 2026-09-08 (`paid-cadences-hold-the-engine-loop`): the whole block runs off the loop
    # thread through `_offload_cadence` — one worker hop under `_BufferedCadenceStore`, which buffers
    # every FOLDED row for the main task to publish and lets only the two registered thread-side
    # seams (`DIAGNOSTIC_EVENTS`, `BACKGROUND_APPENDABLE`) through live. The two constraints recorded
    # below are what the implementation is shaped by, so they are kept rather than deleted with the
    # marker.*
    #
    # WHAT USED TO BE HERE: every paid cadence below executed as ONE event-loop callback — the
    # Strategist consult (unbounded turns under the shipped `agent_max_turns=0`), the concept
    # re-tag/consolidation pass (`_RETAG_CAP` 20 + `_HYP_TAG_CAP` 60 sequential tag calls), the
    # verifier tie-break, the report refresh and `foresight_rank`. `at_creation_boundary` made these
    # gates due WHILE evaluations run, so the hold landed on top of a live GPU.
    #
    # MEASURED 2026-09-04, and it is why this sat open for four days rather than being taken on a
    # hunch. Every `operation` span on v11 (a full 24 h run, 9 evaluations), totalled by name:
    #     strategist_consult 3 calls 5.9 min | concept_coverage 2 calls 4.9 min
    #     foresight_rank     2 calls 2.7 min | report           3 calls 2.0 min
    #     -> every paid cadence together ~15.5 min, against `evaluate` at 5026.6 min.
    # 0.3% of the run. The same sweep found where the hold actually is: `card_build`, 608.6 min over
    # 13 calls — the serial-node-build item, which `_offload_build` closed. What the wall-clock share
    # does NOT price, and what decided this in the end, is WHAT the loop owes during those minutes:
    # the eval watcher tick, operator abort/reset detection, the train-monitor kill signal and the
    # control ACK are all main-task work, so 15.5 minutes of hold is 15.5 minutes of a burning GPU
    # that cannot be stopped, not 0.3% of a delay.
    #
    # DRIVEN 2026-09-08, so the hold was measured rather than asserted before it was removed: a
    # tick-counter task read from INSIDE a blocking stub at each site, over a real `engine.run()`,
    # counted 177->177 (strategist), 38->38 (report), 36->36 (concept), 34->34 (verifier). Zero
    # ticks, all four. The mechanism was one line: `_run_cadences` is a plain `def` with no `await`
    # in it, called as `state = self._run_cadences(state)` from the async spine, so nothing in it
    # could ever yield. (The finalize report holds too but is not one of these — it runs on a run
    # that is already ending.)
    #
    # TWO CONSTRAINTS THE OFFLOAD MEETS, both found by building one and neither obvious from this
    # site, recorded so a future change to it does not re-derive them:
    #   * all nine row types these cadences write are FOLDED and none is in `DIAGNOSTIC_EVENTS`, so
    #     a bare `to_thread` is out. The load-bearing one is `verifier_group_scored`: it MOVES the
    #     champion tie-break, so a worker-thread append landing inside a Card reservation's window
    #     is exactly the `score_moved` conjunct `card_reservation.py::_proposal_receipt_fence`
    #     discards an already-paid proposal on.
    #   * `novelty.py::_offload_under_proposal_sink` cannot be reused as-is, and for ONE reason,
    #     not two: its sink intercepts `_append_proposal_event` only. The second reason recorded
    #     here on 2026-09-08 -- that it publishes under the receipt fence's ELECTION rule while a
    #     cadence must publish unconditionally -- was FALSE, and driven false: the helper publishes
    #     from a bare `finally`, on return AND on raise, which its own two neighbouring docstrings
    #     already said. A wrong reason in this position is worse than no reason, because it steers
    #     the next attempt away from the helper it should be extending.
    def _run_cadences(self, state: RunState) -> RunState:
        # Breadth read-model: record the run's narrowing curve at the strategist cadence BEFORE the
        # Strategist decides, so the same snapshot both (a) feeds the meta-controller's decision
        # context and (b) lands in the log for the UI / historical-replay measurement. It never
        # re-ranks the current champion directly, but it can change later policy/proposal cues;
        # replay-safe (at_node gate), no-op when coverage_context is off. See search/coverage.py.
        state = self._maybe_snapshot_coverage(state)

        # PART IV Phase 2a: concept-graph coverage + uncovered-region snapshot (the "0 coverage in {X}"
        # pivot signal). Deterministic, replay-safe (at_node gate); no-op when concept_pivot is off or
        # the task has no curated concept skeleton. Feeds the explore-stance novelty hint below.
        state = self._maybe_snapshot_concept_coverage(state)

        # PART V (B): seed the RUN BASE concept set from the first evaluated node's authored concepts, once.
        # Idempotent (fires only while run_base_concepts is empty), replay-safe. Turns on per-node DELTA
        # authoring downstream (proposal_cues injects the base + a "author concepts_added/removed" directive).
        state = self._maybe_seed_run_base_concepts(state)

        # R1-c: calibrated §12-verifier metric-tie-break. When select_verifier is on and eligible nodes
        # TIE on the ranked metric, verify the tied nodes (grounded on their realized result) so the
        # fold's final selector breaks the tie by soundness. Lazy (only real ties), replay-safe (persists one
        # verifier_group_scored event), advisory (never overrides a strictly-better metric). No-op when off.
        state = self._maybe_verify_ties(state)

        # docs/BACKLOG.md §0.1 row 17: the LLM VALUE ESTIMATE for the MCTS candidates — how much a
        # model thinks each branch still has left — frozen into the log so the tree can tell an
        # unexpanded branch from a spent one at the same metric. Placed BEFORE the Strategist for
        # the reason `_maybe_verify_ties` is: the policy that reads the estimates may be rebuilt by
        # `_apply_strategy`, and an estimate bought after that rebuild would be spent on a weight
        # the turn no longer uses. Replay-safe (the recorded per-node estimate is what the fold
        # reads, never a live call), and a no-op unless the live policy's `value_weight` is > 0 —
        # which is also the gate on the paid call itself, so today it costs nothing.
        state = self._maybe_estimate_node_values(state)

        # A7 Strategist: adapt the search machinery (policy/operators/fidelity/Developer) before
        # the policy proposes the next actions. No-op when strategist is off (== today).
        state = self._maybe_consult_strategist(state)

        # Deep-Research stage (Phase 2): a "go think hard" step over a bounded stratified run
        # summary + the web that
        # writes a memo to steer the next batch. Fires on a manual request, a cadence, or a
        # Strategist `request_research`. No-op when the stage is off. Replay-safe (gated).
        state = self._maybe_deep_research(state)

        # Run report (conclusion-first, agent-authored): regenerate on a node-count cadence so the
        # Report grows with the search. Selection-neutral narrative; no-op when off. Replay-safe (gated
        # on the report receipt's at_node). The deterministic report renders regardless.
        state = self._maybe_refresh_report(state)

        # Agentic hypothesis-board consolidation: the exact-hash ledger keeps paraphrases apart, so the
        # open board accumulates near-duplicate beliefs (deep-research directions + researcher + human
        # all phrasing the same idea). Hybrid-retrieve the near-dups + let the Researcher decide the
        # true merges, recorded as `hypothesis_merged` events the fold applies deterministically.
        state = self._maybe_merge_hypotheses(state)
        # NO CARD MIRROR HERE (review 2026-09-22, ENG1-06). An in-block
        # `_mirror_hypothesis_card_merges` stood on this line, and this whole block runs under the
        # cadence sink: it read the BUFFERED view, whose seqs are synthetic ("view-only: the publish
        # assigns the real ones"), and stamped `card_merged.source_event_seq` from it. Any
        # passthrough row landing first — the merge's own `llm_usage` — shifted the real seq, so the
        # receipt named the wrong row and the next turn's mirror, keyed on that seq, wrote a SECOND
        # `card_merged` (measured: 33 -> `llm_usage`, then [33, 34]). The mirror at the top of every
        # loop turn in `_run_with_llm_broker` runs on a stable, published prefix before any gate, so
        # it records the merge at its real seq one turn later; `card_merged` is additive audit —
        # replay already applies the merge from `hypothesis_merged` itself.

        # M6 comparative lessons, live-shared (doc 13 §7 items 2+5): on a node-count cadence,
        # distill credit-assigned PAIR lessons into the SHARED cross-run store DURING the run
        # (write side), and re-read the store so lessons distilled by CONCURRENT runs reach
        # this run's proposals (read side). The receipts do not re-rank current nodes, but they gate
        # paid cadence work and their shared-store output steers later proposals; replay-safe
        # (at_node gates), no-op when the cadences are 0.
        state = self._maybe_distill_lessons(state)
        state = self._maybe_refresh_lessons(state)

        # M4 auto-skills, same shape and the same `lessons_every` pace: promote the technique of a
        # card whose evidence has SETTLED into the shared skill store now, instead of holding every
        # promotion for the run-end reflection — which a killed run never reaches. Replay-safe (the
        # `skills_promoted` at_node gate), no-op when the cadence is 0, and the run-end pass skips
        # what this one already wrote so the classifier is still paid once per card.
        state = self._maybe_promote_skills(state)

        # Reconciliation (memory ↔ corrected outcomes): when a node_reset re-eval FLIPS a node's
        # outcome (a false-failure re-scored to evaluated, a demoted champion), this run's DISTILLED
        # lessons grounded in that node go stale — fold-derived memory self-corrects but the LLM-written
        # lesson file does not. Retire + re-derive those lessons from the corrected state. Cheap
        # {node->sig}-hash gate: no-op unless a signature actually moved; LLM only on a genuine drift.
        state = self._maybe_reconcile_lessons(state)
        # Layer 1b: the producers above may run in background/read-only channels, while Card events are
        # main-task-only.  Materialize their opaque memo/lesson/claim refs now, with exact Card + node
        # lifecycle + proposal fences; no bodies or paths cross into the Card ledger.
        return self._sync_card_enrichments(state)

    # ------------------------------- strategist cadence (extracted to engine/strategy.py)
    # The A7 strategist-consultation + coverage-snapshot cluster (`_strategy_core`,
    # `_available_developers`, `_strategy_ctx`, `_coverage_for_ctx`, `_should_consult`,
    # `_record_strategy`, `_ensure_surrogate`, `_apply_strategy`,
    # `_maybe_snapshot_coverage`, `_maybe_consult_strategist`) lives in looplab/engine/strategy.py
    # (StrategyCadenceMixin — inherited, zero call-site churn). `_op_span` did NOT come with it and no
    # longer lives here either: it is a generic new-trace span helper shared by the research /
    # hypothesis-merge / lessons clusters too, so it moved to `engine/shared.py::SharedEngineMixin`
    # (called from more than one cluster, owns no state of its own — the bar that module documents).
    # Two clusters that never belonged to the strategist cadence left it in doc 25 EC-09: the PART
    # IV/V concept cadence (`_should_consult_concepts`, `_maybe_snapshot_concept_coverage`,
    # `_maybe_seed_run_base_concepts`, `_concept_coverage_snapshot` + its steps) is
    # engine/concept_cadence.py (ConceptCadenceMixin) and paces on `concept_retag_every`, not
    # `strategist_every`; the R1-c calibrated-verifier tie-break (`_maybe_verify_ties`,
    # `_metric_tie_groups`, `_verifier_soundness`) is engine/verifier_tiebreak.py
    # (VerifierTiebreakMixin) and is SELECTION machinery. The at_node idempotence gate all three
    # snapshot sites shared is no longer an Engine member at all: it is
    # `search/coverage.py::already_covered_at(state, n, snapshots)`, beside the projection match it
    # composes and beside `latest_live_snapshot`, its mirror on the consumption side.

    # ------------------------------ research cadence (extracted to engine/research_cadence.py)
    # The P2 deep-research + open-hypothesis-board merge + run-report cadence cluster
    # (`_maybe_deep_research`, `_ground_run_start`, `_already_researched_at`, `_run_deep_research`,
    # `_compute_deep_research`, `_record_deep_research`, `_due_research_trigger`,
    # `_maybe_merge_hypotheses`, `_maybe_refresh_report`, `_write_report`) lives in
    # looplab/engine/research_cadence.py (ResearchCadenceMixin — inherited, zero call-site churn).

    # ----------------------------------------------------------- proposal cues
    # `_set_complexity_hint` / `_stamp_novelty_hint` live in looplab/engine/proposal_cues.py
    # (ProposalCuesMixin — inherited, zero call-site churn; the hint-forwarding registry test
    # source-scans that module too).

    # The sub-object forwarding seam, DECLARED (doc 25 ES-13): `<engine name> -> (sub-object, lane)`
    # for every one-line delegator below that forwards to `self.<sub-object>.<same name minus the
    # leading underscore>`. Every entry follows that naming rule exactly, which is what makes the
    # table checkable rather than decorative.
    #
    # It exists because of ONE named cost, and it removes exactly that one: a new `LessonMemory` /
    # `HoldoutGrader` / `Workspace` method needs a hand-written forwarder carrying the correct
    # `@in_llm_lane`, and several of them carry one (the table below is the count; the prose that
    # kept one by hand said 32 and eight while the table held 34 and seven). Forgetting the lane does not fail —
    # the call simply runs outside the capped enrichment lane and competes with foreground work for
    # provider concurrency, which shows up as an unexplained stall, not an error. The two-way guard
    # in `tests/test_engine_forwarding_registry.py` turns both halves (a delegator missing from the
    # table, a table entry whose lane no longer matches) into a red test.
    #
    # The delegators stay WRITTEN OUT rather than generated from this table — see the resolution note
    # in doc 25 for the measurement behind that.
    FORWARDED_SUBOBJECT_MEMBERS = {
        # --- lessons (looplab/engine/lessons.py::LessonMemory)
        "_load_reflection_priors": ("lessons", None),
        "_load_reflection_priors_both": ("lessons", None),
        "_empty_state_for_fp": ("lessons", None),
        "_task_fingerprint": ("lessons", None),
        # `_write_reflection_note` is NOT here: it now opens its own `_op_span("reflection")` and so
        # is no longer a one-line delegator, exactly like `_maybe_distill_lessons` /
        # `_maybe_refresh_lessons` / `_maybe_reconcile_lessons`, which have always been out of this
        # table for the same reason. The guard below refuses a stale entry precisely because a
        # registry that claims to check a lane it no longer reaches "reads as coverage".
        "_reflect_lessons": ("lessons", "enrichment"),
        "_append_lessons": ("lessons", None),
        "_comparative_lessons": ("lessons", "enrichment"),
        "_lessons_store_stamp": ("lessons", None),
        "_distill_skill_body": ("lessons", None),
        "_reflect_client": ("lessons", None),
        "_causal_meta_note": ("lessons", "enrichment"),
        "_store_case": ("lessons", None),
        "_store_concept_capsule": ("lessons", None),
        "_store_research_claims": ("lessons", "enrichment"),
        # `_store_concept_curation` / `_store_claim_curation` / `_store_task_facets` left this
        # table on 2026-09-22 for the same reason `_write_reflection_note` did: each now opens its
        # own op-span, because each PAYS in the post-stage finalize window (review 2026-09-22,
        # ENG3-06). Their `@in_llm_lane("enrichment")` stays and is still what caps them.
        # --- holdout (engine/holdout.py::HoldoutGrader)
        "_graded_output_name": ("holdout", None),
        "_apply_host_grade": ("holdout", None),
        "_host_score_split": ("holdout", None),
        # The one ASYNC delegator in the table, and it went undeclared from 2026 until
        # 2026-09-08 because the guard filtered on `ast.FunctionDef`, which does not match
        # `ast.AsyncFunctionDef` — so an async delegator was invisible to it in BOTH
        # directions. Lane `None` is correct and checked rather than assumed:
        # `HoldoutGrader.holdout_phase` makes no provider call and carries no `in_llm_lane`.
        "_holdout_phase": ("holdout", None),
        "_build_holdout_idx": ("holdout", None),
        "_apply_search_split": ("holdout", None),
        "_holdout_topk": ("holdout", None),
        "_holdout_pending": ("holdout", None),
        # --- workspace (looplab/engine/workspace.py::Workspace)
        "_write_assets": ("workspace", None),
        "_write_node_files": ("workspace", None),
        "_materialize": ("workspace", None),
        "_workspace_fingerprint": ("workspace", None),
        "_substrate_fingerprint": ("workspace", None),
        "_seed_workspace": ("workspace", None),
        "_seed_repo_tree": ("workspace", None),
        "_link_input": ("workspace", None),
        "_sandbox_cwd": ("workspace", None),
    }

    # ---------------------------- cross-run memory / lessons / reflection (extracted)
    # The lessons/reflection cluster lives in looplab/engine/lessons.py (`LessonMemory`,
    # constructed as `self.lessons` in __init__). These thin delegators keep the ORIGINAL
    # method/attribute names on the Engine — tests call and monkeypatch e.g.
    # `engine._write_reflection_note` / `engine._reflect_client` / `engine._prior_note_text` —
    # and LessonMemory routes its internal cross-calls back through them, so an instance-level
    # monkeypatch intercepts every path.
    #
    # The property pairs immediately below and the three `staticmethod(...)` aliases further down are
    # deliberately NOT in `FORWARDED_SUBOBJECT_MEMBERS`: a property forwards an ATTRIBUTE (both ways,
    # via its setter) rather than a call, and a staticmethod alias binds `LessonMemory`'s own function
    # — so unlike every entry in the table it does NOT follow `self.lessons`, and re-pointing that
    # instance would not intercept it. Three different forwarding semantics, kept visibly different.
    @property
    def _lessons_seen_stamp(self):
        return self.lessons.seen_stamp

    @_lessons_seen_stamp.setter
    def _lessons_seen_stamp(self, value) -> None:
        self.lessons.seen_stamp = value

    @property
    def _prior_note_text(self) -> str:
        return self.lessons.prior_note_text

    @_prior_note_text.setter
    def _prior_note_text(self, value: str) -> None:
        self.lessons.prior_note_text = value

    @property
    def _dev_prior_note_text(self) -> str:
        return self.lessons.dev_prior_note_text

    @_dev_prior_note_text.setter
    def _dev_prior_note_text(self, value: str) -> None:
        self.lessons.dev_prior_note_text = value

    def _load_reflection_priors(self, exclude_run_id: Optional[str] = None,
                                exclude_run_uid: Optional[str] = None,
                                role: Optional[str] = None) -> str:
        return self.lessons.load_reflection_priors(
            exclude_run_id=exclude_run_id, exclude_run_uid=exclude_run_uid, role=role)

    def _load_reflection_priors_both(self, exclude_run_id: Optional[str] = None,
                                     exclude_run_uid: Optional[str] = None) -> tuple[str, str]:
        return self.lessons.load_reflection_priors_both(
            exclude_run_id=exclude_run_id, exclude_run_uid=exclude_run_uid)

    def _empty_state_for_fp(self) -> RunState:
        return self.lessons.empty_state_for_fp()

    def _task_fingerprint(self, final: RunState, best=None) -> list[str]:
        return self.lessons.task_fingerprint(final, best)

    @in_llm_lane("enrichment")
    def _write_reflection_note(self, final: RunState) -> None:
        # Own op-trace, like the three `lessons_*` siblings below. Run-end reflection is PAID and
        # sits past the last stage span, so without this its generations land with no span open at
        # all -- billed, and absent from every trace surface. Measured 2026-08-29 over 68 probe
        # runs: 105 of 25,430 billed calls ($0.19 of $100.27) had no `span_id`, all of them in this
        # window, and `core/tracing.py::_note_untraced_generation` names the site in `run.log` as
        # `<file>:<line> in <function>` -- which is `tool_loop.py::drive_tool_loop`. Spelling it as
        # the symbol is this repo's rule (`test_claim_pins::test_no_source_citation_is_dead`); the
        # log's own line number is NOT what the log says, so it is reported as a translation rather
        # than as a quotation. One span here also covers what reflection calls in turn
        # (`_reflect_lessons`, `_comparative_lessons`), which is why those two need none of their own.
        with self._op_span("reflection"):
            return self.lessons.write_reflection_note(final)

    @in_llm_lane("enrichment")
    def _reflect_lessons(self, final: RunState, best, fp: list) -> list:
        return self.lessons.reflect_lessons(final, best, fp)

    def _append_lessons(self, lessons: list, *, hygiene: bool = True, state: RunState = None) -> None:
        return self.lessons.append_lessons(lessons, hygiene=hygiene, state=state)

    @in_llm_lane("enrichment")
    def _comparative_lessons(self, state: RunState, fp: list, exclude=()) -> tuple[list, list]:
        return self.lessons.comparative_lessons(state, fp, exclude=exclude)

    _spent_pairs = staticmethod(LessonMemory.spent_pairs)

    @in_llm_lane("enrichment")
    def _maybe_distill_lessons(self, state: RunState) -> RunState:
        # Own op-trace: LessonMemory writes lessons_distilled via the SAME store, so an append inside
        # this span is stamped with it (current_ids) → the UI scopes the event's trace to the distill.
        with self._op_span("lessons_distill"):
            return self.lessons.maybe_distill_lessons(state)

    @in_llm_lane("enrichment")
    def _maybe_promote_skills(self, state: RunState) -> RunState:
        # Own op-trace for the same reason as the distill above: the classifier calls this pass makes
        # are real money, and a beacon-only phase writes them with `trace_id=null` (CLAUDE.md's span
        # rule). Not in `FORWARDED_SUBOBJECT_MEMBERS` — like every other `_maybe_*` here it opens a
        # span and so is not a one-line delegator.
        with self._op_span("skills_promote"):
            return self.lessons.maybe_promote_skills(state)

    def _lessons_store_stamp(self):
        return self.lessons.lessons_store_stamp()

    @in_llm_lane("enrichment")
    def _maybe_refresh_lessons(self, state: RunState) -> RunState:
        with self._op_span("lessons_refresh"):
            return self.lessons.maybe_refresh_lessons(state)

    @in_llm_lane("enrichment")
    def _maybe_reconcile_lessons(self, state: RunState) -> RunState:
        # Own op-trace: reconcile appends lessons_reconciled / lessons_distilled via the SAME store,
        # so those events are scoped to this span in the UI.
        with self._op_span("lessons_reconcile"):
            return self.lessons.reconcile_lessons(state)

    def _distill_skill_body(self, final: RunState, h, ev: list) -> str:
        return self.lessons.distill_skill_body(final, h, ev)

    def _reflect_client(self):
        return self.lessons.reflect_client()

    @in_llm_lane("enrichment")
    def _causal_meta_note(self, final: RunState, best) -> Optional[str]:
        return self.lessons.causal_meta_note(final, best)

    _consolidate_lessons_file = staticmethod(LessonMemory.consolidate_lessons_file)
    _compact_lessons = staticmethod(LessonMemory.compact_lessons)

    def _store_case(self, final: RunState) -> None:
        return self.lessons.store_case(final)

    def _store_concept_capsule(self, final: RunState) -> None:
        return self.lessons.store_concept_capsule(final)

    @in_llm_lane("enrichment")
    def _store_research_claims(self, final: RunState) -> None:
        return self.lessons.store_research_claims(final)

    # THE THREE FINALIZE STEWARDS PAY, so each opens its own op-trace (review 2026-09-22, ENG3-06).
    # `@in_llm_lane` LABELS a lane and opens no span, and finalize calls these after the last stage
    # span has closed — so with `cross_run_curation` ON (the shipped default) every finalize wrote
    # two or three steward calls with `trace_id=null`: billed, and absent from `looplab timings`,
    # the trace view and every per-phase cost question (CLAUDE.md's span rule). The same window and
    # the same fix as `_write_reflection_note` above; each span is named for the steward's own
    # `finalize_step` receipt, so the trace and the receipt read as one step.
    @in_llm_lane("enrichment")
    def _store_concept_curation(self, final: RunState) -> str:
        with self._op_span("concept_curation"):
            return self.lessons.store_concept_curation(final)

    @in_llm_lane("enrichment")
    def _store_claim_curation(self, final: RunState) -> str:
        with self._op_span("claim_curation"):
            return self.lessons.store_claim_curation(final)

    @in_llm_lane("enrichment")
    def _store_task_facets(self, final: RunState) -> str:
        with self._op_span("task_facets"):
            return self.lessons.store_task_facets(final)


    # -------------------------------------------------- novelty gate (extracted to engine/novelty.py)
    # The E1/T5 novelty/dedup gate cluster (`_idea_text`, `_idea_vec`, `_semantic_duplicate`,
    # `_llm_novelty_gate`, `_apply_novelty_gate`) lives in looplab/engine/novelty.py
    # (NoveltyGateMixin — inherited, zero call-site churn).

    # ------------------------------------------------------------- node creation
    # ---------------------------------------------------------- node building
    # `_ensemble_idea` / `_agent_next_actions` / `_implement` / `_directed_idea` / `_repair` /
    # `_emit_node_created` live in looplab/engine/node_build.py (NodeBuildMixin — inherited,
    # zero call-site churn). `_create_node` / `_rerun_node` / `_create_injected_node` stay HERE:
    # they call the module-global `fold` that two tests monkeypatch through this module.

    # ----------------------------------------------------------- crash & repair
    # `_triage_crash` / `_repair_error_context` / `_prepare_env` live in
    # looplab/engine/crash_repair.py (CrashRepairMixin — inherited, zero call-site churn).

    def _build_calls_an_llm(self) -> bool:
        """Does building one node make provider calls at all?

        Read off the ROLES rather than a backend string, because the engine never sees
        `Settings.backend`: every LLM-backed role carries the shared client (`agents/roles.py` —
        wrappers forward `client` read-through, and `search/foresight.py`'s panel proxies it), and an
        external coding-agent Developer declares `is_code_generating` instead of holding a client.
        Either marker means a build has provider latency to overlap. Neither means the build is pure
        local Python (`task.build_roles()` — the Toy/templated roles) and finishes in microseconds.
        Total by construction: an exotic role that raises on attribute access still answers "no LLM",
        which only ever costs fan-out, never correctness.

        THE FACADE HAS TO BE OPENED, and this is where the first version got it wrong. Under the
        shipped `unified_agent=True`, `self.researcher IS self.developer` — one `UnifiedAgent` — and
        its `client`/`is_code_generating` forwarders come from `WrapsDeveloper`, so they describe the
        DEVELOPER stage only (`agents/unified_agent.py::_wrapped` -> `_active_developer`). On every
        task whose Developer is a fixed template but whose Researcher is an `LLMResearcher` —
        classification, regression (timeseries too, until its LLM path started writing the
        forecaster on 2026-09-08) — both probes therefore read the same client-less
        template and the whole product default answered "no LLM" while calling the provider once per
        node. Measured on `examples/classification_task.json` with stock Settings: `run_started`
        recorded no `speculation_depth` at all (AUTO had settled to 0) even though the run's own
        `llm_usage` rows show the Researcher on the wire before the first node existed. So descend
        into the facade's own per-stage backends as well.

        …AND THE WRAPPER CHAIN, for the same failure one layer out (review 2026-09-22, W5-5
        follow-up). `SurrogateResearcher` hides its fallback's `client` on purpose, so a NON-unified
        run whose LLM Researcher sits behind the surrogate (`surrogate_proposer`, `policy=bohb`) and
        whose Developer is a template read as "no LLM" — driven through `cli/__init__.py::_engine` on
        the toy task at `max_parallel=4`: `llm_parallel` 4 -> 1 and `speculation_depth` 4 -> 0 at
        launch, and `Panel(Surrogate(...))` the same. Every link is now tested the same way, reached
        through the registered handles alone — the facade's stages (`FACADE_STAGE_ATTRS`) and each
        wrapper's wrapped role (`WRAPPED_ROLE_ATTRS`, the names
        `search/researcher_stack.py::researcher_chain` walks) — never an `isinstance` on a search
        class, so a new wrapper is seen by holding its role under a registered name. True here is a
        LAUNCH answer and is right for the surrogate: below its warm-up (every early node, and
        forever on a task whose params carry no numbers) it delegates each proposal to that LLM
        Researcher.
        """
        seen: list = []
        # Breadth-first over the roles and every link reachable from them. `seen` is compared by
        # IDENTITY (a unified facade is both roles, `UnifiedAgent.inner` may be its own developer, a
        # `__getattr__` proxy answers for its base) and CAPPED, so a proxy that mints a fresh object
        # per attribute read can only cost the walk its cap, never loop it.
        pending: list = [getattr(self, "researcher", None), getattr(self, "developer", None)]
        while pending and len(seen) < 32:
            link = pending.pop(0)
            if link is None or any(link is other for other in seen):
                continue
            seen.append(link)
            try:
                if getattr(link, "client", None) is not None:
                    return True
                if getattr(link, "is_code_generating", False):
                    return True
                # A composing facade (UnifiedAgent) exposes its stages PUBLICLY, for the same reason
                # the cost roll-up walks them: `researcher`/`developer` are the per-stage backends and
                # `stage_clients` holds the clients no backend owns (strategy, pilot). A wrapper holds
                # the role it wraps under `base`/`fallback`/`inner`. Both kinds are queued as links and
                # tested exactly like a role; the identity check above is the self-reference guard.
                pending.extend((getattr(link, "researcher", None), getattr(link, "developer", None),
                                getattr(link, "base", None), getattr(link, "fallback", None),
                                getattr(link, "inner", None)))
                if any(client is not None for client in (getattr(link, "stage_clients", None) or ())):
                    return True
            except Exception:  # noqa: BLE001 — a proxy/property that raises is not evidence of an LLM
                continue
        return False

    def _resolve_llm_parallel(self, value: int) -> int:
        """Resolve startup ``llm_parallel`` to a concrete build fan-out. ``0`` = AUTO = the (already
        resolved) ``self._eval_parallel``, so we build exactly as many seeds as we can concurrently evaluate;
        any other value is used as-is (clamped to >=1). The build pool still clamps to 1 downstream
        (`_build_role_pairs`) when no `role_factory` is wired. Live strategy/control updates use 0=1
        because they settle immediately rather than retaining an AUTO mode."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return 1
        # AUTO on a build that calls NO LLM settles to serial width 1. Fan-out exists to overlap
        # provider LATENCY; a Toy/templated build has none, so a width derived from the GPU count buys
        # exactly nothing and costs the property CLAUDE.md invariant #1 states: "a settled build width
        # of 1 keeps the strict 'only the main task appends' behaviour, byte-identical". Since
        # `llm_parallel` began defaulting to AUTO (2026-08-04) and `cli/__init__.py` wires
        # `role_factory` unconditionally, the documented offline smoke fanned out on any GPU box and
        # produced THREE distinct event orders across 8 identical runs — and `bench.py`, the
        # capability-regression harness, inherits the same AUTO width while promising "Deterministic
        # for the toy backend". This restores that promise where it is made instead of retracting it.
        # An EXPLICITLY spelled width is still honoured as spelled: an operator who asks a toy run to
        # fan out (a concurrency test) gets the fan-out and its nondeterministic byte order.
        if value == 0 and not self._build_calls_an_llm():
            return 1
        # Clamp to the config `le=64` ceiling on BOTH branches: AUTO resolves to max_parallel (config
        # `le=1024`), which must not silently exceed the parallel_build cap the config author set (nor
        # eagerly instantiate >64 wired role pairs); the operator budget-override path is otherwise
        # unvalidated. The explicit Settings/Strategist paths are already bounded 0..64.
        resolved = self._eval_parallel if value == 0 else value
        return min(LLM_WIDTH_MAX, max(1, resolved))

    def _resolve_speculation_depth(self, value) -> tuple[int, bool]:
        """Resolve startup ``speculation_depth`` to a settled backlog cap plus its AUTO flag.

        ``-1`` = AUTO = one speculative prefetch per concurrent evaluation lane, i.e. the ALREADY
        SETTLED ``self._eval_parallel`` (itself ``0`` = AUTO = one experiment per detected GPU, at
        least one), clamped to 1..64. Depth follows the eval width rather than the LLM width because
        the backlog exists to keep the box busy: what a prefetch buys is a node ready the moment an
        eval lane frees, so more prefetches than lanes buy nothing. (With ``llm_parallel`` itself
        defaulting to AUTO = the eval width, the two coincide on an unconfigured box.)

        AUTO needs its own sentinel because ``0`` is already the hard off-switch AND a run_started
        pinned search treatment. The returned flag records that the operator asked for AUTO, so
        re-entry can prefer the log's pinned depth over a value re-derived from a different box
        (invariant #6) instead of refusing the resume — see `_require_pinned_speculation_receipt`.
        Anything unparseable degrades to OFF: hardware must never be able to turn speculation ON.

        AUTO ALSO SETTLES TO OFF in three cases, all of them "this run cannot usefully prefetch".
        Every one of them is AUTO-only: an EXPLICITLY spelled depth is honoured (or refused) exactly
        as before, including the hidden calibration bootstrap, which spells ``speculation_depth=1`` on
        the Toy adapter and must keep getting it.

        1. A BUILD THAT CALLS NO LLM (`_build_calls_an_llm`, the same test and the same reasoning
           `_resolve_llm_parallel` applies to the build axis one method up). A prefetch exists to
           overlap the Developer's PROVIDER LATENCY with the running evaluation; a Toy/templated build
           is pure local Python that finishes in microseconds, so the backlog buys nothing — and it
           costs the property CLAUDE.md invariant #1 and `bench.py`'s "deterministic for the toy
           backend" both state. MEASURED, on the documented offline smoke: with AUTO reaching depth 1
           there, 8 identical runs produced TWO event orders (5x129 events, 3x126). The folded state,
           champion and every metric were IDENTICAL in all eight — the divergence is a wall-clock race
           between the producer and the eval terminal, where an eval that finishes first closes the
           admitted batch and the in-flight head is acknowledged `skipped="stale"` and re-requested,
           three extra rows. Nothing is mis-selected and nothing is double-paid, but the log's BYTES
           stop being reproducible, and unlike the build-width case there is no smaller width to fall
           back to: depth 1 IS the minimum. So the fix has to be the same one 5f86626d made for
           builds — do not turn the overlap on where there is no latency to overlap.
        2. A POLICY OTHER THAN ``greedy``, and 3. A RUN DIRECTORY WITH NO RUN ID. The admission block
           in ``__init__`` raises ``ValueError`` for both (the speculative freshness test asks the
           policy for the counterfactual next action and greedy is the only one the Card scorer was
           built against; the run id is half of the re-entry identity check). Those refusals are right
           for an operator who ASKED for a depth — they name a configuration that cannot do what was
           requested — but wrong for a DEFAULT: refusing would mean ``looplab run --policy mcts`` (or
           evolutionary/asha/bohb) no longer starts at all, i.e. a default flip that silently retires
           four of the five shipped policies.

        Same direction as the unparseable case above, and as the eval axis settling to 1 for a task
        that declares itself CPU-locked: AUTO narrows itself to what the run can actually serve.
        """
        try:
            depth = int(value)
        except (TypeError, ValueError, OverflowError):
            return 0, False
        if depth != -1:
            return depth, False
        if (not self._build_calls_an_llm()
                # Bare: its one caller is `__init__`, after `_policy_name` is assigned (ENG1-03).
                or self._policy_name != SPECULATION_POLICY_SCOPE
                or not self.run_dir.name.strip()):
            # Report AUTO=False as well: the run is not speculating, so there is no AUTO treatment
            # for re-entry to adopt, and a log that pinned a positive depth must still fail closed
            # here rather than silently adopting it into a policy or a role set the treatment was
            # never measured on.
            return 0, False
        return min(LLM_WIDTH_MAX, max(1, int(self._eval_parallel))), True

    # ------------------------------------------------------------------ the run's event store
    # A PROPERTY, and the ONE reason is the cadence offload (`_offload_cadence`). Every cadence in
    # `_run_cadences` writes through `self.store.append` from eleven modules, so that is where the
    # worker's buffer has to intercept; hoisting all of those onto a new funnel would be a rename
    # across the cluster whose one forgotten site is a silent breach of invariant #1. Resolving the
    # attribute per CONTEXT instead means the offload installs its view once and every writer in the
    # block is covered, while every other task — the evaluations above all — keeps the real store,
    # because a ContextVar set on this task after they were spawned is invisible to them.
    #
    # The setter is what keeps the ~170 direct `Engine(...)` call sites and the tests that swap
    # `engine.store = <fake>` working unchanged: assignment still lands on one plain attribute.
    @property
    def store(self):
        buffered = _CADENCE_STORE_SINK.get()
        return buffered if buffered is not None else self._event_store

    @store.setter
    def store(self, value) -> None:
        self._event_store = value

    async def _offload_cadence(self, fn):
        """Run the paid cadence block OFF the loop thread, under the store sink, publishing on the
        way out. Returns whatever `fn` returned.

        THE HOLD THIS REMOVES, driven rather than asserted (2026-09-08): a tick-counter task reading
        from inside a blocking stub at each cadence site, over a real `engine.run()`, counted
        177->177 (strategist), 38->38 (report), 36->36 (concept) and 34->34 (verifier). Zero ticks,
        all four — because `_run_cadences` is a plain `def` with no `await` in it, called from the
        async spine, so nothing in it could ever yield. Since `cadence.at_creation_boundary` those
        gates come due WHILE evaluations run, so for as long as the block spends there is no eval
        watcher tick, no operator abort/reset detection, no train-monitor kill signal and no control
        ACK.

        THE CAPTURE->OFFLOAD->PUBLISH TRIPLE, the same shape `novelty.py::
        _offload_under_proposal_sink` uses and for the same invariant. `captured` is bound BEFORE the
        `try` so a raise while installing the sink still leaves the `finally` something to read, the
        var is RESET before the publish so the publish itself reaches the real store, and the publish
        is in a `finally` because a `BudgetExceeded` out of a cadence (the Strategist and the
        deep-research step both spend) would otherwise discard rows that were durable at emit time
        before the offload existed — including the receipts whose absence would buy the same paid
        pass again on resume.

        `abandon_on_cancel=False` (the default) is REQUIRED, not preferred: an abandoned worker keeps
        the sink installed in its copied context and goes on buffering into the same list this task's
        `finally` is publishing. It also matches what the block already committed to — a paid
        provider call bounded by the endpoint timeout — and it is the property invariant #1 rests on
        here, since a cancel delivered mid-block could otherwise leave a cadence's gate spent with
        its receipt in a buffer nobody owns.
        """
        captured = _BufferedCadenceStore(self.store)
        token = _CADENCE_STORE_SINK.set(captured)
        try:
            return await anyio.to_thread.run_sync(fn, limiter=cadence_limiter())
        finally:
            _CADENCE_STORE_SINK.reset(token)
            # Contained: `store.append` raising HERE would REPLACE an exception already unwinding
            # (`shared.py::_append_progress_row` documents that shape), turning a clean
            # `BudgetExceeded` into a generic store error and losing the run its budget receipt.
            # Losing buffered rows to a store that cannot append is the lesser harm, said out loud.
            try:
                self._publish_cadence_events(captured.rows)
            except Exception:  # noqa: BLE001 - never mask the raise already in flight
                _LOG.warning("buffered cadence rows could not be published on the way out",
                             exc_info=True)
            else:
                # Only once EVERY gate the block buffered is durable: an effect registered after
                # its gate must never land when the gate did not (see `_after_durable`).
                for effect in captured.after_publish:
                    try:
                        effect()
                    except Exception:  # noqa: BLE001 - a best-effort store write after a durable gate; never mask the raise in flight
                        _LOG.warning("a deferred cadence side effect failed after its gate was "
                                     "published", exc_info=True)

    def _after_durable(self, effect) -> None:
        """Run `effect` once the events this code path appended BEFORE it are durable.

        Outside a cadence offload that is now: the store is the real log and the appends already
        landed. INSIDE one (`_offload_cadence`), every folded row is buffered until the main task
        publishes it — so a side effect written straight from the worker lands BEFORE the event that
        gates it. That silently reversed `lessons.py::LessonMemory.maybe_distill_lessons`' stated
        event-first ordering (review 2026-09-22, ENG3-05): the `lessons_distilled` gate sat in the
        buffer while the lessons were already in the SHARED store, so a process that died between
        the two left lessons with no gate, and the resume paid for the same distillation again and
        appended the same lessons a second time. Registered here, the write runs on the main task
        right after the publish — and not at all when the publish failed, which is the "the store
        misses one batch" outcome the event-first design chose on purpose.
        """
        sink = _CADENCE_STORE_SINK.get()
        if sink is None:
            effect()
        else:
            sink.after_publish.append(effect)

    def _publish_cadence_events(self, rows) -> int:
        """Append a buffered cadence prefix from the MAIN TASK. Returns how many landed.

        The rows land at the same point in the outer loop the cadence block always wrote at — the
        caller re-reads the tail immediately after and re-enters the turn when it moved — so no
        reader's position assumption changes. The trace/span ids are the ones that were current
        INSIDE the worker, so `looplab timings` and the trace view still attribute each row to the
        cadence that decided it rather than to the loop turn that published it.
        """
        landed = 0
        for event, trace_id, span_id in (rows or ()):
            self.store.append(event.type, event.data, trace_id=trace_id, span_id=span_id)
            landed += 1
        return landed

    def _activate_spec(self, proposal: dict) -> None:
        """Make the ratified onboarding proposal the trusted eval (Phase 3): the eval_spec
        drives `_run_eval`, and the metric adapter is written into every eval workdir as a
        task asset AND added to the protected set so the optimization agent can't edit it
        (freeze + surface-exclude)."""
        if not proposal:
            return
        self._eval_spec = proposal.get("eval_spec", {})
        adapters = proposal.get("adapter_files", {})
        self._assets = {**self._assets, **adapters}        # frozen: written into every wd
        protected = list(self._repo_spec.get("protected_names", []))
        protected += list(adapters)                        # agent may never overwrite them
        self._repo_spec = {**self._repo_spec, "protected_names": protected}
        self._spec_activated = True

    # --------------------------------------------------------- workspace seeding
    # (extracted to engine/workspace.py — see the delegator block after __init__)
    def _workspace_fingerprint(self) -> dict:
        return self.workspace.workspace_fingerprint()

    def _substrate_fingerprint(self) -> dict:
        return self.workspace.substrate_fingerprint()

    def _seed_workspace(self, workdir) -> None:
        return self.workspace.seed_workspace(workdir)

    def _seed_repo_tree(self, src, dst, ignore, mode: str = "auto") -> int:
        return self.workspace.seed_repo_tree(src, dst, ignore, mode)

    def _link_input(self, src, dst) -> None:
        return self.workspace.link_input(src, dst)

    # ------------------------------------------------------------- eval dispatch
    # `_agent_may` / `_ensure_run_setup` / `_do_run_setup` / `_data_binds` / `_run_eval` /
    # `_apply_sweep_best` live in looplab/engine/eval_dispatch.py (EvalDispatchMixin —
    # inherited, zero call-site churn).

    def _sandbox_cwd(self, workdir, cwd_spec) -> str:
        # extracted to engine/workspace.py — see the delegator block after __init__
        return self.workspace.sandbox_cwd(workdir, cwd_spec)

    # -------------------------------------------------------------- staged eval
    # `_resolve_stages` / `_eval_pipeline` / `_resolved_stages` / `_imported_modules` / `_module_file_candidates` /
    # `_stage_reachable_files` / `_safe_reuse_start` / `_stage_check_fn` live in
    # looplab/engine/eval_stages.py (EvalStagesMixin — inherited, zero call-site churn).

    # ---------------------- host grading / holdout (extracted to engine/holdout.py)
    # The host-grading + D1 holdout cluster lives in looplab/engine/holdout.py
    # (`HoldoutGrader`, constructed as `self.holdout` in __init__). These thin delegators keep
    # the ORIGINAL method names on the Engine — internal callers (_run_eval / run() / the
    # critic seam) use them, and HoldoutGrader routes its internal cross-calls back through
    # them, so an instance-level monkeypatch intercepts every path. The holdout-owned MUTABLE
    # state (`_holdout_idx`, `_holdout_fraction`, `_holdout_select`, `_holdout_top_k`)
    # deliberately stays on the Engine: __init__ and run()'s resume block assign it directly
    # (and tests read `eng._holdout_idx`), so plain attributes are lower churn than
    # lessons-style properties.
    def _graded_output_name(self) -> Optional[str]:
        return self.holdout.graded_output_name()

    def _apply_host_grade(self, res, workdir):
        return self.holdout.apply_host_grade(res, workdir)

    def _host_score_split(self, preds, g: dict, *, holdout: bool) -> Optional[float]:
        return self.holdout.host_score_split(preds, g, holdout=holdout)

    def _build_holdout_idx(self, fraction: float, epoch: int = 0) -> frozenset:
        return self.holdout.build_holdout_idx(fraction, epoch)

    def _apply_search_split(self, *, refuse: bool = True) -> None:
        return self.holdout.apply_search_split(refuse=refuse)

    def _holdout_topk(self, state: RunState) -> list[int]:
        return self.holdout.holdout_topk(state)

    def _holdout_pending(self, state: RunState) -> bool:
        return self.holdout.holdout_pending(state)

    async def _holdout_phase(self, state: RunState) -> None:
        return await self.holdout.holdout_phase(state)

    # ---------------------------------------------------------------- eval task
    # `_probe_developer` / `_evaluate` (materialize -> eval -> trust scans -> inline repair ->
    # ONE terminal event) live in looplab/engine/evaluate.py (EvaluateMixin — inherited, zero
    # call-site churn).

    # ------------------------------------------------------------------- confirm
    # `_already_confirmed` / `_run_confirm_seed` / `_confirm_phase` / `_confirm_node` live in
    # looplab/engine/confirm_phase.py (ConfirmPhaseMixin — inherited, zero call-site churn).

    # ------------------------------------------------------------------ ablation
    # `_ablate` / `_segment_blocks` / `_comment_block` / `_ablate_code` live in
    # looplab/engine/ablation.py (AblationMixin — inherited, zero call-site churn).

    # ------------------------------------------------------------- trust & audit
    # `_emit_agent_report` / `_emit_role_telemetry` / `_emit_hypothesis_ranked` /
    # `_emit_foresight_selected` / `_audit_workdir_writes` / `_redact` / `_maybe_crash` /
    # `_leakage_blocks` live in looplab/engine/audit.py (AuditMixin — inherited, zero
    # call-site churn).
