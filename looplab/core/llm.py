"""LLM client + cost accounting (I2/I13, ADR-14/17/11).

`LiteLLMClient` wraps LiteLLM (lazy import — not required for the offline path or
tests). `CostAccountant` tallies per-call cost with warn/hard-stop thresholds and
raises `BudgetExceeded` at 100%. Secrets are never stored as values here — the
client takes a model name and reads the key from the environment via LiteLLM.
"""
from __future__ import annotations

import copy
import http.client
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from typing import Optional

from looplab.core import tracing
# Re-exported for backward compatibility: dozens of importers (and tests) do
# `from looplab.core.llm import LLMError / BudgetExceeded`. The definitions live in
# `looplab.core.errors` so `parse` can import them without importing this module.
from looplab.core.errors import BudgetExceeded, LLMError  # noqa: F401
# Safe top-level import (no cycle): parse imports only from looplab.core.errors now.
from looplab.core.parse import split_think  # noqa: F401  (also a re-export)

# Named retry/backoff constants (previously inline magic numbers).
BACKOFF_CAP_S = 30.0                 # ceiling on any single exponential-backoff sleep
STREAM_STALL_DEGRADE_AFTER = 2       # stream stalls before this client goes non-streaming for good
# Default first-byte (response-headers) window, seconds. The single source: config.py's
# `Settings.llm_header_timeout` imports this constant as its field default.
DEFAULT_HEADER_TIMEOUT_S = 45.0


def _backoff(attempt: int) -> float:
    """Exponential-backoff delay for retry `attempt` (0-based), capped at BACKOFF_CAP_S."""
    return min(2.0 * (2 ** attempt), BACKOFF_CAP_S)


def _raw_socket(resp):
    """Best-effort grab of the raw socket behind a urllib response (http.client wraps it in a
    BufferedReader over a SocketIO). None for a non-socket body (e.g. a test mock)."""
    fp = getattr(resp, "fp", None)
    return getattr(getattr(fp, "raw", None), "_sock", None) or getattr(fp, "_sock", None)


# Substrings that mark an HTTP 400 as "this endpoint rejects our REASONING toggle" (e.g. a
# litellm-proxied model returning UnsupportedParamsError for `reasoning_effort`) rather than a
# genuine bad request — shared by `_post` and `complete_text_stream`.
_REASONING_REJECT_KEYS = ("reasoning", "unsupportedparams", "does not support parameters",
                          "extra_forbidden", "unexpected keyword", "unrecognized")


def _is_reasoning_reject(err_body: str) -> bool:
    """True when a 400 error body (already lowercased) says the endpoint rejected the reasoning
    param — the caller then drops the toggle for this client and retries."""
    return any(k in err_body for k in _REASONING_REJECT_KEYS)


def _reasoning_of(msg: dict) -> str:
    """The dedicated reasoning field of an OpenAI-shaped assistant message: `reasoning` (OpenRouter/
    Ollama) or `reasoning_content` (newer OpenAI/SGLang), whichever is present. '' when absent."""
    return msg.get("reasoning") or msg.get("reasoning_content") or ""


def _is_transient(e: BaseException) -> bool:
    """True for a TRANSIENT network failure worth retrying — a socket timeout, a reset/aborted
    connection, or a mid-read TLS EOF (UNEXPECTED_EOF_WHILE_READING: the peer hung up mid-response,
    common over a busy hosted gateway like OpenRouter). FALSE for steady-state failures that should
    fail FAST: a refused connection (endpoint down), a DNS error (bad host), or a TLS CERT error
    (misconfig). urllib wraps the real cause in `URLError.reason`, so check that too. (`socket.timeout`
    is an alias of `TimeoutError` since Python 3.10.)"""
    for x in (e, getattr(e, "reason", None)):
        if x is None:
            continue
        if isinstance(x, (TimeoutError, ConnectionResetError, ConnectionAbortedError)):
            return True
        if isinstance(x, ssl.SSLError) and not isinstance(x, ssl.SSLCertVerificationError):
            return True   # dropped TLS read (e.g. UNEXPECTED_EOF), NOT a cert problem
    return isinstance(e, (http.client.IncompleteRead, http.client.RemoteDisconnected))


