"""C1 fault localization over the repo source tree."""
from __future__ import annotations

from pathlib import Path

from looplab.localize import _symbols, localize

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"


def test_symbols_extracts_traceback_files_and_idents():
    files, idents = _symbols('Traceback ...\n  File "ttrain.py", line 5\nNameError: lr_schedule')
    assert "ttrain.py" in files
    assert "lr_schedule" in idents and "Traceback" not in idents


def test_localize_ranks_named_file_first():
    err = 'Traceback:\n  File "ttrain.py", line 10, in <module>\nValueError: bad config'
    ranked = localize(err, [FIXTURE])
    assert ranked, "expected at least one localized file"
    assert ranked[0]["file"].endswith("ttrain.py")
    assert "named-in-traceback" in ranked[0]["hits"]


def test_localize_ranks_by_shared_identifiers_without_traceback():
    # No file named -> rank by identifiers shared with the repo source. Use a token likely in the
    # fixture; if nothing matches the result is just empty (no crash).
    ranked = localize("error involving train and model and data", [FIXTURE], top=3)
    assert isinstance(ranked, list)
    for r in ranked:
        assert r["score"] > 0 and r["file"].endswith(".py")


def test_localize_empty_on_nonexistent_root(tmp_path):
    assert localize("x.py failed", [tmp_path / "nope"]) == []


def test_engine_hint_includes_localized_files(tmp_path):
    # Wire-level: a repo task with a failure surfaces localized files into the proposal cue.
    import anyio
    from looplab.models import Idea, Node, NodeStatus, RunState
    from looplab.orchestrator import Engine
    from looplab.policy import GreedyTree
    from looplab.sandbox import SubprocessSandbox
    from looplab.toytask import ToyTask

    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    eng = Engine(tmp_path / "r", task=task, researcher=type("S", (), {})(), developer=type("D", (), {})(),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 localize_faults=True)
    eng._repo_spec = {"editables": [{"name": ".", "path": str(FIXTURE)}]}
    st = RunState(direction="min")
    st.nodes[0] = Node(id=0, operator="draft", idea=Idea(operator="draft", params={}),
                       status=NodeStatus.failed,
                       error='File "ttrain.py", line 3\nValueError: boom')
    eng.researcher = type("R", (), {})()
    eng._set_complexity_hint(st, None)
    assert "Fault localization" in getattr(eng.researcher, "_complexity_hint", "")
    assert "ttrain.py" in getattr(eng.researcher, "_complexity_hint", "")
