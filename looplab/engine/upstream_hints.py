"""The upstream HINT to Developer sessions already at work (doc 73 §4.2, G1).

WHY (2026-10-08). When the live engine advances the base (`engine/upstream_serve.py`), every LATER
Developer call is rebound to the new base (`sync_developer_base`) and its prompt states the promotion
(`core/upstream_board.py::developer_base_note`). A Developer ALREADY inside a build or repair session
learns nothing until that session ends — exactly the agent that may be repairing a crash the base
just fixed. The operator asked for a FORCED hint to the Developers at work, recorded so replay shows
what each was told; Researchers keep their one "Active verified capability base" line
(`agents/state_brief.py`).

THE CONTRACT.
* ISSUED once per live advance by the MAIN task — `upstream_hint_issued {hint_id, text, sessions}`
  (folded into the upstream history): the exact words, and which sessions were open to hear them.
* DELIVERED at a tool-loop TURN BOUNDARY (`agents/tool_loop.py::interjection_scope`), as a `user`
  turn, to a session that was open when the hint was issued (a later call is rebound and told by its
  prompt) — at most `MAX_HINTS_PER_SESSION` per session, each once. A session ending before its next
  turn simply never hears it.
* RECORDED as `upstream_hint_delivered {hint_id, session, node_id}` — a DIAGNOSTIC row the
  Developer's own worker thread appends (invariant #1 admits diagnostics from any thread).
* BOUNDED: the text is capped at `HINT_TEXT_CAP`, the model-written summary inside it fenced
  (`core/evidence.py::fence_untrusted`). It INFORMS — the session's workspace keeps the code it
  started on — and never orders a rewrite.

Without an open session (no live engine, a stub) nothing changes: the tool loop sees no scope and its
message list is byte-identical.

AN EXTERNAL CLI DEVELOPER (doc 73 §4.3, `Settings.upstream_hint_external`). Its loop runs in its own
process, so the tool-loop boundary above never fires for it. A session opened `external=True` also
publishes a NOTICE CHANNEL (`agents/tool_loop.py::notice_channel_scope`): the agent's wrapper OFFERS
pending notices to the agent's own hook (`agents/cli_hook.py`, a `claude` PostToolUse hook — the one
preset with a per-invocation channel), under the same `MAX_HINTS_PER_SESSION`, and ACKNOWLEDGES each
once the hook emitted it — only then is the `upstream_hint_delivered {channel: cli_hook}` row written.
Every other preset has no such channel; its NEXT call states the promotions instead
(`agents/cli_agent.py::CliAgentDeveloper._upstream_note`).
"""
from __future__ import annotations

import itertools
import logging
import threading
from contextlib import contextmanager, nullcontext
from typing import Callable, Optional

_LOG = logging.getLogger(__name__)

MAX_HINTS_PER_SESSION = 3
HINT_TEXT_CAP = 700
_SUMMARY_CAP = 300


def hint_id_for(proposal_id: str) -> str:
    return "hint-" + str(proposal_id)[:64]


