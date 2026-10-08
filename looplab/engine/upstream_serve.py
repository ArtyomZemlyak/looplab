"""The upstream lane served BY THE LIVE ENGINE (doc 73 §2.5, stages 2–4), under
`Settings.upstream_mode`.

WHY (2026-10-08). The upstream lane (doc 72, `engine/upstream.py`) promotes a measured capability
into the base the next lifecycles are seeded from — and every step of it refused while the engine
ran: propose, check and advance each meant pause → wait for exit → act → resume. The operator asked
for the opposite: fixes and capabilities reach the base IN THE BACKGROUND, and every agent knows.

THE MODES (`resolve_upstream_mode`, the one reader of the setting):

* `off` — the stopped-engine lane exactly as doc 72 shipped it.
* `propose` — an operation asked of a LIVE run (`UpstreamLane.propose/check/advance` from the API,
  the Assistant's upstream tools or MCP) is QUEUED (`lane_op_requested`) instead of refused, and
  this engine serves it between turns, without a pause, appending the lane's own rows and the
  positional `lane_op_done` receipt.
* `auto` — `propose`, and the engine also CHECKS every proposal against the current base once and
  ADVANCES every one whose measured gate passed (`auto_next_op`, deterministic action ids, so a crash
  re-entry is the lane's own idempotent retry, invariant #3). Proposals are still authored by the
  Assistant / an external agent / the operator: an automated author would be a paid, prompt-defined
  Maintainer, which this change does not add.

A task with no `upstream` block, or one whose gate cannot run (anything but `trusted_local`, a host
scorer, run setup — `upstream_gate.py::input_identity`'s own refusals), resolves to `off` with the
reason stated: there is nothing to check, so "propose" would be a promise the run cannot keep.

INVARIANT #1. Admission and the heavy work (Git, archives, the bought gate) run in ONE worker thread
that returns values; the MAIN task appends every row: the claim, the buffered `upstream_execution`
charges and the verdict (start < executions < finish, the order `claimed_gate_executions` reads),
the seed and the proposal, `base_advanced`. The gate leases its devices from the run's GPU pool like
an evaluation (`_try_reserve_node_resources` on the source node) and sees only them.

WHY AN ADVANCE MAY LAND OVER RUNNING WORK. A lifecycle whose evaluation started stays on the base it
was seeded on (`upstream_workspace.py::materialization_plan`, the pinned branch), and a node built
by a Developer bound at launch names its authored base on `node_created` (`base_selector`,
`base_stamp`), so the next lifecycle migrates onto the new base with the right three-way merge.
What still refuses is a queued inject or fork (`UpstreamLane._advance_prepare`).

AN INTERRUPTED GATE stays what doc 72 made it: an unresolved claim the operator abandons. This engine
never re-runs one on its own, and an auto operation the lane refused is asked again only after
`AUTO_RETRY_AFTER_S`.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger(__name__)

UPSTREAM_MODES = ("off", "propose", "auto")
LIVE_OPS = ("propose", "check", "advance")
# The lane rows that ANSWER an action id — the ACK side `UpstreamLane._retry` reads.
OP_KINDS = {
    "propose": {"upstream_proposal_started", "upstream_proposed", "upstream_proposal_failed"},
    "check": {"upstream_gate_started", "upstream_gate_finished", "upstream_gate_abandoned"},
    "advance": {"base_advanced"},
}
_STARTED = ("upstream_proposal_started", "upstream_gate_started")
# An auto operation the lane REFUSED (a queued inject blocks an advance, a source changed) is asked
# again only after this long — the refusal is cheap, but asking every loop turn would be a busy loop.
AUTO_RETRY_AFTER_S = 300.0


def upstream_mode_setting(settings) -> str:
    """THE ONE READER of `Settings.upstream_mode`. Anything unreadable is `off`."""
    value = getattr(settings, "upstream_mode", "off")
    return value if value in UPSTREAM_MODES else "off"


def resolve_upstream_mode(settings, upstream) -> tuple[str, str]:
    """`(mode, reason)` this run actually serves. Pure over the settings and the task's `upstream`
    declaration (None when it declares none)."""
    mode = upstream_mode_setting(settings)
    if mode == "off":
        return "off", "upstream_mode is off"
    if upstream is None:
        return "off", "the task declares no upstream block (scorer boundary + tests), so nothing can be checked"
    if getattr(settings, "trust_mode", "trusted_local") != "trusted_local":
        return "off", "the upstream gate runs on the host and needs trust_mode=trusted_local"
    return mode, ""


def run_settings(run_dir):
    """The run's LAUNCHED settings (`config.snapshot.json`) — the same read the API, the Assistant's
    tools and MCP build their lane from, so a gate's `input_identity` agrees across the three paths.
    None when the run has no readable snapshot."""
    from looplab.core.config import read_config_snapshot
    try:
        return read_config_snapshot(Path(run_dir) / "config.snapshot.json", refuse_unknown=True)
    except Exception:  # noqa: BLE001 — an unreadable snapshot means "no live lane", the stopped lane still answers
        return None


LIVE_QUEUE_ROWS = 50


def live_queue(events) -> dict:
    """The live lane's queue as a reader sees it (API, UI, the Assistant): the last
    `LIVE_QUEUE_ROWS` requests in order, each with its receipt once the engine settled it, and how
    many still wait. Pure over the log; the fold's cursor rule (`_advance_request_cursor`) is what
    pairs a receipt with its request, so this pairs by the receipt's own `idx`."""
    requests = [e for e in events if e.type == "lane_op_requested"]
    done = {}
    for e in events:
        if e.type == "lane_op_done" and type(e.data.get("idx")) is int:
            done.setdefault(e.data["idx"], e)
    rows = []
    for idx, req in enumerate(requests):
        receipt = done.get(idx)
        rows.append({"idx": idx, "seq": req.seq, "op": req.data.get("op"),
                     "action_id": req.data.get("action_id"),
                     **({"proposal_id": req.data["proposal_id"]} if req.data.get("proposal_id")
                        else {"proposal_id": (req.data.get("body") or {}).get("proposal_id")}
                        if isinstance(req.data.get("body"), dict) else {}),
                     "status": "pending" if receipt is None else receipt.data.get("outcome"),
                     **({"code": receipt.data["code"]} if receipt is not None and receipt.data.get("code")
                        else {})})
    return {"pending": sum(1 for r in rows if r["status"] == "pending"),
            "total": len(rows), "rows": rows[-LIVE_QUEUE_ROWS:]}


