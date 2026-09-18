"""A synthetic `BENCH_ROOT`, shared by every test that drives `benchmarks/snapshot.sh`.

WHY IT IS SHARED RATHER THAN COPIED. `test_snapshot_carries_the_repo_and_the_runs.py` built one of
these and `test_snapshot_refuses_a_store_that_is_not_there.py` did not — it pointed the script at
the BOX's `/var/tmp/looplab-bench` through an `os.environ.setdefault`. On the bench stand that root
exists and eleven of its tests passed; anywhere else the script reported six MISSING sources, exited
1, and eleven assertions about refusals, locks and the environment record went red for a reason
none of them is about. Measured 2026-09-07 on a box with no arena: 11 of 25 red, all of them
`INCOMPLETE SNAPSHOT`.

A test whose subject is "what does this script REFUSE" must own its inputs; borrowing the box's is
how it comes to depend on a machine. And the second copy is the defect CLAUDE.md §0.8 records four
times over — two builders of one fixture drift, and the one that drifts is the one nobody runs.

`snapshot.sh`'s own comments already treat a synthetic root as the way to drive it ("Driven on a
synthetic BENCH_ROOT with `AlgoTune/.git` deleted and every other source in place").
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          cwd=cwd, check=True, capture_output=True, text=True)


def bench_root(tmp_path) -> Path:
    """A BENCH_ROOT shaped like the real one on the morning of the loss.

    `tmp_path` doubles as the STORE root, so it is stamped: `snapshot.sh` refuses a destination
    whose store root carries no `.persistent-store-id`, because an unmounted geesefs looks exactly
    like a writable empty directory and a backup written there dies with the pod while exiting 0
    (measured 2026-08-31 — the 2026-08-29 loss in miniature). These fixtures ARE a legitimate store,
    so they say so once, here; the refusal itself is covered by
    `test_snapshot_refuses_a_store_that_is_not_there.py`.
    """
    (tmp_path / ".persistent-store-id").write_text("test fixture store\n")

    src = tmp_path / "bench"

    # Both checkouts. The third-party one was always bundled; ours never was.
    for name, subject in (("AlgoTune", "the ruler generation lives in the key"),
                          ("looplab", "the commit the restart was about to eat")):
        repo = src / name
        repo.mkdir(parents=True)
        (repo / "kept.txt").write_text(f"{name} tracked content\n")
        _git(repo, "init", "-q")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", subject)
        # A SECOND BRANCH THE CHECKOUT IS NOT ON. `snapshot.sh` bundles with `--all`, and which
        # branch `git clone <bundle>` lands on is then a question -- the very question the known
        # failure is about ("the clone yields the wrong tree"). With one branch that question cannot
        # be asked: every clone lands right by having nowhere else to go, so the restore test
        # passed for a reason unrelated to what it guards. The live repo carries four local branches.
        _git(repo, "checkout", "-q", "-b", "not-the-one")
        (repo / "kept.txt").write_text(f"{name} content from the WRONG branch\n")
        _git(repo, "commit", "-qam", "a branch the restore must not land on")
        _git(repo, "checkout", "-q", "-")

    # An uncommitted edit, so "(0 dirty files)" is a claim the archive can be checked against.
    (src / "looplab" / "kept.txt").write_text("looplab tracked content\nan edit nobody committed\n")

    # The `.env` the environment record reads (`snapshot.sh` looks for `$SRC/looplab/.env`). It is
    # here because a record that names only the LIVE environment cannot show the two-values-for-one-
    # setting case the "ON DISK ONLY" / "THIS IS THE ONE IN FORCE" markers exist for — and that is
    # the whole of finding C, `.env` line 77 (`LOOPLAB_LLM_STREAM=false`) silently deciding whether
    # 28 % of calls die at nginx's 300 s ceiling. The secret is real in SHAPE so the redactor has
    # something to refuse: this file is never copied into a snapshot, only read.
    (src / "looplab" / ".env").write_text(
        "LOOPLAB_LLM_STREAM=false\n"
        "OPENAI_API_KEY=sk-fixture000000000000000000000000000000000000000\n")

    # The sources the script already knew about, so a MISSING line for one of them cannot be what
    # makes a test red.
    (src / "looplab" / "benchmarks" / "algotune").mkdir(parents=True)
    (src / "looplab" / "benchmarks" / "algotune" / ".baseline_times").mkdir()
    (src / "AlgoTune" / "reports").mkdir()
    (src / "meter").mkdir()
    (src / "logs").mkdir()
    campaign = src / "campaign-final"
    campaign.mkdir()
    (campaign / "B-pde_heat1d.final.json").write_text('{"speedup": 99.0029}\n')

    # A probe mid-flight: the shape whose loss cost sixty-nine runs.
    run = src / "model-probes" / "dsPde3" / "runs" / "r1" / "run"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text('{"type": "llm_usage", "data": {"cost": 0.0717}}\n')
    (run / "spans.jsonl").write_text('{"name": "generation", "attributes": {"phase": "propose"}}\n')
    return src


# ---------------------------------------------------------------------------------------------
# THE ENVIRONMENT A LAUNCH HAS, for the tests that drive `run_probe.sh` on the real stand.

STAND_ROOT = Path("/var/tmp/looplab-bench")


def stand_launch_env(env: dict | None = None) -> dict:
    """`env` with the stand's own interpreter first on PATH, the way a launch has it.

    `run_probe.sh` calls `python -m looplab.cli run` and refuses when that `python` cannot import
    the CLI (`test_the_probe_refuses_a_stand_that_cannot_run.py` — the guard that ends the
    2026-09-18 failure where the probe checked the fence, built the card and wrote the instrument
    record before dying on `No module named 'typer'`). A launch gets that interpreter from
    `benchmarks/box-jhub-l40s.sh`; pytest does not source it, so a harness that drives the script
    for its RECORD-writing must supply the same precondition or it tests the refusal instead.

    ONE builder, because two would drift and the one that drifts is the one nobody runs -- the
    rule this module's own docstring was written for. Sourcing the box profile instead was
    rejected: it reads `$ALGOTUNE_ROOT/.env`, and three tests in this suite exist to prove that
    no key from the operator's shell reaches the record.
    """
    out = dict(os.environ if env is None else env)
    venv = STAND_ROOT / "looplab" / ".venv" / "bin"
    if venv.is_dir():
        out["PATH"] = f"{venv}{os.pathsep}" + out.get("PATH", "")
    return out


# The four bench lanes of this box: eleven cores each plus their hyperthread siblings, 22 CPUs, so
# `ALGOTUNE_EVAL_WORKERS=auto` keys `__w22x1r3` -- the regime the whole corpus was measured in.
BENCH_LANES = ("0-10,48-58", "11-21,59-69", "22-32,70-80", "33-43,81-91")


def free_bench_lane(pytest_module=None) -> str:
    """A bench lane with no probe on it, or skip.

    WHY A TEST THAT WRITES A PROBE RECORD MAY NOT USE THE SERVICE LANE. `run_probe.sh` refuses a
    lane whose width keys a baseline the box does not have (2026-09-18: `0-10` is 11 CPUs and keys
    `__w11x1r3`, which no cache answers, and the evaluation found out 50 minutes and $0.2176 in).
    The service lane `44-47,92-95` is 8 CPUs and keys `__w8x1r3` -- so these tests used to write
    their record on a lane no real probe could ever run on, and the day the launcher started
    checking, seven of them went red at once.

    A dry run still refuses there, deliberately: a rehearsal that passes on a lane a launch would
    refuse is a rehearsal that lies. So the tests move onto the real lanes and say so when they
    cannot -- a skip during a live campaign is honest, a record taken in an impossible regime is
    not.
    """
    import subprocess as _sp
    try:
        out = _sp.run(["ps", "-eo", "args", "--no-headers"], capture_output=True, text=True,
                      timeout=60).stdout
    except (OSError, _sp.SubprocessError):
        out = ""
    for lane in BENCH_LANES:
        if f"-c {lane}" not in out and f'"{lane}"' not in out and f" {lane} " not in out:
            return lane
    if pytest_module is not None:
        pytest_module.skip("every bench lane is busy; a probe record cannot be taken on one")
    raise RuntimeError("every bench lane is busy")
