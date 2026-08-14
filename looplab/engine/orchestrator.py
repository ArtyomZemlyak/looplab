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
import hashlib
import logging
import math
import os
import secrets
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple, Optional

import anyio

from looplab.core.llm import BudgetExceeded
from looplab.tools.agents_md import generate_agents_md
from looplab.events.eventstore import EventStore, EventStoreConcurrencyError, retry_tail_cas
from looplab.events.types import (
    EV_ABLATE,
    EV_APPROVAL_REQUESTED,
    EV_COMMAND_ACK,
    EV_CARD_ADDED,
    EV_DATA_PROFILED, EV_DATA_PROVENANCE,
    EV_DRIFT_UNAVAILABLE, EV_FORK_DONE, EV_FORK_UNFULFILLED, EV_HOST_GRADING,
    EV_INJECT_DONE, EV_INJECT_FAILED,
    EV_FINALIZE_STEP,
    EV_LESSONS_STORE_UNAVAILABLE,
    EV_NODE_BUILDING, EV_NODE_CREATED,
    EV_NODE_FAILED, EV_PAUSE,
    EV_NOVELTY_REJECTED,
    EV_POLICY_DECISION,
    EV_REPORT_GENERATED,
    EV_RESUME_SERVED, EV_RUN_ABORT, EV_RUN_FINISHED,
    EV_RUN_STARTED, EV_RUNG_PROMOTED,
    EV_SETUP_FINISHED, EV_SETUP_STARTED, EV_SETUP_STEP, EV_SPEC_APPROVAL_REQUESTED,
    EV_SPEC_APPROVED, EV_SPEC_PROPOSED,
    EV_ENV_CHANGED, EV_WORKSPACE_CHANGED,
    PROGRESS_STAGE_BUILD)
from looplab.engine.ablation import AblationMixin
from looplab.engine.metric_salvage import settle_mode as settle_metric_salvage_mode
from looplab.runtime.metric_subject import settle_mode as settle_metric_subject_mode
from looplab.engine.widths import (EVAL_WIDTH_MAX, LLM_WIDTH_MAX, settle_width,
                                   settled_width_refusal)
from looplab.engine.audit import AuditMixin
from looplab.engine.cadence import occupancy_due
from looplab.engine.card_reservation import CardReservationMixin, _BuildReservation
from looplab.engine.speculation_gate import CalibrationRuntime, admit_speculation_lane
from looplab.engine.confirm_phase import ConfirmPhaseMixin
from looplab.engine.costs import bind_cost_accountants
from looplab.engine.crash_repair import CrashRepairMixin
from looplab.engine.eval_dispatch import EvalDispatchMixin
from looplab.engine.eval_stages import EvalStagesMixin
from looplab.engine.evaluate import EvaluateMixin
from looplab.engine.node_build import NodeBuildMixin, developer_crash_records
from looplab.engine.proposal_cues import ProposalCuesMixin, normalize_steering_context
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
from looplab.engine.research_cadence import ResearchCadenceMixin
from looplab.engine.finalize import (
    ensure_finish_report,
    finalize_run,
    finalize_scope_quiescent,
    incomplete_finalize_scope,
    mark_finish_report_complete,
    scoped_finish_report,
)
from looplab.events.finalize_protocol import FINALIZE_STEP_BEGUN
from looplab.engine.holdout import HoldoutGrader
from looplab.engine.lessons import LessonMemory
from looplab.engine.options import EngineOptions
from looplab.engine.workspace import WorkspaceSeeder
# Pure triage/fingerprint helpers extracted to looplab/engine/triage.py, imported back under
# their original names so `looplab.engine.orchestrator._rule_triage`, `._holdout_indices`
# (& friends) stay importable — tests import them from this module path. (`_normalize_error_sig`
# was re-exported here too until 2026-08-05; the error-signature guard it served was replaced by
# the triage model's own stop decision — see `engine/triage.py`'s module docstring.)
from looplab.engine.triage import (_MAX_DEP_ROUNDS, _MECHANICAL_MARKERS,  # noqa: F401
                                   _dir_fingerprint, _failure_reason, _holdout_indices,
                                   _rule_triage, _shallow_fingerprint)
from looplab.core.models import (
    Idea, Node, NodeStatus, RunState, durable_idea_payload, is_developer_error)
from looplab.core.config import RUN_START_PINNED_FIELDS, Settings
from looplab.core.errors import ConfigRefusal, EnvironmentRefusal, OperatorRefusal
from looplab.core.fitness import VERIFIER_SELECTION_CONTRACT
from looplab.core.setup_identity import setup_config_hash, setup_manifest_digest
from looplab.core.llm_broker import (LLMConcurrencyBroker, default_llm_lane_limits,
                                     in_llm_lane, llm_broker_scope, llm_lane_scope)
from looplab.search.card_selection import (
    META_CARD_ID, SpeculativeSelectionContext, card_budget_used, card_next_actions,
    refunded_node_reservations, speculative_card_actions, speculative_raw_actions,
)
from looplab.search.speculation_calibration import (
    SPECULATION_CALIBRATION_PROFILE_DIGEST,
    # Re-exported, not used here since doc 25 ES-01 moved the envelope to engine/speculation_gate.py:
    # the engine, the CLI and the tests all spell this on `engine.orchestrator`, and
    # tests/test_calibration_profile_home.py pins that the name did not move out from under them.
    SPECULATION_CALIBRATION_PROFILE_SETTINGS,  # noqa: F401
    SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS,
    SPECULATION_POLICY_SCOPE,
)
from looplab.search.operators import merge_idea
from looplab.search.policy import KIND_EXPAND, SearchPolicy
# The strategist-cadence cluster (StrategyContext / make_policy / validate_strategy / coverage_signal
# / run_phase / operator_yields / NOVELTY_STANCES …) moved to engine/strategy.py (StrategyCadenceMixin),
# which imports those symbols from their canonical sources — so they are no longer imported here.
from looplab.core.profile import profile_dataset
from looplab.events.replay import fold
from looplab.agents.roles import (Developer, Researcher, is_researcher_fallback,
                                  researcher_fallback_cause)
from looplab.runtime.sandbox import Sandbox
from looplab.core.tracing import (
    TRACE_EXPORT_FLUSH_TIMEOUT_MILLIS, AsyncJsonlSpanExporter, Tracer)

# Re-export (back-compat): the engine sentinel lives in engine/options.py since the F3 knob
# collapse (the signature takes **knobs now, so the orchestrator itself no longer needs it);
# kept importable from this module path for pre-collapse importers.
from looplab.engine.options import _UNSET  # noqa: F401

_LOG = logging.getLogger(__name__)

# P0-5 dirty-input diff digest: the byte ceiling on how much of `git diff HEAD` is hashed before the
# digest is marked truncated (`~`). A real code diff is far under this; beyond it we're diffing a
# tracked data/generated file, where buffering the whole patch would spike run-start memory (a latent
# OOM) and a truncated "did-it-change" signal is enough. Module-level so an operator/test can retune.
_DIFF_DIGEST_CAP = 8 * 1024 * 1024

# Back-compatible export: the source-owned definition lives beside the shared runtime-scope digest.
SPECULATION_CALIBRATION_VARIANT_FIELDS = SPECULATION_CALIBRATION_PROFILE_VARIANT_FIELDS
# The immutable calibration profile and its digest live in `search/speculation_calibration.py`, which
# exists to own exactly this source-scoped identity (doc 25 SE-07). They are re-exported here because
# the engine, the CLI and the tests all spell them on this module.


class RunStartPinError(OperatorRefusal, RuntimeError):
    """A re-entry contradicts a value this run's own ``run_started`` pinned (engine invariant #6).

    This is deliberately distinct from an ordinary fatal engine error.  CLI fatal-error recovery
    writes terminal events, while a refused re-entry must return without changing the log it refused
    to trust — so `cli/run_cmds.py::_run_engine_guarded` re-raises this family untouched.

    ``OperatorRefusal`` is the SECOND thing that distinctness has to buy, and it was missing: the
    re-raise kept the log clean and then handed the operator a 33-frame traceback whose last line
    was the carefully written remedy (`engine/widths.py::settled_width_refusal` names the file to
    edit and the two ways to change the width durably).  `RuntimeError` stays the base, so every
    existing `except RuntimeError` / `pytest.raises(RuntimeError)` is unaffected.
    """


class SpeculationAuthorizationError(RunStartPinError):
    """A durable speculation prefix cannot be re-entered under the current evidence authority."""


class SettledWidthPinError(RunStartPinError):
    """A resume explicitly spells a concurrency width other than the one ``run_started`` pinned."""


# Bounded aging for continuous eval dispatch (`_dispatch_evals`). After this many consecutive
# bypasses the queue head gets exclusive claim on GPU releases, so a wide request stops losing every
# partial release to the small jobs behind it. The claim ends the moment the head is admitted, or —
# see the scan — when the pool has fully drained and the head STILL does not fit, which proves it
# wants more than the box physically has and must not be allowed to wedge the batch.
_HEAD_BYPASS_LIMIT = 3




class _InjectedNodePlan(NamedTuple):
    """Pure, bounded preparation result for one operator-authored Node request."""

    idea: Idea
    parent_ids: list[int]
    parent_generations: dict[str, int]
    code: Optional[str]
    implementation_ref: Optional[str]


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


