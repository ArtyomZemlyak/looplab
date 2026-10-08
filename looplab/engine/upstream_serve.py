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
  re-entry is the lane's own idempotent retry, invariant #3). With nothing left to check or advance,
  the AUTOMATED AUTHOR (`engine/upstream_author.py`, `Settings.upstream_author`) drafts the next
  proposal itself — a repair's fix, then the champion's capability — and the draft continues as an
  ordinary `propose` job. The Assistant / an external agent / the operator still propose as before.

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

import contextvars
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


def hint_receipt_sink(engine):
    """The DIAGNOSTIC receipt a Developer session's own worker thread appends when it hears a hint
    (`engine/upstream_hints.py`, doc 73 §4.2 G1) — invariant #1 admits diagnostics from any thread."""
    def _sink(row: dict) -> None:
        from looplab.events.types import EV_UPSTREAM_HINT_DELIVERED
        engine.store.append(EV_UPSTREAM_HINT_DELIVERED, {
            "hint_id": row["hint_id"], "session": row["session"],
            **({"node_id": row["node_id"]} if "node_id" in row else {})})
    return _sink


def _issue_hint(engine, store, proposal: dict, advanced) -> None:
    """`upstream_hint_issued` for one live advance, then post it to the Developer sessions at work
    (doc 73 §4.2 G1). MAIN task, under the write lock; once per proposal."""
    from looplab.engine.upstream_hints import hint_id_for, hint_text
    from looplab.events.types import EV_UPSTREAM_HINT_ISSUED
    hint_id = hint_id_for(proposal["proposal_id"])
    board = getattr(engine, "_upstream_hints", None)
    sessions = board.open_sessions() if board is not None else []
    kind = "fix" if proposal.get("repair_only") is True else "capability"
    text = hint_text(kind=kind, source_node_id=proposal.get("source_node_id"),
                     summary=proposal.get("summary", ""), flag=proposal.get("flag"),
                     paths=proposal.get("capability_paths") or [])
    store.append(EV_UPSTREAM_HINT_ISSUED, {
        "hint_id": hint_id, "proposal_id": proposal["proposal_id"], "advance_seq": advanced.seq,
        "kind": kind, "source_node_id": proposal.get("source_node_id"), "text": text,
        "sessions": sessions[:32]})
    if board is not None:
        board.post({"hint_id": hint_id, "text": text})


def _unhinted_advance(events):
    """`(base_advanced, upstream_proposed data)` of a LIVE advance whose hint row is missing — the
    engine died between the two appends — or None."""
    issued = {e.data.get("proposal_id") for e in events if e.type == "upstream_hint_issued"}
    for e in events:
        if (e.type == "base_advanced" and e.data.get("in_engine") is True
                and e.data.get("proposal_id") not in issued):
            proposal = next((p.data for p in events if p.type == "upstream_proposed"
                             and p.data.get("proposal_id") == e.data.get("proposal_id")), None)
            if proposal is not None:
                return e, proposal
    return None


def advances_per_hour(settings) -> int:
    """THE ONE READER of `Settings.upstream_advances_per_hour` (doc 73 §4.2 G3): automatic advances a
    live engine may make in any rolling hour; 0 (or anything unreadable) = no cap."""
    value = getattr(settings, "upstream_advances_per_hour", 0)
    return value if type(value) is int and value >= 0 else 0


def auto_advances_in_window(events, now: float, window_s: float = 3600.0) -> int:
    """The engine's OWN automatic advances (`auto-advance-*`, `in_engine`) appended in the last
    `window_s` seconds — an operator's advance never counts and is never held."""
    return sum(1 for e in events if e.type == "base_advanced" and e.data.get("in_engine") is True
               and str(e.data.get("action_id") or "").startswith("auto-advance-")
               and float(e.ts or 0.0) >= now - window_s)


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


