"""LLM client + cost accounting (I2/I13, ADR-14/17/11).

`OpenAICompatibleClient` is the LIVE path — an OpenAI-compatible chat client on the
openai SDK (httpx transport), whose per-read timeout reliably bounds a stalled stream.
`LiteLLMClient` wraps LiteLLM (lazy import) as the documented production gateway. The
openai/httpx imports are declared deps but GUARDED, so the offline engine + replay still
import this module (via `core.config`) without the live LLM stack. `CostAccountant`
tallies per-call cost with warn/hard-stop thresholds and raises `BudgetExceeded` at 100%.
Secrets are never stored as values here — the client reads the key from config/env.

The retry/error-classification, SSE-stream, and tool-call-parsing helpers live in the flat
siblings `llm_transient` / `llm_streaming` / `llm_toolcall` and are re-imported below under
their original names, so every historical import/monkeypatch path through this module holds.
"""
from __future__ import annotations

import copy
import json
import math
import sys
import threading
import time
from typing import Callable, Optional

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

# `urllib.request` is monkeypatched THROUGH this module by direct unit tests
# (`llm.urllib.request.urlopen`); the LIVE path goes through the openai SDK
# (see OpenAICompatibleClient._sdk_chat). (`ssl` moved to `llm_transient` alongside the
# SDK-path error classifier that uses it.)
import urllib.request  # noqa: F401

from looplab.core import tracing
from looplab.core.llm_broker import llm_request_permit
# Re-exported for backward compatibility: dozens of importers (and tests) do
# `from looplab.core.llm import LLMError / BudgetExceeded`. The definitions live in
# `looplab.core.errors` so `parse` can import them without importing this module.
from looplab.core.errors import BudgetExceeded, LLMError  # noqa: F401
# Safe top-level import (no cycle): parse imports only from looplab.core.errors now.
from looplab.core.parse import split_think  # noqa: F401  (also a re-export)
# Split siblings (docs/15 §P5.2): retry/backoff + error classification (`llm_transient`), the
# SSE/stream machinery (`llm_streaming`), and native tool-call / assistant-message parsing
# (`llm_toolcall`) were split out of this module. Every moved name is RE-IMPORTED here under its
# original name because tests and callers import/monkeypatch them THROUGH this module — both
# `looplab.core.llm._X` and the flat `looplab.llm._X` must keep resolving to the SAME objects.
from looplab.core.llm_transient import (  # noqa: F401
    BACKOFF_CAP_S, RETRY_AFTER_CAP_S, _REASONING_REJECT_KEYS, _backoff, _err_body,
    _is_reasoning_reject, _is_throttle_403, _retry_after_of, _retry_after_seconds, _sdk_transient)
from looplab.core.llm_streaming import (  # noqa: F401
    _SSETail, _chunk_has_content, _parse_chat_body, _raw_socket, _shutdown_pool_sockets,
    _socket_watchdog, _sse_chunks, _stream_raw_socket, _stream_with_idle_guard)
from looplab.core.llm_toolcall import (  # noqa: F401
    _ANSWER_FIELDS, _CODE_SPAN_RE, _FINAL_NAMES, _NATIVE_INVOKE_RE, _NATIVE_OPEN_RE,
    _NATIVE_PARAM_RE, _apply_native_tool_calls, _args_complete, _assistant_text, _clean_thinking,
    _code_spans, _extract_native_tool_calls, _reasoning_of, _tool_call_slot)

# Named stream/timeout constants (previously inline magic numbers). Their retry/backoff siblings
# (BACKOFF_CAP_S / RETRY_AFTER_CAP_S) live in `llm_transient` and are re-imported above.
STREAM_STALL_DEGRADE_AFTER = 2       # stream stalls before this client goes non-streaming for good
# Default first-byte (response-headers) window, seconds. The single source: config.py's
# `Settings.llm_header_timeout` imports this constant as its field default.
DEFAULT_HEADER_TIMEOUT_S = 45.0

