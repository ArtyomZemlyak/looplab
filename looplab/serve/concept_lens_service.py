"""The paid CONCEPT-LENS subsystem: identities, the two ledger folds, the worker and four commands.

Doc 25 SR-04 called this out as "a subsystem inside a router file": 21 helpers, a `PaidLedgerSpec`,
a second stricter recovery fold, a provider worker and four endpoints — about a third of
`routers/runs.py`, in a file named for the run READ MODEL and conceptually independent of it. The
preamble half was answered in 2026-08-02 (`assert_lens_generation`) and the pure projection half in
2026-08-17 (`serve/concept_frame.py`); this module is the rest, so `routers/runs.py` keeps the four
route decorators with the docstrings that ARE their OpenAPI descriptions, and nothing else.

What that buys is the same thing SR-02 and SR-03 bought: the crash-recovery states become
constructible. A claim whose worker died between the `concept_lens_started` append and its terminal,
a terminal that is visible but not yet confirmed durable, two overlapping claims on one generation,
a receipt whose digest disagrees with its claim — every one of those was previously reachable only
by building the ASGI app, seeding a run and racing HTTP. `tests/test_concept_lens_service.py` drives
them against a stub `srv` with no app at all.

Two seams are threaded in rather than moved, and each for a stated reason:

* ``materialize_core`` — the bounded ConceptFrame fold plus its two process-wide caches. The unpaid
  `GET /concepts` is its other consumer, so it is genuinely read-model machinery and stays in
  `routers/runs.py`; every command here takes it as a keyword, exactly as `trace_clear.py` takes
  `known_engine_liveness`.
* ``srv`` — threaded explicitly where the closures captured it, and `srv.run_dir(run_id)` in place
  of the captured `_run_dir`.

**Refusals are not translated at the boundary.** These functions raise `HTTPException` from inside
the ledger case analysis, exactly as the route bodies did, and for the reason `scope_actions.py` and
`trace_clear.py` both recorded: which failures are terminal and which are retryable is decided by
the ORDER of the handlers around these raises, and a new exception type re-entering that ladder is a
behaviour change dressed as a refactor. The line this module does not cross is serving: it builds no
router object and carries no route decorator, and a guard asserts both.

The three identity-shape constants at the top (`_RUN_GENERATION_RE`, `_SHA256_RE`,
`_MAX_SAFE_INTEGER`) are defined HERE and imported by `routers/runs.py`, which also reads them for
its historical-detail and trace fences. One definition, and the direction is router to service.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path
from typing import Optional

import anyio
from fastapi import HTTPException, Request, Response

from looplab.events.eventstore import EventStore
from looplab.events.types import (
    EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED, EV_CONCEPT_LENS_STARTED,
)
from looplab.serve.assistant import safe_provider_failure
from looplab.serve.concept_frame import (
    MAX_LENS_BODY_BYTES, MAX_LENS_PROMPT_BYTES, MAX_LENS_PROMPT_CHARS, TRUNCATION_CAP_REASONS,
    bounded_lens_label, core_lens_inputs as concept_core_lens_inputs,
    lens_request as concept_lens_request, normalized_custom_lens_name,
    project_frame as project_concept_frame)
from looplab.serve.http import generation_conflict, json_object_bytes
from looplab.serve.paid_ledger import (
    FAIL_CLOSED, PaidLedgerSpec, append_claim, confirm_terminal_receipt, fold_paid_ledger,
    record_terminal)
from looplab.serve.paid_work import (
    RunCostAccountingPending, metered_run_client, run_directory_identity)
from looplab.serve.protocol import RUN_GENERATION_FIELD

# Shared with `routers/runs.py`, which imports them from here: a 64-hex run generation, a 64-hex
# request identity (the two shapes are equal today and are deliberately NOT one constant — they are
# different facts), and the largest integer a browser can round-trip through JSON.
_RUN_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_CONCEPT_LENS_KEY_RE = re.compile(r"^[\x21-\x7e]{16,512}$")
_CONCEPT_LENS_RECOVERY_SCHEMA = 1
_CONCEPT_LENS_SAFE_ERROR_KINDS = frozenset({
    "accounting_pending", "credentials", "rate_limit", "unavailable", "provider_error",
    "capacity", "internal",
})


def lens_idempotency_key(raw: str) -> str:
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


def lens_identity(run_dir: Path, generation: str, idempotency_key: str) -> str:
    return hashlib.sha256(
        ("concept_lens\0" + run_directory_identity(run_dir) + "\0" + generation
         + "\0" + idempotency_key).encode("utf-8")
    ).hexdigest()


def lens_prompt_digest(idempotency_key: str, prompt: str) -> str:
    """Bind prompt equality to the unlogged high-entropy request key.

    A plain prompt hash lets anyone who can read the diagnostic log dictionary-test common prompts.
    HMAC preserves restart-safe equality without turning the event log into a prompt oracle.
    """
    return hmac.new(
        idempotency_key.encode("ascii"), prompt.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def lens_resolution_key(raw: str) -> str:
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


def lens_resolution_identity(run_dir: Path, generation: str, request_id: str,
                             resolution_key: str) -> str:
    """Domain-separated, non-reversible identity for one recovery resolution command."""
    return hashlib.sha256(
        ("concept_lens_resolution\0" + run_directory_identity(run_dir) + "\0" + generation
         + "\0" + request_id + "\0" + resolution_key).encode("utf-8")
    ).hexdigest()


async def lens_json_body(request: Request) -> dict:
    """Read either paid-lens command without permitting an unbounded request body."""
    raw_body = bytearray()
    async for chunk in request.stream():
        if len(raw_body) + len(chunk) > MAX_LENS_BODY_BYTES:
            raise HTTPException(413, {
                "code": "concept_lens_body_too_large",
                "max_bytes": MAX_LENS_BODY_BYTES,
            })
        raw_body.extend(chunk)
    # The UTF-8 decode stays HERE, beside the byte cap: both are this route's own reading policy,
    # and `json.loads` would otherwise accept BOM-marked UTF-16/32 that this endpoint never did.
    try:
        text = bytes(raw_body).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "request body must be valid JSON") from exc
    return json_object_bytes(text)


def assert_lens_generation(srv, rd: Path, *, core_generation: Optional[str],
                            expected_generation: str, stale_message: str,
                            stale_remediation: str, prepared_message: str) -> tuple[Path, str]:
    """The paid-concept-lens generation fence — INSIDE the run sequencer for the two POSTs
    (resolve-recovered, abandon), and as a CAS across the read for the recovery GET, which takes
    no lock since 2026-09-06 (doc 52 row 5).

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
        raise generation_conflict(stale_message, expected=expected_generation,
                                  current=current_generation or None,
                                  remediation=stale_remediation)
    if core_generation != current_generation:
        raise generation_conflict(prepared_message, expected=expected_generation,
                                  current=current_generation)
    return rd, current_generation


