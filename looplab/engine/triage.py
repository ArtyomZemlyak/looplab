"""Pure triage/fingerprint helpers for the engine loop (extracted from orchestrator.py):
workspace drift fingerprinting (`_dir_fingerprint` / `_shallow_fingerprint`), failure
classification (`_failure_reason`), the triage-verdict contract (`TRIAGE_ACTIONS` /
`coerce_triage_action` / `is_transport_failure_verdict`), the "did the repair CALL produce a repair"
predicate
(`repair_artifact_defect`), the deterministic crash-triage fallback (`_rule_triage`, whose
`_MECHANICAL_MARKERS` stderr scan was DELETED on 2026-08-20 — see its obituary below), the
ownership split re-exported from its home in `engine/failure_diagnosis.py`,
the env-prep round bound (`_MAX_DEP_ROUNDS`), the judge re-ask bound
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
gone because a budget is about time and money, not about whose fault a repair was.

AND SINCE 2026-08-20 THE CLASSIFICATION HAS MOVED THE SAME WAY, twice, and the second move is what
shipped. The first cut let a judge RE-READ the three kinds the classifier inferred from text
(`crash`/`oom`/`no_metric`) while keeping the text rules themselves; the second deleted every text
rule in this file and made the split a question of OWNERSHIP rather than of confidence. The whole
argument, the per-reason table and the measurements are in `engine/failure_diagnosis.py`'s module
docstring, which is the one place they are written down; what belongs HERE is only what a reader of
`_failure_reason` needs:

  * every branch of `_failure_reason` now reads a field the ENGINE set — its clock, its watchdogs'
    out-of-band signals, its cross-reader, the return code of the setup command it ran, the
    filesystem contracts its stage runner checked, and the process exit code. None parses a message.
  * `crash` and `no_metric` survive as honest STRUCTURAL residuals and say nothing about the cause.
    They, plus `oom` and `check_failed`, are `DIAGNOSABLE_ENGINE_REASONS` — handed to the
    diagnostician as evidence rather than kept as answers.
  * the deleted rules are named with their obituaries in place, so that nobody reinstates one from
    the corpus win it really did produce: the `setup failed:` stderr prefix (replaced by
    `RunResult.setup_failed`, an out-of-band flag on the branch that already knew), the `-9/137 +
    no-Traceback` kernel-OOM signature, `_is_torch_oom`'s allocator marker list, and
    `_MECHANICAL_MARKERS`.

The rule the whole change generalizes, and the one to apply to any new reason before routing it:
**text may NOMINATE, it may never DECIDE** — which is what `runtime/deps.py::triage_install_
candidates` (nominate) and `is_present` (decide) had already converged on, and whose absence
`crash_repair._prepare_env` records the cost of."""
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


# THE ALLOCATOR-OOM MARKER SCAN IS GONE (2026-08-20), and this note is here so nobody
# reinstates it from the corpus win it really did produce. `_TORCH_OOM_MARKERS` + `_is_torch_oom`
# resolved all 26 of the out-of-memory failures that `runs/` records as `crash`, and that number is
# real. It was still the wrong instrument, for the reason `engine/failure_diagnosis.py` states in
# full: it was TEXT WITH THE LAST WORD. Nothing downstream re-checked it, and a marker list is
# exactly as good as its own spelling — a host `MemoryError`, `DefaultCPUAllocator: can't allocate
# memory`, an OOM re-raised inside another library's exception and a torchrun launcher that swallows
# the child exception into an opaque `Root Cause ... exitcode: 1` block are all out-of-memory
# failures the list cannot see, and 9 of those same 26 rows are precisely that launcher block.
#
# The kernel signature it sat beside (`exit_code in (-9, 137)` AND `"Traceback" not in stderr`) is
# gone with it, and that one was measurably WRONG rather than merely incomplete: a
# `torch.OutOfMemoryError` RAISES, so it prints a full traceback and exits 1, i.e. every conjunct of
# the signature is false for the most common way a training eval dies on this box.
#
# What replaced them is not a longer list. `crash` stays as the engine's honest STRUCTURAL residual
# ("the process exited non-zero"), and the question of what it died OF goes to the diagnostician,
# which is asked once per failure, can read the whole stage log and the code that wrote it, and must
# cite what it stood on. `runtime/deps.py`'s regexes are deliberately NOT touched by any of this —
# there, text NOMINATES and `is_present` DECIDES, which is the discipline this change generalizes.


