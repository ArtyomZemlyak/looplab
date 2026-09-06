"""An ARM may not launch without a preregistration; a CONTROL needs none.

docs/60 §60.9 A4, from §187 and §195 of docs/56: the arm of §115 was sized at twelve a side from a
power table computed on a different outcome and closed at p = 0.1341 having answered nothing; 24
probes bought a power of 0.24; four separate arms reached p ≈ 0.1 on n = 3 and collapsed on the
fourth point (docs/58 §58.3). `benchmarks/arm_power.py` exists so the size is computed BEFORE the
money, and `INSTRUMENT.txt` exists so the arm is readable afterwards -- but nothing made either
happen. `run_probe.sh::require_preregistration` now does: any launch whose `INSTRUMENT.txt` would
carry `card_args:` or `cli_settings:` other than the shipped default (`PROBE_MAKE_TASK_ARGS` or
`PROBE_LOOPLAB_SETTINGS` set) refuses, exit 4, unless the probe directory already holds a
`PREREGISTERED.txt` naming the primary outcome, the size in `arm_power.py`'s own unit (paired
batches), the power at that size and the `arm_power.py` command line that produced it.

The function is EXTRACTED from the launcher and driven (the launcher itself needs the bench stand
two lines further down), and the gate's position and the instrument's new line are checked on the
script. One refusal is also driven through the real script, because it fires before the stand is
touched and therefore runs on any box.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "run_probe.sh"

GOOD = ("primary_outcome: final champion TEST speedup on edge_expansion\n"
        "batches: 9\n"
        "power: 0.81\n"
        "arm_power: python benchmarks/arm_power.py --effect 23.5 --batches 9 --trials 400\n")


def _harness() -> str:
    src = LAUNCHER.read_text(encoding="utf-8")
    found = re.search(r"^require_preregistration\(\) \{.*?^\}$", src, re.M | re.S)
    assert found, "run_probe.sh no longer defines require_preregistration()"
    assert len(found.group(0).splitlines()) > 5, "extracted as an empty body"
    return "set -u\n" + found.group(0) + "\n"


def _gate(probe_dir: Path, **env: str) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith("PROBE_")}
    return subprocess.run(["bash", "-c", _harness() + f'require_preregistration "{probe_dir}"'],
                          capture_output=True, text=True, timeout=60, env={**base, **env})


def test_a_control_on_the_shipped_card_and_settings_needs_nothing(tmp_path):
    """The baseline every arm is preregistered against; an empty, even absent, probe dir is fine."""
    assert _gate(tmp_path / "never-made").returncode == 0


def test_a_card_variant_is_an_arm_and_is_refused_without_the_file(tmp_path):
    got = _gate(tmp_path, PROBE_MAKE_TASK_ARGS="--no-unteachable-rules")
    assert got.returncode == 4, got.stderr
    assert "ARM launch" in got.stderr and "PREREGISTERED.txt" in got.stderr
    assert "arm_power.py" in got.stderr, "the refusal does not say where the power comes from"
    assert "primary_outcome:" in got.stderr and "batches:" in got.stderr and "power:" in got.stderr


def test_a_settings_variant_is_an_arm_too(tmp_path):
    got = _gate(tmp_path, PROBE_LOOPLAB_SETTINGS="-s developer_probe_max_calls=12")
    assert got.returncode == 4, got.stderr


def test_a_complete_preregistration_admits_the_arm(tmp_path):
    (tmp_path / "PREREGISTERED.txt").write_text(GOOD)
    assert _gate(tmp_path, PROBE_MAKE_TASK_ARGS="--no-unteachable-rules").returncode == 0
    assert _gate(tmp_path, PROBE_LOOPLAB_SETTINGS="-s x=1").returncode == 0


def test_each_missing_or_malformed_line_is_named(tmp_path):
    cases = {
        "primary_outcome": GOOD.replace("primary_outcome: final champion TEST speedup on edge_expansion\n",
                                        "primary_outcome:\n"),
        "batches": GOOD.replace("batches: 9\n", "batches: nine\n"),
        "power": GOOD.replace("power: 0.81\n", "power: 81%\n"),
        "arm_power": GOOD.replace("arm_power: python benchmarks/arm_power.py --effect 23.5 --batches 9 "
                                  "--trials 400\n", "arm_power: I computed it\n"),
    }
    for key, text in cases.items():
        (tmp_path / "PREREGISTERED.txt").write_text(text)
        got = _gate(tmp_path, PROBE_MAKE_TASK_ARGS="--x")
        assert got.returncode == 4, (key, got.stderr)
        assert "incomplete" in got.stderr and key in got.stderr, (key, got.stderr)
    # a power outside [0, 1] is malformed too
    (tmp_path / "PREREGISTERED.txt").write_text(GOOD.replace("power: 0.81\n", "power: 1.5\n"))
    assert _gate(tmp_path, PROBE_MAKE_TASK_ARGS="--x").returncode == 4
    (tmp_path / "PREREGISTERED.txt").write_text(GOOD.replace("power: 0.81\n", "power: 1.0\n"))
    assert _gate(tmp_path, PROBE_MAKE_TASK_ARGS="--x").returncode == 0


def test_an_empty_file_is_no_preregistration(tmp_path):
    (tmp_path / "PREREGISTERED.txt").write_text("")
    assert _gate(tmp_path, PROBE_MAKE_TASK_ARGS="--x").returncode == 4


# ------------------------------------------------------------------------------------------------
# the script around the function
# ------------------------------------------------------------------------------------------------

def test_the_launcher_is_still_valid_shell():
    done = subprocess.run(["bash", "-n", str(LAUNCHER)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_the_gate_sits_above_everything_that_touches_the_stand():
    """Source order: the call comes before the stand is entered, before the fence check and before
    the dry-run gate -- an arm with no preregistration has nothing to record yet."""
    src = LAUNCHER.read_text(encoding="utf-8")
    i_call = src.index('require_preregistration "$OUT" || exit $?')
    assert i_call < src.index('cd "$ROOT/looplab"')
    assert i_call < src.index("fence_foreign_results.sh")
    assert i_call < src.index('mkdir -p "$OUT/ws"')
    assert i_call < src.index("PROBE_DRY_RUN:-0")
    assert i_call < src.index('} > "$OUT/INSTRUMENT.txt"')


def test_the_instrument_records_the_preregistration_or_says_none_was_required():
    src = LAUNCHER.read_text(encoding="utf-8")
    block = src[src.index('echo "probe:          $LABEL"'):src.index('} > "$OUT/INSTRUMENT.txt"')]
    assert 'preregistered:  $OUT/PREREGISTERED.txt sha256=' in block, block
    assert "preregistered:  (not required" in block, block


def test_the_real_launcher_refuses_an_arm_before_touching_the_stand(tmp_path):
    """Driven through the script itself: the refusal lands before `cd "$ROOT/looplab"`, so it
    runs on a box with no bench stand at all, leaves no tree behind and spends nothing."""
    base = {k: v for k, v in os.environ.items() if not k.startswith("PROBE_")}
    got = subprocess.run(
        ["bash", str(LAUNCHER), "deepseek-v4-flash", "t_prereg_arm", "0-1", "svm",
         "http://127.0.0.1:1", "1.00"],
        capture_output=True, text=True, timeout=120,
        env={**base, "PROBE_OUT_ROOT": str(tmp_path), "PROBE_MAKE_TASK_ARGS": "--no-unteachable-rules"})
    assert got.returncode == 4, got.stdout + got.stderr
    assert "PREREGISTERED.txt" in got.stderr
    assert not (tmp_path / "t_prereg_arm").exists(), "the refusal left a probe tree behind"
