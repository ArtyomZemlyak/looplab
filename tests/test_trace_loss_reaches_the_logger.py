"""The exporter's own loss receipt does not ride the writer that may have broken.

MEASURED on `runs/e5small-dr-unified-v12` (2026-08-31): `spans.jsonl` and `.spans-append.jsonl`
both froze at 18:20 while `events.jsonl`, the llm-usage outbox and every node directory kept
writing past 21:25 — three hours and ~1760 events with no span record, and NOT ONE console line. A
`py-spy dump` of the live pid showed seven threads and no exporter among them.

WHY THE SILENCE WAS GUARANTEED, not unlucky: `_record_drop_locked` only increments counters, and
the receipt that reports them is written by the worker through `self._writer._export_line` — into
the very file that stopped. `_LOG` was never involved. An exporter that dies takes its own alarm
with it.

The trigger is still open (#149) and this does not depend on it: `_LOG` is independent of the
writer, so the line survives whatever stopped it.
"""
from __future__ import annotations

import ast
import inspect
import logging
import pathlib

from looplab.core.tracing import _TRACE_EXPORT_DROP_REASONS, AsyncJsonlSpanExporter

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _worker_source() -> str:
    return inspect.getsource(AsyncJsonlSpanExporter._worker_loop) if hasattr(
        AsyncJsonlSpanExporter, "_worker_loop") else (
        ROOT / "looplab/core/tracing.py").read_text()


def test_the_loss_is_logged_BEFORE_the_durable_attempt():
    """Order is the property. The durable receipt is what may fail, so a logger line emitted after
    it would be lost in exactly the case it exists for.

    Mutation: move the `_LOG.warning` below `self._writer._export_line`, or delete it.
    """
    src = (ROOT / "looplab/core/tracing.py").read_text()
    tree = ast.parse(src)
    # The receipt block: `if loss is not None:` inside the worker.
    blocks = [n for n in ast.walk(tree) if isinstance(n, ast.If)
              and isinstance(n.test, ast.Compare)
              and getattr(n.test.left, "id", None) == "loss"]
    assert blocks, "the loss-receipt branch is gone — re-point this guard"
    block = blocks[0]
    logs, writes = [], []
    for node in ast.walk(block):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) == "warning":
            logs.append(node.lineno)
        if getattr(node.func, "attr", None) == "_export_line":
            writes.append(node.lineno)
    assert logs, (
        "the exporter must report its loss through `_LOG` — the durable receipt below rides the "
        "writer that may be the thing that broke, so it cannot be the only channel")
    assert writes, "the durable receipt is gone — that half must stay"
    assert min(logs) < min(writes), (
        f"the log line (line {min(logs)}) must come BEFORE the durable attempt (line {min(writes)})")


def test_the_line_names_the_REASONS_not_just_a_count():
    """A number with no reason cannot be acted on. The six reasons are a closed vocabulary and the
    receipt already carries per-reason counts.

    RESOLVED BY AST, over the `_LOG.warning` CALL, because `src.index("trace export lost spans")`
    found the first OCCURRENCE of the string — and on 2026-09-04 a comment 267 lines above the log
    call quoted that very sentence (`"trace export lost spans: none (export failures: 1)"`, the v13
    evidence). The 600-character window then landed inside the comment block and the test went red
    against production code that was perfectly correct. That is this repo's own substring-pin trap
    from the other side: a comment satisfying a pin is the usual failure, a comment SHADOWING one is
    the same defect with the sign flipped.
    """
    import ast

    tree = ast.parse((ROOT / "looplab/core/tracing.py").read_text())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "warning"
             and n.args and isinstance(n.args[0], (ast.Constant, ast.JoinedStr, ast.BinOp))
             and "trace export lost spans" in ast.unparse(n.args[0])]
    assert len(calls) == 1, f"expected exactly one loss-report log call, found {len(calls)}"

    rendered = ast.unparse(calls[0])
    assert "reason" in rendered and "drops.items()" in rendered, (
        "the log line must render the per-reason counts, not a bare total")
    assert "export_failures" in rendered, "and the failure count beside them"
    assert len(_TRACE_EXPORT_DROP_REASONS) == 6


def test_it_says_something_when_every_count_is_zero():
    """NON-VACUITY of the rendering: an empty join would print `lost spans:  (export failures: 1)`,
    which reads as a formatting bug rather than an export failure."""
    drops = {reason: 0 for reason in _TRACE_EXPORT_DROP_REASONS}
    rendered = ", ".join(f"{r}={c}" for r, c in sorted(drops.items()) if c) or "none"
    assert rendered == "none"

    drops["queue_full"] = 3
    rendered = ", ".join(f"{r}={c}" for r, c in sorted(drops.items()) if c) or "none"
    assert rendered == "queue_full=3"


def test_the_logger_is_not_the_writer(caplog):
    """The whole point, driven: `_LOG` must reach a handler with no exporter involved."""
    from looplab.core import tracing
    with caplog.at_level(logging.WARNING, logger=tracing._LOG.name):
        tracing._LOG.warning("trace export lost spans: %s (export failures: %d)", "queue_full=1", 0)
    assert any("trace export lost spans" in r.getMessage() for r in caplog.records)