def _failure_reason(res) -> str:
    """Classify why an eval produced no usable metric — STRUCTURALLY, from what the ENGINE itself
    recorded, and never from the failure's own text. Ordered most-specific first.

    Every branch below reads a field the engine set: its own clock (`timed_out`), its own watchdogs'
    out-of-band signals (`diverged`/`stalled`), its own cross-reader (`drift`), the return code of
    the SETUP command it launched (`setup_failed`), the filesystem contracts its own stage runner
    checked (`stages[-1]["status"]`), and the exit code of the process it started. Not one of them
    parses a message. The two rules that did — the `setup failed:` stderr prefix and the two OOM
    signatures — were deleted on 2026-08-20; see the note above and
    `engine/failure_diagnosis.py`'s module docstring for the rule that replaced them.

    THE LAST TWO BRANCHES ARE RESIDUALS, NOT DIAGNOSES, and that is the whole reason the
    diagnostician exists. `crash` says only "this process exited non-zero" and `no_metric` only "it
    exited zero and no reader found a number"; both are true and neither says what happened.
    `failure_diagnosis.DIAGNOSABLE_ENGINE_REASONS` is the set of answers this function produces that
    are handed on as EVIDENCE rather than kept as answers.

    ("idea_rejected" is NOT classified here — it is set by `_evaluate` when the triage agent judges
    the idea fundamentally wrong, which ends the node. What still reads it is the fold's historical
    debug-anchor map, `events/card_ledger.py::_card_debuggable_leaf_candidate_ids`.
    `unclassified` is not produced here either: it is minted by `_evaluate` when a WIRED
    diagnostician could not answer, which is a fact about the diagnostician and not about the eval.)"""
    if getattr(res, "drift", None) is not None:
        return "drift"
    if res.timed_out:
        return "timeout"
    # THE SETUP FLAG, not the stderr prefix. `run_command_eval` sets `setup_failed` on the branch
    # where it has just observed the setup command IT ran exit non-zero or time out; reading the
    # twelve-character prefix it also writes was a fact making a round trip through a channel the
    # candidate writes too, and `setup` is in `NEVER_SALVAGED_REASONS`, so a stderr opening with
    # those characters could suppress a metric the eval really produced.
    if getattr(res, "setup_failed", False):
        return "setup"
    # The two WATCHDOG verdicts come BEFORE the exit-code branch, because the engine caused the exit
    # code it would otherwise be reading: both watchdogs tree-kill. Read the authenticated flags
    # (`runtime/command_eval.py` fills them from `run_argv`'s out-of-band `signals`), never the
    # stderr sentinels beside them — those are mixed into the candidate's own output and are
    # forgeable.
    # Divergence outranks a stall: a run that logs non-finite records and then goes silent is
    # stall-killed first and the drain confirms the divergence afterwards, which is the same
    # precedence `command_eval._salvageable_stall` applies to the salvage gate.
    if getattr(res, "diverged", False):
        return "diverged"
    if getattr(res, "stalled", False):
        return "stalled"
    if res.exit_code != 0:
        return "crash"          # the process died; nothing out of band saw WHY
    # A declared-contract failure is its OWN reason. Both contract branches in
    # `runtime/command_eval.py::_run_stages` report `exit_code=0`, so without this they landed below
    # and a stage that failed its artifact or assertion contract was reported as "the command
    # printed no metric" — about a stage that had printed one. The literals are spelled out rather
    # than returned through the variable so the registry cross-check in
    # `tests/test_inline_repair_reason_coverage.py` can still derive this function's vocabulary from
    # its own source.
    #
    # Note the three are NOT all on the same side of the ownership split, and the difference is
    # WHO checked: `needs_failed`/`expect_failed` come from the engine's own `stat` of a declared
    # input/output, while `check_failed` comes from `_call_stage_check`, i.e. from ANOTHER MODEL's
    # reading of the candidate's own stdout. Only the third is diagnosable.
    _rows = [row for row in (getattr(res, "stages", None) or []) if isinstance(row, dict)]
    _last = str(_rows[-1].get("status") or "") if _rows else ""
    if _last == "needs_failed":
        return "needs_failed"
    if _last == "expect_failed":
        return "expect_failed"
    if _last == "check_failed":
        return "check_failed"
    return "no_metric"          # exit 0 but no parseable metric emitted


