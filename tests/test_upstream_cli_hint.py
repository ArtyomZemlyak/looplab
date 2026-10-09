"""The upstream notice for an EXTERNAL CLI Developer (doc 73 §4.3, `upstream_hint_external`).

An in-house Developer hears a live advance at its tool-loop turn boundary; an external agent's loop is
its own process. For `claude` the notice rides the agent's OWN turn boundary — a PostToolUse hook
loaded for that invocation with `--settings` — and is recorded only once the hook emitted it. Driven
here through a stand-in `claude` that does what the real CLI documents: it loads `--settings`, and
after each "tool call" runs the PostToolUse hook command through a shell, adding the hook's
`additionalContext` to what it knows. Every other preset (and `claude` too) is told on its NEXT call.
"""
from __future__ import annotations

import json
import sys
import threading

import pytest

from looplab.agents.cli_agent import CliAgentDeveloper, PRESETS
from looplab.agents.cli_hook import HookNotices, external_hint_setting
from looplab.agents.tool_loop import notice_channel
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings
from looplab.core.models import Idea
from looplab.engine.upstream_hints import MAX_HINTS_PER_SESSION, UpstreamHintBoard, developer_session

# The stand-in `claude`: after each of up to 60 "tool calls" (50 ms apart) it runs every PostToolUse
# hook its `--settings` declares, through a shell, with a JSON event on stdin — as the CLI does — and
# keeps the `additionalContext` it got. It stops once it heard `want` notices (argv[1]) and writes what
# it saw into solution.py, which the Developer returns as its code.
_FAKE_CLAUDE = r'''
import json, subprocess, sys, time
want = int(sys.argv[1]); args = sys.argv[2:]
hooks = []
if "--settings" in args:
    settings = json.load(open(args[args.index("--settings") + 1]))
    hooks = [h["command"] for m in settings["hooks"]["PostToolUse"] for h in m["hooks"]]
heard = []
for _ in range(60):
    time.sleep(0.05)
    for command in hooks:
        out = subprocess.run(command, shell=True, input=json.dumps({"hook_event_name": "PostToolUse",
                             "tool_name": "Write"}), capture_output=True, text=True).stdout
        if out.strip():
            heard.append(json.loads(out)["hookSpecificOutput"]["additionalContext"])
    if len(heard) >= want:
        break
open("solution.py", "w").write(json.dumps({"argv": args, "heard": heard}))
'''


def _agent(tmp_path, *, want=1, preset="claude", upstream_note=True, upstream_board=True,
           checkout_base=None):
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE, encoding="utf8")
    dev = CliAgentDeveloper(model="m", spec=PRESETS[preset], timeout=30,
                            cmd_override=[sys.executable, str(script), str(want)],
                            upstream_note=upstream_note, upstream_board=upstream_board,
                            checkout_base=checkout_base)
    dev.CANCEL_POLL_S = 0.05
    return dev


def _run(dev):
    return json.loads(dev.implement(Idea(operator="draft", rationale="r")))


def _post_soon(board, hints, delay=0.2):
    def _post():
        for hint in hints:
            board.post(hint)
    timer = threading.Timer(delay, _post)
    timer.start()
    return timer


def test_the_switch_has_one_reader_is_on_and_resumes_off():
    assert Settings().upstream_hint_external is True
    assert external_hint_setting(Settings(upstream_hint_external=False)) is False
    assert external_hint_setting(object()) is False
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["upstream_hint_external"] is False


def test_a_claude_session_hears_a_live_notice_through_its_own_hook_once(tmp_path):
    rows = []
    board = UpstreamHintBoard(sink=rows.append)
    dev = _agent(tmp_path)
    with board.session("implement_from", None, external=True):
        timer = _post_soon(board, [{"hint_id": "hint-up_a", "text": "Upstream notice (engine): fixed."}])
        seen = _run(dev)
        timer.join()
    assert seen["heard"] == ["Upstream notice (engine): fixed."], seen
    assert "--settings" in seen["argv"] and seen["argv"].index("--settings") < len(seen["argv"]) - 1
    assert rows == [{"hint_id": "hint-up_a", "session": "implement_from#1", "channel": "cli_hook"}], (
        "recorded once, after the hook emitted it")


