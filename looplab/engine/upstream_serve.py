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
never re-runs one on its own. An auto operation the lane refused for a reason that can pass
(`TRANSIENT_REFUSALS`) is asked again only after `AUTO_RETRY_AFTER_S`, and the proposals behind it are
served meanwhile; one refused for a reason about the proposal itself is said once on a `lane_held`
row (`refused:<code>`) and never asked again by this lane — a failed gate is the operator's to re-buy.

THE LOOP'S EXIT (`drain_upstream_job`, after the evaluations drain): an operation whose CLAIM landed
is waited for and settled — its bought charges and verdict, or its proposal — like an adopted
evaluation; one still admitting is dropped unclaimed (a queued request stays queued for the next
engine); a draft the author paid for is recorded and proposed by the next engine from its retained
body. Nothing starts a new phase once the run halted or, for an automatic step, while the operator's
kill switch is on.
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
# The lane rows that ANSWER an action id — the ACK side `UpstreamLane._retry` reads.
OP_KINDS = {
    "propose": {"upstream_proposal_started", "upstream_proposed", "upstream_proposal_failed"},
    "check": {"upstream_gate_started", "upstream_gate_finished", "upstream_gate_abandoned"},
    "advance": {"base_advanced"},
}
_STARTED = ("upstream_proposal_started", "upstream_gate_started")
# An auto operation the lane REFUSED for a passing reason (a queued inject blocks an advance) is asked
# again only after this long — the refusal is cheap, but asking every loop turn would be a busy loop.
AUTO_RETRY_AFTER_S = 300.0
# The lane's refusals that say nothing about the PROPOSAL: the run's state moves past them (a queued
# inject is built, a claim is resolved, a concurrent append completes the log the worker read). Any
# other `UpstreamRefusal` of an automatic step — the source changed, the manifest is gone, the gate's
# evidence moved — refuses that proposal for good, so it is recorded once and not asked again.
TRANSIENT_REFUSALS = frozenset({"upstream_work_pending", "upstream_claim_unresolved",
                                "run_generation_conflict", "upstream_base_conflict",
                                "upstream_engine_running", "upstream_source_unavailable"})


def hint_receipt_sink(engine):
    """The DIAGNOSTIC receipt a Developer session's own worker thread appends when it hears a hint
    (`engine/upstream_hints.py`, doc 73 §4.2 G1) — invariant #1 admits diagnostics from any thread."""
    def _sink(row: dict) -> None:
        from looplab.events.types import EV_UPSTREAM_HINT_DELIVERED
        engine.store.append(EV_UPSTREAM_HINT_DELIVERED, {
            "hint_id": row["hint_id"], "session": row["session"],
            **({"node_id": row["node_id"]} if "node_id" in row else {}),
            # doc 73 §4.3: delivered by an external agent's own hook, not the tool loop.
            **({"channel": row["channel"]} if row.get("channel") else {})})
    return _sink


def external_hint_channel(engine) -> bool:
    """Whether a Developer session of this engine publishes the external agents' notice channel
    (doc 73 §4.3): the live lane serves (`base_stamp`) and `Settings.upstream_hint_external` is on in
    the settings it armed with. False on any other engine, so its external agents keep their bytes."""
    if base_stamp(engine) is None:
        return False
    from looplab.agents.cli_hook import external_hint_setting
    return external_hint_setting(_arm(engine).get("settings"))


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
    engine died between the two appends — or None. One pass over the log: it runs every loop turn."""
    issued, proposed, advances = set(), {}, []
    for e in events:
        if e.type == "upstream_hint_issued":
            issued.add(e.data.get("proposal_id"))
        elif e.type == "upstream_proposed":
            proposed.setdefault(e.data.get("proposal_id"), e.data)
        elif e.type == "base_advanced" and e.data.get("in_engine") is True:
            advances.append(e)
    for e in advances:
        pid = e.data.get("proposal_id")
        if pid not in issued and pid in proposed:
            return e, proposed[pid]
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


def armed_row(events):
    """The latest `lane_armed` row — what the last engine that served this run armed with — or None."""
    return next((e for e in reversed(events) if e.type == "lane_armed"), None)


def served_mode(events, settings, upstream, *, alive: bool) -> tuple[str, str]:
    """`(mode, reason)` the lane is SERVED in, for a reader deciding what to do now (the lane's read,
    its live queue, the Assistant's and MCP's `upstream_status`). A LIVE engine serves the mode it
    armed with (its `lane_armed` row) until it restarts, whatever a `PUT /config` wrote into the
    snapshot since; a stopped run — or an engine that has not armed yet — answers from the launched
    settings and the task's declaration (`resolve_upstream_mode`)."""
    if alive:
        armed = armed_row(events)
        if armed is not None:
            mode = armed.data.get("mode")
            return (mode if mode in UPSTREAM_MODES else "off"), str(armed.data.get("reason") or "")
    return resolve_upstream_mode(settings, upstream)


