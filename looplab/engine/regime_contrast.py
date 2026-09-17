"""What a run's score actually turned on: the IMPLEMENTATION REGIME its nodes shipped.

WHY THIS EXISTS. docs/56 §108 asked why a dollar buys the score it buys and found that on
`edge_expansion` the answer is very nearly one binary choice — did the node write a compiled
kernel:

    edge_expansion, per node    n    median     max
    Cython `.pyx` kernel       42    166.49   277.23
    numba                      14     27.52    28.33
    pure Python                23     22.86    35.02

Six-fold, and it is the whole spread. Two things followed. The loop does not STAY on the regime it
found — of 28 transitions out of a kernel node, 14 proposed a kernel again and 14 proposed
something else (B1, `search/policy.py::exploit_forced_action`, is that half). And the finding never
LEAVES the task: kernel adoption is 26 of 27 runs on `edge_expansion`, 5 of 11 on `discrete_log`
and 0 of 11 on `pde_heat1d` (docs/60 §60.9 B2 is that half).

AND THE CORRECTION THAT MAKES THIS A MEASUREMENT RATHER THAN A SLOGAN (§110). "A compiled kernel is
worth 6x" is true of `edge_expansion` and of nothing else measured: read at CHAMPION level, 26 of
27 `edge_expansion` champions carry a kernel, on `discrete_log` a kernel is neither necessary nor
sufficient, and on `pde_heat1d` not one of the ten champions has one while the spread there is
still 5.5x. `edge_expansion` is 27 of the corpus's 49 runs, which is exactly why the pooled view
reads as a general law and is not one.

So this module computes a CONTRAST, per run, with its sample sizes beside it — never a verdict and
never a recommendation. Whoever reads it (a skill card's evidence, a propose brief, an operator)
reads "on THIS task, these regimes scored this much, over this many nodes", which is a claim that
travels honestly to another task because it names the task it came from. A module that emitted
"write a compiled kernel" would have been wrong on `pde_heat1d` the day it shipped.

Deterministic, model-free and free: it reads the nodes a finished run already has. No LLM, no
store, no event — a pure reading, so a caller can take it at run end, in an instrument over an
archive, or in a test.
"""
from __future__ import annotations

import re
import statistics
from typing import Optional

# THE REGIMES, in the order a reader should think about them: what the node COMPILED, if anything.
#
# `compiled` is an AOT extension — a Cython/C/C++/Rust source the node shipped, or the build recipe
# that turns one into an import. `jit` is a decorator that compiles at call time (numba, torch's
# compiler): a different regime with a different cost, and §108 measured it as a different
# population (median 27.52 against 166.49). `plain` is neither.
REGIME_COMPILED = "compiled"
REGIME_JIT = "jit"
REGIME_PLAIN = "plain"
REGIMES = (REGIME_COMPILED, REGIME_JIT, REGIME_PLAIN)

# A SOURCE FILE THE NODE SHIPPED, by extension. The extension is the honest signal: a `.pyx` in the
# node's files is a Cython source whatever the text inside it says, and reading the text for
# `cimport` would miss a pure-Python-syntax `.pyx` (which is most of them on this bench).
_COMPILED_SUFFIXES = (".pyx", ".pxd", ".c", ".cc", ".cpp", ".rs")
# ... or the recipe that builds one. `cythonize(...)` and `Extension(...)` are the two spellings a
# `setup.py` uses; `# cython:` is the directive header a `.pyx` carries when it was written inline.
_COMPILED_TEXT = re.compile(r"\bcythonize\s*\(|\bExtension\s*\(|^\s*#\s*cython:", re.M)
# JIT is a DECORATOR, and the import alone is not it: `import numba` at the top of a file that never
# decorates anything compiles nothing. The pattern is the decoration site.
_JIT_TEXT = re.compile(r"@(?:\w+\.)?(?:njit|jit|vectorize|guvectorize)\b|@torch\.compile\b")


