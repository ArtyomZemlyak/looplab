"""Does the CHEAP evaluation level rank candidates the way the FULL one does? (doc 68 68.5)

THE QUESTION. `EvalSpec.profiles` is LoopLab's evaluation cascade: the search scores on a cheap
profile (`smoke`, or whatever `idea.eval_profile` / the Strategist's fidelity names) and trusts that
ordering to decide what survives. Doc 68 §5 records an operator who would not trust it blind: they
screened seven backbones cheaply, CHECKED that the cheap level ranked like the full one on the nodes
that had both, and only then ran the leaders in full. Nothing in LoopLab could do that check; this
is it. The box measurement it enables is doc 52's `smoke-full-rank-fidelity-unmeasured`.

WHERE BOTH LEVELS EXIST. The confirm phase re-evaluates the top-K BY THE SEARCH NUMBER at `full`
(`engine/confirm_phase.py`, `confirmed_mean` over `confirmed_seeds`), so a confirmed node carries
its search number AND a confirmation mean — the only pair a run records. Which says what this
number is about: the ordering of the candidates the search PROMOTED, never of those it pruned —
a pruned node is never confirmed. An operator-forced confirmation (`_confirm_node`) writes no
`node_confirmed` and is not read.

WHICH LEVEL A NUMBER IS ON is decided by what it was MEASURED under, never by a label (critic
2026-09-26, driven on real engine runs). The first cut asked `idea.eval_profile == "full"`, and the
label says little: a node that leaves it null is scored at the Strategist's fidelity — which the
rule Strategist sets to `full` in the endgame and under a short budget — and on a task that declares
no profiles `smoke` and `full` are both the base command. Each of those shapes reported seed noise
as a cheap/full disagreement (20 % agreement, rho -0.70, on nodes all measured at `full`). So both
sides are the RECORDED ruler: the search number's `metric_provenance.comparability.protocol.
profile` (`events/card_ledger.py::measured_ruler`) and the confirmation's `Node.confirmed_ruler`,
the digest every counted confirm seed ran under (`engine/comparability.py::agreed_ruler`). A node
on one ruler at both measures seed noise, not fidelity (`same_level`); one missing either record is
`unknown` — every log written before the two records, and every solution-tier task, which has no
profiles. Pairs are formed only among nodes sharing ONE (search ruler, confirm ruler) pair, since
two search numbers on two rulers are no one ordering. The comparison also crosses seed sets (the
search at seed 0, confirm from `confirm_seed_base`, 1 by default) unless an operator sets that base
to 0, so a disagreement is fidelity OR noise. Only nodes whose number counts toward the best are
read (`core/fitness.py::counts_toward_best` over the evaluated population).

WHAT IT REPORTS. Pairwise ordering agreement (a tie on either side is no ordering, and is counted
apart, never as a disagreement) and Spearman's rho over average ranks when three or more nodes of
one ruler group have both. Direction-free: two orderings agree or disagree whichever way the
objective points.

INSTRUMENT, NOT GATE — the house rule for this family (`looplab proxy-accuracy`, `asha-rungs`): it
reads the fold and decides nothing. Whether the cheap level's ordering may be trusted to promote is
the operator's call once the number exists.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

from looplab.core.fitness import counts_toward_best
from looplab.events.card_ledger import measured_ruler

# Printed with every reading, and carried in the JSON, because the number is not interpretable
# without it.
CAVEAT = ("each pair crosses seed sets as well as levels, so a disagreement is fidelity OR noise; "
          "and only the nodes the search promoted to confirmation are ranked, never those it pruned")


def _finite(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def spearman_rho(left: list[float], right: list[float]) -> Optional[float]:
    """Spearman's rho over average ranks, or None below three points or with no variance."""
    if len(left) != len(right) or len(left) < 3:
        return None
    rl, rr = _average_ranks(left), _average_ranks(right)
    ml, mr = sum(rl) / len(rl), sum(rr) / len(rr)
    cov = sum((a - ml) * (b - mr) for a, b in zip(rl, rr))
    var = math.sqrt(sum((a - ml) ** 2 for a in rl) * sum((b - mr) ** 2 for b in rr))
    return None if var == 0 else cov / var