def auto_switched_off(events) -> bool:
    """The operator's kill switch as the log last set it (`upstream_auto_set`, doc 73 §4.2 G2) — the
    same reading `RunState.upstream_auto_paused` folds, for a reader holding only the events."""
    switch = next((e for e in reversed(events) if e.type == "upstream_auto_set"
                   and type(e.data.get("enabled")) is bool), None)
    return switch is not None and switch.data["enabled"] is False


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


def lane_queue_cursor(events) -> int:
    """`RunState.lane_ops_done` without folding the whole log: the fold's two queue handlers are the
    only ones that touch it and read nothing else, so folding just their rows is the same value."""
    from looplab.engine.shared import engine_fold as fold
    return int(fold([e for e in events if e.type in ("lane_op_requested", "lane_op_done")]).lane_ops_done or 0)


def live_queue(events, cursor: Optional[int] = None) -> dict:
    """The live lane's queue as a reader sees it (API, UI, the Assistant): the last
    `LIVE_QUEUE_ROWS` requests in order, each with its receipt once the engine settled it, and how
    many still wait. WAITING is the fold's own answer — `RunState.lane_ops_done`, the cursor
    `_advance_request_cursor` moves, which a receipt advances THROUGH its position — so a request
    below the cursor whose own receipt is missing reads `settled`, never `pending` forever.
    `cursor` is that fold's value when the caller has it; derived here otherwise (`lane_queue_cursor`)."""
    if cursor is None:
        cursor = lane_queue_cursor(events)
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
# The engine's own automatic advances whose TIMESTAMPS the cached live body carries, newest last, so
# the serve layer can count the last hour against ITS clock (`live_view_at`). Well above any hourly
# cap an operator sets (default 2); past it the count is a floor, never an invention.
LIVE_ADVANCE_TS_ROWS = 64
LIVE_ADVANCE_TS_KEY = "auto_advance_ts"


def _armed_cap(data: dict, key: str, kind):
    """One cap off the `lane_armed` row, or None when that engine did not record it (a row written
    before the caps rode on it) — unknown, never a guessed 0 that would read as "no cap"."""
    value = data.get(key)
    if kind is int:
        return value if type(value) is int and value >= 0 else None
    return float(value) if type(value) in (int, float) and 0 <= value < float("inf") else None