def auto_next_op(events, seed_base) -> Optional[tuple[str, dict]]:
    """The next operation `auto` takes, or None. Pure over the log.

    The OLDEST proposal against the current base that still waits: one with no gate claim is
    CHECKED (once — a failed gate is the operator's to re-buy under a new action id), one whose
    latest gate passed and whose evidence no later claim superseded is ADVANCED. Nothing while any
    claim is unresolved (the lane would refuse it anyway). Action ids are deterministic, so a
    re-entry after a crash is the lane's own idempotent retry."""
    from looplab.engine.upstream_state import active_base
    from looplab.events.run_generation import run_generation_token
    try:
        active = active_base(events, seed_base)
    except Exception:  # noqa: BLE001 — an unreadable base is the lane's refusal to state, not ours
        return None
    rows = [e for e in events if e.type.startswith("upstream_") or e.type == "base_advanced"]
    finished = {e.data.get("action_id") for e in rows
                if e.type in ("upstream_proposed", "upstream_proposal_failed", "upstream_gate_finished")}
    abandoned = {e.data.get("claim_action_id") for e in rows if e.type == "upstream_gate_abandoned"}
    if any(e.type in _STARTED and e.data.get("action_id") not in finished | abandoned for e in rows):
        return None
    generation = run_generation_token(events)
    advanced = {e.data.get("proposal_id") for e in rows if e.type == "base_advanced"}
    for proposed in (e for e in rows if e.type == "upstream_proposed"):
        pid = proposed.data.get("proposal_id")
        if pid in advanced or proposed.data.get("expected_base_revision") != active["revision"]:
            continue
        mine = [e for e in rows if e.data.get("proposal_id") == pid and e.seq > proposed.seq]
        gates = [e for e in mine if e.type == "upstream_gate_finished"]
        if not any(e.type == "upstream_gate_started" for e in mine):
            return "check", {"expected_generation": generation, "action_id": f"auto-check-{pid}",
                             "proposal_id": pid}
        if gates and gates[-1].data.get("result", {}).get("passed") is True and not any(
                e.seq > gates[-1].seq and e.type in ("upstream_gate_started", "upstream_gate_abandoned")
                for e in mine):
            return "advance", {"expected_generation": generation, "action_id": f"auto-advance-{pid}",
                               "proposal_id": pid, "expected_base_revision": active["revision"],
                               "evidence_token": gates[-1].data.get("evidence_token")}
    return None


