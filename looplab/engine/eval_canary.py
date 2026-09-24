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
    seconds are charged to the node's eval seconds exactly as the full eval's are;
  * under the attempt's intervention watcher, so an operator abort / reset kills it like the eval.

A FAILED canary is the attempt's crash: RUN_ATTEMPT hands SETTLE_OUTCOME a metric-less `RunResult`
built by `canary_failure_result` (its output as the evidence, the full eval never started), and
the attempt goes through the ordinary triage/repair path. SALVAGE is skipped for it by name — no
rung may turn anything a canary produced into the node's number — and the result carries no stage
rows and no `failed_stage`, so the repair's reuse predicate can never "reuse" a stage the node's
workdir never ran (it answers a full re-run). A PASSED canary lets the full eval start in the same
attempt; its metric is discarded on the spot and is never recorded anywhere but the diagnostic row.

REPLAY / RESUME. Two DIAGNOSTIC rows (`events/types.py::EV_EVAL_CANARY_STARTED` /
`EV_EVAL_CANARY_FINISHED`) keyed on (node, generation, code digest). `canary_already_passed` reads
the log for a `passed` row under the same key, so a resumed process does not re-run a canary it has
already paid for on the same code; a repaired node has a new digest and is canaried again, which is
the point — the repair is exactly the code nobody has run yet.

NOT HERE: selection, the fold, the metric. A canary's number never reaches `node_evaluated`.
"""
from __future__ import annotations

import threading
import time
from typing import Iterable, Optional

from looplab.core.models import coerce_node_id
from looplab.events.replay import event_generation_binds
from looplab.events.types import EV_EVAL_CANARY_FINISHED

# THE MARKER every canary launch carries, set by the engine and never declarable (`core/envsafe.py`
# refuses `LOOPLAB_*` at every declaring level): the eval script reads it to run its tiny slice.
CANARY_ENV = "LOOPLAB_CANARY"

# How much of the canary's stdout/stderr the failure result carries. The downstream windows
# (`_eval_failure_text`, the durable evidence) cut again, so this only bounds memory.
_CANARY_OUTPUT_CHARS = 200_000


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
    return {"env": {**env, CANARY_ENV: "1"}, "timeout": timeout}


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


def canary_passed(res, *, expired: bool = False) -> bool:
    """THE PASS RULE, and it is the full eval's own success rule (`_eval_settle_outcome`): the chain
    exited 0, was not killed by a clock, and the operator's reader found a number. The number itself
    is then discarded — it measured a slice."""
    if res is None or expired:
        return False
    return (getattr(res, "metric", None) is not None and not getattr(res, "timed_out", False)
            and getattr(res, "exit_code", 1) == 0)


def canary_failure_detail(res, *, expired: bool, timeout: float) -> str:
    """One sentence naming HOW the canary failed, from the engine's own record of it."""
    if res is None:
        return "the canary produced no result"
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


def canary_failure_result(res, *, detail: str, log_dir: str, env_names: Iterable[str]):
    """The metric-less `RunResult` a FAILED canary hands SETTLE_OUTCOME as the attempt's result.

    Deliberately NARROW: exit code non-zero (a clean exit that printed no number is still a failure
    of the path, and `crash` is the reason every repair gate reads), `timed_out` False (a canary
    timeout is evidence about the code, not the node's deadline), NO stage rows and NO
    `failed_stage` (the node's workdir ran nothing, so no reuse decision may stand on it), and every
    measurement field left at its default so nothing downstream can carry a canary number. The
    canary's own output is the evidence, framed so the repair and the triage judge read it as what
    it is."""
    from looplab.runtime.command_eval import RunResult
    names = ", ".join(sorted(env_names))
    header = (f"[eval canary] The canary preflight FAILED — {detail}. The canary is this node's own "
              f"eval pipeline run on the task's tiny slice (env: {names}) in a scratch directory; "
              "the FULL evaluation was NOT started. Fix the defect below so the canary passes. "
              f"Canary logs: {log_dir}\n")
    code = getattr(res, "exit_code", None) if res is not None else None
    stdout = (getattr(res, "stdout", "") or "") if res is not None else ""
    stderr = (getattr(res, "stderr", "") or "") if res is not None else ""
    return RunResult(
        exit_code=(code if isinstance(code, int) and code != 0 else 1),
        stdout=stdout[-_CANARY_OUTPUT_CHARS:],
        stderr=header + stderr[-_CANARY_OUTPUT_CHARS:]
        + f"\n[eval canary] ({detail}; the full evaluation was not started)",
        metric=None, timed_out=False)


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
