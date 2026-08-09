"""LLM client + cost accounting (I2/I13, ADR-14/17/11).

`OpenAICompatibleClient` is the LIVE path — an OpenAI-compatible chat client on the
openai SDK (httpx transport), whose per-read timeout reliably bounds a stalled stream.
`LiteLLMClient` is an optional, lazy-imported compatibility adapter; the shipped Settings
factory selects `OpenAICompatibleClient`. The openai/httpx imports are declared deps but
GUARDED, so the offline engine + replay still import this module (via `core.config`) without
the live LLM stack. `CostAccountant` always meters calls and enforces a hard stop only when
its caller supplies a finite limit; shipped Settings do not expose a dollar-cap field.
Secrets are never stored as values here — the client reads the key from config/env.

The retry/error-classification, SSE-stream, and tool-call-parsing helpers live in the flat
siblings `llm_transient` / `llm_streaming` / `llm_toolcall` and are re-imported below under
their original names, so every historical import/monkeypatch path through this module holds.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import sys
from collections import OrderedDict
from contextlib import contextmanager
import threading
import time
from typing import Callable, NamedTuple, Optional
from urllib.parse import urlsplit, urlunsplit

# The LIVE transport runs on the openai SDK over an httpx client. Both are declared runtime deps
# (pyproject `dependencies`), but the import is GUARDED: `core.config` imports `DEFAULT_HEADER_TIMEOUT_S`
# from this module, so this module backs the whole-package import — an offline/replay/`--no-deps`
# install must still `import looplab` without the live LLM stack. When absent, the names are None and
# constructing `OpenAICompatibleClient` raises a clear LLMError (the offline engine never builds one).
try:
    import httpx
    import openai
except ModuleNotFoundError:  # pragma: no cover - deps are declared; guard is for stripped/offline installs
    httpx = None   # type: ignore[assignment]
    openai = None  # type: ignore[assignment]

from looplab.core import tracing
from looplab.core.llm_broker import llm_request_permit
# Re-exported for backward compatibility: dozens of importers (and tests) do
# `from looplab.core.llm import LLMError / BudgetExceeded`. The definitions live in
# `looplab.core.errors` so `parse` can import them without importing this module.
from looplab.core.errors import (  # noqa: F401
    BudgetExceeded, LLMCredentialError, LLMError, credential_cause)
# Safe top-level import (no cycle): parse imports only from looplab.core.errors now.
from looplab.core.parse import split_think  # noqa: F401  (also a re-export)
# Split siblings (docs/15 §P5.2): retry/backoff + error classification (`llm_transient`), the
# SSE/stream machinery (`llm_streaming`), and native tool-call / assistant-message parsing
# (`llm_toolcall`) were split out of this module. Every moved name is RE-IMPORTED here under its
# original name, so `looplab.core.llm._X`, the flat `looplab.llm._X` and the owning sibling's own
# `_X` all READ the same object — that is what keeps existing imports and direct calls working.
#
# What the shim does NOT give you is a monkeypatch seam (doc 25 CO-10). Rebinding
# `looplab.core.llm._X` replaces only THIS module's alias; a sibling that calls `_X` through its own
# module globals keeps calling the original, so the patch is a silent no-op — the exact failure mode
# the project's registry-guard convention exists to prevent. Patch the OWNING module instead
# (`looplab.core.llm_streaming._X`). `tests/test_llm_reexport_seam.py` enumerates which names are
# affected and fails if a new one appears without this note being true of it.
from looplab.core.llm_transient import (  # noqa: F401
    BACKOFF_CAP_S, LLM_FAILURE_CAUSES, RETRY_AFTER_CAP_S, _REASONING_REJECT_KEYS, _backoff,
    _err_body, _is_reasoning_reject, _is_stream_options_reject, _is_throttle_403,
    _retry_after_of, _retry_after_seconds, _sdk_transient, classify_llm_failure)
from looplab.core.llm_streaming import (  # noqa: F401
    _chunk_has_content, _shutdown_pool_sockets, _stream_raw_socket, _stream_with_idle_guard)
from looplab.core.llm_toolcall import (  # noqa: F401
    _ANSWER_FIELDS, _CODE_SPAN_RE, _FINAL_NAMES, _NATIVE_INVOKE_RE, _NATIVE_OPEN_RE,
    _NATIVE_PARAM_RE, _apply_native_tool_calls, _args_complete, _assistant_text, _clean_thinking,
    _code_spans, _extract_native_tool_calls, _reasoning_of, _tool_call_slot)

# Narration for the one thing this module does that takes visible wall-clock time without saying so:
# the 429/5xx backoff (`_policy_throttled`). WARNING because that is the level logging's `lastResort`
# handler emits, so it reaches stderr in a CLI run that configured no logging at all — the same
# reasoning, for the same "silent wait reads as a deadlock" failure, as
# `engine/resources.py::_note_gpu_host_lease_contention`.
_LOG = logging.getLogger(__name__)

# Named stream/timeout constants (previously inline magic numbers). Their retry/backoff siblings
# (BACKOFF_CAP_S / RETRY_AFTER_CAP_S) live in `llm_transient` and are re-imported above.
STREAM_STALL_DEGRADE_AFTER = 2       # stream stalls before this client goes non-streaming for good
# Default first-byte (response-headers) window, seconds. The single source: config.py's
# `Settings.llm_header_timeout` imports this constant as its field default.
DEFAULT_HEADER_TIMEOUT_S = 45.0


def normalize_llm_base_url(value: str) -> str:
    """Return the one canonical spelling accepted for a credential-bearing LLM endpoint.

    The binding is intentionally stricter than a general-purpose URL parser. Userinfo, query and
    fragment components can redirect the apparent authority or make two visually similar strings
    mean different requests; whitespace/control characters and explicit default ports create
    alternate spellings of the same destination. Refuse those forms instead of guessing.
    """
    original = str(value or "")
    raw = original.strip()
    if (not raw or raw != original
            or any(ch.isspace() or ord(ch) < 0x21 or ord(ch) == 0x7F for ch in raw)
            or "\\" in raw or "%" in raw):
        raise LLMError(
            "LLM base URL is empty or contains whitespace/control/escaped characters")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise LLMError("LLM base URL is malformed") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise LLMError("LLM base URL must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise LLMError("LLM base URL must not contain userinfo")
    if parsed.query or parsed.fragment:
        raise LLMError("LLM base URL must not contain a query or fragment")
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        raise LLMError("LLM base URL must omit the scheme's default port")
    try:
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError) as exc:
        raise LLMError("LLM base URL contains an invalid hostname") from exc
    if not host:
        raise LLMError("LLM base URL contains an invalid hostname")
    if ":" not in host:
        labels = host.split(".")
        if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
               or any(not (char.isascii() and (char.isalnum() or char == "-"))
                      for char in label)
               for label in labels):
            raise LLMError("LLM base URL contains an invalid hostname")
    if ":" in host:  # IPv6 literals are emitted in their required bracketed authority form.
        host = f"[{host}]"
    authority = host if port is None else f"{host}:{port}"
    path = (parsed.path or "").rstrip("/")
    if path.startswith("//") or any(part in {".", ".."} for part in path.split("/")):
        raise LLMError("LLM base URL contains an ambiguous path")
    return urlunsplit((scheme, authority, path, "", ""))


def normalize_llm_base_url_or_none(value) -> str | None:
    """`normalize_llm_base_url`, but None instead of raising — for COMPARING, never for connecting.

    Diagnosis-only. A refusal that wants to say "and this variable is what moved the endpoint" has to
    compare two spellings of a URL, and an unset/garbage variable is simply not the knob rather than
    a second error to report on top of the one being explained.
    """
    if not value:
        return None
    try:
        return normalize_llm_base_url(value)
    except LLMError:
        return None


class _NoCredential:
    """Internal sentinel: an endpoint override deliberately gets no shared credential fallback."""

    def __repr__(self) -> str:  # keep patched-factory diagnostics content-free
        return "NO_CREDENTIAL"


NO_CREDENTIAL = _NoCredential()


def _secret_value(value) -> str:
    if value is None:
        return ""
    try:
        return str(value.get_secret_value())
    except AttributeError:
        return str(value)


# The variables an operator actually types. Named constants because every refusal below quotes them
# verbatim, and a refusal that names the WRONG variable leaves the operator worse off than silence.
SHARED_KEY_ENV = "LOOPLAB_LLM_API_KEY"
SHARED_BINDING_ENV = "LOOPLAB_LLM_API_KEY_BASE_URL"
SHARED_ENDPOINT_ENV = "LOOPLAB_LLM_BASE_URL"

# Where a resolved key+binding pair came from. The SOURCE is part of the diagnosis, not decoration:
# the failure this text exists for is "I overrode one half HERE and expected the other half from
# THERE to still apply", so the operator has to be told which source actually won.
_SOURCE_PROCESS = "the process environment"
_SOURCE_DOTENV = "the .env file"
_SOURCE_SETTINGS = "this run's resolved settings"
_SOURCE_SUPPLIED = "the credential supplied for this target"


class _AmbientCredential(NamedTuple):
    """A shared key+binding pair AND the provenance its refusal messages need.

    `source` and `shadowed` exist only to be printed. They are resolved here rather than re-derived
    at the raise site because this function IS the tier decision: a second reader of `os.environ`
    /`.env` could disagree with it, and would then describe a selection that never happened.
    """
    key: str
    binding: str
    source: str                  # a _SOURCE_* label, or "" when neither name was declared anywhere
    shadowed: tuple[str, ...]    # the half the LOSING source still has, dropped by the atomic rule


def _dotenv_pair() -> dict[str, str]:
    """The two shared-credential names as the repo-root `.env` declares them (upper-cased)."""
    try:
        from dotenv import dotenv_values
        return {str(name).upper(): ("" if value is None else str(value))
                for name, value in dotenv_values(".env").items()}
    except Exception:  # noqa: BLE001 - absent/unreadable dotenv is no credential source
        return {}


def _ambient_credential() -> _AmbientCredential:
    """Resolve process env pair, else dotenv pair, without cross-source field merging."""
    names = {
        "key": SHARED_KEY_ENV,
        "binding": SHARED_BINDING_ENV,
    }
    process = {str(name).upper(): str(value) for name, value in os.environ.items()}
    if any(name in process for name in names.values()):
        key, binding = process.get(names["key"], ""), process.get(names["binding"], "")
        shadowed: tuple[str, ...] = ()
        if bool(key) != bool(binding):
            # ONLY on the failing path, so the happy path keeps reading exactly one source. "I set
            # the key in my shell and left its binding in .env" is the single most common way to
            # reach this refusal, and the dropped half is invisible to the operator — the `.env`
            # they are looking at plainly contains it. Say that it was dropped, and why.
            missing = names["binding"] if key else names["key"]
            shadowed = (missing,) if _dotenv_pair().get(missing) else ()
        return _AmbientCredential(key, binding, _SOURCE_PROCESS, shadowed)
    dotenv = _dotenv_pair()
    if any(name in dotenv for name in names.values()):
        return _AmbientCredential(dotenv.get(names["key"], ""), dotenv.get(names["binding"], ""),
                                  _SOURCE_DOTENV, ())
    return _AmbientCredential("", "", "", ())


def _ambient_shared_pair() -> tuple[str, str]:
    """The historical 2-tuple view of `_ambient_credential` — ONE resolution, two shapes.

    Kept because the pair-without-provenance is what most readers want, and because a second
    implementation of the tier rule is exactly the drift this module's other shared definitions
    (`pathsafe`, `jsonutil`) are factored to avoid.
    """
    resolved = _ambient_credential()
    return resolved.key, resolved.binding


def _atomic_pair_rule(key_env: str, binding_env: str, *, tiered: bool) -> str:
    """The one sentence explaining WHY a half-override is refused instead of being completed.

    Every credential refusal below ends up quoting this, because in every one of them the operator's
    next question is the same: "the other half is right there, why did you not use it?". `tiered` is
    the shared pair, which reselects one whole source; a profile pair is read from the environment
    only and has no lower tier to be surprised by.
    """
    return (
        f"{key_env} and {binding_env} are ONE atomic credential: the key is usable only at the "
        "endpoint it is bound to, and LoopLab refuses rather than putting a secret issued for one "
        "host into an Authorization header aimed at another."
        + (f" They are reselected from a SINGLE source ({_SOURCE_PROCESS} first, else "
           f"{_SOURCE_DOTENV}), so overriding one half does not merge with the other half from a "
           "lower source — it replaces the pair." if tiered else ""))


def incomplete_pair_refusal(*, key_env: str, binding_env: str, have_key: bool, source: str,
                            shadowed: bool = False, tiered: bool = True) -> str:
    """Name the half-override behind ``bool(key) != bool(binding)``, and the exact fix.

    Hoisted and named (CLAUDE.md "make the rule statable") because the sentence the operator reads is
    the entire product of this refusal, and it is decided by inputs no call site reaches directly:
    WHICH half is present, WHICH source won, and whether the losing source still holds the other
    half. `tests/test_credential_diagnosis.py` pins the truth table.

    It leads with the ACTION that caused the state, not the state. "An incomplete key+endpoint pair"
    is true and useless: it describes what LoopLab found, leaving the operator to work backwards to
    what they typed. What they typed is the only thing they can change.
    """
    present, missing = ((key_env, binding_env) if have_key else (binding_env, key_env))
    where = f" in {source}" if source else ""
    return (
        f"{present} was set without {missing}.\n"
        f"    set{where}: {present}\n"
        f"    missing there: {missing}"
        + (f"  — it IS set in {_SOURCE_DOTENV}, and was deliberately NOT merged in"
           if shadowed else "")
        + "\n    Why this is refused rather than completed for you: "
        + _atomic_pair_rule(key_env, binding_env, tiered=tiered)
        + f"\n    Fix: set BOTH {key_env} and {binding_env}{where}, with {binding_env} equal to the "
          f"exact endpoint {key_env} was issued for"
        + (f";\n         or unset {present}{where} and let the complete pair from the lower source "
           "apply as a whole." if tiered else "."))


def misbound_credential_refusal(*, key_env: str, binding_env: str, target: str, binding: str,
                                source: str, endpoint_knob: str = "", tiered: bool = True) -> str:
    """Name the OTHER half-override — the endpoint moved and the credential stayed behind.

    The `bound != target` refusal. Same defect as `incomplete_pair_refusal`, reached from the other
    direction and by far the more common one: pointing a run at a different endpoint is a one-word
    change, and nothing about it suggests the key has to move too. Says which endpoint the request
    would go to, which one the key is for, which knob moved it, and which variables to set together.
    """
    moved = (f"{endpoint_knob} was overridden without its credential." if endpoint_knob
             else f"The endpoint was overridden without its credential ({SHARED_ENDPOINT_ENV}, "
                  "`-s llm_base_url=...`, or `llm_base_url` in a config file / .env).")
    return (
        f"{moved}\n"
        f"    this target would call: {target}\n"
        f"    but {key_env} (from {source}) is bound to: {binding}\n"
        f"    Why this is refused rather than retargeted for you: "
        + _atomic_pair_rule(key_env, binding_env, tiered=tiered)
        + f"\n    Fix: move the credential with the endpoint — set {key_env} and {binding_env} "
          f"together in one\n         source, with {binding_env}={target}. If {target} takes no "
          "key, unset both.")


def bound_api_key_for(settings, base_url: str, *, api_key=None,
                      api_key_base_url: str | None = None) -> str:
    """Resolve a key for ``base_url`` and fail before transport on an unbound/mismatched secret."""
    target = normalize_llm_base_url(base_url)
    if api_key is NO_CREDENTIAL:
        return "x"
    shadowed: tuple[str, ...] = ()
    if api_key is None:
        if getattr(settings, "_llm_credential_pair_trusted", False):
            key = _secret_value(getattr(settings, "llm_api_key", None))
            binding = getattr(settings, "llm_api_key_base_url", None)
            source = _SOURCE_SETTINGS
        else:
            # Never infer provenance from already-merged Settings fields. Plain Settings instances
            # may have combined init/file/env/dotenv values; reselect one atomic ambient tier here.
            key, binding, source, shadowed = _ambient_credential()
            source = source or _SOURCE_PROCESS   # nothing declared: still the tier we would read
    else:
        key = _secret_value(api_key)
        binding = api_key_base_url
        source = _SOURCE_SUPPLIED
    if bool(key) != bool(binding):
        raise LLMCredentialError(incomplete_pair_refusal(
            key_env=SHARED_KEY_ENV, binding_env=SHARED_BINDING_ENV, have_key=bool(key),
            source=source, shadowed=bool(shadowed)))
    if not key:
        return "local"
    if not binding:
        raise LLMCredentialError(
            "LLM credential is unbound; set its exact endpoint binding before network use")
    try:
        bound = normalize_llm_base_url(binding)
    except LLMError as exc:
        raise LLMCredentialError(
            f"{SHARED_BINDING_ENV} (from {source}) is not a usable endpoint: {exc}. It must be the "
            f"exact absolute http(s) endpoint {SHARED_KEY_ENV} was issued for — the value is "
            "compared against the endpoint the request would go to, so it cannot be approximate."
        ) from exc
    if bound != target:
        # `resolve_llm_target` hands this function a "shared" target only when the endpoint IS
        # `settings.llm_base_url`, so the endpoint that moved is always the shared one. Name the env
        # var when it is demonstrably the knob that moved it; otherwise the refusal enumerates the
        # spellings that could have, rather than guessing one and sending the operator to the wrong file.
        moved_by_env = normalize_llm_base_url_or_none(os.environ.get(SHARED_ENDPOINT_ENV)) == target
        raise LLMCredentialError(misbound_credential_refusal(
            key_env=SHARED_KEY_ENV, binding_env=SHARED_BINDING_ENV, target=target, binding=bound,
            source=source, endpoint_knob=SHARED_ENDPOINT_ENV if moved_by_env else ""))
    return key

# Provider usage is untrusted JSON.  A signed 64-bit ceiling is far above any real context/call
# count, remains exactly representable by the durable integer stores used by LoopLab, and prevents a
# hand-written/hostile ``10**400`` value from turning every later roll-up into an enormous bigint.
_MAX_USAGE_TOKENS = (1 << 63) - 1
_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def cost_is_reported(value) -> bool:
    """Did the provider actually STATE an amount for this call? (doc 25 COST-01)

    UNPRICED IS NOT FREE. `_safe_cost` degrades every absent/malformed/unusable amount to `0.0`,
    which is also the amount a genuinely free call reports — so after that one conversion the two
    facts are indistinguishable, and a run priced by nobody rolls up as a run that cost nothing.
    Measured on this deployment: `runs/rubert-dr-0805`, 354 calls and 11,616,993 tokens through a
    gateway that reports no `usage.cost`, presented as `$0`. Worse, `runs/rubert-dr-0804` is MIXED —
    104 unpriced calls (4,102,497 tokens) followed by 209 priced ones — so its `$8.26` looked like a
    complete invoice while a third of its tokens were never priced at all. That mixture is why this
    is a per-call COUNTER rather than a "did the total come out zero" heuristic at the read side.

    The acceptance test is exactly `_safe_cost`'s and is stated here ONCE so the two cannot drift:
    any value this rejects is a value `_safe_cost` turns into a zero that must not be read as free.
    """
    if value is None or isinstance(value, (bool, str, bytes, bytearray)):
        return False
    try:
        cost = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(cost) and cost >= 0.0


def inferred_priced_calls(cost: float, calls: int) -> int:
    """Priced-call count for a record whose writer could not state one. Stated ONCE (doc 25 COST-01).

    Two callers, both holding a record that predates the counter: a legacy/third-party accountant
    with no `priced_calls` attribute (`engine/costs.py::_snapshot`) and a usage row written before
    the field existed (`events/replay.py::_row_priced_calls`). Neither constant default works — 0
    reports every historical run with a real invoice as unpriced, `calls` reports the unpriced ones
    as fully priced — so use the only evidence such a record carries: a nonzero amount IS the
    provider having stated one. Inputs must already be sanitized.
    """
    return int(calls) if float(cost) > 0.0 else 0


def _safe_cost(value) -> float:
    """Return a cost only when it is a finite, non-negative numeric value.

    Cost is budget-enforcement input, not merely telemetry. Strings and booleans are malformed,
    while NaN/Infinity can poison comparisons and roll-ups. Decimal-like numeric values remain
    valid for internal gateways such as LiteLLM. Every rejected/absent value degrades to the
    local-model default of zero instead of crashing a completed LLM call or reducing spend.
    """
    if not cost_is_reported(value):
        return 0.0
    return 0.0 if float(value) == 0.0 else float(value)  # canonicalize provider ``-0`` as zero


def _usage_cost_reported(usage) -> bool:
    """Whether an OpenRouter-style payload carries a usable ``usage.cost`` at all."""
    value = usage.get("cost") if isinstance(usage, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return cost_is_reported(value)


def _usage_cost(usage) -> float:
    """Extract OpenRouter-style JSON ``usage.cost`` without trusting provider payload types."""
    if not _usage_cost_reported(usage):
        return 0.0
    return _safe_cost(usage.get("cost"))


def _safe_token_count(value) -> int:
    """Canonicalize one provider token count without coercion.

    JSON booleans, numeric-looking text and integral floats are deliberately not integers here.
    Negative and beyond-int64 values are corrupt telemetry and degrade to zero.
    """
    if type(value) is not int:
        return 0
    return value if 0 <= value <= _MAX_USAGE_TOKENS else 0


def _normalize_usage(usage) -> dict[str, int | float]:
    """Return the one bounded usage shape consumed by accounting, tracing and UI telemetry.

    The input is copied before any field is inspected so a surprising dict subclass cannot expose a
    half-read mutable view.  Every field is normalized before callers mutate accountant state.  An
    absent/invalid/internally contradictory total retains the historical prompt+completion
    fallback, saturated at the same signed-int64 ceiling. A provider total smaller than its two
    components is corrupt telemetry, not an independently trustworthy counter.
    """
    try:
        raw = dict(usage) if isinstance(usage, dict) else {}
    except Exception:  # noqa: BLE001 - provider telemetry must never break a completed response
        raw = {}
    prompt = _safe_token_count(raw.get("prompt_tokens"))
    completion = _safe_token_count(raw.get("completion_tokens"))
    marker = object()
    reported_total = raw.get("total_tokens", marker)
    component_total = min(_MAX_USAGE_TOKENS, prompt + completion)
    if (reported_total is not marker and type(reported_total) is int
            and component_total <= reported_total <= _MAX_USAGE_TOKENS):
        total = reported_total
    else:
        total = component_total
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cost": _usage_cost(raw),
        # `priced` travels WITH the laundered cost because this is the last place the difference
        # still exists: below here `cost` is 0.0 whether the provider said "$0" or said nothing at
        # all (`cost_is_reported`). An int rather than a bool so it flows through every downstream
        # counter sanitizer (`sanitize_usage_delta`, `replay._llm_counter`) unchanged.
        "priced": _normalized_priced(raw),
    }


def _normalized_priced(raw: dict) -> int:
    """The priced marker for one payload, keeping `_normalize_usage` IDEMPOTENT.

    Deriving it from `raw["cost"]` alone is wrong on the second pass: `_post` normalizes the body
    and then hands that SAME dict back through `add` -> `_normalize_usage`, where the laundered
    `0.0` of an unpriced call looks exactly like a provider that reported zero — every unpriced call
    came back marked priced. An already-marked payload is our own output, so its marker wins.

    A provider is out of contract if it sends `priced` itself, and gets bounded to 0/1 here. That is
    not new exposure: it already controls `usage.cost`, which is the same claim by another name.
    """
    marker = raw.get("priced")
    if isinstance(marker, int) and not isinstance(marker, bool):
        return 1 if marker else 0
    return 1 if _usage_cost_reported(raw) else 0


def _call_is_priced(cost, usage, normalized: dict) -> bool:
    """Did THIS `CostAccountant.add` see a stated amount? One rule, stated once (doc 25 COST-01).

    Two inputs claim to carry the price and only one of them is trustworthy at a time:

      * `normalized["priced"]` — minted by `_normalize_usage` from the RAW payload, so it is the
        only witness that survives the laundering. Authoritative whenever the payload had a cost.
      * the `cost` ARGUMENT — independent evidence ONLY when the supplied payload carries no `cost`
        key of its own. Every OpenAI-compatible call site forwards `usage["cost"]` taken from a dict
        `_normalize_usage` produced — always present, laundered to `0.0` when the provider reported
        nothing — so reading the argument there would mark every unpriced call priced. A payload
        with no `cost` key cannot be that laundered value, which leaves the argument meaning what it
        says: LiteLLM's out-of-band `_hidden_params.response_cost`, or a bare `add(0.05)`.
    """
    if normalized.get("priced"):
        return True
    carries_cost = isinstance(usage, dict) and "cost" in usage
    return not carries_cost and cost_is_reported(cost)


def _stream_envelope_is_billable(*, usage_observed: bool, delegated_to_fallback: bool,
                                 stream_completed: bool, produced_content: bool) -> bool:
    """Does `complete_text_stream`'s own envelope go into the durable ledger? (doc 25 CO-04)

    Three inputs, one money rule, stated once so it can be read and tested as a rule rather than
    inferred from a boolean expression buried in a `finally`:

      * `usage_observed` — the provider REPORTED usage for this envelope, so it is real spend
        whatever happened afterwards. Wins over everything else, including a later delegation:
        `_account_keepalive_stall` exists because the mirror-image omission (a billable envelope
        that was never recorded) drifted a run's recorded spend arbitrarily far BELOW the invoice.
      * `delegated_to_fallback` — this stream handed the answer to `complete_text`, which makes its
        OWN provider call and accounts for it. Charging the abandoned envelope on top of that is the
        opposite drift: recorded spend above the invoice. No site reaches this row today (all three
        delegations sit under `if not pieces:`, so `produced_content` is False there anyway) — it is
        the guard for the FOURTH site, one that delegates after already yielding content.
      * `stream_completed` / `produced_content` — a clean stream is one logical call even when usage
        is absent, and once content was yielded a consumer close/cancel still records the call it
        made (unknown cost/tokens stay zero rather than pretending it was free).
    """
    if usage_observed:
        return True
    if delegated_to_fallback:
        return False
    return stream_completed or produced_content


def _stream_usage(value) -> dict:
    """Best-effort mapping extraction for an SDK streaming usage object."""
    if isinstance(value, dict):
        return value
    try:
        dumped = value.model_dump()
    except Exception:  # noqa: BLE001 - malformed optional telemetry is not a transport failure
        return {}
    return dumped if isinstance(dumped, dict) else {}


def reasoning_body(model: str, mode: str = "", style: str = "auto",
                   extra: Optional[dict] = None) -> dict:
    """The provider-specific request fields that TOGGLE a reasoning/thinking model — providers differ:
      - Qwen3 on vLLM/SGLang: `chat_template_kwargs.enable_thinking` (bool)
      - OpenAI / Ollama-v1 / DeepSeek: `reasoning_effort` (low|medium|high|none)
    `mode`: "" = inject nothing (use the server default — unchanged behavior); off|none = disable;
    on = enable at default depth; low|medium|high = enable at that effort. `style`: auto picks `qwen`
    for qwen* models else `effort`. `extra` is merged last (escape hatch, e.g. Anthropic
    `{"thinking": {"type": "enabled", "budget_tokens": N}}`)."""
    mode = (mode or "").strip().lower()
    body: dict = {}
    if mode:
        st = (style or "auto").lower()
        if st == "auto":
            st = "qwen" if "qwen" in (model or "").lower() else "effort"
        on = mode not in ("off", "none", "false", "0")
        if st == "qwen":
            body["chat_template_kwargs"] = {"enable_thinking": on}
        elif st == "effort":
            # OpenAI/OpenRouter accept only low|medium|high — "none" 400s. To DISABLE on an
            # effort-style provider, send nothing (server default); rely on `extra` for a provider
            # that has an explicit off switch (e.g. OpenRouter `{"reasoning": {"enabled": false}}`).
            if on:
                body["reasoning_effort"] = "medium" if mode == "on" else mode
        # st == "none": shape nothing (rely solely on `extra`)
    if extra:
        body = {**body, **extra}
    return body


class _ResponseCache:
    """T7: the bounded LRU of DETERMINISTIC (temperature-0) response bodies (doc 25 CO-05).

    The map, its bound and the lock that pairs them used to be three loose attributes on the client,
    so every reader had to know that `_cache_lock` guards the read-modify-write — a hit's
    `move_to_end` and a put's eviction are each a PAIR of OrderedDict ops, and OrderedDict's
    individual ops being atomic does not make a pair atomic. Worker threads share one client.

    Deep copies live here rather than at the call sites for the same reason: callers mutate the body
    they are handed (`complete_text` -> `_apply_native_tool_calls`), so an entry shared by reference
    is corrupted for every later hit.
    """

    __slots__ = ("_entries", "_lock", "max_entries")

    def __init__(self, max_entries: int = 256):
        self._entries: "OrderedDict[str, dict]" = OrderedDict()
        self._lock = threading.Lock()
        self.max_entries = max_entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._entries

    def __iter__(self):
        with self._lock:
            return iter(list(self._entries))

    def get(self, key: str) -> Optional[dict]:
        """A DEEP COPY of the entry, most-recently-used, or None."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)       # recency: this key survives the next eviction
            return copy.deepcopy(entry)

    def put(self, key: str, body: dict) -> None:
        """Store a COPY and evict the least recently used down to the bound."""
        copied = copy.deepcopy(body)
        with self._lock:
            self._entries[key] = copied
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def peek(self, key: str) -> Optional[dict]:
        """The stored entry itself, without a copy or a recency bump — for tests/introspection that
        need to prove the STORED value was not mutated by a caller's copy."""
        with self._lock:
            return self._entries.get(key)