def upstream_live_body(run_dir, events, *, cursor: Optional[int] = None) -> Optional[dict]:
    """What the UI shows of the live lane (`serve/appstate.py`'s state payload, `upstream_live`), as
    a PURE function of `events` — the server caches it with the log's identity (`/state`, the SSE
    `state_delta` frames), so nothing in it may read the wall clock or a file the log does not pin:
    the mode this run serves and why, its queue with receipts, and the automated author's last rows.
    None for a run that declares no upstream block and queued nothing — the payload keeps its shape.

    THE MODE IS THE ENGINE'S, not the snapshot's: an engine records what it armed with on the
    diagnostic `lane_armed` row (`serve_upstream_requests`), and it keeps serving that mode until it
    restarts — a `PUT /config` edit of the snapshot changes nothing live. So the latest such row in
    `events` (a historical prefix reads the mode in force then) is the answer; only a run whose
    engine never armed falls back to its launched settings and the `run_started` declaration, marked
    `configured: true`.

    THE CAPS (doc 73 §4.3) are the ones that engine ARMED with, off the same row: `author_usd_cap`
    and `advances_per_hour` (0 = no cap; None on a row written before they rode on it) beside what
    they bound, `author_spent_usd` and the engine's own automatic advances. Those are carried as
    their raw timestamps (`auto_advance_ts`): "the last hour" is a fact of the moment a reader asks,
    so `live_view_at` counts it at SERVE time, exactly as `engine_running` is stamped. Each held
    step says whether it still `waiting`; `switch` is the row that last set the kill switch."""
    started = next((e for e in events if e.type == "run_started"), None)
    upstream = started.data.get("upstream") if started is not None else None
    upstream = upstream if isinstance(upstream, dict) and upstream else None
    if upstream is None and not any(e.type == "lane_op_requested" for e in events):
        return None
    armed = armed_row(events)
    if armed is not None:
        mode = armed.data.get("mode") if armed.data.get("mode") in UPSTREAM_MODES else "off"
        reason, author, configured = str(armed.data.get("reason") or ""), armed.data.get("author") is True, False
        cap_usd = _armed_cap(armed.data, "author_usd_cap", float)
        per_hour = _armed_cap(armed.data, "advances_per_hour", int)
    else:
        # No engine armed yet: what the launched settings SAY (`configured: true`), caps included.
        from looplab.engine.upstream_author import author_usd_cap, upstream_author_setting
        settings = run_settings(run_dir)
        mode, reason = (resolve_upstream_mode(settings, upstream) if settings is not None
                        else ("off", "no readable config snapshot"))
        author, configured = mode == "auto" and upstream_author_setting(settings), True
        cap_usd = author_usd_cap(settings) if settings is not None else 0.0
        per_hour = advances_per_hour(settings) if settings is not None else 0
    from looplab.engine.upstream_author import author_spent_usd
    authored = [{"seq": e.seq, "action_id": e.data.get("action_id"), "track": e.data.get("track"),
                 "source_node_id": e.data.get("source_node_id"), "outcome": e.data.get("outcome"),
                 # doc 73 §4.3: drafted from a source the base had moved past (merged onto it).
                 **({"rebased": True} if e.data.get("rebased_from") else {}),
                 **({"code": e.data["code"]} if isinstance(e.data.get("code"), str) else {})}
                for e in events if e.type == "lane_authored"]
    # doc 73 §4.2 G2-G4: the kill switch as the log last set it, the caps that held a step back,
    # and what the author has spent. Whether an engine serves the run NOW is not a fact of the log
    # (this body caches with it): the UI reads it beside, off the payload's `engine_running`.
    switch = next((e for e in reversed(events) if e.type == "upstream_auto_set"
                   and type(e.data.get("enabled")) is bool), None)
    advances = [e for e in events if e.type == "base_advanced"]
    advanced = {e.data.get("proposal_id") for e in advances}
    last_advance_seq = max((e.seq for e in advances), default=None)
    refused = refused_for_good(events)
    spent = author_spent_usd(events)

    def _waiting(e) -> bool:
        # A held advance waits until its proposal advanced; the author until its budget grows. A
        # proposal the lane refused FOR GOOD (`refused:<code>`) waits for nothing: it is passed
        # over — and so does an earlier cap row of a proposal refused since.
        op, pid = e.data.get("op"), e.data.get("proposal_id")
        if str(e.data.get("reason") or "").startswith(REFUSED_PREFIX) or (op, pid) in refused:
            return False
        if op == "advance":
            # SUPERSEDED: `auto_next_op` nominated it against the base current at the hold, and a
            # base revision names its advance's seq (`upstream_state.py::active_base`), so ANY later
            # advance — another proposal's, or the operator's — moved the base past this proposal's
            # `expected_base_revision`; `auto` never asks it again, so it waits for nothing.
            return pid not in advanced and (last_advance_seq is None or last_advance_seq < e.seq)
        return bool(cap_usd) and spent >= cap_usd
    held = [{"seq": e.seq, "op": e.data.get("op"), "reason": e.data.get("reason"),
             **({"proposal_id": e.data["proposal_id"]} if e.data.get("proposal_id") else {}),
             "waiting": _waiting(e)}
            for e in events if e.type == "lane_held"]
    auto_ts = [float(e.ts or 0.0) for e in advances if e.data.get("in_engine") is True
               and str(e.data.get("action_id") or "").startswith("auto-advance-")]
    return {"mode": mode, "reason": reason, "author": author, "configured": configured,
            "queue": live_queue(events, cursor), "authored": authored[-LIVE_AUTHORED_ROWS:],
            "authored_total": len(authored),
            "auto_paused": auto_switched_off(events),
            "switch": ({"seq": switch.seq, "enabled": switch.data["enabled"],
                        **({"reason": str(switch.data["reason"])[:300]}
                           if isinstance(switch.data.get("reason"), str) else {})}
                       if switch is not None else None),
            "held": held[-LIVE_AUTHORED_ROWS:], "author_spent_usd": round(spent, 6),
            "author_usd_cap": cap_usd, "advances_per_hour": per_hour,
            LIVE_ADVANCE_TS_KEY: auto_ts[-LIVE_ADVANCE_TS_ROWS:]}


def live_view_at(body: Optional[dict], now: Optional[float]) -> Optional[dict]:
    """`upstream_live_body` as served at `now`: a COPY whose raw advance timestamps are replaced by
    `advances_last_hour`, the engine's own automatic advances in the hour before `now` (the same
    window `auto_advances_in_window` holds the engine to). `now=None` — a historical `upto_seq`
    read, which is neither then nor now — answers None, and the UI says nothing about the hour."""
    if body is None:
        return None
    out = {key: value for key, value in body.items() if key != LIVE_ADVANCE_TS_KEY}
    stamps = body.get(LIVE_ADVANCE_TS_KEY)
    out["advances_last_hour"] = (None if now is None or not isinstance(stamps, list)
                                 else sum(1 for ts in stamps if ts >= now - 3600.0))
    return out


