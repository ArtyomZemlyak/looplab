"""The durable run-START record: its protocol, stated, and the two helpers that read it (doc 25 SR-01).

`/api/start` is a paid, non-idempotent effect — it materializes a run namespace and launches a
child process — so it carries the same claim -> terminal -> reconcile discipline as every other paid
route on this server: a durable record before the effect, exactly one observational answer after it,
and a lost-response replay that resolves to the SAME startup rather than buying a second one.

SR-01 named five hand-rolled copies of that discipline. `serve/paid_ledger.py` unified the two that
are EVENT ledgers (boss `report_refresh`, the concept lens) and its docstring draws the line this
module sits on the other side of: "the other three are FILE-ledger protocols ... they share the
vocabulary but not the storage, and deliberately stay separate". Variant (5) — this one — is a
RECORD-store protocol: its claim and its terminals are phases of one JSON sidecar written through
`commands.save_start_record`, not events folded out of `events.jsonl`. Stating it as a
`PaidLedgerSpec` would mean moving the start record into the event log, which is a change to how a
run start is made durable, not a de-duplication of how it is described.

So the VOCABULARY is shared — `conflict_policy` is `paid_ledger.FAIL_CLOSED`, the same constant with
the same meaning — and the storage is not. `StartRecordSpec` below states the rest: which fields
carry the request identity, which phases mean "Popen may already have happened", and which statuses
mean established / started / retryable. Every one of those sets used to be a brace literal inline in
a `build_router` closure, spelled two or three times each, which is what made the protocol
unreadable without reading all seven functions.

**Why these were closures and why that was the defect.** `_reconcile_start` and
`_inspect_keyed_start` captured `srv` and `root` from `build_router` and nothing else, so every
branch of a crash-window state machine — an observed-dead claim retired, a pre-Popen namespace
released, an idempotency key reused for a different proposal — was reachable ONLY by building the
whole ASGI app and driving HTTP. `serve/trace_clear.py` made exactly this move for variant (4) and
says the same thing.

Bodies are verbatim moves from `serve/routers/control.py`; the only edits are threading `srv`
(previously captured) as an explicit first argument, `srv.root` in place of the captured `root`, the
public names (a router calls these, so they are not private), and reading the phase/status sets off
`START_RECORD` instead of repeating them as literals.
"""
from __future__ import annotations

import json
import math
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import orjson
from fastapi import HTTPException

from looplab.events.eventstore import MAX_EVENT_BATCH_BYTES, decode_event_record
from looplab.serve.engine_proc import _engine_alive, _engine_liveness
from looplab.serve.paid_ledger import CONFLICT_POLICIES, FAIL_CLOSED


@dataclass(frozen=True)
class StartRecordSpec:
    """One paid startup's durable identity vocabulary and conflict policy.

    `request_digest_field` is None for a protocol whose identity is not request-bound, exactly as in
    `paid_ledger.PaidLedgerSpec.digest_field`: without it the ledger can prove a retry carries the
    same KEY but not that it asks for the same LAUNCH, and `inspect_keyed_start` then has nothing to
    refuse a re-used key on.

    The two phase families are the crash window's two halves and they are not symmetric. A record in
    `spawn_crossed_phases` may have a live child on the other side of Popen, so its ambiguity
    resolves to `uncertain` + `paid_effect_unknown` and never to a retry. A record in
    `pre_spawn_phases` provably has not spent anything, so an absent/dead claim resolves it to
    `not_started` and the namespace it reserved may be released.
    """

    key_digest_field: str
    request_digest_field: Optional[str]
    conflict_policy: str
    spawn_crossed_phases: frozenset[str]
    pre_spawn_phases: frozenset[str]
    established_statuses: frozenset[str]
    started_statuses: frozenset[str]
    retryable_statuses: frozenset[str]

    def __post_init__(self) -> None:
        if self.conflict_policy not in CONFLICT_POLICIES:
            raise ValueError(f"unknown start-record conflict policy: {self.conflict_policy!r}")
        if not self.started_statuses <= self.established_statuses:
            # `started` is the narrower claim ("a child was OBSERVED"), `established` the wider one
            # ("this startup owns the run name"). A spec where a started status is not established
            # would advertise `started: true` beside `ok: false`.
            raise ValueError("started statuses must be a subset of the established ones")
        if self.retryable_statuses & self.established_statuses:
            # Offering a retry for a startup that already owns the run name is the double-launch
            # this whole protocol exists to prevent.
            raise ValueError("an established startup is never retryable")

    @property
    def fails_closed(self) -> bool:
        return self.conflict_policy == FAIL_CLOSED


