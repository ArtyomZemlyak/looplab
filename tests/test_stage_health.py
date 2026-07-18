"""The built-in training-health watchdog (`runtime/sandbox._StageHealthMonitor` + `run_argv(health_check)`):
a declared stage whose loss/grad diverges to nan/inf is tree-killed EARLY instead of burning the whole
(often multi-hour) timeout, and the killed stage carries a DIVERGED marker so the agent learns the cause."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from looplab.runtime.sandbox import _StageHealthMonitor, run_argv  # noqa: E402


def test_monitor_fires_only_after_threshold():
    m = _StageHealthMonitor(threshold=3)
    assert not m.feed("{'loss': 0.5, 'grad_norm': 1.2}\n")
    assert not m.feed("{'loss': 0.0, 'grad_norm': nan}\n")
    assert not m.feed("{'loss': 0.0, 'grad_norm': nan}\n")
    assert m.feed("{'loss': 0.0, 'grad_norm': nan}\n")          # 3rd non-finite line -> fire


def test_monitor_matches_inf_and_grad_norm_variants():
    m = _StageHealthMonitor(threshold=2)
    m.feed("loss: inf\n")
    assert m.feed("grad_norm = -inf\n")                          # inf / -inf / grad_norm spelling all count


def test_monitor_no_false_positive_on_healthy_or_incidental_nan():
    m = _StageHealthMonitor(threshold=3)
    for _ in range(20):
        m.feed("step 10 loss: 0.42 grad_norm: 1.1  saved ckpt to /data/nanometer/run\n")
    assert m.hits == 0                                          # 'nan' inside a word / healthy loss never trips


def test_monitor_handles_chunk_split_across_boundary():
    m = _StageHealthMonitor(threshold=1)
    m.feed("{'loss': 0.0, 'grad_no")                            # line split mid-token across chunks
    assert m.feed("rm': nan}\n")                                # completes -> matches once


def test_monitor_handles_progress_carriage_returns_and_final_partial_line():
    m = _StageHealthMonitor(threshold=3)
    assert not m.feed("loss: nan\rgrad_norm: +inf\r")
    assert not m.feed("loss: infinity")                         # no line terminator yet
    assert m.finish()                                           # EOF makes the final record observable


def test_run_argv_kills_diverged_stage_early():
    # A stage that prints NaN loss then would sleep 60s; the watchdog must kill it in well under that.
    prog = ("import time, sys\n"
            "for i in range(1000):\n"
            "    print(\"{'loss': 0.0, 'grad_norm': nan, 'epoch': %.2f}\" % (i * 0.01)); sys.stdout.flush()\n"
            "    time.sleep(0.02)\n"
            "time.sleep(60)\n")
    t0 = time.time()
    rc, out, err, timed_out = run_argv([sys.executable, "-c", prog], "/tmp", timeout=60, health_check=True)
    dt = time.time() - t0
    assert dt < 20, f"diverged stage not killed early ({dt:.1f}s)"
    assert not timed_out                                        # a divergence kill is NOT a timeout
    assert rc != 0                                              # killed child -> non-zero -> stage-fail path
    assert "DIVERGED" in err                                    # the reason the agent reads


def test_run_argv_combines_stdout_and_stderr_health_evidence():
    # Frameworks commonly route tqdm/Lightning metrics to stderr. Threshold evidence may straddle both
    # streams, but partial records from the two streams must never be concatenated into a fake line.
    prog = ("import sys, time\n"
            "for stream in [sys.stdout, sys.stderr, sys.stdout, sys.stderr, sys.stderr]:\n"
            "    print('loss: nan', file=stream, flush=True)\n"
            "time.sleep(60)\n")
    t0 = time.time()
    rc, out, err, timed_out = run_argv(
        [sys.executable, "-c", prog], "/tmp", timeout=60, health_check=True)

    assert time.time() - t0 < 20
    assert rc != 0 and not timed_out
    assert out.count("loss: nan") == 2 and err.count("loss: nan") == 3
    assert "DIVERGED" in err


def test_run_argv_fails_closed_when_diverged_process_exits_zero_before_poll(tmp_path):
    # A fast subprocess can exit before the parent's 250 ms watchdog poll. Draining still observes all
    # records, so health divergence must override a misleading zero exit code instead of accepting it.
    prog = ("import sys\n"
            "sys.stderr.write('loss: nan\\r' * 4 + 'loss: infinity')\n"
            "sys.stderr.flush()\n")
    rc, _out, err, timed_out = run_argv(
        [sys.executable, "-c", prog], str(tmp_path), timeout=30, health_check=True)

    assert rc != 0 and not timed_out
    assert "DIVERGED" in err


def test_run_argv_healthy_stage_runs_to_completion():
    prog = ("import sys\n"
            "for i in range(5):\n"
            "    print(\"{'loss': %.3f, 'grad_norm': 1.0}\" % (0.5 - i * 0.05)); sys.stdout.flush()\n"
            "print('RECALL@100: 0.87')\n")
    rc, out, err, timed_out = run_argv([sys.executable, "-c", prog], "/tmp", timeout=30, health_check=True)
    assert rc == 0 and not timed_out
    assert "RECALL@100: 0.87" in out
    assert "DIVERGED" not in err


def test_health_check_off_by_default_never_kills():
    prog = ("import sys\n"
            "for i in range(6):\n"
            "    print(\"{'loss': 0.0, 'grad_norm': nan}\"); sys.stdout.flush()\n")
    rc, out, err, timed_out = run_argv([sys.executable, "-c", prog], "/tmp", timeout=30)  # health_check default False
    assert rc == 0 and "DIVERGED" not in err                    # no watchdog when not requested