# --- WHO MAY SAY WHAT FAILED: the ownership split ----------------------------------------------
# The vocabulary, the rule and the whole argument live in `engine/failure_diagnosis.py`, beside the
# diagnostician they govern. They are re-exported HERE, next to `_failure_reason`, because a reader
# of the classifier has to be able to see which of its answers are final and which are handed on —
# and because `agents/unified_agent.py`'s deferred import and the engine's own imports have named
# `engine.triage` for both halves since the seam shipped. Both spellings resolve to the SAME
# objects; there is no second definition anywhere.
#
# `judged_failure_reason` / `JUDGED_FAILURE_REASONS` / `coerce_failure_reason` (2026-08-20, one day)
# are GONE rather than aliased. They named a narrower rule — "a judge may re-read the three kinds
# the engine inferred from text" — and the new one is not a superset of it in the direction that
# matters: `diagnosed_failure_reason` can now answer `unclassified`, which the old name's callers
# would silently mishandle. A renamed rule with changed semantics must break its callers.
from looplab.engine.failure_diagnosis import (       # noqa: E402,F401  (re-export beside its classifier)
    DIAGNOSABLE_ENGINE_REASONS,
    DIAGNOSED_ENGINE_FINAL_OVERLAP,
    DIAGNOSED_FAILURE_REASONS,
    DIAGNOSED_ONLY_REASONS,
    DIAGNOSIS_SUMMARY_CAP,
    DIAGNOSIS_UNAVAILABLE_KEY,
    ENGINE_FINAL_REASONS,
    EVIDENCE_LOCATOR_CAP,
    EVIDENCE_QUOTE_CAP,
    EVIDENCE_SOURCES,
    FINDINGS_CAP,
    FINDING_MEANS_CAP,
    REASON_SOURCE_ENGINE,
    REASON_SOURCE_TRIAGE,
    REASON_SOURCE_UNDIAGNOSED,
    REASON_SOURCES,
    UNCLASSIFIED_REASON,
    coerce_diagnosis_summary,
    coerce_evidence,
    coerce_failure_kind,
    coerce_findings,
    diagnosed_failure_reason,
    resolve_findings,
)

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

# `_MECHANICAL_MARKERS` IS GONE (2026-08-20) — the last text rule in this file, and the one that
# chose an ACTION rather than a classification. It scanned stderr for `ImportError`/`NameError`/
# `TypeError`/"has no attribute"/… and, on a hit, let the no-judge path repair up to the caller's
# full `max_attempts` instead of the tighter blind bound below.
#
# So it was a bound that depended on the TEXT QUALITY of a program's error output, which is exactly
# what this module's header says is not a bound — the 2026-08-05 finding, still true, applied to the
# one branch that had kept it. A Cyrillic identifier, a launcher that swallows the child exception,
# a blank stderr and any traceback in a language other than English all read as "not mechanical" and
# silently got the shorter budget; a `RuntimeError` whose message happens to contain the word
# `TypeError` got the longer one.
#
# COLLAPSING THE TWO BOUNDS COSTS NOTHING MEASURABLE, which is why the fix is a deletion and not a
# better scanner. `_RULE_BLIND_CRASH_ATTEMPTS` is 12, calibrated against the longest legitimate
# chain on record — `runs/rubert-dr-0805` node 0, eight repairs, of which the six stale-dependency
# migrations are the very MECHANICAL crashes this marker list existed to grant the wider budget —
# plus room for a chain half again as long. Twelve clears it, so every chain the markers protected
# is still clear of the one bound that remains.


