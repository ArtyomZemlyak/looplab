"""C4 · Independent critic (ADR-7). A cheap, execution-free critic pass over a generated solution
BEFORE it's trusted: does the code plausibly do what the Idea claims, and is it not an obvious no-op?
Catches the failure modes a validator's syntax check misses — a model that returns a stub, hard-codes
the metric, or ignores the requested hyperparameters. Broad critic warnings remain advisory, while the
narrow ``critic:hardcoded_metric`` signal can exclude selection/breeding or block under
``trust_gate=gate|block``. Findings use the same ``reward_hack_suspected`` event as B5.
"""
from __future__ import annotations

import os
import re

from looplab.core.models import Idea


def critique(idea: Idea, code: str, *, submission_file: str | None = None) -> list[dict]:
    """Return a list of {issue, detail} the critic flags (empty == looks fine).

    `submission_file`: set when the run is graded OUT-OF-PROCESS by a host grader (MLE-bench, and
    any other `host_grader()` task). In that mode the candidate's output contract is to WRITE this
    file — the score is computed by the host from it and *replaces* any self-reported value, so the
    in-code `metric` checks are meaningless. Leaving them on false-positives on every submission
    that merely doesn't happen to use the word "metric" (e.g. an MLE-bench solution that writes
    submission.csv). When host-graded we therefore swap the metric checks for a check that the
    submission file is actually written. Left None for legacy in-workdir grading.
    """
    code = code or ""
    issues: list[dict] = []
    stripped = code.strip()
    if len(stripped) < 20:
        issues.append({"issue": "stub", "detail": "solution is suspiciously short / near-empty"})
        return issues

    if submission_file:
        # Out-of-process grading: the deliverable is the submission file, not an in-code metric.
        name = os.path.basename(str(submission_file).replace("\\", "/")) or str(submission_file)
        # Match the name on a token boundary, NOT as a bare substring: nearly every solution reads
        # `sample_submission.csv`, which *contains* "submission.csv" — a plain `in` test is therefore
        # always true and the check is dead. The lookbehind rejects the `_submission.csv` (and
        # `x.submission.csv`) case while still matching the real write target: `./submission.csv`,
        # `'submission.csv'`, `out/submission.csv`, etc.
        if name and not re.search(r"(?<![\w.\-])" + re.escape(name), code):
            issues.append({"issue": "no_submission_output",
                           "detail": f"code never references '{name}' — the host grader would have "
                                     "no submission to score"})
    else:
        # In-workdir grading: the solution must compute and emit the metric itself.
        if "metric" not in code:
            issues.append({"issue": "no_metric_output",
                           "detail": "code never references 'metric' — it may not emit the required score"})
        # Flag a literal metric value ({"metric": 0.95}) ONLY when nothing in the code assigns the
        # metric from a name/expression. Otherwise a legitimate `print(json.dumps({"metric": score}))`
        # — or a placeholder `{"metric": 0.0}` later overwritten with a computed value — false-positives.
        hardcoded = re.search(r'["\']metric["\']\s*:\s*[0-9.+\-eE]+\s*[}\)]', code)
        # Anchor the `metric` token with a left word boundary. Unanchored, the bare-name alternative
        # matched `metric` as a SUFFIX of any identifier — `is_symmetric = True` (also `asymmetric`,
        # `parametric`, `barometric`, `isometric`) makes `metric = T` match `computed`, so
        # `hardcoded and not computed` is False and the hard-coded-metric cheat slips the HARD
        # `critic:hardcoded_metric` gate (one throwaway `symmetric = x` line defeats it). The quoted
        # `hardcoded`/second-alt regexes already require quotes around `metric`, so only this bare form
        # needs the boundary. Together the two alternatives still admit every legit computed form: the
        # anchored first alt matches `{"metric": score}` and a bare `metric = score`, and the second alt
        # matches the bracket-assignment `result["metric"] = value`.
        computed = re.search(r'(?<![A-Za-z0-9_])["\']?metric["\']?\s*[:=]\s*[A-Za-z_]', code) or \
            re.search(r'\[\s*["\']metric["\']\s*\]\s*=\s*[A-Za-z_]', code)
        if hardcoded and not computed:
            issues.append({"issue": "hardcoded_metric",
                           "detail": "the metric appears to be a hard-coded constant, not computed"})

    # Requested hyperparameters should appear in the code; none appearing suggests a no-op that
    # ignores the proposal (the idea isn't actually implemented). Skipped for the `debug` operator:
    # its params describe a diagnostic or repair (e.g. verify_cuda, test_gru_forward) or are
    # inherited from the parent node being fixed — they are NOT modeling knobs that must be threaded
    # into the solution, so demanding they appear in the code is a category error.
    if (idea.operator or "") != "debug":
        pnames = [str(k) for k in (idea.params or {})]
        if pnames and not any(_param_is_referenced(p, code) for p in pnames):
            issues.append({"issue": "params_ignored",
                           "detail": f"none of the proposed params {pnames} are referenced in the code"})
    return issues


