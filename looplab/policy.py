"""SearchPolicy (I6/I7/I11, ADR-18). `GreedyTree`: seed K drafts, then repeatedly
improve the current best, periodically merging the top-2 (multi-parent DAG step),
and debugging failed leaves up to a depth bound — until the node budget is spent.

The policy is *pure*: it reads a RunState and returns the next actions; the
orchestrator executes them. This is our moat (the loop), not a framework graph.

Action kinds:
    {"kind": "draft"}
    {"kind": "improve", "parent_id": int}
    {"kind": "debug",   "parent_id": int}
    {"kind": "merge",   "parent_ids": [int, int]}
    {"kind": "evaluate","node_id": int}
"""
from __future__ import annotations

import math
from typing import Optional, Protocol

from .models import NodeStatus, RunState


class SearchPolicy(Protocol):
    def next_actions(self, state: RunState) -> list[dict]: ...


# --------------------------------------------------------------------------- #
# Shared self-repair: debug the first failed leaf within the depth bound. Used by
# every policy so error-feedback repair (I7) is policy-agnostic, not greedy-only.
# --------------------------------------------------------------------------- #

def _debug_lineage(state: RunState, node_id: int) -> int:
    """Count 'debug' operators in this node's ancestry (incl. itself)."""
    seen, stack, visited = 0, [node_id], set()
    while stack:
        nid = stack.pop()
        if nid in visited or nid not in state.nodes:
            continue
        visited.add(nid)
        n = state.nodes[nid]
        if n.operator == "debug":
            seen += 1
        stack.extend(n.parent_ids)
    return seen


def debug_action(state: RunState, debug_depth: int) -> Optional[dict]:
    """A debug action for the first failed leaf whose debug-lineage depth is below the
    bound, else None. Caller is responsible for the node budget."""
    if debug_depth <= 0:
        return None
    has_child: set[int] = set()
    for n in state.nodes.values():
        has_child.update(n.parent_ids)
    for n in sorted(state.nodes.values(), key=lambda n: n.id):
        if (n.status is NodeStatus.failed and n.id not in has_child
                and _debug_lineage(state, n.id) < debug_depth):
            return {"kind": "debug", "parent_id": n.id}
    return None


