"""The PLAN artifact and the endgame reserve the dispatcher honours (doc 52 row 18).

Until 2026-09-06 the endgame existed only as a Strategist RULE: `RuleStrategist._decide_machinery`
switched `merge_mode` to `ensemble` at 80 % of the node budget — durable through
`strategy_decision`, but a CONSULT that fires on a cadence and may never land inside the last
fifth of a short run, and one that reserved nothing: the dispatcher kept opening breadth until the
budget died. Doc 10 P2 / doc 11 D13 asked for a durable plan — budget allocation across phases, a
reserve the dispatcher honours, re-planning on stagnation.

The plan is a FOLDED event (`EV_PLAN`, `RunState.plan`) written by the main task:

* `build_plan` cuts the node budget into `seed` / `search` / `endgame` phases from the settings
  the run was launched with (`endgame_reserve_frac`, the fraction of `max_nodes` kept for the
  endgame; 0 = no plan, the historical dispatch). The endgame phase carries `kinds`: `merge`
  (the top-2 ensemble, once) and `sweep` (a champion sweep for every remaining slot).
* `endgame_actions` is the dispatcher's rule inside the reserve: pending evaluations and the
  finish are untouched; every other create is replaced by the endgame's own — the merge if it has
  not been minted in the reserve yet and a partner qualifies (see below), else an `improve` of
  the champion stamped `_sweep`, whose idea `engine/node_build.py::_prepare_node_idea` asks the
  k-NN surrogate for (`search/surrogate.py`, bounds inferred from the run's own evaluated
  params; the LLM Researcher is its fallback below warm-up). A selected Card that already IS an
  endgame action (an admissible merge of two evaluated nodes, an improve of the champion) keeps its
  slot.
* `replan` re-cuts on a live `max_nodes` change (the reserve follows the budget) and on a HARD
  stall — `stall_rung` at two windows, the same identity the Strategist's plateau consult keys on
  — by starting the endgame at the current node count: a search that has stalled for two windows
  spends what is left on recombination and refinement rather than more breadth.

THE MERGE PARTNER (MiniOneRec inf13, 2026-09-27). The ensemble's two parents came from
`search/policy.py::pareto_front` — the run's non-dominated set over the primary metric plus every
authenticated, orientable extra metric, the metric leaders alone when no such axis exists — and on
inf13 that was node 5 (4.166x) and node 12 (4.102x), node 12 being node 5's OWN CHILD: one idea
twice, separated by noise. The merge (card-15 -> node 13) failed the recall gate at -0.262, and the
same pair was minted again as card-16. `_EndgameView.merge_pick` therefore walks the partners in
that order and takes the first that (1) is not an ancestor or descendant of the leader, over every
`parent_ids` edge transitively, and (2) was never paired with it — neither as a merge NODE of any
status, before or after the endgame start, nor as a live merge CARD, either order. No qualifying
partner means no merge: the reserve falls through to the sweep. A selected merge Card is held to the
same rule before it keeps its slot (`_is_endgame_action` accepted any merge, although this docstring
promised "of two evaluated nodes"): two breedable parents, neither the other's ancestor, a pair no
merge node holds, and the oldest live Card of its pair.

ONE PREDICATE FOR EVERY LANE THAT BUYS (`endgame_admits`). The gate only ever saw the turn's
SELECTED actions; the lanes that PAY never asked it. On inf13 the raw lane staged a Researcher
improve of node 12 (card-14, a paid proposal) that the gate then displaced with its own merge, and the
speculative election built the policy's crossover of 12 and 5 (card-16) while the gate would have
refused it — the raw lane re-derives the policy's action from `speculative_raw_actions`, and a Card
election reads `_election_excluded_card_ids`, and neither is the gate. `endgame_admits(state, plan,
action)` answers "would the gate let this action through as it is?" — a Card's action when it is an
endgame action, a raw action only when it IS the reserve's own next action, everything outside the
reserve — and the engine asks it before the raw lanes stage (`orchestrator.py::_handle_create_actions`,
`_occupancy_paced_creates`, `speculation.py::_card_phase_request_build`) and excludes the Cards it
refuses from every election and from the claim and freshness questions that must agree with the
election (`endgame_refused_card_ids`, carried as `SpeculativeSelectionContext.refused_card_ids`).
A build already bought is never refused: evaluations are untouched, and a claimed or committed
subject is exempt from the refusal the same way it is exempt from the in-flight exclusion.

A BOUNDED STALL EPISODE (`Settings.endgame_stall_nodes`, product 3, 0 = the permanent endgame). A
stall-triggered endgame used to run to the end of the budget: on inf13 (`max_nodes` 100000, the
"unbounded" spelling) the row at node 12 reserved 99,988 nodes for merges and sweeps, forever. With
`K > 0` the stall row carries `endgame_end = at_node + K` and the `champion` it was measured against;
`in_endgame` is `start <= n < end` (a row without an end keeps the historical `n >= start`), and the
next `replan` writes a `reopened` row — the ordinary cut, breadth again — once `n` reaches the end or
the best node differs from `champion`. ONE episode per champion (`stall_champions` rides every later
row), so a champion whose episode is spent is not re-stalled the next turn. A BUDGET RE-CUT CARRIES a
live episode: the Card-mode ceiling subtracts pending build requests, so `max_nodes` flickers between
99,998 and 100,000 whenever a request is open, and every flicker used to drop the stall start, re-start
the endgame at the current node and owe the one-time merge again. A legacy stall row (written with
`K = 0`, no `endgame_end`) is re-evaluated on the first turn with `K > 0`: reopened if the corrected
stall rung is under two or the champion was crowned inside it, else bounded from its own start.

THE OPERATOR'S NODES ARE NOT THE ENGINE'S SEARCH (doc 69 69.25, `Settings.endgame_inject_recut`). On
`minionerec-backbones-v10` the plan was cut once, on 24.09, and the operator's inject batches never
re-cut it — `PLAN_REASONS` had no reason to. Of nodes 0-17, twelve were the operator's (0-3, and the
batch 9-16 that carried the count past the reserve start) and six the engine's, so the engine's one
node after the operator's "main axis is the BACKBONE" directive was the reserve's ensemble, node 17,
on the budget's last slot. With the setting on, the reserve is `reserve_frac` of the ENGINE's share
of the budget — `max_nodes` less the nodes an operator inject created (`operator_injected`, off the
`source: "manual"` stamp `engine/node_build.py::_create_injected_node` writes) — and `replan` writes
an `injected` row when a batch moves the ordinary cut's start. It is the cut an operator who had
injected the same nodes before the run began would have got: timing-free, and never an endgame that
starts EARLIER than the cut without the batch. A stall row is not re-cut by a batch (a live episode
closes by its own terms and its `reopened` row cuts over the engine's share; the permanent
`stagnation` row of `endgame_stall_nodes` 0 stands). A run with no injected node, and every run with
the setting off, writes the historical rows byte for byte (no `injected` key).

Every function here is pure over folded state and the settings; the engine writes the row and
reads it back through the fold, so a resume honours the same plan.
"""
from __future__ import annotations

