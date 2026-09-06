"""A9 (docs/60 §60.9): the PATH-keyed read-loop nudge in `agents/tool_loop.py`.

`oldCK8` (2026-09-03, docs/56 §164) spent $0.9574 of its $1.00 inside `propose` and minted no node:
189 `repo_read` calls of ONE 248-line `reference_edge_expansion.py` with `start_line` incrementing,
72-102 chars each. Every net in the loop missed it — the repeat note keys on identical
`(tool, canonical-args)` and the arguments incremented (`repeat_streak` 1 on 192 of 194 reads), the
identical-result note keys on identical results and every line was different, `agent_max_turns` is
0 in every probe. The only constant across the 189 calls was the PATH, which `benchmarks/read_loops.py`
counts and `tests/test_one_file_read_to_death_is_visible.py` pins at the reporting end.

This is the loop end: after `read_loop_nudge_after` (25) reads of one path inside ONE loop, every
further read of it carries `_READ_LOOP_NOTE`. A NUDGE, not a cap — the read still executes. The
threshold is the corpus's normal behaviour (25-38 reference reads per `plan_step`; next-worst after
oldCK8's 186 is 38), so below it nothing changes, which is what the first test pins.

Every test below drives the REAL `drive_tool_loop` with a scripted client and a fake reader whose
pages carry the paginator's own `(lines A-B of T)` header — the exact oldCK8 shape. Each assertion
has a mutation that reddens it, named in its message.
"""
from __future__ import annotations

import ast
import json

import pytest

from looplab.agents import tool_loop
from looplab.agents.agent import LoopOptions, drive_tool_loop
from looplab.agents.loop_options import LOOP_OPTION_FIELDS
from tests._source_scan import iter_trees

_EMIT = {"type": "function", "function": {
    "name": "emit", "description": "final", "parameters": {"type": "object", "properties": {}}}}
_REF = "reference_edge_expansion.py"
_TOTAL_LINES = 248
_LINE = "x" * 78 + "\n"     # ~80 chars per line, so 248 lines is ~19.8k chars = 6 pages of 3600


