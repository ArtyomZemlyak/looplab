"""A traced generation sanitizes each value it persists ONCE, and loses nothing to a second pass.

Review 2026-09-22 (CORE-02). `tracing.generation` sanitized the replayed conversation with
`_trace_messages` and then handed the result to `SpanHandle.set`, which sanitized it AGAIN through
the generic tree walker; `ObservationHandle.output` / `.thinking` / `.tool_calls` did the same. The
comment beside `set` said the second pass "costs nothing". It cost half of every generation's
redaction work, and it was not idempotent: the walker charges the dict KEYS to the same 64 000-char
budget and spends it OLDEST-first, so a conversation near the budget lost its NEWEST message on disk
— the latest tool result or question, the one message `_trace_messages` spends newest-first to keep.

These drive the property through a real tracer and read the bytes back off disk; the security half
(every secret still masked, in the file and in the OTLP mirror) is driven beside it, because a
trusted setter is only acceptable if nothing untrusted can reach it.
"""
from __future__ import annotations

import re
from contextlib import contextmanager

import orjson
import pytest

from looplab.core import redact, tracing
from looplab.core.tracing import JsonlSpanExporter, Tracer

SHAPED = "sk-proj-A1b2C3d4E5f6G7h8I9j0K1l2"            # a known credential SHAPE
ENTROPY = "aZ9k2Lp7qW3xYt5Rb8Nc1Vd6Mf0Gh4J"             # masked only by the entropy pass
SHAPELESS = "hunter2hunter2ZZqq"                         # masked only by the env identity screen


@pytest.fixture(autouse=True)
def _operator_secret(monkeypatch):
    monkeypatch.setenv("LOOPLAB_LLM_API_KEY", SHAPELESS)


def _rows(path):
    return [orjson.loads(line) for line in path.read_bytes().splitlines()]


def _generation(path, op="chat"):
    return next(row["attributes"] for row in _rows(path)
                if row["kind"] == "generation" and row["attributes"].get("op") == op)


def test_a_near_budget_conversation_keeps_its_newest_message_on_disk(tmp_path):
    """The reproduction: 40 tool results of 3 KB, then the question. `_trace_messages` keeps the
    question (it spends newest-first); the second walk used to drop it (it spends oldest-first)."""
    path = tmp_path / "spans.jsonl"
    messages = [{"role": "tool", "content": f"old-{i}-" + "x" * 3000} for i in range(40)]
    messages.append({"role": "user", "content": "NEWEST-QUESTION: what failed?"})
    with Tracer(JsonlSpanExporter(path), run_id="r", capture_llm_io=True).span(
            "root", new_trace=True):
        with tracing.generation(op="chat", model="m", messages=messages):
            pass
    stored = _generation(path)["input"]
    assert stored == tracing._trace_messages(messages), (
        "the persisted input is not what the conversation's sanitizer produced")
    assert stored[-1] == {"role": "user", "content": "NEWEST-QUESTION: what failed?"}, (
        "the newest message was dropped by a second sanitization pass over the stored input")


def test_the_last_tool_call_of_a_near_budget_batch_keeps_its_arguments(tmp_path):
    """Sized so the batch FITS the 64 000-char budget (63 784 chars) but not with the 13 key chars
    per call the second walk charged on top (64 200) — the last call was the one it dropped."""
    path = tmp_path / "spans.jsonl"
    calls = [{"name": f"read_{i}", "arguments": "x" * 2_050} for i in range(31)]
    calls.append({"name": "emit", "arguments": '{"answer": "LAST-CALL"}'})
    assert sum(len(c["name"]) + len(c["arguments"]) for c in calls) < tracing._TRACE_TEXT_CAP
    with Tracer(JsonlSpanExporter(path), run_id="r", capture_llm_io=True).span(
            "root", new_trace=True):
        with tracing.generation(op="chat", model="m") as gen:
            gen.tool_calls(calls)
    stored = _generation(path)["tool_calls"]
    assert stored[-1] == {"name": "emit", "arguments": '{"answer": "LAST-CALL"}'}