def upstream_live_view(run_dir, events, *, cursor: Optional[int] = None,
                       now: Optional[float] = None) -> Optional[dict]:
    """The live lane at `now` (wall clock by default) — `upstream_live_body` then `live_view_at`,
    for a reader that asks once and caches nothing (`looplab inspect`). The server builds the body
    with the cached payload and stamps the hour per serve instead
    (`serve/appstate.py::state_payload`)."""
    return live_view_at(upstream_live_body(run_dir, events, cursor=cursor),
                        time.time() if now is None else now)


def claims_unresolved(events) -> bool:
    """A lane claim (a proposal's or a gate's) has no completion and was not abandoned — the lane
    refuses every new operation until the operator resolves it, so `auto` asks none and the author
    pays for no draft the lane would refuse."""
    rows = [e for e in events if e.type.startswith("upstream_")]
    finished = {e.data.get("action_id") for e in rows
                if e.type in ("upstream_proposed", "upstream_proposal_failed", "upstream_gate_finished")}
    abandoned = {e.data.get("claim_action_id") for e in rows if e.type == "upstream_gate_abandoned"}
    return any(e.type in _STARTED and e.data.get("action_id") not in finished | abandoned for e in rows)


REFUSED_PREFIX = "refused:"


def refused_for_good(events) -> set:
    """`(op, proposal_id)` pairs whose AUTOMATIC step the lane refused for a reason about the
    proposal itself — the `lane_held {reason: "refused:<code>"}` rows `_settle` records once."""
    return {(e.data.get("op"), e.data.get("proposal_id")) for e in events
            if e.type == "lane_held" and str(e.data.get("reason") or "").startswith(REFUSED_PREFIX)}


def auto_next_op(events, seed_base, *, active=None, skip=()) -> Optional[tuple[str, dict]]:
    """The next operation `auto` takes, or None. Pure over the log (and `skip`).

    The OLDEST proposal against the current base that still waits: one with no gate claim is
    CHECKED (once — a failed gate is the operator's to re-buy under a new action id), one whose
    latest gate passed and whose evidence no later claim superseded is ADVANCED. Nothing while any
    claim is unresolved (the lane would refuse it anyway). Action ids are deterministic, so a
    re-entry after a crash is the lane's own idempotent retry.

    A proposal whose step the lane refused for good (`refused_for_good`) is passed over, and so is
    one whose action id is in `skip` — the caller's recent PASSING refusals — so a proposal the lane
    cannot take never stands in front of the ones behind it (critic 2026-10-08: oldest-first with a
    refusal remembered only in memory held the whole lane, and the author behind it, for good).
    `active` is the caller's `active_base` of exactly these events (`_active` caches it)."""
    from looplab.engine.upstream_state import active_base
    from looplab.events.run_generation import run_generation_token
    if active is None:
        try:
            active = active_base(events, seed_base)
        except Exception:  # noqa: BLE001 — an unreadable base is the lane's refusal to state, not ours
            return None
    rows = [e for e in events if e.type.startswith("upstream_") or e.type == "base_advanced"]
    if claims_unresolved(events):
        return None
    generation = run_generation_token(events)
    advanced = {e.data.get("proposal_id") for e in rows if e.type == "base_advanced"}
    refused, skip = refused_for_good(events), set(skip)
    for proposed in (e for e in rows if e.type == "upstream_proposed"):
        pid = proposed.data.get("proposal_id")
        if pid in advanced or proposed.data.get("expected_base_revision") != active["revision"]:
            continue
        mine = [e for e in rows if e.data.get("proposal_id") == pid and e.seq > proposed.seq]
        gates = [e for e in mine if e.type == "upstream_gate_finished"]
        if not any(e.type == "upstream_gate_started" for e in mine):
            if ("check", pid) in refused or f"auto-check-{pid}" in skip:
                continue
            return "check", {"expected_generation": generation, "action_id": f"auto-check-{pid}",
                             "proposal_id": pid}
        if gates and gates[-1].data.get("result", {}).get("passed") is True and not any(
                e.seq > gates[-1].seq and e.type in ("upstream_gate_started", "upstream_gate_abandoned")
                for e in mine):
            if ("advance", pid) in refused or f"auto-advance-{pid}" in skip:
                continue
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
        # `(op, action_id) -> monotonic time` of an automatic step refused for a PASSING reason
        # (`TRANSIENT_REFUSALS`), skipped by `auto_next_op` until `AUTO_RETRY_AFTER_S` has gone by.
        self.refused_auto: dict = {}
        # `(key, active_base)`: the base the loop's automatic decisions read, keyed on what moves it
        # (`_active`) — `active_base` re-verifies the selected archive, which is not free per turn.
        self.active_cache: Optional[tuple] = None
        # Source lifecycles the author found nothing to nominate in (`author_next(skipped=)`).
        self.author_skipped: dict = {}
        # The mode this engine armed with, recorded once on the diagnostic `lane_armed` row.
        self.announced = False
        # A search-ended finish is waiting for the operation in flight
        # (`refuse_finish_over_upstream_job`): settle it, start nothing new.
        self.finishing = False


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


