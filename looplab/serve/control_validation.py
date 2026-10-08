"""HTTP control-payload validation: the per-event registry and `normalize_control` (doc 25 SC-01).

This is the boundary between an HTTP request and an append to the event log, and nothing else.
`run_commands.py` — the durable command lifecycle this was carved out of — keeps the record store,
the spawn leases and the `RunCommandService` worker.  It imports this module; this module imports
nothing from it, so the validator can be exercised (and re-executed as a fresh module by its
registry guard test) without dragging the command service's collaborators in behind it.

The cut is the section boundary doc 25 SC-02 left behind.  Before SC-02 the per-event behaviour was
three flat if/elif chains, and there was no seam to cut along; naming the 35 rules is what turned a
region of one 4,600-line module into a file.

`task_file_for` travels with the validator because `_normalize_restart` is one of its three callers
(the other two are `RunCommandService._spawn` and `._claim_restart_spawn`) and ONE implementation is
the whole point of that name (see its docstring): a second spelling for the command service would be
exactly the drift it exists to prevent.  `run_commands` re-exports it.

PATCH SEAMS.  `run_commands` binds the names it CALLS by value (`normalize_control`, `_error`,
`CONTROL_SPECS`, `EnginePolicy`, `_normalize_finalize_data`, `task_file_for`) and re-exports nothing
else — a re-export of a name only this module uses would look like a patch seam while a
monkeypatch through it missed the validator entirely.  The one name a test does patch is
`_card_resource_envelope`, whose two consumers straddle the two modules (intake normalization here,
the append-time re-check in `RunCommandService`); `run_commands` reaches it through the MODULE
object for exactly that reason, so a single
`monkeypatch.setattr("looplab.serve.control_validation._card_resource_envelope", ...)` is still
observed by both.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from fastapi import HTTPException
from pydantic import ValidationError

from looplab.core.hardware import detect_gpus, gpu_free_mib_uncached
from looplab.core.numeric import LAUNCH_TIMEOUT_LIMIT_S
from looplab.core.models import (
    CARD_STATEMENT_MAX_CHARS,
    Idea, IdeaEmission, durable_idea_payload, effective_card_footprint, idea_field_carried,
    idea_proposal_digest,
)
from looplab.core.redact import redact_secrets
from looplab.events.node_import import NAME_NOT_TEXT, node_import_payload, portable_relative_name
from looplab.events.comment_projection import (
    COMMENT_ID_RE, COMMENT_MAX_PER_NODE_GENERATION, COMMENT_MAX_PER_RUN, COMMENT_MAX_VERSION,
    normalize_comment_text)
from looplab.events.finalize_scope import is_guarded_abort
from looplab.events.types import (
    EV_ANNOTATION, EV_APPROVAL_GRANTED, EV_BUDGET_EXTEND, EV_DEEP_RESEARCH,
    EV_CARD_DROPPED, EV_CARD_EDITED, EV_CARD_REOPENED, EV_CARD_REPRIORITIZED,
    EV_CARD_RESOURCE_PINNED,
    EV_COMMENT_CREATED, EV_COMMENT_EDITED, EV_COMMENT_RESOLUTION_CHANGED, EV_CONCEPT_TAG_EDITED,
    EV_FORCE_ABLATE, EV_FORCE_CONFIRM, EV_FORK, EV_HINT, EV_HYPOTHESIS_ADDED,
    EV_HYPOTHESIS_UPDATED, EV_INJECT_NODE, EV_METRIC_RETARGET, EV_NODE_ABORT, EV_NODE_RESET,
    EV_TRACK_REQUESTED,
    EV_PAUSE, EV_PROMOTE, EV_RESTART, EV_RESUME, EV_RUN_ABORT, EV_RUN_CONCEPTS, EV_RUN_REOPENED,
    EV_SET_STRATEGY, EV_SPEC_APPROVED, EV_RESEARCH_COMPLETED, EV_REPORT_GENERATED)
from looplab.serve.engine_proc import _resolve_task_file
from looplab.serve.http import refusal
# `EnginePolicy` is DEFINED in `serve/protocol.py` (2026-09-26) and imported here as the same class:
# its values are persisted on every durable command record (`engine_policy`), and `looplab stop
# --wait` reads those records to tell whether a command will start an engine after the current one
# exits — from the CLI, which cannot import this module without the `[ui]` extra (fastapi).
from looplab.serve.protocol import (COLLABORATION_EVENTS, CONTROL_EVENTS, EnginePolicy,
                                   CONTROL_DATA_FIELDS, CONTROL_SERVER_DERIVED_FIELDS)


@dataclass(frozen=True)
class ControlSpec:
    """Everything one control event type decides, in ONE record (doc 25 SC-02).

    `engine_policy`/`postcondition` say what the command service does around the append;
    `data_fields` is the payload allow-list; the three callables are the event's own behaviour at
    the three points where it differs from every other event — intake normalization, the append-time
    precondition recheck, and the engine decision.  Those three used to be flat if/elif chains over
    the same event types in three different places, tied to the registry by nothing at all: a new
    control event could ship having been added to only one of them, and two of the branches
    re-spelled a field allow-list that had already drifted from `data_fields`.

    `None` means "this event has no rule of its own here" and is an EXPLICIT choice, not an
    omission: the tables below are asserted complete against `CONTROL_EVENTS`, so a new member
    cannot inherit another event's handler or fall into an unsafe default by being forgotten.
    """
    event_type: str
    engine_policy: EnginePolicy
    postcondition: str
    data_fields: frozenset[str]
    # (intake) -> normalized data
    normalize: Optional[Callable[["_ControlIntake"], dict]]
    # (state, event_type, data, envelope) -> error dict or None
    precondition: Optional[Callable[..., Optional[dict]]]
    # (service, rd, event_type, state, alive, pending_finalize) -> (decision, error) or None
    decide: Optional[Callable[..., Optional[tuple]]]


_LIFECYCLE_CONTROL_TARGETS = {
    EV_NODE_ABORT: "node_id",
    EV_NODE_RESET: "node_id",
    EV_APPROVAL_GRANTED: "node_id",
    EV_FORCE_CONFIRM: "node_id",
    EV_FORCE_ABLATE: "node_id",
    EV_FORK: "from_node_id",
    EV_PROMOTE: "node_id",
}
_ABSENT = object()

# The cross-run import fields are CONSUMED by `_normalize_inject_node`'s import block, which POPS
# them: they are inputs to normalization, never event data.  Named once here because two sites must
# agree about them — the pop, and the residual allow-list that refuses whatever the pop did not
# take.  See `_normalize_inject_node` for why that residual check is not `data_fields` itself.
_INJECT_IMPORT_FIELDS = frozenset({"source_run", "source_node"})


def task_file_for(rd: Path) -> Optional[str]:
    """Resolve the immutable run snapshot, with a safe existing-file legacy fallback.

    ONE implementation, in `engine_proc`, deliberately: this used to be a hand-maintained copy of the
    same snapshot->ui_meta fallback, and two copies of a resolution rule drift the moment one gains a
    validation the other lacks — while the spawn path and the command path would then disagree about
    which task a run is. `routers/control.py` already aliases the engine_proc one; this name stays as
    the public re-export the command layer and its tests import.
    """
    return _resolve_task_file(rd)


def _normalize_finalize_data(data: dict) -> dict:
    unknown = set(data) - {"reason"}
    if unknown:
        raise HTTPException(400, f"run_abort has unknown field(s): {', '.join(sorted(unknown))}")
    if "reason" in data and data.get("reason") is None:
        raise HTTPException(400, "run_abort.reason must not be null")
    reason = data.get("reason", "finalized")
    if not isinstance(reason, str):
        raise HTTPException(400, "run_abort.reason must be a string")
    reason = reason.strip()
    if not reason or len(reason) > 256:
        raise HTTPException(400, "run_abort.reason must be non-empty and at most 256 characters")
    return {"reason": reason}


def _card_resource_envelope() -> tuple[int, tuple[int, ...]]:
    """Return the server-visible GPU count and a complete free-memory envelope when known.

    The server and spawned engine inherit the same ``CUDA_VISIBLE_DEVICES`` fence. ``nvidia-smi``
    reports physical ids, so a numeric fence is joined explicitly; UUID fences retain a trustworthy
    count but intentionally degrade memory validation to count-only. Detection is best-effort and
    fails closed to a zero-GPU envelope for new operator pins.
    """
    cvd = os.environ.get("CUDA_VISIBLE_DEVICES")
    tokens: list[str] | None = None
    if cvd is not None:
        tokens = [token.strip() for token in cvd.split(",") if token.strip()]
        if len(tokens) == 1 and tokens[0].lower() in {"-1", "none", "nodevfiles", "void"}:
            return 0, ()
    try:
        # detect_gpus() is process-cached — fine for the static identity/count, but its mem_free_mib is
        # frozen at the first probe that answered (doc 69 69.23a: a failed probe is retried, not
        # cached), so as the current admission envelope it would accept a pin after memory
        # was consumed (or reject one after it was freed). Take the count/identity from the cache but
        # source the FREE-memory envelope ONLY from a fresh, UNCACHED nvidia-smi query at admission time.
        # This is a serve control path (not a fold path) and resource pins are rare operator actions, so
        # the extra subprocess query is acceptable; when the live query is absent/partial we degrade that
        # device (or the whole join) to COUNT-ONLY rather than fall back to stale server-start telemetry.
        rows = detect_gpus()
    except Exception:  # noqa: BLE001 - validation remains count-safe when inventory is unavailable
        rows = []
    try:
        live_free = gpu_free_mib_uncached()
    except Exception:  # noqa: BLE001 - stay count-safe if the live query is unavailable
        live_free = {}
    # even a successful live probe is a different authority from the Engine scheduler,
    # which initializes `_gpu_mem` once via detect_gpu_inventory/detect_gpus. A pin accepted after VRAM
    # is freed can still wait forever against the engine's older lower ceiling, while the opposite drift
    # can admit work the host no longer fits. Admission and scheduling need one capacity/reservation model.
    memory_by_id = {}
    for row in rows:
        if not isinstance(row, dict) or type(row.get("index")) is not int:
            continue
        idx = row["index"]
        # Live free VRAM only — no fallback to the cached startup `memory.free`. That cached value is not
        # a conservative ceiling (it can exceed today's free memory and accept an unsafe pin), so a device
        # the live probe cannot see is left out of memory_by_id and degrades to count-only below, rather
        # than being admitted against stale server-start telemetry.
        free = live_free.get(idx)
        if type(free) is int and free >= 0:
            memory_by_id[idx] = free
    if tokens is not None:
        count = len(tokens)
        if all(token.isdecimal() and int(token) in memory_by_id for token in tokens):
            return count, tuple(memory_by_id[int(token)] for token in tokens)
        return count, ()
    if rows:
        indices = [row.get("index") for row in rows if isinstance(row, dict)]
        if (len(indices) == len(rows) and len(set(indices)) == len(indices)
                and all(index in memory_by_id for index in indices)):
            return len(rows), tuple(memory_by_id[index] for index in indices)
        return len(rows), ()
    try:
        import torch  # optional; mirrors the engine's count fallback
        return max(0, int(torch.cuda.device_count())), ()
    except Exception:  # noqa: BLE001
        return 0, ()


def _error(code: str, message: str, remediation: str = "", *, retryable: bool = False) -> dict:
    return {"code": code, "message": redact_secrets(str(message)), "retryable": bool(retryable),
            "remediation": redact_secrets(str(remediation))}


# =================================================================================================
# Per-event control behaviour: one function per event type per concern, registered in the tables at
# the end of this section.
#
# Before doc 25 SC-02 the same event types were walked by three separate flat if/elif chains — a
# ~780-line `normalize_control`, a second chain re-checking cards/comments/nodes immediately before
# the append, and a third choosing the engine action — none of which the two registries knew
# about.  The tables now own the dispatch, so `CONTROL_EVENTS` completeness is asserted for
# BEHAVIOUR exactly the way it already was for engine policy and payload fields.
# =================================================================================================


class _ControlIntake:
    """The coercion helpers shared by every per-event normalizer.

    These were closures inside `normalize_control` over `srv`/`rd`/`data`; this is the same scope
    made passable, so each event's rules can live in a function of its own.  `state` stays LAZY for
    the reason it always was: most payloads are refused on shape before any fold is needed, and the
    ones that are not must all observe the SAME fold rather than re-reading a log that a live engine
    is still appending to.

    The helpers read `self.data`, so a handler that REBINDS the payload (rather than mutating it in
    place) must write the new dict back through `ctx.data` before calling another helper.
    """

    __slots__ = ("srv", "rd", "event_type", "data", "_state", "_tail_seq")

    def __init__(self, srv, rd: Path, event_type: str, data: dict):
        self.srv = srv
        self.rd = rd
        self.event_type = event_type
        self.data = data
        self._state = None
        self._tail_seq = None

    def state(self):
        if self._state is None:
            self._state = self.srv.state(self.rd)
        return self._state

    def tail_seq(self) -> int:
        """The seq of the last event currently in the log, or -1 for an empty one.

        LAZY and deliberately separate from `state()`: it costs a second read of `events.jsonl`, so
        only the one rule that needs it pays (`_normalize_fork_receipt`, bounding the vantage point
        an operator claims to have branched from). It reads through `srv.events` — the same seam
        `routers/control.py` computes its pre-normalization approval baseline from — rather than
        deriving a tail from the folded state, which does not carry one.
        """
        if self._tail_seq is None:
            events = self.srv.events(self.rd)
            self._tail_seq = events[-1].seq if events else -1
        return self._tail_seq

    def strict_integer(self, value, name: str) -> int:
        if isinstance(value, bool):
            raise HTTPException(400, f"{name} must be an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
            try:
                return int(value.strip())
            except (ValueError, OverflowError):
                pass
        raise HTTPException(400, f"{name} must be an integer")

    def integer(self, name: str, *, required: bool = True) -> Optional[int]:
        value = self.data.get(name)
        if value is None and not required:
            return None
        return self.strict_integer(value, name)

    def node(self, name: str, *, required: bool = True) -> Optional[int]:
        value = self.integer(name, required=required)
        if value is not None and value not in self.state().nodes:
            raise HTTPException(404, f"no node #{value} in this run")
        return value

    def card(self):
        card_id = self.text("id", limit=256)
        card = self.state().cards.get(card_id)
        if card is None:
            raise HTTPException(404, f"no Card {card_id!r} in this run")
        return card_id, card

    def text(self, name: str, *, required: bool = True, limit: int = 20_000) -> Optional[str]:
        value = self.data.get(name)
        if value is None and not required:
            return None
        if not isinstance(value, str):
            raise HTTPException(400, f"{name} must be a string")
        value = value.strip()
        if required and not value:
            raise HTTPException(400, f"{name} must be non-empty")
        if len(value) > limit:
            raise HTTPException(400, f"{name} must be at most {limit} characters")
        return value

    def hypothesis_id(self, name: str = "id") -> str:
        value = self.data.get(name)
        if (not isinstance(value, str) or value != value.strip()
                or any(unicodedata.category(ch).startswith("C") for ch in value)):
            raise HTTPException(400, "hypothesis id must be canonical printable text")
        return self.text(name, limit=256)

    @staticmethod
    def comment_id(value: object) -> str:
        if not isinstance(value, str) or COMMENT_ID_RE.fullmatch(value) is None:
            raise HTTPException(400, {
                "code": "invalid_comment_id",
                "message": "comment_id must be cmt_ followed by exactly 32 lowercase hex characters",
                "remediation": "refresh the collaboration panel and retry the exact visible comment",
            })
        return value

    @staticmethod
    def comment_text(value: object) -> str:
        try:
            return normalize_comment_text(value)
        except ValueError as exc:
            raise HTTPException(400, {
                "code": "invalid_comment_text",
                "message": str(exc),
                "remediation": "enter non-empty text no larger than 8192 UTF-8 bytes",
            }) from exc

    def comment_version(self, value: object) -> int:
        version = self.strict_integer(value, "expected_version")
        if version < 1:
            raise HTTPException(400, "expected_version must be positive")
        return version


def _normalize_lifecycle_target(ctx: _ControlIntake) -> None:
    """Pin one control to an exact `(node id, lifecycle generation)` subject, in place.

    Shared by the seven `_LIFECYCLE_CONTROL_TARGETS` events rather than inlined seven times: the
    stale-generation rule is the same for all of them, and a per-event copy is how one of them would
    quietly keep accepting a node id whose attempt has moved on.
    """
    target_key = _LIFECYCLE_CONTROL_TARGETS[ctx.event_type]
    event_type = ctx.event_type
    data = ctx.data
    current = ctx.state()
    if event_type == EV_APPROVAL_GRANTED and not current.awaiting_approval:
        raise HTTPException(409, {
            "code": "approval_not_requested",
            "message": "the run is not awaiting result approval",
            "remediation": "refresh the run and approve only its active result request",
        })
    raw_nid = data.get(target_key)
    if event_type == EV_APPROVAL_GRANTED and raw_nid is None:
        # A bare/default approval means "the exact pending request", never "whatever node is
        # best now". The best can change, and reset can reuse a node id with another attempt.
        # Modern callers pass both fields explicitly; this fallback preserves convenience only
        # when the fold still has authoritative subject + lifecycle identity.
        nid = current.approval_subject
        pending_generation = current.approval_generation
        if (not current.awaiting_approval or isinstance(nid, bool)
                or not isinstance(nid, int) or nid < 0
                or isinstance(pending_generation, bool)
                or not isinstance(pending_generation, int) or pending_generation < 0):
            raise HTTPException(409, {
                "code": "approval_target_unavailable",
                "message": "the pending approval target cannot be verified",
                "remediation": "refresh the run and inspect Events before approving",
            })
        data["generation"] = pending_generation
    else:
        nid = ctx.strict_integer(raw_nid, target_key)
        if nid < 0:
            raise HTTPException(400, f"{target_key} must be non-negative")
    node = current.nodes.get(nid)
    if node is None:
        raise HTTPException(404, f"no node #{nid} in this run")
    if node.tombstoned:
        raise HTTPException(409, f"node #{nid} is tombstoned and cannot be controlled")
    if nid in current.aborted_nodes and event_type not in (EV_NODE_ABORT, EV_NODE_RESET):
        raise HTTPException(409, f"node #{nid} is aborted; reset it before {event_type}")
    raw_generation = data.get("generation", _ABSENT)
    if raw_generation is _ABSENT:
        if node.attempt != 0:
            raise HTTPException(
                409, f"stale {event_type}: generation is required "
                     f"(current generation is {node.attempt})")
        generation = 0
    else:
        generation = ctx.strict_integer(raw_generation, "generation")
        if generation < 0:
            raise HTTPException(400, "generation must be non-negative")
    if generation != node.attempt:
        raise HTTPException(
            409, f"stale {event_type}: node #{nid} is generation "
                 f"{node.attempt}, not {generation}")
    data[target_key] = nid
    data["generation"] = generation


# ------------------------------------------------------------------ run lifecycle normalizers

def _normalize_run_abort(ctx: _ControlIntake) -> dict:
    # `_normalize_finalize_data` REBINDS the payload, so write it back before using a helper that
    # reads `ctx.data`.
    ctx.data = _normalize_finalize_data(ctx.data)
    # The event fold is intentionally tolerant of historical hand-authored logs.  HTTP mutation is a
    # stronger trust boundary: reject payloads that would otherwise become permanent replay poison or
    # silently do nothing while the command lifecycle reports success.
    if ctx.data.get("reason") is not None:
        ctx.data["reason"] = ctx.text("reason", limit=256)
    snapshot = ctx.rd / "config.snapshot.json"
    if snapshot.is_file():
        from looplab.core.config import read_config_snapshot
        from looplab.harness.obligations import external_finish_due
        try:
            settings = read_config_snapshot(snapshot)
        except (OSError, ValueError) as exc:
            # A damaged snapshot hides whether the finish obligations apply, so the finish is
            # REFUSED with the coded answer — a durable rejected command naming the code, never a
            # 500 and never the parse error (critic 2026-09-29, driven). A live external engine reads the same file at its own terminal
            # gate (`orchestrator.py`, `external_finish_due`); `pause` reads none and still stops it.
            raise refusal("config_snapshot_unreadable") from exc
        if not settings.external_harness:
            return ctx.data
        from looplab.events.eventstore import EventStore
        events = EventStore(ctx.rd / "events.jsonl").read_all()
        # The report/review evidence and node lifecycle must come from the same
        # prefix; a live evaluation can append between two independent folds.
        from looplab.events.replay import fold
        due = external_finish_due(ctx.rd, settings, fold(events) if events else ctx.state(), events)
        if "source_error" in due:
            raise HTTPException(503, due["source_error"])
        if due["pending_nodes"]:
            raise HTTPException(409, {
                "code": "external_pending_evaluations",
                "node_ids": due["pending_nodes"],
                "message": "wait for pending candidates to settle or explicitly abort them before finalizing",
            })
        if due["report"]:
            raise HTTPException(409, {
                "code": "external_report_required",
                "message": "report_every is enabled; publish a report covering the latest candidate before finishing",
            })
        if due["reviews"]:
            raise HTTPException(409, {
                "code": "external_reviews_required", "phases": due["reviews"],
                "message": "review each enabled cross-run phase before finishing",
            })
    return ctx.data


def _normalize_restart(ctx: _ControlIntake) -> dict:
    if not task_file_for(ctx.rd):
        raise HTTPException(
            400, "restart requires task.snapshot.json or a usable legacy ui_meta.json task file")
    return ctx.data


def _normalize_node_abort(ctx: _ControlIntake) -> dict:
    _normalize_lifecycle_target(ctx)
    data = ctx.data
    data["node_id"] = ctx.node("node_id")
    if data.get("reason") is not None:
        data["reason"] = ctx.text("reason", required=False, limit=1000)
    return data


def _normalize_node_reset(ctx: _ControlIntake) -> dict:
    data = ctx.data
    raw_stage = data.get("from_stage", "eval")
    if not isinstance(raw_stage, str):
        raise HTTPException(400, "from_stage must be a string")
    stage = raw_stage.strip()
    if not stage or len(stage) > 64:
        raise HTTPException(400, "from_stage must be a non-empty stage name")
    data["from_stage"] = stage
    _normalize_lifecycle_target(ctx)
    return data


def _normalize_node_target(ctx: _ControlIntake) -> dict:
    """force_confirm / force_ablate: an exact lifecycle subject and nothing else."""
    _normalize_lifecycle_target(ctx)
    ctx.data["node_id"] = ctx.node("node_id")
    return ctx.data


def _normalize_approval_granted(ctx: _ControlIntake) -> dict:
    _normalize_lifecycle_target(ctx)
    return ctx.data


def _normalize_fork(ctx: _ControlIntake) -> dict:
    _normalize_lifecycle_target(ctx)
    ctx.data["from_node_id"] = ctx.node("from_node_id")
    return ctx.data


def _normalize_promote(ctx: _ControlIntake) -> dict:
    _normalize_lifecycle_target(ctx)
    data = ctx.data
    data["node_id"] = ctx.node("node_id")
    if data.get("alias") is not None:
        data["alias"] = ctx.text("alias", limit=128)
    return data


def _normalize_annotation(ctx: _ControlIntake) -> dict:
    data = ctx.data
    data["node_id"] = ctx.node("node_id")
    data["text"] = ctx.text("text")
    return data


def _normalize_spec_approved(ctx: _ControlIntake) -> dict:
    current = ctx.state()
    if (current.proposed_spec is None or not current.spec_approval_requested
            or current.spec_confirmed):
        raise HTTPException(409, {
            "code": "ratification_not_requested",
            "message": "the run is not awaiting eval-spec ratification",
            "remediation": "refresh the run and ratify only its active spec request",
        })
    return ctx.data


def _normalize_budget_extend(ctx: _ControlIntake) -> dict:
    data = ctx.data
    # ONE allow-list, the registry's own (doc 25 SC-02): this tuple used to be a verbatim second copy
    # of `CONTROL_DATA_FIELDS[EV_BUDGET_EXTEND]` a few hundred lines away, so adding a budget field to
    # one of them left "needs at least one budget field" rejecting the new field's solo payload.
    allowed = CONTROL_DATA_FIELDS[EV_BUDGET_EXTEND]
    if not any(data.get(name) is not None for name in allowed):
        raise HTTPException(400, "budget_extend needs at least one budget field")
    if data.get("add_nodes") is not None:
        value = ctx.integer("add_nodes")
        # Upper bound too: a huge extension (or a 400-digit int) is not a valid budget and would
        # let a single control command balloon the run. Ceiling mirrors Settings.max_nodes.
        if value <= 0 or value > 1_000_000:
            raise HTTPException(400, "add_nodes must be between 1 and 1000000")
        data["add_nodes"] = value
    for name, upper in (("eval_parallel", 1024), ("max_parallel", 1024),
                        ("llm_parallel", 64), ("parallel_build", 64)):
        if data.get(name) is None:
            continue
        value = ctx.integer(name)
        # 0 is a valid live request but settles to serial width 1. Only startup
        # Settings interpret 0 as hardware/eval-coupled AUTO.
        if value < 0 or value > upper:
            raise HTTPException(400, f"{name} must be between 0 and {upper}")
        data[name] = value
    for name in ("max_seconds", "max_eval_seconds", "timeout"):
        if data.get(name) is None:
            continue
        value = data[name]
        if isinstance(value, bool):
            raise HTTPException(400, f"{name} must be a finite positive number")
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(400, f"{name} must be a finite positive number")
        if not math.isfinite(value) or value <= 0:
            raise HTTPException(400, f"{name} must be a finite positive number")
        if name == "timeout" and value > LAUNCH_TIMEOUT_LIMIT_S:
            # `timeout` is a per-eval WALL CLOCK (the script path's), and since 2026-10-02 the run's
            # engine honours one up to the absolute launch limit by raising its ceiling
            # (`engine/width_settling.py::lift_launch_ceiling`). Above the limit it would be accepted
            # and then cut at the launch — so it is refused, by the same bound as `eval_timeout`.
            raise HTTPException(400, (
                f"timeout must be a finite number of seconds in (0, {LAUNCH_TIMEOUT_LIMIT_S:.0f}] — "
                "the absolute launch limit, 7 days"))
        data[name] = value
    if data.get("eval_timeout") is not None:
        # THE EVAL-SPEC BUDGET (2026-09-24): the per-evaluation wall clock of a repo/command task,
        # which `timeout` above does not reach (`engine/shared.py::effective_eval_time_budget`).
        # Bounded ABOVE by the absolute launch limit, `LAUNCH_TIMEOUT_LIMIT_S` (7 days), and by
        # nothing smaller. Until 2026-10-02 the bound was the RUN's `max_launch_timeout_s`, read from
        # its config snapshot, because the engine clamped every launch to that and a larger value would
        # have been accepted and then cut. The engine now LIFTS its ceiling to the operator's value
        # (`engine/width_settling.py::lift_launch_ceiling`; incident 2026-10-01: an operator's 28 h
        # budget SIGKILLed at a 24 h ceiling), so any value up to the week is one it will honour —
        # and this check needs neither the run's snapshot nor any process-wide ceiling. The server
        # installs none (`runtime/sandbox.py`); a check that read one would be reading the default.
        bounds = f"eval_timeout must be a finite number of seconds in (0, {LAUNCH_TIMEOUT_LIMIT_S:.0f}]"
        value = data["eval_timeout"]
        try:
            if isinstance(value, bool):
                raise TypeError("bool")
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(400, bounds)
        if not math.isfinite(value) or value <= 0:
            raise HTTPException(400, bounds)
        if value > LAUNCH_TIMEOUT_LIMIT_S:
            raise HTTPException(400, bounds + " — the absolute launch limit, 7 days: no launch "
                                              "may run longer, whatever the run's configuration")
        data["eval_timeout"] = value
    return data


def _normalize_hint(ctx: _ControlIntake) -> dict:
    data = ctx.data
    data["text"] = ctx.text("text")
    if data.get("replace") is not None and not isinstance(data["replace"], bool):
        raise HTTPException(400, "replace must be a boolean")
    return data


def _normalize_set_strategy(ctx: _ControlIntake) -> dict:
    data = ctx.data
    strategy = data.get("strategy")
    if not isinstance(strategy, dict) or not strategy:
        raise HTTPException(400, "strategy must be a non-empty JSON object")
    unknown_strategy = set(strategy) - {
        "policy", "policy_params", "fidelity", "eval_parallel", "llm_parallel",
        "llm_lane_limits", "card_scoring", "developer",
    }
    if unknown_strategy:
        raise HTTPException(
            400, f"strategy has unknown field(s): {', '.join(sorted(unknown_strategy))}")
    from looplab.search.policy import available_policies
    clean_strategy = {}
    policy = strategy.get("policy")
    if policy is not None:
        if not isinstance(policy, str) or policy not in available_policies():
            raise HTTPException(400, "strategy.policy must name an available policy")
        clean_strategy["policy"] = policy
    fidelity = strategy.get("fidelity")
    if fidelity is not None:
        if fidelity not in {"smoke", "full", "adaptive"}:
            raise HTTPException(400, "strategy.fidelity must be smoke, full, or adaptive")
        clean_strategy["fidelity"] = fidelity
    developer = strategy.get("developer")
    if developer is not None:
        # REFUSED here, not dropped, and that asymmetry is `core/appconfig.py`'s: the operator is
        # present at this boundary and can fix a typo, so a 400 naming the valid set is strictly
        # better than silently keeping the current backend. `validate_strategy` drops instead
        # because there the author is a MODEL, and a hallucinated name must not take a whole
        # decision — or the run — down.
        #
        # `developer_switch_names()` is the one home of this vocabulary (`core/config.py`), the same
        # set `engine/strategy.py::_available_developers` offers the Strategist, so the operator and
        # the model cannot come to be told different things are switchable.
        from looplab.core.config import developer_switch_names
        names = sorted(developer_switch_names())
        if not isinstance(developer, str) or developer not in names:
            raise HTTPException(
                400, f"strategy.developer must be one of: {', '.join(names)}")
        clean_strategy["developer"] = developer
    for name, upper in (("eval_parallel", 1024), ("llm_parallel", 64)):
        if strategy.get(name) is None:
            continue
        value = ctx.strict_integer(strategy[name], f"strategy.{name}")
        if not 0 <= value <= upper:
            raise HTTPException(400, f"strategy.{name} must be between 0 and {upper}")
        # Store the operator's raw live delta. Zero is NOT startup AUTO here; Engine apply
        # deterministically settles it to one without re-reading mutable hardware.
        clean_strategy[name] = value
    lane_limits = strategy.get("llm_lane_limits")
    if lane_limits is not None:
        from looplab.core.llm_broker import LLM_LANES
        if not isinstance(lane_limits, dict):
            raise HTTPException(400, "strategy.llm_lane_limits must be a JSON object")
        if any(not isinstance(lane, str) or lane not in LLM_LANES for lane in lane_limits):
            raise HTTPException(400, "strategy.llm_lane_limits has an unknown lane")
        clean_lanes = {}
        for lane, raw_width in lane_limits.items():
            width = ctx.strict_integer(raw_width, f"strategy.llm_lane_limits.{lane}")
            if not 0 <= width <= 64:
                raise HTTPException(
                    400, f"strategy.llm_lane_limits.{lane} must be between 0 and 64")
            clean_lanes[lane] = width
        clean_strategy["llm_lane_limits"] = clean_lanes
    card_scoring = strategy.get("card_scoring")
    if card_scoring is not None:
        from looplab.agents.strategist import validate_card_scoring
        clean_card_scoring = validate_card_scoring(card_scoring)
        if clean_card_scoring is None:
            raise HTTPException(
                400,
                "strategy.card_scoring must be the complete object "
                "{stance: explore|balanced|exploit, novelty_weight: 0..1, "
                "coverage_weight: 0..1}",
            )
        clean_strategy["card_scoring"] = clean_card_scoring
    params = strategy.get("policy_params")
    if params is not None:
        if not isinstance(params, dict) or not params:
            raise HTTPException(400, "strategy.policy_params must be a non-empty JSON object")
        if policy is None:
            raise HTTPException(400, "strategy.policy_params requires an explicit policy")
        allowed_params = ({"c"} if policy == "mcts" else
                          {"eta", "rung_nodes"} if policy in {"asha", "bohb"} else set())
        unknown_params = set(params) - allowed_params
        if unknown_params:
            raise HTTPException(400, f"strategy.policy_params not supported for {policy}: "
                                         f"{', '.join(sorted(unknown_params))}")
        clean_params = {}
        if "c" in params:
            value = params["c"]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(float(value)) or float(value) < 0):
                raise HTTPException(400, "strategy.policy_params.c must be finite and non-negative")
            clean_params["c"] = float(value)
        for name in ("eta", "rung_nodes"):
            if name not in params:
                continue
            value = ctx.strict_integer(params[name], f"strategy.policy_params.{name}")
            if (name == "eta" and value < 2) or (name == "rung_nodes" and value < 0):
                raise HTTPException(400, f"strategy.policy_params.{name} is out of range")
            clean_params[name] = value
        clean_strategy["policy_params"] = clean_params
    if not clean_strategy:
        raise HTTPException(
            400, "strategy must change policy, fidelity, Card scoring, "
                 "or a canonical concurrency allocation")
    data["strategy"] = clean_strategy
    return data


# ------------------------------------------------------------------ track_requested

def _declared_tracks(rd: Path) -> list[str]:
    """The `eval.tracks` names the run's task snapshot declares, [] when none can be read."""
    from looplab.adapters.task_schema import normalize_task
    try:
        data = json.loads((rd / "task.snapshot.json").read_text(encoding="utf-8"))
        spec = normalize_task(data).get("eval") if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return []
    tracks = spec.get("tracks") if isinstance(spec, dict) else None
    return sorted(tracks) if isinstance(tracks, dict) else []


