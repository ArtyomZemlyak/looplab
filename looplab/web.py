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

from .knowledge_tools import _fn_spec

_DDG = "https://html.duckduckgo.com/html/"
_UA = "Mozilla/5.0 (compatible; LoopLab/1.0; +https://example.invalid/looplab)"
# DuckDuckGo HTML result anchors + snippets (class names are stable on the html endpoint).
_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.DOTALL)
_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(?P<snip>.*?)</a>', re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_WS = re.compile(r"\s+")


def _untag(html: str) -> str:
    """Strip tags + collapse whitespace to plain text (best-effort, no HTML parser dependency)."""
    return _WS.sub(" ", _TAG.sub(" ", _SCRIPT.sub(" ", html))).strip()


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
            _fn_spec(
                "web_search",
                "Search the web (DuckDuckGo) for techniques, datasets, baselines or write-ups to "
                "ground the next idea. Returns the top result titles, URLs and snippets.",
                {"query": {"type": "string",
                           "description": "search terms, e.g. 'gradient boosting tabular leakage'"}},
                ["query"]),
            _fn_spec(
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
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read().decode("utf-8", errors="replace")

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
