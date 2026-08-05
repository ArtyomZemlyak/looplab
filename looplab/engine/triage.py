"""Pure triage/fingerprint helpers for the engine loop (extracted from orchestrator.py):
workspace drift fingerprinting (`_dir_fingerprint` / `_shallow_fingerprint`), failure
classification (`_failure_reason`), the anti-stuck error normalizer (`_normalize_error_sig`),
the repair-class contract (`REPAIR_CLASSES` / `coerce_repair_class` / `_environment_failure`),
the deterministic crash-triage fallback (`_rule_triage` + `_MECHANICAL_MARKERS`), the env-prep
round bound (`_MAX_DEP_ROUNDS`), and the D1 holdout partition (`_holdout_indices`). All are
pure module-level functions/constants — no engine state, no event-log writes — so they stay
trivially replay-safe. The orchestrator re-exports them under the same names for back-compat
(tests import e.g. `looplab.engine.orchestrator._normalize_error_sig`)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

# Both fingerprinters shell out to `git rev-parse` and both run on setup AND on every resume, so
# neither may block the run on a wedged mount. Short on purpose: a healthy repo answers in
# milliseconds, and the fallback (stat/scandir) is a fine fingerprint on its own.
_GIT_TIMEOUT_S = 10.0


def _dir_fingerprint(path) -> str:
    """git HEAD SHA if `path` is (inside) a git repo, else a sha256 over sorted
    (relpath, size, mtime_ns) — cheap and deterministic, catches edits/adds/removes without
    reading file contents. A missing path fingerprints as 'absent'."""
    import subprocess
    from looplab.runtime.sandbox import git_subprocess_env
    p = Path(path)
    if not p.exists():
        return "absent"
    # BOUNDED like every other git call in the engine: a `rev-parse` on a wedged FUSE/network mount
    # hangs forever otherwise, and this runs on setup AND on every resume, so it would block the run
    # with no diagnostic. A timeout just falls through to the stat-based fingerprint below.
    try:
        r = subprocess.run(["git", "-C", str(p), "rev-parse", "HEAD"], timeout=_GIT_TIMEOUT_S,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=git_subprocess_env())
        if r.returncode == 0 and r.stdout.strip():
            return "git:" + r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
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
    from looplab.runtime.sandbox import git_subprocess_env
    p = Path(path)
    if not p.exists():
        return "absent"
    try:                                              # bounded — see _dir_fingerprint
        r = subprocess.run(["git", "-C", str(p), "rev-parse", "HEAD"], timeout=_GIT_TIMEOUT_S,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=git_subprocess_env())
        if r.returncode == 0 and r.stdout.strip():
            return "git:" + r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
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


# A bare or dotted Python identifier, as it appears INSIDE quotes in an exception message
# (`'ColPaliProcessor'`, `'models.glm4v.processing_glm4v'`). Written against the ALREADY-normalized
# text, so the digit-collapsing rule above it has turned `glm4v` into `glmNv` — `N` is a letter, so
# the class still matches. Deliberately anchored to identifier shape: quoted PROSE, a quoted command
# line or a quoted path (already `/PATH`, which cannot start an identifier) is left alone.
_QUOTED_IDENT = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"


def _normalize_error_sig(err: str) -> str:
    """T10: normalize an error before the anti-stuck compare — strip memory addresses, line
    numbers, absolute paths and numeric literals so two SEMANTICALLY-identical errors (same
    exception, same message shape) match even when incidental details differ. The exact-match
    compare missed e.g. the same shape-mismatch recurring with different tensor sizes.

    The identity of a failure is (exception class, message template, where it failed). Everything
    this function erases is an incidental OPERAND of that failure; everything it keeps is part of
    the identity. Concretely —

    ABSORBED: memory addresses; line numbers; filesystem paths and URLs; numeric literals; and —
    added after a live 3.5 h runaway — QUOTED IDENTIFIERS, i.e. the module / attribute / symbol /
    column name a message quotes. A `transformers` install left in a broken state failed every eval
    inside its lazy-import registry naming a DIFFERENT symbol each time
    (`'processing_llava_next'`, `'GraniteSpeechProcessor'`, `'ColPaliProcessor'`,
    `'MarkupLMProcessor'`, `'SplitModulelist'`, …). Those 2345 failures minted 369 distinct
    signatures with a longest identical RUN of 2, so the anti-stuck counter reset on nearly every
    attempt and never reached `inline_repair_stuck_repeat`; the node emitted 2345 `node_repaired`
    events over 3.5 h. Normalized this way the same 2345 collapse to 3 signatures.

    DELIBERATELY STILL DISTINGUISHED, so a node whose error genuinely moves because the agent is
    fixing it keeps its attempts:
      * the exception CLASS — `ModuleNotFoundError` / `ImportError` / `TypeError` are unquoted;
      * the message TEMPLATE — "No module named" vs "cannot import name … from" vs "object has no
        attribute" is unquoted prose and survives verbatim;
      * WHERE it failed — the 160-char tail keeps the last frames' function names (`in __getattr__`,
        `in <module>`, `in _dtensor_from_local_like`), so a repair that moves the failure into a
        different frame mints a NEW signature and the node keeps repairing;
      * quoted text that is not identifier-shaped (a quoted sentence, a quoted command line).
    """
    import re
    s = " ".join((err or "").strip().split())
    s = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", s)
    s = re.sub(r"line \d+", "line N", s)
    s = re.sub(r"(?:[A-Za-z]:)?[/\\][^\s'\":,)]+", "/PATH", s)
    s = re.sub(r"\d+(?:\.\d+)?(?:e[+-]?\d+)?", "N", s)
    # LAST, so every rule above sees its historical input and the pre-existing normalization of
    # unquoted text is byte-identical.
    s = re.sub(f"'{_QUOTED_IDENT}'", "'ID'", s)
    s = re.sub(f'"{_QUOTED_IDENT}"', '"ID"', s)
    return s[-160:]


def _holdout_indices(n: int, fraction: float, epoch: int = 0) -> frozenset:
    """D1: the deterministic holdout partition over n host-held labels. A pure function of
    (n, fraction, epoch) — identical on every resume/replay with no state to persist.

    Reserves an EXACT count = round(fraction·n) rows (clamped to [1, n-1] whenever fraction>0), so
    the holdout size is controlled even for small n — a per-index Bernoulli threshold would leave
    the count uncontrolled (e.g. n=4, frac=0.25 could reserve 0/2/3 rows), making the champion-
    selecting 'unseen signal' noisy on exactly the small-data tasks where it matters most. Which
    rows are chosen is spread deterministically through the label order by Knuth multiplicative
    hashing (no head/tail bias if the data is sorted).

    P0-2 freshly-hidden per-epoch holdout: `epoch` (RunState.search_epoch) SALTS the hash so a run
    reopened after finishing scores its new candidates on a DIFFERENT, never-disclosed partition
    instead of the already-revealed one ('already-seen exam'). `epoch=0` XORs with 0 -> the exact
    original selection, so a normal single-epoch run (and every existing log/golden replay) is
    byte-identical; only a reopened run (epoch>=1) rotates to a fresh split."""
    if float(fraction) <= 0 or n < 2:
        return frozenset()          # fraction 0 = holdout off; n<2 can't split without collapsing
    k = max(1, min(n - 1, int(round(float(fraction) * n))))   # exact reserved count, non-degenerate
    # Pick the k rows with the smallest hash — a stable, uniform, deterministic selection. The epoch
    # salt (0 for the first search) reshuffles which rows rank smallest per epoch; XOR 0 is identity.
    salt = (int(epoch) * 0x9E3779B1) & 0xFFFFFFFF
    ranked = sorted(range(n), key=lambda i: ((((i * 2654435761) ^ salt) & 0xFFFFFFFF), i))
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

# --- The repair-class contract -----------------------------------------------------------------
# WHAT a repair spends, as opposed to whether to repair at all. Two repairs can be equally
# "mechanical" and still be completely different resources:
#
#   "environment" — the repair only RECONCILES the authored code with the installed environment: a
#     removed/renamed library API, a moved import, an absent optional dependency, a major-version
#     migration. The experiment is unchanged; the node is paying off a debt it did not incur (a
#     year-stale `requirements.txt` against today's site-packages).
#   "experiment"  — everything else: the node's own logic, a modelling decision, a too-slow or
#     too-hungry run. This is the repair the operator budgeted for.
#
# Measured on `runs/rubert-dr-0805` node 0 (`inline_repair_attempts: 6`): SIX consecutive repairs
# were PL-2.x / transformers / accelerate migrations — the triage rationale said "mechanical" every
# single time — and the first genuine research question (a DDP `find_unused_parameters` modelling
# decision) arrived with the budget already spent. A repo whose pinned deps are a year stale could
# therefore never reach its own research question: every node re-pays the same migration.
# `engine/evaluate.py` apportions the budget on this class — see the ledger comment there for how
# it stays bounded (the pathological 2345-repair runaway is bounded by the SAME guards, because an
# environment repair still has to move the failure forward to be charged as one).
#
# Registry: the SINGLE spelling of the class vocabulary. It is a duck-typed seam across three
# sites — the triage agent's emit schema (`agents/unified_agent.py::triage_crash`), the engine's
# coercion of the agent's answer (`engine/crash_repair.py::_triage_crash`) and the deterministic
# fallback below — so a typo'd literal would silently disable the apportionment. Adding a class
# means updating this tuple and `tests/test_repair_budget_apportionment.py`, which scans the
# schema against it.
REPAIR_CLASSES = ("environment", "experiment")
# Fail-closed default: an absent, malformed or unknown class charges the EXPERIMENT budget, so a
# model that never fills the field (or a log written before the field existed) behaves exactly as
# the engine did before the split.
DEFAULT_REPAIR_CLASS = "experiment"


def coerce_repair_class(value) -> str:
    """Normalize a triage-supplied repair class to a member of `REPAIR_CLASSES` (fail-closed to
    `DEFAULT_REPAIR_CLASS`). One spelling of "is this a real class?" for every reader."""
    v = str(value or "").strip().lower()
    return v if v in REPAIR_CLASSES else DEFAULT_REPAIR_CLASS


# Environment-reconciliation signatures — the ENGINE's own reading of the traceback, used to
# corroborate the agent's `repair_class` (and to classify at all on the rule path). Deliberately
# narrow: the exemption it unlocks is a second budget, so it answers "environment" only for the
# shapes where the code and the INSTALLED library provably disagree.
#   * an import/module failure of any wording — the classic moved-or-absent module;
#   * a REMOVAL/deprecation announcement, which is a library telling you its API changed under you
#     (`NotImplementedError: Support for 'validation_epoch_end' has been removed in v2.0.0`);
#   * an unresolved name or a signature mismatch RAISED INSIDE AN INSTALLED PACKAGE — the frame is
#     in site-packages, so the disagreement is with the library's own code, not with the node's.
# Everything else — including a RuntimeError raised inside a library, e.g. DDP's
# `find_unused_parameters` complaint, which is a modelling decision wearing a library's voice —
# is experiment work.
_ENV_IMPORT_MARKERS = ("ModuleNotFoundError", "ImportError", "No module named",
                       "cannot import name")
_ENV_REMOVAL_MARKERS = ("has been removed in", "was removed in", "no longer exists",
                        "no longer supported", "is no longer available", "has been renamed")
_ENV_API_MARKERS = ("is not defined", "has no attribute", "unexpected keyword argument",
                    "missing 1 required positional argument", "takes no arguments",
                    "got multiple values for")
_INSTALLED_FRAME = ("site-packages", "dist-packages")


def _environment_failure(error: str) -> bool:
    """True iff the traceback shows the code disagreeing with the INSTALLED environment rather than
    with itself. Pure and conservative — see `_ENV_*` above for the three admitted shapes."""
    err = error or ""
    if any(m in err for m in _ENV_IMPORT_MARKERS) or any(m in err for m in _ENV_REMOVAL_MARKERS):
        return True
    return (any(m in err for m in _INSTALLED_FRAME)
            and any(m in err for m in _ENV_API_MARKERS))


def _rule_triage(reason: str, error: str, attempt: int, max_attempts: int) -> dict:
    """Deterministic crash-triage fallback (no LLM): repair a clear MECHANICAL crash — or a TIMEOUT
    (too slow, not a wrong idea -> reduce compute) — while attempts remain, otherwise abandon.
    Conservatively NEVER returns "reject_idea" — killing a whole idea lineage is a strong call
    reserved for the LLM agent, so the rule path stays safe with the unified agent off (it only ever
    repairs obvious mechanical crashes / timeouts or abandons).

    Carries the same `repair_class` contract as the agent path (its OWN reading of the traceback,
    which is also what corroborates the agent's answer), so budget apportionment does not silently
    switch off when no triage model is wired. A timeout/OOM is always experiment work: the code was
    too slow or too big for the budget, which is the node's own decision, not the environment's."""
    err = error or ""
    if reason in ("timeout", "oom") and attempt <= max_attempts:
        why = ("timeout — reduce compute to fit the budget (rule-based)" if reason == "timeout"
               else "OOM-killed — reduce memory: batch/model size or subsample to fit the pod limit (rule-based)")
        return {"action": "repair", "rationale": why, "repair_class": "experiment"}
    mechanical = any(s in err for s in _MECHANICAL_MARKERS)
    cls = "environment" if _environment_failure(err) else DEFAULT_REPAIR_CLASS
    if reason == "crash" and mechanical and attempt <= max_attempts:
        return {"action": "repair", "rationale": "mechanical crash (rule-based)", "repair_class": cls}
    return {"action": "abandon", "repair_class": cls,
            "rationale": "non-mechanical failure or attempts exhausted (rule-based)"}
