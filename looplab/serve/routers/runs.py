"""Run read-model routes: the runs list, per-run state + SSE stream, node detail/logs/metrics,
traces, provenance, artifacts, config and cost. Handler bodies are verbatim moves from
`serve/server.py::make_app` (BACKLOG §4); captured locals now live on `srv` (AppState)."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import hmac
import json
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Annotated, Any, Optional

import anyio
import orjson
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from looplab.core.atomicio import atomic_write_text, same_file_entry
from looplab.core.config import (
    RUN_START_PINNED_FIELDS, Settings, run_start_pinned_disagreement, run_start_pinned_settings,
    settings_from_snapshot)
from looplab.core.node_evidence import node_attempt
from looplab.core.trace_files import (
    TraceFileIdentity, iter_bounded_trace_jsonl_lines, open_private_trace_file)
from looplab.core.run_deletion import (RunDeletionFenceError, RunDeletionStorageError, assert_run_deletion_write_allowed)
from looplab.core.run_reset import (
    RunResetFenceError, RunResetStorageError, assert_run_reset_write_allowed,
    load_run_reset_marker)
from looplab.serve.http import if_none_match, json_object, json_object_bytes, request_body_contract
from looplab.events.eventstore import (
    EventStore, EventStoreConcurrencyError, EventStoreLockError, JsonlRecordInvalid,
    _interprocess_lock, decode_jsonl_line, iter_event_jsonl)
from looplab.events.replay import FoldCursor, fold
from looplab.events.traceview import (
    TRACE_PROJECTION_SCHEMA, trace_file_revision, trace_projection_json_bytes,
    unavailable_projection)
from looplab.events.types import (
    EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED, EV_CONCEPT_LENS_STARTED,
    EV_TRUST_GATE_CHANGED,
)
# Per-theme rollup for the cross-run map: {theme: {count, best_metric}}. Now lives in `digest` so the
# Researcher's working-set digest and this UI endpoint share one definition.
from looplab.serve.artifacts import (
    _ART_MAX_BYTES, _LOG_TAIL_MAX, ArtifactPolicyUnavailable,
    _artifact_exposure_policy, _artifact_file_identity, _artifact_node_id, _artifact_roots,
    _list_artifact_files)
from looplab.serve.concept_frame import (MAX_LENS_BODY_BYTES as _CONCEPT_FRAME_MAX_LENS_BODY_BYTES, MAX_LENS_PROMPT_BYTES as _CONCEPT_FRAME_MAX_LENS_PROMPT_BYTES, MAX_LENS_PROMPT_CHARS as _CONCEPT_FRAME_MAX_LENS_PROMPT_CHARS, bounded_lens_label as _bounded_lens_label, build_core as _build_concept_core, core_lens_inputs as _concept_core_lens_inputs, lens_request as _concept_lens_request, normalized_custom_lens_name as _normalized_custom_lens_name, project_frame as _project_concept_frame, TRUNCATION_CAP_REASONS as _TRUNCATION_CAP_REASONS)
from looplab.serve.engine_proc import _engine_liveness, reconcile_pending_resume
from looplab.serve.log_pages import (
    DEFAULT_BYTES, DEFAULT_ROWS, MAX_BYTES, MAX_ROWS, MIN_BYTES, EventLogPager)
from looplab.serve.protocol import (
    EXPECTED_RUN_GENERATION_FIELD, PHASE_FINALIZING, POLL_SECONDS, RUN_GENERATION_FIELD,
    SSE_DONE, SSE_STATE)
from looplab.serve.assistant import safe_provider_failure
from looplab.serve.paid_ledger import (
    FAIL_CLOSED, PaidLedgerSpec, append_claim, confirm_terminal_receipt, fold_paid_ledger,
    record_terminal)
from looplab.serve.paid_work import (
    RunCostAccountingPending, metered_run_client, run_directory_identity)
from looplab.serve.public_cards import PublicCardsProjectionMetadata
# Re-exported for `tests/test_concepts_endpoint.py`, which imports it from THIS module to
# prove the endpoint's edge-collapse mirrors the fold's tiebreak.
from looplab.serve.concept_frame import folded_concepts as _folded_concepts  # noqa: F401
from looplab.serve.run_commands import run_generation_token
from looplab.serve.run_files import run_config_thread_lock as _shared_run_config_thread_lock
from looplab.serve.reset_transaction import (
    ResetReceiptError, reconcile_run_reset_observation)

# Snapshot-derived OPERATOR stage names, memoized per run DIRECTORY + snapshot VERSION: keyed on
# (run dir, task.snapshot.json mtime_ns, size), so the normalize+validate work runs once per snapshot
# version per server process instead of on every node_logs poll (the extra stat syscall per call is
# negligible next to the parse it saves). The stat-derived key makes a rewritten/replaced snapshot
# SELF-INVALIDATE — the memo must not outlive the run: a DELETE + same-id relaunch, or a CLI re-entry
# of the run dir rewriting task.snapshot.json, changes the pipeline this panel must render. Keyed by
# the absolute run dir, not the bare run_id — distinct run roots (tests, multi-root deploys) reuse
# ids like "demo" with different snapshots. Values are TUPLES, not lists: a caller that forgets to
# copy before mutating raises instead of silently corrupting the cache. The DEVELOPER-manifest
# fallback in node_logs deliberately stays per-poll: looplab_stages.json is written mid-node by the
# Developer's STAGES phase, so it can appear between polls and differs node to node.
_OP_STAGE_NAMES: dict[tuple[str, int, int], tuple] = {}
# `node_logs` is a sync `def`, so FastAPI serves concurrent polls on threadpool threads.
# The prune below ITERATES this dict while another run's poll inserts into it, which raises
# RuntimeError('dictionary changed size during iteration') -> 500 on the live log panel; two
# same-run misses after a snapshot rewrite also both `del` the same key -> KeyError. Guard the
# read-modify-write exactly like routers/attention.py and AppState._trace_view_cache do; the
# single `.get()` hit path stays lock-free.
_OP_STAGE_NAMES_LOCK = threading.Lock()


_CONCEPT_CORE_CACHE_MAX_ENTRIES = 16
_CONCEPT_CORE_CACHE_MAX_PREFIXES_PER_SOURCE = 4
_CONCEPT_REPLAY_CACHE_MAX_SOURCES = 16
_CONCEPT_REPLAY_PREFIX_PROBE_BYTES = 4_096
_RUN_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONCEPT_LENS_KEY_RE = re.compile(r"^[\x21-\x7e]{16,512}$")
_CONCEPT_LENS_RECOVERY_SCHEMA = 1
_CONVERSATION_ETAG_SCHEMA = 1
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
# `GET /api/runs/{id}/spans/{sid}` falls back to a scan when the light span index cannot resolve
# `sid`. That fallback exists for a span appended PAST the indexed tail, which is a handful of lines
# — but `sid` is an unvalidated path param, so every bogus id took the same path and walked the whole
# (up to ~1 GB) spans.jsonl to find nothing, pinning a threadpool thread per request. The scan now
# starts at the index's coverage boundary, and this cap bounds the remaining case where there is no
# index at all (a foreign or unparseable file leaves nothing covered).
_SPAN_FALLBACK_MAX_LINES = 20_000
# Legacy /log returns the oldest-first envelopes past `since` as one flat array. Bound BOTH the row
# count AND the aggregate serialized bytes it materializes, so neither 100k envelopes nor a handful of
# very large ones can be read whole into one response (OOM). A row cap alone is not a memory cap: one
# fat event or many large events blow the list past memory even under the row limit. Both caps are
# chosen large enough that every realistic run is byte-identical to the old unbounded behaviour; only a
# pathological log is bounded, and an incremental caller pages forward by advancing `since`. The modern
# streaming-bounded transport is /log-page (per-page byte + row limits).
_LEGACY_LOG_MAX_ROWS = 100_000
_LEGACY_LOG_MAX_BYTES = 64 * 1024 * 1024        # ~64 MiB aggregate response ceiling for the flat array
_CONCEPT_LENS_SAFE_ERROR_KINDS = frozenset({
    "accounting_pending", "credentials", "rate_limit", "unavailable", "provider_error",
    "capacity", "internal",
})


def _conversation_etag(run_id: str, node_id: int, run_generation: Optional[str],
                       attempt: int, span_cap: int, source_revision: str, *,
                       trace_run_id: str, task_id: str) -> str:
    """Opaque validator for one exact conversation representation.

    `source_revision` comes from SpanIndex's node/generation/window-specific full-row digests, so a
    parallel append for another node does not invalidate this node. The route identities still live
    here: the same rows requested under another run generation, attempt or settled window are a
    different selected representation and must never validate a cached payload from this one.
    """
    material = orjson.dumps({
        "schema": _CONVERSATION_ETAG_SCHEMA,
        "projection_schema": TRACE_PROJECTION_SCHEMA,
        "run_id": run_id,
        "trace_run_id": trace_run_id,
        "task_id": task_id,
        "node_id": node_id,
        "run_generation": run_generation,
        "attempt": attempt,
        "span_cap": span_cap,
        "source_revision": source_revision,
    }, option=orjson.OPT_SORT_KEYS)
    digest = hashlib.sha256(b"looplab:conversation-etag:v1\0" + material).hexdigest()
    return f'W/"llconv1-{digest}"'


class RunConfigMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_start_pinned_fields: list[str]
    snapshot_mismatch_fields: list[str]
    run_read_only_fields: list[str]


class RunConfigResponse(BaseModel):
    """Flat compatibility payload with one typed metadata member and arbitrary Settings keys."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    metadata: RunConfigMetadata = Field(alias="_looplab_config_meta")


class RunConfigUpdateRequest(BaseModel):
    """Preferred CAS-aware request envelope; legacy flat dictionaries remain accepted."""

    model_config = ConfigDict(extra="forbid")

    settings: dict[str, Any]
    expected_revision: Annotated[Optional[str], Field(pattern=r"^[0-9a-f]{64}$")] = None
    expected_generation: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class LegacyRunConfigUpdateRequest(BaseModel):
    """Compatibility body with setting keys directly at the top level."""

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={"not": {"required": ["settings"]}},
    )

    expected_revision: Annotated[Optional[str], Field(pattern=r"^[0-9a-f]{64}$")] = None
    expected_generation: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class RunConfigUpdateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    config: RunConfigResponse
    changed: list[str]
    normalized_pinned: list[str]
    trust_gate_event_appended: bool
    engine_running: bool | None


class PublicRunStateBody(BaseModel):
    """Typed Card seam inside the intentionally extensible legacy run-state object."""

    model_config = ConfigDict(extra="allow")

    cards: dict[str, dict[str, Any]]
    cards_projection: PublicCardsProjectionMetadata


class PublicRunStateResponse(BaseModel):
    """Stable owner-state envelope; ``state`` keeps additive legacy fields discoverable at runtime."""

    model_config = ConfigDict(extra="forbid")

    state: PublicRunStateBody
    seq: int = Field(ge=-1)
    max_seq: int = Field(ge=-1)
    event_count: int = Field(ge=0)
    generation: Annotated[Optional[str], Field(pattern=r"^[0-9a-f]{64}$")]


def _run_config_revision(snapshot: dict) -> str:
    """Bounded opaque CAS token for one complete on-disk config snapshot."""
    canonical = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _run_config_thread_lock(snapshot_path: Path) -> threading.Lock:
    """Compatibility seam around the shared config writer stripe."""
    return _shared_run_config_thread_lock(snapshot_path)


def _scan_span_tail(
        path: Path, sid: str, since: int,
        *, expected_identity: Optional[TraceFileIdentity] = None,
) -> Optional[dict]:
    """Find one span row by id in the bytes AFTER `since`, bounded by `_SPAN_FALLBACK_MAX_LINES`.

    `since` must be a newline boundary — the span index's `covers` is exactly that (its scan reports
    the last complete-record offset). Uses the SHARED line rule (`decode_jsonl_line`) so the two
    cannot disagree on where a damaged file stops; returns None when the id is not in the scanned
    window. The line cap is this fallback's own bound, not part of that rule."""
    try:
        # The index released its source descriptor before this compatibility scan. Bind the reopen to
        # that same inode so a path swap cannot redirect a guessed span id into another run's trace.
        with open_private_trace_file(
                path, expected_identity=expected_identity, open_file=open) as f:
            if since > 0:
                f.seek(since)
            snapshot_size = os.fstat(f.fileno()).st_size
            remaining = max(0, snapshot_size - f.tell())
            for seen, raw in enumerate(iter_bounded_trace_jsonl_lines(
                    f, size=remaining, label="span detail fallback")):
                if seen >= _SPAN_FALLBACK_MAX_LINES:
                    break                       # this compatibility fallback's own row cap
                try:
                    obj = decode_jsonl_line(raw)
                except JsonlRecordInvalid:
                    break                       # corrupt tail — stop cleanly
                if obj is None:
                    continue                    # blank line: part of the prefix, not a record
                if obj.get("span_id") == sid:
                    return obj
    except OSError:
        return None
    return None


def _concept_lens_idempotency_key(raw: str) -> str:
    """Validate the opaque browser receipt before it becomes an HMAC key.

    Sixteen random visible-ASCII bytes are the minimum supported client contract.  Length alone
    cannot prove entropy, so callers are explicitly required to generate this value with a CSPRNG;
    rejecting short/control-bearing keys prevents accidental low-entropy or ambiguous receipts.
    """
    if not isinstance(raw, str) or _CONCEPT_LENS_KEY_RE.fullmatch(raw) is None:
        raise HTTPException(400, {
            "code": "concept_lens_idempotency_key_invalid",
            "message": (
                "Idempotency-Key must be a cryptographically random visible-ASCII value "
                "between 16 and 512 bytes."
            ),
        })
    return raw


def _concept_lens_identity(run_dir: Path, generation: str, idempotency_key: str) -> str:
    return hashlib.sha256(
        ("concept_lens\0" + run_directory_identity(run_dir) + "\0" + generation
         + "\0" + idempotency_key).encode("utf-8")
    ).hexdigest()


def _concept_lens_prompt_digest(idempotency_key: str, prompt: str) -> str:
    """Bind prompt equality to the unlogged high-entropy request key.

    A plain prompt hash lets anyone who can read the diagnostic log dictionary-test common prompts.
    HMAC preserves restart-safe equality without turning the event log into a prompt oracle.
    """
    return hmac.new(
        idempotency_key.encode("ascii"), prompt.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _concept_lens_resolution_key(raw: str) -> str:
    """Validate a recovery resolution key without aliasing it to the paid request key.

    Recovery is intentionally possible after the browser loses the original idempotency receipt.
    This second key only deduplicates the operator's resolution decision; it can never reconstruct,
    resume, or authorize the paid provider request itself.
    """
    if not isinstance(raw, str) or _CONCEPT_LENS_KEY_RE.fullmatch(raw) is None:
        raise HTTPException(400, {
            "code": "concept_lens_resolution_key_invalid",
            "message": (
                "Resolution-Idempotency-Key must be a cryptographically random visible-ASCII "
                "value between 16 and 512 bytes."
            ),
        })
    return raw


def _concept_lens_resolution_identity(run_dir: Path, generation: str, request_id: str,
                                      resolution_key: str) -> str:
    """Domain-separated, non-reversible identity for one recovery resolution command."""
    return hashlib.sha256(
        ("concept_lens_resolution\0" + run_directory_identity(run_dir) + "\0" + generation
         + "\0" + request_id + "\0" + resolution_key).encode("utf-8")
    ).hexdigest()


async def _concept_lens_json_body(request: Request) -> dict:
    """Read either paid-lens command without permitting an unbounded request body."""
    raw_body = bytearray()
    async for chunk in request.stream():
        if len(raw_body) + len(chunk) > _CONCEPT_FRAME_MAX_LENS_BODY_BYTES:
            raise HTTPException(413, {
                "code": "concept_lens_body_too_large",
                "max_bytes": _CONCEPT_FRAME_MAX_LENS_BODY_BYTES,
            })
        raw_body.extend(chunk)
    # The UTF-8 decode stays HERE, beside the byte cap: both are this route's own reading policy,
    # and `json.loads` would otherwise accept BOM-marked UTF-16/32 that this endpoint never did.
    try:
        text = bytes(raw_body).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "request body must be valid JSON") from exc
    return json_object_bytes(text)


def _assert_lens_generation(srv, rd: Path, *, core_generation: Optional[str],
                            expected_generation: str, stale_message: str,
                            stale_remediation: str, prepared_message: str) -> tuple[Path, str]:
    """The paid-concept-lens generation fence, re-checked INSIDE the run sequencer.

    Three endpoints — recover, resolve-recovered, abandon — each wrote this out: validate the run
    paths, read the current generation, refuse if it is missing or moved since the caller looked,
    then refuse AGAIN if the concept projection this handler prepared was built against a different
    generation. Two checks, because they answer different questions: the first says the CALLER is
    stale, the second says the SERVER's own projection is.

    Only the prose differs per endpoint, and it is client-visible, so it is passed in rather than
    flattened. `derive_concept_lens` deliberately does NOT use this: it is the paying path, and it
    reports "no durable generation identity" as its own `run_generation_unavailable` code rather
    than folding that into "changed" — for a caller about to spend money, "the run has no identity"
    and "the run moved" are different problems with different fixes.

    Returns the validated run dir (``validate_paths`` may re-resolve it) and the current generation.
    """
    rd = srv.commands.validate_paths(rd)
    current_generation = srv.commands.run_generation(rd)
    if not current_generation or current_generation != expected_generation:
        raise HTTPException(409, {
            "code": "run_generation_changed",
            "expected_generation": expected_generation,
            "current_generation": current_generation or None,
            "message": stale_message,
            "remediation": stale_remediation,
        })
    if core_generation != current_generation:
        raise HTTPException(409, {
            "code": "run_generation_changed",
            "expected_generation": expected_generation,
            "current_generation": current_generation,
            "message": prepared_message,
        })
    return rd, current_generation


# The shared claim→terminal event ledger (doc 25 SR-01, `serve/paid_ledger.py`), which owns the fold,
# the fsync-confirm and the sequenced terminal append this route used to hand-roll beside its
# report-refresh twin. FAIL_CLOSED and the bound `request_digest` are this protocol's half of the
# deliberate asymmetry the shared module documents: EVERY paid-lens terminal is written through the
# ledger, so an unclaimed terminal — or one whose digest disagrees with its claim's — is evidence of
# a damaged or forged receipt rather than of paid work, and publishing it would show the operator a
# lens they never asked for. The strict recovery fold below keeps its own separate strategy.
_CONCEPT_LENS_LEDGER = PaidLedgerSpec(
    claim_type=EV_CONCEPT_LENS_STARTED,
    terminal_types=frozenset({EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED}),
    identity_field="lens_request_id",
    digest_field="request_digest",
    conflict_policy=FAIL_CLOSED,
)


def _concept_lens_recovery_ledger(events, generation: str):
    """Strictly fold the current generation into a bounded lost-receipt recovery view.

    The ordinary paid endpoint keeps its legacy-compatible fold — the shared `_CONCEPT_LENS_LEDGER`
    above. Recovery has no original browser receipt with which to disambiguate damaged data, so it
    deliberately fails closed on any malformed, duplicate, out-of-order, or digest-mismatched
    current-generation paid-lens event.
    Only bounded sequence metadata survives this fold; prompt digests remain server-private.
    """
    claims: dict[str, dict] = {}
    terminals: dict[str, object] = {}
    conflict = False
    for event in events:
        if event.type not in {
                EV_CONCEPT_LENS_STARTED, EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED}:
            continue
        data = event.data if isinstance(event.data, dict) else {}
        if data.get("generation") != generation:
            continue
        identity = data.get("lens_request_id")
        digest = data.get("request_digest")
        event_seq = event.seq
        if (not isinstance(identity, str) or _SHA256_RE.fullmatch(identity) is None
                or not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None
                or isinstance(event_seq, bool) or not isinstance(event_seq, int)
                or not 0 <= event_seq <= _MAX_SAFE_INTEGER):
            conflict = True
            continue
        if event.type == EV_CONCEPT_LENS_STARTED:
            input_seq = data.get("input_seq")
            if (isinstance(input_seq, bool) or not isinstance(input_seq, int)
                    or not -1 <= input_seq < event_seq
                    or input_seq > _MAX_SAFE_INTEGER
                    or identity in claims or identity in terminals):
                conflict = True
                continue
            claims[identity] = {
                "request_digest": digest,
                "started_seq": event_seq,
                "input_seq": input_seq,
            }
            continue

        claim = claims.get(identity)
        if (claim is None or identity in terminals
                or event_seq <= claim["started_seq"]
                or claim["request_digest"] != digest):
            conflict = True
            continue
        terminals[identity] = event

    unresolved = set(claims) - set(terminals)
    return claims, terminals, unresolved, conflict