def _normalize_track_requested(ctx: _ControlIntake) -> dict:
    """`track_requested` (doc 73 §1.4): run a DECLARED `eval.tracks.<track>` over settled nodes of
    a live run (`engine/track_lane.py`). Refused here for what the engine could only refuse later:
    a track the task does not declare, and node ids the run does not have. `node_ids` is `"all"` —
    every node evaluated when the request is served — or a list of up to 256 ids."""
    data = ctx.data
    track = ctx.text("track", limit=80)
    declared = _declared_tracks(ctx.rd)
    if track not in declared:
        raise HTTPException(409, {
            "code": "track_not_declared",
            "message": f"the task declares no eval.tracks.{track} (declared: "
                       f"{', '.join(declared) or 'none'})",
        })
    raw = data.get("node_ids", "all")
    if raw == "all":
        node_ids = "all"
    else:
        if not isinstance(raw, list) or not raw or len(raw) > 256:
            raise HTTPException(400, "node_ids must be \"all\" or a non-empty list of up to 256 node ids")
        node_ids = sorted({ctx.strict_integer(x, "node_ids entries") for x in raw})
        missing = [n for n in node_ids if n not in ctx.state().nodes]
        if missing:
            raise HTTPException(404, f"no node(s) {missing[:8]} in this run")
    return {"track": track, "node_ids": node_ids}


