"""General web tools for the Deep-Research stage (network-OPTIONAL), companion to `literature.py`'s
arXiv grounding. Two tools the agentic researcher can call: `web_search` (a dependency-free
DuckDuckGo HTML query returning top result titles + URLs + snippets) and `web_fetch` (GET a URL and
return a crude text extraction). Behind an explicit flag (`web_search` in Settings) because network
egress is unreliable on some boxes / corporate proxies, and every call degrades gracefully — a
blocked/failed request returns a clear "(unavailable)" string rather than crashing the run.
Dependency-free (stdlib urllib + tiny regex parsing), exactly like `LiteratureTools`.

THE WEB DENY-LIST (`EvalSpec.web_deny`, 2026-09-06). `web_fetch` reaches the open web, and the open
web holds PUBLISHED SOLUTIONS to benchmark tasks. Measured over the AlgoTune probe corpus
(docs/56 §150, finding 13, re-derived by hand): **52 of 76 runs (68 %) fetched AlgoTune's published
solver source for the very task they were being graded on**, every one of the 52 with the solver's
source in the tool result — while the card's fence ("the evaluator and the timer are fenced and
are not yours to look at") was PROSE. The measured effect on score was negative, so this is a
validity hazard rather than score inflation, and no result in that document could be stratified on
it. A fence that is merely stated holds right up until it matters (`tools/env_inspect.py`'s own
measurement: a route that opens under pressure).

So the fence is a DECLARATION, exactly like `protect_packages` one field over: the operator who
built the task names the URL prefixes that hold the graded task's published solutions, leaderboard
or grader (`web_deny`), and a fetch under one is REFUSED with a message that names the declaration
— never a silent empty page and never "(unreachable)", which would send the model looking for a
mirror. It is never DERIVED from the URL or the page (docs/36: a fence is a boundary the operator
declares, not a heuristic over names) — a heuristic that guessed "looks like a solutions page"
would refuse a legitimate write-up and still miss the mirror it did not know. Empty (the default)
fences nothing, byte for byte the historical tool. A refusal rides on the tool span as
`result_structured.web_fetch_refused` (the prefix that fired) so a run can be audited for it —
doc 60 A3's acceptance is "refusal count = 0 on a control run", which needs the count to exist.
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
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme.lower() not in urllib.request.getproxies():
            return False
        host = parsed.hostname or ""
        netloc = f"{host}:{parsed.port}" if parsed.port else host
        # `host:port` when the url carries one, because that is what urllib's ProxyHandler passes
        # (`req.host`) and `proxy_bypass_environment` matches NO_PROXY entries against BOTH forms.
        # With the portless hostname, a NO_PROXY entry naming an explicit port
        # ("internal.corp:8080") made urllib connect DIRECT while this answered "proxied" — skipping
        # the peer check on exactly the direct connection it guards.
        return not urllib.request.proxy_bypass(netloc)
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


class WebDenyRefusal(Exception):
    """A fetch landed under an operator-declared `web_deny` prefix (directly, or via a redirect).

    Its OWN type so `_fetch` can answer with the refusal sentence rather than letting the generic
    network `except` turn it into "(web fetch unavailable: ...)" — the "(unreachable)" shape the
    deny-list exists NOT to produce: it reads as a transport failure and sends the model to a
    mirror. Carries the URL that matched and the prefix that fired."""

    def __init__(self, url: str, prefix: str):
        super().__init__(web_deny_refusal(url, prefix))
        self.url = url
        self.prefix = prefix


def normalize_web_deny(entries) -> tuple:
    """Validate operator-declared `web_deny` prefixes into the ONE form `web_deny_match` compares.

    Each entry must be an absolute `http(s)://` URL with a host; anything else is a `ValueError`
    naming the entry, because a prefix that can never match is a fence that fences nothing while
    the task file says it does — the same reason `envsafe.validate_env_map` refuses rather than
    drops. Whitespace is stripped, the scheme and host are lower-cased (both are case-insensitive
    by RFC 3986), the path is kept as written. Duplicates collapse, order is kept."""
    out: list[str] = []
    for raw in (entries or ()):
        text = str(raw or "").strip()
        if not text:
            raise ValueError("web_deny: an empty entry fences nothing; remove it")
        parsed = urllib.parse.urlsplit(text)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            raise ValueError(
                f"web_deny: {text!r} is not an absolute http(s) URL prefix (it needs a scheme and "
                "a host, e.g. 'https://github.com/org/repo/')")
        norm = urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                                        parsed.path, parsed.query, ""))
        if norm not in out:
            out.append(norm)
    return tuple(out)


def _host_path(url: str) -> tuple:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return host, path


def web_deny_match(url: str, deny) -> str | None:
    """The declared prefix that covers `url`, or None.

    A prefix covers a URL when the URL's host IS the prefix's host or a subdomain of it
    (`www.algotune.io` under `algotune.io`; never `algotune.io.evil.example`, which a bare string
    `startswith` would have admitted), and the URL's path starts with the prefix's path — compared
    case-insensitively, since the harmless direction of a case mismatch is over-fencing and the
    harmful one (`/OriPress/AlgoTune` walking around `/oripress/AlgoTune`) is an evasion. The
    SCHEME is deliberately not compared: the declaration is about a RESOURCE, not a transport, and
    the `http://` spelling of a denied `https://` page is the same page one redirect later. A prefix
    with no path (`https://algotune.io`) covers the whole host; end a prefix with `/` to bound it to
    that DIRECTORY — which it names with or without the slash (`.../AlgoTune/` covers the repo's
    landing page `.../AlgoTune` and everything under it, and not `.../AlgoTune-fork/`)."""
    host, path = _host_path(url)
    if not host:
        return None
    for prefix in (deny or ()):
        p_host, p_path = _host_path(prefix)
        if not p_host:
            continue
        if host != p_host and not host.endswith("." + p_host):
            continue
        p_low, low = p_path.lower(), path.lower()
        if p_path in ("", "/") or low.startswith(p_low) or (
                p_low.endswith("/") and low.split("?", 1)[0] == p_low[:-1]):
            return prefix
    return None


def web_deny_refusal(url: str, prefix: str) -> str:
    """The refusal sentence. NAMES THE DECLARATION and says the page exists: "(unavailable)" would
    send the model to a mirror, and a silent empty page would read as an empty page."""
    return (f"(web_fetch refused: {url} is under the operator-declared `web_deny` prefix {prefix}. "
            "Published solutions, leaderboards and the grader of the task being evaluated are "
            "fenced from this run — the page exists, this is a fence and not an unreachable URL, "
            "and a mirror of it is fenced by the same declaration. Ground the idea in the task's "
            "own description and in your own measurements instead.)")


def task_web_deny(task) -> tuple:
    """The task's declared `EvalSpec.web_deny`, through its `eval_spec()` hook — `()` for a task
    with no spec, an adapter that raises, or a declaration that does not validate, the same
    total-and-quiet contract `repo_developer._grader_packages` keeps for `protect_packages`."""
    try:
        spec_fn = getattr(task, "eval_spec", None)
        ev = spec_fn() if callable(spec_fn) else {}
        return normalize_web_deny((ev or {}).get("web_deny") or ())
    except Exception:  # noqa: BLE001 - no spec, no fence: never let a fence break tool construction
        return ()


def build_web_tools(task) -> "WebTools":
    """The ONE constructor of the Researcher-side web tool, carrying the task's `EvalSpec.web_deny`.

    Two sites compose `WebTools` (`agents/factory.py::build_strategist_tools` and
    `agents/deep_research.py::make_deep_researcher`), and until 2026-09-06 both spelled
    `WebTools(enabled=True)` — so the operator's declaration reached neither, which is how 52 of 76
    AlgoTune runs fetched their own task's published solver (docs/56 §150 #13). It lives HERE and
    not in the factory because it is the fence's own composition rule and `agents/factory.py`
    holds a line ceiling whose guard prescribes extraction (`tests/test_agent_factory_split.py`).
    A task with no spec fences nothing (`task_web_deny`)."""
    return WebTools(enabled=True, deny=task_web_deny(task))


class _SSRFRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-run the SSRF check on every redirect target: urlopen follows redirects without re-checking,
    so a public URL could 302 into 169.254.169.254 / an internal host and exfiltrate it otherwise.

    Since 2026-09-06 the operator's `web_deny` is re-checked on every hop for the same reason: a
    fetch of a short-link or a mirror that 302s INTO a denied prefix is the denied page arriving one
    hop later, and a preflight over the caller's URL cannot see it."""
    def __init__(self, deny=()):
        super().__init__()
        self.deny = tuple(deny or ())

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        blocked = _ssrf_blocked(newurl)
        if blocked:
            raise urllib.error.HTTPError(newurl, code, f"SSRF-blocked redirect: {blocked}", headers, fp)
        prefix = web_deny_match(newurl, self.deny)
        if prefix:
            raise WebDenyRefusal(newurl, prefix)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SSRF_OPENER = urllib.request.build_opener(_SSRFRedirectHandler)

