"""A memo source an external agent READ LOCALLY is kept, not blanked.

Codex audit 2026-10-14 L-08: the agent cited `{kind: "local", path, description}` and the memo
projection kept only `title`/`url`/`snippet`, so 124 of 144 sources in one memo became three empty
strings the UI then hid. The description now stands in for the title, the path rides beside it, and
a web source is projected exactly as before.
"""
from __future__ import annotations

from looplab.core.advisory_payloads import sanitize_research_memo_payload


def _memo(*sources):
    return {"summary": "s", "findings": ["f"], "sources": list(sources)}


def test_a_local_source_keeps_its_description_and_path():
    out = sanitize_research_memo_payload(_memo(
        {"kind": "local", "path": "reports/D3-measured-report.md",
         "description": "paired 95% CI table for D3"}), env={})
    (source,) = out["sources"]
    assert source["title"] == "paired 95% CI table for D3"
    assert source["path"] == "reports/D3-measured-report.md"
    assert source["kind"] == "local"
    assert source["url"] == ""


def test_a_path_only_source_is_titled_by_its_path():
    (source,) = sanitize_research_memo_payload(_memo(
        {"kind": "local", "path": "runs/x/stage1.log"}), env={})["sources"]
    assert source["title"] == "runs/x/stage1.log" and source["path"] == "runs/x/stage1.log"


def test_a_titled_source_uses_the_description_as_its_snippet():
    (source,) = sanitize_research_memo_payload(_memo(
        {"title": "trainer_state.json", "path": "out/trainer_state.json",
         "description": "loss flattens after step 4000"}), env={})["sources"]
    assert source["title"] == "trainer_state.json"
    assert source["snippet"] == "loss flattens after step 4000"


def test_a_web_source_is_projected_exactly_as_before():
    (source,) = sanitize_research_memo_payload(_memo(
        {"title": "Paper", "url": "https://arxiv.org/abs/1234.5678", "snippet": "abstract"}),
        env={})["sources"]
    assert set(source) == {"title", "url", "url_identity", "snippet"}
    assert source["title"] == "Paper" and source["snippet"] == "abstract"


def test_the_projection_is_idempotent_over_a_local_source():
    once = sanitize_research_memo_payload(_memo(
        {"kind": "local", "path": "a/b.md", "description": "why"}), env={})
    twice = sanitize_research_memo_payload(once, env={})
    assert twice["sources"] == once["sources"]
