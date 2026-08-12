"""Pure triage/fingerprint helpers for the engine loop (extracted from orchestrator.py):
workspace drift fingerprinting (`_dir_fingerprint` / `_shallow_fingerprint`), failure
classification (`_failure_reason`), the triage-verdict contract (`TRIAGE_ACTIONS` /
`coerce_triage_action` / `is_transport_failure_verdict`), the "did the repair CALL produce a repair"
predicate
(`repair_artifact_defect`), the deterministic crash-triage fallback (`_rule_triage` +
`_MECHANICAL_MARKERS`), the env-prep round bound (`_MAX_DEP_ROUNDS`), the judge re-ask bound
(`_TRIAGE_REASK_LIMIT`), and the D1 holdout partition
(`_holdout_indices`). All are pure module-level functions/constants — no engine state, no
event-log writes — so they stay trivially replay-safe. The orchestrator re-exports them under the
same names for back-compat (tests import e.g. `looplab.engine.orchestrator._rule_triage`).

REMOVED 2026-08-05, deliberately and with the incident that motivated it: `_normalize_error_sig`,
the error-signature normalizer the anti-stuck guard counted recurrences of, and with it the
`repair_class` / `_environment_failure` two-ledger apportionment. Both were heuristics standing in
for a judgement, and both were measured to fail in the direction that costs the most. The signature
collapsed ASCII quoted identifiers and nothing else, so the IDENTICAL registry-walk failure that
terminalized after 3 repairs with an ASCII symbol ran 1741 repairs with no terminal when the symbol
was Cyrillic — in a Russian-language NLP repo; a whitespace-only stderr normalized to the empty
string, which the guard read as an unconditional exemption; and provider prose carrying a varying
request id minted a fresh signature every attempt. The general failure is not the regex: a bound
that depends on the TEXT QUALITY of a program's error output is not a bound. The stopping decision
now belongs to the triage model (`engine/evaluate.py`, consulted once per attempt on the repair
history), with `inline_repair_attempts` as a hard operator backstop — and the two-ledger split is
gone because a budget is about time and money, not about whose fault a repair was."""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path

# `FAILURE_REASONS` is the closed vocabulary `_failure_reason` below classifies into, and importing
# it here re-exports it beside its classifier so the two read as one thing. It is DEFINED in
# `core/models.py` rather than here because `core/config.py` needs it for the `inline_repair_reasons`
# default, and core may not import from `engine`.
from looplab.core.models import FAILURE_REASONS  # noqa: F401

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
    # A declared-contract failure is its OWN reason. Both contract branches in
    # `runtime/command_eval.py::_run_stages` report `exit_code=0`, so without this they landed here
    # and a stage that failed its artifact or assertion contract was reported as "the command
    # printed no metric" — about a stage that had printed one. The literals are spelled out rather
    # than returned through the variable so the registry cross-check in
    # `tests/test_inline_repair_reason_coverage.py` can still derive this function's vocabulary from
    # its own source.
    _rows = [row for row in (getattr(res, "stages", None) or []) if isinstance(row, dict)]
    _last = str(_rows[-1].get("status") or "") if _rows else ""
    if _last == "expect_failed":
        return "expect_failed"
    if _last == "check_failed":
        return "check_failed"
    return "no_metric"          # exit 0 but no parseable metric emitted


# --- Did the repair CALL produce a repair at all? ----------------------------------------------
# `core/models.py::is_developer_error` answers this for ONE shape: LoopLab's own in-band
# "(developer error: …)" sentinel, whose only producer is `adapters/repo_developer.py`. A provider
# that answers a repair request with PROSE — a 502 page, a rate-limit notice, an apology — produces
# none of that, and the prose was committed as the node's code, re-materialized and re-evaluated.
# Measured on the real `_evaluate` under the error-signature guard this design replaced: prose
# carrying a varying request id makes the resulting SyntaxError quote the id, so every attempt
# minted a fresh signature and the loop ran 1568 repairs with NO terminal inside a 90 s wall (the
# same prose without an id stopped after 4). Prose that happens to PARSE — a comment-only or
# docstring-only answer — instead exits 0 with no metric, and the operator was told "the command
# printed no metric" rather than "your provider is answering with prose". Neither shape is a
# question about the failing NODE, so neither should have reached the node's repair budget.
#
# So the engine asks the artifact the one question it can answer without a model: is this the kind
# of thing it replaces? Deliberately NOT a content heuristic — no keyword scan, no "looks like an
# error message" — because the caller must be able to tell a dead provider from a truncated
# generation, which is what the two answers below are for.
_TRIVIAL_BODY = (ast.Pass,)