def test_each_persisted_value_goes_through_the_redactor_exactly_once(tmp_path, monkeypatch):
    """Counted at the redactor's own funnel (`redact._redact_persisted`), by a marker per value."""
    seen: dict[str, int] = {}
    real = redact._redact_persisted
    marker = re.compile(r"VALMARK(\d+)X")

    def counting(value, **kw):
        if isinstance(value, str):
            for found in marker.findall(value):
                seen[found] = seen.get(found, 0) + 1
        return real(value, **kw)

    monkeypatch.setattr(redact, "_redact_persisted", counting)
    path = tmp_path / "spans.jsonl"
    messages = [{"role": "system", "content": "VALMARK1X system"},
                {"role": "user", "content": "VALMARK2X task"}]
    with Tracer(JsonlSpanExporter(path), run_id="r", capture_llm_io=True).span(
            "root", new_trace=True):
        with tracing.generation(op="chat", model="m", messages=messages) as gen:
            gen.output("VALMARK3X answer").thinking("VALMARK4X reasoning")
            gen.tool_calls([{"name": "read", "arguments": "VALMARK5X args"}])
    assert seen == {"1": 1, "2": 1, "3": 1, "4": 1, "5": 1}, (
        f"redactions per value {seen} — a value the tracer had already sanitized was sanitized "
        "again on its way into the span")


def test_every_secret_class_is_still_masked_on_disk(tmp_path):
    path = tmp_path / "spans.jsonl"
    payload = f"shape {SHAPED} entropy {ENTROPY} env {SHAPELESS} ctl \x1b[2J"
    with Tracer(JsonlSpanExporter(path), run_id="r", capture_llm_io=True).span(
            "root", new_trace=True):
        with tracing.generation(op="chat", model="m",
                                messages=[{"role": "tool", "content": payload}]) as gen:
            gen.output(payload).thinking(payload)
            gen.tool_calls([{"name": "fetch", "arguments": payload}])
    raw = path.read_bytes()
    for secret in (SHAPED, ENTROPY, SHAPELESS, "\x1b"):
        assert secret.encode() not in raw, f"{secret!r} reached spans.jsonl"
    attrs = _generation(path)
    assert "REDACTED_ENV" in attrs["output"] and "sk-***" in attrs["output"]


class _FakeOtelSpan:
    def __init__(self):
        self.attributes: dict = {}

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def add_event(self, *_a, **_kw):
        pass

    def set_status(self, *_a, **_kw):
        pass

    def get_span_context(self):
        return type("Ctx", (), {"is_valid": False, "trace_id": 0, "span_id": 0})()


class _FakeOtelTracer:
    def __init__(self):
        self.spans: list = []

    @contextmanager
    def start_as_current_span(self, name, context=None):
        span = _FakeOtelSpan()
        self.spans.append((name, span))
        yield span


def test_the_otlp_mirror_receives_the_sanitized_value_and_nothing_else(tmp_path, monkeypatch):
    """The trusted setter keeps `set`'s mirror: the collector sees exactly the durable value."""
    fake = _FakeOtelTracer()
    monkeypatch.setattr(tracing, "_OTEL", fake, raising=False)
    tracer = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"), run_id="r", capture_llm_io=True)
    tracer._otel = fake
    payload = f"shape {SHAPED} entropy {ENTROPY} env {SHAPELESS}"
    with tracer.span("root", new_trace=True):
        with tracing.generation(op="chat", model="m",
                                messages=[{"role": "user", "content": payload}]) as gen:
            gen.output(payload)
    mirrored = next(span for name, span in fake.spans if name == "generation").attributes
    for key in ("input", "output", "input_carry", "input_from"):
        assert key in mirrored, f"the OTLP mirror lost `{key}`"
    text = repr(mirrored)
    for secret in (SHAPED, ENTROPY, SHAPELESS):
        assert secret not in text, f"{secret!r} reached the OTLP collector"
    assert mirrored["output"] == _generation(tmp_path / "spans.jsonl")["output"]
