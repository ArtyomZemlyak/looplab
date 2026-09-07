"""The run's own turns as EXECUTION-GROUNDED training rows (doc 52 row 33).

Frontis-MA1 (39.39 → 60.61 %) and SandMLE (+20–67 % relative) train operators from exactly the
corpus `spans.jsonl` already holds — what a role was handed and what it answered — and LoopLab
exported MLflow and a notebook only. The thing that makes the corpus worth anything is the
GROUNDING: a turn is a training example only in company with what the node it belongs to actually
produced, and these tests are about that join and about what the export refuses to pretend.
"""
from __future__ import annotations

import json

from typer.testing import CliRunner

from looplab.cli import app
from looplab.events.eventstore import EventStore


def _span(span_id, *, op="propose", node_id=None, output="an answer", messages=None, kind="generation"):
    attributes = {"op": op, "model": "m", "phase": "build"}
    if node_id is not None:
        attributes["node_id"] = node_id
    if messages is not None:
        attributes["input"] = messages
    if output is not None:
        attributes["output"] = output
    return {"name": kind, "kind": kind, "trace_id": "t1", "span_id": span_id, "parent_id": None,
            "run_id": "r", "attributes": attributes, "events": [], "status": "OK",
            "start": 0.0, "duration_s": 1.0}


def _run(tmp_path, spans):
    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    store.append("node_evaluated", {"node_id": 0, "metric": 0.25})
    store.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                                  "idea": {"operator": "improve", "params": {}, "rationale": ""}})
    store.append("node_failed", {"node_id": 1, "error": "boom", "reason": "crash"})
    (rd / "spans.jsonl").write_text(
        "".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    return rd


def _rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_every_answered_turn_becomes_a_row_carrying_its_nodes_outcome(tmp_path):
    rd = _run(tmp_path, [
        _span("s1", node_id=0, messages=[{"role": "user", "content": "propose something"}]),
        _span("s2", op="implement", node_id=1,
              messages=[{"role": "user", "content": "implement it"}], output="code"),
        _span("s3", kind="operation", node_id=0, messages=[{"role": "user", "content": "x"}]),
    ])
    result = CliRunner().invoke(app, ["export-sft", str(rd)])
    assert result.exit_code == 0, result.output
    rows = _rows(rd / "sft.jsonl")
    assert len(rows) == 2                      # the operation span is not a turn
    first, second = rows
    assert first["messages"] == [{"role": "user", "content": "propose something"}]
    assert first["completion"] == "an answer" and first["op"] == "propose"
    # THE GROUNDING: the node's own outcome rides with the turn, both when it worked …
    assert first["outcome"] == {"node_id": 0, "metric": 0.25, "status": "evaluated",
                                "feasible": True, "error_reason": ""}
    # … and when it did not, which is what makes the corpus filterable rather than flattering
    assert second["outcome"]["metric"] is None and second["outcome"]["error_reason"] == "crash"
    assert first["run_id"] == "r" and first["task_id"] == "t" and first["direction"] == "min"
    assert "2 turn(s) from 2 generation span(s)" in result.output


def test_a_turn_with_no_answer_is_not_a_training_example_and_is_not_an_error(tmp_path):
    """A budget cut, a transport failure and a refusal all end a generation with an input and
    nothing after it. Counting those as rows teaches an operator to answer nothing."""
    rd = _run(tmp_path, [
        _span("s1", node_id=0, messages=[{"role": "user", "content": "ask"}], output=None),
        _span("s2", node_id=0, messages=[{"role": "user", "content": "ask again"}]),
    ])
    result = CliRunner().invoke(app, ["export-sft", str(rd)])
    assert result.exit_code == 0
    assert len(_rows(rd / "sft.jsonl")) == 1
    assert "1 generation(s) had no answer to learn from" in result.output


def test_only_successful_keeps_the_turns_whose_node_produced_a_number(tmp_path):
    rd = _run(tmp_path, [
        _span("s1", node_id=0, messages=[{"role": "user", "content": "a"}]),
        _span("s2", node_id=1, messages=[{"role": "user", "content": "b"}]),
        _span("s3", messages=[{"role": "user", "content": "run-level turn, no node"}]),
    ])
    result = CliRunner().invoke(app, ["export-sft", str(rd), "--only-successful"])
    assert result.exit_code == 0
    rows = _rows(rd / "sft.jsonl")
    assert [row["outcome"]["node_id"] for row in rows] == [0]
    assert "2 turn(s) dropped by --only-successful" in result.output


def test_the_op_filter_selects_one_role(tmp_path):
    rd = _run(tmp_path, [
        _span("s1", op="propose", node_id=0, messages=[{"role": "user", "content": "a"}]),
        _span("s2", op="implement", node_id=0, messages=[{"role": "user", "content": "b"}]),
    ])
    result = CliRunner().invoke(app, ["export-sft", str(rd), "--op", "implement"])
    assert result.exit_code == 0
    assert [row["op"] for row in _rows(rd / "sft.jsonl")] == ["implement"]


def test_a_run_with_tracing_off_says_so_rather_than_writing_an_empty_corpus(tmp_path):
    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    result = CliRunner().invoke(app, ["export-sft", str(rd)])
    assert result.exit_code == 2 and "no spans.jsonl" in result.output
    assert not (rd / "sft.jsonl").exists()


def test_a_truncated_input_chain_is_marked_on_the_row(tmp_path):
    """`hydrate_inputs` stamps `input_partial` when an ancestor span is missing, and the export
    carries it: a reader must be able to tell a complete retained projection from a short one."""
    rd = _run(tmp_path, [
        {**_span("s2", node_id=0, messages=[{"role": "user", "content": "tail"}]),
         "attributes": {**_span("s2", node_id=0,
                                messages=[{"role": "user", "content": "tail"}])["attributes"],
                        "input_from": "missing-ancestor", "input_carry": 3}},
    ])
    result = CliRunner().invoke(app, ["export-sft", str(rd)])
    assert result.exit_code == 0
    assert _rows(rd / "sft.jsonl")[0].get("input_partial") is True
