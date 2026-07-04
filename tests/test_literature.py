"""E3 literature-grounded ideation (network-optional arXiv tool)."""
from __future__ import annotations

from looplab.tools.literature import LiteratureTools


def test_spec_shape():
    spec = LiteratureTools().specs()[0]
    assert spec["function"]["name"] == "arxiv_search"
    assert "query" in spec["function"]["parameters"]["properties"]


def test_disabled_returns_graceful_message():
    out = LiteratureTools(enabled=False).execute("arxiv_search", {"query": "x"})
    assert "disabled" in out and "Traceback" not in out


def test_unknown_tool():
    assert "unknown tool" in LiteratureTools().execute("nope", {})


def test_empty_query():
    assert LiteratureTools(enabled=True).execute("arxiv_search", {"query": "  "}) == "(no query)"


def test_network_failure_is_graceful(monkeypatch):
    # Point urlopen at a failure so the network path is exercised offline without crashing.
    import looplab.tools.literature as lit

    def _boom(*a, **k):
        raise OSError("blocked")

    monkeypatch.setattr(lit.urllib.request, "urlopen", _boom)
    out = LiteratureTools(enabled=True).execute("arxiv_search", {"query": "tabular FE"})
    assert out.startswith("(literature search unavailable")
