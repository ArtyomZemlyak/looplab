"""THE EVAL CANARY — run a node's own stage chain on a tiny slice before its full evaluation.

WHY. A repo eval can run for many hours — measured on MiniOneRec SFT: 30 min data prep + 8 h
training + 15 min scoring — and a trivial defect in its TAIL (a `KeyError` in the scoring code, a
missing file, a wrong CLI flag) surfaces only at the very end, after the whole run has been paid
for. The operator was already doing the fix by hand: the same eval command on a tiny slice (2 000
users, 50 test queries) that finishes in ~5 minutes and exercises every stage, scoring included.

WHAT. Under `Settings.eval_canary`, for a task that declares `eval.canary = {"env": {...},
"timeout": 900}` (`adapters/repo_task.py::CanarySpec`), RUN_ATTEMPT (`engine/evaluate.py::
_eval_run_attempt`) runs the SAME resolved stage chain once, before the full eval of that attempt:

  * in a SCRATCH directory (`<run>/canary/node_<id>`, materialized from the same node manifest by
    the same `_materialize`), never the node's workdir — so no artifact, log or metric file the
    canary writes can be read as the real run's output by the metric readers, the salvage rungs,
    the watchdogs or the stage-reuse predicate;
  * with `LOOPLAB_CANARY=1` (`CANARY_ENV`, stamped by the engine — the `LOOPLAB_` namespace is
    engine-owned, so no task can declare or suppress it) and the task's `canary.env` overlaid LAST
    on the eval's declared environment (what either means is the eval script's business — the
    engine never interprets them);
  * with every stage (and the single command) capped at `canary.timeout`, plus the same bound over
    the whole chain (`CanaryClock`), because a canary that runs long has already failed its purpose;
  * under the attempt's own GPU lease and env pin, inside the attempt's own clock (`_t0`), so its
    seconds are charged to the node's eval seconds exactly as the full eval's are — also when a
    PAUSE withholds the full eval after it (`evaluate.py::_pause_withholds_attempt`): no terminal
    is written then and the re-dispatch skips the passed canary, so its seconds ride on the
    `eval_attempt_withheld` row to the lifecycle's next terminal (doc 69 69.12a);
  * under the attempt's intervention watcher, so an operator abort / reset kills it like the eval.

A FAILED canary is the attempt's crash: RUN_ATTEMPT hands SETTLE_OUTCOME a metric-less `RunResult`
built by `canary_failure_result` (its output as the evidence, the full eval never started), and
the attempt goes through the ordinary triage/repair path. SALVAGE is skipped for it by name — no
rung may turn anything a canary produced into the node's number — and the result carries no stage
rows and no `failed_stage`, so the repair's reuse predicate can never "reuse" a stage the node's
workdir never ran (it answers a full re-run). A PASSED canary lets the full eval start in the same
attempt — unless the run was PAUSED meanwhile, when the node waits pending with the pass remembered
(a finalize drains: it lets the full eval run) — and its metric is discarded on the spot and is never
recorded anywhere but the diagnostic row.

REPLAY / RESUME. Two DIAGNOSTIC rows (`events/types.py::EV_EVAL_CANARY_STARTED` /
`EV_EVAL_CANARY_FINISHED`) keyed on (node, generation, code digest). `canary_already_passed` reads
the log for a `passed` row under the same key, so a resumed process does not re-run a canary it has
already paid for on the same code; a repaired node has a new digest and is canaried again, which is
the point — the repair is exactly the code nobody has run yet.

NOT HERE: selection, the fold, the metric. A canary's number never reaches `node_evaluated`.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Iterable, Optional

from looplab.core.evidence import EVIDENCE_LABEL, fence_untrusted
from looplab.core.models import coerce_node_id
from looplab.events.replay import event_generation_binds
from looplab.events.types import EV_EVAL_CANARY_FINISHED

# THE MARKER every canary launch carries, set by the engine and never declarable (`core/envsafe.py`
# refuses `LOOPLAB_*` at every declaring level): the eval script reads it to run its tiny slice.
CANARY_ENV = "LOOPLAB_CANARY"

# How much of the canary's stdout/stderr the failure result carries. The downstream windows
# (`_eval_failure_text`, the durable evidence) cut again, so this only bounds memory.
_CANARY_OUTPUT_CHARS = 200_000

# THE WHOLE BUDGET of a failed canary's own account (`canary_failure_result`'s `canary_account`,
# doc 69 69.7): the header, both labelled and fenced stream tails, and the engine's footer. That text
# is the repair context's failure text, and the repo Developer keeps only the FIRST 4,000 characters
# of its repair context (`adapters/repo_developer.py`, `fenced_head(error, 4000)`), where the failure
# text is not first: the diagnostician's lead (up to `failure_diagnosis.DIAGNOSIS_SUMMARY_CAP`) rides
# in front of it, the per-kind directive and the stuck contract behind — 1,906 characters around a
# 500-character tail, measured (critic 2026-09-29, driven). The first account, two 1,500-character
# tails and ~3.5k in all, pushed the stream's last line and the whole stuck contract out of that
# window; 2,000 keeps both inside it.
CANARY_ACCOUNT_CHARS = 2_000
# What one stream's label and fence cost on top of its tail (a label with two six-digit counts, the
# evidence fence's 50 characters, the newlines), reserved before the tails share the rest.
_ACCOUNT_TAIL_OVERHEAD = 130
# The floor under each stream's share when a long log path or detail eats the budget: a tail too
# short to hold one traceback line is no account at all. It is the ONE thing that may take the
# account past `CANARY_ACCOUNT_CHARS`, and only when the header and footer alone leave less than the
# two floors — with the env names bounded below, a log path of ~800 characters (critic 2026-09-30).
_ACCOUNT_MIN_TAIL = 200
# At most this many characters of the canary's env NAMES in the ACCOUNT's header (the historical
# header keeps them all, byte for byte): the task's own declaration plus `CANARY_ENV`, and forty of
# them at thirty characters each made a 1,859-character header that left the tails only their
# floors — a 2,564-character account (critic 2026-09-30, `probe_account_budget`).
_ACCOUNT_ENV_NAMES_CHARS = 200


def canary_spec(eval_spec) -> Optional[dict]:
    """The task's canary declaration as `{"env": {...}, "timeout": float}`, or None.

    `env` is the task's declared extras PLUS `CANARY_ENV=1`, which always wins. Read defensively off
    the (possibly grandfathered) snapshot dict: a declaration that is not a mapping, or whose
    timeout is not a positive finite number, is "no canary" — a malformed declaration must never
    cost a node its evaluation (the submit-time validator is where it is refused)."""
    if not isinstance(eval_spec, dict):
        return None
    c = eval_spec.get("canary")
    if not isinstance(c, dict):
        return None
    env = c.get("env") or {}
    if not isinstance(env, dict):
        return None
    env = {k: str(v) for k, v in env.items() if isinstance(k, str) and k}
    try:
        timeout = float(c.get("timeout", 900.0))
    except (TypeError, ValueError):
        return None
    if not (timeout > 0 and timeout != float("inf")):
        return None
    mode = c.get("mode", "slice")
    if mode not in ("slice", "smoke"):
        return None
    out = {"env": {**env, CANARY_ENV: "1"}, "timeout": timeout}
    if mode == "smoke":
        out["mode"] = "smoke"     # absent = `slice`: every pre-mode spec reads back byte for byte
    return out


def smoke_canary_passed(res, *, interrupted: bool) -> bool:
    """A SMOKE canary's pass rule (doc 78 §5): the chain SURVIVED its cap (the clock stopped a
    still-running process) or exited 0 before it. A non-zero exit, or a stage that died before the
    clock, is the failure it exists to catch — in seconds rather than after hours of preparation.

    "Survived" is read off the SANDBOX's own kill flag (`RunResult.timed_out`: the drain tree-killed
    a still-running process on the canary clock's event or a stage's cap), never off
    `CanaryClock.expired`. That flag only says the deadline passed before the clock was stopped, and
    the clock is stopped after `_run_eval`'s post-processing — seconds of metric reading and stage
    bookkeeping after the process is gone — so a chain that CRASHED a few seconds under the cap read
    as a survivor, passed, and was remembered as passed for its code digest
    (`canary_already_passed`). A clock that fired after the process exited killed nothing."""
    if res is None or interrupted:
        return False
    if getattr(res, "timed_out", False):
        return True
    return getattr(res, "exit_code", 1) == 0


def capped_pipeline(timeout, stages, cap: float):
    """`(timeout, stages)` with the single command's and every stage's timeout capped at `cap`.

    A COPY: the resolved chain is the dispatcher's own list and the planners re-derive it, so the
    canary must never write its cap into anything the full eval will read."""
    try:
        t = float(timeout) if timeout is not None else cap
    except (TypeError, ValueError):
        t = cap
    capped_t = min(t, cap) if t > 0 else cap
    out = None
    if stages:
        out = []
        for s in stages:
            s = dict(s)
            try:
                st = float(s.get("timeout")) if s.get("timeout") is not None else cap
            except (TypeError, ValueError):
                st = cap
            s["timeout"] = min(st, cap) if st > 0 else cap
            out.append(s)
    return capped_t, (out if stages else stages)


class CanaryClock:
    """ONE bound over the whole canary, beside the per-stage caps: a chain of N stages each under
    `timeout` could otherwise run N x `timeout`. Mirrors the attempt's `cancel` Event too, so an
    operator intervention reaches the canary's children through the one Event `run_command_eval`
    watches. `expired` says whether it was the CLOCK (a canary failure) rather than the operator."""

    def __init__(self, parent: Optional[threading.Event], timeout: float, poll: float = 0.5):
        self.event = threading.Event()
        self.expired = False
        self._parent = parent
        self._deadline = time.monotonic() + float(timeout)
        self._poll = poll
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="looplab-canary-clock",
                                        daemon=True)

    def _watch(self) -> None:
        while not self._done.is_set():
            if self._parent is not None and self._parent.is_set():
                self.event.set()
                return
            if time.monotonic() >= self._deadline:
                self.expired = True
                self.event.set()
                return
            self._done.wait(self._poll)

    def __enter__(self) -> "CanaryClock":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._done.set()
        self._thread.join(timeout=5)


def canary_passed(res, *, expired: bool = False, artifact: bool = False) -> bool:
    """THE PASS RULE, and it is the full eval's own success rule (`_eval_settle_outcome`): the chain
    exited 0, was not killed by a clock, and the operator's reader found a number. The number itself
    is then discarded — it measured a slice.

    `artifact` — the node is an ARTIFACT node (doc 73 §1.4), whose full eval succeeds on a CLEAN
    pipeline with no metric (`_eval_settle_outcome`: the metric is dropped and the attempt is ok
    exactly when `triage._failure_reason` answers `no_metric`). The canary takes that same rule
    (review 2026-10-08, driven): asked for a number, every artifact canary failed, bought a paid
    triage and a repair, and ended the node `unclassified` — an artifact node could never pass with
    the canary on. Kept rather than skipped because the canary's purpose holds for a preparation
    pipeline as well: a crash, a broken stage contract or a missing declared output is caught on the
    slice, cheaply, before the full run. A number the pipeline prints anyway is ignored, as there."""
    if res is None or expired:
        return False
    if artifact:
        import copy
        from looplab.engine.triage import _failure_reason
        unranked = copy.copy(res)
        try:
            unranked.metric = None
        except (AttributeError, TypeError):   # a frozen double: read it as it is
            unranked = res
        return _failure_reason(unranked) == "no_metric"
    return (getattr(res, "metric", None) is not None and not getattr(res, "timed_out", False)
            and getattr(res, "exit_code", 1) == 0)


def own_timeout_fired(res, *, expired: bool, own_timeouts: dict, cap: float):
    """The timed-out stage's OWN declared timeout when that — not the canary's cap — is the clock
    that fired, else None. `capped_pipeline` runs a stage at `min(own, cap)`, so a stage whose own
    timeout is at or under the cap is killed at the same second under a longer canary cap too: the
    doubled-cap retry was pure waste and its sentence ("nor within 2x s") was false (critic
    2026-09-29). `own_timeouts` maps stage name -> declared timeout, `None` -> the single command's."""
    if expired or res is None or not getattr(res, "timed_out", False):
        return None
    own = own_timeouts.get(getattr(res, "failed_stage", None) or None)
    try:
        own = float(own)
    except (TypeError, ValueError):
        return None
    return own if 0 < own <= cap else None


