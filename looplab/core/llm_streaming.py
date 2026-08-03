"""SSE/stream machinery for the LLM clients (split out of `core.llm`).

The idle-guard watchdog that interrupts a stalled stream (`_stream_with_idle_guard`) and the
raw-socket plumbing it needs (`_stream_raw_socket` / `_shutdown_pool_sockets` — only
socket.shutdown() unblocks a recv() wedged in the kernel). `core.llm` re-imports every name under
its original name, so `looplab.core.llm._stream_with_idle_guard` (and the flat `looplab.llm.…`)
READ the same objects — tests and callers import and call through those paths.

There used to be a SECOND, urllib-era reassembly path here (`_socket_watchdog` + `_sse_chunks` +
`_SSETail` + `_raw_socket`, driven by `OpenAICompatibleClient._read_stream`) that no production
code had called since the openai-SDK migration — only tests. It is gone (doc 25 CO-03), along with
`_parse_chat_body`, whose sole caller it was. Its two contracts live on where they are actually
exercised: the stall-kill in `_stream_with_idle_guard` (and its real-socketpair test), and the
degenerate-body path in `_post`'s empty-response classification.

MONKEYPATCH THROUGH THIS MODULE, not through `core.llm` (doc 25 CO-10). Helpers here call each
other by bare global name — `_stream_with_idle_guard` -> `_stream_raw_socket`/`_chunk_has_content`
— and those lookups resolve in THIS module's namespace. Rebinding the `core.llm` alias replaces
only that alias and never reaches the live call.
"""
from __future__ import annotations

import time

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

        # The idle clock measures the PROVIDER only. This is a generator, so wall time also passes
        # while it is suspended at `yield` — and a slow CONSUMER (a UI client draining
        # complete_text_stream's SSE relay slower than idle_limit) drew no events, never reset
        # `last`, and got a healthy connection shut down with an APITimeoutError blaming the
        # endpoint. Time spent inside the consumer is tracked and excluded: `spent` accumulates
        # completed yields, `suspended` marks one in flight (the case that matters — a consumer
        # blocked at a single yield adds nothing to `spent` while it blocks).
        spent = [0.0]                                 # consumer time already returned from
        spent_at_last = [0.0]                         # `spent` when `last` was set
        suspended = [0.0]                             # monotonic start of the in-flight yield, else 0

        def _consumer_gap(now: float) -> float:
            """Consumer time since `last` was set — subtract it before judging the provider."""
            done = spent[0] - spent_at_last[0]
            live = suspended[0]
            return max(0.0, done + ((now - live) if live else 0.0))

        def _wd():
            while not stop.wait(min(5.0, min(fb, idle_limit) / 4)):
                now = time.monotonic()
                # Before ANY chunk: bound the black-holed first byte by `fb`. Once the connection is
                # producing, bound the gap between REAL-CONTENT chunks by `idle_limit` — a proxy that
                # trickles EMPTY keepalive chunks (role-only / blank deltas) to hold the socket open
                # can no longer mask a stalled generation, because empties don't reset `last` (the
                # bug: a 74-min live hang where the watchdog never fired because every keepalive chunk
                # reset the idle timer).
                gap = _consumer_gap(now)
                stalled = ((now - start - gap > fb) if not conn[0]
                           else (now - last[0] - gap > idle_limit))
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
                    spent_at_last[0] = spent[0]
                _y0 = time.monotonic()
                suspended[0] = _y0                # the watchdog must not blame the provider for this
                try:
                    yield ev
                finally:
                    suspended[0] = 0.0
                    spent[0] += time.monotonic() - _y0
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
