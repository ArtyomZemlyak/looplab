"""Host-side official MLE-bench grading.

The candidate (a sandboxed process) writes only ``submission.csv``; the HOST scores it with
mle-bench's *real* competition grader against the held-out ``private/test.csv`` answers — which
live in the mle-bench data dir and are NEVER copied into the candidate workspace. This is the
out-of-process / host-side grading the trust model (B1) requires, specialised to the official
benchmark: the number the search optimises is the genuine MLE-bench metric, plus the medal
thresholds derived from the real competition leaderboard.

Used two ways:
  * :func:`grade` — in-process (host) grading, returns the full report dict.
  * :func:`grade_in_subprocess` — what the engine calls: runs grading in a child process (so a
    grader crash / hang can't take down the orchestrator, and the heavy pandas/sklearn import
    stays out of the engine process), returning ``(metric, report)``.
  * ``python -m looplab.mlebench_grade -c <id> -s <submission.csv> [--data-dir D]`` — prints one
    JSON line ``{"metric": <score|null>, "report": {…}}``; the child entrypoint.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


def grade(competition_id: str, submission_path, data_dir: Optional[str] = None) -> dict:
    """Grade a submission CSV with mle-bench's real grader; return ``CompetitionReport.to_dict()``
    (``score`` is None for a missing/invalid submission). Imports mlebench lazily."""
    from mlebench.grade import grade_csv
    from mlebench.registry import registry
    reg = registry if not data_dir else registry.set_data_dir(Path(data_dir).resolve())
    comp = reg.get_competition(competition_id)
    report = grade_csv(Path(submission_path), comp)
    return report.to_dict()


def grade_in_subprocess(competition_id: str, submission_path, data_dir: Optional[str] = None,
                        *, timeout: float = 300.0) -> tuple[Optional[float], Optional[dict]]:
    """Run :func:`grade` in a child process. Returns ``(metric, report)``; ``(None, None)`` on
    non-zero exit, timeout, or unparseable output (the node then has no metric → it fails)."""
    from .sandbox import _run_argv
    argv = [sys.executable, "-m", "looplab.mlebench_grade",
            "-c", competition_id, "-s", str(submission_path)]
    if data_dir:
        argv += ["--data-dir", str(data_dir)]
    # cwd = repo root so `-m looplab.mlebench_grade` resolves; capped output, tree-kill on timeout.
    root = str(Path(__file__).resolve().parents[1])
    rc, out, _err, to = _run_argv(argv, root, timeout, None, 256_000)
    if rc != 0 or to:
        return None, None
    # Single pass: main() prints exactly one JSON object line carrying BOTH "metric" and "report".
    # Scan from the end for the last parseable JSON object that has a "metric" key and read both from
    # it (so a stray log line that merely contains the text "report" can't be mis-parsed).
    for line in reversed(out.splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "metric" in obj:
            m = obj.get("metric")
            return (float(m) if isinstance(m, (int, float)) and not isinstance(m, bool) else None,
                    obj.get("report"))
    return None, None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Grade a submission with mle-bench's real grader.")
    ap.add_argument("-c", "--competition-id", required=True)
    ap.add_argument("-s", "--submission", required=True, help="path to the submission CSV")
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args(argv)
    report = grade(args.competition_id, args.submission, args.data_dir)
    # One machine-readable line on stdout; `metric` is what the engine reads for selection.
    print(json.dumps({"metric": report.get("score"), "report": report}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