def node_regime(files: dict) -> str:
    """The regime a node's shipped files are in: `compiled`, `jit` or `plain`.

    Precedence is deliberate and stated: a node that ships BOTH a kernel and a jit decorator is
    `compiled`. The AOT extension is what its score is attributed to in §108's table, the two are
    not exclusive in practice (a `.pyx` beside a numba fallback is a common shape), and a tie-break
    decided per call site would make two readings of the same corpus disagree.
    """
    for path, text in (files or {}).items():
        if str(path).endswith(_COMPILED_SUFFIXES):
            return REGIME_COMPILED
    for path, text in (files or {}).items():
        if isinstance(text, str) and _COMPILED_TEXT.search(text):
            return REGIME_COMPILED
    for path, text in (files or {}).items():
        if isinstance(text, str) and _JIT_TEXT.search(text):
            return REGIME_JIT
    return REGIME_PLAIN


def contrast(rows: list) -> Optional[dict]:
    """`[(regime, metric), ...]` -> what each regime was worth here, or None when it says nothing.

    None when fewer than two regimes carry a measured node: a "contrast" over one population is a
    summary of that population wearing a comparative's clothes, and the next reader would take it
    for evidence that something was compared.

    The ratio is the best regime's median over the WORST regime's median, which is the number §108
    reports ("six-fold"), and it is `None` rather than infinity when the worst median is zero or
    negative — a run whose baseline scored zero has no ratio, and inventing one would put an
    unbounded number into a store that other runs read.
    """
    by: dict[str, list[float]] = {}
    for regime, metric in rows:
        if regime in REGIMES and isinstance(metric, (int, float)):
            by.setdefault(regime, []).append(float(metric))
    if len(by) < 2:
        return None
    stats = {r: {"n": len(v), "median": round(statistics.median(v), 6),
                 "max": round(max(v), 6), "min": round(min(v), 6)}
             for r, v in sorted(by.items())}
    best = max(stats, key=lambda r: stats[r]["median"])
    worst = min(stats, key=lambda r: stats[r]["median"])
    lo = stats[worst]["median"]
    return {
        "regimes": stats,
        "best": best,
        "worst": worst,
        "ratio": round(stats[best]["median"] / lo, 4) if lo > 0 else None,
        # THE SAMPLE, once, at the top level: every reader of this row needs it and a reader that
        # has to add up three sub-counts to find out how much evidence it is looking at will not.
        "nodes": sum(s["n"] for s in stats.values()),
    }


def run_contrast(state) -> Optional[dict]:
    """The contrast over ONE finished run's evaluated nodes, stamped with the task it is about.

    The task stamp is not decoration: §110 is the record of what happens when this number is read
    without it. A row that says "compiled beat plain 6x" is a general law and false; one that says
    "on `edge_expansion`, compiled beat plain 6x over 65 nodes" is a measurement another task can
    weigh.
    """
    nodes = [n for n in getattr(state, "feasible_nodes", lambda: [])() if n.metric is not None]
    got = contrast([(node_regime(getattr(n, "files", None)), n.metric) for n in nodes])
    if got is None:
        return None
    return {**got, "task_id": getattr(state, "task_id", ""),
            "direction": getattr(state, "direction", ""),
            "run_id": getattr(state, "run_id", "")}


