"""Shared per-app state for the UI server's routers (BACKLOG §4: the split of `make_app`).

One `AppState` is built by `serve/server.py::make_app` and handed to every
`serve/routers/*.build_router(srv)`; handlers stay closures over it, exactly as they were closures
over `make_app`'s locals. Helper bodies (`run_dir`/`events`/`state_payload`/`phase`) are verbatim
moves of the former closures. Two callables are LATE-BOUND to break the route-calls-route cycles
(`list_tasks_fn` is set by the misc router and read by genesis; `list_runs_fn` is set by the runs
router and, since doc 25 SR-12 gave the scope reports their own `run_membership()` method, read by
nothing in production). Both are rows in `serve/router_wiring.py`, which is where the producer, the
consumers and the mount-time check that they line up are written down — an assignment or a read
that is not in that registry fails the suite.

`make_llm_client` deliberately resolves through the `looplab.serve.server` module attribute AT CALL
TIME: the test suite (and any operator tooling) monkeypatches `looplab.server.make_llm_client`, and
the flat alias + this late binding keep that single patch point working for every router."""
from __future__ import annotations

import os
import stat
import threading
from pathlib import Path
from typing import Callable, Optional

from fastapi import HTTPException

from looplab.core.atomicio import file_identity
from looplab.core.models import Event
from looplab.core.run_deletion import RUN_DELETION_FENCE_PREFIX
from looplab.core.trace_files import open_private_trace_file, trace_file_change_token
from looplab.engine.finalize import incomplete_finalize_scope
from looplab.events.authoring_projection import card_authoring
from looplab.events.eventstore import integrity_wire, iter_event_jsonl, log_integrity
from looplab.events.replay import fold
from looplab.events.types import EV_NODE_CREATED
from looplab.serve.deletion_transaction import (
    DELETE_IDENTITY_PREFIX, DELETE_QUARANTINE_PREFIX, DELETE_RECEIPT_PREFIX)
from looplab.serve.engine_proc import _engine_liveness
from looplab.serve.jobs import JobRegistry
from looplab.serve.llm_context import global_settings, llm_settings
from looplab.serve.projects import ProjectStore
from looplab.serve.protocol import (
    PHASE_APPROVAL, PHASE_FINALIZING, PHASE_FINISHED, PHASE_GROUNDING, PHASE_ONBOARDING, PHASE_PAUSED,
    PHASE_SEARCH, PHASE_SPEC_APPROVAL, RUN_GENERATION_FIELD)
from looplab.serve.public_cards import (INTERNAL_CARD_STATE_FIELDS,
                                        REVIEW_OMITTED_CARD_FIELDS, public_cards_projection)
from looplab.serve.reviews import ReviewStore
from looplab.serve.run_commands import RunCommandService, run_generation_token
from looplab.serve.settings_store import SettingsStore

# Run-root entries that are NOT runs and must never be used as a run_id. The subdirectories would
# collide with the stores that own them (the cross-run scope reports at <run-root>/reports/, …); the
# FILES are the same hazard one level down — `safe_run_dir`'s events.jsonl conflict check passes for a
# file (a file has no events.jsonl child), so /api/start with run_id "secrets.json" used to reserve a
# start record and then either fail late at mkdir or, when the file did not exist yet, OCCUPY the path
# and wedge the later store_secret/os.replace. Names only: the not-yet-created case has nothing on disk
# to test. `safe_run_dir` additionally rejects any run_id that names an existing non-directory, which
# covers server-owned files added later, and the digest-suffixed lifecycle/trace-clear receipts by
# prefix (they cannot be enumerated).
_RESERVED_RUN_IDS = {
    "reports", "assistant", ".reviews", ".reviews.lock", ".command-locks",
    "ui_settings.json", "ui_settings.json.lock",
    "secrets.json", "secrets.json.lock",
    ".settings-launch.lock",
    "projects.json", "projects.json.lock",
}
# `engine_proc._lifecycle_lock_path` builds `<run-root>/.looplab-lifecycle-<digest>.lock`.
_LIFECYCLE_LOCK_PREFIX = ".looplab-lifecycle-"
# `control._trace_clear_receipt_path` builds
# `<run-root>/.trace-clear.<run-digest>.tc_<operation-id>.json`.
_TRACE_CLEAR_RECEIPT_PREFIX = ".trace-clear."
# Whole-run Replay receipts are also root-side service files keyed by a run-path digest.
_RESET_RECEIPT_PREFIX = ".looplab-reset-receipt-"
# Whole-run deletion uses one root fence plus operation-bound receipt/quarantine/identity entries.
# IMPORTED, not respelled: these names are what the deletion writers build their filenames from, and
# a hand-copied prefix here does not fail when a writer's changes — it silently stops recognizing
# that writer's files as service files, which at this call site means `run_dir` treats one as a run
# id. The identity sidecar was missing from the hand-written set for exactly that reason.
_DELETE_SERVICE_PREFIXES = (
    RUN_DELETION_FENCE_PREFIX, DELETE_RECEIPT_PREFIX,
    DELETE_QUARANTINE_PREFIX, DELETE_IDENTITY_PREFIX,
)

