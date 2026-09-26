"""Finalize an evaluator invocation that SETTLED `ok` in a process that died before the terminal.

THE GAP (run minionerec-backbones-v10, node 0, 2026-09-25). `_eval_settle_outcome` appends
`eval_invocation_settled {"outcome": "ok"}` and only THEN, a few statements later, does
`_eval_write_terminal` append the node's one `node_evaluated`. A process killed between the two
(there: an external watchdog, one second after a 26,830 s training settled) left a node that is
still `pending` in the fold, so the resumed process dispatched it again. `_eval_prepare_workdir`
re-materialized the workdir — `rmtree`, `eval.log` with it — and the evaluator ran from scratch:
seven and a half GPU-hours re-bought for a number the log already said had been measured.

The settle row was the only durable trace, and nothing read it for that question:
`unsettled_eval_invocations` asks "was the invocation before this one left OPEN", and a settled one
reads as closed, i.e. as nothing at all.

THE RULE, in the order the recovery phase (`evaluate.py::_eval_recover_settled`) applies it — BEFORE
the workdir is touched, because the second source below lives in it:

  1. the settle row's own `result` — the parsed `RunResult` fields the terminal is written from, which
     `_settle_eval_invocation` now persists with every `ok` settle (`settled_result_record`). The
     terminal is then written from what the dead process measured, not from a re-read;
  2. for a settle row written before that column existed: the invocation's CAPTURED output as it
     survives in the node workdir (`eval.log`, the single-command path's live tee of stdout+stderr),
     re-read under the CURRENT spec by the same `command_eval.captured_result_fields` the live read
     uses — admitted only while nothing says the workdir was rebuilt since the settle, the log was
     written after the claim, and the reader is a pure text/file read (an `adapter`/`host_score`
     reader executes code, which a recovery must not do);
  3. neither: the evaluation re-runs, as it always did, but after a durable
     `eval_invocation_recovered {"action": "rerun", "reason": ...}` saying why the paid result could
     not be used — so a repeat is never silent.

Exactly one terminal: the recovery writes it through the ordinary `_eval_write_terminal`, and a
resumed process only reaches this phase for a node the fold still holds `pending`, so a second
resume after a recovered terminal is refused at ADMIT like any other closed lifecycle.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from looplab.core.node_evidence import read_bounded_regular_file
from looplab.runtime.sandbox import RunResult

# The actions and sources `eval_invocation_recovered` records — closed vocabularies, asserted at the
# append so a typo cannot mint a third answer.
RECOVERY_FINALIZED = "finalized"
RECOVERY_RERUN = "rerun"
RECOVERY_ACTIONS = frozenset({RECOVERY_FINALIZED, RECOVERY_RERUN})
SOURCE_SETTLE_RECORD = "settle_record"
SOURCE_WORKDIR_LOG = "workdir_log"
RECOVERY_SOURCES = frozenset({SOURCE_SETTLE_RECORD, SOURCE_WORKDIR_LOG})

# Bumped if the record's meaning changes; a reader refuses a version it does not know (rule 3).
SETTLED_RESULT_VERSION = 1

# Every `RunResult` field `_eval_write_terminal` (and the trust scan it runs) reads for a SUCCESS,
# minus the two output streams, which ride as bounded redacted tails. A record, not a pickle: each is
# a value the terminal already writes to the log in some form, so it is JSON by construction.
_RESULT_FIELDS = (
    "metric", "exit_code", "timed_out", "stalled", "drift", "extra_metrics",
    "extra_metrics_provenance", "extra_metrics_direction", "violations", "trials", "stages",
    "metric_subject", "eval_inputs", "applied_params", "effective_train_batch", "self_metric",
    "host_scorer",
    # The protocol facets' inputs (doc 68 §1, 2026-09-26), both as DIGESTS: the resolved profile's
    # facet (`comparability.py::digested_protocol` — never the override argv, which no other row
    # carries) and the digest of a printed `eval_fingerprint` (never the value), so a node finalized
    # from its settle record keeps the facets a live terminal records.
    "eval_protocol", "eval_fingerprint",
)

# How much of `eval.log` rule 2 reads: the live capture it stands in for keeps a ~64 KB tail per
# stream (`run_command_eval(max_output_bytes=64_000)`), and the log interleaves both streams.
_LOG_TAIL_BYTES = 128_000
# File timestamps come from a COARSE kernel clock (and a 2 s one on some filesystems), so a log first
# written milliseconds after its claim can read as older than it. The window it guards is hours wide.
_MTIME_SLACK_S = 2.0
# The readers rule 2 may run: each reads text or a file in the workdir and executes nothing.
_PURE_READER_KINDS = frozenset({"stdout_json", "stdout_regex", "file_json", "file_regex"})


def settled_result_record(res, *, stdout_tail: str, stderr_tail: str) -> dict:
    """The evidence an `ok` settle carries so a resumed process can write the terminal WITHOUT
    re-running the evaluator. The tails are the caller's (`_redacted_tail`, the one redact-then-cut
    order every durable tail takes); a stream too long for them loses its front, exactly as the
    terminal's own `stdout_tail` column does."""
    from looplab.engine.comparability import digested_protocol

    rec: dict[str, Any] = {"v": SETTLED_RESULT_VERSION}
    for name in _RESULT_FIELDS:
        value = getattr(res, name, None)
        if name == "eval_protocol":
            value = digested_protocol(value)
        if value is not None and value is not False:
            rec[name] = value
    rec["stdout_tail"] = stdout_tail or ""
    rec["stderr_tail"] = stderr_tail or ""
    return rec


