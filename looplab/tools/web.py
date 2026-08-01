"""General web tools for the Deep-Research stage (network-OPTIONAL), companion to `literature.py`'s
arXiv grounding. Two tools the agentic researcher can call: `web_search` (a dependency-free
DuckDuckGo HTML query returning top result titles + URLs + snippets) and `web_fetch` (GET a URL and
return a crude text extraction). Behind an explicit flag (`web_search` in Settings) because network
egress is unreliable on some boxes / corporate proxies, and every call degrades gracefully — a
blocked/failed request returns a clear "(unavailable)" string rather than crashing the run.
Dependency-free (stdlib urllib + tiny regex parsing), exactly like `LiteratureTools`.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request


def _ssrf_blocked(url: str) -> str | None:
    """SSRF guard: reject a URL whose host resolves to a private / loopback / link-local / reserved
    address (incl. the cloud-metadata endpoint 169.254.169.254), so a model- or page-supplied URL can't
    pull internal services / credentials into the run. Returns a reason when blocked, else None. Best
    effort (checks the initial host; a DNS failure falls through to let urlopen surface its own error)."""
    try:
        host = urllib.parse.urlparse(url).hostname
        if not host:
            return "no host"
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return f"refusing to fetch internal address {ip} (host {host})"
    except (socket.gaierror, ValueError):
        return None
    return None

def _proxied(url: str) -> bool:
    """Will urllib send THIS url through an env-configured proxy?

    Mirrors urllib's own decision (`getproxies()` for the scheme, `proxy_bypass()` for NO_PROXY),
    because the peer check below is only meaningful when we connected to the TARGET ourselves.
    """
    try:
        scheme = urllib.parse.urlparse(url).scheme.lower()
        host = urllib.parse.urlparse(url).hostname or ""
        if scheme not in urllib.request.getproxies():
            return False
        # CLAUDE REVIEW: [EDGE-CASE] Passes the PORTLESS hostname, but urllib's ProxyHandler passes
        # req.host (host:port) to proxy_bypass, and proxy_bypass_environment matches NO_PROXY entries
        # against both forms. A NO_PROXY entry with an explicit port ("internal.corp:8080") makes
        # urllib connect DIRECT while this returns True ("proxied") — so the peer check is skipped on
        # a direct connection. Pass the same host:port urllib uses.
        return not urllib.request.proxy_bypass(host)
    except Exception:  # noqa: BLE001 — an unreadable proxy env must not decide the fetch either way
        return False


def _peer_blocked(response) -> str | None:
    """Verify the address we ACTUALLY connected to, after the socket is open.

    `_ssrf_blocked` resolves the host itself and the transport then resolves again, so the two can
    disagree: a short-TTL DNS rebind returns a public address to the preflight and loopback /
    RFC1918 / 169.254.169.254 to the connect — the classic SSRF TOCTOU, which a preflight
    `getaddrinfo` can never close on its own. Checking `getpeername()` closes it for the case that
    matters: the response body is refused before a single byte reaches the caller (and therefore the
    model), on the initial request and on every redirect hop the opener follows.
    """
    try:
        sock = response.fp.raw._sock            # CPython http.client stream -> the live socket
        peer = sock.getpeername()[0]
    except (AttributeError, OSError, IndexError, TypeError):
        return None                             # unknown transport shape -> keep prior behaviour
    try:
        ip = ipaddress.ip_address(peer.split("%", 1)[0])
    except ValueError:
        return None
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        return f"refusing a connection that landed on internal address {ip}"
    return None


class _SSRFRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-run the SSRF check on every redirect target: urlopen follows redirects without re-checking,
    so a public URL could 302 into 169.254.169.254 / an internal host and exfiltrate it otherwise."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        blocked = _ssrf_blocked(newurl)
        if blocked:
            raise urllib.error.HTTPError(newurl, code, f"SSRF-blocked redirect: {blocked}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SSRF_OPENER = urllib.request.build_opener(_SSRFRedirectHandler)

from looplab.tools._base import fn_spec

_DDG = "https://html.duckduckgo.com/html/"
_UA = "Mozilla/5.0 (compatible; LoopLab/1.0; +https://example.invalid/looplab)"
# Bound the DOWNLOAD itself (not just the text handed to the agent, which is already capped to
# max_bytes): a plain `r.read()` slurps the WHOLE response into host RAM before truncation, so a huge
# / hostile / endless URL is a memory-blowup vector. Read at most this many bytes — far above the ~4k
# chars ever surfaced, small enough to bound RAM. A bigger body is cut here (marked truncated).
_MAX_DOWNLOAD_BYTES = 2_000_000
# How much longer than the per-socket-op timeout the WHOLE body read may take. `urlopen`'s timeout
# bounds one recv, never the transfer, so a hostile server dripping a byte just inside it held the
# thread for up to ~2M reads. A slow but honest CDN still finishes well inside this multiple.
_READ_DEADLINE_FACTOR = 4.0


def _read_bounded(stream, timeout: float, limit: int = _MAX_DOWNLOAD_BYTES) -> bytes:
    """Read at most `limit` bytes AND at most a wall-clock deadline's worth of them.

    Size alone was bounded before: `read(n)` returns at most n bytes, so a multi-GB or endless
    response cannot exhaust host RAM. But TIME was not — `read(n)` blocks until n bytes or EOF and
    `urlopen`'s timeout is per-socket-operation, so a server dripping one byte per (timeout - ε)
    seconds kept this thread alive essentially forever. That matters here specifically because
    `drive_tool_loop` calls `tools.execute()` SYNCHRONOUSLY and its `time_budget_s` does not
    interrupt an in-flight turn: one slow-drip `web_fetch` wedged the whole research phase.

    Whatever arrived before the deadline is returned — a truncated page is strictly better than a
    hung loop, and every caller already treats the body as best-effort text.
    """
    deadline = time.monotonic() + max(1.0, float(timeout or 0) * _READ_DEADLINE_FACTOR)
    chunks: list[bytes] = []
    got = 0
    while got < limit:
        if time.monotonic() >= deadline:
            break
        block = stream.read(min(65536, limit - got))
        if not block:                       # EOF
            break
        chunks.append(block)
        got += len(block)
    return b"".join(chunks)


# DuckDuckGo HTML result anchors + snippets (class names are stable on the html endpoint).
_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.DOTALL)
_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(?P<snip>.*?)</a>', re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_SCRIPT_OPEN = re.compile(r"<(script|style)\b", re.IGNORECASE)
_SCRIPT_CLOSE = {"script": re.compile(r"</script\s*>", re.IGNORECASE),
                 "style": re.compile(r"</style\s*>", re.IGNORECASE)}
_WS = re.compile(r"\s+")


def _strip_script_blocks(html: str) -> str:
    """Drop `<script>`/`<style>` blocks in ONE forward pass over the document.

    This was a single `<(script|style)\\b.*?</\\1>` substitution, which is QUADRATIC on hostile
    input: the lazy body re-scans to end-of-document looking for a close tag, and the engine then
    retries from the next `<script`. A 2 MB page (the `_MAX_DOWNLOAD_BYTES` cap) stuffed with
    repeated unclosed `<script>` cost ~n²/2 steps — minutes of CPU inside `_untag`, on the tool-loop
    thread `drive_tool_loop` never interrupts. Here the cursor only ever moves forward, so a page of
    any shape costs one pass. (A bounded-body regex is NOT enough: it stops the body from re-walking
    but not the engine from restarting at each of the n open tags.)

    An UNCLOSED block keeps the old behaviour — the remainder is left in place for `_TAG` to strip.
    The one deliberate difference: a close tag with trailing space (`</script >`, valid HTML) now
    matches, where the old `</\\1>` did not and leaked the block's source into the extracted text.
    """
    out: list[str] = []
    pos = 0
    while True:
        m = _SCRIPT_OPEN.search(html, pos)
        if m is None:
            out.append(html[pos:])
            break
        out.append(html[pos:m.start()])
        close = _SCRIPT_CLOSE[m.group(1).lower()].search(html, m.end())
        if close is None:
            out.append(html[m.start():])
            break
        pos = close.end()
    return " ".join(out)


def _untag(html: str) -> str:
    """Strip tags + collapse whitespace to plain text (best-effort, no HTML parser dependency)."""
    return _WS.sub(" ", _TAG.sub(" ", _strip_script_blocks(html))).strip()


def _resolve(href: str) -> str:
    """DuckDuckGo wraps result links in a `/l/?uddg=<encoded>` redirect — unwrap it to the real URL."""
    if "uddg=" in href:
        q = urllib.parse.urlparse(href if "//" in href else "https:" + href).query
        target = urllib.parse.parse_qs(q).get("uddg", [None])[0]
        if target:
            return target
    return ("https:" + href) if href.startswith("//") else href


class WebTools:
    """`web_search` + `web_fetch`. `enabled=False` (or a network failure) -> a graceful message."""

    def __init__(self, enabled: bool = True, max_results: int = 5, timeout: float = 8.0,
                 max_bytes: int = 4000):
        self.enabled = enabled
        self.max_results = max_results
        self.timeout = timeout
        self.max_bytes = max_bytes

    def specs(self) -> list[dict]:
        return [
            fn_spec(
                "web_search",
                "Search the web (DuckDuckGo) for techniques, datasets, baselines or write-ups to "
                "ground the next idea. Returns the top result titles, URLs and snippets.",
                {"query": {"type": "string",
                           "description": "search terms, e.g. 'gradient boosting tabular leakage'"}},
                ["query"]),
            fn_spec(
                "web_fetch",
                "Fetch a single web page (from a web_search result URL) and return its main text, "
                "truncated. Use to read a promising result in more detail.",
                {"url": {"type": "string", "description": "an http(s) URL to fetch"}},
                ["url"]),
        ]

    def execute(self, name: str, args: dict) -> str:
        if not self.enabled:
            return "(web tools disabled — enable web_search to use general web grounding)"
        if name == "web_search":
            return self._search(str((args or {}).get("query", "")).strip())
        if name == "web_fetch":
            return self._fetch(str((args or {}).get("url", "")).strip())
        return f"(unknown tool: {name})"

    def _get(self, url: str, data: bytes | None = None) -> str:
        req = urllib.request.Request(url, data=data, headers={"User-Agent": _UA})
        with _SSRF_OPENER.open(req, timeout=self.timeout) as r:   # re-checks SSRF on each redirect hop
            # The preflight `_ssrf_blocked` and the transport resolve DNS INDEPENDENTLY, so a short-TTL
            # rebind can hand the check a public address and the connect a loopback/RFC1918/metadata one.
            # Verify the peer we actually reached before reading: no internal body ever reaches the
            # caller (or the model). This runs on the final hop, after the opener followed any redirects.
            # SKIPPED under a proxy. `_peer_blocked` verifies the address we actually connected to,
            # which closes the DNS-rebind TOCTOU a preflight `getaddrinfo` cannot. Behind an
            # env-configured proxy that peer is the PROXY, so the check answers a different question
            # entirely: on the common loopback/RFC1918 proxy it refused EVERY fetch — a total false
            # positive — and on a public one it passed everything while the rebind window was owned
            # by the proxy either way. The preflight and the per-redirect re-check are unchanged.
            # The proxy question is asked about the hop we ACTUALLY CONNECTED TO (`r.url`), not the
            # url the caller passed. Proxy-ness can flip mid-chain — a NO_PROXY host, or an
            # http->https redirect with only one of http_proxy/https_proxy set — so keying on the
            # original url meant original-proxied -> final-direct SKIPPED the peer check on the very
            # direct connection it exists for (reopening the rebind window), while original-direct ->
            # final-proxied false-blocked every fetch through a loopback proxy.
            landed = None if _proxied(getattr(r, "url", None) or url) else _peer_blocked(r)
            if landed:
                return f"(blocked: {landed})"
            return _read_bounded(r, self.timeout).decode("utf-8", errors="replace")

    def _search(self, query: str) -> str:
        if not query:
            return "(no query)"
        try:
            data = urllib.parse.urlencode({"q": query}).encode()  # POST avoids some bot gates
            html = self._get(_DDG, data=data)
        except Exception as e:  # noqa: BLE001 — network is best-effort; never crash the run
            return f"(web search unavailable: {e})"
        titles = _RESULT.findall(html)[: self.max_results]
        snippets = _SNIPPET.findall(html)
        out = []
        for i, (href, title) in enumerate(titles, 1):
            snip = _untag(snippets[i - 1]) if i - 1 < len(snippets) else ""
            out.append(f"{i}. {_untag(title)}\n   {_resolve(href)}\n   {snip[:300]}")
        return "\n".join(out) if out else "(no results)"

    def _fetch(self, url: str) -> str:
        if not url or not url.startswith(("http://", "https://")):
            return "(web_fetch needs an http(s) URL)"
        blocked = _ssrf_blocked(url)
        if blocked:
            return f"(web_fetch refused: {blocked})"
        try:
            html = self._get(url)
        except Exception as e:  # noqa: BLE001 — network is best-effort; never crash the run
            return f"(web fetch unavailable: {e})"
        text = _untag(html)
        return text[: self.max_bytes] + ("…" if len(text) > self.max_bytes else "")
