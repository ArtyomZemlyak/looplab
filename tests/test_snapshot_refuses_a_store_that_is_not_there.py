"""A snapshot's exit code is a claim that the bytes are safe. Three ways it lied.

All three were MEASURED on 2026-08-31, not reasoned about:

  A. `/home/jovyan/data` is a separate fuseblk (geesefs/S3) mounted over a tmpfs parent. Point the
     snapshot at a destination whose directory has vanished and `mkdir -p` recreates it, 111 MB get
     written, and it exits 0 -- onto storage that dies with the pod. This is the 2026-08-29 failure
     that cost 37 commits, wearing a success code.

  B. Two snapshots started in the same second share $STAMP and therefore share one output
     directory. Observed: rc=0 and rc=1, one 30-file tree interleaved from both, survivor reports
     success.

  C. `.env` was neither copied nor named, so a snapshot could not say which settings produced its
     numbers -- and line 77 of that file (LOOPLAB_LLM_STREAM=false) silently decides whether 28 %
     of calls die at nginx's 300 s ceiling.

Each test below reddens if its fix is removed from benchmarks/snapshot.sh.
"""
import os
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO / "benchmarks" / "snapshot.sh"


def _run(dest, env=None, timeout=600):
    e = dict(os.environ)
    e.setdefault("BENCH_ROOT", "/var/tmp/looplab-bench")
    if env:
        e.update(env)
    return subprocess.run(
        ["bash", str(SNAPSHOT), str(dest)],
        capture_output=True, text=True, timeout=timeout, env=e,
    )


def test_a_refuses_a_store_whose_sentinel_is_gone():
    """The mount is gone but the path is writable: refuse, do not write a doomed backup."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "looplab-bench"      # stands in for the unmounted volume root
        store.mkdir()
        (store / "snapshots").mkdir()
        # Non-empty and sentinel-less == what an unmounted geesefs looks like from above.
        (store / "runs-archive").mkdir()

        r = _run(store / "snapshots")

    assert r.returncode != 0, (
        "snapshot exited 0 against a store with no .persistent-store-id -- "
        "this is the 2026-08-29 evaporating-backup bug\n" + r.stdout[-2000:] + r.stderr[-2000:]
    )
    assert "not mounted" in r.stderr.lower() or "persistent-store-id" in r.stderr, (
        "refused, but not for the stated reason:\n" + r.stderr[-2000:]
    )


def test_a_adopts_a_genuinely_empty_store_and_leaves_the_sentinel():
    """A brand-new store must still work -- the check must not be a wall against first use."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "looplab-bench"
        store.mkdir()                            # empty: nothing to mistake for an unmount
        dest = store / "snapshots"

        r = _run(dest)

        assert r.returncode == 0, (
            "refused a legitimately empty new store:\n" + r.stdout[-2000:] + r.stderr[-2000:]
        )
        assert (store / ".persistent-store-id").is_file(), \
            "adopted the store but left no sentinel, so the next run will refuse it"


def test_b_two_snapshots_at_once_do_not_share_one_directory():
    """Same-second concurrency must not interleave two trees into one output directory."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "looplab-bench"
        store.mkdir()
        dest = store / "snapshots"
        dest.mkdir()
        (store / ".persistent-store-id").write_text("test")

        env = dict(os.environ)
        env.setdefault("BENCH_ROOT", "/var/tmp/looplab-bench")
        procs = [
            subprocess.Popen(["bash", str(SNAPSHOT), str(dest)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=env)
            for _ in range(2)
        ]
        outs = [p.communicate(timeout=900) for p in procs]
        rcs = [p.returncode for p in procs]

        trees = sorted(d for d in dest.iterdir() if d.is_dir() and d.name[0].isdigit())

        # Exactly one may run at a time. The other either waits and gets its own stamp, or
        # declines. What must never happen is two runs writing into one directory.
        succeeded = [i for i, rc in enumerate(rcs) if rc == 0]
        assert len(trees) >= len(succeeded) or len(succeeded) <= 1, (
            f"{len(succeeded)} runs reported success but only {len(trees)} trees exist -- "
            f"they shared a directory. rcs={rcs}\n" + str(outs)[-2000:]
        )
        for t in trees:
            assert not (t / ".partial").exists(), f"{t} left a partial marker"


def test_c_records_the_settings_but_never_the_key():
    """A measurement's configuration must be recoverable from the snapshot, minus the secret."""
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "looplab-bench"
        store.mkdir()
        dest = store / "snapshots"
        dest.mkdir()
        (store / ".persistent-store-id").write_text("test")

        r = _run(dest, env={
            "LOOPLAB_LLM_STREAM": "1",
            "LOOPLAB_LLM_API_KEY": "sk-do-not-leak-me-0123456789",
        })
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]

        trees = [d for d in dest.iterdir() if d.is_dir() and d.name[0].isdigit()]
        assert trees, "no snapshot tree"
        envfile = trees[0] / "ENVIRONMENT.txt"

        assert envfile.is_file(), (
            "no ENVIRONMENT.txt -- the snapshot again cannot say which settings produced its numbers"
        )
        body = envfile.read_text()

        assert "LOOPLAB_LLM_STREAM" in body, (
            "ENVIRONMENT.txt does not record LOOPLAB_LLM_STREAM, the one setting that decides "
            "whether 28 % of calls die at the 300 s ceiling:\n" + body[:2000]
        )
        assert "sk-do-not-leak-me-0123456789" not in body, \
            "the API key was written into a snapshot bound for S3"
        assert "chars>" in body, "nothing was redacted; the redaction path never ran"


def test_c_the_header_names_what_it_omits():
    """A silent omission is the thing being fixed; the omission must be written down."""
    head = SNAPSHOT.read_text()[:4000]
    assert ".env" in head and "NOT copied" in head, (
        "snapshot.sh's header lists what it deliberately skips but still does not name .env"
    )


def test_b2_a_taken_stamp_does_not_become_a_shared_directory():
    """The stamp is not an identity, and this is the half the concurrency test cannot see.

    Found by mutation on 2026-08-31: deleting the uniquifying loop left all four tests above green.
    The concurrency test only proves two SIMULTANEOUS runs do not collide -- the flock already
    guarantees that -- but two runs a second apart (or two under the lock, back to back) still
    resolve to the same $STAMP and, without the loop, silently write into one tree. So take the
    name first and check the snapshot goes somewhere else.
    """
    import time

    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "looplab-bench"
        store.mkdir()
        dest = store / "snapshots"
        dest.mkdir()
        (store / ".persistent-store-id").write_text("test")

        # Claim the name this run is about to want, and make it recognisable.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        squatter = dest / stamp
        squatter.mkdir()
        (squatter / "PRIOR.txt").write_text("written by an earlier snapshot\n")

        r = _run(dest)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]

        assert sorted(p.name for p in squatter.iterdir()) == ["PRIOR.txt"], (
            f"the run wrote into an existing snapshot directory: "
            f"{sorted(p.name for p in squatter.iterdir())}"
        )
        others = [d for d in dest.iterdir() if d.is_dir() and d != squatter and d.name[0].isdigit()]
        assert others, "the run reported success but produced no snapshot directory of its own"
        assert (others[0] / "PROVENANCE.txt").is_file(), \
            f"{others[0].name} is not a real snapshot"
