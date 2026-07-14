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

import copy
import hashlib
import select
import sys
import time
import uuid
from typing import Any, Callable, Optional
from urllib.parse import quote

# Split-module re-exports (docs/15 §P5.2): the HTTP client now lives in serve/tui_api.py and the
# pure helpers + server autostart in serve/tui_format.py, but this module remains the public face —
# tests (tests/test_tui.py, tests/test_stop_finalize_resume.py) and external callers import every
# one of these names from `looplab.serve.tui`, so each moved name is re-exported verbatim.
from looplab.serve.tui_api import (  # noqa: F401
    Api,
    ApiError,
    command_error_transient,
    normalize_run_generation,
)
from looplab.serve.tui_format import (  # noqa: F401 — re-exported for the import-compat note above
    _free_port, _stop_child, dashboard_sig, ensure_server, fmt_ago, fmt_metric, history_for_boss,
    is_critical, parse_pick, phase_meta, run_sig, slug, sort_runs, spec_lines, spec_ready)


_COMMAND_DONE = {"succeeded", "noop"}
_COMMAND_FAILED = {"failed", "rejected", "timed_out"}
_COMMAND_PENDING = {"accepted", "executing"}


def _url_id(value: Any) -> str:
    return quote(str(value), safe="")


def _command_error(record: dict) -> str:
    """Return the command service's structured or plain error as one terminal-friendly line."""
    err = (record or {}).get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("detail") or err.get("code") or "command failed")
    return str(err or "command failed")


def _staged_command(event_type: str, data: dict, idempotency_key: str,
                    expected_generation: str) -> dict:
    """Build the durable pre-POST envelope used to recover an unconfirmed submission.

    The command id alone is insufficient after an early 404: the original POST may still be queued
    behind a proxy/server lock, and SHA-256 is intentionally not reversible.  Persist the exact key
    and intent before sending so a fresh TUI can replay the *same* logical request, never mint a new
    additive budget/fork intent.
    """
    key = str(idempotency_key)
    generation = normalize_run_generation(expected_generation)
    # Detach nested dictionaries/lists from the visible boss action. Otherwise a later in-memory
    # mutation could rewrite both copies and make the recovery equality check bless a different POST.
    payload = copy.deepcopy(data or {})
    return {
        "id": "cmd_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32],
        "status": "accepted",
        "event_type": str(event_type),
        "idempotency_key": key,
        "expected_generation": generation,
        "intent": {"type": str(event_type), "data": payload},
        "submit_unconfirmed": True,
    }


def _staged_replay(turn: dict) -> Optional[tuple[str, str, dict, str]]:
    """Validate and return the exact staged submission, or refuse unsafe/corrupt recovery data."""
    command = turn.get("command") if isinstance(turn, dict) else None
    action = turn.get("action") if isinstance(turn, dict) else None
    if not isinstance(command, dict) or command.get("submit_unconfirmed") is not True:
        return None
    key = command.get("idempotency_key")
    try:
        generation = normalize_run_generation(command.get("expected_generation"))
    except ApiError:
        return None
    intent = command.get("intent")
    if not isinstance(key, str) or not key or len(key) > 512 or not isinstance(intent, dict):
        return None
    event_type = intent.get("type")
    data = intent.get("data")
    expected_id = "cmd_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    if (command.get("id") != expected_id or not isinstance(event_type, str) or not event_type
            or not isinstance(data, dict) or command.get("event_type") != event_type):
        return None
    # The visible action and the hidden replay envelope must describe one immutable intent. A local
    # edit/corrupt status row must fail closed instead of turning reconciliation into a new mutation.
    if (not isinstance(action, dict) or action.get("type") != event_type
            or (action.get("data") or {}) != data):
        return None
    return key, event_type, dict(data), generation


def _observed_command(record: dict, staged: Optional[dict] = None) -> dict:
    """Keep only public lifecycle fields once a server response proves the command record exists."""
    observed = {k: record.get(k) for k in ("id", "status", "event_type", "error")
                if record.get(k) is not None}
    staged_id = (staged or {}).get("id")
    if staged_id and observed.get("id") and staged_id != observed["id"]:
        # The server may attach an identical fresh-key request to an older unresolved command. Keep
        # the pre-POST id as a fold alias so the append-only command_status row can update its action.
        observed["staged_id"] = staged_id
    return observed