def _tool_call(name, args):
    return {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _walk(path, n, start=1):
    """`n` one-line reads of `path` with a DIFFERENT `start_line` each — oldCK8's exact shape."""
    return [_tool_call("repo_read", {"path": path, "lines": 1, "start_line": start + i})
            for i in range(n)]


class _Client:
    def __init__(self, scripted):
        self.scripted = list(scripted) + [_tool_call("emit", {"ok": True})]

    def chat(self, messages, tools, tool_choice="auto"):
        return self.scripted.pop(0)


class _Tools:
    """A reader that pages like `RepoScoutTools._paginate`, a writer and a grep of the same path."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def specs(self):
        return [{"type": "function", "function": {
            "name": n, "description": "", "parameters": {"type": "object", "properties": {}}}}
            for n in ("repo_read", "read_file", "write_file", "repo_grep", "read_asset")]

    def execute(self, name, args):
        self.calls.append((name, dict(args)))
        if name in ("repo_read", "read_file"):
            start = int(args.get("start_line") or 1)
            want = int(args.get("lines") or 0)
            if start > 1 or want:
                shown = min(want or _TOTAL_LINES, _TOTAL_LINES - start + 1)
                head = f"(lines {start}-{start + shown - 1} of {_TOTAL_LINES})\n"
                return head + _LINE * shown
            return _LINE * 40 + "\n… (more below — continue with start_line=41)"
        if name == "read_asset":
            return "col_a,col_b\n1,2\n"
        return "ok"


def _drive(scripted, tools=None, spans=None, monkeypatch=None, **kw):
    """Run the real loop; return the tool messages the MODEL received, in order."""
    if monkeypatch is not None:
        class _Spy:
            def __init__(self):
                self.attrs = {}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def output(self, value):
                pass

            def set(self, key, value):
                self.attrs[key] = value

        def _fake_tool(name, preview):
            spy = _Spy()
            spans.append(spy)
            return spy

        monkeypatch.setattr(tool_loop.tracing, "tool", _fake_tool)
    messages = [{"role": "user", "content": "go"}]
    out = drive_tool_loop(_Client(scripted), tools or _Tools(), messages, _EMIT,
                          finalize=lambda a: ("emit", a), fallback=lambda _m: ("fallback", None),
                          **kw)
    assert out == ("emit", {"ok": True}), "the loop must still end on the model's own emit"
    return [m["content"] for m in messages if m.get("role") == "tool"]


# ------------------------------------------------------------------------ the threshold

def test_24_reads_of_one_path_get_no_note_and_the_25th_does(monkeypatch):
    """Mutation: fire at `>` instead of `>=` (the 26th), or key the ledger on the arguments (never
    fires — every call differs), or on the tool name alone (fires on the 25th read of ANYTHING)."""
    spans: list = []
    tools = _Tools()
    got = _drive(_walk(_REF, 26), tools=tools, spans=spans, monkeypatch=monkeypatch)
    assert len(got) == 26 and len(tools.calls) == 26, "a nudge must never suppress the read"
    assert not any("has now been read" in m for m in got[:24]), (
        "below the threshold the tool message is what it always was — the corpus's normal 25-38 "
        "reference re-reads must not be nudged")
    assert "has now been read 25× this phase" in got[24], got[24]
    assert "has now been read 26× this phase" in got[25], (
        "the note fires on EVERY read after the threshold, not once: a one-shot note is one "
        "compaction away from a loop that has already shown it does not stop")
    # The read's own content is untouched and the note rides after it, outside the cap.
    assert got[24].startswith(f"(lines 25-25 of {_TOTAL_LINES})\n")
    assert got[24].endswith("instead of reading it again)")
    # Trace: the denominator on every read, the flag only where the note really went.
    assert [s.attrs.get("path_reads") for s in spans] == list(range(1, 27))
    assert [s.attrs.get("repeat_path_note_sent") for s in spans] == [None] * 24 + [True, True]
    assert all(s.attrs.get("repeat_streak") == 1 for s in spans), (
        "incrementing arguments are distinct calls to the OLD ledger — that is the whole gap")
    assert not any(s.attrs.get("repeat_note_sent") for s in spans)


def test_the_note_names_the_actual_call_the_file_and_its_size(monkeypatch):
    """The remedy is a concrete call, never "read it whole" in the abstract. Mutation: drop the
    tool/slot from the template, or derive the page count from nothing (a number nobody derived)."""
    got = _drive(_walk(_REF, 25))
    note = got[24].split("\n(note:", 1)[1]
    assert f'`repo_read(path="{_REF}")`' in note, note
    assert "`lines` OMITTED" in note and "continue with start_line=N" in note, note
    assert f"It is {_TOTAL_LINES} lines" in note, note
    # 248 lines at ~79 chars = ~19.6k chars = 6 pages of the reader's own 3600-char page.
    expected_pages = -(-(_TOTAL_LINES * len(_LINE)) // tool_loop._READ_PAGE_CHARS)
    assert f"about {expected_pages} such pages in total" in note, note
    assert f"~{tool_loop._READ_PAGE_CHARS}-char page" in note, (
        "the page width the model is told must be the reader's own, not a second constant")


def test_a_file_whose_size_the_reader_never_stated_gets_no_invented_size(monkeypatch):
    """Unwindowed reads carry no `(lines a-b of T)` header, so T is unknown and the note must not
    guess. Mutation: default `lines_total` to anything, or estimate pages from bytes alone."""
    got = _drive([_tool_call("repo_read", {"path": _REF}) for _ in range(25)],
                 stuck_detection=False)
    note = got[24]
    assert "has now been read 25× this phase" in note
    assert "It is " not in note and "such page" not in note, note
    assert tool_loop._read_loop_fit({"reads": 25, "lines_total": None,
                                     "lines_seen": 0, "chars_seen": 0}) == ""


# ------------------------------------------------------------------------ what is NOT a read loop

def test_a_different_path_is_a_separate_counter():
    """Mutation: key on the tool name and 24 + 24 reads of two files fire on the 25th call."""
    reads = []
    for i in range(24):
        reads += _walk("a.py", 1, start=i + 1) + _walk("b.py", 1, start=i + 1)
    got = _drive(reads)
    assert len(got) == 48 and not any("has now been read" in m for m in got), (
        "two files at 24 reads each are two counters, neither at the threshold")


def test_a_write_or_a_grep_of_the_same_path_is_not_a_read():
    """`read_loops.py`'s rule at the loop end. Mutation: count every tool that names a `path`."""
    scripted = [_tool_call("write_file", {"path": _REF, "content": "x"}) for _ in range(30)]
    scripted += [_tool_call("repo_grep", {"pattern": "def", "path": _REF}) for _ in range(30)]
    scripted += _walk(_REF, 1)            # ONE real read on top of 60 non-reads
    got = _drive(scripted, stuck_detection=False)
    assert len(got) == 61 and not any("has now been read" in m for m in got), (
        "writes and greps of the path must not be charged to its read count")


def test_the_counter_resets_per_loop():
    """A file legitimately re-read across phases (the reference in every `plan_step`) must never
    accumulate. Mutation: hoist `read_state` to module scope, or seed it from the previous call."""
    for _ in range(3):
        got = _drive(_walk(_REF, 13))
        assert not any("has now been read" in m for m in got), (
            "13 reads per loop, three loops: a fresh ledger per invocation stays below 25")


def test_a_write_of_the_path_does_not_count_and_the_ledger_ignores_it(monkeypatch):
    """The span stamp is the denominator only for READERS: a write of the same path carries no
    `path_reads` at all. Mutation: stamp `path_reads` on every call."""
    spans: list = []
    scripted = _walk(_REF, 1) + [_tool_call("write_file", {"path": _REF, "content": "x"})]
    _drive(scripted, spans=spans, monkeypatch=monkeypatch)
    assert [s.attrs.get("path_reads") for s in spans] == [1, None]


# ------------------------------------------------------------------------ configuration

def test_the_threshold_rides_the_bundle_and_zero_is_off():
    """`read_loop_nudge_after` is a `LoopOptions` field (config-shaped: a threshold, not a prompt or
    a callback). Mutation: move it to `EXPLICIT_ONLY_LOOP_ARGS` and no bundle can carry it."""
    assert "read_loop_nudge_after" in LOOP_OPTION_FIELDS
    got = _drive(_walk(_REF, 3), **LoopOptions(read_loop_nudge_after=3))
    assert "has now been read 3× this phase" in got[2]
    got = _drive(_walk(_REF, 30), **LoopOptions(read_loop_nudge_after=0))
    assert not any("has now been read" in m for m in got), "0 is the OFF switch"


def test_the_loops_own_default_is_the_corpus_ceiling():
    """25 = the corpus's normal per-phase reference re-read count; the loop's signature is the one
    source of truth for it (an UNSET bundle field is simply not passed)."""
    import inspect
    assert inspect.signature(drive_tool_loop).parameters["read_loop_nudge_after"].default == 25
    assert "read_loop_nudge_after" not in LoopOptions()


# ------------------------------------------------------------------------ the old note is untouched

def test_the_identical_result_repeat_note_is_byte_identical_and_precedes_the_nudge():
    """THE LOAD-BEARING ONE for the existing contract: `_REPEAT_NOTE`'s text, threshold and
    position are unchanged, and when both fire the old note comes FIRST. Mutation: fold the two
    notes into one template, or append the path note before the repeat note."""
    same = [_tool_call("repo_read", {"path": _REF, "lines": 1, "start_line": 7})] * 25
    got = _drive(same, stuck_detection=False)
    page = f"(lines 7-7 of {_TOTAL_LINES})\n" + _LINE
    assert got[0] == page and got[1] == page
    assert got[2] == page + "\n(note: this exact call has now run 3× this phase with an IDENTICAL result)"
    assert got[23] == page + "\n(note: this exact call has now run 24× this phase with an IDENTICAL result)"
    both = got[24]
    assert both.startswith(
        page + "\n(note: this exact call has now run 25× this phase with an IDENTICAL result)"
        "\n(note: `" + _REF + "` has now been read 25× this phase.")


def test_a_caller_without_a_read_ledger_is_unchanged():
    """`_run_tool_call(..., repeat_state=…)` with no `read_state` is the pre-A9 call: no path
    ledger, no note, no stamp — the seam `tests/test_tool_repeat_streak_is_traced.py` drives."""
    tools = _Tools()
    ledger: dict = {}
    for i in range(30):
        _, note = tool_loop._run_tool_call(tools, "repo_read",
                                           {"path": _REF, "lines": 1, "start_line": i + 1},
                                           repeat_state=ledger)
        assert note == ""


# ------------------------------------------------------------------------ the unpaged reader

def test_read_asset_is_nudged_with_the_unpaged_remedy():
    """`read_asset` has no window and no page, so "read it with `lines` omitted" would be a lie
    about the tool. Mutation: use the paged template for every reader."""
    got = _drive([_tool_call("read_asset", {"name": "train"}) for _ in range(25)],
                 stuck_detection=False)
    note = got[24]
    assert 'has now been read 25× this phase via `read_asset(name="train")`' in note, note
    assert "`lines` OMITTED" not in note and "start_line" not in note, note


# ------------------------------------------------------------------------ the registry

def _fn_spec_calls():
    """Every `fn_spec("<name>", …, {properties}, …)` in `looplab/tools/`, as name -> property keys."""
    found: dict[str, set[str]] = {}
    for path, tree in iter_trees():
        if "tools" not in path.parts:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "fn_spec"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            keys: set[str] = set()
            if len(node.args) >= 3 and isinstance(node.args[2], ast.Dict):
                keys = {k.value for k in node.args[2].keys if isinstance(k, ast.Constant)}
            found.setdefault(str(node.args[0].value), set()).update(keys)
    return found


def test_every_registered_reader_is_a_real_tool_whose_spec_has_the_registered_slot():
    """A registry row naming a tool nobody exposes, or a slot its spec does not declare, is a
    counter that can never increment — the nudge silently off for that reader. Two-way in the
    only direction that is decidable: the tree's readers are not enumerable by AST (a grep is not a
    read), so the reverse half is the docstring's list plus `read_loops.py`'s `READ_TOOLS`."""
    specs = _fn_spec_calls()
    for name, (slot, _paged) in tool_loop._READ_TOOL_PATH_SLOTS.items():
        assert name in specs, f"registered reader {name!r} is not an fn_spec in looplab/tools/"
        assert slot in specs[name], f"{name}'s spec declares no {slot!r} argument: {specs[name]}"


def test_the_benchmarks_read_tools_are_all_registered_here():
    """`benchmarks/read_loops.py` is the reporting end of the same rule; a reader it counts that
    the loop does not nudge is a finding the loop can never act on."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
    import read_loops
    assert read_loops.READ_TOOLS <= set(tool_loop._READ_TOOL_PATH_SLOTS), (
        read_loops.READ_TOOLS - set(tool_loop._READ_TOOL_PATH_SLOTS))


@pytest.mark.parametrize("name,args,key", [
    ("repo_read", {"path": "./a/b.py", "start_line": 3}, "a/b.py"),
    ("read_file", {"path": "a\\b.py"}, "a/b.py"),
    ("read_installed", {"module": "torch.nn"}, "torch.nn"),
    ("read_asset", {"name": "train"}, "train"),
    ("repo_grep", {"pattern": "x", "path": "a.py"}, None),
    ("write_file", {"path": "a.py"}, None),
    ("repo_read", {}, None),
])
def test_the_ledger_key_is_the_normalized_path_of_a_registered_reader_only(name, args, key):
    assert tool_loop._canonical_read_path(name, args) == key
