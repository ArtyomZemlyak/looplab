"""A workdir resolved while a sibling creates its parent is still inside the run (review 2026-09-22,
WIN-4; Windows CI run 36, GitHub Actions run 35823390348).

`test_repair_stop_decision.py::test_only_one_pause_across_concurrent_sibling_evals` failed on the
Windows leg with `reason == "developer_crash"` false for one terminal: node 3 had been closed as
`engine_error` — `ValueError: refusing to materialize outside the run directory:
\\\\?\\C:\\...\\run\\nodes\\node_3` — and the whole run paused for it. The four sibling evals
prepare their workdirs in worker threads at once, and the first to write creates `run/nodes`. On
Windows `Path.resolve()` is `ntpath.realpath`, which strips the `\\\\?\\` prefix of the OS's final
path only when a second probe of the unprefixed name agrees with the first; for a name that does
not exist yet "agrees" means the SAME winerror, and ERROR_PATH_NOT_FOUND became
ERROR_FILE_NOT_FOUND the moment a sibling created `nodes` between node 3's two probes.
`WorkspaceSeeder.materialize` then compared a prefixed workdir against an unprefixed run dir and
refused a path inside it.

`tests/_windows_emulation.py::windows_realpath_race` is that rule as a double, with the window
between the two probes handed to the test, so the interleaving the runner produced by chance is
produced here on purpose. `core/pathsafe.py::resolve_settled` is the fix: ask again while a race's
prefix stands, never strip one `ntpath.realpath` meant.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from looplab.core.pathsafe import resolve_settled
from tests._windows_emulation import VERBATIM_PREFIX, windows_realpath_race
from tests.factories import make_engine


def _parent_appears(parent: Path, name: Path):
    """A window in which a sibling creates `parent` — only while `name` is being resolved."""
    def window(resolving: str) -> None:
        if resolving == str(name):
            parent.mkdir(parents=True, exist_ok=True)
    return window


def test_the_double_keeps_the_prefix_exactly_where_ntpath_does(tmp_path, monkeypatch):
    """The double is only worth its rule: the prefix survives ONLY a changed winerror."""
    run = tmp_path.resolve() / "run"        # resolved BEFORE the double: the host's own spelling
    run.mkdir()
    nodes, workdir = run / "nodes", run / "nodes" / "node_0"
    appears = {str(workdir): nodes, str(run / "own"): run / "own"}

    def window(resolving: str) -> None:
        if resolving in appears:
            appears.pop(resolving).mkdir()

    kept = windows_realpath_race(monkeypatch, window=window)
    assert Path(run).resolve() == run, "an existing entry: the second probe agrees"
    assert Path(run / "a" / "b").resolve() == run / "a" / "b", "missing, and nothing moved"
    assert Path(run / "own").resolve() == run / "own", "the name ITSELF appeared: it resolves"
    raced = Path(workdir).resolve()
    assert str(raced) == VERBATIM_PREFIX + str(workdir), "its missing PARENT appeared: 3 -> 2"
    assert run not in raced.parents
    assert kept == [str(workdir)]


def test_resolve_settled_answers_what_no_race_would_have(tmp_path, monkeypatch):
    run = tmp_path.resolve() / "run"
    run.mkdir()
    nodes, workdir = run / "nodes", run / "nodes" / "node_3"
    kept = windows_realpath_race(monkeypatch, window=_parent_appears(nodes, workdir))

    raced = Path(workdir).resolve()                 # what `materialize` compared before WIN-4
    assert run not in raced.parents and kept == [str(workdir)]
    nodes.rmdir()                                   # the same interleaving again, for the fix
    settled = resolve_settled(workdir)
    assert settled == workdir and run in settled.parents
    assert kept == [str(workdir)] * 2, "the race happened again; the second asking settled it"


def test_a_prefix_ntpath_keeps_on_purpose_is_not_stripped(tmp_path, monkeypatch):
    """An existing entry that only the prefixed spelling reaches (a reserved device name, a trailing
    dot) answers prefixed EVERY time: stripping it would name a different file, so the re-ask keeps
    it. A name spelled with the prefix is answered as `resolve()` answers it."""
    entry = tmp_path.resolve() / "only-the-prefix-reaches-this"
    real = os.path.realpath
    monkeypatch.setattr(os.path, "realpath",
                        lambda path, *a, **k: VERBATIM_PREFIX + real(path, *a, **k))
    assert str(resolve_settled(entry)) == VERBATIM_PREFIX + str(entry)
    spelled = VERBATIM_PREFIX + str(entry)
    assert resolve_settled(spelled) == Path(spelled).resolve()


def test_the_re_asking_is_bounded_by_the_names_depth(tmp_path, monkeypatch):
    """Each re-ask can only lose to ANOTHER component appearing, so a resolve that never settles
    (here: a new answer every time) still returns, after at most one asking per component."""
    real = os.path.realpath
    asked = []

    def never_settles(path, *a, **k):
        asked.append(path)
        return f"{VERBATIM_PREFIX}{real(path, *a, **k)}-{len(asked)}"

    name = tmp_path.resolve() / "a" / "b"
    monkeypatch.setattr(os.path, "realpath", never_settles)
    resolve_settled(name)
    assert 1 < len(asked) <= len(name.parts) + 1, len(asked)


def _engine_with_one_node(tmp_path):
    eng = make_engine(tmp_path / "run")
    eng.store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g",
                                     "direction": "min"})
    node = type("Node", (), {"files": {"helper.py": "X = 1\n"}, "deleted": []})()
    return eng, node


def test_materialize_admits_a_workdir_whose_parent_a_sibling_creates_mid_resolve(
        tmp_path, monkeypatch):
    """The run-36 interleaving on ONE call, no threads needed: the window is where the sibling
    created `run/nodes`. Before WIN-4 this raised "refusing to materialize outside the run
    directory" for a workdir inside it."""
    eng, node = _engine_with_one_node(tmp_path)
    run = Path(eng.run_dir).resolve()
    nodes, workdir = run / "nodes", run / "nodes" / "node_0"
    assert not nodes.exists(), "the premise: no sibling has written yet"
    kept = windows_realpath_race(monkeypatch, window=_parent_appears(nodes, workdir))

    eng._materialize(node, workdir)

    assert str(workdir) in kept, "the premise: the resolve really straddled the creation"
    assert (workdir / "helper.py").read_text(encoding="utf-8") == "X = 1\n"


