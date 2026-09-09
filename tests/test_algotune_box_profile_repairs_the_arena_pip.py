"""The box profile's pip repair must actually LOOK at the arena's interpreter.

THE DEFECT, closed 2026-09-08. `benchmarks/box-jhub-l40s.sh::_algotune_ensure_pip` defaulted its
interpreter to `$ROOT/AlgoTune/.venv/bin/python` — and the profile defines `BENCH_ROOT` and
`ALGOTUNE_ROOT`, never `ROOT`. The path expanded to `/AlgoTune/.venv/bin/python`, `[ -x ]` failed,
the function returned 0, and nothing said a word: the miss path WAS the silent success path.

WHAT THAT COSTS is in the function's own comment, measured 2026-08-28 over a 19-run corpus:
`evaluate_results.py` runs `python -m pip install .` over any candidate carrying a `setup.py`, the
arena's uv-created venv ships no pip, so the whole branch answers `No module named pip` and the
evaluator turns that into `compilation_failed` / `speedup 0.0`. Eight independent runs wrote
`.pyx` + `setup.py`, hit it, and DELETED their extension within 0.2-2.4 minutes; the ones that got
through scored 204-261 against 27.2-48.8 for the ones that gave up. The published champion for this
benchmark ships as `.pyx` + `setup.py`. So a repair that silently does nothing after every container
restart is a 5-9x hole in arm B's numbers.

HOW THIS IS TESTED. The profile is SOURCED in a subshell against a synthetic `BENCH_ROOT` holding a
stub interpreter that records every invocation. Under the old spelling the stub is never called at
all, which is the whole property — a source pin on `$ALGOTUNE_ROOT` would be satisfied by this
comment.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

PROFILE = Path(__file__).resolve().parents[1] / "benchmarks" / "box-jhub-l40s.sh"


def _bench_root(tmp_path: Path, *, with_arena: bool) -> tuple[Path, Path]:
    """A synthetic box: `$BENCH_ROOT/AlgoTune/.venv/bin/python` records how it was called."""
    root = tmp_path / "bench"
    record = tmp_path / "calls.txt"
    if with_arena:
        venv_bin = root / "AlgoTune" / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        stub = venv_bin / "python"
        # Exits non-zero for `-m pip --version` and prints nothing for the ensurepip probe, so the
        # function takes its "no pip and no bundled wheel" branch. What is under test is that it
        # asked THIS interpreter anything at all.
        stub.write_text(f'#!/bin/sh\necho "$@" >> "{record}"\nexit 1\n', encoding="utf-8")
        stub.chmod(0o755)
    else:
        root.mkdir()
    return root, record


def _source(root: Path) -> subprocess.CompletedProcess:
    """Source the profile the way an operator does, in a subshell that inherits nothing of ours."""
    return subprocess.run(
        ["bash", "-c", f'BENCH_ROOT="{root}" . "{PROFILE}"'],
        capture_output=True, text=True, timeout=120,
        # A clean-ish environment: the profile must not be reading these from the test runner.
        env={"PATH": "/usr/bin:/bin", "HOME": str(root)})


def test_the_repair_probes_the_arena_interpreter_the_profile_declares(tmp_path):
    """THE ITEM. Under `$ROOT/AlgoTune/...` this list is empty because nothing was ever run."""
    root, record = _bench_root(tmp_path, with_arena=True)
    proc = _source(root)
    assert record.exists(), (
        "the pip repair never invoked the arena's interpreter\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}")
    calls = record.read_text(encoding="utf-8").splitlines()
    assert any("pip" in call for call in calls), calls


def test_a_box_without_the_arena_says_so_instead_of_returning_silently(tmp_path):
    """The miss path was the silent success path, which is how the defect survived. A profile
    sourced before `setup_algotune.sh` has run is a normal state — and it has to be a stated one."""
    root, record = _bench_root(tmp_path, with_arena=False)
    proc = _source(root)
    assert not record.exists()
    assert "pip" in proc.stderr, proc.stderr
    assert str(root) in proc.stderr, "the sentence must name the path it looked at"


def test_the_profile_never_reads_an_undefined_ROOT(tmp_path):
    """A negative pin, which is the one tier that stays a substring: what must not come back is the
    TEXT. `ROOT` is not among the names this profile exports, so any use of it is the same bug.

    COMMENTS ARE STRIPPED FIRST, and here that is not a convenience: the fix's own comment quotes
    the wrong spelling to say what it cost, so a naive scan is red on the file that fixed it.
    """
    code = "\n".join(line for line in PROFILE.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))
    assert "export ROOT=" not in code
    assert "$ROOT/" not in code.replace("$BENCH_ROOT/", "").replace("$ALGOTUNE_ROOT/", "")