def live_queue(events, cursor: Optional[int] = None) -> dict:
    """The live lane's queue as a reader sees it (API, UI, the Assistant): the last
    `LIVE_QUEUE_ROWS` requests in order, each with its receipt once the engine settled it, and how
    many still wait. WAITING is the fold's own answer — `RunState.lane_ops_done`, the cursor
    `_advance_request_cursor` moves, which a receipt advances THROUGH its position — so a request
    below the cursor whose own receipt is missing reads `settled`, never `pending` forever.
    `cursor` is that fold's value when the caller has it; folded here otherwise."""
    if cursor is None:
        from looplab.engine.shared import engine_fold as fold
        cursor = int(fold(events).lane_ops_done or 0)
    requests = [e for e in events if e.type == "lane_op_requested"]
    done = {}
    for e in events:
        if e.type == "lane_op_done" and type(e.data.get("idx")) is int:
            done.setdefault(e.data["idx"], e)
    rows = []
    for idx, req in enumerate(requests):
        receipt = done.get(idx)
        status = (receipt.data.get("outcome") if receipt is not None
                  else "settled" if idx < cursor else "pending")
        rows.append({"idx": idx, "seq": req.seq, "op": req.data.get("op"),
                     "action_id": req.data.get("action_id"),
                     **({"proposal_id": req.data["proposal_id"]} if req.data.get("proposal_id")
                        else {"proposal_id": (req.data.get("body") or {}).get("proposal_id")}
                        if isinstance(req.data.get("body"), dict) else {}),
                     "status": status,
                     **({"code": receipt.data["code"]} if receipt is not None and receipt.data.get("code")
                        else {})})
    return {"pending": max(0, len(rows) - min(cursor, len(rows))),
            "total": len(rows), "rows": rows[-LIVE_QUEUE_ROWS:]}


LIVE_AUTHORED_ROWS = 20


def upstream_live_view(run_dir, events, *, cursor: Optional[int] = None) -> Optional[dict]:
    """What the UI shows of the live lane (`serve/appstate.py`'s state payload, `upstream_live`): the
    mode this run serves and why, its queue with receipts, and the automated author's last rows.
    None for a run that declares no upstream block and queued nothing — the payload keeps its shape.

    THE MODE IS THE ENGINE'S, not the snapshot's: an engine records what it armed with on the
    diagnostic `lane_armed` row (`serve_upstream_requests`), and it keeps serving that mode until it
    restarts — a `PUT /config` edit of the snapshot changes nothing live. So the latest such row in
    `events` (a historical prefix reads the mode in force then) is the answer; only a run whose
    engine never armed falls back to its launched settings and the `run_started` declaration, marked
    `configured: true`."""
    started = next((e for e in events if e.type == "run_started"), None)
    upstream = started.data.get("upstream") if started is not None else None
    upstream = upstream if isinstance(upstream, dict) and upstream else None
    if upstream is None and not any(e.type == "lane_op_requested" for e in events):
        return None
    armed = next((e for e in reversed(events) if e.type == "lane_armed"), None)
    if armed is not None:
        mode = armed.data.get("mode") if armed.data.get("mode") in UPSTREAM_MODES else "off"
        reason, author, configured = str(armed.data.get("reason") or ""), armed.data.get("author") is True, False
    else:
        from looplab.engine.upstream_author import upstream_author_setting
        settings = run_settings(run_dir)
        mode, reason = (resolve_upstream_mode(settings, upstream) if settings is not None
                        else ("off", "no readable config snapshot"))
        author, configured = mode == "auto" and upstream_author_setting(settings), True
    from looplab.engine.upstream_author import author_spent_usd
    authored = [{"seq": e.seq, "action_id": e.data.get("action_id"), "track": e.data.get("track"),
                 "source_node_id": e.data.get("source_node_id"), "outcome": e.data.get("outcome")}
                for e in events if e.type == "lane_authored"]
    # doc 73 §4.2 G2-G4: the kill switch as the log last set it, the caps that held a step back,
    # and what the author has spent.
    switch = next((e for e in reversed(events) if e.type == "upstream_auto_set"
                   and type(e.data.get("enabled")) is bool), None)
    held = [{"seq": e.seq, "op": e.data.get("op"), "reason": e.data.get("reason"),
             **({"proposal_id": e.data["proposal_id"]} if e.data.get("proposal_id") else {})}
            for e in events if e.type == "lane_held"]
    return {"mode": mode, "reason": reason, "author": author, "configured": configured,
            "queue": live_queue(events, cursor), "authored": authored[-LIVE_AUTHORED_ROWS:],
            "authored_total": len(authored),
            "auto_paused": switch is not None and switch.data["enabled"] is False,
            "held": held[-LIVE_AUTHORED_ROWS:], "author_spent_usd": round(author_spent_usd(events), 6)}