def test_materialize_still_refuses_what_is_not_inside_the_run(tmp_path, monkeypatch):
    """The fix may only remove a race, never the check: the run dir itself and a workdir outside it
    are refused, with the race's prefix in play or not."""
    eng, node = _engine_with_one_node(tmp_path)
    run = Path(eng.run_dir).resolve()
    outside = tmp_path / "elsewhere" / "node_0"
    with pytest.raises(ValueError, match="outside the run directory"):
        eng._materialize(node, run)
    with pytest.raises(ValueError, match="outside the run directory"):
        eng._materialize(node, outside)
    windows_realpath_race(monkeypatch, window=_parent_appears(outside.parent, outside))
    with pytest.raises(ValueError, match="outside the run directory"):
        eng._materialize(node, outside)
    assert not outside.exists(), "a refused workdir is never built"


def test_materialize_refuses_a_nodes_directory_linked_outside_the_run(tmp_path):
    """RESOLVED containment is what the check is for: a linked `nodes` lands elsewhere."""
    eng, node = _engine_with_one_node(tmp_path)
    run = Path(eng.run_dir).resolve()
    (tmp_path / "elsewhere").mkdir()
    try:
        (run / "nodes").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    except OSError as exc:                 # Windows without the symlink privilege
        pytest.skip(f"cannot create a directory symlink here: {exc}")
    with pytest.raises(ValueError, match="outside the run directory"):
        eng._materialize(node, run / "nodes" / "node_0")
    assert not (tmp_path / "elsewhere" / "node_0").exists()