class OpenAICompatibleClient:
    """OpenAI-compatible chat client on the openai SDK (httpx transport). Implements the
    `parse.LLMClient` Protocol, so it drops into the LLM roles like any other backend.

    Works against ANY OpenAI-compatible endpoint — Ollama (`/v1`), SGLang, vLLM, or
    the OpenAI API itself — so the serving backend is a base_url change, not code. The
    SDK's per-read httpx timeout is what reliably bounds a stalled mid-stream read (the
    stdlib-urllib transport this replaced could not interrupt one). This is the backend
    selected by the shipped Settings factory; `LiteLLMClient` remains an optional adapter."""

    def __init__(self, model: str, base_url: str = "http://localhost:11434/v1",
                 api_key: str = "ollama", temperature: float = 0.7,
                 timeout: float = 180.0, accountant: Optional["CostAccountant"] = None,
                 guided_json: bool = False, reasoning: Optional[dict] = None,
                 stream: bool = True, cache: bool = False,
                 header_timeout: Optional[float] = None, trust_env: bool = False,
                 max_retries: int = 8, wall_timeout: Optional[float] = None,
                 retry_after_cap: Optional[float] = None):
        # The live transport needs the openai SDK + httpx. They are declared deps, but the module
        # import is guarded (offline/replay import-safety), so fail with a clear, actionable message
        # here rather than an opaque `NoneType has no attribute 'OpenAI'` if someone stripped them.
        if openai is None or httpx is None:
            raise LLMError("the live LLM path needs the 'openai' and 'httpx' packages — "
                           "`pip install looplab` pulls them in (or `pip install openai httpx`)")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "x"
        self.temperature = temperature
        self.timeout = timeout
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        self._max_retries = max_retries
        if wall_timeout is not None:
            wall_timeout = float(wall_timeout)
            if not math.isfinite(wall_timeout) or wall_timeout <= 0:
                raise ValueError("wall_timeout must be a positive finite number")
        # Optional whole-attempt guard for short, non-streaming probes. Ordinary generation keeps
        # the historical timeout+header+cleanup window; streaming remains governed by its idle guards.
        self.wall_timeout = wall_timeout
        # The largest SERVER-DIRECTED wait (`Retry-After`) this client will absorb, for a caller that
        # has its own wall-clock budget. `None` = the historical behaviour: clamp every directive to
        # the module-wide RETRY_AFTER_CAP_S and sleep it. When SET, a directive above the cap ends the
        # retries instead of being clamped — the same "refuse out-of-range rather than clamp" rule
        # `engine/widths.py` settles concurrency widths by, and for the same reason. Clamping a budget
        # is silent: the endpoint preflight advertises a 60 s bound and, measured against the team
        # endpoint's real `Retry-After: 60`, sat SILENT for 121.4 s (2 retries x 60 s) before refusing.
        # Sleeping through a reset window also buys nothing at a preflight — a rate limit that is
        # minutes wide is the ANSWER, not a blip to ride out, and the refusal quotes the server's own
        # number so the operator learns the wait instead of serving it.
        if retry_after_cap is not None:
            retry_after_cap = float(retry_after_cap)
            if not math.isfinite(retry_after_cap) or retry_after_cap <= 0:
                raise ValueError("retry_after_cap must be a positive finite number")
        self._retry_after_cap = retry_after_cap
        # `header_timeout` bounds the TCP/TLS CONNECT (httpx `connect=`, see `_new_sdk`), so a connection
        # that never ESTABLISHES fails over fast instead of waiting the full idle `timeout`. It ALSO bounds
        # the wait for HTTP response HEADERS on the STREAM path: `create(stream=True)` runs under a wall-
        # clock worker-thread guard (`_bounded_create`, join = header_timeout + min(10s, header_timeout) of
        # slack), so an endpoint that completes TLS then sends no headers is failed over within ~10s of
        # `header_timeout` — not after the full idle `timeout` (the black-holed-request hang this used to
        # have, when the header-read was left to httpx's `read` timeout). Once headers arrive,
        # `_accumulate_stream` reuses `header_timeout` as the first-SSE-EVENT budget (and the idle
        # `timeout` for the inter-token body).
        # The bounded streaming Response is created in the worker and iterated on the caller thread: a sync-
        # httpx sequential handoff, no concurrent access.
        _ht = DEFAULT_HEADER_TIMEOUT_S if header_timeout is None else float(header_timeout)
        self.header_timeout = min(_ht, timeout) if timeout else _ht   # never exceeds the idle timeout
        # Stream EVERY request (SSE) and reassemble it, so `timeout` acts as an INTER-TOKEN idle
        # timeout, NOT a whole-request deadline: a long-but-alive generation keeps streaming tokens
        # (each resets the timer, so it's never cut off), while a genuinely STALLED endpoint (no data
        # for `timeout` s) trips the socket read timeout and is retried on a fresh connection. This is
        # what stops the "27-minute hang" without ever capping a slow reasoning model. Opt out
        # (stream=False) to use one blocking read (subject to the old per-op timeout semantics).
        self.stream = stream
        self.accountant = accountant or CostAccountant()
        # Provider-specific reasoning toggle (from `reasoning_body`) merged into EVERY request, so the
        # whole agent loop (propose/chat/tool) runs with the same thinking setting. Empty = unchanged.
        self.reasoning = reasoning or {}
        # Flips to False permanently for this client the first time the endpoint rejects our reasoning
        # toggle with a 400 (e.g. litellm UnsupportedParamsError for reasoning_effort on glm-5.1), so
        # the request is retried without it and the model works. Deepseek keeps its reasoning; glm-5.1
        # silently drops it. Per-client (per-model), detected once and cached.
        self._reasoning_ok = True
        # Same shape as `_reasoning_ok`, for the OPTIONAL `stream_options: {"include_usage": true}`
        # capability: flips off permanently for this client the first time the endpoint 400s naming
        # that field, so streaming keeps working (without provider-reported usage) instead of the
        # client dying against that endpoint entirely. Per-client, detected once and cached.
        self._stream_options_ok = True
        # 429/5xx/throttle-403 backoff retries before surfacing an LLMError. The normal default is 8;
        # explicit probes can set zero so one user action produces at most one provider request.
        # 8 (≈150s: 2+4+8+16+30+30+30+30) rides out the gateway's COLD-START throttle: after
        #   the engine sits idle (e.g. paused), the FIRST call-burst on resume gets a 403 "security
        #   policy" throttle for up to ~2min, then clears (measured: 1st call 63s, next 11 instant). At 4
        #   (~30s) or even 6 (~90s) that first node developer-crashed → the run auto-paused → resumed →
        #   hit the cold throttle AGAIN: a pause/resume loop. Riding it out WITHIN the node (one node =
        #   one experiment) breaks the loop. A genuine persistent outage just surfaces ~2min later, then
        #   the circuit-breaker pauses — no spam either way.
        # Stall-degrade: a shared/proxied endpoint can stall MID-STREAM on big (code-gen) requests
        # while answering the same request fine without SSE (observed on glm-5.1: non-stream 2s vs a
        # stream that hangs until the watchdog kills it). After a stream stall the NEXT attempt of
        # that call goes non-streaming; after STREAM_STALL_DEGRADE_AFTER stalls streaming is disabled
        # for this client's lifetime. Bounded worst case: one idle-timeout, not retries ×
        # idle-timeout of silence.
        self._stream_stalls = 0
        # H1: when the endpoint supports constrained decoding (vLLM/SGLang), drive structured calls
        # from the Pydantic JSON schema — `response_format` json_schema (OpenAI-standard, vLLM+SGLang)
        # + `guided_json` (vLLM extra) — so a weak model can't emit invalid JSON. Off by default
        # (Ollama needs no constraint and some builds reject unknown fields).
        self.guided_json = guided_json
        # T7: in-process content-addressed response cache for DETERMINISTIC (temperature 0) calls
        # only. None = disabled (default). Never caches sampling calls (temp>0) — those must vary.
        # LRU-BOUNDED: a client is shared across a whole run (researcher+developer+monitors), so an
        # unbounded map of deep-copied response bodies grew for the process's lifetime — code-gen
        # bodies are tens of KB each, and nothing ever evicted them. The cache exists to catch the
        # NEAR-TERM repeats (a retry, a panel re-ask, a verify pass re-issuing the same prompt);
        # those all land well inside a few hundred entries, so a recency bound costs no realistic
        # hit rate. The bound, the lock pairing each read-modify-write, and the deep copies live in
        # `_ResponseCache` above (doc 25 CO-05).
        self._cache: Optional[_ResponseCache] = _ResponseCache() if cache else None
        # Transport: the openai SDK over an httpx client. `connect` bounds TCP/TLS establishment
        # (=header_timeout); `read` = the inter-read idle limit (a long-but-alive generation keeps
        # resetting it, so it's never cut off). httpx's `read` timeout can't catch an SSE
        # keepalive-trickle (bytes reset it while no data EVENT arrives) — that is enforced by
        # `_accumulate_stream`'s idle/first-event guard on the STREAM path (idle_limit=timeout;
        # first_byte_limit=header_timeout bounds the first EVENT AFTER headers; the header-READ itself is
        # bounded separately by `_bounded_create`'s worker-thread guard — see the `header_timeout` note
        # above). The per-request timeout lives on the OpenAI client
        # (`timeout=`), which wins over the http_client's; the http_client exists only to set
        # `trust_env=False` (the internal endpoint needs a DIRECT connection — no proxy env). See
        # `llm_trust_env` if a proxy/custom-CA is required. max_retries=0: we own the retry loop.
        self._trust_env = trust_env
        # In-flight `_bounded_create` count. The abort path below shuts down EVERY socket in the shared
        # httpx pool and rebuilds `self._sdk`, which is correct when this call is the only user but
        # catastrophic when it is not: ONE client instance is shared by researcher+developer
        # (adapters/tasks.py) and reused by the train/ASHA monitors from worker threads, so a pool-wide
        # teardown aborts healthy multi-minute generations belonging to other threads and makes them
        # re-spend. Counting lets the teardown stay scoped to the safe case.
        self._inflight_lock = threading.Lock()
        self._inflight = 0
        # Streams whose BODY is still being read. `_inflight` cannot see them: for stream=True
        # `create()` returns at HEADERS, so `_call`'s finally decrements while the caller thread is
        # still minutes from finishing the generation on the SAME pooled connection. `_alone` then
        # saw a healthy sibling as absent and tore the pool down under it — the exact failure it was
        # added to prevent, in the configuration (streaming, the DEFAULT) its comment calls normal.
        self._stream_inflight = 0
        self._sdk = self._new_sdk()

    def _new_sdk(self):
        """Build (or rebuild) the openai SDK client. Rebuilt after a bounded-non-stream abort closes
        the httpx client to interrupt a trickled body read (a closed client is unusable afterwards)."""
        return openai.OpenAI(
            base_url=self.base_url, api_key=self.api_key, max_retries=0,
            timeout=httpx.Timeout(read=self.timeout, connect=self.header_timeout, write=30.0, pool=10.0),
            # Never forward an Authorization header across an HTTP redirect. Endpoint changes are
            # explicit configuration changes and must pass the credential-binding guard again.
            http_client=httpx.Client(trust_env=self._trust_env, follow_redirects=False))

    def _header_join(self) -> float:
        """The wall-clock budget a streaming `create()` gets to produce RESPONSE HEADERS.

        `header_timeout` plus at most 10s of slack, so a small `header_timeout` still fails over
        fast. Deliberately NOT the idle `timeout` (which can be ~180s): an endpoint that accepts the
        socket, TLS-handshakes and then never sends headers must fail over near `header_timeout`,
        not minutes later. Both streaming entry points bound their create on it, and they used to
        compute it separately (doc 25 CO-04) — two expressions for one budget is how one of them
        ends up loosened alone.
        """
        return self.header_timeout + min(10.0, self.header_timeout)

    def _sdk_chat(self, payload: dict, use_stream: bool) -> dict:
        """The single transport seam: one openai-SDK chat call, returned in the legacy body shape
        ({"choices":[{"message":{content,reasoning?,tool_calls?},"finish_reason"}],"usage"}) the rest
        of the client expects. Non-provider params (a reasoning toggle, vLLM `guided_json`) ride in
        `extra_body`. Streaming accumulates deltas exactly like the old `_read_stream`; a stalled
        stream raises openai.APITimeoutError from httpx's read timeout — no watchdog needed. Tests
        monkeypatch THIS method (not urllib) to script transport behaviour."""
        kwargs: dict = {"model": payload["model"], "messages": payload["messages"],
                        "temperature": payload.get("temperature", self.temperature)}
        if payload.get("max_tokens") is not None:
            kwargs["max_tokens"] = payload["max_tokens"]
        if payload.get("tools"):
            kwargs["tools"] = payload["tools"]
            kwargs["tool_choice"] = payload.get("tool_choice", "auto")
        if payload.get("response_format"):
            kwargs["response_format"] = payload["response_format"]
        extra: dict = {}
        if self.reasoning and self._reasoning_ok:     # provider reasoning toggle (non-standard params)
            extra.update(self.reasoning)
        if payload.get("guided_json"):                # vLLM constrained-decoding extra
            extra["guided_json"] = payload["guided_json"]
        if extra:
            kwargs["extra_body"] = extra
        if use_stream:
            kwargs["stream"] = True
            # `stream_options` is an OPTIONAL OpenAI-compatible capability (it asks the endpoint to
            # report token usage on the final SSE chunk). Sending it unconditionally meant a provider
            # that rejects ONLY this field 400d identically on every retry — and the blocking text
            # fallback re-entered this same builder — so the client was dead against that endpoint.
            # `_stream_options_ok` degrades it exactly like `_reasoning_ok` does the reasoning toggle:
            # once off, streaming keeps working, just without provider-reported usage.
            if self._stream_options_ok:
                kwargs["stream_options"] = {"include_usage": True}
            # Bound the header-WAIT. The static httpx.Timeout treats `header_timeout` as connect-only, so
            # an endpoint that completes TLS then never sends response HEADERS would block create() up to
            # the idle `timeout` (~180s) before failover — the "black-holed request" this design claims to
            # fail over fast. Run create() under the SAME wall-clock guard `_nonstream_bounded` uses, keyed
            # on the header budget (header_timeout + min(10s, header_timeout) of slack, so a small
            # header_timeout still fails over fast); it returns as soon as headers arrive, and
            # `_accumulate_stream`'s idle watchdog then governs the body. Iterating that streaming Response
            # on THIS thread after a worker thread created it is safe: sync-httpx sequential handoff, no
            # concurrent access.
            header_join = self._header_join()
            # ONE slot held CONTINUOUSLY across the header wait AND the body, taken here and passed
            # down as `counted=True` so `_bounded_create` does not add a second.
            #
            # Both simpler arrangements are wrong. Nesting with `_bounded_create` doing its own
            # `_inflight += 1` made a wedged call count ITSELF twice (`_inflight` + `_stream_inflight`),
            # so `_pool_teardown_is_safe_locked` saw a phantom sibling and the call skipped the very
            # teardown it needed. Un-nesting fixed that but opened a handoff window: `_bounded_create`
            # returns only after its worker has already decremented `_inflight`, and `_streaming_body`
            # had not yet incremented `_stream_inflight`, so a healthy header-complete stream was
            # counted by NEITHER cell — long enough for a sibling wedged in its own header wait to see
            # `<= 1`, judge the teardown safe, and have `_shutdown_pool_sockets` rip the socket out
            # from under this stream (spurious APITimeoutError plus a re-spend), which is precisely the
            # failure the counting exists to prevent. Holding the slot the whole time has neither
            # problem: the self-count stays exactly 1, and it never drops to 0 while a socket is live.
            with self._streaming_body():
                _stream = self._bounded_create(kwargs, header_join, counted=True)
                try:
                    return self._accumulate_stream(_stream, self.timeout, self.header_timeout)
                finally:
                    # Own-and-close, like `complete_text_stream` (see its note there). The watchdog
                    # kill closes the response itself, but every OTHER exit — the raw-httpx-error
                    # normalization in `_stream_with_idle_guard`, any openai.APIError mid-iteration —
                    # left the SDK Stream and its pooled socket to leak until GC.
                    _close = getattr(_stream, "close", None)
                    if callable(_close):
                        try:
                            _close()
                        except Exception:  # noqa: BLE001 — a close failure must not mask the result
                            pass
        return self._nonstream_bounded(kwargs)

    def _nonstream_bounded(self, kwargs: dict) -> dict:
        """A NON-STREAM chat call: no SSE loop guards a TRICKLED body (a byte resets httpx's read timer
        while the payload never completes), so bound the WHOLE call (headers + body) via `_bounded_create`
        at the explicit wall guard or the historical timeout+header window, then serialize it."""
        join_s = (self.wall_timeout if self.wall_timeout is not None
                  else self.timeout + self.header_timeout + 10)
        return self._bounded_create(kwargs, join_s).model_dump()

    def _pool_teardown_is_safe_locked(self) -> bool:
        """May this wedged call tear down the SHARED client? Caller holds `_inflight_lock`.

        Counts stream BODIES as well as header waits: a sibling past its headers still owns a pooled
        connection, and `_shutdown_pool_sockets` + rebuilding `self._sdk` would kill its live
        generation. Over-counting is the safe direction — it only skips the teardown more often,
        which the abort path already accepts (it tolerates one lingering daemon instead).
        """
        return (self._inflight + self._stream_inflight) <= 1

    @contextmanager
    def _streaming_body(self):
        """Hold `_stream_inflight` while a stream's BODY is read on THIS thread.

        `_bounded_create`'s `_inflight` covers only the header wait — its worker returns the Stream the
        moment headers land. Everything after that runs here, on the caller thread, with the pooled
        connection still in use, so the abort path has to be able to see it.
        """
        with self._inflight_lock:
            self._stream_inflight += 1
        try:
            yield
        finally:
            with self._inflight_lock:
                self._stream_inflight -= 1

    def _bounded_create(self, kwargs: dict, join_s: float, *, counted: bool = False):
        """Run one `chat.completions.create(**kwargs)` in a worker thread bounded by a `join_s` wall-clock
        deadline; if it overruns, ABORT: `socket.shutdown()` the in-flight connection (forces a recv()
        wedged in the kernel to return — close() alone can't), close + rebuild the httpx client, and raise
        APITimeoutError so `_post` retries/degrades instead of hanging. The wall-clock join guarantees the
        CALLER unblocks, and the socket-shutdown guarantees the WORKER thread exits too (no lingering
        daemons accumulating across a long run on a flaky endpoint — the pre-shutdown behaviour that leaked
        ~one thread per wedged call). Returns the raw SDK result: a STREAM object when stream=True (its
        body is iterated on the caller thread — a sync-httpx streaming Response created in the worker and
        consumed sequentially elsewhere is safe), else the completed response. Shared by the streaming
        header-WAIT (join_s = the header budget; `_accumulate_stream` then governs the body) and the
        non-stream whole-call bound (join_s covers the trickled body too)."""
        box: dict = {}

        def _call():
            try:
                box["resp"] = sdk.chat.completions.create(**kwargs)
            except BaseException as e:  # noqa: BLE001 — ferry ANY error back to the caller thread
                box["exc"] = e
            finally:
                if not counted:
                    with self._inflight_lock:
                        self._inflight -= 1

        with self._inflight_lock:
            # `counted=True`: the CALLER already holds a slot (`_streaming_body`) covering this call
            # for its whole lifetime — header wait and body alike — so adding one here would make a
            # wedged stream count itself twice and forbid its own teardown. The `self._sdk` bind below
            # still happens under this lock either way; that is what the lock is doing here.
            if not counted:
                self._inflight += 1
            # Bind the client UNDER the same lock that counts this call in. The abort path below
            # publishes its replacement `self._sdk` while holding this lock too, so a call either
            # binds the doomed client BEFORE that check — and is therefore counted as a sibling, which
            # forbids the teardown — or binds the fresh one. Reading `self._sdk` inside `_call` left a
            # window where a call could pick up the client the teardown was about to shut down.
            sdk = self._sdk
        th = threading.Thread(target=_call, daemon=True)
        try:
            th.start()
        except BaseException:
            # `_call`'s finally is the ONLY decrement and never runs if the thread never started
            # (`RuntimeError: can't start new thread` under exhaustion). Leaking the counter would
            # pin `_alone` False for the process lifetime, disabling this abort path for every later
            # wedged call — and thread exhaustion is exactly when it is needed. Nothing to undo when
            # `counted`: the caller's `_streaming_body` owns that slot and releases it on the way out.
            if not counted:
                with self._inflight_lock:
                    self._inflight -= 1
            raise
        th.join(join_s)
        if th.is_alive():
            # Force the wedged recv() to return so the worker thread EXITS (doesn't linger): shutdown the
            # in-flight connection's socket BEFORE close() — close() can't interrupt a kernel read, only
            # socket.shutdown() can. Then close()+rebuild the client for the next call.
            # getattr guard (D8b): an SDK shape WITHOUT `_client` (a mock in tests, a foreign SDK)
            # must still reach the intended APITimeoutError below — a bare `self._sdk._client` here
            # turned the timeout into an AttributeError (`_shutdown_pool_sockets` no-ops on None).
            # ONLY when this wedged call is the sole user of the shared client. The teardown below is
            # pool-WIDE (`_shutdown_pool_sockets` shuts every pooled connection) and replaces `self._sdk`,
            # so running it with siblings in flight rips the socket out from under a healthy multi-minute
            # generation on another thread — one client instance is shared by researcher+developer
            # (adapters/tasks.py) and reused by the train/ASHA monitors, and streaming is the DEFAULT, so
            # those siblings are the normal case, not a corner. With siblings present we accept ONE
            # lingering daemon thread (it exits when its own read finally errors) rather than cause N
            # spurious failures and re-spends; the caller still unblocks on the APITimeoutError below,
            # which is the guarantee that actually matters.
            # The check and the SWAP happen under ONE hold of the lock. Reading `_alone` and then
            # releasing left a window in which a sibling could start, bind the doomed client and have
            # its fresh connection ripped out by the teardown below — reintroducing exactly the
            # spurious sibling failure the inflight counting exists to prevent — and `self._sdk =
            # self._new_sdk()` was an unsynchronized write racing those readers. Publishing the
            # replacement here instead means every later call binds the NEW client (see
            # `sdk = self._sdk` above), so the teardown can only ever touch connections belonging to
            # calls that were already counted when `_alone` said there were none.
            doomed = None
            with self._inflight_lock:
                if self._pool_teardown_is_safe_locked():
                    doomed, self._sdk = self._sdk, self._new_sdk()
            if doomed is not None:
                # Force the wedged recv() to return so the worker thread EXITS: shutdown before close() —
                # close() cannot interrupt a kernel read, only socket.shutdown() can.
                _shutdown_pool_sockets(getattr(doomed, "_client", None))
                try:
                    doomed._client.close()
                except Exception:  # noqa: BLE001
                    pass
                th.join(5)   # after the shutdown the recv errors out, so the daemon is reaped here
            raise openai.APITimeoutError(request=httpx.Request("POST", self.base_url))
        if "exc" in box:
            raise box["exc"]
        return box["resp"]                      # RAW SDK result; callers serialize/iterate as needed

    @staticmethod
    def _accumulate_stream(stream, idle_limit: float = 0.0, first_byte_limit: float = 0.0) -> dict:
        """Reassemble an SDK streaming response into the non-streaming body shape. Merges tool_call
        deltas by index (partial name/arguments concatenated), captures reasoning deltas, and keeps
        the final include_usage chunk. httpx's read timeout bounds each iteration — a stall surfaces
        as openai.APITimeoutError out of this loop, caught by `_post`."""
        content: list[str] = []
        reasoning: list[str] = []
        tcs: dict[int, dict] = {}
        finish = None
        usage: dict = {}
        for ev in _stream_with_idle_guard(stream, idle_limit, first_byte_limit):
            if getattr(ev, "usage", None):
                # Same tolerant extractor `complete_text_stream` uses: a provider (or a test mock)
                # whose final chunk carries `usage` as a PLAIN DICT has no `.model_dump()`, and the
                # AttributeError aborted the entire call over optional telemetry.
                usage = _stream_usage(ev.usage)
            if not ev.choices:
                continue
            ch = ev.choices[0]
            d = ch.delta
            if getattr(d, "content", None):
                content.append(d.content)
            r = getattr(d, "reasoning", None) or getattr(d, "reasoning_content", None)
            if r:
                reasoning.append(r)
            for tc in (getattr(d, "tool_calls", None) or []):
                tcd = tc.model_dump()               # reuse the tested index-merge logic (_tool_call_slot)
                idx = _tool_call_slot(tcs, tcd)     # provider-omitted `index` must not collapse calls
                slot = tcs.setdefault(idx, {"id": None, "type": "function",
                                            "function": {"name": "", "arguments": []}})
                if tcd.get("id"):
                    slot["id"] = tcd["id"]
                fn = tcd.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"].append(fn["arguments"])
            if ch.finish_reason:
                finish = ch.finish_reason
        msg: dict = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            msg["reasoning"] = "".join(reasoning)
        if tcs:
            msg["tool_calls"] = [
                {"id": s["id"] or f"call_{i}", "type": "function",
                 "function": {"name": s["function"]["name"], "arguments": "".join(s["function"]["arguments"])}}
                for i, s in sorted(tcs.items())]
        return {"choices": [{"message": msg, "finish_reason": finish}], "usage": usage}

    def _cache_key(self, payload: dict) -> Optional[str]:
        """T7: content-addressed key for a DETERMINISTIC request. Only temperature==0 calls are
        cacheable — a temperature>0 call is a SAMPLE and MUST vary (best-of-N, the researcher panel,
        and the novelty re-propose all depend on independent draws), so caching those would silently
        collapse the search's diversity. Returns None (uncacheable) unless the request is deterministic."""
        if self._cache is None or payload.get("temperature", self.temperature) not in (0, 0.0):
            return None
        import hashlib
        blob = json.dumps({"model": self.model, "messages": payload.get("messages"),
                           "tools": payload.get("tools"), "tool_choice": payload.get("tool_choice"),
                           "response_format": payload.get("response_format"),
                           "max_tokens": payload.get("max_tokens")},
                          sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _cache_get(self, ck: Optional[str]) -> Optional[dict]:
        """T7 cache READ — lookup, recency bump, and the hit's usage zeroing (doc 25 CO-05).

        `None` means "no usable entry": an uncacheable request (`ck is None`), caching disabled, or a
        miss. A hit is returned as a DEEP COPY, because downstream (e.g. complete_text ->
        _apply_native_tool_calls) mutates the message in place, which would otherwise corrupt the
        shared cached entry for every later hit.
        """
        if ck is None or self._cache is None:
            return None
        cached = self._cache.get(ck)              # already a deep copy, already recency-bumped
        if cached is None:
            return None
        # A cache hit performs no provider work. Zero every billed usage counter in this call's
        # copy: otherwise trace aggregation would duplicate both tokens and paid cost even though
        # CostAccountant correctly skips cache hits. The original cached body remains untouched.
        usage = _normalize_usage(cached.get("usage"))
        for field in _USAGE_FIELDS:
            usage[field] = 0
        usage["cost"] = 0.0
        usage["priced"] = 0   # no provider call, so nobody priced it — not "priced at $0"
        cached["usage"] = usage
        # Restore the per-call telemetry a live call would have set.
        self._last_usage = usage
        return cached

    def _cache_put(self, ck: Optional[str], body: dict) -> None:
        """T7 cache WRITE + LRU eviction (doc 25 CO-05). No-op when the request is uncacheable."""
        if ck is None or self._cache is None:
            return
        self._cache.put(ck, body)                # stores a COPY and evicts the least recently used

    # The retry-policy TABLE, keyed on exception type (doc 25 CO-05). An ordered list of
    # `(types, handler-name)` pairs rather than a dict, because both properties a dict would lose are
    # load-bearing: SUBCLASS dispatch (`APITimeoutError` must reach the `APIConnectionError` handler,
    # and `RateLimitError`/`InternalServerError` share one) and ORDER — the entries are tried
    # top-down, exactly as the `except` ladder they replaced. `None` is the catch-all tail.
    #
    # The openai rows are built ONLY when the SDK imported. This module deliberately degrades to
    # `openai = None` for stripped/offline installs (see the guarded import at the top), and a class
    # BODY runs at import: dereferencing `openai.BadRequestError` here took the whole package down
    # with `AttributeError: 'NoneType' has no attribute ...` — `core.config` imports from this
    # module, so every CLI command died, including the offline `looplab replay`. The tail is
    # unconditional because `json.JSONDecodeError` and the catch-all do not depend on the SDK, and
    # without the SDK no openai exception can be raised in the first place, so an empty head is not
    # a weakened policy — it is the only reachable one.
    _RETRY_POLICY: tuple = (((
        (openai.BadRequestError, "_policy_bad_request"),
        (openai.AuthenticationError, "_policy_auth"),
        ((openai.RateLimitError, openai.InternalServerError), "_policy_throttled"),
        (openai.APIConnectionError, "_policy_connection"),
        (openai.PermissionDeniedError, "_policy_forbidden"),
    ) if openai is not None else ()) + (
        (json.JSONDecodeError, "_policy_unparseable"),
        (None, "_policy_unclassified"),
    ))

    def _retry_or_raise(self, exc: BaseException, attempt: int, use_stream: bool) -> bool:
        """This client's per-exception retry policy for ONE failed attempt (doc 25 CO-05).

        Dispatches `_RETRY_POLICY` in order — the same order as the six-way `except` ladder it
        replaced, whose order is load-bearing (see `_policy_bad_request`). Two outcomes only:

        * RETURN → take another attempt. Any backoff `sleep` has already happened in the handler. The
          returned flag is the stream-stall ratchet: True means the next attempt must degrade off SSE.
        * RAISE → the clean `LLMError` the caller would have raised, `from exc`.

        Permanent per-client degrade state (`_stream_options_ok`, `_reasoning_ok`, `_stream_stalls`)
        is mutated by the handlers, exactly as the inline ladder did.
        """
        for types, handler in self._RETRY_POLICY:
            if types is None or isinstance(exc, types):
                return getattr(self, handler)(exc, attempt, use_stream)
        raise LLMError(f"LLM request to {self.base_url} failed: {exc}") from exc  # pragma: no cover

    def _policy_bad_request(self, exc, attempt: int, use_stream: bool) -> bool:
        # A 400 that rejects our REASONING toggle — a litellm-proxied model like glm-5.1
        # returns UnsupportedParamsError for `reasoning_effort` — isn't a real bad request:
        # drop reasoning for this client and retry (deepseek keeps it; glm-5.1 adapts).
        # Checked BEFORE the reasoning branch: `_is_reasoning_reject`'s generic keys
        # ("extra_forbidden", "unrecognized", …) also match a stream_options rejection, so
        # letting it win would drop the reasoning toggle and retry with the ACTUAL offending
        # field still attached — the identical 400, every attempt.
        if use_stream and self._stream_options_ok and _is_stream_options_reject(_err_body(exc)):
            self._stream_options_ok = False   # permanent for this client (see `_sdk_chat`)
            if attempt < self._max_retries:
                return False                  # re-issue without the optional usage field
            raise LLMError(f"LLM request to {self.base_url} rejected `stream_options` on the "
                           f"final attempt; it is now disabled for this client so a retry "
                           f"will succeed: {exc}") from exc
        if self.reasoning and self._reasoning_ok and _is_reasoning_reject(_err_body(exc)):
            self._reasoning_ok = False   # permanent for this client: the NEXT request drops the param
            if attempt < self._max_retries:
                return False             # a remaining attempt re-issues with reasoning dropped
            # On the LAST attempt the loop can't retry — but `_reasoning_ok` is now False, so the
            # caller's retry/fallback WILL succeed. Surface a CLEAR reason instead of falling
            # through to the generic, misleading "no response after retries" (every sibling retry
            # branch guards on attempt<_max_retries; this one silently did not).
            raise LLMError(f"LLM request to {self.base_url} rejected the reasoning param on the "
                           f"final attempt; reasoning is now disabled for this client so a retry "
                           f"will succeed: {exc}") from exc
        raise LLMError(f"LLM request to {self.base_url} failed: {exc}") from exc

    def _policy_auth(self, exc, attempt: int, use_stream: bool) -> bool:
        raise LLMError(f"LLM request to {self.base_url} failed: {exc} — check the API key "
                       "(LOOPLAB_LLM_API_KEY)") from exc

    def _policy_throttled(self, exc, attempt: int, use_stream: bool) -> bool:   # 429 / 5xx
        if attempt < self._max_retries:
            ra = _retry_after_seconds(_retry_after_of(exc))
            # Honor a POSITIVE server Retry-After up to RETRY_AFTER_CAP_S (a directive); otherwise
            # use our own exponential backoff (already ≤ BACKOFF_CAP_S). `if ra` (not `is not
            # None`): `_retry_after_seconds` clamps to max(0.0, …), so a `Retry-After: 0`, a
            # negative value, or an HTTP-date already in the PAST (clock skew) yields ra==0.0 —
            # honoring that would sleep(0) and burn every retry in milliseconds, defeating the
            # 429/5xx backoff entirely. Treat a non-positive directive as "unusable" → backoff.
            delay = min(ra, RETRY_AFTER_CAP_S) if ra else _backoff(attempt)
            if self._retry_after_cap is not None and delay > self._retry_after_cap:
                # A caller that declared a budget gets a refusal rather than a clamp (see the
                # constructor). Name the number the SERVER asked for, not the clamped one: that is
                # the wait the operator actually faces, and it is the whole content of the answer.
                # `ra` decides the wording because the same branch also catches our OWN backoff once
                # it grows past a small cap (`_backoff(3)` is already 16 s), and claiming the
                # endpoint asked for a delay we chose would be a lie the operator cannot check.
                whose = ("the endpoint asked us to wait" if ra else
                         "the next backoff before re-asking would be")
                raise LLMError(
                    f"LLM request to {self.base_url} failed: {exc} — {whose} {delay:.0f}s, longer "
                    f"than this call's {self._retry_after_cap:.0f}s retry budget, so it was not "
                    f"waited out") from exc
            # This wait used to be TOTALLY silent, which is indistinguishable from a hang — the same
            # failure the GPU host-lease notice was added for. WARNING so it reaches stderr through
            # logging's `lastResort` handler in a CLI run that configured no logging at all.
            _LOG.warning(
                "%s answered HTTP %s (%s) — waiting %.0fs before attempt %d of %d. %s The endpoint "
                "is up; nothing is stuck.", self.base_url, getattr(exc, "status_code", "?"),
                classify_llm_failure(exc), delay, attempt + 2, self._max_retries + 1,
                "That is the wait the endpoint itself asked for (Retry-After)." if ra else
                "Our own exponential backoff; the endpoint sent no Retry-After.")
            time.sleep(delay)
            return False
        raise LLMError(f"LLM request to {self.base_url} failed: {exc}") from exc

    def _policy_connection(self, exc, attempt: int, use_stream: bool) -> bool:
        # httpx transport failure. APITimeoutError (a subclass) = a read/connect timeout: a
        # stalled mid-stream read or a black-holed request — always transient, and the
        # reliable interrupt the urllib+ssl path lacked (a glm SSE stall hung for minutes).
        # A plain APIConnectionError is retried only when `_sdk_transient` says so (reset/EOF),
        # else it fails fast (refused/DNS/cert). A STREAM stall degrades the next attempt to
        # non-stream and ratchets the permanent-degrade counter. This ALSO catches a raw httpx
        # stream-body error that `_stream_with_idle_guard` normalized to APIConnectionError
        # (the SDK leaves those unwrapped) — `_sdk_transient` reads its httpx __cause__.
        is_timeout = isinstance(exc, openai.APITimeoutError)
        transient = is_timeout or _sdk_transient(exc)
        stalled = bool(use_stream and transient)
        if stalled:
            self._stream_stalls += 1
        if transient and attempt < self._max_retries:
            time.sleep(_backoff(attempt))
            return stalled
        raise LLMError(f"LLM request to {self.base_url} failed: {exc}") from exc

    def _policy_forbidden(self, exc, attempt: int, use_stream: bool) -> bool:
        # 403 — often a burst/rate-limit throttle, not hard-forbidden.
        if _is_throttle_403(_err_body(exc)) and attempt < self._max_retries:
            time.sleep(_backoff(attempt))
            return False
        raise LLMError(f"LLM request to {self.base_url} failed: {exc}") from exc

    def _policy_unparseable(self, exc, attempt: int, use_stream: bool) -> bool:
        # A gateway 200 with an empty / whitespace / keepalive-only body makes the SDK's
        # decoder raise a RAW json.JSONDecodeError — NOT an openai.APIError — so without this
        # it'd escape `_post` uncaught and abort the run (the role layer only retries+falls
        # back on LLMError). Mirror the old `_parse_chat_body`-None path: a transient gateway
        # hiccup — retry with backoff, then a clean LLMError. NARROW on purpose: a ValueError/
        # AttributeError from our own _accumulate_stream/_tool_call_slot code must NOT be
        # masked here as a "gateway hiccup" — let a real accumulation bug propagate loudly.
        if attempt < self._max_retries:
            time.sleep(_backoff(attempt))
            return False
        raise LLMError(f"LLM request to {self.base_url} returned an unparseable body") from exc

    def _policy_unclassified(self, exc, attempt: int, use_stream: bool) -> bool:
        # The ladder's final `except openai.APIError`: any other SDK-level protocol error -> clean
        # LLMError. `_post` catches only APIError|JSONDecodeError, so nothing else reaches here.
        raise LLMError(f"LLM request to {self.base_url} failed: {exc}") from exc

    @staticmethod
    def _keepalive_stall(parsed: Optional[dict], use_stream: bool) -> bool:
        """Did an HTTP-200 STREAM finish with nothing usable? (doc 25 CO-05)

        A stream is a "keepalive-only stall" ONLY when it produced NOTHING usable: no content, no
        tool_calls, no reasoning, AND no finish_reason. A reasoning model that hit its length limit
        while thinking (finish_reason="length", non-empty `reasoning`, empty `content`) is a REAL —
        if truncated — response, not a stall: retrying it 5× regenerates minutes of reasoning tokens
        and ratchets `_stream_stalls` to the permanent-degrade threshold, turning the idle timeout
        into a hard deadline for the rest of the run. finish_reason present (even "stop" with empty
        content) likewise means the endpoint answered — return it and let the no-choices check decide.
        """
        if not (use_stream and parsed is not None and parsed.get("choices")):
            return False
        ch0 = (parsed.get("choices") or [{}])[0] or {}
        m = ch0.get("message") or {}
        return not (m.get("content") or m.get("tool_calls") or m.get("reasoning")
                    or ch0.get("finish_reason"))

    def _account_keepalive_stall(self, parsed: Optional[dict]) -> None:
        """Bill a keepalive-only stream before discarding it (doc 25 CO-05).

        A keepalive-only stream is still a completed, BILLABLE provider call — the same reasoning
        `_post`'s accepted-body path states ("a billable HTTP-200 envelope with known usage but no
        choices is still a real call and may otherwise be retried for free"). Only the accepted body
        was accounted, so up to `_max_retries` empty attempts spent real money that never reached the
        cost limits or the durable ledger; with a flapping endpoint the run's recorded spend drifted
        arbitrarily far below the invoice.
        """
        usage = _normalize_usage((parsed or {}).get("usage"))
        if usage["total_tokens"] or usage["cost"]:
            self.accountant.add(usage["cost"], usage=usage)
            self._last_usage = usage

    def _post(self, payload: dict) -> dict:
        # T7 LLM response cache: serve an identical DETERMINISTIC (temp 0) request from cache instead
        # of re-hitting the model — cuts cost on retry/panel/verify flows. Sampling calls (temp>0)
        # are never cached (see _cache_key). Replay itself never calls the model (Ideas are recorded
        # in events), so this is a within-run/live cost saver, not a correctness dependency.
        ck = self._cache_key(payload)
        cached = self._cache_get(ck)
        if cached is not None:
            return cached
        # A network blip / HTTP error / non-JSON body must surface as a clean LLMError, not an
        # unhandled transport exception that aborts the whole run — the role layer
        # already retries + falls back on LLMError.
        # Rate-limit/transient resilience: a 429 (or 5xx) is retried with backoff (honoring a
        # Retry-After header when given) BEFORE surfacing — free/shared endpoints (e.g. OpenRouter
        # free tier) rate-limit bursts, and a single 429 shouldn't crash the whole run.
        body = None
        _stalled_prev = False               # this call's previous attempt stalled mid-stream
        for attempt in range(self._max_retries + 1):
            # Build the request per attempt so a param-compat retry (see `_retry_or_raise`) can drop
            # the reasoning toggle. `_reasoning_ok` starts True and flips off permanently for THIS
            # client the first time the endpoint rejects our reasoning param.
            # Stall-degrade: stream by default (httpx read-timeout = inter-token idle guard), but drop
            # to a single blocking read for the attempt right after a stream stall, and permanently
            # once this client has stalled STREAM_STALL_DEGRADE_AFTER times — a flaky proxied endpoint
            # often answers the SAME request fine without SSE while its stream wedges mid-generation.
            # Streaming is decided HERE, per attempt — never by the caller's payload (`_sdk_chat`
            # reads no `stream` key from it), so every call site gets the same degrade behaviour.
            use_stream = (self.stream and self._stream_stalls < STREAM_STALL_DEGRADE_AFTER
                          and not _stalled_prev)
            try:
                # admit immediately around the real provider attempt, not around a
                # whole node build. Retries take fresh fair turns and nested build -> novelty work
                # cannot retain a build permit while asking for another lane at total=1.
                with llm_request_permit():
                    parsed = self._sdk_chat(payload, use_stream)
            except (openai.APIError, json.JSONDecodeError) as e:
                # One catch, one policy table. `_retry_or_raise` RAISES on every non-retryable path,
                # so returning here always means "take another attempt"; what it returns is the
                # stream-stall ratchet that decides whether the next attempt degrades off SSE. The
                # two families are what the ladder caught: every SDK error derives from
                # `openai.APIError`, and a keepalive-only 200 body escapes the SDK's decoder as a RAW
                # `json.JSONDecodeError` that is NOT an APIError.
                _stalled_prev = self._retry_or_raise(e, attempt, use_stream) or _stalled_prev
                continue
            else:
                # HTTP 200 read cleanly, but the body can still be unusable: a hosted gateway
                # sometimes returns an empty / whitespace / SSE-keepalive-only body (OpenRouter sends
                # ': OPENROUTER PROCESSING' heartbeats while a model is queued and can finish with no
                # JSON payload), or a stream that carried no content/tool-call. (A mid-read socket
                # drop surfaces on the SDK path as a connection error caught above.)
                # Treat an empty/unparseable 200 like a transient
                # network failure — retry with backoff — rather than crash the run on a gateway hiccup.
                # `parsed` was computed in the try (streamed-and-reassembled, or _parse_chat_body).
                # A parsed dict is accepted (an `{"error": ...}` envelope has no `choices` and fails
                # fast at the post-loop check). Only two cases retry: an unparseable body (None), or a
                # STREAM that produced an empty message (keepalive-only heartbeats, no content/tool_call).
                empty_stream = self._keepalive_stall(parsed, use_stream)
                if parsed is not None and not empty_stream:
                    body = parsed
                    break
                if empty_stream:                # keepalive-only stream = the same stall family
                    _stalled_prev = True
                    self._stream_stalls += 1
                    self._account_keepalive_stall(parsed)
                if attempt < self._max_retries:
                    time.sleep(_backoff(attempt))
                    continue
                raise LLMError(f"LLM returned non-JSON/empty after {self._max_retries + 1} attempts")
        if body is None:  # loop exhausted retries on a transient code without ever succeeding
            raise LLMError(f"LLM request to {self.base_url} failed: no response after retries")
        usage = _normalize_usage(body.get("usage"))
        body["usage"] = usage
        # OpenRouter includes the billed amount in usage.cost. Local/OpenAI-compatible servers that
        # omit it retain the historical zero-dollar behaviour; malformed values are ignored safely.
        # Account a parsed provider response before semantic validation: a billable HTTP-200 envelope
        # with known usage but no choices is still a real call and may otherwise be retried for free.
        self.accountant.add(usage["cost"], usage=usage)
        self._last_usage = usage
        if "choices" not in body or not body["choices"]:
            # Ollama/vLLM emit {"error": ...} envelopes on a bad request — don't index [0] blind.
            raise LLMError(f"LLM response had no choices: {str(body)[:200]}")
        self._cache_put(ck, body)                # T7
        return body

    def _model_params(self) -> dict:
        """The generation's model_parameters (Langfuse generation metadata): sampling temperature +
        any provider reasoning toggle, so the trace shows HOW the model was called."""
        return {"temperature": self.temperature, **(self.reasoning or {})}

    def _text_payload(self, messages: list[dict], max_tokens: Optional[int]) -> dict:
        if (max_tokens is not None
                and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int)
                     or not 1 <= max_tokens <= 1_000_000)):
            raise ValueError("max_tokens must be an integer between 1 and 1000000")
        payload = {"model": self.model, "messages": messages,
                   "temperature": self.temperature}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        return payload

    def probe(self, messages: list[dict], *, max_tokens: int = 4) -> None:
        """Issue one bounded-output completion and discard its content without tracing it.

        Transport attempt count is configured on the client. A privacy-sensitive caller should also
        construct the client with response caching disabled, as the health route does.
        """
        self._post(self._text_payload(messages, max_tokens))

    def complete_text(self, messages: list[dict], *, max_tokens: Optional[int] = None) -> str:
        model_parameters = self._model_params()
        if max_tokens is not None:
            model_parameters = {**model_parameters, "max_tokens": max_tokens}
        with tracing.generation(op="complete_text", model=self.model, messages=messages,
                                model_parameters=model_parameters) as gen:
            body = self._post(self._text_payload(messages, max_tokens))
            msg = body["choices"][0]["message"]
            _apply_native_tool_calls(msg)   # strip a leaked native tool-call block from the text
            out = msg.get("content") or ""
            # Record the clean answer as the completion (the conclusion) and the raw reasoning
            # separately; return the original text so downstream parsing is unchanged.
            thinking, answer = _clean_thinking(out, _reasoning_of(msg))
            usage = body.get("usage")
            gen.output(answer or out).thinking(thinking).usage(usage).cost(_usage_cost(usage))
            return out

    def complete_text_stream(self, messages: list[dict]):
        """Stream a plain-text completion token-by-token (an OpenAI `stream:true` SSE call). Yields
        content deltas as they arrive; used by the assistant to stream its final answer live. Falls
        back to a single yield of the whole text if the endpoint doesn't stream. Best-effort — any
        transport error mid-stream ends the generator (the caller keeps what it got)."""
        pieces: list[str] = []
        usage = _normalize_usage(None)
        usage_observed = False
        stream_completed = False
        delegated_to_fallback = False

        def _fallback_to_blocking():
            """Hand this answer to the BLOCKING path and yield whatever it produces.

            Three sites reach it — a clean EOF with no content, a non-retryable BadRequestError, and
            any other APIError — and each used to write the same five lines (doc 25 CO-04). The
            duplication mattered because of the flag: `delegated_to_fallback` is what stops the
            `finally` below from charging this envelope on top of the call the fallback makes and
            accounts for itself. A fourth site that forgot to set it would double-bill silently.

            A generator, delegated to with `yield from`, because the caller is one: the `return`
            that ends the stream stays at the call site so the control flow remains visible there.
            """
            nonlocal delegated_to_fallback
            delegated_to_fallback = True
            text = self.complete_text(messages)
            if text:
                yield text
        # The generation span stays open for the whole stream (its duration = time-to-full-answer).
        with tracing.generation(op="complete_text_stream", model=self.model, messages=messages,
                                model_parameters=self._model_params()) as gen:
            try:
                # Reasoning-param retry: like `_post`, a model that 400s on our reasoning toggle
                # retries once without it instead of silently losing streaming forever.
                for _attempt in range(2):
                    kwargs: dict = {"model": self.model, "messages": messages,
                                    "temperature": self.temperature, "stream": True}
                    if self._stream_options_ok:      # optional capability — see `_sdk_chat`
                        kwargs["stream_options"] = {"include_usage": True}
                    if self.reasoning and self._reasoning_ok:
                        kwargs["extra_body"] = dict(self.reasoning)
                    try:
                        # Capture usage BEFORE yielding a co-located delta: if a consumer closes or
                        # cancels while suspended at that yield, the finally block still charges it.
                        # Keep a stream's slot until its final chunk (or consumer close). The context
                        # exits before either blocking fallback below, so total=1 cannot self-deadlock.
                        with llm_request_permit():
                            # Bound the header-WAIT exactly as `_sdk_chat`'s stream path does: run
                            # create() under `_bounded_create`'s wall-clock worker-thread guard (join =
                            # the header budget). Python evaluates create(**kwargs) BEFORE
                            # `_stream_with_idle_guard` ever sees the stream, so without this an endpoint
                            # that accepts the connection then never sends response HEADERS would hang
                            # here until the long idle read timeout. Now it fails over near
                            # `header_timeout`; `_stream_with_idle_guard` then governs the body.
                            header_join = self._header_join()
                            # OWN the SDK Stream so it is closed on EVERY exit, including a consumer
                            # that closes/cancels this generator while suspended at the `yield` below.
                            # Passing `create(...)` inline left the only reference on the C stack of a
                            # dead frame: the broker permit was released but the HTTP response and its
                            # socket stayed open until GC, so a UI reader that navigates away mid-answer
                            # leaked a live connection out of the shared pool.
                            _stream = self._bounded_create(kwargs, header_join)
                            _body = self._streaming_body()
                            _body.__enter__()
                            try:
                                for ev in _stream_with_idle_guard(
                                        _stream, self.timeout, self.header_timeout):
                                    observed = getattr(ev, "usage", None)
                                    if observed is not None:
                                        usage_observed = True
                                        usage = _normalize_usage(_stream_usage(observed))
                                    if not ev.choices:
                                        continue
                                    piece = getattr(ev.choices[0].delta, "content", None) or ""
                                    if piece:
                                        pieces.append(piece)
                                        yield piece
                            finally:
                                try:
                                    _stream.close()
                                except Exception:  # noqa: BLE001 - best-effort release, never mask
                                    pass
                                _body.__exit__(None, None, None)
                        # A role-only/empty clean EOF is NOT a successful assistant answer. Falling
                        # through here returned a generator that yielded nothing, so the caller
                        # persisted a zero-content "success" — and the docstring's promised
                        # single-yield fallback never ran, because it hangs off the BadRequestError /
                        # APIError handlers below, which a clean EOF does not raise. Delegate the same
                        # way those do; `account_here` in the `finally` still charges this envelope
                        # when the provider reported usage for it.
                        if not pieces:
                            yield from _fallback_to_blocking()
                            return
                        stream_completed = True
                        break                    # streamed (or cleanly ended) -> done
                    except openai.BadRequestError as e:
                        if (self._stream_options_ok and not pieces and _attempt == 0
                                and _is_stream_options_reject(_err_body(e))):
                            self._stream_options_ok = False   # see `_sdk_chat`; checked before reasoning
                            continue             # retry the stream once without the usage option
                        if (self.reasoning and self._reasoning_ok and not pieces and _attempt == 0
                                and _is_reasoning_reject(_err_body(e))):
                            self._reasoning_ok = False
                            continue             # retry the stream once without the reasoning toggle
                        if not pieces:           # any other bad request -> blocking fallback
                            yield from _fallback_to_blocking()
                            return
                        break
                    except openai.APIError:
                        # A fallback owns/accountants its own provider call.  The outer stream still
                        # records independently if it had already observed provider usage.
                        if not pieces:
                            yield from _fallback_to_blocking()
                            return
                        break
            finally:
                # The money rule lives in `_stream_envelope_is_billable` — one named rule with its
                # own truth table, because the clause that stops a DELEGATED stream being billed on
                # top of the fallback's own call is unreachable from here today and an inline
                # expression made it look like dead code.
                account_here = _stream_envelope_is_billable(
                    usage_observed=usage_observed,
                    delegated_to_fallback=delegated_to_fallback,
                    stream_completed=stream_completed,
                    produced_content=bool(pieces))
                if account_here:
                    self.accountant.add(usage["cost"], usage=usage)
                    self._last_usage = usage
                gen.output("".join(pieces)).usage(usage if usage_observed else None) \
                   .cost(usage["cost"] if usage_observed else 0.0)

    def chat(self, messages: list[dict], tools: list[dict],
             tool_choice: str = "auto") -> dict:
        """General multi-turn tool-calling step. Returns the raw assistant message
        (content + optional tool_calls) so the caller can run an agent loop."""
        with tracing.generation(op="chat", model=self.model, messages=messages,
                                model_parameters=self._model_params()) as gen:
            body = self._post({
                "model": self.model, "messages": messages, "tools": tools,
                "tool_choice": tool_choice, "temperature": self.temperature,
            })
            msg = body["choices"][0]["message"]
            _apply_native_tool_calls(msg)   # recover a leaked native tool-call block (glm/DeepSeek)
            thinking, answer = _clean_thinking(msg.get("content") or "", _reasoning_of(msg))
            # The output records BOTH the assistant text AND any tool_calls it decided to make, so the
            # trace shows what this generation produced (its content + the tool calls the loop will run).
            usage = body.get("usage")
            gen.output(_assistant_text({**msg, "content": answer})).thinking(thinking) \
               .usage(usage).cost(_usage_cost(usage))
            if msg.get("tool_calls"):
                gen.tool_calls([{"name": (c.get("function") or {}).get("name"),
                                 "arguments": (c.get("function") or {}).get("arguments")}
                                for c in msg["tool_calls"]])
            return msg

    def complete_tool(self, messages: list[dict], json_schema: dict) -> dict:
        tool = {"type": "function",
                "function": {"name": "emit", "description": "Emit the structured result.",
                             "parameters": json_schema}}
        payload = {
            "model": self.model, "messages": messages, "tools": [tool],
            "tool_choice": {"type": "function", "function": {"name": "emit"}},
            "temperature": self.temperature,
        }
        if self.guided_json:   # H1 constrained decoding (vLLM/SGLang); Ollama ignores when off
            payload["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": "emit", "schema": json_schema}}
            payload["guided_json"] = json_schema
        with tracing.generation(op="complete_tool", model=self.model, messages=messages,
                                model_parameters=self._model_params()) as gen:
            body = self._post(payload)
            msg = body["choices"][0]["message"]
            # Recover a leaked native tool-call block (glm/DeepSeek) — but ALWAYS as a tool call
            # here, never folded into content: this endpoint forces a tool named "emit", which is
            # in _FINAL_NAMES, so _apply_native_tool_calls would discard the recovered args and
            # force the expensive text-parse fallback for the exact case recovery was built for.
            if not msg.get("tool_calls"):
                _calls, _clean = _extract_native_tool_calls(msg.get("content") or "")
                if _calls:
                    msg["tool_calls"] = _calls
                    msg["content"] = _clean
            calls = msg.get("tool_calls")
            if not calls:  # endpoint ignored tool_choice -> let parse.py fall back to text
                usage = body.get("usage")
                gen.usage(usage).cost(_usage_cost(usage)).error("no tool_calls in response")
                raise KeyError("no tool_calls in response")
            # This endpoint FORCES `tool_choice: emit`, so the result must actually be that call.
            # Taking `calls[0]` blindly let a backend that ignores tool_choice have some OTHER tool's
            # coincidentally schema-valid arguments accepted as the emit payload — a wrong answer under
            # the right shape, which no downstream validation can catch. Select by name instead of
            # position (also correct when a leaked native block recovered above lands beside a real
            # call), and treat "no emit anywhere" exactly like the empty case: raise so parse.py falls
            # back to the text path, which is the honest reading of an endpoint that ignored the force.
            emit = next((c for c in calls if (c.get("function") or {}).get("name") == "emit"), None)
            if emit is None:
                usage = body.get("usage")
                gen.usage(usage).cost(_usage_cost(usage)).error("forced emit not honored")
                raise KeyError("no tool_calls in response")
            args = emit["function"]["arguments"]
            # Reasoning models emit their chain-of-thought (a `reasoning` field, or inline <think> in
            # `content`) alongside the tool call; capture it (debug channel) instead of discarding it.
            # The completion stays the structured tool args — the clean conclusion the UI renders.
            thinking, _ = _clean_thinking(msg.get("content") or "", _reasoning_of(msg))
            usage = body.get("usage")
            gen.output(args if isinstance(args, str) else json.dumps(args)).thinking(thinking) \
               .usage(usage).cost(_usage_cost(usage))
            return json.loads(args) if isinstance(args, str) else args


