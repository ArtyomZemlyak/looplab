"""WP-TOOLS T3 (2026-09-29): the Researcher reads the code it is improving, not the run's starting code.

Measured on MiniOneRec inf13 (82 h, 33 nodes): the champion, node 15, had a 1,563-line
`service/latency_engine.py` with the ragged path and a new `optimizations/exp34_mixed_length_batch.py`;
the run's starting code had a 1,410-line engine and neither. The Researcher proposing an improvement
of node 15 read "lines 776-863 of 1410" believing it was the champion's file, and 234 reads of files
that exist only in node trees failed. `read_code(node_id)` did not help on a repo task: it names the
node's files and prints none of them.

The critic's T3c, behind `Settings.researcher_repo_view_follows_node`: the view is the starting code
with the bound node's recorded `files` over it and its `deleted` hidden — from the EVENT LOG
(`node_created` carries the cumulative files), never the node's directory on disk. A draft reads the
starting code, deep research the incumbent best (chosen at construction). Every reply names its
tree; `node_id` reads any node's (-1 = the starting code). Off, every spec and reply is the
historical one, byte for byte.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from looplab.agents import tool_loop
from looplab.agents.agent import CompositeTools, ToolUsingResearcher, _researcher_workspace
from looplab.agents.established import EstablishedContext
from looplab.agents.repo_reader import (bound_repo_view, repo_reader_provider,
                                        researcher_workspace_token)
from looplab.agents.tool_loop import _LINES_OF_RE, bound_toolset
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, migrate_config_snapshot
from looplab.core.models import Idea, Node, RunState
from looplab.tools._base import RESULT_CAP
from looplab.tools.knowledge_tools import RepoTools
from looplab.tools.reposcout import REFUSAL_PREFIXES
from looplab.tools.run_tools import RunTools

_BASE_ENGINE = "def infer(batch):\n    return batch  # base engine\n"
_ENGINE_15 = "import math\n\ndef infer(batch):\n    return ragged(batch)  # node 15 engine\n"
_ENGINE_7 = "def infer(batch):\n    return padded(batch)  # node 7 engine\n"
_EXP34 = "def mixed_length_batch():\n    return 34  # only node 15 has me\n"


def _repo(tmp_path):
    root = tmp_path / "deploy"
    (root / "service").mkdir(parents=True)
    (root / "service" / "latency_engine.py").write_text(_BASE_ENGINE, encoding="utf-8")
    (root / "README.md").write_text("# deploy\nNEEDLE in the readme\n", encoding="utf-8")
    (root / "legacy.py").write_text("def old_path():\n    NEEDLE = 1\n", encoding="utf-8")
    return root


def _node(nid, files, deleted=()):
    return Node(id=nid, operator="improve", idea=Idea(operator="improve"), files=dict(files),
                deleted=list(deleted))


def _state():
    n15 = _node(15, {"service/latency_engine.py": _ENGINE_15,
                     "optimizations/exp34_mixed_length_batch.py": _EXP34}, deleted=["legacy.py"])
    n7 = _node(7, {"service/latency_engine.py": _ENGINE_7})
    n36 = _node(36, {"optimizations/exp36.py": "def exp36():\n    pass\n"})
    n40 = _node(40, {"optimizations/exp36.py": "def exp36_v2():\n    pass\n"})
    return RunState(direction="min", nodes={7: n7, 15: n15, 36: n36, 40: n40}, best_node_id=15)


def _reader(tmp_path, **kw):
    return RepoTools([{"name": ".", "path": str(_repo(tmp_path))}], follow_node=True, **kw)


def _read(tools, path, **args):
    return tools.execute("repo_read", {"path": path, **args})


# ------------------------------------------------------------------------------ the parent's tree

def test_the_parent_view_shows_its_new_files_and_hides_its_deletions(tmp_path):
    tools = _reader(tmp_path)
    state = _state()
    tools.bind_state(state, state.nodes[15])
    head = "[view: node #15's tree — the code this proposal improves]\n"

    engine = _read(tools, "service/latency_engine.py")
    assert engine == head + _ENGINE_15, engine
    assert _read(tools, "optimizations/exp34_mixed_length_batch.py") == head + _EXP34, (
        "a file only the parent recorded is a file of its tree — 234 inf13 reads failed on these")
    assert _read(tools, "README.md") == head + "# deploy\nNEEDLE in the readme\n", (
        "a file the parent never touched is the starting code's, headed with the same view")
    gone = _read(tools, "legacy.py")
    assert gone == "(no such file: legacy.py — node #15 deleted it from its tree)", gone

    listed = tools.execute("repo_list", {})
    assert listed.startswith(head)
    rows = listed.splitlines()[1:]
    assert "optimizations/exp34_mixed_length_batch.py" in rows and "service/latency_engine.py" in rows
    assert "README.md" in rows and "legacy.py" not in rows, rows

    hits = tools.execute("repo_grep", {"pattern": "return|NEEDLE"})
    assert hits.startswith(head), hits
    assert "service/latency_engine.py:4: return ragged(batch)  # node 15 engine" in hits
    assert "base engine" not in hits, "the pristine copy of a staged file is deduped away"
    assert "legacy.py" not in hits, "a deleted file is not searched"
    assert "optimizations/exp34_mixed_length_batch.py:2:" in hits
    only = tools.execute("repo_grep", {"pattern": "def", "glob": "optimizations/*.py"})
    assert "optimizations/exp34_mixed_length_batch.py:1: def mixed_length_batch():" in only, only


def test_a_windowed_read_keeps_the_page_header_matchable_and_inside_the_cap(tmp_path):
    """The view line sits on its OWN line, so `tool_loop._LINES_OF_RE` still reads the paginator's
    `(lines a-b of T)` (the read-loop nudge's only size source), and a full page stays under the
    loop's cut. MUTATION: put the view on the same line as the page -> the header no longer matches."""
    tools = _reader(tmp_path)
    state = _state()
    big = "".join(f"line {i:05d}: " + "x" * 60 + "\n" for i in range(400))
    state.nodes[15].files["service/big.py"] = big
    tools.bind_state(state, state.nodes[15])
    page = _read(tools, "service/big.py", start_line=5, lines=300)
    header = _LINES_OF_RE.search(page[:400])
    assert header is not None and header.groups()[2] == "400", page[:200]
    assert page.startswith("[view: node #15's tree — the code this proposal improves]\n(lines 5-")
    assert len(page) <= RESULT_CAP and "more below — continue with start_line=" in page


def test_a_draft_reads_the_starting_code(tmp_path):
    tools = _reader(tmp_path)
    tools.bind_state(_state(), None)
    assert _read(tools, "service/latency_engine.py") == (
        "[view: the run's starting code]\n" + _BASE_ENGINE)


# ------------------------------------------------------------------------------ node_id

def test_node_id_reads_another_tree_and_minus_one_the_starting_code(tmp_path):
    tools = _reader(tmp_path)
    state = _state()
    tools.bind_state(state, state.nodes[15])
    assert _read(tools, "service/latency_engine.py", node_id=7) == (
        "[view: node #7's tree]\n" + _ENGINE_7)
    assert _read(tools, "service/latency_engine.py", node_id=-1) == (
        "[view: the run's starting code]\n" + _BASE_ENGINE)
    assert _read(tools, "legacy.py", node_id=-1).startswith("[view: the run's starting code]\n")
    assert _read(tools, "service/latency_engine.py", node_id=15).startswith(
        "[view: node #15's tree — the code this proposal improves]\n"), "the bound node is the view"
    assert tools.execute("repo_grep", {"pattern": "padded", "node_id": 7}) == (
        "[view: node #7's tree]\nservice/latency_engine.py:2: return padded(batch)  # node 7 engine")
    assert "optimizations" not in tools.execute("repo_list", {"node_id": -1})


@pytest.mark.parametrize("node_id", [99, "abc", True])
def test_an_unknown_node_id_is_a_refusal_the_reader_vocabulary_recognizes(tmp_path, node_id):
    """A refusal must OPEN with a `REFUSAL_PREFIXES` entry: a view line in front of it would make
    the A5 ledger store "(no such …)" as a file's first page, verbatim."""
    tools = _reader(tmp_path)
    state = _state()
    tools.bind_state(state, state.nodes[15])
    for name, args in (("repo_read", {"path": "service/latency_engine.py"}),
                       ("repo_grep", {"pattern": "x"}), ("repo_list", {})):
        out = tools.execute(name, {**args, "node_id": node_id})
        assert out.startswith("(refused:"), out
        assert out.startswith(REFUSAL_PREFIXES) and "[view:" not in out
    store = EstablishedContext()
    assert store.record("repo_read", {"path": "a.py", "node_id": node_id},
                        _read(tools, "a.py", node_id=node_id)) is False


def test_absolute_and_node_workspace_spellings_map_to_the_right_tree(tmp_path):
    """80 of the 159 inf13 propose misses spelled the parent's file absolute or through a node dir."""
    tools = _reader(tmp_path)
    root = tmp_path / "deploy"
    state = _state()
    tools.bind_state(state, state.nodes[15])
    assert _read(tools, f"{root}/service/latency_engine.py").endswith(_ENGINE_15)
    assert _read(tools, f"{root}/optimizations/exp34_mixed_length_batch.py").endswith(_EXP34)
    via_node_dir = _read(tools, "/runs/inf13/nodes/node_7/service/latency_engine.py")
    assert via_node_dir == "[view: node #7's tree]\n" + _ENGINE_7, via_node_dir
    missing = _read(tools, "/runs/inf13/nodes/node_99/service/latency_engine.py")
    assert missing == ("(no such file: /runs/inf13/nodes/node_99/service/latency_engine.py — "
                       "node #99 is not an experiment of this run)"), missing
    for escape in ("../outside.txt", "/etc/passwd"):
        assert _read(tools, escape).startswith("(no such file: "), escape


def test_a_miss_names_the_nodes_that_recorded_the_path(tmp_path):
    tools = _reader(tmp_path)
    state = _state()
    tools.bind_state(state, state.nodes[15])
    out = _read(tools, "optimizations/exp36.py")
    assert out == ("(no such file: optimizations/exp36.py in node #15's tree; node #40, #36 "
                   "recorded it — read it with repo_read(node_id=40, path=\"optimizations/exp36.py\"))"
                   ), out
    plain = _read(tools, "nowhere.py")
    assert plain == "(no such file: nowhere.py in node #15's tree)", plain


# ------------------------------------------------------------------------------ gates on node files

def test_a_node_file_is_gated_like_a_disk_file(tmp_path):
    """The scout serves staged content before any gate and greps it unguarded — right for the
    Developer's own writes, not for another candidate's files sent to a (possibly remote) model."""
    tools = _reader(tmp_path)
    state = _state()
    token = "ghp_" + "q" * 32
    state.nodes[15].files.update({".env": f"TOKEN={token}\n", "cfg/secrets.yaml": f"t: {token}\n",
                                  ".git/config": f"url = https://u:{token}@x.invalid\n",
                                  "weights.bin": token})
    state.nodes[7].files[".env"] = f"TOKEN={token}\n"
    tools.bind_state(state, state.nodes[15])
    for path in (".env", "cfg/secrets.yaml", ".git/config", "weights.bin"):
        out = _read(tools, path)
        assert token not in out and out.startswith(REFUSAL_PREFIXES), (path, out)
        assert "recorded it" not in out, "a miss must not disclose that a node wrote a secret"
    assert token not in tools.execute("repo_grep", {"pattern": "TOKEN|url|t:"})
    listed = tools.execute("repo_list", {"glob": "*"})
    assert ".env" not in listed and "secrets" not in listed and ".git" not in listed


# ------------------------------------------------------------------------------ per-call views

def test_binding_a_per_call_view_leaves_the_original_alone(tmp_path):
    """`bound_toolset` shallow-copies each provider, so `bind_state` must REBIND the view — a view
    built by mutating the shared scout would move every other caller's tree too."""
    tools = CompositeTools([_reader(tmp_path)])
    state = _state()
    view = bound_toolset(tools, state, state.nodes[15])
    assert view.execute("repo_read", {"path": "service/latency_engine.py"}).endswith(_ENGINE_15)
    assert tools.execute("repo_read", {"path": "service/latency_engine.py"}).endswith(_BASE_ENGINE)
    tools.bind_state(state, state.nodes[7])
    assert tools.execute("repo_read", {"path": "service/latency_engine.py"}).endswith(_ENGINE_7)
    assert view.execute("repo_read", {"path": "service/latency_engine.py"}).endswith(_ENGINE_15)
    assert bound_repo_view(view) == "node:15" and bound_repo_view(tools) == "node:7"


# ------------------------------------------------------------------------------ A5 and the nudge

def test_a5_does_not_carry_a_page_read_in_one_tree_into_another(tmp_path):
    """`record` keyed on (tool, path) under ONE "researcher" token: a page read over parent 29 was
    carried "verbatim — do not re-fetch" into a propose over parent 7. MUTATION: keep the stable
    token -> the node-15 engine is rendered into the node-7 chain root."""
    tools = CompositeTools([_reader(tmp_path)])
    state = _state()
    store = EstablishedContext()
    hook = store.hook("propose")

    tools.bind_state(state, state.nodes[15])
    _researcher_workspace(store, tools)
    args = {"path": "service/latency_engine.py"}
    hook("repo_read", args, tools.execute("repo_read", args))
    assert "node 15 engine" in store.render()

    tools.bind_state(state, state.nodes[7])
    _researcher_workspace(store, tools)
    block = store.render()
    assert "node 15 engine" not in block
    assert "read in a DIFFERENT repo tree — re-read" in block, block

    tools.bind_state(state, state.nodes[15])
    _researcher_workspace(store, tools)
    assert "node 15 engine" in store.render(), "re-entering the tree restores its pages"


def test_a5_keys_an_explicit_tree_apart_and_names_it_in_the_re_read_call():
    store = EstablishedContext()
    store.enter_workspace("researcher@node:15")       # a node-following view: node_id is offered
    store.record("repo_read", {"path": "a.py", "node_id": 7}, "[view: node #7's tree]\nA = 7\n")
    store.record("repo_read", {"path": "a.py"}, "[view: node #15's tree]\nA = 15\n")
    assert {(r["path"], r["node_id"]) for r in store.items()} == {("a.py", 7), ("a.py", None)}, (
        "one path in two trees is two files, two rows")
    block = EstablishedContext(item_bytes=0)        # nothing fits: the row names the re-read call
    block.enter_workspace("researcher@node:15")
    block.record("repo_read", {"path": "a.py", "node_id": 7}, "A = 7\n")
    rendered = block.render()
    assert '`repo_read(node_id=7, path="a.py")`' in rendered, rendered
    assert "(repo_read node_id=7;" in rendered


def test_a5_ignores_a_node_id_the_reader_does_not_offer():
    """D6: with the flag off the Researcher's workspace is the plain "researcher" and `repo_read`
    ignores a `node_id` a model invents — so A5 must too, or one file splits into two rows and the
    row names a re-read call that reads something else than it says. MUTATION: drop the gate ->
    two rows, and `repo_read(node_id=7, …)` in the block."""
    for token in ("researcher", None, ("node", 3)):
        store = EstablishedContext(item_bytes=0)
        if token is not None:
            store.enter_workspace(token)
        store.record("repo_read", {"path": "a.py", "node_id": 7}, "A = 1\n")
        store.record("repo_read", {"path": "a.py"}, "A = 1\n")
        assert [(r["path"], r["node_id"], r["count"]) for r in store.items()] == [("a.py", None, 2)]
        assert "node_id" not in store.render(), token


def test_the_read_loop_nudge_keeps_the_node_id_and_counts_each_tree_apart():
    """The note named `repo_read(path=…)` — which re-reads the BOUND view, a different file than
    the one being walked. MUTATION: drop the node_id from the note -> the remedy reads node 15's
    engine for a model walking node 7's."""
    views = frozenset({"repo_read"})                   # the reader OFFERS node_id (flag on)
    ledger: dict = {}
    notes = [tool_loop._note_path_read(ledger, "repo_read",
                                       {"path": "service/x.py", "node_id": 7, "start_line": i},
                                       "(lines 1-1 of 9)\nx\n", 3, views=views)[1]
             for i in range(4)]
    assert notes[1] == "" and '`repo_read(node_id=7, path="service/x.py")`' in notes[2], notes[2]
    reads, note = tool_loop._note_path_read(ledger, "repo_read", {"path": "service/x.py"}, "x\n", 3,
                                            views=views)
    assert reads == 1 and note == "", "the bound view's copy is another file: its own counter"
    assert tool_loop._read_loop_stuck(ledger, "repo_read", {"path": "service/x.py", "node_id": 7},
                                      1, views=views) is not None, "4 reads of node 7's copy pass 4 x 1"
    assert tool_loop._read_loop_stuck(ledger, "repo_read", {"path": "service/x.py"}, 1,
                                      views=views) is None
    plain = tool_loop._note_path_read({}, "repo_read", {"path": "service/x.py"}, "x\n", 1)[1]
    assert '`repo_read(path="service/x.py")`' in plain, "no node_id: the call as it always was"


# ------------------------------------------------------------------------------ deep research, read_code

def test_deep_research_reads_the_incumbent_best_and_enters_that_views_workspace(tmp_path,
                                                                                 monkeypatch):
    from looplab.agents import agent
    from looplab.agents.deep_research import make_deep_researcher
    root = _repo(tmp_path)
    task = SimpleNamespace(params=None,
                           repo_spec=lambda: {"editables": [{"name": ".", "path": str(root)}]})
    settings = SimpleNamespace(
        researcher_tools=True, cross_run_tools=False, all_runs_tools=False,
        cross_run_read_tools=False, memory_dir=None, knowledge_dir=None, skills_dir=None,
        literature_search=False, web_search=False, prompt_dir=None, llm_parser="tool_call",
        hide_empty_tools=False, context_budget_chars=None, agent_max_turns=0,
        agent_time_budget_s=0.0, compressor_model=None, established_context=True,
        established_context_bytes=12288, researcher_repo_view_follows_node=True)
    deep = make_deep_researcher(settings, client=object(), task=task, run_dir=tmp_path / "run")
    seen = {}

    def _fake_loop(client, tools, messages, emit_spec, *, finalize, on_tool_result, **_kw):
        args = {"path": "service/latency_engine.py"}
        seen["read"] = tools.execute("repo_read", args)
        on_tool_result("repo_read", args, seen["read"])
        seen["token"] = deep._established._workspace
        seen["read_code"] = tools.execute("read_code", {"node_id": 15})
        return finalize({"summary": "reviewed"})

    monkeypatch.setattr(agent, "drive_tool_loop", _fake_loop)
    deep.research(_state())
    assert seen["read"] == ("[view: node #15's tree — the run's current best]\n" + _ENGINE_15)
    assert seen["token"] == "researcher@node:15", seen["token"]
    assert 'repo_read(node_id=15, path="service/latency_engine.py")' in seen["read_code"]


def test_read_code_of_a_repo_node_points_at_the_call_that_reads_its_files(tmp_path):
    state = _state()
    plain = RunTools()
    plain.bind_state(state)
    before = plain.execute("read_code", {"node_id": 15})
    assert "repo_read" not in before, "a toolset with no node-following reader names no such call"
    beside = RunTools()
    root = _repo(tmp_path)
    task = SimpleNamespace(params=None, repo_spec=lambda: {
        "editables": [{"name": ".", "path": str(root)}]})
    repo_reader_provider(task, Settings(researcher_repo_view_follows_node=True), beside=[beside])
    beside.bind_state(state)
    after = beside.execute("read_code", {"node_id": 15})
    assert after.startswith(before), "the historical reply is kept; the pointer is appended"
    assert after[len(before):] == (
        "\n(these are NAMES only — read a file as experiment #15 left it with "
        "repo_read(node_id=15, path=\"service/latency_engine.py\"))"), after


def test_propose_reads_its_parents_tree_end_to_end(tmp_path):
    """Through the real `ToolUsingResearcher.propose`: it binds the reader to the parent before its
    first turn, and the first repo_read of the loop returns the parent's file."""
    class _Client:
        def __init__(self):
            self.turns = []
            self.script = [
                {"content": "", "tool_calls": [{"id": "c1", "function": {
                    "name": "repo_read",
                    "arguments": json.dumps({"path": "service/latency_engine.py"})}}]},
                {"content": "", "tool_calls": [{"id": "c2", "function": {
                    "name": "emit", "arguments": json.dumps({
                        "operator": "improve", "params": {}, "rationale": "use the ragged path",
                        "concept_mode": "full"})}}]}]

        def chat(self, messages, tools, tool_choice="auto"):
            self.turns.append(list(messages))
            return self.script.pop(0)

    client = _Client()
    tools = CompositeTools([_reader(tmp_path)])
    researcher = ToolUsingResearcher(client, tools, handoff=False)
    state = _state()
    researcher.propose(state, state.nodes[15])
    result = [m["content"] for m in client.turns[1] if m.get("role") == "tool"][0]
    assert result.startswith("[view: node #15's tree — the code this proposal improves]\n")
    assert "node 15 engine" in result and "base engine" not in result


# ------------------------------------------------------------------------------ the flag

def test_off_is_the_historical_tools_byte_for_byte(tmp_path):
    """OFF (the constructor default, and every run launched before the field): no `node_id`, no
    view line, the starting code whatever the call is bound to, and `bind_state` does nothing.
    The expected specs are spelled out: the pre-T3 ones (with T1's `glob` description)."""
    mounts = [{"name": ".", "path": str(_repo(tmp_path))}]
    off = RepoTools(mounts)
    assert off.follow_node is False
    assert off.specs() == RepoTools(mounts, follow_node=False).specs()
    assert off.specs() == [
        {"type": "function", "function": {
            "name": "repo_grep",
            "description": "Regex search across the editable repo source (.). Returns matching "
                           "<repo>/<path>:<line> hits.",
            "parameters": {"type": "object", "properties": {
                "pattern": {"type": "string"},
                "glob": {"type": "string", "description": (
                    "optional file filter: a file-name glob like *.py, or a repo-relative path "
                    "glob like service/*.py (* stays inside one directory, **/ spans any)")}},
                "required": ["pattern"]}}},
        {"type": "function", "function": {
            "name": "repo_list", "description": "List source files in an editable repo (.).",
            "parameters": {"type": "object", "properties": {
                "repo": {"type": "string"}, "glob": {"type": "string"}}, "required": []}}},
        {"type": "function", "function": {
            "name": "repo_read",
            "description": "Read a file from an editable repo, given a <repo>/<path> (or just <path> "
                           "for the root repo). Returns ONE page of at most ~3600 chars; window with "
                           "start_line (+ optional lines). A page with more file below it ENDS with "
                           "'… (more below — continue with start_line=N)' — continue from exactly "
                           "that N (a single line longer than one page is cut mid-line — the marker "
                           "says so and resumes at the NEXT line); a reply WITHOUT that marker IS the "
                           "end of the file. Never re-read from the top.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer",
                               "description": "1-based line to start from (default top)"},
                "lines": {"type": "integer",
                          "description": "how many lines to return (optional window)"}},
                "required": ["path"]}}},
    ]
    calls = [("repo_read", {"path": "service/latency_engine.py"}),
             ("repo_read", {"path": "optimizations/exp34_mixed_length_batch.py"}),
             ("repo_read", {"path": "service/latency_engine.py", "node_id": 7}),
             ("repo_grep", {"pattern": "return"}), ("repo_list", {})]
    unbound = [off.execute(name, dict(args)) for name, args in calls]
    state = _state()
    off.bind_state(state, state.nodes[15])
    assert [off.execute(name, dict(args)) for name, args in calls] == unbound
    assert unbound[0] == _BASE_ENGINE and unbound[1].startswith("(no such file:")
    assert not any("[view:" in out for out in unbound)
    assert bound_repo_view(CompositeTools([off])) is None
    assert researcher_workspace_token(CompositeTools([off])) is None
    store = EstablishedContext()
    _researcher_workspace(store, CompositeTools([off]))
    assert store._workspace == "researcher", "the historical token, byte for byte"