def hint_text(*, kind: Optional[str], source_node_id, summary: str, flag, paths) -> str:
    """The engine's notice, bounded. Model-written words (`summary`, flag values) sit in a fence.

    "the next build is EVALUATED on the new base", not "STARTS from" it (review 2026-10-09): a build
    from a parent with files is pinned to the base those files were written on
    (`node_build.py::_implement_result`, `upstream_serve.py::files_base`), and a chain holding a CLI
    agent is never rebound (`upstream_serve.py::sync_developer_base`) — its working copy stays the
    launch checkout. What every such build shares is that its NEXT lifecycle merges the overlay onto
    the run's current base before the evaluation, which is also what the CLI note says
    (`agents/cli_agent.py::CliAgentDeveloper._upstream_note`)."""
    from looplab.core.evidence import fence_untrusted
    origin = f"experiment #{source_node_id}" if type(source_node_id) is int else "an earlier experiment"
    named = ", ".join(str(p) for p in list(paths or [])[:6])
    said = " ".join(str(summary or "").split())[:_SUMMARY_CAP] or "(no summary)"
    if kind == "fix":
        head = (f"Upstream notice (engine): the run's base just took a repair's FIX from {origin}"
                + (f" in {named}" if named else "") + ".")
        tail = ("This session keeps the code it started on; the next build is evaluated on the new "
                "base (a repair stays on its lifecycle's base). If you are hitting this failure, it is "
                "fixed in the base: mirror the fix rather than work around it; otherwise carry on.")
    else:
        if isinstance(flag, dict) and isinstance(flag.get("name"), str):
            what = (f"a capability behind flag `{flag['name']}` (default "
                    f"`{str(flag.get('default'))[:64]}`, enabled `{str(flag.get('enabled'))[:64]}`)")
        else:
            what = "a capability"
        head = f"Upstream notice (engine): the run's base just took {what} from {origin}."
        tail = ("This session keeps the code it started on; the next build is evaluated on the new "
                "base, where a recipe can switch the flag on instead of re-implementing it. No action "
                "is required now.")
    text = head + "\n" + fence_untrusted(said, "upstream summary") + "\n" + tail
    return text[:HINT_TEXT_CAP]