def _parse_chat_body(raw: Optional[str]) -> Optional[dict]:
    """Parse an OpenAI-compatible chat response body into its dict, or None when the body is
    UNRECOVERABLE. A hosted gateway can return HTTP 200 with a body that is empty / whitespace /
    truncated (it dropped the connection mid-response) or that interleaves SSE keep-alive COMMENT
    lines (': OPENROUTER PROCESSING' — sent to hold the socket open while a model is queued) around
    the JSON. A None return tells `_post` to retry the request like a transient network failure
    instead of crashing the run on a single gateway hiccup. A parsed dict is always returned as-is
    (including an `{"error": ...}` envelope), so the caller's no-`choices` check still fails fast on
    a genuine bad-request — only an unparseable body is retried."""
    if not raw or not raw.strip():
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Recover the keepalive-interleaved case: drop blank + ':'-comment lines, re-parse the rest.
        kept = "\n".join(ln for ln in raw.splitlines()
                         if ln.strip() and not ln.lstrip().startswith(":"))
        if not kept.strip():
            return None
        try:
            obj = json.loads(kept)
        except (json.JSONDecodeError, ValueError):
            return None
    return obj if isinstance(obj, dict) else None


def _clean_thinking(content: str, reasoning: str = "") -> tuple[str, str]:
    """(thinking, answer) for an assistant turn. Reasoning models surface their chain-of-thought one
    of two ways: a DEDICATED field (`reasoning`/`reasoning_content` — newer Ollama/OpenAI) or INLINE
    <think>…</think> tags in `content`. Prefer the dedicated field (content is then already clean);
    otherwise split the inline tags. Either way the answer is the clean conclusion the UI surfaces."""
    if reasoning:
        _, answer = split_think(content or "")   # strip any stray inline tags too, for safety
        return str(reasoning), (answer or content or "")
    return split_think(content or "")


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


# Native tool-call recovery. Some models (glm-5.1 / DeepSeek served via litellm) emit tool calls in
# their OWN template as plain CONTENT instead of OpenAI `tool_calls` — e.g.
# `<｜DSML｜invoke name="emit"><｜DSML｜parameter name="arguments" ...>{...}</…></…>`. When that leaks
# into content the agent loop sees no tool call and the raw markup reaches the user. These lift the
# leaked call back into OpenAI-shaped tool_calls (name + JSON arguments) and clean the content.
#
# Every pattern REQUIRES a genuine OPENING tag `<…invoke…` (negative lookahead `(?!/)` after the `<`
# so a bare CLOSER `</invoke>` never counts as an opener): otherwise prose that merely QUOTES the
# syntax un-fenced — 'write invoke name="delete_file" … and close with </invoke>' — would be lifted
# into a real, EXECUTED tool call (the docstring's stated worst case). Weak local models routinely
# omit code fences, so the code-span guard alone is not enough — the tag anchor is what makes it safe.
_NATIVE_INVOKE_RE = re.compile(r'<(?!/)[^>]*?invoke\s+name="([^"]+)"(.*?)</[^>]*?invoke>', re.DOTALL)
_NATIVE_PARAM_RE = re.compile(r'parameter\s+name="([^"]+)"[^>]*?>(.*?)</[^>]*?parameter>', re.DOTALL)
_NATIVE_OPEN_RE = re.compile(r'<(?!/)[^>]*?(?:DSML|tool_calls|\binvoke\b)')


_CODE_SPAN_RE = re.compile(r"```.*?(?:```|$)|`[^`\n]*`", re.DOTALL)


def _code_spans(text: str) -> list[tuple[int, int]]:
    """Spans of fenced blocks and inline code — markup QUOTED there is the model talking about
    tool calls (docs, examples, explaining this very file), never a leaked call to recover."""
    return [m.span() for m in _CODE_SPAN_RE.finditer(text)]


