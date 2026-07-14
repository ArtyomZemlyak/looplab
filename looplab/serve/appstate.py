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
from looplab.engine.finalize import incomplete_finalize_scope
from looplab.events.eventstore import iter_jsonl
from looplab.events.replay import fold
from looplab.serve.engine_proc import _engine_liveness
from looplab.serve.jobs import JobRegistry
from looplab.serve.llm_context import llm_settings
from looplab.serve.projects import ProjectStore
from looplab.serve.protocol import (
    PHASE_APPROVAL, PHASE_FINALIZING, PHASE_FINISHED, PHASE_GROUNDING, PHASE_ONBOARDING, PHASE_PAUSED,
    PHASE_SEARCH, PHASE_SPEC_APPROVAL, RUN_GENERATION_FIELD)
from looplab.serve.reviews import ReviewStore
from looplab.serve.run_commands import RunCommandService, run_generation_token
from looplab.serve.settings_store import SettingsStore

# run-root subdirectories that are NOT runs and must never be used as a run_id (would collide with the
# cross-run scope-report store at <run-root>/reports/).
_RESERVED_RUN_IDS = {"reports", "assistant", ".reviews", ".command-locks"}

# Fields that can contain verbatim source, captured process output, private host paths, or an internal
# model-facing prompt. `state_payload` feeds both the public /state GET and headerless EventSource SSE,
# so token auth cannot protect them. Keep that projection useful, but recursively remove raw material
# wherever it is nested (not only under nodes — inject_requests also carries full code/file maps).
_PUBLIC_STATE_RAW_KEYS = {
    "abs_path", "code", "deleted", "files", "preview", "raw", "stderr", "stdout", "stdout_tail",
    "triage_rationale",
}


def _public_state_value(value):
    from looplab.trust.redact import redact_secrets

    if isinstance(value, dict):
        return {k: _public_state_value(v) for k, v in value.items()
                if str(k) not in _PUBLIC_STATE_RAW_KEYS}
    if isinstance(value, list):
        return [_public_state_value(v) for v in value]
    if isinstance(value, tuple):
        return [_public_state_value(v) for v in value]
    if isinstance(value, str):
        # entropy=False (F25): the entropy heuristic masked legitimate high-entropy IDENTIFIERS
        # (config_hash, data_provenance content digests, run-slugs like `runs/exp_2026_ablation_v3`)
        # as ***REDACTED*** on the public /state, breaking any UI/client logic keyed on them. Keep only
        # the known-secret-PATTERN redaction here (sk-…/AWS-key shapes — no usability cost); the one
        # free-form field where an unknown-format secret could realistically appear, node `error`, still
        # gets full entropy redaction on its own path in `state_payload`.
        return redact_secrets(value, entropy=False)
    return value


