"""The identical-result repeat nudge leaves a trace, so its effect can finally be measured.

MEASURED on `runs/e5small-dr-unified-v10` 22 minutes into a fresh run, over its FIRST propose phase
(370 tool spans, 5.5M tokens): **71 repeated `(tool, input)` pairs and 101 repeats whose output was
byte-identical** — 27 % of every tool call in the phase. `repo_read` ran 93 times for 75 distinct
paths, `read_sibling_experiment` 52 for 32.

`agents/tool_loop.py::_run_tool_call` has a nudge for exactly that: an identical-result streak of 3
appends `_REPEAT_NOTE` to the tool message. The nudge is not the defect. **The defect was that its
firing was invisible**: `_tool_obs.output()` recorded the result BEFORE the note existed, and the
note went only into the message handed to the model — so no span, event or export carried it. Nobody
could count its firings on any run, nobody could answer whether a nudged model stops repeating, and
`streak >= 3` was an unvalidated constant.

READ THE SPANS BEFORE THIS CHANGE AND THE NUDGE LOOKS DEAD. It is not; the note is appended one
statement after the span records its output. Anyone re-deriving this must not file "the note never
fires" — the artifact is what this stamp removes.

Every assertion below has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

from looplab.agents import tool_loop


class _Spy:
    """Records what the tool span was told, standing in for the tracer."""

    def __init__(self):
        self.attrs = {}
        self.outputs = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def output(self, value):
        self.outputs.append(value)

    def set(self, key, value):
        self.attrs[key] = value


class _FixedTools:
    """A provider whose result for a given name is fixed, so repeats are byte-identical."""

    def __init__(self, payload="the same bytes every time"):
        self.payload = payload
        self.calls = 0

    def execute(self, name, args):
        self.calls += 1
        return self.payload


def _drive(monkeypatch, calls, tools=None, args=None):
    """Run N identical calls through the real `_run_tool_call`, returning (spans, messages)."""
    spans = []

    def _fake_tool(name, preview):
        spy = _Spy()
        spans.append(spy)
        return spy

    monkeypatch.setattr(tool_loop.tracing, "tool", _fake_tool)
    ledger: dict = {}
    messages = []
    provider = tools if tools is not None else _FixedTools()
    for _ in range(calls):
        result, note = tool_loop._run_tool_call(
            provider, "repo_read", dict(args or {"path": "README.md"}), repeat_state=ledger)
        messages.append(result + note)
    return spans, messages


def test_every_tool_call_carries_its_streak(monkeypatch):
    """Mutation: stamp only when the note fires. The firings then have no DENOMINATOR — "the nudge
    fired 4 times" is unreadable without knowing how many calls could have nudged."""
    spans, _ = _drive(monkeypatch, 3)
    assert [s.attrs.get("repeat_streak") for s in spans] == [1, 2, 3]


def test_the_note_flag_rides_ONLY_when_the_note_really_went(monkeypatch):
    """Mutation: stamp `repeat_note_sent` unconditionally, and every un-nudged call claims a nudge
    nobody sent — the same false-positive shape as a summary on a row that minted a node."""
    spans, messages = _drive(monkeypatch, 4)
    assert [s.attrs.get("repeat_note_sent") for s in spans] == [None, None, True, True]
    assert [("IDENTICAL result" in m) for m in messages] == [False, False, True, True]


def test_a_CHANGED_result_resets_the_streak_and_is_stamped_as_such(monkeypatch):
    """A cursor poll returning a new chunk is not a repeat. Mutation: drop the `result == prev`
    guard and a paginating tool is nudged for making progress — the exact false positive the note's
    own docstring says it was rewritten to avoid."""

    class _Changing:
        def __init__(self):
            self.n = 0

        def execute(self, name, args):
            self.n += 1
            return f"chunk {self.n}"

    spans, messages = _drive(monkeypatch, 4, tools=_Changing())
    assert [s.attrs.get("repeat_streak") for s in spans] == [1, 1, 1, 1]
    assert not any("IDENTICAL result" in m for m in messages)


def test_the_MESSAGE_the_model_receives_is_byte_identical_to_before(monkeypatch):
    """THE LOAD-BEARING ONE. This change is trace-only: the note's text, its threshold and its
    position outside the cap are untouched. Mutation: move the note inside the cap, change the
    threshold, or let the stamp alter `result`, and the agent's own input changes — which would
    make every prompt-contract claim in this repo false about the loop."""
    _, messages = _drive(monkeypatch, 3)
    assert messages[0] == "the same bytes every time"
    assert messages[1] == "the same bytes every time"
    assert messages[2] == (
        "the same bytes every time"
        "\n(note: this exact call has now run 3× this phase with an IDENTICAL result)")


def test_the_streak_is_computed_INSIDE_the_span_so_it_can_be_stamped_at_all(monkeypatch):
    """The structural fact the fix rests on. Mutation: move the block back below the `with`, and
    `_tool_obs` is closed before the streak exists — the stamp silently reaches a dead object or
    raises, and the record goes back to being unable to say whether the nudge fired."""
    spans, _ = _drive(monkeypatch, 1)
    assert len(spans) == 1
    assert "repeat_streak" in spans[0].attrs, (
        "the streak must be stamped on the tool span, not computed after it closes")
    assert spans[0].outputs, "the span must still record its output exactly as before"


def test_distinct_arguments_are_distinct_calls(monkeypatch):
    """Mutation: key the ledger on the tool NAME alone, and reading two different files reads as a
    repeat — which would nudge a Researcher doing exactly the varied scouting it should."""
    spans_a, _ = _drive(monkeypatch, 2, args={"path": "a.py"})
    ledger: dict = {}

    def _fake_tool(name, preview):
        return _Spy()

    monkeypatch.setattr(tool_loop.tracing, "tool", _fake_tool)
    tools = _FixedTools()
    _, first = tool_loop._run_tool_call(tools, "repo_read", {"path": "a.py"}, repeat_state=ledger)
    _, second = tool_loop._run_tool_call(tools, "repo_read", {"path": "b.py"}, repeat_state=ledger)
    assert first == "" and second == ""
    assert len(ledger) == 2, "one ledger entry per (tool, canonical-args), never per tool"
