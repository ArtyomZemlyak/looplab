"""Pure triage/fingerprint helpers for the engine loop (extracted from orchestrator.py):
workspace drift fingerprinting (`_dir_fingerprint` / `_shallow_fingerprint`), failure
classification (`_failure_reason`), the anti-stuck error normalizer (`_normalize_error_sig`),
the deterministic crash-triage fallback (`_rule_triage` + `_MECHANICAL_MARKERS`), the env-prep
round bound (`_MAX_DEP_ROUNDS`), and the D1 holdout partition (`_holdout_indices`). All are
pure module-level functions/constants — no engine state, no event-log writes — so they stay
trivially replay-safe. The orchestrator re-exports them under the same names for back-compat
(tests import e.g. `looplab.engine.orchestrator._normalize_error_sig`)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


def _dir_fingerprint(path) -> str:
    """git HEAD SHA if `path` is (inside) a git repo, else a sha256 over sorted
    (relpath, size, mtime_ns) — cheap and deterministic, catches edits/adds/removes without
    reading file contents. A missing path fingerprints as 'absent'."""
    import subprocess
    p = Path(path)
    if not p.exists():
        return "absent"
    try:
        r = subprocess.run(["git", "-C", str(p), "rev-parse", "HEAD"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0 and r.stdout.strip():
            return "git:" + r.stdout.strip()
    except OSError:
        pass
    if p.is_file():
        st = p.stat()
        return f"file:{st.st_size}:{st.st_mtime_ns}"
    h = hashlib.sha256()
    for f in sorted(p.rglob("*")):
        if f.is_file() and ".git" not in f.parts:
            st = f.stat()
            h.update(f.relative_to(p).as_posix().encode())
            h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
    return "hash:" + h.hexdigest()[:16]


def _shallow_fingerprint(path) -> str:
    """Cheap signature for large/immutable mounts (data, references): git HEAD if it's a git
    repo, else a single os.scandir of the TOP level (entry count + max mtime) — O(top-level),
    never a recursive walk. Catches add/remove/replace at the root; deep edits to immutable
    inputs aren't the resume-drift concern (the editable repos are, and those are deep-hashed)."""
    import subprocess
    p = Path(path)
    if not p.exists():
        return "absent"
    try:
        r = subprocess.run(["git", "-C", str(p), "rev-parse", "HEAD"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0 and r.stdout.strip():
            return "git:" + r.stdout.strip()
    except OSError:
        pass
    if p.is_file():
        st = p.stat()
        return f"file:{st.st_size}:{st.st_mtime_ns}"
    n, newest = 0, 0
    with os.scandir(p) as it:
        for e in it:
            n += 1
            try:
                newest = max(newest, e.stat(follow_symlinks=False).st_mtime_ns)
            except OSError:
                pass
    return f"dir:{n}:{newest}"


def _failure_reason(res) -> str:
    """Classify why an eval produced no usable metric, so the audit trail distinguishes a
    crash from a timeout from a missing-deps setup failure from a drift rejection from a clean
    run that simply printed no metric. Ordered most-specific first. (The "idea_rejected" reason
    is NOT classified here — it is set by `_evaluate` when the crash-triage agent judges the idea
    fundamentally wrong, which then steers `debug_action` away from that lineage.)"""
    if getattr(res, "drift", None) is not None:
        return "drift"
    if res.timed_out:
        return "timeout"
    if (res.stderr or "").startswith("setup failed:"):
        return "setup"
    if res.exit_code != 0:
        # OOM-kill: on a memory-capped pod (a JupyterHub cgroup limit) the kernel SIGKILLs a too-big
        # eval — exit -9 (POSIX, Python returns -signal) or 137 (128+9) — with little/no Python
        # traceback. Distinguish it from an ordinary crash so it's triaged as REPAIRABLE (reduce
        # memory: batch/model size, subsample) instead of a silent abandon that recurs on every heavy
        # eval. Heuristic: the SIGKILL signature with no real traceback in stderr (a timeout-kill is
        # also SIGKILL but `res.timed_out` already returned "timeout" above, so it never reaches here).
        if res.exit_code in (-9, 137) and "Traceback" not in (res.stderr or ""):
            return "oom"
        return "crash"
    return "no_metric"          # exit 0 but no parseable metric emitted


def _normalize_error_sig(err: str) -> str:
    """T10: normalize an error before the anti-stuck compare — strip memory addresses, line
    numbers, absolute paths and numeric literals so two SEMANTICALLY-identical errors (same
    exception, same message shape) match even when incidental details differ. The exact-match
    compare missed e.g. the same shape-mismatch recurring with different tensor sizes."""
    import re
    s = " ".join((err or "").strip().split())
    s = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", s)
    s = re.sub(r"line \d+", "line N", s)
    s = re.sub(r"(?:[A-Za-z]:)?[/\\][^\s'\":,)]+", "/PATH", s)
    s = re.sub(r"\d+(?:\.\d+)?(?:e[+-]?\d+)?", "N", s)
    return s[-160:]


def _holdout_indices(n: int, fraction: float) -> frozenset:
    """D1: the deterministic holdout partition over n host-held labels. A pure function of
    (n, fraction) — identical on every resume/replay with no state to persist.

    Reserves an EXACT count = round(fraction·n) rows (clamped to [1, n-1] whenever fraction>0), so
    the holdout size is controlled even for small n — a per-index Bernoulli threshold would leave
    the count uncontrolled (e.g. n=4, frac=0.25 could reserve 0/2/3 rows), making the champion-
    selecting 'unseen signal' noisy on exactly the small-data tasks where it matters most. Which
    rows are chosen is spread deterministically through the label order by Knuth multiplicative
    hashing (no head/tail bias if the data is sorted)."""
    if float(fraction) <= 0 or n < 2:
        return frozenset()          # fraction 0 = holdout off; n<2 can't split without collapsing
    k = max(1, min(n - 1, int(round(float(fraction) * n))))   # exact reserved count, non-degenerate
    # Pick the k rows with the smallest hash — a stable, uniform, deterministic selection.
    ranked = sorted(range(n), key=lambda i: (((i * 2654435761) & 0xFFFFFFFF), i))
    return frozenset(ranked[:k])


# Env-prep: max auto-install + re-run rounds per node before giving up (a re-run can reveal a
# *second* missing lib; bound it so an odd install state can't loop). The `_dep_attempted` cache
# already prevents re-attempting the same module (one pip attempt per module per run, success or fail).
_MAX_DEP_ROUNDS = 6

# Mechanical-failure signatures: a crash whose stderr matches one of these is almost always a
# code/runtime defect (bad import, removed/renamed API, typo) — repairable in place from the
# traceback alone. Used by the deterministic crash-triage fallback when no LLM agent is wired.
_MECHANICAL_MARKERS = (
    "ImportError", "ModuleNotFoundError", "NameError", "AttributeError", "SyntaxError",
    "IndentationError", "TypeError", "unexpected keyword argument", "has no attribute",
    "is not defined", "no attribute",
)


def _rule_triage(reason: str, error: str, attempt: int, max_attempts: int) -> dict:
    """Deterministic crash-triage fallback (no LLM): repair a clear MECHANICAL crash — or a TIMEOUT
    (too slow, not a wrong idea -> reduce compute) — while attempts remain, otherwise abandon.
    Conservatively NEVER returns "reject_idea" — killing a whole idea lineage is a strong call
    reserved for the LLM agent, so the rule path stays safe with the unified agent off (it only ever
    repairs obvious mechanical crashes / timeouts or abandons)."""
    err = error or ""
    if reason in ("timeout", "oom") and attempt <= max_attempts:
        why = ("timeout — reduce compute to fit the budget (rule-based)" if reason == "timeout"
               else "OOM-killed — reduce memory: batch/model size or subsample to fit the pod limit (rule-based)")
        return {"action": "repair", "rationale": why}
    mechanical = any(s in err for s in _MECHANICAL_MARKERS)
    if reason == "crash" and mechanical and attempt <= max_attempts:
        return {"action": "repair", "rationale": "mechanical crash (rule-based)"}
    return {"action": "abandon", "rationale": "non-mechanical failure or attempts exhausted (rule-based)"}
