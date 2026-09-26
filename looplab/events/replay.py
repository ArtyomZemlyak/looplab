"""Pure fold: events -> RunState (I1/I6, ADR-12). Deterministic; the only producer
of RunState. Resume = re-fold the log. ``best_node_id`` is a deterministic post-pass over eligible
evaluated nodes using trust/fitness treatment, confirmation, holdout and approval state; node id is
only the final tie-break. No separate ``best_updated`` event is needed.

This module is the fold's ENTRY and its core; the handler families that change for their own
reasons live beside it (review 2026-09-22, EVT-12), each owning the ``HANDLERS`` rows of the events
it folds: ``replay_concepts`` (the Part IV/V concept read model), ``replay_journals`` (the spend
ledger, advisory payloads and audit journals), ``replay_cards`` (the card board's receipts),
``replay_selection`` (the trust gate and the champion selector), ``replay_requests`` (operator
intents the fold only queues or records), all over ``replay_ctx`` (the fold context and the
lifecycle-generation rules). What stays HERE is ``fold`` / ``FoldCursor`` / ``_finalize_fold``, the
merged ``_HANDLERS`` table, the run's identity and setup gates, and the node LIFECYCLE together with
the run-state transitions (pause, resume, finish, node abort) that share its invalidation rules.
Every name that moved stays importable from this module where a reader imports it from here.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

from looplab.core.fitness import (VERIFIER_SELECTION_CONTRACT, finite_metric,
                                  is_usable_metric,
                                  verifier_evidence_digest)
from looplab.core.headroom import normalized_reference
from looplab.core.jsonutil import bounded_int, valid_digest_ref
from looplab.core.models import (Event, Idea, Node, NodeStatus, RunState, Trial,
                     coerce_node_id as _coerce_node_id,
                     EXTRA_METRIC_DECLARED, normalize_extra_metric_backfill,
                     normalize_extra_metric_channels, normalize_extra_metric_directions, normalize_extra_metrics,
                     normalize_researcher_footprint,
                     run_setup_key, BENIGN_TERMINAL_REASONS)
# No longer read here — the concept family's materializer inherits through it — but still readable
# from this module as it always was (`tests/test_shared_identity_rules.py` derives the card ledger's
# display set from it).
from looplab.core.models import (  # noqa: F401 — re-export
    INHERITABLE_CONCEPT_PROVENANCE as _INHERITABLE_CONCEPT_PROVENANCE)
# The derived Card ledger (doc 25 EV-01). ONLY the names this module's own handlers call are
# imported: a re-export of a helper `card_ledger` then calls internally would look like a patch seam
# while a monkeypatch through it silently missed the fold, which is the exact failure the flat-import
# alias shim exists to prevent for MODULES. Tests that reach for a ledger internal import it from
# `looplab.events.card_ledger` directly. The receipt bounds the board handlers call moved WITH them
# into `replay_cards.py` (review 2026-09-22, EVT-12), under the same rule — so
# `CARD_ENRICHMENT_JOURNAL_MAX` is deliberately NOT bound here any more: a patch of it on this module
# would lower a cap no fold code reads, and now fails loudly instead.
from looplab.events.card_ledger import (
    _CARD_REPLAY_NODE_ID_MAX,
    _card_replay_id,
    derive_cards as _derive_cards,
)
from looplab.events.finalize_scope import is_guarded_abort
# The fold's context and the lifecycle-generation rules every handler family reads (review
# 2026-09-22, EVT-12: the families split out of this module sit ABOVE `replay_ctx` and below this
# module, so none of them may import it back). Every name stays importable from here —
# `engine/evaluate.py` imports `event_generation_binds` from `replay`, and tests import the rest.
from looplab.events.replay_ctx import (
    _MISSING, _FoldCtx, _control_generation_matches, _event_generation, _generation_matches,
    _node_for_event,
)
from looplab.events.replay_ctx import (  # noqa: F401 — re-export
    event_generation_binds, event_timestamp, run_wall_clock_seconds)
# The CONCEPT family (review 2026-09-22, EVT-12): its table joins `_HANDLERS` below, the node
# lifecycle hands it each `node_created`'s concept envelope, and the post-pass materializes deltas.
from looplab.events.replay_concepts import HANDLERS as _CONCEPT_HANDLERS
from looplab.events.replay_concepts import (_fold_node_concept_envelope,
                                            _materialize_concept_deltas)
# The JOURNALS family: the spend ledger, the advisory memo/literature/report payloads and the
# audit journals. Only its table is read here; the ledger sanitizers and the fold's empty redaction
# environment stay readable from this module (`tests/test_fold_fast_paths_are_exact.py`).
from looplab.events.replay_journals import HANDLERS as _JOURNAL_HANDLERS
from looplab.events.replay_journals import (  # noqa: F401 — re-export
    _FOLD_REDACTION_ENV, _MAX_LLM_COST, _MAX_LLM_COUNTER, _clean_llm_totals, _row_priced_calls)
# The CARDS family: the belief journal, the Card receipts and the speculative build queue. Only its
# table is read here (`_derive_cards` stays this module's call, in `_finalize_fold`).
from looplab.events.replay_cards import HANDLERS as _CARD_HANDLERS
from looplab.events.replay_cards import _on_card_build_done  # noqa: F401 — re-export (tests)
# The SELECTION family: the trust gate, the champion selector and the evidence they read. The two
# post-passes `_finalize_fold` runs are read here; every public name stays importable from `replay`,
# which is where the engine, `digest.py` and the server import it from.
from looplab.events.replay_selection import HANDLERS as _SELECTION_HANDLERS
from looplab.events.replay_selection import _apply_trust_gate, _select_best
from looplab.events.replay_selection import (  # noqa: F401 — re-export
    flagged_node_ids, hard_flagged_ids, is_hard_signal, promotion_eligible_nodes, select_best_node,
    verifier_tie_groups)
# The REQUESTS family: operator intents the fold only queues or records (force-confirm/ablate,
# fork, inject, deep research, hints, strategy pins, budget extensions, promotions, notes). The
# queueing rule, the cursor rule and the two force handlers stay readable from `replay` for the
# tests that import or read them (`tests/test_replay_queue_and_producer_seams.py`).
from looplab.events.replay_requests import HANDLERS as _REQUEST_HANDLERS
from looplab.events.replay_requests import (  # noqa: F401 — re-export
    _advance_request_cursor, _on_force_ablate, _on_force_confirm, _queue_forced_request)
from looplab.events.types import (
    EV_ABLATE, EV_AGENT_VALIDATED, EV_APPROVAL_GRANTED,
    EV_APPROVAL_REQUESTED,
    EV_CONFIRM_EVAL,
    EV_EVAL_NOISE_FLOOR, EV_EVAL_NOISE_SEED,
    EV_FINALIZATION_FINISHED,
    EV_FORESIGHT_SELECTED,
    EV_HOLDOUT_EVALUATED,
    EV_NODE_ABORT, EV_NODE_BUILDING, EV_NODE_CONFIRMED,
    EV_NODE_CREATED, EV_NODE_EVAL_STARTED, EV_NODE_EVALUATED, EV_NODE_FAILED, EV_NODE_REPAIRED,
    EV_NODE_RESET,
    EV_APPLIED_PARAMS_BACKFILLED,
    EV_SCORE_METRICS_BACKFILLED,
    EV_NODE_TOMBSTONED, EV_NODE_VALUE_ESTIMATED, EV_PAUSE, EV_STAGE_FINISHED,
    EV_PROXY_SCORED,
    EV_RESTART, EV_RESUME, EV_RESUME_REQUESTED,
    EV_RESUME_SERVED,
    EV_RUN_ABORT,
    EV_RUN_FINISHED, EV_RUN_REOPENED, EV_RUN_SETUP_FINISHED, EV_RUN_SETUP_STARTED, EV_RUN_STARTED,
    EV_RUN_WIDTH_SETTLED,
    EV_SETUP_FINISHED, EV_SPEC_APPROVAL_REQUESTED, EV_SPEC_APPROVED, EV_SPEC_DRIFT, EV_SPEC_PROPOSED,
    EV_SPECULATION_DEPTH_SETTLED)


# --------------------------------------------------------------------------- fold dispatch
# One handler per event type (docs/15 §P5.1): the bodies below are the VERBATIM arms of the
# former 63-way if/elif chain, one function each, dedented — with exactly three mechanical
# adjustments, all noted in place: (a) `continue` became `return` in _on_node_created (same
# meaning: skip the rest of THIS event); (b) the EV_BEST_CONFIRMED arm writes the fold-local
# through `ctx` (the ONE cross-arm value, threaded explicitly instead of a closure variable);
# (c) the resume/reopen twin arm is ONE handler registered under both keys.
# Every handler is a pure `(st, e, d, ctx) -> None` mutation — no I/O, no LLM calls — invoked
# in log order by `fold`, so determinism/order-tolerance are structurally unchanged; unknown
# event types still no-op via `_HANDLERS.get`. The uniform signature keeps the registry
# mechanical; most handlers ignore `e`/`ctx`.
# Since review 2026-09-22 (EVT-12) "below" is this module's own handlers plus the family modules
# it imports (`replay_concepts`, `replay_journals`, `replay_cards`, `replay_selection`,
# `replay_requests`), each moved VERBATIM and each registering its rows in its own `HANDLERS`;
# the signature, the purity and the dispatch are unchanged, and `_HANDLERS` is their union.


def _settle_folded_speculation_depth(st: RunState) -> None:
    """Recompute the EFFECTIVE Layer-5 depth from the run's two independent depth facts.

    `speculation_depth_pinned` is `run_started`'s launch treatment and `speculation_depth_settled` is
    the floor the run's own adaptive ratchet has narrowed itself to; the effective depth is the pin
    capped by that floor. Both writers call this, which is what makes the pair ORDER-TOLERANT: each
    fact is written by exactly one handler, neither reads the other's field before writing its own,
    and the derivation is a pure minimum. Splicing `speculation_depth_settled` anywhere relative to
    `run_started` therefore lands on the same effective depth — which a minimum taken directly on
    `speculation_depth` did NOT: `run_started` ASSIGNS over a `RunState` default of 0, so a settle
    row folded first was simply overwritten (measured: 4 at position 0, 0 at every other position).

    A floor ABOVE the pin is inert, which is what keeps a stale/foreign/hand-edited row from ever
    RAISING the treatment — the property the minimum was chosen for.
    """
    floor = st.speculation_depth_settled
    st.speculation_depth = (st.speculation_depth_pinned if floor is None
                            else min(st.speculation_depth_pinned, floor))


def _on_run_started(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # FIRST START WINS. `run_started` is the one-time identity anchor and carries the run's immutable
    # authority: `direction` (champion ordering for the whole run), `trust_gate`, `card_driven_selection`,
    # `speculation_depth` and its gate receipts. Last-write-wins let a spliced/duplicated second row
    # INVERT the objective or relax the trust gate after nodes already existed — silently rewriting how
    # every prior result is ranked. This gate mirrors the producer exactly: the engine appends
    # `run_started` only `if not state.run_id` (engine/setup_phase.py::_setup_phase), so on any log it wrote this is a
    # no-op. Keyed on `run_id` rather than "have I seen one" for the same reason: a row that never
    # established identity is not an anchor and must not shadow the real start that follows it.
    if st.run_id:
        return
    # Read with defaults like every other fold handler (RunState already defaults these to ""): the
    # fold loop dispatches handlers with NO per-event try/except, so a bare d["run_id"] KeyError on a
    # malformed/hand-edited run_started would take down the WHOLE fold (every view/replay/resume of the
    # run) — the exact hand-edited-log-tolerance the _on_node_created guard was added to provide.
    st.run_id = d.get("run_id", "")
    _run_uid = d.get("run_uid", "")
    st.run_uid = _run_uid if isinstance(_run_uid, str) else ""
    st.task_id = d.get("task_id", "")
    st.goal = d.get("goal", "")
    # `direction` drives is_better/best-selection for the whole run — a typo ("Max",
    # "maximize") must not silently invert the objective. Accept only the two valid values;
    # anything else falls back to the safe default rather than flipping optimization.
    _dir = str(d.get("direction", "min")).strip().lower()
    st.direction = _dir if _dir in ("min", "max") else "min"
    st.config_hash = d.get("config_hash", "")
    # doc 67 67.14: the task's declared baseline/target, held to the one rule the writer used
    # (`core/headroom.py::normalized_reference`); None on every log that pinned none. Reporting only.
    st.reference_score = normalized_reference(d.get("reference_score"))
    st.workspace = d.get("workspace")
    st.env = d.get("env")   # P0-5 environment identity pinned at start (None on old logs)
    _di = d.get("dirty_inputs")
    st.dirty_inputs = _di if isinstance(_di, list) else []   # P0-5 uncommitted-input enumeration
    _tg = str(d.get("trust_gate", "audit")).strip().lower()
    st.trust_gate = _tg if _tg in ("audit", "gate", "block") else "audit"
    # The HITL gate the run launched with (pinned since 2026-09-06, invariant #6). Only a JSON
    # boolean is a record; anything else — and every log written before the pin — folds to None,
    # which every reader treats as "not recorded, the snapshot decides" and never as False.
    _ra = d.get("require_approval")
    st.require_approval = _ra if isinstance(_ra, bool) else None
    # F1d: the run-level DECLARED ENVIRONMENT the evals ran under. Absent on old logs and on every
    # run that declared none -> `{}` -> the engine keeps its own launch value, i.e. byte-identical
    # legacy behaviour. Coerced to `{str: str}` here rather than trusted: this is read back by
    # `Engine._repin_declared_env` and handed to a child PROCESS, so a hand-edited row carrying a
    # list or a nested object must degrade to "nothing declared" instead of reaching `subprocess`
    # as an unusable env dict — the fold has no exception handler and cannot raise.
    _ee = d.get("eval_env")
    st.eval_env = ({str(k): str(v) for k, v in _ee.items()
                    if isinstance(k, str) and isinstance(v, (str, int, float))
                    and not isinstance(v, bool)}
                   if isinstance(_ee, dict) else {})
    # Layer 3 queue ownership is selection-affecting and therefore pinned by the event log. Accept
    # only the JSON boolean true: strings and integers in malformed/legacy rows fail closed to the
    # byte-identical policy/pilot path.
    st.card_driven_selection = d.get("card_driven_selection") is True
    # A strict bounded integer is required: bools/strings/floats are malformed and must not turn on
    # speculative execution. Absent on old logs -> 0 -> historical alternating build/eval behavior.
    # This is the LAUNCH PIN and nothing else writes it; the EFFECTIVE depth is derived from it and
    # the adaptive floor by `_settle_folded_speculation_depth`, so this handler and
    # `_on_speculation_depth_settled` may land in either order (invariant #5).
    _spec_depth = d.get("speculation_depth", 0)
    st.speculation_depth_pinned = (
        _spec_depth if bounded_int(_spec_depth, 0, 64) else 0)
    # Whether that pin RESOLVED the AUTO sentinel or was SPELLED. `is True` rather than `bool(...)`:
    # only the literal the writer emits may enable the one-way ratchet, so a truthy string or a 1 in
    # a hand-edited log cannot turn someone's spelled treatment into a self-narrowing one. Absent
    # folds to False — see the field's comment in `core/models.py`.
    st.speculation_depth_auto = d.get("speculation_depth_auto") is True
    _settle_folded_speculation_depth(st)
    # Four sha256-prefixed receipt digests admitted by ONE predicate (doc 25 EV-04); they were four
    # copies of the same six-line conjunction. The assignments stay written out rather than a
    # `setattr` loop over field-name strings: a typo in such a loop would silently set the wrong
    # attribute AND leave the real one at its default — the silent-no-op class this fold exists to
    # avoid. The fail-closed "" per field is what makes a malformed digest read as "no receipt"
    # rather than as a receipt nothing can verify.
    _rd = d.get("speculation_gate_receipt_digest", "")
    st.speculation_gate_receipt_digest = _rd if valid_digest_ref(_rd, prefix="sha256:") else ""
    _rd = d.get("speculation_runtime_scope_sha256", "")
    st.speculation_runtime_scope_sha256 = _rd if valid_digest_ref(_rd, prefix="sha256:") else ""
    _rd = d.get("speculation_implementation_digest", "")
    st.speculation_implementation_digest = _rd if valid_digest_ref(_rd, prefix="sha256:") else ""
    _rd = d.get("speculation_calibration_profile_digest", "")
    st.speculation_calibration_profile_digest = (
        _rd if valid_digest_ref(_rd, prefix="sha256:") else "")
    # Bound the row CONTENTS, not just the row count: `dict(row)` copied each row whole, so a
    # hand-edited/foreign run_started could park megabytes in RunState — which FoldCursor then
    # deep-copies on EVERY snapshot. That is the amplification the card handlers' bounding comments
    # warn about, and this was the one fold boundary here without it. The bound is deliberately far
    # above the real schema (`speculation_quality._GPU_IDENTITY_FIELDS` is 7 fields of short
    # scalars, and that consumer REJECTS anything else), so no legitimate log changes shape;
    # oversized keys/values are dropped rather than truncated, because a silently shortened uuid or
    # pci_bus_id would be a different GPU, and the consumer must see the row fail its schema.
    _calibration_inventory = d.get("speculation_calibration_gpu_inventory", [])
    st.speculation_calibration_gpu_inventory = (
        [_bounded_gpu_inventory_row(row) for row in _calibration_inventory]
        if isinstance(_calibration_inventory, list)
        and len(_calibration_inventory) <= 256
        and all(isinstance(row, dict) for row in _calibration_inventory)
        else []
    )
    _calibration_seed = d.get("speculation_calibration_seed")
    st.speculation_calibration_seed = (
        _calibration_seed
        if bounded_int(_calibration_seed, 0, (1 << 63) - 1)
        else None
    )
    _policy_scope = d.get("speculation_policy_scope", "")
    st.speculation_policy_scope = _policy_scope if _policy_scope == "greedy" else ""
    # The SETTLED concurrency widths, pinned as RESOLVED integers (never the `0` AUTO sentinel, which
    # re-derives off the resuming box). Strict bounded ints for the same reason speculation_depth is:
    # a bool/string/float in a hand-edited or foreign row must not reshape a run's execution
    # treatment. Absent (old logs) or malformed -> 0 -> "not recorded" -> the engine keeps its own
    # startup resolution, which is byte-identical to the pre-pin behaviour.
    _eval_parallel = d.get("eval_parallel", 0)
    st.eval_parallel = (_eval_parallel if bounded_int(_eval_parallel, 0, 1024) else 0)
    _llm_parallel = d.get("llm_parallel", 0)
    st.llm_parallel = (_llm_parallel if bounded_int(_llm_parallel, 0, 64) else 0)
    # The setting NAMES the operator spelled explicitly at launch (never values). Absent on old logs
    # -> [] -> no launch pins. Bounded and coerced like every other run_started field: a hand-edited
    # row must not park megabytes in RunState or smuggle a non-string into a set-membership test.
    _es = d.get("explicit_settings")
    st.explicit_settings = (sorted({k for k in _es if isinstance(k, str) and 0 < len(k) <= 128})
                            if isinstance(_es, list) and len(_es) <= 1024 else [])
    # D1: recorded at start so replay applies the same selection rule. Absent in old
    # logs -> False -> byte-identical legacy selection.
    st.holdout_select = bool(d.get("holdout_select", False))
    # The reserved-holdout fraction the run committed to (the split every search metric was
    # scored against). None in old logs; the engine re-uses it on resume so a changed live
    # setting can't make pre/post-resume metrics incomparable.
    _hf = d.get("holdout_fraction")
    st.holdout_fraction = float(_hf) if is_usable_metric(_hf) else None
    # R1-c: recorded at start so replay applies the same selection rule (config isn't available to the
    # pure fold). Absent in old logs -> False -> byte-identical legacy selection.
    # The fold stays pinned to the RECORDED value (never a live re-read); the engine re-pins its own
    # `_select_verifier` gate from this recorded value on resume (`reentry.py::_reentry_repin`), so the
    # fold's tie-break rule and the live verify production can't diverge across a config edit (invariant #6).
    st.select_verifier_tiebreak = bool(d.get("select_verifier", False))
    st.verifier_ci_tie = bool(d.get("verifier_ci_tie", False))   # R1-d: absent on old logs -> exact-tie
    samples = d.get("select_verifier_samples", 3)
    st.select_verifier_samples = (samples if isinstance(samples, int) and not isinstance(samples, bool)
                                  and 1 <= samples <= 32 else 3)
    contract = d.get("select_verifier_contract", VERIFIER_SELECTION_CONTRACT)
    st.select_verifier_contract = (contract if isinstance(contract, str) and len(contract) <= 80
                                   else VERIFIER_SELECTION_CONTRACT)

def _on_node_building(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Transient "a node is being built RIGHT NOW" marker (see EV_NODE_BUILDING docs): show it in
    # the UI the instant work starts, before node_created. NOT added to st.nodes, so id
    # allocation + resume are untouched. Superseded/cleared by this node's node_created below.
    nid = _coerce_node_id(d)
    if nid is None:
        return
    current = st.nodes.get(nid)
    if nid in st.aborted_nodes or (current is not None and current.tombstoned):
        _clear_build_marker(st, d, nid)
        return
    if current is not None and not _generation_matches(current, d):
        return
    # A SETTLED lifecycle is not being built (review 2026-09-22, EVT-11). Concurrent build threads
    # append their own node's rows, so a `node_building` can land AFTER that lifecycle's
    # `node_created` — and after its terminal. Nothing later clears a marker set then: the node
    # read `building…` on the board beside its own metric, forever. Only a PENDING node (a fresh
    # reservation's re-emit, or a reset lifecycle awaiting its rebuild) may carry the marker.
    if current is not None and current.status is not NodeStatus.pending:
        return
    marker = {"node_id": nid, "operator": d.get("operator"),
              "parent_ids": d.get("parent_ids", []), "started": e.ts}
    card_id = _card_replay_id(d.get("card_id"))
    if card_id is not None:
        # Additive link for the Card queue. Keep malformed/oversized ids out of the transient
        # RunState marker just as the durable card journals do; old node_building rows retain their
        # exact marker shape because the key is absent unless a valid id was recorded.
        marker["card_id"] = card_id
    if d.get("speculative") is True:
        card_build_generation = d.get("card_build_generation")
        if bounded_int(card_build_generation, 0, _CARD_REPLAY_NODE_ID_MAX):
            # This is the speculative request epoch, distinct from the Node lifecycle generation
            # below. Keeping both names prevents a reopened-run request from impersonating another
            # request merely because every newly-created Node starts at lifecycle generation zero.
            marker["speculative"] = True
            marker["card_build_generation"] = card_build_generation
    generation = _event_generation(d)
    if type(generation) is int and generation >= 0:
        marker["generation"] = generation
    # Set BOTH the singular back-compat marker and this node's entry in the multi-build collection
    # (same dict object). A concurrent sibling's node_building overwrites `st.building` (last wins) but
    # only its OWN `st.buildings` key, so every in-flight build survives in the collection.
    st.building = marker
    st.buildings[nid] = marker

def _on_node_created(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Don't let a duplicate node_created RESURRECT a settled node (invariant #2 "first terminal
    # wins"): if the id already exists AND is in a TERMINAL state (evaluated/failed), skip the event.
    # Overwriting a terminal node installed a fresh status=pending Node, which re-armed the
    # `first_terminal` guard so a following duplicate terminal RE-added its eval_seconds to
    # total_eval_seconds (cost double-charged) and could flip a settled metric/status/feasibility
    # last-wins — the exact idempotency `_on_node_evaluated` protects the terminal against.
    # A re-emit onto a PENDING id is legitimate and MUST apply: `node_reset` (propose/implement)
    # re-opens a node to pending and the engine re-develops it in place, emitting a SECOND
    # node_created for the same id (orchestrator `_rerun_reset_node`) whose new code/idea must land
    # and clear `rerun_from` — dropping it loops the engine forever re-developing. So the guard keys
    # on terminal status, not mere existence. A clean first build has no prior node -> applies.
    # Coerce BEFORE looking up the settled lifecycle. A numeric-string duplicate ("0") names the
    # same node as integer 0 and must not bypass first-terminal-wins by missing the raw dict key.
    nid = _coerce_node_id(d)
    if nid is None:
        return
    existing = st.nodes.get(nid)
    if existing is not None and existing.status is not NodeStatus.pending:
        return
    # Defensive like the per-trial / unknown-node tolerance below: a malformed or incomplete
    # node_created (missing key, non-coercible idea param in a hand-edited / bring-your-own-script
    # log) must not crash the WHOLE fold — skip the bad event instead (normal engine/control writers
    # round-trip validated payloads, so this only fires on a corrupt or manually spliced log).
    if not _parent_generation_map_matches(st, d):
        _clear_build_marker(st, d, nid)
        return
    current = st.nodes.get(nid)
    # A generation-less abort may deliberately name the next not-yet-created slot. Only the main
    # writer can acknowledge that pre-reservation intent with this narrow marker; ordinary late
    # workers remain inert after an abort, preserving the unknown-abort resurrection fence.
    materialize_aborted_intent = bool(
        d.get("materialize_aborted_intent") is True
        and current is None
        and nid in st.aborted_nodes
        and _event_generation(d) is _MISSING
    )
    if ((nid in st.aborted_nodes and not materialize_aborted_intent)
            or (current is not None and current.tombstoned)):
        _clear_build_marker(st, d, nid)
        return
    generation = _event_generation(d)
    if generation is _MISSING:
        # Old node_created records were unstamped. On an initial create their generation is zero;
        # on a legacy in-place rebuild preserve the generation the preceding node_reset established.
        generation = current.attempt if current is not None else 0
    if generation is None or generation < 0:
        return
    if current is not None and generation != current.attempt:
        return                       # a late rebuild from a superseded lifecycle
    # `d.get("parent_ids", [])` defaults only when the KEY IS ABSENT, so an explicit
    # `"parent_ids": null` — the natural JSON spelling for a root node — reached the comprehension as
    # None and raised TypeError. This runs BEFORE the `try:` that exists to make one corrupt node row
    # survivable, so it bricked every later fold/replay/resume/view of the run instead of skipping
    # that event. Guard the type like `_on_node_repaired` and `_parent_generation_map_matches`
    # already do (fold must stay total).
    raw_parent_ids = d.get("parent_ids")
    parent_ids = [
        parent_id
        for raw_parent_id in (raw_parent_ids if isinstance(raw_parent_ids, list) else [])
        if (parent_id := _coerce_node_id({"node_id": raw_parent_id})) is not None
    ]
    speculative = d.get("speculative") is True
    raw_card_build_generation = d.get("card_build_generation")
    card_build_generation = (
        raw_card_build_generation
        if (speculative
            and bounded_int(raw_card_build_generation, 0, _CARD_REPLAY_NODE_ID_MAX))
        else None
    )
    try:
        n = Node(
            id=nid,
            parent_ids=parent_ids,
            # `_parent_generation_map_matches` proved each parent exists at this event boundary. Capture
            # that boundary even for legacy/mapless rows, otherwise a later parent reset makes provenance
            # point at replacement bytes the child never used.
            parent_generations={
                str(parent_id): st.nodes[parent_id].attempt
                for parent_id in parent_ids
            },
            operator=d["operator"],
            idea=Idea(**d["idea"]),
            code=d.get("code", ""),
            files=d.get("files", {}) or {},
            deleted=d.get("deleted", []) or [],
            attempt=generation,
            origin=d.get("origin"),   # cross-run provenance (None for ordinary nodes)
            # IN-run fork provenance: the operator branched from a node (usually while reading a
            # historical snapshot) and edited its idea. Additive with a reader-side default, so old
            # logs fold byte-identically (invariant 5).
            forked_from=d.get("forked_from"),
            research_origin=d.get("research_origin"),   # 💡 proposed just after a deep-research memo
            model_arm=str(d.get("model_arm") or "")[:64],  # doc 52 row 19: the routed model arm
            footprint_finalized=d.get("footprint_finalized") is True,
            speculative=speculative,
            card_build_generation=card_build_generation,
            # The writer's promise that this lifecycle gets a durable eval-START row before any
            # sandbox work (events/types.py::EV_NODE_EVAL_STARTED). Only a node whose creator made
            # that promise may later be REFUNDED on the absence of one; an old log carries no promise
            # and is charged. Additive + reader-defaulted -> old logs fold byte-identically.
            eval_start_boundary=d.get("eval_start_boundary") is True,
        )
    except (MemoryError, RecursionError):
        # A RESOURCE glitch is NOT a corrupt-data error: it must fail LOUD, not be swallowed.
        # A MemoryError silently caught here drops the node -> fold returns empty nodes ->
        # `_create_node` re-computes node_id=0 forever -> a 184MB node_created(0) runaway. Let
        # it propagate so a transient glitch surfaces instead of self-sustaining into a spin.
        raise
    except Exception:  # noqa: BLE001 — skip just this event (it was `continue` in the loop arm); the fold stays total
        return   # (was `continue` in the loop arm: skip just this event)
    st.nodes[n.id] = n
    _fold_node_concept_envelope(st, ctx, n, d, current)
    if current is None:
        # A holdout score is a disclosed final-exam signal. If a genuinely NEW candidate lands
        # afterwards (an inject/fork/policy action won the finish CAS race), the search has become
        # adaptive to that signal. Rotate the hidden split before any later promotion can reuse it.
        _invalidate_disclosed_holdout(st, fresh_node_ids={n.id})
        # A genuinely new candidate invalidates any confirmation/approval completed for the prior
        # candidate set — including when it is created just AFTER best_confirmed was appended.
        _invalidate_completion_certificates(st, ctx)
    _clear_build_marker(st, d, n.id)   # the real node is here now — drop the "building" marker(s)

def _nonneg_seconds(v) -> float:
    """Coerce a PERSISTED eval-cost value to a FINITE, NON-NEGATIVE float before it enters the
    cumulative budget. A hand-edited / foreign-writer log with eval_seconds="3" (str) would otherwise
    TypeError the WHOLE fold — taking down every view/replay/resume of the run — and a negative value
    would silently REDUCE total_eval_seconds, extending the budget (arch-review §5 P2). Normal engine
    emitters always produce a clean non-negative float, so this only guards malformed input."""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return f if (math.isfinite(f) and f >= 0.0) else 0.0


# One selection-affecting scalar normalizer, shared with `search/speculation_quality` (doc 25 SE-08).
_finite_metric = finite_metric


def _normalize_resource_curve(raw):
    """Coerce untrusted node_evaluated `resource_curve` event data (#7 review) to at most 32 sorted,
    unique, finite `[resource, metric]` pairs, or None. Node assignment validation is off, so a hand-
    edited / corrupt / future log could otherwise land a scalar or an arbitrarily large nested value on
    the Node despite the promised 32-point bound. Invalid entries are dropped and the <=32 bound is
    re-enforced HERE, independently of the writer. A VALID log never exceeds 32 points (the writer
    `extract_resource_curve` already caps it), so the overflow branch below fires only on already-corrupt
    input; there it keeps both endpoints via even spacing — it does NOT reproduce the writer's
    earliest-N-plus-last shape, but the shapes can only differ on input the writer could never emit."""
    if not isinstance(raw, list):
        return None
    by_resource: dict[float, float] = {}
    for entry in raw:
        if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
            continue
        r = _finite_metric(entry[0])
        v = _finite_metric(entry[1])
        if r is not None and v is not None:
            by_resource[r] = v
    if not by_resource:
        return None
    coords = sorted(by_resource)
    if len(coords) > 32:                 # corruption guard: keep both endpoints on the (invalid) overflow
        step = (len(coords) - 1) / 31
        coords = [coords[i] for i in sorted({int(round(i * step)) for i in range(32)})]
    return [[r, by_resource[r]] for r in coords]


def _charge_eval_seconds(st: RunState, kind: str, raw) -> None:
    """P1-2 budget buckets: add a coerced non-negative eval-seconds to the cumulative total AND to its
    category bucket (node|confirm). One helper so the total and the per-kind split can never drift."""
    secs = _nonneg_seconds(raw)
    st.total_eval_seconds += secs
    if secs:
        st.eval_seconds_by_kind[kind] = st.eval_seconds_by_kind.get(kind, 0.0) + secs


def _attempt_matches(n, d: dict) -> bool:
    """P0-1 attempt guard: a node terminal (node_evaluated/node_failed) is honored only if the
    `attempt` it was stamped with still matches the node's current attempt generation. `node_reset`
    bumps `n.attempt`, so a LATE terminal from an abandoned attempt (its eval was in flight when the
    reset happened) carries the OLD attempt and is dropped — it can't land as first-terminal-after-
    reset and accept a metric from discarded code (the real compute is still charged separately).
    Truly unstamped terminals predate reset generations and are accepted only for generation 0."""
    generation = _event_generation(d, legacy_attempt=True)
    # Unstamped terminals are legacy generation-0 records. Accepting one after reset would let a
    # delayed old writer impersonate the current lifecycle (ABA); all modern emitters are stamped.
    if generation is _MISSING:
        return n.attempt == 0
    return generation is not None and generation == n.attempt


def _marker_matches_event(marker: Optional[dict], d: dict, nid: int) -> bool:
    """Core generation guard shared by the singular `st.building` and each per-node `st.buildings`
    entry: only let an event clear the transient marker for the SAME node lifecycle.

    Reruns reuse node ids. A late generation-1 failure must not erase a generation-2 build marker.
    Historical markers were unstamped, so they retain the legacy id-only clear behaviour.
    """
    if not marker or marker.get("node_id") != nid:
        return False
    marker_generation = _event_generation(marker)
    if marker_generation is _MISSING:
        return True
    event_generation = _event_generation(d, legacy_attempt=True)
    return (event_generation is not _MISSING and event_generation is not None
            and event_generation == marker_generation)


def _building_matches_event(st: RunState, d: dict, nid: int) -> bool:
    """Whether `d` clears the SINGULAR back-compat `st.building` marker for `nid`
    (see `_marker_matches_event`)."""
    return _marker_matches_event(st.building, d, nid)


def _clear_build_marker(st: RunState, d: dict, nid: int) -> None:
    """Clear the transient build marker for `nid` on ITS OWN created/terminal/reset/abort event —
    BOTH the singular `st.building` (last concurrent build; back-compat) and the per-node
    `st.buildings` entry, each gated on its own generation. Under concurrent build fan-out the singular
    field holds only the last-appended build, so an EARLIER concurrent build's terminal matches its
    `st.buildings` entry but NOT the singular; keying each off its own marker is exactly what stops
    that entry from leaking a stale breathing 'building…' ghost."""
    if _building_matches_event(st, d, nid):
        st.building = None
    if _marker_matches_event(st.buildings.get(nid), d, nid):
        st.buildings.pop(nid, None)


def _parent_generation_map_matches(st: RunState, d: dict) -> bool:
    """Atomically bind a derived node to the parent lifecycles used to build it.

    The engine captures this map before a potentially slow Researcher/Developer call. If a reset or
    abort lands before node_created, replay sees the changed parent first and rejects the stale child.
    Historical events may omit the map, but their declared parents must still exist and be active.
    """
    raw = d.get("parent_generations", _MISSING)
    parent_ids = d.get("parent_ids") or []
    if not isinstance(parent_ids, list):
        return False
    expected_parents: set[int] = set()
    for raw_parent in parent_ids:
        pid = _coerce_node_id({"node_id": raw_parent})
        if pid is None:
            return False
        expected_parents.add(pid)
    if raw is _MISSING:
        return all(pid in st.nodes and pid not in st.aborted_nodes
                   and not st.nodes[pid].tombstoned for pid in expected_parents)
    if not isinstance(raw, dict):
        return False
    seen: set[int] = set()
    for raw_pid, raw_generation in raw.items():
        pid = _coerce_node_id({"node_id": raw_pid})
        generation = _event_generation({"generation": raw_generation})
        parent = st.nodes.get(pid) if pid is not None else None
        if (pid is None or generation in (_MISSING, None) or parent is None
                or parent.tombstoned or parent.attempt != generation
                or pid in st.aborted_nodes):
            return False
        seen.add(pid)
    return seen == expected_parents


def _charge_terminal_cost(st: RunState, n: Node, d: dict, ctx: "_FoldCtx") -> None:
    """Charge eval compute once per lifecycle even when its terminal arrives after a reset. Generation
    guards protect state/selection, not the cumulative budget: discarding a metric must not refund the
    process time and make repeated resets a max_eval_seconds bypass."""
    generation = _event_generation(d, legacy_attempt=True)
    if generation is _MISSING:
        # Terminals have carried `attempt` since before lifecycle-wide `generation` stamps were
        # introduced. A truly unstamped terminal is therefore a legacy generation-0 record, not the
        # node's current generation (which could have advanced after a reset). Resolving it to the
        # current value would let one delayed duplicate charge the budget again under a fresh key.
        generation = 0
    # A late result may name an older lifecycle and its real compute still counts. An unknown/future
    # lifecycle is causally impossible, though, and must not be able to poison the budget.
    if generation is None or generation > n.attempt:
        return
    key = (n.id, generation)
    if key not in ctx.charged_terminal_generations:
        ctx.charged_terminal_generations.add(key)
        _charge_eval_seconds(st, "node", d.get("eval_seconds"))


def _on_node_evaluated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    n = _node_for_event(st, d)                  # tolerate an event for an unknown/missing node
    if n is not None:
        if n.id in st.aborted_nodes:
            _charge_terminal_cost(st, n, d, ctx)
            return
        matches = _attempt_matches(n, d)
        if not matches:
            _charge_terminal_cost(st, n, d, ctx)  # stale metric ignored; real compute still spent
            return
        # Idempotent (C4): only a node's FIRST terminal event contributes its eval time, so
        # a duplicate node_evaluated/node_failed (corrupt log / double-fold) can't inflate
        # total_eval_seconds or make the budget order-dependent.
        # Invariant #2 "first terminal wins" applies to the WHOLE node, not just eval-seconds:
        # gate every field mutation on `first_terminal` so a CONFLICTING second terminal
        # (node_evaluated then node_failed, from a corrupt / double-appended log) can't flip the
        # node's metric/status/feasibility last-wins. A `node_reset` returns status to pending,
        # so a legitimate re-evaluation still applies (it IS the first terminal after the reset).
        first_terminal = n.status is NodeStatus.pending
        if first_terminal:
            # The lifecycle is settled, so no build of it is in flight: drop a marker a late
            # `node_building` left on it after its `node_created` (review 2026-09-22, EVT-11) — the
            # same generation-gated clear `_on_node_failed` already makes on the other terminal.
            _clear_build_marker(st, d, n.id)
            n.metric = _finite_metric(d.get("metric"))  # invalid/missing remains only in the raw log
            # Additive, reader-side default (invariant #5): the candidate's own number on a
            # host-scored node (doc 52 row 10a); an old log has no key and folds to None.
            n.self_metric = _finite_metric(d.get("self_metric"))
            n.status = NodeStatus.evaluated
            n.terminal_event_seq = e.seq
            n.rerun_stage = None                # any stage-scoped re-run has now landed
            n.stdout_tail = d.get("stdout_tail", "")
            # WHY this node scored what it scored, in the eval's own words — see
            # `engine/evaluate.py::_scored_output_evidence`. Reader-defaulted to "" so every log
            # written before the column existed folds byte-identically; `str()` because assignment
            # validation is off and a corrupt/hand-edited row must not land a non-string here where
            # `run_tools._logs` will `.rstrip()` it.
            n.stderr_tail = str(d.get("stderr_tail", "") or "")
            # ASHA past-experiment curve (#7): a bounded [[resource, metric], ...] the ASHA watchdog
            # reads to find same-resource peers for an EARLY live sample (the 4,000-char stdout_tail keeps
            # only the final epochs). Reader-defaulted to None so pre-#7 logs fold byte-identically.
            # NORMALIZED (#7 review): assignment validation is off, so an untrusted/corrupt event could
            # otherwise land a scalar or huge nested value here despite the 32-point bound; coerce it.
            n.resource_curve = _normalize_resource_curve(d.get("resource_curve"))
            n.eval_seconds = d.get("eval_seconds")
            n.extra_metrics = normalize_extra_metrics(d.get("extra_metrics"))
            # WHICH CHANNEL each extra metric came through. Additive with a reader-side default
            # (invariant #5): absent on every log written before 2026-08-14 -> `{}` -> every key
            # answers `EXTRA_METRIC_UNKNOWN`. That default is deliberately NOT `declared`: all 12
            # extra metrics preserved in `runs/` came from the UNDECLARED auto-capture channel, so
            # reading an untagged historical value as operator-declared would state the one thing
            # that is provably false about it.
            n.extra_metrics_provenance = normalize_extra_metric_channels(
                d.get("extra_metrics_provenance"))
            # ...and WHICH WAY IS BETTER on each, folded the same way and defaulting the same way:
            # absent -> `{}` -> every key answers `EXTRA_METRIC_DIRECTION_UNKNOWN`. Not defaulting
            # to a direction is the same discipline as not defaulting to `declared` one line up —
            # every extra metric preserved in `runs/` was recorded with no direction at all, and
            # picking one for them would state a fact nobody measured.
            n.extra_metrics_direction = normalize_extra_metric_directions(
                d.get("extra_metrics_direction"))
            n.violations = d.get("violations", []) or []
            n.feasible = not n.violations       # #5: constraint-violating -> infeasible
            # Additive with a reader-side default: an old log has no such key and folds to None,
            # which is what a measured metric means here.
            _prov = d.get("metric_provenance")
            n.metric_provenance = _prov if isinstance(_prov, dict) else None
            # Intra-node sweep: per-trial results (audit/UI only; node.metric is already the
            # best trial, set by the engine). Coerce defensively per trial so one malformed
            # entry in a hand-edited/bring-your-own-script log can't crash the whole fold.
            # Event.data is untyped and assignment validation is off, so `trials` itself can arrive as a
            # bare scalar (int/float/bool); the per-trial try/except only guards a bad ITEM, so a
            # non-iterable CONTAINER would raise TypeError OUTSIDE it and poison every replay/resume with
            # no JSON divergence for repair-log to see. Require a list/tuple before iterating — the same
            # defence the resource_curve normalization above applies for the identical reason.
            _raw_trials = d.get("trials", [])
            trials = []
            for t_d in (_raw_trials if isinstance(_raw_trials, (list, tuple)) else []):
                try:
                    trials.append(Trial(**t_d))
                except Exception:  # noqa: BLE001 — a malformed trial row is skipped, never allowed to break the fold
                    continue
            n.trials = trials
            _charge_terminal_cost(st, n, d, ctx)


# DERIVED, not spelled. This set and `serve/attention.py`'s owner-alert filter are the same
# judgement — "this node ended for a reason that says nothing about the experiment" — and were
# hand-written twice; both carried `cancelled`, which no terminal writer mints, so each held one
# word that could never match and neither could tell. See `core/models.py::BENIGN_TERMINAL_REASONS`.
#
# THE UNIFICATION MOVED THIS SET, AND SAYING ONLY THE `cancelled` HALF UNDERSTATED IT. The two
# hand-written copies were not the same: `attention.py` also carried `frozen` and this one did not,
# so taking the shared set ADDED a live reason here. `frozen` is minted by
# `engine/speculation.py::_fail_reserved_build` when a speculative build is terminalized by a
# transient pause/stop/budget crossing — the engine's own doing, at a moment the run is already
# stopping — which is exactly the judgement this set encodes, so the two readers agreeing is the
# correct end state and `attention.py` was the one that had it right.
#
# BUT IT CHANGES FOLDED STATE ON A PRESERVED LOG, which is why it is written down rather than left
# to the shared set's docstring. `_counts_as_current_failure` feeds `_add_current_failure`, so
# `RunState.current_failure_count`, `failure_spike_level` and `failure_spike_seq` all move on any
# log containing a `frozen` terminal, and the consecutive-failure breaker no longer counts one. All
# three fields are `Field(exclude=True)`, so a corpus check that digests `model_dump()` cannot see
# this at all — the reason it went unnoticed. `tests/test_failure_spike_ignores_frozen.py` pins the
# membership deliberately.
_FAILURE_SPIKE_IGNORED_REASONS = set(BENIGN_TERMINAL_REASONS)


def _counts_as_current_failure(st: RunState, n: Node) -> bool:
    return (n.status is NodeStatus.failed and not n.tombstoned and n.id not in st.aborted_nodes
            and str(n.error_reason or "").strip().lower() not in _FAILURE_SPIKE_IGNORED_REASONS)


def _add_current_failure(st: RunState, n: Node, event: Event) -> None:
    if not _counts_as_current_failure(st, n):
        return
    st.current_failure_count += 1
    level = st.current_failure_count // 3
    if level > st.failure_spike_level:
        st.failure_spike_seq = event.seq
    st.failure_spike_level = level


def _remove_current_failure(st: RunState, n: Node) -> None:
    if not _counts_as_current_failure(st, n):
        return
    st.current_failure_count = max(0, st.current_failure_count - 1)
    st.failure_spike_level = st.current_failure_count // 3


def _on_node_failed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    n = _node_for_event(st, d)
    nid = _coerce_node_id(d)
    if nid is not None:
        _clear_build_marker(st, d, nid)
    if n is not None:
        if n.id in st.aborted_nodes and d.get("reason") != "aborted":
            _charge_terminal_cost(st, n, d, ctx)
            return
        matches = _attempt_matches(n, d)
        if not matches:
            _charge_terminal_cost(st, n, d, ctx)
            return
        # First-terminal-wins for the whole node (see node_evaluated above): a conflicting
        # second terminal from a corrupt log must not flip an already-evaluated node to failed.
        first_terminal = n.status is NodeStatus.pending
        if first_terminal:
            n.status = NodeStatus.failed
            n.terminal_event_seq = e.seq
            n.error = d.get("error", "")
            # COERCED, because assignment skips pydantic validation and `_card_debuggable_leaf_ids`
            # later does `node.error_reason not in {"idea_rejected", "card_dropped"}` — a SET
            # membership — inside `_derive_cards`. One node_failed row carrying an unhashable reason
            # (a list/dict from a forged, foreign or hand-edited log) therefore made every
            # fold/replay/resume of that run raise TypeError, forever. Same totality rule the rest of
            # this handler follows.
            _reason = d.get("reason", "")
            n.error_reason = _reason if isinstance(_reason, str) else str(_reason)
            # Crash-triage verdict, when the LLM triage ran (signal-delivery §1): fold it onto
            # the node so the failure-reflection hint / digest can hand it to the next proposal.
            # Additive + reader-defaulted: absent on old logs / rule-triaged nodes -> stays "".
            if d.get("triage_rationale"):
                n.triage_rationale = str(d.get("triage_rationale"))
            n.eval_seconds = d.get("eval_seconds")
            # Durable "no evaluation was ever dispatched for this lifecycle" receipt (Node.
            # never_evaluated). Additive + reader-defaulted: absent on old logs -> False -> the budget
            # accounting folds byte-identically. Only the writers that terminalize a build BEFORE
            # dispatch stamp it, and it rides this single terminal, so "first terminal wins" above is
            # the whole of its order-tolerance argument.
            n.never_evaluated = d.get("never_evaluated") is True
            n.rerun_from = None
            n.rerun_stage = None                # any stage-scoped re-run has now landed
            if d.get("failed_stage"):
                n.failed_stage = d.get("failed_stage")   # Phase 1: which pipeline stage broke
            _charge_terminal_cost(st, n, d, ctx)
            _add_current_failure(st, n, e)

def _on_node_eval_started(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """The durable eval-START boundary: this exact lifecycle entered the sandbox.

    Generation-keyed and split into two receipts. ``eval_started`` is durable/set-only and answers
    "did anything ever run in this lifecycle?" for budget recovery. ``eval_activity_started``
    answers "did the current engine owner admit it?" for the live UI; a resume-owner boundary clears
    only that half so a real re-dispatch can stamp it again. Carries no cost —
    `_charge_terminal_cost` still owns eval seconds.
    """
    n = _node_for_event(st, d)
    if n is not None and _generation_matches(n, d):
        n.eval_started = True
        # First receipt for THIS owner wins. A duplicate admission call in one invocation is a no-op,
        # while a receipt after resume legitimately starts a fresh live clock.
        if n.eval_activity_started is not True:
            n.eval_activity_started = True
            n.eval_started_at = e.ts if e.ts > 0 else None


def _clear_eval_activity_for_new_owner(st: RunState) -> None:
    """Forget only live ownership; preserve the durable proof that compute was already spent."""

    for n in st.nodes.values():
        if n.status is NodeStatus.pending:
            n.eval_activity_started = False
            n.eval_started_at = None

# `engine/metric_salvage.py::SALVAGE_CAUSE_TRIAGE_ACTION`, spelled rather than imported: `events`
# imports only `core` (see the log-role note in `engine/train_monitor.py` for the same rule in the
# other direction), and pulling the engine in here to read one string would invert the layering.
# `tests/test_events_replay.py` pins that the two agree, which is the same treatment every other
# cross-layer literal in this module gets.
_SALVAGE_CAUSE_TRIAGE_ACTION = "salvage_cause_fix"


_REPAIR_LEDGER_MAX = 200
# ...and no single node may take more than this share of it. The global cap alone is first-come, so
# ONE node that repairs pathologically often consumes the whole ledger: measured, a node with 2,345
# repair rows would fill all 200 slots before any other node recorded one, and the ledger's entire
# purpose is telling a LATER node what a SIBLING had to fix. A per-node bound is what keeps it a
# cross-node channel rather than a transcript of the worst node's first two hundred attempts.
_REPAIR_LEDGER_MAX_PER_NODE = 20
_REPAIR_LEDGER_RATIONALE_CAP = 400


def _record_repair_ledger(st: RunState, d: dict, ctx: "_FoldCtx") -> None:
    """Append one row to the cross-node repair ledger — see `RunState.repair_ledger` for why it
    exists and what it deliberately does NOT do.

    Recorded OUTSIDE the pending/generation guard below on purpose: that guard protects the node's
    own CODE from a duplicate or post-terminal row, and this records a fact about the run rather
    than mutating a node. Idempotence is provided instead by the (node, attempt, generation) key, so
    a double-fold collapses to the same single row and replay stays a pure function of the log.

    THE KEY LIVES ON THE FOLD CONTEXT, not on `st.repair_ledger`, and that is what makes the
    guarantee above true for a DROPPED row too. A row the caps refused is not in the ledger, so a
    scan of the ledger could never recognise its duplicate: a re-folded or duplicated
    `node_repaired` past a cap re-incremented `repair_ledger_omitted` and the CLI's "N dropped"
    over-reported — the exact honesty the counter was added for. `_on_node_repaired`'s own comment
    names "a duplicate or post-terminal `node_repaired` (corrupt/double-fold)" as the case it
    defends against.

    The context is also where the counting belongs. The per-node tally used to `sum()` the entire
    ledger before the O(1) global cap was even consulted, so once the ledger was full every
    remaining row paid a 200-entry scan to reach a decision the length check had already made —
    +18 ms per fold on the repair-heavy corpus run, paid on every state poll of a live run.
    """
    node_id = d.get("node_id")
    attempt = d.get("attempt")
    generation = d.get("generation")
    if type(node_id) is not int:
        return
    key = (node_id, attempt, generation)
    # A KEY THE FOLD CANNOT HASH IS A ROW THIS LEDGER CANNOT RECORD, and it must not raise: no
    # handler runs under per-event containment, so the TypeError a list/dict `attempt` or
    # `generation` raised at the set lookup below escaped `fold` — one hand-edited or corrupt
    # `node_repaired` row made every fold, `looplab replay` and resume of its run fail. Found by the
    # EVT-12 split's hostile-payload fuzz (review 2026-09-22), identically on the pre-split code.
    # The ledger records well-formed repairs; the node's own code handling below is untouched.
    try:
        if key in ctx.repair_ledger_keys:
            return
    except TypeError:
        return
    ctx.repair_ledger_keys.add(key)
    # BOTH bounds, and each records what it dropped. A silent cap made the CLI print 200 as a total
    # and let `lessons_reconcile` generalize over a truncated population — see
    # `RunState.repair_ledger_omitted`. The cheap bound is asked first.
    if (len(st.repair_ledger) >= _REPAIR_LEDGER_MAX
            or ctx.repair_ledger_per_node.get(node_id, 0) >= _REPAIR_LEDGER_MAX_PER_NODE):
        omitted = st.repair_ledger_omitted
        omitted["rows"] = int(omitted.get("rows", 0)) + 1
        nodes = omitted.setdefault("nodes", {})
        # Keyed by the node's STRING id: this dict is serialized to JSON in every projection, where
        # an integer key becomes a string anyway — so folding to one spelling here keeps a replayed
        # state equal to a round-tripped one.
        nodes[str(node_id)] = int(nodes.get(str(node_id), 0)) + 1
        return
    # `changed` is the path list the repair itself declared; fall back to the keys of `files` so a
    # row written before that column existed still names what it touched.
    paths = d.get("changed")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        paths = sorted((d.get("files") or {}).keys()) if isinstance(d.get("files"), dict) else []
    rationale = d.get("rationale")
    ctx.repair_ledger_per_node[node_id] = ctx.repair_ledger_per_node.get(node_id, 0) + 1
    st.repair_ledger.append({
        "node_id": node_id,
        "attempt": attempt,
        "generation": generation,
        "reason": d.get("reason") if isinstance(d.get("reason"), str) else None,
        "paths": [p for p in paths][:40],
        "rationale": (rationale[:_REPAIR_LEDGER_RATIONALE_CAP]
                      if isinstance(rationale, str) else None),
    })


def _on_node_repaired(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # In-node inline repair (hybrid crash repair): a NON-terminal event that replaces the
    # node's code with the LLM-repaired version BEFORE the eval that follows it. Idempotent
    # and replay-safe: only mutates while the node is still pending (the single terminal
    # event emitted at the end of the repair loop flips status off pending), so a duplicate
    # or post-terminal node_repaired (corrupt/double-fold) is a no-op — mirrors the
    # `first_terminal` guard above. The LLM/subprocess are never re-invoked; the final code
    # and metric/status are reconstructed purely from this event + the terminal event.
    _record_repair_ledger(st, d, ctx)
    n = _node_for_event(st, d)
    if (n is not None and n.id not in st.aborted_nodes and not n.tombstoned
            and _generation_matches(n, d)
            and n.status is NodeStatus.pending):
        n.code = d.get("code", n.code)
        if d.get("files"):
            n.files = d["files"]
        if d.get("deleted"):
            n.deleted = d["deleted"]
        if isinstance(d.get("idea_footprint"), dict):
            footprint = normalize_researcher_footprint(d["idea_footprint"])
            if footprint is not None:
                n.idea = n.idea.model_copy(deep=True, update={"footprint": footprint})
        if d.get("footprint_finalized") is True:
            n.footprint_finalized = True
        # THE REPAIR EPOCH. Advanced here and nowhere else, so that `_on_stage_finished` below can
        # stamp every stage row with the attempt it belongs to and a reader can tell a superseded
        # row from a current one (`core/models.py::stage_row_superseded`). It is derived from the
        # log's ORDER rather than from a new event column on purpose: `stage_finished` has never
        # carried a repair counter, so a writer-side column would answer this only for rows written
        # after the change while the four affected runs already on disk stayed unattributable.
        #
        # It is the row's OWN durable ordinal, and a row with no `attempt` (or a non-int/bool one
        # — `True` is an `int` in python and would read as epoch 1) falls back to advancing by
        # one, which is what a log written before the ordinal existed can support.
        # WHY NOT PLAIN `max`. The ordinal only rises monotonically since `_durable_repair_ledger`;
        # before it the engine restarted `attempt` per PROCESS, and that function's own measured
        # corpus is eight durable rows reading [1,2,1,2,1,2,1,2]. Under `max` that log ends at
        # `repairs == 2` after eight repairs — rows 3..8 advance the epoch by zero, the last failed
        # stage row carries epoch 2 == `Node.repairs`, and `stage_row_superseded` answers False
        # where the monotone control answers True. That is the stale-row defect reappearing on
        # exactly the historical logs this retroactive derivation exists for.
        #
        # So: an ordinal that EXCEEDS the counter is taken (the modern monotone shape, and it also
        # absorbs a gap); one that does not is read as a per-process restart and advances by one.
        # IDEMPOTENCE (invariant #5) is no longer carried by `max` — which is what made the two
        # indistinguishable — but by the SEQ, which is what a duplicate or a re-folded row really
        # shares: `charged_repair_seqs` is the same shape as the ledger's other de-dup sets. A
        # `salvage_cause_fix` row still charges nothing at all, because it deliberately RE-STATES
        # the ordinal it follows ("the ORDINAL this row FOLLOWS, not a new one" —
        # `engine/evaluate.py`) rather than opening an epoch.
        _seq_key = (n.id, getattr(e, "seq", None))
        if _seq_key[1] is None or _seq_key not in ctx.charged_repair_seqs:
            if _seq_key[1] is not None:
                ctx.charged_repair_seqs.add(_seq_key)
            if str(d.get("triage_action") or "") != _SALVAGE_CAUSE_TRIAGE_ACTION:
                _ordinal = d.get("attempt")
                if isinstance(_ordinal, int) and not isinstance(_ordinal, bool) \
                        and _ordinal > n.repairs:
                    n.repairs = _ordinal
                else:
                    n.repairs += 1


def _requeue_partition_bound_results(st: RunState, *, fresh_node_ids: set[int]) -> None:
    """Make every surviving incumbent comparable on the newly-hidden partition.

    Host grading derives the ordinary search metric *and* every confirmation seed from the
    complement of ``_holdout_idx``.  Rotating that index while retaining those values mixes two
    different datasets in one ranking.  Re-open each evaluated incumbent as a fresh lifecycle so
    the normal eval path materializes its unchanged code on the new complement.  The generation
    bump is essential: it makes late epoch-N workers inert and gives the repeated physical eval its
    own cost-accounting key.  Nodes created/reset by the event that opened this epoch are already
    fresh and are excluded by ``fresh_node_ids``.
    """
    requeued: set[int] = set()
    for n in st.nodes.values():
        if (n.id in fresh_node_ids or n.id in st.aborted_nodes or n.tombstoned
                or n.status is not NodeStatus.evaluated):
            continue
        n.attempt += 1
        n.status = NodeStatus.pending
        n.terminal_event_seq = None
        n.metric = None
        n.error = ""
        n.error_reason = ""
        n.triage_rationale = ""
        n.stdout_tail = ""
        # ...and the eval's own account of the number that attempt produced. Reset with its
        # sibling above: a re-evaluated node that keeps the PREVIOUS attempt's stderr shows the
        # loop a reason for a metric that no longer exists.
        n.stderr_tail = ""
        n.resource_curve = None            # #7: the prior attempt's curve no longer describes this node
        n.eval_seconds = None
        n.never_evaluated = False          # the discard receipt described the prior attempt
        n.eval_started = False             # ...and so did the eval-start boundary
        n.eval_activity_started = False
        n.eval_started_at = None
        n.extra_metrics = {}
        # ...and so did the CHANNEL map describing where those extras came from.
        n.extra_metrics_provenance = {}
        # ...and so did the DIRECTION map saying which way was better on them.
        n.extra_metrics_direction = {}
        # ...and so did the RECONSTRUCTION marker. It describes the map this reset just cleared, and
        # a stale one would mark a later LIVE measurement as backfilled — the exact inversion, with
        # the sign flipped.
        n.extra_metrics_backfill = {}
        n.violations = []
        n.feasible = True
        # WHERE THE OLD METRIC CAME FROM described the old metric, which this epoch just cleared.
        # Left set, a node reset then failed read `metric=None, status=failed,
        # metric_provenance={salvaged: True}` — a provenance record for a value that no longer
        # exists, on the one field a reader consults to decide whether to trust the number.
        n.metric_provenance = None
        n.trials = []
        n.confirmed_mean = None
        n.confirmed_std = None
        n.confirmed_seeds = None
        n.confirmed_ruler = None
        n.holdout_metric = None
        n.generalization_gap = None
        n.verifier_score = None   # R1-c: a soundness score judged the OLD attempt's result — discard it
        n.stages = []
        n.failed_stage = None
        n.repairs = 0            # a fresh lifecycle's repair budget starts at zero, and so does the
        #                          epoch its stage rows are stamped with (there are none left above)
        n.rerun_from = None
        n.rerun_stage = None
        requeued.add(n.id)

    if not requeued:
        return
    for nid in requeued:
        st.confirm_seed_results.pop(nid, None)
        # The eval-NOISE probe's per-seed memo resets with the node for the reason the confirm memo
        # does (see `_requeue_reset_node`): the probe memo-skips every seed already recorded, so a
        # stale entry would summarize PRE-reset metrics as the spread of post-reset code without
        # running a single repeat. The `eval_noise_floor` summary is deliberately NOT cleared — it
        # names the generation it measured, a spread already measured does not become false, and
        # re-measuring would spend the eval seconds again.
        st.eval_noise_seed_results.pop(nid, None)
        st.proxy_scores.pop(nid, None)
    st.proxy_skipped = [nid for nid in st.proxy_skipped if nid not in requeued]
    _purge_node_requests(st, requeued)
    st.policy_scores = {}
    st.policy_chosen = None
    st.policy_reason = ""


def _rotate_search_epoch(st: RunState, *, requeue_partition_scores: bool,
                         fresh_node_ids: set[int] | None = None) -> None:
    """Advance one epoch and invalidate every value bound to the disclosed partition."""
    st.search_epoch += 1
    st.holdout_evaluated_ids.clear()
    st.holdout_epoch_aware = False   # the disclosure is consumed; the new epoch has none yet
    for candidate in st.nodes.values():
        if candidate.tombstoned or candidate.id in st.aborted_nodes:
            continue                         # post-hoc audit evidence is not part of the new pool
        if candidate.holdout_metric is not None:
            candidate.verifier_score = None  # it judged the disclosed holdout evidence being invalidated
        candidate.holdout_metric = None
        candidate.generalization_gap = None
    if requeue_partition_scores:
        _requeue_partition_bound_results(st, fresh_node_ids=fresh_node_ids or set())


def _invalidate_disclosed_holdout(
        st: RunState, *, fresh_node_ids: set[int] | None = None) -> bool:
    """Close a disclosed epoch once active search changes again."""
    if not st.holdout_evaluated_ids:
        return False
    # Requeue every incumbent (wiping its metric to force a re-eval on the newly-hidden complement)
    # ONLY when the disclosed holdout was epoch-aware. A legacy (pre-search-epoch) disclosure must
    # rotate WITHOUT the metric wipe, or replaying an old holdout_select log would drop incumbents the
    # pre-batch fold left intact and change the selected best (invariant 5b, F2).
    _rotate_search_epoch(
        st, requeue_partition_scores=st.holdout_epoch_aware, fresh_node_ids=fresh_node_ids)
    return True


def _clear_approval(st: RunState) -> None:
    """Retract the operator's ratification AND any request still waiting for one.

    Both halves are needed together. Leaving `approved` set hands a stale grant to a candidate set
    the operator never saw; leaving `awaiting_approval` set with the subject gone parks the run on a
    question about a node that no longer exists. The subject/generation/node_id fields are what the
    approval was ABOUT, so they go with it — a retained `approval_subject` would let a later grant
    attach to the wrong node.
    """
    st.approved = False
    st.awaiting_approval = False
    st.approval_subject = None
    st.approval_generation = None
    st.approved_node_id = None


def _invalidate_completion_certificates(st: RunState, ctx: "_FoldCtx") -> None:
    """Retire every "this search is finished" certificate because the candidate set just changed.

    A confirmation and an approval are both statements about a SPECIFIC set of candidates: "these
    were re-measured and this one won", "the operator ratified this one". A new candidate, a
    tombstone, a reset, an abort, or a reopen all change that set, so both statements stop being
    true — and neither is re-derived, they are carried until something clears them.

    Two things must be cleared together, and this is the whole reason the sequence has one home
    (doc 25 EV-03). `st.confirmed_done` is the FOLDED flag that lets the confirm phase re-run;
    `ctx.best_confirmed` is the THREADED snapshot `_select_best`'s confirm-override reads. Clearing
    only the flag leaves the override live, and an epoch-(N-1) certificate then keeps beating
    epoch-N's metric winner — which is exactly the selection bug the reopen site shipped while
    these five copies were kept in step by hand.
    """
    st.confirmed_done = False
    ctx.best_confirmed = None
    _clear_approval(st)


def _on_score_metrics_backfilled(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Apply objectives a score stage MEASURED and the record never kept.

    THE SAME RULE AS ITS SIBLING, and the whole safety of the mechanism: **a live record always
    wins.** This writes only where the node's `extra_metrics` is empty. A backfill re-reads a
    `score.log` long after the eval, and a reconstruction may never overwrite a measurement made
    while the run was happening — which is also what makes a second pass idempotent by CONSTRUCTION
    rather than by a check that could drift.

    NO DIRECTION IS EVER WRITTEN HERE, deliberately, and the omission is the point. Nobody declared
    which way was better when these evals ran; orientation is a forward-looking declaration an
    operator makes in `eval.metrics`, and asserting it retroactively would present a reconstruction
    as a measurement. The consequence is intended: a ranking surface declines to order an axis it
    cannot orient (`ui/src/panels.jsx::paretoFront`), so these values are readable everywhere and
    decide nothing.

    The CHANNEL is `declared`: an operator-owned reader spec is not what produced them, but neither
    is the candidate's stdout scrape — they were printed by the operator's own scoring program and
    recovered from the log the engine itself preserved. `EXTRA_METRIC_ENGINE` would claim the engine
    wrote the print statement, which is false. `declared` is the honest one of the three, and the
    `backfilled` marker beside it is what stops any surface calling this a live measurement.
    """
    node_id = _coerce_node_id(d)
    node = st.nodes.get(node_id) if node_id is not None else None
    if node is None or node.metric is None:
        return
    # THE LIFECYCLE IT WAS READ FOR (review 2026-09-22, EVT-09). `generation` is a REQUIRED key of
    # this row and nothing read it: a backfill planned against lifecycle 0 and applied after a reset
    # and a re-evaluation wrote lifecycle 0's recovered objectives onto lifecycle 1's metric —
    # a reconstruction of one experiment presented beside another's number. An unstamped row is a
    # legacy one and binds as it always did (`event_generation_binds`).
    if not _generation_matches(node, d):
        return
    if node.extra_metrics:
        return                      # a LIVE record. Never overwritten. This is the idempotence.
    found = d.get("extra_metrics")
    if not isinstance(found, dict) or not found:
        return
    node.extra_metrics = normalize_extra_metrics(found)
    node.extra_metrics_provenance = normalize_extra_metric_channels(
        {k: EXTRA_METRIC_DECLARED for k in node.extra_metrics})
    # THE MARKER THE DOCSTRING PROMISES, and it did not exist in folded state until 2026-09-02.
    # The sibling handler below stamps `backfilled: true` into the record it folds, so a
    # reconstruction is legible as one on every surface; this one folded values plus a bare
    # `declared` channel, and the marker — with the per-key decimals the writer argues a reader
    # "must not have to guess" — lived only on the raw event row, which no surface reads. A
    # recovered 2-decimal nDCG@100 therefore rendered on the extras table, the exports and
    # `read_experiment` exactly like a live operator-declared measurement, and v4's nodes 0 and 1
    # — equal on every recovered row ONLY because the print statement cannot separate them — read
    # as MEASURED ties. That is the reconstruction-presented-as-measurement inversion both backfill
    # docstrings exist to refuse, committed by the one handler of the pair that promised otherwise.
    #
    # The channel STAYS `declared` and that is deliberate: the operator's own scoring program
    # printed these numbers, so `auto` and `engine` are both false about them. What was missing was
    # never the channel — it was the second, orthogonal fact that the value was recovered from a log
    # afterwards rather than recorded while the run was happening.
    node.extra_metrics_backfill = normalize_extra_metric_backfill({
        "backfilled": True,
        "backfilled_at": d.get("read_at"),
        "precision_decimals": d.get("precision_decimals"),
    })
    # ...and NOT `extra_metrics_direction`. See the docstring: the axis stays unorientable because
    # nothing in this run ever said which way is better about it.


def _on_applied_params_backfilled(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Apply a REPAIRED applied-params record to a node whose evaluation predates the live one.

    THE RULE, and it is the whole safety of the mechanism: **a live record always wins.** This
    handler writes only where `metric_provenance.applied_params` is absent or None. A backfill is a
    reconstruction — it re-reads the workdir long after the eval, on a tree the eval may have
    rewritten — and a reconstruction may never overwrite a measurement made while the run was
    happening. That also makes re-running the backfill idempotent by CONSTRUCTION rather than by a
    check that could drift: the second pass finds the record it wrote and declines.

    A node with no `metric_provenance` at all gets none: this event repairs what a metric SAYS about
    itself, and a node with no metric has nothing to say. `applied_params: null` with an
    `unrecoverable` reason IS a real answer and is stored as one — "the workdir is gone, so what ran
    cannot be recovered" must be legible, because the alternative is a reader falling back to the
    proposal and calling it fact.

    Every write carries `backfilled: true` and the reason it was possible. No surface may present a
    reconstruction as a measurement, and the flag is how a surface tells them apart.
    """
    # `_coerce_node_id` takes the ROW, not the value — it is the fold's own guard against a forged
    # `{"node_id": [999]}` (unhashable), a bool (`int(True) == 1` would match node 1) and a
    # non-integral float, and passing it a bare value silently defeats all three.
    node_id = _coerce_node_id(d)
    node = st.nodes.get(node_id) if node_id is not None else None
    if node is None or not isinstance(node.metric_provenance, dict):
        return
    # Bound to the lifecycle the workdir was read for, exactly like the sibling above (review
    # 2026-09-22, EVT-09): a row stamped for a superseded generation describes a tree that no
    # longer produced this node's metric. Unstamped = legacy, as `event_generation_binds` says.
    if not _generation_matches(node, d):
        return
    if node.metric_provenance.get("applied_params") is not None:
        return                      # a LIVE record. Never overwritten. This is the idempotence.
    record = d.get("applied_params")
    unrecoverable = str(d.get("unrecoverable") or "").strip()
    if isinstance(record, dict) and record:
        stamped = dict(record)
        stamped["backfilled"] = True
        stamped["backfilled_at"] = d.get("read_at")
        stamped["backfilled_from"] = str(d.get("workdir_digest") or "")[:64]
    elif unrecoverable:
        # NOT an empty record. An empty one is a claim ("the configuration said nothing about
        # anything you declared"); this is the absence of an answer, and the two are opposite facts.
        stamped = {"backfilled": True, "backfilled_at": d.get("read_at"),
                   "unrecoverable": unrecoverable[:200]}
    else:
        return
    prov = dict(node.metric_provenance)
    prov["applied_params"] = stamped
    node.metric_provenance = prov


def _on_node_tombstoned(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Append-only delete (§6.3): mark the listed node ids (a node + its descendant subtree, computed
    # by the writer so the fold stays a pure, order-tolerant set op) as logically deleted. They REMAIN
    # in st.nodes — so parent links still resolve, node-id allocation never reuses the id, and the
    # delete is reversible/auditable — but the evaluated/feasible/breedable/pending helpers skip a
    # tombstoned node, so it is excluded from best-pick, breeding, confirmation, and re-eval.
    # Idempotent: setting the flag twice (duplicate/overlapping tombstone events) is a no-op. Ids
    # coerced defensively — a forged/unhashable id in a hand-edited log is skipped, not a fold crash.
    affected: set[int] = set()
    # `node_ids` MUST be a list. A forged/hand-edited event with a truthy SCALAR (e.g. {"node_ids": 42})
    # would make `42 or []` -> `42` and `for raw in 42` raise TypeError — and the fold loop has no
    # per-event try/except, so that one bad record bricks EVERY replay/resume/view of the run. Guard the
    # type like `_parent_generation_map_matches` already does for `parent_ids` (fold must stay total).
    raw_ids = d.get("node_ids")
    for raw in (raw_ids if isinstance(raw_ids, list) else []):
        nid = _coerce_node_id({"node_id": raw})
        n = st.nodes.get(nid) if nid is not None else None
        if n is not None and not n.tombstoned:
            _remove_current_failure(st, n)
            n.tombstoned = True
            n.rerun_from = None
            n.rerun_stage = None
            affected.add(n.id)
    if not affected:
        return
    # Remove only references/actions that name deleted lifecycles. A post-hoc delete of an already
    # finished run is an audit edit, not an implicit search reopen: the finish/report/finalization and
    # unaffected node evidence remain intact until an explicit resume creates the next epoch.
    _purge_node_requests(st, affected)
    if st.champion in affected:
        st.champion = None
    if st.approval_subject in affected:
        st.awaiting_approval = False
        st.approval_subject = None
        st.approval_generation = None
    if st.approved_node_id in affected:
        st.approved = False
        st.approved_node_id = None
    if st.pause_node_id in affected:
        st.paused = False
        st.pause_node_id = None
        st.pause_generation = None
        st.pause_reason = None
    if st.building and st.building.get("node_id") in affected:
        st.building = None
    for _aff in affected:
        st.buildings.pop(_aff, None)   # a tombstoned subtree may hold several in-progress builds
    if st.finished:
        if ctx.best_confirmed in affected:
            ctx.best_confirmed = None
        return

    # During an active search the candidate-set mutation invalidates completion certificates. If a
    # holdout was already disclosed, rotate now and re-evaluate every surviving incumbent.
    _invalidate_completion_certificates(st, ctx)
    _invalidate_disclosed_holdout(st)

def _on_node_reset(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Re-run an EXISTING node in place (no new id). Discard its state FROM `from_stage` so it
    # becomes pending again; the engine then re-runs just that stage, appending fresh events for
    # the SAME id (which land as the first-terminal-after-reset). Replay-safe: the reset marks
    # where the old lifecycle is abandoned. `eval` = keep idea+code, just re-score (the normal
    # eval loop picks a pending-with-code node up — no marker). `implement`/`propose` = also drop
    # the code and flag `rerun_from` so the engine re-develops (re-proposes for `propose`).
    n = _node_for_event(st, d)
    if n is not None and not n.tombstoned and _control_generation_matches(n, d):
        _remove_current_failure(st, n)
        was_finished = st.finished
        holdout_was_disclosed = bool(st.holdout_evaluated_ids)
        old_generation = n.attempt
        stage = d.get("from_stage", "eval")
        # Bump the attempt generation (P0-1): the engine stamps this on the re-eval's terminal, and a
        # LATE terminal from the attempt this reset abandons carries the OLD generation and is dropped
        # by `_attempt_matches` — so an in-flight pre-reset eval can't land its metric on the new code.
        n.attempt += 1
        # …and the INLINE-REPAIR epoch restarts with it, for exactly the reason the generation
        # bump exists: `engine/evaluate.py::_durable_repair_ledger` is generation-scoped, so the
        # new lifecycle's repair budget genuinely begins at zero and a counter that carried over
        # would report a node as repaired by work no longer in its own lifecycle. The stage rows an
        # eval-type reset RETAINS are re-stamped to the fresh epoch below, where they are chosen.
        n.repairs = 0
        if st.pause_node_id == n.id and st.pause_generation == old_generation:
            st.paused = False
            st.pause_node_id = None
            st.pause_generation = None
            st.pause_reason = None
        n.status = NodeStatus.pending
        n.terminal_event_seq = None
        n.metric = None
        n.error = ""
        n.error_reason = ""
        n.triage_rationale = ""   # the crash-triage verdict describes the NOW-abandoned lifecycle
        n.eval_seconds = None
        n.never_evaluated = False   # the discard receipt described the NOW-abandoned lifecycle
        n.eval_started = False      # ...and so did the eval-start boundary
        n.eval_activity_started = False
        n.eval_started_at = None
        n.stdout_tail = ""
        # ...and the eval's own account of the number that attempt produced. Reset with its
        # sibling above: a re-evaluated node that keeps the PREVIOUS attempt's stderr shows the
        # loop a reason for a metric that no longer exists.
        n.stderr_tail = ""
        n.resource_curve = None            # #7: the abandoned attempt's curve no longer describes this node
        n.extra_metrics = {}
        # ...and so did the CHANNEL map describing where those extras came from.
        n.extra_metrics_provenance = {}
        # ...and so did the DIRECTION map saying which way was better on them.
        n.extra_metrics_direction = {}
        # ...and so did the RECONSTRUCTION marker. It describes the map this reset just cleared, and
        # a stale one would mark a later LIVE measurement as backfilled — the exact inversion, with
        # the sign flipped.
        n.extra_metrics_backfill = {}
        n.violations = []
        n.feasible = True
        # See the same line in `_requeue_partition_bound_results`: the provenance describes the metric this
        # reset just cleared, and a stale `{salvaged: True}` on a failed node is a claim about a
        # number that is gone.
        n.metric_provenance = None
        n.trials = []
        n.confirmed_mean = None
        n.confirmed_std = None
        n.confirmed_seeds = None
        n.confirmed_ruler = None
        n.agent_report = None
        # The PER-SEED confirm memo must reset with the node too: the confirm phase memo-skips
        # every seed already in `confirm_seed_results`, so a stale entry would re-emit
        # node_confirmed from PRE-reset seed metrics for the post-reset code without running a
        # single seed. Pending force-confirm requests are lifecycle-scoped and are cancelled below;
        # completed fulfillment history stays for audit while its generation-aware twin prevents ABA.
        st.confirm_seed_results.pop(n.id, None)
        st.eval_noise_seed_results.pop(n.id, None)   # same rule, same reason as the line above
        _purge_node_requests(st, {n.id})
        # Abort/proxy decisions belong to the lifecycle that was active when they were recorded.
        # Keeping them would immediately abort/skip every reset generation forever.
        st.aborted_nodes = [nid for nid in st.aborted_nodes if nid != n.id]
        st.proxy_scores.pop(n.id, None)
        st.proxy_skipped = [nid for nid in st.proxy_skipped if nid != n.id]
        if st.champion == n.id:
            st.champion = None
        ranked = st.hypothesis_ranking or {}
        if (ranked.get("node_id") == n.id
                and _event_generation(ranked) == old_generation):
            st.hypothesis_ranking = None
        n.failed_stage = None
        # Finish-time scores computed on the NOW-discarded code must not survive the reset, or a
        # holdout-gated best pick / generalization-gap audit keeps using a stale number the node
        # can no longer reproduce (holdout is append-only + skips already-scored ids, so it would
        # never be recomputed for this node). R1-c's verifier_score is exactly such a finish-time
        # score (a soundness judgment on the OLD attempt's result) — it must reset too, else the
        # tie-break would rank the new attempt by a score for a realization it no longer produces.
        n.holdout_metric = None
        n.verifier_score = None
        if n.id in st.holdout_evaluated_ids:
            st.holdout_evaluated_ids.remove(n.id)
        if stage in ("implement", "propose"):
            n.code = ""
            n.files = {}
            n.deleted = []
            n.stages = []                # a re-develop discards the old pipeline outcomes too
            n.rerun_from = stage
            n.rerun_stage = None
            # M1 (§21.18): drop the node's cached concept tags when they go STALE, so the next
            # concept-coverage cadence re-tags it fresh. Scope is tied to the TAGGER'S INPUTS: the snapshot
            # tagger reads only the IDEA (theme/rationale/params — `tools=None`, never the code), so tags
            # staleify only when the idea changes — i.e. `propose` (re-proposes a new idea), NOT `implement`
            # (re-develops CODE with the idea unchanged) nor `eval` (re-scores, idea+code unchanged). If the
            # tagger is later made agentic (reads code, `tools!=None`, §21.18 HT/B1), widen this to
            # `implement` too. No-op on old logs / untagged nodes.
            if stage == "propose":
                st.node_concepts.pop(n.id, None)
                st.node_concept_provenance.pop(n.id, None)
                st.node_concepts_authored.pop(n.id, None)   # the claim belonged to the abandoned Idea
                st.node_concepts_at_vocab.pop(n.id, None)   # keep the B1 staleness map in sync
                st.node_concepts_at_pending.pop(n.id, None)  # …and the F1i evidence gate beside it
                # the raw delta belongs to the Idea being abandoned. Clear it at the reset
                # boundary itself; otherwise a replay between reset and rebuild rematerializes stale
                # taxonomy for the pending node from a proposal that no longer exists.
                st.node_concept_deltas.pop(n.id, None)
                ctx.concept_mode_untrusted.discard(n.id)
                ctx.concept_input_capped.discard(n.id)
                ctx.concept_input_invalid.discard(n.id)
                # generation stamps did not exist on early classifier events. Remember
                # the idea boundary inside this fold so those ambiguous receipts still fail closed,
                # while unstamped receipts after eval/implement-only attempt bumps remain readable.
                ctx.concept_subject_invalidated.add(n.id)
        else:
            # eval-type reset: pending-with-code, the eval loop re-scores it. `from_stage` names
            # the pipeline stage to RESTART from (Phase 2) — the eval re-runs from there, reusing
            # earlier stages' artifacts. Plain "eval" on a single-command node is a full re-score.
            n.rerun_from = None
            n.rerun_stage = stage
            # Preserve only stages strictly BEFORE the requested restart boundary. A new lifecycle
            # that fails early must not retain a later-stage success from the abandoned generation.
            for i, prior in enumerate(n.stages):
                if prior.get("name") == stage:
                    n.stages = n.stages[:i]
                    break
            # RE-STAMP what survives to the fresh epoch. A retained row IS this new lifecycle's
            # starting truth — its artifacts are exactly what the restart reuses — so it is not
            # superseded by anything, and leaving it carrying the OLD lifecycle's repair epoch
            # would print a number beside it from a generation that no longer exists. Written as a
            # replacement dict rather than a mutation because `n.stages` may still be the list a
            # previous fold pass built (the fold re-enters on every read).
            n.stages = [({**prior, "repairs": 0} if isinstance(prior, dict) else prior)
                        for prior in n.stages]
            if holdout_was_disclosed:
                # Stage reuse can retain a model trained on the old search complement. A disclosed
                # partition forces a full freshly-materialized eval in the next epoch; source code
                # survives, but no old stage artifact or workdir checkpoint may be reused.
                n.rerun_stage = None
                n.stages = []
        _clear_build_marker(st, d, n.id)
        # Reset itself clears `finished`, so a later resume cannot observe the old finished edge.
        # Invalidate the completed confirmation/approval epoch here, before clearing it.
        # Requeuing every OTHER incumbent (wiping its metric to force a re-eval on the newly-hidden
        # complement) is a NEW epoch-aware semantic. A legacy unstamped node_reset predates search
        # epochs; firing it there wipes surviving incumbents' metrics that the pre-batch fold left
        # intact — an invariant-5b divergence when replaying an old log. Gate the requeue-all on a
        # modern generation stamp. (A modern generation-0 reset that omits the stamp — allowed only at
        # attempt 0 — likewise skips it: a rare, benign fairness gap, never corruption.) The plain
        # finished-reopen epoch bump below is deliberately NOT gated: a reset is itself the reopen edge
        # and bumps the epoch regardless of stamp (it wipes no incumbent metric — requeue=False).
        reset_is_epoch_aware = _event_generation(d) is not _MISSING
        if holdout_was_disclosed and reset_is_epoch_aware:
            # The target is already a fresh pending generation. Every OTHER active incumbent must
            # also be re-evaluated on the newly-hidden complement; retaining its raw/confirm metric
            # would rank values measured on different partitions in one candidate pool.
            _rotate_search_epoch(
                st, requeue_partition_scores=True, fresh_node_ids={n.id})
        elif was_finished:
            # A reset is itself the actual reopen edge. With no disclosed partition there are no raw
            # scores to invalidate, but confirmation/approval still belong to the prior search epoch.
            _rotate_search_epoch(st, requeue_partition_scores=False)
        # `best_confirmed.generations` covers the whole candidate set. Resetting ANY competitor
        # invalidates the snapshot, even when the previously chosen winner itself was untouched.
        _invalidate_completion_certificates(st, ctx)
        # A reset means there is work to do again, so it RE-OPENS a finished run — else the
        # loop would see the stale run_finished and exit before re-running/re-scoring the node.
        # (Mirrors EV_RESUME's finished-clear; a later run_finished sets it again. `paused` is
        # left alone — that's the operator's separate resume.)
        st.finished = False
        st.stop_reason = None
        st.stop_detail = None
        st.stop_requested = None

def _stage_epoch(row) -> int:
    """The repair epoch already recorded on a folded stage row, defaulting to 0.

    A row this fold built always carries one; the default covers a row a caller handed in (tests
    and the CLI both construct `Node(stages=[…])`) and keeps this arithmetic total, since a `None`
    would make the surrounding `max` a TypeError inside the fold loop, which has no per-event
    try/except."""
    if not isinstance(row, dict):
        return 0
    value = row.get("repairs")
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value

def _on_stage_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Multi-stage eval pipeline (Phase 1): one stage of a node's declared pipeline finished.
    # Last-wins by stage name so a stage-scoped RE-RUN (Phase 2) replaces the prior outcome
    # rather than appending a duplicate.
    n = _node_for_event(st, d)
    if n is not None and n.id not in st.aborted_nodes and _generation_matches(n, d):
        # `repairs` is the REPAIR EPOCH this row was recorded in — see `_on_node_repaired` above
        # and `core/models.py::Node.stages`. It is the fold's own counter and NOT read off the
        # event, which carries no such key: the engine appends one `stage_finished` per stage per
        # ATTEMPT of the inline-repair loop and this fold keeps them last-wins BY NAME, so without
        # it the surviving rows are indistinguishable from the current attempt's and every surface
        # renders a superseded failure as the node's live state (measured: v9 node 5, 177 minutes).
        rec = {"name": d.get("name"), "status": d.get("status"),
               "exit_code": d.get("exit_code"), "seconds": d.get("seconds"),
               "repairs": n.repairs}
        # THE PER-ATTEMPT LEDGER, appended BEFORE the per-name merge below and never rewritten by it
        # (doc 52 row 27; BACKLOG §6 D5): each row is the attempt's own statement — its epoch is
        # `n.repairs` as recorded here, never the merge's MAX — so the attempt a repair supersedes
        # keeps the wall-clock it spent. Append-only across resets (`node_reset` clears `stages`,
        # not this), stamped with the lifecycle generation (`Node.attempt`, the field that keeps its
        # original name for projection compatibility) so a reader can partition.
        n.stage_attempts.append({**rec, "generation": n.attempt, "seq": e.seq})
        for i, s in enumerate(n.stages):
            if s.get("name") == rec["name"]:
                # A "reused" marker means a re-eval SKIPPED this stage (an earlier attempt already
                # ran it) — it must NOT clobber that attempt's REAL completion record (its true
                # exit_code/seconds), else the node reads as if it trained in 0s. Keep the
                # informative record. Order-tolerant: a real record still replaces a prior reused.
                #
                # THE EPOCH STILL ADVANCES, and that is what makes `repairs` mean the right thing.
                # A reuse is the LATER attempt's own statement that this stage's result stands, so
                # the record is that attempt's truth even though the bytes came from an earlier
                # one. Without this, "recorded before the current repair" would convict every
                # deliberately-reused success: measured over `runs/`, three of the seven nodes
                # whose stage rows end at an older epoch are exactly that shape — `rubertlite-dr-
                # unified-v8` node 3 (`mine ok` reused twice, then an evaluated node) and node 10,
                # and `rubertlite-dense-retrieval` node 1 — and none of them is stale.
                if rec["status"] == "reused" and s.get("status") not in (None, "reused"):
                    s["repairs"] = max(_stage_epoch(s), rec["repairs"])
                    break
                # MAX on replacement for the same reason, in the direction order-tolerance needs:
                # a real record arriving AFTER the reused marker that already vouched for it at a
                # newer epoch must not roll the epoch back to the attempt that produced the bytes.
                rec["repairs"] = max(rec["repairs"], _stage_epoch(s))
                n.stages[i] = rec
                break
        else:
            n.stages.append(rec)

def _on_confirm_eval(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)
    seed = _coerce_node_id({"node_id": d.get("seed")}) if "seed" in d else None
    keyed = nid is not None and seed is not None
    n = st.nodes.get(nid) if nid is not None else None
    legacy_attempt = "generation" not in d and "attempt" in d
    generation = _event_generation(d, legacy_attempt=True)
    # Fresh master briefly emitted `attempt`; preserve its historical behavior (a stale attempt is
    # fully dropped). Canonical `generation` events use the stricter lifecycle rule below: stale state
    # is inert, but already-spent compute still counts against the budget.
    if legacy_attempt and n is not None and (generation is None or generation != n.attempt):
        return
    # Old logs did not stamp confirm events: bind those to the extant lifecycle visible at that point.
    # Cost is trusted only for an evaluated lifecycle, an intervention-invalidated lifecycle, or an
    # older generation whose worker actually ran before reset. A forged current-generation event on a
    # still-pending node cannot reserve a seed's dedupe key and suppress the later real compute cost.
    resolved_generation = (n.attempt if n is not None else 0) if generation is _MISSING else generation
    chargeable = (n is not None and isinstance(resolved_generation, int)
                  and resolved_generation <= n.attempt
                  and (resolved_generation < n.attempt
                       or n.status is NodeStatus.evaluated
                       or n.id in st.aborted_nodes or n.tombstoned))
    if keyed and chargeable and isinstance(resolved_generation, int):
        cost_key = (nid, resolved_generation, seed)
        if cost_key not in ctx.charged_confirm_seeds:
            ctx.charged_confirm_seeds.add(cost_key)
            _charge_eval_seconds(st, "confirm", d.get("eval_seconds"))
    if (n is None or n.status is not NodeStatus.evaluated
            or n.id in st.aborted_nodes or n.tombstoned):
        return
    if generation is not _MISSING and (
            n is None or generation is None or generation != n.attempt):
        return                    # stale metric/memo ignored; its real cost was charged above
    # Only a KEYED event (node_id+seed) can participate in the per-seed memo that makes the eval-cost
    # add idempotent; an un-keyed confirm_eval has no memo slot, so a duplicate/re-fold would
    # double-count total_eval_seconds (order/duplication-sensitive — the fold must not be). The sole
    # emitter always writes both keys, so this only guards a future/foreign/hand-edited un-keyed event.
    # Retryable infrastructure refusals still charge any admitted setup/probe time above, but they are
    # not completed seed evidence. Excluding them from the resume memo lets an unchanged seed retry after
    # GPU discovery, a Card re-pin, or the container runtime is repaired.
    # `isinstance` FIRST: this is a set membership, so it hashes the raw value, and an unhashable
    # reason (a list/dict on one confirm_eval row) raised TypeError out of the fold and bricked every
    # replay/resume of the run — the fold loop has no per-event try/except. The earlier `!= "aborted"`
    # comparisons are safe; this one needs the same shape guard as the rest of this handler's reads.
    _reason = d.get("reason")
    retryable_infrastructure = (isinstance(_reason, str)
                                and _reason in {"gpu_unavailable", "gpu_unpinnable"})
    if keyed and not retryable_infrastructure:               # per-seed resume memo (#0)
        st.confirm_seed_results.setdefault(nid, {})[seed] = _finite_metric(d.get("metric"))

def _on_node_confirmed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    n = _node_for_event(st, d)
    if (n is not None and n.status is NodeStatus.evaluated
            and n.id not in st.aborted_nodes and not n.tombstoned
            and _generation_matches(n, d, legacy_attempt=True)):
        # A confirmation certificate is one atomic evidence revision.  Validate every selection-bearing
        # field before touching the node: a torn/foreign row must neither create a partial certificate nor
        # erase the last valid certificate (or its verifier treatment).
        mean = _finite_metric(d.get("mean"))
        std = _finite_metric(d.get("std"))
        seeds = d.get("seeds")
        if (mean is None or std is None or std < 0.0
                or isinstance(seeds, bool) or not isinstance(seeds, int) or seeds <= 0):
            return
        # Confirmation changes the evidence revision judged by the verifier. Invalidate any earlier score;
        # a newly-emerged confirmed tie is re-scored as one complete group by the cadence producer.
        prior_evidence = verifier_evidence_digest(st.direction, n)
        n.confirmed_mean = mean
        n.confirmed_std = std
        n.confirmed_seeds = seeds
        # The ruler of THIS certificate (a later one without it clears it): bounded, and compared
        # only for equality by its one reader, so a junk value can only withhold a pair.
        ruler = d.get("protocol_profile")
        n.confirmed_ruler = ruler if isinstance(ruler, str) and 0 < len(ruler) <= 64 else None
        if verifier_evidence_digest(st.direction, n) != prior_evidence:
            n.verifier_score = None

def _on_eval_noise_seed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """One repeat of the eval-NOISE probe (doc 52 row 11): its cost, and its per-seed resume memo.

    Deliberately SIMPLER than `_on_confirm_eval` above, and the asymmetry is the instrument's whole
    point. A confirm seed's metric becomes a node's `confirmed_mean` and can therefore change which
    node wins, so that handler must reject every stale, forged or torn shape before it touches a
    Node. A noise repeat touches NO node field: it lands in a run-level memo nothing selects on. So
    the only two properties it owes are the two the fold owes every event — the eval seconds are
    charged EXACTLY ONCE per (node, generation, seed), so a duplicate or re-folded row cannot
    inflate the budget or make the fold order-dependent, and a malformed row is inert.
    """
    nid = _coerce_node_id(d)
    seed = _coerce_node_id({"node_id": d.get("seed")}) if "seed" in d else None
    if nid is None or seed is None:
        # UNKEYED: there is no memo slot, so there is no idempotent cost add either. The sole
        # emitter always writes both keys; this only guards a foreign / hand-edited log.
        return
    n = st.nodes.get(nid)
    generation = d.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int):
        generation = n.attempt if n is not None else 0
    cost_key = (nid, generation, seed)
    if cost_key not in ctx.charged_noise_seeds:
        ctx.charged_noise_seeds.add(cost_key)
        # Charged for a SUPERSEDED lifecycle too (its own bucket, `noise`): a reset discards the
        # measurement, never the compute a worker already spent on it — the same rule the confirm
        # and terminal cost keys above are written to.
        _charge_eval_seconds(st, "noise", d.get("eval_seconds"))
    if n is None or generation != n.attempt:
        return                      # spent money, not evidence: a stale repeat memoizes no seed
    st.eval_noise_seed_results.setdefault(nid, {})[seed] = _finite_metric(d.get("metric"))


def _noise_seed_number(v):
    """One persisted SEED as an int (or None) — `_coerce_node_id`'s rules on a bare value."""
    return _coerce_node_id({"node_id": v})


def _noise_number_list(raw, coerce) -> list:
    """A persisted list of numbers from an untrusted payload, element-wise, bounded.

    `eval_noise_floor` is stored on `RunState` for readers, so a hand-edited or foreign log must not
    be able to park an arbitrary nested object there. `coerce` is `_finite_metric` (a metric, which
    may legitimately be None) or `_coerce_node_id` (a seed). The cap is the probe's own bound with
    room to spare — a run cannot ask for more repeats than it can pay for."""
    if not isinstance(raw, list):
        return []
    return [coerce(v) for v in raw[:64]]


def _on_eval_noise_floor(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """The probe's SUMMARY and its completion gate (doc 52 row 11).

    Normalized field by field rather than stored whole, for the reason the list helper above states:
    nothing decides on this record, but it IS carried on `RunState` for every projection over it, so
    its shape is the fold's and not a writer's. Last row wins — a run measures its floor once, and a
    second row could only come from a hand-edited log or a future writer that re-measures."""
    st.eval_noise_floor = {
        "node_id": _coerce_node_id(d),
        "generation": _coerce_node_id({"node_id": d.get("generation")}),
        "seeds": _noise_number_list(d.get("seeds"), _noise_seed_number),
        "metrics": _noise_number_list(d.get("metrics"), _finite_metric),
        "n": max(0, _noise_seed_number(d.get("n")) or 0),
        "mean": _finite_metric(d.get("mean")),
        "std": _finite_metric(d.get("std")),
        "sem": _finite_metric(d.get("sem")),
        "spread": _finite_metric(d.get("spread")),
        "search_metric": _finite_metric(d.get("search_metric")),
        "profile": str(d.get("profile"))[:64] if isinstance(d.get("profile"), str) else None,
        # The RULER the counted repeats measured (`engine/comparability.py::agreed_ruler`), bounded
        # like `profile`, and the mixed flag only as True. Its one reader
        # (`card_ledger.py::_floor_std`) compares it for EQUALITY with a node's recorded facet, so a
        # junk value can only withhold the floor, never lend it to a gain.
        **({"protocol_profile": d["protocol_profile"]}
           if isinstance(d.get("protocol_profile"), str) and 0 < len(d["protocol_profile"]) <= 64
           else {}),
        **({"protocol_mixed": True} if d.get("protocol_mixed") is True else {}),
        # The creation-boundary pass (doc 67 67.1a), only as True: the end ladder re-measures after a
        # mid-search pass that counted fewer than two repeats (`noise_floor.py::_noise_floor_due`).
        **({"mid_search": True} if d.get("mid_search") is True else {}),
        **({"reason": str(d.get("reason"))[:200]} if isinstance(d.get("reason"), str) else {}),
    }


def _on_holdout_evaluated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # D1 holdout-gated promotion: the engine re-scored this val-leader's predictions on
    # the FINAL holdout partition the search never saw. Tolerant like node_evaluated:
    # an event for an unknown node (corrupt log) is skipped, and a null metric (missing
    # predictions) records nothing — such a node simply can't win the holdout pick.
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    # THE HOLDOUT'S OWN COMPUTE (review 2026-09-22, ENG2-15): the withheld scorer or the private
    # grade RAN, whatever this row's measurement is worth below — so its seconds are charged BEFORE
    # the validity checks, exactly once per (node, generation, epoch), into their own bucket. The
    # rule the confirm and noise-floor buckets are written to: a rejected row discards the number,
    # never the compute. Only a row that carries the key is keyed at all, so every log written
    # before it (no `eval_seconds`) folds byte-identically (invariant #5).
    if "eval_seconds" in d and nid is not None:
        cost_key = (nid, _coerce_node_id({"node_id": d.get("generation")}),
                    _coerce_node_id({"node_id": d.get("search_epoch")}))
        if cost_key not in ctx.charged_holdout_keys:
            ctx.charged_holdout_keys.add(cost_key)
            _charge_eval_seconds(st, "holdout", d.get("eval_seconds"))
    if (n is None or n.status is not NodeStatus.evaluated
            or n.id in st.aborted_nodes or n.tombstoned):
        return
    generation = _event_generation(d, legacy_attempt=True)
    if generation is not _MISSING and (
            n is None or generation is None or generation != n.attempt):
        return
    # A prior epoch's holdout was already disclosed; late scores from it cannot enter the newly
    # hidden partition's gate or metric pool. Missing epoch remains legacy-current.
    if d.get("search_epoch", st.search_epoch) != st.search_epoch:
        return
    if "search_epoch" in d:
        # A modern producer stamps `search_epoch` (holdout.py); a legacy holdout_evaluated does not.
        # Record that THIS disclosed holdout carries epoch semantics, so a later candidate change may
        # safely requeue incumbents onto the newly-hidden complement. A legacy (unstamped) disclosure
        # leaves this False, so the requeue-with-metric-wipe stays gated off (invariant-5b, F2).
        st.holdout_epoch_aware = True
    if nid is not None and nid not in st.holdout_evaluated_ids:
        st.holdout_evaluated_ids.append(nid)   # gate: attempted, even if metric is null
    metric = _finite_metric(d.get("metric"))
    if n is not None and metric is not None:
        prior_evidence = verifier_evidence_digest(st.direction, n)
        n.holdout_metric = metric
        if verifier_evidence_digest(st.direction, n) != prior_evidence:
            n.verifier_score = None

def _on_agent_validated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    n = _node_for_event(st, d)
    if (n is not None and n.id not in st.aborted_nodes
            and _generation_matches(n, d)):   # audit only; never affects selection
        n.agent_report = {
            "ok": d.get("ok"), "checks": d.get("checks", []),
            "fell_back": d.get("fell_back"), "attempts": d.get("attempts"),
            "shipped_ok": d.get("shipped_ok"),
        }

def _on_setup_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P0-3: setup completed (task+data preflight, incl. the leakage hard-stop). Folded so resume can
    # tell "setup done" from "crashed mid-setup right after run_started" — the latter must re-run the
    # rest of preflight (leakage!) rather than skip it forever. Idempotent (a re-run re-appends it).
    st.setup_done = True
    # P0-3 manifest: bind the completion to the material it verified (config/workspace/data digest).
    # Additive: absent on old logs -> "" -> resume falls back to the boolean (unchanged behavior).
    if d.get("manifest"):
        st.setup_manifest = str(d.get("manifest"))

def _on_run_setup_started(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # The command is ABOUT to run its arbitrary side effects. Until its finish lands, a resume cannot
    # tell "never started" from "died halfway through the install", so record the open attempt; the
    # finish below closes it. Old logs whose started row carried no `command` simply add nothing —
    # they fold exactly as before.
    # `run_setup_key` joins over the command, so a truthy SCALAR raised TypeError out of the fold.
    # An unusable shape is not a setup attempt we can key — treat it like the old logs that carried
    # no `command` at all and add nothing (fold must stay total).
    if isinstance(d.get("command"), (list, tuple)) and d.get("command"):
        st.run_setup_open.add(run_setup_key(d.get("command")))

def _on_run_setup_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # arch-review §5 P2: a SUCCESSFUL run-level `run_setup` (dep install) is folded (keyed by its
    # command) so a resume skips it instead of re-installing every time — crash-safe exactly-once. A
    # failed/timed-out setup is NOT recorded (the command must actually re-run). Old logs whose
    # run_setup_finished carried no `command` just don't populate the set (setup runs as before).
    if not isinstance(d.get("command"), (list, tuple)) or not d.get("command"):
        return                       # same shape guard as the started handler above
    key = run_setup_key(d.get("command"))
    # ANY finish closes the open attempt — a failed/timed-out command reported its outcome, so the
    # next process is not resuming through an unknown one. Only exit 0 marks it done.
    st.run_setup_open.discard(key)
    if d.get("exit_code") == 0 and not d.get("timed_out"):
        st.run_setup_done.add(key)

def _on_approval_requested(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Compare-and-set: the request carries the seq it believes it follows and is honoured only when it
    # lands exactly there, so a stale one cannot re-open the gate after an intervening abort/reset.
    # `isinstance(raw_after, bool)` is explicit because `isinstance(True, int)` is True — without it a
    # `after_seq=True` request placed at seq 2 would coerce to 1 and SATISFY the CAS. Pinned (both
    # directions, and that exact bool placement) by tests/test_events_replay.py::
    # test_a_stale_approval_request_is_rejected_and_a_current_one_is_not.
    if "after_seq" in d:
        raw_after = d.get("after_seq")
        if isinstance(raw_after, bool):
            return
        try:
            after_seq = int(raw_after)
        except (TypeError, ValueError, OverflowError):
            return
        if e.seq is None or e.seq != after_seq + 1:
            return
    if st.approved:
        return                         # a grant that won the race cannot be re-opened by a stale request
    subject = _coerce_node_id(d)
    node = st.nodes.get(subject) if subject is not None else None
    if node is not None and (node.id in st.aborted_nodes or node.tombstoned):
        return
    generation = _event_generation(d)
    if (subject is not None and generation is not _MISSING
            and (node is None or not _generation_matches(node, d))):
        return
    same_pending = (st.awaiting_approval and st.approval_subject == subject
                    and st.approval_generation == (node.attempt if node is not None else None))
    st.awaiting_approval = True
    # P0-2: record WHICH node the request is for (the engine emits the current best) as audit context,
    # surfaced in the projection so the UI can show what is awaiting approval. This is NOT the grant
    # gate — `_on_approval_granted` binds to node existence, not to this subject (see there).
    st.approval_subject = subject
    st.approval_generation = node.attempt if node is not None else None
    if not same_pending:
        st.approval_request_seq = e.seq

def _on_approval_granted(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P0-2 approval gate: honor a grant that names a REAL node in the run — the current best OR an
    # operator-chosen node (`approve --node-id N` / the boss `approve` action both ratify a specific
    # node). A grant for a node that doesn't exist — a forged/typo'd `approval_granted(node_id=999)`, or
    # an unhashable/bool/non-numeric id — is ignored, so it can't globally flip `approved`; the run stays
    # awaiting the real approval. Binding to node EXISTENCE (deliberately NOT to the pending
    # `approval_subject`) closes the forged-id hole while still allowing a legitimate non-best `--node-id`
    # grant. The id is coerced/guarded by `_coerce_node_id` BEFORE the membership test so a forged
    # unhashable id can't raise inside the `in` and brick the fold. Back-compat: a bare grant with no
    # node_id (old logs / a direct grant) is accepted, so legacy HITL runs fold identically.
    if d.get("node_id") is not None:               # a TARGETED grant must name a real, coercible node
        subj = _coerce_node_id(d)
        if subj is None or subj not in st.nodes:
            return                                 # forged / unhashable / non-existent -> ignore
        node = st.nodes[subj]
        if node.id in st.aborted_nodes or node.tombstoned:
            return
        generation = _event_generation(d)
        if generation is not _MISSING and not _generation_matches(node, d):
            return
        st.approved_node_id = subj
    else:
        # Bare grants are legacy. Modern first-party producers always name + generation-stamp a node;
        # accepting this shape is solely persisted-log compatibility.
        st.approved_node_id = st.approval_subject
    st.awaiting_approval = False
    st.approved = True
    st.approval_subject = None
    st.approval_generation = None

def _on_spec_proposed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # The request is the human-review boundary. Once it exists (and especially after ratification),
    # a late agent event must not swap in content the operator never reviewed under the same card.
    if st.spec_approval_requested or st.spec_confirmed:
        return
    st.proposed_spec = d

def _on_spec_approval_requested(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # A request without a proposal can never be ratified. Treat it as malformed instead of exposing
    # an actionable phase that every first-party approval producer must reject.
    if st.proposed_spec is None or st.spec_confirmed:
        return
    if not st.spec_approval_requested:
        st.spec_approval_request_seq = e.seq
    st.spec_approval_requested = True

def _on_spec_approved(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P0-2: ratify only a spec that was actually PROPOSED. A premature/forged `spec_approved` (no
    # preceding `spec_proposed`) would set `spec_confirmed=True` while `proposed_spec` is None,
    # skipping onboarding entirely. The real flow always folds `spec_proposed` first (the engine
    # gates the emit on it), so this only rejects an out-of-order ratification; old logs are
    # unaffected (they always carry the proposal).
    if st.proposed_spec is not None:
        st.spec_confirmed = True

def _on_spec_drift(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    generation = _event_generation(d)
    if generation is not _MISSING:
        n = _node_for_event(st, d)
        if n is None or n.id in st.aborted_nodes or not _generation_matches(n, d):
            return
    st.drifts.append(d)                         # audit only; metric already discarded

def _on_ablate(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    pid = _coerce_node_id(d, "parent_id")
    n = st.nodes.get(pid) if pid is not None else None
    generation = _event_generation(d)
    resolved_generation = (n.attempt if n is not None else 0) if generation is _MISSING else generation
    valid = (generation is _MISSING
             or (n is not None and isinstance(resolved_generation, int)
                 and resolved_generation <= n.attempt))
    if pid is None or not valid or not isinstance(resolved_generation, int):
        return
    record = dict(d)
    record["parent_id"] = pid
    record.setdefault("generation", resolved_generation)
    st.ablations.append(record)   # historical audit; consumers/gates key it by lifecycle generation
    # Account the ablation probes' eval wall-clock against the cumulative budget (arch-review §4 P1-2:
    # ablation was wholly outside accounting, so a run could spend well past max_eval_seconds on
    # probes). Additive + reader-defaulted: old ablate events carry no eval_seconds -> +0.0.
    ablation_id = d.get("ablation_id")
    # New emitters identify one physical probe operation, so a duplicated append is idempotent while
    # two legitimate cadence runs on the same parent/generation both count. Legacy events had no id and
    # are therefore charged individually; collapsing them by parent would undercount real repeated work.
    if not isinstance(ablation_id, str) or not ablation_id:
        _charge_eval_seconds(st, "node", d.get("eval_seconds"))
    elif ablation_id not in ctx.charged_ablation_ids:
        ctx.charged_ablation_ids.add(ablation_id)
        _charge_eval_seconds(st, "node", d.get("eval_seconds"))

def _on_foresight_selected(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # FOREAGENT receipt: does not re-rank evaluated nodes, but primes later world-model picks with
    # its OWN calibration (did the picked node beat its parent?), closing the predict→outcome loop.
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    generation = _event_generation(d)
    if generation is not _MISSING and (
            n is None or n.id in st.aborted_nodes or not _generation_matches(n, d)):
        return
    if nid is not None:
        record = {"node_id": nid, "confidence": d.get("confidence")}
        if generation is not _MISSING:
            record["generation"] = generation
        st.foresight_selected.append(record)

_GPU_INVENTORY_ROW_KEYS_MAX = 32          # the real schema has 7; this only stops a fat foreign row
_GPU_INVENTORY_SCALAR_CHARS_MAX = 256     # uuid / pci_bus_id / name / driver strings are far shorter


def _bounded_gpu_inventory_row(row: dict) -> dict:
    """Copy one GPU-inventory row with both its key count and its scalar sizes bounded."""
    out: dict = {}
    for key in sorted(row):
        if len(out) >= _GPU_INVENTORY_ROW_KEYS_MAX:
            break
        if not isinstance(key, str) or len(key) > _GPU_INVENTORY_SCALAR_CHARS_MAX:
            continue
        value = row[key]
        if isinstance(value, str) and len(value) > _GPU_INVENTORY_SCALAR_CHARS_MAX:
            continue                      # dropped, not truncated — a shortened uuid is another GPU
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            continue                      # nested containers are unbounded by construction
        out[key] = value
    return out




def _on_proxy_scored(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # A6 proxy/predictive scoring (audit-only): early-signal rank + which nodes were skipped.
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if n is not None and n.id in st.aborted_nodes:
        return
    generation = _event_generation(d)
    if generation is not _MISSING and (n is None or not _generation_matches(n, d)):
        return
    if nid is not None and d.get("score") is not None:
        st.proxy_scores[nid] = d["score"]
    if d.get("skipped") and nid is not None and nid not in st.proxy_skipped:
        st.proxy_skipped.append(nid)

def _on_node_value_estimated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # docs/BACKLOG.md §0.1 row 17: freeze the LLM VALUE ESTIMATE for one node's branch (the model's
    # answer cannot be recomputed in the deterministic fold, exactly as `node_verified` above cannot).
    # Generation-scoped for the same reason and on the same terms: an estimate formed against a
    # reset-abandoned attempt describes code this node no longer carries, and MCTS would keep
    # steering by it. Advisory and search-side only — `MCTSPolicy` reads it as a decaying adjustment
    # to the UCB1 value term (`search/policy.py::value_estimate`); nothing in champion selection
    # reads it at all.
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if n is None or n.id in st.aborted_nodes or n.tombstoned:
        return
    # A brand-new event with one writer, which always stamps `generation` — so REQUIRE the stamp
    # rather than accepting a missing one as current. No legacy log carries this type, so the
    # additive-legacy tolerance the older per-node events must keep would buy nothing here and would
    # let a hand-edited unscoped row steer the search.
    if _event_generation(d) is _MISSING or not _generation_matches(n, d):
        return
    value = d.get("value")
    if is_usable_metric(value) and 0.0 <= float(value) <= 1.0:
        n.value_prior = float(value)


def _on_run_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    accepted_after_seq: int | None = None
    if "after_seq" in d:
        raw = d.get("after_seq")
        if isinstance(raw, bool):
            return
        try:
            after_seq = int(raw)
        except (TypeError, ValueError, OverflowError):
            return
        if e.seq is None or e.seq != after_seq + 1:
            return                    # an external event won the decision→finish race
        accepted_after_seq = after_seq
    pending = ctx.pending_finish_report
    if pending is not None:
        report_seq, report_index, report = pending
        # Modern events bind the report seq into run_finished.after_seq. Historical emitters had no
        # CAS payload, so accept only a physically adjacent report->finish pair. An intervening event,
        # including an unknown forward-compatible one, leaves the provisional narrative unpublished.
        modern_adjacent = accepted_after_seq is not None and report_seq == accepted_after_seq
        legacy_adjacent = (accepted_after_seq is None
                           and ctx.event_index == report_index + 1)
        if modern_adjacent or legacy_adjacent:
            st.report = report
        ctx.pending_finish_report = None
    st.finished = True
    st.finalization_marker_seq = None
    if e.seq is not None:
        st.last_finish_seq = e.seq
        # Recovery is explicitly opted into by modern finish events. Markerless historical finishes
        # were already complete before this protocol existed and must never become synthetic work.
        if not bool(d.get("finalization_required", False)):
            st.finalized_finish_seq = e.seq
    st.stop_reason = d.get("reason")
    # …and the finishing writer's own sentence beside the class. Folded for the same reason the
    # `pause` reason is: it was already durable on the row and no reader could reach it. `error` is
    # the class the engine decided; this is the account it wrote at the same moment.
    _detail = d.get("error")
    st.stop_detail = str(_detail) if isinstance(_detail, str) and _detail.strip() else None
    # Drop dangling markers on normal completion. GUARDED-ABORT finishes deliberately retain crash
    # prefixes: older/external writers may need resume recovery to append the missing node_failed
    # receipt. Other terminal reasons must not leave a false in-flight pulse on a run that is over.
    # The CLASS predicate (`finalize_scope.is_guarded_abort`), not the literal: the ceiling's
    # `budget_exhausted` is written by the SAME outer guard, from the same mid-build exception, so
    # the recovery this clause preserves the prefix for applies to it identically — docs/57's ninth
    # site, found by the tree-wide literal scan and not by the review that counted six.
    if not is_guarded_abort(d.get("reason")):
        st.building = None
        st.buildings.clear()


def _on_finalization_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    raw = d.get("finish_seq")
    if isinstance(raw, bool):
        return
    try:
        finish_seq = int(raw)
    except (TypeError, ValueError, OverflowError):
        return
    if (st.finished and finish_seq == st.last_finish_seq
            and st.finalized_finish_seq != finish_seq):
        st.finalized_finish_seq = finish_seq
        st.finalization_marker_seq = e.seq

def _on_resume_or_run_reopened(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # RESUME (the one operator "continue"): lift EVERY stopped state so re-entering the loop
    # keeps going — whether the run was PAUSED (stop, no finalize), ABORTED (finalize →
    # stop_requested → run_finished), or naturally FINISHED (budget exhausted, then reopened
    # with more budget). Clears paused + finished + stop_requested + stop_reason. Deterministic
    # under replay — a later run_finished simply sets `finished` again. EV_RUN_REOPENED is the
    # legacy alias of RESUME (kept so old logs + the UI's reopen path fold identically); the two
    # 3-verb operator controls are `stop` (EV_PAUSE) and `finalize` (EV_RUN_ABORT).
    #
    # P0-2 search epoch: reopening a run that had already FINISHED (its confirmation/approval
    # promotion completed for the prior candidate set) begins a NEW search epoch. Any nodes added
    # after the reopen are a fresh candidate set, so the prior COMPLETION gates must not carry over:
    # clear `confirmed_done` (so the confirm phase re-runs and can confirm a better new candidate —
    # already-confirmed nodes are cheaply reused via their memoized `confirmed_mean`) and re-open
    # approval (so the possibly-new best is re-ratified rather than inheriting the old grant). A
    # resume from a mere PAUSE (finished never set) is the SAME epoch and leaves these gates intact.
    # Checked BEFORE clearing `finished` below. Back-compat: old logs without a reopen-after-finish
    # keep search_epoch=0 and fold identically.
    if st.finished or st.holdout_evaluated_ids:
        if st.holdout_evaluated_ids:
            # F2: requeue-with-metric-wipe only for an epoch-aware (modern) disclosure; a legacy
            # holdout log rotates without wiping surviving incumbents (invariant 5b).
            _rotate_search_epoch(st, requeue_partition_scores=st.holdout_epoch_aware)
        else:
            _rotate_search_epoch(st, requeue_partition_scores=False)
        # A reopen begins a new candidate epoch, so the prior epoch's confirmation certificate must
        # not keep authorizing selection. Clearing only the folded flag and not the threaded
        # `ctx.best_confirmed` here is what let an epoch-(N-1) certificate keep overriding epoch-N's
        # metric winner — the bug this shared helper now makes unreachable from any one site.
        _invalidate_completion_certificates(st, ctx)
        # P0-2 freshly-hidden per-epoch holdout: the prior epoch's holdout was DISCLOSED at the
        # finish (its scores drove the champion pick), so the reopened epoch must NOT re-score its
        # new candidates on that same partition — the engine rebuilds `_holdout_idx` for the new
        # epoch (a different, never-disclosed split). Clear the gate + the now-stale holdout metrics
        # so the holdout phase re-runs and re-scores every current leader on the fresh split (keeping
        # the champion comparable on ONE holdout). New holdout_evaluated events carry the new epoch;
        # a late one stamped with the prior epoch is dropped by the epoch guard in _on_holdout_evaluated.
    st.paused = False
    st.pause_node_id = None
    st.pause_generation = None
    st.pause_reason = None
    st.finished = False
    st.stop_reason = None
    st.stop_detail = None
    st.stop_requested = None

# --- live operator control events (UI intervention). Intent only; the engine reads
# these and writes the matching domain effect. Deterministic under replay. ---
def _on_resume_requested(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P1-1 durable resume intent: record the request seq + time. A request seq newer than the last
    # `resume_served` (below) is an unfulfilled resume the reconciler re-spawns. Monotonic by seq, so a
    # duplicate/out-of-order fold is idempotent; the ts is the request event's own recorded time.
    if e.seq > st.last_resume_request_seq:
        st.last_resume_request_seq = e.seq
        st.last_resume_request_ts = float(getattr(e, "ts", 0.0) or 0.0)
        mode = d.get("mode")
        if mode in ("resume", "finalize"):
            st.last_resume_request_mode = mode
        elif not d.get("launch_claim"):
            # A real legacy request means ordinary resume. A claim-only record is transport metadata
            # and must preserve the pending intent's mode (especially finalize).
            st.last_resume_request_mode = "resume"
    if d.get("launch_claim") and e.seq > st.last_resume_launch_seq:
        st.last_resume_launch_seq = e.seq
        st.last_resume_launch_ts = float(getattr(e, "ts", 0.0) or 0.0)

def _on_resume_served(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P1-1: the engine acquired the singleton lock and is driving the loop -> every resume requested
    # before this seq is fulfilled. Seq-gated so one serve satisfies several piled-up requests.
    if e.seq > st.last_resume_served_seq:
        st.last_resume_served_seq = e.seq
        # A live loop may also acknowledge a redundant resume request (orchestrator.py) without
        # changing process ownership. Only the CLI's strict marker proves that engine.lock moved to
        # a replacement owner; then every older eval admission belongs to the former process.
        if d.get("engine_owner_boundary") is True:
            _clear_eval_activity_for_new_owner(st)
        if st.finished and st.last_resume_request_mode == "finalize":
            # A finalize hand-off that arrived after run_finished repairs/acknowledges the existing
            # wrap-up; it must not create a second finish. Consume its lingering stop intent once the
            # finalize-mode CLI actually owns the singleton lock.
            st.stop_requested = None

def _on_run_abort(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # FINALIZE: the loop turns stop_requested into a run_finished (which runs the end-of-run
    # finalization — report/lessons/case/cost). A bare `stop` uses EV_PAUSE instead (no finalize).
    st.stop_requested = d.get("reason", "operator")
    if e.seq is not None:
        st.last_stop_request_seq = e.seq

def _on_pause(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # STOP: freeze WITHOUT finalizing (finalize.py gates the wrap-up on `finished`, which a pause
    # never sets). A later `finalize` (EV_RUN_ABORT) can still wrap it up; RESUME lifts it.
    previous = (st.paused, st.pause_node_id, st.pause_generation)
    if d.get("node_id") is not None:
        # A human STOP is stronger than the scoped developer-crash circuit breaker. If the operator
        # paused while a build was still failing, the later automatic pause must not take ownership:
        # node reset/abort may clear only an auto-pause, never the explicit operator stop.
        if st.paused and st.pause_node_id is None:
            return
        nid = _coerce_node_id(d)
        n = st.nodes.get(nid) if nid is not None else None
        if (n is None or n.id in st.aborted_nodes or not _generation_matches(n, d)
                or n.status is not NodeStatus.failed or n.error_reason != "developer_crash"):
            return
        st.pause_node_id = nid
        st.pause_generation = n.attempt
    else:
        st.pause_node_id = None
        st.pause_generation = None
    st.paused = True
    if previous != (st.paused, st.pause_node_id, st.pause_generation):
        st.pause_event_seq = e.seq
        # …and the pausing writer's own words, beside the seq that identifies the row they came from.
        # Under the SAME guard on purpose: a second `pause` that does not change the triple did not
        # take effect (the run was already paused by the first), so the reason a reader is owed is the
        # first one's, exactly as `pause_event_seq` already answers with the first one's row.
        #
        # NOT capped, and that is the faithful choice: this field is a projection of a byte range that
        # is already durable in `events.jsonl`, so a cap here would make `looplab replay` disagree with
        # the log it replayed. Every producer already bounds its own text at the append site.
        reason = d.get("reason")
        st.pause_reason = str(reason) if isinstance(reason, str) and reason.strip() else None


def _on_restart(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Fold one durable, server-owned pause -> replacement-owner handoff.

    The old engine observes the operator pause and releases its singleton lock. At the same time the
    event itself is the resume request watermark, so losing the browser, command worker, or whole UI
    server cannot strand the run: the normal startup reconciler can claim and launch it. A replacement
    CLI clears the pause with ``resume`` and appends ``resume_served`` only after acquiring the lock.
    """
    _on_pause(st, e, {}, ctx)
    if e.seq > st.last_resume_request_seq:
        st.last_resume_request_seq = e.seq
        st.last_resume_request_ts = float(getattr(e, "ts", 0.0) or 0.0)
        st.last_resume_request_mode = "resume"

def _on_node_abort(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    legacy_unknown = n is None and _event_generation(d) is _MISSING
    if (nid is not None
            and (legacy_unknown or (n is not None and _control_generation_matches(n, d)))
            and nid not in st.aborted_nodes):
        if n is not None:
            _remove_current_failure(st, n)
        st.aborted_nodes.append(nid)
        if n is not None:
            n.rerun_from = None
            n.rerun_stage = None
        _clear_build_marker(st, d, nid)
        if st.pause_node_id == nid:
            st.paused = False
            st.pause_node_id = None
            st.pause_generation = None
            st.pause_reason = None
        _purge_node_requests(st, {nid})
        if st.approval_subject == nid or st.approved_node_id == nid:
            # A FINISHED run keeps its certificates; only the grant that named THIS node is void.
            _clear_approval(st)
        if st.champion == nid:
            st.champion = None
        if st.finished:
            if ctx.best_confirmed == nid:
                ctx.best_confirmed = None
            return
        _invalidate_completion_certificates(st, ctx)
        _invalidate_disclosed_holdout(st)

def _purge_node_requests(st: RunState, drop) -> None:
    """Drop queued force-confirm / force-ablate intents naming any node in `drop` (doc 25 EV-09).

    FOUR lists, in two pairs, and each pair must move together: a legacy bare-id list and a
    generation-stamped record list. Filtering one and forgetting its twin is the failure this
    single-sources, and it is not symmetric — the engine's `_pending_forced_*` readers consult the
    STAMPED list first, so a record left behind wins over an id the caller believed it had removed,
    and the request fires against a lifecycle that no longer exists.

    All four call sites — requeue-on-partition, tombstone, reset and abort — drop both queues. They
    are the events that end a node's current lifecycle, and a queued intent names a lifecycle, not
    a node.
    """
    st.confirm_requests = [nid for nid in st.confirm_requests if nid not in drop]
    st.confirm_request_generations = [
        r for r in st.confirm_request_generations if r.get("node_id") not in drop]
    st.ablate_requests = [nid for nid in st.ablate_requests if nid not in drop]
    st.ablate_request_generations = [
        r for r in st.ablate_request_generations if r.get("node_id") not in drop]


def _on_speculation_depth_settled(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Adopt one AUTO re-resolution of the run's speculation depth.

    A MINIMUM, not an assignment, and that is the whole design. The engine only ever settles AUTO
    DOWNWARD (`orchestrator.py::_settle_speculation_depth`), so taking the minimum over every row
    reproduces the engine's own sequence while being ORDER-TOLERANT and IDEMPOTENT — invariant #5's
    two requirements. Last-write-wins would satisfy neither: two rows spliced in the other order, or
    one row folded twice, would land on a different treatment, and a duplicated stale row could raise
    a depth back up after the run had already narrowed it.

    THE MINIMUM IS TAKEN OVER SETTLE ROWS ONLY, into a field of their own, and the effective depth is
    derived from that floor and the launch pin together (`_settle_folded_speculation_depth`). Folding
    the two facts into ONE field made the "order-tolerant" claim above false against the one event
    whose order actually mattered: `_on_run_started` ASSIGNS, so a settle row spliced BEFORE it was
    overwritten and the fold landed on the pin (measured on this exact log: 4 at splice position 0, 0
    at every other position). It was latent — `run_started` is first by construction — but this
    codebase writes ordering PRECONDITIONS down rather than leaving them as properties of an event
    (invariant #1 does it for `EV_NODE_EVAL_STARTED`), and here the precondition could simply be
    removed instead. There is now no order requirement between these two handlers at all.

    Nothing here is re-measured. The row's `evidence` is recorded for the operator and for
    `looplab inspect`; the fold reads only `depth`, so a resume on a box with different hardware
    continues under the treatment THIS RUN chose rather than one re-derived from the new host — the
    same property `run_started`'s pinned widths give, extended to a value that is allowed to move.

    Bounds are strict for the same reason `run_started`'s are: a bool/float/string in a malformed or
    hand-edited row must not be able to change the search treatment. A row the engine never wrote
    (depth above the pinned one) is simply inert, because the derivation caps the floor at the pin.
    """
    depth = d.get("depth")
    if not bounded_int(depth, 0, 64):
        return
    floor = st.speculation_depth_settled
    st.speculation_depth_settled = depth if floor is None else min(floor, depth)
    _settle_folded_speculation_depth(st)


def _on_run_width_settled(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Adopt one proposal-derived RE-PIN of the run's concurrency widths (docs/29 F1).

    LAST WRITE WINS, into fields of this row's own, and both halves of that matter.

    *Into its own fields*, never onto `run_started`'s `eval_parallel`/`llm_parallel`, is what makes
    the pair ORDER-TOLERANT against `run_started` itself (invariant #5). `_on_run_started` ASSIGNS, so
    a repin folded ahead of it would be silently overwritten and the fold would land on the pin — the
    exact defect measured for the `speculation_depth` pair (4 at splice position 0, 0 everywhere
    else), written down there rather than left to be rediscovered here. Each field has one writer and
    neither handler reads the other's before writing its own; `Engine._repin_settled_widths` resolves
    them, and it is the only place that has to know the precedence.

    *Last write wins*, not the minimum `_on_speculation_depth_settled` takes, because this repin is
    genuinely TWO-WAY. The depth ratchet only ever narrows, so a minimum reproduced the engine's own
    sequence for free. A width follows what the research proposed: it narrows when the Cards declare
    wide footprints and widens back — never past the launch pin — when they stop. A minimum would turn
    one wide proposal into a permanent serialization of the run, and a maximum would make a
    hand-edited row able to widen a treatment the engine had already narrowed. LWW over a total log
    order is what the engine actually did, and it is what a resume has to reproduce.

    Nothing here is re-derived. The row's `evidence` records the pool, the demand and the widest
    declared footprint the decision was made from, for `looplab inspect` and the operator; the fold
    reads only the two integers, so a resume on a box with a different GPU count continues at the
    width THIS RUN chose rather than at one recomputed from the new host. That is the whole reason the
    decision needs a durable event and not a per-turn recomputation.

    Bounds are strict, and the floor is 1 rather than 0 for a reason `settle_width` states: a live
    `0` is not AUTO, and a repin row is a live value by definition. A malformed or out-of-range field
    is DROPPED INDEPENDENTLY of its sibling — one poisoned axis in a hand-edited row must not also
    discard a valid re-pin of the other, and dropping leaves that axis at whatever the previous rows
    and the `run_started` pin already established.
    """
    for key, upper in (("eval_parallel", 1024), ("llm_parallel", 64)):
        value = d.get(key)
        if type(value) is not int or not 1 <= value <= upper:
            continue
        setattr(st, f"{key}_settled", value)


# The dispatch registry — event type -> handler. Unknown types are absent: they no-op.
# This table holds the handlers still defined in this module; each handler FAMILY split out of it
# (review 2026-09-22, EVT-12) owns the rows of the events it folds, beside their bodies, and
# `_HANDLERS` below is the union `fold` dispatches through.
_OWN_HANDLERS = {
    EV_RUN_STARTED: _on_run_started,
    EV_NODE_BUILDING: _on_node_building,
    EV_NODE_CREATED: _on_node_created,
    EV_NODE_EVAL_STARTED: _on_node_eval_started,
    EV_NODE_EVALUATED: _on_node_evaluated,
    EV_NODE_FAILED: _on_node_failed,
    EV_NODE_REPAIRED: _on_node_repaired,
    EV_NODE_TOMBSTONED: _on_node_tombstoned,
    EV_APPLIED_PARAMS_BACKFILLED: _on_applied_params_backfilled,
    EV_SCORE_METRICS_BACKFILLED: _on_score_metrics_backfilled,
    EV_RESUME_REQUESTED: _on_resume_requested,
    EV_RESUME_SERVED: _on_resume_served,
    EV_RESTART: _on_restart,
    EV_NODE_RESET: _on_node_reset,
    EV_STAGE_FINISHED: _on_stage_finished,
    EV_CONFIRM_EVAL: _on_confirm_eval,
    EV_NODE_CONFIRMED: _on_node_confirmed,
    EV_EVAL_NOISE_SEED: _on_eval_noise_seed,
    EV_EVAL_NOISE_FLOOR: _on_eval_noise_floor,
    EV_HOLDOUT_EVALUATED: _on_holdout_evaluated,
    EV_AGENT_VALIDATED: _on_agent_validated,
    EV_SETUP_FINISHED: _on_setup_finished,
    EV_RUN_SETUP_STARTED: _on_run_setup_started,
    EV_RUN_SETUP_FINISHED: _on_run_setup_finished,
    EV_APPROVAL_REQUESTED: _on_approval_requested,
    EV_APPROVAL_GRANTED: _on_approval_granted,
    EV_SPEC_PROPOSED: _on_spec_proposed,
    EV_SPEC_APPROVAL_REQUESTED: _on_spec_approval_requested,
    EV_SPEC_APPROVED: _on_spec_approved,
    EV_SPEC_DRIFT: _on_spec_drift,
    EV_ABLATE: _on_ablate,
    EV_FORESIGHT_SELECTED: _on_foresight_selected,
    EV_NODE_VALUE_ESTIMATED: _on_node_value_estimated,
    EV_SPECULATION_DEPTH_SETTLED: _on_speculation_depth_settled,
    EV_RUN_WIDTH_SETTLED: _on_run_width_settled,
    EV_PROXY_SCORED: _on_proxy_scored,
    EV_RUN_FINISHED: _on_run_finished,
    EV_FINALIZATION_FINISHED: _on_finalization_finished,
    EV_RESUME: _on_resume_or_run_reopened,
    EV_RUN_REOPENED: _on_resume_or_run_reopened,
    EV_RUN_ABORT: _on_run_abort,
    EV_PAUSE: _on_pause,
    EV_NODE_ABORT: _on_node_abort,
}
_HANDLER_TABLES = (_OWN_HANDLERS, _CONCEPT_HANDLERS, _JOURNAL_HANDLERS, _CARD_HANDLERS,
                   _SELECTION_HANDLERS, _REQUEST_HANDLERS)
_HANDLERS = {etype: handler for table in _HANDLER_TABLES for etype, handler in table.items()}
# The tables must be DISJOINT: a type two of them claim would be folded by whichever merged last,
# silently — the same no-op-by-shadowing class invariant #7 exists for. A bare `assert` at import,
# like `core/models.py`'s extra-metric channel partition: a coding error to fix before the process
# starts, not a runtime condition to survive.
assert len(_HANDLERS) == sum(len(table) for table in _HANDLER_TABLES), (
    "an event type is claimed by two fold handler tables")


def fold(events: Iterable[Event]) -> RunState:
    st = RunState()
    ctx = _FoldCtx()
    for index, e in enumerate(events):
        ctx.event_index = index
        handler = _HANDLERS.get(e.type)
        # Unknown event types (e.g. "budget") are ignored for state — forward-compat.
        if handler is not None:
            handler(st, e, e.data, ctx)
    return _finalize_fold(st, ctx)


def fold_run_start(events: Iterable[Event]) -> RunState:
    """The run-start record's fields WITHOUT the fold: every `run_started` row, in log order, through
    the fold's own `_on_run_started`, on a fresh `RunState` (review 2026-09-22, SRV2-11).

    EQUAL TO `fold(events)` ON EVERY FIELD THAT HANDLER ALONE WRITES — by construction, not by a second
    spelling of the rule. The handler reads nothing of the fold context and nothing of the state but
    `run_id`, which only it writes (FIRST START WINS: a row that established no identity is folded
    over by the next, here exactly as there), and no other handler or post-pass writes those fields.
    `tests/test_run_start_fold.py` re-derives both halves by AST and drives the equality over real
    logs. ONE field it writes is not the run-start's alone: `trust_gate_changed` moves `trust_gate`
    afterwards, so a reader of this state must never take `trust_gate` from it. Every field no
    `run_started` row writes is the `RunState` default here, not the run's value.

    For readers that want only the run-start record: `GET /api/runs/{id}/config`'s pin overlay paid a
    whole fold — O(log), the card ledger's finalize on top — on every request for thirty-odd fields
    of one row.
    """
    st = RunState()
    ctx = _FoldCtx()
    for index, e in enumerate(events):
        if e.type == EV_RUN_STARTED:
            ctx.event_index = index
            _on_run_started(st, e, e.data, ctx)
    return st


def _finalize_fold(st: RunState, ctx: _FoldCtx) -> RunState:
    """Apply the order-independent read-model tail to one isolated raw fold state."""
    # PART V (B): materialize delta-authored node concepts topologically once the whole DAG is folded
    # (order-tolerant; membership no-op unless a node authored a delta). Always invoke it so the typed
    # corruption receipt is recomputed/cleared for FoldCursor suffix snapshots as well.
    _materialize_concept_deltas(
        st,
        untrusted_modes=ctx.concept_mode_untrusted,
        capped_inputs=ctx.concept_input_capped,
        invalid_inputs=ctx.concept_input_invalid,
        base_capped=ctx.run_base_capped,
        base_invalid=ctx.run_base_invalid,
        run_base_seen=ctx.run_base_seen,
    )

    flagged = _apply_trust_gate(st)
    _select_best(st, flagged, ctx.best_confirmed, ctx.best_confirmed_significant)

    _derive_cards(st, card_enrichment_omissions=ctx.card_enrichment_omissions)
    # docs/23 Layer 1a: the card ledger (mirrors hypotheses); advisory, after best
    return st


class FoldCursor:
    """Incrementally accumulate an event prefix without changing ``fold`` semantics.

    Handlers mutate an *unfinalized* state in log order. ``snapshot`` deep-copies that raw state before
    applying the ordinary fold post-passes, because trust enforcement, best selection and Part-V delta
    materialization mutate their input and therefore must never leak back into the next suffix extension.
    The cursor is intentionally lock-free: its owner must serialize ``extend``/``snapshot`` as one read.
    """

    def __init__(self) -> None:
        self._state = RunState()
        self._ctx = _FoldCtx()
        self._event_count = 0

    @property
    def event_count(self) -> int:
        return self._event_count

    def extend(self, events: Iterable[Event]) -> int:
        """Apply a suffix and return the number of newly accumulated envelopes."""
        added = 0
        for e in events:
            self._ctx.event_index = self._event_count
            handler = _HANDLERS.get(e.type)
            # Unknown event types still advance the physical index because report/finish adjacency is
            # defined over envelopes, even though their state mutation is a forward-compatible no-op.
            if handler is not None:
                handler(self._state, e, e.data, self._ctx)
            self._event_count += 1
            added += 1
        return added

    def snapshot(self) -> RunState:
        """Return an independently mutable state byte-equivalent to ``fold`` of this prefix."""
        # never finalize the accumulator itself. Several post-passes are destructive
        # (``block`` marks nodes infeasible; concept DELTAs overwrite effective memberships). A deep
        # Pydantic copy makes every GET independent and preserves the raw state for the next append.
        state = self._state.model_copy(deep=True)
        return _finalize_fold(state, self._ctx)


