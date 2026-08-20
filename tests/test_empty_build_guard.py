"""A build that wrote nothing must not become a node.

Measured 2026-08-20 on an AlgoTune `discrete_log` run: the implement phase spent 19 generations
calling `run_probe` 24 times, `read_file` 8 and `grep` 7 — and `write_file`/`edit_file` **zero**
times. The session ended on its own wall budget, `_run` returned "" (no error), and the engine
committed a node whose `node_created.files` is `{}`. Its `solver.py` was the untouched template
(`raise NotImplementedError`), the evaluation ran honestly, and the run recorded `speedup: 0.0`
after 195 paid calls and $0.18.

The cost is not the wasted evaluation. It is that a real 0.0 is EVIDENCE — an idea that was tried
and did not work, which the next Researcher turn reads — and this one is an empty box wearing its
clothes. Nothing downstream could tell them apart.
"""
from __future__ import annotations

import pytest

from looplab.adapters.repo_developer import empty_build_refusal
from looplab.core.models import is_developer_error


class _Write:
    """Stands in for `RepoWriteTools` — `_run` only reads `.files` / `.deleted` at the exit."""

    def __init__(self, files=None, deleted=None):
        self.files = dict(files or {})
        self.deleted = list(deleted or [])


def _tail(error, base, base_deleted, write):
    """The REAL rule, driven — not a copy of it. `_run` calls exactly this at its exit, so a change
    to either side shows up here rather than in a second spelling nobody re-derives (CLAUDE.md
    tier 2: hoist the rule so its truth table can be stated)."""
    return empty_build_refusal(error=error, base=base, base_deleted=base_deleted,
                               files=write.files, deleted=write.deleted)


def test_a_fresh_build_that_wrote_nothing_is_a_developer_error():
    out = _tail(error=None, base=None, base_deleted=None, write=_Write())
    assert is_developer_error(out), "an empty working set must not pass as a built node"
    assert "no candidate to evaluate" in out


@pytest.mark.parametrize("write", [
    _Write(files={"solver.py": "print(1)"}),
    _Write(deleted=["old.py"]),
    _Write(files={"a.py": ""}, deleted=["b.py"]),
])
def test_a_build_that_wrote_or_deleted_anything_passes(write):
    assert _tail(error=None, base=None, base_deleted=None, write=write) == ""


def test_a_repair_with_an_empty_change_set_is_NOT_convicted_here():
    """`engine/repair_verify.py` already judges that as `inert` and bounds it with
    INERT_REPAIR_LIMIT. Convicting it twice would charge one event under two vocabularies — and
    the two mean different things: 'nothing was built' vs 'nothing was CHANGED'."""
    assert _tail(error="boom", base=None, base_deleted=None, write=_Write()) == ""


def test_a_refinement_from_a_parent_is_NOT_convicted_here():
    """`implement_from`/`repair_from` pre-load `write.files` from a base, so an unchanged set there
    is a no-op EDIT, not a missing build. Both base spellings must exempt it — `base_deleted` alone
    is how a parent that only removed files arrives."""
    assert _tail(error=None, base={"solver.py": "x"}, base_deleted=None, write=_Write()) == ""
    assert _tail(error=None, base=None, base_deleted=["gone.py"], write=_Write()) == ""


def test_run_actually_calls_the_rule_at_its_exit():
    """Hoisting a rule is only worth it if the caller still calls it. `_source_scan.called_names`
    resolves real `ast.Call` nodes (CLAUDE.md tier 3), so a commented-out call cannot satisfy this —
    and it is the repo's ONE walk, rather than a private `getsource` + `cleandoc` that mis-indents a
    method body."""
    from _source_scan import called_names

    from looplab.adapters.repo_developer import LLMRepoDeveloper

    assert "empty_build_refusal" in called_names(LLMRepoDeveloper._run), (
        "_run no longer consults the empty-build rule")