@dataclass
class UpstreamJob:
    """One operation in flight: where it came from (`idx` = its queue position, None for `auto`),
    which phase, and the worker's answer."""
    op: str
    body: dict
    idx: Optional[int]
    phase: str = "admit"
    thread: Optional[threading.Thread] = None
    out: object = None
    error: Optional[BaseException] = None
    ctx: object = None
    charges: list = field(default_factory=list)

    @property
    def done(self) -> bool:
        return self.thread is not None and not self.thread.is_alive()


class UpstreamServe:
    """The engine's ONE in-flight upstream operation (`Engine._upstream_serve`), and the per-process
    answers it caches: the resolved mode and the authored-base stamp."""

    def __init__(self) -> None:
        self.job: Optional[UpstreamJob] = None
        self.armed: Optional[dict] = None
        self.refused_auto: dict = {}


def _arm(engine) -> dict:
    serve = engine._upstream_serve
    if serve.armed is None:
        spec = getattr(engine, "_repo_spec", None) or {}
        settings = run_settings(engine.run_dir) if spec.get("upstream") is not None else None
        mode, reason = (resolve_upstream_mode(settings, spec["upstream"]) if settings is not None
                        else ("off", "no upstream block or no readable config snapshot"))
        serve.armed = {"mode": mode, "reason": reason, "settings": settings,
                       "stamp": spec.get("effective_seed_base") if mode != "off" else None}
    return serve.armed


def base_stamp(engine) -> Optional[dict]:
    """The base this engine's Developers build on — the launch base (`repo_spec()
    ["effective_seed_base"]`) until a LIVE advance moves it — or None when the live lane is off
    (every other run's `node_created` keeps its shape). A node's own Developer call names the base it
    actually authored on (`take_authored`), which wins over this at `_emit_node_created`."""
    serve = getattr(engine, "_upstream_serve", None)
    if serve is None:
        return None
    try:
        return _arm(engine)["stamp"]
    except Exception:  # noqa: BLE001 — a stamp nobody can compute is no stamp; materialization falls back to the creation prefix
        return None


_AUTHORED = threading.local()
_NO_BASE = object()


def note_authored(base) -> None:
    """Record, for THIS thread, the base the Developer call it just made authored on."""
    _AUTHORED.base = base


def take_authored():
    """The base this thread's last Developer call authored on, consumed (`_NO_BASE` when none)."""
    base = getattr(_AUTHORED, "base", _NO_BASE)
    _AUTHORED.base = _NO_BASE
    return base


