"""The scratch tool must see what the task GAVE the model, not only what the model wrote.

MEASURED over the 46-probe corpus, 2026-09-02. `run_probe` fails 399 times; the largest single
class is `ModuleNotFoundError` at 100, and **94 of those name the reference module** --
`reference_edge_expansion` 43x in 21 probes, `reference_pde_heat1d` 40x in 11,
`reference_discrete_log` 11x in 7. Thirty-nine of forty-six probes hit it.

`_replicate` copied `staged.files` -- what the model has WRITTEN. `reference_*.py` is
operator-planted and deliberately excluded from every submission (`repo_spec["protected_names"]`,
the same list `campaign.sh` hands the scorer as `--protect`), so the one file the card tells the
model to consult was the one file its scratch tool could not import.

It is not only wasted turns: reference use is a measured quantity here (§69.1's 4.9-8.3 %, and the
nine-probe control arm of §94), and a harness that refuses the import suppresses the number the arm
exists to move.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from looplab.tools.dev_probe import DevProbeTools  # noqa: E402
from looplab.runtime import landlock  # noqa: E402


# Same rule as `tests/test_dev_probe.py`: on a kernel without Landlock the confined probe FAILS
# CLOSED (`exit=3`, by design), so any test that actually launches one is skipped here rather than
# red. Derivation-only tests keep running.
_NO_LANDLOCK = landlock.unavailable_reason()


@pytest.fixture(autouse=True)
def _skip_when_the_kernel_cannot_confine(monkeypatch):
    if not _NO_LANDLOCK:
        return
    original = DevProbeTools.execute_result

    def _guarded(self, name, args, **kw):
        code = str((args or {}).get("code") or "") if isinstance(args, dict) else ""
        if name == "run_probe" and code.strip() and getattr(self, "confine_reads", True):
            pytest.skip(f"the probe's kernel read rung fails closed here: {_NO_LANDLOCK}")
        return original(self, name, args, **kw)

    monkeypatch.setattr(DevProbeTools, "execute_result", _guarded)


class _Staged:
    def __init__(self, files):
        self.files = dict(files)


def _spec(tmp_path: Path, protected=("reference_task.py", "description.txt")):
    (tmp_path / "reference_task.py").write_text("VALUE = 41\n", encoding="utf-8")
    (tmp_path / "description.txt").write_text("the brief\n", encoding="utf-8")
    return {"editable_path": str(tmp_path), "protected_names": list(protected)}


def _replicate(tmp_path, work, staged):
    t = DevProbeTools(_spec(tmp_path), staged=_Staged(staged))
    work.mkdir(parents=True, exist_ok=True)
    return t._replicate(work)


def test_the_reference_module_lands_in_the_probe_workspace(tmp_path):
    """MUTATION: drop the `_replicate_given` call and `import reference_task` is a
    ModuleNotFoundError -- the corpus failure, reproduced."""
    work = tmp_path / "work"
    note = _replicate(tmp_path, work, {"solver.py": "x = 1\n"})
    assert (work / "reference_task.py").read_text() == "VALUE = 41\n"
    assert (work / "solver.py").read_text() == "x = 1\n"
    assert "operator-given" in note, note


def test_the_models_own_file_wins_a_name_collision(tmp_path):
    """Its own version is the truth for its own build; the given copy must never shadow it."""
    work = tmp_path / "work"
    _replicate(tmp_path, work, {"reference_task.py": "VALUE = 999\n"})
    assert (work / "reference_task.py").read_text() == "VALUE = 999\n"


def test_a_task_that_gives_nothing_is_unchanged(tmp_path):
    work = tmp_path / "work"
    t = DevProbeTools({"editable_path": str(tmp_path), "protected_names": []},
                      staged=_Staged({"solver.py": "x = 1\n"}))
    work.mkdir()
    note = t._replicate(work)
    assert "operator-given" not in note, note
    assert sorted(p.name for p in work.iterdir()) == ["solver.py"]


def test_a_protected_name_cannot_escape_the_workspace(tmp_path):
    """The containment rule the staged loop already applies, applied to this path too.

    The source is planted OUTSIDE `editable_path` and the name walks up to it, so the only way the
    assertion below can fail is a real write outside the disposable directory. MUTATION: delete the
    `str(dest).startswith(...)` guard in `_replicate_given` and `outside/escape.py` appears.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escape.py").write_text("BAD = 1\n", encoding="utf-8")
    inside = tmp_path / "ws"
    inside.mkdir()
    work = inside / "work"
    t = DevProbeTools({"editable_path": str(inside), "protected_names": ["../outside/escape.py"]},
                      staged=_Staged({"solver.py": "x = 1\n"}))
    work.mkdir()
    before = {p for p in tmp_path.rglob("*")}
    t._replicate(work)
    new_paths = {p for p in tmp_path.rglob("*")} - before
    stray = sorted(str(p.relative_to(tmp_path)) for p in new_paths
                   if work not in p.parents and p != work)
    assert stray == [], f"written outside the disposable workspace: {stray}"
    assert sorted(p.name for p in work.rglob("*")) == ["solver.py"]


def test_a_missing_given_file_is_not_an_error(tmp_path):
    work = tmp_path / "work"
    t = DevProbeTools({"editable_path": str(tmp_path), "protected_names": ["absent.py"]},
                      staged=_Staged({"solver.py": "x = 1\n"}))
    work.mkdir()
    note = t._replicate(work)
    assert "staged file(s) replicated" in note