def _validated_derived_lens(spec, lens_pack: list[dict], inputs: dict):
    """Return the canonical bounded lens triple, or None for an unusable model result."""
    if not isinstance(spec, dict):
        return None
    # Older terminals may carry the previously advertised string `root`.  Root filtering was never
    # implemented, so canonical specs now deliberately drop it.  Replay accepts that one legacy
    # string field even after consolidation renames/removes the concept; structured roots are
    # malformed and fail closed rather than reaching a set membership TypeError.
    legacy_root = spec.get("root")
    if "root" in spec and not isinstance(legacy_root, str):
        return None
    raw_relations = spec.get("rels")
    if not isinstance(raw_relations, list):
        return None
    relations = list(dict.fromkeys(str(rel) for rel in raw_relations))
    name = _normalized_custom_lens_name(spec.get("name"))
    shipped_names = {item.get("name") for item in lens_pack if isinstance(item, dict)}
    if not name or name in shipped_names:
        name = _normalized_custom_lens_name(
            "derived-" + (name or "-".join(relations) or "lens"))
    if not name:
        return None
    try:
        canonical_name, validated_spec, registration = _concept_lens_request(
            name, ",".join(relations), lens_pack)
    except HTTPException:
        return None
    validated_spec.update({
        "label": _bounded_lens_label(spec.get("label"), canonical_name),
        "provenance": "agent",
    })
    return canonical_name, validated_spec, registration


def _concept_lens_spec_matches_terminal(raw_spec, canonical_spec: dict) -> bool:
    """Strictly match a terminal, allowing only the retired legacy string-root field."""
    if not isinstance(raw_spec, dict):
        return False
    if "root" in raw_spec and not isinstance(raw_spec.get("root"), str):
        return False
    return {key: value for key, value in raw_spec.items() if key != "root"} == canonical_spec


def _concept_event_file_identity(path: Path) -> Optional[tuple]:
    """Identity of the exact on-disk byte version a ConceptFrame core was folded from."""
    try:
        status = path.stat()
    except OSError:
        return None
    return (
        str(path.absolute()),
        status.st_dev,
        status.st_ino,
        status.st_ctime_ns,
        status.st_mtime_ns,
        status.st_size,
    )


def _concept_relation_registry_identity(lens_pack: list[dict]) -> tuple[str, ...]:
    """Only the shipped relation vocabulary influences bounded core edge acceptance."""
    return tuple(sorted({relation for lens in lens_pack if isinstance(lens, dict)
                         for relation in (lens.get("rels") or []) if isinstance(relation, str)}))


class _ConceptCoreCache:
    """Small thread-safe LRU of bounded, lens-independent ConceptFrame cores.

    Keys retain generation and the relation-registry version in addition to file identity and requested
    prefix. Generation is not separately read on a hit (that would itself scan the log); instead the
    lookup matches the exact file identity + seq pair and the stored key records the generation proved
    by that folded prefix.
    """

    def __init__(self):
        self._entries: OrderedDict[tuple, dict] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, identity: tuple, requested_seq: Optional[int],
            relation_registry: tuple[str, ...] = (),
            run_id: Optional[str] = None) -> Optional[dict]:
        with self._lock:
            source = identity[0]
            # an append, same-path replacement, or corruption changes at least one exact
            # stat field. Drop every old version for that path before lookup so generations and damaged
            # versus authoritative prefixes cannot coexist indefinitely in this bounded cache.
            for key in [key for key in self._entries
                        if key[0][0] == source and key[0] != identity]:
                del self._entries[key]
            for key in reversed(tuple(self._entries)):
                # run_id is request identity echoed in the cached core. Two legal URL
                # aliases can resolve to one events file, so sharing their source identity must not
                # make one endpoint return the other alias in its versioned response.
                if (key[0] == identity and key[1] == requested_seq
                        and key[3] == relation_registry and key[4] == run_id):
                    core = self._entries[key]
                    self._entries.move_to_end(key)
                    return core
        return None

    def put(self, identity: tuple, requested_seq: Optional[int], core: dict,
            relation_registry: tuple[str, ...] = ()) -> None:
        key = (identity, requested_seq, core.get(RUN_GENERATION_FIELD), relation_registry,
               core.get("run_id"))
        with self._lock:
            self._entries[key] = core
            self._entries.move_to_end(key)
            source_keys = [existing for existing in self._entries
                           if existing[0][0] == identity[0]]
            while len(source_keys) > _CONCEPT_CORE_CACHE_MAX_PREFIXES_PER_SOURCE:
                del self._entries[source_keys.pop(0)]
            while len(self._entries) > _CONCEPT_CORE_CACHE_MAX_ENTRIES:
                self._entries.popitem(last=False)


