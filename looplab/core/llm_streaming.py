"""SSE/stream machinery for the LLM clients (split out of `core.llm`).

The idle-guard watchdogs that interrupt a stalled stream (`_stream_with_idle_guard` for the
openai-SDK path, `_socket_watchdog` + `_sse_chunks` for the urllib-era reassembly path), the
raw-socket plumbing they need (`_raw_socket` / `_stream_raw_socket` / `_shutdown_pool_sockets` —
only socket.shutdown() unblocks a recv() wedged in the kernel), and the non-SSE whole-body
fallback parser (`_parse_chat_body`). `core.llm` re-imports every name under its original name,
so `looplab.core.llm._stream_with_idle_guard` (and the flat `looplab.llm.…`) keep resolving to
the SAME objects — tests and callers import and monkeypatch through those paths.
"""
from __future__ import annotations

import json
import time
from typing import Optional

# httpx/openai are declared runtime deps, but the import is GUARDED for the same reason as in
# `core.llm`: an offline/replay/`--no-deps` install must still import the package without the live
# LLM stack. The users here (`_stream_with_idle_guard`'s error normalization) only touch the names
# on the LIVE stream path, which cannot run unless both are installed.
try:
    import httpx
    import openai
except ModuleNotFoundError:  # pragma: no cover - deps are declared; guard is for stripped/offline installs
    httpx = None   # type: ignore[assignment]
    openai = None  # type: ignore[assignment]


def _raw_socket(resp):
    """Best-effort grab of the raw socket behind a urllib response (http.client wraps it in a
    BufferedReader over a SocketIO). None for a non-socket body (e.g. a test mock)."""
    fp = getattr(resp, "fp", None)
    return getattr(getattr(fp, "raw", None), "_sock", None) or getattr(fp, "_sock", None)


def _shutdown_pool_sockets(http_client) -> int:
    """socket.shutdown(SHUT_RDWR) every live connection socket in an httpx sync client's pool, and
    return how many were shut. This forces a recv() WEDGED in the kernel — a trickling/half-dead
    endpoint that httpx's read timeout can't catch (a byte keeps resetting the timer) — to return an
    error, so a worker thread blocked inside `chat.completions.create` UNBLOCKS and EXITS instead of
    lingering forever (over a long run those daemons accumulate). `client.close()` alone can't do this:
    it never touches an in-flight connection's socket. Best-effort over httpcore internals (pool →
    HTTPConnection._connection._network_stream → socket), mirroring the stream path's socket.shutdown()."""
    import socket as _socket
    try:
        pool = http_client._transport._pool
        conns = list(getattr(pool, "connections", []) or [])
    except Exception:  # noqa: BLE001 — foreign/mock client or a changed httpcore layout: nothing to do
        return 0
    n = 0
    for conn in conns:
        try:
            inner = getattr(conn, "_connection", None)
            ns = getattr(inner, "_network_stream", None) or getattr(conn, "_network_stream", None)
            sock = ns.get_extra_info("socket") if ns is not None else None
            if sock is not None:
                sock.shutdown(_socket.SHUT_RDWR)   # unblocks a recv() stuck in the kernel
                n += 1
        except Exception:  # noqa: BLE001 — an already-closed/foreign socket just skips
            pass
    return n


def _stream_raw_socket(resp):
    """The raw socket behind an httpx STREAMING response, via the `network_stream` transport
    extension. Needed because `response.close()` does NOT interrupt a read already blocked in the
    kernel — only `socket.shutdown()` does (the same lesson the old urllib watchdog learned)."""
    try:
        ns = resp.extensions.get("network_stream")
        return ns.get_extra_info("socket") if ns is not None else None
    except Exception:  # noqa: BLE001
        return None


def _chunk_has_content(ev) -> bool:
    """Does a streamed SDK chunk carry REAL progress — a text / tool-call / reasoning / function
    delta, a finish_reason, or the final usage frame? Empty keepalive/heartbeat chunks (role-only or
    blank deltas that some litellm/openrouter proxies trickle to hold the connection open) return
    False, so the idle-guard doesn't count them as progress and can't be fooled into never timing out
    on a stalled generation. Unknown shapes count as progress (never false-kill a real stream)."""
    try:
        if getattr(ev, "usage", None):
            return True
        for ch in (getattr(ev, "choices", None) or []):
            if getattr(ch, "finish_reason", None):
                return True
            d = getattr(ch, "delta", None)
            if d is not None and (getattr(d, "content", None) or getattr(d, "tool_calls", None)
                                  or getattr(d, "reasoning", None) or getattr(d, "reasoning_content", None)
                                  or getattr(d, "function_call", None)):
                return True
        return False
    except Exception:  # noqa: BLE001 — unknown chunk shape: treat as progress, don't false-kill
        return True


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
    `_post` degrades+retries. idle_limit<=0 or a non-httpx stream (test iterators) disables it.

    This seam is ALSO the single owner of transport normalization: the openai SDK maps transport
    failures to openai.APIConnectionError only for the INITIAL request (headers), so a reset/EOF/read-
    timeout while iterating the STREAM BODY escapes its `Stream.__stream__` as a RAW httpx exception
    (verified live — it aborted runs). Every streaming caller (`_accumulate_stream` for `_post`, and
    `complete_text_stream`) funnels through here, so we normalize it HERE — to openai.APIConnectionError
    with the httpx error as `__cause__` — and the callers' existing openai.* handlers classify it (via
    `_sdk_transient`, which reads reset/EOF-vs-connect off `__cause__`). No caller needs to know httpx."""
    try:
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
        start = time.monotonic()
        last = [start]                                # last REAL-CONTENT time (init = setup)
        conn = [False]                               # has ANY chunk (keepalive incl.) arrived
        killed = [False]
        stop = threading.Event()
        fb = first_byte_limit if first_byte_limit and first_byte_limit > 0 else idle_limit

        def _wd():
            while not stop.wait(min(5.0, min(fb, idle_limit) / 4)):
                now = time.monotonic()
                # Before ANY chunk: bound the black-holed first byte by `fb`. Once the connection is
                # producing, bound the gap between REAL-CONTENT chunks by `idle_limit` — a proxy that
                # trickles EMPTY keepalive chunks (role-only / blank deltas) to hold the socket open
                # can no longer mask a stalled generation, because empties don't reset `last` (the
                # bug: a 74-min live hang where the watchdog never fired because every keepalive chunk
                # reset the idle timer).
                stalled = (now - start > fb) if not conn[0] else (now - last[0] > idle_limit)
                if stalled:
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
            for ev in stream:
                conn[0] = True                    # connection is producing (even an empty keepalive)
                if _chunk_has_content(ev):        # only REAL content resets the idle timer
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
    except httpx.HTTPError as e:
        # Raw httpx from stream-body iteration (SDK-unwrapped) -> normalize to the openai exception the
        # callers already handle. `from e` keeps the httpx error as __cause__ so `_sdk_transient` still
        # tells a transient reset/EOF from a fail-fast connect/DNS. httpx exposes `.request` as a
        # property that RAISES when unset, so extract it defensively (not getattr, which wouldn't
        # swallow that RuntimeError). Covers the no-socket / no-idle-limit passthroughs above too.
        try:
            _req = e.request
        except Exception:  # noqa: BLE001 - the .request property raises RuntimeError when unset
            _req = None
        raise openai.APIConnectionError(message=str(e) or e.__class__.__name__, request=_req) from e


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