def _param_is_referenced(pname: str, code: str) -> bool:
    """Does the written surface mention this parameter — under EITHER of the two spellings a
    parameter legitimately has?

    A repo task's params are FLATTENED dotted paths (`train.training.learning_rate`), and the place
    they are set is a nested config file the Developer writes:

        train:
          training:
            learning_rate: 0.001

    That text does not contain the string `train.training.learning_rate` and never will, so the
    literal search reported `params_ignored` on a node that had implemented every single parameter.
    MEASURED on rubertlite-dr-unified-v6: it fired on nodes 2, 3, 4 and 6 — every node the check ran
    on — while `config.yaml` in each of their own `node.files` contained `learning_rate`,
    `batch_size`, `temperature` and `n_epochs` under their nested keys. A signal that fires on 4 of 4
    carries no information, and the cost is not neutral: it is in the operator's attention feed and
    in the durable trust record, and it teaches a reader to skim past the reward-hack channel, which
    is how the one real signal gets missed. (`params_ignored` is advisory — only
    `critic:hardcoded_metric` excludes a node from selection — so nothing was gated on it.)

    So the LEAF segment counts too. That is a weaker bar than the full path, deliberately and only
    just: the check already fires only when NOTHING matches, so with a dozen parameters it still
    takes a surface that mentions not one of their names to trip. A no-op solution does not
    accidentally contain `uniformity_weight`.

    REVIEW (mega-review 2026-08-13): true of rare leaves, false of the COMMON ones. The scan
    surface concatenates ALL of `node.files`, which for a repo task includes copied-in configs and
    training scripts that natively contain `learning_rate`/`batch_size`/`epochs`/`seed` — so a
    dotted param ending in any ubiquitous ML token now passes even when the Developer threaded
    nothing (counterexample executed: a genuinely-ignored `train.training.learning_rate` no longer
    fires because the repo's own config carries the token). The pre-fix check was 100%
    false-positive on this task shape, so the trade is defensible for an advisory signal — but the
    detector is now near-blind for exactly the params operators most often sweep, which is worth
    saying here and worth a value-aware check eventually."""
    if re.search(rf"\b{re.escape(pname)}\b", code):
        return True
    leaf = pname.rsplit(".", 1)[-1]
    return bool(leaf) and leaf != pname and bool(re.search(rf"\b{re.escape(leaf)}\b", code))


def critic_findings(idea, code: str, *, submission_file: str | None = None) -> list[dict]:
    """`critique`'s issues as gate-visible trust findings (doc 25 CT-10).

    The `critic:` namespace decides gating, not presentation: `critic:hardcoded_metric` EXCLUDES a
    node from selection while every other `critic:` issue stays advisory (`is_hard_signal`). It was
    assembled at the consumer; it belongs with the detector that produced the issue.
    """
    from looplab.trust.findings import CRITIC_NS, finding

    return [finding(CRITIC_NS + str(row["issue"]), row["detail"])
            for row in critique(idea, code, submission_file=submission_file)
            if row.get("issue")]