from typing import Optional

ENDGAME_KINDS = ("merge", "sweep")
PLAN_REASONS = ("initial", "budget_changed", "stagnation", "reopened", "injected")
# WHY a bounded stall episode closed — the `reopen_cause` of a `reopened` row. `stall_retracted` is
# the migration's own: a legacy (unbounded) stall row whose stall does not hold under the corrected
# `agents/strategist.py::stall_rung` (the attempts on the champion, not every id after it).
REOPEN_CAUSES = ("champion_changed", "episode_spent", "stall_retracted")
META_SWEEP = "_sweep"
HARD_STALL_RUNGS = 2


def build_plan(*, max_nodes: int, n_seeds: int, reserve_frac: float, at_node: int,
               reason: str = "initial", endgame_sweep: bool = True,
               endgame_start: Optional[int] = None, injected: int = 0) -> Optional[dict]:
    """The plan row, or None when no reserve is configured (a 0 fraction, or a budget too small to
    hold a seed phase AND at least one reserved slot).

    `injected` is how many of the run's nodes an operator inject created (`operator_injected`; 0
    with `Settings.endgame_inject_recut` off): the reserve is `reserve_frac` of the budget less
    those — the engine's own share — and the row carries the count it was cut over, omitted at 0 so
    a run with no injected node writes the historical row byte for byte."""
    try:
        max_nodes = int(max_nodes)
        n_seeds = max(0, int(n_seeds))
        frac = float(reserve_frac or 0.0)
        injected = max(0, int(injected or 0))
    except (TypeError, ValueError):
        return None
    if max_nodes <= 0 or frac <= 0.0:
        return None
    reserve = max(1, round(max(0, max_nodes - injected) * min(frac, 0.9)))
    if endgame_start is None:
        endgame_start = max_nodes - reserve
    endgame_start = max(min(n_seeds, max_nodes - 1), min(int(endgame_start), max_nodes - 1))
    if endgame_start <= n_seeds and max_nodes - n_seeds < 2:
        return None                       # no room for a search AND a reserve
    reserve = max_nodes - endgame_start
    kinds = list(ENDGAME_KINDS) if endgame_sweep else ["merge"]
    row = {
        "at_node": max(0, int(at_node)),
        "reason": reason if reason in PLAN_REASONS else "initial",
        "max_nodes": max_nodes,
        "reserve_frac": round(frac, 4),
        "endgame_start": endgame_start,
        "reserve": reserve,
        "phases": [
            {"name": "seed", "nodes": min(n_seeds, endgame_start)},
            {"name": "search", "nodes": max(0, endgame_start - n_seeds)},
            {"name": "endgame", "nodes": reserve, "reserve": True, "kinds": kinds},
        ],
        "source": "rule",
    }
    if injected:
        row["injected"] = injected
    return row


def operator_injected(events, nodes) -> int:
    """How many of the run's nodes an operator INJECT created: the ids whose `node_created` carries
    the `source: "manual"` stamp `engine/node_build.py::_create_injected_node` writes, keyed by the
    fold's own coercion (`core/models.py::coerce_node_id`), each counted once (a reset rebuild keeps
    the id the operator's) and only while the fold holds it — the same set `len(state.nodes)` counts
    toward the plan's `at_node`."""
    from looplab.core.models import coerce_node_id
    from looplab.events.types import EV_NODE_CREATED
    ids: set[int] = set()
    for event in events:
        data = event.data if event.type == EV_NODE_CREATED else None
        if isinstance(data, dict) and data.get("source") == "manual":
            node_id = coerce_node_id(data)
            if node_id is not None:
                ids.add(node_id)
    return sum(1 for node_id in ids if node_id in nodes)


# The `run_finished.reason` of the plateau stop (doc 70 70.4), a slug like `time_budget`, so the
# attention feed can name it (`serve/attention.py::_BUDGET_REASONS`).
PLATEAU_STOP_REASON = "plateau"


def plateau_leader(state) -> Optional[int]:
    """The node the SEARCH's own objective last improved on, or None: the first node, in id order,
    holding the best raw search metric among the settled nodes that may count toward the best
    (`core/fitness.py::counts_toward_best`). A later node that only TIES it did not improve on it.

    Not `RunState.best_node_id`. The champion is re-ranked by the confirm pass, the holdout, a
    simplification tie and an approval — and the first two are the end-of-search ladder the plateau
    stop hands the run to (`orchestrator.py::_plateau_stop_turn`), so a stop measured on the champion
    would reopen the very search it ended the moment confirmation crowned another node."""
    from looplab.core.fitness import counts_toward_best, is_usable_metric
    flagged = set(getattr(state, "breed_excluded", None) or ())
    aborted = set(getattr(state, "aborted_nodes", None) or ())
    leader = None
    for node in sorted(state.evaluated_nodes(), key=lambda n: n.id):
        if not (counts_toward_best(node, flagged, aborted) and is_usable_metric(node.metric)):
            continue
        if leader is None or state.is_better(node.metric, leader.metric):
            leader = node
    return None if leader is None else leader.id


