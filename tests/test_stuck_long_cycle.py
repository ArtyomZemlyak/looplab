"""The StuckDetector's LONG-CYCLE rule (doc 69 §3.2, 69.3): a streak of calls in a row, every one of
which re-ran a call+result ALREADY SEEN in this loop, is a cycle of any length.

The run this answers: `minionerec-backbones-v10`'s card-4 plan phase read seven `config/rl_*.yaml`
files round and round for its last 104 calls. Neither short rule could see it (no identical pair in
a row, no strict ping-pong), the repeat and read-loop notes went out 156 times unheeded, and only
`agent_emit_after` stopped it at turn 300. Replayed over that run's spans, "12 in a row" fired in two
phases and nothing healthy; the rule here is that one with two floors that only make it fire LATER
(three distinct pairs; twice each on average), so every firing index below is derived from them.

Offline: a fake client and fake tools, no model.
"""
from __future__ import annotations

import json

import pytest

from looplab.agents.agent import drive_tool_loop, loop_opts_from_settings
from looplab.agents.stuck import StuckDetector
from looplab.core.config import Settings

_SEVEN = [f"config/rl_{i}.yaml" for i in range(7)]


def _lap(detector, paths, tool="read_file", content=lambda p: f"contents of {p}"):
    return [detector.push(tool, {"path": p}, content(p)) for p in paths]


# ----------------------------------------------------------------------------------- the rule
def test_a_seven_file_cycle_ends_on_its_third_lap():
    # Lap 1 is new (seven pairs never seen); laps 2 and 3 are all re-runs. Seven distinct pairs
    # need a streak of max(12, 2 * 7) = 14, i.e. the LAST read of lap 3 — the 21st call.
    d = StuckDetector()
    out = _lap(d, _SEVEN) + _lap(d, _SEVEN) + _lap(d, _SEVEN)
    assert out[:20] == [None] * 20
    assert out[20] is not None
    assert out[20].startswith("re-ran 14 calls in a row (neutral calls aside) whose call AND result "
                              "were each already seen in this loop")
    assert "a cycle over 7 distinct calls" in out[20]
    assert 'read_file({"path": "config/rl_6.yaml"})' in out[20]      # names the call it ended on


def test_a_three_call_cycle_needs_the_threshold_not_the_average():
    # Three distinct pairs: 2 * 3 = 6 < 12, so the THRESHOLD governs — stale call 12, the 15th push.
    d = StuckDetector()
    three = ["a.py", "b.py", "c.py"]
    out = []
    for _ in range(5):
        out += _lap(d, three)
    assert out[:14] == [None] * 14
    assert out[14] is not None and out[14].startswith("re-ran 12 calls in a row")


def test_the_threshold_is_the_callers():
    d = StuckDetector(stale_threshold=7)
    three = ["a.py", "b.py", "c.py"]
    out = []
    for _ in range(4):
        out += _lap(d, three)
    assert out[:9] == [None] * 9 and out[9] is not None and out[9].startswith("re-ran 7 calls")


def test_re_reading_once_what_compaction_dropped_is_not_a_cycle():
    # Twelve files, each re-read ONCE: a streak of 12 over 12 distinct pairs is under the two-each
    # average (24), so a model rebuilding its context after compaction is not stopped. A SECOND
    # full re-read is the cycle, and ends on its last call.
    d = StuckDetector()
    twelve = [f"src/m{i}.py" for i in range(12)]
    first, second, third = _lap(d, twelve), _lap(d, twelve), _lap(d, twelve)
    assert first == [None] * 12 and second == [None] * 12
    assert third[:11] == [None] * 11
    assert third[11] is not None and third[11].startswith("re-ran 24 calls in a row")
    assert "a cycle over 12 distinct calls" in third[11]


def test_one_and_two_cycles_stay_with_their_own_thresholds():
    # The distinct floor: an operator who RAISED `stuck_repeat` / `stuck_alternate` keeps that
    # number — the long-cycle rule never takes a 1- or 2-cycle over at 12.
    d = StuckDetector(repeat_threshold=99, alternate_threshold=99)
    assert [d.push("read_file", {"path": "a.py"}, "same") for _ in range(60)] == [None] * 60
    d = StuckDetector(repeat_threshold=99, alternate_threshold=99)
    assert _lap(d, ["a.py", "b.py"] * 30) == [None] * 60