# ------------------------------------------------------------------ metric_retarget

def _declares_withheld_scorer(rd: Path) -> bool:
    """Whether the run's task snapshot declares a WITHHELD scorer (`eval.holdout_scorer`). Best
    effort: an unreadable snapshot declares nothing here, and the engine still runs no holdout under a
    retargeted objective (`engine/holdout.py::HoldoutGrader.holdout_pending`)."""
    from looplab.adapters.task_schema import normalize_task
    try:
        data = json.loads((rd / "task.snapshot.json").read_text(encoding="utf-8"))
        spec = normalize_task(data).get("eval") if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):    # absent, undecodable, or no task the schema reads
        return False
    scorer = spec.get("holdout_scorer") if isinstance(spec, dict) else None
    return isinstance(scorer, dict) and bool(scorer.get("command"))


def _normalize_metric_retarget(ctx: _ControlIntake) -> dict:
    """`metric_retarget` (doc 68 68.2): make a DECLARED extra metric the objective every node is
    ranked by, or `key: null` to rank by the task's own metric again. Refused here for the cases the
    fold ignores (`events/replay.py::_on_metric_retarget`), so an accepted command is one it applies:

    - a `direction` other than the run's: a flip reverses the standing of every past decision and
      the dispatcher's own orientation (`task.direction`) — a new run, not a retarget;
    - a run with a holdout (host-graded, one already scored, or a withheld scorer declared): its
      unseen number is the TASK's metric, which a retargeted objective would be ranked against;
    - the objective already in force, and a key no evaluated node recorded on the operator's
      DECLARED channel (`core/models.py::objective_value`) — ranking on it would unrank every node.
    """
    from looplab.core.models import EXTRA_METRIC_DECLARED, objective_value
    data = ctx.data
    if "key" not in data:
        raise HTTPException(400, "metric_retarget needs `key`: a declared extra metric, or null")
    key = None if data.get("key") is None else ctx.text("key", limit=256)
    state = ctx.state()
    direction = data.get("direction")
    if direction is not None and not isinstance(direction, str):
        raise HTTPException(400, "metric_retarget `direction` must be 'min' or 'max'")
    # Read in the fold's spelling, and stored in it (`clean` below): "MAX" on a maximized run is the
    # same direction, never a flip (critic 2026-09-27) — and stored as typed, the fold would IGNORE
    # the row (`events/replay.py::_on_metric_retarget` compares exactly) under a `succeeded` command.
    direction = direction.strip().lower() if direction is not None else None
    if direction is not None and direction not in ("min", "max"):
        raise HTTPException(400, "metric_retarget `direction` must be 'min' or 'max'")
    if direction is not None and direction != state.direction:
        raise HTTPException(409, {
            "code": "retarget_direction_flip",
            "message": f"this run {'maximizes' if state.direction == 'max' else 'minimizes'} its "
                       "objective and a retarget keeps the direction: a flip is a new run",
        })
    if state.host_grading or state.holdout_evaluated_ids or _declares_withheld_scorer(ctx.rd):
        raise HTTPException(409, {
            "code": "retarget_with_holdout",
            "message": "this run has a holdout, and its unseen number is the task's own metric — "
                       "a retargeted objective would be ranked against another scale",
        })
    if key == state.objective_key:
        raise HTTPException(409, {
            "code": "retarget_unchanged",
            "message": "the run already ranks by "
                       + (repr(key) if key is not None else "the task's own metric"),
        })
    against = sorted({n.extra_metrics_direction.get(key) for n in state.evaluated_nodes()}
                     - {None, state.direction})
    if key is not None and against:
        raise HTTPException(409, {
            "code": "retarget_direction_flip",
            "message": f"the operator's declaration orients {key!r} as {against[0]!r} and this run "
                       f"is {state.direction!r}: a retarget keeps the direction — a flip is a new run",
        })
    if key is not None and not any(
            objective_value(n.extra_metrics, n.extra_metrics_provenance, key,
                            n.extra_metrics_direction, state.direction) is not None
            for n in state.evaluated_nodes()):
        declared = sorted({name for n in state.evaluated_nodes()
                           for name, channel in (n.extra_metrics_provenance or {}).items()
                           if channel == EXTRA_METRIC_DECLARED})
        # The keys that COULD be the objective ride in the message: the durable command record
        # keeps code, message and remediation, nothing else.
        named = ", ".join(repr(k) for k in declared[:16]) + (" …" if len(declared) > 16 else "")
        raise HTTPException(422, {
            "code": "retarget_key_not_declared",
            "message": f"no evaluated node recorded {key!r} on the operator's declared channel "
                       "(`eval.metrics`), so every node would be unranked; declared: "
                       + (named or "none"),
        })
    clean: dict = {"key": key}
    if direction is not None:
        clean["direction"] = direction
    goal = ctx.text("goal", required=False, limit=4000)
    if goal:
        clean["goal"] = goal
    return clean


# ------------------------------------------------------------------ inject_node

def _normalize_artifact_fields(ctx: _ControlIntake) -> None:
    """`node_kind` and `uses` on an inject (doc 73 §1.4). `node_kind` is `"artifact"` or absent.
    `uses` names artifact nodes that are ALREADY PRODUCED — evaluated in their current lifecycle —
    so the consumer never waits on, or races, a producer: an operator or the Assistant injects the
    consumers once the preparation is done. Refused, never coerced, so a typo cannot silently become
    an experiment that reads nothing."""
    data = ctx.data
    if "node_kind" in data:
        if data["node_kind"] != "artifact":
            raise HTTPException(400, "node_kind must be \"artifact\" (or absent for an experiment)")
    if "uses" not in data:
        return
    uses = data["uses"]
    if (not isinstance(uses, list) or not uses or len(uses) > 32
            or any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in uses)):
        raise HTTPException(400, "uses must be a non-empty list of at most 32 node ids")
    if len(set(uses)) != len(uses):
        raise HTTPException(400, "uses names a node twice")
    state = ctx.state()
    for nid in uses:
        node = state.nodes.get(nid)
        if node is None or getattr(node, "kind", None) != "artifact":
            raise HTTPException(409, {"code": "inject_uses_not_artifact",
                                      "message": f"uses: #{nid} is not an artifact node"})
        if node.status.value != "evaluated" or node.tombstoned:
            raise HTTPException(409, {"code": "inject_uses_not_produced",
                                      "message": f"uses: artifact #{nid} has not been produced yet; "
                                                 "inject its consumers once it is"})


def _import_cross_run_source(ctx: _ControlIntake) -> None:
    """Replace a `{source_run, source_node}` reference with the source node's durable snapshot."""
    data = ctx.data
    srv = ctx.srv
    sr = str(data.pop("source_run"))
    if not sr or len(sr) > 255:
        raise HTTPException(400, "source_run must be a non-empty run id")
    raw_source_node = data.pop("source_node")
    sn = ctx.strict_integer(raw_source_node, "source_node")
    source_rd = srv.run_dir(sr)
    command_service = getattr(srv, "commands", None)
    if command_service is not None and callable(getattr(command_service, "validate_paths", None)):
        source_rd = command_service.validate_paths(source_rd)
    sst = srv.state(source_rd)
    snode = sst.nodes.get(sn)
    if snode is None:
        raise HTTPException(404, f"no experiment #{sn} in run {sr}")
    if snode.tombstoned:
        raise HTTPException(409, f"source experiment #{sn} in run {sr} is tombstoned")
    if sn in sst.aborted_nodes:
        raise HTTPException(409, f"source experiment #{sn} in run {sr} is aborted")
    # The snapshot itself is `events/node_import.py::node_import_payload` — ONE spelling shared
    # with the new-run launch form (`Settings.seed_from_run`, doc 67 67.2): the concept rule, the
    # rationale note and the attempt-stamped `origin` receipt all moved there verbatim.
    data.update(node_import_payload(sst, sn, sr))


def _relative_file_name(value, field: str) -> str:
    # The rule is `events/node_import.py::portable_relative_name`, shared with the new-run seed
    # (`Settings.seed_from_run`); the refusal wording stays this route's.
    portable, defect = portable_relative_name(value)
    if defect == NAME_NOT_TEXT:
        raise HTTPException(400, f"{field} entries must be non-empty relative path strings")
    if portable is None:
        raise HTTPException(400, f"{field} entries must stay within the node workspace")
    return portable