class AppState:
    """Plain state bag + canonical read helpers shared by the routers of ONE app instance."""

    def __init__(self, root: Path, projects: ProjectStore, settings: SettingsStore,
                 jobs: JobRegistry, reviews: ReviewStore | None = None,
                 resume_cancel=None):
        self.root = root
        self.projects = projects
        self.settings = settings
        self.jobs = jobs
        self.reviews = reviews or ReviewStore(root / ".reviews")
        self.commands = RunCommandService(self)
        self.resume_cancel = resume_cancel
        # File identity + content metadata mirror state_payload's reset-safe cache signature.
        self.summary_cache: dict[str, tuple] = {}  # run_id -> (ino, ctime_ns, size, mtime_ns, summary)
        # Per-run folded-state cache keyed by (size, mtime, upto_seq): state_payload re-read + re-folded
        # the WHOLE events.jsonl on every SSE tick (every ~0.4s per client), O(n²) for a repo run whose
        # node_created events embed full file sets. The live-only `engine_running` is re-stamped on a hit.
        self._state_cache: dict[tuple, tuple] = {}
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

    def state(self, rd: Path):
        """`fold(self.events(rd))` — the routers' one-line state hydration (previously spelled out
        at ~16 call sites). DELIBERATELY uncached: engine invariant #4 (state is only observed via
        a fresh fold of the log) — the SSE hot path has its own size+mtime-keyed cache in
        `state_payload`, which is a *payload* cache, never a folded-state handle reused across
        requests."""
        return fold(self.events(rd))

    def state_payload(self, rd: Path, upto_seq: Optional[int] = None) -> dict:
        # Cache the expensive fold+dump+trim by (events.jsonl size, mtime, upto_seq): unchanged log ->
        # reuse the trimmed payload, only re-stamping the live `engine_running` (a lock probe, not the
        # log). Bounds the SSE hot path from O(events) per tick to a stat() + a dict copy.
        try:
            stt = (rd / "events.jsonl").stat()
            # Include file identity/creation time, not only mutable content metadata. Reset archives
            # events.jsonl and creates a replacement that can reuse seq numbers and even the same
            # size/mtime; it must never hit generation A's cached payload for generation B.
            ckey = (str(rd), stt.st_ino, stt.st_ctime_ns, stt.st_size, stt.st_mtime_ns, upto_seq)
        except OSError:
            ckey = None
        if ckey is not None:
            hit = self._state_cache.get(ckey)
            if hit is not None:
                d, last_seq, max_seq, generation, event_count = hit
                out = dict(d)
                # Liveness is a present-time fact. Stamping it into an old prefix fold creates a
                # hybrid object that is neither historical nor live.
                out["engine_running"] = _engine_liveness(rd) if upto_seq is None else None
                return {"state": out, "seq": last_seq, "max_seq": max_seq,
                        "event_count": event_count,
                        RUN_GENERATION_FIELD: generation or None}
        all_evs = self.events(rd)
        generation = run_generation_token(all_evs)
        # This is the count of the full recoverable folded projection, even for a historical
        # ``upto_seq`` fold. It must not be inferred from seq: repaired logs may contain gaps. The
        # raw timeline pager deliberately applies an additional strict-monotonic-seq boundary.
        event_count = len(all_evs)
        max_seq = all_evs[-1].seq if all_evs else -1
        evs = all_evs if upto_seq is None else [e for e in all_evs if e.seq <= upto_seq]
        st = fold(evs)
        last_seq = evs[-1].seq if evs else -1
        # Trim heavy per-node payloads from the live state (code/files/stdout/error) — they are
        # fetched on demand via /nodes/{id}. Keeps SSE ticks small even for code-writing runs.
        d = _public_state_value(st.model_dump(mode="json"))
        better = (lambda a, b: a < b) if st.direction == "min" else (lambda a, b: a > b)
        from looplab.trust.redact import redact_secrets
        for n in d.get("nodes", {}).values():
            n.pop("code", None)
            n.pop("files", None)
            # SECURITY (arch-review §4 P1-3): /state is a LIGHT projection served WITHOUT the UI token,
            # so it must not ship raw captured program output — a secret the candidate prints could ride
            # in the stdout tail. Drop stdout_tail entirely (the full tail is behind the token-gated
            # node-detail endpoint) and redact the short error message the node table still shows.
            n.pop("stdout_tail", None)
            n["error"] = redact_secrets((n.get("error") or "")[:160])
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
        # Two durable protocols coexist: branch-scoped projection markers and upstream's
        # finish-seq handshake (`finalization_required` -> `finalization_finished`). Legacy
        # markerless finishes fold as already finalized, so the union does not manufacture work.
        finalize_incomplete = (
            incomplete_finalize_scope(evs) is not None or st.finalization_pending())
        d["finalization_incomplete"] = finalize_incomplete
        d["phase"] = self.phase(st, finalize_incomplete=finalize_incomplete)
        # Liveness: is a real engine process driving this run RIGHT NOW? (lock probe, not the event log).
        # A run with finished=False but engine_running=False is a ZOMBIE — the UI uses this to stop
        # showing a perpetual "thinking" strip and to resume on the next engine-needing chat action.
        d["engine_running"] = _engine_liveness(rd) if upto_seq is None else None
        if ckey is not None:                 # cache the trimmed payload for the next unchanged tick
            self._state_cache[ckey] = (d, last_seq, max_seq, generation, event_count)
            if len(self._state_cache) > 256:  # bound the cache (many runs / seq points over a session)
                self._state_cache.pop(next(iter(self._state_cache)))
        return {"state": d, "seq": last_seq, "max_seq": max_seq,
                "event_count": event_count,
                RUN_GENERATION_FIELD: generation or None}

    def phase(self, st, *, finalize_incomplete: bool = False) -> str:
        # A pending run_abort is not an ordinary pause: the engine must preserve it, write
        # run_finished, and complete the wrap-up. Surface this before paused because finalize-after-
        # stop intentionally has both stop_requested and paused set. An error finish is not a
        # successful finalize either: explicit retry preserves the stop and re-enters wrap-up.
        if finalize_incomplete or (st.stop_requested and (
                not st.finished or str(st.stop_reason or "").lower() == "error")):
            return PHASE_FINALIZING
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