def canary_failure_detail(res, *, expired: bool, timeout: float, own_timeout=None) -> str:
    """One sentence naming HOW the canary failed, from the engine's own record of it."""
    if res is None:
        return "the canary produced no result"
    if own_timeout is not None and not expired:
        stage = getattr(res, "failed_stage", None)
        whose = f"stage {stage!r} hit its" if stage else "the eval hit its"
        return (f"the canary's {whose} own {own_timeout:g}s timeout (under the canary's "
                f"{timeout:g}s cap)")
    if expired or getattr(res, "timed_out", False):
        return f"the canary did not finish within its {timeout:g}s cap"
    stage = getattr(res, "failed_stage", None)
    code = getattr(res, "exit_code", None)
    if code not in (0, None):
        return (f"the canary exited {code}" + (f" in stage {stage!r}" if stage else ""))
    if getattr(res, "metric", None) is None:
        return ("the canary exited 0 but the task's metric reader found no number in its output"
                + (f" (stage {stage!r})" if stage else ""))
    return "the canary failed"


# THE ONE MECHANICAL RETRY an expired canary gets, at this multiple of its cap (doc 69 69.10). A clock
# kill says nothing about the code, and a cold JIT or compile cache — the one cause a retry heals —
# was measured on MiniOneRec v10: a backbone that had passed twice was killed at the cap with its eval
# already done, then passed at 1020 s once the operator raised the cap. No LLM is asked; a second
# expiry ends the node as `canary_timeout` (`core/models.py::NON_REPAIRABLE_REASONS`).
CANARY_RETRY_CAP_FACTOR = 2.0