# The fields a CLIENT may author on `forked_from`.  The receipt's other FOUR keys are STAMPED by the
# server from state it holds, and a payload that supplies any of them is REFUSED rather than
# overwritten: "what the operator changed" is the whole point of the receipt, so it must be a fact the
# server derived, not a claim the browser made about itself.  (Same rule, same reason, as the Card
# controls' server-stamped provenance a few tables down.)
_FORK_RECEIPT_CLIENT_FIELDS = frozenset({"node_id", "generation", "observed_seq"})
_FORK_RECEIPT_SERVER_FIELDS = frozenset({
    "changed_fields", "base_idea_digest", "authored_fields", "not_carried_fields"})


def _normalize_fork_receipt(ctx: _ControlIntake, parents: list, idea: Idea) -> dict:
    """Validate + stamp the "this node was forked from #P, and here is what I changed" receipt.

    WHY THIS IS NOT A TAIL CAS (the question `routers/control.py`'s approval baseline raises).  An
    approval decision means "accept the gate that is open RIGHT NOW", so it has to be bound to the
    exact tail the normalizer folded — a replacement request would otherwise be granted by a click
    aimed at its predecessor.  A fork-from-a-snapshot is the opposite shape: the operator's whole
    intent travels in the payload (their edited idea verbatim, the parent named by id, that parent's
    lifecycle generation), so it means the same thing at seq N and at the live tail *provided its
    named parent is still the one they saw*.  That is a CONTENT compare-and-swap and
    `_normalize_inject_node`'s `parent_generations` block already performs it — refusing a
    tombstoned/aborted parent (409) and a parent whose `attempt` has moved (409 `stale parent #P`).
    A tail CAS here would be strictly worse than useless: an unrelated append (a metric row, an
    LLM-accounting row — a live run appends several per second) would refuse a fork whose meaning
    nothing had touched, so the operator would be told to retry until they happened to win a race.

    What the seq IS for: `observed_seq` records the VANTAGE POINT the operator branched from, so the
    log says which projection of the run they were reading.  It is bounded by the current tail so it
    can never name a state that does not exist, and it fences nothing.

    The two stamped fields make the lineage checkable rather than asserted.  `base_idea_digest` is
    minted here from the live parent's own Idea, and `changed_fields` is this server's comparison of
    the submitted Idea against it — a fact about two objects the server holds, which is why neither
    may arrive from the client.  Note that a node's idea CAN drift inside one `attempt`
    (`replay.py::_on_node_repaired` rewrites `idea.footprint` on a pending node without bumping the
    generation), so the digest is not a redundant re-spelling of the generation CAS.
    """
    receipt = ctx.data.get("forked_from")
    if not isinstance(receipt, dict):
        raise HTTPException(400, "forked_from must be a JSON object")
    forged = _FORK_RECEIPT_SERVER_FIELDS & set(receipt)
    if forged:
        raise HTTPException(400, {
            "code": "fork_receipt_forged",
            "message": (f"forked_from.{'/'.join(sorted(forged))} is derived by the server and "
                        f"must not be supplied"),
            "remediation": "submit only node_id, generation and observed_seq",
        })
    unknown = set(receipt) - _FORK_RECEIPT_CLIENT_FIELDS
    if unknown:
        raise HTTPException(
            400, f"forked_from has unknown field(s): {', '.join(sorted(unknown))}")
    missing = sorted(_FORK_RECEIPT_CLIENT_FIELDS - set(receipt))
    if missing:
        raise HTTPException(400, f"forked_from is missing: {', '.join(missing)}")
    source_id = ctx.strict_integer(receipt["node_id"], "forked_from.node_id")
    source = ctx.state().nodes.get(source_id)
    if source is None:
        raise HTTPException(404, f"no node #{source_id} in this run")
    # The node a fork DERIVES from is the node it hangs under. Allowing a receipt naming some other
    # node would let the record claim a lineage the DAG does not have, which is exactly the thing
    # doc 36 asks this receipt to make exact.
    if source_id not in parents:
        raise HTTPException(400, {
            "code": "fork_parent_mismatch",
            "message": f"forked_from names #{source_id}, which is not a parent of this experiment",
            "remediation": "branch from a node you also pass as the parent",
        })
    generation = ctx.strict_integer(receipt["generation"], "forked_from.generation")
    if generation != source.attempt:
        # Reachable independently of the parent_generations CAS above only through a hand-built
        # payload; kept explicit so the refusal names the receipt the caller actually got wrong.
        raise HTTPException(
            409, f"stale parent #{source_id}: current generation is {source.attempt}")
    observed_seq = ctx.strict_integer(receipt["observed_seq"], "forked_from.observed_seq")
    tail = ctx.tail_seq()
    if observed_seq < 0 or observed_seq > tail:
        raise HTTPException(400, {
            "code": "fork_observed_seq_out_of_range",
            "message": f"forked_from.observed_seq {observed_seq} is not a seq this run has reached",
            "remediation": f"branch from a snapshot between seq 0 and seq {tail}",
        })
    base = source.idea
    changed = sorted(field for field in Idea.model_fields
                     if getattr(idea, field, None) != getattr(base, field, None))
    # `changed_fields` alone cannot answer the question the receipt exists for, and reading it as if
    # it could is how a branch comes to read as the operator's work when most of it is not.  It is a
    # raw diff of two Ideas, and a branch differs from its parent for TWO unrelated reasons: the
    # operator edited something, and the gesture deliberately does not carry the parent's engine
    # bookkeeping across (`card_id`, `hypothesis`, `footprint`, `theme`, the concept envelope — see
    # `ui/src/forkFromSeqModel.js::FORK_IDEA_FIELDS` for why each is left behind).  Measured on the
    # toy run in `tests/test_fork_from_seq.py`, an operator who edits exactly two things already gets
    # `["card_id", "params", "rationale"]`; against a Researcher-built parent carrying a hypothesis, a
    # theme, a finalized footprint and a concept envelope it is eight fields for the same two edits.
    # A reader shown that list as "what the operator changed" is being told a falsehood, and one shown
    # its complement as "what the parent contributed" is being told a different one.
    #
    # So the server SPLITS it, because only the server holds both ideas at once.  The browser cannot
    # derive this later: the node's idea drifts after intake (`_finalize_developer_footprint` mints a
    # `footprint` the submission had none of), and the parent may since have been reset out of the
    # folded state.  Both halves are stamped, never accepted, for the same reason the other two are.
    #
    # The two lists deliberately do NOT partition `changed_fields`.  `not_carried_fields` claims the
    # PARENT had something here that did not come across, so a field where neither side carries
    # anything and the values still differ (`concept_mode: None` vs `""`) is in neither list — an
    # honest residue beats inflating either claim to make the arithmetic tidy.  `changed_fields`
    # stays the authority for "these two ideas differ here" and is unchanged, so every receipt
    # already on disk keeps its exact meaning.
    authored = [field for field in changed
                if idea_field_carried(getattr(idea, field, None))]
    not_carried = [field for field in changed
                   if not idea_field_carried(getattr(idea, field, None))
                   and idea_field_carried(getattr(base, field, None))]
    return {
        "node_id": source_id,
        "generation": generation,
        "observed_seq": observed_seq,
        # None when the parent's Idea exceeds the versioned identity's bounds — an honest "no digest
        # could be minted" beats a fallback rendering two different ideas could both claim.
        "base_idea_digest": idea_proposal_digest(base),
        "changed_fields": changed,
        # The operator's own substance: a difference this branch puts a VALUE behind.
        "authored_fields": authored,
        # The parent's substance that the branch left behind: a difference the branch is empty at.
        "not_carried_fields": not_carried,
    }


