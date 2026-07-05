"""LLM client + cost accounting (I2/I13, ADR-14/17/11).

`OpenAICompatibleClient` is the LIVE path — an OpenAI-compatible chat client on the
openai SDK (httpx transport), whose per-read timeout reliably bounds a stalled stream.
`LiteLLMClient` wraps LiteLLM (lazy import) as the documented production gateway. The
openai/httpx imports are declared deps but GUARDED, so the offline engine + replay still
import this module (via `core.config`) without the live LLM stack. `CostAccountant`
tallies per-call cost with warn/hard-stop thresholds and raises `BudgetExceeded` at 100%.
Secrets are never stored as values here — the client reads the key from config/env.
"""
from __future__ import annotations

import copy
import json
import re
import time
from typing import Optional

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

# Legacy urllib-transport helpers (still imported by a few direct unit tests) — the LIVE path now
# goes through the openai SDK (see OpenAICompatibleClient._sdk_chat). Kept import-safe until the
# dead helpers are removed.
import http.client  # noqa: F401
import ssl  # noqa: F401
import urllib.error  # noqa: F401
import urllib.request  # noqa: F401

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


def _stream_raw_socket(resp):
    """The raw socket behind an httpx STREAMING response, via the `network_stream` transport
    extension. Needed because `response.close()` does NOT interrupt a read already blocked in the
    kernel — only `socket.shutdown()` does (the same lesson the old urllib watchdog learned)."""
    try:
        ns = resp.extensions.get("network_stream")
        return ns.get_extra_info("socket") if ns is not None else None
    except Exception:  # noqa: BLE001
        return None


def _stream_with_idle_guard(stream, idle_limit: float, first_byte_limit: float = 0.0):
    """Yield the SDK stream's events, but a background watchdog SHUTS DOWN the underlying socket if no
    event arrives in time. Two deadlines: `first_byte_limit` until the FIRST event (bounds a black-
    holed request that accepts the socket then answers nothing — httpx `connect` only bounds TCP/TLS
    establishment, NOT the wait for headers/first byte, so this is what actually caps first-byte),
    then `idle_limit` between events. httpx's per-read timeout can't catch either: an SSE KEEPALIVE-
    COMMENT trickle (`: keepalive`) resets it on every byte while the SDK's decoder skips those
    comment lines, so its iterator blocks on the next `data:` event FOREVER. The watchdog keys on
    real EVENTS (keepalives are already filtered) and calls socket.shutdown() — `resp.close()` alone
    can't unblock a kernel recv (verified live) — so the stall surfaces as openai.APITimeoutError →
    `_post` degrades+retries. idle_limit<=0 or a non-httpx stream (test iterators) disables it."""
    if not idle_limit:
        yield from stream
        return
    import socket as _socket
    import threading
    resp = getattr(stream, "response", None)
    sock = _stream_raw_socket(resp) if resp is not None else None
    if sock is None:                              # a plain iterator / no socket handle — nothing to kill
        yield from stream
        return
    last = [time.monotonic()]
    seen = [False]                                # has the FIRST real event arrived yet
    killed = [False]
    stop = threading.Event()
    fb = first_byte_limit if first_byte_limit and first_byte_limit > 0 else idle_limit

    def _wd():
        while not stop.wait(min(5.0, min(fb, idle_limit) / 4)):
            limit = idle_limit if seen[0] else fb   # first-byte window before any event, idle after
            if time.monotonic() - last[0] > limit:
                killed[0] = True
                try:
                    sock.shutdown(_socket.SHUT_RDWR)   # unblocks a recv() stuck in the kernel
                except Exception:  # noqa: BLE001
                    pass
                try:
                    resp.close()                       # fallback (mock-friendly / frees the response)
                except Exception:  # noqa: BLE001
                    pass
                return

    threading.Thread(target=_wd, daemon=True).start()
    try:
        for ev in stream:                         # each yielded event = real progress (keepalives filtered)
            seen[0] = True
            last[0] = time.monotonic()
            yield ev
    except Exception:
        if killed[0]:
            raise openai.APITimeoutError(request=getattr(resp, "request", None)) from None
        raise
    finally:
        stop.set()
    if killed[0]:
        raise openai.APITimeoutError(request=getattr(resp, "request", None))


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