# Provider usage is untrusted JSON.  A signed 64-bit ceiling is far above any real context/call
# count, remains exactly representable by the durable integer stores used by LoopLab, and prevents a
# hand-written/hostile ``10**400`` value from turning every later roll-up into an enormous bigint.
_MAX_USAGE_TOKENS = (1 << 63) - 1
_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _safe_cost(value) -> float:
    """Return a cost only when it is a finite, non-negative numeric value.

    Cost is budget-enforcement input, not merely telemetry. Strings and booleans are malformed,
    while NaN/Infinity can poison comparisons and roll-ups. Decimal-like numeric values remain
    valid for internal gateways such as LiteLLM. Every rejected/absent value degrades to the
    local-model default of zero instead of crashing a completed LLM call or reducing spend.
    """
    if value is None or isinstance(value, (bool, str, bytes, bytearray)):
        return 0.0
    try:
        cost = float(value)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    if not math.isfinite(cost) or cost < 0.0:
        return 0.0
    return 0.0 if cost == 0.0 else cost  # canonicalize provider ``-0`` as ordinary zero


def _usage_cost(usage) -> float:
    """Extract OpenRouter-style JSON ``usage.cost`` without trusting provider payload types."""
    value = usage.get("cost") if isinstance(usage, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return _safe_cost(value)


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
    }


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