# A PASSED canary that used at least this share of its cap is recorded `near_cap` on its finished row
# and logged at WARNING (doc 69 69.10): MiniOneRec v10 node 10 passed at 1102 s of a 1200 s cap (92 %)
# and the engine said nothing, so the next backbone's expiry was the first the operator heard of it.
# A record, never a gate: the cap is the operator's number, and growing it on its own would have let
# node 12 through, whose canary was right to stop a model scoring at 13-17 min a batch.
CANARY_NEAR_CAP_FRACTION = 0.75


def canary_near_cap(seconds, cap) -> bool:
    """Did a canary that took `seconds` use at least `CANARY_NEAR_CAP_FRACTION` of its `cap`?
    False for anything that is not a positive finite pair — a malformed number records nothing."""
    try:
        seconds, cap = float(seconds), float(cap)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(seconds) and math.isfinite(cap)) or cap <= 0 or seconds < 0:
        return False
    return seconds >= CANARY_NEAR_CAP_FRACTION * cap


def canary_failure_result(res, *, detail: str, log_dir: str, env_names: Iterable[str],
                          expired: bool = False, account_redact=None):
    """The metric-less `RunResult` a FAILED canary hands SETTLE_OUTCOME as the attempt's result.

    Deliberately NARROW: exit code non-zero (a clean exit that printed no number is still a failure
    of the path, and `crash` is the reason every repair gate reads), `timed_out` False (a canary
    timeout is evidence about the code, not the node's deadline), NO stage rows and NO
    `failed_stage` (the node's workdir ran nothing, so no reuse decision may stand on it), and every
    measurement field left at its default so nothing downstream can carry a canary number. The
    canary's own output is the evidence, framed so the repair and the triage judge read it as what
    it is. `expired` — the clock killed it at its cap and at the retry's — marks it
    `canary_expired`, which `triage._failure_reason` names `canary_timeout`, and it gets its OWN
    header: "fix the defect below" is the wrong sentence for a run nothing was seen to be wrong with,
    and a non-expired failure keeps the historical header byte for byte.

    `canary_account` is the same failure as the canary's OWN account (doc 69 69.7,
    `canary_account`), built only when `account_redact` — the engine's `_redact` funnel, handed in
    under `Settings.canary_failure_account` — is given: no funnel, no account, so an account can
    never be cut before it is redacted, and a run with the switch off pays no whole-stream pass."""
    from looplab.runtime.command_eval import RunResult
    ordered = sorted(env_names)

    def _header(names: str) -> str:
        if expired:
            return (f"[eval canary] The canary preflight TIMED OUT — {detail}. The canary is this "
                    f"node's own eval pipeline run on the task's tiny slice (env: {names}) in a "
                    "scratch directory; the FULL evaluation was NOT started. The clock stopped it, "
                    "so this is the candidate's COST on that slice, not a defect the engine "
                    "observed: its pipeline must finish the slice within the cap. Canary logs: "
                    f"{log_dir}\n")
        return (f"[eval canary] The canary preflight FAILED — {detail}. The canary is this node's "
                f"own eval pipeline run on the task's tiny slice (env: {names}) in a scratch "
                "directory; the FULL evaluation was NOT started. Fix the defect below so the "
                f"canary passes. Canary logs: {log_dir}\n")

    header = _header(", ".join(ordered))
    footer = f"[eval canary] ({detail}; the full evaluation was not started)"
    code = getattr(res, "exit_code", None) if res is not None else None
    stdout = (getattr(res, "stdout", "") or "") if res is not None else ""
    stderr = (getattr(res, "stderr", "") or "") if res is not None else ""
    return RunResult(
        exit_code=(code if isinstance(code, int) and code != 0 else 1),
        stdout=stdout[-_CANARY_OUTPUT_CHARS:],
        stderr=header + stderr[-_CANARY_OUTPUT_CHARS:] + "\n" + footer,
        metric=None, timed_out=False, canary_expired=bool(expired),
        canary_account=(None if account_redact is None
                        else canary_account(_header(bounded_env_names(ordered)), footer, stdout,
                                            stderr, account_redact)))


