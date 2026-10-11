"""Is there any work left for a finished run to do under the settings it is reopened with?

THE QUESTION THIS ANSWERS, and why it is asked at all (doc 75 UX-05). `resume`, `stop` and a repeat
`run` on a finished `--out` each reopened the demo and wrote 17, 1 and 17 rows: a fresh
`finalize_scope`, `reflection`, `concept_curation`, `claim_curation` and `llm_cost` — on a run with a
model those steps can SPEND — and not one new experiment, because the node budget was already spent.
Nothing said so; the reason (`max_nodes`) and the remedy (`--max-nodes`) went unnamed.

WHY NOT `len(nodes) >= max_nodes`, the plan's first spelling (rejected by its critique, doc 75 §12):
the engine's real ceiling is `max_nodes + budget_overrides["add_nodes"] + refunded reservations`
(`engine/orchestrator.py::Engine._hard_node_reservation_limit`), and what is USED is the monotonic
id allocator (`engine/card_reservation.py::_node_id_ceiling`), not the node count. The Assistant's
`extend_budget` appends `budget_extend` and reopens through `resume`; a node-count test would refuse
exactly that continuation. So this is the engine's own admission arithmetic as a pure function of
`(state, events, max_nodes)` — the settings the run is about to be reopened WITH, so `--max-nodes 12`
on the same command is capacity.

WHAT IT REFUSES TO DECIDE. Only the one certain case: no node can be minted AND none is left to
evaluate. A finish with capacity left (an empty action ladder, a plateau stop, a wall-clock stop)
reopens exactly as before — the engine, not this reader, decides whether it then finds work. A
`pending_finalize` or `finalization_pending` boundary is `engine/run_boundary.py::classify_prior_run`'s
and is never consulted here. Pure: no I/O, nothing appended.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from looplab.events.types import (EV_APPROVAL_GRANTED, EV_BUDGET_EXTEND, EV_COMMAND_ACK,
                                  EV_DEEP_RESEARCH, EV_FORCE_ABLATE, EV_FORCE_CONFIRM, EV_FORK,
                                  EV_INJECT_NODE, EV_METRIC_RETARGET, EV_NODE_BUILDING, EV_NODE_RESET,
                                  EV_RESTART, EV_RESUME, EV_RUN_REOPENED, EV_SET_STRATEGY,
                                  EV_TRACK_REQUESTED)

# Operator intents a reopened finished run SERVES (forced confirm, deep research, a strategy pin, a
# restart, an inject …). One recorded after the run's last finish is work, whatever the node budget
# says: the server appends it and spawns `looplab resume`, and a no-op child left it unserved while
# the reconciler re-spawned it until the command timed out (code review of doc 75 UX-05).
_REOPENING_INTENTS = frozenset({
    EV_FORCE_CONFIRM, EV_DEEP_RESEARCH, EV_SET_STRATEGY, EV_RESTART, EV_RUN_REOPENED,
    EV_INJECT_NODE, EV_FORK, EV_NODE_RESET, EV_FORCE_ABLATE, EV_BUDGET_EXTEND, EV_METRIC_RETARGET,
    EV_TRACK_REQUESTED, EV_APPROVAL_GRANTED, EV_RESUME,
})


@dataclass(frozen=True)
class NodeBudget:
    used: int        # the next node id the allocator would hand out (= ids ever reserved or created)
    limit: int       # the operator ceiling plus refunded reservations, as the engine computes it
    pending: int     # nodes created and not yet terminal — work a reopened run would still do

    @property
    def spent(self) -> bool:
        return self.used >= self.limit and self.pending == 0


def node_budget(state, events, max_nodes) -> NodeBudget:
    """The engine's node admission arithmetic for a run reopened with `max_nodes`."""
    from looplab.search.card_selection import refunded_node_reservations

    try:
        base = int(max_nodes)
    except (TypeError, ValueError, OverflowError):
        base = 0
    overrides = getattr(state, "budget_overrides", None) or {}
    try:
        added = int(overrides.get("add_nodes", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        added = 0
    operator_limit = max(0, base + added)
    limit = operator_limit + refunded_node_reservations(state, operator_limit)
    building = max((e.data.get("node_id", -1) for e in events
                    if e.type == EV_NODE_BUILDING and isinstance(e.data.get("node_id"), int)),
                   default=-1)
    used = max(max(state.nodes, default=-1), building) + 1
    pending = sum(1 for node in state.nodes.values() if getattr(node, "status", "") == "pending")
    return NodeBudget(used=used, limit=limit, pending=pending)


def nothing_left_sentence(state, events, max_nodes, *, run_dir, verb: str) -> Optional[str]:
    """The one line a re-entering command prints instead of reopening, or None to reopen.

    `verb` is the command the operator typed (`resume` / `run`); the remedy names the flag that
    raises the budget on that same command, with the number one budget higher."""
    if not getattr(state, "finished", False) or work_queued_after_finish(state, events):
        return None
    budget = node_budget(state, events, max_nodes)
    if not budget.spent:
        return None
    evaluated = sum(1 for node in state.nodes.values()
                    if getattr(node, "status", "") in ("evaluated", "failed"))
    more = budget.limit + max(1, budget.limit)
    head = (f"already finished: {evaluated}/{budget.limit} experiments, the node budget is spent; "
            f"nothing to continue.")
    if verb == "run":
        return (f"{head}\n  To continue it: looplab resume {run_dir} --max-nodes {more}"
                f"\n  Or start a fresh run: pass a new --out (e.g. --out {run_dir}-2)")
    return f"{head}\n  To continue it: looplab resume {run_dir} --max-nodes {more}"


def work_queued_after_finish(state, events) -> bool:
    """Is an operator waiting on this finished run? Then it reopens, budget or not.

    Three signals, each the engine's own: a resume request no engine has served
    (`RunState.resume_pending`), a server command intent (`_command_id`) with no `command_ack`
    (`engine/forced_requests.py::_unacked_command_suffix` reads the same marker), and a control
    intent recorded after the run's last finish."""
    try:
        if state.resume_pending():
            return True
    except AttributeError:
        pass
    acked = {(str((event.data or {}).get("command_id")), (event.data or {}).get("event_seq"))
             for event in events if event.type == EV_COMMAND_ACK}
    finish = getattr(state, "last_finish_seq", -1)
    for event in events:
        marker = (event.data or {}).get("_command_id")
        if marker and (str(marker), event.seq) not in acked:
            return True
        if event.seq > finish and event.type in _REOPENING_INTENTS:
            return True
    return False
