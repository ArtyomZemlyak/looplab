"""Retry/backoff + transport-error classification for the LLM clients (split out of `core.llm`).

Free functions only, no client state: exponential backoff (`_backoff`), Retry-After parsing
(`_retry_after_of` / `_retry_after_seconds`), and the classifiers that decide whether an SDK error
is worth retrying (`_sdk_transient`), is a rate-limit throttle dressed as a 403
(`_is_throttle_403`), or is an endpoint rejecting our reasoning toggle (`_is_reasoning_reject`).
`core.llm` re-imports every name under its original name, so `looplab.core.llm._backoff` (and the
flat `looplab.llm._backoff`) keep resolving to the SAME objects — tests and callers import and
monkeypatch through those paths.
"""
from __future__ import annotations

from typing import Optional

# `ssl` is used by the SDK-path error classifier.
import ssl

# httpx is a declared runtime dep, but the import is GUARDED for the same reason as in `core.llm`:
# an offline/replay/`--no-deps` install must still import the package without the live LLM stack.
# `_sdk_transient` (the only user) is only ever called on SDK-path errors, which cannot occur
# unless httpx is installed.
try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover - deps are declared; guard is for stripped/offline installs
    httpx = None  # type: ignore[assignment]

# Named retry/backoff constants (previously inline magic numbers).
BACKOFF_CAP_S = 30.0                 # ceiling on any single SELF-CHOSEN exponential-backoff sleep
# A SERVER-supplied Retry-After is a directive, not our own backoff, so it gets its own (larger)
# ceiling: honor a legit `Retry-After: 60` instead of silently cutting it to the 30s backoff cap,
# while still bounding a pathological far-future HTTP-date.
RETRY_AFTER_CAP_S = 120.0


def _backoff(attempt: int) -> float:
    """Exponential-backoff delay for retry `attempt` (0-based), capped at BACKOFF_CAP_S."""
    return min(2.0 * (2 ** attempt), BACKOFF_CAP_S)


# Substrings that mark an HTTP 400 as "this endpoint rejects our REASONING toggle" (e.g. a
# litellm-proxied model returning UnsupportedParamsError for `reasoning_effort`) rather than a
# genuine bad request — shared by `_post` and `complete_text_stream`.
_REASONING_REJECT_KEYS = ("reasoning", "unsupportedparams", "does not support parameters",
                          "extra_forbidden", "unexpected keyword", "unrecognized")


def _is_reasoning_reject(err_body: str) -> bool:
    """True when a 400 error body (already lowercased) says the endpoint rejected the reasoning
    param — the caller then drops the toggle for this client and retries."""
    return any(k in err_body for k in _REASONING_REJECT_KEYS)


def _is_throttle_403(err_body: str) -> bool:
    """True when a 403 body looks like a RATE-LIMIT / burst security throttle (retryable with backoff),
    NOT a hard 'forbidden' (bad key / plan / route, which must fail fast). A hosted gateway (OpenRouter)
    or a corporate proxy/WAF returns a 403 when a request BURST trips its abuse/rate policy — observed
    live as {"success": false, "error": "Access denied by security policy"}; a backed-off retry rides
    through it (this is what let a 403 outage rapid-fire dozens of dev-crash nodes)."""
    b = (err_body or "").lower()
    return any(k in b for k in ("access denied by security policy", "security policy", "rate limit",
                                "rate-limit", "too many request", "throttl", "temporarily", "try again"))


def _err_body(exc: Exception) -> str:
    """Lowercased text of an openai SDK error (its parsed `body` + message), for reasoning-reject
    detection — the SDK surfaces the endpoint's error payload on `.body` and `.message`."""
    return (str(getattr(exc, "body", "") or "") + " " + str(getattr(exc, "message", "") or exc)).lower()


def _retry_after_of(exc: Exception) -> Optional[str]:
    """The Retry-After header from an openai SDK error's HTTP response, if any (429/503 backoff hint)."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    return headers.get("retry-after") if headers is not None else None


def _sdk_transient(exc: Exception) -> bool:
    """Whether an openai.APIConnectionError is worth RETRYING. Preserves the urllib-era distinction
    now that httpx collapses several causes into APIConnectionError: a refused connection / DNS
    failure / TLS-cert error is steady-state ('endpoint down or misconfigured') → fail FAST (so
    /api/llm/health is instant); a reset / TLS-EOF / mid-read protocol error is a transient hiccup on
    a busy gateway → retry. The real cause is on `__cause__` (httpx wraps it)."""
    for x in (exc, getattr(exc, "__cause__", None)):
        if isinstance(x, ssl.SSLCertVerificationError):
            return False
        if isinstance(x, httpx.ConnectError):     # connection refused / DNS resolution failure
            return False
    return True                                   # reset / EOF / protocol error mid-read → transient


def _retry_after_seconds(ra) -> Optional[float]:
    """Parse a Retry-After header into seconds. It may be a number (int/float seconds) OR an
    HTTP-date; returns the delay in seconds (clamped ≥0) or None when absent/unparseable (caller
    then falls back to exponential backoff)."""
    if not ra:
        return None
    s = str(ra).strip()
    try:
        return max(0.0, float(s))
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None
