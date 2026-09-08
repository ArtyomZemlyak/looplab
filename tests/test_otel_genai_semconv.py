"""doc 52 `otel-bridge-carries-no-genai-semconv`: the OTLP mirror speaks the GenAI conventions.

The bridge opened every span with LoopLab's own attribute names, so a collector could see the tree
and nothing generic could read the LLM call inside it — no GenAI dashboard, no token panel, no
cross-tool comparison. The conventions are now written BESIDE those names, on the OTLP span only, so
`spans.jsonl` (and `traceview` / `looplab timings` / `looplab tokens`, which read it) is unchanged.

Driven through a FAKE OTel tracer rather than the real SDK: the bridge's job is to hand a provider
the right attributes, and that is exactly what a recording double observes — with no optional
dependency, no exporter and no collector.
"""
from __future__ import annotations

from contextlib import contextmanager

from looplab.core import tracing
from looplab.core.tracing import JsonlSpanExporter, Tracer, genai_semconv


# --------------------------------------------------------------------- the mapping, as a truth table

def test_a_generation_maps_operation_model_params_and_usage():
    out = genai_semconv("generation", {
        "op": "complete_text", "model": "qwen3:8b",
        "model_parameters": {"temperature": 0.2, "max_tokens": 512},
        "usage": {"prompt": 900, "completion": 120, "total": 1020}})
    assert out == {"gen_ai.operation.name": "chat", "gen_ai.request.model": "qwen3:8b",
                   "gen_ai.request.temperature": 0.2, "gen_ai.request.max_tokens": 512,
                   "gen_ai.usage.input_tokens": 900, "gen_ai.usage.output_tokens": 120}


def test_an_embedding_call_gets_the_conventions_own_operation_word():
    assert genai_semconv("generation", {"op": "embed_texts"})["gen_ai.operation.name"] == "embeddings"


def test_the_openai_usage_spelling_maps_the_same_way():
    out = genai_semconv("generation", {"usage": {"prompt_tokens": 5, "completion_tokens": 2}})
    assert out == {"gen_ai.usage.input_tokens": 5, "gen_ai.usage.output_tokens": 2}