class GreedyTree:
    def __init__(
        self,
        n_seeds: int = 3,
        max_nodes: int = 8,
        debug_depth: int = 1,
        enable_merge: bool = True,
        merge_every: int = 3,
        max_merges: int = 2,
        ablate_every: int = 0,
    ):
        self.n_seeds = n_seeds
        self.max_nodes = max_nodes
        self.debug_depth = debug_depth
        self.enable_merge = enable_merge
        self.merge_every = merge_every
        self.max_merges = max_merges
        self.ablate_every = ablate_every  # 0 = off (I7 ablation-driven refinement)

    def next_actions(self, state: RunState) -> list[dict]:
        # 1. Evaluate anything created-but-not-evaluated (crash-resume re-entry point).
        pending = state.pending_nodes()
        if pending:
            return [{"kind": "evaluate", "node_id": n.id} for n in pending]

        total = len(state.nodes)

        # 2. Self-repair failed leaves within the depth bound (consumes budget).
        if total < self.max_nodes:
            dbg = debug_action(state, self.debug_depth)
            if dbg:
                return [dbg]

        if total >= self.max_nodes:
            return []  # budget spent -> finish

        # 3. Seed phase.
        if total < self.n_seeds:
            k = min(self.n_seeds - total, self.max_nodes - total)
            return [{"kind": "draft"} for _ in range(k)]

        best = state.best()
        if best is None:
            return [{"kind": "draft"}]

        # 4. Periodic merge of the top-2 evaluated nodes (multi-parent DAG step).
        evaluated = state.feasible_nodes()   # never breed from constraint-violating nodes (#5)
        n_improve = sum(1 for n in state.nodes.values() if n.operator == "improve")
        n_merge = sum(1 for n in state.nodes.values() if n.operator == "merge")
        # One merge per `merge_every` improves (not back-to-back): gate on the merge DEFICIT
        # vs the milestone count, since n_improve is unchanged between consecutive merges.
        if (self.enable_merge and len(evaluated) >= 2 and n_merge < self.max_merges
                and n_improve >= self.merge_every and n_merge < n_improve // self.merge_every):
            top2 = sorted(evaluated, key=lambda n: (n.metric, n.id),
                          reverse=(state.direction == "max"))[:2]
            return [{"kind": "merge", "parent_ids": [top2[0].id, top2[1].id]}]

        # 5. Ablation-driven refinement (I7): periodically ablate the best to find the
        #    highest-impact parameter, then refine just that one.
        n_refine = sum(1 for n in state.nodes.values() if n.operator == "refine_block")
        if (self.ablate_every > 0 and len(best.idea.params) >= 2
                and n_improve >= (n_refine + 1) * self.ablate_every):
            return [{"kind": "ablate", "parent_id": best.id}]

        # 6. Exploit: improve the current best.
        return [{"kind": "improve", "parent_id": best.id}]


class EvolutionaryPolicy:
    """Opt-in alternative SearchPolicy (I22, ADR-2). Maintains a population; each
    generation either crossovers two elites (merge) or mutates a *rotating* elite
    (improve) — so it explores more broadly than GreedyTree's always-exploit-the-best.
    Plugs into the unchanged orchestrator (same action vocabulary), proving the
    SearchPolicy/algorithm seam.
    """

    def __init__(self, pop: int = 4, max_nodes: int = 12, elite: int = 2,
                 debug_depth: int = 1):
        self.pop = pop
        self.max_nodes = max_nodes
        self.elite = max(1, elite)  # guard against /0 in gen % len(elites)
        self.debug_depth = debug_depth

    def next_actions(self, state: RunState) -> list[dict]:
        pending = state.pending_nodes()
        if pending:
            return [{"kind": "evaluate", "node_id": n.id} for n in pending]

        total = len(state.nodes)
        if total < self.max_nodes:
            dbg = debug_action(state, self.debug_depth)
            if dbg:
                return [dbg]
        if total >= self.max_nodes:
            return []

        # Fill the initial population with drafts.
        if total < self.pop:
            k = min(self.pop - total, self.max_nodes - total)
            return [{"kind": "draft"} for _ in range(k)]

        evaluated = sorted(state.feasible_nodes(),   # elites must be feasible (#5)
                           key=lambda n: (n.metric, n.id),
                           reverse=(state.direction == "max"))
        if not evaluated:
            return [{"kind": "draft"}]
        elites = evaluated[: self.elite]
        # Offspring index = how many generation-producing operators (improve/merge) already
        # exist — NOT total node count, so inserted debug/failed nodes can't perturb the
        # crossover/mutate parity or the elite rotation (deterministic w.r.t. eval failures).
        gen = sum(1 for n in state.nodes.values() if n.operator in ("improve", "merge"))

        # Even generations crossover two elites; odd generations mutate a rotating elite.
        if gen % 2 == 0 and len(elites) >= 2:
            i = (gen // 2) % len(elites)
            j = (i + 1) % len(elites)
            return [{"kind": "merge", "parent_ids": [elites[i].id, elites[j].id]}]
        return [{"kind": "improve", "parent_id": elites[gen % len(elites)].id}]


class MCTSPolicy:
    """Opt-in UCB1 tree search (I22, ADR-2). Selects which node to expand by
    UCB1 = reward + c·sqrt(ln N / visits), balancing exploiting good subtrees against
    exploring under-visited ones — distinct from greedy (always the best) and
    evolutionary (rotating elites). Pure: visits/values are derived from the folded DAG.
    """

    def __init__(self, n_seeds: int = 3, max_nodes: int = 12, c: float = 1.4,
                 debug_depth: int = 1):
        self.n_seeds = n_seeds
        self.max_nodes = max_nodes
        self.c = c
        self.debug_depth = debug_depth

    def next_actions(self, state: RunState) -> list[dict]:
        pending = state.pending_nodes()
        if pending:
            return [{"kind": "evaluate", "node_id": n.id} for n in pending]
        total = len(state.nodes)
        if total < self.max_nodes:
            dbg = debug_action(state, self.debug_depth)
            if dbg:
                return [dbg]
        if total >= self.max_nodes:
            return []
        if total < self.n_seeds:
            k = min(self.n_seeds - total, self.max_nodes - total)
            return [{"kind": "draft"} for _ in range(k)]
        evaluated = state.feasible_nodes()   # improve only feasible candidates (#5)
        if not evaluated:
            return [{"kind": "draft"}]

        children: dict[int, list[int]] = {}
        for n in state.nodes.values():
            for p in n.parent_ids:
                children.setdefault(p, []).append(n.id)

        def subtree(nid: int) -> set[int]:
            seen: set[int] = set()
            stack = [nid]
            while stack:
                x = stack.pop()
                if x in seen:
                    continue
                seen.add(x)
                stack.extend(children.get(x, []))
            return seen

        n_total = len(evaluated)
        best_of = min if state.direction == "min" else max
        chosen, best_ucb = None, None
        scores: dict[int, float] = {}   # per-candidate UCB1 (surfaced as a `policy_decision` event)
        for node in sorted(evaluated, key=lambda n: n.id):
            tree = subtree(node.id)
            metrics = [state.nodes[i].metric for i in tree
                       if state.nodes[i].metric is not None and state.nodes[i].feasible]  # #5
            if not metrics:
                continue
            value = best_of(metrics)
            reward = 1.0 / (1.0 + abs(value)) if state.direction == "min" else value
            # Visits = real (feasible, evaluated) trials in the subtree, not failed/infeasible
            # nodes, so the UCB exploration term reflects actual exploration (#76).
            visits = sum(1 for i in tree if state.nodes[i].status is NodeStatus.evaluated
                         and state.nodes[i].feasible) or 1
            ucb = reward + self.c * math.sqrt(math.log(n_total + 1) / visits)
            scores[node.id] = round(ucb, 4)
            if best_ucb is None or ucb > best_ucb:
                best_ucb, chosen = ucb, node.id
        if chosen is None:
            b = state.best()
            chosen = b.id if b is not None else sorted(evaluated, key=lambda n: n.id)[0].id
        return [{"kind": "improve", "parent_id": chosen, "_scores": scores, "_chosen": chosen}]


def make_policy(name: str = "greedy", *, n_seeds: int, max_nodes: int,
                ablate_every: int = 0) -> SearchPolicy:
    """Select a search policy by name (ADR-2 pluggable algorithm)."""
    if name == "greedy":
        return GreedyTree(n_seeds=n_seeds, max_nodes=max_nodes, ablate_every=ablate_every)
    if name == "evolutionary":
        return EvolutionaryPolicy(pop=n_seeds, max_nodes=max_nodes)
    if name == "mcts":
        return MCTSPolicy(n_seeds=n_seeds, max_nodes=max_nodes)
    raise ValueError(f"unknown policy: {name!r}")