def _normalize_inject_node(ctx: _ControlIntake) -> dict:
    data = ctx.data
    # BEFORE the import, because the import MINTS this key and after it runs a server-derived
    # `origin` is indistinguishable from a submitted one.
    if "origin" in data:
        raise HTTPException(400, {
            "code": "origin_forged",
            "message": ("origin is derived by the server from the source experiment and must not "
                        "be supplied"),
            "remediation": ("seed from another run with source_run/source_node and let the server "
                            "record where it came from"),
        })
    if data.get("source_run") and data.get("source_node") is not None:
        _import_cross_run_source(ctx)
    # The RESIDUAL allow-list: the event's own fields MINUS the two the import consumes.  It is not
    # `CONTROL_DATA_FIELDS[EV_INJECT_NODE]` itself, and that is load-bearing rather than a leftover
    # (doc 25 SC-02 read the difference as drift and proposed collapsing the two).  The import above
    # runs only when `source_run` is TRUTHY and `source_node` is present, so `{"source_run": "",
    # "source_node": 0, ...}` arrives here with both fields intact; passing the full allow-list
    # accepts it and writes both straight into the durable event.  Measured on the payload corpus:
    # the collapse turns 61 refusals into acceptances.
    allowed_inject = CONTROL_DATA_FIELDS[EV_INJECT_NODE] - _INJECT_IMPORT_FIELDS
    unknown_inject = set(data) - allowed_inject
    if unknown_inject:
        raise HTTPException(
            400, f"inject_node has unknown field(s): {', '.join(sorted(unknown_inject))}")
    if data.get("parent_id") is not None and data.get("parent_ids") is not None:
        raise HTTPException(400, "inject_node accepts parent_id or parent_ids, not both")
    _normalize_artifact_fields(ctx)
    idea = data.get("idea")
    if not isinstance(idea, dict) or not idea:
        raise HTTPException(400, "idea must be a non-empty JSON object")
    unknown_idea = set(idea) - set(Idea.model_fields)
    if unknown_idea:
        raise HTTPException(400, f"idea has unknown field(s): {', '.join(sorted(unknown_idea))}")
    operator = idea.get("operator")
    if not isinstance(operator, str) or not operator.strip():
        raise HTTPException(400, "idea.operator must be a non-empty string")
    try:
        concept_fields = {"concepts", "concepts_added", "concepts_removed"} & set(idea)
        if "concept_mode" in idea or concept_fields:
            # replay's tolerant Idea reader is not a mutation boundary. Upgrade legacy
            # manual concept envelopes to an explicit modern mode, then validate BEFORE any field can
            # be bounded/dropped and laundered into an apparently exact durable event.
            emission = dict(idea)
            if "concept_mode" not in emission:
                emission["concept_mode"] = "full" if "concepts" in concept_fields else "delta"
            normalized_idea = IdeaEmission.model_validate(emission).to_idea()
        else:
            normalized_idea = Idea.model_validate(idea)
    except ValidationError as exc:
        issues = [f"{'.'.join(map(str, row.get('loc') or ('idea',)))}: {row.get('msg')}"
                  for row in exc.errors(include_url=False)[:5]]
        raise HTTPException(400, f"idea is invalid: {'; '.join(issues)}") from exc
    data["idea"] = durable_idea_payload(normalized_idea)
    if data.get("parent_id") is not None:
        data["parent_id"] = ctx.node("parent_id")
    if data.get("parent_ids") is not None:
        parents = data["parent_ids"]
        if not isinstance(parents, list) or not parents or len(parents) > 64:
            raise HTTPException(400, "parent_ids must be a non-empty list of at most 64 node ids")
        normalized_parents = []
        for value in parents:
            value = ctx.strict_integer(value, "parent_ids entries")
            if value not in ctx.state().nodes:
                raise HTTPException(404, f"no node #{value} in this run")
            normalized_parents.append(value)
        data["parent_ids"] = normalized_parents
    parents = ([data["parent_id"]] if data.get("parent_id") is not None
               else list(data.get("parent_ids") or []))
    if len(set(parents)) != len(parents):
        raise HTTPException(400, "parent ids must be unique")
    raw_snapshot = data.get("parent_generations", _ABSENT)
    if raw_snapshot is not _ABSENT and not isinstance(raw_snapshot, dict):
        raise HTTPException(400, "parent_generations must be an object")
    if raw_snapshot is _ABSENT:
        if any(ctx.state().nodes[pid].attempt != 0 for pid in parents):
            raise HTTPException(409, "parent generation is required after node reset")
        raw_snapshot = {str(pid): 0 for pid in parents}
    if len(raw_snapshot) != len(parents):
        raise HTTPException(400, "parent generation snapshot does not match parents")
    normalized_snapshot: dict[str, int] = {}
    for pid in parents:
        parent = ctx.state().nodes[pid]
        if parent.tombstoned:
            raise HTTPException(409, f"parent #{pid} is tombstoned")
        if pid in ctx.state().aborted_nodes:
            raise HTTPException(409, f"parent #{pid} is aborted")
        raw_generation = raw_snapshot.get(str(pid), raw_snapshot.get(pid, _ABSENT))
        if raw_generation is _ABSENT:
            raise HTTPException(400, f"missing generation for parent #{pid}")
        generation = ctx.strict_integer(raw_generation, "parent generation")
        if generation < 0:
            raise HTTPException(400, "parent generation must be non-negative")
        if generation != parent.attempt:
            raise HTTPException(
                409, f"stale parent #{pid}: current generation is {parent.attempt}")
        normalized_snapshot[str(pid)] = generation
    data["parent_generations"] = normalized_snapshot
    if data.get("code") is not None and not isinstance(data["code"], str):
        raise HTTPException(400, "code must be a string or null")
    files = data.get("files")
    if files is None:
        files = {}
    if not isinstance(files, dict):
        raise HTTPException(400, "files must be an object mapping relative paths to strings")

    normalized_files = {}
    for name, content in files.items():
        name = _relative_file_name(name, "files")
        if not isinstance(content, str):
            raise HTTPException(400, "files values must be strings")
        normalized_files[name] = content
    data["files"] = normalized_files
    deleted = data.get("deleted")
    if deleted is None:
        deleted = []
    if not isinstance(deleted, list):
        raise HTTPException(400, "deleted must be a list of relative path strings")
    data["deleted"] = [_relative_file_name(name, "deleted") for name in deleted]
    from looplab.core.config import read_config_snapshot
    snapshot = ctx.rd / "config.snapshot.json"
    run_settings = None
    if snapshot.is_file():
        try:
            run_settings = read_config_snapshot(snapshot)
        except (OSError, ValueError) as exc:
            # The run mode is unknowable, and with it every external rule below: the one coded
            # refusal every serve reader gives an unreadable snapshot, never a 500 — the external
            # agent's main command landed on this unguarded read once the intent rule stopped
            # pre-empting it (critic 2026-09-29, driven).
            raise refusal("config_snapshot_unreadable") from exc
    if run_settings is not None and run_settings.external_harness:
        from looplab.adapters.tasks import load_task
        from looplab.harness.contract import candidate_surface_refusal
        task_snapshot = ctx.rd / "task.snapshot.json"
        if not task_snapshot.is_file():
            raise HTTPException(409, "external candidate requires the run's task snapshot")
        try:
            task = load_task(task_snapshot, existing_run=True)
            repo_spec = task.repo_spec() if callable(getattr(task, "repo_spec", None)) else None
        except (OSError, ValueError) as exc:
            raise HTTPException(409, "cannot verify the external candidate's task surface") from exc
        # Not `refusal`: that name is this module's coded-refusal helper, which the snapshot read
        # above raises through — a local of the same name made that raise an UnboundLocalError.
        surface_refused = candidate_surface_refusal(repo_spec, normalized_files, data["deleted"])
        if surface_refused:
            raise HTTPException(400, surface_refused)
        # A configured concept workflow is an obligation in external mode, not
        # an advisory prompt. Check effective membership after surface checks.
        from looplab.harness.obligations import candidate_concepts, concept_tags_required
        settings = run_settings
        from looplab.harness.obligations import research_due
        from looplab.events.eventstore import EventStore
        events = EventStore(ctx.rd / "events.jsonl").read_all()
        if research_due(settings, ctx.state(), events):
            raise HTTPException(409, {
                "code": "external_research_required",
                "message": "deep_research_every is enabled; publish research_completed at this node count before submitting a candidate",
            })
        from looplab.harness.obligations import report_cadence_due
        if report_cadence_due(settings, ctx.state(), events):
            raise HTTPException(409, {
                "code": "external_report_cadence_required",
                "message": "report_every is enabled; publish a current report before another candidate",
                "remediation": "command:report_generated",
            })
        from looplab.harness.obligations import run_base_due
        if run_base_due(settings, ctx.state()):
            raise HTTPException(409, {
                "code": "external_run_base_required",
                "message": "concept_run_base is enabled; seed run_concepts from the first scored node's authored tags before the next candidate",
            })
        from looplab.harness.hypotheses import merge_due
        if merge_due(settings, ctx.state(), events):
            raise HTTPException(409, {
                "code": "external_hypothesis_merge_review_required",
                "message": "review the open pure-belief board and merge duplicates or record no_merge before another candidate",
                "remediation": "GET then POST /api/runs/{run_id}/harness-hypotheses",
            })
        from looplab.harness.reviews import cadence_reviews_due
        from looplab.events.run_generation import run_generation_token
        memory_reviews = cadence_reviews_due(ctx.rd, settings, ctx.state(),
                                             run_generation_token(events))
        if memory_reviews:
            raise HTTPException(409, {
                "code": "external_memory_cadence_review_required",
                "phases": memory_reviews,
                "message": "lessons_every is enabled; record each due lesson/skill review before another candidate",
                "remediation": "POST /api/runs/{run_id}/harness-reviews",
            })
        from looplab.harness.selection import verification_due, value_due
        if verification_due(settings, ctx.state()):
            raise HTTPException(409, {
                "code": "external_selection_verifier_required",
                "message": "select_verifier is enabled; score the current tied group before proposing another candidate",
                "remediation": "GET /api/runs/{run_id}/harness-selection then POST /api/runs/{run_id}/harness-selection/verify",
            })
        if value_due(settings, ctx.state()):
            raise HTTPException(409, {
                "code": "external_mcts_value_required",
                "message": "MCTS value_weight is enabled; estimate the current branch batch before proposing another candidate",
                "remediation": "GET /api/runs/{run_id}/harness-selection then POST /api/runs/{run_id}/harness-selection/values",
            })
        if normalized_idea.card_id:
            raise HTTPException(400, {
                "code": "external_card_link_unsupported",
                "message": "injected candidates mint a new Card; remove idea.card_id and supply idea.hypothesis when required",
            })
        if settings.track_hypotheses and not (normalized_idea.hypothesis or "").strip():
            raise HTTPException(400, {
                "code": "external_hypothesis_required",
                "message": "track_hypotheses is enabled; give this candidate a hypothesis statement (injected candidates mint their own Card)",
            })
        if concept_tags_required(settings) and not candidate_concepts(
                normalized_idea, ctx.state(), parents):
            raise HTTPException(400, {
                "code": "external_concepts_required",
                "message": "enabled concept settings require nonempty effective concepts on each candidate",
                "settings": [key for key in
                             ("concept_pivot", "concept_run_base", "cross_run_concepts")
                             if getattr(settings, key)],
                "remediation": "author idea.concepts (full) or a nonempty effective delta",
            })
        from looplab.events.run_generation import run_generation_token
        from looplab.harness.decisions import missing_decisions
        generation = run_generation_token(events)
        if generation and ctx.state().run_uid:
            missing = missing_decisions(ctx.rd, settings, ctx.state(), normalized_idea, generation,
                                        code=data.get("code"), files=normalized_files,
                                        deleted=data["deleted"])
            if missing:
                raise HTTPException(409, {
                    "code": "external_decisions_required", "phases": missing,
                    "message": "enabled decision phases require idea-bound review receipts before admission",
                    "remediation": "POST /api/runs/{run_id}/harness-decisions for this idea",
                })
    # `origin` IS SERVER-DERIVED PROVENANCE, exactly like the fork receipt's stamped half, and it was
    # the one the client could write. `_import_cross_run_source` mints it from the source node it
    # just read — run id, node id, that node's `robust_metric`, its lifecycle generation — and the
    # fold then keeps it verbatim, the DAG renders it as a verified cross-run seed CARRYING A METRIC,
    # and `routers/reviews.py::_SUMMARY_OMIT_KEYS` scrubs it from every review capability precisely
    # because it discloses the portfolio. A caller that supplies its own is claiming a measured
    # result in another run that need not exist.
    #
    # Same refusal as `forked_from`'s `fork_receipt_forged`, and for the same reason: what the
    # server derives, the client may not assert. `_import_cross_run_source` runs earlier in this
    # same normalizer and sets `data["origin"]` itself, so the legitimate path is unaffected — it is
    # the SUBMITTED payload that may not carry one, which is why the check reads the raw body rather
    # than `data`.
    origin = data.get("origin")
    if origin is not None and not isinstance(origin, dict):
        raise HTTPException(400, "origin must be a JSON object or null")
    # `forked_from` is deliberately its OWN key and not a corner of `origin`: `origin` means
    # CROSS-RUN seeding ({"run_id","node_id","metric"}) and `routers/reviews.py::_SUMMARY_OMIT_KEYS`
    # drops it from every review capability for exactly that reason — "a review link is a capability
    # over ONE run, so it must not disclose the PORTFOLIO". An in-run fork receipt is the opposite:
    # it is within-run provenance a one-run bearer was already granted, the same distinction that
    # keeps `research_origin` out of that omit set.
    if "forked_from" in data:
        if data["forked_from"] is None:
            # An explicit null is ABSENT, not "a receipt whose value is null". Popping keeps the
            # durable event byte-identical to an ordinary inject rather than writing a key the fold
            # would read back as `Node.forked_from = None` anyway — a durable row that looks like a
            # branch and carries no lineage is the one shape this receipt must never have.
            data.pop("forked_from")
        else:
            data["forked_from"] = _normalize_fork_receipt(ctx, parents, normalized_idea)
    return data


# ------------------------------------------------------------------ hypothesis board

def _normalize_hypothesis_added(ctx: _ControlIntake) -> dict:
    data = ctx.data
    data["statement"] = ctx.text("statement", limit=CARD_STATEMENT_MAX_CHARS)
    if data.get("id") is not None:
        data["id"] = ctx.hypothesis_id()
    if data.get("source") is not None:
        data["source"] = ctx.text("source", limit=128)
    return data


def _normalize_hypothesis_updated(ctx: _ControlIntake) -> dict:
    data = ctx.data
    data["id"] = ctx.hypothesis_id()
    status = ctx.text("status", limit=64).lower()
    if status not in {"open", "abandoned", "deleted"}:
        raise HTTPException(400, "hypothesis status must be open, abandoned, or deleted")
    data["status"] = status
    return data


def _normalize_research_completed(ctx: _ControlIntake) -> dict:
    """Author a memo through the same sanitized projection as the built-in researcher.

    IDs, verification, trigger and node count are server-derived. A memo cannot self-certify
    its claims or pretend to be a paid internal research attempt.
    """
    from looplab.core.advisory_payloads import (
        sanitize_research_memo_payload, stamp_research_memo)
    from looplab.core.models import ResearchMemo
    from looplab.trust.memo_verify import verify_memo

    raw = ctx.data.get("memo")
    if not isinstance(raw, dict):
        raise HTTPException(400, "research_completed.memo must be an object")
    allowed = set(ResearchMemo.model_fields) - {"at_node", "trigger", "claims_receipt"}
    unknown = set(raw) - allowed
    if unknown:
        raise HTTPException(400, f"research_completed.memo has unknown field(s): {', '.join(sorted(unknown))}")
    if not any(raw.get(key) for key in ("summary", "findings", "claims", "open_questions",
                                        "next_experiments", "recommended_directions")):
        raise HTTPException(400, "research_completed.memo needs a conclusion or direction")
    state = ctx.state()
    at_node = len(state.nodes)
    memo = sanitize_research_memo_payload({**raw, "at_node": at_node, "trigger": "external"})
    if memo.get("claims"):
        verdict = verify_memo(memo, state, client=None)
        if verdict is not None:
            memo["verification"] = verdict
    memo, memo_id = stamp_research_memo(memo)
    return {"memo": memo, "at_node": at_node, "trigger": "external", "served_manual": False,
            **({"memo_id": memo_id} if memo_id else {})}


def _normalize_report_generated(ctx: _ControlIntake) -> dict:
    from looplab.core.advisory_payloads import sanitize_report_payload

    raw = ctx.data.get("content")
    if not isinstance(raw, dict):
        raise HTTPException(400, "report_generated.content must be an object")
    allowed = set(sanitize_report_payload({})) - {"at_node", "trigger"}
    unknown = set(raw) - allowed
    if unknown:
        raise HTTPException(400, f"report_generated.content has unknown field(s): {', '.join(sorted(unknown))}")
    if not any(raw.get(key) for key in ("headline", "summary", "verdict", "champion_summary")):
        raise HTTPException(400, "report_generated.content needs a narrative")
    at_node = len(ctx.state().nodes)
    return {"content": sanitize_report_payload({**raw, "at_node": at_node,
                                                "trigger": "external"}),
            "at_node": at_node, "trigger": "external"}


# ------------------------------------------------------------------ collaboration normalizers

def _normalize_comment_created(ctx: _ControlIntake) -> dict:
    data = ctx.data
    srv = ctx.srv
    current = ctx.state()
    node_id = ctx.node("node_id")
    node_generation = ctx.strict_integer(data.get("node_generation"), "node_generation")
    if node_generation < 0:
        raise HTTPException(400, "node_generation must be non-negative")
    node = current.nodes[node_id]
    if node.attempt != node_generation:
        raise HTTPException(409, {
            "code": "node_generation_changed",
            "message": (f"experiment #{node_id} is generation {node.attempt}, not "
                        f"{node_generation}"),
            "remediation": "refresh the run before commenting on this experiment lifecycle",
        })
    # Count only MODERN comments: legacy EV_ANNOTATION notes cannot be compacted in an append-only
    # log, so counting them here would permanently 409 modern comments on a heavily-annotated run.
    # Mirrors comment_projection.apply_comment_event so validation and fold never diverge.
    modern_count = sum(1 for item in current.comments.values() if not item.legacy)
    if modern_count >= COMMENT_MAX_PER_RUN:
        raise HTTPException(409, {
            "code": "comment_run_limit_reached",
            "message": f"this run already has {COMMENT_MAX_PER_RUN} projected comments",
            "remediation": "archive or compact comment history before creating more",
        })
    per_subject = sum(
        1 for item in current.comments.values()
        if (not item.legacy and item.node_id == node_id
            and item.node_generation == node_generation))
    if per_subject >= COMMENT_MAX_PER_NODE_GENERATION:
        raise HTTPException(409, {
            "code": "comment_subject_limit_reached",
            "message": (f"experiment #{node_id} generation {node_generation} already has "
                        f"{COMMENT_MAX_PER_NODE_GENERATION} comments"),
            "remediation": "resolve or consolidate the existing discussion",
        })
    comment_id = ""
    for _ in range(128):
        candidate = "cmt_" + secrets.token_hex(16)
        if candidate not in current.comments:
            comment_id = candidate
            break
    if not comment_id:
        raise HTTPException(503, {
            "code": "comment_id_unavailable",
            "message": "the server could not allocate a unique comment id",
            "remediation": "retry with the same command idempotency key",
            "retryable": True,
        })
    return {
        "comment_id": comment_id,
        "node_id": node_id,
        "node_generation": node_generation,
        "text": ctx.comment_text(data.get("text")),
        "actor_kind": ("deployment_owner" if getattr(srv, "owner_auth_enabled", False)
                       else "local_operator"),
        "version": 1,
    }


