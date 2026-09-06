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
  not been minted in the reserve yet and two breedable nodes exist, else an `improve` of the
  champion stamped `_sweep`, whose idea `engine/orchestrator.py::_prepare_node_idea` asks the
  k-NN surrogate for (`search/surrogate.py`, bounds inferred from the run's own evaluated
  params; the LLM Researcher is its fallback below warm-up). A selected Card that already IS an
  endgame action (a merge of two evaluated nodes, an improve of the champion) keeps its slot.
* `replan` re-cuts on a live `max_nodes` change (the reserve follows the budget) and on a HARD
  stall — `stall_rung` at two windows, the same identity the Strategist's plateau consult keys on
  — by starting the endgame at the current node count: a search that has stalled for two windows
  spends what is left on recombination and refinement rather than more breadth.

Every function here is pure over folded state and the settings; the engine writes the row and
reads it back through the fold, so a resume honours the same plan.
"""
from __future__ import annotations

from typing import Optional

ENDGAME_KINDS = ("merge", "sweep")
PLAN_REASONS = ("initial", "budget_changed", "stagnation")
META_SWEEP = "_sweep"
HARD_STALL_RUNGS = 2


def build_plan(*, max_nodes: int, n_seeds: int, reserve_frac: float, at_node: int,
               reason: str = "initial", endgame_sweep: bool = True,
               endgame_start: Optional[int] = None) -> Optional[dict]:
    """The plan row, or None when no reserve is configured (a 0 fraction, or a budget too small to
    hold a seed phase AND at least one reserved slot)."""
    try:
        max_nodes = int(max_nodes)
        n_seeds = max(0, int(n_seeds))
        frac = float(reserve_frac or 0.0)
    except (TypeError, ValueError):
        return None
    if max_nodes <= 0 or frac <= 0.0:
        return None
    reserve = max(1, round(max_nodes * min(frac, 0.9)))
    if endgame_start is None:
        endgame_start = max_nodes - reserve
    endgame_start = max(min(n_seeds, max_nodes - 1), min(int(endgame_start), max_nodes - 1))
    if endgame_start <= n_seeds and max_nodes - n_seeds < 2:
        return None                       # no room for a search AND a reserve
    reserve = max_nodes - endgame_start
    kinds = list(ENDGAME_KINDS) if endgame_sweep else ["merge"]
    return {
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


def in_endgame(plan: Optional[dict], total_nodes: int) -> bool:
    if not isinstance(plan, dict):
        return False
    try:
        return int(total_nodes) >= int(plan["endgame_start"])
    except (KeyError, TypeError, ValueError):
        return False


def replan(plan: Optional[dict], *, max_nodes: int, n_seeds: int, reserve_frac: float,
           at_node: int, stall_rung: int, endgame_sweep: bool = True) -> Optional[dict]:
    """A re-cut plan when one is due, else None. Two triggers, in this order: the live node budget
    moved (the reserve follows it), and a hard stall (`stall_rung >= HARD_STALL_RUNGS`) before the
    endgame has begun — then the endgame starts NOW. A run already inside its endgame never
    re-plans on a stall (there is nothing earlier to start)."""
    if not isinstance(plan, dict):
        return None
    try:
        planned_budget = int(plan.get("max_nodes"))
        planned_start = int(plan.get("endgame_start"))
    except (TypeError, ValueError):
        return None
    if int(max_nodes) != planned_budget:
        return build_plan(max_nodes=max_nodes, n_seeds=n_seeds, reserve_frac=reserve_frac,
                          at_node=at_node, reason="budget_changed", endgame_sweep=endgame_sweep)
    if stall_rung >= HARD_STALL_RUNGS and at_node < planned_start and at_node > n_seeds:
        return build_plan(max_nodes=max_nodes, n_seeds=n_seeds, reserve_frac=reserve_frac,
                          at_node=at_node, reason="stagnation", endgame_sweep=endgame_sweep,
                          endgame_start=at_node)
    return None


def _is_endgame_action(action: dict, best_id: Optional[int]) -> bool:
    kind = action.get("kind")
    if kind == "merge":
        return True
    if kind == "improve" and best_id is not None and action.get("parent_id") == best_id:
        return True
    return False


def endgame_actions(state, plan: Optional[dict], actions: list[dict], *,
                    sweep: bool = True) -> list[dict]:
    """The dispatcher's rule inside the reserve (see the module docstring). Outside the reserve, or
    when the turn's actions are evaluations / the finish, the actions are returned untouched."""
    if not in_endgame(plan, len(state.nodes)) or not actions:
        return actions
    if any(a.get("kind") == "evaluate" for a in actions):
        return actions
    best = state.best()
    best_id = best.id if best is not None else None
    from looplab.search.card_selection import META_CARD_ID
    from looplab.search.policy import (KIND_IMPROVE, KIND_MERGE, META_CHOSEN, META_REASON,
                                       rank_by_metric)
    # A selected CARD that already is an endgame action keeps its slot (its proposal is paid for);
    # a plain policy create — an improve of the champion included — is replaced by the endgame's
    # own sequence below, the ensemble first and then the surrogate-proposed sweeps.
    kept = [a for a in actions if META_CARD_ID in a and _is_endgame_action(a, best_id)]
    if kept:
        return kept
    start = int(plan["endgame_start"])
    breedable = rank_by_metric(state, state.breedable_nodes())
    merged_in_reserve = any(n.operator == "merge" and n.id >= start for n in state.nodes.values())
    kinds = (plan.get("phases") or [{}])[-1].get("kinds") or list(ENDGAME_KINDS)
    if "merge" in kinds and not merged_in_reserve and len(breedable) >= 2:
        return [{"kind": KIND_MERGE, "parent_ids": [breedable[0].id, breedable[1].id],
                 META_CHOSEN: breedable[0].id, META_REASON: "endgame: ensemble of the top-2"}]
    if best is None:
        return actions
    if sweep and "sweep" in kinds:
        return [{"kind": KIND_IMPROVE, "parent_id": best.id, META_SWEEP: True,
                 META_CHOSEN: best.id, META_REASON: "endgame: champion sweep (k-NN surrogate)"}]
    return [{"kind": KIND_IMPROVE, "parent_id": best.id, META_CHOSEN: best.id,
             META_REASON: "endgame: refine the champion"}]