def _extract_native_tool_calls(content: str):
    """(tool_calls | None, cleaned_content). Parse a leaked native tool-call block out of `content`.
    Deliberately conservative: only tag-anchored markup OUTSIDE code spans counts — recovering a
    merely-quoted example would truncate the reply and, worse, execute text as a real tool call."""
    if not content or "invoke name=" not in content:
        return None, content
    spans = _code_spans(content)

    def _quoted(pos: int) -> bool:
        return any(a <= pos < b for a, b in spans)

    calls = []
    for m in _NATIVE_INVOKE_RE.finditer(content):
        if _quoted(m.start()):
            continue
        name, body = m.group(1), m.group(2)
        params = {p.group(1): p.group(2).strip() for p in _NATIVE_PARAM_RE.finditer(body)}
        args = params.get("arguments")
        if args is None:
            args = json.dumps(params or {})
        else:
            try:
                json.loads(args)                 # already valid JSON string -> keep as-is
            except (ValueError, TypeError):
                args = json.dumps(params or {})   # fall back to the param map
        calls.append({"id": f"call_{len(calls)}", "type": "function",
                      "function": {"name": name, "arguments": args}})
    if not calls:
        return None, content
    m0 = next((m for m in _NATIVE_OPEN_RE.finditer(content) if not _quoted(m.start())), None)
    if m0 is None:                # invoke text without any tag-anchored opener — quoted, not leaked
        return None, content
    clean = content[:m0.start()].strip()
    return calls, clean


_FINAL_NAMES = {"emit", "finalanswer", "answer", "reply", "respond", "finish", "submit", "final", "done"}
_ANSWER_FIELDS = ("answer", "reply", "text", "response", "summary", "content", "message")


def _apply_native_tool_calls(msg: dict) -> dict:
    """If `msg` has no OpenAI tool_calls but its content carries a leaked native tool-call block,
    recover it. A FINAL-ANSWER-style call (emit / final_answer / answer / …) becomes the clean visible
    content (its answer text) so the loop finalizes on it; any other call is lifted into real
    tool_calls. Either way the raw markup never reaches the user. Mutates + returns `msg`."""
    if not isinstance(msg, dict) or msg.get("tool_calls"):
        return msg
    calls, clean = _extract_native_tool_calls(msg.get("content") or "")
    if not calls:
        return msg
    first = calls[0]
    name = re.sub(r"[_\s-]", "", (first["function"]["name"] or "").lower())
    try:
        args = json.loads(first["function"]["arguments"])
    except (ValueError, TypeError):
        args = {}
    if name in _FINAL_NAMES:
        ans = ""
        if isinstance(args, dict):
            for k in _ANSWER_FIELDS:
                if isinstance(args.get(k), str) and args[k].strip():
                    ans = args[k]
                    break
            if not ans:
                ans = json.dumps(args)
        else:
            ans = str(args)
        msg["content"] = (clean + "\n" + ans).strip() if clean else ans
    else:
        msg["tool_calls"] = calls
        msg["content"] = clean
    return msg


def _assistant_text(msg: dict) -> str:
    """Display text for a tool-calling assistant turn: its content, plus a compact note of any
    tool calls it made (so the trace shows the model chose to call a tool, not an empty reply)."""
    content = msg.get("content") or ""
    calls = msg.get("tool_calls") or []
    if calls:
        names = ", ".join(c.get("function", {}).get("name", "?") for c in calls)
        note = f"[tool_calls: {names}]"
        return f"{content}\n{note}" if content else note
    return content


def _args_complete(slot: dict) -> bool:
    """True when a slot's accumulated `arguments` already form a COMPLETE JSON value — i.e. that call
    is finished, so a following delta belongs to a NEW call, not this one's continuation."""
    joined = "".join(slot["function"]["arguments"])
    if not joined.strip():
        return False
    try:
        json.loads(joined)
        return True
    except (ValueError, TypeError):
        return False