from looplab.tools._base import ToolResult, fn_spec

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
    """`web_search` + `web_fetch`. `enabled=False` (or a network failure) -> a graceful message.

    `deny` is the operator's `EvalSpec.web_deny` (see the module docstring): URL prefixes a
    `web_fetch` may not reach. Empty fences nothing and leaves every byte of the historical tool in
    place — the spec text, the opener, the answers."""

    def __init__(self, enabled: bool = True, max_results: int = 5, timeout: float = 8.0,
                 max_bytes: int = 4000, deny=()):
        self.enabled = enabled
        self.max_results = max_results
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.deny = normalize_web_deny(deny)
        # The module opener when nothing is fenced (it is a documented patch seam —
        # `tests/test_web_tools.py` binds `_SSRF_OPENER.open`), and a private one carrying the
        # deny-list otherwise, so the redirect re-check can see the declaration.
        self._opener = (_SSRF_OPENER if not self.deny
                        else urllib.request.build_opener(_SSRFRedirectHandler(deny=self.deny)))

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
                "truncated. Use to read a promising result in more detail."
                # Spliced only when something IS fenced, so an unfenced task's spec is byte-identical
                # (prompt strings are contracts). The model is told the shape of the refusal, not
                # the list: naming the prefixes would be handing it the map of what to look for.
                + (" Some URL prefixes are fenced by the operator (the published solutions, "
                   "leaderboard or grader of the task being evaluated); a fetch under one is "
                   "refused and says so." if self.deny else ""),
                {"url": {"type": "string", "description": "an http(s) URL to fetch"}},
                ["url"]),
        ]

    def execute(self, name: str, args: dict) -> str:
        return self.execute_result(name, args).content

    def execute_result(self, name: str, args: dict, *, cancel_check=None) -> ToolResult:
        """The typed twin of `execute`. A `web_deny` refusal is DATA on the result, not only a
        sentence in it: `structured.web_fetch_refused` names the prefix that fired and rides onto the
        tool span as `result_structured` (`agents/tool_loop.py::_run_tool_call` stamps every key of
        `ToolResult.trace_attributes`), which is what makes "how many fetches did this run have
        refused" a question `spans.jsonl` can answer. Every other answer is the historical string."""
        if not self.enabled:
            return ToolResult(content="(web tools disabled — enable web_search to use general web "
                                      "grounding)", is_error=True, retryable=False,
                              provenance={"source": "web"})
        if name == "web_search":
            return ToolResult(content=self._search(str((args or {}).get("query", "")).strip()),
                              provenance={"source": "web"})
        if name == "web_fetch":
            url = str((args or {}).get("url", "")).strip()
            try:
                text = self._fetch(url)
            except WebDenyRefusal as refused:
                return ToolResult(
                    content=str(refused), is_error=True, retryable=False,
                    structured={"refused": "web_deny", "web_fetch_refused": refused.prefix,
                                "url": refused.url},
                    provenance={"source": "web", "fence": "web_deny"})
            return ToolResult(content=text, provenance={"source": "web"})
        return ToolResult(content=f"(unknown tool: {name})", is_error=True, retryable=False,
                          provenance={"source": "web"})

    def _get(self, url: str, data: bytes | None = None) -> str:
        req = urllib.request.Request(url, data=data, headers={"User-Agent": _UA})
        with self._opener.open(req, timeout=self.timeout) as r:   # re-checks SSRF on each redirect hop
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
        """The page text, or a refusal string. RAISES `WebDenyRefusal` for a fenced URL — the one
        exception this method lets out, because `execute_result` must tell a refusal from a page
        and the string alone cannot carry the prefix onto the span."""
        if not url or not url.startswith(("http://", "https://")):
            return "(web_fetch needs an http(s) URL)"
        blocked = _ssrf_blocked(url)
        if blocked:
            return f"(web_fetch refused: {blocked})"
        # BEFORE the request, not after: a refused page must never be downloaded at all, and the
        # SSRF preflight above is the precedent for refusing on the caller's URL.
        prefix = web_deny_match(url, self.deny)
        if prefix:
            raise WebDenyRefusal(url, prefix)
        try:
            html = self._get(url)
        except WebDenyRefusal:
            raise                                   # a redirect landed under a denied prefix
        except Exception as e:  # noqa: BLE001 — network is best-effort; never crash the run
            return f"(web fetch unavailable: {e})"
        text = _untag(html)
        return text[: self.max_bytes] + ("…" if len(text) > self.max_bytes else "")
