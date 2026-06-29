"""E3 · Literature-grounded ideation (ADR-16), network-OPTIONAL. A tool the agentic Researcher can
call to ground proposals in real techniques: an arXiv search returning the top paper titles +
abstracts. Behind an explicit flag (network egress is unreliable on some boxes / corporate proxies),
and it degrades gracefully — a blocked/failed request returns a clear "(unavailable)" string rather
than crashing the run. Dependency-free (stdlib urllib + a tiny Atom regex parse).
"""
from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request

from .knowledge_tools import _fn_spec

_ARXIV = "http://export.arxiv.org/api/query"
_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.DOTALL)
_TITLE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
_SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)


class LiteratureTools:
    """A single `arxiv_search` tool. `enabled=False` (or a network failure) -> a graceful message."""

    def __init__(self, enabled: bool = True, max_results: int = 3, timeout: float = 8.0):
        self.enabled = enabled
        self.max_results = max_results
        self.timeout = timeout

    def specs(self) -> list[dict]:
        return [_fn_spec(
            "arxiv_search",
            "Search arXiv for relevant ML techniques/papers to ground the next idea. "
            "Returns the top paper titles + abstracts.",
            {"query": {"type": "string", "description": "search terms, e.g. 'tabular feature engineering'"}},
            ["query"])]

    def execute(self, name: str, args: dict) -> str:
        if name != "arxiv_search":
            return f"(unknown tool: {name})"
        if not self.enabled:
            return "(literature search disabled — enable literature_search to use arXiv grounding)"
        query = str((args or {}).get("query", "")).strip()
        if not query:
            return "(no query)"
        try:
            url = f"{_ARXIV}?" + urllib.parse.urlencode(
                {"search_query": f"all:{query}", "start": 0, "max_results": self.max_results})
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                xml = r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001 — network is best-effort; never crash the run
            return f"(literature search unavailable: {e})"
        out = []
        for i, entry in enumerate(_ENTRY.findall(xml)[: self.max_results], 1):
            t = _TITLE.search(entry)
            s = _SUMMARY.search(entry)
            title = html.unescape(re.sub(r"\s+", " ", (t.group(1) if t else "")).strip())
            summary = html.unescape(re.sub(r"\s+", " ", (s.group(1) if s else "")).strip())[:300]
            out.append(f"{i}. {title}\n   {summary}")
        return "\n".join(out) if out else "(no results)"
