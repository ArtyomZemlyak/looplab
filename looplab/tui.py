"""A terminal control plane for LoopLab — a chat-first TUI that mirrors the most-used slice of the
React web UI (ADR-1's "Textual TUI" lane, finally filled) WITHOUT re-implementing the whole graph
explorer. The web UI is the place to dig into the DAG, traces and per-node detail; this is the place
to *drive*: describe a goal and the boss launches a run, glance at what's running, and chat with the
boss to steer it — all from a terminal, over SSH, no browser.

Design (deliberately small):
  * It is a thin HTTP CLIENT of the SAME server `looplab ui` serves (ADR-18: the read/control plane).
    Every bit of intelligence — the genesis boss, the action-router, run-folding — already lives there;
    the TUI adds zero new server surface and no new engine coupling. If no server is reachable on the
    default local address it auto-launches one (`looplab ui --no-build`, API only — the TUI never needs
    the React bundle) and tears it down on exit.
  * Dependency-free beyond what's already installed: stdlib `urllib` for HTTP, and `rich` (shipped with
    Typer) for rendering. No Textual, no curses, no raw-mode — a redraw-then-`input()` REPL works in any
    terminal and degrades gracefully when piped.

Three surfaces, reached from one dashboard:
  1. Dashboard   — a live table of runs (status · nodes · best metric · age); pick one, or just type a
                   goal to start a new run.
  2. Genesis     — chat the boss into a run spec, tweak it, launch it (POST /api/genesis → /api/start).
  3. Run view    — a compact status panel + a boss chat that APPLIES actions (POST .../command), exactly
                   like the web Dock: free text becomes a plan the engine runs.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from typing import Any, Optional

# ----------------------------------------------------------------------------- HTTP client

class ApiError(Exception):
    """A non-2xx response (or transport failure). `.detail` carries the server's human reason when
    FastAPI returned one (it puts it in `detail`), so callers can show "n_seeds: …" not just "422"."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.detail = message
        self.status = status


class Api:
    """Minimal JSON client for the LoopLab UI server — the stdlib mirror of ui/src/util.js. Sends the
    `X-LoopLab-Token` header when LOOPLAB_UI_TOKEN is set (token-gated deployments), exactly like the
    browser does, so the TUI works behind the same auth."""

    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.token = token or os.environ.get("LOOPLAB_UI_TOKEN") or ""
        self.timeout = timeout

    def _headers(self, body: bool = False) -> dict:
        h = {"Accept": "application/json"}
        if body:
            h["Content-Type"] = "application/json"
        if self.token:
            h["X-LoopLab-Token"] = self.token
        return h

    def _request(self, method: str, path: str, body: Optional[dict] = None, timeout: Optional[float] = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=self._headers(body is not None))
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = (json.loads(e.read().decode("utf-8", errors="replace")) or {}).get("detail", "")
            except Exception:  # noqa: BLE001 - no/garbled JSON body -> fall back to the status line
                pass
            raise ApiError(detail or f"{path}: HTTP {e.code}", status=e.code) from e
        except (urllib.error.URLError, OSError, socket.timeout) as e:
            raise ApiError(f"could not reach {self.base}{path}: {e}") from e
        return json.loads(raw) if raw else None

    def get(self, path: str, timeout: Optional[float] = None) -> Any:
        return self._request("GET", path, timeout=timeout)

    def post(self, path: str, body: Optional[dict] = None, timeout: Optional[float] = None) -> Any:
        return self._request("POST", path, body=body or {}, timeout=timeout)

    def ping(self) -> bool:
        """Is a LoopLab server answering here? (a cheap, short-timeout GET on the runs list)."""
        try:
            self.get("/api/runs", timeout=2.5)
            return True
        except ApiError:
            return False

    # ---- background-job polling: the server hands back {status:'running', job_id} for slow agent work
    # (genesis boss / action-router) so it can't 504 behind a proxy. We poll to completion. Mirrors
    # util.js genesisAwait / jobAwait. Transient poll errors are tolerated (keep polling).
    def _await_job(self, resp: Any, poll_path, *, interval: float, deadline_s: float) -> Any:
        if not isinstance(resp, dict) or resp.get("status") != "running" or not resp.get("job_id"):
            return resp                                     # fast path: already the final result
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            time.sleep(interval)
            try:
                j = self.get(poll_path(resp["job_id"]))
            except ApiError:
                continue                                    # transient — keep polling
            if isinstance(j, dict) and j.get("status") == "done":
                return j
            if isinstance(j, dict) and j.get("status") == "unknown":
                return {"ok": False, "error": "the job expired — try again"}
        return {"ok": False, "error": "timed out waiting for the boss"}

    def genesis(self, messages: list, instruction: str, draft: Optional[dict]) -> dict:
        resp = self.post("/api/genesis", {"messages": messages, "instruction": instruction, "draft": draft},
                         timeout=60)
        return self._await_job(resp, lambda j: f"/api/genesis/{j}", interval=1.5, deadline_s=300)

    def command(self, run_id: str, messages: list, instruction: str, node_id=None) -> dict:
        resp = self.post(f"/api/runs/{run_id}/command",
                         {"messages": messages, "instruction": instruction, "node_id": node_id}, timeout=60)
        return self._await_job(resp, lambda j: f"/api/jobs/{j}", interval=1.5, deadline_s=600)


