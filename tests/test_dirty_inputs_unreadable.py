"""A source we could not read is UNKNOWN, not clean.

`workspace.py::substrate_fingerprint` guards its call with `except Exception: dirty = None` and
records `{"dirty": "unknown"}`, and its comment names an index.lock, a mid-rebase tree, an EIO and a
timeout. Not one of them could reach that branch: `Engine._dirty_inputs` swallowed every per-source
exception with a bare `pass` and returned the same `[]` a genuinely clean tree returns.

WHAT THAT COST: a wedged geesefs mount — a 10 s wall against this box's measured 105-950 ms lstats —
made the node record the bare-HEAD fingerprint, BYTE-EQUAL to a clean checkout, so `comparability`
could certify SAME across a substrate change. That is precisely the confidently-wrong record the
`unknown` branch exists to refuse.

ONLY AN EXCEPTION COUNTS. `git status` exits 128 for "not a git repository", which is the ordinary
condition of a plain data mount; treating a nonzero exit as unreadable would put nearly every run
into the unknown branch and tell the operator nothing. That residue is stated in the code and pinned
by the last test here.

Every assertion has an input that makes it FAIL; the mutations are named.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest


class _Engine:
    """The narrowest host `_dirty_inputs` needs — it reads nothing off self but the method."""


def _dirty(sources, run):
    """`_dirty_inputs` does `import subprocess` INSIDE the function, so the seam is the stdlib
    module object itself — patching `orchestrator.subprocess` finds no such attribute, which is how
    the first cut of this file failed with AttributeError rather than measuring anything."""
    from looplab.engine import orchestrator

    host = _Engine()
    saved = subprocess.run
    subprocess.run = run
    try:
        return orchestrator.Engine._dirty_inputs(host, sources)
    finally:
        subprocess.run = saved


def _ok(stdout="", rc=0):
    return lambda *a, **k: SimpleNamespace(stdout=stdout, stderr="", returncode=rc)


def test_a_TIMEOUT_makes_the_enumeration_unknown_rather_than_clean(tmp_path):
    """The driven reproduction the marker named. Mutation: restore the bare `pass` and this returns
    `[]`, which the fingerprint reads as a clean tree and records byte-equal to one."""
    def _timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=10)

    assert _dirty([str(tmp_path)], _timeout) is None, (
        "a source whose status could not be determined must not be reported as clean — that is how "
        "a wedged mount got certified SAME across a substrate change")


def test_a_MISSING_git_binary_is_unknown_too(tmp_path):
    """Mutation: catch only TimeoutExpired and an EIO/ENOENT quietly reads as clean again."""
    def _missing(*_a, **_k):
        raise FileNotFoundError("git")

    assert _dirty([str(tmp_path)], _missing) is None


def test_ONE_unreadable_source_poisons_the_whole_answer(tmp_path):
    """Partial knowledge is not knowledge here: the fingerprint is one digest over all sources, so a
    reading that silently omits one is a claim about a substrate nobody saw.

    Mutation: return the sources that DID read and a run with one wedged mount records a fingerprint
    that looks complete."""
    calls = {"n": 0}

    def _one_bad(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(stdout=" M a.py\n", stderr="", returncode=0)
        raise OSError("EIO")

    assert _dirty([str(tmp_path / "a"), str(tmp_path / "b")], _one_bad) is None


def test_a_CLEAN_tree_still_answers_an_empty_list(tmp_path):
    """The regression guard: `None` must mean unreadable, never merely empty. Mutation: return None
    whenever `out` is falsy and every clean run becomes 'unknown', which would make the
    distinction useless in the other direction."""
    assert _dirty([str(tmp_path)], _ok(stdout="")) == []


def test_a_NON_REPO_source_is_clean_and_not_unknown(tmp_path):
    """The stated residue, pinned so the narrowing is deliberate rather than forgotten: `git status`
    exits 128 in a plain data mount, and that is the ordinary case, not a failure.

    Mutation: treat any nonzero exit as unreadable and nearly every run — every one with a data
    mount — reports its substrate as unknown."""
    assert _dirty([str(tmp_path)], _ok(stdout="fatal: not a git repository\n", rc=128)) == []


def test_the_fingerprint_takes_its_UNKNOWN_branch_on_that_answer(tmp_path):
    """End to end: the point of returning None is the branch it reaches.

    Mutation: have `substrate_fingerprint` treat None as empty and the honest answer is discarded one
    layer above the fix."""
    from looplab.engine.workspace import WorkspaceSeeder

    class _W:
        """The three members `substrate_fingerprint` actually reads. Named from the real method
        rather than guessed — the first cut omitted `workspace_fingerprint` and failed on it."""

        _e = SimpleNamespace(_dirty_inputs=lambda _s: None)

        def workspace_source_paths(self):
            return [str(tmp_path)]

        def workspace_fingerprint(self):
            return {"head": "abc123"}

    out = WorkspaceSeeder.substrate_fingerprint(_W())
    assert out.get("dirty") == "unknown", f"expected the unknown branch, got {out}"