def _normalize_comment_revision(ctx: _ControlIntake) -> dict:
    """comment_edited / comment_resolution_changed: one optimistic revision of an existing comment."""
    data = ctx.data
    srv = ctx.srv
    event_type = ctx.event_type
    current = ctx.state()
    raw_comment_id = data.get("comment_id")
    # Legacy annotations are projected under a synthetic lookup-only id. Accept that exact
    # shape solely so the caller gets the intentional read-only result below; it is never
    # admitted into a modern collaboration event (modern ids remain strictly validated).
    if (isinstance(raw_comment_id, str)
            and re.fullmatch(r"legacy_(?:0|[1-9]\d*)", raw_comment_id)):
        comment_id = raw_comment_id
    else:
        comment_id = ctx.comment_id(raw_comment_id)
    comment = current.comments.get(comment_id)
    if comment is None:
        raise HTTPException(404, {
            "code": "comment_not_found",
            "message": "the comment does not exist in this run generation",
            "remediation": "refresh the collaboration panel",
        })
    if not comment.editable or comment.legacy:
        raise HTTPException(409, {
            "code": "legacy_comment_read_only",
            "message": "legacy annotations have no verifiable actor or lifecycle and are read-only",
            "remediation": "create a new attributed comment instead",
        })
    if comment.version >= COMMENT_MAX_VERSION:
        raise HTTPException(409, {
            "code": "comment_version_limit_reached",
            "message": f"comment {comment_id} reached its {COMMENT_MAX_VERSION}-revision limit",
            "remediation": "resolve it and create a concise follow-up comment",
        })
    supplied_node_id = ctx.strict_integer(data.get("node_id"), "node_id")
    supplied_generation = ctx.strict_integer(
        data.get("node_generation"), "node_generation")
    if (supplied_node_id != comment.node_id
            or supplied_generation != comment.node_generation):
        raise HTTPException(409, {
            "code": "comment_subject_changed",
            "message": "the submitted node lifecycle does not own this comment",
            "remediation": "refresh the collaboration panel before editing this comment",
        })
    expected_version = ctx.comment_version(data.get("expected_version"))
    if comment.version != expected_version:
        raise HTTPException(409, {
            "code": "comment_version_changed",
            "message": (f"comment {comment_id} is version {comment.version}, not "
                        f"{expected_version}"),
            "current_version": comment.version,
            "remediation": "refresh the comment and re-apply the edit to its current version",
        })
    normalized = {
        "comment_id": comment_id,
        "node_id": comment.node_id,
        "node_generation": comment.node_generation,
        "base_version": expected_version,
        "version": expected_version + 1,
        "actor_kind": ("deployment_owner" if getattr(srv, "owner_auth_enabled", False)
                       else "local_operator"),
    }
    if event_type == EV_COMMENT_EDITED:
        text = ctx.comment_text(data.get("text"))
        if text == comment.text:
            raise HTTPException(409, {
                "code": "comment_unchanged",
                "message": "the edited text is identical to the current comment",
                "remediation": "no update is needed",
            })
        normalized["text"] = text
    else:
        resolved = data.get("resolved")
        if not isinstance(resolved, bool):
            raise HTTPException(400, "resolved must be a boolean")
        if resolved == comment.resolved:
            raise HTTPException(409, {
                "code": "comment_resolution_unchanged",
                "message": "the comment already has that resolution state",
                "remediation": "refresh the collaboration panel",
            })
        normalized["resolved"] = resolved
    return normalized


def _normalize_concept_tag_edited(ctx: _ControlIntake) -> dict:
    # PART V Phase 2b: an operator replaces one node's concept tags. Generation-fenced like a comment
    # (the tags subject is the node), and each id is canonicalized/validated against the SAME
    # concept_id() contract the /concepts frame ships, so the fold + UI key the same vocabulary.
    from looplab.serve.concept_frame import MAX_CONCEPTS_PER_NODE, concept_id
    data = ctx.data
    current = ctx.state()
    node_id = ctx.node("node_id")
    node_generation = ctx.strict_integer(data.get("node_generation"), "node_generation")
    if node_generation < 0:
        raise HTTPException(400, "node_generation must be non-negative")
    node = current.nodes[node_id]
    if node.attempt != node_generation:
        raise HTTPException(409, {
            "code": "node_generation_changed",
            "message": (f"experiment #{node_id} is generation {node.attempt}, not "
                        f"{node_generation}"),
            "remediation": "refresh the run before re-tagging this experiment",
        })
    raw_concepts = data.get("concepts")
    if not isinstance(raw_concepts, list):
        raise HTTPException(400, "concepts must be a list of axis/slug ids")
    if len(raw_concepts) > MAX_CONCEPTS_PER_NODE:
        raise HTTPException(400, f"concepts exceeds {MAX_CONCEPTS_PER_NODE} ids")
    canonical: list[str] = []
    for raw in raw_concepts:
        cid = concept_id(raw) if isinstance(raw, str) else None
        if cid is None:
            raise HTTPException(400, f"invalid concept id: {raw!r}")
        if cid not in canonical:                     # dedup, preserve order
            canonical.append(cid)
    return {"node_id": node_id, "node_generation": node_generation, "concepts": canonical}


def _normalize_run_concepts(ctx: _ControlIntake) -> dict:
    # PART V (D): operator/assistant sets the RUN's BASE concept set (last-write-wins; the fold flows it
    # through the DAG). No node fence — the subject is the run; the top-level expected_generation run
    # token still guards last-write-wins. Each id passes the same concept_id() contract as the frame.
    from looplab.serve.concept_frame import MAX_CONCEPTS_PER_NODE, concept_id
    raw_concepts = ctx.data.get("concepts")
    if not isinstance(raw_concepts, list):
        raise HTTPException(400, "concepts must be a list of axis/slug ids")
    if len(raw_concepts) > MAX_CONCEPTS_PER_NODE:
        raise HTTPException(400, f"concepts exceeds {MAX_CONCEPTS_PER_NODE} ids")
    canonical = []
    for raw in raw_concepts:
        cid = concept_id(raw) if isinstance(raw, str) else None
        if cid is None:
            raise HTTPException(400, f"invalid concept id: {raw!r}")
        if cid not in canonical:
            canonical.append(cid)
    # Mega-review L1: reject a clear-to-empty base. With `concept_run_base` on, an empty base re-arms the
    # engine's `_maybe_seed_run_base_concepts` (its gate is "base is empty"), which silently re-populates
    # it from the first authored node next cadence — the operator's clear would be undone. Empty is thus
    # indistinguishable from "never seeded"; to remove base concepts, disable `concept_run_base` instead.
    # Matches the RunControlTools.set_run_concepts tool, which rejects the same.
    if not canonical:
        raise HTTPException(400, "run base concepts cannot be empty — set a real base or disable concept_run_base")
    return {"concepts": canonical}


# ------------------------------------------------------------------ Card board normalizers

def _normalize_card_reprioritized(ctx: _ControlIntake) -> dict:
    card_id, _card_value = ctx.card()
    priority = ctx.integer("priority")
    if not 0 <= priority < 256:
        raise HTTPException(400, "priority must be between 0 and 255")
    return {
        "id": card_id, "priority": priority,
        "source": "operator", "pinned": True,
    }


def _normalize_card_edited(ctx: _ControlIntake) -> dict:
    card_id, card = ctx.card()
    statement = ctx.text("statement", limit=4_000)
    if statement == card.statement:
        raise HTTPException(409, "Card display statement is unchanged")
    return {"id": card_id, "statement": statement, "source": "operator"}


def _normalize_card_resource_pinned(ctx: _ControlIntake) -> dict:
    data = ctx.data
    card_id, card = ctx.card()
    if data.get("gpus") is None:
        raise HTTPException(400, "card_resource_pinned.gpus is required")
    gpus = ctx.integer("gpus")
    if gpus < 0:
        raise HTTPException(400, "gpus must be non-negative")
    memory = ctx.integer("gpu_mem_mib", required=False)
    if memory is not None and memory < 0:
        raise HTTPException(400, "gpu_mem_mib must be non-negative")
    # Cap at the fold's acceptance range (`_CARD_REPLAY_NODE_ID_MAX` = 2**31-1). In count-only mode
    # (no memory inventory) the per-GPU envelope check below is skipped, so without this bound a
    # larger value would be accepted as a "successful" command yet SILENTLY discard the whole pin at
    # fold time (`_on_card_resource_pinned` returns early on an out-of-range field).
    if memory is not None and memory > (1 << 31) - 1:
        raise HTTPException(400, "gpu_mem_mib exceeds the maximum (2147483647)")
    if gpus == 0 and memory is not None:
        raise HTTPException(400, "a CPU-only resource pin cannot request GPU memory")
    gpu_count, gpu_memory = _card_resource_envelope()
    if gpus > gpu_count:
        raise HTTPException(
            400, f"gpus exceeds the current visible GPU envelope ({gpu_count})")
    requested = {"gpus": gpus}
    if memory is not None:
        requested["gpu_mem_mib"] = memory
    effective = effective_card_footprint(
        card.footprint, requested, gpu_count=gpu_count, gpu_memory_mib=gpu_memory)
    if memory is not None and len(gpu_memory) == gpu_count and gpus > 0:
        envelope = sorted(gpu_memory, reverse=True)[gpus - 1]
        if memory > envelope:
            raise HTTPException(
                400, f"gpu_mem_mib exceeds the {gpus}-GPU envelope ({envelope} MiB/GPU)")
    if effective is None or effective.get("gpus") != gpus:
        raise HTTPException(400, "resource pin cannot form a schedulable footprint")
    return {
        "id": card_id, **requested,
        "source": "operator", "pinned": True,
    }


def _normalize_card_dropped(ctx: _ControlIntake) -> dict:
    card_id, _card_value = ctx.card()
    reason = ctx.text("reason", required=False, limit=400) or "operator dropped"
    return {"id": card_id, "reason": reason, "dropped_by": "operator"}


def _normalize_card_reopened(ctx: _ControlIntake) -> dict:
    """The drop's counterpart, and the SAME shape on purpose.

    `replay_cards.py::_on_card_reopened` reuses `_bounded_card_drop_receipt`, so both receipts must be
    one shape — a second, subtly different bound is how two halves of one lifecycle switch come to
    disagree about which ids are admissible. `by` rather than `dropped_by` because the receipt reads
    either and "dropped_by" on a reopen row would be a lie to whoever reads the log.
    """
    card_id, _card_value = ctx.card()
    reason = ctx.text("reason", required=False, limit=400) or "operator reopened"
    return {"id": card_id, "reason": reason, "by": "operator"}


# ------------------------------------------------------------------ append-time preconditions
#
# Every one of these runs against a FRESH fold, immediately before the strict-lock append, inside
# `_append_collaboration_intent`'s bounded CAS loop.  Intake validated the same subject, but an
# engine can move it in between: this is what stops the append from landing on a Card that has since
# been dropped, a node that has since been reset, or a comment somebody else already revised.

def _precondition_card(state, event_type: str, data: dict, envelope) -> Optional[dict]:
    card_id = data.get("id")
    card = state.cards.get(card_id) if isinstance(card_id, str) else None
    if card is None:
        return _error(
            "card_not_found",
            f"the Card target {card_id!r} no longer exists in this run generation",
            "refresh the Card board before submitting another operator control",
        )
    # Terminal Cards are closed to further operator MUTATION OF THEIR WORK (edit / reprioritize /
    # resource-pin). The React client hides these controls, but a stale client / direct API caller
    # must not append edit/reprioritize/pin history onto an already dropped or merged Card (a
    # self-contradictory board row plus a mutate-after-drop sequence in the append-only log).
    # EV_CARD_DROPPED is deliberately EXCLUDED so an operator keeps authority over the DROP itself
    # on a terminal Card: a re-drop with the SAME reason is a no-op, and overriding an engine-set
    # drop reason/author is an intentional operator-wins affordance — replay applies the LAST
    # card_dropped row, so this is a bounded reason/author revision, NOT byte-idempotence.
    if event_type in {EV_CARD_EDITED, EV_CARD_REPRIORITIZED, EV_CARD_RESOURCE_PINNED} and (
            getattr(card, "status", None) == "dropped"
            or getattr(card, "merged_into", None) is not None):
        return _error(
            "card_lifecycle_closed",
            f"the Card target {card_id!r} is already dropped or merged and cannot be modified",
            "refresh the Card board; a terminal Card no longer accepts edits, priority or "
            "resource pins",
        )
    # A REOPEN THE FOLD WILL DECLINE MUST BE REFUSED HERE, not accepted and then quietly ignored.
    # Only an operator's own drop is undoable — an engine `card_auto_dropped` retires a rejected
    # proposal and reopening one put it back on the selectable board permanently, since
    # `_drop_card_once` is idempotent by history and could never retire it again. `_apply_card_drops`
    # has always refused that; nothing here did, so the POST returned 2xx, `card_reopened` was
    # appended, a success toast fired, and the browser's optimistic `proposed` was never reconciled
    # away because it waits on a status change the fold never makes. A refusal rolls it back.
    #
    # `card.reopenable` is the FOLD'S OWN ANSWER and is deliberately not re-derived here: it is not
    # `dropped_by == "operator"`, which reads only the HEAD receipt and so passes for an operator
    # drop written over an engine one. Two spellings of this rule is how the server comes to accept
    # exactly what replay throws away, which is the defect being closed.
    #
    # Scoped to a card that IS dropped: a reopen of a live card has nothing to undo, the fold treats
    # it as a no-op, and turning that into a 4xx would be a contract change nothing asked for.
    if (event_type == EV_CARD_REOPENED and getattr(card, "status", None) == "dropped"
            and not getattr(card, "reopenable", False)):
        return _error(
            "card_reopen_not_permitted",
            f"the Card target {card_id!r} was stopped by the engine, and only an operator's own "
            "drop can be reopened",
            "refresh the Card board; an engine retirement is part of the run's own lifecycle and "
            "is not an operator control",
        )
    if event_type == EV_CARD_RESOURCE_PINNED:
        gpus = data.get("gpus")
        memory = data.get("gpu_mem_mib")
        gpu_count, gpu_memory = (
            envelope if envelope is not None else _card_resource_envelope())
        if (type(gpus) is not int or gpus < 0 or gpus > gpu_count
                or (memory is not None and (type(memory) is not int or memory < 0))
                or (gpus == 0 and memory is not None)):
            return _error(
                "card_resource_envelope_changed",
                "the Card resource pin no longer fits the visible GPU envelope",
                "refresh hardware state and submit a new pin within the current envelope",
            )
        if memory is not None and len(gpu_memory) == gpu_count and gpus > 0:
            envelope = sorted(gpu_memory, reverse=True)[gpus - 1]
            if memory > envelope:
                return _error(
                    "card_resource_envelope_changed",
                    "the Card GPU-memory pin no longer fits the visible GPU envelope",
                    "refresh hardware state and submit a new pin within the current envelope",
                )
    return None


def _precondition_run_concepts(state, event_type: str, data: dict, envelope) -> Optional[dict]:
    # The run (rather than a comment/node/Card) is the exact collaboration subject. Its
    # generation fence is rechecked by the caller immediately before this precondition.
    return None


