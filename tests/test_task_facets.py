"""AGENTIC task FACETS (§21.20.2) — the LLM classifier the deterministic passport deliberately omits.
Pins: LLM classifies into the fixed axes, facets are an advisory OVERLAY (never in the deterministic
index — CR0 rebuild stays byte-identical), and facet overlap widens cross-task scope for the read tool.
"""
from __future__ import annotations

import orjson

from looplab.engine.task_facets import (
    FACET_AXES, facet_overlap, load_task_facets, propose_task_facets, record_task_facets, steward_task_facets,
)


class _Client:
    def __init__(self, facets):
        self._f = facets

    def complete_tool(self, messages, json_schema):
        return {"facets": self._f}

    def complete_text(self, messages):
        return "{}"


def test_no_client_is_empty():
    assert propose_task_facets("dense retrieval on russian reviews", "dataset", None) == {}


def test_llm_classifies_into_fixed_axes_only():
    client = _Client({"domain": "Information-Retrieval", "language": "russian", "modality": "text",
                      "interaction": "pairwise", "objective": "ranking", "bogus_axis": "x"})
    f = propose_task_facets("dense retrieval on russian reviews", "dataset", client)
    assert set(f) <= set(FACET_AXES) and "bogus_axis" not in f
    assert f["domain"] == "information-retrieval" and f["language"] == "russian"   # normalized


def test_bad_output_degrades_to_empty():
    class _Boom:
        def complete_tool(self, m, j):
            raise RuntimeError("boom")

        def complete_text(self, m):
            raise RuntimeError("no")

    assert propose_task_facets("goal", "dataset", _Boom()) == {}


def test_record_and_load_last_write_wins(tmp_path):
    record_task_facets(str(tmp_path), task_id="t1", facets={"domain": "ir", "language": "ru"})
    record_task_facets(str(tmp_path), task_id="t1", facets={"domain": "ir", "language": "en"})
    got = load_task_facets(str(tmp_path))
    assert got["t1"] == {"domain": "ir", "language": "en"}


def test_facet_overlap_counts_shared_axes():
    a = {"domain": "ir", "language": "ru", "modality": "text"}
    b = {"domain": "ir", "language": "en", "modality": "text"}
    assert facet_overlap(a, b) == 2 and facet_overlap(a, {}) == 0


def test_scope_profile_facets_are_an_overlay_off_the_index():
    from looplab.engine.cross_run_index import scope_profile
    base = scope_profile(task_id="t", kind="dataset", direction="max", goal="g", metric="recall")
    withf = scope_profile(task_id="t", kind="dataset", direction="max", goal="g", metric="recall",
                          facets={"domain": "ir"})
    assert "facets" not in base                          # no facets passed -> byte-identical passport
    assert withf["facets"] == {"domain": "ir"} and withf["fingerprint"] == base["fingerprint"]


def test_facet_overlap_widens_read_tool_scope(tmp_path):
    # two DIFFERENT task ids that share 2 facet axes: a lesson from taskB reaches a query bound to taskA
    from types import SimpleNamespace
    from looplab.tools.cross_run_tools import CrossRunTools
    record_task_facets(str(tmp_path), task_id="taskA", facets={"domain": "ir", "modality": "text"})
    record_task_facets(str(tmp_path), task_id="taskB", facets={"domain": "ir", "modality": "text"})
    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps(
        {"statement": "reranking helps recall", "outcome": "supported", "evidence": [1],
         "run_id": "rB", "task_id": "taskB"}) + b"\n")
    tools = CrossRunTools(tmp_path, role="researcher")
    tools.bind_state(SimpleNamespace(task_id="taskA", goal="totally different wording"))
    row = {"task_id": "taskB", "fingerprint": ["unrelated"]}
    assert tools._in_scope(row)                          # facet overlap (>=2) brings taskB into scope


def test_steward_end_to_end(tmp_path):
    client = _Client({"domain": "ir", "language": "ru", "modality": "text"})
    out = steward_task_facets(str(tmp_path), client, task_id="t1", goal="dense retrieval ru", apply=True)
    assert out["facets"]["domain"] == "ir" and load_task_facets(str(tmp_path))["t1"]["language"] == "ru"


def test_cli_task_facets(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from looplab.cli import app
    import looplab.cli as cli
    monkeypatch.setattr(cli, "make_llm_client",
                        lambda *a, **k: _Client({"domain": "ir", "modality": "text"}))
    r = CliRunner().invoke(app, ["task-facets", str(tmp_path), "dense retrieval",
                                 "--task-id", "t1", "--apply"])
    assert r.exit_code == 0 and "domain" in r.stdout and "recorded for task 't1'" in r.stdout
    assert load_task_facets(str(tmp_path))["t1"]["domain"] == "ir"
