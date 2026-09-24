"""The fold's REQUESTS family: operator intents the fold only QUEUES or RECORDS for the engine.

Split out of `events/replay.py` (review 2026-09-22, EVT-12). Every handler here turns a control
intent into a queue entry, an override or a note that the engine consumes later, plus the positional
receipts that say a queued intent was served — and none of them transitions a node's lifecycle or
the run's state:

* the served queues — force-confirm / force-ablate (one queueing rule, `_queue_forced_request`), fork,
  inject and manual deep-research requests, the `_advance_request_cursor` rule their receipts
  advance by, and `confirm_done`;
* the standing steering — hints, a pinned strategy, budget extensions, a promoted alias;
* the operator's notes — annotations and versioned comments.

What deliberately STAYS in `replay.py` is the other half of the review's "run controls" (~680
lines): pause / restart / resume / reopen / resume-served / run-finished / node-abort, which move a
run or a node through its lifecycle and share the lifecycle's invalidation rules
(`_invalidate_completion_certificates`, `_rotate_search_epoch`, `_purge_node_requests`, …) with the
node handlers there. Moved VERBATIM, comments included; `replay.py` re-exports the names tests import
off it and merges `HANDLERS` into its dispatch table.
"""
from __future__ import annotations

import math

from looplab.core.models import Event, RunState, coerce_node_id as _coerce_node_id
from looplab.events.comment_projection import apply_comment_event
from looplab.events.replay_ctx import (_MISSING, _FoldCtx, _control_generation_matches,
                                       _event_generation, _generation_matches)
from looplab.events.types import (
    EV_ANNOTATION, EV_BUDGET_EXTEND, EV_COMMENT_CREATED, EV_COMMENT_EDITED,
    EV_COMMENT_RESOLUTION_CHANGED, EV_CONFIRM_DONE, EV_DEEP_RESEARCH, EV_FORCE_ABLATE,
    EV_FORCE_CONFIRM, EV_FORK, EV_FORK_DONE, EV_HINT, EV_INJECT_DONE, EV_INJECT_NODE, EV_PROMOTE,
    EV_SET_STRATEGY, standing_hint_dedup_key,
)