# ----------------------------------------------------------------------------- pure formatting helpers
# (kept side-effect-free so they're unit-testable without a live server or a terminal.)

def fmt_metric(v: Any, precision: int = 4) -> str:
    """Compact metric formatting — the Python twin of util.js `fmt` (exp form for very small/large)."""
    if v is None or (isinstance(v, float) and v != v):     # None or NaN
        return "—"
    if not isinstance(v, (int, float)):
        return str(v)
    a = abs(v)
    if a != 0 and (a < 1e-3 or a >= 1e6):
        return f"{v:.2e}"
    return f"{float(f'{v:.{precision}g}'):g}"


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
_PHASE_META = {
    "finished":      ("✓", "green",        "finished"),
    "paused":        ("⏸", "yellow",       "paused"),
    "approval":      ("◆", "magenta",      "awaiting approval"),
    "spec_approval": ("◆", "magenta",      "awaiting spec approval"),
    "onboarding":    ("◆", "magenta",      "onboarding"),
    "grounding":     ("◌", "cyan",         "grounding"),
    "search":        ("●", "cyan",         "searching"),
}


def phase_meta(summary: dict) -> tuple[str, str, str]:
    """(glyph, colour, label) for a run summary or state dict. A non-finished run with a live engine
    reads as a bright "running"; a non-finished run with NO live engine is a stalled/zombie run."""
    phase = summary.get("phase") or ("finished" if summary.get("finished") else "search")
    glyph, colour, label = _PHASE_META.get(phase, ("●", "cyan", phase))
    engine = summary.get("engine_running")
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
    can assert the boss's plan is shown faithfully."""
    if not spec:
        return ["(no plan yet — describe a goal and the boss will propose one)"]
    out: list[str] = []
    out.append(f"run name : {spec.get('run_id') or '—'}")
    task = spec.get("task") or {}
    if spec.get("task_file"):
        out.append(f"task     : {str(spec['task_file']).split('/')[-1]}  (from the catalogue)")
    elif task.get("kind"):
        label = task["kind"]
        if task.get("kind") == "mlebench_real" and task.get("competition"):
            label += f" · {task['competition']}"
        out.append(f"task     : {label}")
        if task.get("goal"):
            out.append(f"goal     : {task['goal']}")
        if task.get("editable_path"):
            out.append(f"repo     : {task['editable_path']}")
    settings = spec.get("settings") or {}
    knobs = [(k, settings[k]) for k in ("llm_model", "max_nodes", "n_seeds", "policy") if settings.get(k) is not None]
    if knobs:
        out.append("settings : " + ", ".join(f"{k}={v}" for k, v in knobs))
    if spec.get("rationale"):
        out.append(f"why      : {spec['rationale']}")
    for i, step in enumerate(spec.get("setup_steps") or [], 1):
        out.append(f"  step {i}. {step}")
    return out