def test_the_flag_is_on_for_new_runs_and_off_for_constructors_and_pre_field_runs(tmp_path):
    assert Settings().researcher_repo_view_follows_node is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["researcher_repo_view_follows_node"] is False
    snapshot = Settings().model_dump(mode="json")
    snapshot.pop("researcher_repo_view_follows_node")
    assert migrate_config_snapshot(snapshot)["researcher_repo_view_follows_node"] is False
    root = _repo(tmp_path)
    task = SimpleNamespace(params=None, repo_spec=lambda: {
        "editables": [{"name": ".", "path": str(root)}]})
    assert repo_reader_provider(task).follow_node is False, "no settings: the constructor default"
    assert repo_reader_provider(task, Settings(researcher_repo_view_follows_node=False)
                                ).follow_node is False
    on = repo_reader_provider(task, Settings())
    assert on.follow_node is True and on.no_parent_view == "base"
    assert repo_reader_provider(task, Settings(), no_parent_view="best").no_parent_view == "best"


@pytest.mark.parametrize("flag", [True, False])
def test_make_roles_wires_the_researchers_reader_by_the_flag(tmp_path, flag):
    from looplab.adapters.repo_task import EvalSpec, RepoTask
    from looplab.adapters.tasks import make_roles
    repo = _repo(tmp_path)
    task = RepoTask(id="e", editable_path=str(repo), edit_surface=["*.py"],
                    eval=EvalSpec(command=["python", "m.py"]))
    settings = Settings()
    settings.backend, settings.unified_agent = "llm", False
    settings.researcher_repo_view_follows_node = flag
    researcher, _ = make_roles(task, settings)
    providers = getattr(researcher.tools, "providers", [researcher.tools])
    readers = [p for p in providers if isinstance(p, RepoTools)]
    assert len(readers) == 1 and readers[0].follow_node is flag
    assert readers[0].no_parent_view == "base"
    run_tools = [p for p in providers if isinstance(p, RunTools)]
    assert run_tools and all(p.repo_read_node_view is flag for p in run_tools)