def bounded_env_names(names, cap: int = _ACCOUNT_ENV_NAMES_CHARS) -> str:
    """`names` joined as the header joins them, cut after the last whole name that fits in `cap`
    characters and followed by how many there are in all — never a name cut in half, and never a
    list that silently looks complete."""
    names = list(names)
    joined = ", ".join(names)
    if len(joined) <= cap:
        return joined
    shown: list = []
    for name in names:
        if len(", ".join(shown + [name])) > cap:
            break
        shown.append(name)
    return (", ".join(shown) + ", " if shown else "") + f"… ({len(names)} in all)"


def canary_account_shares(stdout_chars: int, stderr_chars: int, *, room: int) -> tuple[int, int]:
    """How many characters of each stream's tail fit in `room`: half each, and a stream that needs
    less than its half hands the rest to the other — an empty stdout gives stderr the whole room."""
    half = room // 2
    out_share = min(max(stdout_chars, 0), half)
    err_share = min(max(stderr_chars, 0), room - out_share)
    return min(max(stdout_chars, 0), room - err_share), err_share


def canary_account(header: str, footer: str, stdout, stderr, redact) -> str:
    """A failed canary's OWN account (doc 69 69.7): the engine's header whole (how it failed, where
    its logs are), each stream's tail — labelled, and FENCED as the candidate's evidence, because a
    stdout that ends in `[the canary's stderr was empty]` or a forged header would otherwise read as
    the engine's words (critic 2026-09-29) — and the engine's footer LAST, so the narrow tail windows
    of the same string (the judge's history rows, `fenced_tail(err, 200)`, the Researcher's
    `error_last_line`) still say the failure was a canary's, as the historical tail always did.

    Within `CANARY_ACCOUNT_CHARS`, shared by `canary_account_shares` — each FENCED block held to its
    share, and the header's env names bounded (`bounded_env_names`) — unless the header and footer
    alone leave less than the two `_ACCOUNT_MIN_TAIL` floors. Each stream is redacted WHOLE
    before its tail is cut, through `evaluate._redacted_tail`, the ONE spelling of that order: a
    secret straddling the cut must reach the redactor whole, or its surviving fragment no longer
    matches any rule. A label's second count is the RAW stream's length — what the canary's log file
    holds."""
    out_raw = str(stdout or "").rstrip()
    err_raw = str(stderr or "").rstrip()
    room = max(2 * _ACCOUNT_MIN_TAIL,
               CANARY_ACCOUNT_CHARS - len(header) - len(footer) - 2 * _ACCOUNT_TAIL_OVERHEAD)
    # `rstrip` above already made a whitespace-only stream empty, so its length is its share's ask.
    out_share, err_share = canary_account_shares(len(out_raw), len(err_raw), room=room)
    return (header + _account_tail("stdout", out_raw, out_share, redact)
            + _account_tail("stderr", err_raw, err_share, redact) + footer)