class CostAccountant:
    def __init__(self, limit: Optional[float] = None, warn_frac: float = 0.8,
                 on_delta: Optional[Callable[[dict], None]] = None):
        self.limit = limit
        self.warn_frac = warn_frac
        self.spent = 0.0
        self.warned = False
        # Token accounting (UI cost panel): local models have no $ price, but tokens are the
        # real signal of how much LLM work a run cost. Accumulated across all calls.
        self.calls = 0
        # How many of those `calls` the provider actually PRICED. `spent` is only a complete invoice
        # when this equals `calls`; below it, `spent` is a floor and the gap is unknown, not free.
        # See `cost_is_reported` for the two real runs that made the distinction load-bearing.
        self.priced_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        # The LARGEST single prompt seen = how big the model's CONTEXT WINDOW actually got. Distinct from
        # prompt_tokens (which SUMS the same context re-sent every tool-loop turn → O(turns²)); the UI
        # reads this to show "context" honestly instead of the billed re-send sum.
        self.peak_prompt = 0
        # Optional durable-accounting seam.  The provider call has already succeeded when add() runs,
        # so a sink failure is telemetry failure: remember it, never turn it into a provider retry.
        if on_delta is not None and not callable(on_delta):
            raise TypeError("on_delta must be callable or None")
        self.on_delta = on_delta
        self.last_sink_error: Optional[str] = None
        self._lock = threading.Lock()

    def set_sink(self, callback: Optional[Callable[[dict], None]]) -> None:
        """Install/replace the post-commit delta sink used by a durable run ledger."""
        if callback is not None and not callable(callback):
            raise TypeError("accounting sink must be callable or None")
        with self._lock:
            self.on_delta = callback
            self.last_sink_error = None

    def bind_sink(self, factory: Callable[[Optional[Callable[[dict], None]]],
                                          Optional[Callable[[dict], None]]]) -> dict:
        """Atomically replace a sink and return counters at the ownership boundary.

        Durable run accounting uses this seam so a concurrent ``add`` belongs wholly to the old or
        new owner. There is no snapshot-then-install window in which committed usage can be lost or
        charged to both runs. The factory must only construct a callback; it executes under the
        accountant lock and must not call back into this object.
        """
        if not callable(factory):
            raise TypeError("accounting sink factory must be callable")
        with self._lock:
            callback = factory(self.on_delta)
            if callback is not None and not callable(callback):
                raise TypeError("accounting sink must be callable or None")
            self.on_delta = callback
            self.last_sink_error = None
            return {
                "cost": self.spent,
                "calls": self.calls,
                "priced_calls": self.priced_calls,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
            }

    def add(self, cost: Optional[float], usage: Optional[dict] = None) -> float:
        """Commit one logical provider-call delta after fully sanitizing untrusted telemetry.

        ``calls`` is provider-call truth, not "responses whose gateway happened to report tokens": a
        successful response with missing/malformed usage still increments it once.  Cache hits never
        invoke ``add``.  All candidate counters are computed before one lock-protected assignment, so
        a bad late field cannot leave cost/calls/tokens partially mutated.
        """
        safe_cost = _safe_cost(cost)
        normalized = _normalize_usage(usage)
        delta = {
            "cost": safe_cost,
            "calls": 1,
            "priced_calls": 1 if _call_is_priced(cost, usage, normalized) else 0,
            "prompt_tokens": int(normalized["prompt_tokens"]),
            "completion_tokens": int(normalized["completion_tokens"]),
            "total_tokens": int(normalized["total_tokens"]),
        }
        with self._lock:
            # Keep every durable/public roll-up finite and bounded even after repeated individually
            # valid near-float/int ceilings. Saturation is safer than wrap/Infinity or an exception
            # after some counters have already changed.
            candidate_spent = self.spent + safe_cost
            if not math.isfinite(candidate_spent):
                candidate_spent = sys.float_info.max
            candidate_prompt = min(_MAX_USAGE_TOKENS,
                                   self.prompt_tokens + delta["prompt_tokens"])
            candidate_completion = min(_MAX_USAGE_TOKENS,
                                       self.completion_tokens + delta["completion_tokens"])
            candidate_total = min(_MAX_USAGE_TOKENS,
                                  self.total_tokens + delta["total_tokens"])
            candidate_calls = min(_MAX_USAGE_TOKENS, self.calls + 1)
            candidate_priced = min(_MAX_USAGE_TOKENS,
                                   self.priced_calls + delta["priced_calls"])
            candidate_peak = max(self.peak_prompt, delta["prompt_tokens"])
            candidate_warned = self.warned
            if (self.limit is not None and not candidate_warned
                    and candidate_spent >= self.warn_frac * self.limit):
                candidate_warned = True
            exceeded = self.limit is not None and candidate_spent >= self.limit

            self.spent = candidate_spent
            self.calls = candidate_calls
            self.priced_calls = candidate_priced
            self.prompt_tokens = candidate_prompt
            self.completion_tokens = candidate_completion
            self.total_tokens = candidate_total
            self.peak_prompt = candidate_peak
            self.warned = candidate_warned
            sink = self.on_delta
            committed_spent = self.spent

        if sink is not None:
            try:
                sink(dict(delta))
            except Exception as e:  # noqa: BLE001 - never retry a paid provider call for sink failure
                with self._lock:
                    self.last_sink_error = f"{type(e).__name__}: {e}"[:500]
            else:
                with self._lock:
                    self.last_sink_error = None
        if exceeded:
            raise BudgetExceeded(f"spent {committed_spent:.4f} >= budget {self.limit:.4f}")
        return committed_spent

    def remaining(self) -> Optional[float]:
        with self._lock:
            return None if self.limit is None else max(0.0, self.limit - self.spent)


