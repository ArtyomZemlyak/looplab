"""Operators (I7/I11, ADR-6). The result-moving levers beyond draft/improve:

- merge  : an ensemble/merge node combining ≥2 parents (mean of numeric params) —
           the multi-parent DAG step (parent_ids has length ≥2).
- feature engineering : the CV KEEP/DROP rule over a candidate's own declared per-feature ledger
           (`parse_feature_cv` / `feature_engineering_verdicts`). Not an Idea builder — see the
           block above it for why the mechanical half of an FE step is the decision and not the
           code — and enforced at the eval boundary by `trust/cv.py::feature_cv_findings`.

`debug` — "a depth-bounded fix attempt on a *failed* node (re-propose from it)" — was listed here
and is GONE (F5, 2026-08-13). It never had a function in this module (the orchestrator's role calls
owned it, like draft/improve), and it has no producer anywhere now: a failure is repaired inside the
node that failed, bounded by `engine/repair_judgment.py` rather than by a node budget slot. See
`search/policy.py`'s block above `operator_yields` for the decision and why F8 had to land with it.

draft/improve live in the orchestrator's role calls; merge is purely mechanical
(no model needed) so it lives here as a function. The policy decides *when* each
operator fires; these functions decide *what* the resulting Idea is.
"""
from __future__ import annotations

from looplab.core.models import Idea, Node


def merge_idea(parents: list[Node]) -> Idea:
    """Mean-merge the numeric params of the parents into one new Idea."""
    keys: set[str] = set()
    for p in parents:
        keys |= set(p.idea.params)
    params: dict[str, float] = {}
    for k in sorted(keys):
        # Only mean-merge numerically-coercible values. A non-numeric param (free-form repo task)
        # would otherwise raise inside sum() and, because no node_created event is written, the
        # policy would re-issue the SAME merge every iteration — an infinite loop on resume.
        vals: list[float] = []
        for p in parents:
            if k in p.idea.params:
                try:
                    vals.append(float(p.idea.params[k]))
                except (TypeError, ValueError):
                    continue
        if vals:
            params[k] = round(sum(vals) / len(vals), 4)
    pids = ",".join(str(p.id) for p in parents)
    # a merge inherits the UNION of every parent. A bare durable Idea has unknown/absent
    # membership; an explicit zero delta is the only unambiguous way to preserve that union unchanged.
    return Idea(operator="merge", params=params, rationale=f"mean-merge of nodes {pids}",
                concept_mode="delta", concepts_added=[], concepts_removed=[])


# --------------------------------------------------------------------------------- feature engineering
#
# THE FEATURE-ENGINEERING OPERATOR (docs/BACKLOG.md §0.1 row 13). `Settings.feature_engineering`
# put a SENTENCE in the proposer's prompt — "KEEP a feature only if it improves CV; drop any that
# don't" — and nothing anywhere checked it: no operator here, no reader of any CV evidence, and the
# row that asked for it called the CV gate MANDATORY. An instruction to a model is not a gate.
#
# WHAT AN OPERATOR CAN OWN HERE, and why it is a DECISION rather than an `Idea` builder like
# `merge_idea`. The code an FE step produces is the Developer's, not an arithmetic mean of two
# parents' params — there is nothing mechanical for this module to construct. What IS mechanical, and
# is exactly the half the prompt was asked to be trusted with, is the KEEP/DROP rule over a per-feature
# CV comparison: given the candidate's own declared ledger, which engineered features survive? That
# rule lives here beside `merge_idea` for the same reason `merge_idea` does (no model needed), and
# `trust/cv.py::feature_cv_findings` is what ENFORCES it at the eval boundary — a node that kept a
# feature its own ledger says failed CV carries a `feature_cv:` finding, which under
# `Settings.trust_gate` gate/block excludes it from selection and breeding, and under the default
# `audit` surfaces it. Whether a flag CHANGES selection is `trust_gate`'s decision and never this
# module's.
#
# THE LEDGER IS THE CANDIDATE'S OWN CLAIM, and that is the point rather than a weakness: the
# evidence a node is flagged on is the number it published itself, so there is no heuristic about
# what counts as an engineered feature and no way to be wrong about a node that claimed nothing. A
# candidate that prints no ledger is not flagged — a STATED recall gap, in the safe direction, and
# the alternative (guessing at feature construction from an AST and hard-gating on the guess) is the
# mechanism-not-property shape this repo has been corrected for repeatedly.
#: The stdout marker one CV ledger row rides on: `FEATURE_CV {"feature": ..., "with": ..., ...}`.
#: A MARKER rather than a bare JSON line because the eval's stdout also carries the metric line and
#: whatever the candidate's libraries print; a reader that scanned every JSON object would pick up
#: rows nobody meant as a ledger.
FEATURE_CV_MARKER = "FEATURE_CV"


def parse_feature_cv(text: str) -> list[dict]:
    """The per-feature CV ledger a candidate declared on its stdout, as `{feature, with, without,
    std, n}` rows in source order.

    Total over junk: a malformed row, a missing side of the comparison, a non-finite number or a
    duplicate feature name is DROPPED rather than guessed at — an unreadable claim is not a claim,
    and the gate below may only ever act on what the candidate actually said.
    """
    import json

    from looplab.core.fitness import is_usable_metric
    rows: list[dict] = []
    seen: set[str] = set()
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line.startswith(FEATURE_CV_MARKER):
            continue
        payload = line[len(FEATURE_CV_MARKER):].strip()
        try:
            row = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        name = row.get("feature")
        if not isinstance(name, str) or not name.strip() or name in seen:
            continue
        with_, without = row.get("with"), row.get("without")
        if not (is_usable_metric(with_) and is_usable_metric(without)):
            continue
        std, n = row.get("std"), row.get("n")
        seen.add(name)
        rows.append({"feature": name.strip()[:200], "with": float(with_),
                     "without": float(without),
                     "std": float(std) if is_usable_metric(std) and float(std) >= 0 else 0.0,
                     "n": int(n) if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0})
    return rows


def feature_engineering_verdicts(rows, direction: str = "min") -> list[dict]:
    """THE OPERATOR'S RULE: which engineered features survive their own CV comparison.

    One verdict per ledger row — `{feature, keep, delta, rule, with, without}` — where `delta` is the
    improvement in the run's own direction (positive = the feature helped) and `rule` names which
    test decided it. The two measured sides ride along because every consumer that reports a verdict
    has to quote the numbers it was reached from (a gate that says "this failed CV" and cannot say
    against what is a verdict nobody can check):

    * `one_se` when the row declares a spread (`std` with `n >= 2`), so the acceptance test is this
      repo's OWN — `trust/gate.py::one_se_better`, the >1-SE rule that stops the search chasing seed
      luck. A feature is not kept for an improvement smaller than the noise of the fold it was
      measured on, which is the whole reason feature engineering is described as non-universal.
    * `strict` when it does not: a bare pair of numbers can only be compared strictly, and inventing
      a spread for it would be a stricter-looking gate resting on a number nobody measured.
    """
    from looplab.trust.gate import one_se_better
    out: list[dict] = []
    for row in rows or ():
        with_, without = float(row["with"]), float(row["without"])
        delta = (with_ - without) if direction == "max" else (without - with_)
        std, n = float(row.get("std") or 0.0), int(row.get("n") or 0)
        if std > 0.0 and n >= 2:
            keep, rule = one_se_better(with_, without, std, n, direction=direction), "one_se"
        else:
            keep, rule = delta > 0.0, "strict"
        out.append({"feature": row["feature"], "keep": bool(keep), "delta": delta, "rule": rule,
                    "with": with_, "without": without})
    return out
