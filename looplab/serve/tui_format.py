"""The TUI's pure helpers + rendering + server autostart, split verbatim out of `serve/tui.py`
(docs/15 §P5.2): metric/age formatting, phase glyphs, genesis-spec rendering/gating, chat-history
shaping, input parsing and redraw signatures (all side-effect-free, so tests/test_tui.py exercises
them without a live server or a terminal), the five screen renderers that used to be `Tui` methods
(doc 25 SC-15 — they take the Console they write to, so they still need no server and no terminal),
plus the `ensure_server`/`_free_port`/`_stop_child` autostart trio the REPL's `main` uses.
`serve/tui.py` re-exports every name, so the old import paths keep working."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

# The three looplab imports this module allows itself: the wire-protocol vocabulary it shares with the
# server (phase names), the shared metric formatter (doc 25 XP-09 — a dependency-free `core`
# function, so the TUI still adds no dependencies), and the shared launch-proposal schema (doc 27 —
# also `core`, so the TUI still adds no dependencies and still cannot see `looplab.adapters`).
from looplab.core.fitness import format_metric
from looplab.core.run_proposal import RunProposal, slug_run_id
from looplab.serve.protocol import (PHASE_APPROVAL, PHASE_FINALIZING, PHASE_FINISHED, PHASE_GROUNDING,
                                    PHASE_ONBOARDING, PHASE_PAUSED, PHASE_SEARCH,
                                    PHASE_SPEC_APPROVAL)
from looplab.serve.tui_api import Api, ApiError

# ----------------------------------------------------------------------------- pure formatting helpers
# (kept side-effect-free so they're unit-testable without a live server or a terminal.)

def fmt_metric(v: Any, precision: int = 4) -> str:
    """Compact metric formatting — the Python twin of format.js `fmt` (exp form for very small/large).

    The rule moved to `core.fitness.format_metric` (doc 25 XP-09) — this WAS the most complete of the
    three copies, so it is the shape the shared default keeps; the name stays because the TUI and its
    tests call it."""
    return format_metric(v, precision=precision)


def fmt_ago(sec: Optional[float], now: Optional[float] = None) -> str:
    """Relative age of an epoch-SECONDS timestamp (run mtime/created come from os.stat → seconds)."""
    if not sec:
        return "—"
    now = time.time() if now is None else now
    d = now - sec
    if d < 0:
        return "just now"
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    if d < 7 * 86400:
        return f"{int(d // 86400)}d ago"
    return time.strftime("%Y-%m-%d", time.localtime(sec))


# Phase → (glyph, rich-colour, label). One source of truth for how a run's state reads at a glance,
# shared by the dashboard table and the run-view status panel. `running` is inferred (not finished and
# a live engine), so it isn't a server phase value — handled in phase_meta().
# The keys are the server's phase names (`server._phase`) — the PHASE_* protocol constants.
_PHASE_META = {
    PHASE_FINISHED:      ("✓", "green",   "finished"),
    PHASE_FINALIZING:    ("◐", "yellow",  "finalizing"),
    PHASE_PAUSED:        ("⏸", "yellow",  "paused"),
    PHASE_APPROVAL:      ("◆", "magenta", "awaiting approval"),
    PHASE_SPEC_APPROVAL: ("◆", "magenta", "awaiting spec approval"),
    PHASE_ONBOARDING:    ("◆", "magenta", "onboarding"),
    PHASE_GROUNDING:     ("◌", "cyan",    "grounding"),
    PHASE_SEARCH:        ("●", "cyan",    "searching"),
}


def phase_meta(summary: dict) -> tuple[str, str, str]:
    """(glyph, colour, label) for a run summary or state dict. A non-finished run with a live engine
    reads as a bright "running"; a non-finished run with NO live engine is a stalled/zombie run."""
    phase = summary.get("phase") or ("finished" if summary.get("finished") else "search")
    glyph, colour, label = _PHASE_META.get(phase, ("●", "cyan", phase))
    engine = summary.get("engine_running")
    if phase == PHASE_FINALIZING:
        # A pending run_abort is never an ordinary pause/running state. Keep the lifecycle visible;
        # if its driver has disappeared, say so without relabelling the operation itself as "stalled".
        return (glyph, colour, label if engine is not False else f"{label} · engine stopped")
    if phase not in ("finished", "paused"):
        if engine is True:
            return ("●", "green", "running" if phase == "search" else label)
        if engine is False:                                # not finished, no engine holding the lock
            return ("◍", "red", f"{label} · stalled")
    return (glyph, colour, label)


def sort_runs(runs: list) -> list:
    """Most-recently-active first (the table's default) — live runs naturally bubble up as they tick."""
    return sorted(runs, key=lambda r: r.get("mtime") or 0, reverse=True)


def spec_lines(spec: Optional[dict]) -> list[str]:
    """Flatten a genesis spec ({run_id, task|task_file, settings, rationale, setup_steps}) into the plain
    lines the proposal panel renders — also the exact thing the launch summary echoes. Pure, so a test
    can assert the boss's plan is shown faithfully.

    The flattening itself is `core/run_proposal.py::RunProposal.lines` (doc 27's shared-schema row,
    closed 2026-09-08): `looplab run --goal` prints the same lines under its `Genesis -> kind=…`
    announcement, so the CLI and the TUI describe a plan in one vocabulary instead of two. Only the
    empty case is the TUI's own — it is this panel's copy, not a property of a proposal."""
    if not spec:
        return ["(no plan yet — describe a goal and the boss will propose one)"]
    return RunProposal.from_card(spec).lines()


def launch_body(spec: dict, msgs: Optional[list] = None) -> dict:
    """The ONE `/api/start` body the TUI builds from a genesis spec.

    Asked of `/api/validate` on every draft render and posted to `/api/start` on launch, so the
    readiness footer and the launch are answered about the SAME proposal by the SAME server funnel.
    The TUI used to keep its own `spec_ready` copy of "is this launchable" here — a hand-mirrored
    superset of the adapters' validators whose docstring pointed at the backlog row asking for
    `/api/validate` — and every move of the task schema had to be repaired in both (doc 52 row 8).
    This module may not import `looplab.adapters` (pinned in `tests/test_tui.py`), which is what
    keeps the copy from coming back.

    The body SHAPE is `RunProposal.start_body` since 2026-09-08 (doc 27,
    `three-new-run-planners-no-shared-schema`): the TUI, the Web launch card and any other caller
    now spell the `/api/start` payload once. This function stays because the TUI's spec is a card
    and its chat is a message list — reading those INTO the shared shape is the TUI's own job.
    """
    return RunProposal.from_card(spec).start_body(msgs)


def readiness_reason(verdict: Any) -> Optional[str]:
    """None when `/api/validate` said the proposal is launchable, else the one line the footer
    prints: the server's message, plus each field it named (with a reason of its own)."""
    if not isinstance(verdict, dict):
        return "the server gave no launch verdict"
    if verdict.get("ready") is True:
        return None
    message = str(verdict.get("message") or verdict.get("code") or "not launchable yet")
    errors = verdict.get("field_errors") or {}
    named = [f"{field}: {why}" for field, why in errors.items()
             if isinstance(errors, dict) and why and str(why) != message]
    return message + (f" ({'; '.join(named)})" if named else "")


def slug(s: str) -> str:
    """run-id normaliser (lowercase kebab, ≤40).

    It no longer has to "stay in step with the server's own slugify" — that instruction was here
    because `serve/routers/genesis.py::_normalize_genesis` kept a second copy, and the two had
    already drifted (its `re.sub(r"(^-|-$)", …)` stripped one leading and one trailing dash where
    this one stripped all of them). Both are now `core/run_proposal.py::slug_run_id`; this name
    stays because the TUI's own callers and tests use it."""
    return slug_run_id(s)


# Destructive verbs worth a louder confirm marker — the Python twin of the web Dock's isCritical.
_CRITICAL = {"run_abort", "node_abort", "node_reset", "reset", "run_reopened"}


def is_critical(action: dict) -> bool:
    return (action or {}).get("type") in _CRITICAL


def history_for_boss(history: list) -> list[dict]:
    """Convert stored chat turns into the {role, content} messages the boss endpoints expect — the Python
    twin of the web Dock's buildHistory(). Action rows collapse to a one-line "applied: <label>" note and
    summaries pass through as recaps; turns with no usable content are dropped. (A stored action turn has
    no `content`, so sending it raw would feed the boss "action: None" noise.)"""
    out: list[dict] = []
    for m in history:
        role = m.get("role")
        if role in ("user", "assistant"):
            content = (m.get("content") or "").strip()
            if content:
                out.append({"role": role, "content": content})
        elif role == "action":
            act = m.get("action") or {}
            # A failed OR still-running command must not be reported as "applied" — the next boss turn
            # would otherwise plan on top of a postcondition the server has not observed.
            verb = {"failed": "failed: ", "pending": "requested (pending): ",
                    "running": "requested: "}.get(m.get("status"), "applied: ")
            out.append({"role": "assistant", "content": verb + (act.get("label") or act.get("type") or "action")})
        elif role == "summary":
            content = (m.get("content") or "").strip()
            if content:
                out.append({"role": "assistant", "content": "Earlier recap: " + content})
    return out


def parse_pick(text: str, n: int) -> Optional[list[int]]:
    """Parse a confirm-prompt answer into the 0-based indices to apply, over `n` proposed actions:
      ""/"y"/"yes"/"a"/"all"  -> everything ([0..n-1])
      "n"/"no"/"cancel"/"q"   -> nothing ([])
      "1,3" / "1 3" / "2"     -> just those (1-based in, deduped, in order, out-of-range dropped)
    Returns None when the answer is unrecognised (caller re-asks). Pure, so the "tap to pick" behaviour
    is unit-tested without a terminal."""
    t = (text or "").strip().lower()
    if t in ("", "y", "yes", "a", "all", "apply"):
        return list(range(n))
    if t in ("n", "no", "cancel", "q", "quit", "none"):
        return []
    nums = re.findall(r"\d+", t)
    if not nums:
        return None
    seen: list[int] = []
    for s in nums:
        i = int(s) - 1
        if 0 <= i < n and i not in seen:
            seen.append(i)
    return seen


def dashboard_sig(runs: list) -> tuple:
    """A cheap signature of the runs list — only what's drawn — so the live dashboard redraws when (and
    only when) something visible changed (no flicker while idle)."""
    return tuple((r.get("run_id"), r.get("phase"), r.get("finished"), r.get("engine_running"),
                  r.get("nodes"), r.get("best_confirmed"), r.get("best_metric"), r.get("mtime"))
                 for r in runs)


def run_sig(state: dict) -> tuple:
    """A cheap signature of a run's live state — phase/engine/node-counts/best — so the run view redraws
    only on a real change."""
    nodes = state.get("nodes") or {}
    in_flight = sum(1 for n in nodes.values() if n.get("status") == "pending")
    scored = sum(1 for n in nodes.values() if n.get("metric") is not None and not n.get("error"))
    return (state.get("phase"), state.get("finished"), state.get("engine_running"),
            len(nodes), scored, in_flight, state.get("best_node_id"), state.get("stop_reason"))


# ----------------------------------------------------------------------------- rich rendering
# (doc 25 SC-15's remaining half: the five render helpers used to be METHODS on `serve/tui.py::Tui`,
# where the only way to see what a surface draws was to construct the whole REPL — an Api client, a
# run root and a Console — and the only way to change a line was to touch the file that also holds
# the wizards, the chat persistence and the durable command-recovery state machine. They are pure
# functions of (console, data): every one takes the Console it writes to as its first argument
# instead of reaching for `self.console`, and the two that used to consult `self._interactive()` or
# `self.api.base` take those as keyword arguments — so the CALLER keeps every decision that needs a
# live server or a real terminal, and the rendering keeps none. Bodies moved verbatim; the only
# edits are `self.console` -> `console`, `self._runs_table`/`self._status_panel`/`self._render_chat`
# -> the module functions, and the two injected values above.)

def _esc(value) -> str:
    """Escape one server/LLM/user-supplied value before it enters a rich markup f-string. A stray
    ``[/tag]`` in a command label, error, run id, or chat line otherwise raises rich ``MarkupError``
    and aborts the TUI; for a PERSISTED row (``_reconcile_pending``) that re-crashes on every reopen."""
    from rich.markup import escape
    return escape(str(value))


def runs_table(runs: list):
    from rich.table import Table
    from rich import box
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    t.add_column("#", justify="right", style="dim", width=3)
    t.add_column("run", style="bold", no_wrap=True)
    t.add_column("status", no_wrap=True)
    t.add_column("nodes", justify="right", width=6)
    t.add_column("best", justify="right", width=12)
    t.add_column("task", no_wrap=True, style="dim")
    t.add_column("updated", justify="right", style="dim", no_wrap=True)
    for i, r in enumerate(runs, 1):
        glyph, colour, label = phase_meta(r)
        best = r.get("best_confirmed")
        best = r.get("best_metric") if best is None else best
        t.add_row(str(i), _esc(r.get("run_id", "?")), f"[{colour}]{glyph} {_esc(label)}[/{colour}]",
                  str(r.get("nodes", 0)), fmt_metric(best),
                  _esc((r.get("task_id") or r.get("goal") or "—")[:28]), fmt_ago(r.get("mtime")))
    return t


def status_panel(run_id: str, state: dict):
    from rich.panel import Panel
    glyph, colour, label = phase_meta(state)
    nodes = state.get("nodes") or {}
    best_id = state.get("best_node_id")
    best = None
    if best_id is not None and str(best_id) in {str(k) for k in nodes}:
        bn = nodes.get(str(best_id)) or nodes.get(best_id) or {}
        best = bn.get("confirmed_mean")
        best = bn.get("metric") if best is None else best
    running = sum(1 for n in nodes.values() if n.get("status") == "pending")
    ok = sum(1 for n in nodes.values() if n.get("metric") is not None and not n.get("error"))
    lines = [
        f"[{colour}]{glyph} {_esc(label)}[/{colour}]"
        + (f"   direction={state.get('direction')}" if state.get("direction") else ""),
        f"nodes: [bold]{len(nodes)}[/bold] total · {ok} scored · {running} in flight",
        f"best:  [bold]{fmt_metric(best)}[/bold]" + (f"  (node {best_id})" if best_id is not None else ""),
    ]
    if state.get("goal"):
        lines.append(f"goal:  {_esc(state['goal'])}")
    if state.get("stop_reason"):
        lines.append(f"[dim]stopped: {_esc(state['stop_reason'])}[/dim]")
    return Panel("\n".join(lines), title=f"[bold]{_esc(run_id)}[/bold]", border_style=colour, expand=True)


def draw_dashboard(console, runs: list, *, base: str, live: bool) -> None:
    """The dashboard screen. `base` is the server the TUI is talking to and `live` says whether this
    is a real terminal that auto-refreshes — both decided by the caller (the Api client and
    `Tui._interactive()` respectively), because neither is a question about the drawing."""
    console.clear()
    live_mark = "[green]● live[/green]" if live else ""
    console.print("[bold cyan]LoopLab[/bold cyan] [dim]· terminal control plane[/dim]   "
                  f"[dim]{_esc(base)}[/dim]  {live_mark}")
    if runs:
        console.print(runs_table(runs))
    else:
        console.print("[dim]no runs yet — type a goal below to start your first one.[/dim]\n")
    console.print("[dim]Pick a run by number · type a goal to start one · "
                  "[bold]n[/bold]ew · [bold]r[/bold]efresh · [bold]q[/bold]uit[/dim]")


def render_spec(console, spec: Optional[dict], reason: Optional[str]) -> None:
    """The proposed-run panel. `reason` is the SERVER's readiness verdict (None = launchable), asked
    by `Tui._validate` over `/api/validate` — the TUI carries no launch-readiness rule of its own
    (doc 52 row 8), and this module may not grow one: it renders the verdict it is handed."""
    from rich.panel import Panel
    body = "\n".join(spec_lines(spec))
    foot = "[green]ready — type [bold]launch[/bold] to start[/green]" if reason is None else f"[yellow]{_esc(reason)}[/yellow]"
    console.print(Panel(_esc(body) + "\n\n" + foot, title="proposed run", border_style="green", expand=True))


def draw_run(console, run_id: str, state: Optional[dict], history: list, *, live: bool) -> None:
    console.clear()
    if state is None:
        console.print(f"[red]could not load {_esc(run_id)} — is the server still up?[/red]")
    else:
        console.print(status_panel(run_id, state))
    render_chat(console, history)
    live_mark = "[green]● live[/green] · " if live else ""
    console.print(f"[dim]{live_mark}Chat with the boss · [bold]s[/bold]tatus · "
                  "[bold]stop/finalize/resume[/bold] · [bold]?[/bold] help · "
                  "[bold]back[/bold] · [bold]q[/bold]uit[/dim]")


def render_chat(console, history: list, tail: int = 8) -> None:
    from rich.markdown import Markdown
    shown = [m for m in history if m.get("role") in ("user", "assistant", "action", "summary")]
    if not shown:
        console.print("[dim](no chat yet — ask the boss anything, or tell it what to change)[/dim]")
        return
    for m in shown[-tail:]:
        role = m.get("role")
        if role == "user":
            console.print(f"[bold green]you ›[/bold green] {_esc(m.get('content', ''))}")
        elif role == "action":
            act = m.get("action") or {}
            mark = {"done": "[green]✓[/green]", "pending": "[yellow]…[/yellow]",
                    "failed": "[red]✗[/red]"}.get(m.get("status"), "[cyan]·[/cyan]")
            console.print(f"  {mark} [cyan]{_esc(act.get('label') or act.get('type', 'action'))}[/cyan]")
        elif role == "summary":
            console.print(f"[dim]— recap: {_esc(m.get('content', ''))}[/dim]")
        else:
            console.print("[bold cyan]boss ›[/bold cyan]")
            console.print(Markdown(m.get("content", "")))


def _command_failure_line(label, error) -> str:
    """Escape server/LLM-supplied text before it enters a rich markup string: a stray ``[/tag]`` in a
    label or error message otherwise raises rich ``MarkupError`` and aborts the TUI — and, because
    ``_reconcile_pending`` re-prints the persisted row, it re-crashes on every reopen of the run."""
    return f"  [red]✗[/red] {_esc(label)} — {_esc(error)}"


# ----------------------------------------------------------------------------- server autostart

def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stop_child(child: Optional[subprocess.Popen]) -> None:
    """Terminate a server WE launched and reap it, so it never lingers or zombies — SIGTERM first, then
    SIGKILL if it won't go. A no-op for a reused/external server (child is None) or one already dead."""
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def ensure_server(base_url: Optional[str], run_root: str, *, log=lambda m: None) -> tuple[str, Optional[subprocess.Popen]]:
    """Return a (base_url, child) for a reachable server. If `base_url` already answers, reuse it (no
    child). Otherwise launch our own `looplab ui --no-build` (API only — the TUI never needs the React
    bundle) on a free local port, wait until it answers, and return its handle so the caller can stop it
    on exit. Raises ApiError if a server we launched never comes up."""
    # Absolutize before spawning: the child server runs with cwd=<package parent> (repo root in dev,
    # site-packages for a pip install) and resolves a relative run_root against THAT cwd, so a relative
    # "runs" would otherwise point into the install tree instead of the user's project.
    run_root = os.path.abspath(run_root)
    if base_url:
        if Api(base_url).ping():
            return base_url, None
        # An explicit --server that's down is a user error: don't silently shadow it with a local one.
        raise ApiError(f"no LoopLab server reachable at {base_url} — start one with `looplab ui` or drop --server")

    default = f"http://127.0.0.1:{int(os.environ.get('LOOPLAB_UI_PORT', '8765'))}"
    if Api(default).ping():
        return default, None

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    log(f"no server found — launching one on {url} …")
    env = {**os.environ, "LOOPLAB_RUN_ROOT": run_root}
    child = subprocess.Popen(
        [sys.executable, "-m", "looplab.cli", "ui", "--no-build",
         "--host", "127.0.0.1", "--port", str(port), "--run-root", run_root],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(Path(__file__).resolve().parents[2]))   # repo root (this file sits 2 levels deep)
    api = Api(url)
    try:
        for _ in range(120):                               # up to ~24s for uvicorn to bind
            if child.poll() is not None:                   # died early (no [ui] extra, port taken, …)
                raise ApiError("the auto-launched server exited before it came up — check the [ui] extra "
                               "is installed (pip install 'looplab[ui]') and the port is free")
            if api.ping():
                log("server is up.")
                return url, child
            time.sleep(0.2)
        raise ApiError("timed out waiting for the auto-launched server to start")
    except BaseException:                                  # timeout, ApiError, or Ctrl-C during startup
        _stop_child(child)                                 # never leak the half-started server
        raise