def upstream_board_lines(run_dir, state, events) -> list[str]:
    """`looplab inspect`'s upstream lines: the folded board (`core/upstream_board.py::board_lines`),
    then the LIVE lane as `upstream_live_view` reads it — the mode it serves, the kill switch, the
    queue, the author's drafts and spend, the steps a cap held back. [] when the run never touched the
    lane."""
    from looplab.core.upstream_board import board_lines
    out = list(board_lines(state))
    live = upstream_live_view(run_dir, events, cursor=getattr(state, "lane_ops_done", None))
    if live is None:
        return out
    head = f"upstream lane: {live['mode']}" + (" (as configured; no engine armed it yet)"
                                               if live["configured"] else "")
    if live["reason"]:
        head += f" — {live['reason']}"
    if live["auto_paused"]:
        head += " — automation OFF (kill switch; `looplab upstream-auto RUN --on` resumes it)"
    out.append(head)
    queue = live["queue"]
    if queue["total"]:
        out.append(f"  queue: {queue['pending']} waiting of {queue['total']}")
    if live["author"] or live["authored_total"]:
        counts: dict = {}
        for row in live["authored"]:
            counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
        out.append(f"  author: {live['authored_total']} row(s), ${live['author_spent_usd']:.4f} spent"
                   + (" — last: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
                      if counts else ""))
    for row in live["held"][-3:]:
        out.append(f"  held: {row['op']} {row.get('reason') or ''}"
                   + (f" ({row['proposal_id']})" if row.get("proposal_id") else ""))
    return out


def claims_unresolved(events) -> bool:
    """A lane claim (a proposal's or a gate's) has no completion and was not abandoned — the lane
    refuses every new operation until the operator resolves it, so `auto` asks none and the author
    pays for no draft the lane would refuse."""
    rows = [e for e in events if e.type.startswith("upstream_")]
    finished = {e.data.get("action_id") for e in rows
                if e.type in ("upstream_proposed", "upstream_proposal_failed", "upstream_gate_finished")}
    abandoned = {e.data.get("claim_action_id") for e in rows if e.type == "upstream_gate_abandoned"}
    return any(e.type in _STARTED and e.data.get("action_id") not in finished | abandoned for e in rows)


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
    if claims_unresolved(events):
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
    # A propose the automated author drafted: `{action_id, track, source_node_id, hunk_hashes}`, so a
    # lane refusal of it is recorded on its own `lane_authored` row (`refused`).
    authored: Optional[dict] = None

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
        # Source lifecycles the author found nothing to nominate in (`author_next(skipped=)`).
        self.author_skipped: dict = {}
        # The mode this engine armed with, recorded once on the diagnostic `lane_armed` row.
        self.announced = False


def _arm(engine) -> dict:
    serve = engine._upstream_serve
    if serve.armed is None:
        spec = getattr(engine, "_repo_spec", None) or {}
        settings = run_settings(engine.run_dir) if spec.get("upstream") is not None else None
        mode, reason = (resolve_upstream_mode(settings, spec["upstream"]) if settings is not None
                        else ("off", "no upstream block or no readable config snapshot"))
        launch = spec.get("effective_seed_base") if mode != "off" else None
        # `stamp` is the LAUNCH base and never moves: it is what a Developer that cannot rebind
        # authored on. `current` is the base the engine's live advances moved to — what the
        # Developers that CAN rebind are moved onto before their next call.
        serve.armed = {"mode": mode, "reason": reason, "settings": settings,
                       "stamp": launch, "current": launch}
    return serve.armed


def base_stamp(engine) -> Optional[dict]:
    """The LAUNCH base (`repo_spec()["effective_seed_base"]`) while this engine serves the live lane,
    None when the lane is off — so every other run's `node_created` keeps its shape. It never moves:
    it is what a Developer that cannot rebind (a CLI agent's worktree, `sync_developer_base`) wrote
    its code on."""
    serve = getattr(engine, "_upstream_serve", None)
    if serve is None:
        return None
    try:
        return _arm(engine)["stamp"]
    except Exception:  # noqa: BLE001 — a stamp nobody can compute is no stamp; materialization falls back to the creation prefix
        return None