def spec_ready(spec: Optional[dict]) -> Optional[str]:
    """None when the spec is launchable, else a short reason it isn't (mirrors GenesisChat's gate, so
    the TUI never fires a doomed /api/start). Keeps the launch button honest."""
    if not spec:
        return "no plan yet — describe a goal first"
    if not (spec.get("run_id") or "").strip():
        return "the run needs a name"
    task = spec.get("task") or {}
    if spec.get("task_file"):
        return None
    if not task.get("kind"):
        return "the boss hasn't picked a task yet"
    if task.get("kind") == "mlebench_real" and not (task.get("competition") or "").strip():
        return "set a Kaggle competition id for the MLE-bench task"
    if task.get("kind") == "repo":
        if not (task.get("editable_path") or "").strip():
            return "a repo task needs an editable repo path"
        has_cmd = isinstance(task.get("eval"), dict) and task["eval"].get("command")
        if not (has_cmd or task.get("onboard")):
            return "a repo task needs an eval command (or onboard mode)"
    return None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(s: str) -> str:
    """run-id normaliser — the Python twin of GenesisChat's strict slug() (lowercase kebab, ≤40)."""
    return _SLUG_RE.sub("-", str(s or "").lower()).strip("-")[:40]


# Actions whose effect on a FINISHED/zombie run only takes hold once the engine is reopened+resumed —
# the Python twin of util.js NEEDS_RESUME, so the TUI's apply path matches the web Dock's exactly.
_NEEDS_RESUME = {"fork", "inject_node", "force_confirm", "force_ablate", "deep_research",
                 "set_strategy", "budget_extend"}


def action_needs_engine(action: dict) -> bool:
    return (action or {}).get("type") in _NEEDS_RESUME


# ----------------------------------------------------------------------------- server autostart

def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ensure_server(base_url: Optional[str], run_root: str, *, log=lambda m: None) -> tuple[str, Optional[subprocess.Popen]]:
    """Return a (base_url, child) for a reachable server. If `base_url` already answers, reuse it (no
    child). Otherwise launch our own `looplab ui --no-build` (API only — the TUI never needs the React
    bundle) on a free local port, wait until it answers, and return its handle so the caller can stop it
    on exit. Raises ApiError if a server we launched never comes up."""
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
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    api = Api(url)
    for _ in range(120):                                   # up to ~24s for uvicorn to bind
        if child.poll() is not None:                       # died early (missing [ui] extra, bad port…)
            raise ApiError("the auto-launched server exited — is the [ui] extra installed? (pip install 'looplab[ui]')")
        if api.ping():
            log("server is up.")
            return url, child
        time.sleep(0.2)
    child.terminate()
    raise ApiError("timed out waiting for the auto-launched server to start")


# ----------------------------------------------------------------------------- the interactive app