# THE BOUND ON REPAIRING BLIND, i.e. on the branch F5 added below — a `crash` this path can say
# NOTHING about, repaired anyway because abandoning it outright throws away a node one edit would
# have fixed. It is deliberately TIGHTER than the caller's `max_attempts`, and the reason is that in
# the configuration this whole function serves, `max_attempts` is the only other thing left:
#
#   * no judge (`unified_agent` off) is exactly why control is here at all, and
#     `crash_repair.py::_repair_critic` reads `researcher.repair_critic` — the SAME object that
#     would have carried `triage_crash` — so a run without one has no critic either;
#   * `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` pins `inline_repair_attempts: 0` on every resumed
#     pre-versioning run, which `evaluate._effective_repair_cap` reads as the engine's absolute
#     `_UNLIMITED_REPAIR_CEILING` of 50;
#   * `repair_judgment.repair_redone_work_stop`, the SECONDS floor beside it, returns None whenever
#     the task declares no pipeline cost at all — it is the right bound and it is not always there.
#
# Driven, that product is 51 full pipeline evaluations for one undiagnosable `RuntimeError`
# (`tests/test_first_stage_repair_cost.py`), on a task whose one pipeline may be multi-hour. 50 is
# the ceiling under a JUDGE that can say "I no longer know how to fix this"; with no judge there is
# nothing to defer to and the ceiling is doing all of the work alone.
#
# 12 IS NOT A NEW NUMBER. It is the pre-F8 `Settings.inline_repair_attempts` default, calibrated
# against the longest legitimate chain on record — `runs/rubert-dr-0805` node 0, eight repairs (six
# stale-dependency migrations plus two on its actual research question) — plus room for a chain half
# again as long, which is precisely the calibration a COUNT-driven loop needs and the one the F8
# redesign retired because a judgement replaced it. Where no judgement exists, the count it replaced
# is the honest bound. It clears every legitimate chain in the corpus (v8 node 3's four first-stage
# repairs included) and cuts the blind runaway from 51 evaluations to 13.
#
# It bounds ONLY the undiagnosed branch. A MECHANICAL crash names what to change in its own
# traceback, so the rule path is not guessing there and keeps the caller's cap unchanged — the six
# migrations above are all mechanical, and shortening them is how a node dies before it reaches its
# research question. And it is a `min` with `max_attempts`, never a widening: an operator who spelled
# a smaller cap keeps it.
_RULE_BLIND_CRASH_ATTEMPTS = 12

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
#
# What this tuple is NOT: the value set of the `triage_action` FIELD on a `node_repaired` row. Since
# 2026-08-12 a second, non-triage producer writes that field —
# `engine/metric_salvage.py::SALVAGE_CAUSE_TRIAGE_ACTION` ("salvage_cause_fix"), stamped by
# `engine/evaluate.py::_repair_salvaged_cause` on a cause fix that bought no re-evaluation, so that
# `_durable_repair_ledger` can keep it out of the inline-repair ATTEMPT budget. It is deliberately
# absent from this tuple: no model may emit it, the coercion must never accept it off the wire, and
# the fallback must never produce it — it is a marker, not a verdict. A reader who treats this tuple
# as exhaustive over the field will mis-handle that row; a reader who adds it here breaks the emit
# schema's enum. Keep the two vocabularies separate and cross-referenced.
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