def current_base(engine) -> Optional[dict]:
    """The base the live lane advanced to (the launch base until an in-engine advance), None when
    the lane is off — the target `sync_developer_base` rebinds the Developers to."""
    if base_stamp(engine) is None:
        return None
    return _arm(engine).get("current")


_CHAIN_ATTRS = ("inner", "developer", "fallback", "base")


def _developer_chain(developer):
    """Every object in a Developer's wrapper chain and which of them are LEAVES (no wrapped member
    of its own) — the walk `node_build.py::_reset_developer_footprint` makes, for the same
    `__getattr__`-proxy reason (only attributes in the instance `__dict__` are followed)."""
    pending, seen, members, leaves = [developer], set(), [], []
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        members.append(current)
        own = getattr(current, "__dict__", {})
        children = [own[a] for a in _CHAIN_ATTRS if own.get(a) is not None and own.get(a) is not current]
        if not children:
            leaves.append(current)
        pending.extend(children)
    return members, leaves


def _owns_base(member) -> bool:
    return "authored_base" in getattr(member, "__dict__", {}) and callable(
        getattr(type(member), "rebind_base", None))


def sync_developer_base(engine, developer, pinned=None):
    """The base the Developer call about to run authors on, after rebinding it — or None while the
    live lane is off (the node's row then keeps its historical shape).

    The target is the engine's current base (`current_base`), or `pinned`: a REPAIR's lifecycle base
    (`lifecycle_base`), because the workdir it repairs was seeded from it. A chain is rebound only
    when EVERY leaf owns a base (`LLMRepoDeveloper.rebind_base`); one that holds a member which
    cannot rebind (a CLI agent seeding its worktree from launch-time directories, even beside a
    rebindable fallback) is left alone whole and answers the LAUNCH base, because which member
    writes the code is not known before the call. Under the instance's call lock
    (`node_build.py::_run_developer`), so a call in flight finishes on the base it started with."""
    launch = base_stamp(engine)
    if launch is None:
        return None
    members, leaves = _developer_chain(developer)
    if not leaves or not all(_owns_base(m) for m in leaves):
        return dict(launch)
    target = pinned if pinned is not None else current_base(engine)
    owners = [m for m in members if _owns_base(m)]
    for owner in owners:
        if target is not None and owner.authored_base != target:
            try:
                owner.rebind_base() if pinned is None else owner.rebind_base(dict(pinned))
            except Exception:  # noqa: BLE001 — a failed rebind keeps the old base; the stamp below says which
                _LOG.warning("upstream: Developer rebind failed; it keeps its base", exc_info=True)
    bases = {repr(o.authored_base) for o in leaves}
    # A leaf whose rebind failed is on another base than its siblings: nothing says which one the
    # call will use, so the run's launch stamp is no better — name the leaves' base only when they
    # agree, else the target the engine asked for (the overlay is then merged from it).
    return dict(leaves[0].authored_base) if len(bases) == 1 and leaves[0].authored_base else (
        dict(target) if target is not None else dict(launch))


def lifecycle_base(engine, node) -> Optional[dict]:
    """The base node `node`'s CURRENT lifecycle was seeded on, while this engine serves the live
    lane; None otherwise, or before its current generation was seeded. The same reading
    `upstream_workspace.py::materialization_plan` pins a started lifecycle with (`seeded_basis`)."""
    if base_stamp(engine) is None or node is None:
        return None
    from looplab.engine.upstream_workspace import seeded_basis
    events = engine.store.read_all()
    created = getattr(node, "creation_event_seq", None)
    if created is None:
        return None
    selector, seeded_now = seeded_basis(node, events, created)
    return dict(selector) if seeded_now and isinstance(selector, dict) else None


def files_base(engine, node) -> Optional[dict]:
    """The base `node.files` are an overlay OF — what a node built from them with no Developer call
    (a simplification) must name: its seeded lifecycle base, else the base its own `node_created`
    named. None while the lane is off or when neither is recorded (the creation prefix then rules,
    as on every stopped-lane row)."""
    if base_stamp(engine) is None or node is None:
        return None
    seeded = lifecycle_base(engine, node)
    if seeded is not None:
        return seeded
    created = next((e for e in engine.store.read_all() if e.type == "node_created"
                    and e.seq == getattr(node, "creation_event_seq", None)), None)
    named = created.data.get("base_selector") if created is not None else None
    return dict(named) if isinstance(named, dict) else None


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


