"""Cross-run introspection tools for the assistant (ADR-7 tool protocol).

Where `RunTools` reads the ONE live run bound to it and `SiblingRunTools` reads other runs of the
SAME task, `MachineRunsTools` gives the general-purpose assistant a richer view over every run under
its configured run root — so it can reference an existing run, report which ones are live, and read
logs/traces before steering or fixing it. It is not a host-wide filesystem scanner. Same
`.specs()`/`.execute()` shape as the other providers; every `execute` returns a string and soft-fails
(a junk tool call must never crash the loop).

Runs are folded from disk on demand and cached by each event log's (size, mtime) fingerprint, so
repeated turns don't re-fold unchanged runs. Liveness (`engine_running`) is injected as a callable
by the server (`_engine_alive`) to avoid a circular import and to reuse the one race-free lock probe.

This module is READ-ONLY, and since 2026-09-08 that is all it is (doc 25 TO-02). It used to be a
1,988-line god-module that also held the run-MUTATING provider, the launch-proposal provider, one
assistant turn's mutation journal and the command adapter — four subsystems sharing a file for no
reason but history. They are `run_control_tools.py`, `run_launcher_tools.py`,
`turn_mutation_fence.py` and `run_command_adapter.py`; nothing is re-exported from here, so an
importer names the thing it actually uses.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from looplab.events import digest
from looplab.tools.run_tools import ForeignRunReader
from looplab.tools._base import RESULT_CAP, fn_spec

# A trace is a whole conversation, but the shared tool loop HEAD-truncates every tool result to
# RESULT_CAP chars (agent.drive_tool_loop), so a larger budget would be silently cut there (losing
# the tail with no marker). Stay under that cap (-400 headroom for the header + our truncation hint)
# so our own truncation + the "narrow with `stage`" hint engage first.
_TRACE_CHARS = RESULT_CAP - 400


# How many episodes a map prints at EACH END before the middle is elided. Both ends on purpose (the
# same rule `tools/log_tools.py::_render_search` keeps): a node's first episodes are where a bug
# first showed and its last are where it died, and a reader shown only one end cannot tell an
# always-broken node from a just-broken one. The elided middle is COUNTED and reachable by ordinal,
# so nothing is silently dropped.
_EPISODE_ENDS = 25
_EPISODE_PAGE = 60


def _render_episodes(payload: dict, run_id, nid, from_index, limit, max_chars: int) -> str:
    """One line per episode: its POSITION, label, when, how long, how many spans, and its `anchor`.

    A map is chosen FROM, not read from, so every row is identity plus the seek key and nothing
    else. It always states the node's TOTAL episode count, because a bounded list that does not is
    the "no record matches" answer this repo has now paid for twice.

    Paging is by POSITION in this map (`#1`, `#2`, …) and deliberately NOT by the row's own
    `ordinal`: that field is the engine's inline-repair counter, which is absent on every band that
    is not a repair — a pager keyed on it would silently skip the plan, the build and the training.
    The repair ordinal is still SHOWN, where it exists, because it is what the operator's own
    question ("the third repair") names.
    """
    episodes = list(payload.get("episodes") or [])
    projection = payload.get("projection") or {}
    if not episodes:
        return f"(run {run_id} node #{nid}: no trace episodes recorded for its current attempt.)"
    total = len(episodes)
    rows = list(enumerate(episodes, start=1))
    shown, elided = rows, 0
    if from_index is not None:
        try:
            start = int(from_index)
        except (TypeError, ValueError):
            return f"(from_index must be a whole number, got {from_index!r})"
        try:
            page = max(1, int(limit)) if limit is not None else _EPISODE_PAGE
        except (TypeError, ValueError):
            page = _EPISODE_PAGE
        shown = [row for row in rows if row[0] >= start][:page]
        head = f"from #{start}"
    elif total > 2 * _EPISODE_ENDS:
        shown = rows[:_EPISODE_ENDS] + rows[-_EPISODE_ENDS:]
        elided = total - 2 * _EPISODE_ENDS
        head = "first and last"
    else:
        head = "all"
    lines = [f"run {run_id} · node #{nid} · {total} episode(s) in this attempt ({head} shown; "
             f"pass an `anchor` as `before` to `read_run_trace` to read one):"]
    if projection.get("omitted_episodes"):
        lines.append(f"  [{projection['omitted_episodes']} older episode(s) are past the map's own "
                     f"ceiling and are not listed]")
    if not shown:
        lines.append(f"  (no episode at or after #{from_index} — this node has {total})")
    for position, (index, episode) in enumerate(shown):
        if elided and position == _EPISODE_ENDS:
            lines.append(f"  … {elided} episode(s) elided — pass from_index=<n> to read from there")
        seconds = episode.get("seconds")
        span_count = episode.get("spans")
        lines.append(
            f"  #{index} {episode.get('label') or episode.get('band') or '(unnamed)'}"
            + (f" · repair {episode['ordinal']}" if episode.get("ordinal") is not None else "")
            + (f" · {float(seconds):.0f}s" if isinstance(seconds, (int, float)) else "")
            + (f" · {span_count} span(s)" if span_count else "")
            + (f" · {episode.get('status')}" if episode.get("status") else "")
            + f" · anchor={episode.get('anchor')}")
    text = "\n".join(lines)
    budget = max(max_chars, _TRACE_CHARS)
    if len(text) <= budget:
        return text
    return text[:budget].rstrip() + (f"\n…[+{len(text) - budget} chars truncated — page with "
                                     f"from_index]")


def _render_conversation(convo: dict, run_id, nid, stage: Optional[str], max_chars: int,
                         *, before: Optional[str] = None) -> str:
    """Render `traceview.build_conversation` output as a readable linear thread. One block per stage
    (create_node / evaluate / …); within a stage, requests show the prompt, generations show
    thinking + output + which tools were called, tool turns show input→output. Filtered to one stage
    when `stage` is given (substring match on its label). Bounded to a generous trace budget.

    The window this rendered is a TAIL unless `before` anchored it, and a reader that cannot see
    that reads a bounded window as the whole node — the omission is therefore stated in the header
    from the projection's own receipt, beside the control that moves it."""
    stages = convo.get("stages") or []
    if stage:
        s = str(stage).lower()
        stages = [st for st in stages if s in str(st.get("label") or "").lower()]
    if not stages:
        which = f" matching {stage!r}" if stage else ""
        anchored = f" ending at {before}" if before else ""
        return f"(run {run_id} node #{nid}: no trace stages{which}{anchored} recorded)"
    projection = convo.get("projection") or {}
    omitted = projection.get("omitted_spans") or 0
    reach = ""
    if before:
        reach += f" · window ends at {before}"
    if omitted:
        reach += (f" · {omitted} earlier span(s) of this node are outside this window — call "
                  f"`read_run_trace_episodes` and re-read with `before=<anchor>`")
    lines = [f"run {run_id} · node #{nid} · trace ({len(stages)} stage(s){reach}):"]
    for st in stages:
        roll = st.get("rollup") or {}
        tok = (roll.get("tokens") or {}).get("total")
        meta = f"{roll.get('generations', 0)} gen · {roll.get('tools', 0)} tool"
        meta += f" · {tok} tok" if tok else ""
        lines.append(f"\n══ stage: {st.get('label') or '(unnamed)'} · {meta} ══")
        for t in st.get("turns") or []:
            kind = t.get("type")
            if kind == "request":
                lines.append("▶ REQUEST" + (f" [{t['label']}]" if t.get("label") else ""))
                for m in t.get("messages") or []:
                    body = str(m.get("content") or "").strip()
                    if body:
                        lines.append(f"  [{m.get('role')}] {body}")
            elif kind == "generation":
                if t.get("think"):
                    lines.append(f"🧠 {str(t['think']).strip()}")
                if str(t.get("output") or "").strip():
                    lines.append(f"💬 {str(t['output']).strip()}")
                calls = [c for c in (t.get("tool_calls") or []) if c]
                if calls:
                    lines.append(f"  → called {', '.join(str(c) for c in calls)}")
            elif kind == "tool":
                head = f"⚙ {t.get('name') or 'tool'}"
                if t.get("status") and t["status"] != "OK":
                    head += f" ({t['status']})"
                lines.append(head)
                if str(t.get("input") or "").strip():
                    lines.append(f"    in:  {str(t['input']).strip()}")
                if str(t.get("output") or "").strip():
                    lines.append(f"    out: {str(t['output']).strip()}")
    text = "\n".join(lines)
    budget = max(max_chars, _TRACE_CHARS)
    if len(text) <= budget:
        return text
    return text[:budget].rstrip() + f"\n…[+{len(text) - budget} chars truncated — narrow with `stage`]"