def _precondition_comment_created(state, event_type: str, data: dict, envelope) -> Optional[dict]:
    node_id = data.get("node_id")
    generation = data.get("node_generation")
    node = state.nodes.get(node_id)
    if node is None or node.attempt != generation:
        current = getattr(node, "attempt", None)
        error = _error(
            "node_generation_changed",
            f"the comment target is no longer experiment #{node_id} generation {generation}",
            "refresh the run and create a new comment against the current lifecycle")
        error["current_generation"] = current
        return error
    if data.get("comment_id") in state.comments:
        return _error(
            "comment_id_conflict", "the allocated comment id is already present",
            "submit a new command with a new idempotency key")
    # Count only MODERN comments (legacy EV_ANNOTATION notes are uncompactable in an append-only
    # log): this append-time recheck must match normalize_control's intake cap AND the fold in
    # comment_projection.apply_comment_event, or a heavily-annotated run accepts a comment at
    # intake then silently drops it here — the exact bug the modern-count cap fixes.
    modern_count = sum(1 for item in state.comments.values() if not item.legacy)
    if modern_count >= COMMENT_MAX_PER_RUN:
        return _error(
            "comment_run_limit_reached",
            f"this run already has {COMMENT_MAX_PER_RUN} projected comments",
            "archive or compact comment history before creating more comments")
    per_subject = sum(
        1 for item in state.comments.values()
        if (not item.legacy and item.node_id == node_id
            and item.node_generation == generation))
    if per_subject >= COMMENT_MAX_PER_NODE_GENERATION:
        return _error(
            "comment_subject_limit_reached",
            (f"experiment #{node_id} generation {generation} already has "
             f"{COMMENT_MAX_PER_NODE_GENERATION} comments"),
            "resolve or consolidate the existing discussion")
    return None


def _precondition_concept_tags(state, event_type: str, data: dict, envelope) -> Optional[dict]:
    # Phase 2b: a concept re-tag targets a NODE (not a comment_id). Re-verify the exact subject
    # immediately before the strict-lock append, in case the node was reset since intake.
    node_id = data.get("node_id")
    generation = data.get("node_generation")
    node = state.nodes.get(node_id)
    if node is None or node.attempt != generation:
        error = _error(
            "node_generation_changed",
            f"the re-tag target is no longer experiment #{node_id} generation {generation}",
            "refresh the run and re-tag against the current lifecycle")
        error["current_generation"] = getattr(node, "attempt", None)
        return error
    return None


def _precondition_comment_revision(state, event_type: str, data: dict, envelope) -> Optional[dict]:
    comment_id = data.get("comment_id")
    comment = state.comments.get(comment_id)
    if comment is None or not comment.editable:
        return _error(
            "comment_not_found", "the editable comment no longer exists",
            "refresh the collaboration panel")
    if (comment.node_id != data.get("node_id")
            or comment.node_generation != data.get("node_generation")):
        return _error(
            "comment_subject_changed", "the comment subject identity no longer matches",
            "inspect the event history before retrying")
    expected = data.get("base_version")
    if comment.version != expected:
        error = _error(
            "comment_version_changed",
            f"comment {comment_id} is version {comment.version}, not {expected}",
            "refresh the comment and re-apply the edit to its current version")
        error["current_version"] = comment.version
        return error
    if comment.version >= COMMENT_MAX_VERSION or data.get("version") > COMMENT_MAX_VERSION:
        return _error(
            "comment_version_limit_reached",
            f"comment {comment_id} reached its {COMMENT_MAX_VERSION}-revision limit",
            "resolve it and create a concise follow-up comment")
    return None


# ------------------------------------------------------------------ engine decisions
#
# Return `(decision, error)` to settle the command, or None to fall through to the shared
# engine-policy tail in `RunCommandService._decision`.

def _decide_run_abort(service, rd: Path, event_type: str, state, alive: bool,
                      pending_finalize: bool) -> Optional[tuple]:
    if state.finished and alive:
        return "reject", _error(
            "engine_finishing", "the engine is still completing its terminal write-out",
            "retry after engine_running becomes false", retryable=True)
    if pending_finalize:
        return "attach", None
    # A guarded-abort finish (`error`, or the ceiling's `budget_exhausted`) still owes its wrap-up,
    # so an operator's abort on it is APPENDED like on an `error` finish — a literal `"error"` here
    # answered `noop` for every ceiling-ended run (`events/finalize_scope.py::is_guarded_abort`).
    if state.finished and not is_guarded_abort(state.stop_reason):
        return "noop", None
    return "append", None


def _decide_pause(service, rd: Path, event_type: str, state, alive: bool,
                  pending_finalize: bool) -> Optional[tuple]:
    if pending_finalize:
        return "reject", _error(
            "finalize_in_progress", "cannot stop a run while finalization is pending",
            "wait for finalization to finish; its command record remains observable",
            retryable=True)
    if state.finished or (state.paused and not alive):
        return "noop", None
    return "append", None


def _decide_restart(service, rd: Path, event_type: str, state, alive: bool,
                    pending_finalize: bool) -> Optional[tuple]:
    if pending_finalize:
        return "reject", _error(
            "finalize_in_progress", "cannot restart while finalization is pending",
            "wait for finalization to finish, then submit a new restart command",
            retryable=True)
    # Always append a fresh restart boundary. Even a currently paused/dead run needs an exact
    # request sequence that the replacement owner's later resume_served can satisfy.
    return "append", None


def _decide_resume(service, rd: Path, event_type: str, state, alive: bool,
                   pending_finalize: bool) -> Optional[tuple]:
    """resume / run_reopened: both re-open a stopped run, so both answer the same questions."""
    if pending_finalize:
        return "reject", _error(
            "finalize_in_progress", "cannot resume while finalization is pending",
            "wait for finalization to finish, then submit a new resume command",
            retryable=True)
    if state.finished and alive:
        return "reject", _error(
            "engine_finishing", "the engine is still completing its terminal write-out",
            "retry after engine_running becomes false", retryable=True)
    if alive and not state.paused and not state.finished:
        return "noop", None
    return "append", None


def _decide_approval_granted(service, rd: Path, event_type: str, state, alive: bool,
                             pending_finalize: bool) -> Optional[tuple]:
    if not state.awaiting_approval:
        return "reject", _error(
            "approval_not_requested", "the run is not awaiting result approval",
            "approve only while the run phase is approval")
    return None


def _decide_spec_approved(service, rd: Path, event_type: str, state, alive: bool,
                          pending_finalize: bool) -> Optional[tuple]:
    if not state.spec_approval_requested or state.spec_confirmed:
        return "reject", _error(
            "ratification_not_requested", "the run is not awaiting eval-spec ratification",
            "ratify only while the run phase is spec_approval")
    return None


# ================================================================== the per-event registry
#
# Five tables, one per concern, each asserted COMPLETE against `CONTROL_EVENTS` and then joined into
# `CONTROL_SPECS`.  The completeness assertions are the mechanism: a new control event cannot ship
# with a missing handler, because the module refuses to import.

assert set(CONTROL_DATA_FIELDS) == set(CONTROL_EVENTS), "every control event needs a data allowlist"
# Protocol owns the wire field metadata; this module owns validation/normalization.
assert _INJECT_IMPORT_FIELDS <= CONTROL_DATA_FIELDS[EV_INJECT_NODE], (
    "the cross-run import fields must be accepted by inject_node's payload allowlist")

# `None` = this event needs no per-event intake rule beyond its allow-list (its payload is empty, or
# every field is already pinned by the shared checks).  Five events share a normalizer with a
# sibling; that is deliberate and visible here rather than hidden in an `elif ... in {A, B}`.
_CONTROL_NORMALIZERS: dict[str, Optional[Callable]] = {
    EV_RUN_ABORT: _normalize_run_abort,
    EV_PAUSE: None,
    EV_RESTART: _normalize_restart,
    EV_RESUME: None,
    EV_RUN_REOPENED: None,
    EV_NODE_ABORT: _normalize_node_abort,
    EV_NODE_RESET: _normalize_node_reset,
    EV_BUDGET_EXTEND: _normalize_budget_extend,
    EV_HINT: _normalize_hint,
    EV_SET_STRATEGY: _normalize_set_strategy,
    EV_METRIC_RETARGET: _normalize_metric_retarget,
    EV_TRACK_REQUESTED: _normalize_track_requested,
    EV_FORCE_CONFIRM: _normalize_node_target,
    EV_FORCE_ABLATE: _normalize_node_target,
    EV_FORK: _normalize_fork,
    EV_INJECT_NODE: _normalize_inject_node,
    EV_DEEP_RESEARCH: None,
    EV_RESEARCH_COMPLETED: _normalize_research_completed,
    EV_REPORT_GENERATED: _normalize_report_generated,
    EV_APPROVAL_GRANTED: _normalize_approval_granted,
    EV_SPEC_APPROVED: _normalize_spec_approved,
    EV_ANNOTATION: _normalize_annotation,
    EV_COMMENT_CREATED: _normalize_comment_created,
    EV_COMMENT_EDITED: _normalize_comment_revision,
    EV_COMMENT_RESOLUTION_CHANGED: _normalize_comment_revision,
    EV_CONCEPT_TAG_EDITED: _normalize_concept_tag_edited,
    EV_RUN_CONCEPTS: _normalize_run_concepts,
    EV_PROMOTE: _normalize_promote,
    EV_HYPOTHESIS_ADDED: _normalize_hypothesis_added,
    EV_HYPOTHESIS_UPDATED: _normalize_hypothesis_updated,
    EV_CARD_REPRIORITIZED: _normalize_card_reprioritized,
    EV_CARD_EDITED: _normalize_card_edited,
    EV_CARD_RESOURCE_PINNED: _normalize_card_resource_pinned,
    EV_CARD_DROPPED: _normalize_card_dropped,
    EV_CARD_REOPENED: _normalize_card_reopened,
}
assert set(_CONTROL_NORMALIZERS) == set(CONTROL_EVENTS), (
    "every control event needs an explicit intake normalizer (None = allow-list only)")

# Only COLLABORATION_EVENTS reach the append-time recheck; the cross-check below is what makes the
# `None`s honest.  It also closes the hole the old if/elif chain had: an unlisted event type fell
# through to the COMMENT branch, so a new collaboration event added to `COLLABORATION_EVENTS` and
# nowhere else was silently rechecked against a comment id it does not have.
_CONTROL_PRECONDITIONS: dict[str, Optional[Callable]] = {
    EV_RUN_ABORT: None,
    EV_PAUSE: None,
    EV_RESTART: None,
    EV_RESUME: None,
    EV_RUN_REOPENED: None,
    EV_NODE_ABORT: None,
    EV_NODE_RESET: None,
    EV_BUDGET_EXTEND: None,
    EV_HINT: None,
    EV_SET_STRATEGY: None,
    EV_METRIC_RETARGET: None,
    EV_TRACK_REQUESTED: None,
    EV_FORCE_CONFIRM: None,
    EV_FORCE_ABLATE: None,
    EV_FORK: None,
    EV_INJECT_NODE: None,
    EV_DEEP_RESEARCH: None,
    EV_RESEARCH_COMPLETED: None,
    EV_REPORT_GENERATED: None,
    EV_APPROVAL_GRANTED: None,
    EV_SPEC_APPROVED: None,
    EV_ANNOTATION: None,
    EV_COMMENT_CREATED: _precondition_comment_created,
    EV_COMMENT_EDITED: _precondition_comment_revision,
    EV_COMMENT_RESOLUTION_CHANGED: _precondition_comment_revision,
    EV_CONCEPT_TAG_EDITED: _precondition_concept_tags,
    EV_RUN_CONCEPTS: _precondition_run_concepts,
    EV_PROMOTE: None,
    EV_HYPOTHESIS_ADDED: None,
    EV_HYPOTHESIS_UPDATED: None,
    EV_CARD_REPRIORITIZED: _precondition_card,
    EV_CARD_EDITED: _precondition_card,
    EV_CARD_RESOURCE_PINNED: _precondition_card,
    EV_CARD_DROPPED: _precondition_card,
    EV_CARD_REOPENED: _precondition_card,
}
assert set(_CONTROL_PRECONDITIONS) == set(CONTROL_EVENTS), (
    "every control event needs an explicit append-time precondition (None = not applicable)")
assert {event for event, handler in _CONTROL_PRECONDITIONS.items() if handler is not None} == set(
    COLLABORATION_EVENTS), (
    "exactly the collaboration events take the append-time recheck path")

# `None` = the shared engine-policy tail decides (see `RunCommandService._decision`).  Collaboration
# events never reach the dispatch at all: their decision is settled before liveness is even probed.
_CONTROL_DECISIONS: dict[str, Optional[Callable]] = {
    EV_RUN_ABORT: _decide_run_abort,
    EV_PAUSE: _decide_pause,
    EV_RESTART: _decide_restart,
    EV_RESUME: _decide_resume,
    EV_RUN_REOPENED: _decide_resume,
    EV_NODE_ABORT: None,
    EV_NODE_RESET: None,
    EV_BUDGET_EXTEND: None,
    EV_HINT: None,
    EV_SET_STRATEGY: None,
    EV_METRIC_RETARGET: None,
    EV_TRACK_REQUESTED: None,
    EV_FORCE_CONFIRM: None,
    EV_FORCE_ABLATE: None,
    EV_FORK: None,
    EV_INJECT_NODE: None,
    EV_DEEP_RESEARCH: None,
    EV_RESEARCH_COMPLETED: None,
    EV_REPORT_GENERATED: None,
    EV_APPROVAL_GRANTED: _decide_approval_granted,
    EV_SPEC_APPROVED: _decide_spec_approved,
    EV_ANNOTATION: None,
    EV_COMMENT_CREATED: None,
    EV_COMMENT_EDITED: None,
    EV_COMMENT_RESOLUTION_CHANGED: None,
    EV_CONCEPT_TAG_EDITED: None,
    EV_RUN_CONCEPTS: None,
    EV_PROMOTE: None,
    EV_HYPOTHESIS_ADDED: None,
    EV_HYPOTHESIS_UPDATED: None,
    EV_CARD_REPRIORITIZED: None,
    EV_CARD_EDITED: None,
    EV_CARD_RESOURCE_PINNED: None,
    EV_CARD_DROPPED: None,
    EV_CARD_REOPENED: None,
}
assert set(_CONTROL_DECISIONS) == set(CONTROL_EVENTS), (
    "every control event needs an explicit engine decision (None = the shared policy tail)")