# ------------------------------------------------------------------------------ post-review fixes

def test_a_path_spelled_another_way_still_reads_the_node_tree_and_its_deletions(tmp_path):
    """D1: the disk half RESOLVES its path while the key did not, so `service//x.py`,
    `service/./x.py` and `<root>//service/x.py` served the BASE bytes under node 15's header, and
    `service/../legacy.py` served a file node 15 deleted. MUTATION: drop the key normalisation and
    the rebuilt key -> base bytes under `[view: node #15's tree …]`."""
    tools = _reader(tmp_path)
    root = tmp_path / "deploy"
    state = _state()
    tools.bind_state(state, state.nodes[15])
    head = "[view: node #15's tree — the code this proposal improves]\n"
    for spelled in ("service//latency_engine.py", "service/./latency_engine.py",
                    "./service/latency_engine.py", f"{root}//service/latency_engine.py",
                    f"{root}/service/../service/latency_engine.py",
                    "optimizations/../service/latency_engine.py"):
        assert _read(tools, spelled) == head + _ENGINE_15, spelled
    assert _read(tools, "optimizations//exp34_mixed_length_batch.py") == head + _EXP34
    for spelled in ("service/../legacy.py", f"{root}/./legacy.py", "service/..//legacy.py"):
        assert _read(tools, spelled) == (
            f"(no such file: {spelled} — node #15 deleted it from its tree)"), spelled
    for escape in ("../outside.txt", "service/../../outside.txt"):
        out = _read(tools, escape)
        assert out.startswith("(no such file: ") and "recorded it" not in out, out


