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

from looplab.core.models import NodeStatus, RunState


class SearchPolicy(Protocol):
    def next_actions(self, state: RunState) -> list[dict]: ...


def _metric_scores(nodes) -> dict[int, float]:
    """Candidate comparison surfaced as a `policy_decision` event ("why this node"): map each
    node's id to its observed metric. Lets the UI show the alternatives the policy weighed
    against the one it chose — even for policies (GreedyTree) that pick by raw metric."""
    return {n.id: round(n.metric, 4) for n in nodes if n.metric is not None}


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


def operator_yields(state: RunState) -> dict[str, dict]:
    """P4: per-operator empirical yield, folded purely from the DAG — {op: {"n": tried,
    "gain": mean positive Δmetric-over-best-parent per eval-second}}. The data for a
    deterministic UCB over operators (the cheap, principled 'adaptive operator mix' — the
    Strategist's rule table becomes priors, not hard-coded cadences). Draft nodes have no
    parent, so 'draft' yield is not defined here (drafts are the exploration baseline)."""
    out: dict[str, dict] = {}
    for n in state.nodes.values():
        if not n.parent_ids or n.status is not NodeStatus.evaluated or n.metric is None:
            continue
        pm = [state.nodes[p].metric for p in n.parent_ids
              if p in state.nodes and state.nodes[p].metric is not None]
        if not pm:
            continue
        # direction-aware improvement over the best parent (clamped at 0 — a regression yields
        # no positive credit), amortized per eval-second so cheap wins rank above slow ones.
        base = max(pm) if state.direction == "max" else min(pm)
        delta = (n.metric - base) if state.direction == "max" else (base - n.metric)
        gain = max(0.0, delta) / max(0.1, (n.eval_seconds or 0.1))
        d = out.setdefault(n.operator, {"n": 0, "gain": 0.0})
        d["gain"] = (d["gain"] * d["n"] + gain) / (d["n"] + 1)
        d["n"] += 1
    return out


def _bandit_pick(yields: dict[str, dict], candidates: list[str], c: float = 0.8) -> str:
    """Deterministic UCB1 over operator kinds: an UNTRIED operator is optimistically tried first
    (classic UCB1 infinite priority, in candidate order); otherwise mean normalized gain + an
    exploration bonus for rarely-tried operators. Ties break by candidate order (caller lists
    its default first)."""
    for k in candidates:
        if yields.get(k, {"n": 0})["n"] == 0:
            return k                      # optimism under uncertainty: try every operator once
    total = sum(d["n"] for d in yields.values()) or 1
    gmax = max((d["gain"] for d in yields.values()), default=0.0) or 1.0
    best_k, best_s = candidates[0], None
    for k in candidates:
        d = yields[k]
        score = (d["gain"] / gmax) + c * math.sqrt(math.log(total + 1) / (d["n"] + 1))
        if best_s is None or score > best_s + 1e-12:
            best_k, best_s = k, score
    return best_k


