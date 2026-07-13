"""The TUI's pure helpers + server autostart, split verbatim out of `serve/tui.py` (docs/15 §P5.2):
metric/age formatting, phase glyphs, genesis-spec rendering/gating, chat-history shaping, input
parsing and redraw signatures (all side-effect-free, so tests/test_tui.py exercises them without a
live server or a terminal), plus the `ensure_server`/`_free_port`/`_stop_child` autostart trio the
REPL's `main` uses. `serve/tui.py` re-exports every name, so the old import paths keep working."""
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

# The one looplab import this module allows itself: the wire-protocol vocabulary it shares with the
# server (phase names). Everything else stays stdlib so the TUI adds no dependencies.
from looplab.serve.protocol import (PHASE_APPROVAL, PHASE_FINALIZING, PHASE_FINISHED, PHASE_GROUNDING,
                                    PHASE_ONBOARDING, PHASE_PAUSED, PHASE_SEARCH,
                                    PHASE_SPEC_APPROVAL)
from looplab.serve.tui_api import Api, ApiError

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
    elif task:
        # A COMPOSABLE (kind-less) genesis task — Genesis proposes these with NO `kind`, so the
        # branches above skip them; still surface the substance of the run (goal + capabilities), not
        # just the run-name/settings, so the operator sees what they're about to spend tokens on.
        if task.get("goal"):
            out.append(f"goal     : {task['goal']}")
        if task.get("direction"):
            out.append(f"direction: {task['direction']}")
        for lbl, key in (("repo", "editable_path"), ("repo", "repo"), ("data", "data_path"),
                         ("dataset", "dataset"), ("cmd", "cmd"), ("competition", "competition")):
            if task.get(key):
                out.append(f"{lbl:<9}: {task[key]}")
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
    """None when the spec is launchable, else a short reason it isn't (mirrors the backend truth,
    `EvalSpec._command_or_stages`, so the TUI never fires a doomed /api/start — see the BACKLOG
    "unify the launch-readiness gate" item). Keeps the launch button honest."""
    if not spec:
        return "no plan yet — describe a goal first"
    if not (spec.get("run_id") or "").strip():
        return "the run needs a name"
    task = spec.get("task") or {}
    if spec.get("task_file"):
        return None
    if not task:
        return "the boss hasn't picked a task yet"
    from looplab.adapters.tasks import normalize_task
    try:
        task = normalize_task(task)           # composable (repo/dataset/cmd/kaggle) -> canonical + kind
    except ValueError as e:
        # A half-assembled / malformed spec (a string `cmd`, no recognizable capability field) must
        # read as "not launchable yet + why", never crash the genesis screen (mega-review fix).
        return str(e)
    if task.get("kind") == "mlebench_real" and not (task.get("competition") or "").strip():
        return "set a Kaggle competition id"
    if task.get("kind") == "repo":
        if not (task.get("editable_path") or task.get("editables")):
            return "a repo task needs a `repo` path"
        # EvalSpec accepts a `command` OR a `stages` pipeline (see EvalSpec._command_or_stages), so an
        # operator stages-only `cmd:{stages:[…]}` is launchable — the gate must not demand `command`.
        _eval = task.get("eval")
        has_cmd = isinstance(_eval, dict) and (_eval.get("command") or _eval.get("stages"))
        if not (has_cmd or task.get("onboard")):
            return "a repo task needs a `cmd` (a command or a stages pipeline, or metric reader \"auto\")"
    return None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(s: str) -> str:
    """run-id normaliser (lowercase kebab, ≤40) — must stay in step with the server's own
    slugify+de-dup in serve/routers/genesis.py::_normalize_genesis."""
    return _SLUG_RE.sub("-", str(s or "").lower()).strip("-")[:40]


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
