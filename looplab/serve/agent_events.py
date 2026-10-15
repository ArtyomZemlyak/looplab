"""WAKING AN EXTERNAL AGENT: a durable outbox of actionable events, a long-poll read, and a hook.

WHY (audit 2026-10-14, L-01, S1). The harness was pull-only: an external agent learned that a node
finished, a stage failed or a monitor question opened only by polling. Codex closed that gap with
its own bridge — 33 call sites of `codex queue --thread <hard-coded id> --message <text>` spread
over supervisor/handoff scripts with a hand-kept `notification-ledger.json` — and the bridge was
where the night's incidents happened: bursts (5 notifications in 46 s), duplicates, stale alerts
25 minutes after the run they described had been replaced, failed deliveries never retried. And
when the agent's own turn died (`server_overloaded`), nothing woke it: 8 GPUs idled ~12 h.

WHAT. The UI server — the one long-lived LoopLab process — runs `AgentEventPump`, which derives
ACTIONABLE events from what LoopLab itself recorded (never from agent scripts) and appends them to
a durable outbox under the run root (`.agent-events/outbox.jsonl`):

  * from the event log of every external-harness run, per new row: `node_evaluated`,
    `node_failed` (with the failure forensics summary when recorded), `stage_failed` (a
    `stage_finished` row whose status is a failure), `run_paused`, `run_finished`;
  * from the run's harness-progress projection (the same read the agent would make):
    `monitor_question` per pending checkpoint the rules did not answer, `obligation_due` per
    admission/finish obligation, and `run_idle` — a live engine waiting for the agent's next
    experiment with node budget left;
  * from the host GPU queue: `gpu_free` once the pool has been free with nobody queued for
    `GPU_FREE_AFTER_S`.

Every row carries a monotonically increasing `id` (the CURSOR) and a dedupe `key`, so a restarted
server, two servers over one root or a re-derived condition never produce the same event twice.

Two ways to be woken, both reading that one outbox:

  * `GET /api/agent-events?after=<id>&timeout=<s>` (MCP `wait_for_next`) — a long poll that returns
    as soon as an event past the cursor exists (or after `timeout`, with none);
  * the NOTIFICATION HOOK: an operator-configured argv template (`.agent-events/notify.json`,
    `looplab agent-notify set`), e.g. `["codex", "queue", "--thread", "{thread}", "--message",
    "{message}"]`. The pump batches events (`debounce_s` of quiet, at most `max_delay_s` late),
    runs the command WITHOUT a shell, retries failures with backoff up to `max_attempts`, and
    records every attempt in `.agent-events/deliveries.jsonl` — the ledger the UI shows. The
    message is composed by LoopLab from structured fields only (ids, kinds, short summaries), passed
    through the persistent-text redactor, and names the cursor to read the details from; raw logs,
    tokens and environment values are never put on a command line.

Only the OPERATOR configures the command (owner token or CLI): a harness token may set only the
run's template variables (e.g. its thread id) and switch notifications for its run on or off,
because a command template is code the server executes.

NOT HERE: deciding anything. An event is a nudge to read LoopLab's own state; it grants no
permission, answers no checkpoint and starts no work.
"""
from __future__ import annotations

import json
import logging
import os
import re
import string
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from fastapi import HTTPException

from looplab.core.atomicio import append_jsonl_bytes_locked, file_identity
from looplab.events.eventstore import interprocess_lock

_log = logging.getLogger("looplab.server")

EVENTS_DIR = ".agent-events"
OUTBOX = "outbox.jsonl"
STATE = "state.json"
NOTIFY = "notify.json"
LEDGER = "deliveries.jsonl"
RUN_NOTIFY = "agent_notify.json"
HOOKS_DIR = "hooks"
# THE DEFAULT HOOK is the historical single `notify.json`; every other hook is a NAMED AGENT
# CONNECTION (doc 78 leftover, 2026-10-14): one per agent that wants waking — `codex`, `claude` —
# each with its own command, vars, kinds, run filter, delivery watermark and retry state, so a
# failing Codex bridge neither delays nor re-delivers what a Claude Code session is told.
DEFAULT_HOOK = "default"
_HOOK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")

ACTIONABLE_KINDS = (
    "node_evaluated", "node_failed", "stage_failed", "monitor_question", "obligation_due",
    "run_idle", "run_paused", "run_finished", "gpu_free",
)
# Kinds that describe a CONDITION that can stop holding (re-checked at delivery), as opposed to a
# FACT the log recorded (a node evaluated, a stage failed), which stays true once true.
CONDITION_KINDS = frozenset({"monitor_question", "obligation_due", "run_idle"})
# The outbox is a bounded tail: past this many bytes it is rotated to `outbox.jsonl.1` (one
# generation kept), and a reader whose cursor fell off the retained window is told so.
_OUTBOX_MAX_BYTES = 4 * 1024 * 1024
_LEDGER_MAX_BYTES = 2 * 1024 * 1024
_KEYS_PER_RUN = 512
_SUMMARY_CHARS = 300
GPU_FREE_AFTER_S = 60.0
# A run whose log did not change is re-derived at most this often (its time-based conditions —
# idle, pending questions — can still appear without a new event row).
_RECHECK_S = 30.0
_MIN_RUN_INTERVAL_S = 3.0
MAX_WAIT_S = 600

_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
# A var value never STARTS with `-`: a run's vars are writable by the harness token and land as a
# whole argv element of the operator's command, so `--thread {thread}` with `thread=--exec=x` would
# hand the agent an OPTION of the operator's program, not a value (review 2026-10-15).
_VAR_VALUE = re.compile(r"^(?!-)[A-Za-z0-9._:@/+=,-]{0,256}$")
_BUILTIN_FIELDS = frozenset({"message", "run_id", "runs", "kinds", "count", "after", "last_id",
                             "first_id"})
NOTIFY_DEFAULTS = {"enabled": True, "debounce_s": 15.0, "max_delay_s": 120.0, "max_batch": 20,
                   "max_attempts": 5, "timeout_s": 60.0, "retry_base_s": 30.0,
                   "kinds": list(ACTIONABLE_KINDS), "runs": None, "vars": {},
                   "message_chars": 1500}


# ------------------------------------------------------------------------------------- storage
def events_dir(root) -> Path:
    return Path(root) / EVENTS_DIR


def _lock(root):
    d = events_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    return interprocess_lock(d / ".lock", required=True)


def _read_json(path: Path, default):
    try:
        raw = path.read_bytes()
    except OSError:
        return default
    if len(raw) > 4 * 1024 * 1024:
        return default
    try:
        data = json.loads(raw)
    except ValueError:
        return default
    return data if isinstance(data, type(default)) else default