#: The one startup protocol this server speaks. FAIL_CLOSED because its terminals go through the
#: durable record store: an ambiguous record is evidence that a paid child MAY exist, and admitting
#: it as "not started" would buy a second one.
START_RECORD = StartRecordSpec(
    key_digest_field="idempotency_key_digest",
    request_digest_field="request_digest",
    conflict_policy=FAIL_CLOSED,
    spawn_crossed_phases=frozenset({"popen_pending", "popen_returned", "engine_observed"}),
    pre_spawn_phases=frozenset({"reserved", "materialized"}),
    established_statuses=frozenset({"accepted", "executing", "succeeded"}),
    started_statuses=frozenset({"executing", "succeeded"}),
    retryable_statuses=frozenset({"not_started", "failed"}),
)


def start_public(record: dict) -> dict:
    status = str(record.get("status") or "uncertain")
    # ``accepted`` proves only that Popen returned and its ownership evidence was persisted.  The
    # child is positively started only once its exact PID generation, engine lock, or run_started
    # event is observed.  Likewise, never advertise retry while a paid effect may have escaped.
    started = status in START_RECORD.started_statuses
    paid_effect_unknown = bool(record.get("paid_effect_unknown"))
    can_retry = (status in START_RECORD.retryable_statuses and not paid_effect_unknown
                 and record.get("namespace_released") is not False)
    result = {
        "ok": status in START_RECORD.established_statuses,
        "run_id": str(record.get("run_id") or ""),
        "start_id": str(record.get("id") or ""),
        "status": status,
        "started": started,
        "can_retry": can_retry,
        "paid_effect_unknown": paid_effect_unknown,
    }
    if record.get("validation_token"):
        result["validation_token"] = str(record["validation_token"])
    if record.get("error_code"):
        result["error"] = {"code": str(record["error_code"])}
    return result


def start_meta_id(rd: Path) -> str:
    path = rd / "ui_meta.json"
    if path.is_symlink():
        return ""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return ""
    return str(value.get("start_id") or "") if isinstance(value, dict) else ""


def release_unspawned_start_namespace(
        srv, rd: Path, *, start_id: str, task_file: Path) -> bool:
    """Remove only this request's pristine materialization before the Popen boundary.

    The caller holds ``commands.sequence(rd)`` and has already retired its exact PID-less claim.
    Any unexpected/reparse entry leaves the namespace intact and therefore fail-closed; the
    root-side start record remains as the durable audit receipt either way.
    """
    try:
        run_info = rd.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    attributes = int(getattr(run_info, "st_file_attributes", 0) or 0)
    try:
        invalid_run = (
            stat.S_ISLNK(run_info.st_mode) or not stat.S_ISDIR(run_info.st_mode)
            or bool(attributes & reparse_flag) or rd.resolve() != rd
            or rd.parent != srv.root)
    except OSError:
        return False
    if invalid_run:
        return False
    try:
        entries = list(rd.iterdir())
    except OSError:
        return False
    allowed = {"task.input.json", "ui_meta.json", "chat.jsonl"}
    if any(entry.name not in allowed for entry in entries):
        return False

    meta = rd / "ui_meta.json"
    if meta in entries:
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return False
        if (not isinstance(payload, dict)
                or str(payload.get("task_file") or "") != str(task_file)
                or (start_id and str(payload.get("start_id") or "") != start_id)
                or (not start_id and payload.get("start_id"))):
            return False

    for entry in entries:
        try:
            info = entry.lstat()
            entry_attributes = int(getattr(info, "st_file_attributes", 0) or 0)
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                    or bool(entry_attributes & reparse_flag)
                    or entry.resolve().parent != rd):
                return False
        except OSError:
            return False
    try:
        for entry in entries:
            entry.unlink()
        rd.rmdir()
    except OSError:
        return False
    return True