def fidelity_rank_agreement(state) -> dict:
    """One run's cheap-vs-full ordering agreement over the nodes that carry both levels.

    `{"nodes": [{node, profile, cheap_ruler, full_ruler, cheap, full, full_std, seeds}], "pairs",
      "concordant", "discordant", "ties", "agreement", "spearman", "same_level", "unknown",
      "groups"}` — `agreement` is None below one ordered pair and `spearman` below three nodes in
    one ruler group (and when the nodes span several groups: one rho over two rulers is none), which
    are different answers from 0.0 and must not render the same. `profile` is the node's declared
    `eval_profile`, or None — a label, shown, never decided on.
    """
    flagged = frozenset(state.breed_excluded or ())
    aborted = frozenset(state.aborted_nodes or ())
    rows, same_level, unknown = [], 0, 0
    for node in sorted(state.evaluated_nodes(), key=lambda n: n.id):
        full = _finite(node.confirmed_mean)
        cheap = _finite(node.metric)
        if full is None or cheap is None or not counts_toward_best(node, flagged, aborted):
            continue
        cheap_ruler, full_ruler = measured_ruler(node), node.confirmed_ruler
        if not cheap_ruler or not full_ruler:
            unknown += 1
            continue
        if cheap_ruler == full_ruler:
            same_level += 1
            continue
        rows.append({"node": node.id,
                     "profile": getattr(getattr(node, "idea", None), "eval_profile", None),
                     "cheap_ruler": cheap_ruler, "full_ruler": full_ruler, "cheap": cheap,
                     "full": full, "full_std": _finite(node.confirmed_std),
                     "seeds": node.confirmed_seeds})
    groups: dict = {}
    for row in rows:
        groups.setdefault((row["cheap_ruler"], row["full_ruler"]), []).append(row)
    pairs = concordant = discordant = ties = 0
    for members in groups.values():
        for index, left in enumerate(members):
            for right in members[index + 1:]:
                d_cheap = left["cheap"] - right["cheap"]
                d_full = left["full"] - right["full"]
                if d_cheap == 0 or d_full == 0:
                    ties += 1
                    continue
                pairs += 1
                if (d_cheap > 0) == (d_full > 0):
                    concordant += 1
                else:
                    discordant += 1
    return {
        "nodes": rows,
        "pairs": pairs,
        "concordant": concordant,
        "discordant": discordant,
        "ties": ties,
        "agreement": (concordant / pairs) if pairs else None,
        "spearman": (spearman_rho([r["cheap"] for r in rows], [r["full"] for r in rows])
                     if len(groups) == 1 else None),
        "same_level": same_level,
        "unknown": unknown,
        "groups": len(groups),
    }


def fidelity_agreement_report(rows: Iterable[dict]) -> dict:
    """The corpus reading: pairs POOLED across runs — each pair lies inside one run, since two runs'
    numbers are not on one scale — and how many runs contributed any."""
    rows = list(rows)
    concordant = sum(r["concordant"] for r in rows)
    discordant = sum(r["discordant"] for r in rows)
    pairs = concordant + discordant
    return {
        "runs": len(rows),
        "runs_with_pairs": sum(1 for r in rows if r["pairs"]),
        "nodes_with_both": sum(len(r["nodes"]) for r in rows),
        "same_level": sum(r["same_level"] for r in rows),
        "unknown": sum(r["unknown"] for r in rows),
        "pairs": pairs,
        "concordant": concordant,
        "discordant": discordant,
        "ties": sum(r["ties"] for r in rows),
        "agreement": (concordant / pairs) if pairs else None,
        "caveat": CAVEAT,
    }
