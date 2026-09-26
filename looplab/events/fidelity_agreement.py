"""Does the CHEAP evaluation level rank candidates the way the FULL one does? (doc 68 68.5)

THE QUESTION. `EvalSpec.profiles` is LoopLab's evaluation cascade: the search scores on a cheap
profile (`smoke`, or whatever `idea.eval_profile` / the Strategist's fidelity names) and trusts that
ordering to decide what survives — ASHA's rungs, the proxy kill, promotion. Doc 68 §5 records an
operator who would not trust it blind: they screened seven backbones cheaply, CHECKED that the cheap
level ranked like the full one on the nodes that had both, and only then ran the leaders in full.
Nothing in LoopLab could do that check; this is it. The box measurement it enables is doc 52's
`smoke-full-rank-fidelity-unmeasured`.

WHERE BOTH LEVELS EXIST. The confirm phase re-evaluates the top-K at the FULL profile
(`engine/confirm_phase.py`, `confirmed_mean` over `confirmed_seeds`), so a confirmed node carries its
search number AND a full-profile mean — the only pair a run records. A node whose own search profile
was already `full` measures seed noise, not fidelity, and is counted apart (`same_level`); the
comparison also crosses seed sets (the search at seed 0, confirm from `confirm_seed_base`), which the
report states rather than hides. Only nodes whose number counts toward the best are read — feasible,
not tombstoned, aborted or gate-excluded — the pool every other measurement in this repo uses.

WHAT IT REPORTS. Pairwise ordering agreement (a tie on either side is no ordering, and is counted
apart, never as a disagreement) and Spearman's rho over average ranks when three or more nodes have
both. Direction-free: two orderings agree or disagree whichever way the objective points.

INSTRUMENT, NOT GATE — the house rule for this family (`looplab proxy-accuracy`, `asha-rungs`): it
reads the fold and decides nothing. Whether a cheap level may be trusted to prune is the operator's
call once the number exists.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

FULL_PROFILE = "full"


def _counts(node, state) -> bool:
    return (node.metric is not None and node.feasible and not node.tombstoned
            and node.id not in (state.aborted_nodes or ())
            and node.id not in (state.breed_excluded or ()))


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

    `{"nodes": [{node, profile, cheap, full, full_std, seeds}], "pairs", "concordant", "discordant",
      "ties", "agreement", "spearman", "same_level"}` — `agreement` is None below one ordered pair and
    `spearman` below three nodes, which are different answers from 0.0 and must not render the same.
    """
    rows = []
    same_level = 0
    for node in sorted((state.nodes or {}).values(), key=lambda n: n.id):
        full = _finite(node.confirmed_mean)
        cheap = _finite(node.metric)
        if full is None or cheap is None or not _counts(node, state):
            continue
        profile = getattr(getattr(node, "idea", None), "eval_profile", None)
        if profile == FULL_PROFILE:
            same_level += 1
            continue
        rows.append({"node": node.id, "profile": profile or "search default", "cheap": cheap,
                     "full": full, "full_std": _finite(node.confirmed_std),
                     "seeds": node.confirmed_seeds})
    pairs = concordant = discordant = ties = 0
    for index, left in enumerate(rows):
        for right in rows[index + 1:]:
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
        "spearman": spearman_rho([r["cheap"] for r in rows], [r["full"] for r in rows]),
        "same_level": same_level,
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
        "pairs": pairs,
        "concordant": concordant,
        "discordant": discordant,
        "ties": sum(r["ties"] for r in rows),
        "agreement": (concordant / pairs) if pairs else None,
    }