def _authoring_owners(developer):
    """Every object in a Developer's wrapper chain that OWNS an authored base — the walk
    `node_build.py::_reset_developer_footprint` makes, for the same `__getattr__`-proxy reason."""
    pending, seen = [developer], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if "authored_base" in getattr(current, "__dict__", {}) and callable(
                getattr(type(current), "rebind_base", None)):
            yield current
        for attr in ("inner", "developer", "fallback", "base"):
            child = getattr(current, attr, None) if attr in getattr(current, "__dict__", {}) else None
            if child is not None and child is not current:
                pending.append(child)


def sync_developer_base(engine, developer, pinned=None):
    """Rebind a Developer whose base is not the one this call must author on, and return that base
    (None when no owner in its chain carries one). The target is the engine's current base
    (`base_stamp`), or `pinned` — a REPAIR's lifecycle base (`lifecycle_base`), because the workdir
    it repairs was seeded from it. Under the instance's call lock (`node_build.py::_run_developer`),
    so a call in flight finishes on the base it started with."""
    target = pinned if pinned is not None else base_stamp(engine)
    authored = None
    for owner in _authoring_owners(developer):
        if target is not None and owner.authored_base != target:
            try:
                owner.rebind_base() if pinned is None else owner.rebind_base(dict(pinned))
            except Exception as exc:  # noqa: BLE001 — a failed rebind keeps the old base; the stamp below says which
                _LOG.warning("upstream: Developer rebind failed (%s); it keeps its base", exc)
        authored = owner.authored_base if authored is None else authored
    return authored


def lifecycle_base(engine, node) -> Optional[dict]:
    """The base node `node`'s CURRENT lifecycle was seeded on (its binding `workspace_seeded`
    selection), while this engine serves the live lane; None otherwise, or before it was seeded."""
    if base_stamp(engine) is None or node is None:
        return None
    from looplab.events.replay import event_generation_binds
    keys = ("run_dir", "event_seq", "digest")
    found = None
    for e in engine.store.read_all():
        # The same two rows `upstream_workspace.py::materialization_plan` reads the lifecycle's
        # origin from, bound by the ONE generation rule: the seed, then a migration that re-based
        # the pending overlay before its evaluation started.
        if (e.type not in ("workspace_seeded", "node_overlay_rebased")
                or e.data.get("node_id") != node.id
                or not event_generation_binds(e.data, node.attempt)):
            continue
        selection = ((e.data.get("base_revision") or {}).get("selection")
                     if e.type == "workspace_seeded" else e.data.get("selector"))
        if isinstance(selection, dict) and all(k in selection for k in keys):
            found = {k: selection[k] for k in keys}
    return found


def _lane(engine, settings):
    from looplab.engine.upstream import UpstreamLane
    return UpstreamLane(engine.run_dir, engine.task, settings)


def _admit(lane, op, body):
    """The worker's first phase: the lane's own refusals, the ACK of a settled action, or the work's
    context. Appends nothing."""
    from looplab.core.errors import UpstreamRefusal
    if lane.task.upstream is None:
        raise UpstreamRefusal("upstream_disabled", "The task declares no upstream block")
    events = lane._current(body.get("expected_generation"))
    previous = lane._retry(events, body, OP_KINDS[op])
    if previous is not None:
        if previous.type in _STARTED:
            raise UpstreamRefusal("upstream_claim_unresolved",
                                  "This action's claim has no completion; abandon it explicitly")
        return "ack", lane.receipt(previous)
    if op == "propose":
        return "ctx", lane._propose_admit(events, body)
    if op == "check":
        return "ctx", lane._check_admit(events, body)
    return "ctx", lane._advance_prepare(events, body, in_engine=True)