# THE INTAKE BOUND on the triage model's own free text, and it is an INTAKE bound only — every SINK
# keeps its own, tighter cap for its own reason (`node_repaired.rationale` 300 after redaction,
# `repair_log["fix"]` 200 for the judge's history, `node_failed.triage_rationale` 300,
# `proposal_cues` 90). Those sinks are what bound the durable row and the prompt; this one bounds
# only what the ENGINE carries between them, so tightening it buys nothing downstream and costs the
# one reader that needs the whole sentence.
#
# It was 300 — the same number as the durable sink — and that silently truncated the input to
# `repair_verify.verify_repair`, which reads the rationale to ask "did this repair do what it said?".
# A crash rationale is written in one shape: DIAGNOSIS first ("diverged right after the R-Drop KL
# term, unlike the working nll_cos runs"), then "Fix: <the concrete things I am about to change>".
# So a cut at 300 lands almost exactly on the seam and feeds the extractor the CITATIONS while
# discarding the CLAIMS — the one half it exists to check. Measured over `runs/` on 2026-08-14:
# 83 of the 123 model-authored `node_repaired` rationales in the corpus are stored at exactly the
# cap, i.e. the MEDIAN rationale the rung read was truncated; and over the 54 repairs whose full
# text could be recovered from `spans.jsonl` and replayed, 5 verdicts are wrong because of it —
# 3 of the 7 `unmet`s and 2 of the 3 `unstated`s are `verified` on the text the model actually
# wrote. `rubertlite-dr-unified-v7` node 0 attempt 2 is the live instance: its only surviving claim
# was `nll_cos`, cited as the BASELINE it was comparing against, while `kl_div`/`log_target`/
# `rdrop_alpha` — named after the cut, and present in the diff — were never read.
#
# 2000 is not a round number picked for comfort: the 93 full rationales in the corpus run
# 121-690 chars (median 330, p90 460), so this is ~2.9x the longest one ever written here and still
# bounds a model that decides to answer with an essay. Widening it can only ADD claims, and a claim
# that was met stays met, so no `verified` can become `unmet` by this change — the failure it fixes
# is one-directional.
#
# IT LIVES HERE, and that is the 2026-08-14 correction to the fix above. Raising it in
# `engine/crash_repair.py` alone changed NOTHING, because the intake is not the first cap the text
# meets: the ONLY implementation of the duck-typed `triage_crash` seam in this tree is
# `agents/unified_agent.py::triage_crash`, it is the shipped default (`Settings.unified_agent`), and
# its own emit finalizer already cut the rationale to 300 before returning. A bound applied to what a
# seam RETURNED cannot widen what the seam's implementation already threw away, so both layers must
# read ONE constant — which is why it sits in the module `unified_agent.py` already imports for the
# verdict vocabulary (stdlib-only at module scope, so the `agents` -> `engine` deferred import that
# reaches it cannot cycle) rather than in the mixin, which imports half the engine.
TRIAGE_RATIONALE_CAP = 2000


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
    """Deterministic crash-triage fallback (no LLM): repair while attempts remain, otherwise
    abandon. Conservatively NEVER returns "reject_idea" — killing a whole idea lineage is a strong
    call reserved for the LLM agent, so the rule path stays safe with the unified agent off.

    THE NO-JUDGE PATH, and only that. It runs when no triage model is WIRED (`unified_agent` off),
    which is a configuration, not a failure — so it is allowed to keep repairing, bounded by the
    caller's `max_attempts`. A judge that IS wired and cannot answer is a different condition
    entirely and must NOT land here: it is `unanswerable` (see the verdict contract above), because
    "the provider is dead" and "the operator runs without a triage model" call for opposite
    behaviour and conflating them is what let a dead provider drive an unbounded repair loop.

    It is ALSO the one producer of `failure_diagnosis.DIAGNOSIS_UNAVAILABLE_KEY`, and that marker is
    load-bearing rather than decorative. The verdict this returns is shaped exactly like a model's —
    a dict with an `action` in `AGENT_TRIAGE_ACTIONS` — so without a marker saying "nobody was
    asked", `diagnosed_failure_reason` would read a rule-path `repair` as a diagnostician that
    answered with no `failure_kind`, i.e. mint `unclassified` on EVERY offline, toy and
    `unified_agent=false` run. The marker is unforgeable from the wire by construction:
    `crash_repair._ask_triage` rebuilds an agent verdict from a fixed key list, so no model output
    can set it. `error` is accepted and deliberately unread — the signature is a duck-typed seam
    with several callers, and the text is precisely what this path stopped consulting.

    This path cannot express the stop decision the model makes — it has no memory of the repair
    history — so `max_attempts` is doing all the work here, which is exactly why the backstop is not
    optional."""
    # The two watchdog verdicts join timeout/oom here because they are the same KIND of fact: the run
    # was stopped by a resource or health rule, not by a mistaken idea, so the deterministic path can
    # safely say "repair" without a judge. Each carries its OWN rationale — the whole reason they are
    # separate reasons is that "reduce memory" is the wrong instruction for both.
    #
    # THE `oom` ARM IS UNREACHABLE ON THIS PATH SINCE 2026-08-20 and is kept deliberately. Both of
    # `_failure_reason`'s `oom` producers were text rules and both were deleted, so with no judge
    # wired nothing can classify a failure as `oom` any more — the allocator OOMs that used to land
    # here arrive as `crash` and take the blind branch below. It stays because this is the ONE place
    # the rule path spells the memory-reduction directive, and a future router that does carry a
    # diagnosed reason here must find it rather than fall silently to "no judge wired".
    if reason in ("timeout", "oom", "diverged", "stalled", "not_learning") and attempt <= max_attempts:
        why = {"timeout": "timeout — reduce compute to fit the budget (rule-based)",
               "oom": "out of memory (kernel OOM-kill or a torch allocator raise) — reduce memory: "
                      "per-device batch, model size, sequence length or a subsample (rule-based)",
               "diverged": "health-check killed it — the loss/grad_norm went non-finite; stabilise the "
                           "objective (LR, warmup, grad clipping, epsilons), do NOT cut memory (rule-based)",
               "stalled": "stall watchdog killed it — the stage was alive and silent; remove the hang or "
                          "emit a heartbeat, do NOT cut memory (rule-based)",
               "not_learning": "training watchdog killed it — the loss stopped moving and the judge "
                               "named the implementation; make the objective able to descend, do NOT "
                               "cut memory (rule-based)"}[reason]
        return {"action": "repair", "rationale": why, DIAGNOSIS_UNAVAILABLE_KEY: True}
    # A CRASH IS REPAIRED BLIND, while attempts remain — changed 2026-08-13 with F5, and only because
    # F5 removed what this branch used to defer to. `abandon` here was the conservative answer on the
    # reasoning that killing a lineage is a strong call reserved for the LLM agent; what made it
    # conservative rather than merely lossy is that an abandoned node then got a DEBUG NODE, which
    # handed the same failure to the same Developer with a fresh node's budget. With the Debug node
    # deleted, the identical verdict means "throw the node away", so the cautious spelling became the
    # destructive one — a `RuntimeError` ends a node that one repair would have fixed.
    #
    # `unclassified` joins it (2026-08-20) and lands on exactly this branch by design: a failure a
    # WIRED diagnostician could not name is the definition of repairing blind, so it gets the blind
    # bound and never the caller's full cap. It buys no extra attempt — the same `min` with
    # `max_attempts` — and it is in `FAILURE_REASONS`, so an operator who narrowed
    # `inline_repair_reasons` still governs it. (Reaching this branch AT ALL requires a wired judge,
    # which contradicts this function's own precondition, so it is unreachable in practice today;
    # it is spelled anyway because the alternative is the catch-all `abandon` below, and a value
    # meaning "nobody could say" must not be the one thing that ends a node outright.)
    #
    # THE SINGLE BOUND, since `_MECHANICAL_MARKERS` was deleted: there is no longer a wider budget
    # for a crash whose text names a Python exception class. See that constant's obituary above for
    # why 12 still clears every legitimate chain in the corpus.
    if reason in ("crash", UNCLASSIFIED_REASON):
        blind_cap = min(int(max_attempts), _RULE_BLIND_CRASH_ATTEMPTS)
        if attempt <= blind_cap:
            return {"action": "repair",
                    "rationale": "crash with attempts remaining and no judge wired (rule-based)",
                    DIAGNOSIS_UNAVAILABLE_KEY: True}
        # Named separately from the catch-all below: an operator reading this terminal must be able
        # to tell "your cap ran out" from "nothing here could say what to change next", because the
        # remedies are different (raise the cap vs wire a triage model).
        return {"action": "abandon",
                "rationale": (f"a crash this rule path cannot classify has had its {blind_cap} blind "
                              f"repair attempt(s) and no triage model is wired to decide what to "
                              f"change next (rule-based)"),
                DIAGNOSIS_UNAVAILABLE_KEY: True}
    return {"action": "abandon",
            "rationale": "non-repairable failure or attempts exhausted (rule-based)",
            DIAGNOSIS_UNAVAILABLE_KEY: True}