def test_the_cap_holds_and_an_unheard_notice_is_never_recorded(tmp_path):
    rows = []
    board = UpstreamHintBoard(sink=rows.append)
    dev = _agent(tmp_path, want=MAX_HINTS_PER_SESSION + 1)
    hints = [{"hint_id": f"hint-up_{i}", "text": f"notice {i}"} for i in range(MAX_HINTS_PER_SESSION + 1)]
    with board.session("repair_from", 4, external=True):
        timer = _post_soon(board, hints)
        seen = _run(dev)
        timer.join()
    heard = "\n\n".join(seen["heard"])
    assert [f"notice {i}" in heard for i in range(len(hints))] == [True] * MAX_HINTS_PER_SESSION + [False]
    assert [r["hint_id"] for r in rows] == [h["hint_id"] for h in hints[:MAX_HINTS_PER_SESSION]]
    assert all(r["node_id"] == 4 and r["channel"] == "cli_hook" for r in rows)
    # A channel whose hook never ran (hooks disabled) records nothing: offered is not delivered.
    quiet_rows = []
    quiet = UpstreamHintBoard(sink=quiet_rows.append)
    with quiet.session("implement_from", None, external=True):
        quiet.post({"hint_id": "hint-up_q", "text": "q"})
        hook = HookNotices.open(notice_channel(), "claude")
        hook.pump()
        hook.close()
    assert quiet_rows == []


def test_no_channel_no_switch_or_no_hook_keeps_the_historical_argv(tmp_path):
    board = UpstreamHintBoard(sink=lambda row: None)
    plain = _run(_agent(tmp_path, want=0))
    assert "--settings" not in plain["argv"], "no session channel"
    with board.session("implement_from", None, external=False):
        assert notice_channel() is None
        assert "--settings" not in _run(_agent(tmp_path, want=0))["argv"], "the engine did not arm it"
    with board.session("implement_from", None, external=True):
        assert notice_channel() is not None
        assert "--settings" not in _run(_agent(tmp_path, want=0, upstream_note=False))["argv"]
        assert "--settings" not in _run(_agent(tmp_path, want=0, preset="codex"))["argv"], (
            "codex has no per-invocation hook LoopLab can own")
    assert notice_channel() is None


def _promotion_events(tmp_path):
    from looplab.events.eventstore import EventStore
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("base_advanced", {"action_id": "a", "proposal_id": "up_" + "1" * 24, "source_node_id": 3,
                                   "flag": {"name": "MOMENTUM", "default": "0.0", "enabled": "0.2"},
                                   "summary": "Momentum through recipe.env", "selector": {}})
    return store.read_all()


@pytest.mark.parametrize("preset", ["claude", "codex", "aider"])
def test_the_next_call_states_the_promotions(tmp_path, preset):
    from looplab.events.replay import fold
    events = _promotion_events(tmp_path)
    dev = _agent(tmp_path, want=0, preset=preset)
    dev.bind_state(fold(events))
    note = dev._upstream_note()
    assert "VERIFIED CAPABILITIES IN THIS RUN'S BASE" in note and "MOMENTUM" in note
    assert "launch checkout" in note
    off = _agent(tmp_path, want=0, preset=preset, upstream_note=False)
    off.bind_state(fold(events))
    assert off._upstream_note() == "", "off: the message keeps its bytes"
    nothing = _agent(tmp_path, want=0, preset=preset)
    nothing.bind_state(fold(events[:1]))
    assert nothing._upstream_note() == "", "a run that promoted nothing renders its historical bytes"
    if preset == "claude":
        seen = _run(dev)
        assert any("VERIFIED CAPABILITIES" in a for a in seen["argv"]), "the message carries it"


def test_the_factory_reads_the_switch_once():
    from looplab.agents.developer_backends import external_cli_developer
    from types import SimpleNamespace

    task = SimpleNamespace(agent_brief=lambda: "b")
    for flag in (True, False):
        settings = Settings(developer_backend="claude", validate_agent=False, upstream_hint_external=flag)
        dev = external_cli_developer(task, settings, SimpleNamespace(brief="b"), param_search=False)
        assert dev.upstream_note is flag
        # review 2026-10-09: the paragraph also answers to the in-house Developer's switch, through
        # its one reader; a task with no repo spec names no checkout base.
        settings = Settings(developer_backend="claude", validate_agent=False, upstream_board_brief=flag)
        dev = external_cli_developer(task, settings, SimpleNamespace(brief="b"), param_search=False)
        assert dev.upstream_board is flag and dev.checkout_base is None


def test_a_session_publishes_the_channel_only_when_the_engine_arms_it():
    board = UpstreamHintBoard(sink=lambda row: None)

    def implement_from(*_):
        return notice_channel()
    with developer_session(board, implement_from, (), external=True):
        assert notice_channel() is not None
    with developer_session(board, implement_from, ()):
        assert notice_channel() is None
    with developer_session(None, implement_from, (), external=True):
        assert notice_channel() is None