def test_a_symlinked_directory_in_the_repo_reads_the_node_copy_it_points_at(tmp_path):
    """The key rebuilt from where the disk read would land (`RepoTools._placed`) catches what no
    lexical normalisation can: an alias directory inside the mount."""
    import os
    tools = _reader(tmp_path)
    try:
        os.symlink(tmp_path / "deploy" / "service", tmp_path / "deploy" / "alias")
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support symlinks")
    state = _state()
    tools.bind_state(state, state.nodes[15])
    assert _read(tools, "alias/latency_engine.py").endswith(_ENGINE_15)
    assert _read(tools, "alias/latency_engine.py", node_id=-1).endswith(_BASE_ENGINE)


def test_a_node_file_withheld_for_its_type_is_refused_not_denied(tmp_path):
    """D3: a `.cu` / `.ipynb` / `.pyx` node file EXISTS in the node's tree; "(no such file …)" was
    false. A credential-shaped name still reads as absent — its existence is not disclosed.
    MUTATION: drop `withheld` -> "(no such file: kernels/attn.cu in node #15's tree)"."""
    tools = _reader(tmp_path)
    state = _state()
    state.nodes[15].files.update({"kernels/attn.cu": "__global__ void k() {}\n",
                                  "nb/explore.ipynb": "{}", "fast/ops.pyx": "cdef int x\n",
                                  "cfg/secrets.yaml": "t: 1\n"})
    tools.bind_state(state, state.nodes[15])
    for path, name in (("kernels/attn.cu", "attn.cu"), ("nb/explore.ipynb", "explore.ipynb"),
                       ("fast/ops.pyx", "ops.pyx"), ("kernels//attn.cu", "attn.cu")):
        assert _read(tools, path) == (f"(refused: {name} is not a readable source file — "
                                      "repository internals and binaries are not returned)"), path
    assert _read(tools, "cfg/secrets.yaml") == (
        "(no such file: cfg/secrets.yaml in node #15's tree)"), "no existence disclosure"