def result_from_record(rec) -> tuple[Optional[RunResult], str]:
    """The `RunResult` a settle row's `result` stands for, or `(None, reason)`."""
    if not isinstance(rec, dict):
        return None, "settle_row_has_no_result"
    if rec.get("v") != SETTLED_RESULT_VERSION:
        return None, f"settle_result_version_unknown:{rec.get('v')!r}"
    metric = rec.get("metric")
    if not isinstance(metric, (int, float)) or isinstance(metric, bool):
        return None, "settle_result_has_no_metric"
    kwargs = {name: rec.get(name) for name in _RESULT_FIELDS
              if name not in ("metric", "exit_code", "timed_out", "stalled")}
    res = RunResult(exit_code=int(rec.get("exit_code") or 0),
                    stdout=str(rec.get("stdout_tail") or ""),
                    stderr=str(rec.get("stderr_tail") or ""),
                    metric=float(metric), timed_out=False,
                    stalled=bool(rec.get("stalled")), **kwargs)
    return res, ""


def _reader_kinds(spec) -> set:
    """Every `kind` named anywhere inside an eval-spec fragment (metric, extras, constraints)."""
    kinds: set = set()
    stack = [spec]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if isinstance(item.get("kind"), str):
                kinds.add(item["kind"])
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return kinds


def result_from_workdir_log(workdir, eval_spec, *, since: float, pipeline_stages,
                            enforce_drift: bool) -> tuple[Optional[RunResult], str]:
    """Rule 2: re-read the invocation's captured output from the node workdir under the CURRENT
    spec, or `(None, reason)`. Pure reads only; never raises."""
    from looplab.runtime import command_eval

    if not isinstance(eval_spec, dict) or not isinstance(eval_spec.get("metric"), dict):
        return None, "no_command_eval_spec"
    if pipeline_stages:
        # A staged pipeline's metric is read off the LAST stage's output, and which stage that was
        # (the engine may append a `score` stage) is not recoverable from the workdir alone.
        return None, "staged_pipeline_output_not_recoverable"
    if isinstance(eval_spec.get("host_scorer"), dict):
        return None, "host_scorer_not_recoverable"
    readers = {k: eval_spec.get(k) for k in ("metric", "metrics", "constraints", "cross_check")}
    unsafe = _reader_kinds(readers) - _PURE_READER_KINDS
    if unsafe:
        return None, "reader_executes_code:" + ",".join(sorted(unsafe))
    log = os.path.join(str(workdir), "eval.log")
    try:
        mtime = os.lstat(log).st_mtime
    except OSError:
        return None, "workdir_log_missing"
    if mtime < since - _MTIME_SLACK_S:
        return None, "workdir_log_predates_the_invocation"
    raw = read_bounded_regular_file(log, _LOG_TAIL_BYTES, tail=True)
    if raw is None:
        return None, "workdir_log_unreadable"
    out = raw.decode("utf-8", errors="replace")
    cwd = os.path.join(str(workdir), str(eval_spec.get("cwd") or "."))
    metric_spec = eval_spec["metric"]
    try:
        m = command_eval.read_metric(out, cwd, metric_spec, since=since)
        if m is None:
            return None, "metric_not_in_workdir_log"
        fields = command_eval.captured_result_fields(
            out, cwd, metric_spec, m, metrics=eval_spec.get("metrics") or None,
            constraints=eval_spec.get("constraints") or None,
            cross_check=eval_spec.get("cross_check"), enforce_drift=enforce_drift,
            drift_tolerance=float(eval_spec.get("drift_tolerance", 1e-6)), since=since)
    except Exception as exc:  # noqa: BLE001 — a reader fault means "no evidence", and the node re-runs as before
        return None, f"workdir_log_read_failed:{type(exc).__name__}"
    if fields.get("metric") is None:
        return None, "metric_withheld_by_drift_check"
    return RunResult(exit_code=0, stdout=out, stderr="", timed_out=False, **fields), ""
