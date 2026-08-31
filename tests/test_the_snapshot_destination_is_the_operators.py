"""`BENCH_ROOT` moved what the snapshot READ and nothing moved where it WROTE.

`snapshot.sh:28` was `DEST="${1:-/home/jovyan/data/looplab-bench/snapshots}"`, and both of its
callers invoke it with no argument -- `snapshot_timer.sh` in its `_loop`, `campaign.sh` after an
arm. So on both paths the hardcoded persistent path was the only destination reachable, whatever
`BENCH_ROOT` said. Measured on 35124d05: `grep -rn SNAPSHOT_DEST` over the whole tree returned
nothing.

It cost something on 2026-08-31. An agent started `snapshot_timer.sh` against a synthetic
`BENCH_ROOT` to exercise it; the timer read the fake tree and wrote a snapshot of it into the LIVE
rotation on the persistent mount, beside the real ones, where it had to be found and deleted by
hand. Nothing inside the snapshot said which root it had been taken from, so "found" meant reading
the contents and inferring.

Two claims here, and they are the two halves of that incident: the destination is a variable, and
a snapshot says where it came from.

WHY THE MUTATION FOR THESE IS RUN AGAINST A COPY OF THE SCRIPT: reverting the fix in place means a
test process writing into `/home/jovyan/data/looplab-bench/snapshots` -- the production rotation,
and the exact damage under discussion. The mutation was driven on 2026-08-31 against a copy whose
hardcoded fallback pointed at a scratch directory instead; with `${SNAPSHOT_DEST:-...}` removed,
both tests below go red, having written to the fallback rather than to the destination they asked
for.
"""
import subprocess
from pathlib import Path

from tests.test_snapshot_carries_the_repo_and_the_runs import SNAPSHOT, _bench_root

TIMER = Path(__file__).resolve().parents[1] / "benchmarks" / "snapshot_timer.sh"
TREES = Path(__file__).resolve().parents[1] / "benchmarks" / "bench_trees.sh"


def _env(src, archive, **extra):
    return {"PATH": "/usr/bin:/bin", "HOME": str(src.parent), "BENCH_ROOT": str(src),
            "SNAPSHOT_RUNS_ARCHIVE": str(archive), **extra}


def test_the_destination_follows_the_environment_when_no_caller_passes_one(tmp_path):
    """The shape both callers are in: `snapshot.sh` with no argument at all."""
    src = _bench_root(tmp_path)
    dest, archive = tmp_path / "snapshots", tmp_path / "runs-archive"

    result = subprocess.run(["bash", str(SNAPSHOT)], capture_output=True, text=True, timeout=300,
                            env=_env(src, archive, SNAPSHOT_DEST=str(dest)))
    assert result.returncode == 0, result.stdout + result.stderr

    written = sorted(dest.glob("2*"))
    assert written, (
        "no argument was passed, so the snapshot went wherever the script hardcodes -- which is "
        "this box's live rotation, whatever BENCH_ROOT said\n" + result.stdout)
    assert (written[0] / "looplab.bundle").exists(), result.stdout


def test_an_explicit_argument_still_beats_the_environment(tmp_path):
    """Precedence, stated: argument, then environment, then the box default. A caller that names a
    destination is the one that knows."""
    src = _bench_root(tmp_path)
    named, from_env = tmp_path / "named", tmp_path / "from-env"

    result = subprocess.run(["bash", str(SNAPSHOT), str(named)], capture_output=True, text=True,
                            timeout=300,
                            env=_env(src, tmp_path / "runs-archive", SNAPSHOT_DEST=str(from_env)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(named.glob("2*")), result.stdout
    assert not from_env.exists(), result.stdout


def test_the_snapshot_records_which_root_it_was_taken_from(tmp_path):
    """"Is this one mine?" -- the restorer's first question, and the archive had no answer.

    A snapshot of a synthetic root sitting in the live rotation is indistinguishable from a real one
    until somebody reads its contents; PROVENANCE.txt already names both checkouts' commits and the
    box's cpu/memory, and named neither end of the copy it is a receipt for.
    """
    src = _bench_root(tmp_path)
    dest = tmp_path / "snapshots"

    result = subprocess.run(["bash", str(SNAPSHOT), str(dest)], capture_output=True, text=True,
                            timeout=300, env=_env(src, tmp_path / "runs-archive"))
    assert result.returncode == 0, result.stdout + result.stderr

    prov = (next(dest.glob("2*")) / "PROVENANCE.txt").read_text()
    assert f"bench root: {src}" in prov, (
        "the snapshot does not say what it is a snapshot OF; a foreign one in this rotation can "
        "only be identified by reading what is inside it\n" + prov)
    assert f"destination: {dest}" in prov, prov


# ------------------------------------------------------------------------ and through the TIMER
#
# The claim above is about `snapshot.sh`. The incident was about `snapshot_timer.sh`, which is a
# different thing: it re-execs itself with `setsid nohup` into a detached loop, and the question is
# whether the destination survives that. It is driven here for real -- the actual timer, the actual
# `snapshot.sh`, a one-second interval -- because "it inherits the environment" is the kind of claim
# that reads as obviously true and is what the `_loop` re-exec exists to break.


def test_a_timer_started_against_a_scratch_root_writes_only_to_the_scratch_destination(tmp_path):
    import os
    import signal
    import time

    binroot = tmp_path / "bin"
    binroot.mkdir()
    for f in (TIMER, SNAPSHOT, TREES):
        (binroot / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        (binroot / f.name).chmod(0o755)   # `_loop` execs its sibling, it does not `bash` it
    src = _bench_root(tmp_path)
    dest = tmp_path / "scratch-snapshots"

    proc = subprocess.Popen(
        ["bash", str(binroot / "snapshot_timer.sh"), "_loop", "1"],
        env={**os.environ, "BENCH_ROOT": str(src), "SNAPSHOT_DEST": str(dest),
             "SNAPSHOT_RUNS_ARCHIVE": str(tmp_path / "runs-archive")},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        # Waited on PROVENANCE.txt, not on the directory: `mkdir -p "$OUT"` is the FIRST thing the
        # snapshot does, so a directory here means "started", and killing the loop at that moment
        # leaves a half-written snapshot the assertions below cannot read.
        end = time.time() + 60
        while time.time() < end and not sorted(dest.glob("2*/PROVENANCE.txt")):
            time.sleep(0.1)
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        out = proc.communicate(timeout=30)[0]

    written = sorted(dest.glob("2*"))
    assert written, (
        "the timer honoured BENCH_ROOT for what it read and ignored it for where it wrote, so the "
        "snapshot of this scratch tree went into the box's live rotation instead\n" + out)
    prov = (written[0] / "PROVENANCE.txt").read_text()
    assert f"bench root: {src}" in prov, prov