def test_a_mount_under_a_nodes_directory_is_not_read_as_a_node_workspace(tmp_path):
    """D5: the node-directory spelling was tried BEFORE mount-root membership, so a repo living
    under `…/nodes/node_3/…` — or a repo path containing `/nodes/node_1/` — was redirected to a
    node's tree or refused. MUTATION: test the node-dir pattern first -> node 3's file, or
    "(no such file: … node #1 is not an experiment of this run)"."""
    root = tmp_path / "exp" / "nodes" / "node_3" / "deploy"
    (root / "service").mkdir(parents=True)
    (root / "service" / "latency_engine.py").write_text(_BASE_ENGINE, encoding="utf-8")
    (root / "results" / "nodes" / "node_1").mkdir(parents=True)
    (root / "results" / "nodes" / "node_1" / "log.txt").write_text("LOG ONE\n", encoding="utf-8")
    tools = RepoTools([{"name": ".", "path": str(root)}], follow_node=True)
    state = _state()
    state.nodes[3] = _node(3, {"service/latency_engine.py": "NODE THREE\n"})
    tools.bind_state(state, state.nodes[15])
    head = "[view: node #15's tree — the code this proposal improves]\n"
    assert _read(tools, f"{root}/service/latency_engine.py") == head + _ENGINE_15
    assert _read(tools, "results/nodes/node_1/log.txt") == head + "LOG ONE\n"
    assert _read(tools, f"{root}/results/nodes/node_1/log.txt") == head + "LOG ONE\n"
    # …while an absolute path under NO mount is still read through the node it names.
    assert _read(tools, "/elsewhere/runs/r/nodes/node_7/service/latency_engine.py").endswith(
        _ENGINE_7)


