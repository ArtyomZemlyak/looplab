from typer.testing import CliRunner

from looplab.cli import app
from looplab.core.models import Idea


class _TextReadyClient:
    def complete_text(self, _messages):
        return "ready"


def _run_smoke(monkeypatch, structured):
    import looplab.cli.export_cmds as export_cmds
    import looplab.core.parse as parse_module

    monkeypatch.setattr(export_cmds, "make_llm_client", lambda _settings: _TextReadyClient())
    if isinstance(structured, Exception):
        def parse_failure(*_args, **_kwargs):
            raise structured
        monkeypatch.setattr(parse_module, "parse_structured", parse_failure)
    else:
        monkeypatch.setattr(parse_module, "parse_structured",
                            lambda *_args, **_kwargs: structured)
    return CliRunner().invoke(app, ["smoke", "--model", "test/model"])


def test_smoke_requires_the_requested_structured_semantics(monkeypatch):
    result = _run_smoke(monkeypatch, Idea(
        operator="try_params", params={"x": 1.0, "y": 2.0}, rationale="smoke"))

    assert result.exit_code == 0
    assert "text OK" in result.output and "structured OK" in result.output


def test_smoke_fails_when_a_valid_tool_call_drops_requested_params(monkeypatch):
    result = _run_smoke(monkeypatch, Idea(operator="multiply", params={}))

    assert result.exit_code == 1
    assert "structured FAILED" in result.output
    assert "structured OK" not in result.output


def test_smoke_fails_when_structured_parsing_falls_back_unsuccessfully(monkeypatch):
    result = _run_smoke(monkeypatch, RuntimeError("tool call unavailable"))

    assert result.exit_code == 1
    assert "structured FAILED: tool call unavailable" in result.output
