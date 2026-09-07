"""Editing `run_probe.sh` while probes are using it made one of them run its BODY TWICE.

Bash reads a script BY OFFSET as it executes. `run_probe.sh` already wrapped its body in `main()`
for this reason, with `main "$@"` and `exit "$?"` below it described in a comment as "the second half
of the lock". Those are TWO commands: between them the shell returns to the file at a saved offset,
and if the file grew meanwhile the offset lands mid-line in a different statement. The lock did not
hold.

Reproduced 2026-09-01 on a minimal script — insert 40 lines mid-function while it sleeps:

    BODY START / BODY END / "padding: command not found" / BODY START / BODY END / syntax error

The body ran a SECOND time. That is the 2026-08-27 loss, when a probe spent $0.0743 past its ceiling
right after the engine had honestly hit it and exited. It happened again the same day this test was
written: I edited the file while four probes were running it, and `remEEctl1` finished with
`run_probe.sh: line 291: -c: command not found`.

Two independent repairs, both measured here, because either alone leaves a hole:
  * `{ … }` around the whole script — bash must parse the entire compound command before executing
    any of it, so a later edit cannot change what runs.
  * an ATOMIC replace when editing — a new inode leaves the running shells reading the old one.
Without the wrap, an in-place edit re-runs the body. Without the atomic replace, the wrap protects
only scripts edited after this commit.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROBE = REPO / "benchmarks" / "algotune" / "run_probe.sh"

_VICTIM = textwrap.dedent("""\
    #!/bin/bash
    %s
    main() {
      echo "BODY"
      sleep 3
    }
    main "$@"
    exit "$?"
    %s
    """)

_GROWER = textwrap.dedent("""\
    import os, pathlib, time
    time.sleep(1)
    p = pathlib.Path(%r)
    s = p.read_text().replace('  echo "BODY"',
                              "\\n".join("  # pad %%d" %% i for i in range(40)) + '\\n  echo "BODY"', 1)
    %s
    """)


def _drive(tmp_path, *, wrapped: bool, atomic: bool):
    victim = tmp_path / "victim.sh"
    victim.write_text(_VICTIM % ("{" if wrapped else "", "}" if wrapped else ""))
    write = ("tmp = p.with_suffix('.tmp'); tmp.write_text(s); os.replace(tmp, p)"
             if atomic else "p.write_text(s)")
    grower = tmp_path / "grow.py"
    grower.write_text(_GROWER % (str(victim), write))
    g = subprocess.Popen(["python3", str(grower)], cwd=tmp_path)
    r = subprocess.run(["bash", str(victim)], capture_output=True, text=True, timeout=300)
    g.wait(timeout=300)
    return r


def test_an_unwrapped_script_edited_in_place_RE_RUNS_ITS_BODY(tmp_path):
    """The defect itself, driven end to end. If this ever stops reproducing, the two repairs below
    are protecting against nothing and the tests for them mean nothing either."""
    r = _drive(tmp_path, wrapped=False, atomic=False)
    assert r.stdout.count("BODY") >= 2, (
        "the hazard did not reproduce, so the repairs below are untested:\n" + r.stdout + r.stderr
    )


def test_the_brace_wrap_stops_it(tmp_path):
    r = _drive(tmp_path, wrapped=True, atomic=False)
    assert r.stdout.count("BODY") == 1, r.stdout + r.stderr
    assert r.returncode == 0, r.stderr


def test_an_atomic_replace_stops_it_even_unwrapped(tmp_path):
    """The operational half: a new inode leaves the running shell reading the old file to the end."""
    r = _drive(tmp_path, wrapped=False, atomic=True)
    assert r.stdout.count("BODY") == 1, r.stdout + r.stderr
    assert r.returncode == 0, r.stderr


def test_run_probe_is_wrapped(tmp_path):
    """The real script, structurally: first non-shebang line opens the block, last closes it."""
    lines = [ln for ln in PROBE.read_text().splitlines() if ln.strip()]
    assert lines[0].startswith("#!"), lines[0]
    assert lines[1].startswith("{"), (
        "run_probe.sh is not wrapped in a single compound command:\n  " + lines[1]
    )
    assert lines[-1].strip() == "}", lines[-1]
    assert subprocess.run(["bash", "-n", str(PROBE)], capture_output=True).returncode == 0