def test_a_deleted_file_stays_hidden_under_a_named_mount(tmp_path):
    """`_is_deleted_abs` compared a named mount's `<name>/rel` deletion keys against a path relative
    to the FIRST root, so no named mount's deletion was ever hidden from `find_files` / `list_dir`
    / `repo_list`. MUTATION: drop its `_disp` branch -> every assertion below goes red."""
    from looplab.tools.reposcout import RepoScoutTools
    a, b = tmp_path / "a", tmp_path / "b"
    for root, names in ((a, ("keep.py", "old.py")), (b, ("util.py", "gone.py"))):
        root.mkdir()
        for name in names:
            (root / name).write_text("x = 1\n", encoding="utf-8")
    scout = RepoScoutTools(roots=[str(a), str(b)], default_root=str(a),
                           named_roots=[("a", str(a)), ("b", str(b))],
                           deleted=["a/old.py", "b/gone.py"])
    found_a = scout.execute("find_files", {"root": str(a), "pattern": "**/*.py"}).splitlines()
    found_b = scout.execute("find_files", {"root": str(b), "pattern": "**/*.py"}).splitlines()
    assert found_a == ["a/keep.py"] and found_b == ["b/util.py"], (found_a, found_b)
    assert "old.py" not in scout.execute("list_dir", {"path": str(a)})
    assert "gone.py" not in scout.execute("list_dir", {"path": str(b)})

    tools = RepoTools([{"name": "a", "path": str(a)}, {"name": "b", "path": str(b)}],
                      follow_node=True)
    state = RunState(direction="max", nodes={5: _node(5, {"b/new.py": "y = 2\n"},
                                                      deleted=["a/old.py", "b/gone.py"])})
    tools.bind_state(state, state.nodes[5])
    listed_a = tools.execute("repo_list", {"repo": "a"}).splitlines()[1:]
    listed_b = tools.execute("repo_list", {"repo": "b"}).splitlines()[1:]
    assert listed_a == ["a/keep.py"] and listed_b == ["b/new.py", "b/util.py"], (listed_a, listed_b)