def test_the_live_engine_arms_it_only_with_the_lane_and_the_switch(tmp_path):
    from types import SimpleNamespace

    from looplab.engine.upstream_serve import UpstreamServe, external_hint_channel
    assert external_hint_channel(SimpleNamespace()) is False, "no live lane"
    for flag in (True, False):
        engine = SimpleNamespace(_upstream_serve=UpstreamServe())
        engine._upstream_serve.armed = {"mode": "auto", "reason": "", "stamp": {"digest": "d"},
                                        "current": {"digest": "d"},
                                        "settings": Settings(upstream_hint_external=flag)}
        assert external_hint_channel(engine) is flag
    off = SimpleNamespace(_upstream_serve=UpstreamServe())
    off._upstream_serve.armed = {"mode": "off", "reason": "", "stamp": None, "current": None,
                                 "settings": Settings()}
    assert external_hint_channel(off) is False


# --- review 2026-10-09 -------------------------------------------------------------------------


def _advance_events(tmp_path):
    """One promotion whose advance PRODUCED the base with digest `d-new`."""
    from looplab.events.eventstore import EventStore
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("base_advanced", {"action_id": "a", "proposal_id": "up_" + "2" * 24, "source_node_id": 5,
                                   "flag": {"name": "USE_FIX", "default": "0", "enabled": "1"},
                                   "summary": "the loader fix", "selector": {"digest": "d-new"}})
    return store.read_all()


def test_a_repair_is_told_only_the_promotions_in_its_own_checkout(tmp_path):
    """Finding 1: a repair stays on its lifecycle's base, so it is never told about a promotion its
    checkout does not have, nor that its edits are merged onto the current base. A BUILD still is:
    its next lifecycle merges it onto the current base."""
    from looplab.events.replay import fold
    state = fold(_advance_events(tmp_path))
    launch = _agent(tmp_path, want=0, checkout_base={"digest": "d-launch"})
    launch.bind_state(state)
    build = launch._upstream_note()
    assert "USE_FIX" in build and "merges your edits onto the run's current base" in build
    assert launch._upstream_note(repair=True) == "", "the launch checkout carries no promotion"
    unknown = _agent(tmp_path, want=0)
    unknown.bind_state(state)
    assert unknown._upstream_note(repair=True) == "", "no checkout base known: nothing is claimed"
    advanced = _agent(tmp_path, want=0, checkout_base={"digest": "d-new"})
    advanced.bind_state(state)
    note = advanced._upstream_note(repair=True)
    assert "USE_FIX" in note and "a repair is evaluated on the base its experiment started on" in note
    assert "merges your edits" not in note
    # Driven through the real message: the repair's argv carries no promotion, the build's does.
    seen = json.loads(launch.repair(Idea(operator="draft", rationale="r"), "", "boom"))
    assert not any("USE_FIX" in a for a in seen["argv"])
    seen = json.loads(launch.implement(Idea(operator="draft", rationale="r")))
    assert any("USE_FIX" in a for a in seen["argv"])


def test_the_promotions_paragraph_answers_to_upstream_board_brief(tmp_path):
    """Finding 6: gated the way the in-house Developer's same paragraph is."""
    from looplab.agents.cli_hook import external_notice_kwargs
    from looplab.events.replay import fold
    off = _agent(tmp_path, want=0, upstream_board=False)
    off.bind_state(fold(_advance_events(tmp_path)))
    assert off._upstream_note() == "" and off._upstream_note(repair=True) == ""
    assert external_notice_kwargs(Settings(upstream_board_brief=False))["upstream_board"] is False
    assert external_notice_kwargs(Settings())["upstream_board"] is True
    assert external_notice_kwargs(object())["upstream_board"] is False, "absent reads OFF"
    spec = {"effective_seed_base": {"run_dir": "r", "event_seq": 1, "digest": "d"}}
    assert external_notice_kwargs(Settings(), spec)["checkout_base"] == spec["effective_seed_base"]
    assert CliAgentDeveloper(model="m").upstream_board is False, "the constructor default is OFF"


class _Channel:
    def __init__(self, board, sid):
        from looplab.engine.upstream_hints import _ExternalChannel
        self._inner = _ExternalChannel(board, sid)

    def offer(self):
        return self._inner.offer()

    def acknowledge(self, hint_id, *, channel="cli_hook"):
        return self._inner.acknowledge(hint_id, channel=channel)


