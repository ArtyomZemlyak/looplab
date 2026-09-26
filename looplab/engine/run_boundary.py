"""WHERE A FOLDED RUN STANDS, and whether `--drain-only` may drive it — pure functions of
`(RunState, events)` that decide which lifecycle event, if any, a re-entry may append.

Moved out of `cli/run_cmds.py` for doc 68 68.3b: the server's command worker asks the SAME drain
question before it spawns `looplab resume --drain-only` for a `node_reset` command
(`serve/run_commands.py::RunCommandService._spawn_under_claim`). A drain the CLI refuses exits
before it builds an engine, so it never writes the `command_ack` the command waits for, and the
worker would re-spawn it every monitor pass until the observation deadline; asked here first, the
refusal becomes the command's own terminal answer. One rule, two askers — never a copy, because a
copy is how the two ends of a boundary drift (doc 25 CT-03). `serve/` may import the engine, and
nothing here imports `cli` or `serve`.

`classify_prior_run` / `terminal_projection_incomplete` / `is_wrap_up` are the prior-run ladder
every CLI entry point shares; `drain_owed` (also re-exported by `engine/orchestrator.py`, whose drain
turn reads it) / `awaiting_rebuild` / `requeued_by_epoch` / `drain_only_refusal` are the drain's
own. The CLI keeps the operator-facing notices (`WRAP_UP_NOTICE`) and re-exports every name here
under its old spelling.
"""
from __future__ import annotations

from typing import Optional

from looplab.core.models import NodeStatus, RunState
from looplab.events.finalize_scope import incomplete_finalize_scope, is_guarded_abort

# The two wrap-up boundaries: respect the wrap-up already on disk, never lift the run. The CLI's
# `WRAP_UP_NOTICE` is the notice per kind and is held to exactly this set.
WRAP_UP_KINDS: frozenset[str] = frozenset({"finalization_pending", "pending_finalize"})


def terminal_projection_incomplete(state, events) -> bool:
    """Whether a folded run has an ACCEPTED terminal boundary whose wrap-up did not complete.

    `run_finished` lands BEFORE the engine's finalization checklist, and a richer scoped checklist
    can be interrupted part-way, so "finished" never answers "is the wrap-up done" on its own. Three
    commands need this exact disjunction and each used to spell it out (doc 25 CT-03) — and it is
    replay-critical, because it decides whether a command may append a lifecycle event at all.
    """
    return incomplete_finalize_scope(events) is not None or state.finalization_pending()


def classify_prior_run(prior, prior_events) -> str:
    """Which lifecycle boundary a folded prior run sits at, before a command re-enters the loop.

    `run` and `resume` each carried this ladder inline with identical echo strings for the first two
    rungs, and `finalize` a third variant of the same predicates. It decides WHICH EVENT, if any, is
    appended before re-entry, so a fix applied to one copy and not the others silently gives the
    entry points different lifecycles (doc 25 CT-03).

    The ORDER is the contract, not an implementation detail:

    * an incomplete terminal projection outranks everything — the wrap-up on disk owns the boundary;
    * a stop request newer than the finish, or a finish whose reason is `error`, is a PENDING
      FINALIZE and must be respected rather than lifted (the UI's finalize path spawns `resume` and
      still has to finalize);
    * only then the liftable states: a plain finish, then a plain pause.

    Callers keep their surface-specific differences — `run` REOPENS a finished dir while `resume`
    appends `resume` to it — because those genuinely differ; only the classification is shared.
    """
    if terminal_projection_incomplete(prior, prior_events):
        return "finalization_pending"
    # The CLASS, not the literal: a ceiling-ended run finishes `budget_exhausted` through the same
    # guarded path as `error`, and a literal here excluded it from `pending_finalize` — the
    # ordinary terminal of every budgeted campaign read as a clean finish with nothing owed.
    if prior.stop_requested and (
            not prior.finished or is_guarded_abort(prior.stop_reason)):
        return "pending_finalize"
    if prior.finished:
        return "finished"
    if prior.paused:
        return "paused"
    return "live"


