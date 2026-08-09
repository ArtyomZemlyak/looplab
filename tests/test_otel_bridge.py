"""Live OpenTelemetry bridge (ADR-08): with the [otel] extra installed and a real provider,
our spans become genuine recording OTel spans (valid ids, correct nesting) that an exporter
receives. Runs in a SUBPROCESS so installing a global OTel provider can't leak into the rest of
the suite. Skipped when opentelemetry isn't installed (the default offline path)."""
from __future__ import annotations

import subprocess
import sys

import pytest

pytest.importorskip("opentelemetry.sdk")

_SCRIPT = r'''
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

exp = InMemorySpanExporter()
prov = TracerProvider()
prov.add_span_processor(SimpleSpanProcessor(exp))
trace.set_tracer_provider(prov)                      # real provider BEFORE importing LoopLab

import tempfile
from looplab.core.tracing import Tracer, JsonlSpanExporter

with tempfile.TemporaryDirectory() as d:
    t = Tracer(JsonlSpanExporter(d + "/s.jsonl"), run_id="r")
    with t.span("parent", new_trace=True, node_id=1):
        with t.span("child"):
            pass

prov.force_flush()
spans = {s.name: s for s in exp.get_finished_spans()}
assert set(spans) == {"parent", "child"}, spans
p, c = spans["parent"], spans["child"]
assert p.context.trace_id != 0                        # recording provider -> valid ids
assert c.context.trace_id == p.context.trace_id       # same trace
assert c.parent.span_id == p.context.span_id          # child nests under parent
assert p.attributes["looplab.run_id"] == "r" and c.attributes["looplab.run_id"] == "r"
print("BRIDGE_OK")
'''


