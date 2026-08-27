"""The run's own terminal report must reach the trace, and the fence must still close after it.

docs/53 §2c, re-measured over the 30-run corpus before this was written: fifteen `report_generated`
rows carry a `span_id` that appears in NO artifact -- not `spans.jsonl`, not `.spans-append.jsonl`,
not `trace.json` -- and no `looplab.exporter.loss` receipt anywhere names the loss. The split is
clean: all fifteen are the `trigger="finish"` report of a run that ended on the spend ceiling, and
every run that ended otherwise kept its report span.

The item was filed as "a span can vanish between close and flush". It does not. The span never
reached the queue: a ceiling hit escapes `Engine.run`, whose `finally` retired the exporter, and the
CLI's guarded handler THEN opened `tracer.span("report")` on its way to `run_finished`.
`AsyncJsonlSpanExporter.export` refuses a post-shutdown row and records that drop with
`durable=False` -- on purpose, so a terminal exporter can never be resurrected as a receipt writer
behind a trace reset. Right for a straggler from a background thread; wrong for the run's own
terminal report, which is synchronous, on the main thread, and inside the same engine lock.

So the LIFETIME moved and the FENCE did not: `_run_engine_guarded`, which owns the terminal, now
owns the trace too. The second and third tests are the mutation guards for that sentence -- one
fails if the fix is "stop retiring the exporter", the other if the deferral leaks into every engine.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from looplab.adapters.toytask import ToyTask
from looplab.cli.run_cmds import _run_engine_guarded
from looplab.core.llm import BudgetExceeded
from looplab.engine.orchestrator import Engine
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree


class _StubReportWriter:
    """The provider is irrelevant here: the subject is the SPAN around the call, not its content."""

    def generate(self, state, trigger: str = "") -> dict:
        return {"headline": "ceiling", "at_node": len(getattr(state, "nodes", {}) or {})}


def _engine(run_dir: Path, *, report_writer=None) -> Engine:
    task = ToyTask.load(
        Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    researcher, developer = task.build_roles()
    return Engine(run_dir, task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=3, max_nodes=2),
                  max_parallel=1, report_every=3, report_writer=report_writer)


def _ceiling_engine(run_dir: Path) -> Engine:
    """A real Engine whose loop hits the operator's spend ceiling, as the campaign's eleven did."""
    eng = _engine(run_dir, report_writer=_StubReportWriter())

    async def _ceiling():
        raise BudgetExceeded(
            "LLM spend ceiling reached: $1.0007 of the $1.0000 budget")

    eng._run_with_llm_broker = _ceiling
    return eng


def _spans(run_dir: Path) -> list[dict]:
    path = run_dir / "spans.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _events(run_dir: Path) -> list[dict]:
    return [json.loads(line)
            for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_the_finish_report_span_reaches_disk_when_the_ceiling_ends_the_run(tmp_path):
    """The corpus signature, reproduced end to end and then closed.

    Before the fix this run reproduced it exactly: `report_generated` carried a `span_id`,
    `spans.jsonl` held zero rows, and the exporter's own counters read
    `dropped_shutdown=1, loss_receipts=0` -- the span refused and no receipt written.
    """
    run_dir = tmp_path / "run"
    eng = _ceiling_engine(run_dir)

    with pytest.raises(BudgetExceeded):
        _run_engine_guarded(eng)

    reports = [e for e in _events(run_dir) if e.get("type") == "report_generated"]
    assert len(reports) == 1, "the ceiling path must still buy exactly one terminal report"
    assert reports[0].get("data", {}).get("trigger") == "finish"
    span_id = reports[0].get("span_id")
    assert span_id, "the report event names the span it was written inside"

    on_disk = {s.get("span_id"): s for s in _spans(run_dir)}
    assert span_id in on_disk, (
        "the finish report's span is absent from spans.jsonl -- the exact hole docs/53 §2c "
        "measured fifteen times in the campaign corpus")
    assert on_disk[span_id]["name"] == "report"

    metrics = eng.tracer.exporter.metrics()
    # Not a restatement of the assertion above: a row could reach disk while some OTHER span of the
    # same terminal was refused, and a silent refusal is the defect regardless of which row it hits.
    assert metrics["dropped_shutdown"] == 0, metrics
    assert metrics["loss_receipts"] == 0 and metrics["export_failures"] == 0, metrics


def test_the_fence_still_closes_when_the_guarded_owner_is_done(tmp_path):
    """The mutation guard for "just never shut the exporter down".

    The retirement is a real terminal boundary, not a formality: a span that closes after the owner
    has finished must be REFUSED, or a background straggler could append behind a trace reset/clear.
    Deleting the `retire_tracer()` call in `_run_engine_guarded`'s `finally` turns this red while
    leaving the test above green.
    """
    run_dir = tmp_path / "run"
    eng = _ceiling_engine(run_dir)
    with pytest.raises(BudgetExceeded):
        _run_engine_guarded(eng)

    before = len(_spans(run_dir))
    with eng.tracer.span("straggler_after_the_owner_returned"):
        pass
    eng.tracer.force_flush(timeout_millis=2_000)

    assert eng.tracer.exporter.metrics()["shutdown"] is True
    assert len(_spans(run_dir)) == before, (
        "a span closing after the trace owner returned reached the artifact -- the lifecycle fence "
        "is open")
    assert not any(s.get("name") == "straggler_after_the_owner_returned" for s in _spans(run_dir))


def test_an_engine_nobody_deferred_still_retires_its_own_exporter(tmp_path):
    """The mutation guard for "defer by default".

    `Engine.run` is called directly by the server, the TUI and ~40 tests. Those callers do not trace
    afterwards and never call `retire_tracer`, so the deferral must be opt-in: a run driven straight
    through `anyio.run(eng.run)` has to end with a terminal exporter exactly as before.
    """
    import anyio

    run_dir = tmp_path / "run"
    eng = _engine(run_dir)
    assert eng._trace_retirement_deferred is False
    state = anyio.run(eng.run)

    assert state.finished
    assert eng.tracer.exporter.metrics()["shutdown"] is True, (
        "Engine.run left its exporter accepting; the lifecycle lock can now be released with a live "
        "writer behind it")
    assert _spans(run_dir), "the run traced nothing at all -- this assertion would be vacuous"