def is_wrap_up(kind: str) -> bool:
    """Whether this boundary is WRAP-UP ONLY: the loop may complete the terminal the log already
    carries, and cannot start new work.

    Separate from `announce_wrap_up` because it is needed EARLIER and without the echo: `_engine`
    decides refuse-vs-warn on an unreachable LLM endpoint from exactly this predicate (a run that
    can only finish an existing wrap-up has no proposal left to degrade), and that decision happens
    before the engine exists, while the notice belongs at the point where the run is re-entered.
    """
    return kind in WRAP_UP_KINDS


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


def awaiting_rebuild(prior, node) -> bool:
    """A reset from implement/propose the loop head rebuilds before the drain evaluates it."""
    return (node.status is NodeStatus.pending and not node.tombstoned
            and node.id not in prior.aborted_nodes
            and node.rerun_from in ("implement", "propose"))


def requeued_by_epoch(prior, prior_events, owed) -> list[int]:
    """The owed nodes whose CURRENT lifecycle the holdout epoch rotation opened — re-queued for a
    full re-evaluation on the newly hidden rows, not reset by anyone (doc 68 68.3a, critic
    2026-09-26). Read off the one derivation of which event opened each lifecycle,
    `events/git_export.py::node_lifecycles` (`requeued_at` on the lifecycle the rotation ended)."""
    from looplab.events.git_export import node_lifecycles
    from looplab.events.types import EV_HOLDOUT_EVALUATED

    # A rotation re-queues only after a disclosure, so a log that never disclosed one cannot hold a
    # re-queued lifecycle — and the fold walk below is skipped for it (critic 2026-09-26).
    if not owed or not any(getattr(e, "type", None) == EV_HOLDOUT_EVALUATED
                           for e in prior_events or ()):
        return []
    superseded, _born = node_lifecycles(prior_events or ())
    out = []
    for node_id in owed:
        node = prior.nodes[node_id]
        ended = superseded.get((node_id, node.attempt - 1)) if node.attempt > 0 else None
        if ended is not None and getattr(ended, "requeued_at", ""):
            out.append(node_id)
    return sorted(out)


def _epoch_salted_split(prior) -> bool:
    """Whether the host scores this run's search on a split salted by the search epoch: host grading
    with a holdout fraction above 0 — or one the log never pinned (older than the 2026-07-03 pin),
    which the resume fills from the snapshot's default: unknown is not zero (critic 2026-09-26)."""
    if not getattr(prior, "host_grading", None):
        return False
    fraction = getattr(prior, "holdout_fraction", None)
    return fraction is None or float(fraction) > 0


def measured_epochs(events) -> dict[int, int]:
    """The search epoch each node's latest evaluation was MEASURED in: the fold's `search_epoch` right
    after that node's last accepted `node_evaluated` row, read off the fold itself (`FoldCursor`, one
    pass) — a rotation that re-queues a node re-measures it, a finished-reopen does not."""
    from looplab.core.models import coerce_node_id
    from looplab.events.replay import FoldCursor
    from looplab.events.types import EV_NODE_EVALUATED

    cursor = FoldCursor()
    raw = cursor._state
    out: dict[int, int] = {}
    for event in events or ():
        cursor.extend((event,))
        data = getattr(event, "data", None)
        if getattr(event, "type", None) != EV_NODE_EVALUATED or not isinstance(data, dict):
            continue
        nid = coerce_node_id(data)
        node = raw.nodes.get(nid) if nid is not None else None
        if node is not None and node.status is NodeStatus.evaluated:
            out[nid] = int(raw.search_epoch)
    return out


