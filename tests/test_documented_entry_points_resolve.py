"""Every `python -m looplab.…` the docs promise must actually be runnable.

WHY THIS FILE EXISTS. On 2026-08-20 `looplab/judgebench/__main__.py` was removed from the index by a
mistaken backout, and `python -m looplab.judgebench` — documented in `docs/guide/judge-bench.md` and
the source of every measured number in that day's judge-quality work — was broken on master for
hours while the suite stayed green. Nothing went red because NO TEST INVOKED THE MODULE. An entry
point that nothing runs is indistinguishable from a deleted one until a human tries to use it, and
the docs that name it are the only thing that notices, silently and much later.

TWO CHECKS, because one of them would NOT have caught the incident that prompted the file. The
file was still on disk that whole time — `git rm --cached` untracks without deleting — so `find_spec`
would have answered "resolves" happily while a fresh clone of master had nothing. Resolution catches
a DELETION; tracking catches an UNTRACKING; only both together answer "can someone who clones this
repository run what the docs tell them to run".

The resolution half is deliberately STATIC — `find_spec`, no subprocess. Two reasons, and the second is
measured: executing four `--help` calls would make a documentation guard depend on process startup,
and on this box a 30 s subprocess `--help` timeout has already been observed to fail under load and
pass in isolation. A guard that flakes under load is one an operator learns to re-run rather than
read. Resolution is the whole property anyway: the failure this catches is ABSENCE.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# `python -m looplab.x.y` — the spelling the docs actually use.
_INVOCATION = re.compile(r"python -m (looplab(?:\.[a-z_][a-z_0-9]*)*)")

_DOC_ROOTS = ("docs", "README.md", "CLAUDE.md")


def _documented_targets() -> dict[str, list[str]]:
    """`{module: [citing file, …]}` for every `python -m looplab.…` the documentation promises."""
    found: dict[str, set[str]] = {}
    for entry in _DOC_ROOTS:
        base = ROOT / entry
        files = [base] if base.is_file() else sorted(base.rglob("*.md")) if base.is_dir() else []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:                                # pragma: no cover - unreadable doc
                continue
            for module in _INVOCATION.findall(text):
                found.setdefault(module, set()).add(str(path.relative_to(ROOT)))
    return {module: sorted(cites) for module, cites in sorted(found.items())}


def _runnable(module: str) -> bool:
    """Would `python -m <module>` resolve? A PACKAGE needs a `__main__` submodule; a module needs
    only itself. `find_spec` imports the parent package but never executes the target."""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, AttributeError, ValueError):
        return False
    if spec is None:
        return False
    if spec.submodule_search_locations is None:           # a plain module: `-m` runs it directly
        return True
    try:
        return importlib.util.find_spec(f"{module}.__main__") is not None
    except (ImportError, AttributeError, ValueError):
        return False


def test_every_documented_entry_point_resolves():
    """The rule: if the docs say to run it, it has to be there."""
    targets = _documented_targets()
    assert targets, (
        "no `python -m looplab.…` invocation was found in the documentation at all — this guard "
        "would then be vacuously true of everything, which is the exact shape it exists to catch")

    broken = {module: cites for module, cites in targets.items() if not _runnable(module)}
    assert not broken, (
        "the documentation promises entry points that do not resolve — restore the module or "
        "correct the docs:\n  " + "\n  ".join(
            f"python -m {module}   (named in {', '.join(cites)})" for module, cites in broken.items()))


def _tracked_paths() -> set[str]:
    """Repo-relative paths git has under version control. READ-ONLY: `git ls-files` reads the index
    and never takes `index.lock`, so this is safe while another process is committing — which on
    this box is the normal state, not the exception."""
    import subprocess

    proc = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                          timeout=120, check=False)
    assert proc.returncode == 0, (
        "`git ls-files` failed, so this guard cannot tell a tracked entry point from an untracked "
        f"one. That is a red, not a skip — a skip here is the silent pass the file exists to "
        f"prevent. stderr: {proc.stderr.strip()[:400]}")
    return {line for line in proc.stdout.splitlines() if line}


def test_every_documented_entry_point_is_actually_TRACKED():
    """Resolving is not enough: an untracked file still resolves for everyone who has it on disk.

    This is the check that would have caught 2026-08-20. `git rm --cached` removed
    `looplab/judgebench/__main__.py` from the index and left the bytes in place, so every local
    import kept working, every test stayed green, and only a fresh clone would have been broken. The
    guard above cannot see that; this one can.
    """
    tracked = _tracked_paths()
    assert tracked, "git reports no tracked files at all — this guard would be vacuous"

    missing = {}
    for module, cites in _documented_targets().items():
        if not _runnable(module):
            continue                                      # already reported by the test above
        spec = importlib.util.find_spec(module)
        target = spec if spec.submodule_search_locations is None else importlib.util.find_spec(
            f"{module}.__main__")
        origin = getattr(target, "origin", None)
        if not origin:
            continue
        try:
            rel = str(Path(origin).resolve().relative_to(ROOT))
        except ValueError:                                # outside the repo (an installed copy)
            continue
        if rel not in tracked:
            missing[module] = (rel, cites)

    assert not missing, (
        "the documentation promises entry points whose source is NOT under version control — they "
        "work here and are missing from a fresh clone:\n  " + "\n  ".join(
            f"python -m {module}   ({rel} untracked; named in {', '.join(cites)})"
            for module, (rel, cites) in missing.items()))


def test_the_resolution_check_can_actually_fail():
    """NON-VACUITY, driven. Without this, `_runnable` returning True unconditionally would leave the
    test above green over a tree with every entry point deleted — which is precisely the state that
    went unnoticed for hours and prompted the file."""
    assert not _runnable("looplab.this_module_does_not_exist"), (
        "a module that is not there must not resolve")
    assert not _runnable("looplab.core"), (
        "`looplab.core` is a package with no `__main__`, so `python -m looplab.core` fails — a "
        "check that calls it runnable is not asking the `-m` question")
    assert _runnable("looplab.judgebench"), (
        "`looplab.judgebench` HAS a `__main__` and must resolve — if this is red, the entry point "
        "is missing again")


def test_the_tracking_check_sees_a_file_that_is_present_but_untracked():
    """NON-VACUITY for the half that matters, driven against the real index.

    The incident state was ON DISK AND UNTRACKED, so a tracking check that merely enumerates
    something would have looked fine while being unable to detect it. This creates exactly that
    state — a real file inside the repo that git does not know about — and requires it to be absent
    from the tracked set. Without this, `_tracked_paths` returning every path under `ROOT` would pass
    the guard above over the very tree that prompted it.
    """
    tracked = _tracked_paths()
    assert "looplab/judgebench/__main__.py" in tracked, (
        "the entry point deleted on 2026-08-20 must be tracked; if this is red it was un-tracked "
        "again and a fresh clone is broken")

    probe = ROOT / "tests" / ".untracked_probe_for_entry_point_guard.py"
    probe.write_text("# transient probe; the guard must not see this as tracked\n", encoding="utf-8")
    try:
        assert str(probe.relative_to(ROOT)) not in _tracked_paths(), (
            "a file that exists on disk but is not in the index was reported as tracked — the "
            "guard cannot tell a committed entry point from one that only exists locally, which "
            "is the exact failure it was written for")
    finally:
        probe.unlink(missing_ok=True)