def test_a_new_call_and_result_ends_the_streak():
    # Seven files round and round, but every tenth call learns something new: the streak never
    # reaches 14, so 200 calls of it never fire. (A re-read scattered through real work.)
    d = StuckDetector()
    out = []
    for i in range(200):
        if i % 10 == 9:
            out.append(d.push("grep", {"pattern": f"p{i}"}, f"hit {i}"))
        else:
            path = _SEVEN[i % 7]
            out.append(d.push("read_file", {"path": path}, f"contents of {path}"))
    assert out == [None] * 200


def test_results_that_change_are_progress():
    d = StuckDetector()
    out = []
    for lap in range(10):
        out += _lap(d, _SEVEN, content=lambda p, lap=lap: f"{p} at lap {lap}")
    assert out == [None] * 70


def test_keyed_on_the_pair_not_the_result():
    # Different arguments answered alike ("no matches") are different calls, not a repeat.
    d = StuckDetector()
    assert [d.push("grep", {"pattern": f"p{i}"}, "no matches") for i in range(100)] == [None] * 100


def test_a_neutral_call_neither_extends_nor_breaks_a_streak():
    # Eleven re-runs, then twenty ALREADY-SEEN plan updates: neutral, so the streak is still 11 —
    # were they counted, the first of them would have been the 12th; were they progress, the
    # streak would be gone. The next re-read is the 12th and fires.
    plans = [{"plan": f"step {k}"} for k in range(3)]
    three = ["a.py", "b.py", "c.py"]
    d = StuckDetector(repeat_threshold=99, alternate_threshold=99, neutral_tools=("update_plan",))
    for plan in plans:                                         # seen once, so later ones repeat
        assert d.push("update_plan", plan, "plan updated") is None
    assert _lap(d, three) == [None] * 3                        # new
    assert _lap(d, (three * 4)[:11]) == [None] * 11            # eleven re-runs
    assert [d.push("update_plan", plans[i % 3], "plan updated") for i in range(20)] == [None] * 20
    last = d.push("read_file", {"path": "c.py"}, "contents of c.py")
    assert last is not None and last.startswith("re-ran 12 calls in a row")
    assert "a cycle over 3 distinct calls" in last             # the plan pairs never joined it


def test_without_neutral_tools_a_plan_update_is_a_new_call():
    # The caller decides what is neutral: with nothing declared, a plan rewritten every lap is new
    # each time and ends every streak — which is why the loop declares `update_plan`.
    d = StuckDetector()
    out = []
    for lap in range(20):
        out += _lap(d, ["a.py", "b.py", "c.py"])
        out.append(d.push("update_plan", {"plan": f"lap {lap}"}, "plan updated"))
    assert out == [None] * 80


def test_zero_is_off_and_remembers_nothing():
    d = StuckDetector(stale_threshold=0)
    out = []
    for _ in range(15):
        out += _lap(d, _SEVEN)
    assert out == [None] * 105
    assert not d._seen and not d._streak                     # off costs no memory at all


def test_a_disabled_detector_is_silent():
    d = StuckDetector(enabled=False)
    out = []
    for _ in range(5):
        out += _lap(d, _SEVEN)
    assert out == [None] * 35


def test_the_ledger_is_charged_on_a_push_a_short_rule_answers():
    # repeat_threshold=2 makes the repeat rule answer the second of two identical pushes. That push
    # is still a re-run and must be CHARGED to the long-cycle streak, or the streak lags one call
    # behind every short-rule firing. Pushes: a b c (new), a a (the second answered by the repeat
    # rule), then b c a b c a b c a b -> the 12th stale call is the 15th push.
    d = StuckDetector(repeat_threshold=2)
    seq = ["a", "b", "c", "a", "a", "b", "c", "a", "b", "c", "a", "b", "c", "a", "b"]
    out = [d.push("read_file", {"path": s}, f"contents of {s}") for s in seq]
    assert out[4] is not None and "repeated the same call+result" in out[4]
    assert [r for i, r in enumerate(out[:14]) if i != 4] == [None] * 13
    assert out[14] is not None and out[14].startswith("re-ran 12 calls in a row")


