"""Cross-run introspection tools for the assistant (ADR-7 tool protocol).

Where `RunTools` reads the ONE live run bound to it and `SiblingRunTools` reads other runs of the
SAME task, `RunsTools` gives the general-purpose assistant a view over EVERY run on this machine —
so it can reference an existing run, report which ones are live, and read one in detail before
steering or fixing it. Same `.specs()`/`.execute()` shape as the other providers; every `execute`
returns a string and soft-fails (a junk tool call must never crash the loop).

Runs are folded from disk on demand and cached by each event log's (size, mtime) fingerprint, so
repeated turns don't re-fold unchanged runs. Liveness (`engine_running`) is injected as a callable
by the server (`_engine_alive`) to avoid a circular import and to reuse the one race-free lock probe.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from looplab.events import digest
from looplab.core.models import RunState
from looplab.tools.run_tools import RunTools, _fn

_TRACE_CHARS = 12000      # a trace is a whole conversation — give it the same larger budget as logs


def _render_conversation(convo: dict, run_id, nid, stage: Optional[str], max_chars: int) -> str:
    """Render `traceview.build_conversation` output as a readable linear thread. One block per stage
    (create_node / evaluate / …); within a stage, requests show the prompt, generations show
    thinking + output + which tools were called, tool turns show input→output. Filtered to one stage
    when `stage` is given (substring match on its label). Bounded to a generous trace budget."""
    stages = convo.get("stages") or []
    if stage:
        s = str(stage).lower()
        stages = [st for st in stages if s in str(st.get("label") or "").lower()]
    if not stages:
        which = f" matching {stage!r}" if stage else ""
        return f"(run {run_id} node #{nid}: no trace stages{which} recorded)"
    lines = [f"run {run_id} · node #{nid} · trace ({len(stages)} stage(s)):"]
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


class RunsTools:
    """Read-only view over ALL runs under the run-root (for the assistant)."""

    def __init__(self, run_root, alive_fn: Optional[Callable[[Path], bool]] = None,
                 max_chars: int = 3500):
        self.run_root = Path(run_root)
        self.alive_fn = alive_fn
        self.max_chars = max_chars
        self._cache: dict[str, tuple] = {}     # run_id -> (sig, RunState)
        self._reader = RunTools(max_chars=max_chars)

    # RunsTools is not bound to a single run; accept bind_state for CompositeTools symmetry (no-op).
    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            _fn("list_runs",
                "List EVERY LoopLab run on this machine with its goal, phase, best metric, node count "
                "and whether its engine is LIVE right now. Use to reference an existing run, see what "
                "is running, or pick one to inspect/steer.",
                {"only_live": {"type": "boolean",
                               "description": "if true, list only runs whose engine is currently live"}}),
            _fn("read_run",
                "Read ONE run in detail: goal, direction, phase, best experiment and its top "
                "experiments. Use a run_id from list_runs before steering or fixing it.",
                {"run_id": {"type": "string"},
                 "sort": {"type": "string", "enum": ["best", "worst", "recent"]},
                 "limit": {"type": "integer"}},
                ["run_id"]),
            _fn("read_run_experiment",
                "Read one experiment of a run in full detail (params, metric, robustness, rationale, "
                "failure, sweep trials). Use run_id + node_id from read_run.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "trials": {"type": "string", "description": "how many sweep trials: a number, or 'all'"}},
                ["run_id", "node_id"]),
            _fn("read_run_logs",
                "Read one experiment's EXECUTION LOGS: the captured stdout tail from training/eval and "
                "the FULL error/stderr (not the short failure summary). Use to see what a node printed "
                "while training, or why it failed, in full. Use run_id + node_id from read_run.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"}},
                ["run_id", "node_id"]),
            _fn("read_run_trace",
                "Read one experiment's AGENT TRACE as a linear, de-duplicated conversation: the "
                "system+user request once per sub-loop, then each LLM generation's reasoning + output "
                "and the tools it called, interleaved with tool results. This is the full train of "
                "thought that produced the node. Use run_id + node_id from read_run.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "stage": {"type": "string", "description": "optional: only the stage whose label "
                                                            "contains this text (e.g. 'repair')"}},
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
                                        args.get("stage"))
            return f"(unknown tool: {name})"
        except (KeyError, TypeError, ValueError, ArithmeticError) as e:
            return f"(tool error: {e})"

    # --- machine-readable summaries (also reused by the /api/assistant run-ref expansion) ------------
    def summaries(self, only_live: bool = False) -> list[dict]:
        """Structured per-run summary for EVERY run (used by the tool AND by @run-mention expansion)."""
        out = []
        for rid in self._run_ids():
            st = self._state(rid)
            if st is None:
                continue
            live = self._alive(rid)
            if only_live and not live:
                continue
            best = st.best()
            out.append({
                "run_id": rid, "goal": st.goal or st.task_id, "direction": st.direction,
                "phase": ("finished" if st.finished else ("live" if live else "idle")),
                "nodes": len(st.nodes),
                "best_metric": (digest.node_metric(best) if best else None),
                "best_node_id": (best.id if best else None),
                "engine_running": live, "finished": st.finished,
            })
        return out

    # --- internals -----------------------------------------------------------
    def _run_ids(self) -> list[str]:
        try:
            return sorted(p.name for p in self.run_root.iterdir()
                          if p.is_dir() and (p / "events.jsonl").exists())
        except OSError:
            return []

    def _safe_dir(self, run_id: Optional[str]) -> Optional[Path]:
        if not run_id:
            return None
        rd = (self.run_root / str(run_id)).resolve()
        if rd.parent != self.run_root.resolve():
            return None
        if not (rd / "events.jsonl").exists():
            return None
        return rd

    @staticmethod
    def _sig(rd: Path):
        try:
            s = (rd / "events.jsonl").stat()
            return (s.st_size, int(s.st_mtime))
        except OSError:
            return (0, 0)

    def _state(self, run_id: Optional[str]) -> Optional[RunState]:
        rd = self._safe_dir(run_id)
        if rd is None:
            return None
        sig = self._sig(rd)
        hit = self._cache.get(str(run_id))
        if hit and hit[0] == sig:
            return hit[1]
        from looplab.events.eventstore import iter_jsonl
        from looplab.core.models import Event
        from looplab.events.replay import fold
        try:
            st = fold(Event(**o) for o in iter_jsonl(rd / "events.jsonl"))
        except (OSError, ValueError, TypeError):
            return None
        self._cache[str(run_id)] = (sig, st)
        return st

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
            best = digest._fmt_num(r["best_metric"]) if r["best_metric"] is not None else "—"
            lines.append(f"{r['run_id']}: {str(r['goal'])[:70]} · best={best} ({r['direction']}) · "
                         f"{r['nodes']} nodes · {r['phase']}{live}")
        return f"{len(lines)} run(s):\n" + "\n".join(lines)

    def _read_run(self, run_id, sort, limit) -> str:
        st = self._state(run_id)
        if st is None:
            return f"(no such run: {run_id!r})"
        best = st.best()
        live = self._alive(str(run_id))
        head = (f"run {run_id} · goal: {st.goal or st.task_id} · direction={st.direction} · "
                f"phase={'finished' if st.finished else ('live' if live else 'idle')} · "
                f"{len(st.nodes)} nodes · best={digest._fmt_num(digest.node_metric(best)) if best else '—'}"
                + (f" (#{best.id})" if best else ""))
        self._reader.bind_state(st, None)
        listing = self._reader.execute("list_experiments",
                                       {"sort": sort or "best", "limit": int(limit or 8)})
        return head + "\n" + listing

    def _read_experiment(self, run_id, nid: int, trials_arg=None) -> str:
        st = self._state(run_id)
        if st is None:
            return f"(no such run: {run_id!r})"
        self._reader.bind_state(st, None)
        return f"run {run_id} · " + self._reader.execute(
            "read_experiment", {"node_id": nid, "trials": trials_arg})

    def _read_logs(self, run_id, nid: int) -> str:
        st = self._state(run_id)
        if st is None:
            return f"(no such run: {run_id!r})"
        self._reader.bind_state(st, None)
        return f"run {run_id} · " + self._reader.execute("read_logs", {"node_id": nid})

    def _read_trace(self, run_id, nid: int, stage: Optional[str] = None) -> str:
        """The node's agent trace as a linear, de-duplicated conversation. Reuses the SAME
        `build_conversation` projection the Web UI's Trace tab shows (so the assistant reads exactly
        what the human sees), rendered to text and bounded to `max_chars`."""
        rd = self._safe_dir(run_id)
        st = self._state(run_id)
        if rd is None or st is None:
            return f"(no such run: {run_id!r})"
        from looplab.serve.traceview import build_conversation, load_spans
        spans_path = rd / "spans.jsonl"
        if not spans_path.exists():
            return (f"(run {run_id} has no spans.jsonl — no agent trace was recorded. This run may "
                    "predate tracing, or ran with tracing off.)")
        try:
            convo = build_conversation(st, load_spans(spans_path), nid)
        except (OSError, ValueError, TypeError) as e:
            return f"(could not read trace: {e})"
        return _render_conversation(convo, run_id, nid, stage, self.max_chars)


class RunLauncherTools:
    """Lets the assistant PROPOSE a new run (the evolution of the Genesis 'New run' flow). It does not
    launch anything itself — it records an editable spec that the UI shows as a launch card, and the
    user starts it via the existing /api/start. So run-creation is one assistant capability rather than
    a separate modal."""

    def __init__(self):
        self.proposals: list[dict] = []

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            _fn("propose_run",
                "Propose a NEW LoopLab run for the user to launch (a run name + a task + optional "
                "settings). The user reviews an editable card and starts it — you do not launch it. "
                "Give EITHER an inline `task` object (with a `kind`: dataset/repo/mlebench_real/…) OR a "
                "`task_file` from the catalogue. Put model/max_nodes/etc. in `settings`.",
                {"run_id": {"type": "string", "description": "short kebab-case name you invent"},
                 "task": {"type": "object", "description": "inline task object with a `kind`"},
                 "task_file": {"type": "string", "description": "a catalogue task path (alternative to task)"},
                 "settings": {"type": "object", "description": "engine overrides, e.g. {\"llm_model\":..,\"max_nodes\":..}"},
                 "rationale": {"type": "string"}},
                ["run_id"]),
        ]

    def execute(self, name: str, args: dict) -> str:
        if name != "propose_run":
            return f"(unknown tool: {name})"
        args = args or {}
        rid = str(args.get("run_id") or "").strip()
        if not rid:
            return "(propose_run needs a run_id)"
        task = args.get("task") if isinstance(args.get("task"), dict) else None
        task_file = args.get("task_file") or None
        if not task and not task_file:
            return "(propose_run needs an inline `task` object with a kind, or a `task_file`)"
        spec = {"run_id": rid, "task": task or {}, "task_file": task_file,
                "settings": args.get("settings") if isinstance(args.get("settings"), dict) else {},
                "rationale": str(args.get("rationale") or "")}
        self.proposals.append(spec)
        what = (task.get("kind") if task else None) or task_file or "a task"
        return (f"(proposed run '{rid}' ({what}) — shown to the user as a launch card; they will start "
                "it. Tell them what you proposed.)")