def _gate(engine, lane, job: UpstreamJob) -> dict:
    """The worker's check phase: lease the source node's devices like an evaluation, run the bought
    gate with charges BUFFERED on the job, hand the devices back."""
    from looplab.engine.upstream_gate import execute_gate
    ctx = job.ctx
    reservation = None
    try:
        while getattr(engine, "_try_reserve_node_resources", None) is not None:
            epoch = engine._gpu_pool_epoch()
            reservation = engine._try_reserve_node_resources(ctx["node"])
            if reservation is not None:
                break
            engine._wait_for_gpu_change(epoch)
        env = {}
        if reservation is not None:
            full = engine._resource_eval_env(reservation) or {}
            if "CUDA_VISIBLE_DEVICES" in full:
                env["CUDA_VISIBLE_DEVICES"] = full["CUDA_VISIBLE_DEVICES"]
        try:
            return execute_gate(lane.rd, lane.task, lane.settings, ctx["node"], ctx["proposal"],
                                ctx["manifest"], job.body["action_id"], job.charges.append,
                                extra_env=env)
        except Exception as exc:  # noqa: BLE001 — a bought check settles failed, preserving its buffered charges
            return lane._check_failed_result(ctx, exc, list(job.charges))
    finally:
        if reservation is not None:
            engine._release_gpus(reservation.get("gpu_ids"))


def _spawn(job: UpstreamJob, fn) -> None:
    def _work():
        try:
            job.out = fn()
        except BaseException as exc:  # noqa: BLE001 — the worker reports; the MAIN task decides what it means
            job.error = exc
    job.thread = threading.Thread(target=_work, name=f"looplab-upstream:{job.op}", daemon=True)
    job.thread.start()


def _start(engine, lane, job: UpstreamJob) -> None:
    job.phase = "admit"
    _spawn(job, lambda: _admit(lane, job.op, job.body))


def _code(exc) -> str:
    return str(getattr(exc, "code", None) or type(exc).__name__)[:80]


async def _settle(engine, lane, job: UpstreamJob) -> bool:
    """The MAIN task's half of one finished phase. True when it appended."""
    store = engine.store
    receipt: Optional[dict] = None
    async with engine._write_lock:
        if job.phase == "admit":
            if job.error is not None:
                receipt = {"outcome": "refused", "code": _code(job.error)}
            elif job.out[0] == "ack":
                receipt = {"outcome": "succeeded", "seq": job.out[1].get("seq")}
            elif job.op == "advance":
                from looplab.engine.upstream_state import active_base
                prepared = job.out[1]
                current = active_base(store.read_all(), engine._repo_spec.get("seed_base"))["revision"]
                if current != prepared["proposal"]["expected_base_revision"]:
                    receipt = {"outcome": "refused", "code": "upstream_base_conflict"}
                else:
                    receipt = {"outcome": "succeeded", "seq": lane._advance_commit(prepared, store.append).seq}
                    # The Developers build on the promoted base from their NEXT call
                    # (`sync_developer_base`), and a node built by one that has not rebound yet
                    # names its own base on `node_created`.
                    selector = prepared["proposal"]["selector"]
                    _arm(engine)["stamp"] = {k: selector[k] for k in ("run_dir", "event_seq", "digest")}
            else:
                job.ctx = job.out[1]
                if job.op == "propose":
                    lane._propose_started(job.ctx, store.append)
                    job.phase, job.out = "work", None
                    _spawn(job, lambda: lane._propose_build(job.ctx))
                else:
                    lane._check_started(job.ctx, store.append)
                    job.phase, job.out = "work", None
                    _spawn(job, lambda: _gate(engine, lane, job))
                return True
        elif job.op == "propose":
            if job.error is not None:
                lane._propose_failed(job.ctx, job.error, store.append)
                receipt = {"outcome": "failed", "code": _code(job.error)}
            else:
                seed = lane._propose_seed(job.out, store.append)
                row = lane._propose_proposed(job.ctx, job.out, seed.seq, store.append)
                receipt = {"outcome": "succeeded", "seq": row.seq}
        else:                                   # the check's verdict, after its buffered charges
            result = job.out if job.error is None else lane._check_failed_result(
                job.ctx, job.error, list(job.charges))
            for row in job.charges:
                lane._check_execution(job.ctx, row, store.append)
            finished = lane._check_finished(job.ctx, result, store.append)
            receipt = {"outcome": "succeeded" if result.get("passed") else "failed", "seq": finished.seq}
        if job.idx is not None:
            done = {"idx": job.idx, "op": job.op, "action_id": job.body.get("action_id"),
                    "outcome": receipt["outcome"]}
            if receipt.get("code"):
                done["code"] = receipt["code"]
            if receipt.get("seq") is not None:
                done["seq"] = receipt["seq"]
            store.append("lane_op_done", done)
        elif receipt.get("outcome") == "refused":
            engine._upstream_serve.refused_auto[(job.op, job.body.get("action_id"))] = time.monotonic()
    if receipt.get("outcome") == "refused":
        _LOG.info("upstream %s %s refused: %s", job.op, job.body.get("action_id"), receipt.get("code"))
    engine._upstream_serve.job = None
    return True