class LiteLLMClient:
    """Optional LiteLLM adapter implementing the `parse.LLMClient` Protocol.

    It lazy-imports ``litellm`` so the package installs and tests run without that optional
    dependency. The shipped Settings factory does not select this adapter automatically.
    """

    def __init__(self, model: str, accountant: Optional[CostAccountant] = None, **kwargs):
        self.model = model
        self.accountant = accountant or CostAccountant()
        self.kwargs = kwargs

    def _litellm(self):
        import litellm  # lazy
        return litellm

    def _completion(self, **kwargs):
        """Call litellm.completion with the OpenAICompatibleClient's resilience contract: map any
        provider exception to `LLMError` (so `parse_structured` and the role layer's `except LLMError`
        retry+fallback treat it like any other bad response instead of crashing the run — the module
        docstring's promise, previously honored by ONE backend only), and retry transient failures
        (rate-limit / timeout / connection / 5xx) with exponential backoff before surfacing."""
        litellm = self._litellm()
        last: Optional[BaseException] = None
        for attempt in range(4):
            try:
                # match the OpenAI-compatible transport seam. One attempt borrows one
                # atomic total+lane slot; backoff/retry waiting itself consumes no shared capacity.
                with llm_request_permit():
                    return litellm.completion(model=self.model, **kwargs)
            except Exception as e:  # noqa: BLE001 - normalize EVERY provider error to LLMError
                last = e
                name = type(e).__name__.lower()
                transient = any(k in name for k in (
                    "ratelimit", "timeout", "apiconnection", "serviceunavailable",
                    "internalserver", "overloaded", "apierror"))
                if transient and attempt < 3:
                    time.sleep(_backoff(attempt))
                    continue
                raise LLMError(f"litellm completion for {self.model} failed: {e}") from e
        # Not reachable today — every iteration returns or raises, since `attempt < 3` is False on
        # the last of `range(4)`. It stays as the fallthrough guard: if those two bounds ever drift
        # apart, this raises instead of silently returning None into `_completion`'s callers.
        raise LLMError(f"litellm completion for {self.model} failed: {last}")

    def _account(self, resp) -> None:
        self.accountant.add(self._cost(resp), usage=self._usage(resp))

    def _usage(self, resp) -> Optional[dict]:
        try:
            u = resp.usage
            payload = {
                "prompt_tokens": getattr(u, "prompt_tokens", 0),
                "completion_tokens": getattr(u, "completion_tokens", 0),
                "total_tokens": getattr(u, "total_tokens", 0),
            }
            # LiteLLM states the amount OUT OF BAND (`_hidden_params.response_cost`), so fold it in
            # here: `_normalize_usage` is where the priced/unpriced witness is minted, and a response
            # this backend DID price must not be indistinguishable from one it did not. Pre-normalize
            # through `_safe_cost` because `_usage_cost_reported` deliberately refuses non-int/float
            # payloads (untrusted JSON) while this gateway legitimately hands back a `Decimal`; the
            # key is OMITTED, not zeroed, when nothing was reported.
            raw_cost = self._cost(resp)
            if cost_is_reported(raw_cost):
                payload["cost"] = _safe_cost(raw_cost)
            return _normalize_usage(payload)
        except Exception:
            return None

    def complete_text(self, messages: list[dict]) -> str:
        with tracing.generation(op="complete_text", model=self.model, messages=messages) as gen:
            resp = self._completion(messages=messages, **self.kwargs)
            self._account(resp)
            if not getattr(resp, "choices", None):
                raise LLMError(f"litellm response had no choices for {self.model}")
            m = resp.choices[0].message
            out = m.content or ""
            # Not `_reasoning_of`: litellm messages are objects (getattr, not dict.get) and probe
            # `reasoning_content` FIRST — deliberately divergent, don't unify blindly.
            thinking, answer = _clean_thinking(
                out, getattr(m, "reasoning_content", None) or getattr(m, "reasoning", None) or "")
            u = self._usage(resp)
            gen.output(answer or out).thinking(thinking).usage(u).cost(_safe_cost(self._cost(resp)))
            return out

    def _cost(self, resp):
        try:
            return resp._hidden_params.get("response_cost")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return None

    def complete_tool(self, messages: list[dict], json_schema: dict) -> dict:
        tool = {
            "type": "function",
            "function": {"name": "emit", "description": "Emit the structured result.",
                         "parameters": json_schema},
        }
        with tracing.generation(op="complete_tool", model=self.model, messages=messages) as gen:
            resp = self._completion(
                messages=messages, tools=[tool],
                tool_choice={"type": "function", "function": {"name": "emit"}}, **self.kwargs,
            )
            self._account(resp)
            if not getattr(resp, "choices", None):
                raise LLMError(f"litellm response had no choices for {self.model}")
            calls = resp.choices[0].message.tool_calls
            if not calls:  # endpoint ignored tool_choice -> KeyError so parse.py falls back
                gen.usage(self._usage(resp)).error("no tool_calls in response")
                raise KeyError("no tool_calls in response")
            # This endpoint FORCES `tool_choice: emit`, so the result must actually be that call.
            # Taking `calls[0]` blindly let a backend that ignores tool_choice have some OTHER tool's
            # coincidentally schema-valid arguments accepted as the emit payload — a wrong answer
            # under the right shape, which no downstream validation can catch. Select by NAME, the
            # same fix OpenAICompatibleClient.complete_tool carries, and treat "no emit anywhere"
            # exactly like the empty case: raise so parse.py falls back to the text path, the honest
            # reading of an endpoint that ignored the force. litellm hands back objects rather than
            # dicts, hence the getattr walk.
            emit = next(
                (c for c in calls
                 if getattr(getattr(c, "function", None), "name", None) == "emit"), None)
            if emit is None:
                gen.usage(self._usage(resp)).error("forced emit not honored")
                raise KeyError("no tool_calls in response")
            args = emit.function.arguments
            m = resp.choices[0].message
            # Not `_reasoning_of`: object attributes + reasoning_content-first (see complete_text).
            thinking, _ = _clean_thinking(
                getattr(m, "content", None) or "",
                getattr(m, "reasoning_content", None) or getattr(m, "reasoning", None) or "")
            gen.output(args if isinstance(args, str) else json.dumps(args)).thinking(thinking) \
               .usage(self._usage(resp)).cost(_safe_cost(self._cost(resp)))
            return json.loads(args) if isinstance(args, str) else (args or {})


