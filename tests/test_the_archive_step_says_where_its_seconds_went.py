"""The runs-archive step is the slow half of a snapshot, and it would not say why.

Measured 2026-09-04: 121 s with no probe live, 193 s, 601 s with four, and once 1765 s — 98 % of the
1800 s interval, which is what §206's period fix was for. Three candidate causes were refuted by
measurement (0.06 ms per stat on the store, 144 MiB/s to read the whole archive, 1.05 ms per exec in
a shell that is not this sweep's), so §207 closed with the cause unknown. A fourth theory would have
been cheaper to write than this, and worth less: `archive_tree` has exactly three parts, and the
next occurrence should say which one it was.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / "benchmarks"


def _bench(tmp_path: Path, files: int = 3, sleep_in: str | None = None):
    """A toy BENCH_ROOT with one run tree, so `snapshot.sh` finds something to archive."""
    root = tmp_path / "bench"
    run = root / "model-probes" / "p1" / "runs" / "t" / "run"
    run.mkdir(parents=True)
    # `bench_run_trees` discovers a run tree by finding an `events.jsonl` in it -- a tree of
    # differently-named files is not a run tree and archive_tree never sees it.
    (run / "events.jsonl").write_text("x" * 100, encoding="utf-8")
    for i in range(files - 1):
        (run / f"spans{i}.jsonl").write_text("x" * (100 + i), encoding="utf-8")
    (root / "logs").mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (store / ".persistent-store-id").write_text("test\n", encoding="utf-8")
    env = dict(os.environ, BENCH_ROOT=str(root), SNAPSHOT_DEST=str(store / "snapshots"))
    return root, store, env


def _run_snapshot(env) -> str:
    got = subprocess.run(["bash", str(HERE / "snapshot.sh")], env=env,
                         capture_output=True, text=True, timeout=300)
    return got.stdout + got.stderr


def test_the_three_parts_are_timed_separately(tmp_path):
    _, _, env = _bench(tmp_path)
    out = _run_snapshot(env)
    got = re.search(r"(\d+)s prefix-check \+ (\d+)s cp -ru \+ (\d+)s repair", out)
    assert got, (
        "the archive step printed no breakdown; a step that takes 601 s must say which of its "
        f"three parts took them. Output:\n{out}")
    assert all(int(g) >= 0 for g in got.groups())


def test_the_breakdown_names_the_part_that_is_actually_slow(tmp_path):
    """A timer that reports the same number for every part is not a timer. Make the prefix check
    slow -- a `cmp` per file is what it costs -- and only that part must move."""
    root, store, env = _bench(tmp_path, files=2)
    # A stub `cmp` on PATH that sleeps, so the prefix-check loop is the slow one and nothing else is.
    binn = tmp_path / "bin"
    binn.mkdir()
    (binn / "cmp").write_text("#!/bin/bash\nsleep 2\nexit 1\n", encoding="utf-8")
    os.chmod(binn / "cmp", 0o755)
    # The archive must already hold the files, or the prefix check has nothing to compare.
    arch = store / "runs-archive" / "model-probes"
    shutil.copytree(root / "model-probes" / "p1", arch / "p1")
    env = dict(env, PATH=f"{binn}:{env['PATH']}")
    out = _run_snapshot(env)
    got = re.search(r"(\d+)s prefix-check \+ (\d+)s cp -ru \+ (\d+)s repair", out)
    assert got, f"no breakdown printed:\n{out}"
    pre, copy, repair = (int(g) for g in got.groups())
    assert pre >= 2, f"the slow part was the prefix check; it reported {pre}s (of {got.group(0)})"
    assert copy <= 1 and repair <= 1, (
        f"only the prefix check was made slow, but the breakdown blames {got.group(0)} -- the "
        "clock is not being reset between parts")