# The shared claim→terminal event ledger (doc 25 SR-01, `serve/paid_ledger.py`), which owns the fold,
# the fsync-confirm and the sequenced terminal append this route used to hand-roll beside its
# report-refresh twin. FAIL_CLOSED and the bound `request_digest` are this protocol's half of the
# deliberate asymmetry the shared module documents: EVERY paid-lens terminal is written through the
# ledger, so an unclaimed terminal — or one whose digest disagrees with its claim's — is evidence of
# a damaged or forged receipt rather than of paid work, and publishing it would show the operator a
# lens they never asked for. The strict recovery fold below keeps its own separate strategy.
CONCEPT_LENS_LEDGER = PaidLedgerSpec(
    claim_type=EV_CONCEPT_LENS_STARTED,
    terminal_types=frozenset({EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED}),
    identity_field="lens_request_id",
    digest_field="request_digest",
    conflict_policy=FAIL_CLOSED,
)


def lens_recovery_ledger(events, generation: str):
    """Strictly fold the current generation into a bounded lost-receipt recovery view.

    The ordinary paid endpoint keeps its legacy-compatible fold — the shared `CONCEPT_LENS_LEDGER`
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


def validated_derived_lens(spec, lens_pack: list[dict], inputs: dict):
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
    name = normalized_custom_lens_name(spec.get("name"))
    shipped_names = {item.get("name") for item in lens_pack if isinstance(item, dict)}
    if not name or name in shipped_names:
        name = normalized_custom_lens_name(
            "derived-" + (name or "-".join(relations) or "lens"))
    if not name:
        return None
    try:
        canonical_name, validated_spec, registration = concept_lens_request(
            name, ",".join(relations), lens_pack)
    except HTTPException:
        return None
    validated_spec.update({
        "label": bounded_lens_label(spec.get("label"), canonical_name),
        "provenance": "agent",
    })
    return canonical_name, validated_spec, registration


def lens_spec_matches_terminal(raw_spec, canonical_spec: dict) -> bool:
    """Strictly match a terminal, allowing only the retired legacy string-root field."""
    if not isinstance(raw_spec, dict):
        return False
    if "root" in raw_spec and not isinstance(raw_spec.get("root"), str):
        return False
    return {key: value for key, value in raw_spec.items() if key != "root"} == canonical_spec


# ------------------------------------------------- the bounded responses and the terminal writers
def lens_uncertain(base_frame: dict, generation: str, identity: str, message: str) -> dict:
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


def lens_terminal_response(event, core: dict, lens_pack: list[dict], identity: str) -> dict:
    generation = core[RUN_GENERATION_FIELD]
    base_frame = project_concept_frame(
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
        inputs = concept_core_lens_inputs(core)
        prepared = validated_derived_lens(data.get("spec"), lens_pack, inputs)
        if prepared is not None:
            canonical_name, validated_spec, registration = prepared
            if lens_spec_matches_terminal(data.get("spec"), validated_spec):
                frame = project_concept_frame(
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
        **lens_uncertain(
            base_frame, generation, identity,
            "The saved lens receipt is malformed. Resume only with this same request identity."),
        "seq": event.seq,
    }


def record_lens_terminal(srv, run_dir: Path, generation: str, identity: str,
                         request_digest: str, event_type: str, terminal_fields: dict):
    """Append one terminal iff the exact claim is still unresolved.

    Provider work and explicit operator abandonment can finish in different processes.  The run
    command sequencer is therefore the only terminal commit point: a late worker observes and
    replays the winner instead of appending a conflicting second receipt.
    """
    return record_terminal(
        srv, CONCEPT_LENS_LEDGER, run_dir, generation, identity, event_type,
        terminal_fields, request_digest=request_digest)


def record_lens_failure(srv, run_dir: Path, generation: str, identity: str,
                        request_digest: str, error_kind: str):
    safe_kind = (error_kind if error_kind in _CONCEPT_LENS_SAFE_ERROR_KINDS
                 else "provider_error")
    return record_lens_terminal(
        srv, run_dir, generation, identity, request_digest, EV_CONCEPT_LENS_FAILED,
        {"error_kind": safe_kind})


def run_lens_worker(srv, settings, run_dir: Path, generation: str, identity: str,
                    request_digest: str, prompt: str, core: dict,
                    lens_pack: list[dict]) -> dict:
    from looplab.search.concept_lens import derive_lens

    base_frame = project_concept_frame(
        core, requested_lens="is_a", lens_pack=lens_pack)
    inputs = concept_core_lens_inputs(core)
    provider_started = False
    try:
        with metered_run_client(srv, settings, run_dir, generation) as client:
            provider_started = True
            spec = derive_lens(
                prompt, inputs["edges"], client, concepts=inputs["concept_ids"],
                parser="tool_call_once", raise_on_failure=True)
            prepared = validated_derived_lens(spec, lens_pack, inputs) if spec else None
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
            event = record_lens_terminal(
                srv, run_dir, generation, identity, request_digest,
                EV_CONCEPT_LENS_COMPLETED,
                {key: value for key, value in terminal_data.items()
                 if key not in {"lens_request_id", "generation", "request_digest"}},
            )
            if event is None:
                return lens_uncertain(
                    base_frame, generation, identity,
                    "The paid lens finished, but its generation-fenced durable terminal could "
                    "not be confirmed. Resume only with this same request identity.")
            return lens_terminal_response(
                event, core, lens_pack, identity)
    except Exception as exc:  # noqa: BLE001 - provider payloads never cross this boundary
        if provider_started:
            return lens_uncertain(
                base_frame, generation, identity,
                "The paid lens attempt may have reached the provider, but its durable receipt "
                "is unavailable. Resume only with this same request identity.")
        if isinstance(exc, RunCostAccountingPending):
            error_kind = "accounting_pending"
        else:
            error_kind = str(safe_provider_failure(exc).get("error_kind") or "provider_error")
        failure_event = record_lens_failure(
            srv, run_dir, generation, identity, request_digest, error_kind)
        if failure_event is not None:
            return lens_terminal_response(
                failure_event, core, lens_pack, identity)
        return lens_uncertain(
            base_frame, generation, identity,
            "The lens request failed before provider dispatch, but its terminal receipt could "
            "not be confirmed. Resume only with this same request identity.")

    raise AssertionError("unreachable paid-lens worker path")


# ------------------------------------------------------------------ the four operator commands
async def durable_derive_concept_lens(srv, run_id: str, request: Request, response: Response,
                                      *, materialize_core) -> dict:
    """Claim, dispatch and settle ONE paid derived lens. The route keeps the docstring the OpenAPI
    description is generated from; this is the body.

    ``materialize_core`` is the router's own ConceptFrame materializer — the bounded fold plus its
    two process-wide caches, which the unpaid `/concepts` GET is the other consumer of. It stays a
    read-model concern of `routers/runs.py` and is threaded in rather than duplicated here.
    """
    from looplab.search.concept_lens import default_lenses

    rd = srv.run_dir(run_id)
    body = await lens_json_body(request)
    prompt_value = body.get("prompt")
    prompt = prompt_value.strip() if isinstance(prompt_value, str) else ""
    if not prompt:
        raise HTTPException(400, "prompt is required")
    if (len(prompt) > MAX_LENS_PROMPT_CHARS
            or len(prompt.encode("utf-8")) > MAX_LENS_PROMPT_BYTES):
        raise HTTPException(413, {
            "code": "concept_lens_prompt_too_large",
            "max_chars": MAX_LENS_PROMPT_CHARS,
            "max_bytes": MAX_LENS_PROMPT_BYTES,
        })
    expected_generation = body.get("expected_generation")
    if (not isinstance(expected_generation, str)
            or _RUN_GENERATION_RE.fullmatch(expected_generation) is None):
        raise HTTPException(400, {
            "code": "invalid_run_generation",
            "message": "expected_generation must be the exact generation from the Concepts response.",
            "remediation": "Refresh the run before creating another paid concept lens.",
        })

    raw_idempotency_key = lens_idempotency_key(
        request.headers.get("Idempotency-Key", ""))

    lens_pack = default_lenses()
    core = materialize_core(rd, run_id, None, lens_pack)
    generation = core[RUN_GENERATION_FIELD]
    if not generation:
        raise HTTPException(409, {
            "code": "run_generation_unavailable",
            "message": "The run has no durable generation identity.",
        })
    if generation != expected_generation:
        raise generation_conflict(
            "The run changed before paid lens creation began.",
            expected=expected_generation, current=generation,
            remediation="Reload the Concepts view and submit a new request intentionally.")
    base_frame = project_concept_frame(
        core, requested_lens="is_a", lens_pack=lens_pack)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "X-LoopLab-Token, Authorization, Idempotency-Key"
    request_digest = lens_prompt_digest(raw_idempotency_key, prompt)
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
                raise generation_conflict(
                    "The run changed before paid lens creation began.",
                    expected=expected_generation, current=current_generation,
                    remediation="Reload the Concepts view and submit a new request intentionally.")
            if core[RUN_GENERATION_FIELD] != current_generation:
                raise generation_conflict(
                    "The run changed while the concept frame was being prepared.",
                    expected=expected_generation, current=current_generation,
                    remediation="Reload the Concepts view and submit a new request intentionally.")

            job_identity = lens_identity(
                rd, current_generation, raw_idempotency_key)
            store = EventStore(rd / "events.jsonl")
            ledger = fold_paid_ledger(
                CONCEPT_LENS_LEDGER, store.read_all(), current_generation)
            claims, terminals = ledger.claims, ledger.terminals
            unresolved, conflicts = ledger.unresolved, ledger.conflicts
            if job_identity in conflicts:
                return lens_uncertain(
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
                        **lens_uncertain(
                            base_frame, current_generation, job_identity,
                            "The saved lens terminal is visible but its durable receipt is "
                            "unconfirmed. Resume only with this same request identity."),
                        # The terminal remains non-authoritative (ok=False, ambiguous=True), but
                        # its bounded event position is already visible in the exact ledger fold.
                        # Preserve that receipt coordinate just as malformed-terminal replay does;
                        # omitting it made the response shape depend on transient fsync contention.
                        "seq": terminal.seq,
                    }
                return lens_terminal_response(
                    terminal, core, lens_pack, job_identity)
            if unresolved:
                if unresolved != {job_identity}:
                    if job_identity in unresolved:
                        return lens_uncertain(
                            base_frame, current_generation, job_identity,
                            "Multiple paid-lens claims overlap this run generation and require repair.")
                    raise HTTPException(409, {
                        "code": "concept_lens_in_progress",
                        "message": "Another concept lens already owns this run generation.",
                        "remediation": "Wait for its receipt or reload before trying again.",
                    })
                reservation = srv.jobs.rejoin(job_identity)
                if reservation is None:
                    return lens_uncertain(
                        base_frame, current_generation, job_identity,
                        "The earlier paid lens attempt has no live process receipt.")
                compute = lambda: run_lens_worker(  # noqa: E731
                    srv, srv.llm_settings(rd), rd, current_generation, job_identity,
                    request_digest, prompt, core, lens_pack)
            else:
                # A bounded partial frame is the exact safe substrate already rendered by GET.  Only
                # corruption-adjacent reasons block paid work; cap limitations remain in the eventual
                # derived response so the operator sees precisely what the model received.
                blocking = [reason for reason in base_frame["completeness"]["reasons"]
                            if reason not in TRUNCATION_CAP_REASONS]
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
                compute = lambda: run_lens_worker(  # noqa: E731
                    srv, settings, rd, current_generation, job_identity,
                    request_digest, prompt, core, lens_pack)
                try:
                    append_claim(
                        store, CONCEPT_LENS_LEDGER, job_identity, current_generation,
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
        return lens_uncertain(
            base_frame, expected_generation, job_identity,
            "The lens worker ended without a durable terminal receipt. Resume only with this "
            "same request identity.")
    return result

def durable_recover_concept_lens_receipt(srv, run_id: str, response: Response,
                                         expected_generation: str, *, materialize_core) -> dict:
    """The lost-receipt recovery projection. Observational only, and it takes NO lock — the fence is
    a CAS across the read (see the comment below). The route keeps the client-visible docstring."""
    from looplab.search.concept_lens import default_lenses

    if _RUN_GENERATION_RE.fullmatch(expected_generation) is None:
        raise HTTPException(400, {
            "code": "invalid_run_generation",
            "message": "expected_generation must be the exact generation from Concepts.",
        })
    rd = srv.run_dir(run_id)
    lens_pack = default_lenses()
    core = materialize_core(rd, run_id, None, lens_pack)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "X-LoopLab-Token, Authorization"

    # NO COMMAND SEQUENCER (2026-09-06, doc 52 row 5): a GET that took the exclusive
    # cross-process lock was refused whenever a writer held the run. The fence is a CAS
    # ACROSS the read instead — taken here, and again after the ledger is folded.
    rd, current_generation = assert_lens_generation(
        srv, rd, core_generation=core[RUN_GENERATION_FIELD],
        expected_generation=expected_generation,
        stale_message="The run changed before paid-lens recovery was inspected.",
        stale_remediation="Reload Concepts and inspect only the current generation.",
        prepared_message="The run changed while its recovery projection was prepared.")

    store = EventStore(rd / "events.jsonl")
    claims, terminals, unresolved, conflict = lens_recovery_ledger(
        store.read_all(), current_generation)
    _rd_after, generation_after = srv.commands.generation_fence(rd)
    if generation_after != current_generation:
        raise generation_conflict(
            "The run changed while its recovery projection was read.",
            expected=expected_generation, current=generation_after or None,
            remediation="Reload Concepts and inspect only the current generation.")
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
            "terminal": lens_terminal_response(
                terminal, core, lens_pack, request_id),
        }
    return {**common, "state": "none"}

async def durable_abandon_recovered_concept_lens(
        srv, run_id: str, request: Request, response: Response, *, materialize_core) -> dict:
    """Resolve one exactly identified orphan without possessing or replaying its paid key."""
    from looplab.search.concept_lens import default_lenses

    rd = srv.run_dir(run_id)
    body = await lens_json_body(request)
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
    resolution_key = lens_resolution_key(
        request.headers.get("Resolution-Idempotency-Key", ""))

    lens_pack = default_lenses()
    core = materialize_core(rd, run_id, None, lens_pack)
    generation = core[RUN_GENERATION_FIELD]
    base_frame = project_concept_frame(
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
            rd, current_generation = assert_lens_generation(
                srv, rd, core_generation=generation,
                expected_generation=expected_generation,
                stale_message="The run changed before the recovered claim could be resolved.",
                stale_remediation=(
                    "Reload recovery; never resolve a claim from another generation."),
                prepared_message="The run changed while its recovery frame was prepared.")

            store = EventStore(rd / "events.jsonl")
            claims, terminals, unresolved, conflict = lens_recovery_ledger(
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
                    return lens_uncertain(
                        base_frame, current_generation, request_id,
                        "The recovered terminal is visible but its durability is unconfirmed.")
                return lens_terminal_response(
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

            resolution_id = lens_resolution_identity(
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
                return lens_uncertain(
                    base_frame, current_generation, request_id,
                    "The recovery resolution could not be confirmed durable; no provider retry "
                    "was sent. Inspect recovery again before retrying this resolution.")
            return lens_terminal_response(
                terminal, core, lens_pack, request_id)

    return await anyio.to_thread.run_sync(_resolve)

async def durable_abandon_concept_lens(srv, run_id: str, request: Request, response: Response,
                                       *, materialize_core) -> dict:
    """Terminalize an orphaned/uncertain paid claim without provider retry. The route keeps the
    client-visible docstring explaining why abandonment is operator-driven and never time-based."""
    from looplab.search.concept_lens import default_lenses

    rd = srv.run_dir(run_id)
    body = await lens_json_body(request)
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
    idempotency_key = lens_idempotency_key(
        request.headers.get("Idempotency-Key", ""))

    lens_pack = default_lenses()
    core = materialize_core(rd, run_id, None, lens_pack)
    generation = core[RUN_GENERATION_FIELD]
    base_frame = project_concept_frame(
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
            rd, current_generation = assert_lens_generation(
                srv, rd, core_generation=generation,
                expected_generation=expected_generation,
                stale_message="The run changed before the paid claim could be abandoned.",
                stale_remediation=(
                    "Reload Concepts; never abandon a receipt from another generation."),
                prepared_message="The run changed while the concept frame was being prepared.")
            computed_id = lens_identity(rd, current_generation, idempotency_key)
            if not hmac.compare_digest(request_id, computed_id):
                raise HTTPException(409, {
                    "code": "concept_lens_request_mismatch",
                    "message": "request_id is not bound to this run, generation, and Idempotency-Key.",
                })

            store = EventStore(rd / "events.jsonl")
            store_path = store.path
            ledger = fold_paid_ledger(
                CONCEPT_LENS_LEDGER, store.read_all(), current_generation)
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
                    return lens_uncertain(
                        base_frame, current_generation, request_id,
                        "The saved lens terminal is visible but its durability is unconfirmed.")
                return lens_terminal_response(
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
    terminal = record_lens_terminal(
        srv, rd, expected_generation, request_id, request_digest,
        EV_CONCEPT_LENS_COMPLETED,
        {"outcome": "abandoned", "reason": "operator_abandoned", "resolution": "operator"},
    )
    if terminal is None or not confirm_terminal_receipt(store_path):
        return lens_uncertain(
            base_frame, expected_generation, request_id,
            "The operator resolution could not be confirmed durably; no provider retry was sent.")
    return lens_terminal_response(
        terminal, core, lens_pack, request_id)