def _run_terminal_gate(state) -> bool:
    """Whether the RUN has stopped accepting new eval work (doc 25 ES-06).

    Written out at three eval-dispatch sites. `getattr` defaults are kept: two of the three read a
    state the caller re-folded mid-loop, and a folded `RunState` always carries these three, so the
    defaults cannot change a live decision — they only keep a hand-built test stub from raising.
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
# Poll geometry for a durable node-creating control head parked on an exhausted node budget
# (`Engine._defer_for_node_budget`). Starts at the historical tick and doubles up to the ceiling, so
# a multi-hour wait costs O(log(wait)) full-log refolds instead of two per second.
_BUDGET_WAIT_MIN_S = 0.5
_BUDGET_WAIT_MAX_S = 4.0


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
_NON_EVIDENCE_FAILURE_REASONS = frozenset({"superseded"})


def stamp_proposal_span(span, idea, *, node_id=None) -> None:
    """Bind a Researcher `propose` operation to the CARD it produced.

    THE LINK DID NOT EXIST. The product model is that the Researcher works per CARD (a hypothesis)
    while the Developer works per NODE, and one card can carry several nodes. Nothing recorded that:
    measured on `runs/rubert-dr-0807` (2026-08-11), all 15 `propose` spans carry an EMPTY attribute
    map, no span anywhere in the run carries a card id, and every `card_added` / `card_build_*` /
    `card_enriched` event has `trace_id: None`. So "show me the research behind this card" had no
    join to answer it — not through the spans, not through the events — and the only reason it was
    not noticed is that no surface had ever tried to ask.

    `_link` has resolved the writer-owned Card id onto the Idea by the time this runs, so the id is
    simply there to be written down.

    `idea` may be None, and the re-proposal path passes None ON PURPOSE. Its `node.idea.card_id` is
    the card this very path is about to DROP (`_drop_card_once(..., reason="reproposed")`, and it is
    handed to `_plan_native_card` as `superseded_card_id`), so stamping it would file the research
    that REPLACED a card as the research that produced it — the one mis-attribution a card trace must
    never make. The replacement is minted afterwards under `_id_lock`; the link survives through the
    `node_created` event that shares this span's trace.

    `proposed_for_node` is deliberately NOT spelled `node_id`. `node_id` is the attribution key
    `traceview.effective_node_id` projects the WHOLE trace by, so using it here would move every
    Researcher trace into one node's per-node view — and a card's research belongs to all of its
    nodes, not to whichever one happened to be prepared first. The node this proposal was prepared
    for is still worth recording; it is context, not ownership.
    """
    if span is None:
        return
    card_id = getattr(idea, "card_id", None) if idea is not None else None
    if isinstance(card_id, str) and card_id.strip():
        span.set("card_id", card_id.strip())
    if isinstance(node_id, int) and not isinstance(node_id, bool) and node_id >= 0:
        span.set("proposed_for_node", node_id)
    operator = getattr(idea, "operator", None) if idea is not None else None
    if isinstance(operator, str) and operator.strip():
        span.set("operator", operator.strip())


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


class Engine(ConfirmPhaseMixin, AblationMixin, NoveltyGateMixin, StrategyCadenceMixin,
             ConceptCadenceMixin, VerifierTiebreakMixin,
             ResearchCadenceMixin, EvalStagesMixin, CrashRepairMixin, EvalDispatchMixin,
             AuditMixin, ResourceSchedulingMixin, SpeculationMixin, EvaluateMixin, NodeBuildMixin,
             CardReservationMixin,
             ProposalCuesMixin,
             TrainingMonitorMixin, AshaMonitorMixin,
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
        _speculation_gate_calibration: bool = False,  # private mechanics-test/bootstrap seam
        _speculation_runtime_scope_sha256: Optional[str] = None,
        # Private CLI→Engine provenance seam. Narrow calibration/receipt paths independently
        # reconstruct this digest from their source-owned full Settings profile before trusting it.
        # BACKLOG §4 (docs/15 F3): every PURE-CONFIG knob — one per EngineOptions field — is
        # accepted via **knobs and validated against EngineOptions, so adding a knob is TWO edits
        # (Settings field + EngineOptions field) instead of four. Each knob's type/default/why
        # lives on EngineOptions (engine/options.py), which mirrors the old signature comments.
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

        def _opt(field: str):
            return knobs[field] if field in knobs else getattr(options, field)

        # Layer-2 decoupling (docs/23): the CANONICAL `eval_parallel`/`llm_parallel` win over the legacy
        # `max_parallel`/`parallel_build` when set; None => fall back to the legacy field => byte-identical.
        _eval_parallel_opt = _opt("eval_parallel")
        _eval_parallel_value = (_eval_parallel_opt if _eval_parallel_opt is not None
                                else _opt("max_parallel"))
        _llm_parallel_opt = _opt("llm_parallel")
        _llm_parallel_value = (_llm_parallel_opt if _llm_parallel_opt is not None
                               else _opt("parallel_build"))
        train_monitor = _opt("train_monitor")
        train_monitor_interval_s = _opt("train_monitor_interval_s")
        train_monitor_kill = _opt("train_monitor_kill")
        train_monitor_kill_confidence = _opt("train_monitor_kill_confidence")
        asha_live = _opt("asha_live")
        asha_live_kill = _opt("asha_live_kill")
        asha_live_quantile = _opt("asha_live_quantile")
        asha_live_min_siblings = _opt("asha_live_min_siblings")
        asha_live_kill_confidence = _opt("asha_live_kill_confidence")
        sweep_timeout_mult = _opt("sweep_timeout_mult")
        eval_stall_timeout_s = _opt("eval_stall_timeout_s")
        eval_deadline_grace_s = _opt("eval_deadline_grace_s")
        eval_env = _opt("eval_env")
        confirm_seed_base = _opt("confirm_seed_base")
        coverage_context = _opt("coverage_context")
        concept_pivot = _opt("concept_pivot")
        graded_novelty = _opt("graded_novelty")
        capability_expansion = _opt("capability_expansion")
        fingerprint_universal = _opt("fingerprint_universal")
        cross_run_concepts = _opt("cross_run_concepts")
        concept_run_base = _opt("concept_run_base")
        cross_run_advisory = _opt("cross_run_advisory")
        cross_run_structured_claims = _opt("cross_run_structured_claims")
        cross_run_curation = _opt("cross_run_curation")
        task_facets_finalize = _opt("task_facets_finalize")
        cross_run_curation_auto = _opt("cross_run_curation_auto")
        concept_tidy = _opt("concept_tidy")
        cross_run_read_tools = _opt("cross_run_read_tools")
        phase_handoff_summary = _opt("phase_handoff_summary")
        trust_mode = _opt("trust_mode")
        seed_mode = _opt("seed_mode")
        read_fence = _opt("read_fence")
        metric_subject = _opt("metric_subject")
        landlock = _opt("landlock")
        max_nodes = _opt("max_nodes")
        policy_name = _opt("policy_name")
        ablate_every = _opt("ablate_every")
        strategist_every = _opt("strategist_every")
        concept_retag_every = _opt("concept_retag_every")
        concurrent_research_repeat = _opt("concurrent_research_repeat")
        concurrent_research_interval_s = _opt("concurrent_research_interval_s")
        concurrent_research_max_calls = _opt("concurrent_research_max_calls")
        concurrent_consolidate = _opt("concurrent_consolidate")
        report_every = _opt("report_every")
        merge_mode = _opt("merge_mode")
        complexity_cue = _opt("complexity_cue")
        budget_aware = _opt("budget_aware")
        failure_reflection = _opt("failure_reflection")
        watchdog_reflection = _opt("watchdog_reflection")
        deep_repair = _opt("deep_repair")
        localize_faults = _opt("localize_faults")
        feature_engineering = _opt("feature_engineering")
        ablate_code_blocks = _opt("ablate_code_blocks")
        trust_gate = _opt("trust_gate")
        code_leakage_detect = _opt("code_leakage_detect")
        critic_check = _opt("critic_check")
        redact_output = _opt("redact_output")
        novelty_mode = _opt("novelty_mode")
        novelty_gate = _opt("novelty_gate")
        novelty_epsilon = _opt("novelty_epsilon")
        reflection_priors = _opt("reflection_priors")
        comparative_lessons = _opt("comparative_lessons")
        lessons_every = _opt("lessons_every")
        lessons_refresh_every = _opt("lessons_refresh_every")
        track_hypotheses = _opt("track_hypotheses")
        surrogate_explore = _opt("surrogate_explore")
        unified_agent = _opt("unified_agent")
        agent_drives_actions = _opt("agent_drives_actions")
        card_driven_selection = _opt("card_driven_selection")
        speculation_depth = _opt("speculation_depth")
        speculation_gate_receipt = _opt("speculation_gate_receipt")
        inline_repair = _opt("inline_repair")
        inline_repair_attempts = _opt("inline_repair_attempts")
        repair_critic_after = _opt("repair_critic_after")
        inline_repair_reasons = _opt("inline_repair_reasons")
        inline_repair_retrain_cap = _opt("inline_repair_retrain_cap")
        metric_salvage = _opt("metric_salvage")
        metric_salvage_repair = _opt("metric_salvage_repair")
        auto_install_deps = _opt("auto_install_deps")
        dep_install_timeout = _opt("dep_install_timeout")
        agent_control = _opt("agent_control")
        holdout_fraction = _opt("holdout_fraction")
        holdout_select = _opt("holdout_select")
        holdout_top_k = _opt("holdout_top_k")
        select_verifier = _opt("select_verifier")
        verifier_ci_tie = _opt("verifier_ci_tie")
        select_verifier_samples = _opt("select_verifier_samples")
        debug_depth = _opt("debug_depth")
        operator_bandit = _opt("operator_bandit")
        novelty_semantic = _opt("novelty_semantic")
        novelty_semantic_threshold = _opt("novelty_semantic_threshold")
        digest_char_cap = _opt("digest_char_cap")
        research_verify = _opt("research_verify")
        workdir_audit = _opt("workdir_audit")
        trace_llm_io = _opt("trace_llm_io")

        self.run_dir = Path(run_dir)
        self.task = task
        self.researcher = researcher
        # P1: propagate the hypothesis-tracking knob to the researcher (LLMResearcher reads it;
        # UnifiedAgent forwards it to its inner researcher). Default-on already via the constructor;
        # this makes an explicit OFF reach the prompt. Best-effort (toy researchers ignore it).
        try:
            setattr(self.researcher, "track_hypotheses", track_hypotheses)
        except Exception:  # noqa: BLE001
            pass
        self.developer = developer
        self.sandbox = sandbox
        self.policy = policy
        # A7 Strategist: the policy is now hot-swappable, so the engine keeps the knobs needed to
        # rebuild it (n_seeds/max_nodes/ablate_every) + the meta-controller + operator-mix state.
        self.n_seeds = _opt("n_seeds")
        self.max_nodes = max_nodes
        # The policy's OWN node budget is the base a live add_nodes override extends — NOT self.max_nodes
        # (the engine default can differ from a passed-in policy's, e.g. in tests). Tracked separately so
        # the override is applied idempotently (absolute set per iteration) without compounding, and
        # re-captured on a strategy-driven policy swap below.
        self._base_max_nodes = getattr(policy, "max_nodes", max_nodes)
        self._policy_name = policy_name
        self._ablate_every = ablate_every
        self.strategist = strategist
        # In-process memo for `_maybe_consult_strategist`: the operator pin (plus the two live inputs
        # its whitelist consults) that last validated down to NO surviving fields. An invalid pin
        # "drifts" forever, and without this the strategy path rebuilt the whole StrategyContext on
        # every loop pass to re-derive the same no-op. Nothing durable keys off it — see there.
        self._invalid_pin_verdict: Optional[tuple] = None
        self.strategist_every = max(1, strategist_every)
        self.concept_retag_every = max(1, concept_retag_every)
        # STORED RAW: 0 is OFF here (every other interval knob reads 0 that way too), so a clamp
        # would turn "never stop the run for me" into "stop after one failure".
        self.systemic_failure_stop = _opt("systemic_failure_stop")
        self.deep_researcher = deep_researcher
        # STORED RAW, deliberately — this was `max(0, deep_research_every)` until 2026-08-07, and
        # under the new spelling that clamp is exactly backwards: `0` now means "start immediately"
        # and OFF is NEGATIVE, so it would have converted every spelled-off knob into a paid think at
        # every node. The whole settling rule is stated once, in
        # `engine/cadence.py::deep_research_window`, and applied at the two gates that read this
        # attribute — so `-1` (off), a junk value (off) and `0` (immediate) all mean here exactly
        # what the operator wrote, and the diagnostics that echo the knob do not lie about it.
        # (Hence `_opt` inline: with no transform left, the local it used to be resolved into buys
        # nothing — `tests/test_source_scan_helper.py` is the guard that says so.)
        self.deep_research_every = _opt("deep_research_every")
        self.concurrent_research = _opt("concurrent_research")
        # Repeated concurrent research (don't idle a multi-day eval): the overlapped think re-runs on
        # an adaptive time cadence for the whole window instead of once. Off in the library default
        # (one-shot == today); the product turns it on. Interval floors the budget-derived pace;
        # max_calls is a per-window LLM backstop. See _spawn_research / _research_overlap_loop.
        self._concurrent_research_repeat = bool(concurrent_research_repeat)
        self._concurrent_research_interval_s = max(1.0, float(concurrent_research_interval_s or 1800.0))
        self._concurrent_research_max_calls = max(0, int(concurrent_research_max_calls or 0))
        # Overlap the hypothesis-board consolidation with the eval too (dedup the board the repeated
        # research keeps filling). Off in the library default (== today); product turns it on.
        self._concurrent_consolidate = bool(concurrent_consolidate)
        self.report_writer = report_writer
        self.report_every = max(0, report_every)
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
        self._complexity_cue = complexity_cue
        self._prefer_sweep = False   # A7: Strategist-set bias toward intra-node sweeps (audit-driven)
        self._budget_aware = budget_aware
        self._failure_reflection = failure_reflection
        self._watchdog_reflection = watchdog_reflection
        self._deep_repair = deep_repair
        # Hybrid in-node crash repair (triage + inline repair). See Settings.inline_repair.
        self._inline_repair = inline_repair
        self._inline_repair_attempts = max(0, int(inline_repair_attempts))   # 0 = no operator cap
        # F8: how many durable repairs before the CRITIC is asked whether the chain is
        # circling. It is a cadence, not a bound — the critic can only stop, never extend.
        self._repair_critic_after = max(0, int(repair_critic_after))
        self._inline_repair_reasons = tuple(inline_repair_reasons or ("crash",))
        self._inline_repair_retrain_cap = max(0, int(inline_repair_retrain_cap))
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
        self._auto_install_deps = bool(auto_install_deps) and trust_mode == "trusted_local"
        self._dep_install_timeout = float(dep_install_timeout)
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
        # Agent governance (Settings.agent_control): per-setting allow-list of which roles may change it
        # at runtime. A setting absent from the map is LOCKED (no agent). Enforced at the strategist /
        # boss / researcher seams via `_agent_may`. `None` (a bare Engine(...) with no options) resolves
        # to the SHIPPED default matrix — so a directly-constructed engine behaves like a real CLI run
        # (the EngineOptions "Engine() == shipped defaults" invariant); pass an explicit `{}` to lock
        # every knob against the agents.
        from looplab.core.config import default_agent_control
        self._agent_control: dict = (dict(agent_control) if agent_control is not None
                                     else default_agent_control())
        self._localize_faults = localize_faults
        self._feature_engineering = feature_engineering
        self._ablate_code_blocks = ablate_code_blocks
        self.proxy_scorer = proxy_scorer
        self.proxy_kill_fraction = _opt("proxy_kill_fraction")
        self.reward_hack_detect = _opt("reward_hack_detect")
        if trust_gate not in ("audit", "gate", "block"):
            # A security control must fail LOUDLY: silently coercing a typo ("Gate") to "audit"
            # would run with no enforcement while the caller believes the gate is on.
            raise ConfigRefusal(
                f"trust_gate must be 'audit', 'gate' or 'block', got {trust_gate!r}")
        self.trust_gate = trust_gate
        self._code_leakage_detect = code_leakage_detect
        self._critic_check = critic_check
        self._redact_output = redact_output
        # novelty_mode is the primary selector; a legacy novelty_gate=True forces the "algo" path.
        self._novelty_mode = str(novelty_mode or "llm") if not novelty_gate else "algo"
        self._novelty_gate = novelty_gate
        self._novelty_epsilon = novelty_epsilon
        # T5 semantic novelty (Phase 2): reject a proposal whose idea TEXT is a near-duplicate of
        # an existing node's — with one informed re-propose when the duplicate FAILED (the
        # ShinkaEvolve lever: novelty rejection before evaluation, ablation-ranked above model
        # routing). hash_embed is the zero-dep default; T4 wires a real embedder from config.
        self._novelty_semantic = bool(novelty_semantic)
        self._novelty_semantic_threshold = float(novelty_semantic_threshold)
        if embedder is None:
            from looplab.tools.vectorstore import hash_embed as _he
            embedder = _he
        self._embedder = embedder
        self._idea_vecs: dict[tuple, list] = {}  # (len, prefix) of idea text -> embedding (in-memory)
        self._debug_depth = max(1, int(debug_depth))
        self._operator_bandit = bool(operator_bandit)
        # M5: the Researcher's always-on digest budget (0 = auto-scale with run size).
        try:
            setattr(researcher, "_digest_cap", int(digest_char_cap))
        except Exception:  # noqa: BLE001 — toy researchers without attrs are fine
            pass
        self._research_verify = bool(research_verify)
        self._workdir_audit = bool(workdir_audit)
        # ADR-17 capture policy for THIS run's tracer (below). None = declare nothing and let the
        # process-wide `set_llm_capture` default decide, exactly as before this knob existed.
        self._trace_llm_io = None if trace_llm_io is None else bool(trace_llm_io)
        self._coverage_context = bool(coverage_context)
        self._concept_pivot = bool(concept_pivot)
        self._graded_novelty = bool(graded_novelty)
        self._capability_expansion = bool(capability_expansion)
        self._fingerprint_universal = bool(fingerprint_universal)
        self._cross_run_concepts = bool(cross_run_concepts)
        self._concept_run_base = bool(concept_run_base)
        self._cross_run_advisory = bool(cross_run_advisory)
        self._cross_run_structured_claims = bool(cross_run_structured_claims)
        self._cross_run_curation = bool(cross_run_curation)
        self._task_facets_finalize = bool(task_facets_finalize)
        self._cross_run_curation_auto = bool(cross_run_curation_auto)
        self._concept_tidy = bool(concept_tidy)
        self._cross_run_read_tools = bool(cross_run_read_tools)
        self._phase_handoff_summary = bool(phase_handoff_summary)
        # Novelty stance (Strategist-owned dial): how hard the proposer / foresight ranker / novelty
        # gate push for NEW directions. "balanced" == today's behavior; the Strategist raises it to
        # "explore" when coverage shows narrowing, or "exploit" to converge. Set by _apply_strategy.
        self._novelty_stance = "balanced"
        # Memora synergy: the SAME abstractor Memora uses for the case/KB index, applied to the
        # cross-run LESSONS tier so lesson retrieval gains anchor-expansion (harmonic recall)
        # instead of fingerprint-Jaccard alone. None (memora off) => the legacy Jaccard-only path.
        self._lesson_abstractor = lesson_abstractor
        self._exploit_suite = None   # 4.3 hardened ruleset; loaded once memory_dir is set (below)
        self._reflection_priors = reflection_priors
        # M6 comparative lessons: credit-assigned pair distillation (run-end and, when the
        # cadences are set, mid-run into/from the SHARED cross-run store — the live-share seam).
        self._comparative_lessons_on = comparative_lessons
        self.lessons_every = max(0, lessons_every)
        self.lessons_refresh_every = max(0, lessons_refresh_every)
        # Cross-run memory / lessons / reflection cluster (looplab/engine/lessons.py). The Engine
        # keeps thin delegators under the original `_`-names below (tests call/monkeypatch them);
        # the lessons-owned mutable state (seen stamp, prior note) lives on LessonMemory.
        self.lessons = LessonMemory(self)
        self._track_hypotheses = track_hypotheses
        self._surrogate_explore = surrogate_explore
        # Unified self-driving agent: in unified mode `researcher is developer` (one object plays
        # both roles); `agent_drives_actions` additionally lets it pick the next macro action.
        self.unified_agent = unified_agent
        self.agent_drives_actions = unified_agent and agent_drives_actions
        # The Card authority wins when both opt-in selectors are enabled. Letting the
        # free-form agent arm pre-empt it would silently bypass the atomic existing-work claim below.
        self.card_driven_selection = bool(card_driven_selection)
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
        # AUTO (-1) follows the same settled width; every other value is used as spelled. Resolving
        # BEFORE the local is read again keeps one settled integer flowing into the envelope checks,
        # the runtime-scope digest and the run_started pin — a hardware-derived depth must never reach
        # the durable log, or replay on another box would rebuild a different search treatment.
        speculation_depth, self._speculation_depth_auto = self._resolve_speculation_depth(
            speculation_depth)
        # Keep a settled, bounded scalar for the Layer-5 producer/consumer seam. Zero is a hard
        # off-switch; no task group/request event is allowed to infer a non-zero depth from hardware.
        self.speculation_depth = max(0, min(64, int(speculation_depth or 0)))
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
            card_driven_selection=card_driven_selection,
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
            _startup_llm_total = (min(64, int(_llm_parallel_opt))
                                  if _llm_parallel_opt is not None
                                  and int(_llm_parallel_opt) > 0 else None)
        except (TypeError, ValueError, OverflowError):
            _startup_llm_total = None
        self._llm_broker = LLMConcurrencyBroker(
            total=_startup_llm_total,
            lane_limits=default_llm_lane_limits(_startup_llm_total),
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
        self.timeout = _opt("timeout")
        self.max_eval_timeout = _opt("max_eval_timeout")
        # Eval stall watchdog cap (seconds); 0 disables. Threaded into command_eval and surfaced to the
        # Developer so its code can emit periodic progress to avoid a false silence-kill.
        self.eval_stall_timeout_s = float(eval_stall_timeout_s)
        # Most extra wall clock a live-log judge may buy for a stage at its deadline, ONCE per
        # command. 0 (default) = the historical unconditional tree-kill. See
        # `Settings.eval_deadline_grace_s` for the 22.0 discarded GPU-hours and for why it is opt-in.
        self.eval_deadline_grace_s = float(eval_deadline_grace_s)
        # F1d RUN-LEVEL DECLARED ENVIRONMENT. Copied, never aliased: `EngineOptions` is frozen but
        # its dict is not, and `_repin_declared_env` REPLACES this on a resume with what
        # `run_started` recorded (invariant #6) — mutating the caller's Settings dict from here
        # would rewrite the launch config object a UI process may still be serving.
        self._eval_env: dict = dict(eval_env or {})
        self._train_monitor = bool(train_monitor)
        self._train_monitor_interval_s = train_monitor_interval_s
        self._train_monitor_kill = bool(train_monitor_kill)
        self._train_monitor_kill_confidence = train_monitor_kill_confidence
        # ASHA live-curve rank watchdog (advisory in the product surface; opt-in kill). off == today.
        self._asha_live = bool(asha_live)
        self._asha_live_kill = bool(asha_live_kill)
        self._asha_live_quantile = float(asha_live_quantile)
        self._asha_live_min_siblings = max(1, int(asha_live_min_siblings))
        # Minimum confidence the LLM stop-verdict needs before the rank flag may actually kill. The judge
        # is consulted only INSIDE the rank gate, so this can only ever narrow the stop set.
        self._asha_live_kill_confidence = asha_live_kill_confidence
        self.sweep_timeout_mult = max(1.0, sweep_timeout_mult)
        self.crash_after = crash_after
        self.confirm_top_k = _opt("confirm_top_k")
        self.confirm_seeds = _opt("confirm_seeds")
        self.max_seconds = _opt("max_seconds")
        self.max_eval_seconds = _opt("max_eval_seconds")
        self.memory_dir = _opt("memory_dir")
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
        self.require_approval = _opt("require_approval")
        self.archive_resolution = _opt("archive_resolution")
        # RepoTask onboarding (Phase 3): `onboarder()` -> a proposed {eval_spec,
        # adapter_files, goal}; ratified per `eval_trust_mode` then frozen+trusted.
        self.onboarder = onboarder
        self.eval_trust_mode = _opt("eval_trust_mode")
        # Sandbox tier for the command-eval path (ADR-13, Phase 4): "untrusted" wraps each
        # eval in `docker run --network none` (real isolation for an arbitrary framework);
        # "trusted_local" runs it directly. The solution.py path uses self.sandbox instead.
        self.trust_mode = trust_mode
        self.docker_image = _opt("docker_image")
        # Resource caps for the untrusted/hostile command-eval Docker tier (make_docker_wrap).
        # Mirror the solution.py DockerSandbox tier so both untrusted tiers bound memory/cpu.
        self.sandbox_memory = _opt("sandbox_memory")
        self.sandbox_cpus = _opt("sandbox_cpus")
        self._seed_mode = seed_mode or "auto"   # run-wide fallback for per-editable seeding
        # Source-tree READ FENCE policy (off|warn|deny) — read by `engine/resources.py`, which
        # materializes the fence lazily on the first eval and stamps its marker into the child env.
        # It is the counterpart to `_seed_mode`: seeding decides what a node's copy CONTAINS, this
        # decides that the copy is the only place the node may read from.
        self._read_fence = read_fence or "deny"
        # METRIC SUBJECT rung (off|audit|require) — read by `engine/eval_dispatch.py` (which hands
        # `run_command_eval` the declared subject), by `engine/eval_stages.py` (which derives the
        # protected score stage's `needs` from it) and by `engine/evaluate.py` (which folds the
        # record onto the terminal and, under `require`, mints the violation). Settled through the
        # module's own vocabulary so an unknown rung from another binary's snapshot degrades to the
        # conservative one rather than silently to the strictest.
        self.metric_subject = settle_metric_subject_mode(metric_subject)
        # Kernel read ALLOW-LIST (off|enforce). Read by `engine/resources.py`, which derives the
        # allow-list from the operator's declared mounts and stamps it into the child env; the
        # boundary itself is applied in the child, between fork and exec.
        self._landlock = str(landlock or "off")
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
        if trust_mode in ("untrusted", "hostile"):
            import shutil as _sh
            if not _sh.which("docker"):
                raise EnvironmentRefusal(
                    f"trust_mode={trust_mode!r} needs the docker CLI to sandbox evals, but it was "
                    "not found on PATH. Install Docker or use trust_mode='trusted_local'.")
        self._spec_activated = False
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.store = EventStore(self.run_dir / "events.jsonl")
        # Bind after EventStore exists and before any role can make an LLM call. Paid usage now
        # survives process restarts in the same append-only source of truth as the run itself.
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
        self.confirm_seed_base = max(0, int(confirm_seed_base))
        self._holdout_select = bool(holdout_select)
        self._holdout_top_k = max(1, int(holdout_top_k))
        self._select_verifier = bool(select_verifier)
        self._verifier_ci_tie = bool(verifier_ci_tie)
        self._select_verifier_samples = max(1, int(select_verifier_samples))
        # The FRACTION defines the split every search metric is scored against, so it must be pinned
        # in the event log (like trust_gate / holdout_select) — on resume the recorded value is
        # re-used (see run()), so a changed live setting can't silently make pre/post-resume metrics
        # incomparable. `_build_holdout_idx` rebuilds the partition from a fraction.
        self._holdout_fraction = float(holdout_fraction)
        self._holdout_idx: frozenset = self._build_holdout_idx(self._holdout_fraction)
        self._holdout_epoch = 0
        # RepoTask (ADR-7): an existing repo the agent edits + a command-based eval.
        rs = getattr(task, "repo_spec", None)
        self._repo_spec: dict = rs() if callable(rs) else {}
        es = getattr(task, "eval_spec", None)
        self._eval_spec: dict = es() if callable(es) else {}
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
            broker = self._llm_broker = LLMConcurrencyBroker()
        try:
            with llm_broker_scope(broker), llm_lane_scope("engine"):
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
                # this same event loop (never threads), and every one of the eight node-terminal
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
                        try:
                            return await self._run_with_llm_broker()
                        finally:
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
            # Engine.run owns exactly one exporter lifetime. Always make its final barrier terminal:
            # a background span that closes after return must be rejected rather than append behind
            # reset/clear. Shutdown drains accepted work and, on its bounded timeout, atomically
            # abandons anything that has not crossed the lifecycle writer fence. Python still cannot
            # interrupt an in-progress filesystem call; a crossed writer keeps the fence until done.
            _trace_shutdown = getattr(getattr(self, "tracer", None), "shutdown", None)
            if callable(_trace_shutdown):
                try:
                    _stopped = bool(_trace_shutdown(
                        timeout_millis=TRACE_EXPORT_FLUSH_TIMEOUT_MILLIS))
                except Exception:  # noqa: BLE001 - never mask cancellation/domain failure in finally
                    _stopped = False
                if not _stopped:
                    _LOG.warning(
                        "trace exporter did not stop before lifecycle release; pending rows were "
                        "abandoned behind the trace-writer fence")

    def _enter_run(self) -> bool:
        """Authorize re-entry, recover, ACK and set up: everything before the first loop turn.

        An EXACT cut out of `_run_with_llm_broker` (doc 25 XP-06).  Measured rather than assumed:
        nothing after this block reads either of its two locals — `events` is dead after the setup
        gate and `state` is re-folded at the top of every loop turn — so its entire output is the
        one `entry_finished` flag finalization needs.

        It stays in THIS module because it folds twice.  `fold` here is the module-global
        monkeypatch seam (`tests/test_creation_runaway_guard.py` and friends replace it), and a
        method that reached it from another engine file would bind a different object; see
        `engine/card_reservation.py::_fold` for the deferred-attribute pattern that costs.
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
            # A scoped terminal intent is itself a work gate. Finalize/recover that exact scope
            # below; never reopen setup/search while a paid-report or terminal append is in flight.
            pending_scope = incomplete_finalize_scope(decision_events)
            self._pending_finalize_scope = pending_scope
            if pending_scope is not None:
                break
            # `/resume` records a durable request even when this process is already alive. A live
            # loop acknowledges it only when it can actually re-enter work; terminal/HITL/pause gates
            # leave it pending so the post-exit waiter (or on-load reconciler) spawns a fresh CLI,
            # whose normal resume path lifts the appropriate gate.
            if state.resume_pending() and not state.finished and not state.paused:
                self.store.append(EV_RESUME_SERVED, {})
                continue
            # Terminal/operator gates precede ALL work, including reset rebuilds. An explicit pause
            # must freeze a queued rerun; a scoped developer-crash pause must stop a stale reset batch.
            # A prior invocation guard may have appended run_finished(error) after a durable abort.
            # That is a retryable failed wrap-up, not the abort's terminal result; republish the
            # stable abort scope and let scoped finalization deduplicate every completed side effect.
            if (state.finished and state.stop_requested
                    and str(state.stop_reason or "").lower() == "error"):
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
                self._rerun_node(_resets[0], state)
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
                if self._head_request(speculative_state) is not None or speculative_state.buildings:
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
            state = self._run_cadences(state)
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

            actions = self._select_actions(state)
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

        # Every `break` above can leave adopted evaluations running (the eval task group is owned by
        # `Engine.run`, not by this loop), and finalization reads the FOLD: champion, budget summary,
        # diversity archive, case store. Draining here — not at the task group's join, which happens
        # after `finalize_run` has already returned — is what keeps that read complete.
        await self._drain_adopted_evals()
        # Finalize (extracted to looplab/engine/finalize.py, a pure move): budget summary,
        # diversity archive, LLM cost roll-up, case store + reflection note, read-model,
        # trace.json + tree.html. Event emission order is preserved exactly.
        return finalize_run(self, entry_finished=entry_finished, start_time=start)

    def _settle_terminal_gate(self, state, reason: str, *, decision_seq: int,
                              max_es: Optional[float] = None,
                              drain_forced_request: bool = False) -> str:
        """One terminal gate: settle what is in flight, then finish only if the log is quiescent.

        Four gates in the run loop spelled this ladder out (leakage, aborted, time_budget,
        eval_budget) and the ORDER is the whole rule — a Card build or a forced Node creator still
        in flight must be settled BEFORE finalization can win, or the run finishes with its own
        durable request head unacknowledged. Returns the outer loop's signal: "break" once the run
        is durably finished, "continue" to re-enter every gate on a fresh fold. An in-flight head
        also yields "continue", because the settle attempt churns the tail either way and its return
        value means "a head existed", not "this CAS succeeded".

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

    async def _handle_no_actions(self, state, *, decision_seq) -> str:
        """The empty-action ladder: confirm -> holdout -> HITL approval -> finish (doc 25 XP-06).

        Lifted verbatim out of the run loop's `if not actions:` branch, which — like the ES-05
        `creates` branch before it — always continued or broke and never fell through. Its six
        outer-loop `break`/`continue` statements cannot cross a function boundary, so they return a
        signal instead; the caller acts on it unconditionally.

        It stays in THIS module because it folds: `fold` is the module-global monkeypatch seam.
        """
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
        `_proposal_authority_seq` and discard paid proposals; a DIAGNOSTIC row is excluded from that
        fence today, but it would still be an append per poll turn for the whole of a multi-hour
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
        width = max(1, int(getattr(self, "_eval_parallel", 0) or 1))
        if not occupancy_due(inflight=len(running), queued=len(queued), width=width):
            return []
        if any(action.get("node_id") not in running for action in evals):
            return []                     # a slot could be filled from the board; do that instead
        if state.buildings or self._head_request(state) is not None:
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
                    if self._stage_card_creates(lane, state):
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
                    if self._request_card_build():
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
        _pb_pairs = (self._build_role_pairs(min(self._llm_parallel, len(creates)))
                     if (self._llm_parallel > 1 and len(creates) > 1
                         and all(a.get("kind") == "draft" for a in creates)
                         and not any(META_CARD_ID in a for a in creates)) else None)
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
                # Per-idea FOREAGENT telemetry snapshots captured by _propose_batch (aligned
                # 1:1 with _ideas), so each build emits ITS OWN
                # hypothesis_ranked/foresight_selected.
                _ideas, _telem, _dropped_batch = self._consume_batch_proposal(
                    state, len(_chunk))
                if not _ideas:
                    self._record_dropped_batch_cards(_dropped_batch)
                    self._pending_batch_dropped = []
                    self._pending_batch_novelty_gated = []
                    continue
                # Third and last lane that reserves a node from a proposal without crossing
                # `_prepare_node_idea`'s `_link` funnel. A dead provider hands the shared batch
                # researcher N degraded FALLBACKS at once, which is how the same non-proposal used to
                # become several byte-identical nodes in one chunk. MAIN TASK — this is the loop task,
                # before any `start_soon`.
                if any(self._refuse_degraded_proposal(_idea, main_task=True) for _idea in _ideas):
                    self._record_dropped_batch_cards(_dropped_batch)
                    self._pending_batch_dropped = []
                    self._pending_batch_novelty_gated = []
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
                _reserved = [
                    # `retry_attach` stays off (default): these Ideas came from the shared batch
                    # proposal and never crossed `_prepare_node_idea._link`, so no earlier pass
                    # planned an attach for this pass to agree with.
                    self._reserve_node_build(
                        _a, _idea, scored_against=state.best_node_id,
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
                self._pending_batch_dropped = []
                # The accepted Ideas are now durably reserved, so the unreserved compatibility
                # capability is no longer reachable or needed.
                self._pending_batch_novelty_gated = []
                # Cost guardrail (Phase 4): surface the concurrent build fan-out width in the
                # trace (spans.jsonl / OTel). `built` is structurally bounded by `fan` (=len of
                # the role pool) which is bounded by `parallel_build`, so a batch can never exceed
                # the configured fan-out — this span makes the actual per-batch cost observable.
                # CODEX AGENT: this join is a bulk-synchronous build barrier, not independent
                # adaptive research threads. Fast workers cannot select/propose from completed
                # sibling evidence until the slowest build and later eval batch finish; feed each
                # completion back to a central scheduler and refill the freed lane immediately.
                with self.tracer.span("parallel_build_batch", fan=_fan, built=len(_chunk),
                                      parallel_build=self._llm_parallel):
                    async with anyio.create_task_group() as _tg:
                        for _a, _res, _pair, _idea, _tel in zip(
                                _chunk, _reserved, _pb_pairs, _ideas, _telem):
                            if _res is None:
                                continue
                            # _create_node_guarded: an UNEXPECTED exception in one build becomes a
                            # node_failed terminal for its already-reserved id (node_building was
                            # appended up front) instead of tearing down the task group and killing
                            # the whole run — the rest of the concurrent batch still finishes.
                            _tg.start_soon(anyio.to_thread.run_sync,
                                           functools.partial(self._create_node_guarded,
                                                          _a, _pair, _res, _idea, _tel))
                # Circuit breaker under concurrency: `start_soon` does not yield, so no worker runs
                # until the task group JOINS above — the pause flag can only be observed HERE, after
                # the whole chunk finishes. So a developer/build crash pauses after AT MOST this one
                # chunk (bounded by the fan-out width), not mid-chunk; stop before the next chunk.
                if self._create_paused:
                    self._drain_create_pause()
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
                    self._create_node(a, reserved=reservation)
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
                self._create_node(a)  # sequential -> deterministic ids/proposals
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
        for request_index in range(done, len(state.card_build_requests)):
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

    def _unmaterialized_card_reservations(self, state: RunState) -> int:
        """Count durable requests not yet represented by a distinct physical Node reservation."""

        return len(self._unmaterialized_card_request_indices(state))

    def _node_reservation_slots_remaining(
        self,
        state: RunState,
        *,
        events=None,
        consume_request: bool = False,
    ) -> int:
        """Return strict remaining physical slots at every new-Node append boundary.

        ``consume_request`` is used only while converting the exact outstanding speculative head into
        ``node_building``; that request already owns one slot and must not be charged twice.
        """

        if events is None:
            events = self.store.read_all()
        raw_used = self._node_id_ceiling(events, state)
        unmaterialized = self._unmaterialized_card_request_indices(state)
        request_used = len(unmaterialized)
        head_index = max(
            0, min(int(state.card_builds_done), len(state.card_build_requests)),
        )
        if consume_request and head_index in unmaterialized:
            request_used -= 1
        return max(0, self._hard_node_reservation_limit(state) - raw_used - request_used)

    def _refresh_speculation_budget(self, state: RunState, *, events=None) -> None:
        """Refresh the live policy denominator without refunding the hard Node admission ceiling.

        Card selection ranks an effective view that excludes tombstoned and currently gated Nodes. The
        configured ``max_nodes + add_nodes`` limit, however, bounds physical Node reservations. Translate
        its remaining raw slots into the effective denominator so policy intent keeps the filtered view
        while every slot already reserved — including a failed reservation gap — remains spent. This
        overrides the SpeculationMixin helper so serial and speculative Card admission share one limit.
        """
        hard_limit = self._hard_node_reservation_limit(state)
        if events is None:
            events = self.store.read_all()
        raw_used = self._node_id_ceiling(events, state)
        request_used = self._unmaterialized_card_reservations(state)
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

    def _select_actions(self, state: RunState) -> list[dict]:
        """Apply the explicit macro-selection authority order for one fresh fold."""
        # Receipt-backed Card selection is the narrowest authority and therefore wins when both opt-in
        # selectors are enabled. The default false flag takes the exact historical branches below.
        if self.card_driven_selection:
            return card_next_actions(
                state, self.policy, self.policy.max_nodes,
                scoring=getattr(self, "_card_scoring", None),
            )
        if self.agent_drives_actions:
            return self._agent_next_actions(state)
        return self.policy.next_actions(state)

    def _run_start_pinned_values(self) -> dict:
        """The config values whose run-start record, not a later snapshot, owns re-entry semantics."""
        values = {
            "holdout_fraction": self._holdout_fraction,
            "holdout_select": self._holdout_select,
            "select_verifier": self._select_verifier,
            "select_verifier_samples": self._select_verifier_samples,
            "verifier_ci_tie": self._verifier_ci_tie,
        }
        legacy_fields = RUN_START_PINNED_FIELDS - {"card_driven_selection", "speculation_depth"}
        if values.keys() != legacy_fields:
            raise RuntimeError("run-start pinned settings contract drifted")
        # Keep the default run_started payload byte-identical. Replay treats an absent key as false;
        # only the opt-in path needs an additive durable marker.
        if self.card_driven_selection:
            values["card_driven_selection"] = True
        if self._speculation_implementation_digest:
            values["speculation_implementation_digest"] = (
                self._speculation_implementation_digest
            )
        if self._speculation_runtime_scope_sha256:
            values["speculation_runtime_scope_sha256"] = (
                self._speculation_runtime_scope_sha256
            )
        # Preserve the default run_started bytes just like the Card selector flag. Replay supplies
        # zero for an absent key, while an enabled overlap treatment must be durable across resume.
        if ((self.card_driven_selection and self.speculation_depth)
                or self._speculation_gate_calibration):
            values["speculation_depth"] = self.speculation_depth
        if self._speculation_gate_calibration:
            if (
                not self._speculation_gate_admitted
                or not self._speculation_implementation_digest
                or not self._speculation_runtime_scope_sha256
                or self._speculation_calibration_profile_digest
                != SPECULATION_CALIBRATION_PROFILE_DIGEST
                or not self._speculation_calibration_gpu_inventory
                or type(self._speculation_calibration_seed) is not int
                or self._speculation_policy_scope != SPECULATION_POLICY_SCOPE
            ):
                raise RuntimeError("calibration reached run start outside its exact profile envelope")
            values.update({
                "speculation_calibration_profile_digest": (
                    self._speculation_calibration_profile_digest),
                "speculation_calibration_gpu_inventory": list(
                    self._speculation_calibration_gpu_inventory),
                "speculation_calibration_seed": self._speculation_calibration_seed,
                "speculation_policy_scope": self._speculation_policy_scope,
            })
        elif self.card_driven_selection and self.speculation_depth:
            # Neither a runtime-scope pin nor an implementation digest is required here: both are
            # EVIDENCE identities for the calibrated lane, which measured one exact
            # Settings/policy/sandbox envelope under one exact source tree. The product lane measured
            # no such envelope and claims no evidence, so minting either would be a pin that means
            # nothing (and, for the source digest, one that any later edit would revoke). The lane
            # token below is what a resume compares, and it differs between the two lanes.
            if (
                not self._speculation_gate_admitted
                or not self._speculation_gate_receipt_digest
                or not (self._speculation_implementation_digest
                        or getattr(self, "_speculation_product_lane", False))
            ):
                raise RuntimeError("positive Card speculation reached run start without gate evidence")
            values["speculation_gate_receipt_digest"] = (
                self._speculation_gate_receipt_digest
            )
            values["speculation_policy_scope"] = self._speculation_policy_scope
        return values

    def _run_start_settled_widths(self) -> dict:
        """The RESOLVED concurrency widths this run is executing at, for the ``run_started`` record.

        Both Settings fields ship ``0`` = AUTO, a sentinel resolved off the LIVE BOX
        (`_detect_gpu_ids`). `config.snapshot.json` therefore stores the operator's INTENT, not the
        treatment the log was written under, and re-entry re-derives from whatever hardware it lands
        on: a 1-GPU run resumed on a 2-GPU host doubles its eval concurrency and flips the build spine
        from the serial one to the concurrent-append seam (invariant #1) MID-LOG, with nothing
        recorded either way. Pin the settled INTEGERS — the same fix, for the same reason, that
        `_resolve_speculation_depth` already applies to its own AUTO sentinel (invariant #6).

        These deliberately stay OUT of `RUN_START_PINNED_FIELDS`. That contract is the HTTP config
        editor's refuse-list ("start a new run to use different semantics"), and both widths remain
        operator-mutable mid-run through the durable `budget_extend` control event — exactly the
        reason `trust_gate` is excluded from it too. What re-entry owes them is narrower and lives in
        `_repin_settled_widths`: adopt the pin when the axis was launched AUTO, refuse a differently
        spelled explicit width, and stand aside once a control event has taken the axis over.
        """
        return {"eval_parallel": self._eval_parallel, "llm_parallel": self._llm_parallel}

    def _repin_declared_env(self, entry: RunState) -> None:
        """Restore the RUN-LEVEL DECLARED ENVIRONMENT `run_started` recorded (engine invariant #6).

        This is the strict reading of that invariant and not a convenience: a declared variable is
        the reason a node read one corpus rather than another (`VS_LOCAL_DATA_ROOT` is the measured
        case), so a resume that took a DIFFERENT value from live config would keep appending nodes
        to a log whose earlier nodes were evaluated under other conditions — and nothing in the run
        would say so. The log wins, always, for the whole rest of the run.

        Called from `_enter_run` beside `_repin_settled_widths`, and BEFORE any append, for the same
        reason: what the log recorded has to be in force before this invocation decides anything.

        ADOPT, never refuse. The widths refuse a contradicting re-entry because a width is a
        LAUNCH knob the operator re-spells on the resume command line and a silent adoption there
        would ignore something they typed; this value is normally spelled once, in a config file the
        resume re-reads on its own, so refusing would turn an unchanged file into a hard stop. The
        operator IS told at WARNING when the two disagree, and the way to run under a different
        environment is a new run — which is the honest answer, because the comparison the old nodes
        belong to no longer holds.

        A run that recorded NOTHING (an old log, or one launched with no declaration) keeps this
        process's own launch value untouched: inventing "the log said empty, so drop yours" would
        make the very first resume of a run started before this field existed silently lose an
        environment the operator has since declared, which is a regression rather than a pin.
        """
        recorded = getattr(entry, "eval_env", None)
        if not isinstance(recorded, dict) or not recorded:
            return
        live = dict(getattr(self, "_eval_env", None) or {})
        if live != recorded:
            # Name the DISAGREEING variables with BOTH values, not the two key sets: the ordinary
            # case is one variable whose VALUE changed (a data root re-pointed at a different
            # corpus), and two identical-looking key lists is a warning that reports a conflict
            # while hiding it. Printing values is safe precisely because a secret-shaped one was
            # refused at declaration time — that refusal is what lets this message be useful.
            names = sorted(set(recorded) | set(live))
            detail = "; ".join(f"{n}: log={recorded.get(n)!r} launch={live.get(n)!r}"
                               for n in names if recorded.get(n) != live.get(n))
            _LOG.warning(
                "resume: this run's declared eval_env disagrees with the launch config (%s). The "
                "RECORD wins (engine invariant #6) — every node in this log was evaluated under the "
                "recorded environment and results have to stay comparable. Start a NEW run to "
                "evaluate under a different one.", detail)
        self._eval_env = dict(recorded)

    def _repin_settled_widths(self, entry: RunState, *, source: Optional[str] = None) -> None:
        """Restore the widths ``run_started`` pinned, or refuse a re-entry that contradicts them.

        Called at the same re-entry boundaries as `_require_pinned_speculation_receipt` and, like it,
        BEFORE any append — a refusal must leave the log it declined to trust untouched.  "Before any
        append" is a promise only the CALLER can keep: reaching `Engine.run` is already past the
        CLI's own reopen/resume writes, so `cli/run_cmds.py::_preflight_settled_widths` runs this at
        the same four command-level boundaries the speculation receipt is authorized at.  This
        in-engine call stays as the backstop for every other entry point.

        ``source`` names the surface whose knob the operator must actually change, and is passed only
        by the CLI, which is the only layer that knows: `run` writes `config.snapshot.json` from its
        launch settings and never reads it back, while `resume` restores the run's settings FROM that
        snapshot.  Naming the wrong one sends the operator to edit a file with no effect on the
        command they ran, so `engine/widths.py::SETTLED_WIDTH_SOURCES` owns the mapping and ``None``
        keeps the generic phrasing a library ``Engine(...)`` caller gets.

        Deliberately NOT called per loop iteration: `_apply_control_overrides` re-applies an
        operator's `budget_extend` widths on every turn, so a per-iteration re-pin would either undo
        the operator's own live retune or refuse the run over it.
        """
        for axis, upper, recorded, resolved, auto in (
            ("eval_parallel", EVAL_WIDTH_MAX, getattr(entry, "eval_parallel", 0),
             self._eval_parallel, self._eval_parallel_startup_auto),
            ("llm_parallel", LLM_WIDTH_MAX, getattr(entry, "llm_parallel", 0),
             self._llm_parallel, self._llm_parallel_startup_auto),
        ):
            # 0 = the key is absent or malformed = a log written before widths were pinned. Keep this
            # process's own startup resolution: that is byte-identical to the pre-pin behaviour, and
            # inventing a width for a legacy log would be the very re-derivation this pin prevents.
            if type(recorded) is not int or not 1 <= recorded <= upper:
                continue
            if auto:
                # AUTO asked the BOX to decide. On re-entry the run's own log outranks a different
                # box — including a SMALLER one: continuing at the pinned width keeps one search
                # treatment across the whole log, and a width above what the hardware can serve is
                # bounded by the resource scheduler, not by silently rewriting the treatment.
                setattr(self, f"_{axis}", recorded)
                continue
            if recorded == resolved:
                continue
            # An operator who already retuned this axis through a durable control event owns it: the
            # override is re-applied by `_apply_control_overrides` on every turn, so the launch flag
            # has no effect on the running width and refusing the resume over it would be a false
            # alarm about a value that does nothing. Either spelling of the axis counts.
            if any(key in (getattr(entry, "budget_overrides", None) or {})
                   for key in (("max_parallel", "eval_parallel") if axis == "eval_parallel"
                               else ("parallel_build", "llm_parallel"))):
                continue
            raise SettledWidthPinError(
                settled_width_refusal(axis, resolved=resolved, recorded=recorded, source=source))

    def _setup_phase(self, state: RunState) -> None:
        # Per-RUN reset of the dep-install circuit breaker: it is a module global, so in the long-lived
        # `looplab ui` server a run that latched (egress blip) would leave auto-install disabled for the
        # next run in the same process until some pip call happens to respond.
        try:
            from looplab.runtime.deps import reset_install_latch
            reset_install_latch()
        except Exception:  # noqa: BLE001 - best-effort; a missing helper must not block setup
            pass
        # SETUP-COMPLETION GATE (arch-review §3 P0-3): gate on `setup_done` (folded from
        # setup_finished), NOT on run_id. run_started is appended in the MIDDLE of this block — before
        # AGENTS.md/provenance/host-grading/profiling and the leakage hard-stop — so a crash right
        # after it used to make every later resume skip the rest of preflight (leakage included)
        # forever. Gating on setup_done re-runs the body until it actually completes. Legacy logs that
        # never emitted setup_finished but already reached a node (or finished) are treated as
        # set-up-complete via `state.nodes`/`state.finished`, so they never re-run setup.
        # P0-3 material re-verification: on a PRE-node resume, re-run preflight if setup completed
        # against a DIFFERENT material manifest than we now hold (edited config / changed data or
        # workspace) — the `setup_done` boolean alone would skip the leakage/grounding checks on the
        # changed inputs. Only pre-node (a node present => the run is underway; mid-run drift is handled
        # by workspace_changed below). Re-running records a fresh setup_finished with the new manifest,
        # so this can never loop. Old logs (no recorded manifest) keep the pure-boolean behavior.
        _setup_stale = bool(state.setup_done and not state.nodes and state.setup_manifest
                            and self._setup_manifest() != state.setup_manifest)
        if not (state.setup_done or state.nodes or state.finished) or _setup_stale:
            # SETUP PHASE (task + data), an explicit, ONLINE-watchable phase: the pre-node work
            # (fingerprint the workspace, hash data provenance, profile columns, write AGENTS.md) is
            # otherwise silent between run_started and the first node. `setup_started` +/ `setup_step`
            # + `setup_finished` events land in the activity feed live, and a `setup` span (node_id=-1)
            # captures the trace so the UI's Setup pseudo-node shows what happened. setup_finished is
            # now folded (setup_done); the others stay pure observability.
            _su_t0 = time.time()
            self.store.append(EV_SETUP_STARTED,
                              {"phase": "task+data", "repo": bool(self._repo_spec),
                               "goal": (self.task.goal or "")[:200]})
            def _su_step(step: str, **detail):
                self.store.append(EV_SETUP_STEP, {"step": step, **detail})
            with self.tracer.span("setup", new_trace=True, node_id=-1) as _su:
                def _ev(name, **kv):
                    if _su is not None:
                        _su.event(name, **kv)
                cfg_hash = setup_config_hash(self.task.model_dump(mode="json"))
                # Reproducibility (item #4): pin the editable repo(s)+data fingerprint at start so a
                # resume can tell whether the source workspace changed underneath.
                _ev("workspace_fingerprint")
                wf = self._workspace_fingerprint()
                _su_step("workspace fingerprint", sources=list(wf.keys()))
                # run_started is the one-time identity anchor: append it only if it isn't already
                # recorded, so a resume RE-ENTERING setup after a crash-right-after-run_started (P0-3)
                # re-runs the REST of preflight (leakage) without minting a second run_started.
                if not state.run_id:
                    self.store.append(
                        EV_RUN_STARTED,
                        {
                            "run_id": self.run_dir.name,
                            # Display ids are only unique inside a run root. Cross-run memory uses this
                            # persisted incarnation id so roots named ``run_local`` cannot overwrite or
                            # self-exclude one another.
                            "run_uid": secrets.token_hex(16),
                            "task_id": self.task.id,
                            "goal": self.task.goal,
                            "direction": self.task.direction,
                            "config_hash": cfg_hash,
                            "workspace": wf,
                            # P0-5 environment identity: pin the interpreter + key-lib versions so a
                            # resume can flag a library upgrade that breaks bit-reproducibility.
                            "env": self._env_fingerprint(),
                            # P0-5 dirty-input enumeration: which repo files were uncommitted at start
                            # (repo tasks only; a clean/non-repo run records []). Provenance on top of
                            # the workspace content hash in `wf`.
                            "dirty_inputs": (self._dirty_inputs(wf) if self._repo_spec else []),
                            # T2 trust enforcement: recorded here so the pure fold applies the same
                            # gate on replay/resume (config isn't available to `replay.fold`). Absent in
                            # old logs -> "audit" -> byte-identical legacy selection.
                            "trust_gate": self.trust_gate,
                            # Holdout and verifier policy are immutable run-start semantics. Re-entry
                            # restores this shared contract from the fold rather than accepting a later
                            # snapshot edit that would mix incomparable scores or selection rules.
                            **self._run_start_pinned_values(),
                            # F1d: the run-level DECLARED ENVIRONMENT, when there is one. ABSENT
                            # otherwise, which keeps the default `run_started` payload BYTE-IDENTICAL
                            # — the same discipline `_run_start_pinned_values` follows for the Card
                            # selector flag, and here it is load-bearing twice over:
                            # `search/speculation_quality.py::_CALIBRATION_RUN_STARTED_FIELDS`
                            # compares the payload's key SET for equality, so an unconditional new key
                            # would revoke every issued calibration receipt, and the calibration
                            # profile declares no environment.
                            **({"eval_env": dict(self._eval_env)} if self._eval_env else {}),
                            # The SETTLED widths, not their AUTO sentinel: re-entry must never
                            # re-derive this run's execution treatment from a different box.
                            **self._run_start_settled_widths(),
                            # …and WHETHER the pinned depth resolved that sentinel. The pin alone
                            # cannot say, and only an AUTO run may ratchet itself down, so leaving
                            # this in the process let a later `looplab run <dir>` under the shipped
                            # `-1` default settle a SPELLED treatment to 0, irreversibly.
                            "speculation_depth_auto": bool(
                                getattr(self, "_speculation_depth_auto", False)),
                            "select_verifier_contract": VERIFIER_SELECTION_CONTRACT,
                        },
                    )
                # AGENTS.md (I18): run-level task-contract provenance. Repo backends receive their
                # task-specific brief directly and retain a seed repo's own AGENTS.md; this manifest
                # mirrors that contract without being copied over repository-owned instructions.
                # Runtime lines remain honest: capable tasks get the auto-install capability sentence,
                # offline/synthetic tasks stay numpy+stdlib (task_runtime_caps returns None for those).
                from looplab.core.hardware import detect_gpu, task_runtime_caps
                _md_caps = task_runtime_caps(self.task, auto_install=self._auto_install_deps,
                                             gpu=detect_gpu() if self._auto_install_deps else None)
                (self.run_dir / "AGENTS.md").write_text(
                    generate_agents_md(self.task, runtime_caps=_md_caps), encoding="utf-8")
                _ev("agents_md")
                _su_step("wrote AGENTS.md")
                # D4 data provenance: pin a content hash of every task asset/dataset into the run so a
                # result is tied to the exact data (repo tasks also pin via `workspace`). Reproducibility.
                prov = {name: hashlib.sha256(
                            c.encode("utf-8") if isinstance(c, str) else bytes(c)).hexdigest()[:16]
                        for name, c in (self._assets or {}).items()}
                if prov:
                    self.store.append(EV_DATA_PROVENANCE, {"assets": prov})
                    _ev("data_provenance", n=len(prov))
                    _su_step("data provenance", assets=list(prov))
                # Out-of-process host-side grading active: record WHICH scorer + how many held-out labels
                # (NEVER the labels themselves — the log is readable). Surfaced in the Trust panel.
                if self._host_grader is not None:
                    hg = self._host_grader
                    evt = {
                        "scorer": hg.get("scorer", "rmse"),
                        "predictions": self._graded_output_name()}
                    if hg.get("kind") == "mlebench":          # real MLE-bench: answers live in the
                        evt["competition"] = hg.get("competition")   # mle-bench data dir, never here —
                        # so there is no in-memory label list to count; n_labels=0 would mislead the Trust
                        # panel into "nothing held out". Omit it; `competition` signals host-held answers.
                    else:
                        evt["n_labels"] = len(hg.get("labels") or [])
                    self.store.append(EV_HOST_GRADING, evt)
                # Grounding pre-phase (I16): profile the dataset if the task exposes one.
                cols = getattr(self.task, "columns", None)
                if callable(cols):
                    self.store.append(EV_DATA_PROFILED, {"columns": profile_dataset(cols())})
                    _ev("data_profiled")
                    _su_step("data profiled")
                # Leakage-first grounding (I9): if the task exposes split/feature/target/time
                # data and a leak is detected, refuse to run — don't produce results on leaky data.
                leakage_blocked = self._leakage_blocks()
            # P0-3: bind this completion to the material it verified (reuse the wf computed above), so a
            # later resume can tell "done for THIS material" from "done for material that has changed".
            self.store.append(EV_SETUP_FINISHED, {"seconds": round(time.time() - _su_t0, 3),
                                                  "manifest": self._setup_manifest(wf=wf)})
            if leakage_blocked:
                # Preserve `_setup_phase`'s direct-call contract while using the same final-report
                # CAS as every other completion. If a control races this append, run()'s top-level
                # leakage gate refolds and retries instead of losing the intent.
                setup_events = self.store.read_all()
                setup_state = fold(setup_events)
                setup_seq = setup_events[-1].seq if setup_events else -1
                self._finish_with_report_if_quiescent(
                    setup_state, {"reason": "leakage"}, after_seq=setup_seq)
        elif self._repo_spec and state.workspace and not state.workspace_changed:
            # Resume (item #4): the editable workspace is copied fresh each node, so if the
            # operator's repo changed since the run started, later nodes silently evaluate a
            # DIFFERENT codebase. Record it instead of pretending the run is reproducible.
            now = self._workspace_fingerprint()
            if now != state.workspace:
                self.store.append(EV_WORKSPACE_CHANGED, {"was": state.workspace, "now": now})
        # P0-5 environment drift: on ANY resume where an env was pinned at run start, flag a Python/
        # library change — a run continued after an upgrade is no longer bit-reproducible, so record it
        # instead of pretending it is. Diagnostic-only (mirrors workspace_changed). state.env is None on
        # the first run (run_started is appended mid-setup, after this fold) and on old logs -> skipped.
        if state.env is not None and not state.env_changed:
            # `not state.env_changed` (F18): emit the drift note ONCE. Without the folded-flag gate a
            # run resumed repeatedly after an env upgrade re-appended an identical env_changed every time.
            _cur_env = self._env_fingerprint()
            if _cur_env != state.env:
                self.store.append(EV_ENV_CHANGED, {"was": state.env, "now": _cur_env})

    def _require_pinned_speculation_receipt(self, entry: RunState) -> None:
        """Fail closed on positive-depth or calibration re-entry before any log mutation."""
        profile_digest = str(getattr(
            entry, "speculation_calibration_profile_digest", "") or "")
        calibration_gpu = getattr(entry, "speculation_calibration_gpu_inventory", None)
        calibration_seed = getattr(entry, "speculation_calibration_seed", None)
        # TWO DIFFERENT DEPTH FACTS, and reading only one of them is what made an adaptively settled
        # run unresumable through the engine's OWN printed advice. `speculation_depth_pinned` is what
        # `run_started` recorded — the LAUNCH treatment invariant #6 owns, and the only thing an
        # operator's spelled depth may be compared against. `speculation_depth` is that pin narrowed
        # by every `speculation_depth_settled` row (`replay.py::_on_speculation_depth_settled`) — a
        # measurement THIS RUN made about itself, which the run is allowed to make and the operator is
        # not. Until 2026-08-06 the single folded field carried both, so a resume that spelled exactly
        # the depth `run_started` pinned was refused with "speculation_depth was pinned at 0" for a
        # log whose run_started said 1.
        recorded_depth = getattr(entry, "speculation_depth_pinned", 0)
        adaptive_depth = getattr(entry, "speculation_depth", 0)
        recorded_impl = str(getattr(
            entry, "speculation_implementation_digest", "") or "")
        recorded_scope = str(getattr(entry, "speculation_policy_scope", "") or "")
        recorded_receipt = str(getattr(
            entry, "speculation_gate_receipt_digest", "") or "")
        recorded_runtime_scope = str(getattr(
            entry, "speculation_runtime_scope_sha256", "") or "")
        recorded_calibration = bool(
            profile_digest or calibration_gpu or calibration_seed is not None)
        # Treat every durable speculation authority/prefix as gated, even when another field was
        # corrupted or omitted.  In particular card=false must not turn a receipt/implementation/
        # policy/depth prefix into an inert-looking log that recovery or command ACK may mutate.
        recorded_marker = bool(
            recorded_calibration
            or recorded_impl
            or recorded_scope
            or recorded_receipt
            or recorded_runtime_scope
            or (type(recorded_depth) is int and recorded_depth > 0)
        )
        if not recorded_marker:
            return

        def reject(*causes: str) -> None:
            # NAME THE CAUSE. This message used to list every pin the check knows about and let the
            # operator guess which one moved — and the one that actually fired most often (a
            # whole-source implementation digest revoked by an unrelated edit) was not even in the
            # list, so the text pointed at a receipt/profile/seed/GPU mismatch that had not happened.
            detail = "; ".join(causes) if causes else "a run-start speculation pin does not match"
            raise SpeculationAuthorizationError(
                f"cannot resume this run's Card speculation/calibration: {detail}. The run-start "
                "record owns these values (engine invariant #6) — re-run with the launch settings "
                "the log pinned rather than editing them on the resume command."
            )

        # ONE re-entry rule for the depth, and it is stated here once. It used to be two that
        # contradicted each other: this AUTO-only adoption, and `_reentry_repin`'s unconditional
        # `self.speculation_depth = _entry.speculation_depth`.
        #
        #   * AUTO is a STARTUP resolution off the live box (`_resolve_speculation_depth`), exactly
        #     like eval_parallel/llm_parallel — the operator asked the BOX to decide, so on re-entry
        #     the run's own log outranks a different box. Adopt the run's EFFECTIVE depth, its own
        #     ratchet included: a run narrowing itself is not an operator disagreement and must never
        #     refuse. This is what keeps a resume on a differently-sized box continuing the run's own
        #     search treatment.
        #   * An EXPLICITLY spelled depth is never adopted, and it is compared against the LAUNCH PIN
        #     below — a changed explicit treatment must still fail closed. Note what that means for a
        #     run that ratcheted: spelling the pin is ACCEPTED and still runs at the settled depth,
        #     because the ratchet is a durable one-way fact about this run that no resume flag can
        #     un-record (`engine/speculation.py::_settle_speculation_depth` says so in the warning it
        #     prints, which used to advise the opposite).
        auto_depth = bool(getattr(self, "_speculation_depth_auto", False))
        if (
            auto_depth
            and type(adaptive_depth) is int
            and 0 <= adaptive_depth <= 64
        ):
            self.speculation_depth = adaptive_depth
        # Which recorded depth THIS process has to agree with, per the rule above. Computed once so
        # the legacy adoption below and the refusal further down cannot drift apart.
        authoritative_depth = adaptive_depth if auto_depth else recorded_depth
        depth_agrees = (type(authoritative_depth) is int
                        and authoritative_depth == self.speculation_depth)

        # LEGACY PRODUCT-LANE ADOPTION (invariant #6 again). A build before the receipt-lane fix
        # carried a supplied receipt's identity into `run_started` even on a workload the receipt
        # never measured, so those logs pin a whole-source `speculation_implementation_digest` that no
        # later process can reproduce once anything is edited or upgraded. They are the exact runs the
        # product lane exists to keep resumable, and refusing them forever punishes the operator for a
        # bug in the writer. Adopt what the log recorded — but ONLY for that precise legacy shape:
        # this process must itself be in the product lane, and the log must carry no calibration
        # fields and no runtime-scope pin, so a calibrated lane's envelope can never be adopted away.
        if (
            getattr(self, "_speculation_product_lane", False)
            and not self._speculation_gate_calibration
            and not recorded_calibration
            and not recorded_runtime_scope
            and recorded_impl
            and recorded_receipt
            and recorded_scope == SPECULATION_POLICY_SCOPE
            and getattr(entry, "card_driven_selection", False) is True
            and depth_agrees
        ):
            self._speculation_implementation_digest = recorded_impl
            self._speculation_gate_receipt_digest = recorded_receipt

        causes: list[str] = []
        if (
            not isinstance(getattr(entry, "run_id", None), str)
            or not entry.run_id.strip()
            or entry.run_id != self.run_dir.name
        ):
            causes.append(
                f"the log was written for run id {getattr(entry, 'run_id', None)!r}, "
                f"not {self.run_dir.name!r}")
        if not self._speculation_gate_admitted:
            causes.append(
                "this process did not admit Card speculation at all (card_driven_selection off, "
                "depth 0, or a policy other than "
                f"{SPECULATION_POLICY_SCOPE!r}), but the log records a speculative prefix")
        # EQUALITY, not "must be present", for BOTH evidence identities: the calibrated lane pins an
        # implementation digest and a runtime scope, the product lane deliberately pins neither (see
        # `speculation_product_authority_digest` for why a whole-source digest must not gate a real
        # run's resume). Empty-vs-empty is the product lane agreeing with itself; either lane meeting
        # the other's log still fails closed, in both directions.
        if recorded_impl != self._speculation_implementation_digest:
            causes.append(
                "the run started in the "
                f"{'calibrated' if recorded_impl else 'product'} lane but is being resumed in the "
                f"{'calibrated' if self._speculation_implementation_digest else 'product'} one "
                "(speculation_implementation_digest differs)")
        if recorded_runtime_scope != self._speculation_runtime_scope_sha256:
            causes.append(
                "the calibrated runtime-scope pin differs — the Settings/roles/sandbox envelope the "
                "receipt was measured under is not the one this process is launching")
        if not getattr(self, "_speculation_product_lane", False) and not recorded_impl:
            causes.append(
                "the log carries no evidence identity, so it cannot be resumed under a receipt")
        if getattr(entry, "card_driven_selection", False) is not True:
            causes.append("the log did not pin card_driven_selection=true")
        if not depth_agrees:
            # NAME BOTH FACTS when they differ. The old text said "pinned at <settled value>", which
            # was not a value `run_started` ever carried, so it sent the operator to re-run with a
            # launch setting the log does not record — and the depth it printed was the one the
            # ratchet had already overridden.
            settled_note = (
                f" (and settled by this run to {adaptive_depth!r})"
                if adaptive_depth != recorded_depth else "")
            causes.append(
                f"speculation_depth was pinned at run start to {recorded_depth!r}{settled_note}, "
                f"and this process resolved {self.speculation_depth!r}")
        if recorded_scope != SPECULATION_POLICY_SCOPE:
            causes.append(
                f"the log pinned policy scope {recorded_scope!r}, not {SPECULATION_POLICY_SCOPE!r}")
        if self._speculation_policy_scope != SPECULATION_POLICY_SCOPE:
            causes.append(
                f"this process resolved policy scope {self._speculation_policy_scope!r}, "
                f"not {SPECULATION_POLICY_SCOPE!r}")
        if causes:
            reject(*causes)

        # The hidden evidence bootstrap is immutable: any control would invalidate the paired
        # measurement.  A public receipt, by contrast, admits the measured launch envelope and keeps
        # explicit Stage-6 operator controls available.  Those interventions remain in the event log
        # and the quality evidence reader rejects such a run as future calibration evidence.
        if self._speculation_gate_calibration and (
            self._policy_name != SPECULATION_POLICY_SCOPE
            or bool(getattr(entry, "budget_overrides", None))
            or getattr(entry, "pending_strategy", None) is not None
            or bool(getattr(entry, "active_strategy", None))
        ):
            reject(
                "this is the hidden calibration bootstrap, whose paired measurement admits no "
                "policy swap, budget override or Strategy — and the log records one")

        if recorded_calibration:
            if (
                self._speculation_gate_calibration is not True
                or profile_digest != SPECULATION_CALIBRATION_PROFILE_DIGEST
                or self._speculation_calibration_profile_digest != profile_digest
                or not isinstance(calibration_gpu, list)
                or calibration_gpu != self._speculation_calibration_gpu_inventory
                or type(calibration_seed) is not int
                or calibration_seed != self._speculation_calibration_seed
                # Calibration never serializes its internal admission token as a public receipt.
                or bool(getattr(entry, "speculation_gate_receipt_digest", ""))
            ):
                reject(
                    "the log is a calibration bootstrap and its exact profile digest, GPU inventory "
                    "and seed are not the ones this process resolved")
            return

        if self._speculation_gate_calibration:
            reject("this process is a calibration bootstrap but the log is not one")
        if not recorded_receipt:
            causes.append("the log records no speculation lane token")
        elif recorded_receipt != self._speculation_gate_receipt_digest and (
            recorded_receipt not in getattr(
                self, "_speculation_product_authority_tokens", frozenset())
        ):
            causes.append(
                "the speculation lane token differs — on the product lane it is derived from the "
                "policy scope and the TASK KIND, so a resume that names a different task kind (or a "
                "receipt-authorized log met by a receiptless process) lands here")
        if causes:
            reject(*causes)

    def _request_create_pause(self, node_id: int, reason: str) -> None:
        """Ask the MAIN task to append the run-global auto-pause gate.

        Called from a build worker thread, where appending EV_PAUSE directly would put a FOLDED,
        run-global, selection-affecting event outside invariant #1's worker seam. `list.append` is
        atomic under the GIL, so several crashing siblings in one chunk queue safely; only the FIRST
        is appended — they are the same "a build crashed, stop the batch" gate and one pause is what
        the run needs.
        """
        # Lazily initialised: the run loop resets the queue each iteration, but a build can crash
        # on a path that has not reached that reset yet.
        if not isinstance(getattr(self, "_pending_create_pause", None), list):
            self._pending_create_pause = []
        self._pending_create_pause.append({
            "node_id": node_id, "generation": 0, "reason": reason})
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
        if main_task:
            if not fold(self.store.read_all()).paused:
                self.store.append(EV_PAUSE, {"reason": reason})
        else:
            # Same queue and same drain as `_request_create_pause`; only the payload differs.
            if not isinstance(getattr(self, "_pending_create_pause", None), list):
                self._pending_create_pause = []
            self._pending_create_pause.append({"reason": reason})
        return True

    def _reentry_repin(self) -> bool:
        _events = self.store.read_all()
        _entry = fold(_events)
        # Re-pin after setup for the same reason the receipt check repeats here: a FRESH run's own
        # run_started was appended by `_setup_phase` a few lines ago (a no-op re-pin), while a resume
        # re-reads a tail another writer may have extended.
        self._repin_settled_widths(_entry)
        self._require_pinned_speculation_receipt(_entry)
        self._pending_finalize_scope = incomplete_finalize_scope(_events)
        # A failed finalize attempt is recorded as finished(reason=error) by the CLI guard, but its
        # durable stop is still pending. Treat that as NOT already finalized so the retry below can
        # write run_finished(aborted) and re-run budget/archive/case/cost wrap-up exactly once.
        entry_finished = bool(_entry.finished and self._pending_finalize_scope is None and not (
            _entry.stop_requested and str(_entry.stop_reason or "").lower() == "error"))
        # Restore Card authority before replaying the active Strategy: its conditional governance
        # grant for card_scoring depends on this run-start-pinned value, not the ambient snapshot.
        if _entry.run_id:
            self.card_driven_selection = _entry.card_driven_selection
            # THE LOG'S OWN TREATMENT WINS (invariant #6), and the value adopted is the EFFECTIVE
            # depth — the launch pin narrowed by every settle row this run wrote. Same single rule
            # `_require_pinned_speculation_receipt` states, and it ran at the top of this method,
            # where it has ALREADY failed closed on a spelled depth that disagrees with the launch pin.
            #
            # SAY SO WHERE IT CANNOT. A log whose `run_started` recorded no speculative prefix at all
            # never reaches that refusal (the guard returns on `recorded_marker`), so a resume
            # spelling `-s speculation_depth=2` over a run that pinned none was accepted there and
            # then silently clamped to 0 right here — the operator's explicit flag doing nothing, with
            # nothing said, which is the shape of the two rules disagreeing. It still cannot take
            # effect (turning the treatment on mid-run would write a speculative prefix into a log
            # whose run_started carries no receipt authorizing one, and the run's own next re-entry
            # would then have to refuse it), but it is no longer silent.
            # Gated on `_speculation_gate_admitted` so this says only what it means. A depth spelled
            # with Layer 3 OFF never entered the lane in the first place (`admit_speculation_lane`
            # requires `card_driven_selection`), so it is not re-entry that is ignoring it — and
            # `_run_start_pinned_values` omits the key in that case, which would otherwise make every
            # FRESH `-s card_driven_selection=false -s speculation_depth=2` run warn about its own
            # run_started.
            if (not getattr(self, "_speculation_depth_auto", False)
                    and getattr(self, "_speculation_gate_admitted", False)
                    and self.speculation_depth != _entry.speculation_depth):
                _LOG.warning(
                    "ignoring speculation_depth=%d on re-entry: this run's log is the authority for "
                    "its search treatment (engine invariant #6) and records %d — run_started pinned "
                    "%d. A depth can only be chosen at LAUNCH; start a new run to use a different "
                    "one.",
                    self.speculation_depth, _entry.speculation_depth,
                    getattr(_entry, "speculation_depth_pinned", 0))
            self.speculation_depth = _entry.speculation_depth
        # A7 Strategist: re-apply the last-decided strategy on (re)entry so a resumed run continues
        # with it WITHOUT re-consulting the Strategist (the decision lives in the event log).
        if _entry.active_strategy:
            # A recorded Developer backend is part of this run's treatment. If today's credential or
            # endpoint cannot reconstruct it, refuse re-entry instead of silently continuing on the
            # constructor's backend and making fold/live disagree.
            self._apply_strategy(_entry.active_strategy, _strict_developer=True)
        # R1-c resume-safety (invariant #6): the fold applies the RECORDED tie-break rule
        # (`st.select_verifier_tiebreak`, folded from run_started); re-pin the engine's live-verify gate
        # to match so `_maybe_verify_ties` produces atomic group scores consistently with what the fold
        # reads — not a possibly-changed live `LOOPLAB_SELECT_VERIFIER`. Its direct peer `holdout_select`
        # is re-pinned the same way below. Guard on `run_id` (set only by run_started): on a path where
        # setup hasn't recorded run_started yet, keep the live value rather than zero it from an empty fold.
        if _entry.run_id:
            self._select_verifier = _entry.select_verifier_tiebreak
            self._verifier_ci_tie = _entry.verifier_ci_tie   # R1-d: re-pin the recorded CI-tie rule
            self._select_verifier_samples = _entry.select_verifier_samples
        # Pinned by tests/test_holdout.py::test_a_resume_honours_the_recorded_split_not_a_changed_live
        # _setting, which resumes with every one of these settings CHANGED and asserts the recorded
        # values win (both this block and the verifier re-pin above).
        # D1 resume-safety: honor the holdout split the run ORIGINALLY committed to (recorded in
        # run_started), not a possibly-changed live `holdout_fraction` — otherwise nodes evaluated
        # before vs. after a config change would be scored on different splits and the champion pick
        # would mix incomparable metrics. Recorded holdout_select likewise wins on resume.
        if _entry.holdout_fraction is not None:
            self._holdout_fraction = _entry.holdout_fraction
            self._holdout_select = _entry.holdout_select
            # P0-2 freshly-hidden per-epoch holdout: rebuild the partition for the CURRENT search
            # epoch. A run reopened after finishing (search_epoch>=1) then scores its new candidates
            # on a never-disclosed split instead of the one revealed at the prior finish ('already-
            # seen exam'). Epoch 0 rebuilds the byte-identical original partition, so a normal
            # single-epoch run (and every replay of an existing log) is unchanged.
            self._holdout_idx = self._build_holdout_idx(self._holdout_fraction, _entry.search_epoch)
            self._holdout_epoch = _entry.search_epoch
        # E4: cross-run meta-learned priors. Excluding THIS run's id matters on resume: a run that
        # already mid-run-distilled its own comparative lessons (M6) must not read them back as if
        # they were another run's experience — its own results are already in the digest. The stamp
        # is taken BEFORE the read (a write landing in between is re-read next refresh — safe).
        self._lessons_seen_stamp = self._lessons_store_stamp()
        # §role-split: the RESEARCHER prior carries only R&D lessons; the DEVELOPER prior only its own
        # code-fix lessons (routed into the idea handed to the Developer via `_directed_idea`). One
        # scan builds both — the two role pools share every untagged lesson, so re-reading/re-embedding
        # the store per role is wasted work.
        _rid = _entry.run_id or None
        _ruid = _entry.run_uid or None
        # BEST-EFFORT, exactly like the refresh path (`lessons.maybe_refresh_lessons`) this mirrors.
        # `_load_reflection_priors_both` reads the SHARED store through `read_jsonl_lenient`, which
        # RAISES OSError on an unreadable lessons.jsonl / meta_notes.jsonl (permissions, a transient
        # FS fault) — while `_lessons_store_stamp` one line up already swallows the same OSError.
        # Unguarded, that failed the run during DETERMINISTIC setup, before the first node, on every
        # start AND every resume: a true crash-loop, strictly worse than the mid-run refresh case the
        # sibling guard was written for. The stamp is reset to None so the first refresh cadence
        # retries the store instead of reading the pre-read stamp as "already seen, unchanged".
        try:
            self._prior_note_text, self._dev_prior_note_text = \
                self._load_reflection_priors_both(
                    exclude_run_id=_rid, exclude_run_uid=_ruid)
        except (OSError, ValueError) as e:  # noqa: BLE001 - an advisory prior cannot fail the run
            self._lessons_seen_stamp = None
            self.store.append(EV_LESSONS_STORE_UNAVAILABLE, {
                "mode": "read", "phase": "run_start", "error": str(e)[:300]})
        return entry_finished

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

    def _apply_control_overrides(self, state: RunState) -> tuple[Optional[float], Optional[float]]:
        # Effective budgets: an operator may raise (or lower) them live via a `budget_extend`
        # control event (folded into state.budget_overrides), e.g. "keep going for 600s more".
        # max_seconds ("keep going 600s more") is a first-class operator budget extension via the
        # budget_extend control event, not an agent_control-governed knob — applied as-is.
        _bo = state.budget_overrides
        if self._speculation_gate_calibration and _bo:
            raise RuntimeError(
                "Card speculation calibration forbids runtime budget/resource overrides; "
                "max_nodes and the complete execution envelope are receipt-bound")

        def _finite_ceiling(key: str, fallback: Optional[float]) -> Optional[float]:
            raw = _bo.get(key)
            if raw is None or isinstance(raw, bool):
                return fallback
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError):
                return fallback
            return value if math.isfinite(value) and value > 0 else fallback

        # apply stays total even for a manually constructed/forward-version RunState;
        # replay normally sanitizes these first, but a poison ceiling must never disable a budget.
        max_s = _finite_ceiling("max_seconds", self.max_seconds)
        # A `budget_extend` is a HUMAN control intent, NOT an agent decision: CONTROL_EVENTS are
        # UI/CLI-authored (see the engine-writer invariant), and the boss action-builder
        # (serve/routers/boss.py::_Action) can ONLY ever emit `add_nodes` — it carries no field for
        # any resource ceiling. So the budget fields below reach the log ONLY from an operator via the
        # /control endpoint. Apply them AS-IS ("a human can always change it via the UI/snapshot").
        # Gating them on `_agent_may("boss", …)` (as an earlier M4 pass did) protected against nothing
        # — no agent authors them — and only ever DROPPED the operator's OWN override, silently pinning
        # the run to the old cap. Agent-authored resource retunes (the Strategist's timeout/max_parallel)
        # remain governed by the matrix in `_apply_strategy`, which is where the M4 lock genuinely lives.
        max_es = _finite_ceiling("max_eval_seconds", self.max_eval_seconds)
        if "timeout" in _bo and not isinstance(_bo["timeout"], bool):
            try:
                _timeout = float(_bo["timeout"])
                if math.isfinite(_timeout) and _timeout > 0:
                    self.timeout = max(0.1, _timeout)
            except (TypeError, ValueError, OverflowError):
                pass
        # Legacy first, canonical last: a modern command carrying both spellings is deterministic.
        # Live 0 settles to serial width 1; only launch-time Settings retain hardware/eval AUTO.
        for _key in ("max_parallel", "eval_parallel"):
            if _key in _bo:
                _settled = settle_width(_bo[_key], EVAL_WIDTH_MAX)
                if _settled is not None:
                    self._eval_parallel = _settled
        for _key in ("parallel_build", "llm_parallel"):
            if _key in _bo:
                _settled = settle_width(_bo[_key], LLM_WIDTH_MAX)
                if _settled is not None:
                    self._llm_parallel = _settled
        # A canonical live control opts into the shared provider-call ceiling. Replay may also retain
        # the last canonical total beside a newer legacy build-only override so resume cannot silently
        # change broker behavior; legacy-only historical controls remain unbounded for compatibility.
        if "llm_broker_total" in _bo or "llm_parallel" in _bo:
            self._reconfigure_llm_broker(
                _bo.get("llm_broker_total", _bo.get("llm_parallel")))
        return max_s, max_es

    async def _defer_for_node_budget(self, state: RunState) -> bool:
        """Keep a durable node-creating control head live until ``budget_extend`` admits it.

        Returning immediately as "not served" would let the empty-action branch finalize with a
        stranded fork/inject/ablation request. Returning immediately as "served" would tight-spin.
        One bounded poll turn keeps the engine responsive to abort/pause/budget controls and survives
        process restart because the request itself remains the append-only queue authority.
        """

        if self._node_reservation_slots_remaining(state) >= 1:
            self._budget_wait_s = _BUDGET_WAIT_MIN_S      # the wait ended; the next one starts short
            return False
        # BACK OFF geometrically. Each tick makes the main loop re-read and re-fold the ENTIRE event
        # log (plus the per-turn ack/mirror re-reads), and this head can wait hours for an operator's
        # budget_extend — a fixed 0.5s tick meant hours of O(total-events) busy-polling on a long log,
        # the same cost class the resource-wait comment in _dispatch_evals flags. The ceiling is
        # small enough that abort/pause/budget controls are still observed within a few seconds,
        # which is what "keeps the engine responsive" above actually requires.
        delay = getattr(self, "_budget_wait_s", _BUDGET_WAIT_MIN_S)
        await anyio.sleep(delay)
        self._budget_wait_s = min(delay * 2.0, _BUDGET_WAIT_MAX_S)
        return True

    @staticmethod
    def _pending_forced_ablation(state: RunState) -> Optional[dict]:
        """Return the first exact forced-ablation lifecycle not yet acknowledged."""

        forced = next((r for r in state.ablate_request_generations
                       if r.get("node_id") in state.nodes
                       and r.get("node_id") not in state.aborted_nodes
                       and not state.nodes[r["node_id"]].tombstoned
                       and state.nodes[r["node_id"]].attempt == r.get("generation")
                       and not any(a.get("parent_id") == r["node_id"]
                                   and a.get("generation") == r.get("generation")
                                   for a in state.ablations)), None)
        if forced is not None:
            return dict(forced)
        legacy = next((parent_id for parent_id in state.ablate_requests
                       if parent_id in state.nodes
                       and parent_id not in state.aborted_nodes
                       and not state.nodes[parent_id].tombstoned
                       and not any(a.get("parent_id") == parent_id
                                   for a in state.ablations)), None)
        if legacy is None:
            return None
        return {
            "node_id": legacy,
            "generation": state.nodes[legacy].attempt,
        }

    def _append_inject_failure(
        self,
        state: RunState,
        *,
        error: str,
        reason: str,
    ) -> bool:
        """Atomically append one positional inject failure and its replay gate."""

        request_idx = state.injects_done

        def _plan(events, tail) -> bool:
            current = fold(events)
            if current.injects_done > request_idx:
                return True
            if (
                current.injects_done != request_idx
                or len(current.inject_requests) <= request_idx
            ):
                return False
            self.store.append_many([
                (EV_INJECT_FAILED, {
                    "idx": request_idx,
                    "error": str(error)[:500],
                    "reason": reason,
                }),
                (EV_INJECT_DONE, {
                    "idx": request_idx,
                    "skipped": reason,
                }),
            ], expected_last_seq=tail)
            return True

        # The counter pair did not advance, so the request is still open and the next turn retries it.
        return retry_tail_cas(self.store, _plan, on_exhaust=lambda: False)

    def _close_node_creating_forced_request_before_terminal_gate(
        self,
        state: RunState,
        *,
        reason: str,
    ) -> bool:
        """Durably skip one forced Node creator before a stronger terminal budget wins.

        Wall/eval ceilings intentionally outrank operator work, but finalizing without a matching
        acknowledgement leaves a replay-visible queue head stranded in a finished run. Close one head
        per turn, then re-fold before the terminal CAS. A node-budget-only wait never calls this helper
        and therefore remains resumable via ``budget_extend{add_nodes}``.
        """

        if len(state.fork_requests) > state.forks_done:
            request = state.fork_requests[state.forks_done]
            self.store.append(EV_FORK_DONE, {
                # The POSITION is the receipt's identity (`_advance_request_cursor`): `from_node_id`
                # cannot separate two queued forks of the same parent, which is the ordinary pattern.
                "idx": state.forks_done,
                "from_node_id": request.get("from_node_id"),
                "generation": request.get("generation"),
                "skipped": reason,
            })
            return True
        if len(state.inject_requests) > state.injects_done:
            self._append_inject_failure(
                state,
                error=f"not executed: terminal {reason} gate won",
                reason=reason,
            )
            # A lost tail CAS still means this head blocks finalization. Re-fold and retry next turn.
            return True
        forced_ablate = self._pending_forced_ablation(state)
        if forced_ablate is not None:
            self.store.append(EV_ABLATE, {
                "parent_id": forced_ablate["node_id"],
                "generation": forced_ablate["generation"],
                "impacts": {},
                "eval_seconds": 0.0,
                "skipped": reason,
            })
            return True
        return False

    async def _serve_forced_requests(self, state: RunState) -> bool:
        # Operator-forced steering (Phase 5), one per iteration then re-fold. Each is gated on
        # the domain event it produces (fork_done / an ablate event / node_confirmed), so a
        # resume never repeats it — deterministic under replay. Returns True when a request was
        # served OR deliberately left pending for node budget (the caller re-folds via `continue`);
        # False lets the loop fall through. The pending branch performs its own bounded wait.
        if len(state.fork_requests) > state.forks_done:
            req = state.fork_requests[state.forks_done]
            pid = req.get("from_node_id")
            generation = req.get("generation")
            current = state.nodes.get(pid)
            # Unstamped queued-before-create requests are historical and bind when their node appears.
            # Every modern producer stamps, so explicit generations remain strict CAS.
            served = (current is not None and not current.tombstoned
                      and pid not in state.aborted_nodes
                      and (generation is None or current.attempt == generation))
            if served:
                # A valid fork remains the durable queue head while the physical Node ceiling is full.
                # Do not append fork_done: a later budget_extend must be able to serve this same intent.
                if await self._defer_for_node_budget(state):
                    return True
                generation = current.attempt
                # CLAIM THE REQUEST BEFORE THE PAID PRODUCER — the same at-most-once boundary
                # `_claim_paid_finalize_step` states ("persist the boundary before dispatching a
                # paid/external effect"). `_create_node` runs the Researcher + Developer (real spend) and
                # durably appends `node_created`; with the receipt written AFTER it, a crash in that gap
                # left the request still at the queue head, so resume re-served it and minted a SECOND
                # paid child for the same fork. Ordering the receipt first makes the failure mode
                # at-most-once: a crash in the gap loses ONE queued fork intent instead of duplicating an
                # experiment and its spend — and an operator can simply re-request the fork, whereas a
                # duplicate is silent, already charged, and pollutes the tree.
                # Fold-safe: `_on_fork_done` advances only the fork cursor and `_on_node_created` only
                # the node table, so the swap is order-tolerant. It is NOT byte-identical on every old
                # log: `_on_fork` drops a request whose parent is tombstoned/aborted at that point in
                # the replay, and the cursor is now bounded by the queue it indexes, so a historical
                # receipt for a request the current fold declines no longer advances past it.
                self.store.append(EV_FORK_DONE, {
                    "idx": state.forks_done, "from_node_id": pid, "generation": generation})
                # Beyond the crash-in-the-gap above, `_create_node` can also decline SILENTLY in a
                # live process: `_reserve_node_build` returns None on a lost proposal-authority CAS, a
                # slot race or `paused`, and the novelty/card-contract gate can drop the proposal — it
                # then simply returns. The receipt is already spent, so the operator's request used to
                # vanish with the Researcher call paid and NOTHING in the log saying the fork produced
                # nothing. Record that. Fold-ignored (see EV_FORK_UNFULFILLED), so the cursor and every
                # selection input are untouched and the append stays splice-neutral by construction;
                # this only makes the drop legible. Re-fold rather than trusting a cached state
                # (invariant 4) and look for a node PARENTED ON `pid` past the pre-call ceiling: a
                # concurrent parallel-build sibling can add an unrelated node in the same window, and
                # miscounting that as success is the safe direction (it only stays quiet).
                before = {n.id for n in fold(self.store.read_all()).nodes.values()}
                self._create_node({"kind": "improve", "parent_id": pid,
                                   "parent_generations": {str(pid): generation}})
                after = fold(self.store.read_all()).nodes
                if not any(nid not in before and pid in (getattr(nd, "parent_ids", None) or [])
                           for nid, nd in after.items()):
                    self.store.append(EV_FORK_UNFULFILLED, {
                        "idx": state.forks_done, "from_node_id": pid, "generation": generation})
            else:
                self.store.append(EV_FORK_DONE, {
                    "idx": state.forks_done, "from_node_id": pid, "generation": generation,
                    "skipped": "stale_generation"})        # advance the gate past an unservable head
            return True
        # Operator-authored experiment (manual tree edit): the human hand-adds a node (an idea
        # + optional parent + optional ready-made code). Materialize it into a real pending node;
        # the policy then evaluates it next (pending nodes are scheduled first). Gated on
        # `inject_done` so a resume never re-creates it — deterministic under replay.
        if len(state.inject_requests) > state.injects_done:
            req = state.inject_requests[state.injects_done]
            # Reject a structurally impossible durable row before waiting for Node capacity. The
            # validator is pure/bounded and mirrors materialization; no Developer/LLM work occurs.
            try:
                self._prepare_injected_node(state, req)
            except Exception as exc:  # noqa: BLE001 - legacy/hand-authored event rows are untrusted
                self._append_inject_failure(
                    state,
                    error=str(exc),
                    reason="invalid_request",
                )
                return True
            # Unlike malformed input, temporary budget exhaustion is not a failed inject. Leave the
            # request unacknowledged so an additive budget extension can admit it exactly once.
            if await self._defer_for_node_budget(state):
                return True
            # CLAIM THE REQUEST BEFORE THE PAID PRODUCER, exactly as the fork branch above does and
            # for the same reason. `_create_injected_node` can run a Developer session (real spend)
            # and durably appends `node_created`; with the receipt written AFTER it, a crash inside
            # that call left this request at the queue head, so resume re-served it and bought the
            # session again — and a crash between the durable `node_created` and the receipt re-served
            # too, where Card dedup then closed the SUCCEEDED inject as "materialization_failed" or
            # minted a duplicate node. Receipt-first makes the failure at-most-once: a crash in the
            # gap loses ONE queued inject intent instead of duplicating an already-charged experiment,
            # and the operator can simply re-request it. Fold-safe: `_on_inject_done` advances only
            # the inject cursor and `_on_node_created` only the node table, so the swap is
            # order-tolerant (invariant #3 — the side effect is gated on its event).
            self.store.append(EV_INJECT_DONE, {"idx": state.injects_done})
            try:
                self._create_injected_node(req)
            except Exception as e:  # noqa: BLE001 - a malformed operator/API inject must not
                # crash-loop the engine: the gate has already advanced, so this only records WHY the
                # (already-spent) request produced nothing. Terminalize any surviving build marker
                # first: the failure happened inside this same invocation, so unlike an escaping
                # serial build exception there may be no resume boundary to clean a partial
                # reservation.
                failed_state = fold(self.store.read_all())
                if failed_state.buildings:
                    self._recover_interrupted_builds(failed_state)
                # NOT `_append_inject_failure`: that helper appends the failure AND the gate as one
                # atomic pair, and returns early when the gate has already moved — which it has,
                # three lines up. Append the diagnosis alone. `EV_INJECT_FAILED` is DIAGNOSTIC
                # (fold-ignored), so it changes no state either way; it carries `idx` so the log,
                # `looplab replay` and the trace still say which request produced nothing.
                self.store.append(EV_INJECT_FAILED, {
                    "idx": state.injects_done,
                    "error": str(e)[:500],
                    "reason": "materialization_failed",
                })
            return True
        forced_ablate = self._pending_forced_ablation(state)
        if forced_ablate is not None:
            # Ablation probes culminate in one new refine_block Node. Avoid both the paid probes and a
            # false completion while that physical reservation has no budget slot.
            if await self._defer_for_node_budget(state):
                return True
            await self._ablate(forced_ablate["node_id"],
                               expected_generation=forced_ablate["generation"])
            return True
        forced_confirm = next((r for r in state.confirm_request_generations
                               if r.get("node_id") in state.nodes
                               and r.get("node_id") not in state.aborted_nodes
                               and not state.nodes[r["node_id"]].tombstoned
                               and state.nodes[r["node_id"]].attempt == r.get("generation")
                               and state.nodes[r["node_id"]].status is NodeStatus.evaluated
                               and r not in state.confirmed_forced_generations), None)
        if forced_confirm is None:
            legacy_confirm = next((nid for nid in state.confirm_requests
                                   if nid in state.nodes
                                   and nid not in state.aborted_nodes
                                   and not state.nodes[nid].tombstoned
                                   and state.nodes[nid].status is NodeStatus.evaluated
                                   and nid not in state.confirmed_forced), None)
            if legacy_confirm is not None:
                forced_confirm = {"node_id": legacy_confirm,
                                  "generation": state.nodes[legacy_confirm].attempt}
        if forced_confirm is not None:
            await self._confirm_node(state.nodes[forced_confirm["node_id"]])
            return True
        return False

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
        state = self._mirror_hypothesis_card_merges(state)

        # M6 comparative lessons, live-shared (doc 13 §7 items 2+5): on a node-count cadence,
        # distill credit-assigned PAIR lessons into the SHARED cross-run store DURING the run
        # (write side), and re-read the store so lessons distilled by CONCURRENT runs reach
        # this run's proposals (read side). The receipts do not re-rank current nodes, but they gate
        # paid cadence work and their shared-store output steers later proposals; replay-safe
        # (at_node gates), no-op when the cadences are 0.
        state = self._maybe_distill_lessons(state)
        state = self._maybe_refresh_lessons(state)

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

    def _spawn_research(self, tg, state: RunState) -> bool:
        """Overlap a DUE deep-research 'think' with the in-flight eval(s), INDEPENDENT of max_parallel.
        The memo is computed on a `state` snapshot in a worker thread, then RECORDED IMMEDIATELY when
        research finishes — NOT coupled to the eval completing — so its directions steer the very next
        proposal instead of landing ~an eval later. Recording from the research task is safe because
        `_record_deep_research` admits only `BACKGROUND_APPENDABLE` event types and
        `EventStore.append` serializes writers under an interprocess lock with collision-safe seq
        derivation. Those records never rewrite the current champion, but their hints/open hypotheses
        deliberately steer later proposals. No-op when concurrent_research is off.

        Two modes: the library default fires ONCE per window when a trigger is due (== today,
        byte-identical). With `concurrent_research_repeat` on, the overlapped think RE-RUNS on an
        adaptive time cadence for the whole eval window (`_research_overlap_loop`) so a multi-day
        training doesn't leave the reasoning agents idle after one memo — the caller cancels the
        loop when the evals join (see `_dispatch_evals`).

        RETURNS whether a research task was actually started, because the Card session's
        `research_spawned` latch is set from it. The latch used to be set unconditionally by the
        caller, which turned "we already started research for this eval window" into "we already
        ASKED whether research was due, once". Those differ exactly when the answer was NO — and on a
        long-eval GPU run that is the normal case: the cadence is counted in NODES, so the first
        admission of a session sits at `n=1` while `deep_research_every` is 3. Measured on
        `runs/rubert-dr-0807` (12-node budget, `deep_research_every=3`, `concurrent_research=True`,
        hours per node): the session asked at n=1, latched, then admitted n=2 and n=3 without ever
        re-asking, and the run recorded ZERO `research_attempted`/`research_completed` rows. The
        serial `_maybe_deep_research` could not cover it either — it requires no pending nodes, and
        under speculation there always are some. So the one feature built to use the idle reasoning
        agents during a multi-hour training never ran on the workload it exists for.

        That latch was HALF the defect. The other half was the window itself: even asked at every
        admission, `_due_research_trigger` answered NO until three nodes existed. The shipped default
        is now `deep_research_every=0` = no window at all (`engine/cadence.py::deep_research_window`),
        so the FIRST eval admission of the run — `n=1`, the first multi-hour training — is a due
        trigger and this method starts the think beside it. Nothing about the safety argument above
        changes: the same `BACKGROUND_APPENDABLE` allow-list, the same capped `deep_research` broker
        lane (`core/llm_broker.py::BACKGROUND_LANE_PRODUCERS`, one concurrent request), and the same
        containment for ordinary failures. The global `BudgetExceeded` hard stop still propagates.
        It just happens hours earlier."""
        if not self.concurrent_research:
            return False
        # repeat is a continuation of a research episode, not an independent timer.
        # Requiring a due cadence/strategist trigger here keeps a spelled-OFF cadence
        # (``deep_research_every=-1``; ``0`` has meant "start immediately" since 2026-08-07) truly
        # manual-only and prevents a long eval from silently starting paid research on its own.
        rtrig = self._due_research_trigger(state)
        if rtrig is None:
            return False
        # Defensive getattr: some tests build a partial Engine (no __init__) — a missing knob means
        # the safe one-shot default (== today), exactly like the train-monitor gates.
        if getattr(self, "_concurrent_research_repeat", False):
            # Repeat mode: keep researching for the whole eval window. Pass the initially-due trigger
            # so the FIRST pass fires promptly (matching one-shot promptness). `_due_research_trigger`
            # already rejects a missing model, so an unavailable stage cannot spin stub memos either.
            tg.start_soon(self._research_overlap_loop, rtrig)
            return True

        async def _bg(snap=state, trig=rtrig):
            # Best-effort ordinary errors must not disturb the in-flight eval. BudgetExceeded is the
            # global run hard stop and therefore must escape this task-group boundary.
            try:
                # Receipt first (the trigger gate must be spent BEFORE the provider call, or a kill
                # between the model answering and the memo landing buys the same think twice), then
                # the provider call, then the record — as ONE non-abandonable thread hop, never three
                # awaits. A worker thread has no cancellation points, so a sibling eval that finishes
                # (or raises) and unwinds this shared group cannot land a cancel BETWEEN spending the
                # gate and landing the memo: `_research_attempt_step` documents why that split was
                # not a rare-kill case but the normal path on any fast-eval task. Deliberately NOT
                # abandon_on_cancel (the default): the DeepResearcher owns a paid client and mutable
                # run-bound tools, and the record WRITES the event log — both must be joined before
                # the eval window closes.
                await anyio.to_thread.run_sync(
                    functools.partial(self._research_attempt_step, snap, trig, manual=False))
            except BudgetExceeded:
                raise
            except Exception:  # noqa: BLE001 — never let deep research disturb the eval
                pass
        tg.start_soon(_bg)
        return True

    def _research_repeat_cadence(self) -> float:
        """Base interval (seconds) between REPEATED concurrent-research passes. Research is expensive
        (multi-turn LLM + web/arXiv), so the config `concurrent_research_interval_s` is a FLOOR, not a
        ceiling: the effective pace is max(config, ~5% of the per-experiment time budget). A two-day
        eval is re-researched roughly hourly; a short eval's first tick outlasts it (so it fires once
        or not at all). Falls back to the config interval when no budget is known."""
        cfg = max(1.0, float(getattr(self, "_concurrent_research_interval_s", 1800.0) or 1800.0))
        budget = None
        fn = getattr(self, "_experiment_time_budget", None)
        if callable(fn):
            try:
                budget = fn()
            except Exception:  # noqa: BLE001 — cadence is advisory; a budget hiccup just uses the config
                budget = None
        if isinstance(budget, (int, float)) and not isinstance(budget, bool) and budget > 0:
            derived = min(3600.0, max(300.0, float(budget) * 0.05))
            return max(cfg, derived)         # research is costly: never MORE often than the config floor
        return cfg

    async def _research_overlap_loop(self, initial_trigger: Optional[str] = None) -> None:
        """Repeated concurrent deep-research: keep the reasoning agents productive for the WHOLE eval
        window (a multi-day training must not idle them after a single memo). Re-runs the overlapped
        think on an adaptive cadence, records ONLY memos whose content is NEW (identical re-runs are
        skipped so the log/hypothesis board don't bloat), backs off geometrically as the analysis
        converges (capped so it always re-checks — new sibling-eval results or cross-run lessons can
        land mid-window), and stops calling the LLM past the per-window cap. Its allowlisted
        BACKGROUND_APPENDABLE records are order-tolerant and never rewrite the current champion, while
        their hints/open hypotheses deliberately steer later proposals and are reconstructed by replay.
        Runs in `_dispatch_evals`'s background task group; cancelled when the evals join — which is
        exactly why each paid pass is a SINGLE indivisible `_research_attempt_step` hop."""
        from looplab.engine.train_monitor import next_monitor_sleep
        base = self._research_repeat_cadence()
        # Fire promptly if research was already due at spawn (one-shot promptness); else wait a full
        # cadence before the first deepening pass, so a short eval that outlasts no tick never researches.
        next_sleep = 0.0 if initial_trigger else base
        last_sig: Optional[str] = None
        converged = 0
        calls = 0
        cap = self._concurrent_research_max_calls
        trig = initial_trigger or "repeat"
        while True:
            await anyio.sleep(next_sleep)    # only cancellation (evals joined) unwinds the loop from here
            try:
                # Re-fold each tick (invariant #4): pick up sibling evals that finished + fresh hints.
                # This snapshot read is pure and owns no paid/shared role, so cancellation may abandon
                # only this read without permitting a late event/cost or rebinding run-scoped tools.
                snap = await anyio.to_thread.run_sync(
                    lambda: fold(self.store.read_all()), abandon_on_cancel=True)
                # Overlap the hypothesis-board CONSOLIDATION too (Phase 2): repeated research keeps
                # ADDING near-duplicate directions as open hypotheses, so dedup/merge them on the same
                # loop instead of only between nodes. `_maybe_merge_hypotheses` self-gates (open board
                # >= 4 AND grown >= 2 since its last pass) so it no-ops until there is something to
                # merge. This overlap is allowed only for legacy Hypothesis/Policy selection;
                # hypothesis_merged changes native Card ownership/readiness and therefore runs only
                # later on Card mode's joined main-task cadence. NOT abandon_on_cancel — this is
                # REQUIRED for safety, not style:
                # an abandoned merge worker could append EV_HYPOTHESIS_MERGED (and set _last_hyp_merge_n)
                # AFTER _dispatch_evals returns, concurrently with the main task's serial merge, which
                # is exactly the race the "background joined before _run_cadences" argument rules out.
                # So eval-join WAITS for an in-flight consolidate — one hybrid-retrieval + one
                # merge-decision LLM call, bounded by the endpoint timeout (comparable to the record
                # thread, not shorter). The self-gate keeps this rare: a converged tick whose board did
                # not grow no-ops fast. Runs before the research cap so a capped-out window still keeps
                # the board tidy. No-op when off / no reflect client / board small.
                if (getattr(self, "_concurrent_consolidate", False)
                        and not getattr(self, "card_driven_selection", False)):
                    await anyio.to_thread.run_sync(
                        functools.partial(self._maybe_merge_hypotheses, snap))
                if cap > 0 and calls >= cap:
                    return                   # research LLM budget spent; the health monitor still runs
                # Counted as an ATTEMPT, before the call rather than after it returns. Incrementing
                # only on success meant a provider that consistently RAISES (broken auth, endpoint
                # down, or a failure after tokens were already charged) never touched
                # `concurrent_research_max_calls` and was re-called every `base` seconds for the whole
                # eval window — the one budget backstop, blind to exactly the failure mode that can
                # spend money without producing anything.
                calls += 1
                # ONE hop for the whole paid pass: receipt -> provider -> record. Only the FIRST pass
                # carries the initially-due cadence/strategist trigger and thus a durable gate worth
                # receipting; `_record_research_attempt` no-ops for the `repeat` passes that follow
                # (their cadence is an in-process timer, not a folded marker).
                #
                # These three used to be three separate awaits, and the eval-join cancel below
                # (`_dispatch_evals`/`_run_card_session`'s `finally`) then landed on the leading
                # checkpoint of the RECORD hop: the gate was spent, the provider was paid AND waited
                # for, and the finished memo was discarded. Not a rare kill — the normal path on any
                # task whose evals finish faster than the research call (measured live: 4
                # `research_attempted` / 0 `research_completed` over a 12-node run). A worker thread
                # has no cancellation points, so folding the three into `_research_attempt_step` makes
                # the cancel arrive only AFTER the memo is durable — see that method for the full
                # argument and for why the shielded window stays bounded.
                #
                # Deliberately NOT abandon_on_cancel (unlike the pure reads above), for BOTH halves of
                # the step: the DeepResearcher owns a paid client and mutable run-bound tools, so
                # abandoning it permits post-finalization usage events and lets the next research pass
                # rebind the same tools under a live call; and the record WRITES the event log (and may
                # run a verify LLM pass), so abandoning it could append
                # research_completed/hint/hypothesis_added AFTER _dispatch_evals returns — possibly
                # past finalize. Waiting for the append (bounded, far shorter than the compute path)
                # is safer.
                sig, recorded = await anyio.to_thread.run_sync(
                    functools.partial(self._research_attempt_step, snap, trig,
                                      manual=False, last_sig=last_sig),
                    abandon_on_cancel=False)
                if sig is None:
                    next_sleep = base
                    continue
                if not recorded:             # converged — same conclusions; don't re-record, just back off
                    converged += 1
                    # cap = max(base, 3600): the backoff must never drop BELOW the configured interval
                    # FLOOR. next_monitor_sleep returns min(cap, base·2^k); with the default cap=3600 a
                    # base>3600 (user set interval_s>1h) would be clamped to 3600 < base, re-calling the
                    # LLM MORE often than the floor when converged. Raising the cap to base keeps the
                    # floor honoured (for base>3600 the sleep just stays at base — still bounded by the cap).
                    next_sleep = next_monitor_sleep(base, status="healthy", healthy_streak=converged,
                                                    cap=max(base, 3600.0))
                    continue
                last_sig = sig
                converged = 0
                next_sleep = base
                trig = "repeat"              # subsequent passes are repeats, not the initial due trigger
            except anyio.get_cancelled_exc_class():
                raise                        # cooperative cancellation (evals joined) — must propagate
            except BudgetExceeded:
                raise                        # global hard stop; never turn it into a retry tick
            except Exception:  # noqa: BLE001 — an advisory tick hiccup must not disturb the eval
                next_sleep = base
                continue

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
        async with anyio.create_task_group() as bg_tg:
            self._spawn_research(bg_tg, state)
            try:
                if self._eval_parallel <= 1:
                    limiter = anyio.CapacityLimiter(1)
                    for a in evals:
                        cur = fold(self.store.read_all())
                        if self._skip_if_aborted(a, cur):
                            continue
                        # Re-check the eval-compute budget BEFORE each eval (not just per loop
                        # iteration), so a multi-eval batch can't overshoot by a whole batch (#2/#25).
                        if (max_es is not None and cur.total_eval_seconds >= max_es):
                            break
                        node = cur.nodes.get(a["node_id"])
                        reservation = None
                        generation = None
                        skip_eval = False
                        if node is not None and hasattr(self, "_wait_reserve_node_resources"):
                            generation = node.attempt
                            while True:
                                # Resource waits must not pin a stale fold forever.  A GPU->CPU Card
                                # re-pin does not release a GPU (and therefore does not bump the pool
                                # epoch), so re-fold after every bounded condition tick and fence the
                                # exact lifecycle plus run-level operator gates before retrying.
                                # with the cross-run host lease this wait is no longer
                                # bounded by a sibling eval in this process — it lasts as long as
                                # ANOTHER run holds the pool (hours of training), and every 0.5s tick
                                # re-folds the WHOLE log (the parallel branch folds twice per tick):
                                # an O(total-events) busy-poll, the same cost confirm F26 documents.
                                # The stated reason (a re-pin doesn't bump the pool epoch) doesn't
                                # need an unconditional fold — a re-pin always APPENDS, so gate the
                                # re-fold on the tail seq having changed, or lengthen the idle tick.
                                waiting = fold(self.store.read_all())
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
                                admitted = fold(self.store.read_all())
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
                        try:
                            await self._evaluate(a["node_id"], limiter, max_es)
                        finally:
                            if reservation is not None and generation is not None:
                                self._clear_eval_resource_reservation(a["node_id"], generation)
                                self._release_gpus(reservation.get("gpu_ids"))
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
                    # `bg_tg`'s lifecycle and every `pending_nodes()`-keyed guarantee are unchanged.
                    slots = anyio.Semaphore(self._eval_parallel, fast_acquire=True)

                    async def _eval_in_slot(nid: int, generation: Optional[int],
                                            reservation: Optional[dict]) -> None:
                        try:
                            # A private single-token limiter -> `_evaluate`'s `async with limiter` is a
                            # no-op; the outer semaphore is what bounds fan-out and drives the refill.
                            await self._evaluate(nid, anyio.CapacityLimiter(1), max_es)
                        finally:
                            if reservation is not None and generation is not None:
                                self._clear_eval_resource_reservation(nid, generation)
                                self._release_gpus(reservation.get("gpu_ids"))
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
                            # overshoot is bounded to the ~max_parallel evals already in flight.
                            # CODEX AGENT: a "hard cumulative" budget cannot count only completed
                            # charges: every lane can enter under the same remaining balance and each
                            # timeout may exceed it. Reserve the worst-case/time-bounded charge atomically
                            # at admission, then release the unused portion when the evaluation settles.
                            if (max_es is not None and cur.total_eval_seconds >= max_es):
                                break
                            await slots.acquire()     # blocks only when the pool is full -> the refill point
                            # the pre-check above may be minutes old after a genuine refill
                            # wait. Re-fold while owning the freed slot so a sibling that crossed the hard
                            # eval budget (or an operator abort) cannot be followed by one more admission.
                            cur = fold(self.store.read_all())
                            if max_es is not None and cur.total_eval_seconds >= max_es:
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
                            elif scan is not pending and slots.value >= self._eval_parallel - 1:
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
                            try:
                                tg.start_soon(_eval_in_slot, chosen["node_id"], generation,
                                              chosen_reservation)
                            except BaseException:
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
    # leading underscore>`. All 32 follow that naming rule exactly, which is what makes the table
    # checkable rather than decorative.
    #
    # It exists because of ONE named cost, and it removes exactly that one: a new `LessonMemory` /
    # `HoldoutGrader` / `Workspace` method needs a hand-written forwarder carrying the correct
    # `@in_llm_lane`, and eight of the thirty-two carry one. Forgetting the lane does not fail —
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
        "_write_reflection_note": ("lessons", "enrichment"),
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
        "_store_concept_curation": ("lessons", "enrichment"),
        "_store_claim_curation": ("lessons", "enrichment"),
        "_store_task_facets": ("lessons", "enrichment"),
        # --- holdout (looplab/trust/holdout.py::HoldoutGrader)
        "_graded_output_name": ("holdout", None),
        "_apply_host_grade": ("holdout", None),
        "_host_score_split": ("holdout", None),
        "_build_holdout_idx": ("holdout", None),
        "_holdout_topk": ("holdout", None),
        "_holdout_pending": ("holdout", None),
        # --- workspace (looplab/engine/workspace.py::Workspace)
        "_write_assets": ("workspace", None),
        "_write_node_files": ("workspace", None),
        "_materialize": ("workspace", None),
        "_workspace_fingerprint": ("workspace", None),
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

    @in_llm_lane("enrichment")
    def _store_concept_curation(self, final: RunState) -> str:
        return self.lessons.store_concept_curation(final)

    @in_llm_lane("enrichment")
    def _store_claim_curation(self, final: RunState) -> str:
        return self.lessons.store_claim_curation(final)

    @in_llm_lane("enrichment")
    def _store_task_facets(self, final: RunState) -> str:
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

    def _consume_batch_proposal(self, state, width: int):
        """Run one batched proposal and READ its three-attribute result. Returns
        ``(ideas, telemetry, dropped)``.

        `_propose_batch` (novelty.py) signals its results through three instance attributes rather
        than a return value: `_pending_batch_telemetry`, `_pending_batch_dropped` and
        `_pending_batch_novelty_gated`. Two call sites — `run`'s concurrent-build chunk and
        `_stage_card_creates` — each read that protocol by hand, including the padding rule and the
        snapshot-before-reset ordering (doc 25 ES-08).

        The padding is load-bearing: telemetry must align 1:1 with `ideas` so each build emits ITS
        OWN hypothesis_ranked/foresight_selected. A short list silently shifts every later idea's
        telemetry onto the wrong node.

        Both `dropped` and `telemetry` are SNAPSHOTTED (copied) here, so a caller may reset the
        attributes at whatever point its own durability ordering requires without losing what it is
        about to record. Resetting stays at the call sites precisely because that ordering differs:
        `run` clears after the reservations are durable, `_stage_card_creates` clears in a `finally`.
        """
        # The BATCH proposal's progress beacon, and the one that matters most: on the shipped default
        # width this — not `_prepare_node_idea` from `_create_node_scoped` — is the path a run
        # actually takes, and it is the single longest wholly invisible stretch in the loop. It runs
        # before any node id exists, so no `node_building` marker has been appended and the UI has
        # literally nothing to draw; the strip falls through to "Planning next experiment…" for the
        # entire Researcher call. This is the funnel BOTH batch call sites go through (doc 25 ES-08),
        # which is why the beacon belongs here rather than duplicated at each.
        #
        # No `node_id`: a batch proposes `width` ideas at once and none of them has an id yet.
        # Emitting a prospective one would name a node that most of these ideas will not become.
        # `count` is the honest shape, and a beacon without a node_id is the run-level phase it is.
        with self._progress(PROGRESS_STAGE_BUILD, "propose", count=int(width)):
            ideas = self._propose_batch(state, width)
        telemetry = list(getattr(self, "_pending_batch_telemetry", None) or [])
        if len(telemetry) < len(ideas):
            telemetry.extend([None] * (len(ideas) - len(telemetry)))
        dropped = list(getattr(self, "_pending_batch_dropped", None) or [])
        return ideas, telemetry, dropped

    def _fail_reserved_build(self, *, node_id: int, card_id: Optional[str], generation: int,
                             reason: str, error: str, drop_card: bool = True,
                             never_evaluated: bool = False) -> None:
        """Close a pre-node reservation and, when bare, its immutable Card work item.

        A terminal on a bare ``node_building`` clears the transient marker but creates no Node evidence.
        Without the paired card_auto_dropped receipt that Card would resurrect as a fresh proposed item after
        replay.  Existing-node reruns pass ``drop_card=False`` because they reuse the original lifecycle.

        ``never_evaluated`` stamps the durable pre-dispatch receipt (``Node.never_evaluated``) that the
        L5 node-budget refund is proven from.  It stays OPT-IN and defaults False: only a caller that
        can show, from the state it just folded, that no evaluation was ever dispatched for this exact
        lifecycle may claim it — an ordinary failed/aborted build keeps counting exactly as before.

        ``drop_card`` is a caller's INTENT and is no longer sufficient on its own. Since the `attach`
        disposition (2026-08-12) a reservation's ``card_id`` is not always one that reservation
        minted, and every close path across `orchestrator`/`speculation`/`ablation` reaches here —
        one of them `_recover_interrupted_builds`, which for an interrupted build has no Node to read
        and so cannot see the difference at all. `_reservation_minted_card` is the ownership half,
        and it is
        checked HERE rather than at each site precisely because the sites do not know: the same
        intent that is right for a bare first build ("close the work item nobody will ever own")
        deleted the PARENT's card from the board after one interrupted repair.
        """
        # Fail closed first. If the process dies between these two appends, the still-live build marker
        # makes recovery retry the terminal, while the Card is already non-selectable. Skip an existing
        # drop receipt so that prefix recovery remains idempotent.
        if card_id and drop_card and not self._reservation_minted_card(
                self.store.read_all(), node_id, card_id):
            # An attached repair, or a card another node's durable idea already names. The
            # reservation still gets its terminal below; the work item it JOINED stays on the board.
            drop_card = False
        if card_id and drop_card:
            self._drop_card_once(card_id, reason=reason)
        payload = {
            "node_id": node_id,
            "generation": generation,
            "error": error,
            "reason": reason,
            "eval_seconds": 0.0,
        }
        if card_id:
            payload["card_id"] = card_id
        if never_evaluated:
            payload["never_evaluated"] = True
        self.store.append(EV_NODE_FAILED, payload)

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
        classification, regression, timeseries — both probes therefore read the same client-less
        template and the whole product default answered "no LLM" while calling the provider once per
        node. Measured on `examples/classification_task.json` with stock Settings: `run_started`
        recorded no `speculation_depth` at all (AUTO had settled to 0) even though the run's own
        `llm_usage` rows show the Researcher on the wire before the first node existed. So descend
        into the facade's own per-stage backends as well.
        """
        seen: list = []
        for role in (getattr(self, "researcher", None), getattr(self, "developer", None)):
            if role is None or any(role is other for other in seen):
                continue
            seen.append(role)
            try:
                if getattr(role, "client", None) is not None:
                    return True
                if getattr(role, "is_code_generating", False):
                    return True
                # A composing facade (UnifiedAgent) exposes its stages PUBLICLY, for the same reason
                # the cost roll-up walks them: `researcher`/`developer` are the per-stage backends and
                # `stage_clients` holds the clients no backend owns (strategy, pilot). Guard against
                # self-reference so a role that names itself cannot loop.
                for stage in (getattr(role, "researcher", None), getattr(role, "developer", None)):
                    if stage is None or stage is role:
                        continue
                    if (getattr(stage, "client", None) is not None
                            or getattr(stage, "is_code_generating", False)):
                        return True
                if any(client is not None for client in (getattr(role, "stage_clients", None) or ())):
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
        return min(64, max(1, resolved))

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
                or getattr(self, "_policy_name", "") != SPECULATION_POLICY_SCOPE
                or not self.run_dir.name.strip()):
            # Report AUTO=False as well: the run is not speculating, so there is no AUTO treatment
            # for re-entry to adopt, and a log that pinned a positive depth must still fail closed
            # here rather than silently adopting it into a policy or a role set the treatment was
            # never measured on.
            return 0, False
        return min(64, max(1, int(self._eval_parallel))), True

    def _reconfigure_llm_broker(self, value) -> None:
        """Apply one live canonical total without replacing a broker held by active borrowers."""
        if isinstance(value, bool):
            return
        if isinstance(value, float) and (
                not math.isfinite(value) or not value.is_integer()):
            return
        try:
            # Live Strategist/operator zero is a finite safety floor (1), not startup AUTO. This
            # matches the canonical runtime contract and avoids surprising GPU-count re-resolution.
            raw_total = int(value)
        except (TypeError, ValueError, OverflowError):
            return
        # this method is also a defensive resume boundary for manually-constructed or
        # forward-version state. Never turn an invalid/huge value into a different valid paid-call cap.
        if not 0 <= raw_total <= 64:
            return
        total = max(1, raw_total)
        broker = getattr(self, "_llm_broker", None)
        if broker is None:
            self._llm_broker = LLMConcurrencyBroker(
                total=total, lane_limits=default_llm_lane_limits(total))
            return
        snapshot = broker.snapshot()
        current_lanes = snapshot["lane_limits"]
        # A total-only live delta must not erase a prior Strategist/operator lane allocation (and a
        # persistent budget override is re-applied every loop). Recompute the work-conserving defaults
        # only until a validated Strategy has explicitly owned the split; otherwise retain that split.
        next_lanes = (current_lanes if getattr(self, "_llm_lane_limits_explicit", False)
                      else default_llm_lane_limits(total))
        broker.reconfigure(total=total, lane_limits=next_lanes)

    def _build_role_pairs(self, n: int) -> list:
        """Up to `n` (researcher, developer) pairs for a parallel build batch: the primary (self's roles)
        plus fresh WIRED pairs from `role_factory`, cached in `self._role_pool` and reused across batches
        (each pair's per-build state — developer.last_files, researcher hints — is captured at node_created
        before the next batch reuses it, so reuse is safe). `role_factory` None or `n<=1` -> just the
        primary pair, and the caller stays serial. Fresh pairs are what isolate per-build role state so
        concurrent drafts don't clobber each other."""
        if n <= 1 or self.role_factory is None:
            return [(self.researcher, self.developer)]
        if self._role_pool is None:
            self._role_pool = []
        while len(self._role_pool) < n - 1:
            try:
                pair = self.role_factory()
            except Exception:  # noqa: BLE001 — a factory failure just caps fan-out, never crashes the run
                break
            if not (isinstance(pair, tuple) and len(pair) == 2):
                break
            if self._pool_developer_override is not None and self.developer_factory is not None:
                try:
                    pair = (pair[0], self.developer_factory(self._pool_developer_override))
                except Exception:  # noqa: BLE001 - cap fan-out if the selected backend cannot be built
                    break
            self._role_pool.append(pair)
        # workers are constructed lazily, after Engine.__init__ bound the primary role
        # graph. Attach every newly reachable accountant before the first concurrent paid request.
        bind_cost_accountants(self)
        return [(self.researcher, self.developer)] + self._role_pool[: n - 1]

    def _prepare_node_idea(self, action: dict, state: RunState, *, researcher,
                           prospective_node_id: int, source: str,
                           proposal_events=None, preproposed=None) -> Optional[Idea]:
        """Finish the concrete Idea before Card/node reservation, without implementing code.

        A native ownership receipt binds the final operator/params/space/profile/footprint, so the
        old reserve-before-propose ordering cannot produce an honest Card.  This helper is the moved
        proposal half of ``_create_node``; every Developer call remains after durable reservation.
        """
        kind = action["kind"]
        events = list(proposal_events) if proposal_events is not None else self.store.read_all()
        try:
            setattr(researcher, "_steering_context", [])
        except Exception:  # noqa: BLE001 - wrappers may expose a read-only compatibility surface
            pass
        parent_snapshot = self._build_parent_snapshot(state, action)
        if parent_snapshot is None:
            return None
        _kind, parents, parent_generations = parent_snapshot

        def _link(candidate, *, proposed: bool = True) -> Optional[Idea]:
            if candidate is None:
                return None
            # The proposal path's provider circuit breaker, at the ONE funnel every proposal
            # (draft/improve/debug and a preproposed batch idea) passes through before a Card or a
            # node id exists. A degraded FALLBACK is the ABSENCE of a proposal, so nothing downstream
            # — the Card statement, the hypothesis board, the node rationale, the cross-run case —
            # may be minted from it. See `_refuse_degraded_proposal`.
            #
            # `proposed=False` for the two MECHANICAL ideas, which no Researcher authored: the merge
            # operator's mean/ensemble Idea, and the repair path's copy of the failing parent's Idea.
            # The copy is why this flag exists rather than an unconditional check — it inherits the
            # PARENT's rationale, so replaying or resuming a log written before this change (one whose
            # nodes already carry `fallback (…)` rationales, e.g. `/tmp/ll-s4b/run`) would debug such a
            # node and raise a provider pause naming a failure that is not happening now.
            if proposed and self._refuse_degraded_proposal(candidate, main_task=False):
                return None
            linked = (candidate if isinstance(candidate, Idea)
                      else Idea.model_validate(candidate)).model_copy(deep=True)
            linked.card_id = None  # a Researcher/plugin can never claim writer namespace authority
            if self._speculation_gate_calibration:
                # Mechanical merge/debug Ideas do not pass through ToyResearcher, but they are still
                # members of the calibrated workload. Keep every physical node inside the same
                # one-GPU resource/provenance envelope without changing ordinary Idea bytes.
                linked.footprint = {"gpus": 1}
                linked.concept_mode = "full"
                linked.concepts = [
                    f"operator/{_kind}", "objective/quadratic", "space/two-dimensional"]
                linked.concepts_added = []
                linked.concepts_removed = []
            # Bind the Card and the durable Node to the action that execution will actually honor.
            # Keeping the model-requested value here would make a 3600s request with a 90s ceiling
            # appear as 3600s in both receipts even though eval_dispatch runs only 90s.
            linked.eval_timeout = self._effective_researcher_eval_timeout(linked)
            steering_context = normalize_steering_context(
                getattr(researcher, "_steering_context", []))
            if steering_context is None:
                return None
            plan = self._plan_native_card(
                events, state, linked, parents=parents, parent_generations=parent_generations,
                scored_against=state.best_node_id, source=source, at_node=prospective_node_id,
                steering_context=steering_context,
                # The proposal half of the build spine, matching `_create_node_scoped`'s
                # `_reserve_node_build(retry_attach=True)`. This pass runs OUTSIDE `_id_lock`, so the
                # two can genuinely disagree when a `card_dropped`/`card_merged`/terminal lands
                # between them; the commit pass is the authority and now RESOLVES that race instead
                # of returning None and losing the turn in silence (see the fence there).
                retry_attach=True,
            )
            if plan.disposition == "invalid":
                self._append_proposal_event(EV_NOVELTY_REJECTED, {
                    "node_id": prospective_node_id, "generation": 0,
                    "kind": "card_contract",
                    "reason": "proposal cannot form a bounded native Card action",
                    "action": "dropped",
                })
            return plan.idea if plan.disposition in {"mint", "reuse", "attach"} else None

        if preproposed is not None:
            already_gated = False
            pending_batch = getattr(self, "_pending_batch_novelty_gated", None)
            if isinstance(pending_batch, list):
                for index, batch_idea in enumerate(pending_batch):
                    if preproposed is batch_idea:
                        # Consume the capability exactly once.  Equality is intentionally insufficient:
                        # a direct plugin/caller proposal that happens to match a batch result has not
                        # itself crossed the proposal-bound gate.
                        del pending_batch[index]
                        already_gated = True
                        break
            candidate = (self._canonicalize_draft_idea(preproposed)
                         if kind == "draft" else preproposed)
            linked = _link(candidate)
            if linked is None or kind in {"merge", "debug"}:
                return linked
            if already_gated:
                return linked
            # Direct callers may supply a concrete proposal without a batch reservation. Resolve its
            # final writer-owned Card id first, then run the same proposal-bound novelty sidecar as the
            # ordinary draft/improve path. Reserved parallel batches bypass this helper entirely: their
            # shared proposal pass has already applied the gate.
            with self._progress(PROGRESS_STAGE_BUILD, "novelty",
                                node_id=prospective_node_id, prospective=True, operator=kind):
                final = self._apply_novelty_gate(
                    state, linked, researcher=researcher,
                    prospective_node_id=prospective_node_id,
                )
            return _link(final)

        if kind == "draft":
            self._set_complexity_hint(state, None, researcher=researcher)
            with self.tracer.span("propose") as _span:
                idea = _link(self._canonicalize_draft_idea(researcher.propose(state, None)))
                stamp_proposal_span(_span, idea, node_id=prospective_node_id)
            if idea is None:
                return None
            with self._progress(PROGRESS_STAGE_BUILD, "novelty",
                                node_id=prospective_node_id, prospective=True, operator=kind):
                final = self._apply_novelty_gate(
                    state, idea,
                    repropose=lambda: _link(self._canonicalize_draft_idea(
                        researcher.propose(state, None))),
                    researcher=researcher, prospective_node_id=prospective_node_id)
            return _link(final)

        if kind == "merge":
            parents = list(action["parent_ids"])
            pnodes = [state.nodes[node_id] for node_id in parents]
            return _link(self._ensemble_idea(pnodes) if self._merge_mode == "ensemble"
                         else merge_idea(pnodes), proposed=False)

        parent = state.nodes[action["parent_id"]]
        if kind == "debug":
            # REFUSED (F5). This branch used to copy the failed parent's own Idea onto a NEW node
            # and hand it back to the Developer — the Debug node, i.e. another attempt at the
            # experiment that just failed, paid for out of the node budget. `None` is the answer
            # this function already gives for "no idea could be prepared", so the caller declines
            # the build without a new failure mode; the loop-level filter above is what normally
            # stops such an action reaching here at all, and this is the second door.
            return None

        # improve / capability-expand
        self._set_complexity_hint(state, parent, researcher=researcher)
        authoritative_operator = "improve"
        if (getattr(self, "_capability_expansion", False)
                and getattr(self, "_novelty_stance", None) == "explore"):
            from looplab.engine.proposal_cues import _LOCK_IN_STREAK
            from looplab.search.lock_in import capability_expansion_due
            if capability_expansion_due(state, streak_threshold=_LOCK_IN_STREAK)[0]:
                authoritative_operator = KIND_EXPAND
        with self.tracer.span("propose") as _span:
            idea = _link(self._canonicalize_idea_operator(
                researcher.propose(state, parent), authoritative_operator))
            stamp_proposal_span(_span, idea, node_id=prospective_node_id)
        if idea is None:
            return None
        with self._progress(PROGRESS_STAGE_BUILD, "novelty",
                            node_id=prospective_node_id, prospective=True, operator=kind):
            final = self._apply_novelty_gate(
                state, idea,
                repropose=lambda p=parent: _link(self._canonicalize_idea_operator(
                    researcher.propose(state, p), authoritative_operator)),
                researcher=researcher, prospective_node_id=prospective_node_id)
        return _link(final)

    @in_llm_lane("build")
    def _create_node(self, action: dict, roles=None, reserved=None, preproposed=None,
                     pretelemetry=None, precoded=None,
                     precoded_max_eval_seconds: Optional[float] = None) -> None:
        """Run proposal, reservation and implementation in one node-scoped handoff context."""
        from looplab.agents.agent import handoff_scope

        if reserved is not None:
            trace_node_id = reserved.node_id
        else:
            trace_events = self.store.read_all()
            trace_state = fold(trace_events)
            trace_node_id = self._node_id_ceiling(trace_events, trace_state)
        with self.tracer.span(
                "create_node", new_trace=True, node_id=trace_node_id,
                generation=0, operator=action.get("kind")), \
                handoff_scope(enabled=self._phase_handoff_summary):
            if precoded is not None:
                # Layer 5: the isolated producer already completed every slow role call.  Keep the
                # ordinary path below literally unchanged; this main-task branch only commits the
                # exact buffered result and its durable speculative marker.
                return self._create_precoded_node(
                    action,
                    reserved,
                    precoded,
                    max_eval_seconds=precoded_max_eval_seconds,
                )
            return self._create_node_scoped(
                action, roles, reserved, preproposed=preproposed,
                pretelemetry=pretelemetry)

    def _create_node_scoped(self, action: dict, roles=None, reserved=None, preproposed=None,
                            pretelemetry=None) -> None:
        # Variant-1 parallel build: `roles` is a per-build (researcher, developer) pair from the pool
        # (isolated per-build state so concurrent drafts don't clobber each other's hints/last_files);
        # `reserved` is a pre-reserved (state, id, kind, parents, parent_generations) tuple (the parallel
        # path reserves ids up front, serially, then fans out). `preproposed` (Phase 2) is a draft Idea
        # the shared researcher already proposed + novelty-gated in the batch pass (`_propose_batch`), so
        # the fan-out only IMPLEMENTS it. All default to the serial behaviour.
        researcher, developer = roles if roles is not None else (self.researcher, self.developer)
        if reserved is None:
            proposal_events = self.store.read_all()
            proposal_state = fold(proposal_events)
            if self._build_parent_snapshot(proposal_state, action) is None:
                return
            prospective_node_id = self._node_id_ceiling(proposal_events, proposal_state)
            source = "engine" if action.get("kind") == "merge" else "researcher"
            # The PROSPECTIVE id, not a real one: this whole phase runs before `node_building` is
            # appended, which is the reason it is the invisible one — the UI has no node to draw yet,
            # so it falls through to "Planning next experiment…" for however long the Researcher
            # takes. `prospective: True` marks the id as the ceiling's guess so a reader never treats
            # it as a committed node; the `reserve` beacon below carries the id that was actually
            # taken, and the two agree except when a concurrent build wins the id first.
            with self._progress(PROGRESS_STAGE_BUILD, "propose", node_id=prospective_node_id,
                                prospective=True, operator=action.get("kind")):
                idea = self._prepare_node_idea(
                    action, proposal_state, researcher=researcher,
                    prospective_node_id=prospective_node_id,
                    source=source, proposal_events=proposal_events, preproposed=preproposed)
            if idea is None:
                self._discard_node_build_telemetry(researcher=researcher, developer=developer)
                return
            steering_context = normalize_steering_context(
                getattr(researcher, "_steering_context", []))
            if steering_context is None:
                self._discard_node_build_telemetry(researcher=researcher, developer=developer)
                return
            with self._progress(PROGRESS_STAGE_BUILD, "reserve", node_id=prospective_node_id,
                                prospective=True, operator=action.get("kind")):
                reserved = self._reserve_node_build(
                    action, idea, scored_against=proposal_state.best_node_id,
                    source=source, steering_context=steering_context,
                    # The ordinary build spine, and the ONE site that commits an attach. `_link` above
                    # planned with the same flag, so a `debug` re-attempt of a question card-N already
                    # asks becomes another node under card-N instead of a byte-identical twin. Spelled
                    # here rather than defaulted inside the reservation: four other callers reach that
                    # method and none of them may attach (see `_plan_native_card`).
                    retry_attach=True)
        if reserved is None:
            self._discard_node_build_telemetry(researcher=researcher, developer=developer)
            return
        state = reserved.state
        node_id = reserved.node_id
        kind = reserved.kind
        parent_generations = reserved.parent_generations
        idea = reserved.idea.model_copy(deep=True) if reserved.idea is not None else None
        if idea is None:
            # Legacy direct reservation: retain the historical behavior for internal callers, but do
            # not pretend it produced a native Card. Production paths always prepare before reserve.
            idea = self._prepare_node_idea(
                action, state, researcher=researcher,
                prospective_node_id=node_id,
                source="engine" if action.get("kind") == "merge" else "researcher",
                proposal_events=self.store.read_all(), preproposed=preproposed)
            if idea is None:
                self._discard_node_build_telemetry(researcher=researcher, developer=developer)
                return
        # Phase-handoff ledger for THIS node build: propose → stages → plan → implement each distill
        # their transcript into a brief the next phase reads (see agents.agent.run_phase), so later
        # phases trust what earlier ones explored instead of re-reading the repo. Node-scoped (fresh
        # per build), and a no-op when the setting is off.
        with self.tracer.span("materialize_node", node_id=node_id, operator=kind):
            # node_building was appended inside _reserve_node_build (under _id_lock) — the id is committed
            # to the log atomically, so a PARALLEL build (parallel_build>1) can never pick the same id.
            # Restore THIS pre-proposed idea's own FOREAGENT telemetry after main-task reservation and
            # before the worker's audit emitters consume it.
            if pretelemetry:
                for _attr, _val in pretelemetry.items():
                    if _val is not None:
                        try:
                            setattr(researcher, _attr, _val)
                        except Exception:  # noqa: BLE001
                            pass
            # Per-call output: never let a reused wrapper/backend leak another node's resource
            # finalization into this build.  The exact pooled Developer is cleared and read below.
            self._reset_developer_footprint(developer)
            if kind == "draft":
                parents: list[int] = []        # not whatever label the LLM returns
                # The progress beacon rides ON the existing tracer span rather than nesting inside
                # it: same boundary, same body, no reindentation of code whose comments are
                # load-bearing. The span feeds `spans.jsonl` (a per-node trace an operator opens
                # after the fact); the beacon feeds the live strip, which is the surface that was
                # blank while this call ran.
                with self.tracer.span("implement"), self._progress(
                        PROGRESS_STAGE_BUILD, "implement", node_id=node_id, operator=kind):
                    code = self._implement(
                        self._directed_idea(idea.model_copy(deep=True), state),
                        developer=developer, state=state)
            elif kind == "merge":
                parents = list(action["parent_ids"])
                # A0b: real ensembling (code recombination) when configured/Strategist-selected;
                # else the legacy mean-param merge. Toy/baseline developers degrade to mean.
                pnodes = [state.nodes[i] for i in parents]
                with self.tracer.span("implement"), self._progress(
                        PROGRESS_STAGE_BUILD, "implement", node_id=node_id, operator=kind):
                    # A code-ensemble merge must SEED from the primary parent's solution (like improve),
                    # not implement() from scratch: from-scratch gave the Developer no base, so the
                    # ensemble node shipped without the agent-authored eval entrypoint and crash-failed
                    # ("can't open file test_looplab.py" — live node 63, 3 repairs couldn't recover). Now
                    # parent[0]'s working code + entrypoint carry over and the idea directs blending in
                    # the other parent. Mean-param merges (numeric tasks, no files) stay from-scratch.
                    _didea = self._directed_idea(
                        idea.model_copy(deep=True), state)   # §1: directives steer the merge code too
                    code = self._implement(
                        _didea,
                        pnodes[0] if self._merge_mode == "ensemble" and pnodes else None,
                        developer=developer, state=state)
            # The `debug` build branch is GONE (F5). It called `developer.repair` on a FRESH node
            # seeded from the failed parent's files — inline repair with a node-budget slot attached
            # to it. Its whole justification was that the in-node loop had a fixed count and had to
            # hand off somewhere when the count ran out; F8 removed the count, so the hand-off has
            # nowhere to go and no reason to exist. `_prepare_node_idea` refuses the kind before a
            # build is ever reached, so this branch was unreachable as well as unwanted.
            else:  # improve
                parent = state.nodes[action["parent_id"]]
                parents = [parent.id]
                with self.tracer.span("implement"), self._progress(
                        PROGRESS_STAGE_BUILD, "implement", node_id=node_id, operator=kind):
                    code = self._implement(
                        self._directed_idea(idea.model_copy(deep=True), state), parent,
                        developer=developer, state=state)
            idea, footprint_finalized = self._finalize_developer_footprint(
                idea, developer, code)
            # 💡 deep-research provenance: tag the first couple of nodes created right after a research
            # memo (its directions are the active steering) so the UI can show WHERE research landed in
            # the tree. Audit/UI only — never affects search. Coarse-but-honest (temporal proximity).
            research_origin = None
            if state.research:
                _m = state.research[-1]
                _ra = _m.get("at_node")
                if _ra is not None and _ra <= node_id < _ra + 2:
                    from looplab.core.advisory_payloads import valid_advisory_ref
                    _memo_id = _m.get("memo_id")
                    research_origin = {
                        "at_node": _ra,
                        "trigger": _m.get("trigger"),
                        **({"memo_id": _memo_id}
                           if valid_advisory_ref(_memo_id, "memo") else {}),
                    }
            latest = fold(self.store.read_all())
            if any(pid not in latest.nodes
                   or latest.nodes[pid].attempt != generation
                   or latest.nodes[pid].tombstoned
                   or pid in latest.aborted_nodes
                   for pid, generation in ((int(pid), gen)
                                           for pid, gen in parent_generations.items())):
                # Clear both the transient node owner and its immutable, now-unbuildable Card.
                self._fail_reserved_build(
                    node_id=node_id, card_id=reserved.card_id, generation=0,
                    error="parent lifecycle changed while building", reason="superseded")
                self._discard_node_build_telemetry(researcher=researcher, developer=developer)
                return
            materialize_abort = node_id in state.aborted_nodes
            self._emit_node_created(
                node_id=node_id,
                parent_ids=parents,
                operator=idea.operator,
                idea=durable_idea_payload(idea),
                code=code,
                files=getattr(developer, "last_files", {}) or {},         # per-build developer (pool-safe)
                deleted=getattr(developer, "last_deleted", []) or [],
                research_origin=research_origin,
                # Variant-1: read the receipt THIS build stamped on its own researcher (set under
                # `_advisory_lock` in `_set_complexity_hint`), so a concurrent sibling draft's advisory
                # write to `self._cross_run_advisory_receipt` can't mis-stamp this node. Falls back to
                # the shared attr only when a path never refreshed it (attr genuinely absent).
                cross_run_receipt=(_rcpt if (_rcpt := getattr(researcher, "_cross_run_advisory_receipt", None))
                                   is not None else getattr(self, "_cross_run_advisory_receipt", {})),
                **({"parent_generations": parent_generations} if parent_generations else {}),
                **({"footprint_finalized": True} if footprint_finalized else {}),
                # A legacy generation-less abort may intentionally reserve a not-yet-created slot.
                # Mark only an intent already present in the reservation snapshot. An abort that lands
                # after node_building is a losing-worker race and deliberately gets no escape hatch.
                **({"materialize_aborted_intent": True}
                   if materialize_abort else {}),
            )
            if node_id not in fold(self.store.read_all()).nodes:
                self._fail_reserved_build(
                    node_id=node_id, card_id=reserved.card_id, generation=0,
                    error="node creation was rejected during replay", reason="superseded")
                self._discard_node_build_telemetry(researcher=researcher, developer=developer)
                return
            if materialize_abort:
                # Preserve the already-recorded operator intent as the first terminal for this newly
                # materialized lifecycle. This also keeps a Developer-error sentinel from stealing the
                # terminal with an unrelated crash/pause after the operator had already cancelled it.
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node_id, "generation": 0,
                    "error": "aborted by operator",
                    "reason": "aborted", "eval_seconds": 0.0,
                })
            # The Developer session CRASHED when its code is the "(developer error: …)" sentinel (an
            # exception in _run — e.g. an LLM 401/timeout). FAIL the node now: without this it stays
            # pending, and the eval runs the PARENT's carried-over entrypoint and inherits the PARENT's
            # metric — a false success that pollutes the search (the 401-window nodes 50-54 each faked
            # the parent's 0.81 this way). node_created → node_failed keeps the one-terminal invariant.
            elif is_developer_error(code):
                # Terminal only — see `_request_create_pause` below for why the pause is queued.
                crash_terminal, _crash_pause = developer_crash_records(
                    node_id, 0, code,
                    "auto-paused: a Developer session crashed (LLM unreachable or a hard error, "
                    "unresolved within the node) — resume once it's fixed")
                self.store.append(crash_terminal[0], crash_terminal[1])
                # Circuit-breaker — PAUSE on the FIRST developer_crash. A developer_crash means the
                # Developer couldn't finish THIS node even after the LLM client's own within-call retries
                # (429 / 5xx / throttle-403 all back off + retry): a problem that a NEW node can't fix
                # (LLM unreachable, or a hard error), NOT a bad experiment. One node = one experiment; if
                # it can't be resolved within the node, stop the whole run rather than rapid-fire more
                # dead nodes (the 403 blowout spun 67 of them). Freeze (not finish) so a plain `resume`
                # continues once the cause is resolved — no premature report/lessons.
                # REQUESTED, not appended here. On the parallel-build fan-out (`_pb_pairs`) this
                # method runs in an `anyio.to_thread` WORKER thread, and EV_PAUSE is a FOLDED,
                # run-GLOBAL, selection-affecting event — outside invariant #1's documented worker
                # seam (a worker may append only its OWN node's node_created / node_failed /
                # per-node audit) and not in BACKGROUND_APPENDABLE or DIAGNOSTIC_EVENTS. It is
                # splice-neutral for the `paused` flag alone, but NOT against a concurrent EV_RESUME
                # (which folds `paused=False`): a worker's byte position relative to an external
                # control is nondeterministic. `_request_create_pause` records the intent; the MAIN
                # task appends it where it already observes `_create_paused`, after the join.
                self._request_create_pause(
                    node_id,
                    "auto-paused: a Developer session crashed (LLM unreachable or a hard error, "
                    "unresolved within the node) — resume once it's fixed")
        self._consume_node_build_telemetry(
            node_id, 0, researcher=researcher, developer=developer)

    def _consume_node_build_telemetry(self, node_id: int, generation: int,
                                      *, researcher=None, developer=None) -> None:
        """Attribute this build's role telemetry to the node it belongs to, then clear it.

        All three creation paths end with this triple, and it is the CONSUMING half of the pairing
        `_discard_node_build_telemetry` performs on every failure path. Skipping it is not inert: a
        "propose" reset re-runs the researcher (setting last_hyp_priority/last_foresight), and the
        pick set left behind then leaks onto the NEXT created node's id — the exact mis-attribution
        `_emit_role_telemetry` exists to prevent. Because it is three separate emits with no shared
        name, a path could quietly keep two of them and lose the third; here they move together.

        Variant-1: pass THIS build's pooled roles so concurrent draft builds cannot cross-wire each
        other's telemetry (last_report / last_hyp_priority / last_foresight). For the serial paths
        `researcher`/`developer` ARE `self.researcher`/`self.developer`, so omitting them is
        byte-identical to passing them.
        """
        self._emit_agent_report(node_id, **({"developer": developer} if developer is not None else {}))
        self._emit_hypothesis_ranked(
            node_id, generation, **({"researcher": researcher} if researcher is not None else {}))
        self._emit_foresight_selected(
            node_id, generation,
            **({"researcher": researcher} if researcher is not None else {}),
            **({"developer": developer} if developer is not None else {}))

    def _create_node_guarded(self, action: dict, roles=None, reserved=None, preproposed=None,
                             pretelemetry=None) -> None:
        """Variant-1 parallel build: run one pooled build, converting an UNEXPECTED exception into a
        `node_failed` terminal for its already-reserved id (its `node_building` was appended up front
        under `_id_lock`) instead of letting the exception propagate through the task group and tear
        down — and kill — the whole run. Keeps the one-terminal-per-node invariant (the reserved id
        gets exactly one terminal) and lets the rest of the concurrent batch finish. Used ONLY on the
        parallel path; the serial path keeps its historical crash-on-raise so bugs surface in tests."""
        try:
            self._create_node(action, roles, reserved, preproposed=preproposed,
                              pretelemetry=pretelemetry)
        except Exception as exc:  # noqa: BLE001 — one build's crash must not abort the concurrent batch
            node_id = reserved[1] if reserved else None
            if node_id is None:
                return
            latest = fold(self.store.read_all())
            node = latest.nodes.get(node_id)
            # Synthesise a terminal ONLY when this id has no node_created yet (a bare node_building whose
            # build raised before landing). A node that ALREADY has node_created carries real generated
            # code and is `pending` — the exception then came from the post-creation audit emitters
            # (audit-only); leave it for the evaluator. Marking a built, code-carrying node `failed` here
            # would silently discard a good build (review finding #2). If _create_node already wrote a
            # terminal (developer-crash sentinel, or node_evaluated), likewise nothing to do.
            if node is None:
                try:
                    self._fail_reserved_build(
                        node_id=node_id,
                        card_id=getattr(reserved, "card_id", None),
                        generation=0,
                        error=f"(build error: {exc})",
                        reason="build_crash",
                    )
                    # An EXCEPTION out of a build (not the graceful "(developer error: …)" sentinel) is a
                    # HARD fault — an LLM client that RAISES on a 401/outage, or a real bug in implement().
                    # The serial path crashes the run on such a raise; under concurrency we can't crash
                    # (it would kill sibling builds), so mirror the developer_crash circuit-breaker: PAUSE
                    # so the batch loop stops after this chunk instead of burning the node budget on
                    # repeated build_crash nodes (review finding #3). A plain resume continues once fixed.
                    # Same worker-seam reason as the developer_crash branch above: request the
                    # global pause, let the main task append it after the join.
                    self._request_create_pause(
                        node_id,
                        "auto-paused: a node build raised (LLM unreachable or a hard error, "
                        "unresolved within the build) — resume once it's fixed")
                except Exception:  # noqa: BLE001 — best-effort terminal; never re-raise into the group
                    pass

    @in_llm_lane("build")
    def _rerun_node(self, node: Node, state: RunState) -> None:
        """node_reset "propose"/"implement": re-run this EXISTING node id IN PLACE (never mints a new
        id — the whole point is to FIX a node, not proliferate). "implement" keeps the Researcher's idea
        (only the Developer re-runs — the "researcher ok, developer crashed" case); "propose" re-proposes
        a fresh idea too. Emits node_building + node_created for the SAME id — the fold applies it over the
        reset (clearing the rerun marker), the node goes pending-with-code, and the eval loop scores it
        next. Same developer-crash circuit-breaker as a first build. (An "eval" reset never reaches here —
        the fold left it pending-with-code and the eval dispatch re-scores it directly.)"""
        if (node.id in state.aborted_nodes or node.tombstoned
                or node.status is not NodeStatus.pending):
            return
        stage = node.rerun_from
        parents = list(node.parent_ids)
        parent = state.nodes.get(parents[0]) if parents else None
        generation = node.attempt
        parent_generations = {str(pid): state.nodes[pid].attempt for pid in parents
                              if pid in state.nodes}
        if len(parent_generations) != len(parents) or any(
                pid in state.aborted_nodes or state.nodes[pid].tombstoned for pid in parents):
            self.store.append(EV_NODE_FAILED, {
                "node_id": node.id, "generation": generation,
                "error": "parent is missing or aborted", "reason": "parent_unavailable",
                "eval_seconds": 0.0})
            return
        replacement_card = stage == "propose" and node.operator != "merge"
        with self.tracer.span(
                "create_node", new_trace=True, node_id=node.id, generation=generation,
                operator=node.operator):
            if replacement_card:
                # Re-proposal changes immutable work-item meaning. Finish the Idea first, then replace
                # the old Card with one exact native receipt while keeping the operator-requested node id.
                self._set_complexity_hint(state, parent)
                # RE-PROPOSAL: the card id genuinely is not knowable while this span is open —
                # the point of a re-proposal is that the old Card is DROPPED and a replacement is
                # minted afterwards, under `_id_lock`, from `_plan_native_card`. So this site stamps
                # the node context only. The card link is still derivable and still durable: this
                # span is nested in this node's `create_node` trace, and the `node_created` event
                # that trace ends with carries both that `trace_id` and the replacement `card_id`.
                with self.tracer.span("propose") as _span:
                    proposed = self.researcher.propose(state, parent)
                    # `None`, not `node.idea`: that idea still carries the card this path is
                    # about to drop, and stamping it would file this re-proposal under the card it
                    # REPLACED. See `stamp_proposal_span`.
                    stamp_proposal_span(_span, None, node_id=node.id)
                idea = self._canonicalize_idea_operator(proposed, node.operator)
                if idea is None:
                    self._fail_reserved_build(
                        node_id=node.id, card_id=node.idea.card_id, generation=generation,
                        error="researcher returned no replacement proposal",
                        reason="proposal_rejected", drop_card=bool(node.idea.card_id))
                    return
                idea = idea.model_copy(deep=True, update={
                    "card_id": None,
                    # Re-proposal is the same Researcher-owned action boundary as a fresh proposal.
                    # Persist the governed value so rerun receipts cannot diverge from execution.
                    "eval_timeout": self._effective_researcher_eval_timeout(idea),
                })
                with self._id_lock:
                    events = self.store.read_all()
                    latest = fold(events)
                    current = latest.nodes.get(node.id)
                    parents_current = all(
                        pid in latest.nodes
                        and latest.nodes[pid].attempt == parent_generation
                        and pid not in latest.aborted_nodes
                        and not latest.nodes[pid].tombstoned
                        for pid, parent_generation in (
                            (int(pid), value) for pid, value in parent_generations.items()))
                    if (current is None or current.attempt != generation
                            or current.rerun_from != "propose" or current.tombstoned
                            or node.id in latest.aborted_nodes or not parents_current):
                        self._discard_node_build_telemetry()
                        return
                    plan = self._plan_native_card(
                        events, latest, idea, parents=parents,
                        parent_generations=parent_generations,
                        scored_against=latest.best_node_id, source="researcher", at_node=node.id,
                        steering_context=getattr(self.researcher, "_steering_context", []),
                        superseded_card_id=current.idea.card_id,
                    )
                    if plan.disposition not in {"mint", "reuse"}:
                        self._fail_reserved_build(
                            node_id=node.id, card_id=current.idea.card_id,
                            generation=generation,
                            error="replacement proposal was duplicate or outside the Card contract",
                            reason="proposal_rejected", drop_card=bool(current.idea.card_id))
                        self._discard_node_build_telemetry()
                        return
                    # THE SAME OWNERSHIP CHECK the refusal path four lines up routes through (via
                    # `_fail_reserved_build`). `_drop_card_once` has none of its own, so dropping
                    # unconditionally destroyed a card that a DIFFERENT node had attached to — a
                    # debug re-attempt landing on the same work item — and a dropped card is
                    # unrecoverable, because `_retry_attach_card` refuses `dropped` forever. The
                    # later repair then minted a byte-identical twin: exactly the duplicate work
                    # item the attach disposition exists to prevent. Reproduced end-to-end (mint on
                    # node 0, attach on node 1, `node_reset from_stage=propose` on node 0).
                    # Fail-closed here means the superseded card survives as proposed inventory,
                    # which is the cost `_reservation_minted_card`'s own docstring prices against
                    # deleting somebody else's finished work item.
                    if self._reservation_minted_card(events, node.id, current.idea.card_id):
                        self._drop_card_once(current.idea.card_id, reason="reproposed")
                    if plan.disposition == "mint":
                        self.store.append(EV_CARD_ADDED, plan.payload)
                    self.store.append(EV_NODE_BUILDING, {
                        "node_id": node.id, "generation": generation,
                        "operator": node.operator, "parent_ids": parents,
                        "card_id": plan.card_id,
                    })
                    state = latest
                    idea = plan.idea
                    active_card_id = plan.card_id
            else:
                # An implement reset keeps immutable Idea/Card identity and only re-runs Developer.
                idea = node.idea.model_copy(deep=True)
                active_card_id = idea.card_id
                building_payload = {
                    "node_id": node.id, "generation": node.attempt,
                    "operator": node.operator, "parent_ids": parents,
                }
                if active_card_id:
                    building_payload["card_id"] = active_card_id
                self.store.append(EV_NODE_BUILDING, building_payload)
            self._reset_developer_footprint(self.developer)
            with self.tracer.span("implement"):
                # §1: a reset RE-BUILDS the node from scratch, so standing operator directives must
                # steer its code too — same as the four _create_node build sites.
                code = self._implement(
                    self._directed_idea(idea.model_copy(deep=True), state), parent, state=state)
            idea, footprint_finalized = self._finalize_developer_footprint(
                idea, self.developer, code)
            latest = fold(self.store.read_all())
            current = latest.nodes.get(node.id)
            parents_current = all(
                pid in latest.nodes and latest.nodes[pid].attempt == parent_generation
                and pid not in latest.aborted_nodes and not latest.nodes[pid].tombstoned
                for pid, parent_generation in ((int(pid), gen)
                                                for pid, gen in parent_generations.items()))
            if (current is None or current.attempt != generation
                    or current.tombstoned or node.id in latest.aborted_nodes or not parents_current):
                self._fail_reserved_build(
                    node_id=node.id, card_id=active_card_id, generation=generation,
                    error="node lifecycle changed while rebuilding", reason="superseded",
                    drop_card=replacement_card)
                self._discard_node_build_telemetry()   # serial single-node path: self.researcher/self.developer
                return
            self._emit_node_created(
                node_id=node.id, parent_ids=parents, operator=idea.operator,
                idea=durable_idea_payload(idea), code=code,
                files=getattr(self.developer, "last_files", {}) or {},
                deleted=getattr(self.developer, "last_deleted", []) or [],
                generation=generation,
                **({"parent_generations": parent_generations} if parent_generations else {}),
                **({"footprint_finalized": True} if footprint_finalized else {}))
            landed = fold(self.store.read_all()).nodes.get(node.id)
            if (landed is None or landed.attempt != generation or landed.rerun_from is not None
                    or landed.code != code):
                self._fail_reserved_build(
                    node_id=node.id, card_id=active_card_id, generation=generation,
                    error="rebuilt node creation was rejected during replay", reason="superseded",
                    drop_card=replacement_card)
                self._discard_node_build_telemetry()   # serial single-node path: self.researcher/self.developer
                return
            if is_developer_error(code):
                for crash_type, crash_data in developer_crash_records(
                        node.id, generation, code,
                        "auto-paused: a Developer session crashed (LLM unreachable or a hard "
                        "error, unresolved within the node) — resume once it's fixed"):
                    self.store.append(crash_type, crash_data)
        self._consume_node_build_telemetry(node.id, generation)

    def _prepare_injected_node(
        self,
        state: RunState,
        req: Mapping,
    ) -> _InjectedNodePlan:
        """Purely validate and normalize an inject request before any slot/LLM wait.

        Control/API writers already enforce a stricter schema. This boundary also handles legacy or
        hand-authored event rows and deliberately mirrors the tolerant materializer semantics; it has
        no provider, Developer, filesystem, or event-log side effect.
        """

        if not isinstance(req, Mapping):
            raise ValueError("injected request must be an object")
        idea_d = dict(req.get("idea") or {})
        idea_d.setdefault("operator", "manual")
        # Coerce params to floats defensively (a manual form may send strings); drop unparseable.
        raw_params = idea_d.get("params") or {}
        if not isinstance(raw_params, dict):
            raw_params = {}
        params: dict[str, float] = {}
        for key, value in raw_params.items():
            try:
                params[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        idea_d["params"] = params

        raw_parents = req.get("parent_ids")
        if isinstance(raw_parents, list):
            parents = [parent_id for parent_id in raw_parents if parent_id is not None]
        else:
            parent_id = req.get("parent_id")
            parents = [parent_id] if parent_id is not None else []
        # REJECT an unknown id instead of dropping it. Dropping left the operator with a ROOTLESS
        # node and no error — lineage silently lost to a typo — while the comparable stale and
        # tombstoned/aborted cases both fail the request loudly. (`parent_generations` caught the
        # drop only when the caller happened to supply a snapshot.) An explicitly parentless inject
        # still works: it passes no parent_id / an empty list, which never reaches this check.
        missing = [parent_id for parent_id in parents if parent_id not in state.nodes]
        if missing:
            raise ValueError(f"no such parent node(s): {missing}")
        unavailable = [
            parent_id for parent_id in parents
            if state.nodes[parent_id].tombstoned or parent_id in state.aborted_nodes
        ]
        if unavailable:
            raise ValueError(f"parent node(s) unavailable: {unavailable}")
        parent_generations = {
            str(parent_id): state.nodes[parent_id].attempt for parent_id in parents
        }
        expected_parent_generations = req.get("parent_generations")
        if expected_parent_generations is not None:
            if not isinstance(expected_parent_generations, dict):
                raise ValueError("parent_generations must be an object")
            if len(expected_parent_generations) != len(parent_generations):
                raise ValueError("parent generation snapshot does not match parents")
            for parent_id, generation in parent_generations.items():
                if expected_parent_generations.get(parent_id) != generation:
                    raise ValueError(f"stale parent generation for node #{parent_id}")

        # AN OPERATOR MAY NOT INJECT A DEBUG NODE EITHER (F5). This surface offered
        # draft/improve/debug/merge and was the last producer left once the policies and the Card
        # lane lost theirs — and it is the one that most looks like an exception worth making, since
        # a human asked for it. It is not: what the operator gets instead is strictly better, because
        # the node they would have opened is now repaired in place with no budget slot spent and no
        # second lineage to reconcile. Raised as a `ValueError` like every other refusal here, so the
        # control path answers the operator rather than the run dying (`_drain_injects`).
        if str(idea_d.get("operator") or "").strip().lower() == "debug":
            raise ValueError(
                "debug nodes were removed on 2026-08-13: a failure is repaired inside the node that "
                "failed, for as long as the repair judgment allows. Reset the failed node to repair "
                "it again, or inject a draft/improve if this is genuinely a different experiment.")

        code = req.get("code")
        # U3 real merge: this combines Idea metadata only. Developer work remains after reservation.
        if not code and idea_d.get("operator") == "merge" and len(parents) >= 2:
            parent_nodes = [state.nodes[parent_id] for parent_id in parents]
            idea = (self._ensemble_idea(parent_nodes) if self._merge_mode == "ensemble"
                    else merge_idea(parent_nodes))
        else:
            idea = Idea(**idea_d)
        idea = idea.model_copy(deep=True, update={"card_id": None})
        implementation_ref = self._implementation_ref(
            code=code,
            files=req.get("files"),
            deleted=req.get("deleted"),
        )
        return _InjectedNodePlan(
            idea,
            parents,
            parent_generations,
            code,
            implementation_ref,
        )

    @in_llm_lane("build")
    def _create_injected_node(self, req: dict) -> None:
        """Materialize an operator-authored experiment (`inject_node` control event) into a real
        pending node. The operator supplies an idea (operator label, params, rationale, optional
        theme) and optionally a parent and ready-made code. If no code is given, the Developer
        implements the idea — so a human can describe an experiment and let the agent build it.
        The new node enters the search as `pending`; the policy evaluates it next.

        Manual injection deliberately bypasses the policy's proposal step — the human IS the
        researcher here — but everything downstream (eval, confirmation, best-selection, lineage)
        is identical to an agent-authored node, so a hand-added winner can be selected as best."""
        state = fold(self.store.read_all())
        prepared = self._prepare_injected_node(state, req)
        idea = prepared.idea
        parents = prepared.parent_ids
        parent_generations = prepared.parent_generations
        code = prepared.code
        implementation_ref = prepared.implementation_ref
        reservation = self._reserve_node_build(
            {
                "kind": idea.operator,
                "parent_ids": parents,
                "parent_generations": parent_generations,
            },
            idea,
            scored_against=state.best_node_id,
            source="operator",
            implementation_ref=implementation_ref,
            # NO ATTACH HERE, deliberately (`retry_attach` defaults off and this site keeps it off).
            # An operator `debug` injection against a failed node whose card is live would otherwise
            # attach — and an attach mints no `card_added`, so BOTH of the two receipts that make
            # this an operator-authored experiment are silently discarded: `source="operator"` (the
            # board would credit the Researcher's card) and `implementation_ref`, whose stated
            # purpose is that folding two injections with ready-made code "would lose executable
            # work". The human IS the researcher here; their work item is their own.
        )
        if reservation is None:
            raise ValueError("injected idea could not reserve one exact native Card")
        state = reservation.state
        node_id = reservation.node_id
        parent_generations = reservation.parent_generations
        idea = reservation.idea.model_copy(deep=True)
        with self.tracer.span("create_node", new_trace=True, node_id=node_id,
                              generation=0, operator=idea.operator, source="manual"):
            developer_called = not bool(code)
            footprint_finalized = False
            if developer_called:
                try:
                    self._reset_developer_footprint(self.developer)
                    with self.tracer.span("implement"):
                        # An injected experiment usually BUILDS ON its parent (a human picked it as the
                        # base) — hand the parent's solution to a parent-aware developer. Preserve the
                        # receipt-bound Idea by handing the plugin a deep working copy.
                        _pnode = state.nodes.get(parents[0]) if parents else None
                        code = self._implement(idea.model_copy(deep=True), _pnode, state=state)
                except Exception:
                    self._fail_reserved_build(
                        node_id=node_id, card_id=reservation.card_id, generation=0,
                        error="injected Developer raised before node creation", reason="build_crash")
                    self._discard_node_build_telemetry()
                    raise
                idea, footprint_finalized = self._finalize_developer_footprint(
                    idea, self.developer, code)
            latest = fold(self.store.read_all())
            if any(pid not in latest.nodes
                   or latest.nodes[pid].attempt != generation
                   or latest.nodes[pid].tombstoned
                   or pid in latest.aborted_nodes
                   for pid, generation in ((int(pid), gen)
                                           for pid, gen in parent_generations.items())):
                self._fail_reserved_build(
                    node_id=node_id, card_id=reservation.card_id, generation=0,
                    error="parent lifecycle changed while building", reason="superseded")
                self._discard_node_build_telemetry()   # serial single-node path: self.researcher/self.developer
                return
            try:
                self._emit_node_created(
                    node_id=node_id,
                    parent_ids=parents,
                    operator=idea.operator,
                    idea=durable_idea_payload(idea),
                    code=code,
                    # Honour explicit files/deleted on the request (a cross-run `import` ships the
                    # sibling's full multi-file solution); else use the Developer's last build, and
                    # only when the Developer actually implemented (no ready-made code was supplied).
                    files=(req.get("files")
                           or ({} if req.get("code") else getattr(self.developer, "last_files", {}))) or {},
                    deleted=req.get("deleted") or [],
                    source="manual",
                    **({"parent_generations": parent_generations} if parent_generations else {}),
                    **({"footprint_finalized": True} if footprint_finalized else {}),
                    # Cross-run provenance: a DICT when this inject seeded from a sibling run's
                    # experiment (an `import` action), else None. Coerce defensively — a non-dict
                    # origin (a hand-authored/API inject that passed a label string) would make the
                    # folded Node fail validation and silently vanish, so the inject gate would keep
                    # re-creating the SAME node id forever.
                    origin=req.get("origin") if isinstance(req.get("origin"), dict) else None,
                )
            except Exception:
                try:
                    landed = node_id in fold(self.store.read_all()).nodes
                except Exception:
                    landed = False
                if not landed:
                    self._fail_reserved_build(
                        node_id=node_id, card_id=reservation.card_id, generation=0,
                        error="injected node append failed", reason="build_crash")
                raise
            if node_id not in fold(self.store.read_all()).nodes:
                self._fail_reserved_build(
                    node_id=node_id, card_id=reservation.card_id, generation=0,
                    error="injected node creation was rejected during replay", reason="superseded")
                self._discard_node_build_telemetry()   # serial single-node path: self.researcher/self.developer
                return
            # Mirror _create_node / _rerun_node: a Developer session that CRASHED returns the
            # "(developer error: …)" sentinel as its code (an LLM 401/timeout/hard error). Without
            # this guard the injected node stays pending and its eval runs the PARENT's carried-over
            # entrypoint/files and inherits the PARENT's metric — a false success (the exact bug the
            # two sibling create paths already fix). FAIL it now (node_created → node_failed keeps the
            # one-terminal invariant) and trip the SAME developer-crash circuit-breaker, so an operator
            # inject during an LLM outage can't silently slip a garbage-code node past it.
            if is_developer_error(code):
                for crash_type, crash_data in developer_crash_records(
                        node_id, 0, code,
                        "auto-paused: a Developer session crashed while building an injected node "
                        "(LLM unreachable or a hard error, unresolved within the node) — resume "
                        "once it's fixed"):
                    self.store.append(crash_type, crash_data)
        if developer_called:
            self._consume_node_build_telemetry(node_id, 0)

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

    def _setup_manifest(self, wf: "dict | None" = None) -> str:
        """P0-3 content-addressed setup: a stable digest of the MATERIAL the task+data preflight
        verified — the config hash, the workspace fingerprint, and the data-asset provenance. Binds
        `setup_done` to the exact inputs so a pre-node resume re-runs preflight (leakage!) when they
        changed rather than trusting a stale boolean. Deterministic (pure content hashes), so an
        unchanged workspace yields the recorded digest and never loops. `wf` may be passed to reuse an
        already-computed fingerprint.

        The hashing itself is `core/setup_identity.setup_manifest_digest` — the quality reader
        re-derives this exact digest to prove calibration evidence came from the shipped writer, and
        `search` may not import the engine, so the derivation lives where both can reach it
        (doc 25 SE-01)."""
        wf = self._workspace_fingerprint() if wf is None else wf
        prov = {name: hashlib.sha256(
                    c.encode("utf-8") if isinstance(c, str) else bytes(c)).hexdigest()[:16]
                for name, c in (self._assets or {}).items()}
        return setup_manifest_digest(self.task.model_dump(mode="json"), wf, prov)

    def _env_fingerprint(self) -> dict:
        """Use the same source-owned environment identity as the quality receipt validator.

        A calibration run is pinned here and re-read later by ``speculation_quality``; two nearly
        identical package lists would make valid local evidence impossible to revalidate (or, worse,
        omit a broken direct dependency from one side).  The shared helper is metadata-only and never
        touches the network.
        """
        from looplab.search.speculation_quality import speculation_environment_fingerprint
        return speculation_environment_fingerprint()

    def _dirty_inputs(self, wf: "dict | None") -> list:
        """P0-5 dirty-input enumeration: for each git-repo workspace source, the uncommitted-file LIST
        (`git status --porcelain`) plus a bounded DIGEST of the actual diff vs HEAD (`git diff HEAD`) —
        the EXPLICIT record of which inputs differ from a clean checkout AND a content fingerprint of
        HOW, on top of the HEAD-SHA the workspace fingerprint pins (which is blind to uncommitted work).
        The digest (not the diff TEXT) is stored on purpose: it detects a changed dirty-content across
        runs WITHOUT leaking a secret a raw patch could carry (a pasted key, an edited .env) into the
        world-readable log.

        Corner-case behavior (all best-effort — a source never fails the run):
          * A heavy UNTRACKED artifact costs nothing: `git diff HEAD` never emits untracked files, so
            only its NAME lands in the porcelain list. A heavy TRACKED+modified text file would make
            git stream a giant patch, so the diff is hashed INCREMENTALLY and capped at
            `_DIFF_DIGEST_CAP` — the engine never buffers the whole patch, and an over-cap digest is
            marked `~` (truncated) so a reader knows the tail was not seen.
          * A gitignored file is INVISIBLE here BY DESIGN — porcelain skips it and the repo fingerprint
            is HEAD-only, so declared-non-source scratch (`runs/`, `__pycache__`, `model.pkl`, `.env`)
            never pollutes the enumeration (and `.env`'s secret never enters the log). A gitignored
            path that is genuinely a run INPUT should be mounted as a `data:` source, where
            `_shallow_fingerprint` covers it outside git's ignore rules.
          * Multiple sources under one repo share a single diff (computed once per resolved root).
        Bounded output: <=500 porcelain lines x 200 chars, and one capped digest per repo root."""
        import os
        import subprocess
        import time
        from looplab.runtime.sandbox import git_subprocess_env

        git_env = git_subprocess_env()

        def _diff_digest(root: str) -> "str | None":
            # Incrementally hash `git diff HEAD` (staged + unstaged) so a multi-GB tracked-file diff
            # never lands in memory: raw fd reads, an 8 MiB byte cap, and a wall-clock deadline.
            proc = None
            try:
                proc = subprocess.Popen(["git", "-C", root, "diff", "HEAD"],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        env=git_env)
                fd = proc.stdout.fileno()
                h, read, truncated, deadline = hashlib.sha256(), 0, False, time.monotonic() + 15
                while read < _DIFF_DIGEST_CAP:
                    if time.monotonic() > deadline:
                        truncated = True
                        break
                    chunk = os.read(fd, min(65536, _DIFF_DIGEST_CAP - read))
                    if not chunk:
                        break                                       # EOF: the whole diff was hashed
                    h.update(chunk)
                    read += len(chunk)
                else:
                    truncated = bool(os.read(fd, 1))                # bytes remained past the cap
                return (h.hexdigest()[:16] + ("~" if truncated else "")) if read else None
            except Exception:  # noqa: BLE001 — no HEAD / git error / decode: keep the file list only
                return None
            finally:
                if proc is not None:
                    try:
                        proc.stdout.close()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        proc.terminate()                            # stop git if we bailed mid-stream
                        proc.wait(timeout=5)
                    except Exception:  # noqa: BLE001
                        try:
                            proc.kill()
                        except Exception:  # noqa: BLE001
                            pass

        out: list = []
        digests: dict = {}                                          # resolved-root -> digest (once)
        for src in sorted((wf or {}).keys()):
            try:
                p = Path(src)
                root = str(p if p.is_dir() else p.parent)
                r = subprocess.run(["git", "-C", root, "status", "--porcelain"],
                                   capture_output=True, text=True, timeout=10, env=git_env)
                dirty = [ln[:200] for ln in r.stdout.splitlines() if ln.strip()][:500]
                if r.returncode == 0 and dirty:
                    entry = {"source": src, "dirty": dirty}
                    if root not in digests:
                        digests[root] = _diff_digest(root)
                    if digests[root] is not None:
                        entry["diff_digest"] = digests[root]
                    out.append(entry)
            except Exception:  # noqa: BLE001 — git missing / not a repo / timeout: no enumeration
                pass
        return out

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
    # `_resolve_stages` / `_resolved_stages` / `_imported_modules` / `_module_file_candidates` /
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