def _spawn(job: UpstreamJob, fn, *, carry_context: bool = False) -> None:
    # `carry_context`: the author's PAID calls run in the spawning task's context, so they land in its
    # trace and the run's broker lane like any other paid call (`_author_work`).
    context = contextvars.copy_context() if carry_context else None

    def _work():
        try:
            job.out = context.run(fn) if context is not None else fn()
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
    if job.op == "author":
        return await _settle_author(engine, lane, job)
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
                    advanced = lane._advance_commit(prepared, store.append)
                    _issue_hint(engine, store, prepared["proposal"], advanced)
                    receipt = {"outcome": "succeeded", "seq": advanced.seq}
                    # The Developers build on the promoted base from their NEXT call
                    # (`sync_developer_base`), and a node built by one that has not rebound yet
                    # names its own base on `node_created`.
                    selector = prepared["proposal"]["selector"]
                    _arm(engine)["current"] = {k: selector[k] for k in ("run_dir", "event_seq", "digest")}
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
            if job.authored is not None and job.op == "propose":
                # The lane refused a draft the author paid for: said on its own row, so the UI
                # does not read `drafted` as "proposed" and a re-entry does not propose it again.
                refusal = {"action_id": job.authored["action_id"], "track": job.authored["track"],
                           "source_node_id": job.authored["source_node_id"], "outcome": "refused"}
                refusal["code"] = receipt.get("code") or "refused"
                store.append("lane_authored", refusal)
    if receipt.get("outcome") == "refused":
        _LOG.info("upstream %s %s refused: %s", job.op, job.body.get("action_id"), receipt.get("code"))
    engine._upstream_serve.job = None
    return True


def _author_work(engine, pick, generation):
    """The author's worker: its two paid calls under a span (`_paid_progress`), the body back."""
    import contextlib

    from looplab.engine.upstream_author import author_draft
    # A SPAN, not a `_paid_progress` beacon: its phase is a closed word of one node's build or
    # evaluation (`events/types.py::PROGRESS_PHASES`), and authoring is neither — the cadence shape
    # `engine/value_estimate.py` takes for the same reason.
    span = getattr(engine, "_op_span", None)
    scope = (span("upstream_author", node_id=pick["node"].id, track=pick["track"])
             if callable(span) else contextlib.nullcontext())
    # THE MONEY (doc 73 §4.2 G4): what THIS thread committed across the two calls, recorded on the
    # source's `lane_authored` row whether the draft succeeded or raised.
    from looplab.core.llm_budget import thread_committed_usd_exact
    start = thread_committed_usd_exact()
    try:
        with scope:
            return author_draft(engine, pick, generation=generation)
    finally:
        try:
            pick["cost_usd"] = round(max(0.0, float(thread_committed_usd_exact() - start)), 6)
        except OverflowError:
            pick["cost_usd"] = float("inf")


async def _settle_author(engine, lane, job: UpstreamJob) -> bool:
    """The MAIN task's half of an authoring job: the `lane_authored` row, then — for a drafted
    proposal — the same job continues as the lane's `propose` (its own refusals, rows and Git work).
    A spend ceiling the worker met is the run's to stop on: raised here, after the job is cleared."""
    from looplab.core.errors import budget_stop_leaf
    pick, serve = job.ctx, engine._upstream_serve
    out = job.out if job.error is None else {"outcome": "failed", "code": _code(job.error)}
    async with engine._write_lock:
        row = {"action_id": job.body["action_id"], "track": pick["track"],
               "source_node_id": pick["node"].id, "outcome": out["outcome"]}
        row["hunk_hashes"] = sorted({r["hunk_hash"] for r in pick["rows"]})
        if out.get("reason"):
            row["reason"] = str(out["reason"])[:500]
        if out.get("code"):
            row["code"] = out["code"]
        if isinstance(pick.get("cost_usd"), float):
            row["cost_usd"] = pick["cost_usd"]
        engine.store.append("lane_authored", row)
        if out["outcome"] == "drafted":
            job.authored = {k: row[k] for k in ("action_id", "track", "source_node_id", "hunk_hashes")}
            job.op, job.body, job.ctx, job.out = "propose", out["body"], None, None
            _start(engine, lane, job)
            return True
    serve.job = None
    stop = budget_stop_leaf(job.error)
    if stop is not None:
        raise stop
    if job.error is not None:
        _LOG.warning("upstream author failed on node %s: %s", pick["node"].id, _code(job.error))
    return True