def weighted_parent(state: RunState, feasible=None) -> Optional[int]:
    """ShinkaEvolve-shaped parent selection, derandomized for replay safety: prefer parents
    with a high fitness RANK that are still UNDER-EXPANDED — weight = 1/rank / (1 + children).
    Expanding a node lowers its weight, so selection rotates through good stepping stones
    instead of hammering the single global best (weighted parent sampling beat both
    hill-climbing and random selection in ShinkaEvolve's ablation). Deterministic: pure
    argmax with id tie-break."""
    pool = feasible if feasible is not None else state.feasible_nodes()
    if not pool:
        return None
    ranked = sorted(pool, key=lambda n: (n.metric, n.id), reverse=(state.direction == "max"))
    kids: dict[int, int] = {}
    for n in state.nodes.values():
        for p in n.parent_ids:
            kids[p] = kids.get(p, 0) + 1
    best_id, best_w = None, -1.0
    for rank, n in enumerate(ranked, start=1):
        w = (1.0 / rank) / (1.0 + kids.get(n.id, 0))
        if w > best_w + 1e-12:
            best_id, best_w = n.id, w
    return best_id


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
                and n.error_reason != "idea_rejected"   # crash-triage judged the idea wrong: don't debug it
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
        operator_bandit: bool = False,
    ):
        self.n_seeds = n_seeds
        self.max_nodes = max_nodes
        self.debug_depth = debug_depth
        self.enable_merge = enable_merge
        self.merge_every = merge_every
        self.max_merges = max_merges
        self.ablate_every = ablate_every  # 0 = off (I7 ablation-driven refinement)
        # P4: replace the FIXED merge/ablate cadences with a deterministic UCB over operator
        # yields folded from the run itself (Δmetric per eval-second). Off by default: the
        # cadence defaults are well-tested and the bandit has no direct published ablation —
        # `thorough` turns it on.
        self.operator_bandit = operator_bandit

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

        evaluated = state.feasible_nodes()   # never breed from constraint-violating nodes (#5)
        n_improve = sum(1 for n in state.nodes.values() if n.operator == "improve")
        n_merge = sum(1 for n in state.nodes.values() if n.operator == "merge")
        n_refine = sum(1 for n in state.nodes.values() if n.operator == "refine_block")

        # P4 operator bandit: pick the next operator by observed yield (deterministic UCB over
        # Δmetric per eval-second folded from the run), instead of the fixed cadences below.
        # Only chooses among currently-LEGAL operators; falls through to the cadence logic when
        # off or when it picks the default (improve).
        if self.operator_bandit:
            cands = ["improve"]
            if self.enable_merge and len(evaluated) >= 2 and n_merge < self.max_merges:
                cands.append("merge")
            if self.ablate_every > 0 and len(best.idea.params) >= 2:
                cands.append("refine_block")
            pick = _bandit_pick(operator_yields(state), cands)
            if pick == "merge":
                top2 = sorted(evaluated, key=lambda n: (n.metric, n.id),
                              reverse=(state.direction == "max"))[:2]
                return [{"kind": "merge", "parent_ids": [top2[0].id, top2[1].id],
                         "_scores": _metric_scores(top2), "_chosen": top2[0].id,
                         "_reason": "bandit: merge top-2"}]
            if pick == "refine_block":
                return [{"kind": "ablate", "parent_id": best.id,
                         "_scores": _metric_scores(evaluated), "_chosen": best.id,
                         "_reason": "bandit: ablate highest-impact param"}]
            return [{"kind": "improve", "parent_id": best.id,
                     "_scores": _metric_scores(evaluated), "_chosen": best.id,
                     "_reason": "bandit: exploit best"}]

        # 4. Periodic merge of the top-2 evaluated nodes (multi-parent DAG step).
        # One merge per `merge_every` improves (not back-to-back): gate on the merge DEFICIT
        # vs the milestone count, since n_improve is unchanged between consecutive merges.
        if (self.enable_merge and len(evaluated) >= 2 and n_merge < self.max_merges
                and n_improve >= self.merge_every and n_merge < n_improve // self.merge_every):
            top2 = sorted(evaluated, key=lambda n: (n.metric, n.id),
                          reverse=(state.direction == "max"))[:2]
            return [{"kind": "merge", "parent_ids": [top2[0].id, top2[1].id],
                     "_scores": _metric_scores(top2), "_chosen": top2[0].id,
                     "_reason": "merge top-2"}]

        # 5. Ablation-driven refinement (I7): periodically ablate the best to find the
        #    highest-impact parameter, then refine just that one.
        if (self.ablate_every > 0 and len(best.idea.params) >= 2
                and n_improve >= (n_refine + 1) * self.ablate_every):
            return [{"kind": "ablate", "parent_id": best.id,
                     "_scores": _metric_scores(evaluated), "_chosen": best.id,
                     "_reason": "ablate highest-impact param"}]

        # 6. Exploit: improve the current best (over all feasible candidates).
        return [{"kind": "improve", "parent_id": best.id,
                 "_scores": _metric_scores(evaluated), "_chosen": best.id,
                 "_reason": "exploit best"}]


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

        # Even generations crossover two elites; odd generations mutate a WEIGHTED parent:
        # fitness-rank × under-expansion over the WHOLE feasible archive (not just elites) —
        # ShinkaEvolve's #1-ranked lever (weighted parent sampling beat both hill-climbing and
        # random), derandomized for replay safety. Good stepping stones outside the elite set
        # stay reachable; expanding a node lowers its weight, so selection rotates naturally.
        if gen % 2 == 0 and len(elites) >= 2:
            i = (gen // 2) % len(elites)
            j = (i + 1) % len(elites)
            return [{"kind": "merge", "parent_ids": [elites[i].id, elites[j].id]}]
        pid = weighted_parent(state, evaluated)
        if pid is None:
            pid = elites[gen % len(elites)].id
        return [{"kind": "improve", "parent_id": pid,
                 "_scores": _metric_scores(evaluated), "_chosen": pid,
                 "_reason": "weighted parent (fitness-rank × under-expansion)"}]


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


class ASHAPolicy:
    """A1 · Asynchronous Successive Halving (ASHA / Hyperband, ADR-2). Allocates compute by
    *racing*: seed a wide rung-0 of cheap drafts, then promote only the top 1/eta survivors to the
    next rung (an `improve` that gets more attention), recursively — instead of full-expanding
    every lineage. Adapted to LoopLab's tree substrate: a "rung" is a generation (draft=rung 0,
    improve-of-survivor=rung 1, …); promotion = spending the next node on a survivor's lineage. The
    fidelity (smoke at low rungs, full near the top) is driven by the Strategist/eval-profile seam.

    Pure: rungs/survivors are derived from the folded DAG, so it's deterministic and replay-safe.
    Emits `_rung`/`_promoted` meta on its action so the engine can log a `rung_promoted` event."""

    def __init__(self, n_seeds: int = 4, max_nodes: int = 12, eta: int = 3, debug_depth: int = 1,
                 rung_nodes: int = 0):
        self.n_seeds = max(2, n_seeds)
        # rung-0 width: an explicit rung_nodes (>0) overrides n_seeds, else default to n_seeds.
        self.rung0 = max(2, rung_nodes) if rung_nodes else self.n_seeds
        self.max_nodes = max_nodes
        self.eta = max(2, eta)                 # keep top 1/eta per rung
        self.debug_depth = debug_depth

    def _generation(self, state: RunState) -> dict[int, int]:
        """Generation (rung) of each node: a draft is rung 0; an improve/merge child is parent+1.
        Computed by a monotone pass over ids (parents always precede children)."""
        gen: dict[int, int] = {}
        for n in sorted(state.nodes.values(), key=lambda n: n.id):
            if not n.parent_ids:
                gen[n.id] = 0
            else:
                gen[n.id] = 1 + max((gen.get(p, 0) for p in n.parent_ids), default=0)
        return gen

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

        # Rung 0: fill to rung0 cheap drafts (the wide base of the bracket).
        drafts = [n for n in state.nodes.values() if not n.parent_ids]
        if len(drafts) < self.rung0:
            k = min(self.rung0 - len(drafts), self.max_nodes - total)
            return [{"kind": "draft"} for _ in range(k)]

        gen = self._generation(state)
        feasible = {n.id for n in state.feasible_nodes()}
        if not feasible:
            return [{"kind": "draft"}]
        has_child: set[int] = set()
        for n in state.nodes.values():
            has_child.update(n.parent_ids)

        # Promote from the LOWEST rung that still has an unexpanded survivor (asynchronous: don't
        # wait for a whole rung to finish before promoting from a lower one).
        by_rung: dict[int, list[int]] = {}
        for nid in feasible:
            by_rung.setdefault(gen.get(nid, 0), []).append(nid)
        for r in sorted(by_rung):
            members = by_rung[r]
            # successive-halving survivor count: keep the top ⌈n/η⌉ (round UP so a rung wider than η
            # always promotes ≥2 — `floor` would collapse e.g. n=4,η=3 to 1 survivor and never halve).
            keep = max(1, math.ceil(len(members) / self.eta))
            survivors = sorted(
                members, key=lambda i: (state.nodes[i].metric, i),
                reverse=(state.direction == "max"))[:keep]
            unexpanded = [i for i in survivors if i not in has_child]
            if len(survivors) <= 1:
                continue            # rung collapsed to a single leader: nothing left to halve here
            if unexpanded:
                chosen = sorted(unexpanded)[0]
                scores = {i: round(state.nodes[i].metric, 4) for i in members
                          if state.nodes[i].metric is not None}
                return [{"kind": "improve", "parent_id": chosen,
                         "_scores": scores, "_chosen": chosen,
                         "_reason": f"promote rung {r + 1}",
                         "_rung": r + 1, "_promoted": survivors}]

        # All rungs collapsed/expanded -> exploit the global best with remaining budget.
        b = state.best()
        if b is None:
            return [{"kind": "draft"}]
        return [{"kind": "improve", "parent_id": b.id,
                 "_scores": _metric_scores(state.feasible_nodes()), "_chosen": b.id,
                 "_reason": "exploit best (rungs collapsed)"}]


def legal_actions(state: RunState, policy: SearchPolicy, *, max_nodes: int) -> list[dict]:
    """Pure legal-action gate for the self-driving unified agent (replaces the policy as the
    *master* of action selection without surrendering pipeline discipline). Returns the set of
    macro actions the agent may choose from given the folded state, derived from the SAME
    invariants every `next_actions` enforces — so whatever the agent picks, the pipeline stays
    correct. Forced phases return a single non-negotiable set (the agent has no discretion):

      * pending nodes        -> only `evaluate` (crash-resume re-entry invariant)
      * node budget spent    -> `[]` (finish)
      * seed phase           -> only `draft`

    Otherwise the explore/exploit envelope is built from REAL nodes (draft / improve any feasible /
    debug a failed leaf within depth / merge the top-2 / ablate the best), so a chosen parent can
    never be illegal. Deterministic and side-effect-free — safe to call on every loop turn."""
    pending = state.pending_nodes()
    if pending:
        return [{"kind": "evaluate", "node_id": n.id} for n in pending]
    total = len(state.nodes)
    if total >= max_nodes:
        return []
    n_seeds = getattr(policy, "rung0", None) or getattr(policy, "n_seeds", getattr(policy, "pop", 3))
    if total < n_seeds:
        return [{"kind": "draft"}]
    actions: list[dict] = [{"kind": "draft"}]
    feasible = sorted(state.feasible_nodes(), key=lambda n: (n.metric, n.id),
                      reverse=(state.direction == "max"))
    actions.extend({"kind": "improve", "parent_id": n.id} for n in feasible)
    dbg = debug_action(state, getattr(policy, "debug_depth", 1))
    if dbg:
        actions.append(dbg)
    if len(feasible) >= 2:
        actions.append({"kind": "merge", "parent_ids": [feasible[0].id, feasible[1].id]})
    best = state.best()
    if best is not None and len(best.idea.params) >= 2:
        actions.append({"kind": "ablate", "parent_id": best.id})
    return actions


# Policy registry (ADR-2). The Strategist (A7) may only pick from these names; new policies
# auto-register here and become selectable without engine changes.
_POLICIES = ("greedy", "evolutionary", "mcts", "asha", "bohb")


def available_policies() -> list[str]:
    return list(_POLICIES)


def make_policy(name: str = "greedy", *, n_seeds: int, max_nodes: int,
                ablate_every: int = 0, **params) -> SearchPolicy:
    """Select a search policy by name (ADR-2 pluggable algorithm). `params` carries
    policy-specific overrides the Strategist may pass (e.g. mcts `c`, asha `eta`) plus the
    run-wide `debug_depth` / `operator_bandit` knobs (Settings)."""
    depth = int(params.get("debug_depth", 1) or 1)
    if name == "greedy":
        return GreedyTree(n_seeds=n_seeds, max_nodes=max_nodes, ablate_every=ablate_every,
                          debug_depth=depth,
                          operator_bandit=bool(params.get("operator_bandit", False)))
    if name == "evolutionary":
        return EvolutionaryPolicy(pop=n_seeds, max_nodes=max_nodes, debug_depth=depth)
    if name == "mcts":
        c = float(params.get("c", 1.4))
        return MCTSPolicy(n_seeds=n_seeds, max_nodes=max_nodes, c=c, debug_depth=depth)
    if name in ("asha", "bohb"):
        # A3 BOHB = Hyperband racing (ASHA) × surrogate-guided proposal (A2). The racing schedule is
        # the ASHA policy; the surrogate is wired as the Researcher (cli enables it for `bohb`), so
        # the policy object is the same — the fusion is the racing schedule + a surrogate proposer.
        eta = int(params.get("eta", 3))
        return ASHAPolicy(n_seeds=n_seeds, max_nodes=max_nodes, eta=eta, debug_depth=depth,
                          rung_nodes=int(params.get("rung_nodes", 0) or 0))
    raise ValueError(f"unknown policy: {name!r}")