def has_first_run_started(rd: Path) -> bool:
    """Whether the first identity event is a durable, correlated ``run_started``.

    Current engines durably emit ``setup_started``/``setup_step`` immediately before their
    identity anchor; older valid engines emitted ``run_started`` at sequence zero.  Accept both
    layouts, but fail closed on a torn line, a malformed/unsupported envelope, a sequence gap,
    an unrelated pre-identity event, or a run id that does not name this exact directory.  A
    merely parseable ``{"type": "run_started"}`` is not process evidence.
    """
    path = rd / "events.jsonl"
    if path.is_symlink():
        return False
    try:
        with path.open("rb") as stream:
            expected_seq = 0
            total_bytes = 0
            for _ in range(4096):
                raw = stream.readline(MAX_EVENT_BATCH_BYTES + 1)
                if not raw:
                    return False
                total_bytes += len(raw)
                if (len(raw) > MAX_EVENT_BATCH_BYTES
                        or total_bytes > 2 * MAX_EVENT_BATCH_BYTES
                        or not raw.endswith(b"\n") or not raw.strip()):
                    return False
                physical = orjson.loads(raw)
                if (not isinstance(physical, dict)
                        or not {"v", "seq", "ts", "type", "data"} <= set(physical)):
                    return False
                for event in decode_event_record(physical, strict=True):
                    version = event.v
                    seq = event.seq
                    ts = event.ts
                    event_type = event.type
                    data = event.data
                    if (type(version) is not int or version != 1
                            or type(seq) is not int or seq != expected_seq
                            or isinstance(ts, bool) or not isinstance(ts, (int, float))
                            or not math.isfinite(ts) or ts <= 0
                            or not isinstance(event_type, str)
                            or not isinstance(data, dict)):
                        return False
                    expected_seq += 1
                    if event_type == "run_started":
                        run_id = data.get("run_id")
                        return isinstance(run_id, str) and run_id == rd.name
                    if event_type not in {"setup_started", "setup_step"}:
                        return False
            return False
    except (OSError, ValueError, TypeError, orjson.JSONDecodeError):
        return False


def reconcile_start(srv, rd: Path, record: dict) -> tuple[dict, dict]:
    """Fold durable run/claim evidence into one observational startup state.

    Callers hold ``commands.sequence(rd)``. This function may retire an observed/dead spawn
    claim and finish an explicitly recorded pre-Popen namespace cleanup, but never creates a
    directory, lease, event, or process.
    """
    updated = dict(record)
    start_id = str(updated.get("id") or "")
    meta_matches = start_meta_id(rd) == start_id
    liveness = _engine_liveness(rd)

    def transition(**changes) -> None:
        # Stable polling must be observational: publish a new timestamp only for an actual state
        # transition, not on every GET of the same evidence.
        if any(updated.get(key) != value for key, value in changes.items()):
            updated.update(changes)
            updated["updated_at"] = time.time()

    if (updated.get("status") == "failed"
            and updated.get("phase") == "failed_before_spawn"
            and updated.get("paid_effect_unknown") is False
            and updated.get("namespace_released") is False):
        evidence = srv.commands.observe_external_spawn(rd, f"start:{start_id}")
        if evidence in {"absent", "dead_or_cleared"} and liveness is False:
            released = release_unspawned_start_namespace(
                srv, rd, start_id=start_id, task_file=rd / "task.input.json")
            if released:
                transition(namespace_released=True)

    if meta_matches and has_first_run_started(rd):
        transition(status="succeeded", phase="event_observed", paid_effect_unknown=False,
                   error_code=None)
    elif meta_matches and (liveness is True
                           or (liveness is False and _engine_alive(rd))):
        transition(status="executing", phase="engine_observed", paid_effect_unknown=False,
                   error_code=None)
    elif str(updated.get("phase") or "") in START_RECORD.spawn_crossed_phases:
        evidence = srv.commands.observe_external_spawn(rd, f"start:{start_id}")
        # A start_id in ui_meta is the durable correlation between this sidecar and this run
        # directory.  An engine lock without it may belong to a manually replaced incarnation.
        if meta_matches and evidence in {"live", "pending_known"}:
            transition(status="executing", paid_effect_unknown=False, error_code=None)
        elif not meta_matches or evidence in {"uncertain", "mismatched"}:
            transition(status="uncertain", paid_effect_unknown=True,
                       error_code="start_uncertain")
        else:
            # Popen may already have crossed the provider boundary before dying. A new explicit
            # launch is possible only after review/revalidation; never call it automatically.
            transition(status="failed", phase="failed_after_spawn",
                       paid_effect_unknown=True, error_code="start_failed_after_spawn")
    elif str(updated.get("phase") or "") in START_RECORD.pre_spawn_phases:
        evidence = srv.commands.observe_external_spawn(rd, f"start:{start_id}")
        if evidence in {"absent", "dead_or_cleared"}:
            transition(status="not_started", paid_effect_unknown=False, error_code=None)
        else:
            transition(status="uncertain", paid_effect_unknown=True,
                       error_code="start_uncertain")
    if updated != record:
        srv.commands.save_start_record(rd, updated)
    return updated, start_public(updated)