class Tui:
    """The redraw-then-prompt REPL. Holds the rich Console + the Api client; each surface is a method
    that draws itself then reads one line. Kept thin: the heavy lifting is in the pure helpers above and
    on the server."""

    def __init__(self, api: Api, run_root: str):
        from rich.console import Console
        self.api = api
        self.run_root = run_root
        self.console = Console()

    # ---- small rich builders ------------------------------------------------
    def _rule(self, title: str):
        from rich.rule import Rule
        self.console.print(Rule(f"[bold]{title}[/bold]", style="dim"))

    def _runs_table(self, runs: list):
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
            t.add_row(str(i), r.get("run_id", "?"), f"[{colour}]{glyph} {label}[/{colour}]",
                      str(r.get("nodes", 0)), fmt_metric(best),
                      (r.get("task_id") or r.get("goal") or "—")[:28], fmt_ago(r.get("mtime")))
        return t

    def _status_panel(self, run_id: str, state: dict):
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
            f"[{colour}]{glyph} {label}[/{colour}]"
            + (f"   direction={state.get('direction')}" if state.get("direction") else ""),
            f"nodes: [bold]{len(nodes)}[/bold] total · {ok} scored · {running} in flight",
            f"best:  [bold]{fmt_metric(best)}[/bold]" + (f"  (node {best_id})" if best_id is not None else ""),
        ]
        if state.get("goal"):
            lines.append(f"goal:  {state['goal']}")
        if state.get("stop_reason"):
            lines.append(f"[dim]stopped: {state['stop_reason']}[/dim]")
        return Panel("\n".join(lines), title=f"[bold]{run_id}[/bold]", border_style=colour, expand=True)

    # ---- data fetch with a spinner -----------------------------------------
    def _fetch_runs(self) -> list:
        with self.console.status("loading runs…", spinner="dots"):
            try:
                return sort_runs(self.api.get("/api/runs") or [])
            except ApiError as e:
                self.console.print(f"[red]could not load runs: {e}[/red]")
                return []

    # ---- dashboard ----------------------------------------------------------
    def dashboard(self) -> None:
        """The home surface: a table of runs + a one-line command bar. Returns when the user quits."""
        while True:
            self.console.clear()
            self.console.print("[bold cyan]LoopLab[/bold cyan] [dim]· terminal control plane[/dim]   "
                               f"[dim]{self.api.base}[/dim]")
            runs = self._fetch_runs()
            if runs:
                self.console.print(self._runs_table(runs))
            else:
                self.console.print("[dim]no runs yet — type a goal below to start your first one.[/dim]\n")
            self.console.print("[dim]Pick a run by number · type a goal to start one · "
                               "[bold]n[/bold]ew · [bold]r[/bold]efresh · [bold]q[/bold]uit[/dim]")
            try:
                raw = self.console.input("[bold green]» [/bold green]").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not raw:
                continue
            low = raw.lower()
            if low in ("q", "quit", "exit"):
                return
            if low in ("r", "refresh"):
                continue
            if low in ("n", "new"):
                self.genesis()
                continue
            if raw.isdigit():
                idx = int(raw) - 1
                if 0 <= idx < len(runs):
                    self.run_view(runs[idx]["run_id"])
                else:
                    self._pause(f"no run #{raw} — there are {len(runs)}.")
                continue
            # Anything else is treated as a goal — straight into genesis planning (the chat-first path).
            self.genesis(seed=raw)

    # ---- genesis (new run via chat) ----------------------------------------
    def genesis(self, seed: Optional[str] = None) -> None:
        from rich.panel import Panel
        msgs: list[dict] = []
        spec: Optional[dict] = None
        self.console.clear()
        self._rule("New run — describe it, the boss plans the rest")
        self.console.print("[dim]Tell me what to run; I’ll name it, pick the task and set the knobs. "
                           "Commands: [bold]launch[/bold] · [bold]back[/bold] · [bold]q[/bold]uit[/dim]\n")
        if not seed:
            for s in ("Run nomad2018 on minimax-m3, 100 nodes",
                      "Spooky author identification with deepseek, 50 nodes",
                      "A quick toy quadratic run to smoke-test"):
                self.console.print(f"[dim]  · e.g.[/dim] {s}")
            self.console.print()

        pending = seed
        while True:
            if pending is None:
                try:
                    pending = self.console.input("[bold green]you » [/bold green]").strip()
                except (EOFError, KeyboardInterrupt):
                    return
            text, pending = pending, None
            if not text:
                continue
            low = text.lower()
            if low in ("back", "b"):
                return
            if low in ("q", "quit", "exit"):
                raise _Quit()
            if low in ("launch", "start", "go"):
                if self._launch(spec, msgs):
                    return
                continue
            # A goal/refinement turn -> the boss (re)plans.
            msgs.append({"role": "user", "content": text})
            self.console.print(f"[green]you ›[/green] {text}")
            try:
                with self.console.status("boss is planning…", spinner="dots"):
                    r = self.api.genesis(msgs, text, spec)
            except ApiError as e:
                self.console.print(f"[red]planner unreachable: {e}[/red]")
                msgs.pop()
                continue
            reply = (r or {}).get("reply") or "(planned — see the card)"
            msgs.append({"role": "assistant", "content": reply})
            self.console.print(Panel(reply, title="boss", border_style="cyan", expand=True))
            new_spec = (r or {}).get("spec")
            # Only adopt a REAL spec — the offline soft-fail returns ok:false with a blank spec, which
            # must not wipe a good draft the user already has (mirrors GenesisChat).
            if (r or {}).get("ok") is not False and new_spec and (
                    new_spec.get("run_id") or new_spec.get("task_file") or (new_spec.get("task") or {}).get("kind")):
                spec = new_spec
            if (r or {}).get("ok") is False and (r or {}).get("error"):
                self.console.print(f"[yellow]{r['error']}[/yellow]")
            self._render_spec(spec)

    def _render_spec(self, spec: Optional[dict]) -> None:
        from rich.panel import Panel
        body = "\n".join(spec_lines(spec))
        reason = spec_ready(spec)
        foot = "[green]ready — type [bold]launch[/bold] to start[/green]" if reason is None else f"[yellow]{reason}[/yellow]"
        self.console.print(Panel(body + "\n\n" + foot, title="proposed run", border_style="green", expand=True))

    def _launch(self, spec: Optional[dict], msgs: list) -> bool:
        reason = spec_ready(spec)
        if reason:
            self.console.print(f"[yellow]can’t launch yet: {reason}[/yellow]")
            return False
        rid = slug(spec["run_id"])
        body: dict = {"run_id": rid, "settings": spec.get("settings") or {}}
        if spec.get("task_file"):
            body["task_file"] = spec["task_file"]
        else:
            body["task"] = spec["task"]
        if msgs:                                            # carry the planning chat into the run's history
            body["chat"] = [{"role": m["role"], "content": m["content"]} for m in msgs]
        try:
            with self.console.status(f"starting {rid}…", spinner="dots"):
                self.api.post("/api/start", body)
        except ApiError as e:
            if e.status == 409:
                self.console.print(f"[yellow]a run named “{rid}” already exists — rename it (edit the goal) and retry[/yellow]")
            else:
                self.console.print(f"[red]launch failed: {e}[/red]")
            return False
        self.console.print(f"[green]▶ started [bold]{rid}[/bold][/green]")
        # The engine is a freshly-spawned subprocess; its events.jsonl appears a beat later, before which
        # /state 404s. Wait briefly so the run view opens on real status instead of a transient error.
        with self.console.status("waiting for the engine to start…", spinner="dots"):
            for _ in range(25):
                try:
                    self.api.get(f"/api/runs/{rid}/state")
                    break
                except ApiError:
                    time.sleep(0.2)
        self.run_view(rid)
        return True

    # ---- run view (status + boss chat) -------------------------------------
    def run_view(self, run_id: str) -> None:
        history = self._load_chat(run_id)
        self._draw_run(run_id, history, redraw=True)
        while True:
            try:
                raw = self.console.input("[bold green]» [/bold green]").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not raw:
                continue
            low = raw.lower()
            if low in ("back", "b"):
                return
            if low in ("q", "quit", "exit"):
                raise _Quit()
            if low in ("s", "status", "r", "refresh"):
                self._draw_run(run_id, history, redraw=True)
                continue
            if low in ("?", "help"):
                self._run_help()
                continue
            # Quick controls map to the same /control endpoint the web buttons use — deterministic, no LLM.
            if low in ("pause", "resume", "stop"):
                self._control(run_id, "run_abort" if low == "stop" else low,
                              {"reason": "tui"} if low == "stop" else {})
                self._draw_run(run_id, history, redraw=True)
                continue
            # Anything else: talk to the boss. It may just reply, or return a plan the engine runs.
            history.append({"role": "user", "content": raw})
            self.console.print(f"[green]you ›[/green] {raw}")
            self._boss_turn(run_id, raw, history)

    def _draw_run(self, run_id: str, history: list, redraw: bool) -> None:
        if redraw:
            self.console.clear()
        with self.console.status("loading run…", spinner="dots"):
            try:
                payload = self.api.get(f"/api/runs/{run_id}/state")
            except ApiError as e:
                self.console.print(f"[red]could not load {run_id}: {e}[/red]")
                return
        state = (payload or {}).get("state") or {}
        self.console.print(self._status_panel(run_id, state))
        self._render_chat(history)
        self.console.print("[dim]Chat with the boss · [bold]s[/bold]tatus · [bold]pause/resume/stop[/bold] "
                           "· [bold]?[/bold] help · [bold]back[/bold] · [bold]q[/bold]uit[/dim]")

    def _render_chat(self, history: list, tail: int = 8) -> None:
        from rich.markdown import Markdown
        shown = [m for m in history if m.get("role") in ("user", "assistant", "action", "summary")]
        if not shown:
            self.console.print("[dim](no chat yet — ask the boss anything, or tell it what to change)[/dim]")
            return
        for m in shown[-tail:]:
            role = m.get("role")
            if role == "user":
                self.console.print(f"[bold green]you ›[/bold green] {m.get('content', '')}")
            elif role == "action":
                act = m.get("action") or {}
                mark = {"done": "[green]✓[/green]", "failed": "[red]✗[/red]"}.get(m.get("status"), "[cyan]·[/cyan]")
                self.console.print(f"  {mark} [cyan]{act.get('label') or act.get('type', 'action')}[/cyan]")
            elif role == "summary":
                self.console.print(f"[dim]— recap: {m.get('content', '')}[/dim]")
            else:
                self.console.print("[bold cyan]boss ›[/bold cyan]")
                self.console.print(Markdown(m.get("content", "")))

    def _boss_turn(self, run_id: str, instruction: str, history: list) -> None:
        """One boss command: free text -> a plan applied in order (then reopen+resume if needed), or an
        advisory reply. Mirrors the web Dock's runCommand + autoApplyPlan."""
        prior = history[:-1]                                # history minus the just-appended user turn
        try:
            with self.console.status("boss is reading & deciding…", spinner="dots"):
                r = self.api.command(run_id, prior, instruction)
        except ApiError as e:
            self.console.print(f"[red]boss unreachable: {e}[/red]")
            history.pop()
            return
        r = r or {}
        if r.get("ok") and r.get("actions"):
            if r.get("reply"):
                self._append_and_show(run_id, history, {"role": "assistant", "content": r["reply"]})
            self._apply_plan(run_id, history, r["actions"])
        elif r.get("ok") and r.get("reply"):
            self._append_and_show(run_id, history, {"role": "assistant", "content": r["reply"]})
        elif r.get("error"):
            self.console.print(f"[yellow]⚠ {r['error']}[/yellow]")
        else:
            self.console.print("[yellow]⚠ no reply from the boss[/yellow]")

    def _apply_plan(self, run_id: str, history: list, actions: list) -> None:
        # Probe liveness once: a finished or zombie run needs reopen+resume to actually run engine steps.
        try:
            state = (self.api.get(f"/api/runs/{run_id}/state") or {}).get("state") or {}
        except ApiError:
            state = {}
        engine_dead = bool(state.get("finished")) or state.get("engine_running") is False
        needs_resume = False
        for action in actions:
            label = action.get("label") or action.get("type", "action")
            turn = {"role": "action", "action": action, "status": "running"}
            try:
                self.api.post(f"/api/runs/{run_id}/control",
                              {"type": action.get("type"), "data": action.get("data") or {}})
                turn["status"] = "done"
                self.console.print(f"  [green]✓[/green] [cyan]{label}[/cyan]")
                if engine_dead and action_needs_engine(action):
                    needs_resume = True
            except ApiError as e:
                turn["status"] = "failed"
                self.console.print(f"  [red]✗[/red] {label} — {e}")
            history.append(turn)
            self._persist(run_id, turn)
        if needs_resume:
            try:
                with self.console.status("reopening & resuming the run…", spinner="dots"):
                    self.api.post(f"/api/runs/{run_id}/control", {"type": "run_reopened", "data": {}})
                    self.api.post(f"/api/runs/{run_id}/resume", {})
                self.console.print("[green]↻ resumed — running the plan[/green]")
            except ApiError as e:
                self.console.print(f"[red]resume failed: {e}[/red]")
        elif engine_dead:
            self.console.print("[dim]saved — these take effect on the next reopen[/dim]")

    def _control(self, run_id: str, etype: str, data: dict) -> None:
        try:
            self.api.post(f"/api/runs/{run_id}/control", {"type": etype, "data": data})
            self.console.print(f"[green]✓ {etype}[/green]")
        except ApiError as e:
            self.console.print(f"[red]{etype} failed: {e}[/red]")

    def _append_and_show(self, run_id: str, history: list, turn: dict) -> None:
        from rich.markdown import Markdown
        history.append(turn)
        self.console.print("[bold cyan]boss ›[/bold cyan]")
        self.console.print(Markdown(turn.get("content", "")))
        self._persist(run_id, turn)

    def _persist(self, run_id: str, turn: dict) -> None:
        """Best-effort durable append to the run's chat.jsonl (the same sidecar the web UI writes), so a
        TUI conversation survives and shows up in the browser too. Never fails the chat on error."""
        try:
            self.api.post(f"/api/runs/{run_id}/chat-log", turn)
        except ApiError:
            pass

    def _load_chat(self, run_id: str) -> list:
        try:
            return list(self.api.get(f"/api/runs/{run_id}/chat-log") or [])
        except ApiError:
            return []

    def _run_help(self) -> None:
        self.console.print(
            "[dim]Just say what you want — the boss turns it into actions and the run applies them.\n"
            "  e.g. “you have 20 more nodes, try a few neural nets”, “focus on feature engineering”,\n"
            "       “what’s working so far?”, “promote the best node”.\n"
            "Quick controls: [bold]pause[/bold] · [bold]resume[/bold] · [bold]stop[/bold] · "
            "[bold]status[/bold] · [bold]back[/bold] · [bold]q[/bold]uit[/dim]")

    def _pause(self, msg: str) -> None:
        self.console.print(f"[yellow]{msg}[/yellow]")
        try:
            self.console.input("[dim]press Enter…[/dim]")
        except (EOFError, KeyboardInterrupt):
            pass


class _Quit(Exception):
    """Raised from a nested surface to unwind straight out of the app (a global quit)."""


def main(server: Optional[str], run_root: str) -> int:
    """Entry point for `looplab tui`. Ensures a server, runs the dashboard, cleans up a child server."""
    try:
        from rich.console import Console
    except ModuleNotFoundError:
        print("The TUI needs `rich` (it ships with Typer). Try: pip install -e .", file=sys.stderr)
        return 1
    console = Console()
    try:
        base, child = ensure_server(server, run_root, log=lambda m: console.print(f"[dim]{m}[/dim]"))
    except ApiError as e:
        console.print(f"[red]{e}[/red]")
        return 1
    try:
        Tui(Api(base), run_root).dashboard()
    except _Quit:
        pass
    finally:
        if child is not None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
    console.print("[dim]bye[/dim]")
    return 0