def _tool_call_slot(tcs: dict, tc: dict) -> int:
    """Pick the merge slot for a streamed tool-call delta. When the provider supplies `index` (the
    OpenAI spec), use it verbatim. When it OMITS `index` (several Ollama builds / OpenAI-compat
    gateways emit one WHOLE call per delta with no index), a blind `.get("index", 0)` collapses every
    call into slot 0 — the later call overwrites the earlier's id/name and their argument fragments
    concatenate into invalid JSON. So without an index we START A NEW SLOT when the delta begins a
    new call and otherwise keep appending to the open slot (so a single call streamed in fragments
    stays merged). A new call is signalled by a NEW id, or — for a provider that ECHOES `function.name`
    on every continuation delta while omitting ids — by a repeated name ONLY once the current slot's
    arguments already parse as complete JSON (so an echoed name mid-fragment doesn't split one call)."""
    idx = tc.get("index")
    if idx is not None:
        return idx
    if not tcs:
        return 0
    cur = max(tcs)
    slot = tcs[cur]
    fn = tc.get("function") or {}
    new_id = tc.get("id") and slot.get("id") and tc["id"] != slot["id"]
    new_named = fn.get("name") and slot["function"]["name"] and _args_complete(slot)
    return cur + 1 if (new_id or new_named) else cur


def _socket_watchdog(resp, idle_limit: float):
    """Shared stream-stall watchdog. A stalled generation can hide from an in-loop wall-clock check
    two ways: SSE keepalive heartbeats reset the per-socket read timeout forever, and a server that
    trickles bytes WITHOUT completing a line blocks inside recv() so the loop's check never even runs
    (both observed as multi-minute/hour hangs on glm-5.1). This starts a daemon thread that, once no
    progress for `idle_limit`s, SHUTS DOWN the underlying socket — which is what actually interrupts a
    recv() already blocked in the kernel (resp.close() alone does not: the blocked syscall keeps
    waiting on the fd). shutdown() makes the recv raise OSError, retried on a fresh connection.

    Returns (last_progress, killed, stop):
      - last_progress[0]: set to time.monotonic() on every REAL token so a long-but-alive generation
        is never cut off (no hard deadline — only true silence trips it).
      - killed[0]: True once the watchdog fired (read the loop's EOF as a stall, not a clean end).
      - stop(): end the watchdog thread — ALWAYS call it in a finally.
    """
    import socket as _socket
    import threading
    last_progress = [time.monotonic()]
    killed = [False]
    _stop = threading.Event()
    # None for a non-socket body (e.g. a test mock) -> close() path.
    _sock = _raw_socket(resp)

    def _watchdog():
        while not _stop.wait(min(5.0, idle_limit / 4)):
            if time.monotonic() - last_progress[0] > idle_limit:
                killed[0] = True            # so the loop's EOF is read as a stall, not a clean end
                if _sock is not None:
                    try:
                        _sock.shutdown(_socket.SHUT_RDWR)   # unblocks a recv() stuck in the kernel
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    resp.close()            # fallback (and mock-friendly for tests)
                except Exception:  # noqa: BLE001
                    pass
                return

    threading.Thread(target=_watchdog, daemon=True).start()
    return last_progress, killed, _stop.set