def plateau_rearm_floor(events) -> int:
    """The first node id the plateau may count (doc 70 70.4): the next id after the last point the
    operator gave the run MORE SEARCH — a `budget_extend` whose `add_nodes` the fold grants
    (`events/replay_requests.py::accepted_add_nodes`), or a `resume`/`run_reopened` of a FINISHED
    run. 0 when neither happened. A pure function of the log, like the fold.

    Without it a run the stop finished could never be continued: reopened with twelve more nodes,
    it re-read the same K nodes after the same leader and finished again before building one."""
    from looplab.core.models import coerce_node_id
    from looplab.events.replay_requests import accepted_add_nodes
    from looplab.events.types import (EV_BUDGET_EXTEND, EV_NODE_CREATED, EV_RESUME,
                                      EV_RUN_FINISHED, EV_RUN_REOPENED)
    next_id, floor, finished = 0, 0, False
    for event in events:
        data = event.data if isinstance(event.data, dict) else {}
        if event.type == EV_NODE_CREATED:
            node_id = coerce_node_id(data)
            if node_id is not None:
                next_id = max(next_id, node_id + 1)
        elif event.type == EV_RUN_FINISHED:
            finished = True
        elif event.type in (EV_RESUME, EV_RUN_REOPENED):
            if finished:
                floor = next_id
            finished = False
        elif event.type == EV_BUDGET_EXTEND and accepted_add_nodes(data.get("add_nodes")):
            floor = next_id
    return floor


def plateau_nodes(state, *, floor: int = 0) -> tuple[Optional[int], int]:
    """`(leader_id, n)`: how many SETTLED nodes inside the plan's endgame window came after the search
    leader (`plateau_leader`) without taking its place — the nodes an operator's
    `Settings.plateau_stop_nodes` is counted in (doc 70 70.4). `(None, 0)` with no plan or no leader.

    The window is the CURRENT plan row's: `[endgame_start, endgame_end)` for a bounded stall episode,
    from `endgame_start` to the end of the budget otherwise — so a bounded episode the plan already
    `reopened` counts nothing (breadth resumed), while the ordinary reserve and the permanent stall
    endgame of `endgame_stall_nodes` 0 do. Only ids AFTER the leader and at or past `floor`
    (`plateau_rearm_floor`) count, so a new leader and an operator's extension each restart the
    count. A node still pending has not answered yet, and one that ended for a reason that says
    nothing about the experiment (`core/models.py::BENIGN_TERMINAL_REASONS`: superseded, a dropped
    Card, an operator abort) was not an attempt."""
    from looplab.core.models import NodeStatus
    plan = getattr(state, "plan", None)
    if not isinstance(plan, dict):
        return None, 0
    try:
        start = int(plan["endgame_start"])
        end = plan.get("endgame_end")
        end = None if end is None else int(end)
    except (KeyError, TypeError, ValueError):
        return None, 0
    leader = plateau_leader(state)
    if leader is None:
        return None, 0
    low = max(start, leader + 1, int(floor or 0))
    aborted = set(getattr(state, "aborted_nodes", None) or [])
    silent = _plateau_silent_reasons()
    count = 0
    for node in (getattr(state, "nodes", None) or {}).values():
        if node.id < low or (end is not None and node.id >= end):
            continue
        if node.status is NodeStatus.pending or node.tombstoned or node.id in aborted:
            continue
        if str(getattr(node, "error_reason", "") or "") in silent:
            continue
        if getattr(node, "kind", None) == "artifact":
            continue                  # doc 73 §1.4: a preparation step, not an attempt at the goal
        count += 1
    return leader, count


# What `plateau_nodes` does not count: the benign terminals, plus `engine_error` — said HERE, at
# the one reader that needs it, as `core/models.py::BENIGN_TERMINAL_REASONS` asks. An engine error is
# evidence about the BOX (a full disk, a read-only run dir; the run is paused beside it), so it is
# not BENIGN for the owner alert — but it is no attempt at the experiment either, and counting it let
# a box fault end the search as a plateau (incident 2026-10-06). Asked at every count, never frozen
# into a module constant at import: a constant computed once read whatever the vocabulary was when
# this module was first imported, so a reason added to it later (or patched in a test) was counted
# as an attempt here while every other reader of the vocabulary skipped it.
def _plateau_silent_reasons() -> frozenset:
    from looplab.core.models import BENIGN_TERMINAL_REASONS
    return BENIGN_TERMINAL_REASONS | {"engine_error"}


def plateau_stop_due(state, stop_nodes: int, *, floor: int = 0) -> Optional[str]:
    """Why the SEARCH should end on a plateau, or None (doc 70 70.4, `Settings.plateau_stop_nodes`).

    A hard stall moves the endgame earlier (`replan`) and never ends the run: the budget ran on to
    `max_nodes` without a new leader. With `stop_nodes` K > 0, K settled endgame nodes after the
    search leader (`plateau_nodes`, from `floor` on) end the search, and the run then ends the way a
    spent node budget ends it (`orchestrator.py::_plateau_stop_turn`). `stop_nodes <= 0` — the
    default everywhere — never stops."""
    if not isinstance(stop_nodes, int) or isinstance(stop_nodes, bool) or stop_nodes <= 0:
        return None
    leader, count = plateau_nodes(state, floor=floor)
    if leader is None or count < stop_nodes:
        return None
    return (f"plateau: {count} endgame node(s) after node {leader} produced no new leader "
            f"(plateau_stop_nodes={stop_nodes})")


def in_endgame(plan: Optional[dict], total_nodes: int) -> bool:
    """Inside the reserve: `endgame_start <= n`, and `n < endgame_end` when the row bounds its
    episode. A row without `endgame_end` — every row before bounded episodes, and every non-stall
    row — keeps the historical open-ended reading."""
    if not isinstance(plan, dict):
        return False
    try:
        n = int(total_nodes)
        if n < int(plan["endgame_start"]):
            return False
        end = plan.get("endgame_end")
        return end is None or n < int(end)
    except (KeyError, TypeError, ValueError):
        return False