class LlmTarget(NamedTuple):
    """Everything that decides WHICH model a role talks to, resolved and immutable.

    Immutable and hashable on purpose: this IS the client cache key (`adapters/tasks.py`), so two
    roles resolving to the same place share one client while two that differ in ANY property —
    including the credential — stay separate. Keying a cache on (model, base_url) alone would
    silently hand one role another's key and mis-attribute its spend."""
    model: str
    base_url: str
    temperature: float | None
    api_key_env: str | None
    # shared = guarded shared key; profile = api_key_env bound to this target; none = deliberate
    # no-credential endpoint override. Default preserves four-argument construction compatibility.
    credential_mode: str = "shared"


# The unified agent's exact stage vocabulary. This exported registry is shared by resolution,
# Settings validation, docs and tests: a stage rename or typo must have one authoritative answer,
# rather than one map accepting a key that another component silently ignores. Historical snapshots
# get a deliberately narrower compatibility path in `core.config.settings_from_snapshot`.
AGENT_STAGE_KEYS = frozenset({"propose", "implement", "repair", "strategy", "pilot"})

# The role names `Settings.role_profiles` accepts. REGISTRY-GUARDED, like the project's other
# duck-typed seams: `tests/test_llm_targets.py` scans the source both ways, so a name here without a
# reader (or a reader without a name here) is a red test rather than a setting that silently does
# nothing.
#
# Unified stages double as roles, which is why they are resolved with `role=` rather than a separate
# `stage=` argument: a stage IS a role here, and giving them their own parameter is what let
# `role_profiles` bindings on them validate and then never be read. Compose the role registry from
# `AGENT_STAGE_KEYS` so these two public contracts cannot drift apart.
LLM_ROLE_KEYS = AGENT_STAGE_KEYS | frozenset({
    # split roles + the standalone helpers that build their own client
    "researcher", "developer", "strategist", "compressor", "embed",
})

