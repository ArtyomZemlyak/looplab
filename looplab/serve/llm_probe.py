"""The paid LLM-health PROBE behind `POST /api/llm/health`: one revision-fenced, idempotent,
output-capped (4 tokens) provider reachability check, and the app-local registry that makes a retry
of the same operation id replay its outcome instead of paying again.

Review 2026-09-22, SRV2-13 (doc 50 SR-04). This was eleven closures and a route body inside
`routers/misc.py::build_router`, so every branch of the fence — a configuration change mid-flight,
a replay, the single-flight refusal — was reachable only through the ASGI app. The bodies are
verbatim; the only edits thread what the closures captured: `srv` as an explicit first argument
(`srv.settings` for the captured `store`), and the three registry cells — the operation map, its
lock and the process-keyed HMAC key — as one `LLMHealthRegistry` the router builds once per app,
the lifetime the closures had. The move re-keyed ten reason-less blind handlers in the containment
census; each was reviewed and states its reason now, and their backlog rows are deleted.

NOT A `serve/paid_ledger.py` SPEC — measured, not assumed. That ledger is the house protocol for
paid UI-side work, and the probe shares its vocabulary (claim before the call, one terminal after
it, an orphan stays uncertain and is never replayed into a second call). It cannot share its
storage:
  * STORE. The ledger folds claims and terminals out of ONE RUN's `events.jsonl` and commits a
    terminal under that run's command sequencer. The probe belongs to no run: its inputs are the
    server's saved Settings and secret store.
  * FENCE. The ledger fences on the run GENERATION. The probe fences on the pair (settings revision,
    secret revision) — read six times on the success path, from the settings load to the post-call
    confirmation — plus a process-keyed HMAC of the EFFECTIVE target (model, URL, key, timeouts),
    computed three times, which catches ambient env/.env drift that no revision records.
  * IDENTITY. The ledger's is a 64-hex digest under a generation; the probe's is a client-minted
    UUIDv4 bound to the revision pair, and a reuse under another pair is a 409 conflict.
  * DURABILITY. A ledger claim survives a restart. This registry is process memory by design (256
    entries, a 10-minute TTL after completion); the durable half is the BROWSER's recovery record
    (`ui/src/Settings.jsx`), which reconciles with `replay_only`, and an absent entry then answers
    410 `outcome_unknown` with no provider call — absence stays absence.
  * CONCURRENCY. The ledger serializes per run; the probe admits ONE probe in flight per app.
  * ACCOUNTING. `paid_work.py::metered_run_client` attributes every usage delta to a run
    generation; the probe's spend is attributed to no ledger, which `docs/guide/concepts.md` states
    as deliberate (non-run-scoped spend is excluded). Routing it through the ledger would need a
    run to charge, or a new non-run storage tier inside the ledger — more protocol, not less.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import time
from collections import OrderedDict
from typing import Annotated, Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from looplab.core.config import Settings
from looplab.serve.assistant import safe_provider_failure


_LLM_HEALTH_OPERATION_MAX = 256
_LLM_HEALTH_OPERATION_TTL_S = 10 * 60.0
_LLM_HEALTH_MAX_TOKENS = 4
_LLM_HEALTH_TIMEOUT_MIN_S = 0.25
_LLM_HEALTH_TIMEOUT_MAX_S = 60.0


class LLMHealthRequest(BaseModel):
    """One paid provider probe bound to the saved Settings snapshot the owner displayed."""

    model_config = ConfigDict(extra="forbid")

    expected_settings_revision: Annotated[str, Field(min_length=1, max_length=256)]
    expected_secret_revision: Annotated[str, Field(min_length=1, max_length=256)]
    operation_id: str = Field(
        min_length=36, max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )
    replay_only: bool = False


# App-local paid-probe registry. It intentionally does not cross run roots/processes and retains
# only opaque request identities plus terminal public envelopes -- never Settings, credentials,
# provider output, clients, or raw exceptions.
class LLMHealthRegistry:
    """The three cells `build_router` closed over, as one object with the same lifetime: the router
    builds one per app, so it still crosses no app, run root or process."""

    def __init__(self) -> None:
        self.operations: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.lock = threading.Lock()
        self.identity_key = secrets.token_bytes(32)


def _llm_health_revisions(srv) -> tuple[str, str]:
    store = srv.settings
    # The fixed lock order matches GET /api/settings. Never hold either lock across provider I/O.
    with store.ui_settings_transaction(), store.secret_transaction():
        return store.ui_settings_revision(), store.secret_revision()


def _llm_health_effective_identity(registry: LLMHealthRegistry, settings: Settings,
                                   timeout_s: float) -> str:
    """Process-keyed identity of the actual probe inputs, including URL/key, without a verifier."""
    from looplab.core.llm import (
        bound_api_key_for, client_kwargs_for, resolve_llm_target,
    )
    target = resolve_llm_target(settings)
    target_kwargs = client_kwargs_for(target, timeout=timeout_s)
    secret = bound_api_key_for(
        settings, target.base_url, api_key=target_kwargs.get("api_key"),
        api_key_base_url=target_kwargs.get("api_key_base_url"))
    header_timeout = min(float(getattr(settings, "llm_header_timeout", timeout_s)), timeout_s)
    canonical = json.dumps({
        "model": target.model,
        "base_url": str(target.base_url).rstrip("/"),
        "api_key": secret,
        "api_key_base_url": target_kwargs.get("api_key_base_url"),
        "temperature": target.temperature,
        "trust_env": bool(getattr(settings, "llm_trust_env", False)),
        "header_timeout": header_timeout,
        "wall_timeout": timeout_s,
        "stream": False,
        "reasoning": False,
        "cache": False,
        "max_tokens": _LLM_HEALTH_MAX_TOKENS,
    }, ensure_ascii=True, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")
    return "probe-v1:" + hmac.new(
        registry.identity_key, canonical, hashlib.sha256).hexdigest()


def _llm_health_prune_locked(registry: LLMHealthRegistry, now: float) -> None:
    for operation_id, entry in list(registry.operations.items()):
        completed_at = entry.get("completed_at")
        if completed_at is not None and now - completed_at >= _LLM_HEALTH_OPERATION_TTL_S:
            registry.operations.pop(operation_id, None)


def _llm_health_reserve(registry: LLMHealthRegistry,
                        body: LLMHealthRequest) -> tuple[str, dict[str, Any] | None]:
    identity = (body.expected_settings_revision, body.expected_secret_revision)
    now = time.monotonic()
    with registry.lock:
        _llm_health_prune_locked(registry, now)
        existing = registry.operations.get(body.operation_id)
        if existing is not None:
            registry.operations.move_to_end(body.operation_id)
            return (("replay", existing) if existing["identity"] == identity
                    else ("conflict", existing))
        # Reconciliation is observation-only. After TTL eviction or a process restart, absence
        # must stay absence instead of silently turning the old paid UUID into a new provider call.
        if body.replay_only:
            return "missing", None
        # One app issues at most one paid probe at a time, even when two tabs mint different UUIDs.
        if any(entry.get("completed_at") is None for entry in registry.operations.values()):
            return "busy", None
        while len(registry.operations) >= _LLM_HEALTH_OPERATION_MAX:
            victim = next((key for key, entry in registry.operations.items()
                           if entry.get("completed_at") is not None), None)
            if victim is None:
                return "capacity", None
            registry.operations.pop(victim, None)
        entry = {
            "identity": identity,
            "done": threading.Event(),
            "outcome": None,
            "completed_at": None,
        }
        registry.operations[body.operation_id] = entry
        return "leader", entry


def _llm_health_finish(registry: LLMHealthRegistry, operation_id: str, entry: dict[str, Any],
                       outcome: tuple[int, dict[str, Any]]) -> None:
    # `outcome` is already the public, allow-listed envelope. Keep no exception, Settings, client,
    # provider text, URL, key, or response object reachable from the replay registry.
    public_outcome = (int(outcome[0]), dict(outcome[1]))
    with registry.lock:
        entry["outcome"] = public_outcome
        entry["completed_at"] = time.monotonic()
        if registry.operations.get(operation_id) is entry:
            registry.operations.move_to_end(operation_id)
        entry["done"].set()


def _llm_health_render(entry: dict[str, Any]):
    entry["done"].wait()
    outcome = entry.get("outcome")
    if not isinstance(outcome, tuple) or len(outcome) != 2:
        raise HTTPException(503, detail={
            "code": "llm_health_outcome_unavailable",
            "message": "The provider-check outcome is unavailable.",
        })
    status_code, payload = outcome
    if status_code != 200:
        raise HTTPException(status_code=status_code, detail=dict(payload))
    return dict(payload)


def _llm_health_base(body: LLMHealthRequest, revisions: tuple[str, str], *,
                     provider_attempted: bool, effective_identity: str) -> dict:
    return {
        "operation_id": body.operation_id,
        "settings_revision": revisions[0],
        "secret_revision": revisions[1],
        "provider_attempted": provider_attempted,
        "effective_identity": effective_identity,
    }


def _llm_health_changed(body: LLMHealthRequest, revisions: tuple[str, str], *,
                        provider_attempted: bool, change_scope: str,
                        effective_identity: str | None = None,
                        current_effective_identity: str | None = None) -> tuple[int, dict]:
    after = provider_attempted
    detail = {
        "code": ("llm_configuration_changed_after_attempt" if after
                 else "llm_configuration_changed"),
        "message": ("LLM configuration changed while the provider attempt was in flight; "
                    "the paid outcome is ambiguous." if after else
                    "LLM configuration changed before the provider attempt."),
        "remediation": ("Do not start a new probe automatically; inspect the saved configuration "
                        "and explicitly choose whether to try again." if after else
                        "Reload saved Settings and start a new provider check."),
        "operation_id": body.operation_id,
        "provider_attempted": provider_attempted,
        "ambiguous": after,
        "outcome_unknown": after,
        "retryable": False,
        "change_scope": change_scope,
        "expected_settings_revision": body.expected_settings_revision,
        "expected_secret_revision": body.expected_secret_revision,
        "settings_revision": revisions[0],
        "secret_revision": revisions[1],
    }
    if effective_identity is not None:
        detail["effective_identity"] = effective_identity
    if current_effective_identity is not None:
        detail["current_effective_identity"] = current_effective_identity
    return 409, detail


def _llm_health_unverifiable(body: LLMHealthRequest, *,
                             provider_attempted: bool) -> tuple[int, dict]:
    return (409 if provider_attempted else 503), {
        "code": ("llm_health_outcome_unverifiable_after_attempt" if provider_attempted
                 else "llm_health_precondition_unavailable"),
        "message": ("The provider attempt may have completed, but its configuration fence "
                    "could not be verified." if provider_attempted else
                    "The saved LLM configuration fence is temporarily unavailable."),
        "remediation": ("Treat this operation as terminal and ambiguous; do not start another "
                        "probe automatically." if provider_attempted else
                        "Retry later with a new operation ID after settings storage is available."),
        "operation_id": body.operation_id,
        "provider_attempted": provider_attempted,
        "ambiguous": provider_attempted,
        "outcome_unknown": provider_attempted,
        "retryable": False,
        "expected_settings_revision": body.expected_settings_revision,
        "expected_secret_revision": body.expected_secret_revision,
    }


def _llm_health_definitive_rejection(_exc: Exception, failure: dict) -> bool:
    # Only classifications whose semantics prove that the provider declined the request may
    # release the recovery fence. A generic/provider-specific 4xx (including proxy 4xx) can be
    # emitted after upstream work started, so its billing/outcome must remain unknown.
    return failure.get("error_kind") in {"credentials", "rate_limit"}


def _run_llm_health(srv, registry: LLMHealthRegistry, body: LLMHealthRequest,
                    attempt_state: dict[str, bool]) -> tuple[int, dict[str, Any]]:
    expected = (body.expected_settings_revision, body.expected_secret_revision)

    def _preflight_failure(exc: Exception,
                           effective_identity: str | None = None) -> tuple[int, dict[str, Any]]:
        try:
            current = _llm_health_revisions(srv)
        except Exception:  # noqa: BLE001 — an unreadable fence answers the no-attempt 503
            return _llm_health_unverifiable(body, provider_attempted=False)
        if current != expected:
            return _llm_health_changed(
                body, current, provider_attempted=False, change_scope="saved",
                effective_identity=effective_identity)
        if effective_identity is None:
            return _llm_health_unverifiable(body, provider_attempted=False)
        return 200, {
            "ok": False,
            **safe_provider_failure(exc),
            **_llm_health_base(
                body, current, provider_attempted=False,
                effective_identity=effective_identity),
            "outcome_unknown": False,
        }

    try:
        revisions = _llm_health_revisions(srv)
    except Exception:  # noqa: BLE001 -- never persist/reflect lock or filesystem detail
        return _llm_health_unverifiable(body, provider_attempted=False)
    if revisions != expected:
        return _llm_health_changed(
            body, revisions, provider_attempted=False, change_scope="saved")

    try:
        timeout_s = float(os.environ.get("LOOPLAB_HEALTHCHECK_TIMEOUT", "10.0"))
        if (not math.isfinite(timeout_s)
                or not _LLM_HEALTH_TIMEOUT_MIN_S <= timeout_s <= _LLM_HEALTH_TIMEOUT_MAX_S):
            raise ValueError("health-check timeout is outside the supported range")
        settings = srv.llm_settings()
        effective_identity = _llm_health_effective_identity(registry, settings, timeout_s)
    except Exception as exc:  # noqa: BLE001 — no client yet: `_preflight_failure` classifies it
        return _preflight_failure(exc)

    # Preserve the settings-load CAS boundary before constructing any client object.
    try:
        revisions = _llm_health_revisions(srv)
    except Exception:  # noqa: BLE001 — unreadable fence, no client yet: the no-attempt 503
        return _llm_health_unverifiable(body, provider_attempted=False)
    if revisions != expected:
        return _llm_health_changed(
            body, revisions, provider_attempted=False, change_scope="saved",
            effective_identity=effective_identity)

    try:
        # Re-resolve ambient env/.env before construction. The HMAC is process-keyed, so it
        # detects key/URL drift without exposing or storing either value.
        current_settings = srv.llm_settings()
        current_effective_identity = _llm_health_effective_identity(
            registry, current_settings, timeout_s)
    except Exception as exc:  # noqa: BLE001 — no client yet: `_preflight_failure` classifies it
        return _preflight_failure(exc, effective_identity)

    try:
        revisions = _llm_health_revisions(srv)
    except Exception:  # noqa: BLE001 — unreadable fence, no client yet: the no-attempt 503
        return _llm_health_unverifiable(body, provider_attempted=False)
    if revisions != expected:
        return _llm_health_changed(
            body, revisions, provider_attempted=False, change_scope="saved",
            effective_identity=effective_identity)

    if current_effective_identity != effective_identity:
        return _llm_health_changed(
            body, revisions, provider_attempted=False, change_scope="effective",
            effective_identity=effective_identity,
            current_effective_identity=current_effective_identity)

    try:
        from looplab.core.llm import make_llm_client_for

        def _probe_factory(current_settings, **target_kwargs):
            return srv.make_llm_client(
                current_settings, **target_kwargs, max_retries=0, stream=False,
                disable_reasoning=True, wall_timeout=timeout_s, cache=False)

        client = make_llm_client_for(
            settings, timeout=timeout_s, factory=_probe_factory)
    except Exception as exc:  # noqa: BLE001 — nothing sent yet: `_preflight_failure` classifies it
        return _preflight_failure(exc, effective_identity)

    # This is deliberately the final operation before entering the one-call client method.
    try:
        revisions = _llm_health_revisions(srv)
    except Exception:  # noqa: BLE001 — unreadable fence, nothing sent: the no-attempt 503
        return _llm_health_unverifiable(body, provider_attempted=False)
    if revisions != expected:
        return _llm_health_changed(
            body, revisions, provider_attempted=False, change_scope="saved",
            effective_identity=effective_identity)

    provider_error: Exception | None = None
    attempt_state["provider_attempted"] = True
    try:
        client.probe(
            [{"role": "user", "content": "Reply with one word: ready"}],
            max_tokens=_LLM_HEALTH_MAX_TOKENS)
    except Exception as exc:  # noqa: BLE001 — provider failure is the result; classified below
        provider_error = exc

    # A paid attempt is now possible. Any failure to prove the postcondition is terminal and
    # ambiguous; it must never be downgraded to an ordinary retryable provider failure.
    try:
        revisions = _llm_health_revisions(srv)
    except Exception:  # noqa: BLE001 — post-attempt: an unreadable fence is terminal + ambiguous
        return _llm_health_unverifiable(body, provider_attempted=True)
    if revisions != expected:
        return _llm_health_changed(
            body, revisions, provider_attempted=True, change_scope="saved",
            effective_identity=effective_identity)
    try:
        current_settings = srv.llm_settings()
        current_effective_identity = _llm_health_effective_identity(
            registry, current_settings, timeout_s)
        confirmed_revisions = _llm_health_revisions(srv)
    except Exception:  # noqa: BLE001 — post-attempt: an unconfirmable fence is terminal + ambiguous
        return _llm_health_unverifiable(body, provider_attempted=True)
    if confirmed_revisions != expected:
        return _llm_health_changed(
            body, confirmed_revisions, provider_attempted=True, change_scope="saved",
            effective_identity=effective_identity)
    if current_effective_identity != effective_identity:
        return _llm_health_changed(
            body, confirmed_revisions, provider_attempted=True, change_scope="effective",
            effective_identity=effective_identity,
            current_effective_identity=current_effective_identity)

    base = _llm_health_base(
        body, confirmed_revisions, provider_attempted=True,
        effective_identity=effective_identity)
    if provider_error is not None:
        failure = safe_provider_failure(provider_error)
        # Only an explicit auth/rate-limit rejection proves the provider declined the request.
        # A 5xx, empty/no-choices HTTP 200, timeout, connection loss, or generic post-send error
        # may already have generated/billed work even though no usable response reached us.
        outcome_unknown = not _llm_health_definitive_rejection(provider_error, failure)
        if outcome_unknown:
            message = ("The provider-check outcome is unresolved; the request may have "
                       "completed or been billed.")
            failure = {**failure,
                       "code": "llm_health_provider_outcome_unknown",
                       "error": message,
                       "message": message,
                       "remediation": "Do not start another probe automatically.",
                       "retryable": False}
        return 200, {"ok": False, **failure, **base,
                     "outcome_unknown": outcome_unknown}
    return 200, {"ok": True, **base, "outcome_unknown": False}


def llm_health_operation(srv, registry: LLMHealthRegistry, body: LLMHealthRequest):
    """The route's whole body: reserve the operation id, then either replay its recorded outcome or
    run the one fenced provider call, terminalize the flight and render it."""
    reservation, entry = _llm_health_reserve(registry, body)
    if reservation == "conflict":
        raise HTTPException(409, detail={
            "code": "llm_health_operation_conflict",
            "message": "This operation ID belongs to a different saved-configuration identity.",
            "remediation": "Use a new UUIDv4 for a different saved Settings snapshot.",
            "operation_id": body.operation_id,
            "expected_settings_revision": body.expected_settings_revision,
            "expected_secret_revision": body.expected_secret_revision,
            "provider_attempted": False,
            "outcome_unknown": False,
        })
    if reservation == "busy":
        raise HTTPException(409, detail={
            "code": "llm_health_in_progress",
            "message": "Another provider check is already in progress.",
            "remediation": "Wait for the active check to finish before starting another one.",
            "operation_id": body.operation_id,
            "expected_settings_revision": body.expected_settings_revision,
            "expected_secret_revision": body.expected_secret_revision,
            "provider_attempted": False,
            "outcome_unknown": False,
        })
    if reservation == "missing":
        raise HTTPException(410, detail={
            "code": "llm_health_replay_unavailable",
            "message": ("The prior provider-check result is no longer available; replay-only "
                        "reconciliation made no new provider call."),
            "remediation": ("Treat the prior outcome as unresolved. Start a new provider check "
                            "only through an explicit new user action and UUIDv4."),
            "operation_id": body.operation_id,
            "expected_settings_revision": body.expected_settings_revision,
            "expected_secret_revision": body.expected_secret_revision,
            "provider_attempted": False,
            "outcome_unknown": True,
            "ambiguous": True,
            "retryable": False,
            "replay_only": True,
        })
    if reservation == "capacity":
        raise HTTPException(503, detail={
            "code": "llm_health_capacity",
            "message": "Provider-check replay capacity is temporarily unavailable.",
            "operation_id": body.operation_id,
            "expected_settings_revision": body.expected_settings_revision,
            "expected_secret_revision": body.expected_secret_revision,
            "provider_attempted": False,
            "outcome_unknown": False,
        })
    assert entry is not None
    if reservation == "replay":
        return _llm_health_render(entry)

    attempt_state = {"provider_attempted": False}
    try:
        outcome = _run_llm_health(srv, registry, body, attempt_state)
    except Exception:  # noqa: BLE001 -- terminalize the flight; never strand duplicate waiters
        outcome = _llm_health_unverifiable(
            body, provider_attempted=attempt_state["provider_attempted"])
    _llm_health_finish(registry, body.operation_id, entry, outcome)
    return _llm_health_render(entry)
