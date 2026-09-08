"""Guard: step feedback keeps the TAIL of a command's output and says when it cut."""
from looplab.adapters import repo_developer as rd


class _Result:
    def __init__(self, content):
        self.content = content


class _Tools:
    def __init__(self, content):
        self._c = content

    def execute_result(self, name, args):
        return _Result(self._c)


def _feedback(monkeypatch, content):
    dev = rd.LLMRepoDeveloper.__new__(rd.LLMRepoDeveloper)
    monkeypatch.setattr(dev, "_step_feedback_command_name", lambda: "test", raising=False)
    monkeypatch.setattr(rd, "_STEP_FEEDBACK_CAP", 100, raising=False)
    import looplab.tools.dev_commands as dc
    monkeypatch.setattr(dc, "DevCommandTools", lambda *a, **k: _Tools(content))
    return dev._step_feedback(None)


def test_the_cut_keeps_the_end_and_announces_itself(monkeypatch):
    text = "HEAD-MARKER\n" + ("x" * 500) + "\nFAILED: assertion at the very end"
    out = _feedback(monkeypatch, text)
    assert "FAILED: assertion at the very end" in out, (
        "for a command the END is the failure and the final metric line")
    assert "HEAD-MARKER" not in out
    assert "truncated" in out, "a silent cut reads as a complete output"


def test_an_output_under_the_cap_is_byte_identical(monkeypatch):
    text = "short and complete\n"
    assert _feedback(monkeypatch, text) == text