# `repair_developer`: the facade's REPAIR backend when the operator routed repair to another model
# (`agents/factory.py`, `UnifiedAgent._for_stage`) — the member that actually writes a repair, so a
# chain that skipped it rebound the implement Developer and stamped a base the repair never read
# (critic 2026-10-08). `engine/costs.py::_CHILD_ATTRS` names the same child for the same reason.
_CHAIN_ATTRS = ("inner", "developer", "repair_developer", "fallback", "base")


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


def lifecycle_base(engine, node, events=None) -> Optional[dict]:
    """The base node `node`'s CURRENT lifecycle was seeded on, while this engine serves the live
    lane; None otherwise, or before its current generation was seeded. The same reading
    `upstream_workspace.py::materialization_plan` pins a started lifecycle with (`seeded_basis`).
    `events` is the caller's read of the log, when it holds one."""
    if base_stamp(engine) is None or node is None:
        return None
    from looplab.engine.upstream_workspace import seeded_basis
    events = engine.store.read_all() if events is None else events
    created = getattr(node, "creation_event_seq", None)
    if created is None:
        return None
    selector, seeded_now = seeded_basis(node, events, created)
    return dict(selector) if seeded_now and isinstance(selector, dict) else None


def files_base(engine, node, events=None) -> Optional[dict]:
    """The base `node.files` are an overlay OF — what a node built from them must name: with no
    Developer call (a simplification, an inject shipping a fork's files) as its own base, and with
    one (an improve, a merge, a rebuild) as the base the Developer is PINNED to, because the call
    edits those files and the overlay it returns is still theirs. Its seeded lifecycle base, else
    the base its own `node_created` named. None while the lane is off or when neither is recorded
    (the creation prefix then rules, as on every stopped-lane row)."""
    if base_stamp(engine) is None or node is None:
        return None
    events = engine.store.read_all() if events is None else events
    seeded = lifecycle_base(engine, node, events)
    if seeded is not None:
        return seeded
    created = next((e for e in events if e.type == "node_created"
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


def _active(engine, events) -> dict:
    """`active_base` of `events` for the loop's automatic decisions, CACHED on the serve object.

    `active_base` re-verifies the selected archive (`seed_base.py::selected_seed_base`: the origin
    run's log and the archive's bytes) and `auto_next_op` and `author_next` each asked for it on
    every loop turn (critic 2026-10-08). Its answer moves only with the run's generation, the last
    `base_advanced` row and the launch selector, so those three are the key; a refusal is never
    cached — it raises to the caller each time, as before. The lane's own admit, in the worker,
    still verifies from scratch."""
    from looplab.engine.upstream_state import active_base
    from looplab.events.run_generation import run_generation_token
    serve = engine._upstream_serve
    seed = (getattr(engine, "_repo_spec", None) or {}).get("seed_base")
    last = next((e.seq for e in reversed(events) if e.type == "base_advanced"), None)
    key = (run_generation_token(events), last, repr(seed))
    cached = getattr(serve, "active_cache", None)
    if cached is not None and cached[0] == key:
        return cached[1]
    active = active_base(events, seed)
    serve.active_cache = (key, active)
    return active


def _stopping(state, job: UpstreamJob) -> bool:
    """No NEW phase may start: the run halted (paused, finishing, stop requested), or — for an
    automatic step, `idx` None — the operator's kill switch is on (doc 73 §4.2 G2)."""
    return bool(getattr(state, "halted", False)) or (
        job.idx is None and getattr(state, "upstream_auto_paused", False) is True)


async def _settle(engine, lane, job: UpstreamJob, *, stopping: bool = False) -> bool:
    """The MAIN task's half of one finished phase. True when it appended.

    `stopping` (`_stopping`, or the loop's exit drain): a phase whose CLAIM landed is settled as
    always — its charges, verdict or proposal — but no new phase starts. An admitted operation is
    dropped UNCLAIMED instead of claimed (a queued request stays queued for the next engine, an
    automatic one is derived again), an admitted advance is not committed, and a drafted proposal
    is recorded and left to the next engine (`_settle_author`)."""
    from looplab.core.errors import UpstreamRefusal
    store = engine.store
    receipt: Optional[dict] = None
    if job.op == "author":
        return await _settle_author(engine, lane, job, stopping=stopping)
    if stopping and job.phase == "admit" and job.error is None and job.out[0] == "ctx":
        engine._upstream_serve.job = None
        _LOG.info("upstream %s %s not started: the run halted or its automation is switched off",
                  job.op, job.body.get("action_id"))
        return False
    # Whether a refusal is about the PROPOSAL (recorded once, never asked again) or about the run's
    # state (`TRANSIENT_REFUSALS`, asked again later). Only the lane's own deliberate refusals can be
    # the former: anything else a worker raised (an `OSError`) is the box's, and passes.
    for_good = False
    async with engine._write_lock:
        if job.phase == "admit":
            if job.error is not None:
                receipt = {"outcome": "refused", "code": _code(job.error)}
                for_good = (isinstance(job.error, UpstreamRefusal)
                            and receipt["code"] not in TRANSIENT_REFUSALS)
            elif job.out[0] == "ack":
                receipt = {"outcome": "succeeded", "seq": job.out[1].get("seq")}
            elif job.op == "advance":
                receipt = _advance_settle(engine, lane, job.out[1])
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
                if job.authored.get("source_action_id"):
                    refusal["source_action_id"] = job.authored["source_action_id"]
                refusal["code"] = receipt.get("code") or "refused"
                store.append("lane_authored", refusal)
            elif for_good and job.op in ("check", "advance") and job.body.get("proposal_id"):
                # Refused for a reason about the proposal itself: said ONCE, durably, and
                # `auto_next_op` passes the proposal over from now on — in this process and the next.
                store.append("lane_held", {"op": job.op, "reason": REFUSED_PREFIX + receipt["code"],
                                           "proposal_id": job.body["proposal_id"]})
    if receipt.get("outcome") == "refused":
        _LOG.info("upstream %s %s refused: %s", job.op, job.body.get("action_id"), receipt.get("code"))
    engine._upstream_serve.job = None
    return True


def _advance_settle(engine, lane, prepared) -> dict:
    """The MAIN task's commit of an admitted advance, under the write lock: the lane's two
    re-checks against the log as it is NOW — the worker admitted it against an older read — then
    `base_advanced` and its hint. Refused (never raised) when the base moved, when the base cannot
    be read, or when an inject or fork was queued meanwhile (`upstream_work_pending`, the rule
    `UpstreamLane._advance_prepare` applies in the worker)."""
    from looplab.core.errors import UpstreamRefusal
    from looplab.engine.shared import engine_fold as fold
    from looplab.engine.upstream_state import active_base
    store = engine.store
    events = store.read_all()
    try:
        current = active_base(events, engine._repo_spec.get("seed_base"))["revision"]
    except (UpstreamRefusal, OSError) as exc:     # an unreadable base: refused on the receipt, not raised
        return {"outcome": "refused", "code": _code(exc)}
    if current != prepared["proposal"]["expected_base_revision"]:
        return {"outcome": "refused", "code": "upstream_base_conflict"}
    state = fold(events)
    if (len(state.inject_requests) > state.injects_done
            or len(state.fork_requests) > state.forks_done):
        return {"outcome": "refused", "code": "upstream_work_pending"}
    advanced = lane._advance_commit(prepared, store.append)
    _issue_hint(engine, store, prepared["proposal"], advanced)
    # The Developers build on the promoted base from their NEXT call (`sync_developer_base`), and a
    # node built by one that has not rebound yet names its own base on `node_created`.
    selector = prepared["proposal"]["selector"]
    _arm(engine)["current"] = {k: selector[k] for k in ("run_dir", "event_seq", "digest")}
    return {"outcome": "succeeded", "seq": advanced.seq}


def _author_work(engine, pick, generation):
    """The author's worker: its two paid calls under a span (`_op_span`), the body back."""
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
            if pick.get("rebase") is not None:
                # doc 73 §4.3: merge first (Git, no model call); only a clean merge that nominates a
                # capability is drafted and paid for.
                from looplab.engine.upstream_author import rebase_source
                merged = rebase_source(engine.run_dir, engine.task, pick)
                if merged["outcome"] != "ready":
                    return merged
            return author_draft(engine, pick, generation=generation)
    finally:
        try:
            pick["cost_usd"] = round(max(0.0, float(thread_committed_usd_exact() - start)), 6)
        except OverflowError:
            pick["cost_usd"] = float("inf")


async def _settle_author(engine, lane, job: UpstreamJob, *, stopping: bool = False) -> bool:
    """The MAIN task's half of an authoring job: the `lane_authored` row, then — for a drafted
    proposal — the same job continues as the lane's `propose` (its own refusals, rows and Git work).
    Not while `stopping`: the draft is retained (`retain_draft`) and its row says `drafted`, so the
    next engine whose automation runs proposes it unpaid (`unproposed_draft`).
    A spend ceiling the worker met is the run's to stop on: raised here, after the job is cleared."""
    from looplab.core.errors import budget_stop_leaf
    pick, serve = job.ctx, engine._upstream_serve
    out = job.out if job.error is None else {"outcome": "failed", "code": _code(job.error)}
    from looplab.engine.upstream_author import UNRECORDED_OUTCOMES
    if out.get("outcome") in UNRECORDED_OUTCOMES:
        # A rebased source that nominates no capability on this base: memoized like the native
        # path's empty nomination (against the base and the pending triggers), and no row. A merge
        # that could not run (`rebase_unavailable`) is no answer either: no row, memoized until its
        # retry time (`upstream_author.py::RebaseRetry`), so a Git timeout never closes the source.
        serve.author_skipped[job.body["action_id"]] = pick.get("pending")
        serve.job = None
        return False
    async with engine._write_lock:
        row = {"action_id": job.body["action_id"], "track": pick["track"],
               "source_node_id": pick["node"].id, "outcome": out["outcome"]}
        row["hunk_hashes"] = sorted({r["hunk_hash"] for r in pick["rows"] or []})
        if out.get("reason"):
            row["reason"] = str(out["reason"])[:500]
        if out.get("code"):
            row["code"] = out["code"]
        if isinstance(pick.get("cost_usd"), float):
            row["cost_usd"] = pick["cost_usd"]
        if pick.get("rebase") is not None:
            # doc 73 §4.3: which lifecycle this rebase was of, the base it was measured on, and —
            # on a conflict — the paths the merge could not apply.
            row["source_action_id"] = pick["source_action_id"]
            row["rebased_from"] = pick["rebase"]["from_digest"]
            if out.get("conflicts"):
                row["conflicts"] = list(out["conflicts"])
        engine.store.append("lane_authored", row)
        if out["outcome"] == "drafted" and not stopping:
            job.authored = {k: row[k] for k in ("action_id", "track", "source_node_id", "hunk_hashes",
                                                "source_action_id") if k in row}
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


def _start_author(engine, armed, state, events, *, active=None) -> None:
    """Start the automated author on the next source, when it is on and one is due."""
    from looplab.engine.upstream_author import (author_next, author_rebase_setting, unproposed_draft,
                                                upstream_author_setting)
    from looplab.events.run_generation import run_generation_token
    serve = engine._upstream_serve
    if not upstream_author_setting(armed["settings"]) or claims_unresolved(events):
        return
    # A draft paid for and recorded, whose proposal the lane never claimed (a crash in between):
    # proposed again from its retained body, with no new call.
    pending = unproposed_draft(engine.run_dir, events)
    if pending is not None:
        row, body = pending
        serve.job = UpstreamJob(op="propose", body=body, idx=None, authored={
            k: row.get(k) for k in ("action_id", "track", "source_node_id", "hunk_hashes",
                                    "source_action_id") if k in row})
        _start(engine, _lane(engine, armed["settings"]), serve.job)
        return
    pick = author_next(engine.run_dir, engine.task, state, events, skipped=serve.author_skipped,
                       active=active, rebase=author_rebase_setting(armed["settings"]))
    if pick is None:
        return
    generation = run_generation_token(events)
    serve.job = UpstreamJob(op="author", body={"action_id": pick["action_id"]}, idx=None,
                            phase="author", ctx=pick)
    _spawn(serve.job, lambda: _author_work(engine, pick, generation), carry_context=True)


def _queued_body(lane, request: dict) -> dict:
    """A queued request's exact body: inline for check/advance, the retained request file for a
    propose (verified against its hash, as the stopped lane's exact recovery does)."""
    from looplab.core.errors import UpstreamRefusal
    from looplab.engine.upstream_state import digest, read_retained_json
    from looplab.engine.upstream_workspace import owned_path
    if request.get("op") != "propose":
        return dict(request.get("body") or {})
    body = read_retained_json(owned_path(lane.rd, str(request.get("request_path") or "")))
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
        return await _settle(engine, _lane(engine, _arm(engine)["settings"]), job,
                             stopping=serve.finishing or _stopping(state, job))
    requests = getattr(state, "lane_op_requests", None) or []
    done = int(getattr(state, "lane_ops_done", 0) or 0)
    if not requests and getattr(engine, "_repo_spec", {}).get("upstream") is None:
        return False
    armed = _arm(engine)
    if state.halted or serve.finishing:
        return False
    if not serve.announced:
        # What THIS engine serves, once per process (`upstream_live_view` reads it): diagnostic, so
        # its position keys nothing.
        from looplab.engine.upstream_author import upstream_author_setting
        serve.announced = True
        async with engine._write_lock:
            # The caps ride on the row (doc 73 §4.3) because they are what THIS engine enforces
            # until it restarts: a reader showing them beside the hour's count reads them here,
            # never off the snapshot a `PUT /config` may have edited since (`upstream_live_body`).
            from looplab.engine.upstream_author import author_usd_cap
            engine.store.append("lane_armed", {
                "mode": armed["mode"], "reason": armed["reason"],
                "author": armed["mode"] == "auto" and upstream_author_setting(armed["settings"]),
                "author_usd_cap": author_usd_cap(armed["settings"]),
                "advances_per_hour": advances_per_hour(armed["settings"])})
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
    from looplab.core.errors import UpstreamRefusal
    try:
        active = _active(engine, events)
    except (UpstreamRefusal, OSError):  # an unreadable base is the lane's to state; nothing automatic starts
        return False
    now = time.monotonic()
    skip = {aid for (_op, aid), at in serve.refused_auto.items() if now - at < AUTO_RETRY_AFTER_S}
    nxt = auto_next_op(events, engine._repo_spec.get("seed_base"), active=active, skip=skip)
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
        _start_author(engine, armed, state, events, active=active)
        return False
    op, body = nxt
    per_hour = advances_per_hour(armed["settings"])
    if op == "advance" and per_hour and auto_advances_in_window(events, time.time()) >= per_hour:
        # THE HOURLY CAP (doc 73 §4.2 G3): the passed gate waits; said once per proposal.
        if not any(e.type == "lane_held" and e.data.get("proposal_id") == body["proposal_id"]
                   and not str(e.data.get("reason") or "").startswith(REFUSED_PREFIX)
                   for e in events):
            async with engine._write_lock:
                engine.store.append("lane_held", {"op": "advance", "reason": f"rate_cap:{per_hour}/h",
                                                  "proposal_id": body["proposal_id"]})
            return True
        return False
    serve.job = UpstreamJob(op=op, body=body, idx=None)
    _start(engine, _lane(engine, armed["settings"]), serve.job)
    return False


def refuse_finish_over_upstream_job(engine, state, data) -> bool:
    """Refuse a SEARCH-ENDED finish while the live lane has an operation in flight, and stop starting
    new ones (`UpstreamServe.finishing`): the next loop turns settle it, then the finish is retried —
    so its folded rows land BEFORE the finish claims its scope, never inside it. The shape of
    `track_lane.py::refuse_finish_over_track_queue`; a halted run or a stop-class finish is never
    held back (`drain_upstream_job` then leaves the claim for the operator)."""
    from looplab.engine.track_lane import _search_ended
    serve = getattr(engine, "_upstream_serve", None)
    if serve is None or serve.job is None or state.halted or not _search_ended((data or {}).get("reason")):
        return False
    serve.finishing = True
    return True


async def drain_upstream_job(engine) -> None:
    """THE LOOP'S EXIT (`orchestrator.py`, after `_drain_adopted_evals`): settle the one operation in
    flight instead of dropping it.

    Before this, every `break` out of the loop (a pause, a stop, the run finishing) left `serve.job`
    as it was: a gate whose claim had landed never got its charges or its verdict appended, so the
    claim stood unresolved — which refuses every later lane operation, the automatic check and the
    author included, until the operator abandons it — and its daemon thread was orphaned (critic
    2026-10-08). A claimed operation is now waited for like an adopted evaluation — the same barrier,
    for the same reason: the gate leases the run's devices like one and bounds every execution by
    the run's own per-evaluation ceiling (`upstream_gate.py::execute_gate`) — and settled; nothing
    new starts (`_settle(stopping=True)`). A raising exit (a crash, a hard budget stop) still leaves
    the claim as doc 72 designed it: unresolved, for the operator to abandon."""
    import anyio
    serve = getattr(engine, "_upstream_serve", None)
    if serve is None or serve.job is None:
        return
    # NEVER INSIDE A FINISH: the lane's settling rows are folded, and appended after a finish
    # claimed its scope they make the staged finish read as abandoned (`events/finalize_scope.py`;
    # the same defect the track lane had, review 2026-10-08). A search-ended finish was already held
    # back until the job settled (`refuse_finish_over_upstream_job`); a stop-class finish is not
    # held, and its claim stays unresolved for the operator, as a crash leaves it.
    from looplab.events.finalize_scope import incomplete_finalize_scope
    from looplab.engine.shared import engine_fold as fold
    events = engine.store.read_all()
    if fold(events).finished or incomplete_finalize_scope(events) is not None:
        return
    while serve.job is not None:
        job = serve.job
        while not job.done:
            await anyio.sleep(0.05)
        await _settle(engine, _lane(engine, _arm(engine)["settings"]), job, stopping=True)
