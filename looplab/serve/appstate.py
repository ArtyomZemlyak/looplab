"""Shared per-app state for the UI server's routers (BACKLOG §4: the split of `make_app`).

One `AppState` is built by `serve/server.py::make_app` and handed to every
`serve/routers/*.build_router(srv)`; handlers stay closures over it, exactly as they were closures
over `make_app`'s locals. Helper bodies (`run_dir`/`events`/`state_payload`/`phase`) are verbatim
moves of the former closures. Two callables are LATE-BOUND to break the route-calls-route cycles
(`list_runs_fn` is set by the runs router and read by the scope reports; `list_tasks_fn` is set by
the misc router and read by genesis).

`make_llm_client` deliberately resolves through the `looplab.serve.server` module attribute AT CALL
TIME: the test suite (and any operator tooling) monkeypatches `looplab.server.make_llm_client`, and
the flat alias + this late binding keep that single patch point working for every router."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from fastapi import HTTPException

from looplab.core.models import Event
from looplab.events.eventstore import iter_jsonl
from looplab.events.replay import fold
from looplab.serve.engine_proc import _engine_alive
from looplab.serve.jobs import JobRegistry
from looplab.serve.llm_context import llm_settings
from looplab.serve.projects import ProjectStore
from looplab.serve.protocol import (
    PHASE_APPROVAL, PHASE_FINISHED, PHASE_GROUNDING, PHASE_ONBOARDING, PHASE_PAUSED,
    PHASE_SEARCH, PHASE_SPEC_APPROVAL)
from looplab.serve.settings_store import SettingsStore

# run-root subdirectories that are NOT runs and must never be used as a run_id (would collide with the
# cross-run scope-report store at <run-root>/reports/).
_RESERVED_RUN_IDS = {"reports", "assistant"}


class AppState:
    """Plain state bag + canonical read helpers shared by the routers of ONE app instance."""

    def __init__(self, root: Path, projects: ProjectStore, settings: SettingsStore,
                 jobs: JobRegistry):
        self.root = root
        self.projects = projects
        self.settings = settings
        self.jobs = jobs
        self.summary_cache: dict[str, tuple] = {}   # run_id -> (size, mtime, summary); skips re-folding
        self.reports_dir = root / "reports"
        # Late-bound route callables (set by their owning router's build_router; see module docstring).
        self.list_runs_fn: Optional[Callable[[], list]] = None
        self.list_tasks_fn: Optional[Callable[[], dict]] = None

    # ------------------------------------------------------------------ helpers
    def run_dir(self, run_id: str) -> Path:
        rd = (self.root / run_id).resolve()
        if self.root != rd and self.root not in rd.parents:   # path-traversal guard
            raise HTTPException(404, "no such run")
        if not (rd / "events.jsonl").exists():
            raise HTTPException(404, "no such run")
        return rd

    def events(self, rd: Path, upto_seq: Optional[int] = None) -> list[Event]:
        evs = [Event(**o) for o in iter_jsonl(rd / "events.jsonl")]
        if upto_seq is not None:
            evs = [e for e in evs if e.seq <= upto_seq]
        return evs

    def state_payload(self, rd: Path, upto_seq: Optional[int] = None) -> dict:
        evs = self.events(rd, upto_seq)
        st = fold(evs)
        last_seq = evs[-1].seq if evs else -1
        # Trim heavy per-node payloads from the live state (code/files/stdout/error) — they are
        # fetched on demand via /nodes/{id}. Keeps SSE ticks small even for code-writing runs.
        d = st.model_dump(mode="json")
        better = (lambda a, b: a < b) if st.direction == "min" else (lambda a, b: a > b)
        for n in d.get("nodes", {}).values():
            n.pop("code", None); n.pop("files", None)
            n["stdout_tail"] = (n.get("stdout_tail") or "")[:160]
            n["error"] = (n.get("error") or "")[:160]
            # Intra-node sweep: a node can carry many trials — replace the full array with a compact
            # summary for the live state (card badge + spark + explode-hull header). The full trials
            # ride along the on-demand /nodes/{id} detail endpoint, like code/files do.
            trials = n.pop("trials", None) or []
            if trials:
                vals = [t.get("metric") for t in trials if t.get("metric") is not None]
                best = None
                for m in vals:
                    if best is None or better(m, best):
                        best = m
                ok = sum(1 for t in trials if t.get("metric") is not None and not t.get("error"))
                n["trials_summary"] = {
                    "count": len(trials), "best": best, "ok": ok, "failed": len(trials) - ok,
                    "series": vals[:64],   # cap the inline sparkline series
                }
        d["phase"] = self.phase(st)
        # Liveness: is a real engine process driving this run RIGHT NOW? (lock probe, not the event log).
        # A run with finished=False but engine_running=False is a ZOMBIE — the UI uses this to stop
        # showing a perpetual "thinking" strip and to resume on the next engine-needing chat action.
        d["engine_running"] = _engine_alive(rd)
        return {"state": d, "seq": last_seq, "max_seq": last_seq}

    def phase(self, st) -> str:
        if st.finished:
            return PHASE_FINISHED
        if st.paused:
            return PHASE_PAUSED
        if st.awaiting_approval:
            return PHASE_APPROVAL
        if st.spec_approval_requested and not st.spec_confirmed:
            return PHASE_SPEC_APPROVAL
        if st.proposed_spec is not None and not st.spec_confirmed:
            return PHASE_ONBOARDING
        if not st.nodes and st.data_profile is None and st.run_id:
            return PHASE_GROUNDING
        return PHASE_SEARCH

    def llm_settings(self, rd: Optional[Path] = None):
        """Per-run LLM settings (see `llm_context.llm_settings`) over THIS app's settings store."""
        return llm_settings(self.settings, rd)

    def make_llm_client(self, *args, **kwargs):
        """Late-bound client factory — resolves `looplab.serve.server.make_llm_client` at call time
        so a monkeypatch of `looplab.server.make_llm_client` reaches every router (see module doc)."""
        from looplab.serve import server as _server
        return _server.make_llm_client(*args, **kwargs)