def repair_artifact_defect(code) -> str:
    """`""` when `code` could BE the repaired program it replaces; otherwise how it is not one.

      * ``"no_code"`` — it parses and its module body can never execute anything: empty, comments
        only, or nothing but string literals (a docstring, i.e. a quoted sentence). Such an artifact
        provably cannot print a metric, so it is not a repair under any reading, and the caller
        treats it exactly like `core/models.py::DEVELOPER_ERROR_PREFIX`, the Developer's own
        in-band crash sentinel.
      * ``"unparseable"`` — it is not Python at all. AMBIGUOUS on purpose: a dead provider's prose
        and a truncated generation look identical here, and abandoning a node on one truncated
        answer would be a regression. The caller keeps repairing and only bounds it.

    Pure and total — every parse failure is an answer, never a raise. Only meaningful for the
    whole-file `code` artifact; a repo/multi-file repair ships its work in `files` and the caller
    gates on that before asking.
    """
    if not isinstance(code, str) or not code.strip():
        return "no_code"
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        # ValueError covers a NUL byte in the source; the two resource errors cover pathological
        # nesting. All three mean the same thing to the caller: not Python.
        return "unparseable"
    for stmt in tree.body:
        if isinstance(stmt, _TRIVIAL_BODY):
            continue
        if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)):
            continue          # a module docstring — or a provider's sentence in triple quotes
        return ""
    return "no_code"


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

# How many EXTRA times the engine re-asks a judge that did not produce a readable verdict, before it
# acts on the non-answer. 1 = two asks in total.
#
# It exists because the two things this bounds are asymmetric. Acting on the FIRST non-answer costs a
# whole node for free: measured on the shipped loop, a single `ConnectionError` on attempt 1 stopped
# the node with `developer.repair` calls = 0 — a flapping provider (one 502, one dropped socket) and
# a live model that emitted one out-of-enum verdict both ended the node before a single repair had
# been tried. A re-ask is one triage call, which is the cheapest call in the loop and strictly
# cheaper than the eval it is deciding whether to repeat. Not larger than 1 because the LLM client
# underneath already runs its own transport retry ladder (`core/llm.py::_RETRY_POLICY`), so each ask
# here is an entire retried request, not a bare socket attempt: a genuinely dead endpoint still
# reaches the circuit breaker within one extra round-trip rather than being drip-fed retries.
#
# This is the "minimum attempts floor" in the only form that is safe. A floor spelled as "always
# allow N repairs before the judge may stop the node" would repair BLIND — which is precisely the
# behaviour the 2345-repair incident consisted of — so the floor is on the JUDGE's answer, not on the
# repairs.
_TRIAGE_REASK_LIMIT = 1

# Mechanical-failure signatures: a crash whose stderr matches one of these is almost always a
# code/runtime defect (bad import, removed/renamed API, typo) — repairable in place from the
# traceback alone. Used by the deterministic crash-triage fallback when no LLM agent is wired.
_MECHANICAL_MARKERS = (
    "ImportError", "ModuleNotFoundError", "NameError", "AttributeError", "SyntaxError",
    "IndentationError", "TypeError", "unexpected keyword argument", "has no attribute",
    "is not defined", "no attribute",
)