def _start_author(engine, armed, state, events) -> None:
    """Start the automated author on the next source, when it is on and one is due."""
    from looplab.engine.upstream_author import author_next, upstream_author_setting
    from looplab.events.run_generation import run_generation_token
    from looplab.engine.upstream_author import unproposed_draft
    serve = engine._upstream_serve
    if not upstream_author_setting(armed["settings"]) or claims_unresolved(events):
        return
    # A draft paid for and recorded, whose proposal the lane never claimed (a crash in between):
    # proposed again from its retained body, with no new call.
    pending = unproposed_draft(engine.run_dir, events)
    if pending is not None:
        row, body = pending
        serve.job = UpstreamJob(op="propose", body=body, idx=None, authored={
            k: row.get(k) for k in ("action_id", "track", "source_node_id", "hunk_hashes")})
        _start(engine, _lane(engine, armed["settings"]), serve.job)
        return
    pick = author_next(engine.run_dir, engine.task, state, events, skipped=serve.author_skipped)
    if pick is None:
        return
    generation = run_generation_token(events)
    serve.job = UpstreamJob(op="author", body={"action_id": pick["action_id"]}, idx=None,
                            phase="author", ctx=pick)
    _spawn(serve.job, lambda: _author_work(engine, pick, generation), carry_context=True)


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
    if not serve.announced:
        # What THIS engine serves, once per process (`upstream_live_view` reads it): diagnostic, so
        # its position keys nothing.
        from looplab.engine.upstream_author import upstream_author_setting
        serve.announced = True
        async with engine._write_lock:
            engine.store.append("lane_armed", {
                "mode": armed["mode"], "reason": armed["reason"],
                "author": armed["mode"] == "auto" and upstream_author_setting(armed["settings"])})
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
    events = engine.store.read_all()
    lost = _unhinted_advance(events) if armed["mode"] != "off" else None
    if lost is not None:
        async with engine._write_lock:
            _issue_hint(engine, engine.store, lost[1], lost[0])
        return True
    if armed["mode"] != "auto":
        return False
    # THE KILL SWITCH (`upstream_auto_set {enabled: false}`, doc 73 §4.2 G2): no author, no automatic
    # check, no automatic advance. Operator-queued operations above are still served.
    if getattr(state, "upstream_auto_paused", False):
        return False
    nxt = auto_next_op(events, engine._repo_spec.get("seed_base"))
    if nxt is None:
        from looplab.engine.upstream_author import author_spent_usd, author_usd_cap, upstream_author_setting
        cap_usd = author_usd_cap(armed["settings"])
        if (upstream_author_setting(armed["settings"]) and cap_usd
                and author_spent_usd(events) >= cap_usd):
            if not any(e.type == "lane_held" and e.data.get("op") == "author" for e in events):
                async with engine._write_lock:
                    engine.store.append("lane_held", {"op": "author", "reason": f"cost_cap:{cap_usd:g}usd"})
                return True
            return False
        _start_author(engine, armed, state, events)
        return False
    op, body = nxt
    per_hour = advances_per_hour(armed["settings"])
    if op == "advance" and per_hour and auto_advances_in_window(events, time.time()) >= per_hour:
        # THE HOURLY CAP (doc 73 §4.2 G3): the passed gate waits; said once per proposal.
        if not any(e.type == "lane_held" and e.data.get("proposal_id") == body["proposal_id"]
                   for e in events):
            async with engine._write_lock:
                engine.store.append("lane_held", {"op": "advance", "reason": f"rate_cap:{per_hour}/h",
                                                  "proposal_id": body["proposal_id"]})
            return True
        return False
    refused_at = serve.refused_auto.get((op, body["action_id"]))
    if refused_at is not None and time.monotonic() - refused_at < AUTO_RETRY_AFTER_S:
        return False
    serve.job = UpstreamJob(op=op, body=body, idx=None)
    _start(engine, _lane(engine, armed["settings"]), serve.job)
    return False