# The single policy registry for every appendable control event.  Keep the equality assertion: a new
# CONTROL_EVENTS member must make an explicit engine/postcondition choice instead of silently falling
# into an unsafe default.
#
# The event type is spelled ONCE, as the mapping key, and stamped onto each spec below (doc 25
# SC-16). It used to appear twice per entry — key and first argument — so a copy-paste could pair one
# event's key with another's spec, and NOTHING asserted the two agreed: the mismatch would surface as
# a control silently running under the wrong engine policy (a NO_SPAWN intent waking a dead engine,
# or an ENSURE_RUNNING command quietly never spawning one).
_CONTROL_POLICIES: dict[str, tuple[EnginePolicy, str]] = {
    EV_RUN_ABORT: (EnginePolicy.ENSURE_DRIVER_PRESERVE_STOP, "finished_and_stopped"),
    # `paused`, NOT `paused_and_stopped`. The postcondition must observe THE EFFECT THE OPERATOR
    # ASKED FOR. A pause is a folded, reversible flag; the engine PROCESS then finishes its in-flight
    # evaluation before releasing engine.lock, which on a GPU stage takes hours. Requiring the
    # process exit meant that on exactly the runs where pausing matters the command could only ever
    # time out — measured on `rubertlite-dr-unified-v2`: the pause landed in under a second and the
    # command reported "not observed in time" twenty minutes later, after which the run could not be
    # controlled at all. The process half is still reported, as `engine_stopped` on the succeeded
    # record; it is an observation, never a gate. `paused_and_stopped` remains a live postcondition
    # value in `RunCommandService._postcondition` because durable records written before this change
    # carry it.
    EV_PAUSE: (EnginePolicy.NO_SPAWN, "paused"),
    EV_RESTART: (EnginePolicy.RESTART_AFTER_EXIT, "restart_served"),
    EV_RESUME: (EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_RUN_REOPENED: (EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_NODE_ABORT: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_NODE_RESET: (EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_BUDGET_EXTEND: (EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_HINT: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_SET_STRATEGY: (EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    # A folded intent, like a hint: the fold re-ranks every node the moment it lands, a live engine
    # reads it at its next fold, and a stopped run needs no engine to be re-ranked (doc 68 68.2).
    EV_METRIC_RETARGET: (EnginePolicy.NO_SPAWN, "folded_intent"),
    # A folded queue entry the LIVE engine serves (doc 73 §1.4, `engine/track_lane.py`); a stopped
    # run keeps it queued for its next engine (`looplab evaluate-track` answers on a stopped run).
    EV_TRACK_REQUESTED: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_FORCE_CONFIRM: (EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_FORCE_ABLATE: (EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_FORK: (EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_INJECT_NODE: (EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_DEEP_RESEARCH: (EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_RESEARCH_COMPLETED: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_REPORT_GENERATED: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_APPROVAL_GRANTED: (EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_SPEC_APPROVED: (EnginePolicy.ENSURE_RUNNING, "engine_ack"),
    EV_ANNOTATION: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_COMMENT_CREATED: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_COMMENT_EDITED: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_COMMENT_RESOLUTION_CHANGED: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_CONCEPT_TAG_EDITED: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_RUN_CONCEPTS: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_PROMOTE: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_HYPOTHESIS_ADDED: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_HYPOTHESIS_UPDATED: (EnginePolicy.NO_SPAWN, "folded_intent"),
    # Layer 6 operator Card steering never spawns compute, wakes a dead engine, or opens a
    # request/done counter. Live selection and scheduling observe these folded intents.
    EV_CARD_REPRIORITIZED: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_CARD_EDITED: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_CARD_RESOURCE_PINNED: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_CARD_DROPPED: (EnginePolicy.NO_SPAWN, "folded_intent"),
    EV_CARD_REOPENED: (EnginePolicy.NO_SPAWN, "folded_intent"),
}


CONTROL_SPECS: dict[str, ControlSpec] = {
    event_type: ControlSpec(event_type, policy, postcondition,
                            CONTROL_DATA_FIELDS[event_type],
                            _CONTROL_NORMALIZERS[event_type],
                            _CONTROL_PRECONDITIONS[event_type],
                            _CONTROL_DECISIONS[event_type])
    for event_type, (policy, postcondition) in _CONTROL_POLICIES.items()
}
assert set(CONTROL_SPECS) == set(CONTROL_EVENTS), "every control event needs an explicit ControlSpec"


# The `node_reset` stages whose fulfilment is an INTERNAL role: the fold drops the node's code and
# flags `rerun_from` (`events/replay.py::_on_node_reset`), and an external engine never re-develops
# (`orchestrator.py` skips its `_resets` batch), so admitted, the node would sit pending with no
# code. Every other stage — `eval`, or a pipeline stage such as `score` — is an evaluation the
# engine owns, and the harness manifest publishes exactly this pair as the refused one.
_EXTERNAL_REFUSED_RESET_STAGES = frozenset({"propose", "implement"})


def external_intent_refusal(event_type: str, data: dict) -> Optional[HTTPException]:
    """The refusal an EXTERNALLY driven run gives this control intent, or None to admit it.

    PURE: whether the run IS external is the caller's question (`_external_mode_restriction` reads
    the snapshot, and only for an intent this answers non-None), so the rule is a truth table over
    the intent alone (`tests/test_external_intent_refusal.py`). It runs BEFORE the event's
    normalizer, so a stage is compared as that normalizer will store it (stripped)."""
    if event_type in (EV_FORK, EV_FORCE_ABLATE, EV_DEEP_RESEARCH):
        return HTTPException(409, "external harness owns this decision; submit a ready-made "
                             "inject_node with parent_id and code/files for a new candidate")
    if event_type == EV_NODE_RESET:
        stage = data.get("from_stage", "eval")
        if isinstance(stage, str) and stage.strip() in _EXTERNAL_REFUSED_RESET_STAGES:
            return HTTPException(409, "external harness refuses node_reset from propose or "
                                 "implement; reset from eval (or a later pipeline stage) to "
                                 "remeasure, or submit a corrected ready-made inject_node")
        return None
    if event_type == EV_INJECT_NODE and not (data.get("code") or data.get("files") or
                                              data.get("deleted") or
                                              (data.get("source_run") and
                                               data.get("source_node") is not None)):
        return HTTPException(400, "external harness requires code or files for inject_node")
    return None


# THE AGENT TOKEN ON AN INTERNAL RUN (doc 70 70.8, critic 2026-09-29, driven). `LOOPLAB_HARNESS_TOKEN`
# is the `owner` principal (`serve/server.py`), and the refusal below held only on an EXTERNALLY
# driven run, so on an internal run its holder queued a `fork` the live engine then built with the
# owner's paid Developer — though the harness manifest promises "scoped agent requests cannot …
# invoke LoopLab's owner model workflows". An internal run's own loop IS that workflow. Two readings
# of "what it may not do" were each too wide. A hand list — the external rule's five intents and the
# three starts — left every other intent whose engine policy is not `NO_SPAWN` able to START the loop:
# the command worker spawns a plain `looplab resume` for it when no engine is alive (critic crit_v60
# F1, driven: an agent `node_reset`, a ready-made inject and a `budget_extend` each did). The policy
# table — keep the `NO_SPAWN` intents — then left twenty that never START the engine but DRIVE it: a
# `metric_retarget` replaced the run's goal and discarded paid confirmation evals, a `promote` moved
# the exported champion, a dropped Card cancelled an in-flight evaluation, a memo, a report and a
# hypothesis spoke as the owner's (critic crit_v61 M1, driven). So on an internal run (or one with
# no snapshot) the token keeps an explicit ALLOW-LIST — a pause, an annotation and a new comment, each
# of which only adds or stops and none of which any agent or engine prompt reads — asserted below to
# be `NO_SPAWN` intents; anything added later is refused to it until it is added here. A node abort (it
# moved the champion and cancels an in-flight eval, as a dropped Card does) and an edit or resolution
# of a comment (the owner's included, attributed to the owner) are not additive and were taken off it
# (critic crit_v62 F4, driven). So was a HINT, even one that only adds (critic crit_v63 L1, driven): a
# hint event names no author, every role reads the last six as "Operator directives" with the newest
# marked "MOST RECENT, follow this when they conflict" (`agents/hints.py::render_hint_directives`),
# so one agent hint outranked the owner's and six pushed it out of every prompt — refusing `replace`
# refused the word, not the effect. On an external run it meets the external rule.
AGENT_TOKEN_REFUSED_STARTS = frozenset({EV_RESUME, EV_RESTART, EV_RUN_REOPENED})
AGENT_TOKEN_INTERNAL_INTENTS = frozenset({EV_PAUSE, EV_ANNOTATION, EV_COMMENT_CREATED})
assert all(CONTROL_SPECS[kind].engine_policy is EnginePolicy.NO_SPAWN
           for kind in AGENT_TOKEN_INTERNAL_INTENTS), "an agent intent must never start the engine"


def agent_token_refusal(event_type: str, data: dict) -> Optional[HTTPException]:
    """The refusal the AGENT token gets for this intent on an INTERNAL run, or None to admit it.

    PURE, like `external_intent_refusal` beside it: whether the run is internal is the caller's
    question (`_external_mode_restriction`). Admitted: exactly `AGENT_TOKEN_INTERNAL_INTENTS`; an
    unknown type is refused (fail closed). A 403 with its own code — this credential may not,
    whoever else may — which the command service records on the REJECTED command as it is."""
    if event_type in AGENT_TOKEN_INTERNAL_INTENTS:
        return None
    external = external_intent_refusal(event_type, data)
    if event_type in AGENT_TOKEN_REFUSED_STARTS:
        message = ("the agent token cannot start an internal run's own Researcher/Developer loop "
                   "(resume, restart or reopen)")
    elif event_type == EV_HINT:
        message = ("the agent token cannot add a hint to an internal run: every role reads the run's "
                   "hints as the operator's directives, the newest first")
    elif external is not None:
        message = ("the agent token cannot queue an intent LoopLab's own model fulfils on an "
                   "internal run: " + str(external.detail))
    else:
        message = (f"the agent token cannot submit {event_type!r} on an internal run: it starts, "
                   "drives or speaks for the owner's run")
    return agent_token_refused(message)


def agent_token_refused(message: str) -> HTTPException:
    """The agent token's one refusal shape (`agent_token_refused`, 403, not retryable)."""
    return HTTPException(403, {
        "code": "agent_token_refused", "message": message, "retryable": False,
        "remediation": ("on an internal run the agent token may only pause it, annotate a node "
                        "or add a comment; ask the operator to act with the owner's token, or "
                        "drive a run launched with external_harness")})


def run_is_external(rd: Path) -> bool:
    """Whether `rd` was launched with `external_harness`, read off ITS snapshot, not the server's
    ambient config — one UI serves many run modes. No snapshot reads internal (for the agent token
    that FAILS CLOSED); an unreadable one is refused with the one coded answer every serve reader
    gives it (`serve/http.py::REFUSALS`), never its parse error."""
    snapshot = Path(rd) / "config.snapshot.json"
    if not snapshot.is_file():
        return False
    from looplab.core.config import read_config_snapshot
    try:
        return bool(read_config_snapshot(snapshot).external_harness)
    except (OSError, ValueError) as exc:
        raise refusal("config_snapshot_unreadable") from exc


def refuse_agent_token_intent(rd: Path, event_type, data) -> None:
    """Raise the refusal the AGENT token gets for `event_type` on run `rd` — the submit rule, for a
    caller that holds an intent it did not submit through `normalize_control`: the retry of a
    durable record (critic crit_v60 F5, driven: the agent token re-drove the owner's failed
    `resume`, which spawned the run's loop)."""
    _external_mode_restriction(rd, str(event_type or ""),
                               dict(data) if isinstance(data, dict) else {}, agent_token=True)


def _external_mode_restriction(rd: Path, event_type: str, data: dict, *,
                               agent_token: bool = False) -> None:
    """Refuse intents that would call an internal role in an externally driven run — and, for the
    AGENT token, in an internal one (`agent_token_refusal`, doc 70 70.8)."""
    # Every control intent reaches this boundary through the command service (`normalize_control`).
    # An external run must never queue an intent whose engine fulfillment invokes its internal
    # Researcher/Developer loop.
    # Read the run's snapshot, not the server's ambient config: one UI serves many run modes.
    # The RULE first, and the snapshot only for an intent it would refuse: the snapshot's own
    # readers (a resume's admission, a budget extension's ceiling) each answer an UNREADABLE
    # snapshot with their own coded refusal or fail-closed default, and a blanket refusal here
    # pre-empted every one of them — as it did a reset from `eval`, which no mode refuses. Asking
    # the rule itself, rather than a hand-kept list of the intents it covers, is what keeps a
    # refusal added to the rule from being gated out before it runs (critic 2026-09-29).
    refused = external_intent_refusal(event_type, data)
    agent_refused = agent_token_refusal(event_type, data) if agent_token else None
    if refused is None and agent_refused is None:
        return
    # The run mode is unknowable from an unreadable snapshot: refused (`run_is_external`).
    if run_is_external(rd):
        if refused is not None:
            raise refused
        return                  # a resume of an external run is the harness's own to drive
    # An internal run — or one with no snapshot yet, which for the agent token FAILS CLOSED: the
    # owner's historical admission of such a run is untouched (`agent_refused` is None for it).
    if agent_refused is not None:
        raise agent_refused


def normalize_control(srv, rd: Path, event_type: str, data, *, agent_token: bool = False) -> dict:
    """Validate/normalize one control payload for both /control and /commands.

    The common preamble and tail serve every registered ControlSpec. External mode restrictions
    apply before the event-specific normalizer — and again over what it made of the payload — and
    before any durable append.
    """
    spec = CONTROL_SPECS.get(event_type)
    if spec is None:
        raise HTTPException(400, f"unknown control event: {event_type!r}")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise HTTPException(400, "control data must be a JSON object")
    data = dict(data)
    unknown = set(data) - spec.data_fields
    if unknown:
        raise HTTPException(
            400, f"{event_type} has unknown field(s): {', '.join(sorted(unknown))}")
    _external_mode_restriction(rd, event_type, data, agent_token=agent_token)

    if spec.normalize is not None:
        data = spec.normalize(_ControlIntake(srv, rd, event_type, data))
        # …and AGAIN over what the normalizer made of it (critic crit_v60 F3, driven): an import's
        # `{source_run, source_node}` reads as ready-made above, and the import then resolves it into
        # the node it WILL carry — for a source node reset from `implement`, no code and no files,
        # which the run's Developer would build. The rule judges that payload, not the reference.
        _external_mode_restriction(rd, event_type, data, agent_token=agent_token)

    try:
        # Encode INSIDE the guard: json.dumps(ensure_ascii=False) accepts a lone surrogate (valid
        # JSON \uD800) but str.encode("utf-8") then raises UnicodeEncodeError — a ValueError subclass.
        # Keeping the encode out here surfaced it as a 500; every sibling validation error is a 400.
        encoded_bytes = json.dumps(
            data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(400, f"control data must be finite, encodable JSON: {exc}") from exc
    if len(encoded_bytes) > 1_048_576:
        raise HTTPException(413, "control data is too large (maximum 1 MiB)")
    return data