# Where a role reads its per-role model/endpoint/temperature fields from, when it has them. Absent =
# the role has no fields of its own (`pilot`) and resolves profile -> shared. Every name here is
# checked against `Settings.model_fields` by the registry test: these are read with a defaulted
# `getattr`, so a rename would otherwise silently degrade the role to the shared values.
_ROLE_FIELDS: dict[str, tuple[str, str, str | None]] = {
    "propose": ("researcher_model", "researcher_base_url", "researcher_temperature"),
    "researcher": ("researcher_model", "researcher_base_url", "researcher_temperature"),
    "implement": ("developer_model", "developer_base_url", "developer_temperature"),
    "repair": ("developer_model", "developer_base_url", "developer_temperature"),
    "developer": ("developer_model", "developer_base_url", "developer_temperature"),
    "strategy": ("strategist_model", "strategist_base_url", "strategist_temperature"),
    "strategist": ("strategist_model", "strategist_base_url", "strategist_temperature"),
    "compressor": ("compressor_model", "compressor_base_url", None),
    "embed": ("embed_model", "embed_base_url", None),
}


def role_profile(settings, role: str | None) -> dict:
    """The connection profile a role resolves to (`role_profiles[role]`, else `llm_profile`), or {}.

    Public because a caller sometimes has to ask "did the PROFILE supply this?" rather than "what did
    it resolve to" — `make_embedder` and the history compressor are switched on by a model being
    configured at all, and `resolve_llm_target` always returns a model (it falls back to the shared
    one), so they cannot use the resolved value as their gate."""
    profiles = getattr(settings, "llm_profiles", None) or {}
    if not profiles:
        return {}
    name = (getattr(settings, "role_profiles", None) or {}).get(role or "")
    if name is None:
        name = getattr(settings, "llm_profile", None)
    entry = profiles.get(name) if name else None
    return entry if isinstance(entry, dict) else {}


