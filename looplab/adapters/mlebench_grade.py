"""Host-side official MLE-bench grading.

The candidate (a sandboxed process) writes only ``submission.csv``; the HOST scores it with
mle-bench's *real* competition grader against the held-out ``private/test.csv`` answers — which
live in the mle-bench data dir and are NEVER copied into the candidate workspace. This is the
out-of-process / host-side grading the trust model (B1) requires, specialised to the official
benchmark: the number the search optimises is the genuine MLE-bench metric, plus the medal
thresholds derived from the real competition leaderboard.

Two protocols since 2026-09-06 (doc 52 §5.1 row 3). The SEARCH protocol grades the agent-invisible
split `adapters/mlebench_split.py` carves out of the public train rows, with the competition's OWN
grader (`--answers`), and never names the private answers during a run; the private grade lands ONCE,
at finish, for the search champion. The legacy protocol (`holdout_fraction=0`) grades every node on
the private answers and is recorded as such in `host_grading.protocol`.

Used two ways:
  * :func:`grade` — in-process (host) grading, returns the full report dict.
  * :func:`grade_in_subprocess` — what the engine calls: runs grading in a child process (so a
    grader crash / hang can't take down the orchestrator, and the heavy pandas/sklearn import
    stays out of the engine process), returning ``(metric, report)``.
  * ``python -m looplab.adapters.mlebench_grade -c <id> -s <submission.csv> [--data-dir D]`` — prints one
    JSON line ``{"metric": <score|null>, "report": {…}}``; the child entrypoint.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Optional


def percentile_rank(score, leaderboard_scores, *, lower_is_better: bool) -> Optional[float]:
    """The competition-leaderboard PERCENTILE RANK of `score` (doc 52 row 23): the share of
    leaderboard entries the submission BEATS, in percent, on the scale AIRA₂ and OpenAI report
    MLE-bench-30 on — so a LoopLab number can be read beside theirs. A tie is not a beat. `None`
    for an invalid score or an empty leaderboard; a leaderboard row that is not a finite number is
    dropped rather than counted as beaten."""
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
        return None
    values = []
    for raw in leaderboard_scores or []:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)
    if not values:
        return None
    beaten = sum(1 for v in values if (score < v if lower_is_better else score > v))
    return round(100.0 * beaten / len(values), 3)


def _leaderboard_scores(comp) -> tuple[list, Optional[bool]]:
    """The competition's leaderboard scores and its direction, off the same `get_leaderboard` the
    medal thresholds come from; `([], None)` when the leaderboard cannot be read."""
    try:
        from mlebench.data import get_leaderboard

        board = get_leaderboard(comp)
        lower = bool(comp.grader.is_lower_better(board))
        column = board["score"]
        scores = list(column.tolist() if hasattr(column, "tolist") else column)
        return scores, lower
    except Exception:  # noqa: BLE001 — a missing leaderboard costs the percentile, never the grade
        return [], None


def grade(competition_id: str, submission_path, data_dir: Optional[str] = None) -> dict:
    """Grade a submission CSV with mle-bench's real grader; return ``CompetitionReport.to_dict()``
    (``score`` is None for a missing/invalid submission) plus the leaderboard `percentile` rank and
    `leaderboard_size` (doc 52 row 23; `None`/0 when the leaderboard is unreadable). Imports
    mlebench lazily."""
    from mlebench.grade import grade_csv

    from looplab.adapters.mlebench_real import _competition

    # One registry/data-dir resolution (doc 25 RA-10). Grading against a differently-resolved data
    # dir than the one the task was prepared under scores a submission with the wrong answers.
    comp = _competition(competition_id, data_dir)
    report = grade_csv(Path(submission_path), comp).to_dict()
    scores, lower = _leaderboard_scores(comp)
    report["percentile"] = (percentile_rank(report.get("score"), scores, lower_is_better=lower)
                            if lower is not None else None)
    report["leaderboard_size"] = len(scores)
    return report


def grade_search_split(competition_id: str, submission_path, answers_path,
                       data_dir: Optional[str] = None) -> Optional[float]:
    """Score a submission against the SEARCH split's answers (the private format, carved by
    `mlebench_split.carve`) with the competition's own grader.

    Grades through `grade_csv` on a copy of the competition whose `answers` is the split's file, so
    the submission validation and the metric are exactly the official ones; the medal thresholds the
    report derives from the leaderboard are about the private set and are discarded here — a split
    score is a search signal, not a report. Returns None for an invalid submission."""
    import copy

    from mlebench.grade import grade_csv

    from looplab.adapters.mlebench_real import _competition

    comp = _competition(competition_id, data_dir)
    split = copy.copy(comp)
    try:
        object.__setattr__(split, "answers", Path(answers_path))
    except (AttributeError, TypeError):
        split.answers = Path(answers_path)   # type: ignore[attr-defined]
    score = grade_csv(Path(submission_path), split).to_dict().get("score")
    ok = isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score)
    return float(score) if ok else None


def grade_search_split_in_subprocess(competition_id: str, submission_path, answers_csv: str,
                                     hidden_ids, data_dir: Optional[str] = None, *,
                                     timeout: float = 300.0) -> Optional[float]:
    """The engine's SEARCH-time call: the candidate's submission restricted to the hidden ids, and
    the answers text, are written into a private temp dir (mode 0700, removed after) and graded by
    the child. Only the hidden rows leave the workdir and the private answers are never named.

    On the subprocess tier that temp dir is as readable to a SIBLING candidate as the mle-bench data
    dir (holding `private/test.csv`) already is — the trust model's disclosed caveat, no wider — and
    the Docker tier sees neither. `(None)` for a missing/malformed submission, a grader failure or a
    timeout, exactly like `grade_in_subprocess`."""
    import shutil
    import tempfile

    from looplab.adapters.mlebench_split import filter_submission
    from looplab.runtime.sandbox import _last_json_dict, run_argv

    tmp = Path(tempfile.mkdtemp(prefix="looplab-search-grade-"))
    try:
        try:
            text = Path(submission_path).read_text(encoding="utf-8-sig", errors="replace")
            (tmp / "submission.csv").write_text(filter_submission(text, hidden_ids, keep=True),
                                                encoding="utf-8")
        except (OSError, ValueError):
            return None
        (tmp / "answers.csv").write_text(answers_csv, encoding="utf-8")
        argv = [sys.executable, "-m", "looplab.adapters.mlebench_grade",
                "-c", competition_id, "-s", str(tmp / "submission.csv"),
                "--answers", str(tmp / "answers.csv")]
        if data_dir:
            argv += ["--data-dir", str(Path(data_dir).resolve())]
        root = str(Path(__file__).resolve().parents[2])
        rc, out, _err, to = run_argv(argv, root, timeout, None, 256_000)
        if rc != 0 or to:
            return None
        obj = _last_json_dict(out, lambda o: "metric" in o)
        if obj is None:
            return None
        m = obj.get("metric")
        ok = isinstance(m, (int, float)) and not isinstance(m, bool) and math.isfinite(m)
        return float(m) if ok else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def grade_in_subprocess(competition_id: str, submission_path, data_dir: Optional[str] = None,
                        *, timeout: float = 300.0) -> tuple[Optional[float], Optional[dict]]:
    """Run :func:`grade` in a child process. Returns ``(metric, report)``; ``(None, None)`` on
    non-zero exit, timeout, or unparseable output (the node then has no metric → it fails)."""
    from looplab.runtime.sandbox import run_argv
    argv = [sys.executable, "-m", "looplab.adapters.mlebench_grade",
            "-c", competition_id, "-s", str(submission_path)]
    if data_dir:
        # Resolve to an absolute path HERE (host/engine cwd) — the child runs with cwd=repo root,
        # so passing a relative data_dir would let it resolve to a different dir than the engine did.
        argv += ["--data-dir", str(Path(data_dir).resolve())]
    # cwd = repo root so `-m looplab.adapters.mlebench_grade` resolves; capped output, tree-kill on timeout.
    root = str(Path(__file__).resolve().parents[2])
    rc, out, _err, to = run_argv(argv, root, timeout, None, 256_000)
    if rc != 0 or to:
        return None, None
    # Single pass: main() prints exactly one JSON object line carrying BOTH "metric" and "report".
    # Scan from the end for the last parseable JSON object that has a "metric" key and read both from
    # it (so a stray log line that merely contains the text "report" can't be mis-parsed).
    from looplab.runtime.sandbox import _last_json_dict
    obj = _last_json_dict(out, lambda o: "metric" in o)
    if obj is None:
        return None, None
    m = obj.get("metric")
    ok = isinstance(m, (int, float)) and not isinstance(m, bool) and math.isfinite(m)
    return (float(m) if ok else None, obj.get("report"))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Grade a submission with mle-bench's real grader.")
    ap.add_argument("-c", "--competition-id", required=True)
    ap.add_argument("-s", "--submission", required=True, help="path to the submission CSV")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--answers", default=None,
                    help="grade against THIS answers CSV (the search split) instead of the private "
                         "answers; the report is then omitted — a split score is a search signal")
    args = ap.parse_args(argv)
    if args.answers:
        metric = grade_search_split(args.competition_id, args.submission, args.answers, args.data_dir)
        print(json.dumps({"metric": metric, "report": None, "protocol": "search_split"}))
        return 0
    report = grade(args.competition_id, args.submission, args.data_dir)
    # One machine-readable line on stdout; `metric` is what the engine reads for selection.
    print(json.dumps({"metric": report.get("score"), "report": report}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