class OpenAICompatibleClient:
    """Dependency-free OpenAI-compatible chat client (stdlib urllib). Implements the
    `parse.LLMClient` Protocol, so it drops into the LLM roles like any other backend.

    Works against ANY OpenAI-compatible endpoint — Ollama (`/v1`), SGLang, vLLM, or
    the OpenAI API itself — so the serving backend is a base_url change, not code.
    Chosen for the live path because it has no install footprint and runs on the same
    Python 3.14 the engine uses (LiteLLM remains the documented production gateway)."""

    def __init__(self, model: str, base_url: str = "http://localhost:11434/v1",
                 api_key: str = "ollama", temperature: float = 0.7,
                 timeout: float = 180.0, accountant: Optional["CostAccountant"] = None,
                 guided_json: bool = False, reasoning: Optional[dict] = None,
                 stream: bool = True, cache: bool = False,
                 header_timeout: Optional[float] = None):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "x"
        self.temperature = temperature
        self.timeout = timeout
        # First-byte (response-HEADERS) timeout: a shared endpoint sometimes accepts the TLS
        # connection and never answers (black-holed request) — waiting the full idle `timeout` for
        # headers ×retries turned one call into ~13 minutes of silence. Headers arrive fast when a
        # request is actually admitted (streaming starts before generation finishes), so a short
        # header window fails futile attempts over to a fresh connection quickly. The idle `timeout`
        # still governs the BODY (inter-token) phase, restored right after headers arrive.
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
        self._max_retries = 4               # 429/5xx backoff retries before surfacing an LLMError
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
        raw_lines: list[str] = []
        got_sse = False
        # Idle timeout by PROGRESS: tracked as wall-clock since the last REAL token and enforced both
        # by an in-loop check (below) AND a socket-shutdown watchdog (`_socket_watchdog`) — a stall
        # can hide from either alone (keepalive heartbeats reset the read timeout; a partial-line
        # trickle blocks in recv() before the in-loop check runs). A long-but-alive generation keeps
        # emitting content, resetting the clock — no hard deadline.
        idle_limit = self.timeout
        last_progress, _killed, _stop_wd = _socket_watchdog(resp, idle_limit)
        try:
            try:
                lines = iter(resp)              # SSE responses iterate line-by-line
            except TypeError:
                lines = iter(())                # a non-iterable body (e.g. a test mock) -> read() below
            while True:
                try:
                    raw = next(lines)
                except StopIteration:
                    break
                except ValueError:
                    # The watchdog called resp.close() while lines buffered in the BufferedReader were
                    # still draining -> readline-of-closed-file. That IS the stall the watchdog fired
                    # on; fall through to the `_killed` check (a TimeoutError _post retries) instead of
                    # leaking a bare ValueError past _post's transient tuple and crashing the run.
                    if _killed[0]:
                        break
                    raise
                if time.monotonic() - last_progress[0] > idle_limit:
                    raise TimeoutError(f"LLM stream stalled — no new tokens for {idle_limit:.0f}s; "
                                       f"retrying on a fresh connection")
                s = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
                raw_lines.append(s)
                line = s.strip()
                if not line or not line.startswith("data:"):
                    continue
                got_sse = True
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except ValueError:
                    continue                    # keepalive / non-JSON heartbeat line
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
            raise TimeoutError(f"LLM stream stalled — no new tokens for {idle_limit:.0f}s; "
                               f"retrying on a fresh connection")   # _post catches -> retry
        # Not actually an SSE stream (a non-streaming endpoint, or a test mock that returns one JSON
        # body): parse the whole body as a normal chat completion so streaming stays transparent.
        if not got_sse and not content and not tcs:
            blob = "".join(raw_lines)
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
            cached = self._cache[ck]
            # Restore the per-call telemetry a live call would have set, and hand back a DEEP COPY:
            # downstream (e.g. complete_text -> _apply_native_tool_calls) mutates the message in
            # place, which would otherwise corrupt the shared cached entry for every later hit.
            self._last_usage = cached.get("usage") or {}
            return copy.deepcopy(cached)
        # A network blip / HTTP error / non-JSON body must surface as a clean LLMError, not an
        # unhandled URLError/HTTPError/JSONDecodeError that aborts the whole run — the role layer
        # already retries + falls back on LLMError. (urllib.error was imported but unused before.)
        # Rate-limit/transient resilience: a 429 (or 5xx) is retried with backoff (honoring a
        # Retry-After header when given) BEFORE surfacing — free/shared endpoints (e.g. OpenRouter
        # free tier) rate-limit bursts, and a single 429 shouldn't crash the whole run.
        body = None
        _stalled_prev = False               # this call's previous attempt stalled mid-stream
        for attempt in range(self._max_retries + 1):
            # Build the request per attempt so a param-compat retry (below) can drop the reasoning
            # toggle. `_reasoning_ok` starts True and flips off permanently for THIS client the first
            # time the endpoint rejects our reasoning param.
            p = dict(payload)
            if self.reasoning and self._reasoning_ok:   # inject the reasoning toggle
                p = {**p, **self.reasoning}
            # Stall-degrade: stream by default (timeout = inter-token idle guard), but drop to a
            # plain blocking read for the attempt right after a stream stall, and permanently once
            # this client has stalled twice — a flaky proxied endpoint often answers the SAME request
            # fine without SSE while its stream wedges mid-generation.
            use_stream = (self.stream and self._stream_stalls < STREAM_STALL_DEGRADE_AFTER
                          and not _stalled_prev)
            if use_stream:                      # force streaming so `timeout` is an inter-token idle guard
                p = {**p, "stream": True, "stream_options": {"include_usage": True}}
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=json.dumps(p).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.api_key}"},
            )
            # The short header window applies ONLY to stream attempts: an SSE response sends its
            # headers as soon as the request is admitted (before generation finishes), so no-headers-
            # in-45s ≈ black-holed. A NON-stream response sends nothing — headers included — until the
            # whole generation completes, so its header wait must be the full idle timeout or every
            # legitimate >45s generation would die at the header phase (self-triggering with the
            # stall-degrade below, which is exactly what switches a call to non-stream).
            _first_byte_to = self.header_timeout if use_stream else self.timeout
            try:
                with urllib.request.urlopen(req, timeout=_first_byte_to) as resp:
                    # Headers arrived — restore the full idle timeout for the BODY phase (the short
                    # window above only bounds connect + first byte; slow token gaps are legitimate).
                    _sk = _raw_socket(resp)
                    if _sk is not None:
                        try:
                            _sk.settimeout(self.timeout)
                        except OSError:
                            pass
                    # Streaming: reassemble SSE lines (a stall raises socket.timeout HERE, inside the
                    # `with`, and lands in the transient-retry handler below). Non-streaming: one read.
                    parsed = self._read_stream(resp) if use_stream \
                        else _parse_chat_body(resp.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                # A 400 that rejects our REASONING toggle — e.g. a litellm-proxied model like glm-5.1
                # returns UnsupportedParamsError for `reasoning_effort` — isn't a real bad request:
                # drop reasoning for this client and retry immediately, so the model still works
                # (deepseek accepts reasoning_effort, glm-5.1 doesn't; the client adapts per model).
                if e.code == 400 and self.reasoning and self._reasoning_ok:
                    try:
                        eb = e.read().decode("utf-8", "replace").lower()
                    except Exception:  # noqa: BLE001
                        eb = ""
                    if _is_reasoning_reject(eb):
                        self._reasoning_ok = False
                        continue
                if e.code in (429, 500, 502, 503, 504) and attempt < self._max_retries:
                    ra = _retry_after_seconds(e.headers.get("Retry-After") if e.headers else None)
                    delay = ra if ra is not None else _backoff(attempt)
                    time.sleep(min(delay, BACKOFF_CAP_S))
                    continue
                hint = (" — check the API key (LOOPLAB_LLM_API_KEY)" if e.code == 401 else "")
                raise LLMError(f"LLM request to {self.base_url} failed: {e}{hint}") from e
            except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead) as e:
                # Retry TRANSIENT network failures (timeout, reset, TLS EOF / IncompleteRead mid-read)
                # — the modes that
                # otherwise abort a long run, since the tool-using proposer / pilot call client.chat
                # directly and don't wrap LLMError. A refused connection / DNS / TLS CERT error is a
                # steady-state "endpoint down or misconfigured" signal: fail FAST so /api/llm/health
                # stays instant and a wrong base_url surfaces on the first call, not after backoff.
                # Count a stall ONLY for a transient failure of a STREAM attempt (idle timeout /
                # reset / TLS EOF mid-stream). A fail-fast error (refused, DNS, cert) means the
                # endpoint is down/misconfigured — it says nothing about SSE health, and counting it
                # would ratchet `_stream_stalls` to the permanent-degrade threshold while the
                # endpoint restarts. Degrade applies to the REMAINING attempts of this call.
                if use_stream and _is_transient(e):
                    _stalled_prev = True
                    self._stream_stalls += 1
                if _is_transient(e) and attempt < self._max_retries:
                    time.sleep(_backoff(attempt))
                    continue
                raise LLMError(f"LLM request to {self.base_url} failed: {e}") from e
            else:
                # HTTP 200 read cleanly, but the body can still be unusable: a hosted gateway
                # sometimes returns an empty / whitespace / SSE-keepalive-only body (OpenRouter sends
                # ': OPENROUTER PROCESSING' heartbeats while a model is queued and can finish with no
                # JSON payload), or a stream that carried no content/tool-call. (A mid-read socket drop
                # is caught above as IncompleteRead.) Treat an empty/unparseable 200 like a transient
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
        if "choices" not in body or not body["choices"]:
            # Ollama/vLLM emit {"error": ...} envelopes on a bad request — don't index [0] blind.
            raise LLMError(f"LLM response had no choices: {str(body)[:200]}")
        usage = body.get("usage") or {}
        # No price table for local models -> account tokens as 0 cost, but track them.
        self.accountant.add(0.0, usage=usage)
        self._last_usage = usage
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
            gen.output(answer or out).thinking(thinking).usage(body.get("usage"))
            return out

    def complete_text_stream(self, messages: list[dict]):
        """Stream a plain-text completion token-by-token (an OpenAI `stream:true` SSE call). Yields
        content deltas as they arrive; used by the assistant to stream its final answer live. Falls
        back to a single yield of the whole text if the endpoint doesn't stream. Best-effort — any
        transport error mid-stream ends the generator (the caller keeps what it got)."""
        pieces: list[str] = []
        usage: dict = {}
        # The generation span stays open for the whole stream (its duration = time-to-full-answer).
        with tracing.generation(op="complete_text_stream", model=self.model, messages=messages,
                                model_parameters=self._model_params()) as gen:
            # Reasoning-param retry: like `_post`, a model that 400s on our reasoning toggle (glm-5.1
            # behind litellm) must not silently lose streaming forever — flip `_reasoning_ok` off for
            # this client and retry the stream ONCE without it, instead of paying a wasted 400
            # round-trip per turn and always falling back to the blocking path.
            for _attempt in range(2):
                payload = {"model": self.model, "messages": messages,
                           "temperature": self.temperature,
                           "stream": True, "stream_options": {"include_usage": True}}
                if self.reasoning and self._reasoning_ok:
                    payload = {**payload, **self.reasoning}
                req = urllib.request.Request(
                    f"{self.base_url}/chat/completions", data=json.dumps(payload).encode("utf-8"),
                    method="POST", headers={"Content-Type": "application/json",
                                            "Authorization": f"Bearer {self.api_key}"})
                try:
                    # Idle timeout by PROGRESS, enforced by the SAME socket-shutdown watchdog as
                    # `_read_stream`: the in-loop wall-clock check alone can't catch a server that
                    # trickles bytes without completing a line (recv() blocks in the kernel — the
                    # documented ~15-min hang), and the caller's cancel check only runs per yielded
                    # piece, so Stop would never be observed. The watchdog shuts the socket down.
                    with urllib.request.urlopen(req, timeout=self.header_timeout) as resp:
                        _sk2 = _raw_socket(resp)   # restore the idle timeout for the body phase
                        if _sk2 is not None:
                            try:
                                _sk2.settimeout(self.timeout)
                            except OSError:
                                pass
                        last_progress, _killed, _stop_wd = _socket_watchdog(resp, self.timeout)
                        try:
                            lines = iter(resp)
                            while True:
                                try:
                                    raw = next(lines)
                                except StopIteration:
                                    break
                                except ValueError:
                                    if _killed[0]:
                                        break
                                    raise
                                if time.monotonic() - last_progress[0] > self.timeout:
                                    raise TimeoutError(
                                        f"LLM stream stalled — no new tokens for {self.timeout:.0f}s")
                                line = raw.decode("utf-8", "replace").strip()
                                if not line or not line.startswith("data:"):
                                    continue
                                chunk = line[5:].strip()
                                if chunk == "[DONE]":
                                    break
                                try:
                                    obj = json.loads(chunk)
                                except ValueError:
                                    continue
                                if obj.get("usage"):           # final usage chunk (include_usage)
                                    usage = obj["usage"]
                                    last_progress[0] = time.monotonic()
                                delta = ((obj.get("choices") or [{}])[0] or {}).get("delta") or {}
                                piece = delta.get("content") or ""
                                if piece:
                                    pieces.append(piece)
                                    last_progress[0] = time.monotonic()
                                    yield piece
                        finally:
                            _stop_wd()
                        if _killed[0] and not pieces:   # watchdog killed a stalled, silent stream
                            raise TimeoutError(
                                f"LLM stream stalled — no new tokens for {self.timeout:.0f}s")
                    break                        # streamed (or cleanly ended) -> done
                except urllib.error.HTTPError as e:
                    if (e.code == 400 and self.reasoning and self._reasoning_ok
                            and not pieces and _attempt == 0):
                        try:
                            eb = e.read().decode("utf-8", "replace").lower()
                        except Exception:  # noqa: BLE001
                            eb = ""
                        if _is_reasoning_reject(eb):
                            self._reasoning_ok = False
                            continue             # retry the stream once without the reasoning toggle
                    if not pieces:               # any other HTTP error -> blocking fallback
                        text = self.complete_text(messages)
                        if text:
                            yield text
                        return
                    break
                except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead):
                    if not pieces:               # never streamed -> fall back to a single blocking call
                        text = self.complete_text(messages)   # its own nested generation span
                        if text:
                            yield text
                        return
                    break
            if usage:                            # account the streamed answer's tokens like every other call
                self.accountant.add(0.0, usage=usage)
                self._last_usage = usage
            gen.output("".join(pieces)).usage(usage or None)

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
            gen.output(_assistant_text({**msg, "content": answer})).thinking(thinking).usage(body.get("usage"))
            if msg.get("tool_calls"):
                gen.set("tool_calls", [{"name": (c.get("function") or {}).get("name"),
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
                gen.usage(body.get("usage")).error("no tool_calls in response")
                raise KeyError("no tool_calls in response")
            args = calls[0]["function"]["arguments"]
            # Reasoning models emit their chain-of-thought (a `reasoning` field, or inline <think> in
            # `content`) alongside the tool call; capture it (debug channel) instead of discarding it.
            # The completion stays the structured tool args — the clean conclusion the UI renders.
            thinking, _ = _clean_thinking(msg.get("content") or "", _reasoning_of(msg))
            gen.output(args if isinstance(args, str) else json.dumps(args)).thinking(thinking).usage(body.get("usage"))
            return json.loads(args) if isinstance(args, str) else args


class CostAccountant:
    def __init__(self, limit: Optional[float] = None, warn_frac: float = 0.8):
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

    def add(self, cost: Optional[float], usage: Optional[dict] = None) -> float:
        self.spent += max(0.0, float(cost or 0.0))
        if usage:
            self.calls += 1
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            self.prompt_tokens += pt
            self.completion_tokens += ct
            self.total_tokens += int(usage.get("total_tokens") or (pt + ct))
        if self.limit is not None:
            if not self.warned and self.spent >= self.warn_frac * self.limit:
                self.warned = True  # caller may surface a warning event
            if self.spent >= self.limit:
                raise BudgetExceeded(f"spent {self.spent:.4f} >= budget {self.limit:.4f}")
        return self.spent

    def remaining(self) -> Optional[float]:
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
        cost = None
        try:
            cost = resp._hidden_params.get("response_cost")  # type: ignore[attr-defined]
        except Exception:
            cost = None
        self.accountant.add(cost or 0.0, usage=self._usage(resp))

    def _usage(self, resp) -> Optional[dict]:
        try:
            u = resp.usage
            return {"prompt_tokens": getattr(u, "prompt_tokens", 0),
                    "completion_tokens": getattr(u, "completion_tokens", 0),
                    "total_tokens": getattr(u, "total_tokens", 0)}
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
            gen.output(answer or out).thinking(thinking).usage(u).cost(self._cost(resp))
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
               .usage(self._usage(resp)).cost(self._cost(resp))
            return json.loads(args) if isinstance(args, str) else (args or {})