def resolve_llm_target(settings, *, role: str | None = None) -> LlmTarget:
    """The ONE place that answers "which model, endpoint, temperature and key does this role use?".

    Model, endpoint and temperature resolve INDEPENDENTLY, first non-empty of:
        model        agent_stage_models[role] > <role>_model      > profile.model       > llm_model
        base_url     agent_stage_base_urls[role] > <role>_base_url > profile.base_url   > llm_base_url
        temperature                            <role>_temperature > profile.temperature > llm_temperature

    The CREDENTIAL is deliberately not independent. A key is only meaningful for the endpoint it was
    issued for, so the profile's `api_key_env` travels only while the resolved endpoint is still the
    one that profile would have used. Resolving it independently meant a `<role>_base_url` or a stage
    map could redirect the request while keeping the key — putting one provider's live secret in an
    Authorization header to a different host. Without a profile key the shared `llm_api_key` applies,
    exactly as before.

    With no profiles configured and no role asked for, this short-circuits to the shared values —
    the single-model operator's path, unchanged."""
    profiles = getattr(settings, "llm_profiles", None) or {}
    if not profiles and role is None:
        return LlmTarget(settings.llm_model, settings.llm_base_url,
                         getattr(settings, "llm_temperature", None), None, "shared")
    stage_models = getattr(settings, "agent_stage_models", None) or {}
    stage_urls = getattr(settings, "agent_stage_base_urls", None) or {}
    role_key = role or ""
    stage_key = role_key if role_key in AGENT_STAGE_KEYS else ""
    profile = role_profile(settings, role)

    role_model = role_url = role_temp = None
    fields = _ROLE_FIELDS.get(role_key)
    if fields:
        m_field, u_field, t_field = fields
        role_model = getattr(settings, m_field, None)
        role_url = getattr(settings, u_field, None)
        role_temp = getattr(settings, t_field, None) if t_field else None

    # The endpoint this profile stands for; anything that overrides it also drops its credential.
    profile_url = profile.get("base_url") or settings.llm_base_url
    base_url = stage_urls.get(stage_key) or role_url or profile_url

    temperature = role_temp
    if temperature is None:
        temperature = profile.get("temperature")
    if temperature is None:
        temperature = getattr(settings, "llm_temperature", None)
    profile_env = profile.get("api_key_env") or None
    profile_key_travels = bool(profile_env and (
        normalize_llm_base_url(base_url) == normalize_llm_base_url(profile_url)))
    shared_endpoint = (
        normalize_llm_base_url(base_url) == normalize_llm_base_url(settings.llm_base_url))
    # Dropping a profile key because an override redirected the endpoint must not silently fall back
    # to the shared key. A genuinely shared endpoint with no profile credential keeps the shared path.
    credential_mode = ("profile" if profile_key_travels else
                       "none" if profile_env or not shared_endpoint else "shared")
    return LlmTarget(
        model=(stage_models.get(stage_key) or role_model or profile.get("model")
               or settings.llm_model),
        base_url=base_url,
        temperature=temperature,
        api_key_env=profile_env if profile_key_travels else None,
        credential_mode=credential_mode,
    )


def apply_llm_model_override(settings, model: str):
    """Apply a CLI default-model override without discarding an active profile connection.

    A default profile owns model, endpoint and credential as one connection. Writing only
    ``settings.llm_model`` leaves the profile model ahead of it in the resolver and makes a visible
    ``--model`` flag a no-op. Updating a copied active profile keeps its endpoint/key binding while
    making the requested model effective and durable in run snapshots.
    """
    profile_name = getattr(settings, "llm_profile", None)
    profiles = getattr(settings, "llm_profiles", None) or {}
    active = profiles.get(profile_name) if profile_name else None
    if isinstance(active, dict):
        updated_profiles = dict(profiles)
        updated_profiles[profile_name] = {**active, "model": model}
        settings.llm_profiles = updated_profiles
    else:
        settings.llm_model = model
    return settings


