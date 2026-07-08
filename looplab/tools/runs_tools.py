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
from looplab.tools.run_tools import RunTools
from looplab.tools._base import fn_spec
from looplab.tools._runcache import RunStateCache

# A trace is a whole conversation, but the shared tool loop HEAD-truncates every tool result to 4000
# chars (agent.drive_tool_loop), so a larger budget would be silently cut there (losing the tail with
# no marker). Stay under that cap so our own truncation + the "narrow with `stage`" hint engage first.
_TRACE_CHARS = 3600


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
        # Traversal-guarded, (size, mtime)-fingerprinted fold cache — shared with SiblingRunTools.
        self._runs = RunStateCache(self.run_root)
        self._reader = RunTools(max_chars=max_chars)

    # RunsTools is not bound to a single run; accept bind_state for CompositeTools symmetry (no-op).
    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            fn_spec("list_runs",
                "List EVERY LoopLab run on this machine with its goal, phase, best metric, node count "
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
                "Read one experiment's EXECUTION LOGS: the captured stdout tail from training/eval and "
                "the FULL error/stderr (not the short failure summary). Use to see what a node printed "
                "while training, or why it failed, in full. Use run_id + node_id from read_run.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"}},
                ["run_id", "node_id"]),
            fn_spec("read_run_trace",
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
        return self._runs.run_ids()

    def _safe_dir(self, run_id: Optional[str]) -> Optional[Path]:
        return self._runs.safe_dir(run_id)

    def _state(self, run_id: Optional[str]) -> Optional[RunState]:
        return self._runs.state(run_id)

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
                f"{len(st.nodes)} nodes · best={digest.fmt_num(digest.node_metric(best)) if best else '—'}"
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
        except Exception as e:  # noqa: BLE001 — a torn/hand-edited spans.jsonl (e.g. a null `attributes`
            return f"(could not read trace: {e})"  # → AttributeError) must soft-fail, never crash the loop
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
            fn_spec("propose_run",
                "Propose a NEW LoopLab run for the user to launch (a run name + a task + optional "
                "settings). The user reviews an editable card and starts it — you do not launch it. "
                "Give EITHER an inline `task` object OR a `task_file` from the catalogue. Put "
                "model/max_nodes/etc. in `settings`. The task is VALIDATED before it becomes a card — an "
                "invalid one is bounced back to you to fix.\n"
                "A task is COMPOSABLE — there is NO `kind`. You describe what you HAVE and the engine "
                "infers the task. Always give `goal` and `direction` (EXACTLY \"max\" or \"min\"), then "
                "add the capability fields that apply:\n"
                "• `repo`: ABSOLUTE path to an editable codebase that EXISTS on disk — the agent may edit "
                "ANY file within it (protect exceptions with `protect:[...]`).\n"
                "• `dataset`: read-only data/model weights that live OUTSIDE the repo, as "
                "{\"<mount>\":\"<ABSOLUTE path>\"} (a bare path is mounted as ./dataset). They appear at "
                "./<mount> in the workdir. A repo that trains but has NO dataset mounts fails every node "
                "with file-not-found — DISCOVER the paths from the repo (README, configs, script defaults) "
                "+ the user's message, VERIFY each exists, and if a required path is unknown ASK in "
                "`reply` (never omit/guess).\n"
                "• `cmd`: HOW to run + score one experiment. Either a bare argv "
                "([\"python\",\"test.py\"]) or an object {command:[...], metric:{reader,...}, timeout}. "
                "`metric.reader` is one of stdout_json / stdout_regex / file_json / file_regex — HOW to "
                "read the printed metric. For stdout_json/file_json give `key` (the JSON field, e.g. "
                "\"recall\"); for stdout_regex/file_regex give `pattern` (a regex whose group 1 is the "
                "number, e.g. \"RECALL@100: ([0-9.]+)\") — NOT `key`; add `path` for the file_* readers. Set "
                "`reader:\"auto\"` ONLY for the narrow case where a training COMMAND already runs and you "
                "just need the agent to write the metric reader.\n"
                "• `kaggle`: a Kaggle / MLE-bench competition slug (the official grader scores a "
                "submission — no `cmd` needed).\n"
                "`cmd` IS A CONTRACT — the command that runs + the reader that reads its metric. It is the "
                "SCORING step, NOT the trainer: training is a SEPARATE stage the agent declares at run time "
                "(its `declare_stages` tool), and the engine runs it BEFORE `cmd`. WHAT the agent may EDIT "
                "is a SEPARATE, independent decision — `edit_surface` (globs the agent may edit; default = "
                "the WHOLE repo) minus `protect` (exceptions). The file `cmd` runs is NOT auto-protected, "
                "so decide edit-scope explicitly:\n"
                "  • `cmd` points at an OPERATOR-owned scorer the agent must not tamper with (e.g. the "
                "framework's test.py) → add that file to `protect` (the agent then adds a train stage before "
                "it; your protected cmd scores the freshly-trained model).\n"
                "  • `cmd` points at a file the agent must BUILD → leave it editable (a protected file can't "
                "be created).\n"
                "  • NO existing scorer anywhere → point `cmd` at an entrypoint the agent will BUILD "
                "(e.g. [\"python\",\"looplab_eval.py\"]) and leave it editable — a repo task ALWAYS "
                "carries a `cmd` (or metric.reader \"auto\"); say in the goal what it must train and "
                "print.\n"
                "In every case say each node must actually TRAIN a fresh model and score THAT model — never "
                "read a pre-existing checkpoint or a static results file (results_last.csv is a PRIOR run's "
                "output, not a score). If training happens, set `cmd.timeout` GENEROUSLY (seconds): training "
                "runs minutes-to-hours but the default is 600s, which SIGKILLs it mid-first-epoch into an "
                "undertrained model — size it to the full schedule (often 7200-14400s).\n"
                "OPTIONAL fields (the engine honors them — reach for them when the task needs it): "
                "`edit_surface`:[globs] restricts what the agent may edit (default: the WHOLE repo); "
                "`cmd.setup`:[argv] runs before each eval (e.g. pip install -r requirements.txt); "
                "`cmd.profiles`:{smoke:{overrides,timeout},full:{…}} gives a cheap search eval + a full "
                "confirm eval; `params`:{name:[lo,hi]} + a `%params%` token in a command tunes numeric "
                "hyperparameters with NO code edit; `editables`:[{name,path,surface}] mounts several "
                "editable repos. Per-source DATA permissions: a `dataset`/`data` value may be an object "
                "{path, mount(read-only symlink vs copy-in), edit(may edit the original — default no), "
                "copy_modify, preprocess, extend} — default is read-only with copy/preprocess/extend allowed, "
                "so the agent can derive/augment a training set but not touch the original.",
                {"run_id": {"type": "string", "description": "short kebab-case name you invent"},
                 "task": {"type": "object", "description": "composable inline task: goal + direction + the fields you have (repo / dataset / cmd{command|stages,metric:{reader,key},timeout} / kaggle). No `kind`."},
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
        # VALIDATE before proposing so the card the user sees is actually launchable — an invalid spec
        # (e.g. a repo task with no `eval` and no `onboard`) is bounced BACK to you to fix here, instead
        # of failing only when the user clicks Start (which spawns an engine that dies with no events).
        if task:
            try:
                from looplab.adapters.tasks import validate_task
                validate_task(task)
            except Exception as e:  # noqa: BLE001
                return (f"(NOT proposed — the task is INVALID: {e}\nFix it and call propose_run again. "
                        "A repo task MUST carry a `cmd` {command|stages, metric:{reader,key}} — point it "
                        "at a file the agent will BUILD if no scorer exists — or set metric.reader "
                        "\"auto\"; `repo` must be an ABSOLUTE path that exists.)")
        spec = {"run_id": rid, "task": task or {}, "task_file": task_file,
                "settings": args.get("settings") if isinstance(args.get("settings"), dict) else {},
                "rationale": str(args.get("rationale") or "")}
        self.proposals.append(spec)
        what = (task.get("kind") if task else None) or task_file or "a task"
        return (f"(proposed run '{rid}' ({what}) — shown to the user as a launch card; they will start "
                "it. Tell them what you proposed.)")


class RunControlTools:
    """Lets the assistant DRIVE an existing run's lifecycle — finalize, stop, resume, reset a node,
    delete a node, or delete the whole run — by writing the control event / editing the log the way the
    UI does. Mutating: every verb goes through `decide(mode, ...)` + the injected `approver` (a UI
    confirm-card), so it's denied in read-only `plan` mode, asks in default/acceptEdits, and runs inline
    only in `auto`. Destructive edits (delete node/run) additionally REFUSE while the engine is live —
    the engine is the sole writer of events.jsonl, so rewriting it under a live one would corrupt it."""

    def __init__(self, run_root, alive_fn: Optional[Callable[[Path], bool]] = None,
                 mode: str = "plan", approver: Optional[Callable] = None):
        self.run_root = Path(run_root)
        self.alive_fn = alive_fn
        self.mode = mode
        self.approver = approver

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        return [
            fn_spec("finalize_run",
                "Finalize a run: stop it AND wrap up (final report + cross-run lessons + cost roll-up). "
                "Use to END a run cleanly. Takes effect while the engine is running (it reads the event).",
                {"run_id": {"type": "string"}}, ["run_id"]),
            fn_spec("stop_run",
                "Freeze a run (pause, NO wrap-up) — resumable later. Use to PAUSE without finalizing.",
                {"run_id": {"type": "string"}}, ["run_id"]),
            fn_spec("resume_run",
                "Mark a stopped/finished run to resume. (Records the resume intent; if no engine is "
                "attached, the user still starts it from the UI Resume button.)",
                {"run_id": {"type": "string"}}, ["run_id"]),
            fn_spec("reset_node",
                "Re-run an existing node IN PLACE from a stage (no new node): 'eval' re-scores (keep the "
                "code), 'implement' re-runs only the Developer (keep the idea), 'propose' is a full redo. "
                "Applied on the next resume.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "stage": {"type": "string", "enum": ["propose", "implement", "eval"]}},
                ["run_id", "node_id"]),
            fn_spec("delete_node",
                "DELETE a node AND its descendants from a run (removes their events, spans and workdirs; "
                "the best node is recomputed). DESTRUCTIVE + backs the log up. Refuses while the engine "
                "is live — stop the run first.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"}},
                ["run_id", "node_id"]),
            fn_spec("delete_run",
                "DELETE an entire run and all its artifacts. DESTRUCTIVE + irreversible. Refuses while "
                "the engine is live — stop the run first.",
                {"run_id": {"type": "string"}}, ["run_id"]),
        ]

    # ------------------------------------------------------------------ helpers
    def _rd(self, run_id) -> Optional[Path]:
        # Resolve a run_id to its dir, refusing traversal (must be a direct, existing child of run-root).
        rid = str(run_id or "").strip().strip("/")
        if not rid or "/" in rid or "\\" in rid or rid.startswith("."):
            return None
        rd = self.run_root / rid
        return rd if (rd / "events.jsonl").exists() else None

    def _gate(self, name: str, rid: str, verb: str) -> Optional[str]:
        # Returns a "declined/disabled" string to short-circuit, or None to proceed.
        from looplab.tools.perm_modes import decide
        d = decide(self.mode, "run_control")
        if d == "deny":
            return "(run control is disabled in read-only plan mode — switch to default/acceptEdits/auto.)"
        if d == "ask":
            action = {"tool": name, "tool_kind": "run_control", "label": f"{name} {rid}",
                      "verb": verb, "preview": f"{name}({rid})"}
            verdict = str(self.approver(action) or "deny") if self.approver else "deny"
            if not verdict.startswith("allow"):
                return f"(declined by the user: {name} {rid})"
        return None

    def _live(self, rd: Path) -> bool:
        """Is a run's engine actively writing its log? The flock probe is primary, but on FUSE / NFS / S3
        mounts flock can wrongly report "not live" — so ALSO trip on a fresh-write backstop: a run that
        is neither paused nor finished AND whose events.jsonl was appended in the last 30s is treated as
        live (a running engine is the sole writer and appends constantly). This gates the destructive
        delete_node/delete_run so they can't rewrite the log out from under a live engine even when flock
        lies. Conservative: a genuinely crashed run (stale mtime) still deletes."""
        try:
            if self.alive_fn and self.alive_fn(rd):
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            import time as _time
            from looplab.events.eventstore import EventStore
            from looplab.events.replay import fold
            evp = rd / "events.jsonl"
            st = fold(EventStore(evp).read_all())
            if st.finished or st.paused:
                return False                              # a settled run is safe to act on
            return (_time.time() - evp.stat().st_mtime) < 30.0   # recent write on an unsettled run -> live
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ dispatch
    def execute(self, name: str, args: dict) -> str:
        args = args or {}
        rid = str(args.get("run_id") or "").strip()
        rd = self._rd(rid)
        if rd is None:
            return f"(no such run: {rid!r})"
        try:
            if name in ("finalize_run", "stop_run", "resume_run"):
                return self._control(name, rid, rd)
            if name == "reset_node":
                return self._reset_node(rid, rd, args)
            if name == "delete_node":
                return self._delete_node(rid, rd, args)
            if name == "delete_run":
                return self._delete_run(rid, rd)
        except Exception as e:  # noqa: BLE001 — a tool error must never crash the loop
            return f"(tool error in {name}: {e})"
        return f"(unknown tool: {name})"

    def _control(self, name: str, rid: str, rd: Path) -> str:
        from looplab.events.eventstore import EventStore
        from looplab.events.types import EV_PAUSE, EV_RESUME, EV_RUN_ABORT
        etype, data, verb = {
            "finalize_run": (EV_RUN_ABORT, {"reason": "finalized"}, f"finalize run {rid} (stop + wrap up)"),
            "stop_run": (EV_PAUSE, {}, f"stop (freeze) run {rid}"),
            "resume_run": (EV_RESUME, {}, f"resume run {rid}"),
        }[name]
        blocked = self._gate(name, rid, verb)
        if blocked:
            return blocked
        EventStore(rd / "events.jsonl").append(etype, data)
        tail = (" — takes effect while the engine is running; if it isn't, start it from the UI."
                if name != "resume_run" else " — start it from the UI Resume if no engine is attached.")
        return f"({name.split('_')[0]} recorded for {rid}{tail})"

    def _reset_node(self, rid: str, rd: Path, args: dict) -> str:
        from looplab.events.eventstore import EventStore
        from looplab.events.replay import fold
        from looplab.events.types import EV_NODE_RESET
        try:
            nid = int(args.get("node_id"))
        except (TypeError, ValueError):
            return "(reset_node needs an integer node_id)"
        stage = str(args.get("stage") or "eval").strip()
        if not stage or len(stage) > 64:      # propose|implement|eval OR an eval-pipeline stage name
            return "(stage must be a non-empty stage name)"
        if nid not in fold(EventStore(rd / "events.jsonl").read_all()).nodes:
            return f"(no node #{nid} in {rid})"
        blocked = self._gate("reset_node", rid, f"reset node #{nid} of {rid} from {stage}")
        if blocked:
            return blocked
        EventStore(rd / "events.jsonl").append(EV_NODE_RESET, {"node_id": nid, "from_stage": stage})
        return f"(node #{nid} of {rid} queued to re-run from {stage} — applied on the next resume)"

    def _delete_node(self, rid: str, rd: Path, args: dict) -> str:
        import json
        import shutil
        from looplab.events.eventstore import EventStore
        from looplab.events.replay import fold
        try:
            nid = int(args.get("node_id"))
        except (TypeError, ValueError):
            return "(delete_node needs an integer node_id)"
        if self._live(rd):
            return f"(run {rid} is LIVE — stop it first; the engine is the sole writer of its log)"
        st = fold(EventStore(rd / "events.jsonl").read_all())
        if nid not in st.nodes:
            return f"(no node #{nid} in {rid})"
        # The node AND every descendant (deleting a node alone would orphan its children's parent links).
        subtree = {nid}
        changed = True
        while changed:
            changed = False
            for n in st.nodes.values():
                if n.id not in subtree and any(p in subtree for p in n.parent_ids):
                    subtree.add(n.id)
                    changed = True
        blocked = self._gate("delete_node", rid, f"delete node(s) {sorted(subtree)} of {rid}")
        if blocked:
            return blocked
        evp = rd / "events.jsonl"
        recs = [json.loads(x) for x in evp.read_text("utf-8").splitlines() if x.strip()]
        kept = [r for r in recs
                if not (isinstance(r.get("data"), dict) and r["data"].get("node_id") in subtree)]
        shutil.copy(evp, rd / f"events.jsonl.bak-del{nid}")     # recoverable backup
        evp.write_text("".join(json.dumps(r) + "\n" for r in kept), "utf-8")
        sp = rd / "spans.jsonl"
        if sp.exists():
            skept = [x for x in sp.read_text("utf-8").splitlines()
                     if x.strip() and (json.loads(x).get("attributes") or {}).get("node_id") not in subtree]
            sp.write_text("".join(x + "\n" for x in skept), "utf-8")
        for d in subtree:
            shutil.rmtree(rd / "nodes" / f"node_{d}", ignore_errors=True)
        st2 = fold(EventStore(evp).read_all())
        broken = sorted({p for n in st2.nodes.values() for p in n.parent_ids if p not in st2.nodes})
        return (f"(deleted node(s) {sorted(subtree)} from {rid}; {len(st2.nodes)} nodes left, "
                f"best now #{st2.best_node_id}, broken parent links: {broken or 'none'}. "
                f"Backup: events.jsonl.bak-del{nid})")

    def _delete_run(self, rid: str, rd: Path) -> str:
        import shutil
        if self._live(rd):
            return f"(run {rid} is LIVE — stop it first before deleting)"
        blocked = self._gate("delete_run", rid, f"DELETE the entire run {rid} (irreversible)")
        if blocked:
            return blocked
        shutil.rmtree(rd, ignore_errors=True)
        return f"(deleted run {rid} and all its artifacts)"