def inspect_keyed_start(srv, rd: Path, key_digest: str, request_digest: str):
    record = srv.commands.load_start_record(rd)
    if record is None:
        return None, None, False
    same_key = secrets.compare_digest(
        str(record.get(START_RECORD.key_digest_field) or ""), key_digest)
    # Only a REQUEST-BOUND protocol can prove the retry is the same launch the key was minted for;
    # a spec with no digest field would be comparing "" to "" and adding nothing. Same distinction
    # `paid_ledger.PaidLedgerSpec.digest_field` draws, for the same reason.
    if (START_RECORD.request_digest_field is not None and same_key
            and not secrets.compare_digest(
                str(record.get(START_RECORD.request_digest_field) or ""), request_digest)):
        raise HTTPException(409, {
            "code": "idempotency_key_reused",
            "message": "this idempotency key belongs to a different launch request",
            "field_errors": {"idempotency_key": "generate a new key for the edited proposal"},
        })
    reconciled, public = reconcile_start(srv, rd, record)
    return reconciled, public, same_key


def raise_existing_start(public: dict, *, same_key: bool) -> None:
    status = str(public.get("status") or "uncertain")
    if not same_key:
        raise HTTPException(409, {
            "code": "run_id_conflict",
            "message": "this run name is already owned by another startup",
            "start_id": public.get("start_id"),
            "field_errors": {"run_id": "choose another run name"},
            "remediation": "Use the card that owns the existing startup, or choose another name.",
        })
    # FAIL_CLOSED, and this is where the policy decides: a startup that may have crossed Popen is
    # REFUSED rather than replayed. Under a first-terminal-wins policy the same evidence would be
    # allowed to launch again, which is a second paid child for one operator intent.
    if START_RECORD.fails_closed and (
            status == "uncertain" or public.get("paid_effect_unknown") is True):
        raise HTTPException(409, {
            "code": "start_uncertain",
            "message": "the earlier startup may have crossed Popen; observe it before retrying",
            "start_id": public.get("start_id"),
            "status": status,
            "paid_effect_unknown": bool(public.get("paid_effect_unknown")),
            "remediation": "Use the startup status endpoint; do not submit another launch.",
        })
    if status in START_RECORD.established_statuses:
        return
    if same_key:
        raise HTTPException(409, {
            "code": "start_not_completed",
            "message": "this startup did not establish a run",
            "start_id": public.get("start_id"),
            "remediation": "Review provider/error evidence, then validate again before a new launch.",
        })