class UpstreamHintBoard:
    """The engine's live hint board (`Engine._upstream_hints`). Thread-safe: the MAIN task posts,
    Developer worker threads open sessions and drain them."""

    def __init__(self, sink: Optional[Callable[[dict], None]] = None) -> None:
        self._lock = threading.Lock()
        self._hints: list[dict] = []
        self._sessions: dict[int, dict] = {}
        self._ids = itertools.count(1)
        self.sink = sink

    def open_sessions(self) -> list[str]:
        with self._lock:
            return [s["label"] for s in self._sessions.values()]

    def post(self, hint: dict) -> None:
        """Make `hint` (`hint_id`, `text`) deliverable to every session open NOW."""
        with self._lock:
            if any(h["hint_id"] == hint["hint_id"] for h in self._hints):
                return
            self._hints.append({"hint_id": hint["hint_id"], "text": hint["text"]})
            for s in self._sessions.values():
                s["pending"].append(len(self._hints) - 1)

    def _open(self, label: str, node_id) -> int:
        with self._lock:
            sid = next(self._ids)
            self._sessions[sid] = {"label": f"{label}#{sid}", "node_id": node_id, "pending": [],
                                   "delivered": 0}
            return sid

    def _close(self, sid: int) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def drain(self, sid: int) -> list[str]:
        """The `user` turns this session has not heard yet — at most `MAX_HINTS_PER_SESSION` ever."""
        out, rows = [], []
        with self._lock:
            s = self._sessions.get(sid)
            if s is None or not s["pending"]:
                return []
            while s["pending"] and s["delivered"] < MAX_HINTS_PER_SESSION:
                hint = self._hints[s["pending"].pop(0)]
                s["delivered"] += 1
                out.append(hint["text"])
                rows.append({"hint_id": hint["hint_id"], "session": s["label"],
                             **({"node_id": s["node_id"]} if type(s["node_id"]) is int else {})})
            if s["delivered"] >= MAX_HINTS_PER_SESSION:
                s["pending"].clear()
        for row in rows:
            if self.sink is not None:
                try:
                    self.sink(row)
                except Exception:  # noqa: BLE001 — a lost receipt row must never end a Developer session
                    _LOG.warning("upstream hint receipt not recorded", exc_info=True)
        return out

    def offer(self, sid: int) -> list[dict]:
        """The notices this session has not HEARD yet, `[{"hint_id", "text"}]` — at most the room
        `MAX_HINTS_PER_SESSION` leaves, oldest first — and NOT recorded: an external agent's hook
        delivers them, and `acknowledge` records each once it did.

        Offering MOVES NOTHING (review 2026-10-09). It used to pop the notice off `pending` and
        count it against the cap as it was handed over, so a notice whose hook never ran — the agent
        finished before its next tool call, a `notices.json` write failed, a validation retry
        started a fresh invocation in the same session — was spent: never re-offered, and charged
        against the cap the session was never told. Now only `acknowledge` (the hook EMITTED it) or a
        `drain` takes a notice off `pending` and counts it; an offered notice stays at the head of
        `pending`, so the next offer hands it over again and a later notice gets room only once an
        earlier one was heard — the cap still bounds what the agent can be shown."""
        with self._lock:
            s = self._sessions.get(sid)
            if s is None or not s["pending"]:
                return []
            room = max(0, MAX_HINTS_PER_SESSION - s["delivered"])
            out = []
            for index in s["pending"][:room]:
                hint = self._hints[index]
                s.setdefault("offered", set()).add(hint["hint_id"])
                out.append({"hint_id": hint["hint_id"], "text": hint["text"]})
        return out

    def acknowledge(self, sid: int, hint_id: str, *, channel: str) -> bool:
        """Record that the session's external channel DELIVERED `hint_id` — once, and only for a
        notice it was offered — and only now count it against the cap and take it off `pending`
        (`offer`). True when a receipt row was handed to the sink."""
        with self._lock:
            s = self._sessions.get(sid)
            if s is None or hint_id not in s.get("offered", ()) or hint_id in s.setdefault("acked", set()):
                return False
            s["acked"].add(hint_id)
            index = next((i for i in s["pending"] if self._hints[i]["hint_id"] == hint_id), None)
            # Absent from `pending`: an in-house `drain` in the same session (a validation fallback's
            # tool loop) already told it and counted it — the hook told it too, so the row is still
            # true, but the cap is not charged twice for one notice.
            if index is not None:
                s["pending"].remove(index)
                s["delivered"] += 1
                if s["delivered"] >= MAX_HINTS_PER_SESSION:
                    s["pending"].clear()
            row = {"hint_id": hint_id, "session": s["label"], "channel": str(channel)[:32],
                   **({"node_id": s["node_id"]} if type(s["node_id"]) is int else {})}
        if self.sink is not None:
            try:
                self.sink(row)
            except Exception:  # noqa: BLE001 — a lost receipt row must never end a Developer session
                _LOG.warning("upstream hint receipt not recorded", exc_info=True)
                return False
        return True

    @contextmanager
    def session(self, label: str, node_id=None, *, external: bool = False):
        """One Developer session: open, scope the tool loop's interjection on it — and, `external`,
        an external agent's notice channel (`_ExternalChannel`) — then close."""
        from looplab.agents.tool_loop import interjection_scope, notice_channel_scope
        sid = self._open(label, node_id)
        try:
            with interjection_scope(lambda: self.drain(sid)), \
                    notice_channel_scope(_ExternalChannel(self, sid) if external else None):
                yield sid
        finally:
            self._close(sid)


class _ExternalChannel:
    """One session's notice channel for an external agent (`agents/cli_hook.py::HookNotices`)."""

    def __init__(self, board: UpstreamHintBoard, sid: int) -> None:
        self._board, self._sid = board, sid

    def offer(self) -> list[dict]:
        return self._board.offer(self._sid)

    def acknowledge(self, hint_id: str, *, channel: str = "cli_hook") -> bool:
        return self._board.acknowledge(self._sid, hint_id, channel=channel)


def developer_session(board, fn, args, *, external: bool = False):
    """The scope `node_build.py::_run_developer` wraps one Developer call in: a hint session named
    after the call (`implement_from`, `repair_from`, …) and the node a repair is for, or a no-op
    without a board. `external` (`upstream_serve.py::external_hint_channel`) also publishes the
    session's notice channel for an external CLI agent."""
    if board is None:
        return nullcontext()
    label = getattr(fn, "__name__", "developer")
    node_id = None
    for a in args:
        nid = getattr(a, "id", None)
        if type(nid) is int and hasattr(a, "attempt"):
            node_id = nid
            break
    return board.session(str(label)[:40], node_id if label.startswith("repair") else None,
                         external=external)