def _account_tail(stream: str, raw: str, share: int, redact) -> str:
    """One labelled, fenced stream of the canary's own account, or the sentence that it was empty —
    itself evidence (a crash that wrote only to stdout is exactly the case the stderr tail was blind
    to)."""
    from looplab.engine.evaluate import _redacted_tail
    shown = _redacted_tail(redact, raw, share)
    if not shown.strip():
        return f"[the canary's {stream} was empty]\n"
    # THE FENCED BLOCK is held to the share, not only the tail inside it: the fence marks every
    # spelling of its own markers in what it quotes, and each mark GROWS the text, so a stream dense
    # with `END UNTRUSTED_RUN_EVIDENCE` took a 2,000-character account to 2,157 and the Developer's
    # head window lost the end of the stuck contract (critic 2026-09-30, driven). The kept tail
    # shrinks until the block fits, as `core/evidence.py::_fenced_cut` shrinks a cut block.
    budget = share + 2 * len(EVIDENCE_LABEL) + 6
    kept = shown
    block = fence_untrusted(kept, EVIDENCE_LABEL)
    while len(block) > budget and kept:
        kept = kept[min(len(kept), len(block) - budget):]
        block = fence_untrusted(kept, EVIDENCE_LABEL)
    label = (f"[the canary's {stream}, its last {len(kept):,} of {len(raw):,} characters]"
             if len(raw) > share or len(kept) < len(shown) else f"[the canary's {stream}]")
    return f"{label}\n{block}\n"