def drain_only_refusal(prior, prior_kind: str, prior_events=None) -> Optional[tuple[int, str]]:
    """Why `resume --drain-only` (doc 68 68.3a) will not drive this run — `(exit code, message)`,
    decided BEFORE anything is appended — or None to proceed. Critic 2026-09-26, driven: the plain
    lift below appends `resume`, and on a FINISHED run that opens a new search epoch; after a holdout
    disclosure the epoch rotation re-queues every evaluated node, which the drain then counted as
    owed and re-evaluated, all of them, on a run nobody had reset.

    * a wrap-up boundary: a finalize is pending, and a drain never finalizes (exit 2);
    * nothing owed at all (`drain_owed` above, or a reset from implement/propose
      still waiting for the loop head's rebuild): nothing to drain, and a finish or a pause is not
      lifted only to be put back (exit 0) — a `node_reset` re-opens a finished run itself;
    * a holdout was disclosed and lifting a pause or a finish would rotate the epoch, re-queuing
      every evaluated node (exit 2);
    * owed nodes the epoch rotation RE-QUEUED rather than anyone reset — one reset after a
      disclosure re-opens every incumbent, and a drain would retrain each of them from scratch
      without having said so (exit 2);
    * a FINISHED host-graded run with a holdout split: lifting a finish opens a new search epoch,
      which re-carves the rows the host scores the search on, so the drained node would be ranked
      against incumbents measured on other rows (exit 2; critic 2026-09-26, driven; the root is
      doc 68 68.3c) — and the same split already re-carved since an incumbent was measured, which
      is where a reset of a finished run leaves it (`measured_epochs`);
    * any other FINISHED run that still owes work — the eval budget finalized it with a reset node
      pending — is lifted and drained like a paused one.
    """
    if is_wrap_up(prior_kind):
        return 2, ("a finalize is pending on this run and --drain-only never finalizes; run "
                   "`looplab resume` without it (or `looplab finalize`) to complete it")
    owed = sorted(node.id for node in prior.nodes.values() if drain_owed(prior, node))
    rebuild = [node.id for node in prior.nodes.values() if awaiting_rebuild(prior, node)]
    if not (owed or rebuild):
        if prior_kind == "finished":
            # Not "a reset re-opens it INSTEAD of a new epoch": a reset of a finished run opens one
            # too (critic 2026-09-26) — the finish is simply not lifted for nothing.
            return 0, ("run is finished and nothing is owed an evaluation — nothing to drain; the "
                       "finish is left as it was (a `node_reset` re-opens a finished run, in a new "
                       "search epoch like any reopen)")
        return 0, "nothing is owed an evaluation — nothing to drain; the run is left as it was"
    if prior_kind in ("paused", "finished") and prior.holdout_evaluated_ids:
        return 2, ("a holdout was disclosed on this run: lifting its "
                   + ("pause" if prior_kind == "paused" else "finish")
                   + " opens a new search epoch and re-queues every evaluated node for "
                   "re-evaluation on the newly hidden rows, which --drain-only will not buy on its "
                   "own; resume without --drain-only if that is the intent")
    salted = _epoch_salted_split(prior)
    if prior_kind == "finished" and salted:
        return 2, ("this host-graded run is finished, and lifting a finish opens a new search epoch, "
                   "which re-carves the split the host scores the search on: node(s) "
                   f"{', '.join(map(str, owed or rebuild))} would be scored on other rows than every "
                   "incumbent they are ranked against (doc 68 68.3c). --drain-only will not mix the "
                   "two; a plain `looplab resume` would")
    epoch = int(getattr(prior, "search_epoch", 0) or 0)
    if salted and epoch > 0:
        # The SAME mixing when the epoch already moved before the drain — a reset of a finished run
        # opens one itself and clears the finish, so the clause above never saw it (critic
        # 2026-09-26, driven: a reset node re-scored 0.5111 on new rows against incumbents measured
        # on the old ones; it is also the doc 68 68.3b drain command's own path). Decided from what
        # the log says each incumbent was MEASURED in, not from the drain's own lift.
        measured = measured_epochs(prior_events or ())
        stale = sorted(node.id for node in prior.nodes.values()
                       if node.status is NodeStatus.evaluated and not node.tombstoned
                       and node.id not in prior.aborted_nodes and node.id not in owed
                       and measured.get(node.id, 0) < epoch)
        if stale:
            return 2, (f"the split this host-graded run is scored on was re-carved (search epoch "
                       f"{epoch}) after node(s) {', '.join(map(str, stale))} were measured: node(s) "
                       f"{', '.join(map(str, owed or rebuild))} would be scored on other rows than "
                       "the incumbents they are ranked against (doc 68 68.3c). --drain-only will not "
                       "mix the two; a plain `looplab resume` would")
    requeued = requeued_by_epoch(prior, prior_events, owed)
    if requeued:
        return 2, (f"node(s) {', '.join(map(str, requeued))} were re-queued by the holdout epoch "
                   "rotation, not reset: after a holdout disclosure one reset re-opens every "
                   "evaluated node for a full re-evaluation on the newly hidden rows, and a drain "
                   f"would retrain all {len(owed)} owed node(s); resume without --drain-only if "
                   "that is the intent")
    return None
