"""Zero-dependency tests for LoopLab's OpenTelemetry edge configuration contract."""
from __future__ import annotations

import pytest

from looplab.core import tracing
from looplab.core.tracing import (_otel_env_requests_otlp, _otel_sdk_disabled,
                                  JsonlSpanExporter, Tracer)


@pytest.mark.parametrize("value", ["1", "on", "ON", "t", "TRUE", "y", "yes", " Yes "])
def test_otel_sdk_disabled_accepts_truthy_operator_spellings(value):
    assert _otel_sdk_disabled({"OTEL_SDK_DISABLED": value}) is True


@pytest.mark.parametrize("value", ["", "0", "off", "false", "no", "unexpected"])
def test_otel_sdk_disabled_rejects_non_truthy_values(value):
    assert _otel_sdk_disabled({"OTEL_SDK_DISABLED": value}) is False


@pytest.mark.parametrize("key", [
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
])
def test_otel_endpoint_explicitly_requests_otlp(key):
    assert _otel_env_requests_otlp({key: " http://collector:4318 "}) is True


@pytest.mark.parametrize("value", ["otlp", " OTLP ", "console,otlp", "otlp,zipkin"])
def test_explicit_otlp_exporter_requests_auto_wiring(value):
    assert _otel_env_requests_otlp({"OTEL_TRACES_EXPORTER": value}) is True


@pytest.mark.parametrize("value", ["", "none", "console", "zipkin", "console,zipkin"])
def test_other_exporters_do_not_silently_request_otlp(value):
    assert _otel_env_requests_otlp({"OTEL_TRACES_EXPORTER": value}) is False


@pytest.mark.parametrize("value", ["none", "console", "zipkin", "console,zipkin", "none,otlp"])
@pytest.mark.parametrize("endpoint", [
    "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
])
def test_explicit_non_otlp_exporter_overrides_an_inherited_otlp_endpoint(value, endpoint):
    assert _otel_env_requests_otlp({
        "OTEL_TRACES_EXPORTER": value,
        endpoint: "http://collector:4318/v1/traces",
    }) is False


@pytest.mark.parametrize("otel_request", [
    {"OTEL_TRACES_EXPORTER": "otlp"},
    {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
    {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://collector:4318/v1/traces"},
])
def test_sdk_disabled_overrides_every_otlp_request(otel_request):
    assert _otel_env_requests_otlp({**otel_request, "OTEL_SDK_DISABLED": "true"}) is False


def test_tracer_reprobes_provider_after_module_import(monkeypatch, tmp_path):
    sentinel = object()
    monkeypatch.setattr(tracing, "_OTEL", None)
    monkeypatch.setattr(tracing, "_init_otel", lambda: sentinel)

    tracer = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"))

    assert tracer._otel is sentinel
    assert tracing._OTEL is sentinel


def test_tracer_provider_reprobe_is_never_fatal(monkeypatch, tmp_path):
    def broken_probe():
        raise RuntimeError("broken optional SDK")

    monkeypatch.setattr(tracing, "_OTEL", None)
    monkeypatch.setattr(tracing, "_init_otel", broken_probe)

    tracer = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"))

    assert tracer._otel is None


def test_sdk_disabled_blocks_an_already_discovered_provider(monkeypatch, tmp_path):
    sentinel = object()
    monkeypatch.setattr(tracing, "_OTEL", sentinel)
    monkeypatch.setenv("OTEL_SDK_DISABLED", "yes")

    tracer = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"))

    assert tracer._otel is None