def test_the_loop_counts_a_node_id_only_where_the_reader_offers_it(tmp_path):
    """D6, driven through the real `drive_tool_loop`: with the flag OFF `repo_read` offers no
    `node_id` and ignores an invented one, so the nudge must name the call as it always was and
    count one file once; ON, it keeps the node_id. MUTATION: ungate `_read_node_id` -> the OFF note
    names `repo_read(node_id=7, …)`, a call that reads the starting code, not node 7's file."""
    from looplab.agents.agent import drive_tool_loop
    emit = {"type": "function", "function": {"name": "emit", "description": "final",
                                             "parameters": {"type": "object", "properties": {}}}}

    def _drive(tools):
        calls = [{"content": "", "tool_calls": [{"id": f"c{i}", "function": {
            "name": "repo_read", "arguments": json.dumps({
                "path": "service/latency_engine.py", "node_id": 7, "start_line": i + 1,
                "lines": 1})}}]} for i in range(3)]
        calls.append({"content": "", "tool_calls": [{"id": "e", "function": {
            "name": "emit", "arguments": "{}"}}]})
        seen: list = []

        class _Client:
            def chat(self, messages, tools, tool_choice="auto"):
                seen.append([m["content"] for m in messages if m.get("role") == "tool"])
                return calls.pop(0)

        drive_tool_loop(_Client(), tools, [{"role": "user", "content": "go"}], emit,
                        finalize=lambda args: args, fallback=lambda msgs: None,
                        read_loop_nudge_after=3, stuck_detection=False)
        return seen[-1][-1]

    off = RepoTools([{"name": ".", "path": str(_repo(tmp_path / "off"))}])
    note = _drive(off)
    assert '`repo_read(path="service/latency_engine.py")`' in note, note
    assert "node_id=7" not in note and "has now been read 3×" in note
    on = RepoTools([{"name": ".", "path": str(_repo(tmp_path / "on"))}], follow_node=True)
    on.bind_state(_state(), None)
    assert '`repo_read(node_id=7, path="service/latency_engine.py")`' in _drive(on)
    assert tool_loop._view_readers(off.specs()) == frozenset()
    assert tool_loop._view_readers(on.specs()) == frozenset({"repo_read"})