def known_regimes(rows: list, task_id: str) -> dict:
    """Fold the shared ledger into what is KNOWN about `task_id`, and what is known elsewhere.

    The read side of docs/60 §60.9 B2, and the shape it returns is the whole argument. `here` is
    what this task's own runs measured; `elsewhere` is one line per OTHER task, each naming its own
    sample; `untried_here` is the regimes no run of this task has ever shipped.

    That last field is the one §419 is about. `pde_heat1d` ran 17 of its 18 nodes as `jit` and never
    once compiled, and 108 read that as a failure to carry the kernel finding. It is not: the task
    found a regime and stayed in it, and what is actually missing there is a COMPARISON. "Nobody has
    tried `compiled` on this task" is a true sentence that invites a check; "a compiled kernel is
    worth 6x" is a false one that instructs, and 110 is the record of it being false.

    Rows are ledger lines (`regime_contrast.jsonl`), so they are data from earlier runs and may be
    anything; every access is defensive and a malformed row is skipped rather than failing a read
    that is only ever advisory.
    """
    here: dict[str, list] = {}
    elsewhere: list = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        regimes = row.get("regimes")
        if not isinstance(regimes, dict):
            continue
        if row.get("task_id") == task_id:
            for regime, stats in regimes.items():
                if regime in REGIMES and isinstance(stats, dict):
                    here.setdefault(regime, []).append(stats)
        else:
            elsewhere.append({"task_id": row.get("task_id", ""), "best": row.get("best", ""),
                              "worst": row.get("worst", ""), "ratio": row.get("ratio"),
                              "nodes": row.get("nodes", 0)})
    summary = {}
    for regime, seen in sorted(here.items()):
        medians = [s["median"] for s in seen if isinstance(s.get("median"), (int, float))]
        counts = [s["n"] for s in seen if isinstance(s.get("n"), int)]
        if medians:
            # The MEDIAN OF THE RUN MEDIANS, not a pooled median: the ledger holds one row per run
            # and a run with forty nodes would otherwise speak forty times louder than one with two
            # about a question -- "does this regime work here" -- that is asked once per run.
            summary[regime] = {"runs": len(medians), "median_of_medians": round(
                statistics.median(medians), 6), "nodes": sum(counts)}
    return {"task_id": task_id, "here": summary,
            "untried_here": [r for r in REGIMES if r not in summary],
            "elsewhere": sorted(elsewhere, key=lambda e: -(e.get("nodes") or 0))[:8]}


# The label the propose prior carries. Named here rather than at the call site so the instrument
# that counts whether a proposal CITED this block has one string to look for.
REGIME_PRIOR_LABEL = "Implementation regimes measured"


def regime_prior_line(rows: list, task_id: str, *, elsewhere_limit: int = 2) -> tuple:
    """`(text, receipt)` for the propose prior — evidence about regimes, and never an instruction.

    Empty text when the ledger says nothing about anything, because a prior that renders a header
    over "no data" spends a slot to say so.

    THE WORDING IS THE FIX, and this is the third time this bench has written that down (§342,
    §410). Three clauses, in this order:

      * what THIS task measured, with its sample -- the only part that is evidence about the task
        the proposer is working on;
      * what nobody has tried here -- §419's finding, because `pde_heat1d` ran 17 of 18 nodes as
        `jit` and has no comparison at all, and "nobody has tried compiled here" invites a check
        while "a compiled kernel is worth 6x" instructs and is false there;
      * what OTHER tasks measured, each naming its own task and sample, so a number can never be
        read as a general law. §110 is the record of that sentence being read as one.

    There is no recommendation clause and there is no superlative. The proposer is being handed a
    measurement, not a verdict, and the difference is the whole reason the module exists.
    """
    known = known_regimes(rows, task_id)
    here, elsewhere = known["here"], known["elsewhere"]
    if not here and not elsewhere:
        return "", {}
    parts = []
    if here:
        measured = "; ".join(
            f"{regime} (median {stats['median_of_medians']:.4g} over {stats['nodes']} node(s) "
            f"in {stats['runs']} run(s))"
            for regime, stats in sorted(here.items(), key=lambda kv: -kv[1]["median_of_medians"]))
        parts.append(f"{REGIME_PRIOR_LABEL} on this task: {measured}.")
        if known["untried_here"]:
            parts.append("Never tried here: " + ", ".join(known["untried_here"]) + ".")
    else:
        parts.append(f"{REGIME_PRIOR_LABEL} on this task: none yet.")
    for other in elsewhere[:elsewhere_limit]:
        ratio = other.get("ratio")
        if other.get("best") and other.get("worst") and isinstance(ratio, (int, float)):
            parts.append(f"On {other['task_id']}: {other['best']} over {other['worst']} "
                         f"{ratio:.3g}x ({other.get('nodes', 0)} nodes).")
    return " ".join(parts), {"here": here, "untried_here": known["untried_here"],
                             "elsewhere": [e.get("task_id", "") for e in elsewhere[:elsewhere_limit]]}