def _write_json(path: Path, data) -> None:
    tmp = Path(f"{path}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    try:
        with open(path, "rb") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue      # a torn tail line from a crash is skipped, never fatal
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _rotate_if_large(path: Path, cap: int) -> None:
    try:
        if path.stat().st_size > cap:
            os.replace(path, Path(f"{path}.1"))
    except OSError:
        pass


def read_outbox(root, *, after: int = -1, run_id: Optional[str] = None,
                kinds: Optional[Iterable[str]] = None, limit: int = 100) -> dict:
    """Rows with `id > after`, oldest first; the newest id; and whether `after` fell off the tail."""
    d = events_dir(root)
    rows = _read_jsonl(d / f"{OUTBOX}.1") + _read_jsonl(d / OUTBOX)
    wanted = set(kinds) if kinds else None
    newest = max((int(r.get("id", -1)) for r in rows if isinstance(r.get("id"), int)), default=-1)
    oldest = min((int(r.get("id", -1)) for r in rows if isinstance(r.get("id"), int)), default=-1)
    out = [r for r in rows if isinstance(r.get("id"), int) and r["id"] > after
           and (run_id is None or r.get("run_id") in (run_id, None))
           and (wanted is None or r.get("kind") in wanted)]
    out.sort(key=lambda r: r["id"])
    truncated = len(out) > limit
    out = out[:limit]
    # A cursor PAST the newest id means the outbox was removed or rebuilt under the reader: say so
    # and hand back the newest id, or the reader would wait forever for ids that will never come.
    stale_cursor = bool(after > newest)
    return {"events": out, "newest_id": newest,
            "next_after": out[-1]["id"] if out else (newest if stale_cursor else after),
            "has_more": truncated, "stale_cursor": stale_cursor,
            "gap": bool(after >= 0 and rows and oldest > after + 1)}


def append_events(root, candidates: list[dict], *, run_state: Optional[dict] = None) -> list[dict]:
    """Append candidate events not seen before (dedupe by `key`); returns what was appended.

    `run_state` = `{run_name: {"seq": int, "run_uid": str}}` watermarks to persist in the same
    critical section, so a crash never records a watermark past events it did not append."""
    d = events_dir(root)
    appended = []
    with _lock(root):
        state = _read_json(d / STATE, {})
        next_id = int(state.get("next_id", 0) or 0)
        seen = state.setdefault("keys", {})
        for cand in candidates:
            scope = str(cand.get("run_id") or "_host")
            keys = seen.setdefault(scope, [])
            if cand["key"] in keys:
                continue
            row = {"id": next_id, "ts": time.time(), **cand}
            next_id += 1
            keys.append(cand["key"])
            if len(keys) > _KEYS_PER_RUN:
                del keys[:len(keys) - _KEYS_PER_RUN]
            appended.append(row)
        if appended:
            _rotate_if_large(d / OUTBOX, _OUTBOX_MAX_BYTES)
            payload = b"".join(json.dumps(row, sort_keys=True).encode() + b"\n"
                               for row in appended)
            append_jsonl_bytes_locked(d / OUTBOX, payload)
        state["next_id"] = next_id
        if run_state:
            runs = state.setdefault("runs", {})
            for name, mark in run_state.items():
                runs[name] = {**runs.get(name, {}), **mark}
        _write_json(d / STATE, state)
    return appended


def run_watermarks(root) -> dict:
    return _read_json(events_dir(root) / STATE, {}).get("runs", {}) or {}


# ----------------------------------------------------------------------------------- derivation
def _clip(text, n: int = _SUMMARY_CHARS) -> str:
    from looplab.core.redact import redact_secrets

    text = " ".join(str(text or "").split())
    text = redact_secrets(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def derive_log_events(run_id: str, run_uid: str, events) -> list[dict]:
    """Actionable rows from NEW event-log rows (each keyed by its seq)."""
    from looplab.events.types import (EV_NODE_EVALUATED, EV_NODE_FAILED, EV_PAUSE,
                                      EV_RUN_FINISHED, EV_STAGE_FINISHED)
    from looplab.runtime.command_eval import STAGE_FAILED_STATUSES

    out = []
    for ev in events:
        data = ev.data if isinstance(getattr(ev, "data", None), dict) else {}
        base = {"run_id": run_id, "run_uid": run_uid, "seq": ev.seq,
                "key": f"{run_uid}:seq:{ev.seq}"}
        node = data.get("node_id")
        if ev.type == EV_NODE_EVALUATED:
            metric = data.get("metric")
            out.append({**base, "kind": "node_evaluated", "node_id": node,
                        "summary": f"node {node} evaluated: metric={metric}",
                        "details": {"metric": metric, "generation": data.get("generation")}})
        elif ev.type == EV_NODE_FAILED:
            forensics = data.get("failure_forensics") if isinstance(
                data.get("failure_forensics"), dict) else {}
            headline = forensics.get("headline") or data.get("reason") or ""
            out.append({**base, "kind": "node_failed", "node_id": node,
                        "summary": _clip(f"node {node} failed: {headline}"),
                        "details": {"reason": data.get("reason"),
                                    "generation": data.get("generation"),
                                    "error": _clip(data.get("error"), 400),
                                    **({"forensics": {k: forensics.get(k) for k in
                                                      ("headline", "signals", "exit_codes",
                                                       "root_exception")
                                                      if forensics.get(k)}}
                                       if forensics else {})}})
        elif ev.type == EV_STAGE_FINISHED and data.get("status") in STAGE_FAILED_STATUSES:
            out.append({**base, "kind": "stage_failed", "node_id": node,
                        "summary": _clip(f"node {node} stage {data.get('name')} "
                                         f"{data.get('status')} (exit {data.get('exit_code')})"),
                        "details": {"stage": data.get("name"), "status": data.get("status"),
                                    "exit_code": data.get("exit_code")}})
        elif ev.type == EV_PAUSE:
            out.append({**base, "kind": "run_paused",
                        "summary": _clip(f"run paused: {data.get('reason') or 'operator'}"),
                        "details": {"reason": data.get("reason")}})
        elif ev.type == EV_RUN_FINISHED:
            out.append({**base, "kind": "run_finished",
                        "summary": _clip(f"run finished: {data.get('reason') or ''}"),
                        "details": {"reason": data.get("reason")}})
    return out


def derive_condition_events(run_id: str, run_uid: str, progress: dict, *,
                            budget_left: Optional[int]) -> list[dict]:
    """Actionable CONDITIONS from one harness-progress projection (keyed on what makes them new)."""
    out = []
    seq = progress.get("event_seq")
    for q in progress.get("pending_checkpoints") or []:
        question = q.get("question") if isinstance(q.get("question"), dict) else q
        cid = question.get("checkpoint_id")
        if not cid:
            continue
        out.append({"run_id": run_id, "run_uid": run_uid, "seq": seq,
                    "kind": "monitor_question", "node_id": question.get("node_id"),
                    "key": f"{run_uid}:checkpoint:{cid}",
                    "summary": _clip(f"{question.get('phase_id')} question pending for node "
                                     f"{question.get('node_id')} stage {question.get('stage')}"),
                    "details": {"checkpoint_id": cid, "phase_id": question.get("phase_id"),
                                "stage": question.get("stage")}})
    revision = progress.get("evidence_revision")
    at_node = progress.get("at_node")
    step = progress.get("next_step") or {}
    idle = (step.get("code") == "choose_direction"
            and (progress.get("execution") or {}).get("engine_running") is True)
    dues = []
    if idle:
        dues += [b.get("phase_id") for b in progress.get("candidate_blockers_if_expanding") or []]
    if progress.get("finish_report_due"):
        dues.append("finish_report")
    dues += [f"finish_review:{r}" for r in progress.get("finish_reviews_due") or []
             if isinstance(r, str)]
    for phase in sorted({d for d in dues if d}):
        out.append({"run_id": run_id, "run_uid": run_uid, "seq": seq, "kind": "obligation_due",
                    "key": f"{run_uid}:obligation:{phase}:{at_node}:{revision}",
                    "summary": _clip(f"obligation due: {phase}"),
                    "details": {"phase_id": phase, "at_node": at_node}})
    if idle and (budget_left is None or budget_left > 0):
        out.append({"run_id": run_id, "run_uid": run_uid, "seq": seq, "kind": "run_idle",
                    "key": f"{run_uid}:idle:{seq}",
                    "summary": _clip("engine is idle waiting for the next experiment"
                                     + (f" ({budget_left} node(s) of budget left)"
                                        if budget_left is not None else "")),
                    "details": {"budget_left": budget_left, "at_node": at_node}})
    return out


# ------------------------------------------------------------------------------- notifications
def check_hook_name(hook: Optional[str]) -> str:
    """The hook's name, `default` for None; raises ValueError for anything else unsafe."""
    if hook is None or hook == "" or hook == DEFAULT_HOOK:
        return DEFAULT_HOOK
    if not isinstance(hook, str) or not _HOOK_NAME.match(hook):
        raise ValueError("hook names are 1-32 characters of [A-Za-z0-9_.-], starting alphanumeric")
    return hook


def _hook_path(root, hook: Optional[str]) -> Path:
    name = check_hook_name(hook)
    if name == DEFAULT_HOOK:
        return events_dir(root) / NOTIFY
    return events_dir(root) / HOOKS_DIR / f"{name}.json"


def read_notify_config(root, hook: Optional[str] = None) -> Optional[dict]:
    data = _read_json(_hook_path(root, hook), {})
    return data if data else None


def notify_hooks(root) -> dict[str, dict]:
    """Every configured hook by name: the default (`notify.json`) and each named connection."""
    out: dict[str, dict] = {}
    default = read_notify_config(root)
    if default:
        out[DEFAULT_HOOK] = default
    try:
        named = sorted((events_dir(root) / HOOKS_DIR).glob("*.json"))
    except OSError:
        named = []
    for path in named:
        name = path.stem
        if name == DEFAULT_HOOK or not _HOOK_NAME.match(name):
            continue
        config = _read_json(path, {})
        if config:
            out[name] = config
    return out


def validate_notify_config(body: dict) -> dict:
    """The one validator for a hook configuration (CLI, owner API). Raises ValueError."""
    if not isinstance(body, dict):
        raise ValueError("notify config must be an object")
    command = body.get("command")
    if isinstance(command, str):
        import shlex
        command = shlex.split(command)
    if (not isinstance(command, list) or not command or len(command) > 64
            or not all(isinstance(a, str) and 0 < len(a) <= 2048 for a in command)):
        raise ValueError("command must be a non-empty argv list of strings (no shell)")
    variables = body.get("vars") or {}
    _check_vars(variables)
    fields = _template_fields(command)
    unknown = fields - _BUILTIN_FIELDS - set(variables)
    if unknown:
        raise ValueError(f"command uses undefined placeholders: {sorted(unknown)} "
                         f"(built-ins: {sorted(_BUILTIN_FIELDS)}; others come from vars)")
    out = {**NOTIFY_DEFAULTS, "command": command, "vars": dict(variables)}
    for key in ("debounce_s", "max_delay_s", "timeout_s", "retry_base_s"):
        if key in body:
            value = float(body[key])
            if not (0 <= value <= 3600):
                raise ValueError(f"{key} must be within 0..3600 seconds")
            out[key] = value
    for key, top in (("max_batch", 200), ("max_attempts", 20), ("message_chars", 8000)):
        if key in body:
            value = int(body[key])
            if not (1 <= value <= top):
                raise ValueError(f"{key} must be within 1..{top}")
            out[key] = value
    if "enabled" in body:
        out["enabled"] = bool(body["enabled"])
    if body.get("kinds") is not None:
        kinds = list(body["kinds"])
        bad = [k for k in kinds if k not in ACTIONABLE_KINDS]
        if bad or not kinds:
            raise ValueError(f"unknown kinds {bad}; choose from {list(ACTIONABLE_KINDS)}")
        out["kinds"] = kinds
    if body.get("runs") is not None:
        runs = body["runs"]
        if not isinstance(runs, list) or not all(isinstance(r, str) and r for r in runs):
            raise ValueError("runs must be a list of run ids (or null for every external run)")
        out["runs"] = runs
    return out


def _check_vars(variables) -> None:
    if not isinstance(variables, dict) or len(variables) > 16:
        raise ValueError("vars must be an object with at most 16 entries")
    for name, value in variables.items():
        if not isinstance(name, str) or not _VAR_NAME.match(name) or name in _BUILTIN_FIELDS:
            raise ValueError(f"invalid var name {name!r}")
        if not isinstance(value, str) or not _VAR_VALUE.match(value):
            raise ValueError(f"var {name!r} must be a short token ([A-Za-z0-9._:@/+=,-]) "
                             "that does not start with '-'")


def _template_fields(command: list[str]) -> set[str]:
    fields = set()
    for arg in command:
        try:
            parsed = list(string.Formatter().parse(arg))
        except ValueError as exc:
            raise ValueError(f"invalid placeholder syntax in {arg!r}: {exc}") from None
        for _, field, spec, conv in parsed:
            if field is None:
                continue
            if not field or not _VAR_NAME.match(field) or spec or conv:
                raise ValueError(f"placeholders must be plain {{name}}, got {{{field}}}")
            fields.add(field)
    return fields


def write_notify_config(root, body: dict, hook: Optional[str] = None) -> dict:
    config = validate_notify_config(body)
    path = _hook_path(root, hook)
    name = check_hook_name(hook)
    with _lock(root):
        previous = _read_json(path, {})
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, config)
        state_path = events_dir(root) / STATE
        state = _read_json(state_path, {})
        hooks = state.get("delivery_hooks") if isinstance(state.get("delivery_hooks"), dict) else {}
        # A hook that was NOT delivering — removed, or switched off — resumes FROM NOW when it is
        # (re)configured to deliver: its old watermark sits behind every event that landed while it
        # was gone, and resuming there is a burst of old events past the debounce (each older than
        # `max_delay_s`), not a wake-up (review 2026-10-15). A reconfiguration of a hook that was
        # delivering keeps its position.
        was_delivering = bool(previous) and previous.get("enabled", True) is not False
        resumes = bool(config.get("enabled", True)) and not was_delivering
        if name != DEFAULT_HOOK:
            # A new connection is woken for what happens FROM NOW: pin its watermark at the
            # outbox's end when it is first configured (a reconfiguration keeps its position).
            pin = name not in hooks or resumes
        else:
            # The default hook's FIRST configuration keeps its historical start (the retained
            # outbox); only a default hook that had a watermark and stopped delivering is re-pinned.
            pin = resumes and isinstance(state.get("delivery"), dict)
        if pin:
            row = {"through": int(state.get("next_id", 0) or 0) - 1, "attempts": 0,
                   "next_try": 0, "failed": 0}
            if name == DEFAULT_HOOK:
                state["delivery"] = row
            else:
                hooks[name] = row
                state["delivery_hooks"] = hooks
            _write_json(state_path, state)
    return config


def clear_notify_config(root, hook: Optional[str] = None) -> None:
    """Remove a hook. A named connection's delivery watermark goes with it (per-connection state
    nothing else reads); a re-added hook is pinned FROM NOW by `write_notify_config`."""
    path = _hook_path(root, hook)
    name = check_hook_name(hook)
    with _lock(root):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        if name != DEFAULT_HOOK:
            state_path = events_dir(root) / STATE
            state = _read_json(state_path, {})
            hooks = state.get("delivery_hooks")
            if isinstance(hooks, dict) and name in hooks:
                del hooks[name]
                _write_json(state_path, state)


def _run_entry(data: dict) -> dict:
    out = {}
    if isinstance(data.get("vars"), dict):
        try:
            _check_vars(data["vars"])
            out["vars"] = dict(data["vars"])
        except ValueError:
            out["invalid"] = True
    if isinstance(data.get("enabled"), bool):
        out["enabled"] = data["enabled"]
    return out


def read_run_notify(rd, hook: Optional[str] = None) -> dict:
    """A run's override for one hook: the top-level `vars`/`enabled` are the DEFAULT hook's (the
    historical shape); `hooks: {name: {vars, enabled}}` holds a named connection's."""
    data = _read_json(Path(rd) / RUN_NOTIFY, {})
    name = check_hook_name(hook)
    if name == DEFAULT_HOOK:
        out = _run_entry(data)
        hooks = data.get("hooks")
        if isinstance(hooks, dict):
            named = {k: _run_entry(v) for k, v in hooks.items()
                     if isinstance(k, str) and _HOOK_NAME.match(k) and isinstance(v, dict)}
            if named:
                out["hooks"] = named
        return out
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    entry = hooks.get(name)
    return _run_entry(entry) if isinstance(entry, dict) else {}


def write_run_notify(rd, *, variables: Optional[dict] = None, enabled: Optional[bool] = None,
                     hook: Optional[str] = None) -> dict:
    name = check_hook_name(hook)
    data = _read_json(Path(rd) / RUN_NOTIFY, {})
    if name == DEFAULT_HOOK:
        current = {k: v for k, v in data.items() if k in ("vars", "enabled", "hooks")}
        target = current
    else:
        current = {k: v for k, v in data.items() if k in ("vars", "enabled", "hooks")}
        hooks = current.get("hooks") if isinstance(current.get("hooks"), dict) else {}
        target = dict(hooks.get(name) or {}) if isinstance(hooks.get(name), dict) else {}
        hooks = dict(hooks)
        hooks[name] = target
        current["hooks"] = hooks
    if variables is not None:
        _check_vars(variables)
        target["vars"] = dict(variables)
    if enabled is not None:
        target["enabled"] = bool(enabled)
    target.pop("invalid", None)
    _write_json(Path(rd) / RUN_NOTIFY, current)
    return read_run_notify(rd, name)


def compose_message(rows: list[dict], *, limit_chars: int, failed_before: int = 0) -> str:
    runs = sorted({r.get("run_id") or "host" for r in rows})
    head = (f"LoopLab: {len(rows)} new event(s)"
            + (f" in {len(runs)} runs" if len(runs) > 1 else f" in run {runs[0]}" if runs else "")
            + ".")
    lines = [head]
    for row in rows:
        where = f"[{row.get('run_id')}] " if row.get("run_id") and len(runs) > 1 else ""
        lines.append(f"- #{row['id']} {row['kind']}: {where}{row.get('summary', '')}")
    if failed_before:
        lines.append(f"({failed_before} earlier notification(s) could not be delivered.)")
    after = rows[0]["id"] - 1 if rows else -1
    lines.append(f"Read details: GET /api/agent-events?after={after} (MCP wait_for_next "
                 f"after={after}); then read run_progress for each run before acting.")
    text = "\n".join(lines)
    from looplab.core.redact import redact_secrets
    text = redact_secrets(text)
    if len(text) > limit_chars:
        text = text[: limit_chars - 40] + "\n… (truncated; read the outbox)"
    return text


def render_argv(command: list[str], fields: dict) -> list[str]:
    return [arg.format_map(fields) for arg in command]


def _child_env() -> dict:
    """The server's environment minus LoopLab's own credentials: a hook never sees the UI or
    harness token even when its command is a shell wrapper that prints its environment."""
    env = dict(os.environ)
    for name in list(env):
        if name in ("LOOPLAB_UI_TOKEN", "LOOPLAB_HARNESS_TOKEN") or (
                name.startswith("LOOPLAB_") and "TOKEN" in name):
            env.pop(name, None)
    return env


def ledger_rows(root, *, limit: int = 100, run_id: Optional[str] = None) -> list[dict]:
    d = events_dir(root)
    rows = _read_jsonl(d / f"{LEDGER}.1") + _read_jsonl(d / LEDGER)
    if run_id:
        rows = [r for r in rows if run_id in (r.get("runs") or [])]
    return rows[-limit:][::-1]


def _append_ledger(root, row: dict) -> None:
    d = events_dir(root)
    with _lock(root):
        _rotate_if_large(d / LEDGER, _LEDGER_MAX_BYTES)
        append_jsonl_bytes_locked(d / LEDGER, json.dumps(row, sort_keys=True).encode() + b"\n")


def _question_row(rd: Path, checkpoint_id) -> tuple[Optional[dict], list[dict]]:
    """The question row `checkpoint_id` names in the run's checkpoint journal and the journal rows
    that PRECEDED it (what `checkpoints.ask` saw when it decided), or `(None, [])`."""
    if not checkpoint_id:
        return None, []
    rows = _read_jsonl(Path(rd) / "harness_checkpoints.jsonl")
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        if row.get("type") == "question" and row.get("checkpoint_id") == checkpoint_id:
            return row, rows[:index]
    return None, []


def _condition_identity(row: dict) -> tuple:
    """WHAT a condition event says holds, for the delivery-time re-check — not its dedupe key.

    The dedupe key also names WHEN the condition was seen (`run_idle` the log's seq,
    `obligation_due` the node count and evidence revision), so it answers "is this the same
    sighting?", which is the outbox's question. Delivery asks "does it still hold?": an engine
    still idle after an unrelated diagnostic row, or a finish report still due after a new
    evidence revision, is still worth the wake-up — compared by key, every such append made a true
    condition read stale, dropped it and restarted the debounce, possibly forever (review
    2026-10-15). A question stays its own checkpoint id (the key already is exactly that)."""
    kind = row.get("kind")
    run_uid = row.get("run_uid")
    if kind == "run_idle":
        return (kind, run_uid)
    if kind == "obligation_due":
        return (kind, run_uid, (row.get("details") or {}).get("phase_id"))
    return (kind, row.get("key"))


def coalesce(rows: list[dict]) -> list[dict]:
    """One `monitor_question` per run in a batch: the newest, its summary saying how many it
    stands for — a burst of ticks is ONE reason to look, not N wake-up lines."""
    latest: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for row in rows:
        if row.get("kind") == "monitor_question":
            run = str(row.get("run_id"))
            counts[run] = counts.get(run, 0) + 1
            latest[run] = row
    out = []
    for row in rows:
        if row.get("kind") != "monitor_question":
            out.append(row)
            continue
        run = str(row.get("run_id"))
        if latest.get(run) is not row:
            continue
        if counts[run] > 1:
            row = {**row, "summary": _clip(f"{counts[run]} monitor questions; latest: "
                                           f"{row.get('summary', '')}"),
                   "coalesced": counts[run]}
        out.append(row)
    return out


# ------------------------------------------------------------------------------------- the pump
class AgentEventPump:
    """The server-owned producer of the outbox and deliverer of notifications. One thread."""

    def __init__(self, srv, *, interval: float = 2.0, clock=time.time, runner=None):
        self.srv = srv
        self.root = Path(srv.root)
        self.interval = interval
        self.clock = clock
        self.runner = runner or self._run_command
        self.stop_event = threading.Event()
        self.kick_event = threading.Event()
        self.thread = None
        self._run_marks: dict[str, tuple] = {}
        self._gpu_free_since: Optional[float] = None
        self._rule_progress: dict[tuple, float] = {}
        self._routine_seen: dict[str, bool] = {}
        self._lock = threading.Lock()

    # lifecycle
    def start(self):
        if os.environ.get("LOOPLAB_AGENT_EVENTS", "1") == "0":
            return
        self.thread = threading.Thread(target=self._loop, name="looplab-agent-events",
                                       daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.kick_event.set()
        if self.thread:
            self.thread.join(timeout=0.5)

    def kick(self):
        self.kick_event.set()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — a background producer must never take the server down
                _log.warning("agent event pump tick failed", exc_info=True)
            self.kick_event.wait(self.interval)
            self.kick_event.clear()

    # one pass
    def tick(self) -> list[dict]:
        with self._lock:
            appended = []
            try:
                children = sorted(p for p in self.root.iterdir()
                                  if p.is_dir() and not p.name.startswith("."))
            except OSError:
                children = []
            for rd in children:
                if self.stop_event.is_set():
                    break
                try:
                    appended += self._scan_run(rd)
                except Exception:  # noqa: BLE001 — one unreadable run must not stop the others
                    _log.debug("agent events: run %s skipped", rd.name, exc_info=True)
            try:
                appended += self._scan_gpu()
            except Exception:  # noqa: BLE001 — the GPU probe is advisory
                _log.debug("agent events: gpu probe failed", exc_info=True)
            try:
                self.deliver()
            except Exception:  # noqa: BLE001 — delivery failures are recorded per attempt
                _log.warning("agent notification delivery failed", exc_info=True)
            return appended

    def _signature(self, rd: Path):
        sig = []
        for name in ("events.jsonl", "harness_checkpoints.jsonl", "harness_reviews.jsonl",
                     "harness_decisions.jsonl", "engine.lock"):
            try:
                st = (rd / name).stat()
                sig.append(file_identity(st))
            except OSError:
                sig.append(None)
        return tuple(sig)

    def _scan_run(self, rd: Path) -> list[dict]:
        if not (rd / "events.jsonl").exists() or not (rd / "config.snapshot.json").exists():
            return []
        now = self.clock()
        sig = self._signature(rd)
        last = self._run_marks.get(rd.name)
        if last is not None:
            last_sig, last_at = last
            if now - last_at < _MIN_RUN_INTERVAL_S:
                return []
            if last_sig == sig and now - last_at < _RECHECK_S:
                return []
            if last_sig == sig:
                # Nothing on disk moved. Only a LIVE engine can make a time-based condition (idle,
                # an open question) newly true without a write, so a stopped run is not re-folded
                # every 30 s — on a root with dozens of runs that fold is the pump's whole cost.
                from looplab.engine.run_lifecycle import engine_liveness
                if engine_liveness(rd) is not True:
                    self._run_marks[rd.name] = (sig, now)
                    return []
        from looplab.core.config import read_config_snapshot
        from looplab.core.errors import OperatorRefusal
        try:
            settings = read_config_snapshot(rd / "config.snapshot.json")
        except (OperatorRefusal, OSError, ValueError):
            # An unreadable or foreign snapshot is simply not an external run THIS server can read.
            self._run_marks[rd.name] = (sig, now)
            return []
        if not getattr(settings, "external_harness", False):
            self._run_marks[rd.name] = (sig, now)
            return []
        from looplab.events.eventstore import EventStore
        from looplab.events.replay import fold
        from looplab.events.run_generation import run_generation_token

        events = EventStore(rd / "events.jsonl").read_all()
        if not events:
            self._run_marks[rd.name] = (sig, now)
            return []
        state = fold(events)
        run_uid = str(getattr(state, "run_uid", "") or rd.name)
        marks = run_watermarks(self.root).get(rd.name) or {}
        last_seq = marks.get("seq") if marks.get("run_uid") == run_uid else None
        if last_seq is None:
            # First sight of this run incarnation: start at its current end — a newly enabled pump
            # must not replay a whole history as "new" — but still report current CONDITIONS.
            new_events = []
        else:
            new_events = [ev for ev in events if ev.seq > int(last_seq)]
        candidates = derive_log_events(rd.name, run_uid, new_events)
        conditions = self._conditions(rd, settings, state, events, run_uid)
        if conditions is not None:
            candidates += conditions
        appended = append_events(self.root, candidates,
                                 run_state={rd.name: {"seq": events[-1].seq, "run_uid": run_uid}})
        # The signature taken BEFORE the read, never a fresh one: a row appended between
        # `read_all` and here is not in `events`, and a signature that already covered it would
        # read "nothing moved" on every later tick — for a stopped engine, forever (review
        # 2026-10-15). An append in that window now costs one more scan, which is the point.
        self._run_marks[rd.name] = (sig, now)
        return appended

    def _conditions(self, rd: Path, settings, state, events, run_uid: str) -> Optional[list]:
        """The run's actionable CONDITIONS right now (questions, obligations, idle), or None when
        the progress source is unreadable. Questions the agent's DECLARED monitor rules call
        routine are left out (see `_routine_by_rules`)."""
        from looplab.events.run_generation import run_generation_token
        try:
            from looplab.harness.progress import snapshot as progress_snapshot
            from looplab.serve.node_activity import public_node_activity
            progress = progress_snapshot(rd, run_generation_token(events),
                                         activity_reader=public_node_activity, limit=1)
            max_nodes = int(getattr(settings, "max_nodes", 0) or 0)
            add = int((getattr(state, "budget_overrides", {}) or {}).get("add_nodes", 0) or 0)
            budget_left = (max_nodes + add - len(state.nodes)) if max_nodes > 0 else None
            rows = derive_condition_events(rd.name, run_uid, progress, budget_left=budget_left)
        except (HTTPException, OSError, ValueError, KeyError, TypeError):
            # An incomplete source (a damaged sidecar, a generation race) yields log events only;
            # the agent's own progress read reports the source health.
            _log.debug("agent events: progress for %s unavailable", rd.name, exc_info=True)
            return None
        return [row for row in rows
                if not (row["kind"] == "monitor_question" and self._routine_by_rules(rd, row))]

    def _routine_by_rules(self, rd: Path, row: dict) -> bool:
        """Is this pending question ROUTINE under the agent's declared monitor rules?

        Live check 2026-10-14 (run minionerec-new-all-two-epochs-e7): an engine started BEFORE the
        rules shipped never applies them, so every train_monitor tick reached the agent as a wake-up
        although the agent had declared it routine. The pump evaluates the SAME rules
        (`harness/monitor_rules.py::evaluate`) on the question's observation and does not wake
        anyone for a routine one. It answers nothing — the question stays for the engine's own
        deadline or a newer engine's rule answer; this only decides whether waking is warranted.
        `progress_advances` compares against the progress the journal's RULE answers recorded for
        the same node lifecycle and phase before this question (`checkpoints.
        rule_previous_progress`, the reading `ask` decides with), and against the last progress
        this pump itself accepted (in memory: the only record when the engine applies no rules) —
        the HIGHER of the two, so the pump never calls routine a question whose progress `ask`
        already judged stalled (review 2026-10-15: with only its own memory, a question `ask`
        left open was routine here at `previous=None`, and nobody was woken)."""
        from looplab.harness.checkpoints import rule_previous_progress
        from looplab.harness.monitor_rules import evaluate, read_rules
        rules = read_rules(rd)
        if not rules:
            return False
        cid = (row.get("details") or {}).get("checkpoint_id")
        if cid in self._routine_seen:           # judged once: its own progress is not "previous"
            return self._routine_seen[cid]
        question, before = _question_row(rd, cid)
        if question is None:
            return False
        lifecycle = (rd.name, question.get("node_id"), question.get("node_generation"),
                     question.get("phase_id"))
        known = [p for p in (rule_previous_progress(before, question),
                             self._rule_progress.get(lifecycle)) if p is not None]
        previous = max(known) if known else None
        try:
            verdict = evaluate(rules, question, previous_progress=previous)
        except Exception:  # noqa: BLE001 — an unevaluable rule leaves the question to the agent
            verdict = None
        if verdict is not None:
            progress = (verdict.get("facts") or {}).get("progress")
            if isinstance(progress, (int, float)):
                self._rule_progress[lifecycle] = progress
        self._routine_seen[cid] = verdict is not None
        if len(self._routine_seen) > 4096:
            for key in list(self._routine_seen)[:1024]:
                del self._routine_seen[key]
        return verdict is not None

    def _still_actionable(self, rows: list[dict]) -> tuple[list[dict], list[dict]]:
        """`(keep, stale)`: re-derive each run's CONDITIONS at delivery time and drop condition
        events (question, obligation, idle) that no longer hold — a question already answered or
        closed, an obligation met, an engine no longer idle. Facts from the log (a node evaluated
        or failed, a stage failed, a pause/finish) and host events always stay."""
        from looplab.core.config import read_config_snapshot
        from looplab.core.errors import OperatorRefusal
        from looplab.events.eventstore import EventStore
        from looplab.events.replay import fold
        current: dict[str, Optional[set]] = {}
        keep, stale = [], []
        for row in rows:
            run_id = row.get("run_id")
            if row.get("kind") not in CONDITION_KINDS or not run_id:
                keep.append(row)
                continue
            if run_id not in current:
                keys = None
                rd = self.root / run_id
                try:
                    settings = read_config_snapshot(rd / "config.snapshot.json")
                    events = EventStore(rd / "events.jsonl").read_all()
                    state = fold(events)
                    run_uid = str(getattr(state, "run_uid", "") or run_id)
                    rows_now = self._conditions(rd, settings, state, events, run_uid)
                    keys = None if rows_now is None else {_condition_identity(r)
                                                          for r in rows_now}
                except (OperatorRefusal, OSError, ValueError):
                    keys = None
                current[run_id] = keys
            keys = current[run_id]
            if keys is None or _condition_identity(row) in keys:
                keep.append(row)          # unknowable now: deliver rather than silently drop
            else:
                stale.append(row)
        return keep, stale

    def _scan_gpu(self) -> list[dict]:
        from looplab.engine import resources
        from looplab.engine.gpu_queue import queue_status

        lease = resources.default_gpu_host_lease_path()
        if not Path(lease).exists():
            return []
        status = queue_status(lease)
        now = self.clock()
        if status["lease_held"] is False and not status["queue_length"]:
            if self._gpu_free_since is None:
                self._gpu_free_since = now
            elif now - self._gpu_free_since >= GPU_FREE_AFTER_S:
                key = f"gpu_free:{int(self._gpu_free_since)}"
                return append_events(self.root, [{
                    "run_id": None, "run_uid": None, "seq": None, "kind": "gpu_free",
                    "key": key, "summary": "the host GPU pool is free and nobody is queued",
                    "details": {"free_since": self._gpu_free_since}}])
        else:
            self._gpu_free_since = None
        return []

    # notifications
    def deliver(self) -> Optional[dict]:
        """Deliver every configured hook once; returns the default hook's ledger record (the
        historical return), else the first named hook's, else None."""
        records = {}
        for name, config in notify_hooks(self.root).items():
            try:
                records[name] = self._deliver_hook(name, config)
            except Exception:  # noqa: BLE001 — one connection's failure must not starve the others
                _log.warning("agent notify hook %s failed", name, exc_info=True)
        if records.get(DEFAULT_HOOK) is not None:
            return records[DEFAULT_HOOK]
        return next((r for r in records.values() if r is not None), None)

    def _deliver_hook(self, hook: str, config: dict) -> Optional[dict]:
        if not config or not config.get("enabled", True) or not config.get("command"):
            return None
        d = events_dir(self.root)
        state = _read_json(d / STATE, {})
        delivery = _hook_delivery(state, hook)
        through = int(delivery.get("through", -1))
        now = self.clock()
        if now < float(delivery.get("next_try", 0) or 0):
            return None
        page = read_outbox(self.root, after=through, kinds=config.get("kinds"),
                           limit=int(config.get("max_batch", 20)))
        rows = [r for r in page["events"] if self._run_wanted(config, r.get("run_id"))]
        if not rows:
            if page["events"]:
                self._save_delivery(state, through=page["events"][-1]["id"], attempts=0,
                                    next_try=0, failed=delivery.get("failed", 0), hook=hook)
            return None
        newest_ts = max(float(r.get("ts", 0)) for r in rows)
        oldest_ts = min(float(r.get("ts", 0)) for r in rows)
        if (now - newest_ts < float(config.get("debounce_s", 15))
                and now - oldest_ts < float(config.get("max_delay_s", 120))
                and not page["has_more"]):
            return None
        # NOTHING STALE WAKES AN AGENT (live check 2026-10-14): a condition is re-derived NOW, and a
        # question answered or closed during the debounce, an obligation met or an engine no
        # longer idle is dropped (recorded in the ledger, never sent); a burst of questions of one
        # run is ONE line. A retry is re-checked too: a question that closed while the hook was
        # failing is not worth a wake-up either.
        fresh, stale = self._still_actionable(rows)
        if stale and int(delivery.get("attempts", 0)) == 0:      # logged once, not per retry
            _append_ledger(self.root, {
                "ts": now, "attempt": 0, "status": "dropped_stale",
                "event_ids": [r["id"] for r in stale],
                "runs": sorted({r.get("run_id") for r in stale if r.get("run_id")}),
                "kinds": sorted({r["kind"] for r in stale}),
                "error": "no longer actionable at delivery (answered, closed or no longer due)",
                **({"hook": hook} if hook != DEFAULT_HOOK else {})})
        if not fresh:
            self._save_delivery(state, through=rows[-1]["id"], attempts=0, next_try=0,
                                failed=delivery.get("failed", 0), hook=hook)
            return None
        last_seen = rows[-1]["id"]
        # A RETRY sends only what has not been delivered: with several per-run var groups (one
        # thread per run), one failing group keeps `through` where it was, and before this every
        # retry re-sent the groups that had already succeeded — the duplicates the hook exists to
        # end (review 2026-10-15). `delivered` is the ids sent within the pending window; it is
        # cleared whenever the window closes (all sent, abandoned, or nothing left to send).
        delivered = {i for i in (delivery.get("delivered") or []) if isinstance(i, int)}
        rows = [r for r in coalesce(fresh) if r["id"] not in delivered]
        # Per-run variables: a run may name its own thread; one delivery per distinct var set.
        groups: dict[str, list[dict]] = {}
        merged_vars: dict[str, dict] = {}
        for row in rows:
            run_cfg = (read_run_notify(self.root / row["run_id"], hook)
                       if row.get("run_id") else {})
            if run_cfg.get("enabled") is False:
                continue
            variables = {**config.get("vars", {}), **run_cfg.get("vars", {})}
            gkey = json.dumps(variables, sort_keys=True)
            groups.setdefault(gkey, []).append(row)
            merged_vars[gkey] = variables
        attempts = int(delivery.get("attempts", 0)) + 1
        failed_before = int(delivery.get("failed", 0))
        all_ok, record = True, None
        for gkey, grows in groups.items():
            message = compose_message(grows, limit_chars=int(config.get("message_chars", 1500)),
                                      failed_before=failed_before)
            fields = {**merged_vars[gkey], "message": message,
                      "run_id": grows[0].get("run_id") or "",
                      "runs": ",".join(sorted({r.get("run_id") or "host" for r in grows})),
                      "kinds": ",".join(sorted({r["kind"] for r in grows})),
                      "count": str(len(grows)), "after": str(grows[0]["id"] - 1),
                      "first_id": str(grows[0]["id"]), "last_id": str(grows[-1]["id"])}
            try:
                argv = render_argv(config["command"], fields)
            except (KeyError, ValueError) as exc:
                result = {"ok": False, "exit_code": None, "error": f"template: {exc}"}
            else:
                result = self.runner(argv, float(config.get("timeout_s", 60)))
            record = {"ts": now, "attempt": attempts, "event_ids": [r["id"] for r in grows],
                      "runs": sorted({r.get("run_id") for r in grows if r.get("run_id")}),
                      "kinds": sorted({r["kind"] for r in grows}),
                      "status": "delivered" if result["ok"] else "failed",
                      "exit_code": result.get("exit_code"),
                      "error": _clip(result.get("error") or "", 400),
                      "command": config["command"][0], "message_chars": len(message)}
            if hook != DEFAULT_HOOK:
                record["hook"] = hook
            all_ok &= bool(result["ok"])
            if result["ok"]:
                delivered.update(r["id"] for r in grows)
            _append_ledger(self.root, record)
        last_id = last_seen
        if all_ok:
            self._save_delivery(state, through=last_id, attempts=0, next_try=0, failed=0,
                                hook=hook)
        elif attempts >= int(config.get("max_attempts", 5)):
            _append_ledger(self.root, {"ts": now, "attempt": attempts, "status": "abandoned",
                                       "event_ids": [r["id"] for r in rows],
                                       "runs": sorted({r.get("run_id") for r in rows
                                                       if r.get("run_id")}),
                                       "kinds": sorted({r["kind"] for r in rows}),
                                       "error": "gave up after max_attempts; the events stay in "
                                                "the outbox",
                                       **({"hook": hook} if hook != DEFAULT_HOOK else {})})
            self._save_delivery(state, through=last_id, attempts=0, next_try=0,
                                failed=failed_before + 1, hook=hook)
        else:
            backoff = float(config.get("retry_base_s", 30)) * (2 ** (attempts - 1))
            self._save_delivery(state, through=through, attempts=attempts,
                                next_try=now + min(backoff, 3600), failed=failed_before,
                                hook=hook, delivered=delivered)
        return record

    def _run_wanted(self, config, run_id) -> bool:
        runs = config.get("runs")
        return runs is None or run_id is None or run_id in runs

    def _save_delivery(self, _state, *, through, attempts, next_try, failed,
                       hook: str = DEFAULT_HOOK, delivered=()):
        d = events_dir(self.root)
        row = {"through": through, "attempts": attempts, "next_try": next_try, "failed": failed}
        if delivered:
            row["delivered"] = sorted(delivered)
        with _lock(self.root):
            state = _read_json(d / STATE, {})
            if hook == DEFAULT_HOOK:
                state["delivery"] = row
            else:
                hooks = state.get("delivery_hooks") if isinstance(state.get("delivery_hooks"), dict) else {}
                hooks[hook] = row
                state["delivery_hooks"] = hooks
            _write_json(d / STATE, state)

    @staticmethod
    def _run_command(argv: list[str], timeout: float) -> dict:
        try:
            proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=timeout, env=_child_env(), start_new_session=True,
                                  check=False)
        except subprocess.TimeoutExpired:
            return {"ok": False, "exit_code": None, "error": f"timed out after {timeout:.0f}s"}
        except OSError as exc:
            return {"ok": False, "exit_code": None, "error": f"could not start: {exc.strerror}"}
        tail = (proc.stderr or b"")[-400:].decode("utf-8", "replace")
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
                "error": "" if proc.returncode == 0 else tail}