# ----------------------------------------------------------------------------- the interactive app

def _esc(value) -> str:
    """Escape one server/LLM/user-supplied value before it enters a rich markup f-string. A stray
    ``[/tag]`` in a command label, error, run id, or chat line otherwise raises rich ``MarkupError``
    and aborts the TUI; for a PERSISTED row (``_reconcile_pending``) that re-crashes on every reopen."""
    from rich.markup import escape
    return escape(str(value))


def _command_failure_line(label, error) -> str:
    """Escape server/LLM-supplied text before it enters a rich markup string: a stray ``[/tag]`` in a
    label or error message otherwise raises rich ``MarkupError`` and aborts the TUI — and, because
    ``_reconcile_pending`` re-prints the persisted row, it re-crashes on every reopen of the run."""
    return f"  [red]✗[/red] {_esc(label)} — {_esc(error)}"


class Tui:
    """The redraw-then-prompt REPL. Holds the rich Console + the Api client; each surface is a method
    that draws itself then reads one line. Kept thin: the heavy lifting is in the pure helpers above and
    on the server."""

    def __init__(self, api: Api, run_root: str):
        from rich.console import Console
        self.api = api
        self.run_root = run_root
        self.console = Console()

    # ---- input: blocking, and live (auto-refresh while waiting) -------------
    def _interactive(self) -> bool:
        """True only on a real terminal where we can poll stdin for live refresh. Piped stdin (tests,
        scripts) or no select() falls back to a plain blocking prompt — deterministic, no redraws."""
        try:
            return sys.stdin.isatty() and sys.stdout.isatty() and hasattr(select, "select")
        except (ValueError, OSError):
            return False

    def _prompt(self, prompt: str) -> Optional[str]:
        """A single blocking prompt. Returns the stripped line, or None on EOF/^C (caller treats as
        quit/back)."""
        try:
            return self.console.input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return None

    def _live_prompt(self, prompt: str, fetch: Callable[[], Any], render: Callable[[Any], None],
                     sig: Callable[[Any], Any], interval: float = 2.0):
        """Render fetch()→render(), then wait for a line; while waiting, re-fetch every `interval`s and
        redraw ONLY when the signature changes (so an idle screen never flickers and a live run updates
        itself the instant something happens). Returns (line, data): `line` is the stripped input or None
        on EOF/^C; `data` is the latest fetched payload the render reflects (so a selection maps to what's
        on screen). Non-tty → a plain blocking prompt over one fetch (no refresh)."""
        data = fetch()
        render(data)
        if not self._interactive():
            return self._read_line_fallback(prompt), data
        cur = sig(data)
        self.console.print(prompt, end="")
        while True:
            try:
                ready, _, _ = select.select([sys.stdin], [], [], interval)
                if ready:
                    line = sys.stdin.readline()
                    return (None if line == "" else line.strip()), data
            except (KeyboardInterrupt, EOFError):
                return None, data
            except (OSError, ValueError, TypeError):
                # stdin became unusable for select/readline (closed, replaced by a non-fd object, …):
                # fall back to a plain blocking prompt instead of crashing the REPL with a traceback.
                return self._read_line_fallback(prompt), data
            new = fetch()
            if sig(new) != cur:
                data, cur = new, sig(new)
                render(data)                                 # something changed → redraw + reprint prompt
                self.console.print(prompt, end="")

    def _read_line_fallback(self, prompt: str) -> Optional[str]:
        """A plain blocking read of one line (no live refresh). Returns the stripped line, or None on
        EOF/^C. Used for non-tty stdin and as the safety net when select()/readline() can't be used."""
        self.console.print(prompt, end="")
        try:
            line = sys.stdin.readline()
        except (KeyboardInterrupt, EOFError, OSError, ValueError):
            return None
        return None if line == "" else line.strip()

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

    # ---- data fetch (quiet: the live loop polls on a timer, so no per-poll spinner/flicker) ---------
    def _fetch_runs(self) -> list:
        try:
            return sort_runs(self.api.get("/api/runs") or [])
        except ApiError:
            return []

    def _fetch_state(self, run_id: str) -> Optional[dict]:
        try:
            return (self.api.get(f"/api/runs/{_url_id(run_id)}/state") or {}).get("state") or {}
        except ApiError:
            return None

    # ---- dashboard ----------------------------------------------------------
    def _draw_dashboard(self, runs: list) -> None:
        self.console.clear()
        live = "[green]● live[/green]" if self._interactive() else ""
        self.console.print("[bold cyan]LoopLab[/bold cyan] [dim]· terminal control plane[/dim]   "
                           f"[dim]{self.api.base}[/dim]  {live}")
        if runs:
            self.console.print(self._runs_table(runs))
        else:
            self.console.print("[dim]no runs yet — type a goal below to start your first one.[/dim]\n")
        self.console.print("[dim]Pick a run by number · type a goal to start one · "
                           "[bold]n[/bold]ew · [bold]r[/bold]efresh · [bold]q[/bold]uit[/dim]")

    def dashboard(self) -> None:
        """The home surface: a LIVE table of runs (auto-refreshes when anything changes) + a command bar.
        Returns when the user quits."""
        while True:
            raw, runs = self._live_prompt("[bold green]» [/bold green]",
                                          fetch=self._fetch_runs, render=self._draw_dashboard, sig=dashboard_sig)
            if raw is None:                                  # EOF / ^C
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
            self.console.print(f"[green]you ›[/green] {_esc(text)}")
            try:
                with self.console.status("boss is planning…", spinner="dots"):
                    r = self.api.genesis(msgs, text, spec)
            except ApiError as e:
                self.console.print(f"[red]planner unreachable: {_esc(e)}[/red]")
                msgs.pop()
                continue
            except KeyboardInterrupt:
                self.console.print("[dim]cancelled.[/dim]")
                msgs.pop()
                continue
            reply = (r or {}).get("reply") or "(planned — see the card)"
            msgs.append({"role": "assistant", "content": reply})
            self.console.print(Panel(reply, title="boss", border_style="cyan", expand=True))
            new_spec = (r or {}).get("spec")
            # Only adopt a REAL spec — the offline soft-fail returns ok:false with a blank spec, which
            # must not wipe a good draft the user already has.
            if (r or {}).get("ok") is not False and new_spec and (
                    new_spec.get("run_id") or new_spec.get("task_file") or (new_spec.get("task") or {}).get("kind")):
                spec = new_spec
            if (r or {}).get("ok") is False and (r or {}).get("error"):
                self.console.print(f"[yellow]{_esc(r['error'])}[/yellow]")
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
            self.console.print(f"[yellow]can’t launch yet: {_esc(reason)}[/yellow]")
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
                self.console.print(f"[yellow]a run named “{_esc(rid)}” already exists — rename it (edit the goal) and retry[/yellow]")
            else:
                self.console.print(f"[red]launch failed: {_esc(e)}[/red]")
            return False
        except KeyboardInterrupt:
            self.console.print("[dim]cancelled.[/dim]")
            return False
        self.console.print(f"[green]▶ started [bold]{_esc(rid)}[/bold][/green]")
        # The engine is a freshly-spawned subprocess; its events.jsonl appears a beat later, before which
        # /state 404s. Wait briefly so the run view opens on real status instead of a transient error.
        try:
            with self.console.status("waiting for the engine to start…", spinner="dots"):
                for _ in range(25):
                    try:
                        self.api.get(f"/api/runs/{_url_id(rid)}/state")
                        break
                    except ApiError:
                        time.sleep(0.2)
        except KeyboardInterrupt:
            pass                                             # the run did start — drop straight into it
        self.run_view(rid)
        return True

    # ---- run view (status + boss chat) -------------------------------------
    def run_view(self, run_id: str) -> None:
        """A LIVE per-run view: the status panel auto-refreshes the instant the engine ticks, and a chat
        steers the boss. Returns to the dashboard on `back`."""
        history = self._load_chat(run_id)
        # A prior session may have ended after a durable command was accepted but before its terminal
        # status arrived. Reconcile that record before this session can plan or submit anything else.
        self._reconcile_pending(run_id, history)
        while True:
            raw, _ = self._live_prompt("[bold green]» [/bold green]",
                                       fetch=lambda: self._fetch_state(run_id),
                                       render=lambda st: self._draw_run(run_id, st, history),
                                       sig=lambda st: run_sig(st or {}))
            if raw is None:                                  # EOF / ^C -> back to dashboard
                return
            if not raw:
                continue
            low = raw.lower()
            if low in ("back", "b"):
                return
            if low in ("q", "quit", "exit"):
                raise _Quit()
            if low in ("s", "status", "r", "refresh"):
                continue                                     # _live_prompt redraws on the next loop
            if low in ("?", "help"):
                self._run_help()
                self._pause("")                              # let them read it before the live redraw
                continue
            # Quick controls use the same durable command lifecycle as Web/Assistant — deterministic,
            # idempotent, and no LLM.
            # 3 verbs: stop = freeze (no wrap-up, reversible), finalize = stop + wrap-up (terminal),
            # resume = continue from any stopped state. `pause` is a back-compat alias of `stop`.
            if low in ("stop", "pause", "finalize", "resume"):
                if low == "finalize" and not self._confirm(
                        "Finalize this run? (stops AND writes the final report / cross-run lessons / cost)"):
                    continue
                _ev = {"stop": "pause", "pause": "pause", "finalize": "run_abort", "resume": "resume"}[low]
                self._control(run_id, _ev, {"reason": "finalized"} if _ev == "run_abort" else {},
                              history=history, label=low)
                continue
            # Anything else: talk to the boss. It may just reply, or propose a plan we confirm then run.
            if not self._reconcile_pending(run_id, history):
                self.console.print("[yellow]wait for the pending run command before planning another action.[/yellow]")
                continue
            user_turn = {"role": "user", "content": raw}
            history.append(user_turn)
            self._persist(run_id, user_turn)                 # save the question too (the web persists it)
            self.console.print(f"[green]you ›[/green] {_esc(raw)}")
            self._boss_turn(run_id, raw, history)

    def _draw_run(self, run_id: str, state: Optional[dict], history: list) -> None:
        self.console.clear()
        if state is None:
            self.console.print(f"[red]could not load {_esc(run_id)} — is the server still up?[/red]")
        else:
            self.console.print(self._status_panel(run_id, state))
        self._render_chat(history)
        live = "[green]● live[/green] · " if self._interactive() else ""
        self.console.print(f"[dim]{live}Chat with the boss · [bold]s[/bold]tatus · "
                           "[bold]stop/finalize/resume[/bold] · [bold]?[/bold] help · "
                           "[bold]back[/bold] · [bold]q[/bold]uit[/dim]")

    def _render_chat(self, history: list, tail: int = 8) -> None:
        from rich.markdown import Markdown
        shown = [m for m in history if m.get("role") in ("user", "assistant", "action", "summary")]
        if not shown:
            self.console.print("[dim](no chat yet — ask the boss anything, or tell it what to change)[/dim]")
            return
        for m in shown[-tail:]:
            role = m.get("role")
            if role == "user":
                self.console.print(f"[bold green]you ›[/bold green] {_esc(m.get('content', ''))}")
            elif role == "action":
                act = m.get("action") or {}
                mark = {"done": "[green]✓[/green]", "pending": "[yellow]…[/yellow]",
                        "failed": "[red]✗[/red]"}.get(m.get("status"), "[cyan]·[/cyan]")
                self.console.print(f"  {mark} [cyan]{_esc(act.get('label') or act.get('type', 'action'))}[/cyan]")
            elif role == "summary":
                self.console.print(f"[dim]— recap: {_esc(m.get('content', ''))}[/dim]")
            else:
                self.console.print("[bold cyan]boss ›[/bold cyan]")
                self.console.print(Markdown(m.get("content", "")))

    def _boss_turn(self, run_id: str, instruction: str, history: list) -> None:
        """One boss command: free text -> a plan applied in order by the command service, or an
        advisory reply. The TUI never owns engine wake-up or transition postconditions."""
        if not self._reconcile_pending(run_id, history):
            self.console.print("[yellow]pending command is not complete; no new plan was requested.[/yellow]")
            return
        prior = history_for_boss(history[:-1])              # cleaned history minus the new user turn
        try:
            with self.console.status("boss is reading & deciding…", spinner="dots"):
                r = self.api.command(run_id, prior, instruction)
        except ApiError as e:
            self.console.print(f"[red]boss unreachable: {_esc(e)}[/red]")
            return                                           # the question stays in the (persisted) history
        except KeyboardInterrupt:
            self.console.print("[dim]cancelled.[/dim]")      # Ctrl-C during the (possibly multi-minute) call
            return
        r = r or {}
        if r.get("ok") and r.get("actions"):
            if r.get("reply"):
                self._append_and_show(run_id, history, {"role": "assistant", "content": r["reply"]})
            chosen = self._confirm_plan(r["actions"])        # the human picks what (if anything) to apply
            if chosen:
                self._apply_plan(run_id, history, chosen)
            else:
                self.console.print("[dim]nothing applied.[/dim]")
        elif r.get("ok") and r.get("reply"):
            self._append_and_show(run_id, history, {"role": "assistant", "content": r["reply"]})
        elif r.get("error"):
            self.console.print(f"[yellow]⚠ {_esc(r['error'])}[/yellow]")
        else:
            self.console.print("[yellow]⚠ no reply from the boss[/yellow]")

    def _confirm(self, question: str) -> bool:
        """A yes/no gate for a single destructive control (default: no)."""
        ans = self._prompt(f"[yellow]{question}[/yellow] [dim](y/N)[/dim] ")
        return (ans or "").strip().lower() in ("y", "yes")

    def _confirm_plan(self, actions: list) -> list:
        """Show the boss's proposed actions and let the human apply all, pick a subset, or cancel — the
        terminal twin of the web confirm cards. Critical steps (abort/reset) are flagged. Returns the
        actions to apply (possibly empty)."""
        self.console.print("[bold]Boss proposes:[/bold]")
        for i, a in enumerate(actions, 1):
            label = a.get("label") or a.get("type", "action")
            mark = "[red]⚠[/red]" if is_critical(a) else "[cyan]▸[/cyan]"
            why = a.get("rationale")
            self.console.print(f"  [bold]{i}[/bold] {mark} {_esc(label)}" + (f" [dim]— {_esc(why)}[/dim]" if why else ""))
        while True:
            ans = self._prompt("[green]Apply?[/green] [dim]Enter=all · numbers to pick (e.g. 1,3) · n=cancel[/dim] » ")
            if ans is None:                                  # EOF/^C -> treat as cancel (safe default)
                return []
            picks = parse_pick(ans, len(actions))
            if picks is None:
                self.console.print("[dim]…didn’t catch that — Enter for all, numbers like 1,3, or n to cancel[/dim]")
                continue
            return [actions[i] for i in picks]

    def _apply_plan(self, run_id: str, history: list, actions: list) -> None:
        """Apply an ordered boss plan through the authoritative command lifecycle.

        The command service owns event validation, engine wake-up and postconditions. In particular,
        this method must never append ``run_reopened`` after ``run_abort``: doing so clears the pending
        finalize and resumes computation instead of wrapping the run up.
        """
        if not self._reconcile_pending(run_id, history):
            self.console.print("[yellow]plan held: a previous command is still pending.[/yellow]")
            return
        for index, action in enumerate(actions):
            label = action.get("label") or action.get("type", "action")
            turn = {"role": "action", "action": action, "status": "running"}
            staged_index = None
            try:
                if action.get("type") == "__refresh_report__":
                    result = self.api.refresh_report(run_id)
                    if not isinstance(result, dict) or result.get("ok") is not True:
                        turn["status"] = "failed"
                        error = (result or {}).get("error") if isinstance(result, dict) else None
                        turn["error"] = error or "report refresh returned no confirmed success"
                        self.console.print(_command_failure_line(label, turn['error']))
                    else:
                        turn["status"] = "done"
                        self.console.print(f"  [green]✓[/green] [cyan]{_esc(label)}[/cyan]")
                else:
                    key = str(uuid.uuid4())
                    expected_generation = self.api.run_generation(run_id)
                    turn["status"] = "pending"
                    turn["command"] = _staged_command(
                        str(action.get("type") or ""), action.get("data") or {}, key,
                        expected_generation)
                    history.append(turn)
                    staged_index = len(history) - 1
                    if self._persist(run_id, turn) is False:
                        turn["status"] = "failed"
                        turn["error"] = "could not durably stage command identity; nothing was submitted"
                        self.console.print(_command_failure_line(label, turn['error']))
                        return
                    result = self.api.run_command(
                        run_id, str(action.get("type") or ""), action.get("data") or {},
                        idempotency_key=key, expected_generation=expected_generation)
                    turn["command"] = _observed_command(result, turn.get("command"))
                    status = result.get("status")
                    if status in _COMMAND_DONE:
                        turn["status"] = "done"
                        suffix = " (already satisfied)" if status == "noop" else ""
                        self.console.print(f"  [green]✓[/green] [cyan]{_esc(label)}[/cyan]{suffix}")
                    elif status in _COMMAND_PENDING:
                        turn["status"] = "pending"
                        self.console.print(f"  [yellow]…[/yellow] [cyan]{_esc(label)}[/cyan] — requested, pending")
                    elif status in _COMMAND_FAILED:
                        turn["status"] = "failed"
                        turn["error"] = _command_error(result)
                        self.console.print(_command_failure_line(label, turn['error']))
                    else:
                        turn["status"] = "failed"
                        turn["error"] = f"unexpected command status: {status or 'missing'}"
                        self.console.print(_command_failure_line(label, turn['error']))
            except ApiError as e:
                ambiguous = command_error_transient(e)
                turn["status"] = "pending" if ambiguous else "failed"
                turn["error"] = str(e)
                if ambiguous:
                    self.console.print(
                        f"  [yellow]…[/yellow] {label} — response lost; checking the staged command id")
                else:
                    self.console.print(_command_failure_line(label, e))
            if staged_index is None:
                history.append(turn)
                self._persist(run_id, turn)
            else:
                self._persist_command_status(run_id, turn, action_index=staged_index)
            remaining = len(actions) - index - 1
            if turn["status"] == "pending":
                if remaining:
                    self.console.print(f"  [yellow]{remaining} later plan step(s) were not submitted; "
                                       "retry after this command completes.[/yellow]")
                return
            if turn["status"] == "failed":
                if remaining:
                    self.console.print(f"  [yellow]{remaining} later plan step(s) were not submitted "
                                       "because this ordered step failed.[/yellow]")
                return
            if action.get("type") == "run_abort":
                if remaining:
                    self.console.print(f"  [dim]{remaining} later plan step(s) were not submitted: "
                                       "the run is finalized.[/dim]")
                return

    def _control(self, run_id: str, etype: str, data: dict, *, history: Optional[list] = None,
                 label: Optional[str] = None) -> dict:
        if history is not None and not self._reconcile_pending(run_id, history):
            self.console.print("[yellow]another run command is still pending.[/yellow]")
            return {"status": "executing", "error": "another command is pending", "event_type": etype}
        key = None
        expected_generation = None
        staged_turn = None
        staged_index = None
        try:
            expected_generation = self.api.run_generation(run_id)
        except ApiError as e:
            self.console.print(f"[red]{etype} failed: {_esc(e)}[/red]")
            return {"status": "failed", "error": str(e), "event_type": etype}
        if history is not None:
            key = str(uuid.uuid4())
            staged_turn = {
                "role": "action", "action": {"type": etype, "data": data, "label": label or etype},
                "status": "pending",
                "command": _staged_command(etype, data, key, expected_generation),
            }
            history.append(staged_turn)
            staged_index = len(history) - 1
            if self._persist(run_id, staged_turn) is False:
                staged_turn["status"] = "failed"
                staged_turn["error"] = "could not durably stage command identity; nothing was submitted"
                self.console.print(f"[red]{etype} failed: {staged_turn['error']}[/red]")
                return {"status": "failed", "error": staged_turn["error"], "event_type": etype}
        try:
            result = self.api.run_command(
                run_id, etype, data, idempotency_key=key,
                expected_generation=expected_generation)
            status = result.get("status")
            if staged_turn is not None:
                staged_turn["command"] = _observed_command(result, staged_turn.get("command"))
            if status in _COMMAND_DONE:
                suffix = " (already satisfied)" if status == "noop" else ""
                self.console.print(f"[green]✓ {etype}{suffix}[/green]")
                if staged_turn is not None:
                    staged_turn["status"] = "done"
            elif status in _COMMAND_PENDING:
                self.console.print(f"[yellow]… {etype} requested — pending[/yellow]")
            elif status in _COMMAND_FAILED:
                self.console.print(f"[red]{etype} failed: {_esc(_command_error(result))}[/red]")
                if staged_turn is not None:
                    staged_turn["status"] = "failed"
                    staged_turn["error"] = _command_error(result)
            else:
                self.console.print(f"[red]{etype} failed: unexpected command status {_esc(status or 'missing')}[/red]")
                if staged_turn is not None:
                    staged_turn["status"] = "failed"
                    staged_turn["error"] = f"unexpected command status: {status or 'missing'}"
            if staged_turn is not None:
                self._persist_command_status(run_id, staged_turn, action_index=staged_index)
            return result
        except ApiError as e:
            ambiguous = command_error_transient(e)
            self.console.print(
                f"[yellow]… {etype} response lost — checking the staged command id[/yellow]"
                if ambiguous else f"[red]{etype} failed: {e}[/red]")
            if staged_turn is not None:
                staged_turn["status"] = "pending" if ambiguous else "failed"
                staged_turn["error"] = str(e)
                self._persist_command_status(run_id, staged_turn, action_index=staged_index)
            if ambiguous and staged_turn is not None:
                return {**staged_turn["command"], "status": "executing", "error": str(e)}
            return {"status": "failed", "error": str(e), "event_type": etype}

    def _reconcile_pending(self, run_id: str, history: list) -> bool:
        """Refresh persisted pending action rows before allowing another ordered action.

        Chat storage is append-only, so terminal reconciliations are persisted as compact
        ``command_status`` rows. ``_load_chat`` folds those rows back into their original action; this
        keeps the transcript to one visible action while making the outcome survive a new process.
        Transport/5xx failures leave the row pending. A 404 is terminal only for a command whose POST
        was confirmed: a pre-POST/ambiguous staged row still owns its exact key+intent, so it first
        replays that same logical request through the bounded command client. 401/403 keep it pending
        so restored credentials can retry without lying about the command's outcome.
        """
        unresolved = False
        for action_index, turn in enumerate(history):
            if turn.get("role") != "action" or turn.get("status") != "pending":
                continue
            command = turn.get("command") or {}
            command_id = command.get("id")
            label = (turn.get("action") or {}).get("label") or command.get("event_type") or "action"
            if not command_id:
                turn["status"] = "failed"
                turn["error"] = "pending command has no durable id; its outcome cannot be verified"
                self._persist_command_status(run_id, turn, action_index=action_index)
                self.console.print(_command_failure_line(label, turn['error']))
                continue
            try:
                record = self.api.get_run_command(run_id, command_id)
            except ApiError as exc:
                # ``TuiApi._command_record`` reports a malformed/mismatched 200 envelope as an
                # ApiError(status=200).  That is an authoritative local protocol failure, not a
                # transient outage: keeping it pending would block every later control forever.
                replay = _staged_replay(turn) if exc.status == 404 else None
                if replay is not None:
                    key, event_type, data, expected_generation = replay
                    try:
                        # GET may race a still-queued first POST. Re-submit the exact durable key and
                        # payload; server idempotency serializes both arrivals into one command/event.
                        record = self.api.run_command(
                            run_id, event_type, data, wait_s=2.0, idempotency_key=key,
                            expected_generation=expected_generation)
                    except ApiError as retry_exc:
                        if command_error_transient(retry_exc) or retry_exc.status in (401, 403):
                            unresolved = True
                            kind = ("access denied" if retry_exc.status in (401, 403)
                                    else "same-command status unavailable")
                            self.console.print(
                                f"  [yellow]…[/yellow] {_esc(label)} — {kind}: {_esc(retry_exc)}")
                            continue
                        turn["status"] = "failed"
                        turn["error"] = f"staged command could not be recovered: {retry_exc}"
                        self._persist_command_status(run_id, turn, action_index=action_index)
                        self.console.print(_command_failure_line(label, turn['error']))
                        continue
                elif exc.status in (200, 404):
                    turn["status"] = "failed"
                    if exc.status == 200:
                        prefix = "invalid command response"
                    elif command.get("submit_unconfirmed") is True:
                        prefix = "staged command recovery data is invalid"
                    else:
                        prefix = "command record is unavailable"
                    turn["error"] = f"{prefix}: {exc}"
                    self._persist_command_status(run_id, turn, action_index=action_index)
                    self.console.print(_command_failure_line(label, turn['error']))
                    continue
                if replay is None:
                    unresolved = True
                    kind = "access denied" if exc.status in (401, 403) else "status unavailable"
                    self.console.print(f"  [yellow]…[/yellow] {_esc(label)} — {kind}: {_esc(exc)}")
                    continue

            turn["command"] = _observed_command(record, command)
            status = record.get("status")
            if status in _COMMAND_DONE:
                turn["status"] = "done"
                turn.pop("error", None)
                self._persist_command_status(run_id, turn, action_index=action_index)
                suffix = " (already satisfied)" if status == "noop" else ""
                self.console.print(f"  [green]✓[/green] [cyan]{_esc(label)}[/cyan]{suffix}")
            elif status in _COMMAND_FAILED:
                turn["status"] = "failed"
                turn["error"] = _command_error(record)
                self._persist_command_status(run_id, turn, action_index=action_index)
                self.console.print(_command_failure_line(label, turn['error']))
            elif status in _COMMAND_PENDING:
                unresolved = True
            else:
                unresolved = True
                self.console.print(
                    f"  [yellow]…[/yellow] {_esc(label)} — unexpected command status {status or 'missing'}")
        return not unresolved

    def _persist_command_status(self, run_id: str, turn: dict, *, action_index: Optional[int] = None) -> None:
        command = turn.get("command") or {}
        update = {
            "role": "command_status",
            "command_id": command.get("id"),
            "status": turn.get("status"),
            "command": command,
        }
        if action_index is not None:
            update["action_index"] = action_index
        if turn.get("error"):
            update["error"] = turn["error"]
        self._persist(run_id, update)

    def _append_and_show(self, run_id: str, history: list, turn: dict) -> None:
        from rich.markdown import Markdown
        history.append(turn)
        self.console.print("[bold cyan]boss ›[/bold cyan]")
        self.console.print(Markdown(turn.get("content", "")))
        self._persist(run_id, turn)

    def _persist(self, run_id: str, turn: dict) -> bool:
        """Best-effort durable append to the run's chat.jsonl (the same sidecar the web UI writes), so a
        TUI conversation survives and shows up in the browser too. Never fails the chat on error."""
        try:
            self.api.post(f"/api/runs/{_url_id(run_id)}/chat-log", turn)
        except ApiError:
            return False
        return True

    def _load_chat(self, run_id: str) -> list:
        try:
            rows = list(self.api.get(f"/api/runs/{_url_id(run_id)}/chat-log") or [])
        except ApiError:
            return []
        history: list[dict] = []
        by_command: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("role") == "command_status":
                row_command_id = row.get("command_id")
                target = by_command.get(str(row_command_id or ""))
                row_command = row.get("command") if isinstance(row.get("command"), dict) else {}
                staged_id = row_command.get("staged_id")
                if target is None and staged_id:
                    target = by_command.get(str(staged_id))
                if target is None and not row_command_id and isinstance(row.get("action_index"), int):
                    index = row["action_index"]
                    if 0 <= index < len(history) and history[index].get("role") == "action":
                        target = history[index]
                if target is not None:
                    target["status"] = row.get("status") or target.get("status")
                    if isinstance(row.get("command"), dict):
                        target["command"] = row["command"]
                    if row.get("error"):
                        target["error"] = row["error"]
                    else:
                        target.pop("error", None)
                    current_id = (target.get("command") or {}).get("id")
                    if current_id:
                        by_command[str(current_id)] = target
                continue
            history.append(row)
            if row.get("role") == "action":
                command_id = (row.get("command") or {}).get("id")
                if command_id:
                    by_command[str(command_id)] = row
        return history

    def _run_help(self) -> None:
        self.console.print(
            "[dim]Just say what you want — the boss turns it into actions and the run applies them.\n"
            "  e.g. “you have 20 more nodes, try a few neural nets”, “focus on feature engineering”,\n"
            "       “what’s working so far?”, “promote the best node”.\n"
            "Quick controls: [bold]pause[/bold] · [bold]resume[/bold] · [bold]stop[/bold] · "
            "[bold]status[/bold] · [bold]back[/bold] · [bold]q[/bold]uit[/dim]")

    def _pause(self, msg: str) -> None:
        if msg:
            self.console.print(f"[yellow]{_esc(msg)}[/yellow]")
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
    child = None
    try:
        base, child = ensure_server(server, run_root, log=lambda m: console.print(f"[dim]{_esc(m)}[/dim]"))
    except ApiError as e:
        console.print(f"[red]{_esc(e)}[/red]")
        return 1
    except KeyboardInterrupt:                               # Ctrl-C while waiting for autostart
        console.print("[dim]bye[/dim]")
        return 0
    try:
        Tui(Api(base), run_root).dashboard()
    except (_Quit, KeyboardInterrupt):                      # global quit / a Ctrl-C that escaped a turn
        pass
    finally:
        _stop_child(child)                                  # never leak the server we launched
    console.print("[dim]bye[/dim]")
    return 0