def test_the_short_rules_win_a_tie():
    # A streak that also completes an identical run: the more specific sentence is the one handed
    # to the model.
    d = StuckDetector(repeat_threshold=4)
    _lap(d, ["a.py", "b.py", "c.py"])
    _lap(d, ["a.py", "b.py", "c.py"] * 3)                       # 9 stale, distinct 3
    out = [d.push("read_file", {"path": "c.py"}, "contents of c.py") for _ in range(3)]
    assert out[:2] == [None, None]
    assert out[2] is not None and out[2].startswith("repeated the same call+result")


def test_an_unencodable_result_is_still_a_pair():
    d = StuckDetector()
    lone = "\ud800 broken surrogate"
    out = []
    for _ in range(6):
        out += _lap(d, ["a.py", "b.py", "c.py"], content=lambda p: p + lone)
    assert out[14] is not None and out[14].startswith("re-ran 12 calls")


# ------------------------------------------------------------------------------------ the loop
_EMIT = {"type": "function", "function": {
    "name": "emit", "description": "final", "parameters": {"type": "object", "properties": {}}}}


class _FileTools:
    """`read_file` returns fixed content per path, so a lap repeats call AND result."""

    def __init__(self):
        self.reads: list[str] = []

    def specs(self):
        return [{"type": "function", "function": {
            "name": "read_file", "description": "",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]

    def execute(self, name, args):
        if name != "read_file":                                # e.g. `update_plan` with no plan tool
            return f"(unknown tool: {name})"
        self.reads.append(args.get("path"))
        return f"contents of {args.get('path')}"


class _CyclingClient:
    """A model walking the same files round and round, one read per turn — optionally rewriting its
    plan between reads. No `complete_tool`: a forced emit cannot happen, so a stuck exit falls to
    `fallback`, which makes the exit itself observable."""

    def __init__(self, paths, *, plan_every_read=False):
        self.paths, self.plan_every_read, self.turn = paths, plan_every_read, 0

    def chat(self, messages, tools, tool_choice="auto"):
        path = self.paths[self.turn % len(self.paths)]
        calls = [{"id": f"r{self.turn}", "function": {
            "name": "read_file", "arguments": json.dumps({"path": path})}}]
        if self.plan_every_read:
            calls.append({"id": f"p{self.turn}", "function": {
                "name": "update_plan",
                "arguments": json.dumps({"plan": f"re-check the configs, pass {self.turn}"})}})
        self.turn += 1
        return {"content": "", "tool_calls": calls}


def _drive(client, tools, **opts):
    cutoffs: list[dict] = []
    messages = [{"role": "user", "content": "go"}]
    out = drive_tool_loop(client, tools, messages, _EMIT, max_turns=60,
                          finalize=lambda a: ("emit", a), fallback=lambda _m: ("fallback", None),
                          on_budget=cutoffs.append, **opts)
    return out, cutoffs, messages


def test_the_loop_ends_a_seven_file_cycle_through_the_stuck_exit():
    tools = _FileTools()
    out, cutoffs, messages = _drive(_CyclingClient(_SEVEN), tools)
    assert out == ("fallback", None)
    assert len(tools.reads) == 21                              # three laps, not sixty turns
    assert [c["kind"] for c in cutoffs] == ["stuck"]
    assert cutoffs[0]["detail"].startswith("re-ran 14 calls in a row")
    nudge = [m["content"] for m in messages if m["role"] == "user"][-1]
    assert nudge.startswith("Stop: you appear to be stuck (re-ran 14 calls in a row")


def test_zero_turns_the_loop_rule_off():
    tools = _FileTools()
    out, cutoffs, _ = _drive(_CyclingClient(_SEVEN), tools, stuck_stale_streak=0)
    assert out == ("fallback", None)
    assert len(tools.reads) == 60                              # ran to the turn ceiling
    assert [c["kind"] for c in cutoffs] == ["turns"]


def test_the_setting_reaches_the_loop():
    # Three files, threshold 7 from Settings: stale call 7 is the 10th read (default 12: the 15th).
    tools = _FileTools()
    opts = loop_opts_from_settings(Settings(agent_stuck_stale_streak=7))
    out, cutoffs, _ = _drive(_CyclingClient(["a.py", "b.py", "c.py"]), tools,
                             **opts.without("summary_client", "self_plan", "context_budget_chars"))
    assert [c["kind"] for c in cutoffs] == ["stuck"] and len(tools.reads) == 10
    tools = _FileTools()
    _drive(_CyclingClient(["a.py", "b.py", "c.py"]), tools)
    assert len(tools.reads) == 15


@pytest.mark.parametrize("self_plan, reads", [(True, 21), (False, 60)])
def test_plan_updates_between_laps_do_not_hide_the_cycle(self_plan, reads):
    # With the plan tool mounted, a model rewriting its checklist after every read is still lapping
    # the same seven files: the loop's `update_plan` is neutral, so it ends on lap three exactly as
    # without the plan. Without self-plan there IS no plan tool — the call is an unknown tool whose
    # varying arguments are new calls each time, and the rule stays out of it (the turn ceiling ends
    # the loop) — unless the CALLER declares it neutral (`stuck_neutral_tools`, below).
    tools = _FileTools()
    out, cutoffs, _ = _drive(_CyclingClient(_SEVEN, plan_every_read=True), tools,
                             self_plan=self_plan)
    assert len(tools.reads) == reads
    assert [c["kind"] for c in cutoffs] == (["stuck"] if self_plan else ["turns"])


# ------------------------------------------------------------ critic 2026-09-29 (a383), driven
def test_a_new_pair_resets_the_streak_s_breadth_too():
    """30 files each re-read once (a streak of breadth 30), a NEW grep ends it, then a 3-file cycle:
    the second streak's breadth is 3, so it fires at its 12th stale call — not at the 60th a breadth
    kept from the first streak would demand. MUTATION: keep the breadth across a new pair."""
    d = StuckDetector()
    files = [f"src/m{i}.py" for i in range(30)]
    assert _lap(d, files) == [None] * 30
    assert _lap(d, files) == [None] * 30
    assert d.push("grep", {"pattern": "fresh"}, "one hit") is None
    out = _lap(d, files[:3] * 4)
    assert out[:11] == [None] * 11
    assert out[11] is not None and out[11].startswith("re-ran 12 calls in a row")


def test_digests_do_not_collide_over_a_long_session():
    """MUTATION: a one-byte digest -> distinct pairs collide and count as re-runs."""
    d = StuckDetector()
    assert all(d.push("grep", {"pattern": f"p{i}"}, f"hit {i}") is None for i in range(3000))


def test_neutral_calls_stay_visible_to_the_short_rules():
    """Neutral means only "not in the long rule's streak": the two short rules still see the call.
    A read / plan-update alternation whose plan changes is no 1- or 2-cycle. MUTATION: drop neutral
    calls from the short rules' window -> four identical reads in a row read as a 1-cycle."""
    d = StuckDetector(neutral_tools=("update_plan",))
    out = []
    for i in range(4):
        out.append(d.push("read_file", {"path": "a.py"}, "contents of a.py"))
        out.append(d.push("update_plan", {"plan": f"v{i}"}, "plan updated"))
    assert out == [None] * 8


def test_a_bare_string_is_one_neutral_tool_name():
    """`frozenset("update_plan")` is a set of characters. MUTATION: take the string as an iterable."""
    d = StuckDetector(neutral_tools="update_plan")
    out = []
    for i in range(30):
        out.append(d.push("read_file", {"path": ("a.py", "b.py", "c.py")[i % 3]},
                          "contents"))
        out.append(d.push("update_plan", {"plan": f"v{i}"}, "plan updated"))
    assert any(out), "the plan updates are neutral, so the three-file cycle is still seen"


def test_each_rule_names_itself():
    """What the loop stamps on `agent_phase_completed` (`stuck_rule`)."""
    repeat = StuckDetector()
    assert [repeat.push("x", {}, "same") for _ in range(4)][-1] and repeat.last_rule == "repeat"
    ping = StuckDetector()
    out = [ping.push("a" if i % 2 else "b", {}, "same") for i in range(8)]
    assert out[-1] and ping.last_rule == "alternate"
    cycle = StuckDetector()
    out = []
    for _ in range(5):
        out += _lap(cycle, ["a.py", "b.py", "c.py"])
    assert out[14] and cycle.last_rule == "stale_cycle"


class _TodoCyclingClient(_CyclingClient):
    """The assistant's shape: a visible checklist (`write_todos`) rewritten after every read."""

    def chat(self, messages, tools, tool_choice="auto"):
        path = self.paths[self.turn % len(self.paths)]
        calls = [{"id": f"r{self.turn}", "function": {
            "name": "read_file", "arguments": json.dumps({"path": path})}},
            {"id": f"t{self.turn}", "function": {
                "name": "write_todos",
                "arguments": json.dumps({"todos": [{"content": f"pass {self.turn}"}]})}}]
        self.turn += 1
        return {"content": "", "tool_calls": calls}


@pytest.mark.parametrize("neutral, reads", [(("write_todos",), 21), ((), 60)])
def test_a_caller_declares_its_own_checklist_neutral(neutral, reads):
    """The assistant runs with the loop's `update_plan` off and its own `write_todos` on; a
    checklist rewritten between laps broke every streak and the rule never fired (critic
    2026-09-29, driven: 160 calls, `turns`). MUTATION: ignore `stuck_neutral_tools`."""
    tools = _FileTools()
    _out, cutoffs, _ = _drive(_TodoCyclingClient(_SEVEN), tools, self_plan=False,
                              stuck_neutral_tools=neutral)
    assert len(tools.reads) == reads
    assert [c["kind"] for c in cutoffs] == (["stuck"] if neutral else ["turns"])


class _PollTools(_FileTools):
    def execute(self, name, args):
        self.reads.append(name)
        return {"read_output": "[t1] running (no new output)",
                "list_background": "t1 running", "run_command": ""}.get(name, "(unknown)")


class _PollingClient:
    """The assistant waiting on a quiet background job: three constant calls, round and round."""

    def __init__(self):
        self.turn = 0

    def chat(self, messages, tools, tool_choice="auto"):
        name, args = [("read_output", {"id": "t1"}), ("list_background", {}),
                      ("run_command", {"cmd": "sleep 30"})][self.turn % 3]
        self.turn += 1
        return {"content": "", "tool_calls": [{"id": f"c{self.turn}", "function": {
            "name": name, "arguments": json.dumps(args)}}]}


def test_the_assistant_s_waiting_tools_are_neutral():
    """Three constant polls of a quiet job were stopped at call 15 (critic 2026-09-29, driven); with
    the assistant's own declaration the long rule stays out of a wait. MUTATION: drop the
    declaration from `serve/assistant.py` -> the source pin below; drop the wiring -> the loop."""
    from looplab.serve.assistant import ASSISTANT_STUCK_NEUTRAL_TOOLS

    tools = _PollTools()
    _out, cutoffs, _ = _drive(_PollingClient(), tools, self_plan=False,
                              stuck_neutral_tools=ASSISTANT_STUCK_NEUTRAL_TOOLS)
    assert len(tools.reads) == 60 and [c["kind"] for c in cutoffs] == ["turns"]
    tools = _PollTools()
    _out, cutoffs, _ = _drive(_PollingClient(), tools, self_plan=False)
    assert len(tools.reads) == 15 and [c["kind"] for c in cutoffs] == ["stuck"]


class _EveryWaitClient:
    """All three declared waiting tools plus two constant commands, round and round — so each
    declared name decides whether the long rule sees three distinct pairs or two."""

    plan = [("read_output", {"id": "t1"}), ("list_background", {}), ("write_todos", {"t": 1}),
            ("run_command", {"cmd": "sleep 30"}), ("run_command", {"cmd": "true"})]

    def __init__(self):
        self.turn = 0

    def chat(self, messages, tools, tool_choice="auto"):
        name, args = self.plan[self.turn % len(self.plan)]
        self.turn += 1
        return {"content": "", "tool_calls": [{"id": f"c{self.turn}", "function": {
            "name": name, "arguments": json.dumps(args)}}]}


def test_every_declared_waiting_tool_is_what_keeps_the_rule_out():
    """MUTATIONS: drop any ONE name from `ASSISTANT_STUCK_NEUTRAL_TOOLS` -> a third distinct pair
    and the wait is cut (critic 2026-09-30: the three-call poll above could not tell)."""
    from looplab.serve.assistant import ASSISTANT_STUCK_NEUTRAL_TOOLS

    tools = _PollTools()
    _out, cutoffs, _ = _drive(_EveryWaitClient(), tools, self_plan=False,
                              stuck_neutral_tools=ASSISTANT_STUCK_NEUTRAL_TOOLS)
    assert len(tools.reads) == 60 and [c["kind"] for c in cutoffs] == ["turns"]
    for dropped in ASSISTANT_STUCK_NEUTRAL_TOOLS:
        tools = _PollTools()
        _out, cutoffs, _ = _drive(
            _EveryWaitClient(), tools, self_plan=False,
            stuck_neutral_tools=tuple(t for t in ASSISTANT_STUCK_NEUTRAL_TOOLS if t != dropped))
        assert [c["kind"] for c in cutoffs] == ["stuck"], dropped


class _RunPollTools(_FileTools):
    def execute(self, name, args):
        self.reads.append(name)
        return {"list_runs": "r1 running", "read_run": "r1: node 3 evaluating",
                "read_logs": "(no new lines)"}.get(name, "(unknown)")


class _RunPollingClient:
    """The assistant polling a QUIET run by reading it round and round."""

    plan = [("list_runs", {}), ("read_run", {"run": "r1"}), ("read_logs", {"run": "r1", "node": 0})]

    def __init__(self):
        self.turn = 0

    def chat(self, messages, tools, tool_choice="auto"):
        name, args = self.plan[self.turn % len(self.plan)]
        self.turn += 1
        return {"content": "", "tool_calls": [{"id": f"c{self.turn}", "function": {
            "name": name, "arguments": json.dumps(args)}}]}


def test_polling_a_quiet_run_is_cut_on_purpose():
    """The run readers are deliberately NOT neutral (critic 2026-09-30, driven: a poll of
    `list_runs` / `read_run` / `read_logs` ends at call 15). Waiting on a run is `watch_run`'s job —
    its own description says to use it instead of polling — and each poll re-sends the whole
    transcript to learn that nothing moved. MUTATION: declare a run reader neutral -> 60 polls."""
    from looplab.serve.assistant import ASSISTANT_STUCK_NEUTRAL_TOOLS

    tools = _RunPollTools()
    _out, cutoffs, _ = _drive(_RunPollingClient(), tools, self_plan=False,
                              stuck_neutral_tools=ASSISTANT_STUCK_NEUTRAL_TOOLS)
    assert len(tools.reads) == 15 and [c["kind"] for c in cutoffs] == ["stuck"]


def test_the_assistant_passes_its_declaration_and_every_name_is_a_tool_it_offers(tmp_path):
    """A declared name that is no tool the assistant offers neutralizes nothing, silently.
    MUTATIONS: stop passing the declaration; rename one of its tools."""
    import ast
    from pathlib import Path

    from looplab.serve.assistant import ASSISTANT_STUCK_NEUTRAL_TOOLS, TodoTools
    from looplab.tools.shell_tools import ShellTools

    tree = ast.parse((Path(__file__).resolve().parents[1] / "looplab" / "serve"
                      / "assistant.py").read_text(encoding="utf-8"))
    passed = [ast.unparse(k.value) for node in ast.walk(tree) if isinstance(node, ast.Call)
              and ast.unparse(node.func) == "drive_tool_loop"
              for k in node.keywords if k.arg == "stuck_neutral_tools"]
    assert passed == ["ASSISTANT_STUCK_NEUTRAL_TOOLS"]
    offered = {spec["function"]["name"]
               for provider in (TodoTools(), ShellTools([tmp_path]))
               for spec in provider.specs()}
    assert set(ASSISTANT_STUCK_NEUTRAL_TOOLS) <= offered, offered


class _BouncingClient:
    """Laps 1-3 of three files, an emit the validator refuses, two re-read laps to fix it, then the
    corrected emit."""

    plan = ([("read_file", {"path": p}) for p in ("manifest.yaml", "train.py", "data.py")] * 3
            + [("emit", {"path": "wrong"})]
            + [("read_file", {"path": p}) for p in ("manifest.yaml", "train.py", "data.py")] * 2
            + [("emit", {"path": "data.py"})])

    def __init__(self):
        self.turn = 0

    def chat(self, messages, tools, tool_choice="auto"):
        name, args = self.plan[min(self.turn, len(self.plan) - 1)]
        self.turn += 1
        return {"content": "", "tool_calls": [{"id": f"c{self.turn}", "function": {
            "name": name, "arguments": json.dumps(args)}}]}


def test_a_refused_emit_ends_the_stale_streak():
    """The validator's refusal is new information; re-reading what it named is the repair (critic
    2026-09-29, driven: at 12 the loop fell to the fallback before the corrected emit). MUTATION:
    do not reset the streak on a bounced emit."""
    emit = {"type": "function", "function": {"name": "emit", "description": "final", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}}}}}
    tools = _FileTools()
    cutoffs: list = []
    out = drive_tool_loop(_BouncingClient(), tools, [{"role": "user", "content": "go"}], emit,
                          max_turns=40, finalize=lambda a: ("emit", a),
                          fallback=lambda _m: ("fallback", None),
                          validate=lambda a: None if a.get("path") == "data.py"
                          else "needs input `data.py` is not declared",
                          on_budget=cutoffs.append, self_plan=False)
    assert out == ("emit", {"path": "data.py"}) and cutoffs == []