def _hook_delivery(state: dict, hook: str) -> dict:
    """A hook's delivery watermark. A NEW named hook starts at the outbox's current end: it was
    configured now, and replaying the whole retained history into a fresh agent connection is a
    burst, not a wake-up (the default hook keeps its historical start)."""
    if hook == DEFAULT_HOOK:
        return state.get("delivery") or {}
    hooks = state.get("delivery_hooks") if isinstance(state.get("delivery_hooks"), dict) else {}
    row = hooks.get(hook)
    if isinstance(row, dict):
        return row
    return {"through": int(state.get("next_id", 0)) - 1}


def notify_status(root) -> dict:
    """The UI/API view: the configured hook (command shown, vars shown), delivery watermark, and
    the recent ledger. Nothing secret is stored here by construction (vars are short tokens).
    `hooks` lists every configured connection — the default and each named one."""
    config = read_notify_config(root)
    state = _read_json(events_dir(root) / STATE, {})
    hooks = {name: {"config": cfg, "delivery": _hook_delivery(state, name)}
             for name, cfg in notify_hooks(root).items()}
    return {"configured": bool(config), "config": config,
            "delivery": state.get("delivery") or {}, "newest_event_id": state.get("next_id", 0) - 1,
            "hooks": hooks, "ledger": ledger_rows(root, limit=50)}