def test_spans_become_real_recording_otel_spans():
    r = subprocess.run([sys.executable, "-c", _SCRIPT], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert "BRIDGE_OK" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"


def test_real_engine_run_exports_instrumented_spans(tmp_path):
    # Gap: the bridge was only checked with synthetic direct spans. Run the ACTUAL instrumented
    # engine (offline toy task, no LLM) under a real provider and confirm the engine's own
    # spans (create_node/propose/implement/evaluate + their nesting) reach an OTel exporter.
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    toy = (root / "examples" / "toy_task.json").as_posix()
    script = (
        "from opentelemetry import trace\n"
        "from opentelemetry.sdk.trace import TracerProvider\n"
        "from opentelemetry.sdk.trace.export import SimpleSpanProcessor\n"
        "from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter\n"
        "exp = InMemorySpanExporter(); prov = TracerProvider()\n"
        "prov.add_span_processor(SimpleSpanProcessor(exp)); trace.set_tracer_provider(prov)\n"
        "import tempfile, anyio\n"
        "from looplab.adapters.tasks import load_task\n"
        "from looplab.engine.orchestrator import Engine\n"
        "from looplab.search.policy import GreedyTree\n"
        "from looplab.runtime.sandbox import SubprocessSandbox\n"
        f"task = load_task({toy!r})\n"
        "r, d = task.build_roles()\n"
        "with tempfile.TemporaryDirectory() as td:\n"
        "    anyio.run(Engine(td, task=task, researcher=r, developer=d,\n"
        "                     sandbox=SubprocessSandbox(),\n"
        "                     policy=GreedyTree(n_seeds=1, max_nodes=2)).run)\n"
        "prov.force_flush()\n"
        "spans = exp.get_finished_spans()\n"
        "names = {s.name for s in spans}\n"
        "assert {'create_node','propose','implement','evaluate'} <= names, names\n"
        "cn = next(s for s in spans if s.name == 'create_node')\n"
        "pr = next(s for s in spans if s.name == 'propose')\n"
        "assert cn.context.trace_id != 0\n"
        "assert pr.parent.span_id == cn.context.span_id and pr.context.trace_id == cn.context.trace_id\n"
        "import json as _j; print('ENGINE_OTEL_OK', _j.dumps(sorted(names)))\n")
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert "ENGINE_OTEL_OK" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"


def test_exception_sets_otel_span_status_error():
    # An exception inside a bridged span must mark the REAL OTel span ERROR (not just JSONL),
    # so Jaeger/Tempo agree with spans.jsonl.
    script = (
        "from opentelemetry import trace\n"
        "from opentelemetry.sdk.trace import TracerProvider\n"
        "from opentelemetry.sdk.trace.export import SimpleSpanProcessor\n"
        "from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter\n"
        "exp = InMemorySpanExporter(); prov = TracerProvider()\n"
        "prov.add_span_processor(SimpleSpanProcessor(exp)); trace.set_tracer_provider(prov)\n"
        "import tempfile\n"
        "from looplab.core.tracing import Tracer, JsonlSpanExporter\n"
        "from opentelemetry.trace import StatusCode\n"
        "import tempfile as _t\n"
        "with _t.TemporaryDirectory() as d:\n"
        "    tr = Tracer(JsonlSpanExporter(d + '/s.jsonl'), run_id='r')\n"
        "    try:\n"
        "        with tr.span('boom', new_trace=True):\n"
        "            raise ValueError('x')\n"
        "    except ValueError:\n"
        "        pass\n"
        "prov.force_flush()\n"
        "s = exp.get_finished_spans()[0]\n"
        "assert s.status.status_code == StatusCode.ERROR, s.status\n"
        "print('OTEL_ERR_OK')\n")
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert "OTEL_ERR_OK" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"


def test_exception_secrets_are_redacted_in_jsonl_and_otel():
    script = r'''
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
import tempfile

exp = InMemorySpanExporter(); prov = TracerProvider()
prov.add_span_processor(SimpleSpanProcessor(exp)); trace.set_tracer_provider(prov)
from looplab.core.tracing import Tracer, JsonlSpanExporter

secret = "abcdef0123456789ABCDEF0123456789"
UnsafeError = type("Provider Authorization: Bearer " + secret, (RuntimeError,), {})
with tempfile.TemporaryDirectory() as d:
    path = d + "/s.jsonl"
    tr = Tracer(JsonlSpanExporter(path), run_id="r")
    try:
        with tr.span("provider", new_trace=True):
            raise UnsafeError("Authorization: Bearer " + secret)
    except UnsafeError:
        pass
    persisted = open(path, encoding="utf-8").read()
prov.force_flush()
span = exp.get_finished_spans()[0]
events = repr([(event.name, dict(event.attributes)) for event in span.events])
assert span.status.status_code == StatusCode.ERROR
assert secret not in persisted and secret not in events and secret not in span.status.description
print("OTEL_REDACT_OK")
'''
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert "OTEL_REDACT_OK" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"


def test_tool_call_arguments_are_redacted_in_jsonl_and_otel():
    script = r'''
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import tempfile

exp = InMemorySpanExporter(); prov = TracerProvider()
prov.add_span_processor(SimpleSpanProcessor(exp)); trace.set_tracer_provider(prov)
from looplab.core import tracing
from looplab.core.tracing import Tracer, JsonlSpanExporter

secret = "abcdef0123456789ABCDEF0123456789"
tracing.set_llm_capture(True)
with tempfile.TemporaryDirectory() as d:
    path = d + "/s.jsonl"
    tr = Tracer(JsonlSpanExporter(path), run_id="r")
    with tr.span("root", new_trace=True):
        with tracing.generation(op="chat", model="m") as gen:
            gen.tool_calls([{"name": "fetch", "arguments": "Authorization: Bearer " + secret}])
    persisted = open(path, encoding="utf-8").read()
prov.force_flush()
generation = next(span for span in exp.get_finished_spans() if span.name == "generation")
mirrored = str(dict(generation.attributes).get("tool_calls", ""))
assert secret not in persisted and secret not in mirrored
assert "***" in persisted and "***" in mirrored
print("OTEL_TOOL_CALL_REDACT_OK")
'''
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert "OTEL_TOOL_CALL_REDACT_OK" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"


@pytest.mark.parametrize(("config_key", "config_value"), [
    ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318"),
    ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces"),
    ("OTEL_TRACES_EXPORTER", "otlp"),
])
def test_explicit_otlp_env_auto_wires_a_real_provider(config_key, config_value):
    # Explicit OTLP selection + no provider pre-installed -> _init_otel wires a real
    # TracerProvider (so spans would ship to a collector). No collector needed for the check.
    script = (
        "import os\n"
        "for k in ('OTEL_SDK_DISABLED', 'OTEL_EXPORTER_OTLP_ENDPOINT',\n"
        "          'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT', 'OTEL_TRACES_EXPORTER'):\n"
        "    os.environ.pop(k, None)\n"
        f"os.environ[{config_key!r}]={config_value!r}\n"
        "from opentelemetry import trace\n"
        "from opentelemetry.sdk.trace import TracerProvider\n"
        "import looplab.core.tracing as tr\n"
        "assert tr._OTEL is not None\n"
        "assert isinstance(trace.get_tracer_provider(), TracerProvider)\n"
        "print('ENV_OK')\n")
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert "ENV_OK" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"


@pytest.mark.parametrize("exporter", ["none", "console", "zipkin", "console,zipkin"])
def test_non_otlp_exporter_does_not_auto_wire_otlp(exporter):
    script = (
        "import os\n"
        "for k in ('OTEL_SDK_DISABLED', 'OTEL_EXPORTER_OTLP_ENDPOINT',\n"
        "          'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT'):\n"
        "    os.environ.pop(k, None)\n"
        f"os.environ['OTEL_TRACES_EXPORTER']={exporter!r}\n"
        "from opentelemetry import trace\n"
        "from opentelemetry.sdk.trace import TracerProvider\n"
        "import tempfile\n"
        "import looplab.core.tracing as tr\n"
        "assert tr._OTEL is None\n"
        "assert not isinstance(trace.get_tracer_provider(), TracerProvider)\n"
        "with tempfile.TemporaryDirectory() as d:\n"
        "    tracer = tr.Tracer(tr.JsonlSpanExporter(d + '/s.jsonl'))\n"
        "    assert tracer._otel is None\n"
        "print('NON_OTLP_OK')\n")
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert "NON_OTLP_OK" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"


def test_sdk_disabled_blocks_even_a_manually_configured_provider():
    script = r'''
import os
for key in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_TRACES_EXPORTER"):
    os.environ.pop(key, None)
os.environ["OTEL_SDK_DISABLED"] = "YES"

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import tempfile

exp = InMemorySpanExporter(); prov = TracerProvider()
prov.add_span_processor(SimpleSpanProcessor(exp)); trace.set_tracer_provider(prov)
import looplab.core.tracing as tr
assert tr._OTEL is None
with tempfile.TemporaryDirectory() as d:
    tracer = tr.Tracer(tr.JsonlSpanExporter(d + "/s.jsonl"))
    assert tracer._otel is None
    with tracer.span("local-only", new_trace=True):
        pass
prov.force_flush()
assert not exp.get_finished_spans()
print("SDK_DISABLED_OK")
'''
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert "SDK_DISABLED_OK" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"


def test_provider_configured_after_import_is_discovered_before_tracer_construction():
    script = r'''
import os
for key in ("OTEL_SDK_DISABLED", "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_TRACES_EXPORTER"):
    os.environ.pop(key, None)

import looplab.core.tracing as tr
assert tr._OTEL is None

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import tempfile

exp = InMemorySpanExporter(); prov = TracerProvider()
prov.add_span_processor(SimpleSpanProcessor(exp)); trace.set_tracer_provider(prov)
with tempfile.TemporaryDirectory() as d:
    tracer = tr.Tracer(tr.JsonlSpanExporter(d + "/s.jsonl"))
    assert tracer._otel is not None
    with tracer.span("late-provider", new_trace=True):
        pass
prov.force_flush()
assert [span.name for span in exp.get_finished_spans()] == ["late-provider"]
print("LATE_PROVIDER_OK")
'''
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert "LATE_PROVIDER_OK" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"