def test_the_rule_that_stopped_the_loop_is_stamped_on_its_phase_row():
    """Inside a run every loop reports its completion through the phase sink; the stuck exit now
    names its rule there, observer or not (critic 2026-09-29: only four callers pass `on_budget`).
    MUTATION: drop the stamp -> the row says `fallback` and nothing else."""
    from looplab.core.phase_events import PHASE_COMPLETED, phase_sink_scope

    rows: list = []
    with phase_sink_scope(lambda etype, data: rows.append((etype, data))):
        drive_tool_loop(_CyclingClient(_SEVEN), _FileTools(), [{"role": "user", "content": "go"}],
                        _EMIT, max_turns=60, finalize=lambda a: ("emit", a),
                        fallback=lambda _m: ("fallback", None))
    (completed,) = [data for etype, data in rows if etype == PHASE_COMPLETED]
    assert completed["stuck_rule"] == "stale_cycle"
    assert completed["stuck_detail"].startswith("re-ran 14 calls in a row")
    rows.clear()
    with phase_sink_scope(lambda etype, data: rows.append((etype, data))):
        drive_tool_loop(_CyclingClient(_SEVEN), _FileTools(), [{"role": "user", "content": "go"}],
                        _EMIT, max_turns=60, finalize=lambda a: ("emit", a),
                        fallback=lambda _m: ("fallback", None), stuck_stale_streak=0)
    (completed,) = [data for etype, data in rows if etype == PHASE_COMPLETED]
    assert "stuck_rule" not in completed and "stuck_detail" not in completed