def _queued_body(lane, request: dict) -> dict:
    """A queued request's exact body: inline for check/advance, the retained request file for a
    propose (verified against its hash, as the stopped lane's exact recovery does)."""
    import json

    from looplab.core.errors import UpstreamRefusal
    from looplab.core.node_evidence import read_bounded_regular_file
    from looplab.engine.upstream_state import digest
    from looplab.engine.upstream_workspace import owned_path
    if request.get("op") != "propose":
        return dict(request.get("body") or {})
    raw = read_bounded_regular_file(owned_path(lane.rd, str(request.get("request_path") or "")),
                                    2 * 1024 * 1024 + 1)
    try:
        body = json.loads(raw) if raw is not None and len(raw) <= 2 * 1024 * 1024 else None
    except ValueError:
        body = None
    if not isinstance(body, dict) or digest(body) != request.get("request_hash"):
        raise UpstreamRefusal("upstream_request_unavailable", "The queued proposal's retained request changed")
    return body


async def serve_upstream_requests(engine, state) -> bool:
    """One loop turn's look at the live upstream lane. True when it appended (the caller re-folds)."""
    serve = getattr(engine, "_upstream_serve", None)
    if serve is None:
        return False
    job = serve.job
    if job is not None:
        if not job.done:
            return False
        return await _settle(engine, _lane(engine, _arm(engine)["settings"]), job)
    requests = getattr(state, "lane_op_requests", None) or []
    done = int(getattr(state, "lane_ops_done", 0) or 0)
    if not requests and getattr(engine, "_repo_spec", {}).get("upstream") is None:
        return False
    armed = _arm(engine)
    if state.halted:
        return False
    if done < len(requests):
        request = requests[done]
        if armed["mode"] == "off":
            async with engine._write_lock:
                engine.store.append("lane_op_done", {
                    "idx": done, "op": request.get("op"), "action_id": request.get("action_id"),
                    "outcome": "refused", "code": "upstream_mode_off"})
            return True
        lane = _lane(engine, armed["settings"])
        try:
            body = _queued_body(lane, request)
        except Exception as exc:  # noqa: BLE001 — an unreadable queued request is refused on its receipt
            async with engine._write_lock:
                engine.store.append("lane_op_done", {
                    "idx": done, "op": request.get("op"), "action_id": request.get("action_id"),
                    "outcome": "refused", "code": _code(exc)})
            return True
        serve.job = UpstreamJob(op=str(request.get("op")), body=body, idx=done)
        _start(engine, lane, serve.job)
        return False
    if armed["mode"] != "auto":
        return False
    events = engine.store.read_all()
    nxt = auto_next_op(events, engine._repo_spec.get("seed_base"))
    if nxt is None:
        return False
    op, body = nxt
    refused_at = serve.refused_auto.get((op, body["action_id"]))
    if refused_at is not None and time.monotonic() - refused_at < AUTO_RETRY_AFTER_S:
        return False
    serve.job = UpstreamJob(op=op, body=body, idx=None)
    _start(engine, _lane(engine, armed["settings"]), serve.job)
    return False