def canary_already_passed(events, node_id: int, generation: int, code_digest: str) -> bool:
    """True when the log already holds a PASSED canary for this exact (node, generation, code).

    Read off the raw rows like `evaluate.py::_durable_full_retrains`: the gate is the durable row
    itself (invariant #3), so a resumed process — and a later attempt in this one, e.g. after a
    dependency round that changed no code — never re-runs a canary it has already paid for."""
    if not code_digest:
        return False
    for e in events or []:
        if getattr(e, "type", None) != EV_EVAL_CANARY_FINISHED:
            continue
        d = getattr(e, "data", None) or {}
        # Keyed exactly as `evaluate.py::_durable_row_belongs` keys a durable row (the fold's own
        # node/generation rules), then on the code digest and an explicit `passed: true`.
        if (isinstance(d, dict) and coerce_node_id(d) == node_id
                and event_generation_binds(d, generation)
                and d.get("code_digest") == code_digest and d.get("passed") is True):
            return True
    return False


# ------------------------------------------------------------------- the IMPLICIT sandboxed smoke
# doc 78 leftover (2026-10-14). The canary needs `eval.canary`; an external agent that declared none
# got no preflight at all, and a SMOKE canary run with the real environment can write the real
# absolute paths a script names (doc 78 §5 tells the author to redirect them through `canary.env`).
# The SANDBOXED smoke closes both: when the run's OPERATOR opts in (`harness_policy.json`
# `implicit_canary: "sandboxed"`, owner token only) an undeclared task gets a smoke canary whose
# process tree may WRITE only its scratch tree — a Landlock ruleset (read anywhere, write the
# scratch, the device nodes minus `/dev/shm`, `/proc`, `/sys`), `TMPDIR` and the compiler caches
# pointed into the scratch. A refused write is NOT a verdict on the candidate: a smoke that died on
# a permission error is INCONCLUSIVE and the full eval proceeds; only a crash with no permission
# denial in its output (an unknown CLI flag, an ImportError) fails the canary.
# Without Landlock on the kernel there is no safe sandbox, so nothing runs and the contract says so.
IMPLICIT_CANARY_MODES = ("off", "sandboxed")
IMPLICIT_CANARY_TIMEOUT_S = 120.0
_POLICY_FILE = "harness_policy.json"
_DENIAL_MARKERS = ("Permission denied", "PermissionError", "EACCES", "[Errno 13]",
                   "Read-only file system", "Operation not permitted", "[Errno 30]")