def test_judgebench_forwards_the_detector_s_other_two_thresholds(monkeypatch, tmp_path):
    """A case's `loop` block could name `stuck_stale_streak` and nothing forwarded it (critic
    2026-09-29). MUTATION: drop either keyword from `run_case`."""
    from looplab.agents import tool_loop
    from looplab.judgebench import trajectory as T

    seen: dict = {}

    def _capture(*_args, **kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(tool_loop, "drive_tool_loop", _capture)
    case = dict(T.read_corpus()["cases"][0])
    case["loop"] = dict(case.get("loop") or {}, stuck_stale_streak=0, stuck_alternate=7)
    T.run_case(case, tmp_path / "case")
    assert seen["stuck_stale_streak"] == 0 and seen["stuck_alternate"] == 7


def test_a_refused_emit_forgets_the_streak_s_breadth_too():
    """`reset_stale` ends the streak whole: its LENGTH and the distinct pairs it had re-run. Kept,
    the breadth from before the bounce let two calls re-read afterwards count as a three-pair cycle
    (critic 2026-09-30). MUTATION: keep `_streak` -> the two-call re-read below is cut at 12."""
    lap = [("read_file", {"path": p}, f"<{p}>") for p in ("a", "b", "c")]
    det = StuckDetector(repeat_threshold=4, alternate_threshold=4, stale_threshold=12)
    for name, args, obs in lap * 3:                      # 6 re-seen calls over 3 distinct pairs
        assert det.push(name, args, obs) is None
    det.reset_stale()
    # A A B B … — neither a 4-repeat nor a strict ping-pong, and only TWO distinct pairs.
    pattern = [lap[0], lap[0], lap[1], lap[1]] * 6
    assert all(det.push(name, args, obs) is None for name, args, obs in pattern)


def test_a_bare_string_names_one_neutral_tool_through_the_loop():
    """`drive_tool_loop(stuck_neutral_tools="read_output")` is ONE name, not eleven characters.
    MUTATION: pass `tuple(stuck_neutral_tools)` -> no tool is neutral and the wait is cut."""
    tools = _PollTools()
    _out, cutoffs, _ = _drive(_PollingClient(), tools, self_plan=False,
                              stuck_neutral_tools="read_output")
    assert len(tools.reads) == 60 and [c["kind"] for c in cutoffs] == ["turns"]