def _emit(hook, hint_id):
    """What the hook does once it emitted a notice: its exclusive mark under `delivered/`."""
    (hook.root / "delivered" / hint_id).touch()


def test_an_offered_but_unheard_notice_is_offered_again(tmp_path):
    """Finding 2: offering is not delivering. A notice whose hook never ran in one invocation (the
    agent finished before its next tool call) is offered to the next invocation of the same session
    (a validation retry), and is charged against the cap only once the hook emitted it."""
    rows = []
    board = UpstreamHintBoard(sink=rows.append)
    sid = board._open("repair_from", 4)
    for i in range(MAX_HINTS_PER_SESSION + 1):
        board.post({"hint_id": f"h{i}", "text": f"notice {i}"})
    first = HookNotices(_Channel(board, sid), tmp_path / "first")
    first.pump()
    assert [n["hint_id"] for n in first.offered] == ["h0", "h1", "h2"]
    first.close()                                    # the hook never ran
    assert rows == []
    second = HookNotices(_Channel(board, sid), tmp_path / "second")
    second.pump()
    assert [n["hint_id"] for n in second.offered] == ["h0", "h1", "h2"], "re-offered, not spent"
    _emit(second, "h0")
    second.pump()
    assert [r["hint_id"] for r in rows] == ["h0"]
    assert [n["hint_id"] for n in second.offered] == ["h0", "h1", "h2"], "no room for h3 yet"
    for hid in ("h1", "h2"):
        _emit(second, hid)
    second.pump()
    assert [r["hint_id"] for r in rows] == ["h0", "h1", "h2"]
    assert board.offer(sid) == [], "the cap is reached by notices HEARD, and then h3 is never offered"
    second.close()
    assert all(r["node_id"] == 4 and r["channel"] == "cli_hook" for r in rows)


def test_a_failed_notices_write_keeps_the_notice_pending(tmp_path, monkeypatch):
    """Finding 3: a `notices.json` write that fails leaves the notice off disk — so it must not count
    as published, and the next pump publishes it."""
    board = UpstreamHintBoard(sink=lambda row: None)
    sid = board._open("implement_from", None)
    board.post({"hint_id": "h0", "text": "notice 0"})
    hook = HookNotices(_Channel(board, sid), tmp_path / "ch")
    real = HookNotices._write_notices

    def _full_disk(self, notices):
        raise OSError("disk full")
    monkeypatch.setattr(HookNotices, "_write_notices", _full_disk)
    hook.pump()
    assert hook.offered == [] and json.loads((hook.root / "notices.json").read_text()) == []
    monkeypatch.setattr(HookNotices, "_write_notices", real)
    hook.pump()
    assert json.loads((hook.root / "notices.json").read_text()) == [{"hint_id": "h0", "text": "notice 0"}]
    hook.close()


def test_the_hook_command_is_quoted_for_the_shell_that_runs_it(tmp_path, monkeypatch):
    """Finding 4: Claude Code runs a hook's command through a POSIX shell on Windows too (Git Bash),
    so the line is never cmd.exe-quoted: "/" separators and single quotes. Driven twice — the Windows
    spelling through the rendering double, and a real shell run over a root holding a space and `$`."""
    import shlex
    import subprocess

    import looplab.agents.cli_hook as cli_hook
    from tests._windows_emulation import windows_path_rendering

    win_path = windows_path_rendering(monkeypatch, cli_hook)
    root = win_path(tmp_path / "Win Temp" / "notices $HOME")
    assert "\\" in str(root / "hook.py"), "the double renders the Windows spelling"
    real_name = cli_hook.os.name
    monkeypatch.setattr(cli_hook.os, "name", "nt")
    try:
        hook = HookNotices(_Channel(UpstreamHintBoard(), 1), root)
    finally:
        monkeypatch.setattr(cli_hook.os, "name", real_name)
    settings = json.loads((root / "settings.json").read_text())
    command = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert "\\" not in command, command
    assert shlex.split(command) == [win_path(sys.executable).as_posix(), (root / "hook.py").as_posix(),
                                    root.as_posix()]
    hook.close()
    monkeypatch.undo()

    board = UpstreamHintBoard(sink=lambda row: None)
    sid = board._open("implement_from", None)
    board.post({"hint_id": "h0", "text": "heard through the shell"})
    hook = HookNotices(_Channel(board, sid), tmp_path / "Local Temp" / "notices $HOME")
    hook.pump()
    settings = json.loads((hook.root / "settings.json").read_text())
    line = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    out = subprocess.run(line, shell=True, input="{}", capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"] == "heard through the shell"
    hook.close()
