"""C4 · Independent critic (ADR-7). A cheap, execution-free critic pass over a generated solution
BEFORE it's trusted: does the code plausibly do what the Idea claims, and is it not an obvious no-op?
Catches the failure modes a validator's syntax check misses — a model that returns a stub, hard-codes
the metric, or ignores the requested hyperparameters. Audit-only (surfaced in the Trust panel via the
same reward_hack_suspected event); never changes selection. Ties to B5.
"""
from __future__ import annotations

import os
import re

from .models import Idea


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
        computed = re.search(r'["\']?metric["\']?\s*[:=]\s*[A-Za-z_]', code) or \
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
        if pnames and not any(re.search(rf"\b{re.escape(p)}\b", code) for p in pnames):
            issues.append({"issue": "params_ignored",
                           "detail": f"none of the proposed params {pnames} are referenced in the code"})
    return issues