def run_event_summary(root, run_id: str, *, limit: int = 20) -> dict:
    page = read_outbox(root, after=-1, run_id=run_id, limit=10**6)
    events = [e for e in page["events"] if e.get("run_id") == run_id][-limit:][::-1]
    return {"events": events, "notify": read_run_notify(Path(root) / run_id),
            "hooks": sorted(notify_hooks(root)),
            "ledger": ledger_rows(root, limit=limit, run_id=run_id)}


async def wait_for_events(pump: Optional[AgentEventPump], root, *, after: int, timeout: float,
                          run_id: Optional[str], kinds: Optional[list[str]], limit: int,
                          sleep=None, clock=time.monotonic) -> dict:
    """The long poll: return as soon as something past `after` exists, else after `timeout` with
    an empty page.

    The WAIT is on the event loop and only each outbox READ borrows a worker thread (review
    2026-10-15): the whole poll used to run in one `anyio.to_thread.run_sync`, so each waiting
    agent held one of the threadpool's 40 tokens for up to `MAX_WAIT_S`, and 40 idle polls starved
    every sync route of the server."""
    import anyio

    sleep = sleep or anyio.sleep
    deadline = clock() + max(0.0, min(float(timeout), MAX_WAIT_S))
    if pump is not None:
        pump.kick()
    while True:
        page = await anyio.to_thread.run_sync(
            lambda: read_outbox(root, after=after, run_id=run_id, kinds=kinds, limit=limit))
        if page["events"] or clock() >= deadline:
            page["timed_out"] = not page["events"]
            return page
        await sleep(min(1.0, max(0.05, deadline - clock())))


def describe_event_kinds() -> dict[str, Any]:
    return {
        "node_evaluated": "a node produced its terminal measurement (metric in details)",
        "node_failed": "a node ended without a measurement; details carry the engine's reason and "
                       "the failure forensics headline (root exception, signals, exit codes)",
        "stage_failed": "a pipeline stage ended with a failure status (exit code in details)",
        "monitor_question": "an evaluation checkpoint waits for the agent's answer (rules did not "
                            "answer it)",
        "obligation_due": "an admission or finish obligation is due before the next step",
        "run_idle": "the engine is running and waits for the agent's next experiment, with node "
                    "budget left",
        "run_paused": "the run was paused (reason in details)",
        "run_finished": "the run recorded its finish",
        "gpu_free": "the host GPU pool has been free with nobody queued for a minute",
    }