class _ConceptReplaySource:
    """One path's incremental EventStore + unfinalized replay accumulator."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.store: Optional[EventStore] = None
        self.cursor = FoldCursor()
        self.identity: Optional[tuple] = None
        self.divergence: Optional[dict] = None
        self.first_event = None
        self.boundary_event = None
        self.head_probe = b""
        self.tail_probe = b""
        self.users = 0

    def reset(self) -> None:
        self.store = EventStore(self.path)
        self.invalidate()

    def invalidate(self) -> None:
        self.cursor = FoldCursor()
        self.identity = None
        self.divergence = None
        self.first_event = None
        self.boundary_event = None
        self.head_probe = b""
        self.tail_probe = b""

    def probes(self, prefix_size: int) -> tuple[bytes, bytes]:
        """Read constant-size anchors from a claimed durable prefix."""
        width = min(max(0, prefix_size), _CONCEPT_REPLAY_PREFIX_PROBE_BYTES)
        try:
            with self.path.open("rb") as stream:
                head = stream.read(width)
                stream.seek(max(0, prefix_size - width))
                tail = stream.read(width)
        except OSError:
            return b"", b""
        return head, tail


class _ConceptReplayCache:
    """Bounded LRU of append-incremental event readers and fold cursors.

    A source lock covers stat-version validation, EventStore top-up, cursor extension and deep snapshot.
    Consequently concurrent GETs for one live run apply each suffix once.  Replacement, shrink,
    same-size rewrite and corruption transitions discard the raw accumulator before it can be reused.
    """

    def __init__(self):
        self._sources: OrderedDict[str, _ConceptReplaySource] = OrderedDict()
        self._lock = threading.Lock()

    def _source(self, path: Path) -> _ConceptReplaySource:
        key = str(path.absolute())
        with self._lock:
            source = self._sources.get(key)
            if source is None:
                source = _ConceptReplaySource(path)
                self._sources[key] = source
            source.users += 1
            self._sources.move_to_end(key)
            self._prune_unlocked()
            return source

    def _prune_unlocked(self) -> None:
        while len(self._sources) > _CONCEPT_REPLAY_CACHE_MAX_SOURCES:
            victim = next((key for key, source in self._sources.items() if source.users == 0), None)
            if victim is None:
                return  # all sources are in flight; the next release restores the hard idle bound
            del self._sources[victim]

    def _release(self, source: _ConceptReplaySource) -> None:
        with self._lock:
            source.users -= 1
            self._prune_unlocked()

    @staticmethod
    def _identity_requires_reset(previous: Optional[tuple], current: tuple) -> bool:
        if previous is None:
            return False
        old_lineage = previous[1:3]
        new_lineage = current[1:3]
        if new_lineage != old_lineage or current[5] < previous[5]:
            return True
        # An append changes size. A metadata change at the SAME size is an in-place rewrite and must
        # not inherit the previous cursor even when the filesystem preserves the inode.
        return current[5] == previous[5] and current != previous

    def snapshot(self, path: Path, identity: tuple):
        """Return ``(events, deep-finalized-state, divergence, identity-after-read)``."""
        source = self._source(path)
        try:
            with source.lock:
                reset = self._identity_requires_reset(source.identity, identity)
                if (not reset and source.identity is not None
                        and identity[5] > source.identity[5]
                        and source.probes(source.identity[5]) != (source.head_probe, source.tail_probe)):
                    # Same-inode growth is normally append. Constant-size prefix anchors catch an in-place
                    # rewrite/replacement that grew (a case stat size alone cannot distinguish) without
                    # re-reading the whole history and accidentally extending EventStore's stale bytes.
                    reset = True
                if source.store is None or reset:
                    source.reset()
                events = source.store.read_all()
                divergence = source.store.divergence

                # EventStore independently resets its byte cache on replacement/rewrite. Guard the replay
                # cursor too: count/boundary checks cover a reset noticed between the outer stat and read.
                boundary_changed = (
                    source.cursor.event_count > len(events)
                    or (source.cursor.event_count and len(events) >= source.cursor.event_count
                        and source.boundary_event != events[source.cursor.event_count - 1])
                    or (events and source.first_event is not None and source.first_event != events[0])
                )
                # corruption changes the meaning of "complete prefix" even if no valid event
                # was added. Rebuild from the recoverable prefix on every clean<->corrupt/detail transition;
                # never reuse an authoritative cursor while merely attaching a partial-source receipt.
                if reset or boundary_changed or divergence != source.divergence:
                    source.cursor = FoldCursor()

                source.cursor.extend(events[source.cursor.event_count:])
                state = source.cursor.snapshot()
                divergence_copy = dict(divergence) if divergence is not None else None
                observed_after = _concept_event_file_identity(path)
                if observed_after == identity:
                    source.identity = identity
                    source.divergence = divergence_copy
                    source.first_event = events[0] if events else None
                    source.boundary_event = events[source.cursor.event_count - 1] if events else None
                    source.head_probe, source.tail_probe = source.probes(identity[5])
                else:
                    # The deep snapshot remains a coherent result of the bytes EventStore consumed, but
                    # it has no reusable stat identity. Discard every mutable cursor component so the
                    # retry cannot bind old events to replacement probes observed after this read.
                    source.store = None
                    source.invalidate()
                return events, state, divergence_copy, observed_after
        finally:
            self._release(source)


def _operator_stage_names(rd: Path) -> tuple:
    """Names of the OPERATOR-declared `cmd.stages` pipeline from the run's verbatim
    task.snapshot.json, vetted the way the ENGINE consumes them — () when the task declares none
    (single-command eval) or the declared list doesn't survive validation. Memoized on the snapshot's
    stat identity (see `_OP_STAGE_NAMES` above); returns an immutable tuple — callers list() it
    before mutating."""
    snap_path = rd / "task.snapshot.json"
    try:
        stt = snap_path.stat()
    except OSError:
        return ()   # engine still starting (snapshot not written yet) — don't memoize a transient miss
    key = (str(rd), stt.st_mtime_ns, stt.st_size)
    cached = _OP_STAGE_NAMES.get(key)
    if cached is not None:
        return cached
    names: tuple = ()
    try:
        from looplab.adapters.tasks import normalize_task
        from looplab.runtime.command_eval import validate_stages
        snap = json.loads(snap_path.read_text("utf-8"))
        es = (normalize_task(snap) if isinstance(snap, dict) else {}).get("eval") or {}
        if es.get("stages"):
            # ENGINE PARITY (Engine._resolve_stages): the engine re-runs the shared validator over
            # the snapshot's stages (an old/hand-edited snapshot bypasses pydantic) and on ANY error
            # falls back to the Developer-manifest + protected-`score` path — mirror it exactly, or
            # this panel would render phantom stage bands for a pipeline the engine never ran AND
            # miss the manifest stages it actually did run.
            clean, err = validate_stages(es["stages"])
            if err is None:
                names = tuple(str(s["name"]) for s in clean)
    except Exception:  # noqa: BLE001 - no/foreign/kind-less snapshot -> fall back to the manifest
        names = ()
    # Prune this run dir's entries for OLDER snapshot versions before inserting, so the dict stays
    # bounded per run (a rewrite would otherwise leave one dead entry behind per version).
    with _OP_STAGE_NAMES_LOCK:
        for stale in [k for k in _OP_STAGE_NAMES if k[0] == key[0]]:
            _OP_STAGE_NAMES.pop(stale, None)
        _OP_STAGE_NAMES[key] = names
    return names


def build_router(srv) -> APIRouter:
    router = APIRouter()
    log_pages = EventLogPager()
    _run_dir = srv.run_dir

    def _trace_unavailable(**shape) -> dict:
        """Keep every trace read-failure envelope on one truthful, versioned contract."""
        return {"schema": TRACE_PROJECTION_SCHEMA, **shape,
                "projection": unavailable_projection()}

    def _trace_response(payload: dict) -> Response:
        """Bypass recursive framework encoding for topology-preserving trace projections."""
        return Response(trace_projection_json_bytes(payload), media_type="application/json")

    def _assert_trace_reset_clear(rd: Path) -> None:
        """Refuse a trace snapshot while Replay is between archived sidecars and new events."""
        try:
            marker = load_run_reset_marker(rd)
        except RunResetStorageError as exc:
            raise HTTPException(503, {
                "code": "reset_fence_unavailable",
                "message": "Replay ownership cannot be inspected safely for this trace read.",
            }) from exc
        if marker is not None:
            raise HTTPException(409, {
                "code": "run_reset_in_progress",
                "expected_generation": marker.get("expected_generation"),
                "operation_id": marker.get("operation_id"),
                "message": "The run is being replayed; its trace snapshot is temporarily unavailable.",
                "remediation": "Wait for this Replay operation to settle, then reload run state.",
            })

    def _begin_trace_read(rd: Path, expected_generation: Optional[str]) -> str:
        """Capture the run identity that owns a trace-sidecar read.

        Trace files are not part of the event fold, so a reset/replacement can otherwise publish
        generation-A bytes in a generation-B screen.  Every trace projection uses this same
        before/after CAS in addition to the durable reset marker.
        """
        _assert_trace_reset_clear(rd)
        if (expected_generation is not None
                and _RUN_GENERATION_RE.fullmatch(expected_generation) is None):
            raise HTTPException(400, {
                "code": "invalid_run_generation",
                "message": "expected_generation must be the exact generation from run state.",
                "remediation": "Reload the run before reading trace evidence.",
            })
        generation = srv.commands.run_generation(rd)
        if expected_generation is not None and generation != expected_generation:
            raise HTTPException(409, {
                "code": "run_generation_changed",
                "expected_generation": expected_generation,
                "current_generation": generation or None,
                "message": "The run was reset or replaced before its trace was read.",
                "remediation": "Reload run state and request the current generation.",
            })
        return generation

    def _finish_trace_read(rd: Path, before_generation: str,
                           expected_generation: Optional[str]) -> str:
        """Complete the trace-sidecar lifecycle CAS after the potentially slow projection."""
        _assert_trace_reset_clear(rd)
        generation = srv.commands.run_generation(rd)
        if (generation != before_generation
                or (expected_generation is not None
                    and generation != expected_generation)):
            raise HTTPException(409, {
                "code": "run_generation_changed",
                "expected_generation": expected_generation or before_generation or None,
                "current_generation": generation or None,
                "message": "The run was reset or replaced while its trace was being read.",
                "remediation": "Reload run state and request the current generation.",
            })
        return generation
    _base_state_payload = srv.state_payload

    def _state_payload(rd: Path, upto_seq: Optional[int] = None) -> dict:
        """Serve state and reconcile only the exact durable reset operation that owns its marker."""
        if upto_seq is not None:
            return _base_state_payload(rd, upto_seq)
        try:
            marker = load_run_reset_marker(rd)
        except RunResetStorageError:
            # Reads remain available when ownership storage is damaged; every writer still fails
            # closed on the same marker. Do not make a diagnostic state request destructive.
            marker = None
        if marker is not None:
            try:
                # Reconcile before folding so a request that waited behind Replay cannot publish a
                # generation-A payload after generation B has already committed.
                with srv.commands.sequence(rd):
                    reconcile_run_reset_observation(srv, rd)
            except (HTTPException, OSError, ResetReceiptError, RunResetStorageError):
                # The exact POST remains the recovery authority while observation is incomplete.
                pass
        return _base_state_payload(rd, upto_seq)
    concept_core_cache = _ConceptCoreCache()
    concept_replay_cache = _ConceptReplayCache()

    def _materialize_concept_core(rd: Path, run_id: str, requested_seq: Optional[int],
                                  lens_pack: list[dict]) -> dict:
        """Fold one stable event-file snapshot and cache only its bounded ConceptFrame core."""
        path = rd / "events.jsonl"
        relation_registry = _concept_relation_registry_identity(lens_pack)

        def _from_snapshot(events, current_state, source_divergence):
            if not events:
                raise HTTPException(409, {
                    "code": "run_generation_unavailable",
                    "message": "The run has no durable generation identity.",
                    "remediation": "Wait for run_started, then refresh concepts.",
                })
            projected = (events if requested_seq is None
                         else [event for event in events if event.seq <= requested_seq])
            generation = run_generation_token(projected)
            max_seq = max((event.seq for event in events), default=-1)
            captured_seq = max((event.seq for event in projected), default=-1)
            return _build_concept_core(
                (current_state if requested_seq is None else fold(projected)),
                run_id=run_id, lens_pack=lens_pack,
                generation=generation, requested_seq=requested_seq,
                captured_seq=captured_seq, max_seq=max_seq,
                source_divergence=source_divergence)

        # A read raced by an append/replacement is still a coherent prefix, but it has no stat identity
        # we can safely reuse. Retry a few times for a cacheable stable view, then serve one uncached
        # coherent snapshot rather than delaying a busy live run indefinitely.
        for _attempt in range(3):
            identity = _concept_event_file_identity(path)
            if identity is not None:
                cached = concept_core_cache.get(
                    identity, requested_seq, relation_registry, run_id=run_id)
                if (cached is not None
                        and _concept_event_file_identity(path) == identity):
                    return cached
                events, state, divergence, after = concept_replay_cache.snapshot(path, identity)
            else:
                # A transient stat failure has no trustworthy cache key. Keep this attempt isolated:
                # installing it into the persistent cursor could bind generation-A bytes to generation B.
                source = EventStore(path)
                events = source.read_all()
                state = fold(events)
                divergence = source.divergence
                after = _concept_event_file_identity(path)
            # equality is required even when the first stat failed. Treating
            # ``None -> identity B`` as stable could cache bytes read from replaced generation A
            # under B's identity and poison every later hit until the file changed again.
            if after != identity:
                continue
            core = _from_snapshot(events, state, divergence)
            if identity is not None:
                concept_core_cache.put(after, requested_seq, core, relation_registry)
            return core

        # Three moving identities mean we cannot prove the shared cursor corresponds to any one stat
        # version (a reset/rewrite may look like growth during the race). Re-read and fold one isolated
        # recoverable prefix; it remains uncached, so no uncertain identity can poison later requests.
        source = EventStore(path)
        events = source.read_all()
        return _from_snapshot(events, fold(events), source.divergence)

    # ------------------------------------------------------------------ runs list
    # File identity is part of the signature: reset replaces events.jsonl and can preserve both
    # content metadata fields, so size+mtime alone can return generation A's summary for generation B.


    # `list_runs_membership` and `_run_summaries` are `AppState.run_membership()` /
    # `AppState.run_summaries()` now (doc 25 SR-12) — real methods rather than attributes this
    # router assigned onto the bag for `routers/reports.py` to read back.


    @router.get("/api/runs")
    def list_runs():
        out = srv.run_summaries()
        root = srv.root
        # Overlay project membership (kept OUT of the summary cache — assignments change
        # independently of the event log, so a finished/cached run can still be re-filed).
        pdata = srv.projects.load()
        assignments, labels = pdata["assignments"], pdata.get("labels", {})
        st_assign = pdata.get("supertask_assignments", {})
        # engine_running is a live fact (lock probe), so it stays OUT of the mtime-keyed summary cache —
        # a zombie's events.jsonl is unchanged, yet its liveness flips the instant its engine dies.
        def _alive(rd, resume_pending: bool):
            alive = _engine_liveness(rd)
            # P1-1 on-load reconcile: any not-alive run may carry an unserved durable resume. This
            # includes request-after-run_finished (an inject/reopen that hit the old finalization
            # tail); reconcile_pending_resume itself rejects ordinary finished runs with no pending
            # request. The detached re-spawn appears as running on the next refresh.
            if alive is False and resume_pending:
                try:
                    reconcile_pending_resume(
                        rd, cancel_event=srv.resume_cancel,
                        launch_env=lambda: srv.settings.launch_env_for_run(rd))
                except Exception:  # noqa: BLE001 — recovery is best-effort; never break the run list
                    pass
            return alive
        return [{**s, "project_id": assignments.get(s["run_id"]),
                 "label": labels.get(s["run_id"]),
                 "supertask_id": st_assign.get(s["run_id"]),
                 "engine_running": _alive(
                     root / s["run_id"], bool(s.get("resume_pending")))} for s in out]

    # Late-bind the runs list WITH its live-fact overlay. The cross-run scope reports it was bound
    # for read `srv.run_membership()` now — a report GET must not probe every run's lock (doc 25
    # SR-12) — so this has no production consumer left; `serve/router_wiring.py` records that as an
    # explicit empty consumer tuple, and its source scan turns a NEW reader into a registry update
    # rather than another unwritten cross-router protocol.
    srv.list_runs_fn = list_runs

    # ------------------------------------------------------------------ state + time-travel
    @router.get("/api/runs/{run_id}/state", response_model=PublicRunStateResponse)
    def get_state(run_id: str, seq: Optional[int] = None):
        """Return the bounded public run state.

        ``state.cards`` remains the backwards-compatible mapping; ``state.cards_projection`` carries
        its collection and per-card completeness receipts. Historical ``seq`` reads use the same wire
        projection as the live state, SSE, and review surfaces.
        """
        return _state_payload(_run_dir(run_id), seq)

    @router.get("/api/runs/{run_id}/lifecycle")
    def get_lifecycle(run_id: str):
        """Bounded identity/liveness probe used after a terminal SSE stream closes."""
        return srv.state_probe(_run_dir(run_id))

    @router.get("/api/runs/{run_id}/concepts")
    def get_concepts(run_id: str, response: Response, lens: str = "is_a",
                     rels: Optional[str] = None, seq: Optional[int] = None):
        """Return one versioned, bounded, generation-bound ConceptFrame."""
        from looplab.search.concept_lens import default_lenses

        rd = _run_dir(run_id)
        lens_pack = default_lenses()
        canonical_lens, requested_spec, lens_registration = _concept_lens_request(
            lens, rels, lens_pack)
        if seq is not None and seq < -1:
            raise HTTPException(400, {
                "code": "concept_seq_invalid",
                "message": "Historical concept sequence must be -1 or greater.",
            })

        # the exact event-prefix fold and every lens-independent bounded read model are
        # cached together; switching lenses now performs only this pure tree projection.
        core = _materialize_concept_core(rd, run_id, seq, lens_pack)
        frame = _project_concept_frame(
            core, requested_lens=canonical_lens, lens_pack=lens_pack,
            requested_spec=requested_spec, lens_registration=lens_registration)
        response.headers["Cache-Control"] = "no-store"
        return frame

    def _concept_lens_uncertain(base_frame: dict, generation: str, identity: str,
                                message: str) -> dict:
        return {
            **base_frame,
            "ok": False,
            "code": "concept_lens_uncertain",
            "error_kind": "uncertain",
            "error": message,
            "generation": generation,
            "request_id": identity,
            "ambiguous": True,
        }

    def _concept_lens_terminal_response(event, core: dict, lens_pack: list[dict],
                                        identity: str) -> dict:
        generation = core[RUN_GENERATION_FIELD]
        base_frame = _project_concept_frame(
            core, requested_lens="is_a", lens_pack=lens_pack)
        data = event.data if isinstance(event.data, dict) else {}
        if event.type == EV_CONCEPT_LENS_FAILED:
            raw_kind = str(data.get("error_kind") or "")
            kind = (raw_kind if raw_kind in _CONCEPT_LENS_SAFE_ERROR_KINDS
                    else "provider_error")
            reason = "accounting_pending" if kind == "accounting_pending" else "no_model"
            return {
                **base_frame,
                "ok": False,
                "code": "concept_lens_failed",
                "reason": reason,
                "error_kind": kind,
                "error": "Concept lens creation failed before a model request was sent.",
                "generation": generation,
                "request_id": identity,
                "seq": event.seq,
            }
        outcome = data.get("outcome")
        if event.type == EV_CONCEPT_LENS_COMPLETED and outcome == "abandoned":
            abandon_reason = data.get("reason")
            if abandon_reason not in {"operator_abandoned", "operator_recovered_abandon"}:
                abandon_reason = "operator_abandoned"
            return {
                **base_frame,
                "ok": False,
                "code": "concept_lens_abandoned",
                "reason": abandon_reason,
                "abandoned": True,
                "resolved": True,
                "provider_outcome": "unknown",
                "billing_status": "unknown",
                "warning": (
                    "The provider may already have completed and billed this request; provider-side "
                    "usage can remain unavailable after operator abandonment."
                ),
                "generation": generation,
                "request_id": identity,
                "seq": event.seq,
            }
        if event.type == EV_CONCEPT_LENS_COMPLETED and outcome == "declined":
            reason = data.get("reason")
            if reason not in {"declined", "invalid_spec"}:
                reason = "declined"
            return {
                **base_frame,
                "ok": False,
                "reason": reason,
                "generation": generation,
                "request_id": identity,
                "seq": event.seq,
            }
        if event.type == EV_CONCEPT_LENS_COMPLETED and outcome == "derived":
            inputs = _concept_core_lens_inputs(core)
            prepared = _validated_derived_lens(data.get("spec"), lens_pack, inputs)
            if prepared is not None:
                canonical_name, validated_spec, registration = prepared
                if _concept_lens_spec_matches_terminal(data.get("spec"), validated_spec):
                    frame = _project_concept_frame(
                        core, requested_lens=canonical_name, lens_pack=lens_pack,
                        requested_spec=validated_spec, lens_registration=registration)
                    return {
                        **frame,
                        "ok": True,
                        "spec": validated_spec,
                        "generation": generation,
                        "request_id": identity,
                        "seq": event.seq,
                    }
        return {
            **_concept_lens_uncertain(
                base_frame, generation, identity,
                "The saved lens receipt is malformed. Resume only with this same request identity."),
            "seq": event.seq,
        }

    def _record_concept_lens_terminal(run_dir: Path, generation: str, identity: str,
                                      request_digest: str, event_type: str,
                                      terminal_fields: dict):
        """Append one terminal iff the exact claim is still unresolved.

        Provider work and explicit operator abandonment can finish in different processes.  The run
        command sequencer is therefore the only terminal commit point: a late worker observes and
        replays the winner instead of appending a conflicting second receipt.
        """
        return record_terminal(
            srv, _CONCEPT_LENS_LEDGER, run_dir, generation, identity, event_type,
            terminal_fields, request_digest=request_digest)

    def _record_concept_lens_failure(run_dir: Path, generation: str, identity: str,
                                     request_digest: str, error_kind: str):
        safe_kind = (error_kind if error_kind in _CONCEPT_LENS_SAFE_ERROR_KINDS
                     else "provider_error")
        return _record_concept_lens_terminal(
            run_dir, generation, identity, request_digest, EV_CONCEPT_LENS_FAILED,
            {"error_kind": safe_kind})

    def _run_concept_lens_worker(settings, run_dir: Path, generation: str, identity: str,
                                 request_digest: str, prompt: str, core: dict,
                                 lens_pack: list[dict]) -> dict:
        from looplab.search.concept_lens import derive_lens

        base_frame = _project_concept_frame(
            core, requested_lens="is_a", lens_pack=lens_pack)
        inputs = _concept_core_lens_inputs(core)
        provider_started = False
        try:
            with metered_run_client(srv, settings, run_dir, generation) as client:
                provider_started = True
                spec = derive_lens(
                    prompt, inputs["edges"], client, concepts=inputs["concept_ids"],
                    parser="tool_call_once", raise_on_failure=True)
                prepared = _validated_derived_lens(spec, lens_pack, inputs) if spec else None
                if prepared is None:
                    outcome = "declined"
                    reason = "declined" if not spec else "invalid_spec"
                    terminal_data = {
                        "lens_request_id": identity,
                        "generation": generation,
                        "request_digest": request_digest,
                        "outcome": outcome,
                        "reason": reason,
                    }
                else:
                    canonical_name, validated_spec, registration = prepared
                    terminal_data = {
                        "lens_request_id": identity,
                        "generation": generation,
                        "request_digest": request_digest,
                        "outcome": "derived",
                        "spec": validated_spec,
                    }
                event = _record_concept_lens_terminal(
                    run_dir, generation, identity, request_digest,
                    EV_CONCEPT_LENS_COMPLETED,
                    {key: value for key, value in terminal_data.items()
                     if key not in {"lens_request_id", "generation", "request_digest"}},
                )
                if event is None:
                    return _concept_lens_uncertain(
                        base_frame, generation, identity,
                        "The paid lens finished, but its generation-fenced durable terminal could "
                        "not be confirmed. Resume only with this same request identity.")
                return _concept_lens_terminal_response(
                    event, core, lens_pack, identity)
        except Exception as exc:  # noqa: BLE001 - provider payloads never cross this boundary
            if provider_started:
                return _concept_lens_uncertain(
                    base_frame, generation, identity,
                    "The paid lens attempt may have reached the provider, but its durable receipt "
                    "is unavailable. Resume only with this same request identity.")
            if isinstance(exc, RunCostAccountingPending):
                error_kind = "accounting_pending"
            else:
                error_kind = str(safe_provider_failure(exc).get("error_kind") or "provider_error")
            failure_event = _record_concept_lens_failure(
                run_dir, generation, identity, request_digest, error_kind)
            if failure_event is not None:
                return _concept_lens_terminal_response(
                    failure_event, core, lens_pack, identity)
            return _concept_lens_uncertain(
                base_frame, generation, identity,
                "The lens request failed before provider dispatch, but its terminal receipt could "
                "not be confirmed. Resume only with this same request identity.")

        raise AssertionError("unreachable paid-lens worker path")

    @router.post("/api/runs/{run_id}/concepts/lens")
    async def derive_concept_lens(run_id: str, request: Request, response: Response):
        """Create one generation-bound derived lens behind a durable paid-work claim."""
        from looplab.search.concept_lens import default_lenses

        rd = _run_dir(run_id)
        body = await _concept_lens_json_body(request)
        prompt_value = body.get("prompt")
        prompt = prompt_value.strip() if isinstance(prompt_value, str) else ""
        if not prompt:
            raise HTTPException(400, "prompt is required")
        if (len(prompt) > _CONCEPT_FRAME_MAX_LENS_PROMPT_CHARS
                or len(prompt.encode("utf-8")) > _CONCEPT_FRAME_MAX_LENS_PROMPT_BYTES):
            raise HTTPException(413, {
                "code": "concept_lens_prompt_too_large",
                "max_chars": _CONCEPT_FRAME_MAX_LENS_PROMPT_CHARS,
                "max_bytes": _CONCEPT_FRAME_MAX_LENS_PROMPT_BYTES,
            })
        expected_generation = body.get("expected_generation")
        if (not isinstance(expected_generation, str)
                or _RUN_GENERATION_RE.fullmatch(expected_generation) is None):
            raise HTTPException(400, {
                "code": "invalid_run_generation",
                "message": "expected_generation must be the exact generation from the Concepts response.",
                "remediation": "Refresh the run before creating another paid concept lens.",
            })

        raw_idempotency_key = _concept_lens_idempotency_key(
            request.headers.get("Idempotency-Key", ""))

        lens_pack = default_lenses()
        core = _materialize_concept_core(rd, run_id, None, lens_pack)
        generation = core[RUN_GENERATION_FIELD]
        if not generation:
            raise HTTPException(409, {
                "code": "run_generation_unavailable",
                "message": "The run has no durable generation identity.",
            })
        if generation != expected_generation:
            raise HTTPException(409, {
                "code": "run_generation_changed",
                "expected_generation": expected_generation,
                "current_generation": generation,
                "message": "The run changed before paid lens creation began.",
                "remediation": "Reload the Concepts view and submit a new request intentionally.",
            })
        base_frame = _project_concept_frame(
            core, requested_lens="is_a", lens_pack=lens_pack)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "X-LoopLab-Token, Authorization, Idempotency-Key"
        request_digest = _concept_lens_prompt_digest(raw_idempotency_key, prompt)
        reservation = None
        compute = None
        # OFF the event loop. `sequence()` takes the run command sequencer (a thread lock plus an OS
        # file lock with a bounded-retry acquire, so it can park for the whole lock_acquire_timeout)
        # and the paid-lens ledger reads the ENTIRE events.jsonl. Run inline on this `async def`
        # handler's loop, all of that stalled every SSE stream and every other async handler on the
        # server. The sequenced section still runs as ONE unit — only the thread it runs on changes.
        # Every early exit inside it returns a response dict; `None` means "claimed, keep going", and
        # what the caller then needs is handed back through `claimed`.
        claimed: dict = {}

        def _claim():
            nonlocal rd
            with srv.commands.sequence(rd):
                rd = srv.commands.validate_paths(rd)
                current_generation = srv.commands.run_generation(rd)
                if not current_generation:
                    raise HTTPException(409, {
                        "code": "run_generation_unavailable",
                        "message": "The run has no durable generation identity.",
                    })
                if current_generation != expected_generation:
                    raise HTTPException(409, {
                        "code": "run_generation_changed",
                        "expected_generation": expected_generation,
                        "current_generation": current_generation,
                        "message": "The run changed before paid lens creation began.",
                        "remediation": "Reload the Concepts view and submit a new request intentionally.",
                    })
                if core[RUN_GENERATION_FIELD] != current_generation:
                    raise HTTPException(409, {
                        "code": "run_generation_changed",
                        "expected_generation": expected_generation,
                        "current_generation": current_generation,
                        "message": "The run changed while the concept frame was being prepared.",
                        "remediation": "Reload the Concepts view and submit a new request intentionally.",
                    })

                job_identity = _concept_lens_identity(
                    rd, current_generation, raw_idempotency_key)
                store = EventStore(rd / "events.jsonl")
                ledger = fold_paid_ledger(
                    _CONCEPT_LENS_LEDGER, store.read_all(), current_generation)
                claims, terminals = ledger.claims, ledger.terminals
                unresolved, conflicts = ledger.unresolved, ledger.conflicts
                if job_identity in conflicts:
                    return _concept_lens_uncertain(
                        base_frame, current_generation, job_identity,
                        "This paid-lens identity has conflicting receipts and requires repair.")
                if conflicts:
                    # Do not fabricate an ambiguous receipt for a fresh identity that has never claimed
                    # provider work.  The operator must repair the unrelated ledger conflict first.
                    raise HTTPException(409, {
                        "code": "concept_lens_ledger_conflict",
                        "message": "Another paid-lens identity has conflicting durable receipts.",
                        "remediation": "Repair the conflicting receipts before creating new paid work.",
                    })
                existing_digest = claims.get(job_identity)
                if existing_digest is not None and existing_digest != request_digest:
                    raise HTTPException(409, {
                        "code": "idempotency_key_reused",
                        "message": "This Idempotency-Key already belongs to a different lens prompt.",
                        "remediation": "Reuse it only for the exact request, or create a new key.",
                    })
                terminal = terminals.get(job_identity)
                if terminal is not None:
                    if not confirm_terminal_receipt(store.path):
                        return {
                            **_concept_lens_uncertain(
                                base_frame, current_generation, job_identity,
                                "The saved lens terminal is visible but its durable receipt is "
                                "unconfirmed. Resume only with this same request identity."),
                            # The terminal remains non-authoritative (ok=False, ambiguous=True), but
                            # its bounded event position is already visible in the exact ledger fold.
                            # Preserve that receipt coordinate just as malformed-terminal replay does;
                            # omitting it made the response shape depend on transient fsync contention.
                            "seq": terminal.seq,
                        }
                    return _concept_lens_terminal_response(
                        terminal, core, lens_pack, job_identity)
                if unresolved:
                    if unresolved != {job_identity}:
                        if job_identity in unresolved:
                            return _concept_lens_uncertain(
                                base_frame, current_generation, job_identity,
                                "Multiple paid-lens claims overlap this run generation and require repair.")
                        raise HTTPException(409, {
                            "code": "concept_lens_in_progress",
                            "message": "Another concept lens already owns this run generation.",
                            "remediation": "Wait for its receipt or reload before trying again.",
                        })
                    reservation = srv.jobs.rejoin(job_identity)
                    if reservation is None:
                        return _concept_lens_uncertain(
                            base_frame, current_generation, job_identity,
                            "The earlier paid lens attempt has no live process receipt.")
                    compute = lambda: _run_concept_lens_worker(  # noqa: E731
                        srv.llm_settings(rd), rd, current_generation, job_identity,
                        request_digest, prompt, core, lens_pack)
                else:
                    # A bounded partial frame is the exact safe substrate already rendered by GET.  Only
                    # corruption-adjacent reasons block paid work; cap limitations remain in the eventual
                    # derived response so the operator sees precisely what the model received.
                    blocking = [reason for reason in base_frame["completeness"]["reasons"]
                                if reason not in _TRUNCATION_CAP_REASONS]
                    if blocking:
                        return {
                            **base_frame,
                            "ok": False,
                            "reason": "concept_frame_partial",
                            "blocking_reasons": blocking,
                            "generation": current_generation,
                            "request_id": job_identity,
                        }
                    try:
                        settings = srv.llm_settings(rd)
                    except Exception as exc:  # noqa: BLE001 - no claim/provider exists yet
                        failure = safe_provider_failure(exc)
                        return {
                            **base_frame,
                            "ok": False,
                            "reason": "no_model",
                            "error_kind": failure["error_kind"],
                            "error": failure["message"],
                            "generation": current_generation,
                            "request_id": job_identity,
                        }
                    reservation = srv.jobs.reserve(job_identity, consume_on_poll=False)
                    if reservation.get("status") != "running":
                        return {
                            **base_frame,
                            **reservation,
                            "reason": "capacity",
                            "generation": current_generation,
                            "request_id": job_identity,
                        }
                    compute = lambda: _run_concept_lens_worker(  # noqa: E731
                        settings, rd, current_generation, job_identity,
                        request_digest, prompt, core, lens_pack)
                    try:
                        append_claim(
                            store, _CONCEPT_LENS_LEDGER, job_identity, current_generation,
                            request_digest=request_digest,
                            # Recovery's strict fold reads `input_seq` to bound what a lost-receipt
                            # projection may disclose; the ordinary fold never looks at it.
                            claim_fields={"input_seq": core["captured_seq"]},
                        )
                        srv.jobs.start_reserved(reservation["job_id"], compute)
                    except Exception:
                        srv.jobs.discard_reservation(str(reservation.get("job_id") or ""))
                        raise
                claimed.update(compute=compute, reservation=reservation,
                               job_identity=job_identity)
                return None

        early = await anyio.to_thread.run_sync(_claim)
        if early is not None:
            return early
        compute = claimed["compute"]
        reservation = claimed["reservation"]
        job_identity = claimed["job_identity"]

        result = await srv.jobs.run_as_job(
            compute,
            inline_wait=min(0.5, srv.jobs.inline_wait),
            consume_inline_result=False,
            reserved_job_id=reservation["job_id"],
        )
        if result.get("status") == "running":
            return {
                **result,
                "generation": expected_generation,
                "request_id": job_identity,
            }
        if result.get("code") == "job_failed":
            return _concept_lens_uncertain(
                base_frame, expected_generation, job_identity,
                "The lens worker ended without a durable terminal receipt. Resume only with this "
                "same request identity.")
        return result

    @router.get("/api/runs/{run_id}/concepts/lens/recovery")
    def recover_concept_lens_receipt(run_id: str, response: Response,
                                    expected_generation: str = Query(...)):
        """Discover current-generation paid work after the browser loses its private receipt.

        This owner-plane projection intentionally contains no prompt, digest, paid idempotency key,
        or resolution key. It is observational only: an orphan remains fenced until the operator
        submits the separately idempotent recovery-abandon command below.
        """
        from looplab.search.concept_lens import default_lenses

        if _RUN_GENERATION_RE.fullmatch(expected_generation) is None:
            raise HTTPException(400, {
                "code": "invalid_run_generation",
                "message": "expected_generation must be the exact generation from Concepts.",
            })
        rd = _run_dir(run_id)
        lens_pack = default_lenses()
        core = _materialize_concept_core(rd, run_id, None, lens_pack)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "X-LoopLab-Token, Authorization"

        with srv.commands.sequence(rd):
            rd, current_generation = _assert_lens_generation(
                srv, rd, core_generation=core[RUN_GENERATION_FIELD],
                expected_generation=expected_generation,
                stale_message="The run changed before paid-lens recovery was inspected.",
                stale_remediation="Reload Concepts and inspect only the current generation.",
                prepared_message="The run changed while its recovery projection was prepared.")

            store = EventStore(rd / "events.jsonl")
            claims, terminals, unresolved, conflict = _concept_lens_recovery_ledger(
                store.read_all(), current_generation)
            common = {
                "schema": _CONCEPT_LENS_RECOVERY_SCHEMA,
                "generation": current_generation,
            }
            if conflict or len(unresolved) > 1:
                return {
                    **common,
                    "state": "conflict",
                    "code": "concept_lens_recovery_conflict",
                    "message": (
                        "Paid-lens receipts are malformed or overlap; recovery is disabled until "
                        "the durable ledger is repaired."
                    ),
                }
            if unresolved:
                request_id = next(iter(unresolved))
                claim = claims[request_id]
                projection = {
                    **common,
                    "request_id": request_id,
                    "started_seq": claim["started_seq"],
                    "input_seq": claim["input_seq"],
                }
                process_receipt = srv.jobs.rejoin(request_id)
                if process_receipt is not None:
                    job_id = process_receipt.get("job_id")
                    process_job = srv.jobs.get(job_id) if isinstance(job_id, str) else None
                    if (isinstance(job_id, str) and re.fullmatch(r"[0-9a-f]{16}", job_id)
                            and process_job is not None
                            and process_job.get("status") in {"running", "done"}):
                        return {
                            **projection,
                            "state": "running",
                            "job_id": job_id,
                            "status": process_job["status"],
                        }
                return {**projection, "state": "orphaned"}
            if terminals:
                # Multiple completed requests are valid history. The latest claim is the only useful
                # lost-receipt candidate; only overlapping unresolved work is ambiguous above.
                request_id = max(
                    terminals, key=lambda identity: claims[identity]["started_seq"])
                claim = claims[request_id]
                terminal = terminals[request_id]
                if not confirm_terminal_receipt(store.path):
                    return {
                        **common,
                        "state": "conflict",
                        "code": "concept_lens_recovery_terminal_unconfirmed",
                        "message": "The visible terminal receipt could not be confirmed durable.",
                    }
                return {
                    **common,
                    "state": "terminal",
                    "request_id": request_id,
                    "started_seq": claim["started_seq"],
                    "input_seq": claim["input_seq"],
                    "terminal": _concept_lens_terminal_response(
                        terminal, core, lens_pack, request_id),
                }
            return {**common, "state": "none"}

    @router.post("/api/runs/{run_id}/concepts/lens/recovery/abandon")
    async def abandon_recovered_concept_lens(run_id: str, request: Request,
                                             response: Response):
        """Resolve one exactly identified orphan without possessing or replaying its paid key."""
        from looplab.search.concept_lens import default_lenses

        rd = _run_dir(run_id)
        body = await _concept_lens_json_body(request)
        expected_generation = body.get("expected_generation")
        if (not isinstance(expected_generation, str)
                or _RUN_GENERATION_RE.fullmatch(expected_generation) is None):
            raise HTTPException(400, {
                "code": "invalid_run_generation",
                "message": "expected_generation must be the exact generation from recovery.",
            })
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or _SHA256_RE.fullmatch(request_id) is None:
            raise HTTPException(400, {
                "code": "concept_lens_request_id_invalid",
                "message": "request_id must be the exact identifier from recovery.",
            })
        expected_started_seq = body.get("expected_started_seq")
        if (isinstance(expected_started_seq, bool)
                or not isinstance(expected_started_seq, int)
                or not 0 <= expected_started_seq <= _MAX_SAFE_INTEGER):
            raise HTTPException(400, {
                "code": "concept_lens_started_seq_invalid",
                "message": "expected_started_seq must be the exact safe integer from recovery.",
            })
        resolution_key = _concept_lens_resolution_key(
            request.headers.get("Resolution-Idempotency-Key", ""))

        lens_pack = default_lenses()
        core = _materialize_concept_core(rd, run_id, None, lens_pack)
        generation = core[RUN_GENERATION_FIELD]
        base_frame = _project_concept_frame(
            core, requested_lens="is_a", lens_pack=lens_pack)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = (
            "X-LoopLab-Token, Authorization, Resolution-Idempotency-Key")

        # OFF the event loop, for the same reason as the paid-lens claim above: `sequence()` waits on
        # a cross-process flock and this section then re-reads the log to resolve the claim. Every
        # exit inside it produces this handler's response, so the whole thing is simply the offloaded
        # body.
        def _resolve():
            nonlocal rd
            with srv.commands.sequence(rd):
                rd, current_generation = _assert_lens_generation(
                    srv, rd, core_generation=generation,
                    expected_generation=expected_generation,
                    stale_message="The run changed before the recovered claim could be resolved.",
                    stale_remediation=(
                        "Reload recovery; never resolve a claim from another generation."),
                    prepared_message="The run changed while its recovery frame was prepared.")

                store = EventStore(rd / "events.jsonl")
                claims, terminals, unresolved, conflict = _concept_lens_recovery_ledger(
                    store.read_all(), current_generation)
                if conflict or len(unresolved) > 1:
                    raise HTTPException(409, {
                        "code": "concept_lens_recovery_conflict",
                        "message": "The paid-lens ledger is not safe for automatic recovery.",
                    })
                claim = claims.get(request_id)
                if claim is None:
                    raise HTTPException(409, {
                        "code": "concept_lens_recovery_claim_missing",
                        "message": "No paid-lens claim matches this run generation and request_id.",
                    })
                if claim["started_seq"] != expected_started_seq:
                    raise HTTPException(409, {
                        "code": "concept_lens_started_seq_mismatch",
                        "expected_started_seq": expected_started_seq,
                        "current_started_seq": claim["started_seq"],
                        "message": "The durable claim does not match the inspected recovery receipt.",
                    })

                terminal = terminals.get(request_id)
                if terminal is not None:
                    if not confirm_terminal_receipt(store.path):
                        return _concept_lens_uncertain(
                            base_frame, current_generation, request_id,
                            "The recovered terminal is visible but its durability is unconfirmed.")
                    return _concept_lens_terminal_response(
                        terminal, core, lens_pack, request_id)
                if unresolved != {request_id}:
                    raise HTTPException(409, {
                        "code": "concept_lens_recovery_claim_missing",
                        "message": "The inspected claim is no longer the single unresolved paid request.",
                    })

                process_receipt = srv.jobs.rejoin(request_id)
                process_job = (srv.jobs.get(process_receipt["job_id"])
                               if process_receipt is not None else None)
                if process_job is not None and process_job.get("status") == "running":
                    raise HTTPException(409, {
                        "code": "concept_lens_still_running",
                        "message": "The original paid lens worker is still running in this process.",
                        "remediation": "Poll its job receipt instead of resolving it as an orphan.",
                    })

                resolution_id = _concept_lens_resolution_identity(
                    rd, current_generation, request_id, resolution_key)
                try:
                    terminal = store.append(
                        EV_CONCEPT_LENS_COMPLETED,
                        {
                            "lens_request_id": request_id,
                            "generation": current_generation,
                            "request_digest": claim["request_digest"],
                            "outcome": "abandoned",
                            "reason": "operator_recovered_abandon",
                            "resolution": "operator_recovery",
                            "resolution_id": resolution_id,
                        },
                        require_lock=True,
                        require_durable=True,
                    )
                except Exception:  # noqa: BLE001 - a possibly visible resolution must not be replayed
                    terminal = None
                if terminal is None or not confirm_terminal_receipt(store.path):
                    return _concept_lens_uncertain(
                        base_frame, current_generation, request_id,
                        "The recovery resolution could not be confirmed durable; no provider retry "
                        "was sent. Inspect recovery again before retrying this resolution.")
                return _concept_lens_terminal_response(
                    terminal, core, lens_pack, request_id)

        return await anyio.to_thread.run_sync(_resolve)

    @router.post("/api/runs/{run_id}/concepts/lens/abandon")
    async def abandon_concept_lens(run_id: str, request: Request, response: Response):
        """Explicitly terminalize an orphaned/uncertain paid claim without provider retry.

        This is deliberately operator-driven and never time-based.  A process-local running worker
        blocks abandonment, but a worker in an older process may still finish concurrently; the
        shared command sequencer makes its late terminal lose cleanly to whichever terminal commits
        first.  Provider-side completion, billing, and usage can remain unknowable after abandonment.
        """
        from looplab.search.concept_lens import default_lenses

        rd = _run_dir(run_id)
        body = await _concept_lens_json_body(request)
        expected_generation = body.get("expected_generation")
        if (not isinstance(expected_generation, str)
                or _RUN_GENERATION_RE.fullmatch(expected_generation) is None):
            raise HTTPException(400, {
                "code": "invalid_run_generation",
                "message": "expected_generation must be the exact generation from Concepts.",
            })
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or _SHA256_RE.fullmatch(request_id) is None:
            raise HTTPException(400, {
                "code": "concept_lens_request_id_invalid",
                "message": "request_id must be the exact receipt from the paid lens request.",
            })
        idempotency_key = _concept_lens_idempotency_key(
            request.headers.get("Idempotency-Key", ""))

        lens_pack = default_lenses()
        core = _materialize_concept_core(rd, run_id, None, lens_pack)
        generation = core[RUN_GENERATION_FIELD]
        base_frame = _project_concept_frame(
            core, requested_lens="is_a", lens_pack=lens_pack)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "X-LoopLab-Token, Authorization, Idempotency-Key"
        request_digest = None
        store_path = rd / "events.jsonl"

        # OFF the event loop, same as the paid-lens claim and the recovery resolution above:
        # `sequence()` waits on a cross-process flock and this inspection re-reads the log. Every
        # early exit inside raises, so `_inspect` only ever returns the names the tail below needs.
        def _inspect():
            nonlocal rd
            with srv.commands.sequence(rd):
                rd, current_generation = _assert_lens_generation(
                    srv, rd, core_generation=generation,
                    expected_generation=expected_generation,
                    stale_message="The run changed before the paid claim could be abandoned.",
                    stale_remediation=(
                        "Reload Concepts; never abandon a receipt from another generation."),
                    prepared_message="The run changed while the concept frame was being prepared.")
                computed_id = _concept_lens_identity(rd, current_generation, idempotency_key)
                if not hmac.compare_digest(request_id, computed_id):
                    raise HTTPException(409, {
                        "code": "concept_lens_request_mismatch",
                        "message": "request_id is not bound to this run, generation, and Idempotency-Key.",
                    })

                store = EventStore(rd / "events.jsonl")
                store_path = store.path
                ledger = fold_paid_ledger(
                    _CONCEPT_LENS_LEDGER, store.read_all(), current_generation)
                claims, terminals = ledger.claims, ledger.terminals
                unresolved, conflicts = ledger.unresolved, ledger.conflicts
                if request_id in conflicts:
                    raise HTTPException(409, {
                        "code": "concept_lens_ledger_conflict",
                        "message": "The paid-lens claim has conflicting durable receipts and needs repair.",
                    })
                terminal = terminals.get(request_id)
                if terminal is not None:
                    if not confirm_terminal_receipt(store.path):
                        return _concept_lens_uncertain(
                            base_frame, current_generation, request_id,
                            "The saved lens terminal is visible but its durability is unconfirmed.")
                    return _concept_lens_terminal_response(
                        terminal, core, lens_pack, request_id)
                request_digest = claims.get(request_id)
                if request_digest is None or request_id not in unresolved:
                    raise HTTPException(409, {
                        "code": "concept_lens_claim_missing",
                        "message": "No unresolved paid-lens claim matches this receipt.",
                        "remediation": "Reload Concepts and keep the original request receipt.",
                    })

                process_receipt = srv.jobs.rejoin(request_id)
                process_job = (srv.jobs.get(process_receipt["job_id"])
                               if process_receipt is not None else None)
                if process_job is not None and process_job.get("status") == "running":
                    raise HTTPException(409, {
                        "code": "concept_lens_still_running",
                        "message": "The original paid lens worker is still running in this process.",
                        "remediation": "Wait for its terminal receipt before choosing abandonment.",
                    })

            # `rd` rides the `nonlocal` above; the tail below needs only these two.
            return request_digest, store_path

        request_digest, store_path = await anyio.to_thread.run_sync(_inspect)
        # Re-enter the cross-process sequencer at the single terminal commit helper.  A worker from an
        # older server process can win between the inspection above and this call; in that case the
        # helper returns its real terminal instead of overwriting it with abandonment.
        terminal = _record_concept_lens_terminal(
            rd, expected_generation, request_id, request_digest,
            EV_CONCEPT_LENS_COMPLETED,
            {"outcome": "abandoned", "reason": "operator_abandoned", "resolution": "operator"},
        )
        if terminal is None or not confirm_terminal_receipt(store_path):
            return _concept_lens_uncertain(
                base_frame, expected_generation, request_id,
                "The operator resolution could not be confirmed durably; no provider retry was sent.")
        return _concept_lens_terminal_response(
            terminal, core, lens_pack, request_id)

    def _assert_historical_generation(rd: Path, expected: Optional[str]) -> str:
        if expected is None:
            raise HTTPException(400, {
                "code": "historical_generation_required",
                "message": "Historical node detail requires the exact run generation.",
                "remediation": "Use the generation returned by the historical /state response.",
            })
        with srv.commands.sequence(rd):
            rd = srv.commands.validate_paths(rd)
            current = srv.commands.run_generation(rd)
            if expected != current:
                raise HTTPException(409, {
                    "code": "run_generation_changed",
                    "expected_generation": expected,
                    "current_generation": current or None,
                    "message": "The run was reset or replaced before historical detail was read.",
                    "remediation": "Open the current generation or reload the exact historical view.",
                })
        return current

    @router.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str, request: Request):
        """Stream canonical public state frames, including the Cards completeness receipt."""
        rd = _run_dir(run_id)
        try:
            initial_entry = rd.lstat()
            initial_identity = (
                initial_entry.st_dev, initial_entry.st_ino, initial_entry.st_mode,
                int(getattr(initial_entry, "st_file_attributes", 0) or 0),
            )
        except OSError as exc:
            raise HTTPException(404, "no such run") from exc

        def _same_stream_run() -> bool:
            """Revalidate the exact directory captured before this streaming response started."""
            try:
                current = _run_dir(run_id)
                entry = current.lstat()
            except (HTTPException, OSError):
                # End the already-started response cleanly. The fetch-stream client reconnects and
                # receives the authoritative 404/410 (or a temporary availability response) from
                # the route boundary instead of accepting a fabricated empty state after quarantine.
                return False
            current_identity = (
                entry.st_dev, entry.st_ino, entry.st_mode,
                int(getattr(entry, "st_file_attributes", 0) or 0),
            )
            return current == rd and current_identity == initial_identity

        async def gen():
            last_sent = -2
            last_alive = None
            last_generation = None
            last_event_count = None
            last_beat = time.monotonic()
            # A quiet/"thinking" run (a long LLM call or eval) advances no seq and flips no liveness,
            # so without this the stream goes byte-silent. Behind jupyter-server-proxy (tornado) and
            # any nginx hop, an idle read-timeout then tears the connection down → the client reconnects
            # → full re-fold → drops again: a reconnect sawtooth that freezes the live UI. Emit an SSE
            # comment every KEEPALIVE seconds of silence so the proxy's idle timer never fires; a failed
            # keepalive write also surfaces a proxy-side disconnect promptly (X-Accel-Buffering is an
            # nginx-only hint that tornado ignores, so the regular small write is what actually flows).
            KEEPALIVE = 15.0
            # engine_running is a post-fold liveness probe with no seq of its own: a run that dies
            # AFTER its last event (a zombie) never advances seq, so also re-emit when liveness flips,
            # else the stalled/zombie UI never updates over a live stream.
            # Initial snapshot so a fresh/reconnecting client is immediately correct.
            while True:
                if await request.is_disconnected():
                    break
                payload = await anyio.to_thread.run_sync(_state_payload, rd)
                # Deletion atomically renames the directory out of the run namespace. Revalidate
                # after building the snapshot so that race cannot emit one ghost empty frame; a
                # later replacement at the same textual path is rejected by directory identity too.
                if not await anyio.to_thread.run_sync(_same_stream_run):
                    break
                alive = payload["state"].get("engine_running")
                generation = payload.get(RUN_GENERATION_FIELD)
                event_count = payload.get("event_count")
                if (payload["seq"] != last_sent or alive != last_alive
                        or generation != last_generation or event_count != last_event_count):
                    last_sent = payload["seq"]
                    last_alive = alive
                    last_generation = generation
                    last_event_count = event_count
                    last_beat = time.monotonic()
                    # CODEX AGENT: every event serializes and retransmits the complete growing folded
                    # state, so one long-lived client receives quadratic bytes and repeats whole-state
                    # encoding. Stream bounded deltas/event batches plus generation/checkpoint receipts,
                    # reserving full snapshots for initial/recovery sync.
                    yield (f"id: {payload['seq']}\n"
                           f"event: {SSE_STATE}\n"
                           f"data: {json.dumps(payload)}\n\n")
                    if (payload["state"].get("finished") and alive is False
                            and payload["state"].get("phase") != PHASE_FINALIZING):
                        # ``run_finished`` precedes the engine releasing its singleton while terminal
                        # reports/cost/lessons are still being flushed; an error finish with the stop
                        # intent preserved is also canonical finalization recovery, not terminal. Keep
                        # the stream open through
                        # that FINISHING window; emitting ``done`` on the event alone made the browser
                        # close/reconnect every 2.5s while the canonical lifecycle correctly remained
                        # non-terminal. Once the driver is gone, end this stream — the client then
                        # reconnect-polls so a later reopen is picked up without a manual reload.
                        yield f"event: {SSE_DONE}\ndata: {{}}\n\n"
                        break
                elif time.monotonic() - last_beat >= KEEPALIVE:
                    last_beat = time.monotonic()
                    yield ": keepalive\n\n"     # ignored by EventSource; resets the proxy read-timer
                await anyio.sleep(POLL_SECONDS)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ node detail
    def _node_attempt(st, nid: int) -> Optional[int]:
        """Current lifecycle for a folded node or its pre-create building marker.

        Kept as a local seam over the shared `core.node_evidence.node_attempt`, which the reviewer
        route reads too — both metric-sidecar readers must fence on the SAME attempt."""
        return node_attempt(st, nid)

    def _cached_node_attempt(rd: Path, nid: int) -> Optional[int]:
        """Current attempt from the metadata-keyed live-state projection cache.

        Trace/log polls need a before+after lifecycle CAS, but four fresh full event folds every four
        seconds defeats the point of the indexed trace path. ``_state_payload`` keys its fold by the
        exact events-file identity and returns a copy, so an unchanged log is a stat+lookup while an
        append/reset still changes the observation used by the second check.
        """
        frame = _state_payload(rd)
        state = frame.get("state") if isinstance(frame, dict) else None
        state = state if isinstance(state, dict) else {}
        nodes = state.get("nodes")
        nodes = nodes if isinstance(nodes, dict) else {}
        node = nodes.get(str(nid), nodes.get(nid))
        if isinstance(node, dict):
            attempt = node.get("attempt", 0)
            return attempt if type(attempt) is int and attempt >= 0 else 0
        buildings = state.get("buildings")
        marker = None
        if isinstance(buildings, dict):
            marker = buildings.get(str(nid), buildings.get(nid))
        elif isinstance(buildings, list):
            marker = next((row for row in buildings
                           if isinstance(row, dict) and row.get("node_id") == nid), None)
        building = state.get("building")
        if marker is None and isinstance(building, dict) and building.get("node_id") == nid:
            marker = building
        if isinstance(marker, dict):
            attempt = marker.get("generation")
            return attempt if type(attempt) is int and attempt >= 0 else 0
        return None

    def _legacy_node_log_dir_identity(rd: Path, nid: int) -> Optional[tuple[int, int]]:
        """Prove a legacy attempt-zero log directory is this run's exact node child.

        Old runs can have durable ``nodes/node_N`` logs without a folded ``node_created`` or
        ``node_building`` lifecycle row.  That compatibility case must not turn an arbitrary path
        occupying the legacy spelling into node evidence: require a correlated folded run identity,
        real (non-reparse) ``nodes`` and ``node_N`` directories, their canonical direct-child paths,
        and a stable inode observation.  ``node_logs`` compares this receipt again after reading, in
        addition to its run-generation/reset fences.
        """
        if type(nid) is not int or nid < 0:
            return None
        try:
            frame = _state_payload(rd)
            state = frame.get("state") if isinstance(frame, dict) else None
            if not isinstance(state, dict) or state.get("run_id") != rd.name:
                return None

            run = rd.resolve(strict=True)
            nodes = run / "nodes"
            node_dir = nodes / f"node_{nid}"
            reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))

            nodes_info = nodes.lstat()
            before = node_dir.lstat()
            if (not stat.S_ISDIR(nodes_info.st_mode)
                    or not stat.S_ISDIR(before.st_mode)
                    or int(getattr(nodes_info, "st_file_attributes", 0) or 0) & reparse_flag
                    or int(getattr(before, "st_file_attributes", 0) or 0) & reparse_flag
                    or nodes.resolve(strict=True) != nodes
                    or node_dir.resolve(strict=True) != node_dir):
                return None
            after = node_dir.lstat()
        except (OSError, RuntimeError, ValueError):
            return None

        # Type and reparse policy are validated immediately above on every call. The receipt itself
        # answers the canonical weaker question: did this child pathname keep pointing at the same
        # filesystem entry while logs were read?
        identity = same_file_entry(before)
        if not identity[1] or same_file_entry(after) != identity:
            return None
        return identity

    def _node_trace_snapshot(rd: Path, nid: int, *, attempt: int) -> tuple[dict, Optional[str]]:
        """Project one node trace only when its cheap file CAS is stable across the read."""
        before = trace_file_revision(rd / "spans.jsonl")
        trace = _node_trace(rd, nid, attempt=attempt)
        after = trace_file_revision(rd / "spans.jsonl")
        return trace, before if before is not None and before == after else None

    @router.get("/api/runs/{run_id}/nodes/{nid}")
    def node_detail(run_id: str, nid: int, seq: Optional[int] = None,
                    expected_generation: Optional[str] = None):
        rd = _run_dir(run_id)
        # Historical Inspector/Report must use the same prefix fold as the visible DAG.  Falling back
        # to the live fold here leaked later code, annotations and confirmations into old snapshots.
        request_generation = (_assert_historical_generation(rd, expected_generation)
                              if seq is not None or expected_generation is not None else None)
        historical_generation = request_generation if seq is not None else None
        st = fold(srv.events(rd, seq)) if seq is not None else srv.state(rd)
        if request_generation is not None:
            # The expensive fold runs without the exclusive command sequencer. A reset may win while
            # it is assembled, but a mixed-generation payload is rejected before any field is returned.
            request_generation = _assert_historical_generation(rd, expected_generation)
            if seq is not None:
                historical_generation = request_generation
        n = st.nodes.get(nid)
        if n is None:
            # A node still BUILDING has no node_created yet (not in st.nodes), but its create_node
            # sub-spans (propose/implement generations + tool calls) already flush to spans.jsonl tagged
            # with this node_id AS THEY COMPLETE. Serve a minimal in-progress detail carrying that live
            # trace instead of 404ing — otherwise the Trace tab can't fill in until the whole build ends
            # (the exact "nothing, then everything at once" the operator hit).
            if seq is not None:
                raise HTTPException(404, "no such node at requested sequence")
            # Prefer THIS node's own build marker from the multi-build collection; under parallel_build>1
            # the singular `st.building` holds only the last-appended build, so keying off it would return
            # a sibling build's operator/parent_ids. Fall back to the singular for a serial-build log.
            b = st.buildings.get(nid)
            if b is None and st.building and st.building.get("node_id") == nid:
                b = st.building
            attempt = _node_attempt(st, nid)
            trace, trace_revision = _node_trace_snapshot(rd, nid, attempt=attempt or 0)
            building = bool(b)
            if building or trace.get("nodes"):
                b = b or {}
                if request_generation is not None:
                    request_generation = _assert_historical_generation(rd, expected_generation)
                return {"id": nid, "status": "building",
                        "attempt": attempt or 0, "run_generation": request_generation,
                        "operator": b.get("operator"), "parent_ids": b.get("parent_ids", []),
                        "idea": None, "code": "", "annotations": [], "trace": trace,
                        "trace_revision": trace_revision}
            raise HTTPException(404, "no such node")
        out = n.model_dump(mode="json")
        out["annotations"] = st.annotations.get(nid, [])
        out["confirm_seeds_detail"] = st.confirm_seed_results.get(nid, {})
        # parent diff (vs the first parent's solution.py) — files-as-truth lineage
        if n.parent_ids:
            p = st.nodes.get(n.parent_ids[0])
            if p is not None:
                out["parent_code"] = p.code
                out["parent_id_diffed"] = p.id
        # spans.jsonl is a current sidecar rather than an event-versioned projection. Never label its
        # future contents as historical; the UI explains that trace is unavailable in History.
        # Deliberately NO `?span_limit=` here, even though this payload is what the Inspector's Trace
        # tab first renders. Three reasons, and the third is the one that bites: (1) this route folds
        # the whole event log and assembles code/annotations/parent-diff, so paging a trace through it
        # re-pays all of that per click, while `/nodes/{nid}/trace` is O(node) via the span index and
        # already takes a `limit`; (2) the historical branch below returns NO trace at all, so the
        # parameter would be dead exactly where the generation assertion is load-bearing; (3) a limit
        # that DID apply here would have to be re-fenced against `_assert_historical_generation`,
        # since the window is chosen after the first assertion and the payload is returned after the
        # last — a second paging path is a second chance to publish a mixed-generation payload for a
        # window the operator could already get from the endpoint built for it.
        if seq is None:
            out["trace"], out["trace_revision"] = _node_trace_snapshot(
                rd, nid, attempt=n.attempt)
        else:
            out["trace"], out["trace_revision"] = {"nodes": []}, None
        if request_generation is not None:
            request_generation = _assert_historical_generation(rd, expected_generation)
        out["run_generation"] = request_generation
        if seq is not None:
            out["historical_seq"] = seq
            out["historical_generation"] = historical_generation
        return out

    def _node_dir(rd: Path, nid: int) -> Path:
        return rd / "nodes" / f"node_{nid}"

    @router.get("/api/runs/{run_id}/nodes/{nid}/logs")
    def node_logs(run_id: str, nid: int, tail: int = 200_000,
                  attempt: Optional[int] = Query(default=None, ge=0),
                  expected_generation: Optional[str] = Query(default=None)):
        """Live training/eval logs for a node — the streamed stdout/stderr of its eval + setup
        subprocesses. `tail` caps bytes returned (from the end) so the UI can poll cheaply. Empty
        strings when a log doesn't exist yet.

        A SINGLE-command eval tees to `eval.log`; a MULTI-STAGE eval (data_prep → train → score) tees
        each stage to its OWN `<stage>.log` (command_eval `_log(f"{name}.log")`) and never writes
        `eval.log`. So `eval` carries ONLY eval.log's tail (empty for a multi-stage node — deliberately
        NO fallback duplication into it), and the per-stage tails come back as the ordered `stages`
        map, which the UI's log panel renders per stage."""
        rd = _run_dir(run_id)
        _assert_trace_reset_clear(rd)
        if (expected_generation is not None
                and _RUN_GENERATION_RE.fullmatch(expected_generation) is None):
            raise HTTPException(400, {
                "code": "invalid_run_generation",
                "message": "expected_generation must be the exact generation from run state.",
                "remediation": "Reload the run before reading node logs.",
            })
        before_generation = srv.commands.run_generation(rd)
        if expected_generation is not None and before_generation != expected_generation:
            raise HTTPException(409, {
                "code": "run_generation_changed",
                "expected_generation": expected_generation,
                "current_generation": before_generation or None,
                "message": "The run was reset or replaced before its node logs were read.",
                "remediation": "Reload run state and request the current generation.",
            })
        current_attempt = _cached_node_attempt(rd, nid)
        legacy_node_identity = None
        if current_attempt is None:
            # Pre-lifecycle event logs still own useful attempt-zero stdout. Accept that narrow
            # compatibility shape only when its physical node directory is positively bound to this
            # run; the receipt is rechecked below so a reset/path replacement cannot publish bytes.
            legacy_node_identity = _legacy_node_log_dir_identity(rd, nid)
            if legacy_node_identity is None:
                raise HTTPException(404, "no such node")
            current_attempt = 0
        if attempt is not None and attempt != current_attempt:
            raise HTTPException(409, {
                "code": "node_attempt_changed",
                "node_id": nid,
                "expected_attempt": attempt,
                "current_attempt": current_attempt,
                "message": "The node was reset before its logs were read.",
                "remediation": "Reload node state and request the current attempt.",
            })
        nd = _node_dir(rd, nid)
        # Clamp the client-controlled tail so a hostile/large value can't force an unbounded read; and
        # seek to the tail instead of read_bytes() so we never pull a multi-GB training log into RAM.
        # Per-stage logs — a multi-stage eval tees each stage to `<name>.log`. Bound the set to the
        # node's DECLARED stages, in pipeline order. NOT a bare `*.log` glob: that would surface any
        # stray log the training code writes to its cwd (a framework's own debug.log) as a phantom
        # stage band, and let an unbounded file count inflate the response. Each `<name>.log` is
        # tailed independently (a missing/racing one just yields "").
        # OPERATOR-declared stages first: when the task's `cmd` carries VALID `stages`, those ARE the
        # canonical pipeline (Engine._resolve_stages ignores the Developer's manifest) and no `score`
        # stage is appended — the LAST operator stage prints the metric. Their names come from the
        # run's verbatim task.snapshot.json (normalize_task maps composable `cmd` → `eval`,
        # validate_stages vets the list the same way the engine does), memoized on the snapshot's
        # stat identity (a rewrite self-invalidates) — so live logs surface for operator cmd.stages
        # pipelines too, the very mode the per-stage logs fix targeted (mega-review D7). list() the
        # cached tuple: the fallback below appends "score", which must never leak into the cache.
        stage_names: list[str] = list(_operator_stage_names(rd))
        if not stage_names:
            # Single-command cmd (or an INVALID operator stage list — engine parity: it too ignores
            # a bad list and takes this path): the Developer's `looplab_stages.json` manifest
            # declares the PRECEDING stages, and the engine appends the protected, operator-owned
            # `score` stage after them. Re-read EVERY poll, never memoized — the manifest is written
            # mid-node by the STAGES phase, so it can appear between polls and differs per node.
            try:
                from looplab.runtime.command_eval import materialized_stages
                man = json.loads((nd / "looplab_stages.json").read_text("utf-8"))
                clean = materialized_stages(man)
                stage_names = [str(s["name"]) for s in clean] if clean else []
            except (OSError, ValueError, TypeError):
                pass
            if "score" not in stage_names:  # the engine appends a protected `score` stage post-manifest
                stage_names.append("score")
        # `tail` is a per-log convenience limit, but the HTTP response itself also needs one hard
        # envelope. Divide the existing byte cap across declared stages plus eval/setup/run setup, so
        # a valid many-stage pipeline cannot multiply a 5 MB query into an 80+ MB response.
        requested_n = min(max(0, tail), _LOG_TAIL_MAX)
        file_slots = max(1, len(stage_names) + 3)
        n = min(requested_n, _LOG_TAIL_MAX // file_slots)

        def _tail(name: str, base: Path = nd) -> str:
            try:
                root = base.resolve()
                p = (root / name).resolve()
                # Names are direct children, not paths. This second boundary also rejects a log
                # symlink that points outside the run/node directory.
                if p.parent != root:
                    return ""
                size = p.stat().st_size
                with open(p, "rb") as f:
                    if size > n:
                        f.seek(size - n)
                    b = f.read(n)
            except (OSError, ValueError):
                return ""
            return b.decode("utf-8", "replace")

        stages = {name: body for name in stage_names if (body := _tail(f"{name}.log"))}
        # run_setup.log lives in the RUN dir (shared setup), not the node dir. Tail-cap it like every
        # other log here (seek-to-end, byte-bounded) instead of read_text()-ing the whole file into RAM
        # on every poll — a verbose dependency-install log would otherwise defeat the tail cap.
        payload = {"eval": _tail("eval.log"), "stages": stages, "setup": _tail("setup.log"),
                   "run_setup": _tail("run_setup.log", rd)}
        _assert_trace_reset_clear(rd)
        after_generation = srv.commands.run_generation(rd)
        after_attempt = _cached_node_attempt(rd, nid)
        if legacy_node_identity is not None:
            after_legacy_identity = _legacy_node_log_dir_identity(rd, nid)
            if after_legacy_identity != legacy_node_identity:
                after_attempt = None
            elif after_attempt is None:
                after_attempt = 0
        if (after_generation != before_generation
                or (expected_generation is not None
                    and after_generation != expected_generation)):
            raise HTTPException(409, {
                "code": "run_generation_changed",
                "expected_generation": expected_generation or before_generation or None,
                "current_generation": after_generation or None,
                "message": "The run was reset or replaced while its node logs were being read.",
                "remediation": "Reload run state and request the current generation.",
            })
        if after_attempt != current_attempt:
            raise HTTPException(409, {
                "code": "node_attempt_changed",
                "node_id": nid,
                "expected_attempt": current_attempt,
                "current_attempt": after_attempt,
                "message": "The node was reset while its logs were being read.",
                "remediation": "Reload node state and request the current attempt.",
            })
        return {**payload, "node_id": nid, "attempt": current_attempt,
                "run_generation": after_generation or None}

    @router.get("/api/runs/{run_id}/nodes/{nid}/metrics")
    def node_metrics(run_id: str, nid: int,
                     attempt: Optional[int] = Query(default=None, ge=0)):
        """Online metric SERIES a node's training logged — every scalar (loss, each recall@k, grad
        norms, lr, …), not just the objective — read via the pluggable metrics adapters (TensorBoard
        today). The response is fenced to ``node_id`` + lifecycle ``attempt``; reset-era points are
        excluded by the engine's attempt receipt. Empty until current-attempt logs appear."""
        from looplab.serve.metrics_adapters import fenced_node_metrics
        rd = _run_dir(run_id)
        current_attempt = _node_attempt(srv.state(rd), nid)
        if current_attempt is None:
            raise HTTPException(404, "no such node")
        if attempt is not None and attempt != current_attempt:
            raise HTTPException(409, {
                "code": "node_attempt_changed",
                "node_id": nid,
                "expected_attempt": attempt,
                "current_attempt": current_attempt,
                "message": "The node was reset before its metric evidence was read.",
                "remediation": "Reload node state and request the current attempt.",
            })
        m = fenced_node_metrics(_node_dir(rd, nid), current_attempt)
        after_attempt = _node_attempt(srv.state(rd), nid)
        if after_attempt != current_attempt:
            raise HTTPException(409, {
                "code": "node_attempt_changed",
                "node_id": nid,
                "expected_attempt": current_attempt,
                "current_attempt": after_attempt,
                "message": "The node was reset while its metric evidence was being read.",
                "remediation": "Reload node state and request the current attempt.",
            })
        return {"node_id": nid, "attempt": current_attempt, "metrics": m}

    def _node_trace(rd: Path, nid: int, cap: Optional[int] = None, *,
                    attempt: int = 0) -> dict:
        try:
            # light=True: the tree carries structure + tokens + timing but NOT prompts/outputs. The UI
            # fetches a bounded/redacted observation projection lazily via /spans/{sid} when expanded,
            # so a heavily-repaired node's trace stays small and its omission receipt remains explicit.
            # `srv.node_trace_view` builds over ONLY this node's spans via the light span INDEX (in-
            # memory, O(node) — not the whole-run tree), so a 1 GB, 4000-node run's node trace is ~ms.
            tv = srv.node_trace_view(rd, nid, cap=cap, generation=attempt)
            return {"node_id": nid, "attempt": attempt, "schema": tv.get("schema"),
                    "nodes": tv.get("nodes", {}).get(str(nid), []),
                    "rollup": tv.get("rollups", {}).get(str(nid), {}),
                    "summary": tv.get("summary", {}),
                    "projection": tv.get("projection", {})}
        except Exception:  # noqa: BLE001
            return {"node_id": nid, "attempt": attempt,
                    **_trace_unavailable(nodes=[], rollup={}, summary={})}

    @router.get("/api/runs/{run_id}/nodes/{nid}/trace")
    def node_trace(run_id: str, nid: int, limit: int = Query(default=0, ge=0),
                   attempt: Optional[int] = Query(default=None, ge=0),
                   expected_generation: Optional[str] = Query(default=None)):
        """The LIGHT trace tree for ONE node — the hot path for expanding a node's trace card. Reads
        only that node's spans via the index (O(node)), so the UI can fetch a node's trace lazily on
        expand instead of loading (and re-rendering) the whole-run timeline for a 4000-node run.

        `limit` (>0) is the UI's "load more spans" control: it raises this node's span ceiling on demand
        (default 512, clamped to TRACE_NODE_SPAN_CAP_MAX in node_trace_view); 0/absent keeps the default.
        A negative limit is refused at the boundary (422) rather than silently read as the default — the
        same wire contract as the conversation twin, so both pagers fail loudly on a client defect."""
        rd = _run_dir(run_id)
        _assert_trace_reset_clear(rd)
        if (expected_generation is not None
                and _RUN_GENERATION_RE.fullmatch(expected_generation) is None):
            raise HTTPException(400, {
                "code": "invalid_run_generation",
                "message": "expected_generation must be the exact generation from run state.",
                "remediation": "Reload the run before reading its node trace.",
            })
        before_generation = srv.commands.run_generation(rd)
        if expected_generation is not None and before_generation != expected_generation:
            raise HTTPException(409, {
                "code": "run_generation_changed",
                "expected_generation": expected_generation,
                "current_generation": before_generation or None,
                "message": "The run was reset or replaced before its node trace was read.",
                "remediation": "Reload run state and request the current generation.",
            })
        observed_attempt = _cached_node_attempt(rd, nid)
        current_attempt = observed_attempt if observed_attempt is not None else 0
        # Setup/pseudo-node and legacy trace rows have no folded lifecycle marker and therefore use
        # attempt zero.  An EXPLICIT non-current attempt is never a historical selector: returning
        # it would let an old event row commit abandoned-attempt evidence into the current run.
        if attempt is not None and attempt != current_attempt:
            raise HTTPException(409, {
                "code": "node_attempt_changed",
                "node_id": nid,
                "expected_attempt": attempt,
                "current_attempt": current_attempt,
                "message": "The node was reset before its trace was read.",
                "remediation": "Reload node state and request the current attempt.",
            })
        attempt = current_attempt
        # 0/absent settles to the default inside `node_trace_view` — the one settle rule owns it, so
        # this route does not get a second opinion about what an unrequested window means.
        payload = _node_trace(rd, nid, cap=limit, attempt=attempt)
        _assert_trace_reset_clear(rd)
        after_observed_attempt = _cached_node_attempt(rd, nid)
        after_attempt = after_observed_attempt if after_observed_attempt is not None else 0
        if after_attempt != current_attempt:
            raise HTTPException(409, {
                "code": "node_attempt_changed",
                "node_id": nid,
                "expected_attempt": current_attempt,
                "current_attempt": after_attempt,
                "message": "The node was reset while its trace was being read.",
                "remediation": "Reload node state and request the current attempt.",
            })
        after_generation = srv.commands.run_generation(rd)
        if (after_generation != before_generation
                or (expected_generation is not None
                    and after_generation != expected_generation)):
            raise HTTPException(409, {
                "code": "run_generation_changed",
                "expected_generation": expected_generation or before_generation or None,
                "current_generation": after_generation or None,
                "message": "The run was reset or replaced while its node trace was being read.",
                "remediation": "Reload run state and request the current generation.",
            })
        return _trace_response({**payload, "run_generation": after_generation or None})

    @router.get("/api/runs/{run_id}/spans/{sid}")
    def span_io(run_id: str, sid: str,
                expected_generation: Optional[str] = Query(default=None)):
        """Bounded, redacted I/O projection for one observation; raw diagnostics stay in spans.jsonl."""
        rd = _run_dir(run_id)
        before_generation = _begin_trace_read(rd, expected_generation)
        try:
            # Seek straight to the span's byte offset via the index instead of scanning the whole
            # (up to 1 GB) spans.jsonl for one span. Falls back to a scan if the index lacks it (a
            # span past the indexed tail, a foreign/torn file) so nothing is ever unreachable.
            from looplab.events.span_index import get_index
            from looplab.events.traceview import (
                TRACE_DETAIL_SPAN_CAP, _cap_span_io, _cap_str, _normalize_span)
            idx = get_index(rd / "spans.jsonl")
            s = idx.full_span(sid) if idx is not None else None
            indexed_span = s is not None
            # Scan only what the index does NOT cover. Everything before `covers` was already
            # searched by `full_span` above, so re-walking it can only ever find nothing — and `sid`
            # is an unvalidated path param, so every bogus id used to walk the whole file. With an
            # index the window is the few just-appended lines the fallback exists for; without one
            # (a foreign or unparseable spans.jsonl) `_scan_span_tail`'s line cap bounds it.
            if s is None:
                raw_span = _scan_span_tail(
                    rd / "spans.jsonl", sid,
                    int(getattr(idx, "covers", 0) or 0) if idx is not None else 0,
                    expected_identity=(getattr(idx, "identity", None)
                                       if idx is not None else None))
                if raw_span is not None:
                    s = _normalize_span(raw_span)
            if s is not None:
                a = s.get("attributes") or {}
                trace_spans = []
                trace_total = (idx.trace_span_count(s.get("trace_id"))
                               if idx is not None and indexed_span else None)
                # Delta-encoded generation: reconstruct its input from a bounded trace window, then
                # project it through the per-message/per-span caps below.
                # (tracing stores only the per-turn delta to keep spans.jsonl ~6x smaller) — so the
                # expanded observation shows the retained diagnostic input projection. No-op for
                # tools/old logs; capture-time filtering and response caps still apply.
                if s.get("kind") == "generation" and "input_carry" in a:
                    from looplab.events.traceview import hydrate_inputs
                    tid = s.get("trace_id")
                    trace_spans = (idx.full_spans_for_trace(
                        tid, TRACE_DETAIL_SPAN_CAP, anchor_sid=sid) if idx is not None else [s])
                    # `s` may be a just-appended span past the indexed tail (found via the scan fallback
                    # above but absent from the index snapshot) — include it so its own delta joins the
                    # chain and reconstruction resolves, instead of returning the raw delta until top-up.
                    if not any(c.get("span_id") == sid for c in trace_spans):
                        trace_spans = trace_spans + [s]
                    s = next((h for h in hydrate_inputs(trace_spans) if h.get("span_id") == sid), s)
                s = _cap_span_io(s)
                projection = dict(s.get("_projection") or {})
                # Snapshot the selected span's own omission truth BEFORE folding in
                # trace cardinality. Hidden siblings make the aggregate response partial, but they
                # do not make this span's bounded input/output projection incomplete.
                detail_truncated = projection.get("truncated") is True
                projection["detail_truncated"] = detail_truncated
                visible = len(trace_spans) if trace_spans else 1
                if trace_total is None:
                    projection.update({
                        "trace_cardinality_unavailable": True,
                        "truncated": True,
                    })
                else:
                    trace_total = max(visible, trace_total)
                    omitted_trace_spans = max(0, trace_total - visible)
                    siblings_elided = omitted_trace_spans > 0
                    projection.update({
                        "trace_total_spans": trace_total,
                        "trace_visible_spans": visible,
                        "omitted_trace_spans": omitted_trace_spans,
                        "siblings_elided": siblings_elided,
                        "truncated": detail_truncated or siblings_elided,
                    })
                payload = {"schema": TRACE_PROJECTION_SCHEMA, "span_id": s.get("span_id"),
                        "name": s.get("name"), "kind": s.get("kind"),
                        "attributes": s.get("attributes") or {}, "events": s.get("events") or [],
                        "duration_s": s.get("duration_s"), "status": s.get("status"),
                        "projection": projection}
                generation = _finish_trace_read(
                    rd, before_generation, expected_generation)
                return {**payload, "run_generation": generation or None}
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            pass
        try:
            safe_sid = _cap_str(sid, 256)
        except Exception:  # noqa: BLE001
            safe_sid = ""
        generation = _finish_trace_read(rd, before_generation, expected_generation)
        return {**_trace_unavailable(span_id=safe_sid, attributes={}, events=[]),
                "run_generation": generation or None}

    @router.get("/api/runs/{run_id}/trace/tail")
    def trace_tail(run_id: str, limit: int = 30,
                   expected_generation: Optional[str] = Query(default=None)):
        """LIVE 'what is the agent doing right now' feed: the most recent generation (LLM thinking/
        output) + tool (name + args) observations, newest last. Powers the Dock's live-trace disclosure
        so a user can watch the agent reason/act during a coarse 'Thinking…'/'Planning…' status instead
        of only seeing the label. Reads just the TAIL of spans.jsonl (bounded regardless of run length);
        text is capped here; /spans/{sid} exposes a larger bounded/redacted detail projection."""
        rd = _run_dir(run_id)
        _assert_trace_reset_clear(rd)
        if (expected_generation is not None
                and _RUN_GENERATION_RE.fullmatch(expected_generation) is None):
            raise HTTPException(400, {
                "code": "invalid_run_generation",
                "message": "expected_generation must be the exact generation from run state.",
                "remediation": "Reload the run before reading its live trace.",
            })
        before_generation = srv.commands.run_generation(rd)
        if expected_generation is not None and before_generation != expected_generation:
            raise HTTPException(409, {
                "code": "run_generation_changed",
                "expected_generation": expected_generation,
                "current_generation": before_generation or None,
                "message": "The run was reset or replaced before its live trace was read.",
                "remediation": "Reload run state and request the current generation.",
            })

        def _generation_bound(payload: dict) -> dict:
            """Publish only a snapshot whose run identity stayed stable for the whole read."""
            _assert_trace_reset_clear(rd)
            after_generation = srv.commands.run_generation(rd)
            if (after_generation != before_generation
                    or (expected_generation is not None
                        and after_generation != expected_generation)):
                raise HTTPException(409, {
                    "code": "run_generation_changed",
                    "expected_generation": expected_generation or before_generation or None,
                    "current_generation": after_generation or None,
                    "message": "The run was reset or replaced while its live trace was being read.",
                    "remediation": "Reload run state and request the current generation.",
                })
            payload["run_generation"] = after_generation or None
            return payload
        # Default poll stays small (the Dock polls limit=40); the ceiling is raised so the UI's
        # "load earlier spans" control can page further back through history on explicit demand. The
        # backward scan below is still bounded by _MAX_TAIL (8 MiB), so a larger limit never re-reads
        # an unbounded file — it just surfaces more of the already-bounded tail window.
        limit = max(1, min(int(limit or 30), 400))
        # Read BACKWARD from EOF until we have `limit` complete generation/tool observations (or hit a
        # hard ceiling), instead of a fixed 256KB window. A single span line can be 100KB+ (a
        # repo-Developer generation carries the whole prompt+output on it), so a fixed window could land
        # ENTIRELY inside one line and return an empty feed during the heavy generations a user most
        # wants to watch. The hard ceiling ensures a multi-MB spans.jsonl is never re-parsed in full on
        # every poll.
        # Collected newest -> oldest while the file is scanned backwards, then reversed for the wire.
        # Keeping only qualifying observations means a dense tail of operation spans cannot consume the
        # generation/tool limit and hide the activity immediately before it.
        recent: list[dict] = []
        from looplab.events.traceview import (
            _cap_str, _finite_number, _normalize_span)
        _CHUNK = 262144
        _MAX_TAIL = 8 * 1024 * 1024
        source_truncated = False
        p = rd / "spans.jsonl"

        def _observation(line: bytes) -> Optional[dict]:
            """Project one complete physical line, or None when it is not a live observation."""
            if not line.strip():
                return None
            try:
                s = json.loads(line)
            except (ValueError, TypeError):
                return None
            s = _normalize_span(s)
            if s is None or s.get("kind") not in ("generation", "tool"):
                return None
            a = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
            node_id = a.get("node_id")
            if not ((isinstance(node_id, int) and not isinstance(node_id, bool)
                     and 0 <= node_id <= (1 << 63) - 1)
                    or (isinstance(node_id, str) and 0 < len(node_id) <= 128)):
                node_id = None
            elif isinstance(node_id, str):
                node_id = _cap_str(node_id, 128)
            it = {"span_id": s.get("span_id"), "kind": s.get("kind"),
                  "node_id": node_id,
                  "start": _finite_number(s.get("start"), maximum=1e15),
                  "duration_s": _finite_number(s.get("duration_s"), nonnegative=True, maximum=1e15),
                  "status": _cap_str(str(s.get("status") or ""), 32)}
            if s.get("kind") == "generation":
                it["model"] = _cap_str(str(a.get("model") or ""), 160)
                txt = a.get("thinking") or a.get("output") or ""
                it["text"] = _cap_str(txt, 500) if isinstance(txt, str) else ""
            else:
                it["tool"] = _cap_str(str(a.get("tool") or ""), 128)
                inp = a.get("input")
                if isinstance(inp, dict):
                    arg = inp.get("path") or inp.get("pattern") or inp.get("query") \
                        or inp.get("command") or inp.get("root") or ""
                    it["arg"] = _cap_str(str(arg), 160)
                it["output"] = _cap_str(str(a.get("output") or ""), 200)
            return it

        try:
            # Read at most the exact 8 MiB suffix. `pending` holds fragments of ONE line in reverse
            # chunk order; it is joined only once, when the preceding newline (or BOF) is reached. This
            # keeps a multi-chunk generation line linear instead of repeatedly prepending/copying the
            # whole accumulated suffix (`blob = chunk + blob`).
            # Open first and derive the snapshot size from THIS hardened descriptor.  Besides the
            # reset-safe single-inode snapshot, nofollow+nlink/type checks keep this public polling
            # route from becoming a cross-run/external-file reader or blocking on a crafted FIFO.
            with open_private_trace_file(p, open_file=open) as f:
                descriptor_stat = os.fstat(f.fileno())
                sz = descriptor_stat.st_size
                lower = max(0, sz - _MAX_TAIL)
                source_truncated = lower > 0
                start = sz
                pending: list[bytes] = []
                at_eof = True
                stopped = False
                while start > lower and not stopped:
                    step = min(_CHUNK, start - lower)
                    start -= step
                    f.seek(start)
                    chunk = f.read(step)
                    if len(chunk) != step:
                        raise OSError(
                            f"short read of trace tail: expected {step} bytes, got {len(chunk)}")
                    right = len(chunk)
                    while True:
                        newline = chunk.rfind(b"\n", 0, right)
                        if newline < 0:
                            if right:
                                pending.append(chunk[:right])
                            break
                        fragment = chunk[newline + 1:right]
                        if fragment:
                            pending.append(fragment)
                        line = b"".join(reversed(pending)) if pending else b""
                        pending.clear()

                        # The bytes after the first newline seen from EOF are a torn final row unless
                        # the snapshot ended in a newline (in that case `line` is simply empty). A span
                        # is published only by its terminating newline, so never surface that fragment.
                        eof_fragment = at_eof
                        at_eof = False
                        if eof_fragment and line:
                            source_truncated = True
                        elif (item := _observation(line)) is not None:
                            recent.append(item)
                            if len(recent) >= limit:
                                # We stopped inside this chunk. Any bytes before the selected row are an
                                # uninspected source prefix even when the 8 MiB byte ceiling was not hit.
                                source_truncated = source_truncated or start > 0 or newline > 0
                                stopped = True
                        right = newline
                        if stopped:
                            break

                # BOF completes the first physical line without a preceding newline. So does an exact
                # suffix boundary whose immediately preceding byte is a newline: without that one-byte
                # probe, a complete qualifying row of exactly _MAX_TAIL bytes is silently dropped just
                # because its first byte happens to equal `lower`.
                boundary_completes_line = start == 0
                if not stopped and pending and start == lower and lower > 0:
                    f.seek(lower - 1)
                    boundary = f.read(1)
                    if len(boundary) != 1:
                        raise OSError("short read while validating trace-tail boundary")
                    boundary_completes_line = boundary == b"\n"

                # The sole exception is a one-line file with no final newline: that is the same torn
                # EOF row handled above and remains invisible.
                if not stopped and pending and boundary_completes_line:
                    line = b"".join(reversed(pending))
                    if at_eof:
                        source_truncated = True
                    elif (item := _observation(line)) is not None:
                        recent.append(item)

                # open_private_trace_file rechecks path↔descriptor identity, type and link ownership
                # when this block exits. A generation check alone cannot see the reset phase where
                # spans.jsonl was replaced before its first new event.
        except FileNotFoundError:
            # Only INITIAL absence reaches this branch. The helper translates disappearance after
            # lstat/open into ESTALE, so a reset race cannot be laundered into exact-empty evidence.
            return _generation_bound({
                "schema": TRACE_PROJECTION_SCHEMA, "tail": [],
                "projection": {"schema": TRACE_PROJECTION_SCHEMA,
                               "truncated": False, "source_truncated": False,
                               "visible_spans": 0, "omitted_spans": 0},
            })
        except OSError:
            # The source existed at the initial lookup but became unreadable/vanished before or
            # during the snapshot read. Its cardinality is unavailable, not exact zero.
            return _generation_bound(_trace_unavailable(tail=[]))
        shown = list(reversed(recent))             # append/file order: oldest -> newest
        omitted = 0                                # older qualifying rows were not inspected/countable
        return _generation_bound({
            "schema": TRACE_PROJECTION_SCHEMA, "tail": shown,
            "projection": {"schema": TRACE_PROJECTION_SCHEMA,
                           "truncated": source_truncated or omitted > 0,
                           "source_truncated": source_truncated,
                           "visible_spans": len(shown), "omitted_spans": omitted},
        })

    @router.get("/api/runs/{run_id}/nodes/{nid}/conversation")
    def node_conversation(run_id: str, nid: int, request: Request, response: Response,
                          limit: int = Query(default=0, ge=0),
                          attempt: Optional[int] = Query(default=None, ge=0),
                          expected_generation: Optional[str] = Query(default=None)):
        """The node's trace as a LINEAR, de-duplicated conversation: the system+user request shown
        once per sub-loop, then each generation's delta (reasoning + text + tool calls) interleaved
        with the tool executions — so the agent's activity reads without the recorded tree's per-turn
        re-send of the whole message history. All text remains bounded/redacted for the browser.

        `limit` (>0) is the Inspector's "load more" control, the conversation twin of the one on
        `/trace`: it raises this node's conversation WINDOW — spans read, plus the stage/turn render
        caps derived from it (`conversation_render_caps`) — on demand. 0/absent keeps the default
        window; anything above TRACE_NODE_SPAN_CAP_MAX clamps to it (`settle_node_span_cap`), which
        is the same ceiling the span tree pages against. A NEGATIVE limit is refused at the boundary
        (422) rather than silently read as "default": it can only be a client defect, and a paging
        control that quietly ignores its own argument is how a dead pager looks from the outside.

        ``attempt`` binds this CURRENT-node view to the lifecycle rendered by the Inspector. It is
        checked before and after the read; old attempts remain available through attempt-history
        evidence rather than being silently concatenated into the current conversation.

        Successful stable snapshots carry an opaque ``cursor`` equal to their ETag. A caller may
        return it as ``If-None-Match``; an unchanged node/generation/window then answers 304 before
        the full rows are read or the conversation is rebuilt. Legacy unconditional callers keep
        receiving the same 200 JSON envelope (with one additive cursor field)."""
        rd = _run_dir(run_id)
        _assert_trace_reset_clear(rd)
        if (expected_generation is not None
                and _RUN_GENERATION_RE.fullmatch(expected_generation) is None):
            raise HTTPException(400, {
                "code": "invalid_run_generation",
                "message": "expected_generation must be the exact generation from run state.",
                "remediation": "Reload the run before reading its node conversation.",
            })
        before_generation = srv.commands.run_generation(rd)
        if expected_generation is not None and before_generation != expected_generation:
            raise HTTPException(409, {
                "code": "run_generation_changed",
                "expected_generation": expected_generation,
                "current_generation": before_generation or None,
                "message": "The run was reset or replaced before its conversation was read.",
                "remediation": "Reload run state and request the current generation.",
            })
        # This is the CURRENT-node Inspector, not an attempt-history endpoint. Resolve an omitted
        # attempt for transitional/legacy callers, but reject an explicitly stale lifecycle before
        # reading any potentially large trace window. A node-less legacy trace belongs to attempt 0,
        # matching the per-node trace/index convention.
        current_attempt = _cached_node_attempt(rd, nid)
        current_attempt = current_attempt if current_attempt is not None else 0
        if attempt is not None and attempt != current_attempt:
            raise HTTPException(409, {
                "code": "node_attempt_changed",
                "node_id": nid,
                "expected_attempt": attempt,
                "current_attempt": current_attempt,
                "message": "The node was reset before its conversation evidence was read.",
                "remediation": "Reload node state and request the current attempt.",
            })

        from looplab.events.traceview import (
            TRACE_CONVERSATION_SPAN_CAP, build_conversation, load_spans, settle_node_span_cap)
        from looplab.events.span_index import get_index
        span_cap = settle_node_span_cap(limit, default=TRACE_CONVERSATION_SPAN_CAP)
        missing_revision = hashlib.sha256(b"looplab:conversation-source:missing:v1").hexdigest()
        trace_scalars = srv.trace_scalars(rd)

        def source_snapshot():
            """Cheap selected-row identity plus whether it may authorize a bodyless response."""
            observed = get_index(rd / "spans.jsonl")
            if observed is None:
                # `load_spans` below may observe a file created after this stat. Unknown lets its
                # projector derive the honest count instead of pinning that raced body to zero.
                return None, missing_revision, None, True
            revision, count, conditional_ok = observed.node_window_snapshot(
                nid, span_cap, generation=current_attempt)
            return observed, revision, count, conditional_ok

        def recheck_lifecycle() -> str | None:
            """The same post-read CAS also fences a bodyless conditional hit."""
            after_attempt = _cached_node_attempt(rd, nid)
            after_attempt = after_attempt if after_attempt is not None else 0
            _assert_trace_reset_clear(rd)
            after_generation = srv.commands.run_generation(rd)
            if (after_generation != before_generation
                    or (expected_generation is not None
                        and after_generation != expected_generation)):
                raise HTTPException(409, {
                    "code": "run_generation_changed",
                    "expected_generation": expected_generation or before_generation or None,
                    "current_generation": after_generation or None,
                    "message": "The run was reset or replaced while its conversation was being read.",
                    "remediation": "Reload run state and request the current generation.",
                })
            if after_attempt != current_attempt:
                raise HTTPException(409, {
                    "code": "node_attempt_changed",
                    "node_id": nid,
                    "expected_attempt": current_attempt,
                    "current_attempt": after_attempt,
                    "message": "The node was reset while its conversation evidence was being read.",
                    "remediation": "Reload node state and request the current attempt.",
                })
            return after_generation

        idx = None
        total = None
        source_revision = None
        source_conditional_ok = False
        try:
            idx, source_revision, total, source_conditional_ok = source_snapshot()
        except Exception:  # noqa: BLE001 - the projection below reports the unavailable source
            pass
        candidate_etag = (_conversation_etag(
            run_id, nid, before_generation, current_attempt, span_cap, source_revision,
            trace_run_id=trace_scalars.run_id, task_id=trace_scalars.task_id)
            if source_revision is not None else None)
        if source_conditional_ok and candidate_etag is not None and if_none_match(
                request.headers.get("if-none-match"), candidate_etag):
            # Re-observe both the selected content and lifecycle BEFORE deciding 304. The second
            # source read catches a relevant append/rewrite during the first comparison; the
            # lifecycle read catches even a very short reset that already removed its marker.
            try:
                idx, source_revision, total, source_conditional_ok = source_snapshot()
            except Exception:  # noqa: BLE001 - fall through to the unavailable 200 projection
                idx, source_revision, total, source_conditional_ok = None, None, None, False
            after_generation = recheck_lifecycle()
            trace_scalars = srv.trace_scalars(rd)
            refreshed_etag = (_conversation_etag(
                run_id, nid, after_generation, current_attempt, span_cap, source_revision,
                trace_run_id=trace_scalars.run_id, task_id=trace_scalars.task_id)
                if source_revision is not None else None)
            if source_conditional_ok and refreshed_etag is not None and if_none_match(
                    request.headers.get("if-none-match"), refreshed_etag):
                return Response(status_code=304, headers={
                    "Cache-Control": "no-store",
                    "ETag": refreshed_etag,
                })
            candidate_etag = refreshed_etag
        try:
            # Read only THIS node's traces' spans (by byte offset via the index), not the whole
            # spans.jsonl — a node's conversation on a 1 GB run no longer scans the entire file.
            if source_revision is None:
                idx, source_revision, total, source_conditional_ok = source_snapshot()
                candidate_etag = _conversation_etag(
                    run_id, nid, before_generation, current_attempt, span_cap, source_revision,
                    trace_run_id=trace_scalars.run_id, task_id=trace_scalars.task_id)
            spans = (idx.full_spans_for_node(
                nid, span_cap, generation=current_attempt)
                if idx is not None else load_spans(rd / "spans.jsonl"))
            conversation = build_conversation(
                trace_scalars, spans, nid, total_spans=total, span_cap=span_cap,
                # The index already fenced before its row limit. The fallback receives the whole run,
                # so it must apply the same trace-root generation fence before its own tail cap.
                generation=current_attempt if idx is None else None,
                # Both branches above return normalized projections, never raw durable dictionaries.
                _normalized=True)
        except Exception:  # noqa: BLE001
            conversation = _trace_unavailable(run_id=run_id, node_id=str(nid), stages=[])
        # A reset can win while offsets are read / the conversation is assembled. Re-read the folded
        # lifecycle after the slow work and refuse the whole payload rather than publishing attempt A
        # with attempt B's sidecar bytes. The UI treats this 409 as a stale observation and reloads.
        after_generation = recheck_lifecycle()
        stable_etag = None
        if not (conversation.get("projection") or {}).get("unavailable"):
            try:
                (_after_idx, after_source_revision, _after_total,
                 after_conditional_ok) = source_snapshot()
                after_trace_scalars = srv.trace_scalars(rd)
                after_etag = _conversation_etag(
                    run_id, nid, after_generation, current_attempt, span_cap,
                    after_source_revision, trace_run_id=after_trace_scalars.run_id,
                    task_id=after_trace_scalars.task_id)
                if (after_conditional_ok and candidate_etag is not None
                        and hmac.compare_digest(candidate_etag, after_etag)):
                    stable_etag = candidate_etag
            except Exception:  # noqa: BLE001 - a raced source gets a non-cacheable successful body
                pass
        response.headers["Cache-Control"] = "no-store"
        if stable_etag is not None:
            response.headers["ETag"] = stable_etag
        return {**conversation, "node_id": str(nid), "attempt": current_attempt,
                "run_generation": after_generation or None, "cursor": stable_etag}

    @router.get("/api/runs/{run_id}/log")
    def event_log(run_id: str, since: int = -1):
        """Raw event envelopes (for the activity feed + event/span explorer). `since` = exclusive
        seq lower bound. Returns the oldest-first envelopes past `since`, bounded by BOTH
        `_LEGACY_LOG_MAX_ROWS` rows and `_LEGACY_LOG_MAX_BYTES` of serialized response, so neither many
        rows nor a few very large events can be materialized whole into one response (OOM); an
        incremental caller pages forward by advancing `since`, and `/log-page` is the modern
        streaming-bounded transport. (`iter_event_jsonl` still reads each physical line whole, so a
        single pathological multi-GB event line is not bounded here — the writer bounds event size.)"""
        rd = _run_dir(run_id)
        # Defence-in-depth, mirroring /log-page: `run_dir` confines the directory, but a crafted
        # events.jsonl SYMLINK (e.g. inside an imported run bundle) could still escape the run dir.
        candidate = rd / "events.jsonl"
        if candidate.is_symlink() or rd not in candidate.resolve().parents:
            raise HTTPException(404, "no such run")
        # A row cap is not a memory cap: 100k large envelopes (or a few fat ones) still OOM under it.
        # Stop at whichever limit hits first — row count OR cumulative serialized bytes — so the flat
        # array a legacy caller receives is memory-bounded. iter_event_jsonl preserves batch-envelope
        # expansion; measuring the retained dicts is the response's own serialization cost.
        out: list = []
        budget = _LEGACY_LOG_MAX_BYTES
        for o in iter_event_jsonl(candidate):
            if not (isinstance(o, dict) and o.get("seq", -1) > since):
                continue
            # MEASURE BEFORE RETAINING. Charging the budget after the append made this a soft
            # ceiling: the worst case was budget + one unbounded row, so a single imported/legacy
            # envelope larger than the whole budget was still materialized and returned whole. The
            # current writer's event cap bounds what WE write; it cannot prove an imported log obeys
            # it. An oversize row is replaced by a bounded marker that keeps its `seq`, so a paging
            # caller advancing `since` steps over it instead of stalling on it forever.
            size = len(json.dumps(o, default=str))
            if size > budget:
                if not out:
                    out.append({"seq": o.get("seq"), "type": o.get("type"), "ts": o.get("ts"),
                                "data": {"omitted": "event_too_large", "bytes": size}})
                break
            out.append(o)
            budget -= size
            if len(out) >= _LEGACY_LOG_MAX_ROWS:
                break
        return out

    @router.get("/api/runs/{run_id}/log-page")
    def event_log_page(
            run_id: str,
            direction: str = "tail",
            limit: int = Query(DEFAULT_ROWS, ge=1, le=MAX_ROWS),
            byte_limit: int = Query(DEFAULT_BYTES, ge=MIN_BYTES, le=MAX_BYTES),
            cursor: Optional[str] = None,
            generation: Optional[str] = None,
            anchor_seq: Optional[int] = Query(None, ge=0)):
        """Bounded timeline transport. Cursors survive append and fail closed across run reset."""
        rd = _run_dir(run_id)
        candidate = rd / "events.jsonl"
        if candidate.is_symlink():
            raise HTTPException(404, "no such run")
        log_path = candidate.resolve()
        if rd not in log_path.parents:
            raise HTTPException(404, "no such run")
        return log_pages.page(
            log_path, direction=direction, limit=limit, byte_limit=byte_limit,
            cursor=cursor, generation=generation, anchor_seq=anchor_seq)

    def _assert_artifact_generation(rd: Path, expected: str, *, phase: str) -> str:
        if _RUN_GENERATION_RE.fullmatch(expected) is None:
            raise HTTPException(400, {
                "code": "invalid_run_generation",
                "message": "expected_generation must be the exact generation returned by run state.",
                "remediation": "Reload the run before requesting its files.",
            })
        with srv.commands.sequence(rd):
            rd = srv.commands.validate_paths(rd)
            current = srv.commands.run_generation(rd)
            if current != expected:
                raise HTTPException(409, {
                    "code": "run_generation_changed",
                    "expected_generation": expected,
                    "current_generation": current or None,
                    "message": f"The run was reset or replaced {phase} its files were read.",
                    "remediation": "Reload Files and request only the current run generation.",
                })
        return current

    def _artifact_attempt_conflict(node_id: int, expected_attempt: Optional[int],
                                   current_attempt: Optional[int], message: str) -> None:
        raise HTTPException(409, {
            "code": "node_attempt_changed",
            "node_id": node_id,
            "expected_attempt": expected_attempt,
            "current_attempt": current_attempt,
            "message": message,
            "remediation": "Reload Files and request the current node attempt.",
        })

    @router.get("/api/runs/{run_id}/artifacts")
    def artifacts(run_id: str, expected_generation: str = Query(...)):
        """List files currently visible to the run, grouped by root.

        The run directory and declared RepoTask host paths are live request-time views. They can contain
        inputs, later edits and outputs, so this endpoint explicitly reports workspace visibility rather
        than claiming file-level production provenance it cannot prove. The response is fenced to the
        required run generation; files in a current node workdir carry their node id and attempt.
        """
        rd = _run_dir(run_id)
        generation = _assert_artifact_generation(rd, expected_generation, phase="before")
        before_state = srv.state(rd)
        try:
            exposed = _artifact_exposure_policy(rd)
        except ArtifactPolicyUnavailable:
            # A 503 is deliberate: a failed security proof is unavailable, not a truthful empty list.
            raise HTTPException(503, "artifact inventory unavailable") from None
        out = []
        node_attempts: dict[int, int] = {}

        def _identify_node_file(candidate: Path, file_info: dict) -> Optional[dict]:
            node_id = _artifact_node_id(rd, candidate)
            if node_id is None:
                return file_info
            attempt = _node_attempt(before_state, node_id)
            # A leftover/crash-era node directory without folded ownership has no trustworthy
            # attempt identity. Drop it before it can consume a bounded inventory slot.
            if attempt is None:
                return None
            node_attempts[node_id] = attempt
            return {**file_info, "node_id": node_id, "attempt": attempt}

        for r in _artifact_roots(rd):
            files, truncated = _list_artifact_files(
                r["base"], exposed=exposed, transform=_identify_node_file)
            visibility = "run_workspace" if r["id"] == "run" else "live_task_path"
            out.append({"id": r["id"], "label": r["label"], "path": str(r["base"]),
                        "is_run_dir": r["id"] == "run", "truncated": truncated,
                        "visibility": visibility, "n_files": len(files), "files": files})
        generation = _assert_artifact_generation(rd, expected_generation, phase="while")
        after_state = srv.state(rd)
        for node_id, expected_attempt in node_attempts.items():
            current_attempt = _node_attempt(after_state, node_id)
            if current_attempt != expected_attempt:
                _artifact_attempt_conflict(
                    node_id, expected_attempt, current_attempt,
                    "The node was reset while its file inventory was being prepared.")
        generation = _assert_artifact_generation(rd, expected_generation, phase="while")
        return {
            "run_id": run_id,
            "run_generation": generation,
            "inventory_semantics": "live_workspace_snapshot",
            "roots": out,
        }

    @router.get("/api/runs/{run_id}/artifact")
    def artifact(run_id: str, root: str, path: str,
                 expected_generation: str = Query(...),
                 node_id: Optional[int] = Query(default=None, ge=0),
                 attempt: Optional[int] = Query(default=None, ge=0)):
        """Serve ONE artifact's content for inline viewing. `root` must be one of the ids returned by
        /artifacts; `path` is resolved within that root and traversal-guarded (a browser can never read
        outside the declared roots). Text is returned UTF-8 (errors replaced) capped at 2 MB; binary or
        oversize files return is_text=false / truncated=true with no inline content. The exact inventory
        generation is required; a canonical node-workdir target additionally requires node id + attempt."""
        rd = _run_dir(run_id)
        generation = _assert_artifact_generation(rd, expected_generation, phase="before")
        base = next((r["base"] for r in _artifact_roots(rd) if r["id"] == root), None)
        if base is None:
            raise HTTPException(404, "no such artifact root")
        candidate = base / path
        try:
            target = candidate.resolve(strict=True)
            target_stat = target.stat()
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(404, "no such artifact") from None
        if target != base and base not in target.parents:     # path-traversal guard
            raise HTTPException(404, "no such artifact")
        target_node_id = _artifact_node_id(rd, target)
        target_attempt = None
        if target_node_id is not None:
            if node_id is None or attempt is None:
                raise HTTPException(400, {
                    "code": "node_attempt_required",
                    "node_id": target_node_id,
                    "message": "Node workspace files require their exact node id and attempt.",
                    "remediation": "Reload Files and echo the node_id and attempt from its inventory entry.",
                })
            target_attempt = _node_attempt(srv.state(rd), target_node_id)
            if node_id != target_node_id:
                raise HTTPException(409, {
                    "code": "node_attempt_changed",
                    "node_id": target_node_id,
                    "expected_node_id": node_id,
                    "current_node_id": target_node_id,
                    "expected_attempt": attempt,
                    "current_attempt": target_attempt,
                    "message": "The requested file now belongs to a different node identity.",
                    "remediation": "Reload Files and request the current node and attempt.",
                })
            if target_attempt is None or attempt != target_attempt:
                _artifact_attempt_conflict(
                    target_node_id, attempt, target_attempt,
                    "The node attempt changed before its file was read.")
        try:
            exposed = _artifact_exposure_policy(rd)
        except ArtifactPolicyUnavailable:
            raise HTTPException(503, "artifact access unavailable") from None
        expected_identity = _artifact_file_identity(target_stat)
        if expected_identity is None:
            raise HTTPException(503, "artifact access unavailable")
        if not exposed(candidate, path, target_stat):
            raise HTTPException(404, "no such artifact")
        try:
            with open(target, "rb") as f:
                opened_stat = os.fstat(f.fileno())
                opened_identity = _artifact_file_identity(opened_stat)
                if opened_identity is None:
                    raise HTTPException(503, "artifact access unavailable")
                if opened_identity != expected_identity:
                    raise HTTPException(404, "no such artifact")
                current_target = candidate.resolve(strict=True)
                if current_target != base and base not in current_target.parents:
                    raise HTTPException(404, "no such artifact")
                current_identity = _artifact_file_identity(current_target.stat())
                if current_identity is None:
                    raise HTTPException(503, "artifact access unavailable")
                if current_identity != opened_identity:
                    raise HTTPException(404, "no such artifact")
                # authorize the opened descriptor as well as the path; writable roots can
                # otherwise swap a safe pathname to a protected trace hardlink before open. Refresh
                # protected identities too in case the run-root source was replaced during the race.
                opened_exposed = _artifact_exposure_policy(rd)
                if not opened_exposed(current_target, path, opened_stat):
                    raise HTTPException(404, "no such artifact")
                size = opened_stat.st_size
                head = f.read(_ART_MAX_BYTES + 1)
        except ArtifactPolicyUnavailable:
            raise HTTPException(503, "artifact access unavailable") from None
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(404, "no such artifact") from None
        generation = _assert_artifact_generation(rd, expected_generation, phase="while")
        if target_node_id is not None:
            current_attempt = _node_attempt(srv.state(rd), target_node_id)
            if current_attempt != target_attempt:
                _artifact_attempt_conflict(
                    target_node_id, target_attempt, current_attempt,
                    "The node was reset while its file was being read.")
        generation = _assert_artifact_generation(rd, expected_generation, phase="while")
        body = head[:_ART_MAX_BYTES]
        response_identity = {"run_generation": generation}
        if target_node_id is not None:
            response_identity.update({"node_id": target_node_id, "attempt": target_attempt})
        if b"\x00" in body:                                    # a NUL anywhere in the read → binary
            return {"root": root, "path": path, "size": size, "is_text": False,
                    "truncated": False, "content": None, **response_identity}
        return {"root": root, "path": path, "size": size, "is_text": True,
                "truncated": size > _ART_MAX_BYTES,
                "content": body.decode("utf-8", errors="replace"), **response_identity}

    @router.get("/api/runs/{run_id}/trace")
    def trace(run_id: str,
              expected_generation: Optional[str] = Query(default=None)):
        rd = _run_dir(run_id)
        before_generation = _begin_trace_read(rd, expected_generation)
        try:
            # light=True: strip prompt/output text — the run-level timeline needs only structure +
            # timing + token usage; a heavy run's recorded I/O can be ~50 MB and crash the browser. Served
            # via the light span index + a file-identity cache (`srv.trace_view`): reads ~20 MB of
            # structure, not the whole (up to 1 GB) spans.jsonl, so the first click is ~1 s not ~15 s.
            payload = srv.trace_view(rd)
        except Exception:  # noqa: BLE001 — a malformed/foreign spans.jsonl must degrade, not 500
            payload = _trace_unavailable(
                run_id=run_id, task_id="", nodes={}, rollups={}, unscoped=[], summary={})
        generation = _finish_trace_read(rd, before_generation, expected_generation)
        return _trace_response({**payload, "run_generation": generation or None})

    @router.get("/api/runs/{run_id}/operations")
    def operations(run_id: str, limit: int = Query(default=0, ge=0),
                   expected_generation: Optional[str] = Query(default=None)):
        """Every ROOT operation the run performed — the index the run-level agents were missing.

        A node's Developer work is reachable through `/nodes/{n}/trace`; a RUN-level agent's is not,
        because it has no node to be projected under. The Researcher is the case that matters: its
        `propose` span is a root with no `node_id` (the idea exists before the node does), so it
        cannot appear on any per-node surface, and the one bucket that did hold it — `trace_view`'s
        `unscoped` — is both tail-capped and unread by the UI. Measured on `rubert-dr-0807`: 15
        `propose` operations carrying 493 generations and 944 tool calls, none of them reachable.

        Rows are a LISTING, not a trace: name, node (or null for run-level), timing, status and the
        size of the operation's trace. Open one with `/trace/by_trace/{trace_id}`, which already
        bounds its own I/O. `limit` lowers the row cap; it can never raise it past
        `traceview.OPERATIONS_CAP`, and what it drops is stated in `projection.omitted_operations`.
        """
        rd = _run_dir(run_id)
        _assert_trace_reset_clear(rd)
        before_generation = _begin_trace_read(rd, expected_generation)
        try:
            payload = srv.operations_view(rd, cap=limit)
        except Exception:  # noqa: BLE001 — a malformed/foreign spans.jsonl must degrade, not 500
            payload = _trace_unavailable(operations=[])
        generation = _finish_trace_read(rd, before_generation, expected_generation)
        return {**payload, "run_id": run_id, "run_generation": generation or None}

    @router.get("/api/runs/{run_id}/trace/by_trace/{trace_id}")
    def trace_by_trace(run_id: str, trace_id: str,
                       expected_generation: Optional[str] = Query(default=None)):
        """Spans of ONE operation's trace (by trace_id) as a tree, WITH capped I/O — powers the
        per-event trace expansion: a strategy_decision / hypothesis_merged event carries its own
        operation's trace_id (the engine wraps each op in a named new_trace span and appends the event
        inside, so eventstore stamps it), and the UI shows only THAT trace here, not the node's whole
        Researcher+Developer trace."""
        rd = _run_dir(run_id)
        before_generation = _begin_trace_read(rd, expected_generation)
        from looplab.events.traceview import (
            TRACE_DETAIL_SPAN_CAP, _bounded_tail, _cap_span_io, _normalized_id,
            _projection_counter, _response_projection, _tree, hydrate_inputs, load_spans)
        try:
            # Read only this trace's spans (by byte offset via the index), not the whole spans.jsonl.
            from looplab.events.span_index import get_index
            idx = get_index(rd / "spans.jsonl")
            safe_tid = _normalized_id(trace_id)
            if safe_tid is None:
                raise ValueError("invalid trace id")
            total = idx.trace_span_count(safe_tid) if idx is not None else None
            if idx is not None:
                raw = idx.full_spans_for_trace(safe_tid, TRACE_DETAIL_SPAN_CAP)
            else:
                raw, total = _bounded_tail(
                    (s for s in load_spans(rd / "spans.jsonl")
                     if s.get("trace_id") == safe_tid), TRACE_DETAIL_SPAN_CAP)
            # Reconstruct the retained delta-encoded input before applying browser caps, so the per-op
            # tree does not mistake a delta for a complete diagnostic projection.
            # Both `raw` branches above are already normalized (SpanIndex._read_full / load_spans).
            spans = [_cap_span_io(s) for s in hydrate_inputs(raw, _normalized=True)]
            total = max(len(spans), _projection_counter(total) if total is not None else len(raw))
            projection = _response_projection(
                total_spans=total, visible_spans=len(spans),
                truncated_spans=sum(1 for span in spans
                                    if (span.get("_projection") or {}).get("truncated") is True))
            payload = {"schema": TRACE_PROJECTION_SCHEMA,
                    "spans": _tree(spans, _normalized=True),
                    "count": total, "visible_count": len(spans),
                    "omitted_count": projection["omitted_spans"], "projection": projection}
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 — malformed spans must degrade, not 500
            payload = _trace_unavailable(spans=[])
        generation = _finish_trace_read(rd, before_generation, expected_generation)
        return _trace_response({**payload, "run_generation": generation or None})

    @router.get("/api/runs/{run_id}/prov")
    def prov(run_id: str):
        """W3C-PROV-style provenance of the search DAG: each node's solution is an entity
        generated by an experiment activity (its operator), derived from its parent nodes. Lets
        the lineage be queried as a knowledge-graph ('which change improved metric M the most')."""
        st = srv.state(_run_dir(run_id))
        agent = f"agent:looplab/{st.config_hash or 'run'}"
        ent, act, wgb, used, wdf, waw = {}, {}, {}, {}, [], {}
        for n in st.nodes.values():
            generation = n.attempt
            e, a = f"sol:{n.id}:{generation}", f"exp:{n.id}:{generation}"
            ent[e] = {"prov:label": f"solution node {n.id} · generation {generation}",
                      "ll:node_id": n.id, "ll:generation": generation,
                      "ll:metric": n.robust_metric,
                      "ll:status": n.status, "ll:operator": n.operator, "ll:feasible": n.feasible,
                      "ll:is_best": n.id == st.best_node_id}
            act[a] = {"prov:label": f"{n.operator} experiment · node {n.id} · generation {generation}",
                      "ll:node_id": n.id, "ll:generation": generation,
                      "ll:params": n.idea.params,
                      "ll:rationale": n.idea.rationale}
            wgb[f"wgb:{n.id}:{generation}"] = {"prov:entity": e, "prov:activity": a}
            waw[f"waw:{n.id}:{generation}"] = {"prov:activity": a, "prov:agent": agent}
            for p in n.parent_ids:
                parent_generation = n.parent_generations.get(str(p), 0)
                parent_entity = f"sol:{p}:{parent_generation}"
                # If the parent has since been reset, its historical lifecycle is no longer a full Node
                # in the folded state. Keep an explicit superseded entity so the edge stays resolvable
                # without inventing the replacement generation's metric/status.
                ent.setdefault(parent_entity, {
                    "prov:label": f"solution node {p} · generation {parent_generation}",
                    "ll:node_id": p,
                    "ll:generation": parent_generation,
                    "ll:lifecycle": "historical",
                })
                edge = f"{n.id}:{generation}-{p}:{parent_generation}"
                used[f"used:{edge}"] = {"prov:activity": a, "prov:entity": parent_entity}
                wdf.append({"prov:generatedEntity": e, "prov:usedEntity": parent_entity})
        return {"prefix": {"prov": "http://www.w3.org/ns/prov#", "ll": "urn:looplab:"},
                "entity": ent, "activity": act, "agent": {agent: {"prov:type": "prov:SoftwareAgent"}},
                "wasGeneratedBy": wgb, "used": used,
                "wasAssociatedWith": waw,
                "wasDerivedFrom": {f"wdf:{i}": d for i, d in enumerate(wdf)}}

    @router.get("/api/runs/{run_id}/config", response_model=RunConfigResponse)
    def run_config(run_id: str):
        rd = _run_dir(run_id)
        snap = rd / "config.snapshot.json"
        # A torn write (crash mid-save) or a hand-edited snapshot is UNREADABLE, not an internal
        # fault: it used to raise JSONDecodeError into a bare 500 traceback that named neither the
        # file nor the problem. Every other snapshot reader in this package fails closed with an
        # explicit message; so does this one now.
        try:
            current = (json.loads(snap.read_text(encoding="utf-8"))
                       if snap.exists() else Settings().masked_snapshot())
        # UnicodeDecodeError included: `read_text(encoding="utf-8")` raises it on a snapshot holding
        # invalid UTF-8 (a crash mid-multibyte, a hand-edit saved as latin-1), and it is a ValueError
        # but NOT an OSError or JSONDecodeError — so it used to escape into exactly the bare 500
        # traceback this clause exists to replace.
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(500, f"the run configuration snapshot is unreadable: {exc}") from exc
        if not isinstance(current, dict):
            raise HTTPException(500, "the run configuration snapshot is not a JSON object")
        return _run_config_payload(rd, current)

    def _run_config_payload(rd: Path, snapshot: dict) -> dict:
        """Flat, backward-compatible config plus metadata for immutable run-start semantics.

        A few older UI versions were allowed to rewrite those fields in the snapshot even though the
        engine ignored them on re-entry. Overlay the folded values so both API and form show the policy
        the run actually uses; metadata lets the form render them as read-only without duplicating the
        Python contract in JavaScript.

        The overlay applies exactly where the snapshot CONTRADICTS the run-start record
        (`run_start_pinned_disagreement`), not wherever the two bytes differ: an AUTO sentinel is a
        launch-time request the pin is the resolution OF, so overwriting it here would show — and then
        let the form save back — one run's resolved integer in place of the operator's own spelling.
        """
        pinned = run_start_pinned_settings(srv.state(rd))
        # Keep revision bound to the exact on-disk object, but expose the complete effective Settings
        # projection that resume consumes. Preserve unknown snapshot keys; GET stays read-only and
        # never backfills legacy bytes.
        effective = {**snapshot, **settings_from_snapshot(snapshot).masked_snapshot()}
        mismatches = sorted(
            k for k, value in pinned.items()
            if run_start_pinned_disagreement(k, effective.get(k), value))
        effective.update({k: pinned[k] for k in mismatches})
        effective["_looplab_config_meta"] = {
            "config_revision": _run_config_revision(snapshot),
            "run_start_pinned_fields": sorted(pinned),
            "snapshot_mismatch_fields": mismatches,
            # A profile is expanded into explicit snapshot values at launch. Changing only its name
            # later cannot reapply that bundle truthfully, so the server declares it read-only.
            "run_read_only_fields": ["profile"],
        }
        return effective

    def _repair_trust_gate_event(rd: Path, requested: str) -> bool:
        """Make snapshot/event dual-write retryable without appending duplicate gate events."""
        store = EventStore(rd / "events.jsonl")
        for _attempt in range(4):
            events = store.read_all()
            if fold(events).trust_gate == requested:
                return False
            expected = events[-1].seq if events else -1
            try:
                store.append(
                    EV_TRUST_GATE_CHANGED,
                    {"trust_gate": requested, "source": "config_edit"},
                    expected_last_seq=expected,
                    require_lock=True,
                )
                return True
            except EventStoreConcurrencyError:
                # Another writer advanced the log. Refold under a fresh CAS: it may already have
                # applied this exact gate, in which case the retry becomes a no-op.
                continue
        raise HTTPException(409, "the run changed while trust_gate was being saved; retry the edit")

    def _put_run_config_locked(
            rd: Path, snap: Path, incoming: dict, expected_revision: Optional[str],
            expected_generation: str) -> dict:
        """Read/compare/merge/validate/write while the caller holds both config locks."""
        from pydantic import ValidationError
        from looplab.serve.settings_store import _ALLOWED_FIELDS, _SECRET_FIELDS

        if not snap.exists():
            raise HTTPException(
                404, "run has no config.snapshot.json (it predates self-describing runs)")
        try:                                     # same unreadable-vs-fault distinction as the GET
            current = json.loads(snap.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(500, f"the run configuration snapshot is unreadable: {exc}") from exc
        if not isinstance(current, dict):
            raise HTTPException(500, "the run configuration snapshot is not a JSON object")
        current_revision = _run_config_revision(current)
        if expected_revision is not None and expected_revision != current_revision:
            raise HTTPException(409, {
                "code": "run_config_revision_conflict",
                "resource": "run_config",
                "message": "Run configuration changed after this form was loaded; reload and retry.",
                "expected_revision": expected_revision,
                "current_revision": current_revision,
            })

        allowed, secret = _ALLOWED_FIELDS, _SECRET_FIELDS
        folded = srv.state(rd)
        pinned = run_start_pinned_settings(folded)
        attempted_pinned_changes = sorted(
            key for key, value in incoming.items()
            if key in pinned and run_start_pinned_disagreement(key, value, pinned[key])
        )
        if attempted_pinned_changes:
            raise HTTPException(
                422,
                "run-start pinned settings cannot be changed after creation: "
                + ", ".join(attempted_pinned_changes)
                + ". Start a new run to use different holdout/verifier semantics.",
            )

        updated = dict(current)
        # Repair snapshots written by older servers. These values are not a new policy decision: the
        # RUN-START RECORD has always been the authority used by replay/re-entry, and GET already
        # overlays it.
        #
        # "the event fold" was the wording here until 2026-08-06, and it stopped being true the day
        # the AUTO depth was allowed to ratchet itself down: `st.speculation_depth` became the run's
        # EFFECTIVE treatment rather than run_started's value, and this repair — which needs no
        # operator intent at all, an unrelated `timeout` edit reaches it — wrote that settled value
        # into `config.snapshot.json` as though it were the pin. Measured through this route:
        # run_started pinned 4, the snapshot held -1 (AUTO), GET reported 0, a PUT of `timeout` wrote
        # 0, a PUT restoring -1 came back 422, and the next re-entry refused the run outright.
        # `run_start_pinned_settings` reads the PIN now, and the repair fires only on a genuine
        # disagreement, so the AUTO sentinel survives an unrelated save.
        normalized_pinned = sorted(
            k for k, value in pinned.items()
            if run_start_pinned_disagreement(k, updated.get(k), value))
        updated.update({k: pinned[k] for k in normalized_pinned})
        # A hand-authored/legacy sparse snapshot may omit the mutable trust gate. Preserve the folded
        # policy in that case rather than manufacturing an "audit" downgrade during an unrelated edit.
        updated.setdefault("trust_gate", folded.trust_gate)
        # A profile switch would silently no-op on resume because every old profile field is already
        # explicit. Treat every unequal value (including null) as a read-only-field violation.
        if "profile" in incoming and incoming["profile"] != updated.get("profile"):
            raise HTTPException(422, "profile can't be changed per-run after launch — the snapshot "
                                     "already contains the expanded profile; set the individual "
                                     "fields (trust_gate, confirm_top_k, …) instead")

        changed = {}
        for key, value in incoming.items():
            if (key in allowed and key not in secret and key not in RUN_START_PINNED_FIELDS
                    and updated.get(key) != value):
                updated[key] = value
                changed[key] = value
        # Validate the merged config before persistence. Optional fields may deliberately be cleared
        # with null; required fields remain protected by Settings' schema.
        try:
            settings_from_snapshot({
                key: value for key, value in updated.items()
                if key in allowed and key not in secret
            })
        except ValidationError as exc:
            fields = "; ".join(
                f"{'.'.join(str(x) for x in error['loc']) or '?'}: {error['msg']}"
                for error in exc.errors())
            raise HTTPException(422, f"invalid settings — {fields}") from exc
        except Exception as exc:  # noqa: BLE001 - normalize any other coercion failure
            raise HTTPException(422, f"invalid settings: {exc}") from exc

        # This is the last authority check before the first durable mutation. Reset owns this same
        # config lock, so the named generation cannot be replaced between this check and the write.
        current_generation = srv.commands.run_generation(rd)
        if current_generation != expected_generation:
            raise HTTPException(409, {
                "code": "run_generation_changed",
                "expected_generation": expected_generation,
                "current_generation": current_generation or None,
                "message": "The run was reset while these settings were being edited.",
                "remediation": "Reload the run's configuration and re-apply your changes.",
            })
        atomic_write_text(snap, json.dumps(updated, indent=2))
        # trust_gate is enforced by the fold, so repair its event while this config transaction is
        # still serialized. A legacy request can retry an ambiguous dual-write failure safely.
        try:
            gate_repaired = _repair_trust_gate_event(rd, updated["trust_gate"])
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                500, f"snapshot updated but trust_gate event append failed: {exc}") from exc
        return {
            "ok": True,
            "config": _run_config_payload(rd, updated),
            "changed": sorted(changed),
            "normalized_pinned": normalized_pinned,
            "trust_gate_event_appended": gate_repaired,
            "engine_running": _engine_liveness(rd),
        }

    @router.put(
        "/api/runs/{run_id}/config",
        response_model=RunConfigUpdateResponse,
        openapi_extra=request_body_contract(
            RunConfigUpdateRequest, LegacyRunConfigUpdateRequest),
    )
    async def put_run_config(run_id: str, request: Request):
        """Per-run settings edit: rewrite THIS run's config.snapshot.json so a later RESUME re-enters
        the loop with the new values. Resume reads it via `settings_from_snapshot` (cli.py), preserving
        historical defaults for fields absent from an older snapshot. It
        does NOT read the UI's global new-run defaults — so editing the snapshot is the only way to
        change a specific run's settings (e.g. raise `timeout`, enable timeout repair) before continuing it.

        Writing the snapshot is SAFE even while the engine is live: the only other writer is `run` at
        startup (under the singleton lock, before the loop), and a running engine never re-reads the
        snapshot — so a concurrent PUT can't corrupt or race it. A live engine just keeps its in-memory
        settings until it's stopped & resumed; we return `engine_running` so the UI can say "applies on
        the next restart" (and offer a pause→resume to apply now). Only known, non-secret fields are
        applied (masked llm_api_key + any unknown keys preserved); the merged config is validated through
        `Settings()` so a bad value (e.g. n_seeds<1, timeout<=0, bad enum) is rejected 422 — with the
        offending field surfaced — instead of poisoning the next resume."""
        rd = _run_dir(run_id)
        snap = rd / "config.snapshot.json"
        if not snap.exists():
            raise HTTPException(404, "run has no config.snapshot.json (it predates self-describing runs)")
        body = await json_object(request)
        has_expected_revision = "expected_revision" in body
        expected_revision = body.get("expected_revision")
        # An explicit null is treated as absent (no CAS), matching the now-nullable request schema; only
        # a present, non-null value must be the 64-char revision.
        if has_expected_revision and expected_revision is not None and (
                not isinstance(expected_revision, str)
                or _SHA256_RE.fullmatch(expected_revision) is None):
            raise HTTPException(400, "expected_revision must be the 64-character config revision")
        # `expected_revision` fences the snapshot BYTES, which is not the same as fencing the RUN.
        # `config.snapshot.json` is deliberately absent from reset's archive list (control.py), so it
        # survives a reset byte-identical — and a revision observed against generation A therefore
        # still matches after the run has been reset into generation B, letting a delayed PUT rewrite
        # the replacement run's settings. `expected_generation` is the fence that actually names the
        # run, so every write must name the exact generation displayed with the edited form.
        expected_generation = body.get(EXPECTED_RUN_GENERATION_FIELD)
        if (not isinstance(expected_generation, str)
                or _RUN_GENERATION_RE.fullmatch(expected_generation) is None):
            raise HTTPException(400, {
                "code": "invalid_run_generation",
                "message": "expected_generation must be the exact generation from the run state.",
                "remediation": "Reload the run before saving its configuration.",
            })
        incoming = body.get("settings", body)
        if not isinstance(incoming, dict):
            raise HTTPException(400, "settings must be a JSON object")
        try:
            # The required OS lock is the cross-process guarantee; the bounded stripe supplies the
            # same guarantee to multiple threads in this process on every supported platform.
            # OFF the event loop. The striped `threading.Lock` (unrelated runs can share a stripe),
            # the required OS interprocess lock, and `_put_run_config_locked`'s `srv.state(rd)` —
            # a full event-log read + fold — are all blocking. Run inline on this `async def`
            # handler's loop, a contended stripe or a large run froze every other client, SSE
            # included, for the whole duration. Every exception below still propagates out of the
            # worker to the same handlers; only the thread the section runs on changes.
            def _write() -> dict:
                with (_run_config_thread_lock(snap),
                      _interprocess_lock(Path(str(snap) + ".lock"), required=True)):
                    assert_run_reset_write_allowed(rd)
                    assert_run_deletion_write_allowed(rd)
                    return _put_run_config_locked(
                        rd, snap, incoming, expected_revision, expected_generation)

            return await anyio.to_thread.run_sync(_write)
        except EventStoreLockError as exc:
            raise HTTPException(503, {
                "code": "run_config_lock_unavailable",
                "message": "Run configuration locking is unavailable; no settings were written.",
            }) from exc
        except RunResetFenceError as exc:
            raise HTTPException(409, {
                "code": "run_reset_in_progress",
                "operation_id": exc.operation_id,
                "message": "Replay is replacing this run; settings were not written.",
                "remediation": "Observe or retry that exact Replay operation first.",
            }) from exc
        except RunResetStorageError as exc:
            raise HTTPException(503, {
                "code": "run_reset_fence_unavailable",
                "message": "Replay ownership cannot be verified; settings were not written.",
            }) from exc
        except RunDeletionFenceError as exc:
            raise HTTPException(410, {
                "code": "run_deletion_in_progress",
                "operation_id": exc.operation_id,
                "message": "This run is being deleted; settings were not written.",
                "remediation": "Observe or retry that exact deletion operation first.",
            }) from exc
        except RunDeletionStorageError as exc:
            raise HTTPException(503, {
                "code": "run_deletion_fence_unavailable",
                "message": "Deletion ownership cannot be verified; settings were not written.",
            }) from exc
    @router.get("/api/runs/{run_id}/cost")
    def run_cost(run_id: str):
        st = srv.state(_run_dir(run_id))
        if st.llm_cost is None:
            # No roll-up exists yet (offline/toy run, or one that has not finalized). The historical
            # body was this zero-filled dict alone, which a client cannot tell from a finished run
            # that genuinely spent nothing — the same "absent is not zero" confusion `priced_calls`
            # fixes one level down. `recorded` says which it is; the zeros stay, so any existing
            # arithmetic client is byte-for-byte unaffected.
            return {"cost": 0.0, "calls": 0, "priced_calls": 0, "total_tokens": 0,
                    "recorded": False}
        return {**st.llm_cost, "recorded": True}

    @router.get("/api/runs/{run_id}/agents_md", deprecated=True)
    def agents_md(run_id: str):
        """DEPRECATED. Serve a run's AGENTS.md.

        No first-party client reads this — neither `ui/src` nor the TUI mentions it. Marked
        deprecated rather than deleted because it is a PUBLIC route whose outside callers this
        repository cannot see (doc 25 SR-13)."""
        rd = _run_dir(run_id)
        f = rd / "AGENTS.md"
        # Same defence-in-depth the /log and /log-page routes above apply to events.jsonl: `run_dir`
        # confines the DIRECTORY, but `exists()`/`read_text()` follow symlinks, so a crafted AGENTS.md
        # symlink (an imported run bundle, or the sandbox writing into the run dir) would otherwise turn
        # this route into an arbitrary host-file read. Refuse a symlink and anything resolving outside
        # the run dir, and bound the body — an unbounded read of an attacker-sized file is an OOM.
        if f.is_symlink() or not f.exists():
            return PlainTextResponse("")
        if rd.resolve() not in f.resolve().parents:
            return PlainTextResponse("")
        with open(f, "rb") as fh:
            head = fh.read(_ART_MAX_BYTES + 1)
        body = head[:_ART_MAX_BYTES].decode("utf-8", errors="replace")
        if len(head) > _ART_MAX_BYTES:
            body += f"\n\n[truncated at {_ART_MAX_BYTES} bytes]"
        return PlainTextResponse(body)

    return router