class OpenAICompatibleClient:
    """OpenAI-compatible chat client on the openai SDK (httpx transport). Implements the
    `parse.LLMClient` Protocol, so it drops into the LLM roles like any other backend.

    Works against ANY OpenAI-compatible endpoint — Ollama (`/v1`), SGLang, vLLM, or
    the OpenAI API itself — so the serving backend is a base_url change, not code. The
    SDK's per-read httpx timeout is what reliably bounds a stalled mid-stream read (the
    stdlib-urllib transport this replaced could not interrupt one). LiteLLM remains the
    documented production gateway (see `LiteLLMClient`)."""

    def __init__(self, model: str, base_url: str = "http://localhost:11434/v1",
                 api_key: str = "ollama", temperature: float = 0.7,
                 timeout: float = 180.0, accountant: Optional["CostAccountant"] = None,
                 guided_json: bool = False, reasoning: Optional[dict] = None,
                 stream: bool = True, cache: bool = False,
                 header_timeout: Optional[float] = None, trust_env: bool = False):
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
        # `header_timeout` bounds the TCP/TLS CONNECT (httpx `connect=`, see `_new_sdk`), so a connection
        # that never ESTABLISHES fails over fast instead of waiting the full idle `timeout`. After headers
        # arrive, `_accumulate_stream` reuses it as the first-SSE-EVENT budget (and the idle `timeout` for
        # the inter-token body). KNOWN LIMITATION (do not over-claim): on the STREAM path it does NOT yet
        # bound the wait for HTTP response HEADERS once the socket is connected — that read is a `read`
        # timeout, so `_sdk.chat.completions.create()` blocks up to the idle `timeout` before failover.
        # An endpoint that completes TLS then sends no headers is therefore failed over after `timeout`,
        # not `header_timeout`; bounding the header-read too needs the same wall-clock worker-thread guard
        # `_nonstream_bounded` already uses (its stream body would then be read cross-thread — deferred to
        # a change that can be validated against a real streaming endpoint, not a fake SDK).
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
        self._max_retries = 8               # 429/5xx/throttle-403 backoff retries before surfacing an
        #   LLMError. 8 (≈150s: 2+4+8+16+30+30+30+30) rides out the gateway's COLD-START throttle: after
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
        self._cache: Optional[dict] = {} if cache else None
        # Transport: the openai SDK over an httpx client. `connect` bounds TCP/TLS establishment
        # (=header_timeout); `read` = the inter-read idle limit (a long-but-alive generation keeps
        # resetting it, so it's never cut off). httpx's `read` timeout can't catch an SSE
        # keepalive-trickle (bytes reset it while no data EVENT arrives) — that is enforced by
        # `_accumulate_stream`'s idle/first-event guard on the STREAM path (idle_limit=timeout;
        # first_byte_limit=header_timeout bounds the first EVENT AFTER headers, NOT the header-read
        # itself — see the `header_timeout` note above). The per-request timeout lives on the OpenAI client
        # (`timeout=`), which wins over the http_client's; the http_client exists only to set
        # `trust_env=False` (the internal endpoint needs a DIRECT connection — no proxy env). See
        # `llm_trust_env` if a proxy/custom-CA is required. max_retries=0: we own the retry loop.
        self._trust_env = trust_env
        self._sdk = self._new_sdk()

    def _new_sdk(self):
        """Build (or rebuild) the openai SDK client. Rebuilt after a bounded-non-stream abort closes
        the httpx client to interrupt a trickled body read (a closed client is unusable afterwards)."""
        return openai.OpenAI(
            base_url=self.base_url, api_key=self.api_key, max_retries=0,
            timeout=httpx.Timeout(read=self.timeout, connect=self.header_timeout, write=30.0, pool=10.0),
            http_client=httpx.Client(trust_env=self._trust_env))

    def _sdk_chat(self, payload: dict, use_stream: bool) -> dict:
        """The single transport seam: one openai-SDK chat call, returned in the legacy body shape
        ({"choices":[{"message":{content,reasoning?,tool_calls?},"finish_reason"}],"usage"}) the rest
        of the client expects. Non-provider params (a reasoning toggle, vLLM `guided_json`) ride in
        `extra_body`. Streaming accumulates deltas exactly like the old `_read_stream`; a stalled
        stream raises openai.APITimeoutError from httpx's read timeout — no watchdog needed. Tests
        monkeypatch THIS method (not urllib) to script transport behaviour."""
        kwargs: dict = {"model": payload["model"], "messages": payload["messages"],
                        "temperature": payload.get("temperature", self.temperature)}
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
            kwargs["stream_options"] = {"include_usage": True}
            return self._accumulate_stream(self._sdk.chat.completions.create(**kwargs),
                                           self.timeout, self.header_timeout)
        return self._nonstream_bounded(kwargs)

    def _nonstream_bounded(self, kwargs: dict) -> dict:
        """A NON-STREAM chat call bounded by a wall-clock deadline. httpx's per-read timeout can't catch
        a proxy that TRICKLES the response body (a byte resets the read timer while the payload never
        completes — the keepalive pathology the stream idle-guard exists for, but there's no SSE loop
        to guard here). Run the blocking call in a worker thread; if it overruns the read+connect
        budget, ABORT: `socket.shutdown()` the in-flight connection (forces a recv() wedged in the
        kernel to return — close() alone can't), close + rebuild the httpx client, and raise
        APITimeoutError so `_post` retries/degrades instead of hanging. The wall-clock join guarantees
        the CALLER unblocks, and the socket-shutdown guarantees the WORKER thread exits too (no lingering
        daemons accumulating across a long run on a flaky endpoint — the pre-shutdown behaviour that
        leaked ~one thread per wedged call)."""
        box: dict = {}

        def _call():
            try:
                box["resp"] = self._sdk.chat.completions.create(**kwargs)
            except BaseException as e:  # noqa: BLE001 — ferry ANY error back to the caller thread
                box["exc"] = e

        th = threading.Thread(target=_call, daemon=True)
        th.start()
        th.join(self.timeout + self.header_timeout + 10)
        if th.is_alive():
            # Force the wedged recv() to return so the worker thread EXITS (doesn't linger): shutdown the
            # in-flight connection's socket BEFORE close() — close() can't interrupt a kernel read, only
            # socket.shutdown() can. Then close()+rebuild the client for the next call.
            # getattr guard (D8b): an SDK shape WITHOUT `_client` (a mock in tests, a foreign SDK)
            # must still reach the intended APITimeoutError below — a bare `self._sdk._client` here
            # turned the timeout into an AttributeError (`_shutdown_pool_sockets` no-ops on None).
            _shutdown_pool_sockets(getattr(self._sdk, "_client", None))
            try:
                self._sdk._client.close()
            except Exception:  # noqa: BLE001
                pass
            th.join(5)     # after the shutdown the recv errors out, so the daemon is reaped here
            self._sdk = self._new_sdk()
            raise openai.APITimeoutError(request=httpx.Request("POST", self.base_url))
        if "exc" in box:
            raise box["exc"]
        return box["resp"].model_dump()

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
                usage = ev.usage.model_dump()
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

    def _read_stream(self, resp) -> dict:
        """Reassemble an OpenAI SSE stream into the non-streaming response body shape
        ({"choices":[{"message":{content,reasoning?,tool_calls?}, "finish_reason"}], "usage"}). Each
        `for raw in resp` line is bounded by the socket `timeout`, so with streaming that timeout is an
        INTER-TOKEN idle timeout: a stall (no line for `timeout` s) raises socket.timeout here and
        propagates to `_post`'s transient-retry path; a steady stream is never cut off however long the
        generation runs. tool_call deltas are merged by `index` (partial name/arguments concatenated)."""
        content: list[str] = []
        reasoning: list[str] = []
        tcs: dict[int, dict] = {}
        finish = None
        usage: dict = {}
        tail = _SSETail()                       # raw lines + got-SSE flag for the non-SSE fallback
        # Idle timeout by PROGRESS: tracked as wall-clock since the last REAL token and enforced both
        # by an in-loop check (in `_sse_chunks`) AND a socket-shutdown watchdog (`_socket_watchdog`)
        # — a stall can hide from either alone (keepalive heartbeats reset the read timeout; a
        # partial-line trickle blocks in recv() before the in-loop check runs). A long-but-alive
        # generation keeps emitting content, resetting the clock — no hard deadline.
        idle_limit = self.timeout
        last_progress, _killed, _stop_wd = _socket_watchdog(resp, idle_limit)
        stall_msg = (f"LLM stream stalled — no new tokens for {idle_limit:.0f}s; "
                     f"retrying on a fresh connection")
        try:
            try:
                lines = iter(resp)              # SSE responses iterate line-by-line
            except TypeError:
                lines = iter(())                # a non-iterable body (e.g. a test mock) -> read() below
            for obj in _sse_chunks(lines, last_progress, _killed, idle_limit, stall_msg, tail=tail):
                if obj.get("usage"):
                    usage = obj["usage"]
                    last_progress[0] = time.monotonic()
                ch = (obj.get("choices") or [{}])[0] or {}
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                    last_progress[0] = time.monotonic()   # real token => progress
                r = delta.get("reasoning") or delta.get("reasoning_content")
                if r:
                    reasoning.append(r)
                    last_progress[0] = time.monotonic()
                for tc in (delta.get("tool_calls") or []):
                    last_progress[0] = time.monotonic()
                    idx = _tool_call_slot(tcs, tc)   # provider-omitted `index` must not collapse calls
                    slot = tcs.setdefault(idx,
                                          {"id": None, "type": "function",
                                           "function": {"name": "", "arguments": []}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"].append(fn["arguments"])
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
        finally:
            _stop_wd()                          # stop the watchdog before we return / propagate
        if _killed[0]:                          # watchdog shut the socket -> a stall, not a real end;
            raise TimeoutError(stall_msg)       # _post catches -> retry
        # Not actually an SSE stream (a non-streaming endpoint, or a test mock that returns one JSON
        # body): parse the whole body as a normal chat completion so streaming stays transparent.
        if not tail.got_sse and not content and not tcs:
            blob = "".join(tail.raw_lines)
            if not blob and hasattr(resp, "read"):
                try:
                    blob = resp.read().decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    blob = ""
            whole = _parse_chat_body(blob)
            if whole is not None:               # any parsed JSON body (incl. an {"error":…} envelope,
                return whole                    # which the caller fails fast on at the no-choices check)
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
                           "response_format": payload.get("response_format")},
                          sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _post(self, payload: dict) -> dict:
        # T7 LLM response cache: serve an identical DETERMINISTIC (temp 0) request from cache instead
        # of re-hitting the model — cuts cost on retry/panel/verify flows. Sampling calls (temp>0)
        # are never cached (see _cache_key). Replay itself never calls the model (Ideas are recorded
        # in events), so this is a within-run/live cost saver, not a correctness dependency.
        ck = self._cache_key(payload)
        if ck is not None and ck in self._cache:
            cached = copy.deepcopy(self._cache[ck])
            # A cache hit performs no provider work. Zero every billed usage counter in this call's
            # copy: otherwise trace aggregation would duplicate both tokens and paid cost even though
            # CostAccountant correctly skips cache hits. The original cached body remains untouched.
            usage = _normalize_usage(cached.get("usage"))
            for field in _USAGE_FIELDS:
                usage[field] = 0
            usage["cost"] = 0.0
            cached["usage"] = usage
            # Restore the per-call telemetry a live call would have set, and hand back a DEEP COPY:
            # downstream (e.g. complete_text -> _apply_native_tool_calls) mutates the message in
            # place, which would otherwise corrupt the shared cached entry for every later hit.
            self._last_usage = usage
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
            # Build the request per attempt so a param-compat retry (below) can drop the reasoning
            # toggle. `_reasoning_ok` starts True and flips off permanently for THIS client the first
            # time the endpoint rejects our reasoning param.
            # Stall-degrade: stream by default (httpx read-timeout = inter-token idle guard), but drop
            # to a single blocking read for the attempt right after a stream stall, and permanently
            # once this client has stalled STREAM_STALL_DEGRADE_AFTER times — a flaky proxied endpoint
            # often answers the SAME request fine without SSE while its stream wedges mid-generation.
            use_stream = (self.stream and self._stream_stalls < STREAM_STALL_DEGRADE_AFTER
                          and not _stalled_prev)
            try:
                # CODEX AGENT: admit immediately around the real provider attempt, not around a
                # whole node build. Retries take fresh fair turns and nested build -> novelty work
                # cannot retain a build permit while asking for another lane at total=1.
                with llm_request_permit():
                    parsed = self._sdk_chat(payload, use_stream)
            except openai.BadRequestError as e:
                # A 400 that rejects our REASONING toggle — a litellm-proxied model like glm-5.1
                # returns UnsupportedParamsError for `reasoning_effort` — isn't a real bad request:
                # drop reasoning for this client and retry (deepseek keeps it; glm-5.1 adapts).
                if self.reasoning and self._reasoning_ok and _is_reasoning_reject(_err_body(e)):
                    self._reasoning_ok = False   # permanent for this client: the NEXT request drops the param
                    if attempt < self._max_retries:
                        continue                 # a remaining iteration re-issues with reasoning dropped
                    # On the LAST attempt the loop can't retry — but `_reasoning_ok` is now False, so the
                    # caller's retry/fallback WILL succeed. Surface a CLEAR reason instead of falling
                    # through to the generic, misleading "no response after retries" (every sibling retry
                    # branch guards on attempt<_max_retries; this one silently did not).
                    raise LLMError(f"LLM request to {self.base_url} rejected the reasoning param on the "
                                   f"final attempt; reasoning is now disabled for this client so a retry "
                                   f"will succeed: {e}") from e
                raise LLMError(f"LLM request to {self.base_url} failed: {e}") from e
            except openai.AuthenticationError as e:
                raise LLMError(f"LLM request to {self.base_url} failed: {e} — check the API key "
                               "(LOOPLAB_LLM_API_KEY)") from e
            except (openai.RateLimitError, openai.InternalServerError) as e:   # 429 / 5xx
                if attempt < self._max_retries:
                    ra = _retry_after_seconds(_retry_after_of(e))
                    # Honor a POSITIVE server Retry-After up to RETRY_AFTER_CAP_S (a directive); otherwise
                    # use our own exponential backoff (already ≤ BACKOFF_CAP_S). `if ra` (not `is not
                    # None`): `_retry_after_seconds` clamps to max(0.0, …), so a `Retry-After: 0`, a
                    # negative value, or an HTTP-date already in the PAST (clock skew) yields ra==0.0 —
                    # honoring that would sleep(0) and burn every retry in milliseconds, defeating the
                    # 429/5xx backoff entirely. Treat a non-positive directive as "unusable" → backoff.
                    time.sleep(min(ra, RETRY_AFTER_CAP_S) if ra else _backoff(attempt))
                    continue
                raise LLMError(f"LLM request to {self.base_url} failed: {e}") from e
            except openai.APIConnectionError as e:
                # httpx transport failure. APITimeoutError (a subclass) = a read/connect timeout: a
                # stalled mid-stream read or a black-holed request — always transient, and the
                # reliable interrupt the urllib+ssl path lacked (a glm SSE stall hung for minutes).
                # A plain APIConnectionError is retried only when `_sdk_transient` says so (reset/EOF),
                # else it fails fast (refused/DNS/cert). A STREAM stall degrades the next attempt to
                # non-stream and ratchets the permanent-degrade counter. This ALSO catches a raw httpx
                # stream-body error that `_stream_with_idle_guard` normalized to APIConnectionError
                # (the SDK leaves those unwrapped) — `_sdk_transient` reads its httpx __cause__.
                is_timeout = isinstance(e, openai.APITimeoutError)
                transient = is_timeout or _sdk_transient(e)
                if use_stream and transient:
                    _stalled_prev = True
                    self._stream_stalls += 1
                if transient and attempt < self._max_retries:
                    time.sleep(_backoff(attempt))
                    continue
                raise LLMError(f"LLM request to {self.base_url} failed: {e}") from e
            except openai.PermissionDeniedError as e:   # 403 — often a burst/rate-limit throttle, not hard-forbidden
                if _is_throttle_403(_err_body(e)) and attempt < self._max_retries:
                    time.sleep(_backoff(attempt))
                    continue
                raise LLMError(f"LLM request to {self.base_url} failed: {e}") from e
            except openai.APIError as e:   # any other SDK-level protocol error -> clean LLMError
                raise LLMError(f"LLM request to {self.base_url} failed: {e}") from e
            except json.JSONDecodeError as e:
                # A gateway 200 with an empty / whitespace / keepalive-only body makes the SDK's
                # decoder raise a RAW json.JSONDecodeError — NOT an openai.APIError — so without this
                # it'd escape `_post` uncaught and abort the run (the role layer only retries+falls
                # back on LLMError). Mirror the old `_parse_chat_body`-None path: a transient gateway
                # hiccup — retry with backoff, then a clean LLMError. NARROW on purpose: a ValueError/
                # AttributeError from our own _accumulate_stream/_tool_call_slot code must NOT be
                # masked here as a "gateway hiccup" — let a real accumulation bug propagate loudly.
                if attempt < self._max_retries:
                    time.sleep(_backoff(attempt))
                    continue
                raise LLMError(f"LLM request to {self.base_url} returned an unparseable body") from e
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
                ch0 = ((parsed.get("choices") or [{}])[0] or {}) if parsed else {}
                m = ch0.get("message") or {}
                # A stream is a "keepalive-only stall" ONLY when it produced NOTHING usable: no
                # content, no tool_calls, no reasoning, AND no finish_reason. A reasoning model that
                # hit its length limit while thinking (finish_reason="length", non-empty `reasoning`,
                # empty `content`) is a REAL — if truncated — response, not a stall: retrying it 5×
                # regenerates minutes of reasoning tokens and ratchets `_stream_stalls` to the
                # permanent-degrade threshold, turning the idle timeout into a hard deadline for the
                # rest of the run. finish_reason present (even "stop" with empty content) likewise
                # means the endpoint answered — return it and let the no-choices check decide.
                empty_stream = (use_stream and parsed is not None and parsed.get("choices")
                                and not m.get("content") and not m.get("tool_calls")
                                and not m.get("reasoning") and not ch0.get("finish_reason"))
                if parsed is not None and not empty_stream:
                    body = parsed
                    break
                if empty_stream:                # keepalive-only stream = the same stall family
                    _stalled_prev = True
                    self._stream_stalls += 1
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
        if ck is not None:                       # T7: cache a COPY (the returned body is mutated
            self._cache[ck] = copy.deepcopy(body)  # in place by callers — keep the cached entry clean)
        return body

    def _model_params(self) -> dict:
        """The generation's model_parameters (Langfuse generation metadata): sampling temperature +
        any provider reasoning toggle, so the trace shows HOW the model was called."""
        return {"temperature": self.temperature, **(self.reasoning or {})}

    def complete_text(self, messages: list[dict]) -> str:
        with tracing.generation(op="complete_text", model=self.model, messages=messages,
                                model_parameters=self._model_params()) as gen:
            body = self._post({"model": self.model, "messages": messages,
                               "temperature": self.temperature, "stream": False})
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
        # The generation span stays open for the whole stream (its duration = time-to-full-answer).
        with tracing.generation(op="complete_text_stream", model=self.model, messages=messages,
                                model_parameters=self._model_params()) as gen:
            try:
                # Reasoning-param retry: like `_post`, a model that 400s on our reasoning toggle
                # retries once without it instead of silently losing streaming forever.
                for _attempt in range(2):
                    kwargs: dict = {"model": self.model, "messages": messages,
                                    "temperature": self.temperature, "stream": True,
                                    "stream_options": {"include_usage": True}}
                    if self.reasoning and self._reasoning_ok:
                        kwargs["extra_body"] = dict(self.reasoning)
                    try:
                        # Capture usage BEFORE yielding a co-located delta: if a consumer closes or
                        # cancels while suspended at that yield, the finally block still charges it.
                        # Keep a stream's slot until its final chunk (or consumer close). The context
                        # exits before either blocking fallback below, so total=1 cannot self-deadlock.
                        with llm_request_permit():
                            for ev in _stream_with_idle_guard(
                                    self._sdk.chat.completions.create(**kwargs),
                                    self.timeout, self.header_timeout):
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
                        stream_completed = True
                        break                    # streamed (or cleanly ended) -> done
                    except openai.BadRequestError as e:
                        if (self.reasoning and self._reasoning_ok and not pieces and _attempt == 0
                                and _is_reasoning_reject(_err_body(e))):
                            self._reasoning_ok = False
                            continue             # retry the stream once without the reasoning toggle
                        if not pieces:           # any other bad request -> blocking fallback
                            delegated_to_fallback = True
                            text = self.complete_text(messages)
                            if text:
                                yield text
                            return
                        break
                    except openai.APIError:
                        # A fallback owns/accountants its own provider call.  The outer stream still
                        # records independently if it had already observed provider usage.
                        if not pieces:
                            delegated_to_fallback = True
                            text = self.complete_text(messages)
                            if text:
                                yield text
                            return
                        break
            finally:
                # A clean stream is one logical call even if usage is absent. Once content was
                # yielded, a consumer close/cancel also records the known call even when this
                # provider sends usage only in an unread final chunk; its unknown cost/tokens remain
                # zero rather than pretending the partial generation was free or fully measured.
                # A blocking fallback owns its own successful provider call.
                account_here = usage_observed or (
                    not delegated_to_fallback and (stream_completed or bool(pieces)))
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
                "tool_choice": tool_choice, "temperature": self.temperature, "stream": False,
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
            "temperature": self.temperature, "stream": False,
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
            args = calls[0]["function"]["arguments"]
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
            candidate_peak = max(self.peak_prompt, delta["prompt_tokens"])
            candidate_warned = self.warned
            if (self.limit is not None and not candidate_warned
                    and candidate_spent >= self.warn_frac * self.limit):
                candidate_warned = True
            exceeded = self.limit is not None and candidate_spent >= self.limit

            self.spent = candidate_spent
            self.calls = candidate_calls
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
    """Real backend. Implements the `parse.LLMClient` Protocol. Lazy-imports litellm
    so the package installs and tests run without it."""

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
                # CODEX AGENT: match the OpenAI-compatible transport seam. One attempt borrows one
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
        raise LLMError(f"litellm completion for {self.model} failed: {last}")

    def _account(self, resp) -> None:
        self.accountant.add(self._cost(resp), usage=self._usage(resp))

    def _usage(self, resp) -> Optional[dict]:
        try:
            u = resp.usage
            return _normalize_usage({
                "prompt_tokens": getattr(u, "prompt_tokens", 0),
                "completion_tokens": getattr(u, "completion_tokens", 0),
                "total_tokens": getattr(u, "total_tokens", 0),
            })
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
            args = calls[0].function.arguments
            m = resp.choices[0].message
            # Not `_reasoning_of`: object attributes + reasoning_content-first (see complete_text).
            thinking, _ = _clean_thinking(
                getattr(m, "content", None) or "",
                getattr(m, "reasoning_content", None) or getattr(m, "reasoning", None) or "")
            gen.output(args if isinstance(args, str) else json.dumps(args)).thinking(thinking) \
               .usage(self._usage(resp)).cost(_safe_cost(self._cost(resp)))
            return json.loads(args) if isinstance(args, str) else (args or {})


def make_llm_client(settings, *, model: str | None = None,
                    base_url: str | None = None,
                    timeout: float | None = None,
                    temperature: float | None = None) -> OpenAICompatibleClient:
    """The one Settings -> live client factory (used by cli, serve, adapters and the agent loop).
    Historically lived in adapters/tasks.py — the only reason `agents` ever imported `adapters` —
    but constructing an LLM client is a foundation (core) capability; both old import paths keep
    resolving via re-exports (adapters.tasks and looplab.serve.server, the monkeypatch point)."""
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else "local"
    mdl = model or settings.llm_model
    reasoning = reasoning_body(mdl, getattr(settings, "llm_reasoning", ""),
                               getattr(settings, "llm_reasoning_style", "auto"),
                               getattr(settings, "llm_reasoning_extra", None))
    # `timeout` lets a caller bound a UI-side probe (e.g. the health check) well under a proxy's
    # gateway timeout; omitted -> the run-wide `llm_timeout` setting (idle/stall limit, default 180s).
    extra = {"timeout": timeout if timeout is not None
             else float(getattr(settings, "llm_timeout", 180.0) or 180.0)}
    return OpenAICompatibleClient(
        model=mdl, base_url=base_url or settings.llm_base_url, api_key=key,
        temperature=(temperature if temperature is not None else settings.llm_temperature),
        accountant=CostAccountant(),
        guided_json=getattr(settings, "llm_guided_json", False),   # H1 constrained decoding
        reasoning=reasoning,                                        # provider-aware thinking toggle
        stream=getattr(settings, "llm_stream", True),              # inter-token idle-timeout via SSE
        header_timeout=float(getattr(settings, "llm_header_timeout", 45.0) or 45.0),
        trust_env=bool(getattr(settings, "llm_trust_env", False)),  # direct-connect by default (bypass proxy)
        cache=getattr(settings, "llm_cache", False),               # T7 deterministic-response cache
        **extra,
    )