def _on_budget_extend(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # max_seconds / max_eval_seconds are ABSOLUTE new ceilings (last write wins). add_nodes is
    # an ADDITIVE delta — "give the run N more nodes" — so several extensions accumulate; the
    # orchestrator folds it into the policy's effective max_nodes so a finished run, once
    # reopened, proposes more experiments instead of immediately re-finishing.
    # max_seconds/max_eval_seconds (budgets) + timeout/the two parallel axes are ABSOLUTE new
    # values (last write wins). Canonical and legacy parallel spellings remain replay-compatible.
    # COERCE to number in the fold: a UI form / TUI can post a STRING ("600"), and the engine
    # compares these numerically (`total_eval_seconds >= max_es`), so an un-coerced string would
    # raise TypeError in the main loop — and because the event replays, EVERY resume re-crashes
    # (a permanent poison event). A non-numeric value is skipped, not stored.
    # `eval_timeout` (2026-09-24) is the operator's live per-eval budget of an EVAL-SPEC task — the
    # number `Settings.timeout` is not, on that branch (`engine/shared.py::effective_eval_time_budget`).
    # Absolute and last-write-wins like its siblings; its reader is `command_eval.eval_timeout_override`.
    for _k in ("max_seconds", "max_eval_seconds", "timeout", "eval_timeout"):
        _raw = d.get(_k)
        if _raw is None or isinstance(_raw, bool):
            continue
        try:
            _v = float(_raw)
        except (TypeError, ValueError, OverflowError):
            continue
        # malformed historical control events must remain total under replay. Reject
        # non-finite/non-positive ceilings instead of persisting a resume-crashing poison value.
        if math.isfinite(_v) and _v > 0:
            st.budget_overrides[_k] = _v
    for _legacy, _canonical, _upper in (
            ("max_parallel", "eval_parallel", 1024),
            ("parallel_build", "llm_parallel", 64)):
        _selected: tuple[str, int] | None = None
        # Legacy first, canonical last: canonical wins when one event carries both valid spellings.
        # Across events, whichever spelling arrived last owns the whole axis family and removes the
        # stale sibling; otherwise apply's canonical-last order could resurrect an older value.
        for _k in (_legacy, _canonical):
            _raw = d.get(_k)
            if _raw is None or isinstance(_raw, bool):
                continue
            if isinstance(_raw, float) and (
                    not math.isfinite(_raw) or not _raw.is_integer()):
                continue
            try:
                _v = int(_raw)
            except (TypeError, ValueError, OverflowError):
                continue
            if 0 <= _v <= _upper:
                _selected = (_k, _v)
        if _selected is not None:
            _key, _value = _selected
            # one folded key per authority family preserves true event-order LWW while
            # retaining the latest event's spelling for old/no-broker resume compatibility.
            st.budget_overrides.pop(
                _legacy if _key == _canonical else _canonical, None)
            st.budget_overrides[_key] = _value
            if _canonical == "llm_parallel" and _key == _canonical:
                # the legacy alias historically governed only build fan-out. Preserve the
                # last explicit canonical shared-total intent independently, so canonical->legacy
                # sequences behave identically before and after process restart without retroactively
                # throttling legacy-only logs.
                st.budget_overrides["llm_broker_total"] = _value
    _raw_add = d.get("add_nodes")
    if _raw_add is not None and not isinstance(_raw_add, bool):
        if not (isinstance(_raw_add, float) and (
                not math.isfinite(_raw_add) or not _raw_add.is_integer())):
            try:
                _add = int(_raw_add)
                if 0 < _add <= 1_000_000:
                    st.budget_overrides["add_nodes"] = (
                        int(st.budget_overrides.get("add_nodes", 0)) + _add)
            except (TypeError, ValueError, OverflowError):
                pass

def _on_hint(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Append-only by default; a `replace` hint supersedes all prior standing directives
    # (mirrors set_strategy/pending_strategy) so the boss can rewrite the single directive
    # instead of accumulating contradictory ones. Replay-safe: deterministic over the log.
    if d.get("replace"):
        st.pending_hints = [d]
    elif not any(
        standing_hint_dedup_key(hint) == standing_hint_dedup_key(d)
        for hint in st.pending_hints
        if isinstance(hint, dict)
    ):
        # Standing directives are semantic state, not command history. A double click, lost-response
        # retry, or old duplicate events must not repeat the same instruction in every later prompt.
        st.pending_hints.append(d)

def _on_set_strategy(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # A7 operator override (HITL parity with pause/hint): the human pins a Strategy. The
    # engine applies it before consulting the Strategist, so a human always wins. The pin owns
    # only the fields it names (including canonical concurrency/lane allocations) and STAYS in force
    # for the rest
    # of the run (it is not cleared on apply) — a later set_strategy overwrites it; the
    # Strategist keeps tuning everything else (see Engine._maybe_consult_strategist).
    st.pending_strategy = d.get("strategy")


def _queue_forced_request(st: RunState, d: dict, requests: list, generations: list) -> None:
    """Fold a `force_confirm` / `force_ablate` intent onto its queue pair.

    The two handlers were byte-identical but for the target lists. The shape is a generation CAS:
    a stamped intent is queued only when the node is live and its `attempt` still matches, so a
    control authored against a since-reset lifecycle is DROPPED rather than applied to new code.
    The legacy arm exists for logs predating the stamp — an unstamped intent for a node that has not
    been created yet binds when it appears, which is why it is admitted with no generation check.
    """
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if (n is not None and not n.tombstoned and nid not in st.aborted_nodes
            and _control_generation_matches(n, d)):
        requests.append(nid)
        generations.append({"node_id": nid, "generation": n.attempt})
    elif (nid is not None and nid not in st.aborted_nodes and n is None
          and _event_generation(d) is _MISSING):
        requests.append(nid)   # legacy queued-before-create intent


def _on_force_confirm(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    _queue_forced_request(st, d, st.confirm_requests, st.confirm_request_generations)

def _on_force_ablate(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    _queue_forced_request(st, d, st.ablate_requests, st.ablate_request_generations)

def _on_fork(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d, "from_node_id")
    n = st.nodes.get(nid) if nid is not None else None
    if (n is not None and not n.tombstoned and nid not in st.aborted_nodes
            and _control_generation_matches(n, d)):
        record = dict(d)
        record["from_node_id"] = nid
        record.setdefault("generation", n.attempt)
        st.fork_requests.append(record)
    elif (nid is not None and nid not in st.aborted_nodes and n is None
          and _event_generation(d) is _MISSING):
        st.fork_requests.append(dict(d))  # legacy queued-before-create intent

def _advance_request_cursor(done: int, total: int, idx: object) -> int:
    """The ONE rule every `<x>_requests` / `<x>s_done` positional gate advances by.

    `fork_requests`/`forks_done` and `inject_requests`/`injects_done` are the operator-steering
    queues: the engine serves `requests[done]` and appends a receipt naming the position it just
    completed. Both used to hand-roll their own partial version of this, with complementary holes —
    fork keyed on `from_node_id` (not unique: re-forking one promising node is the ordinary pattern,
    so a duplicate receipt consumed the operator's SECOND fork), inject keyed on a stamped absolute
    index with no queue bound (an orphan receipt walked the cursor past the queue and stranded the
    next intent forever). `_on_card_build_done` already had the right shape; this is that shape,
    shared, so a third queue cannot invent a fourth set of semantics.

    The receipt names its own position, which makes the rule self-healing rather than cumulative:

    * a receipt for a position BEFORE the cursor is one already completed — a duplicate or a replay —
      and must not consume the request now at the head;
    * a receipt at or after the cursor completes through that position, clamped to the queue, so a
      log whose stamped indices were computed under older fold semantics still converges on
      "everything up to here was served" instead of silently dropping every later receipt;
    * the cursor may never overrun the queue, so nothing can strand an intent appended afterwards.

    A row with NO usable index is legacy (or forged): it advances by one, still queue-bounded, so old
    logs fold exactly as they always did. Bools are rejected — `type(True) is int` is False, but an
    explicit check keeps that a stated property rather than an accident of the type test.
    """
    total = max(0, total)
    done = min(max(0, done), total)
    if isinstance(idx, bool) or type(idx) is not int:
        return min(done + 1, total)          # legacy/unusable index — advance one, never past the end
    if idx < done:
        return done                          # already completed; not this head's receipt
    return min(idx + 1, total)


def _on_fork_done(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    """Advance the fork queue cursor by the position the receipt names.

    Every producer serves `fork_requests[forks_done]` and stamps that index (`_serve_forced_requests`
    and `_close_node_creating_forced_request_before_terminal_gate`). `from_node_id` is deliberately
    NOT the key: two queued forks of the same parent are indistinguishable by it, which is exactly the
    case a duplicate receipt used to consume. It stays a bind for LEGACY rows that carry no index, and
    `generation` is compared by neither — the served branch stamps `current.attempt`, which for a
    legacy unstamped request differs from the request record by design.
    """
    if "idx" not in d:
        # Legacy receipt: keep the parent bind that shipped before the index existed. It cannot
        # separate two same-parent forks, but it still rejects a receipt naming a different fork.
        if st.forks_done < len(st.fork_requests):
            head = st.fork_requests[st.forks_done]
            receipt_pid = _coerce_node_id(d, "from_node_id")
            head_pid = _coerce_node_id(head, "from_node_id")
            if receipt_pid is not None and head_pid is not None and receipt_pid != head_pid:
                return                       # a receipt for some other fork cannot consume this head
    st.forks_done = _advance_request_cursor(
        st.forks_done, len(st.fork_requests), d.get("idx"))


def _on_inject_node(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # COPY, like `_on_fork` does: `EventStore` caches parsed `Event`s across `read_all()`, so
    # storing the live `data` dict would let any in-place mutation of a folded request change
    # what every later fold in this process sees — folded state silently diverging from the
    # bytes on disk. No consumer mutates today; the copy keeps it that way by construction.
    st.inject_requests.append(dict(d))  # operator-authored experiment (manual tree edit)

def _on_inject_done(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Same positional rule as `_on_fork_done` — see `_advance_request_cursor` for why the receipt's
    # own index, not the fold's current cursor, is the authority. Both producers stamp
    # `{"idx": state.injects_done}`; a legacy row without one advances by a queue-bounded step.
    st.injects_done = _advance_request_cursor(
        st.injects_done, len(st.inject_requests), d.get("idx"))

def _on_deep_research(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.research_requests.append(d)       # manual "go think hard" request (control event)

def _on_confirm_done(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)   # forced-confirm finished for this node (gate; selection untouched)
    n = st.nodes.get(nid) if nid is not None else None
    if (n is not None and nid not in st.aborted_nodes and _generation_matches(n, d)
            and nid not in st.confirmed_forced):
        st.confirmed_forced.append(nid)
    if n is not None and nid not in st.aborted_nodes and _generation_matches(n, d):
        key = {"node_id": nid, "generation": n.attempt}
        if key not in st.confirmed_forced_generations:
            st.confirmed_forced_generations.append(key)

def _on_annotation(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # `annotation` is a sanctioned /control event appended VERBATIM, and `annotations` is keyed by int
    # node id (dict[int, list[str]]) — so a forged `{"node_id":[999]}` would make `setdefault` hash the
    # unhashable list and raise TypeError, bricking the fold (same class as the approval grant above).
    # `_coerce_node_id` guards the key (reject bool / unhashable / non-coercible) so it can never raise; a
    # null/garbage id simply drops the note.
    nid = _coerce_node_id(d)
    if nid is None:
        return
    st.annotations.setdefault(nid, []).append(d.get("text", ""))
    if apply_comment_event(st.comments, e) is not None:
        st.comments_revision = e.seq


def _on_comment(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Collaboration is audit-only and selection-neutral.  The shared reducer applies only an exact
    # version chain and turns malformed/hand-authored records into deterministic no-ops.
    if apply_comment_event(st.comments, e) is not None:
        st.comments_revision = e.seq

def _on_promote(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    legacy_unknown = (n is None and nid not in st.aborted_nodes
                      and _event_generation(d) is _MISSING)
    if legacy_unknown or (n is not None and not n.tombstoned and nid not in st.aborted_nodes
                          and _control_generation_matches(n, d)):
        st.promotions.append(d)
        if d.get("alias", "champion") == "champion":
            st.champion = nid


# This family's rows of the fold's dispatch table. `replay.py::_HANDLERS` is assembled from every
# family's table and refuses a type two of them claim, so a request handler is registered HERE,
# beside its body, and nowhere else. The three comment events share one reducer, as they always did.
HANDLERS = {
    EV_BUDGET_EXTEND: _on_budget_extend,
    EV_HINT: _on_hint,
    EV_SET_STRATEGY: _on_set_strategy,
    EV_FORCE_CONFIRM: _on_force_confirm,
    EV_FORCE_ABLATE: _on_force_ablate,
    EV_FORK: _on_fork,
    EV_FORK_DONE: _on_fork_done,
    EV_INJECT_NODE: _on_inject_node,
    EV_INJECT_DONE: _on_inject_done,
    EV_DEEP_RESEARCH: _on_deep_research,
    EV_CONFIRM_DONE: _on_confirm_done,
    EV_ANNOTATION: _on_annotation,
    EV_COMMENT_CREATED: _on_comment,
    EV_COMMENT_EDITED: _on_comment,
    EV_COMMENT_RESOLUTION_CHANGED: _on_comment,
    EV_PROMOTE: _on_promote,
}