def implicit_canary_policy(run_dir) -> dict:
    """`{mode, timeout}` from the run's operator policy (`harness_policy.json`); `off` when the file
    is absent, unreadable or says anything else. Read by the engine on every canary decision, so an
    operator switching it on takes effect at the next attempt without a restart."""
    import json
    from pathlib import Path

    try:
        data = json.loads((Path(run_dir) / _POLICY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"mode": "off"}
    mode = data.get("implicit_canary") if isinstance(data, dict) else None
    if mode not in IMPLICIT_CANARY_MODES:
        return {"mode": "off"}
    timeout = data.get("implicit_canary_timeout", IMPLICIT_CANARY_TIMEOUT_S)
    if type(timeout) not in (int, float) or not (0 < timeout <= 3600):
        timeout = IMPLICIT_CANARY_TIMEOUT_S
    return {"mode": mode, "timeout": float(timeout)}


def implicit_canary_unavailable() -> Optional[str]:
    """Why a sandboxed smoke cannot be built on this box, or None."""
    from looplab.runtime import landlock

    return landlock.unavailable_reason()


def implicit_canary_spec(timeout: float) -> dict:
    """The spec `_run_eval` runs for an undeclared task: a SMOKE whose writes are sandboxed."""
    return {"env": {CANARY_ENV: "1"}, "timeout": float(timeout), "mode": "smoke",
            "sandbox": True, "implicit": True}


def sandboxed_env(env: Optional[dict], scratch) -> dict:
    """`env` with the canary's write sandbox: a Landlock allow-list (`LOOPLAB_LANDLOCK_ALLOWLIST`,
    applied by `runtime/sandbox.py::run_argv` around every launch, which adds the launch's own cwd)
    and the scratch-local temp/cache directories. Reads keep what the engine's own allow-list
    granted (read-only), or everything when the run has none."""
    import os
    from pathlib import Path

    from looplab.runtime import landlock

    scratch = Path(scratch).resolve()
    tmp = scratch / ".canary-tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    out = dict(env or {})
    existing = landlock.parse_env(out.get(landlock.LANDLOCK_ENV) or "")
    rules = ([(path, "read") for path, _mode in existing] if existing else [("/", "read")])
    rules.append((str(scratch), "readwrite"))
    try:
        devices = sorted(n for n in os.listdir("/dev") if n not in ("shm", "mqueue"))
    except OSError:
        devices = []
    # Only real directories and device nodes: under an allow-list a rule the launcher cannot add
    # REFUSES the whole launch (`landlock.py::_LAUNCHER`), and `/dev` carries LINKS that resolve to a
    # pipe or nothing (`/dev/stderr` -> `/proc/self/fd/2` failed with EBADFD on this box). The
    # standard streams need no rule: they are inherited descriptors, which Landlock does not check.
    import stat as _stat
    for n in devices:
        path = os.path.join("/dev", n)
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            continue
        if _stat.S_ISDIR(mode) or _stat.S_ISCHR(mode) or _stat.S_ISBLK(mode):
            rules.append((path, "readwrite"))
    rules += [("/proc", "readwrite"), ("/sys", "readwrite")]
    out[landlock.LANDLOCK_ENV] = landlock.format_env(rules)
    for name in ("TMPDIR", "TMP", "TEMP", "TRITON_CACHE_DIR", "TORCHINDUCTOR_CACHE_DIR",
                 "MPLCONFIGDIR", "CUDA_CACHE_PATH"):
        out[name] = str(tmp)
    return out


# With no Python traceback in its output, how many of a stream's last non-empty lines may carry the
# denial: a shell tool's own refusal (`cp: cannot create regular file 'x': Permission denied`) is its
# last word, and a denial further up is something the run went on past.
_DENIAL_TAIL_LINES = 3
_LAUNCHER_DENIAL = "LOOPLAB landlock:"


def _death_chain(exceptions: list) -> list:
    """The exceptions the process DIED of: the last one printed, every one it was raised while
    handling (`chained`), and — when all of those are wrappers (torchrun's `ChildFailedError`, a
    `CalledProcessError`) — the chain printed just before them, which is what the wrapper reports."""
    from looplab.core.failure_forensics import WRAPPER_TYPES

    chain: list = []
    i = len(exceptions) - 1
    while i >= 0:
        chain.append(exceptions[i])
        while i > 0 and exceptions[i]["chained"]:
            i -= 1
            chain.append(exceptions[i])
        if not all(e["type"] in WRAPPER_TYPES for e in chain):
            break
        i -= 1
    return chain


def sandbox_denied(res) -> bool:
    """Did the sandboxed smoke die OF a REFUSED WRITE (inconclusive) rather than of its own code?

    The denial must be the CAUSE, not merely somewhere in the output: a smoke that logged a
    `Permission denied` from a probe it recovered from (a cache it could not write, a lock file it
    fell back from) and then died of an `ImportError` or an unknown CLI flag is exactly the defect the
    canary exists to catch, and reading the marker anywhere in either stream turned it into an
    inconclusive PASS. So: with a Python traceback, the denial is in the chain the process died of
    (`_death_chain`, over `core/failure_forensics.py::parse_exceptions`) — a `PermissionError`, or
    an exception whose line names the refusal (`_DENIAL_MARKERS`); without one, it is in the last
    `_DENIAL_TAIL_LINES` non-empty lines of stderr (stdout when stderr is empty). The launcher's own
    sentence (it could not build the sandbox at all) is the sandbox's by construction, wherever it is."""
    from looplab.core.failure_forensics import parse_exceptions

    stdout = str(getattr(res, "stdout", "") or "")
    stderr = str(getattr(res, "stderr", "") or "")
    if _LAUNCHER_DENIAL in stderr or _LAUNCHER_DENIAL in stdout:
        return True
    for text in (stderr, stdout):
        exceptions = parse_exceptions(text, source="")
        if exceptions:
            return any(any(marker in f"{e['qualified_type']}: {e['message']}"
                           for marker in _DENIAL_MARKERS)
                       for e in _death_chain(exceptions))
    tail = [line for line in (stderr or stdout).splitlines() if line.strip()][-_DENIAL_TAIL_LINES:]
    return any(marker in line for line in tail for marker in _DENIAL_MARKERS)
