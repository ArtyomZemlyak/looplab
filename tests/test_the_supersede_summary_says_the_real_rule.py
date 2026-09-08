"""The summary said "a shorter source replaced a longer archive" and that is false in the case the
mechanism exists for.

`campaign.sh` does `rm -rf "$TASK_ROOT"` at the head of every attempt, and **nothing makes attempt 2
shorter than attempt 1** — the supersede loop's own comment says exactly that, which is why it tests
whether the archive is a PREFIX of the source rather than comparing sizes. Driven end to end on
2026-09-04 against the real `snapshot.sh`: attempt 1 of 400 rows, attempt 2 of 50, attempt 3 of 50
with different content, attempt 4 of 900. All four are preserved —
`events.jsonl.superseded-1/2/3` plus the live file — and the summary called every one of them
"a shorter source", including the 900-row one. The per-file line beside it printed the true sizes,
so the summary and the detail told two different stories.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_snapshot_carries_the_repo_and_the_runs import _bench_root  # noqa: E402


def _rows(n: int, attempt: int) -> str:
    return "".join(json.dumps({"seq": i, "attempt": attempt}) + "\n" for i in range(n))


def _stand(tmp_path):
    holder = tmp_path / "holder"
    holder.mkdir(parents=True)          # `_bench_root` writes its sentinel INTO this directory
    root = Path(_bench_root(holder))
    # The DESTINATION's store root needs its own sentinel: `snapshot.sh` refuses a store that is
    # non-empty and unmarked, because an unmounted volume looks exactly like one.
    store = tmp_path / "store"
    store.mkdir(parents=True, exist_ok=True)
    (store / ".persistent-store-id").write_text("test fixture store\n", encoding="utf-8")
    env = dict(os.environ, BENCH_ROOT=str(root),
               SNAPSHOT_DEST=str(tmp_path / "store" / "snapshots"),
               CAMPAIGN_RUNS=str(root / "camp-runs"))
    return root, env


def _attempt(root, env, n: int, rows: int) -> str:
    """campaign.sh's own line: delete the task root, write a fresh log, snapshot."""
    task = root / "camp-runs" / "edge_expansion"
    if task.exists():
        import shutil
        shutil.rmtree(task)
    run = task / "run"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(_rows(rows, n), encoding="utf-8")
    got = subprocess.run(["bash", str(HERE / "snapshot.sh")], env=env,
                         capture_output=True, text=True, timeout=300)
    assert got.returncode == 0, got.stdout[-2000:] + got.stderr[-2000:]
    return got.stdout + got.stderr


def test_a_LONGER_replacement_is_not_described_as_a_shorter_source(tmp_path):
    root, env = _stand(tmp_path)
    _attempt(root, env, 1, 400)
    out = _attempt(root, env, 2, 900)
    assert "superseded" in out, f"a 900-row attempt over 400 archived rows was not preserved:\n{out}"
    summary = [l for l in out.splitlines() if "kept as .superseded-N" in l]
    assert summary, out
    assert "shorter source" not in " ".join(summary), (
        f"the summary calls a 900-row replacement a shorter source: {summary}")
    assert "not a continuation" in " ".join(summary), summary


def test_every_attempt_survives_shorter_equal_and_longer(tmp_path):
    root, env = _stand(tmp_path)
    _attempt(root, env, 1, 400)
    _attempt(root, env, 2, 50)        # shorter
    _attempt(root, env, 3, 50)        # equal length, different content
    _attempt(root, env, 4, 900)       # longer
    arc = tmp_path / "store" / "runs-archive" / "camp-runs" / "edge_expansion" / "run"
    seen = {}
    for path in arc.iterdir():
        body = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        seen[path.name] = sorted({json.loads(l)["attempt"] for l in body})
    assert seen.get("events.jsonl") == [4], seen
    assert sorted(v[0] for k, v in seen.items() if "superseded" in k) == [1, 2, 3], (
        f"an attempt was lost: {seen}")