# Fields that can contain verbatim source, captured process output, private host paths, or an internal
# model-facing prompt. `state_payload` feeds both the public /state GET and headerless EventSource SSE,
# so token auth cannot protect them. Keep that projection useful, but recursively remove raw material
# wherever it is nested (not only under nodes — inject_requests also carries full code/file maps).
_PUBLIC_STATE_RAW_KEYS = {
    "abs_path", "annotations", "code", "comments", "deleted", "files", "preview", "raw",
    "stderr", "stdout", "stdout_tail", "stderr_tail", "triage_rationale",
}


def _public_state_value(value):
    from looplab.core.redact import redact_secrets

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
        # Keyed on `atomicio.file_identity` — the SAME canonical signature `state_payload`'s
        # reset-safe cache key is built from, not a narrower mirror of it. The comment here used to
        # claim that equivalence while `run_projections` spelled the tuple by hand minus `st_dev` and
        # the Windows reparse field, which is how the two drifted apart unnoticed (doc 25 SC-11).
        self.summary_cache: dict[str, tuple] = {}  # run_id -> (file_identity(events.jsonl), summary)
        # Per-run folded-state cache keyed by (size, mtime, upto_seq): state_payload re-read + re-folded
        # the WHOLE events.jsonl on every SSE tick (every ~0.4s per client), O(n²) for a repo run whose
        # node_created events embed full file sets. The live-only `engine_running` is re-stamped on a hit.
        self._state_cache: dict[tuple, tuple] = {}
        # Guards the state-cache insert+evict for the same reason as the trace-view lock below: /state,
        # the SSE stream, and the /trace + /nodes routes (via trace_scalars -> state_payload) all reach
        # state_payload concurrently on the threadpool, and `pop(next(iter(dict)))` on a dict another
        # thread is inserting into raises "dictionary changed size during iteration" (a 500).
        self._state_cache_lock = threading.Lock()
        # Per-run event-log INTEGRITY receipt, keyed by the same `file_identity` (see `log_integrity`).
        # Deliberately a separate map from `_state_cache`: that one is keyed by (run, identity, seq,
        # audience) and holds four entries per run, while this answers ONE question about the file and
        # is read by the run LIST too — which never builds a state payload at all. Shares
        # `_state_cache_lock` because both are small dict ops under the same reader fan-out.
        self._integrity_cache: dict[str, tuple] = {}
        # Run-level light trace-view cache keyed by (spans.jsonl, events.jsonl) file identity. The Dock
        # refetches /trace on every node add/settle and polls it while a node builds; without this each
        # fetch rebuilt the view. Combined with the span index (which makes the span read O(new spans)),
        # an unchanged run's trace is served from here instantly. See `trace_view`.
        self._trace_view_cache: dict[str, tuple] = {}
        # Guards the trace-view cache's insert+evict: the FastAPI threadpool runs `trace_view`
        # concurrently, and `pop(next(iter(dict)))` on a dict another thread is inserting into raises
        # "dictionary changed size during iteration". Held only around the cheap dict ops, never the
        # (slow) span read + build below.
        self._trace_view_lock = threading.Lock()
        self.reports_dir = root / "reports"
        # Late-bound route callables (set by their owning router's build_router; see module docstring
        # and the `serve/router_wiring.py` registry that enumerates producer + consumers).
        # `list_runs_fn` remains for the LIVE-fact overlay only (the runs router's own route body,
        # which probes engine liveness); the two SIDE-EFFECT-FREE projections are methods below
        # (doc 25 SR-12), so a reader no longer depends on which build_router calls have run.
        self.list_runs_fn: Optional[Callable[[], list]] = None
        # The runnable-task catalogue `GET /api/tasks` returns, late-bound for the genesis boss, which
        # grounds its proposed run spec on the same list. Reading it back off this bag is what keeps
        # `routers/genesis.py` and `routers/misc.py` independent leaves — neither imports the other.
        self.list_tasks_fn: Optional[Callable[[], dict]] = None

    def run_summaries(self, only=None) -> list:
        """The mtime-cached per-run fold summaries, WITHOUT the live-fact overlay (doc 25 SR-12).

        `only` is an optional run-id set that bounds the fold; see `run_projections.run_summaries`."""
        from looplab.serve.run_projections import run_summaries
        return run_summaries(self, only=only)

    def run_membership(self) -> list:
        """Only the columns `reports._scope_run_ids` joins on. Side-effect free by construction."""
        from looplab.serve.run_projections import run_membership
        return run_membership(self)

    # ------------------------------------------------------------------ helpers
    def run_dir(self, run_id: str) -> Path:
        from looplab.core.run_deletion import (
            RunDeletionStorageError, load_run_deletion_fence)

        root = self.root.resolve()
        if (not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id
                or run_id in {".", ".."} or "/" in run_id or "\\" in run_id):
            raise HTTPException(404, "no such run")
        requested = root / run_id
        lowered = requested.name.lower()
        if (lowered in _RESERVED_RUN_IDS or lowered.startswith((
                _LIFECYCLE_LOCK_PREFIX, _TRACE_CLEAR_RECEIPT_PREFIX, _RESET_RECEIPT_PREFIX,
                *_DELETE_SERVICE_PREFIXES))):
            raise HTTPException(404, "no such run")
        try:
            fence = load_run_deletion_fence(requested)
        except RunDeletionStorageError as exc:
            raise HTTPException(503, {
                "code": "run_deletion_fence_unavailable",
                "message": "Deletion ownership cannot be verified for this run.",
            }) from exc
        if fence is not None:
            raise HTTPException(410, {
                "code": "run_deletion_in_progress",
                "operation_id": fence["operation_id"],
                "message": "This run is being deleted.",
            })
        try:
            entry = requested.lstat()
            rd = requested.resolve(strict=True)
            junction_fn = getattr(requested, "is_junction", None)
            junction = bool(callable(junction_fn) and junction_fn())
        except (FileNotFoundError, OSError) as exc:
            raise HTTPException(404, "no such run") from exc
        attributes = int(getattr(entry, "st_file_attributes", 0) or 0)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        # A run is a DIRECT CHILD of the root, and nothing else. Accepting any DESCENDANT (root is in
        # the parents of root/a/b/c) let a run_id like "run1/nodes/n3_ws" resolve to a sandbox-WRITABLE
        # node workspace: any events.jsonl the evaluated candidate wrote there became addressable as a
        # fake "run" by every caller — read routes, inject_node's `source_run` before commands
        # .validate_paths tightens it, assistant tooling — so candidate-authored events would be folded
        # and rendered to the operator, with unbounded server-side fold work behind it. Single-segment
        # HTTP route params largely masked it, but the command service had already had to re-restrict
        # to `canonical.parent == root`; this is the base helper, so it enforces the same rule (and
        # rejects the root itself EXPLICITLY, rather than relying on root never having an events.jsonl).
        if (rd.parent != root or rd != requested.resolve(strict=False)
                or stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode)
                or bool(attributes & reparse_flag) or junction):
            raise HTTPException(404, "no such run")
        events = rd / "events.jsonl"
        try:
            event_entry = events.lstat()
            event_target = events.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise HTTPException(404, "no such run") from exc
        event_attributes = int(getattr(event_entry, "st_file_attributes", 0) or 0)
        if (stat.S_ISLNK(event_entry.st_mode) or not stat.S_ISREG(event_entry.st_mode)
                or bool(event_attributes & reparse_flag) or event_target != events
                or event_target.parent != rd):
            raise HTTPException(404, "no such run")
        return rd

    def events(self, rd: Path, upto_seq: Optional[int] = None) -> list[Event]:
        evs = [Event(**o) for o in iter_event_jsonl(rd / "events.jsonl")]
        if upto_seq is not None:
            evs = [e for e in evs if e.seq <= upto_seq]
        return evs

    def log_integrity(self, rd: Path) -> dict:
        """Is the prefix `events()` just returned the WHOLE log? — cached per file identity.

        `events()` reads through `iter_event_jsonl`, which STOPS at the first corrupt or non-dense
        record and reports nothing, and this class never builds an `EventStore` — so until 2026-08-14
        the divergence receipt was structurally unreachable from every HTTP surface: `/api/runs`,
        `/state`, the SSE stream, `/lifecycle`, `/nodes`, `/prov`, `/cost`, the review payload, the
        assistant's run context and the whole TUI. `runs/rubertlite-dense-retrieval` is what that
        costs: a 1,624-record log served as a confident 20-event run with `nodes: 2`.
        (Three surfaces had already solved it in isolation and each with its own vocabulary — the
        ConceptFrame's `source_integrity`, the attention feed's omit-and-flag, `RunStateCache`'s
        `[PARTIAL SOURCE]` note. This publishes the ConceptFrame's spelling from the SHARED reader so
        a fourth does not appear, and `eventstore.log_integrity` is the one derivation all of them
        can reach.)

        Keyed by `file_identity` exactly like `summary_cache`/`_state_cache`, so a finished run pays
        the scan once and a live run pays it on the same ticks it already re-folds on. Cost, measured
        on the corpus: 62 ms for the 12 MB rubertlite log and 33 ms for the 5.2 MB `rubert-dr-0807` —
        at or below the `iter_event_jsonl` + `Event(**o)` pass this sits beside (3.5 ms / 44 ms), and
        a divergent log stops the scan at its boundary rather than reading on.

        **IT IS A SECOND PASS OVER THE SAME FILE, AND THAT IS NOT REMOVABLE FROM HERE** (checked
        2026-08-15, because it reads like an obvious duplicate of the fold's own read and will again
        to the next person). The two passes answer different questions: `events()` reads through
        `iter_event_jsonl`, which STOPS at the first corrupt or non-dense record, while this receipt
        is precisely a statement about the bytes BEHIND that stop — `corrupt_line` and
        `dropped_lines` count complete records the reader by construction never looks at, which is
        why `log_divergence` continues past the boundary on purpose. Nor is the healthy answer
        derivable from the returned list: a stopping reader cannot tell "reached EOF" from "stopped
        at line 21", and an `append_many` envelope carries several events on one physical line, so no
        arithmetic over the folded events recovers the file's own record count. The way to pay ONCE
        is one walk that produces both — which belongs beside `iter_event_jsonl` and `log_divergence`
        in `events/eventstore.py`, where the line RULE already lives; re-deriving that rule here to
        save a read is the drift `core/jsonlio.py` exists to prevent.
        """
        log = rd / "events.jsonl"
        try:
            sig = file_identity(log.stat())
        except OSError:
            # No stat means no cache key, so answer uncached rather than not answering. `log_integrity`
            # decides: a path that cannot be scanned is `unreadable` (the direction that fails toward
            # "we cannot show you this run"), while an ABSENT log is `complete` — nothing was read, so
            # nothing is claimed, which is `log_divergence`'s own rule and unreachable from here
            # anyway (`run_dir` 404s and `run_summaries` skips a directory with no log).
            return integrity_wire(log_integrity(log))
        with self._state_cache_lock:
            hit = self._integrity_cache.get(str(rd))
            if hit is not None and hit[0] == sig:
                return dict(hit[1])
        # Widened to the fixed wire shape HERE, at the one point every HTTP surface reads through, so
        # `/state`, `/lifecycle`, the run list, `/cost` and `/log-page` cannot publish two shapes for
        # one file. The Python receipt stays minimal for the text surfaces that only read it.
        receipt = integrity_wire(log_integrity(log))
        with self._state_cache_lock:
            self._integrity_cache[str(rd)] = (sig, receipt)
            if len(self._integrity_cache) > 512:
                self._integrity_cache.pop(next(iter(self._integrity_cache)))
        return dict(receipt)

    def state(self, rd: Path):
        """`fold(self.events(rd))` — the routers' one-line state hydration (previously spelled out
        at ~16 call sites). DELIBERATELY uncached: engine invariant #4 (state is only observed via
        a fresh fold of the log) — the SSE hot path has its own size+mtime-keyed cache in
        `state_payload`, which is a *payload* cache, never a folded-state handle reused across
        requests."""
        return fold(self.events(rd))

    def state_payload(self, rd: Path, upto_seq: Optional[int] = None,
                      *, audience: str = "owner") -> dict:
        """`audience="review"` builds the same payload for a ONE-RUN review capability: the Card
        projection is narrowed so nothing describing sibling runs rides along (see
        `REVIEW_OMITTED_CARD_FIELDS`). Narrowing happens at projection time so the completeness
        receipt describes what the response actually carries — a scrub applied to a finished DTO
        would leave the receipt certifying data that is no longer there."""
        omit_fields = REVIEW_OMITTED_CARD_FIELDS if audience == "review" else frozenset()
        # Cache the expensive fold+dump+trim by (events.jsonl size, mtime, upto_seq): unchanged log ->
        # reuse the trimmed payload, only re-stamping the live `engine_running` (a lock probe, not the
        # log). Bounds the SSE hot path from O(events) per tick to a stat() + a dict copy.
        try:
            stt = (rd / "events.jsonl").stat()
            # Include file identity/creation time, not only mutable content metadata. Reset archives
            # events.jsonl and creates a replacement that can reuse seq numbers and even the same
            # size/mtime; it must never hit generation A's cached payload for generation B.
            # `audience` is part of the key: the review payload is a DIFFERENT projection of the same
            # log, and serving it from the owner entry (or vice versa) would leak across the boundary.
            # `file_identity`, not a hand-rolled tuple: this one used to omit `st_dev`, so two
            # runs whose logs shared an inode number across devices could collide on one key.
            ckey = (str(rd), *file_identity(stt), upto_seq, audience)
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
                out["source_integrity"] = self.log_integrity(rd)
                return {"state": out, "seq": last_seq, "max_seq": max_seq,
                        "event_count": event_count,
                        "source_integrity": self.log_integrity(rd),
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
        # exclude open event journals before Pydantic copies them, then publish only the
        # bounded derived DTO. This shared boundary feeds owner state, SSE, and review state.
        d = st.model_dump(mode="json", exclude=INTERNAL_CARD_STATE_FIELDS | {"cards"})
        # Scrub the broad folded state before inserting the independently bounded/redacted Card DTO.
        # Otherwise valid action params/search dimensions named "raw" or "code" are removed after the
        # Card projection has declared them exact, leaving a completeness receipt that describes data
        # no longer present on the wire.
        d = _public_state_value(d)
        # `cards` and its completeness receipt must come from one projection invocation.
        # Re-projecting the two halves separately would let mutable caller input or a later selector
        # change publish counts that do not describe the actual mapping in this SSE/state frame.
        card_fragment = public_cards_projection(st.cards, omit_fields=omit_fields).model_dump(mode="json")
        d.update(card_fragment)
        # DEPRECATED read-only `hypotheses` compat projection: the duplicate core Hypothesis model was
        # removed after Card became canonical, but docs/23 deferred the /state CONTRACT
        # retirement to a post-L6 window — without the key an external client can't tell "known empty"
        # from "schema removed". Re-derive one compatibility row per canonical Card work item, reusing the
        # ALREADY bounded+redacted Card DTO values so no raw card text leaks past the public boundary, in
        # old Hypothesis shape (`status` == the research `verdict`). Several rows may share one `belief_id`;
        # clients needing canonical beliefs should migrate to the Card/belief APIs. It
        # is read-only telemetry — never folded, never read by the engine — and clients should migrate to
        # `cards`.
        _card_dtos = card_fragment.get("cards")
        if isinstance(_card_dtos, dict):
            d["hypotheses"] = {
                c.id: {
                    "id": c.id, "statement": dto.get("statement", ""),
                    "source": dto.get("source", "researcher"), "status": dto.get("verdict", "open"),
                    "rationale": dto.get("rationale", ""), "evidence": list(dto.get("evidence") or []),
                    "created_at_node": dto.get("created_at_node", 0),
                    "best_delta": dto.get("best_delta"), "priority": dto.get("priority"),
                }
                for c in st.research_cards()
                if isinstance((dto := _card_dtos.get(c.id)), dict)
            }
        better = (lambda a, b: a < b) if st.direction == "min" else (lambda a, b: a > b)
        from looplab.core.redact import redact_secrets
        for n in d.get("nodes", {}).values():
            n.pop("code", None)
            n.pop("files", None)
            # SECURITY (arch-review §4 P1-3): /state is a LIGHT projection served WITHOUT the UI token,
            # so it must not ship raw captured program output — a secret the candidate prints could ride
            # in the stdout tail. Drop stdout_tail entirely (the full tail is behind the token-gated
            # node-detail endpoint) and redact the short error message the node table still shows.
            n.pop("stdout_tail", None)
            # …and the SCORED node's own stderr tail beside it, for the identical reason: it is
            # captured program output on the same untoken-gated projection, and a node that
            # scored is exactly as able to have printed a secret as one that crashed.
            # OPEN[stderr-tail-scrub-untested-at-the-boundary] the stdout drop one line up is
            # regression-tested; this new sibling pop has no test, on a DENY-LIST projection where
            # a dropped pop leaks captured output with nothing red.
            # proof:absent:stderr_tail@tests/test_server.py
            # REVIEW 2026-08-30 (security-guard): `tests/test_server.py` asserts the stdout tail is
            # absent from /state and still behind the token-gated detail; per CLAUDE.md's contract
            # rule, drive the same pair for this field (the reviews router is safe by construction
            # — its allow-list excludes both tails; /state is the one deny-style surface).
            n.pop("stderr_tail", None)
            # Redact BEFORE truncating: a secret straddling byte 160 would otherwise lose its tail,
            # leaving a prefix too short for the pattern/entropy rules to catch (fragment leak).
            n["error"] = redact_secrets(n.get("error") or "")[:160]
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
        # In-flight AUTHORING (derived, never folded — `events/authoring_projection.py`): the open
        # card-build head, which the fold cannot express as `Card.status` (see
        # `card_ledger.py::_card_building_ids` — folding the request would make the servicer of that
        # very head unable to claim it, and every speculative build would return "stale"). Measured on
        # rubertlite-dr-unified-v5: the board said "has not started" for 2,130 s of a 2,128 s build,
        # i.e. the Building lane was occupied for 0.3 seconds of it. Derived from `evs`, so an
        # `upto_seq` fold reports what was in flight THEN, and so it caches with the rest of the
        # payload.
        d["card_authoring"] = card_authoring(evs, st)
        # Liveness: is a real engine process driving this run RIGHT NOW? (lock probe, not the event log).
        # A run with finished=False but engine_running=False is a ZOMBIE — the UI uses this to stop
        # showing a perpetual "thinking" strip and to resume on the next engine-needing chat action.
        d["engine_running"] = _engine_liveness(rd) if upto_seq is None else None
        # MIRRORED into the projection as well as onto the envelope, and stamped in both the miss and
        # the hit path exactly like `engine_running`. The envelope is the canonical position (it is a
        # fact about the RECORD), but `state` is the object every browser consumer actually receives —
        # `useRunState` publishes the folded snapshot and not the frame around it — and a receipt that
        # does not travel with the thing it qualifies is a receipt nobody reads. Not stored in the
        # cache tuple: the receipt is keyed on the FILE, so a repair must be observed on the next tick
        # rather than inherited from a cached body.
        d["source_integrity"] = self.log_integrity(rd)
        if ckey is not None:                 # cache the trimmed payload for the next unchanged tick
            with self._state_cache_lock:      # only the dict ops; the fold/trim above ran lock-free
                self._state_cache[ckey] = (d, last_seq, max_seq, generation, event_count)
                if len(self._state_cache) > 256:  # bound the cache (many runs / seq points / session)
                    self._state_cache.pop(next(iter(self._state_cache)))
        # The receipt rides on the ENVELOPE beside `event_count`, not inside the folded `state`: it is
        # a fact about the RECORD, not about the run, and it must stay true for a historical
        # `upto_seq` fold too — an operator scrubbed to seq 12 of a truncated log is looking at a
        # prefix of a prefix. It is re-read on the cache hit above rather than stored in the cache
        # tuple for the same reason `engine_running` is: it is keyed on the file, not on (file, seq,
        # audience), so one map answers every entry and a repair between ticks is observed.
        return {"state": d, "seq": last_seq, "max_seq": max_seq,
                "event_count": event_count,
                "source_integrity": self.log_integrity(rd),
                RUN_GENERATION_FIELD: generation or None}

    def state_probe(self, rd: Path) -> dict:
        """Small current-lifecycle envelope for idle terminal clients.

        Reuse the size/mtime-keyed public-state cache so an unchanged run costs a stat + liveness
        probe, then transfer only identity fields. A client reopens the full SSE stream when any
        field changes; it never needs to download the complete folded state just to discover that a
        finished run is still finished.
        """
        payload = self.state_payload(rd)
        state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
        return {
            "schema": 1,
            "seq": payload.get("seq", -1),
            "event_count": payload.get("event_count"),
            # Carried into the probe as well: this envelope's whole job is to let an idle client
            # decide whether to reopen the stream, and its `event_count` is the number a truncated
            # log understates. A probe that reports `event_count: 20` with no receipt is the same
            # confident partial view one endpoint over.
            "source_integrity": payload.get("source_integrity"),
            RUN_GENERATION_FIELD: payload.get(RUN_GENERATION_FIELD),
            "engine_running": state.get("engine_running"),
        }

    def trace_scalars(self, rd: Path):
        """A lightweight state carrying ONLY the three fields the trace projections read
        (`build_trace_view` → run_id/task_id/total_eval_seconds; `build_conversation` → run_id/task_id).
        Pulled from the CACHED `state_payload` so the trace hot path never triggers a SECOND full fold
        of events.jsonl just to read three scalars (the old `/trace` folded the whole 1 GB log for them).
        Falls back to empty scalars if the log can't be folded — the trace view (spans) still renders."""
        from types import SimpleNamespace
        try:
            s = self.state_payload(rd)["state"]
        except Exception:  # noqa: BLE001 — a malformed log must not 500 the trace; degrade to spans-only
            s = {}
        # Fall back to the run dir name for run_id (rd == root/run_id, so rd.name IS the run id) when the
        # log can't be folded — so a corrupt-log `/trace` still carries the correct run_id, matching the
        # pre-index endpoint's degraded response (which returned the URL's run_id) rather than an empty one.
        return SimpleNamespace(run_id=s.get("run_id") or rd.name, task_id=s.get("task_id") or "",
                               total_eval_seconds=float(s.get("total_eval_seconds") or 0.0))

    def trace_view(self, rd: Path) -> dict:
        """The run-level LIGHT trace view (`build_trace_view(light=True)`), read via the incremental
        span index (`events.span_index`) instead of parsing the whole spans.jsonl, and cached by
        (spans.jsonl, events.jsonl) file identity so an unchanged run is served instantly on refetch.
        A missing sidecar is a known empty trace; an unreadable sidecar raises so the HTTP projection
        can distinguish unavailable telemetry from a successful empty read."""
        from looplab.events.span_index import get_index
        from looplab.events.traceview import TRACE_VIEW_SPAN_CAP, build_trace_view, load_spans
        sp = rd / "spans.jsonl"

        def _sig(p: Path, *, unavailable_raises: bool = False):
            try:
                stt = p.stat()
                # Reset/clear_trace replace files atomically.  A replacement can deliberately retain
                # size+mtime, so those two mutable metadata fields are not a file identity.  Match the
                # reset-safe state/list caches and the span index: include the underlying file identity
                # and creation/change time as well as the content metadata.
                return file_identity(stt)
            except FileNotFoundError:
                return None
            except OSError:
                if unavailable_raises:
                    raise
                return None
        key = str(rd)
        events_path = rd / "events.jsonl"
        # The first durable event is the run-generation identity.  Reading only that first JSONL line
        # is cheap and adds a semantic reset fence even on filesystems with weak/reused inode metadata.
        generation = run_generation_token(iter_event_jsonl(events_path))
        # One no-follow/nonblocking descriptor supplies both the cache signature and readability
        # proof.  Path.stat()+plain open followed links, accepted hard-link aliases, and could block
        # forever on a FIFO before the hardened SpanIndex reader got a chance to reject it.
        span_cacheable = True
        try:
            with open_private_trace_file(sp, open_file=open) as source:
                source_stat = os.fstat(source.fileno())
                change_token = trace_file_change_token(source.fileno(), source_stat)
                span_sig = (file_identity(source_stat), change_token)
                # On Windows an unavailable FILE_BASIC_INFO.ChangeTime leaves creation time as the
                # only ctime-like value.  It cannot distinguish a same-size rewrite with restored
                # mtime, so this observation may build a view but must never authorize cache reuse.
                span_cacheable = change_token is not None
        except FileNotFoundError:
            span_sig = None
        sig = (span_sig, _sig(events_path), generation)
        with self._trace_view_lock:
            hit = self._trace_view_cache.get(key)
        if span_cacheable and hit is not None and hit[0] == sig:
            return hit[1]
        source_missing = span_sig is None
        idx = None if source_missing else get_index(sp)
        if not source_missing and idx is None:
            # The source vanished after its successful signature/readability probe. Do not collapse
            # that race into the known-empty state reserved for initial absence.
            raise OSError("trace source disappeared during projection read")
        total = idx.span_count() if idx is not None else (0 if source_missing else None)
        spans = (idx.light_spans(TRACE_VIEW_SPAN_CAP) if idx is not None
                 else ([] if source_missing else load_spans(sp)))
        view = build_trace_view(
            self.trace_scalars(rd), spans, light=True, total_spans=total,
            span_cap=TRACE_VIEW_SPAN_CAP)
        with self._trace_view_lock:      # only the dict ops — the slow build above ran lock-free
            self._trace_view_cache[key] = (sig, view)
            # the cached response itself is span-capped by TRACE_VIEW_SPAN_CAP.  Count-only
            # eviction previously retained up to four ~200 MB views and let one request exhaust memory.
            while len(self._trace_view_cache) > 4:
                self._trace_view_cache.pop(next(iter(self._trace_view_cache)))
        return view

    def invalidate_trace_view(self, rd: Path) -> None:
        """Explicitly evict a run's large derived trace after reset/rewrite.

        Identity checks remain the correctness boundary; this bounded lock only prevents retaining a
        now-unreachable (potentially hundreds-of-MB) view until the next request notices the change.
        """
        with self._trace_view_lock:
            self._trace_view_cache.pop(str(rd), None)

    def invalidate_run_caches(self, rd: Path) -> None:
        """Evict every in-process projection retained for a quarantined run."""
        key = str(rd)
        self.summary_cache.pop(rd.name, None)
        with self._state_cache_lock:
            stale = [cache_key for cache_key in self._state_cache
                     if cache_key and cache_key[0] == key]
            for cache_key in stale:
                self._state_cache.pop(cache_key, None)
        self.invalidate_trace_view(rd)
        from looplab.events.span_index import invalidate
        invalidate(rd / "spans.jsonl")

    def node_trace_view(self, rd: Path, nid, cap: Optional[int] = None,
                        generation: Optional[int] = None,
                        before: Optional[str] = None) -> dict:
        """The LIGHT trace view built over ONLY one node's spans (via `light_spans_for_node`, in-memory)
        — so expanding a node's trace is O(node), not O(whole run) indexed down. `build_trace_view` over
        just that node's traces preserves its bounded tree/rollup projection without scanning unrelated
        nodes; its summary is correspondingly node-scoped. If the index cannot be used, `trace_view`
        supplies the bounded run projection, while unreadable telemetry still propagates as unavailable.

        `cap` (the UI's "load more spans" control) raises the per-node span ceiling on explicit demand;
        the default stays TRACE_NODE_SPAN_CAP, and it is clamped to TRACE_NODE_SPAN_CAP_MAX so a single
        huge node can never materialize an unbounded tree. Still O(node) — a bigger cap only surfaces
        more of THAT node's already-scoped spans. ``generation`` fences a reset-surviving node id to
        one lifecycle before either count or cap is applied.

        ``before`` SEEKS that window rather than growing it: the cap's spans ENDING at the anchored
        span instead of at the node's newest one (`SpanIndex._anchored`). It is what makes an early
        repair reachable at all — widening only ever extends the same tail, and its ceiling is real.
        ``total_spans`` deliberately stays the node's FULL count, so the omission receipt keeps
        describing how big this node's trace is rather than how much of it precedes the anchor."""
        from looplab.events.span_index import get_index
        from looplab.events.traceview import (TRACE_NODE_SPAN_CAP, build_trace_view,
                                              settle_node_span_cap)
        # One settle rule, shared with the conversation route — see settle_node_span_cap for why the
        # floor/ceiling may not be re-spelled here.
        span_cap = settle_node_span_cap(cap, default=TRACE_NODE_SPAN_CAP)
        idx = get_index(rd / "spans.jsonl")
        if idx is None:
            return build_trace_view(
                self.trace_scalars(rd), [], light=True, total_spans=0, span_cap=span_cap)
        return build_trace_view(
            self.trace_scalars(rd),
            idx.light_spans_for_node(nid, span_cap, generation=generation, before=before),
            light=True,
            total_spans=idx.node_span_count(nid, generation=generation),
            span_cap=span_cap,
            # The node's build claims, resolved over the WHOLE index. A `?before=` anchor inside the
            # build ends the window before the `materialize_node` row that names it, so a map
            # re-derived from the window would drop the whole block into `unscoped` — the node's own
            # tree tab showing nothing for a window it had just selected for that node.
            claimed_traces=idx.node_build_traces(nid, generation=generation))

    def node_episode_map(self, rd: Path, nid, generation: Optional[int] = None, *,
                         cap: Optional[int] = None, before: Optional[str] = None,
                         snapshot: Optional[str] = None) -> dict:
        """The node's EPISODE MAP — every band of its trace, with no band's contents.

        Sits beside `node_trace_view` because it is the control that makes that view's window
        usable: the window is bounded and a heavily-repaired node is far larger than any window the
        server can afford, so the operator needs somewhere to point it (`?before=`). Reads only the
        in-memory light index — no spans.jsonl bytes at all — so a bounded episode page can sit beside
        a bounded span read (measured 82 ms to derive the 7,048 bands of rubert-dr-0804 node 1).
        An unreadable/absent index degrades to the same explicit unavailable receipt every other
        trace surface uses, never to an empty map.
        """
        from looplab.events.span_index import get_index
        from looplab.events.traceview import (TRACE_NODE_EPISODE_CAP, node_episodes,
                                              unavailable_projection)
        idx = get_index(rd / "spans.jsonl")
        if idx is None:
            return {"node_id": str(nid), "episodes": [],
                    "projection": unavailable_projection()}
        return node_episodes(
            idx.light_spans_for_node(nid, None, generation=generation), nid,
            total_spans=idx.node_span_count(nid, generation=generation),
            cap=TRACE_NODE_EPISODE_CAP if cap is None else cap,
            before=before,
            snapshot=snapshot,
            # Both the light rows and their counts come from the index, which normalized them when
            # it built them; re-running the redaction/entropy scan over a whole 14,507-span node on
            # every open is the same pure work `build_conversation` documents skipping.
            _normalized=True)

    def card_trace_view(self, rd: Path, card_id: str) -> dict:
        """The whole story of ONE card: its research, then every node it produced.

        The fold is the only component that knows which nodes a card owns (`idea.card_id`) and which
        trace each node's build ran in (`node_created.trace_id`), so this resolves both here and hands
        them to the pure projection rather than letting it guess from spans.
        """
        from looplab.events.span_index import get_index
        from looplab.events.traceview import project_card_trace

        events = self.events(rd)
        state = fold(events)
        node_ids, trace_ids = [], {}
        for node in state.nodes.values():
            if str(getattr(node.idea, "card_id", None) or "") != str(card_id):
                continue
            node_ids.append(node.id)
        for event in events:
            # The registry constant, not a literal (invariant 7): a misspelling here does not
            # fail, it matches nothing — `trace_ids` stays empty and the card trace surface loses
            # every node section while still returning 200.
            if event.type != EV_NODE_CREATED:
                continue
            data = event.data or {}
            node_id = data.get("node_id")
            if node_id in node_ids and event.trace_id:
                # LAST wins: a node reset re-builds under a new trace, and the story an operator
                # opens is the one that is live now.
                trace_ids[str(node_id)] = event.trace_id
        idx = get_index(rd / "spans.jsonl")
        # KNOWN COST, deliberately not narrowed here — see docs/34 (CARD-TRACE-SCAN). This copies the
        # WHOLE run's light span list (a 1 GB run's index is ~220 MB of dicts) and `project_card_trace`
        # then rescans it once per owned node, so a card owning 5 nodes on a 200k-span run does ~1M
        # predicate evaluations on the request thread. It cannot simply be given the owned traces:
        # research is matched TWO ways and the first is "a `propose` span carrying this card_id",
        # which may live in any trace, so a trace-scoped selection would silently drop the research
        # section for the draft/debug/improve paths. Narrowing it properly needs a card_id (or span
        # name) dimension on `SpanIndex`, which is an index-schema change, not a call-site one.
        spans = idx.light_spans() if idx is not None else []
        return project_card_trace(spans, card_id=str(card_id), node_ids=node_ids,
                                  node_trace_ids=trace_ids, _normalized=idx is not None)

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
        # GROUNDING means "setup is still running", and it is folded from `setup_finished` — not
        # derived from the ABSENCE of a data profile, which is what it used to be. That derivation
        # was true only for the task kinds that have a `columns()` hook: a `RepoTask` has none, so
        # `data_profiled` never fires, `data_profile` stays None, and the phase stayed "grounding"
        # until the first node existed. Measured on rubertlite-dr-unified-v5: setup finished at
        # 6.5 s and the UI said "Setting up task and data…" for the next 40 minutes, across a
        # 13-minute proposal and a 23-minute build. A phase derived from what has NOT happened
        # names the wrong thing as soon as one task kind stops emitting it.
        # …and a legacy log that never emitted `setup_finished` is set-up-complete once a NODE exists.
        # That is not a guess, it is the engine's OWN rule: `orchestrator.py::_setup_phase` skips
        # setup entirely on `state.setup_done or state.nodes or state.finished`, so such a resumed run
        # never appends `setup_finished` and `st.setup_done` stays False for the rest of its life —
        # this phase would read "grounding" while nodes are being built and evaluated. The clause
        # cannot mask the defect the comment above records: a modern run folds `setup_done` seconds
        # in, long before its first node exists.
        if st.run_id and not st.setup_done and not st.nodes:
            return PHASE_GROUNDING
        return PHASE_SEARCH

    def llm_settings(self, rd: Optional[Path] = None):
        """Per-run LLM settings (see `llm_context.llm_settings`) over THIS app's settings store."""
        return llm_settings(self.settings, rd)

    def global_settings(self):
        """Typed environment + saved UI defaults for run-independent owner surfaces."""
        return global_settings(self.settings)

    def make_llm_client(self, *args, **kwargs):
        """Late-bound client factory — resolves `looplab.serve.server.make_llm_client` at call time
        so a monkeypatch of `looplab.server.make_llm_client` reaches every router (see module doc)."""
        from looplab.serve import server as _server
        return _server.make_llm_client(*args, **kwargs)
