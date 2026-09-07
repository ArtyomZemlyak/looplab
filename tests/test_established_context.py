"""A5 (docs/60 §60.9): what earlier phases of a run already read is carried into the next chain.

The measurement behind it (docs/56 §200.2): 46.7 % of tool-calling turns across 97 AlgoTune probe
runs requested nothing but content already retrieved in that run, 11,235 of 11,853 by a DIFFERENT
phase. The block is DATA seeded once per chain root, never a rule and never a wider page.

Three properties are pinned here: the store's own truth table (what is recorded, what is carried
verbatim, what becomes an index row, in what order, under what budget); the wiring — a read made
in the Developer's plan phase reaches the next phase's opening prompt through the SAME
`on_tool_result` hook `drive_tool_loop` already exposes; and the byte-identity contract — no
store, or an empty one, leaves every prompt exactly as it was.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from looplab.agents import established as est
from looplab.agents.established import EstablishedContext, established_context_from_settings
from looplab.agents.tool_loop import _READ_TOOL_PATH_SLOTS


def test_the_read_tool_registry_is_the_tool_loops_own():
    """One reading of "which tools return a file": the nudge's table and this store's must agree,
    or a reader the nudge counts is one the block cannot carry (and vice versa)."""
    assert set(est.READ_TOOL_PATH_SLOTS) == set(_READ_TOOL_PATH_SLOTS)
    for name, slot in est.READ_TOOL_PATH_SLOTS.items():
        assert _READ_TOOL_PATH_SLOTS[name][0] == slot


def test_nothing_recorded_renders_nothing_and_non_readers_are_ignored():
    store = EstablishedContext()
    assert store.render() == ""
    assert store.record("write_file", {"path": "a.py", "content": "x"}, "ok") is False
    assert store.record("grep", {"pattern": "x"}, "a.py:1: x") is False
    assert store.record("read_file", {}, "content") is False          # no path, nothing to key on
    assert store.record("read_file", {"path": "a.py"}, "(error: no such file)") is False
    assert store.render() == ""


def test_a_whole_read_is_carried_verbatim_and_a_window_is_only_indexed():
    store = EstablishedContext()
    assert store.record("read_file", {"path": "./ref.py"}, "def f():\n    return 1\n", phase="plan")
    block = store.render()
    assert "`ref.py`" in block and "def f():\n    return 1\n" in block
    assert "first page verbatim" in block and "across plan" in block
    # a windowed read of ANOTHER file never becomes its content — a fragment's meaning depends
    # on the marker that led to it — so that file gets an index row naming the re-read call
    store.record("read_file", {"path": "big.py", "start_line": 40, "lines": 20}, "x = 1\n", phase="plan")
    block = store.render()
    assert "`big.py`" in block and "not carried" in block
    assert 'read_file(path="big.py")' in block
    assert "x = 1" not in block


def test_the_most_refetched_item_comes_first_and_the_budget_binds():
    store = EstablishedContext(budget_bytes=len(est._HEADER.encode()) + 500, item_bytes=6144)
    store.record("read_file", {"path": "once.py"}, "A" * 200, phase="plan")
    for _ in range(3):
        store.record("read_file", {"path": "thrice.py", "start_line": 1}, "B" * 200, phase="plan_step")
    rows = store.items()
    assert [r["path"] for r in rows] == ["thrice.py", "once.py"]
    assert rows[0]["count"] == 3 and rows[0]["phases"] == ["plan_step"]
    block = store.render()
    # the thrice-read file spends the budget; the once-read one degrades to an index row
    assert "B" * 200 in block
    assert "A" * 200 not in block and "`once.py`" in block and "not carried" in block


def test_an_index_row_is_charged_and_the_rows_are_capped():
    """An index row is cheap and a run reads an unbounded number of distinct paths. Uncharged and
    uncapped, the block grows with the run and is pasted into every chain root after that — the
    opposite of what it is for. Over either bound the remainder is ONE counted line."""
    store = EstablishedContext(budget_bytes=len(est._HEADER.encode()) + 200, item_bytes=1)
    for i in range(40):
        store.record("read_file", {"path": f"f{i:02d}.py"}, "body", phase="plan")
    block = store.render()
    assert len(block.encode()) < len(est._HEADER.encode()) + 400, "the budget did not bind"
    assert block.count("\n- `") <= est._MAX_INDEX_ROWS
    assert "more file(s) read earlier in this run, not listed" in block
    # the count is honest: every path is either a row or in the tally
    tallied = int(block.split("(+")[1].split(" more")[0])
    assert block.count("\n- `") + tallied == 40


def test_a_refusal_is_never_stored_as_the_files_first_page():
    """Decided by the READER's own vocabulary. A `(no such file: x)` carried under a header that
    promises the file verbatim is worse than not carrying it: it asserts the refusal twice."""
    from looplab.tools.reposcout import REFUSAL_PREFIXES
    store = EstablishedContext()
    for prefix in REFUSAL_PREFIXES:
        assert store.record("read_file", {"path": "gone.py"}, f"{prefix} gone.py)") is False
    assert store.record("read_file", {"path": "gone.py"}, "(error: boom)") is False
    assert store.render() == ""
    assert store.record("read_file", {"path": "real.py"}, "x = 1\n") is True


def test_a_write_drops_the_page_it_invalidates_and_the_row_says_so():
    """THE defect the predecessor of this block was removed for: a file read in `plan`, rewritten
    in `plan_step`, then seeded into the next phase as its "first page verbatim"."""
    store = EstablishedContext()
    store.record("read_file", {"path": "solver.py"}, "old = 1\n", phase="plan")
    assert "old = 1" in store.render()
    hook = store.hook("plan_step")
    hook("write_file", {"path": "solver.py", "content": "new = 2\n"}, "wrote solver.py")
    block = store.render()
    assert "old = 1" not in block, "a pre-edit page must never be carried forward"
    assert "CHANGED since you read it" in block and "`solver.py`" in block
    # …and a re-read after the write is the CURRENT bytes, so it may be carried again
    hook("read_file", {"path": "solver.py"}, "new = 2\n")
    after = store.render()
    assert "new = 2" in after and "CHANGED since you read it" not in after


def test_every_writer_invalidates_and_a_reader_is_not_a_writer():
    store = EstablishedContext()
    for tool, slot in est.WRITE_TOOL_PATH_SLOTS.items():
        store.record("read_file", {"path": "w.py"}, "before\n")
        assert "before" in store.render(), tool
        assert store.invalidate(tool, {slot: "./w.py"}) is True, tool
        assert "before" not in store.render(), tool
    assert store.invalidate("read_file", {"path": "w.py"}) is False
    assert store.invalidate("write_file", {}) is True, "a writer with no path is still handled"


def test_the_write_table_names_the_real_write_tools():
    """Pinned against the writer's own specs, so a renamed or added write tool is a red test rather
    than a page that silently survives the edit that invalidated it."""
    import ast, inspect
    from looplab.adapters import repo_write_tools as rw
    dispatched = {
        n.comparators[0].value
        for n in ast.walk(ast.parse(inspect.getsource(rw)))
        if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) and n.left.id == "name"
        and n.comparators and isinstance(n.comparators[0], ast.Constant)
        and isinstance(n.comparators[0].value, str)
    }
    for tool in est.WRITE_TOOL_PATH_SLOTS:
        assert tool in dispatched, f"{tool} is not a tool `repo_write_tools` dispatches"


def test_tool_loop_notes_are_stripped_and_an_oversized_page_is_indexed():
    store = EstablishedContext(item_bytes=50)
    store.record("read_file", {"path": "s.py"}, "small\n\n(note: `s.py` has now been read 25x)")
    assert store.items()[0]["content"] == "small\n"
    store.record("read_file", {"path": "huge.py"}, "H" * 51)
    assert store.items()[-1]["content"] is None
    assert "`huge.py`" in store.render() and "H" * 51 not in store.render()


def test_the_hook_records_and_still_calls_the_inner_observer():
    store = EstablishedContext()
    seen = []
    hook = store.hook("propose", inner=lambda n, a, r: seen.append((n, a, r)))
    hook("read_file", {"path": "r.py"}, "body")
    hook("grep", {"pattern": "x"}, "hit")
    assert seen == [("read_file", {"path": "r.py"}, "body"), ("grep", {"pattern": "x"}, "hit")]
    assert store.items()[0]["phases"] == ["propose"]


def test_the_factory_builder_honours_the_setting():
    from looplab.core.config import Settings, LEGACY_CONFIG_SNAPSHOT_DEFAULTS
    on = Settings(); assert on.established_context is True
    assert isinstance(established_context_from_settings(on), EstablishedContext)
    assert established_context_from_settings(Settings(established_context=False)) is None
    assert established_context_from_settings(on).budget_bytes == on.established_context_bytes
    # a resumed old run never gains the block: the LEGACY snapshot default is OFF
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["established_context"] is False


# ---- the wiring, driven through the Developer's plan phase ---------------------------------

def _task(root: Path):
    from looplab.adapters.repo_task import EvalSpec, RepoTask
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text("print('x')\n", encoding="utf-8")
    (root / "ref.py").write_text("def reference():\n    return 42\n", encoding="utf-8")
    return RepoTask(id="r", goal="g", direction="max", editable_path=str(root),
                    edit_surface=["*.py"], protect=[], eval=EvalSpec(command=[sys.executable, "main.py"]))


def _developer(root: Path, **kw):
    from looplab.adapters.repo_developer import LLMRepoDeveloper
    from looplab.core.models import Idea
    dev = LLMRepoDeveloper(object(), _task(root), **kw)
    return dev, Idea(operator="tweak", params={"lr": 0.1}, rationale="try it")


def _capture_run_phase(monkeypatch, replay_read=None):
    """Stub `run_phase`: record the messages it was handed and, if asked, feed one read_file
    result through the hook exactly as the tool loop would."""
    import looplab.agents.agent as agent_mod
    calls = []

    def _fake(client, tools, messages, emit_spec, **kw):
        calls.append({"messages": [dict(m) for m in messages], "kw": kw})
        hook = kw.get("on_tool_result")
        if replay_read and hook is not None:
            hook("read_file", {"path": "ref.py"}, replay_read)
        return []
    monkeypatch.setattr(agent_mod, "run_phase", _fake)
    return calls


def test_a_read_in_the_plan_phase_reaches_the_next_chain_root(monkeypatch, tmp_path):
    store = EstablishedContext()
    dev, idea = _developer(tmp_path / "a", established=store)
    calls = _capture_run_phase(monkeypatch, replay_read="def reference():\n    return 42\n")
    dev._propose_plan("SYSTEM", idea)
    first = calls[0]["messages"][1]["content"]
    assert "ALREADY ESTABLISHED" not in first, "the first chain of a run has nothing to carry"
    assert callable(calls[0]["kw"]["on_tool_result"]), "the phase hands the loop the recording hook"
    dev._propose_plan("SYSTEM", idea)
    second = calls[1]["messages"][1]["content"]
    assert "ALREADY ESTABLISHED" in second and "def reference():" in second
    assert "across plan" in second


def test_no_store_and_an_empty_store_are_byte_identical(monkeypatch, tmp_path):
    calls = _capture_run_phase(monkeypatch)
    dev_none, idea = _developer(tmp_path / "b")
    dev_none._propose_plan("SYSTEM", idea)
    dev_empty, _ = _developer(tmp_path / "c", established=EstablishedContext())
    dev_empty._propose_plan("SYSTEM", idea)
    assert calls[0]["messages"] == calls[1]["messages"]
    assert calls[0]["kw"]["on_tool_result"] is None
    assert callable(calls[1]["kw"]["on_tool_result"])


def test_the_researcher_appends_the_same_block(monkeypatch):
    from looplab.agents.agent import _established_block, _established_hook
    store = EstablishedContext()
    assert _established_block(None) == "" and _established_block(store) == ""
    assert _established_hook(None, "propose") is None
    store.record("read_file", {"path": "ref.py"}, "body", phase="plan")
    assert _established_block(store).startswith("\n\n=== ALREADY ESTABLISHED")
    hook = _established_hook(store, "propose")
    hook("read_file", {"path": "ref.py"}, "body")
    assert store.items()[0]["phases"] == ["plan", "propose"]


@pytest.mark.parametrize("phase", ["plan", "plan_step", "stages", "implement", "repair"])
def test_every_developer_phase_names_its_hook(phase):
    """The five phase labels the Developer records under, pinned by AST so a phase that stops
    handing the hook to `run_phase` goes red rather than silently recording nothing."""
    import ast, inspect
    from looplab.adapters import repo_developer as rd
    tree = ast.parse(inspect.getsource(rd))
    labels = {
        n.args[0].value for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_established_hook" and n.args and isinstance(n.args[0], ast.Constant)
    }
    # the implement/repair site names both through one conditional expression
    conditional = {
        v.value for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_established_hook" and n.args and isinstance(n.args[0], ast.IfExp)
        for v in (n.args[0].body, n.args[0].orelse) if isinstance(v, ast.Constant)
    }
    assert phase in labels | conditional
