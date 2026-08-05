"""Retry/backoff + transport-error classification for the LLM clients (split out of `core.llm`).

Free functions only, no client state: exponential backoff (`_backoff`), Retry-After parsing
(`_retry_after_of` / `_retry_after_seconds`), and the classifiers that decide whether an SDK error
is worth retrying (`_sdk_transient`), is a rate-limit throttle dressed as a 403
(`_is_throttle_403`), is an endpoint rejecting our reasoning toggle (`_is_reasoning_reject`), or —
once every retry is spent — WHAT KIND of failure the operator is actually looking at
(`classify_llm_failure`). `core.llm` re-imports every name under its original name, so
`looplab.core.llm._backoff` (and the flat `looplab.llm._backoff`) keep resolving to the SAME
objects — tests and callers import and monkeypatch through those paths.
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

# Same guard, same reason. `classify_llm_failure` differs from `_sdk_transient` in one way that
# matters here: it IS reachable without an SDK error in hand (a custom transport supplied through
# the `make_llm_client` seam may raise anything at all), so it must degrade to its unclassified
# answer rather than trip over a missing module.
try:
    import openai
except ModuleNotFoundError:  # pragma: no cover - deps are declared; guard is for stripped/offline installs
    openai = None  # type: ignore[assignment]

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


def _is_stream_options_reject(err_body: str) -> bool:
    """True when a 400 body names `stream_options` as the rejected field.

    `stream_options: {"include_usage": true}` is an OPTIONAL OpenAI-compatible capability that we
    send unconditionally on every streaming call to get token usage back. A provider that rejects
    only this field 400s identically on every retry, and the blocking text fallback re-enters the
    same builder — so the whole client was dead against such an endpoint. Checked BEFORE
    `_is_reasoning_reject`, whose generic keys ("extra_forbidden", "unrecognized", …) also match
    these bodies and would otherwise mis-attribute the rejection to the reasoning toggle and retry
    with the offending field still attached."""
    return "stream_options" in (err_body or "") or "stream options" in (err_body or "")


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


# The closed vocabulary of REASONS a provider call can have failed for good, once the client has
# spent every retry it was given. It exists because the two ends of this list demand OPPOSITE
# actions from the operator and used to be reported with one sentence: a run refused for
# "throttled" is refused by an endpoint that is UP, correctly configured and answering, so telling
# that operator to "start the endpoint or point LOOPLAB_LLM_BASE_URL at one" is advice that cannot
# possibly help — measured twice on live launches against a rate-limited (HTTP 429,
# `Current limit: 50`) endpoint. `agents/preflight.py` renders exactly one remedy per member and
# `tests/test_llm_client.py` pins the truth table, so a new member without a remedy is a red test
# rather than a run refused with silence.
LLM_FAILURE_CAUSES = ("throttled", "overloaded", "unreachable", "credential", "model", "protocol")

# How deep to walk `__cause__` before giving up. The client raises `LLMError(...) from exc` at every
# one of its policy sites, so the SDK error is at depth 1; the extra room is for a caller that wraps
# the LLMError again (`make_llm_client_for`, a role's own error path).
_CAUSE_CHAIN_DEPTH = 6


def _cause_chain(exc: BaseException):
    """`exc` and its `__cause__` ancestors, bounded and cycle-safe."""
    seen: set[int] = set()
    for _ in range(_CAUSE_CHAIN_DEPTH):
        if exc is None or id(exc) in seen:
            return
        seen.add(id(exc))
        yield exc
        exc = getattr(exc, "__cause__", None)


def classify_llm_failure(exc: BaseException) -> str:
    """Which member of `LLM_FAILURE_CAUSES` does this failed provider call belong to?

    Read off the SDK error the client raised `from`, NOT off the message text: `raise LLMError(...)
    from exc` is used at every `_RETRY_POLICY` site, so the openai exception is the ground truth and
    survives the client's own wording changes. An unclassifiable failure answers "protocol" — the
    one honest answer for "the endpoint said something we cannot interpret" — and deliberately NOT
    "unreachable", because defaulting to the diagnosis with the loudest remedy ("start the
    endpoint") is exactly the defect this function was added to remove.

    Classification is by STATUS CODE, not by exception class, for the 4xx family: the openai SDK
    maps each code to its own class, but the mapping is the SDK's and the codes are the endpoint's.
    The one exception is 403, which is genuinely ambiguous on the wire — a hosted gateway or WAF
    returns it for a request BURST as well as for a hard "not allowed" — so it re-uses the same
    `_is_throttle_403` body test `_policy_forbidden` decides retryability with. Both halves are
    real: the team endpoint returns a hard 403 (`team_model_access_denied`) for a model outside the
    allow-list, including for a tier suffix like `qwen3.5-122b:max`.
    """
    for err in _cause_chain(exc):
        if openai is None or not isinstance(err, openai.APIError):
            continue
        # No status = the request never got an HTTP answer: refused connection, DNS failure, TLS
        # error, or a read/connect timeout (`APITimeoutError`, an APIConnectionError subclass).
        # This is the ONLY family that means "there is nothing there".
        status = getattr(err, "status_code", None)
        if not isinstance(status, int):
            return "unreachable" if isinstance(err, openai.APIConnectionError) else "protocol"
        if status == 401:
            return "credential"
        if status == 403:
            return "throttled" if _is_throttle_403(_err_body(err)) else "model"
        if status == 429:
            return "throttled"
        # 408 keeps the 5xx company on purpose: "the endpoint answered, and its answer was that it
        # could not serve this request right now" is the same operator situation either way.
        if status == 408 or status >= 500:
            return "overloaded"
        if status in (400, 404):
            return "model"
        return "protocol"
    return "protocol"


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