class MachineRunsTools(ForeignRunReader):
    """Read-only view over ALL runs under the run-root (for the assistant).

    Cache/reader composition and the delegate-with-receipt shape come from `ForeignRunReader`
    (doc 25 TO-05); this provider adds the LIVENESS column and the trace projection, and — like
    `AllRunsTools` — deliberately applies no task scope."""

    def __init__(self, run_root, alive_fn: Optional[Callable[[Path], bool]] = None,
                 max_chars: int = 3500):
        super().__init__(run_root, max_chars=max_chars)
        self.alive_fn = alive_fn

    # MachineRunsTools is not bound to a single run; accept bind_state for CompositeTools symmetry (no-op).
    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            fn_spec("list_runs",
                # Scoped to THIS run root, not the machine: `_run_ids()` iterates `self.run_root`
                # only. The old "EVERY run on this machine" wording made an absent result read as
                # portfolio-wide evidence that nobody had tried something, which is exactly the
                # inference that causes a repeated experiment. Factual correction of a described
                # scope, not a prompt rewrite.
                "List every LoopLab run under this run root with its goal, phase, best metric, node count "
                "and whether its engine is LIVE right now. Use to reference an existing run, see what "
                "is running, or pick one to inspect/steer.",
                {"only_live": {"type": "boolean",
                               "description": "if true, list only runs whose engine is currently live"}}),
            fn_spec("read_run",
                "Read ONE run in detail: goal, direction, phase, best experiment and its top "
                "experiments. Use a run_id from list_runs before steering or fixing it.",
                {"run_id": {"type": "string"},
                 "sort": {"type": "string", "enum": ["best", "worst", "recent"]},
                 "limit": {"type": "integer"}},
                ["run_id"]),
            fn_spec("read_run_experiment",
                "Read one experiment of a run in full detail (params, metric, robustness, rationale, "
                "failure, sweep trials). Use run_id + node_id from read_run.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "trials": {"type": "string", "description": "how many sweep trials: a number, or 'all'"}},
                ["run_id", "node_id"]),
            fn_spec("read_run_logs",
                "Read one experiment's EXECUTION LOGS: the captured stdout/stderr TAILS as recorded "
                "in the event log (bounded, not the raw full stream — the tail end holds the error "
                "and the final metric line). Far more than the short failure summary. Use to see what "
                "a node printed while training, or why it failed. Use run_id + node_id from read_run.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"}},
                ["run_id", "node_id"]),
            fn_spec("read_run_trace",
                "Read one experiment's bounded CAPTURED AGENT TRACE as a linear, de-duplicated "
                "conversation: recorded requests, model output/reasoning fields, tool calls and tool "
                "results. Capture may be disabled, redacted or truncated; this is not proof of the "
                "model's complete internal reasoning. The window is the node's LATEST steps unless "
                "you pass `before`; a long node (thousands of steps over hours) does not fit in one "
                "window, and the answer says how many earlier steps it left out. To read those, call "
                "read_run_trace_episodes and pass an episode's `anchor` as `before`. "
                "Use run_id + node_id from read_run.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "stage": {"type": "string", "description": "optional: only the stage whose label "
                                                            "contains this text (e.g. 'repair')"},
                 "before": {"type": "string", "description": "optional: an `anchor` from "
                                                             "read_run_trace_episodes — the window "
                                                             "then ENDS at that step instead of at "
                                                             "the node's newest one"}},
                ["run_id", "node_id"]),
            fn_spec("read_run_trace_episodes",
                "Map one experiment's trace: every episode it recorded (plan, build, train, each "
                "repair…) with its ordinal, label, duration, span count and the `anchor` that seeks "
                "read_run_trace to it — and none of their contents. Cheap. Use this FIRST when a "
                "node ran long or was repaired many times, to find the part worth reading.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "from_index": {"type": "integer",
                                "description": "optional: list from this episode POSITION onward "
                                               "(the `#n` in each row); the default shows both ends "
                                               "and counts the elided middle"},
                 "limit": {"type": "integer",
                           "description": "optional: how many rows to list from `from_index`"}},
                ["run_id", "node_id"]),
        ]

    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        try:
            if name == "list_runs":
                return self._list_runs(bool(args.get("only_live")))
            if name == "read_run":
                return self._read_run(args.get("run_id"), args.get("sort"), args.get("limit"))
            if name == "read_run_experiment":
                return self._read_experiment(args.get("run_id"), int(args.get("node_id")),
                                             args.get("trials"))
            if name == "read_run_logs":
                return self._read_logs(args.get("run_id"), int(args.get("node_id")))
            if name == "read_run_trace":
                return self._read_trace(args.get("run_id"), int(args.get("node_id")),
                                        args.get("stage"), args.get("before"))
            if name == "read_run_trace_episodes":
                return self._read_trace_episodes(args.get("run_id"), int(args.get("node_id")),
                                                 args.get("from_index"), args.get("limit"))
            return f"(unknown tool: {name})"
        # BROAD on purpose — the module docstring's "soft-fails, never raises" contract. A narrower
        # tuple missed AttributeError and everything else that folding a foreign log or building a
        # trace conversation can raise, and `drive_tool_loop` does NOT guard `tools.execute`
        # (cross_run_tools.py documents that), so one odd shape killed the whole assistant turn.
        # `RunControlTools` below already catches this way.
        except Exception as e:  # noqa: BLE001 - a tool must not be able to end the turn
            return f"(tool error: {e})"

    # --- machine-readable summaries (also reused by the /api/assistant run-ref expansion) ------------
    def summaries(self, only_live: bool = False) -> list[dict]:
        """Structured per-run summary for EVERY run (used by the tool AND by @run-mention expansion)."""
        out = []
        for rid in self._run_ids():
            # A SWEEP over every run under the root — its folds must not evict the runs the turn is
            # actually working with (`tools/_runcache.py::_cache_max`), and the ROW it needs is
            # kept when that state is dropped, so the assistant's second sweep of a turn (this is
            # also the @run-mention expansion) folds nothing (`tools/_runcache.py::summary`).
            row = self._summary(rid)
            if row is None:
                continue
            live = self._alive(rid)
            if only_live and not live:
                continue
            out.append({
                "run_id": rid, "goal": row["goal"] or row["task_id"], "direction": row["direction"],
                "phase": ("finished" if row["finished"] else ("live" if live else "idle")),
                "nodes": row["nodes"],
                "best_metric": row["best_display_metric"],
                "best_node_id": row["best_node_id"],
                "engine_running": live, "finished": row["finished"],
            })
        return out

    # --- internals -----------------------------------------------------------
    def _run_ids(self) -> list[str]:
        return self._runs.run_ids()

    def _safe_dir(self, run_id: Optional[str]) -> Optional[Path]:
        return self._runs.safe_dir(run_id)

    def _alive(self, run_id: str) -> bool:
        if self.alive_fn is None:
            return False
        rd = self._safe_dir(run_id)
        try:
            return bool(rd is not None and self.alive_fn(rd))
        except Exception:  # noqa: BLE001 - liveness is best-effort; never crash the loop
            return False

    def _list_runs(self, only_live: bool) -> str:
        rows = self.summaries(only_live)
        if not rows:
            return "(no live runs)" if only_live else "(no runs yet)"
        lines = []
        for r in rows:
            live = " · LIVE" if r["engine_running"] else ""
            best = digest.fmt_num(r["best_metric"]) if r["best_metric"] is not None else "—"
            lines.append(f"{r['run_id']}: {str(r['goal'])[:70]} · best={best} ({r['direction']}) · "
                         f"{r['nodes']} nodes · {r['phase']}{live}"
                         + self._partial_suffix(r["run_id"]))
        return f"{len(lines)} run(s):\n" + "\n".join(lines)

    def _read_run(self, run_id, sort, limit) -> str:
        st = self._state(run_id)
        if st is None:
            return f"(no such run: {run_id!r})"
        note = self._runs.source_note(run_id)
        best = st.best()
        live = self._alive(str(run_id))
        head = (f"run {run_id} · goal: {st.goal or st.task_id} · direction={st.direction} · "
                f"phase={'finished' if st.finished else ('live' if live else 'idle')} · "
                f"{len(st.nodes)} nodes · best={digest.fmt_num(digest.node_metric(best)) if best else '—'}"
                + (f" (#{best.id})" if best else ""))
        self._reader.bind_state(st, None)
        listing = self._reader.execute("list_experiments",
                                       {"sort": sort or "best", "limit": int(limit or 8)})
        return (f"{note}\n" if note else "") + head + "\n" + listing

    def _read_experiment(self, run_id, nid: int, trials_arg=None) -> str:
        return self._delegate(run_id, "read_experiment", {"node_id": nid, "trials": trials_arg},
                              prefix=f"run {run_id} · ")

    def _read_logs(self, run_id, nid: int) -> str:
        return self._delegate(run_id, "read_logs", {"node_id": nid}, prefix=f"run {run_id} · ")

    def _trace_source(self, run_id, nid: int):
        """`(run_dir, state, spans_path, attempt)` for a node trace read, or a refusal SENTENCE.

        The three-line preamble both trace surfaces need — resolve the run, prove a trace was
        recorded at all, and settle the node's lifecycle generation. Returned as a tuple or a string
        so the two callers cannot come to disagree about which attempt they are reading (the map's
        anchors are only valid inside the generation the window then reads)."""
        rd = self._safe_dir(run_id)
        st = self._state(run_id)
        if rd is None or st is None:
            return f"(no such run: {run_id!r})"
        spans_path = rd / "spans.jsonl"
        if not spans_path.exists():
            return (f"(run {run_id} has no spans.jsonl — no agent trace was recorded. This run may "
                    "predate tracing, or ran with tracing off.)")
        attempt = getattr(st.nodes.get(nid), "attempt", 0)
        return rd, st, spans_path, (attempt if type(attempt) is int and attempt >= 0 else 0)

    def _read_trace(self, run_id, nid: int, stage: Optional[str] = None,
                    before: Optional[str] = None) -> str:
        """The node's agent trace as a linear, de-duplicated conversation. Reuses the SAME
        `build_conversation` projection the Web UI's Trace tab shows (so the assistant reads exactly
        what the human sees), rendered to text and bounded to `max_chars`.

        `before` is the F6 SEEK, and it is what makes that "exactly what the human sees" claim true
        again: the window is the newest `TRACE_CONVERSATION_SPAN_CAP` spans of one
        `(node_id, generation)`, so without an anchor everything older is unreachable at any
        parameter — measured on `runs/rubert-dr-0804` node 1 (14,507 spans over 3 h 50 m, 2,345
        inline repairs, all of them generation 0 because inline repair does not bump `Node.attempt`):
        74 % of that node could not be read, INCLUDING every early repair an operator asks "what
        happened in this node" to find out about. The HTTP routes gained `?before=` and an episode
        map; this surface is a sibling caller of the very same `full_spans_for_node` /
        `build_conversation` and did not. Anchors come from `read_run_trace_episodes`.

        An anchor this run's index cannot place is REFUSED, never degraded to the tail — the routes'
        rule (`_settle_window_anchor`), for the same reason: answering with the newest spans under an
        older episode's label is worse than answering nothing, and worse still for a reader that
        cannot see the label."""
        source = self._trace_source(run_id, nid)
        if isinstance(source, str):
            return source
        _rd, st, spans_path, attempt = source
        note = self._runs.source_note(run_id)   # a truncated log must not read as complete
        from looplab.events.span_index import get_index
        from looplab.events.traceview import (
            TRACE_CONVERSATION_SPAN_CAP, build_conversation, load_spans, settle_trace_anchor)
        try:
            index = get_index(spans_path)
            anchor = settle_trace_anchor(before) if str(before or "").strip() else None
            if str(before or "").strip() and (
                    anchor is None or index is None or not index.has_span(anchor)):
                return (f"(run {run_id} node #{nid}: {before!r} is not a step in this run's trace "
                        f"index, so the window cannot be placed on it — call "
                        f"`read_run_trace_episodes` for this node and use an episode's `anchor`.)")
            if index is not None:
                total = index.node_span_count(nid, generation=attempt)
                spans = index.full_spans_for_node(
                    nid, TRACE_CONVERSATION_SPAN_CAP, generation=attempt, before=anchor)
                convo = build_conversation(
                    st, spans, nid, total_spans=total,
                    span_cap=TRACE_CONVERSATION_SPAN_CAP,
                    # The node's build claims, resolved over the whole index — `spans` is a bounded
                    # window and the claiming span can fall outside it. Same reason as the route's:
                    # an anchor INSIDE the build legitimately ends before the `materialize_node` row
                    # that names it, and re-deriving from the window would then drop every row.
                    claimed_traces=index.node_build_traces(nid, generation=attempt),
                    _normalized=True)
            else:
                # Missing indexes are rare (the source existence was checked above). Preserve the
                # compatibility path, but apply the same attempt fence before the conversation cap.
                convo = build_conversation(
                    st, load_spans(spans_path), nid, generation=attempt, _normalized=True)
        except Exception as e:  # noqa: BLE001 — an unexpected hand-edited/I/O failure must soft-fail
            return f"(could not read trace: {e})"  # and never terminate the agent tool loop
        return ((f"{note}\n" if note else "")
                + _render_conversation(convo, run_id, nid, stage, self.max_chars, before=anchor))

    def _read_trace_episodes(self, run_id, nid: int, from_index=None, limit=None) -> str:
        """THE MAP of one node's trace — every episode, with none of their contents.

        The half that makes `before` usable: an anchor the reader cannot discover is not a control.
        Same derivation as `/nodes/{nid}/episodes` (`traceview.node_episodes` over the in-memory
        light index, no spans.jsonl bytes at all), so the map and the window it aims speak one
        vocabulary and describe one generation."""
        source = self._trace_source(run_id, nid)
        if isinstance(source, str):
            return source
        _rd, _st, spans_path, attempt = source
        from looplab.events.span_index import get_index
        from looplab.events.traceview import node_episodes
        try:
            index = get_index(spans_path)
            if index is None:
                return (f"(run {run_id} node #{nid}: this run's trace index is unavailable, so its "
                        "episodes cannot be mapped — read the trace without an anchor.)")
            payload = node_episodes(
                index.light_spans_for_node(nid, None, generation=attempt), nid,
                total_spans=index.node_span_count(nid, generation=attempt), _normalized=True)
        except Exception as e:  # noqa: BLE001 — same soft-fail contract as every tool here
            return f"(could not read episodes: {e})"
        note = self._runs.source_note(run_id)
        return ((f"{note}\n" if note else "")
                + _render_episodes(payload, run_id, nid, from_index, limit, self.max_chars))