# --- The triage-verdict contract ---------------------------------------------------------------
# WHETHER TO KEEP REPAIRING THIS NODE. The inline-repair loop consults the triage model once per
# attempt, and its answer IS the stopping rule (`engine/evaluate.py`), backstopped by the operator's
# hard `inline_repair_attempts` cap. It replaced two heuristics that both failed measurably: an
# error-signature recurrence counter (defeated by any error text it did not happen to normalize —
# Cyrillic symbols, a blank stderr, a varying request id) and the environment/experiment
# apportionment that spent one of two ledgers depending on whose fault a repair was. A budget is
# about time and money; the model that reads the failure is the thing that can say "I no longer know
# how to fix this".
#
#   "repair"       — the idea is sound and the model knows what to change next.
#   "abandon"      — stop repairing this node. The model's own "I do not know how to fix this any
#                    more", and also what a circling repair history should produce.
#   "reject_idea"  — stop, and mark the whole lineage as wrong, not just this node.
#   "unanswerable" — THE TRANSPORT FAILED. The judge was wired and the REQUEST NEVER COMPLETED: the
#                    call raised, the endpoint was unreachable, the request was refused. Not a
#                    judgement about the node — it is the dead-provider
#                    condition the developer-crash circuit breaker exists for, so the engine routes
#                    it there (terminal + RUN-level pause naming the provider).
#   "unreadable"   — THE MODEL ANSWERED SOMETHING THE ENGINE DOES NOT RECOGNISE. An out-of-enum
#                    action, an empty one, a non-dict, a missing key — including the literal string
#                    "unanswerable" arriving from the wire, and including NO EMIT AT ALL (the tool
#                    loop ran to its prose/turn/wall-clock bound and returned without one). The
#                    provider is demonstrably alive (it
#                    produced bytes), so this is a per-NODE stop with no pause: the node terminalizes
#                    like an `abandon` and the run keeps going.
#
# THE LINE BETWEEN THEM IS MECHANICAL, NOT DESCRIPTIVE, and that is the whole of the enforcement:
# `drive_tool_loop` RAISING is `unanswerable`, `drive_tool_loop` RETURNING its no-emit fallback is
# `unreadable`. "The loop never emitted" sat on the `unanswerable` side until 2026-08-06 and was
# reachable from a healthy endpoint — a local server that ignores `tool_choice` plus a model that
# answers in prose ends the loop with no emit after four SUCCESSFUL requests, and that paused the
# whole run. See `agents/unified_agent.py::triage_crash`'s two fallbacks.
#
# WHY THOSE ARE TWO VERDICTS AND NOT ONE. They used to be one, with `unanswerable` as the fail-closed
# default for every unparseable answer, and the collapse was a measured defect in both directions:
#   * a healthy model emitting ONE out-of-enum verdict on a SyntaxError in its own generated code
#     produced `node_failed reason='developer_crash'` plus a run-level pause carrying `node_id=None`
#     — not clearable by a node reset, and under `eval_parallel > 1` it took every healthy in-flight
#     sibling down with it, all on the strength of one bad emit;
#   * the pause reason told the operator to check credits, key and base URL — advice derived from the
#     MODEL'S OWN rationale, on an endpoint that was answering fine.
# Only the first of the two is a provider outage, and only a provider outage should stop the run.
# Excluding `unanswerable` from `AGENT_TRIAGE_ACTIONS` could never enforce that on its own, because
# the fail-closed default WAS `unanswerable`: every rejected wire value became the very verdict the
# exclusion existed to keep off the wire.
#
# Registry: the SINGLE spelling of the verdict vocabulary. It is a duck-typed seam across three
# sites — the triage agent's emit schema (`agents/unified_agent.py::triage_crash`), the engine's
# coercion of the agent's answer (`engine/crash_repair.py::_triage_crash`) and the deterministic
# fallback below — so a typo'd literal would silently turn a stop into "keep going". Adding a
# verdict means updating this tuple and `tests/test_repair_stop_decision.py`, which scans the
# schema against it.
UNANSWERABLE_TRIAGE_ACTION = "unanswerable"
UNREADABLE_TRIAGE_ACTION = "unreadable"
TRIAGE_ACTIONS = ("repair", "abandon", "reject_idea", UNANSWERABLE_TRIAGE_ACTION,
                  UNREADABLE_TRIAGE_ACTION)
# The verdicts a MODEL may emit. Both engine verdicts are absent: a model that emitted `unanswerable`
# would be asserting its own unreachability, and one that emitted `unreadable` would be asserting the
# engine could not read it.
AGENT_TRIAGE_ACTIONS = ("repair", "abandon", "reject_idea")
# Fail-closed default, and it is `unreadable`, NOT `unanswerable` and emphatically NOT "repair".
# Defaulting a verdict nobody could parse to "keep spending" is precisely how a dead provider produced
# 2345 in-node repairs on one node. Defaulting it to `unanswerable` is the mirror-image error: it
# accuses a provider that just answered, and halts a whole run over one malformed emit. "Nobody could
# read this" and "nobody was there" are different facts, and this default is the first of them.
DEFAULT_TRIAGE_ACTION = UNREADABLE_TRIAGE_ACTION