def test_a_workspace_file_is_found_however_spelled_when_every_mount_is_named(tmp_path):
    """D1's lexical half: with every mount NAMED, a node's workspace-root file (`looplab_stages.json`)
    maps into no mount, so the key rebuilt from the disk path cannot find it — only the normalised
    spelling can. MUTATION: drop the `posixpath.normpath` -> "(no such file: …)"."""
    a = tmp_path / "a"
    a.mkdir()
    (a / "x.py").write_text("BASE = 1\n", encoding="utf-8")
    tools = RepoTools([{"name": "a", "path": str(a)}], follow_node=True)
    state = RunState(direction="max", nodes={9: _node(9, {"looplab_stages.json": "{}\n",
                                                           "a/x.py": "NODE = 9\n"})})
    tools.bind_state(state, state.nodes[9])
    head = "[view: node #9's tree — the code this proposal improves]\n"
    for spelled in ("looplab_stages.json", "./looplab_stages.json", "a/../looplab_stages.json"):
        assert _read(tools, spelled) == head + "{}\n", spelled
    for spelled in ("a/x.py", "a//x.py", "./a/./x.py", f"{a}/x.py"):
        assert _read(tools, spelled) == head + "NODE = 9\n", spelled


def test_a_windows_drive_root_is_an_absolute_spelling_too():
    """The Windows CI leg (2026-09-30): a mount's absolute spelling was registered only when it began
    with `/`, so on Windows no absolute spelling of any mount mapped — `C:\\…\\a/x.py` read as
    "(no such file …)" in the test above. MUTATION: drop the drive clause -> None for `C:/…`."""
    from looplab.tools.knowledge_tools import _absolute_root_spelling
    assert _absolute_root_spelling("C:\\Users\\runner\\a\\") == "C:/Users/runner/a"
    assert _absolute_root_spelling("d:/work/repo") == "d:/work/repo"
    assert _absolute_root_spelling("/abs/repo/") == "/abs/repo"
    for relative in ("repo", "./repo", "", "/", "C:", "C:repo"):
        assert _absolute_root_spelling(relative) is None, relative