class _SSETail:
    """Raw-line side channel of `_sse_chunks`, used by `_read_stream`'s non-SSE fallback: every line
    as received (`raw_lines` — joined and parsed as one JSON body when the response turns out not to
    be SSE at all) and whether any `data:` line was ever seen (`got_sse`)."""
    __slots__ = ("raw_lines", "got_sse")

    def __init__(self):
        self.raw_lines: list[str] = []
        self.got_sse = False


def _sse_chunks(lines, last_progress, killed, idle_limit: float, stall_msg: str,
                tail: Optional[_SSETail] = None):
    """Shared SSE consumption for `_read_stream` and `complete_text_stream`: iterate the response's
    lines and yield each parsed `data:` chunk as a dict, ending at EOF or the `[DONE]` sentinel.
    The two callers MERGE chunks differently (full chat-response reassembly incl. tool_calls/usage
    vs. yielded text deltas), so chunk interpretation — including bumping `last_progress[0]` on every
    real token — stays with them; this generator owns only the transport concerns they duplicated:

      - `last_progress`/`killed` are the `_socket_watchdog` cells for this response. No progress for
        `idle_limit`s raises TimeoutError(stall_msg) (each caller passes its own exact message); a
        ValueError from a watchdog-closed response ends the stream so the CALLER's post-`killed`
        check decides what a watchdog kill means (always-raise vs. keep a partial stream).
      - keepalive/comment lines (no `data:` prefix) and non-JSON heartbeat payloads are skipped.
      - `tail` (passed by `_read_stream` only) records every raw line and the got-SSE flag, feeding
        its whole-body fallback for a non-SSE (plain JSON) response.
    """
    while True:
        try:
            raw = next(lines)
        except StopIteration:
            return
        except ValueError:
            # The watchdog called resp.close() while lines buffered in the BufferedReader were
            # still draining -> readline-of-closed-file. That IS the stall the watchdog fired
            # on; fall through to the `_killed` check (a TimeoutError _post retries) instead of
            # leaking a bare ValueError past _post's transient tuple and crashing the run.
            if killed[0]:
                return
            raise
        if time.monotonic() - last_progress[0] > idle_limit:
            raise TimeoutError(stall_msg)
        s = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        if tail is not None:
            tail.raw_lines.append(s)
        line = s.strip()
        if not line or not line.startswith("data:"):
            continue
        if tail is not None:
            tail.got_sse = True
        chunk = line[5:].strip()
        if chunk == "[DONE]":
            return
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue                    # keepalive / non-JSON heartbeat line
        yield obj


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
        # Transport: the openai SDK over an httpx client. `connect` bounds TCP/TLS establishment;
        # `read` = the inter-read idle limit (a long-but-alive generation keeps resetting it, so it's
        # never cut off). IMPORTANT: httpx's `read` timeout can't catch an SSE keepalive-trickle
        # (bytes reset it while no data EVENT arrives) NOR bound the wait-for-FIRST-byte — those are
        # enforced by `_stream_with_idle_guard` on the STREAM path (idle_limit=timeout,
        # first_byte_limit=header_timeout). The per-request timeout lives on the OpenAI client
        # (`timeout=`), which wins over the http_client's; the http_client exists only to set
        # `trust_env=False` (the internal endpoint needs a DIRECT connection — no proxy env). See
        # `llm_trust_env` if a proxy/custom-CA is required. max_retries=0: we own the retry loop.
        self._sdk = openai.OpenAI(
            base_url=self.base_url, api_key=self.api_key, max_retries=0,
            timeout=httpx.Timeout(read=self.timeout, connect=self.header_timeout, write=30.0, pool=10.0),
            http_client=httpx.Client(trust_env=trust_env))

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
        resp = self._sdk.chat.completions.create(**kwargs)
        return resp.model_dump()

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
            # Stall-degrade: stream by default (httpx read-timeout = inter-token idle guard), but drop
            # to a single blocking read for the attempt right after a stream stall, and permanently
            # once this client has stalled STREAM_STALL_DEGRADE_AFTER times — a flaky proxied endpoint
            # often answers the SAME request fine without SSE while its stream wedges mid-generation.
            use_stream = (self.stream and self._stream_stalls < STREAM_STALL_DEGRADE_AFTER
                          and not _stalled_prev)
            try:
                parsed = self._sdk_chat(payload, use_stream)
            except openai.BadRequestError as e:
                # A 400 that rejects our REASONING toggle — a litellm-proxied model like glm-5.1
                # returns UnsupportedParamsError for `reasoning_effort` — isn't a real bad request:
                # drop reasoning for this client and retry (deepseek keeps it; glm-5.1 adapts).
                if self.reasoning and self._reasoning_ok and _is_reasoning_reject(_err_body(e)):
                    self._reasoning_ok = False
                    continue
                raise LLMError(f"LLM request to {self.base_url} failed: {e}") from e
            except openai.AuthenticationError as e:
                raise LLMError(f"LLM request to {self.base_url} failed: {e} — check the API key "
                               "(LOOPLAB_LLM_API_KEY)") from e
            except (openai.RateLimitError, openai.InternalServerError) as e:   # 429 / 5xx
                if attempt < self._max_retries:
                    ra = _retry_after_seconds(_retry_after_of(e))
                    time.sleep(min(ra if ra is not None else _backoff(attempt), BACKOFF_CAP_S))
                    continue
                raise LLMError(f"LLM request to {self.base_url} failed: {e}") from e
            except openai.APIConnectionError as e:
                # httpx transport failure. APITimeoutError (a subclass) = a read/connect timeout: a
                # stalled mid-stream read or a black-holed request — always transient, and the
                # reliable interrupt the urllib+ssl path lacked (a glm SSE stall hung for minutes).
                # A plain APIConnectionError is retried only when `_sdk_transient` says so (reset/EOF),
                # else it fails fast (refused/DNS/cert). A STREAM stall degrades the next attempt to
                # non-stream and ratchets the permanent-degrade counter.
                is_timeout = isinstance(e, openai.APITimeoutError)
                transient = is_timeout or _sdk_transient(e)
                if use_stream and transient:
                    _stalled_prev = True
                    self._stream_stalls += 1
                if transient and attempt < self._max_retries:
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
                kwargs: dict = {"model": self.model, "messages": messages,
                                "temperature": self.temperature, "stream": True,
                                "stream_options": {"include_usage": True}}
                if self.reasoning and self._reasoning_ok:
                    kwargs["extra_body"] = dict(self.reasoning)
                try:
                    # httpx's per-read timeout bounds each stream iteration, so a stalled/silent
                    # stream raises openai.APITimeoutError here instead of hanging — the reliable
                    # interrupt the urllib+watchdog path approximated. Reasoning-param retry mirrors
                    # `_post`: a model that 400s on the toggle (glm-5.1) drops it and retries once.
                    for ev in _stream_with_idle_guard(
                            self._sdk.chat.completions.create(**kwargs), self.timeout, self.header_timeout):
                        if getattr(ev, "usage", None):
                            usage = ev.usage.model_dump()
                        if not ev.choices:
                            continue
                        piece = getattr(ev.choices[0].delta, "content", None) or ""
                        if piece:
                            pieces.append(piece)
                            yield piece
                    break                        # streamed (or cleanly ended) -> done
                except openai.BadRequestError as e:
                    if (self.reasoning and self._reasoning_ok and not pieces and _attempt == 0
                            and _is_reasoning_reject(_err_body(e))):
                        self._reasoning_ok = False
                        continue                 # retry the stream once without the reasoning toggle
                    if not pieces:               # any other bad request -> blocking fallback
                        text = self.complete_text(messages)
                        if text:
                            yield text
                        return
                    break
                except openai.APIError:
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