def test_a_tool_observation_is_an_executed_tool_and_a_parse_is_not():
    assert genai_semconv("tool", {"tool": "read_file"}) == {
        "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "read_file"}
    # `structured_parse` also opens kind="tool"; calling a parser choice an executed tool would be a
    # lie in the one vocabulary a collector reads without knowing anything about LoopLab.
    assert genai_semconv("tool", {"parser": "baml"}) == {}


def test_nothing_is_guessed_from_absent_or_wrong_typed_facts():
    assert genai_semconv("generation", {}) == {}
    assert genai_semconv("operation", {"op": "implement", "model": "m"}) == {}
    # A wrong `gen_ai.*` value is read by generic tooling as authoritative, so a bad type yields none.
    assert genai_semconv("generation", {"model": 7, "model_parameters": {"temperature": "warm"},
                                        "usage": {"prompt": "many"}}) == {}
    # The provider is never asserted: an OpenAI-COMPATIBLE endpoint may be anything.
    assert not any(k.startswith("gen_ai.provider") or k == "gen_ai.system"
                   for k in genai_semconv("generation", {"op": "chat", "model": "m"}))


# ------------------------------------------------------------------- the bridge, through a double

class _FakeOtelSpan:
    def __init__(self):
        self.attributes: dict = {}
        self.events: list = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def add_event(self, name, fields=None):
        self.events.append((name, dict(fields or {})))

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


def _traced(tmp_path, monkeypatch):
    fake = _FakeOtelTracer()
    monkeypatch.setattr(tracing, "_OTEL", fake, raising=False)
    tracer = Tracer(JsonlSpanExporter(str(tmp_path / "spans.jsonl")), run_id="r")
    tracer._otel = fake
    return tracer, fake


def test_the_mirrored_generation_carries_gen_ai_beside_looplabs_own_names(tmp_path, monkeypatch):
    tracer, fake = _traced(tmp_path, monkeypatch)
    with tracer.span("implement", kind="operation"):
        with tracing.generation(op="complete_text", model="qwen3:8b",
                                model_parameters={"temperature": 0.3}) as gen:
            gen.usage({"prompt_tokens": 1200, "completion_tokens": 44})

    mirrored = dict(next(span for name, span in fake.spans if name == "generation").attributes)
    assert mirrored["gen_ai.operation.name"] == "chat"
    assert mirrored["gen_ai.request.model"] == "qwen3:8b"
    assert mirrored["gen_ai.request.temperature"] == 0.3
    # The LATE key: usage is stamped after the call returns and is the whole of `gen_ai.usage.*`.
    assert mirrored["gen_ai.usage.input_tokens"] == 1200
    assert mirrored["gen_ai.usage.output_tokens"] == 44
    # BESIDE, not instead: every existing consumer reads these.
    assert mirrored["op"] == "complete_text" and mirrored["model"] == "qwen3:8b"


def test_the_durable_row_is_untouched_by_the_conventions(tmp_path, monkeypatch):
    """`spans.jsonl` is what `traceview`, `looplab timings` and `looplab tokens` read; the mirror may
    not add a key to it (nor grow every row of a long run by a second copy of its own facts)."""
    import json

    spans = tmp_path / "spans.jsonl"
    fake = _FakeOtelTracer()
    monkeypatch.setattr(tracing, "_OTEL", fake, raising=False)
    tracer = Tracer(JsonlSpanExporter(str(spans)), run_id="r")
    tracer._otel = fake
    with tracer.span("implement", kind="operation"):
        with tracing.generation(op="complete_text", model="m") as gen:
            gen.usage({"prompt": 3, "completion": 1})
    tracer.force_flush()

    rows = [json.loads(line) for line in spans.read_text().splitlines() if line.strip()]
    assert rows, "no span was exported"
    for row in rows:
        assert not [k for k in row.get("attributes", {}) if k.startswith("gen_ai.")]


def test_a_tool_span_mirrors_the_execute_tool_convention(tmp_path, monkeypatch):
    tracer, fake = _traced(tmp_path, monkeypatch)
    with tracer.span("implement", kind="operation"):
        with tracing.tool("read_file", {"path": "a.py"}):
            pass
    mirrored = dict(next(span for name, span in fake.spans if name == "tool").attributes)
    assert mirrored["gen_ai.operation.name"] == "execute_tool"
    assert mirrored["gen_ai.tool.name"] == "read_file"


def test_a_broken_bridge_provider_cannot_fail_the_span(tmp_path, monkeypatch):
    """Same rule as every other mirrored write: observability may not decide whether the work
    proceeds, so a provider that raises on `set_attribute` costs the span nothing."""
    class _Hostile(_FakeOtelSpan):
        def set_attribute(self, key, value):
            if str(key).startswith("gen_ai."):
                raise RuntimeError("collector said no")
            super().set_attribute(key, value)

    class _HostileTracer(_FakeOtelTracer):
        @contextmanager
        def start_as_current_span(self, name, context=None):
            span = _Hostile()
            self.spans.append((name, span))
            yield span

    fake = _HostileTracer()
    monkeypatch.setattr(tracing, "_OTEL", fake, raising=False)
    tracer = Tracer(JsonlSpanExporter(str(tmp_path / "spans.jsonl")), run_id="r")
    tracer._otel = fake
    with tracer.span("implement", kind="operation"):
        with tracing.generation(op="complete_text", model="m") as gen:
            gen.usage({"prompt": 1, "completion": 1})
    mirrored = dict(next(span for name, span in fake.spans if name == "generation").attributes)
    assert mirrored["model"] == "m"       # our own names still landed