def client_kwargs_for(target: LlmTarget, *, role: str | None = None,
                      timeout: float | None = None) -> dict:
    """The `make_llm_client` arguments a resolved target implies.

    Split out from `make_llm_client_for` so a caller that must go through its OWN module's
    `make_llm_client` name can still share the resolution. That matters because the re-exports in
    `adapters.tasks`, `cli` and `serve.server` are documented monkeypatch seams: a helper here that
    always called this module's binding would quietly route per-role construction past all of them.

    `api_key` appears ONLY when the resolved profile supplied one, so with no profiles the arguments
    are what the historical `make_llm_client(settings, ...)` call passed. A profile naming a variable
    that is not set fails LOUDLY, quoting the variable and the role — never its value; discovering a
    missing credential at the first paid call, halfway into a run, is strictly worse."""
    kwargs: dict = {"model": target.model, "base_url": target.base_url,
                    "temperature": target.temperature, "timeout": timeout}
    if target.api_key_env:
        # Every refusal below carries a role-neutral `cause_detail` alongside its role-named message.
        # A profile is shared BY roles, so one unset variable is one mistake however many roles read
        # it — `validate_bound_profiles` groups on the cause and prints it once. See
        # `core/errors.py::LLMCredentialError`.
        named = f"role {role!r} "
        binding_env = f"{target.api_key_env}_BASE_URL"
        if target.api_key_env in {SHARED_KEY_ENV, SHARED_BINDING_ENV}:
            raise LLMCredentialError(
                f"profile api_key_env {target.api_key_env} aliases the shared credential; "
                "use the shared target or a dedicated profile variable")
        value = os.environ.get(target.api_key_env)
        if not value:
            raise LLMCredentialError(
                f"{named}is bound to a connection profile whose api_key_env "
                f"{target.api_key_env} is unset or empty (endpoint {target.base_url}). Set that "
                "environment variable, or drop the binding.",
                cause_detail=(
                    f"connection-profile credential {target.api_key_env} is unset or empty in "
                    f"{_SOURCE_PROCESS} (endpoint {target.base_url}). Set that environment "
                    f"variable — together with its binding {binding_env} — or drop api_key_env "
                    "from that profile."))
        binding = os.environ.get(binding_env)
        if not binding:
            # The profile tier's spelling of the shared tier's headline defect: half a pair.
            detail = incomplete_pair_refusal(
                key_env=target.api_key_env, binding_env=binding_env, have_key=True,
                source=_SOURCE_PROCESS, tiered=False)
            raise LLMCredentialError(f"{named}profile credential: {detail}", cause_detail=detail)
        if normalize_llm_base_url(binding) != normalize_llm_base_url(target.base_url):
            detail = misbound_credential_refusal(
                key_env=target.api_key_env, binding_env=binding_env,
                target=normalize_llm_base_url(target.base_url),
                binding=normalize_llm_base_url(binding), source=_SOURCE_PROCESS,
                endpoint_knob="That profile's `base_url`", tiered=False)
            raise LLMCredentialError(f"{named}profile credential: {detail}", cause_detail=detail)
        kwargs["api_key"] = value
        kwargs["api_key_base_url"] = binding
    elif target.credential_mode == "none":
        kwargs["api_key"] = NO_CREDENTIAL
    return kwargs


def make_llm_client_for(settings, *, role: str | None = None, timeout: float | None = None,
                        factory=None) -> OpenAICompatibleClient:
    """Resolve a role's target and build its client — the profile-aware front door to the factory.

    `factory` lets a caller name the `make_llm_client` binding to build through, so a module whose
    re-export is a monkeypatch seam (`adapters.tasks`, `cli`, `serve.server`) keeps that seam alive;
    omitted, this module's own binding is used."""
    target = resolve_llm_target(settings, role=role)
    configured_profile_env = role_profile(settings, role).get("api_key_env")
    if target.credential_mode == "none" and configured_profile_env:
        raise LLMError(
            f"role {role!r} overrides the endpoint of profile credential "
            f"{configured_profile_env}; bind that role to a matching profile")
    build = factory if factory is not None else make_llm_client
    return build(settings, **client_kwargs_for(target, role=role, timeout=timeout))


def llm_credential_consumers(settings) -> tuple[bool, set[str | None]]:
    """Return strict, required credential consumers for this engine configuration.

    ``None`` is the default/profile target used by the baseline role client and run report. The
    leading bool remains for callers that can describe a truly raw shared client; all in-tree engine
    clients now resolve through a role/default target, so it is currently false. Optional clients
    with a documented local fallback are listed separately below.
    """
    live_backend = getattr(settings, "backend", "toy") == "llm"
    roles: set[str | None] = set()
    if live_backend:
        roles.update({None, "researcher"})
        if getattr(settings, "unified_agent", False):
            roles.update({"propose", "implement", "repair", "pilot"})
            if getattr(settings, "strategist_backend", "off") in {"llm", "agent"}:
                roles.add("strategy")
        else:
            roles.add("developer")
            if getattr(settings, "strategist_backend", "off") in {"llm", "agent"}:
                roles.add("strategist")
    return False, roles


def llm_optional_credential_consumers(settings) -> set[str | None]:
    """Return targets whose credential failure deliberately falls back to a local implementation."""
    roles: set[str | None] = set()
    live_backend = getattr(settings, "backend", "toy") == "llm"
    if (getattr(settings, "memora", False) and getattr(settings, "memora_llm", False)):
        roles.add(None)
    if (live_backend and (getattr(settings, "compressor_model", None)
                          or role_profile(settings, "compressor").get("model"))):
        roles.add("compressor")
    if (getattr(settings, "embed_model", None)
            or role_profile(settings, "embed").get("model")):
        roles.add("embed")
    # A live backend already requires the default target; Memora must not downgrade that requirement.
    roles.difference_update(llm_credential_consumers(settings)[1])
    return roles


def render_credential_failures(causes: dict[str, list[str]]) -> str:
    """Render {root cause -> roles it breaks} as ONE diagnosis per cause. Order-preserving.

    The counting rule this function exists for: a run has one credential configuration, so N roles
    failing to resolve it is N symptoms of ONE mistake, not N mistakes. Printing it once per role
    (which is what `"; ".join(failures)` did, seven times over, because the role prefix made every
    string distinct and defeated the `dict.fromkeys` dedup) buries the diagnosis under its own
    repetitions and tells the operator nothing about which knob is wrong.
    """
    if len(causes) == 1:
        detail, roles = next(iter(causes.items()))
        named = list(dict.fromkeys(roles))
        if not named:
            return detail
        if len(named) == 1:                       # one role: nothing was collapsed, say it plainly
            return f"{detail}\n  Affects: {named[0]}."
        return (f"{detail}\n  This one cause is why all {len(named)} of these fail; they share the "
                f"credential configuration and are not {len(named)} separate problems: "
                + ", ".join(named) + ".")
    lines = [f"{len(causes)} distinct problems."]
    for index, (detail, roles) in enumerate(causes.items(), start=1):
        lines.append(f"  [{index}] {detail}")
        if roles:
            lines.append(f"      Affects: {', '.join(dict.fromkeys(roles))}.")
    return "\n".join(lines)


def validate_bound_profiles(
    settings, *,
    consumer_roles: set[str | None] | frozenset[str | None] | None = None,
    external_fallback_roles: set[str] | frozenset[str] | None = None,
    trusted_in_process_roles: set[str] | frozenset[str] | None = None,
) -> None:
    """Fail before engine construction when an active credential target is unusable.

    Collects by ROOT CAUSE rather than by role (`core/errors.py::LLMCredentialError.cause_detail`),
    so one wrong variable produces one diagnosis naming every role it breaks — see
    `render_credential_failures` for the counting rule and what it replaced. Keyword-only role
    overrides let the task-aware agent composition plan narrow the settings-level superset without
    changing the historical public call: omitted overrides preserve the old settings-only policy.
    `external_fallback_roles` is the compatibility spelling for validation fallbacks;
    `trusted_in_process_roles` also covers task-owned consumers such as Repo onboarding. Excluding
    an external-only role removes requiredness, not the contradiction check on an explicit key that
    the secret-scrubbed subprocess cannot consume.
    """
    shared_active, checked = llm_credential_consumers(settings)
    if consumer_roles is not None:
        checked = set(consumer_roles)
    else:
        checked = set(checked)
    trusted_roles = set(external_fallback_roles or ()) | set(trusted_in_process_roles or ())
    # A live external-only scoped plan can legitimately have no checked Developer roles, but its
    # explicit dedicated-key contradiction still has to be audited below. Toy mode has no client.
    if not shared_active and not checked and getattr(settings, "backend", "toy") != "llm":
        return
    # Insertion-ordered {role-neutral cause -> the role labels it breaks}.
    failures: dict[str, list[str]] = {}

    def _record(exc: BaseException, role_label: str | None = None) -> None:
        failures.setdefault(credential_cause(exc), []).extend(
            [role_label] if role_label else [])
    # External coding-agent processes are deliberately launched with every secret-looking
    # environment variable removed. They may use their own local credential store, but a
    # LoopLab-managed shared/profile key can never reach them. Reject that contradictory setup
    # before the run starts instead of validating a credential the selected Developer then loses.
    if getattr(settings, "developer_backend", "default") != "default":
        external_roles = ({"implement", "repair"}
                          if getattr(settings, "unified_agent", False)
                          else {"developer"})
        for role in sorted(external_roles):
            # This role is consumed by a trusted IN-PROCESS fallback/onboarder, not by the nested
            # coding-agent process. The latter still receives cli_agent.py's scrubbed environment.
            if role in trusted_roles:
                continue
            # A task-scoped plan may remove a genuinely external-only role from REQUIRED consumers,
            # so its key need not exist and its endpoint is not probed. An explicit dedicated key is
            # still a contradictory promise: the scrubbed coding subprocess can never receive it.
            target = resolve_llm_target(settings, role=role)
            # A dedicated role-profile key is an explicit promise that LoopLab will supply that
            # exact variable to this consumer, which the external-process isolation contract
            # forbids. A shared key may legitimately serve the in-process validation fallback while
            # the coding tool authenticates from its own store, so do not reject that combination.
            if target.api_key_env:
                failures.setdefault(
                    f"external developer backend {settings.developer_backend!r} cannot use the "
                    "LoopLab-managed credential selected for it; external coding tools are "
                    "launched without inherited secrets. Remove api_key_env from that role's "
                    "profile, then use a credentialless/local endpoint or configure the coding "
                    "tool's own credential store.", []).append(role)
    if shared_active:
        try:
            # Compatibility path for an explicitly declared raw shared client.
            bound_api_key_for(settings, settings.llm_base_url)
        except LLMError as exc:
            _record(exc, "the shared target")
    for role in sorted(checked, key=lambda item: item or ""):
        label = role or "the default target"
        target = resolve_llm_target(settings, role=role)
        env = target.api_key_env
        if env:
            try:
                client_kwargs_for(target, role=role)
            except LLMError as exc:
                _record(exc, label)
            continue
        if target.credential_mode == "shared":
            try:
                bound_api_key_for(settings, target.base_url)
            except LLMError as exc:
                _record(exc, label)
        elif target.credential_mode == "none":
            # `resolve_llm_target` deliberately drops api_key_env after an endpoint override. Keep
            # the original profile intent in the preflight decision: otherwise a profile key bound
            # to A plus a role/stage override to B passed whenever no shared key happened to exist,
            # spawned the engine, and only then made an unauthenticated request to B.
            configured_profile_env = role_profile(settings, role).get("api_key_env")
            if configured_profile_env:
                failures.setdefault(
                    f"the endpoint of profile credential {configured_profile_env} is overridden "
                    "for this role; bind that role to a matching profile", []).append(label)
    if failures:
        raise LLMCredentialError(
            "LLM credential preflight failed: " + render_credential_failures(failures))


def make_llm_client(settings, *, model: str | None = None,
                    base_url: str | None = None,
                    timeout: float | None = None,
                    temperature: float | None = None,
                    api_key=None,
                    api_key_base_url: str | None = None,
                    max_retries: int | None = None,
                    stream: bool | None = None,
                    disable_reasoning: bool = False,
                    wall_timeout: float | None = None,
                    retry_after_cap: float | None = None,
                    cache: bool | None = None) -> OpenAICompatibleClient:
    """The one Settings -> live client factory (used by cli, serve, adapters and the agent loop).
    Historically lived in adapters/tasks.py — the only reason `agents` ever imported `adapters` —
    but constructing an LLM client is a foundation (core) capability; both old import paths keep
    resolving via re-exports (adapters.tasks and looplab.serve.server, the monkeypatch point).

    `api_key` overrides the shared `settings.llm_api_key` for ONE client — the per-role credential
    path (`make_llm_client_for`). Probe controls are additive and preserve every ordinary caller's
    historical defaults."""
    endpoint = normalize_llm_base_url(base_url or settings.llm_base_url)
    # A direct endpoint override is not permission to send the shared key there. Role/profile callers
    # either pass an explicitly bound key or the NO_CREDENTIAL sentinel; protect ad-hoc callers too.
    if (api_key is None and base_url is not None
            and endpoint != normalize_llm_base_url(settings.llm_base_url)):
        api_key = NO_CREDENTIAL
    key = bound_api_key_for(
        settings, endpoint, api_key=api_key, api_key_base_url=api_key_base_url)
    mdl = model or settings.llm_model
    reasoning = ({} if disable_reasoning else
                 reasoning_body(mdl, getattr(settings, "llm_reasoning", ""),
                                getattr(settings, "llm_reasoning_style", "auto"),
                                getattr(settings, "llm_reasoning_extra", None)))
    # `timeout` lets a caller bound a UI-side probe (e.g. the health check) well under a proxy's
    # gateway timeout; omitted -> the run-wide `llm_timeout` setting (idle/stall limit, default 180s).
    extra = {"timeout": timeout if timeout is not None
             else float(getattr(settings, "llm_timeout", 180.0) or 180.0)}
    # Probe-only transport controls are additive and omitted for every historical caller. Keeping
    # the ordinary constructor defaults out of kwargs also preserves simple patch/test factories.
    if max_retries is not None:
        extra["max_retries"] = max_retries
    if wall_timeout is not None:
        extra["wall_timeout"] = wall_timeout
    if retry_after_cap is not None:
        extra["retry_after_cap"] = retry_after_cap
    return OpenAICompatibleClient(
        model=mdl, base_url=endpoint, api_key=key,
        temperature=(temperature if temperature is not None else settings.llm_temperature),
        accountant=CostAccountant(),
        guided_json=getattr(settings, "llm_guided_json", False),   # H1 constrained decoding
        reasoning=reasoning,                                        # provider-aware thinking toggle
        stream=(getattr(settings, "llm_stream", True) if stream is None else stream),
        # Fall back to the CONSTANT this module declares as the single source of the default (which
        # config.py imports for its own field default) — a literal here would drift the moment it moved.
        header_timeout=float(getattr(settings, "llm_header_timeout", DEFAULT_HEADER_TIMEOUT_S)
                             or DEFAULT_HEADER_TIMEOUT_S),
        trust_env=bool(getattr(settings, "llm_trust_env", False)),  # direct-connect by default (bypass proxy)
        cache=(getattr(settings, "llm_cache", False) if cache is None else cache),
        **extra,
    )