# The key an ENGINE-SIDE caller stamps on a verdict to report that it observed the transport fail —
# `agents/unified_agent.py::triage_crash`'s `_fallback`, which is where `resilient` hands control when
# the pilot loop could not complete. It is the ONLY way `unanswerable` reaches the engine from a
# return value, and it is unforgeable from the wire BY CONSTRUCTION: the model answers through a
# JSON-schema tool call whose properties are action/rationale/missing_dependency, and `_finalize`
# rebuilds the returned dict from exactly those three, so no model output can ever set this key.
TRIAGE_TRANSPORT_FAILURE_KEY = "transport_failure"


def is_transport_failure_verdict(out) -> bool:
    """Did the CALLEE report that the transport failed, as opposed to answering something odd?

    The one place that question is spelled, because getting it wrong in either direction is
    expensive: read a live model's confused emit as a transport failure and one bad answer pauses the
    run; read a real outage as a confused emit and the loop keeps spending on an endpoint that is
    gone. Requires BOTH the engine-side marker and the matching action, so a duck-typed researcher
    has to opt in deliberately rather than by echoing a string."""
    return (isinstance(out, dict)
            and out.get(TRIAGE_TRANSPORT_FAILURE_KEY) is True
            and str(out.get("action", "")).strip().lower() == UNANSWERABLE_TRIAGE_ACTION)


def coerce_triage_action(value) -> str:
    """Normalize a triage-supplied action to a member of `AGENT_TRIAGE_ACTIONS`, failing closed to
    `DEFAULT_TRIAGE_ACTION` ("unreadable"). One spelling of "is this a real verdict?" for every
    reader — and the one place that refuses to invent a permissive answer for a malformed one.

    Note what it does with the literal string "unanswerable": it REJECTS it, like any other
    non-verdict. This function is the enforcement point of "`unanswerable` is engine-minted only" —
    the vocabulary tuple only documents it. A transport failure never travels as a bare action
    string; it carries `TRIAGE_TRANSPORT_FAILURE_KEY` and is recognised by
    `is_transport_failure_verdict` before this is ever consulted."""
    v = str(value or "").strip().lower()
    return v if v in AGENT_TRIAGE_ACTIONS else DEFAULT_TRIAGE_ACTION


def _rule_triage(reason: str, error: str, attempt: int, max_attempts: int) -> dict:
    """Deterministic crash-triage fallback (no LLM): repair a clear MECHANICAL crash — or a TIMEOUT
    (too slow, not a wrong idea -> reduce compute) — while attempts remain, otherwise abandon.
    Conservatively NEVER returns "reject_idea" — killing a whole idea lineage is a strong call
    reserved for the LLM agent, so the rule path stays safe with the unified agent off (it only ever
    repairs obvious mechanical crashes / timeouts or abandons).

    THE NO-JUDGE PATH, and only that. It runs when no triage model is WIRED (`unified_agent` off),
    which is a configuration, not a failure — so it is allowed to keep repairing, bounded by the
    caller's `max_attempts`. A judge that IS wired and cannot answer is a different condition
    entirely and must NOT land here: it is `unanswerable` (see the verdict contract above), because
    "the provider is dead" and "the operator runs without a triage model" call for opposite
    behaviour and conflating them is what let a dead provider drive an unbounded repair loop.

    This path cannot express the stop decision the model makes — it has no memory of the repair
    history — so `max_attempts` is doing all the work here, which is exactly why the backstop is not
    optional."""
    err = error or ""
    if reason in ("timeout", "oom") and attempt <= max_attempts:
        why = ("timeout — reduce compute to fit the budget (rule-based)" if reason == "timeout"
               else "OOM-killed — reduce memory: batch/model size or subsample to fit the pod limit (rule-based)")
        return {"action": "repair", "rationale": why}
    mechanical = any(s in err for s in _MECHANICAL_MARKERS)
    if reason == "crash" and mechanical and attempt <= max_attempts:
        return {"action": "repair", "rationale": "mechanical crash (rule-based)"}
    return {"action": "abandon",
            "rationale": "non-mechanical failure or attempts exhausted (rule-based)"}
