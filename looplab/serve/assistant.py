"""The general-purpose assistant: a persistent chat agent embedded in the Web UI that can read the
machine, reference/steer runs, and (in later phases) write files, run commands and edit LoopLab
itself. It is the evolution of the pre-run Genesis chat into a full assistant.

This module is the DEPENDENCY-LIGHT core: a `SessionStore` (append-only per-session transcripts under
`<run_root>/assistant/`) and a `run_turn` that assembles a toolset and drives the shared
`agent.drive_tool_loop`. The FastAPI server wires the LLM client, run-liveness probe and settings in;
keeping those injected makes the whole thing unit-testable with a scripted fake client (see
`tests/test_assistant_endpoint.py`), exactly like `genesis`/`server` are tested today.

Permission MODES (Claude-Code-style) are honored by the tool PROVIDERS, not here: `run_turn` just
passes the mode down. In P0 the toolset is read-only (`plan` mode); write/shell/git providers and the
pause-resume approval flow arrive in P1.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from looplab.core.atomicio import atomic_write_text, best_effort_fsync
from looplab.events.eventstore import iter_jsonl

# The LoopLab source tree (…/looplab/looplab/assistant.py -> repo root two levels up). The assistant
# may read (and, in later phases, edit) the code that runs it — this is what "fix LoopLab itself"
# needs — so the repo root is always an allowed root alongside the run-root and the user's home.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Permission modes, mirroring Claude Code. `plan` is read-only (the safe default); the mutating modes
# are honored by the write/shell/git providers in P1. Re-exported from the single source of truth
# (tools/perm_modes.py — the decision table the providers enforce) so the mode SET can never drift
# between the session layer and the enforcing providers.
from looplab.tools.perm_modes import DEFAULT_MODE, MODES, normalize_mode  # noqa: F401


# --------------------------------------------------------------------------- session persistence
class SessionStore:
    """Append-only assistant sessions under `<run_root>/assistant/<sid>/`.

    `meta.json` holds {id,title,created,updated,parent,mode}; `messages.jsonl` holds one turn per line
    ({role,content,ts,...}). Append is single-writer + best-effort fsync like the run chat log. The
    `assistant` dir sits beside runs but is a RESERVED id (server refuses a run named `assistant`), so
    it never collides with a real run."""

    def __init__(self, run_root):
        self.dir = Path(run_root) / "assistant"
        self._append_lock = threading.Lock()   # serialize appends so a large turn can't interleave
        # Serialize meta read-modify-write so concurrent writers (a Share click landing while a turn's
        # reply persist bumps `updated`, or two tabs switching mode) can't each read the same meta and
        # clobber the other's field — losing a `shared` flag / title / mode.
        self._meta_lock = threading.RLock()

    def _sdir(self, sid: str) -> Path:
        d = (self.dir / sid).resolve()
        if d.parent != self.dir.resolve():        # path-traversal guard (sid must be a direct child)
            raise ValueError("bad session id")
        return d

    def _meta_path(self, sid: str) -> Path:
        return self._sdir(sid) / "meta.json"

    def _msgs_path(self, sid: str) -> Path:
        return self._sdir(sid) / "messages.jsonl"

    def create(self, title: str = "", parent: Optional[str] = None, mode: str = DEFAULT_MODE,
               *, now: Optional[float] = None) -> dict:
        sid = secrets.token_hex(8)
        d = self._sdir(sid)
        d.mkdir(parents=True, exist_ok=True)
        ts = time.time() if now is None else now
        meta = {"id": sid, "title": (title or "New chat")[:120], "created": ts, "updated": ts,
                "parent": parent, "mode": normalize_mode(mode)}
        atomic_write_text(self._meta_path(sid), json.dumps(meta))
        return meta

    def _read_meta(self, sid: str) -> Optional[dict]:
        try:
            return json.loads(self._meta_path(sid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def update_meta(self, sid: str, **fields) -> Optional[dict]:
        # Read-modify-write under the meta lock so concurrent updates don't drop each other's fields.
        with self._meta_lock:
            meta = self._read_meta(sid)
            if meta is None:
                return None
            meta.update({k: v for k, v in fields.items() if v is not None})
            meta["updated"] = fields.get("updated", time.time())
            atomic_write_text(self._meta_path(sid), json.dumps(meta))
            return meta

    def list(self) -> list[dict]:
        if not self.dir.exists():
            return []
        out = []
        for d in self.dir.iterdir():
            if d.is_dir():
                m = self._read_meta(d.name)
                if m:
                    out.append(m)
        out.sort(key=lambda m: m.get("updated", 0), reverse=True)
        return out

    def messages(self, sid: str) -> list[dict]:
        try:
            return list(iter_jsonl(self._msgs_path(sid)))
        except OSError:
            return []

    def get(self, sid: str) -> Optional[dict]:
        meta = self._read_meta(sid)
        if meta is None:
            return None
        return {"meta": meta, "messages": self.messages(sid)}

    def append(self, sid: str, turn: dict) -> None:
        d = self._sdir(sid)
        if not d.exists():
            raise ValueError("no such session")
        line = {**turn, "ts": turn.get("ts", time.time())}
        # A large turn (attached-file contents) exceeds the buffer and becomes multiple write() syscalls
        # that can interleave with a concurrent append → a corrupt mid-file line, which iter_jsonl stops
        # at, silently dropping every later turn on the next read. Serialize appends to prevent that.
        with self._append_lock:
            with open(self._msgs_path(sid), "ab") as f:
                f.write((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
                try:
                    best_effort_fsync(f.fileno())
                except OSError:
                    pass
        self.update_meta(sid, updated=line["ts"])

    def append_if_len(self, sid: str, turn: dict, expected_len: int) -> bool:
        """Append `turn` ONLY if the transcript currently holds exactly `expected_len` messages —
        the check and the write happen atomically under the append lock. Returns True if appended,
        False if a concurrent turn changed the length in between (so a late or cancelled reply can't
        interleave into a newer turn's transcript, e.g. u1,u2,a1,a2). Closes the TOCTOU window a
        separate 'count then append' left open."""
        d = self._sdir(sid)
        if not d.exists():
            return False
        line = {**turn, "ts": turn.get("ts", time.time())}
        with self._append_lock:
            try:
                cur = sum(1 for _ in iter_jsonl(self._msgs_path(sid)))
            except OSError:
                cur = -1
            if cur != expected_len:
                return False
            with open(self._msgs_path(sid), "ab") as f:
                f.write((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
                try:
                    best_effort_fsync(f.fileno())
                except OSError:
                    pass
        self.update_meta(sid, updated=line["ts"])
        return True

    def fork(self, sid: str, *, now: Optional[float] = None) -> Optional[dict]:
        """Clone a session's transcript into a fresh child session (OpenCode-style fork)."""
        src = self.get(sid)
        if src is None:
            return None
        child = self.create(title="Fork of " + src["meta"].get("title", "chat"),
                            parent=sid, mode=src["meta"].get("mode", DEFAULT_MODE), now=now)
        for turn in src["messages"]:
            self.append(child["id"], turn)
        # `append` stamped `updated` with each copied turn's OLD ts; restore it to the fork's creation
        # time so a fresh fork sorts to the TOP of the list (not buried by its ancestors' timestamps).
        return self.update_meta(child["id"], updated=child["updated"]) or child


# --------------------------------------------------------------------------- system prompt + toolset
def system_prompt(mode: str, *, repo_root: Path = REPO_ROOT, knowledge_dir: str | None = None) -> str:
    mode = normalize_mode(mode)
    mode_line = {
        "plan": "MODE=plan: you are READ-ONLY. You may inspect files and runs and PROPOSE changes in "
                "prose, but you cannot write files or run commands. Say what you would do.",
        "default": "MODE=default: read-only tools execute immediately; every mutating action (writing "
                   "a file, running a command, a git mutation, run control) pauses on a confirm card "
                   "and runs only if the user approves it.",
        "acceptEdits": "MODE=acceptEdits: file edits apply immediately; commands, git and launching a "
                       "run are still proposed for approval.",
        "auto": "MODE=auto: you may write files and run commands directly without asking. Be careful.",
    }[mode]
    return (
        "You are the LoopLab assistant — a capable coding/research agent embedded in the LoopLab Web "
        "UI. LoopLab is an autonomous ML research engine; you help the user do ANYTHING: understand and "
        "steer runs, work in their repos and data, and edit/repair LoopLab's OWN codebase.\n\n"
        + mode_line + "\n\n"
        f"The LoopLab source tree is at {repo_root}. You have read-only tools to inspect this machine "
        "(list_dir, read_file, find_files, grep) and to view runs (list_runs, read_run, read_run_experiment). "
        "To read what a node actually DID, you can go deeper: `read_run_logs` returns a node's captured "
        "stdout tail from training/eval plus its full error/stderr, and `read_run_trace` returns the "
        "node's agent trace as a linear conversation (the LLM's reasoning, outputs and tool calls that "
        "produced it). Ground your answers in what you actually read — inspect before you assert. When "
        "the user refers to a run, use list_runs/read_run to find and read it.\n"
        + ("" if mode == "plan" else
           "You can also drive a run's LIFECYCLE directly: finalize_run (stop + wrap-up: report, "
           "lessons, cost), stop_run (freeze, no wrap-up), resume_run, reset_node (re-run a node in "
           "place from a stage), and the DESTRUCTIVE delete_node / delete_run. And you can adjust a "
           "LIVE run's settings: extend_budget (more nodes/time — REOPENS a finished run so the budget "
           "is used), set_directive (a standing steer for the agents, e.g. 'use only sklearn'), and "
           "set_trust_gate (audit/gate/block). Each is gated by your mode and may raise a confirm card.\n")
        + "When the user wants to START a new autonomous-ML run, call `propose_run` with a run name + an "
        "inline COMPOSABLE `task` (goal + direction + the fields you have: repo / dataset / cmd / "
        "kaggle — there is NO `kind` field, the engine infers the task from what you describe) or a "
        "catalogue `task_file`, plus any settings (model, max_nodes) implied by their words — they get "
        "an editable launch card.\n"
        + (f"There is a shared KNOWLEDGE BASE at {knowledge_dir} — markdown notes that EVERY autonomous "
           "run's Researcher searches (via kb_search) to reuse past findings. When the user shares "
           "experiment results, lessons, recipes, or domain facts worth keeping across runs (e.g. an "
           "attached file describing past experiments and their metrics), DISTILL the essentials and "
           "save them with the `remember` tool so future runs benefit. `remember` works in EVERY mode "
           "(it's a safe, scoped append to the KB) — you don't need write mode for it.\n"
           if knowledge_dir else "")
        + "Be concise and concrete; use Markdown. When you have the answer, call `final_answer` exactly "
        "once with your reply.")


# @-mentions: `@run:<id>` and `@file:<path>` in the user's message are expanded (server-side, before
# the model sees it) into grounding blocks — the OpenCode/Claude-Code pattern. The UI ALSO renders a
# live inline card for each `@run:<id>`, so a running run shows up right in the chat.
_MENTION = re.compile(r"""@(run|file):([^\s\])"'>}]+)""")


def expand_mentions(text: str, run_root, *, alive_fn: Optional[Callable] = None, roots=()) -> tuple:
    """Return (expanded_text, refs). For each @run:<id> append a run summary; for each @file:<path>
    append the file's contents (path/secret-gated via the read scout). `refs` lists what was
    referenced so the caller/UI can render live cards. Unknown/refused mentions are left as-is."""
    blocks, refs = [], []
    for m in _MENTION.finditer(text or ""):
        kind, raw = m.group(1), m.group(2).rstrip(".,;:!?)")
        if kind == "run":
            from looplab.tools.runs_tools import RunsTools
            summary = RunsTools(run_root, alive_fn=alive_fn)._read_run(raw, "best", 6)
            if not summary.startswith("(no such run"):
                blocks.append(f"[@run:{raw}]\n{summary}")
                refs.append({"type": "run", "id": raw})
        elif kind == "file":
            from looplab.tools.reposcout import RepoScoutTools
            body = RepoScoutTools(list(roots) or [Path.home(), REPO_ROOT, Path(run_root)])._read_file(raw)
            # Skip refused/unreadable files (outside roots, secret, missing) instead of embedding the
            # refusal string in the prompt — mirrors the @run branch and the docstring's promise. The
            # scout returns a single-line "(…reason…)" on refusal; a real file is multi-line or not so
            # wrapped, so this only drops genuine refusals.
            _b = body.strip()
            if not (_b.startswith("(") and _b.endswith(")") and "\n" not in _b):
                blocks.append(f"[@file:{raw}]\n```\n{body}\n```")
                refs.append({"type": "file", "path": raw})
    expanded = text if not blocks else (text + "\n\n--- Referenced context ---\n" + "\n\n".join(blocks))
    return expanded, refs


def _emit_spec() -> dict:
    return {"type": "function", "function": {
        "name": "final_answer",
        "description": "Provide your final reply to the user (Markdown). Call this exactly once when "
                       "you are done using tools.",
        "parameters": {"type": "object",
                       "properties": {"reply": {"type": "string"}}, "required": ["reply"]}}}


def build_tools(run_root, alive_fn: Optional[Callable] = None, mode: str = DEFAULT_MODE, *,
                approver: Optional[Callable] = None, trust_mode: str = "trusted_local", extra_roots=(),
                client=None, subagents: bool = False, mcp: bool = False, settings=None,
                on_todos: Optional[Callable] = None, cancel_check: Optional[Callable] = None):
    """The assistant's toolset. Read tools (filesystem scout + cross-run introspection) are present in
    EVERY mode; the mutating write/shell/git providers are added only when the mode allows mutation
    (plan is read-only), mirroring "deny drops the tool from the schema". Each mutating provider gets
    the mode + the injected `approver` (which blocks on a UI confirm-card in `ask` situations).
    `subagents`/`mcp` add the `task` delegation tool and any configured MCP-server tools (top level
    only — a subagent runs with subagents=False to prevent unbounded nesting)."""
    from looplab.agents.agent import CompositeTools
    from looplab.tools.reposcout import RepoScoutTools
    from looplab.tools.runs_tools import RunsTools, RunLauncherTools, RunControlTools
    mode = normalize_mode(mode)
    roots = [Path.home(), REPO_ROOT, Path(run_root)] + list(extra_roots)
    providers = [RepoScoutTools(roots), RunsTools(run_root, alive_fn=alive_fn), RunLauncherTools()]
    kdir = getattr(settings, "knowledge_dir", None) if settings else None
    if kdir:                                            # write half of the KB — safe (scoped .md append) in EVERY mode
        from looplab.tools.knowledge_tools import KnowledgeWriteTools
        providers.append(KnowledgeWriteTools(kdir))
    if mode != "plan":
        from looplab.tools.write_tools import WriteTools
        from looplab.tools.shell_tools import ShellTools
        from looplab.tools.git_tools import GitTools
        sh = ShellTools(roots, mode=mode, trust_mode=trust_mode, approver=approver,
                        default_cwd=REPO_ROOT)   # the spec promises "default: repo root", not $HOME
        backup_dir = Path(run_root) / "assistant" / "backups"
        providers += [WriteTools(roots, mode=mode, approver=approver, repo_root=REPO_ROOT,
                                 backup_dir=backup_dir),
                      sh, GitTools(sh, cwd=REPO_ROOT),
                      # Drive an existing run's lifecycle (finalize/stop/resume/reset/delete node/run),
                      # self-gated by the same mode+approver so destructive verbs raise a confirm card.
                      RunControlTools(run_root, alive_fn=alive_fn, mode=mode, approver=approver)]
    providers.append(TodoTools(on_todos=on_todos))
    if subagents and client is not None:
        providers.append(SubagentTools(client, run_root, alive_fn=alive_fn, settings=settings,
                                       cancel_check=cancel_check))
    if mcp:
        try:
            from looplab.tools.mcp_tools import McpTools
            m = McpTools.cached()      # connect to MCP servers ONCE per process, not per turn
            if m.specs():
                providers.append(m)
        except Exception:  # noqa: BLE001 - MCP is optional; never break the toolset
            pass
    return CompositeTools(providers)


def run_turn(client, run_root, messages: list, instruction: str, mode: str = DEFAULT_MODE, *,
             alive_fn: Optional[Callable] = None, settings=None, on_step: Optional[Callable] = None,
             approver: Optional[Callable] = None, extra_roots=(), _subagent: bool = False,
             on_todos: Optional[Callable] = None, reply_sink: Optional[Callable] = None,
             on_text: Optional[Callable] = None, cancel_check: Optional[Callable] = None) -> dict:
    """Run ONE assistant turn: drive the shared tool loop over the mode's toolset and return a
    response dict {ok, reply, steps, applied, mode}. `messages` is the prior conversation
    (role/content); `instruction` is the new user message. Pure orchestration — the caller injects the
    LLM client, the run-liveness probe, Settings and the `approver` (so it is unit-testable with a
    scripted fake client + a stub approver)."""
    from looplab.agents.agent import drive_tool_loop, loop_opts_from_settings
    mode = normalize_mode(mode)
    trust_mode = getattr(settings, "trust_mode", "trusted_local") if settings is not None else "trusted_local"
    tools = build_tools(run_root, alive_fn=alive_fn, mode=mode, approver=approver,
                        trust_mode=trust_mode, extra_roots=extra_roots,
                        client=client, subagents=not _subagent, mcp=not _subagent, settings=settings,
                        on_todos=on_todos, cancel_check=cancel_check)
    roots = [Path.home(), REPO_ROOT, Path(run_root)] + list(extra_roots)
    from looplab.serve.assistant_commands import expand_command
    grounded, refs = expand_mentions(expand_command(instruction), run_root, alive_fn=alive_fn, roots=roots)
    convo = [{"role": "system", "content": system_prompt(
        mode, knowledge_dir=(getattr(settings, "knowledge_dir", None) if settings else None))}]
    for m in messages:
        role = m.get("role")
        # A user turn may carry `raw` — the full model-facing instruction (attached-file contents,
        # UI-context preamble) persisted alongside the clean display `content`. Prefer it, or the
        # model loses the attachments on every turn after the one they were sent with (the browser
        # is the only other place that content exists).
        body = (m.get("raw") or m.get("content")) if role == "user" else m.get("content")
        # A TYPED @mention (`@file:…`/`@run:…`) has display==instruction, so no `raw` was persisted —
        # the stored `content` is only the literal mention text, and the grounding (file body / run
        # summary) the model saw on the original turn would be LOST on every later turn. Re-expand a
        # historical user turn's mentions here (skip turns that already carry a grounded `raw`) so the
        # context stays present — the same asymmetry the `raw` mechanism fixed for attachments.
        if role == "user" and not m.get("raw") and body and "@" in body:
            try:
                body, _ = expand_mentions(body, run_root, alive_fn=alive_fn, roots=roots)
            except Exception:  # noqa: BLE001 - grounding re-expansion is best-effort
                pass
        if role in ("user", "assistant") and body:
            convo.append({"role": role, "content": body})
    convo.append({"role": "user", "content": grounded})

    steps: list[dict] = []

    def _on_step(ev: dict) -> None:
        label = _step_label(ev)
        steps.append({"tool": (ev or {}).get("tool", ""), "arg": str((ev or {}).get("arg", "")),
                      "label": label, "turn": (ev or {}).get("turn", 0)})
        if on_step is not None:
            try:
                on_step({**(ev or {}), "label": label})
            except Exception:  # noqa: BLE001 - progress must never perturb the loop
                pass

    box: dict = {}

    def _fin(args):
        box["reply"] = (args or {}).get("reply", "") if isinstance(args, dict) else ""
        return box["reply"]

    def _fb(msgs):
        if box.get("reply"):
            return box["reply"]
        for m in reversed(msgs):
            if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip():
                return m["content"].strip()
        return "(no reply)"

    opts = loop_opts_from_settings(settings) if settings is not None else {}
    opts["self_plan"] = False        # the assistant uses the visible write_todos tool instead
    max_turns = int(getattr(settings, "agent_max_turns", 0) or 0)
    # Interactive assistant: bound the turn's wall-clock so a stalled shared-LLM call can't leave the
    # chat "thinking" forever. Falls back to 5 min when the setting is unset (0 = unlimited).
    time_budget = float(getattr(settings, "agent_time_budget_s", 0.0) or 0.0) or 300.0
    def _collect(attr):
        return [a for p in getattr(tools, "providers", []) if hasattr(p, attr) for a in getattr(p, attr)]

    try:
        reply = drive_tool_loop(client, tools, convo, _emit_spec(),
                                max_turns=max_turns, time_budget_s=time_budget,
                                finalize=_fin, fallback=_fb, on_step=_on_step, on_text=on_text,
                                cancel_check=cancel_check, **opts)
    except Exception as e:  # noqa: BLE001 - surface a usable error, never crash the request
        return {"ok": False, "error": str(e), "reply": f"(assistant error: {e})", "steps": steps,
                "applied": _collect("applied"), "proposals": _collect("proposals"),
                "todos": _collect("todos"), "refs": refs, "mode": mode}
    reply = reply or box.get("reply") or "(no reply)"
    # Real token streaming of the FINAL answer: after the tool loop has acted, generate the
    # user-facing answer with a streaming call over the accumulated trace, pushing tokens to the sink.
    # (One extra call; reuses the context the loop built. The loop's emit reply is the fallback.)
    # GUARD (belt): drive_tool_loop compacts a long trace IN PLACE (slice-assign), so `convo` stays
    # current through auto_summary. If tools nonetheless ran and `convo` holds no tool-result
    # messages (compaction summarized every one away), streaming over it would make the model
    # re-answer BLIND — skip streaming and keep the loop's (correct) reply.
    trace_ok = (not steps) or any(m.get("role") == "tool" for m in convo)
    # If the user cancelled, DON'T fire a fresh (un-cancellable) streaming completion for the final
    # answer — that call could hang on the shared LLM and keep the worker (and its SSE stream) alive
    # long after Stop. Keep the loop's already-computed reply instead.
    try:
        _cancelled = bool(cancel_check and cancel_check())
    except Exception:  # noqa: BLE001 - a broken cancel probe must not discard a computed reply
        _cancelled = False
    if reply_sink is not None and trace_ok and not _cancelled:
        try:
            # Strip UNANSWERED tool calls anywhere in the trace, not just a trailing message:
            # when the model paired a retrieval call with final_answer, the loop executed the
            # retrieval but returned on final_answer, leaving its tool_call_id dangling — strict
            # OpenAI-compatible endpoints 400 on that and the turn silently loses streaming.
            answered = {m.get("tool_call_id") for m in convo if m.get("role") == "tool"}
            base = []
            for m in convo:
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    kept = [c for c in m["tool_calls"] if c.get("id") in answered]
                    if not kept and not (m.get("content") or "").strip():
                        continue
                    if len(kept) != len(m["tool_calls"]):
                        m = {**m, "tool_calls": kept} if kept else \
                            {k: v for k, v in m.items() if k != "tool_calls"}
                base.append(m)
            stream_msgs = base + [{"role": "user", "content": "Now write your final answer to the "
                                   "user in Markdown, based on everything above. Be concise."}]
            streamed = []
            for piece in client.complete_text_stream(stream_msgs):
                if cancel_check and cancel_check():   # stop honored mid-stream too
                    break
                streamed.append(piece)
                try:
                    reply_sink(piece)
                except Exception:  # noqa: BLE001 - a sink failure must not abort the turn
                    pass
            if "".join(streamed).strip():
                reply = "".join(streamed)
        except Exception:  # noqa: BLE001 - streaming is an enhancement; keep the loop's reply
            pass
    return {"ok": True, "reply": reply, "steps": steps,
            "applied": _collect("applied"), "proposals": _collect("proposals"),
            "todos": _collect("todos"), "refs": refs, "mode": mode}


def _step_label(ev: dict) -> str:
    tool = (ev or {}).get("tool", "")
    arg = str((ev or {}).get("arg", ""))
    short = arg.rsplit("/", 1)[-1] if arg else ""
    return ({"read_file": f"reading {short}", "list_dir": f"listing {short or 'a directory'}",
             "find_files": f"searching {short or 'files'}",
             "list_runs": "listing runs", "read_run": f"reading run {short or ''}".strip(),
             "task": "delegating a subtask"}.get(tool)
            or (f"{tool} {short}".strip() if tool else "thinking"))


class TodoTools:
    """A visible TODO list for multi-step work (Claude-Code TodoWrite). The model calls `write_todos`
    to keep an up-to-date checklist; the latest list is surfaced live to the UI (via `on_todos`) and
    returned with the turn, so a long task shows its plan and progress instead of an opaque wait."""

    def __init__(self, on_todos: Optional[Callable] = None):
        self.todos: list[dict] = []
        self.on_todos = on_todos

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        from looplab.tools._base import fn_spec
        return [fn_spec(
            "write_todos",
            "Record/update your TODO list for a multi-step task (replaces the previous list). Mark each "
            "item pending / in_progress / completed as you go. Use it for any task with 3+ steps.",
            {"todos": {"type": "array", "items": {"type": "object", "properties": {
                "content": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}}}},
            ["todos"])]

    def execute(self, name: str, args: dict) -> str:
        if name != "write_todos":
            return f"(unknown tool: {name})"
        items = [{"content": str(t.get("content", "")), "status": t.get("status", "pending")}
                 for t in ((args or {}).get("todos") or []) if isinstance(t, dict)]
        self.todos = items
        if self.on_todos:
            try:
                self.on_todos(items)
            except Exception:  # noqa: BLE001 - live surface is best-effort
                pass
        done = sum(1 for t in items if t["status"] == "completed")
        return f"(todos updated: {done}/{len(items)} done)"


class SubagentTools:
    """Delegate a self-contained subtask to a FRESH agent with its own context (Claude-Code `task`).
    The subagent runs a full read-only inner turn and returns ONLY its final text — the token-saving
    point (the main loop never sees the subagent's intermediate tool churn). Runs in `plan` mode
    (read-only) so a subagent can research/inspect freely without mutating behind the user's back; the
    main agent applies any change itself (under the user's mode/approval). Nesting is prevented — the
    inner turn is built with subagents=False."""

    def __init__(self, client, run_root, alive_fn: Optional[Callable] = None, settings=None,
                 cancel_check: Optional[Callable] = None):
        self.client = client
        self.run_root = run_root
        self.alive_fn = alive_fn
        self.settings = settings
        self.cancel_check = cancel_check   # forwarded so Stop interrupts a long-running subagent too

    def bind_state(self, state=None, parent=None) -> None:
        return None

    def specs(self) -> list[dict]:
        from looplab.tools._base import fn_spec
        return [fn_spec(
            "task",
            "Delegate a focused, self-contained subtask to a fresh sub-agent that has its OWN context "
            "and read-only tools (inspect files, read runs). It returns only its final answer — use it "
            "to research/summarize a big area without cluttering your own context. Give a complete, "
            "standalone prompt.",
            {"prompt": {"type": "string", "description": "the full standalone subtask"}},
            ["prompt"])]

    def execute(self, name: str, args: dict) -> str:
        if name != "task":
            return f"(unknown tool: {name})"
        prompt = str((args or {}).get("prompt") or "").strip()
        if not prompt:
            return "(task needs a prompt)"
        # Bail immediately if the user already hit Stop before this subtask even began.
        if self.cancel_check is not None:
            try:
                if self.cancel_check():
                    return "(cancelled by the user)"
            except Exception:  # noqa: BLE001 - a broken cancel probe must not block the subtask
                pass
        # Inner turn: read-only, no further subagents (build_tools called with subagents=False by
        # passing client=None to the recursive run_turn's build — enforced by _subagent flag).
        # Forward cancel_check so Stop interrupts the inner loop at its next turn boundary instead of
        # letting it run its full time-budget while the outer UI is already dead.
        res = run_turn(self.client, self.run_root, [], prompt, "plan",
                       alive_fn=self.alive_fn, settings=self.settings, _subagent=True,
                       cancel_check=self.cancel_check)
        return res.get("reply") or "(subagent returned nothing)"