def final_reserve_reached(plan: Optional[dict], total_nodes: int) -> Optional[bool]:
    """Whether node `total_nodes` is inside the plan's FINAL reserve — the endgame the budget's end
    closes, which no re-cut of the same budget reopens — or None when there is no readable plan row.

    The rule Strategist's endgame switch (doc 69 69.25a, `agents/strategist.py::endgame_reached`):
    `in_endgame` over the SAME count the dispatcher's gate reads, so on an ordinary row the machinery
    the rule sets and the actions the gate admits start at one node, wherever an inject batch or a
    budget change has moved the cut. A permanent stall row (`endgame_stall_nodes` 0, no
    `endgame_end`) is final too: it runs to the budget's end, so a resumed run whose log holds one
    switches at its start, as its gate did.

    A BOUNDED stall episode (`endgame_end`) is final only from the start its `reopened` row would
    cut on (`_ordinary_start`: the ordinary cut of the row's own budget, seeds, fraction and injected
    count — the one `_reopened` takes): before it, the episode reopens into the search — after its K
    nodes or on a new champion — and the endgame settings the rule would write (no ablation, the
    ensemble merge) would outlive it; from it on, every reopening lands inside the ordinary reserve.
    Counting the whole episode out (critic crit_v59 F1, driven) switched late by the overlap on an
    episode that straddles the cut, and never on one whose end is past the budget's, which no
    `reopened` row closes. Read off the row rather than stored beside it, so an episode row written
    before this reads the same way (crit_v61 L2). None — a missing or unreadable row — leaves the
    caller's own reading in place."""
    if not isinstance(plan, dict):
        return None
    try:
        int(plan["endgame_start"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if plan.get("endgame_end") is None:
        return in_endgame(plan, total_nodes)
    final = _ordinary_start(plan)
    if final is None:
        return None
    try:
        return in_endgame(plan, total_nodes) and int(total_nodes) >= final
    except (TypeError, ValueError, OverflowError):
        return None


def _ordinary_start(plan: dict) -> Optional[int]:
    """Where the ORDINARY cut of `plan`'s own budget, seed count, fraction and injected count starts
    its reserve — the start a `reopened` row cut from this row takes (`_reopened` cuts with the row's
    own fraction) — or None when the row cannot say. The seed count is the seed phase's, which an
    episode row's start (always past the seeds) leaves whole."""
    phases = plan.get("phases")
    try:
        seeds = phases[0]["nodes"]
        frac = plan["reserve_frac"]
        # A fraction `_own_fraction` would not take is not the one the reopen cuts with.
        if (type(seeds) is not int or not isinstance(frac, (int, float))
                or isinstance(frac, bool)):
            return None
        row = build_plan(max_nodes=int(plan["max_nodes"]), n_seeds=seeds,
                         reserve_frac=float(frac), at_node=0, injected=_recorded_injected(plan))
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return None
    return None if row is None else row["endgame_start"]


def _int_or_none(value) -> Optional[int]:
    return value if type(value) is int else None


def _stall_champions(plan: dict) -> list[int]:
    """The champions whose one stall episode is spent, as the row carries them (defensive: a junk
    entry is dropped, never guessed)."""
    raw = plan.get("stall_champions")
    out: list[int] = []
    for value in (raw if isinstance(raw, list) else []):
        if type(value) is int and value not in out:
            out.append(value)
    return out


def _episode(plan: dict) -> Optional[tuple[int, int, Optional[int]]]:
    """`(start, end, champion)` when the row bounds a stall episode, else None."""
    end = plan.get("endgame_end")
    if end is None:
        return None
    try:
        return int(plan["endgame_start"]), int(end), _int_or_none(plan.get("champion"))
    except (KeyError, TypeError, ValueError):
        return None


def _with_spent(row: Optional[dict], spent: list[int]) -> Optional[dict]:
    """Carry the spent-champion memory onto a re-cut row; a row before any episode stays the shape it
    always was (no key), so a run that never stalls writes byte-identical plan rows."""
    if row is not None and spent:
        row["stall_champions"] = list(spent)
    return row


def _episode_row(cut: dict, *, start: int, end: int, champion: Optional[int], spent: list[int],
                 reason: str) -> Optional[dict]:
    """A plan row that bounds a stall episode to `[start, end)`. Its `reserve` (and the endgame
    phase's `nodes`) is the episode's length, not the rest of the budget: the row describes what the
    reserve will spend before the `reopened` row cuts the ordinary plan again."""
    row = build_plan(**cut, reason=reason, endgame_start=start)
    if row is None:
        return None
    reserve = max(1, min(int(end), row["max_nodes"]) - row["endgame_start"])
    row["reserve"] = reserve
    row["phases"][-1]["nodes"] = reserve
    row["endgame_end"] = int(end)
    row["champion"] = champion
    row["stall_champions"] = list(spent)
    return row


def _injected_recut(plan: dict, cut: dict, planned_start: int,
                    spent: list[int]) -> Optional[dict]:
    """An `injected` row when the operator's nodes moved the ordinary cut's start, else None (the
    module docstring has the account). The count the row was cut over is its `injected` (absent =
    0); a `stagnation` row — the permanent stall endgame of `stall_nodes` 0, the only stall row that
    reaches here — is not re-cut by a batch."""
    if plan.get("reason") == "stagnation":
        return None
    recorded = _recorded_injected(plan)
    if cut["injected"] == recorded:
        return None
    # A RESERVE THE RUN HAD ALREADY ENTERED keeps its start (critic 2026-09-30, crit_v48 F2): the
    # batch's nodes are the operator's, and the engine's own count before them had reached the start,
    # so moving it re-opened a reserve the run was spending — and owed its once-only ensemble again
    # (driven: a second top-2 merge over the operator's two injects). Measured in ENGINE nodes: the
    # count before the batch (`at_node` less the batch's size) against the row's start.
    if cut["at_node"] - max(0, cut["injected"] - recorded) >= planned_start:
        return None
    # …and cut with the row's OWN fraction (F6): the historical rule never re-cuts on a changed
    # `endgame_reserve_frac`, and a batch must not smuggle one in — driven, a fraction raised from
    # 0.25 to 0.5 moved the start from 15 to 10 on one inject, earlier than without the batch.
    row = build_plan(**_own_fraction(plan, cut), reason="injected")
    if row is None or row["endgame_start"] == planned_start:
        return None
    return _with_spent(row, spent)


def _own_fraction(plan: dict, cut: dict) -> dict:
    """`cut` with the row's OWN `reserve_frac` when it records a usable one. Only a row cut FRESH from
    the configuration takes the live fraction — an ordinary budget re-cut (the historical rule) and
    the unbounded stall row of `stall_nodes` 0; an episode's rows — its start, the migration of that
    unbounded row, a budget carry, an inject extension — and its `reopened` row keep the fraction
    the row was cut with, so the start `final_reserve_reached` reads off an episode row is the one
    its reopen cuts (critic crit_v62 F3, driven: a carry and an extension took a fraction lowered on
    resume and unread nodes the rule had read as final). One corner stays (critic crit_v63 N1): a
    stall while `stall_nodes` was 0 records the fraction live THEN, so a fraction moved on resume
    before it moves the reopen too — later when lowered, earlier when raised (critic crit_v64) —
    against the same history with the setting on at the stall. A stored fraction is rounded to 4
    places; a fraction the row rounded to 0 is no usable one, and the live cut stands."""
    frac = plan.get("reserve_frac")
    if isinstance(frac, (int, float)) and not isinstance(frac, bool) and frac > 0:
        return {**cut, "reserve_frac": frac}
    return cut


def _recorded_injected(plan: dict) -> int:
    """The operator-injected count a plan row was cut over (absent = 0)."""
    recorded = plan.get("injected")
    return recorded if type(recorded) is int and recorded > 0 else 0


def _reopened(plan: dict, cut: dict, spent: list[int], cause: str) -> Optional[dict]:
    # The row's OWN fraction, as `_injected_recut` takes (critic crit_v61 L1, driven: a fraction
    # lowered on resume mid-episode reopened into the search at nodes the rule had already read as
    # its final reserve — `final_reserve_reached` reads the start this cut gives).
    row = build_plan(**_own_fraction(plan, cut), reason="reopened")
    if row is None:
        return None
    row["reopen_cause"] = cause if cause in REOPEN_CAUSES else "episode_spent"
    return _with_spent(row, spent)


def replan(plan: Optional[dict], *, max_nodes: int, n_seeds: int, reserve_frac: float,
           at_node: int, stall_rung: int, endgame_sweep: bool = True, stall_nodes: int = 0,
           champion: Optional[int] = None, injected: int = 0) -> Optional[dict]:
    """A re-cut plan when one is due, else None.

    `stall_nodes` 0 (the bare-library and legacy-snapshot value) is the historical rule byte for
    byte: two triggers, in this order — the live node budget moved (the reserve follows it), and a
    hard stall (`stall_rung >= HARD_STALL_RUNGS`) before the endgame has begun, which starts the
    endgame NOW and for good. A run already inside its endgame never re-plans on a stall (there is
    nothing earlier to start).

    `stall_nodes` K > 0 bounds the stall's endgame to K nodes (the module docstring has the account),
    in this order:

    1. a row that bounds an episode is closed by its own terms — `reopened` once `at_node` reaches its
       `endgame_end` or `champion` is not the one it was measured against — whatever K now is, so a
       run whose operator turned the setting off mid-episode still leaves it; a budget change inside
       a live episode CARRIES it (the flicker must not drop the stall start);
    2. a LEGACY stall row (no `endgame_end`) is re-evaluated once: reopened when the corrected rung is
       under two or the champion was crowned inside it, else bounded from its own start;
    3. the budget re-cut, carrying the spent-champion memory;
    4. a hard stall before the reserve starts ONE episode `[at_node, at_node + K)` for a champion
       that has not had one.

    `injected` (doc 69 69.25; 0 = the rules above byte for byte) is the operator-injected node
    count every cut here is taken over, and — last, in either branch — an ordinary row whose
    recorded count differs is re-cut as `injected` when that moves its start (`_injected_recut`).
    """
    if not isinstance(plan, dict):
        return None
    try:
        planned_budget = int(plan.get("max_nodes"))
        planned_start = int(plan.get("endgame_start"))
    except (TypeError, ValueError):
        return None
    try:
        stall_nodes = max(0, int(stall_nodes or 0))
    except (TypeError, ValueError):
        stall_nodes = 0
    champion = _int_or_none(champion)
    try:
        injected = max(0, int(injected or 0))
    except (TypeError, ValueError):
        injected = 0
    cut = {"max_nodes": max_nodes, "n_seeds": n_seeds, "reserve_frac": reserve_frac,
           "at_node": at_node, "endgame_sweep": endgame_sweep, "injected": injected}
    spent = _stall_champions(plan)
    own = _own_fraction(plan, cut)         # every episode row keeps the row's fraction (crit_v62 F3)
    episode = _episode(plan)
    if episode is not None:
        start, end, holder = episode
        if champion != holder:
            return _reopened(plan, cut, spent, "champion_changed")
        # A BATCH LANDING INSIDE A LIVE EPISODE extends it by the batch (critic 2026-09-30, crit_v48
        # F3): the episode is K of the ENGINE's nodes, and the operator's ids used to spend it —
        # driven, three injects at node 9 of an episode [8, 11) reopened it after one engine node,
        # and the champion's one episode was gone. `injected` is 0 with the setting off.
        batch = injected - _recorded_injected(plan)
        if batch > 0 and at_node - batch < end:
            return _episode_row(own, start=start, end=end + batch, champion=holder, spent=spent,
                                reason="injected")
        if at_node >= end:
            return _reopened(plan, cut, spent, "episode_spent")
        if int(max_nodes) != planned_budget:
            return _episode_row(own, start=start, end=end, champion=holder, spent=spent,
                                reason="budget_changed")
        return None
    if stall_nodes <= 0:
        if int(max_nodes) != planned_budget:
            return build_plan(max_nodes=max_nodes, n_seeds=n_seeds, reserve_frac=reserve_frac,
                              at_node=at_node, reason="budget_changed", endgame_sweep=endgame_sweep,
                              injected=injected)
        if stall_rung >= HARD_STALL_RUNGS and at_node < planned_start and at_node > n_seeds:
            return build_plan(max_nodes=max_nodes, n_seeds=n_seeds, reserve_frac=reserve_frac,
                              at_node=at_node, reason="stagnation", endgame_sweep=endgame_sweep,
                              endgame_start=at_node, injected=injected)
        return _injected_recut(plan, cut, planned_start, [])
    if plan.get("reason") == "stagnation":
        # THE MIGRATION: an unbounded stall row this setting did not write (inf13's, at node 12).
        # Nothing on it names the champion the stall was measured against, so the stall is measured
        # again, now, on the corrected count — and a champion whose id is at or past the row's start
        # did not exist when it was written, so the stall it recorded was some other champion's.
        if stall_rung < HARD_STALL_RUNGS or champion is None:
            return _reopened(plan, cut, spent, "stall_retracted")
        if champion >= planned_start:
            return _reopened(plan, cut, spent, "champion_changed")
        if champion in spent:
            return _reopened(plan, cut, spent, "episode_spent")
        end = planned_start + stall_nodes
        if at_node >= end:
            return _reopened(plan, cut, spent + [champion], "episode_spent")
        return _episode_row(own, start=planned_start, end=end, champion=champion,
                            spent=spent + [champion], reason="stagnation")
    if int(max_nodes) != planned_budget:
        return _with_spent(build_plan(**cut, reason="budget_changed"), spent)
    if (stall_rung >= HARD_STALL_RUNGS and n_seeds < at_node < planned_start
            and champion is not None and champion not in spent):
        return _episode_row(own, start=at_node, end=at_node + stall_nodes, champion=champion,
                            spent=spent + [champion], reason="stagnation")
    return _injected_recut(plan, cut, planned_start, spent)


# ----------------------------------------------------------------------------- the reserve's rule

def _parent_pair(value) -> Optional[tuple[int, int]]:
    """The two distinct integer parents of a merge, or None for any other shape."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    a, b = value
    if type(a) is not int or type(b) is not int or a == b:
        return None
    return a, b


def _action_identity(action: dict) -> tuple:
    """What makes two actions the same WORK for the reserve: the kind, the parents (a merge is
    symmetric in them) and whether it is the surrogate's sweep rather than the Researcher's refine."""
    kind = action.get("kind")
    if kind == "merge":
        pair = _parent_pair(action.get("parent_ids"))
        parents: tuple = tuple(sorted(pair)) if pair is not None else ("?",)
    elif kind in ("improve", "ablate"):
        parents = (action.get("parent_id"),)
    else:
        parents = ()
    return kind, parents, bool(action.get(META_SWEEP))


# The Card statuses that still own their work: not terminal, not dropped (`card_ledger.py`'s frozen
# lifecycle lane). A `building` Card may not have its node folded yet, which is exactly why a live
# merge CARD refuses its pair beside the merge NODES.
_LIVE_CARD_STATUSES = frozenset({"proposed", "building", "coded", "running"})


class _EndgameView:
    """What the reserve reads off ONE fold, computed once however many questions are asked of it —
    `endgame_refused_card_ids` asks one per Card on the board."""

    def __init__(self, state, plan: dict):
        from looplab.search.policy import rank_by_metric
        self.state = state
        self.plan = plan
        self.best = state.best()
        self.best_id = self.best.id if self.best is not None else None
        self.breedable = rank_by_metric(state, state.breedable_nodes())
        self.breedable_ids = {n.id for n in self.breedable}
        self._ancestors: dict[int, frozenset[int]] = {}
        self._own: dict[bool, Optional[dict]] = {}
        # The last phase's kinds, total over whatever `phases` the fold kept: a last phase that is
        # not a dict raised where a Card kept its slot (critic 2026-09-27, NIT).
        phases = plan.get("phases")
        last = phases[-1] if isinstance(phases, list) and phases else {}
        kinds = last.get("kinds") if isinstance(last, dict) else None
        self.kinds = list(kinds) if isinstance(kinds, (list, tuple)) and kinds else list(ENDGAME_KINDS)
        # Every pair a merge NODE already holds, any status, before or after the reserve start. A
        # merge of more than two parents holds every pair of them — conservative, and no engine
        # path writes one.
        self.merged_pairs: set[frozenset] = set()
        for node in state.nodes.values():
            if node.operator != "merge":
                continue
            parents = sorted({p for p in node.parent_ids if type(p) is int})
            for i, a in enumerate(parents):
                for b in parents[i + 1:]:
                    self.merged_pairs.add(frozenset((a, b)))
        # Every live merge CARD, by pair, oldest first (`cards_added` order; an unlisted id sorts last).
        order = {row.get("id"): i for i, row in enumerate(getattr(state, "cards_added", None) or [])
                 if isinstance(row, dict)}
        live: dict[frozenset, list[str]] = {}
        for card in state.cards.values():
            if (card.operator != "merge" or card.merged_into is not None
                    or card.dropped_reason is not None or card.status not in _LIVE_CARD_STATUSES):
                continue
            pair = _parent_pair(list(card.parent_ids or []))
            if pair is not None:
                live.setdefault(frozenset(pair), []).append(card.id)
        self.live_merge_cards = {
            pair: sorted(ids, key=lambda cid: (order.get(cid, len(order)), str(cid)))
            for pair, ids in live.items()}

    def ancestors(self, node_id: int) -> frozenset[int]:
        """Every node reachable over `parent_ids` from `node_id`, transitively (cycle-safe)."""
        cached = self._ancestors.get(node_id)
        if cached is not None:
            return cached
        seen: set[int] = set()
        stack = [node_id]
        while stack:
            node = self.state.nodes.get(stack.pop())
            if node is None:
                continue
            for parent in node.parent_ids:
                if type(parent) is int and parent not in seen:
                    seen.add(parent)
                    stack.append(parent)
        seen.discard(node_id)
        result = frozenset(seen)
        self._ancestors[node_id] = result
        return result

    def related(self, a: int, b: int) -> bool:
        """One of the two descends from the other — an ensemble of an idea and its own refinement."""
        return a in self.ancestors(b) or b in self.ancestors(a)

    def merge_admissible(self, pair: Optional[tuple[int, int]], *,
                         card_id: Optional[str] = None) -> bool:
        """May the reserve spend a slot merging `pair`? `card_id` names the Card asking (its own
        live row is not a rival); None is the reserve's OWN merge, which any live Card of the pair
        refuses."""
        if pair is None:
            return False
        a, b = pair
        if a not in self.breedable_ids or b not in self.breedable_ids:
            return False                  # "of two evaluated nodes": both breedable
        if self.related(a, b):
            return False
        key = frozenset(pair)
        if key in self.merged_pairs:
            return False
        owners = self.live_merge_cards.get(key, [])
        if card_id is None:
            return not owners
        # Two live Cards of one pair would refuse each other forever; the OLDEST keeps the pair, and
        # a Card that is not itself a live owner (it could not be elected anyway) has a rival.
        return not owners or owners[0] == card_id

    def merge_pick(self) -> Optional[tuple]:
        """`(leader, partner, historical, on_front)`: the leader and the first partner that passes
        `merge_admissible`, walked in the historical order — the Pareto front's members after the
        leader when the front has two or more, then the rest of the breedable ranking. `historical`
        says the partner is the one the reserve always took (the reason string keeps its bytes);
        `on_front` that the historical pair was the front's."""
        from looplab.search.policy import pareto_front
        if len(self.breedable) < 2:
            return None
        front = pareto_front(self.state, self.breedable)
        if len(front) >= 2:
            leader = front[0]
            order = list(front[1:]) + [n for n in self.breedable if n not in front]
        else:
            leader = self.breedable[0]
            order = list(self.breedable[1:])
        for rank, partner in enumerate(order):
            if partner.id == leader.id:
                continue
            if self.merge_admissible((leader.id, partner.id)):
                return leader, partner, rank == 0, len(front) >= 2
        return None

    def simplify_passes(self) -> bool:
        """doc 67 67.5: may a simplification of the champion take this turn as it is? Not while the
        reserve's once-only ensemble is still owed (its own next action is a merge — the lineage and
        pair rules decide whether a partner qualifies, as they do for the ensemble itself), and ONE
        cut per champion in the reserve."""
        start = int(self.plan["endgame_start"])
        own = self.own(sweep=False)
        ensemble_owed = own is not None and own.get("kind") == "merge"
        cut_in_reserve = self.best is not None and any(
            n.id >= start and isinstance(n.simplified, dict)
            and n.simplified.get("parent_id") == self.best.id for n in self.state.nodes.values())
        return not ensemble_owed and not cut_in_reserve

    def card_is_endgame_action(self, action: dict) -> bool:
        """A Card-owned action that already IS the reserve's kind of work: an improve of the
        champion, or a merge `merge_admissible` passes for this Card."""
        from looplab.search.card_selection import META_CARD_ID
        kind = action.get("kind")
        if kind == "improve":
            return self.best_id is not None and action.get("parent_id") == self.best_id
        if kind == "merge":
            card_id = action.get(META_CARD_ID)
            return self.merge_admissible(_parent_pair(action.get("parent_ids")),
                                         card_id=card_id if isinstance(card_id, str) else "")
        return False

    def own(self, *, sweep: bool) -> Optional[dict]:
        """The reserve's own next action — the ensemble while it is owed and a partner qualifies,
        else the champion's sweep (or refine) — or None when there is no champion to spend it on."""
        key = bool(sweep)
        if key in self._own:
            return self._own[key]
        from looplab.search.policy import KIND_IMPROVE, KIND_MERGE, META_CHOSEN, META_REASON
        action: Optional[dict] = None
        start = int(self.plan["endgame_start"])
        merged_in_reserve = any(n.operator == "merge" and n.id >= start
                                for n in self.state.nodes.values())
        kinds = self.kinds
        pick = (self.merge_pick() if "merge" in kinds and not merged_in_reserve else None)
        if pick is not None:
            # THE ONE PLACE SELECTION READS THE NON-DOMINATED FRONT (docs/BACKLOG.md §0.1 row 12).
            # The ensemble's two parents come from `pareto_front` rather than straight off the scalar
            # ranking: the top-2 by metric are frequently the same idea twice — an improve and its
            # own parent, separated by noise — and an ensemble of two near-identical models buys the
            # run nothing it did not already have. The front's second member is the best node NOT
            # dominated by the leader, i.e. one that pays for its lower metric with a declared
            # objective the leader loses on, which is the recombination an endgame reserve exists to
            # spend its slots on.
            #
            # INERT UNTIL A RUN RECORDS A REAL SECOND OBJECTIVE, by construction and not by a flag:
            # with no authenticated, orientable extra metric the only axis is the primary metric,
            # the front is the metric leader alone, `len(front) < 2`, and this falls through to the
            # byte-identical top-2 ranking it always used. That is why there is no new setting here
            # — a knob would imply the front is a policy choice, and it is a reading of what the
            # record supports. The LINEAGE and PAIR rules (`merge_admissible`) walk past a partner
            # that is the leader's own ancestor or descendant or was already paired with it; the
            # reason keeps its historical bytes whenever the historical partner qualified.
            leader, partner, historical, on_front = pick
            if historical:
                reason = ("endgame: ensemble of the Pareto front's top-2" if on_front
                          else "endgame: ensemble of the top-2")
            else:
                reason = ("endgame: ensemble of the leader and its best partner outside its "
                          "lineage that no merge has paired it with")
            action = {"kind": KIND_MERGE, "parent_ids": [leader.id, partner.id],
                      META_CHOSEN: leader.id, META_REASON: reason}
        elif self.best is not None:
            if sweep and "sweep" in kinds:
                action = {"kind": KIND_IMPROVE, "parent_id": self.best.id, META_SWEEP: True,
                          META_CHOSEN: self.best.id,
                          META_REASON: "endgame: champion sweep (k-NN surrogate)"}
            else:
                action = {"kind": KIND_IMPROVE, "parent_id": self.best.id,
                          META_CHOSEN: self.best.id, META_REASON: "endgame: refine the champion"}
        self._own[key] = action
        return action

    def admits(self, action: dict, *, sweep: bool) -> bool:
        """`endgame_admits` over this view (the caller has already asked `in_endgame`)."""
        from looplab.search.card_selection import META_CARD_ID
        if action.get("kind") == "evaluate":
            return True
        if action.get("kind") == "simplify":
            return self.simplify_passes()
        if META_CARD_ID in action and self.card_is_endgame_action(action):
            return True
        own = self.own(sweep=sweep)
        if own is None:
            return True                   # nothing of its own to spend: the gate hands the turn back
        return META_CARD_ID not in action and _action_identity(action) == _action_identity(own)


def endgame_admits(state, plan: Optional[dict], action: dict, *, sweep: bool = True) -> bool:
    """Would the endgame gate let `action` through AS IT IS? The ONE predicate the gate, the raw
    proposal lanes and every Card election share (see the module docstring).

    Outside the reserve: yes. An evaluation: yes. A Card's action: when it already is an endgame
    action (an improve of the champion, a merge `merge_admissible` passes). A raw action: only when
    it is the reserve's own next action (`endgame_actions` replaces every other create with that one)
    — which is why a raw lane that re-derives the POLICY's action must ask before it pays. When the
    reserve has nothing of its own (no champion, no admissible merge) every action passes, as the
    gate passes the turn through."""
    if not isinstance(action, dict) or not in_endgame(plan, len(state.nodes)):
        return True
    return _EndgameView(state, plan).admits(action, sweep=sweep)


def endgame_admitted(state, actions: list[dict], *, sweep: bool = True) -> list[dict]:
    """The members of a raw lane the gate would let through as they are, asked BEFORE the lane
    stages a Card for one — a paid Researcher proposal (inf13's card-14: an improve of a
    non-champion, proposed, paid for, then displaced by the gate's own merge). Outside the reserve
    the lane is returned whole, so every raw lane is byte-identical there."""
    plan = getattr(state, "plan", None)
    if not in_endgame(plan, len(state.nodes)):
        return list(actions)
    view = _EndgameView(state, plan)
    return [action for action in actions
            if not isinstance(action, dict) or view.admits(action, sweep=sweep)]


def endgame_refused_card_ids(state, plan: Optional[dict]) -> frozenset[str]:
    """The LIVE Cards on the board whose action the gate would refuse right now — what every election
    excludes BEFORE ranking, so no build is bought that the gate then displaces. Only a Card that
    still owns its work can be elected or claimed, so a terminal, dropped or merged-away one is never
    named. Empty outside the reserve, so every lane is byte-identical there. The `sweep` switch
    cannot move a Card's answer (it only shapes the reserve's own raw action), so none is asked."""
    if not in_endgame(plan, len(state.nodes)):
        return frozenset()
    from looplab.search.card_selection import card_action
    view = _EndgameView(state, plan)
    refused: set[str] = set()
    for card in state.cards.values():
        if (card.status not in _LIVE_CARD_STATUSES or card.merged_into is not None
                or card.dropped_reason is not None):
            continue
        action = card_action(card)
        if action is not None and not view.admits(action, sweep=True):
            refused.add(card.id)
    return frozenset(refused)


def endgame_actions(state, plan: Optional[dict], actions: list[dict], *,
                    sweep: bool = True) -> list[dict]:
    """The dispatcher's rule inside the reserve (see the module docstring). Outside the reserve, or
    when the turn's actions are evaluations / the finish, the actions are returned untouched."""
    if not in_endgame(plan, len(state.nodes)) or not actions:
        return actions
    if any(a.get("kind") == "evaluate" for a in actions):
        return actions
    from looplab.search.card_selection import META_CARD_ID
    view = _EndgameView(state, plan)
    # A SIMPLIFICATION of the champion passes too (doc 67 67.5): it proposes nothing and pays no
    # model — the champion's own program, one measured block commented out — so it is the reserve's
    # purpose, polishing the champion; replaced by the sequence below, it vanished with no receipt.
    # But not ahead of the once-only ensemble, and ONE per champion in the reserve: a cut that
    # measured worse leaves the champion where it was, and the next nominated block of the same
    # program spent the next slot on the same question — five nominated blocks took a reserve of
    # three and the ensemble and the sweeps never ran (critic 2026-09-27, driven). A cut that WON is
    # the new champion, and may be simplified once in turn. `simplify_passes` is the rule, asked
    # by `admits` too, so the gate and the lanes read it the same way.
    if any(a.get("kind") == "simplify" for a in actions):
        if view.simplify_passes():
            return actions
        actions = [a for a in actions if a.get("kind") != "simplify"]
    own = view.own(sweep=sweep)
    if own is None:
        # Nothing of the reserve's own to spend (no champion, no admissible merge): a Card that is
        # an endgame action still keeps its slot, and otherwise the turn passes through untouched.
        kept = [a for a in actions if META_CARD_ID in a and view.card_is_endgame_action(a)]
        return kept or actions
    # A selected CARD that already is an endgame action keeps its slot (its proposal is paid for);
    # a plain policy create — an improve of the champion included — is replaced by the endgame's
    # own sequence, the ensemble first and then the surrogate-proposed sweeps. Asked through
    # `admits`, the predicate every buying lane shares, so the gate and the lanes cannot disagree:
    # with an action of its own, `admits` keeps exactly the Cards that are endgame actions.
    kept = [a for a in actions if META_CARD_ID in a and view.admits(a, sweep=sweep)]
    return kept or [own]
