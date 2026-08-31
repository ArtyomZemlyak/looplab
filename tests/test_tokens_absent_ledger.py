"""`looplab tokens` says "no denominator", not "the ledger says zero".

`RunState.llm_cost` stays None when the log carries no `llm_usage`/`llm_cost` row at all — usage
rows lost, or a backend billed outside this engine. `token_spend_by_phase` has always handled that
correctly, and `test_token_spend.py` even names the collapse as a mutation of it ("'no ledger'
becomes 'the ledger says zero'"). The CALLER threw the distinction away one line before the call:
`int((state.llm_cost or {}).get("total_tokens") or 0)`.

REPRODUCED BEFORE THE FIX, with the run dir this test builds: one 400-token generation span and no
cost rows printed

    ledger     :              0 tokens (llm_usage, the durable record)
    residual   :           -400 tokens (spans over-attribute)

at exit 0 — two claims the log does not support, the second inviting exactly the "our spans
double-count" investigation that the first one manufactured.

DRIVEN THROUGH THE COMMAND, not the pure function: the function was already right, so a test of it
cannot see this defect at all. That is the whole lesson of the bug.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from looplab.cli import app


def _run_dir(tmp_path, *, with_cost: bool, billed: int = 400):
    d = tmp_path / "run"
    d.mkdir()
    rows = [{"v": 1, "seq": 0, "ts": 1.0, "type": "run_started",
             "data": {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"}}]
    if with_cost:
        rows.append({"v": 1, "seq": 1, "ts": 2.0, "type": "llm_usage",
                     "data": {"usage_id": "u1", "prompt_tokens": billed * 3 // 4,
                              "completion_tokens": billed // 4, "total_tokens": billed}})
    with (d / "events.jsonl").open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    with (d / "spans.jsonl").open("w") as f:
        f.write(json.dumps({
            "name": "generation", "kind": "generation", "trace_id": "a" * 32,
            "span_id": "b" * 16, "run_id": "r",
            "attributes": {"op": "chat", "model": "m", "phase": "propose",
                           "usage": {"prompt": 300, "completion": 100, "total": 400}}}) + "\n")
    return d


def _tokens(run_dir) -> str:
    result = CliRunner().invoke(app, ["tokens", str(run_dir)])
    assert result.exit_code == 0, result.output
    return result.output


def test_a_log_with_NO_cost_row_reports_no_denominator(tmp_path):
    """Mutation: restore `int((state.llm_cost or {}).get(...) or 0)` and this prints a ledger of 0
    and a residual of -400 — a run reported as billed nothing whose spans "over-attribute"."""
    out = _tokens(_run_dir(tmp_path, with_cost=False))
    assert "no denominator" in out, (
        "a log that records no spend has no denominator to reconcile against; saying 0 asserts the "
        "run was billed nothing, which is a different and unsupported claim")
    assert "llm_usage or llm_cost row" in out, (
        "and it must name WHY there is none — mutation: reuse the 'no readable events.jsonl' "
        "wording and the message is false about a log the command just folded")
    assert "residual" not in out, (
        "there is nothing to subtract from, so no residual may be printed at all — this is the "
        "-400 'spans over-attribute' line that sent a reader hunting for double-counting")
    assert "ledger     :              0" not in out


def test_a_log_WITH_a_cost_row_still_reconciles(tmp_path):
    """The regression guard: the fix must not turn a real ledger into 'n/a'.

    Mutation: return None whenever `total_tokens` is falsy and a genuinely zero-token run (a fold
    that recorded a cost row of 0) stops reconciling, which is a real state and not an absence."""
    out = _tokens(_run_dir(tmp_path, with_cost=True))
    assert "the durable record" in out and "no denominator" not in out
    assert "residual" in out, "with a denominator the reconciliation must still be printed"


def test_a_cost_row_that_says_ZERO_is_a_denominator_not_an_absence(tmp_path):
    """The distinction the fix turns on, and the case the first cut of this file only PROMISED.

    Its regression test billed 400 tokens, so `int(...) or None` — collapsing a real zero-token
    ledger back onto "absent" — SURVIVED the mutation run while the docstring above claimed to guard
    exactly that. A row saying the run was billed 0 is a measurement; no row at all is a missing
    measurement, and only the second has no denominator.

    Mutation: `int(state.llm_cost.get("total_tokens") or 0) or None`, which reads identically to the
    fix at a glance."""
    out = _tokens(_run_dir(tmp_path, with_cost=True, billed=0))
    assert "no denominator" not in out, (
        "an llm_usage row of 0 tokens SAYS the run was billed nothing — that is a fact the log "
        "carries, not the absence of one")
    assert "the durable record" in out
    assert "residual" in out, (
        "and the reconciliation is real: 400 attributed against a ledger of 0 is a genuine "
        "over-attribution the operator should see")


def test_a_run_dir_with_NO_events_file_names_that_reason_instead(tmp_path):
    """The two absences are different facts with different remedies, and the message must not
    conflate them. Mutation: drop `ledger_absent` and print one fixed sentence for both."""
    d = tmp_path / "run"
    d.mkdir()
    with (d / "spans.jsonl").open("w") as f:
        f.write(json.dumps({
            "name": "generation", "kind": "generation", "trace_id": "a" * 32,
            "span_id": "b" * 16, "run_id": "r",
            "attributes": {"op": "chat", "model": "m", "phase": "propose",
                           "usage": {"prompt": 300, "completion": 100, "total": 400}}}) + "\n")
    out = _tokens(d)
    assert "no readable events.jsonl" in out
    assert "llm_usage or llm_cost row" not in out
