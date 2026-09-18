"""The arm driver takes its size from the preregistration, crosses its lanes, and reads no outcome.

WHY THERE IS A DRIVER AT ALL. Until 2026-09-18 there was none: §137's arm and the probe-cap arm were
run by hand. B1 is twelve batches — 48 probes and $48 — and every one of them needs its condition,
its lane and its own copy of the preregistration. A hand doing that 48 times will get one wrong, and
it will get it wrong SILENTLY, because a probe on the wrong side of the arm looks exactly like a
probe.

The three properties here are the ones that make the driver worth trusting more than the hand.

* **The size is read, never passed.** An argument that can set `batches` is a knob someone will
  turn to a number the data have already suggested. The registered file is the only source, and
  changing it leaves a mark in git.
* **The lanes cross.** §266: over 37 probes of the old arm, 17 of 18 treated ran on the first two
  lanes and 18 of 19 controls on the last two, while §190's test permutes LABELS inside a batch —
  valid only if the four probes are exchangeable, which nothing had shown. A ruler reading gave
  +4.5 % for the treated lanes at p = 0.016, and the ordering scrambled on the next sitting (sign
  test p = 0.34 over six). Not proven, not excludable. Crossing by batch parity makes the confound
  estimable at no cost.
* **It reads nothing.** Both preregistrations end "READ ONCE, at N complete batches. No interim
  look." A driver that prints a p-value between batches IS the interim look.

Driven against a stub `run_probe.sh`, so the real dispatch logic runs while nothing is spent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DRIVER = REPO / "benchmarks" / "algotune" / "run_arm.sh"
LANES = ("0-10,48-58", "11-21,59-69", "22-32,70-80", "33-43,81-91")

PRE = """primary_outcome: whether the thing happened
batches: 4
power: 0.85
arm_power: benchmarks/arm_power.py --outcome rate --pooled

TASK                edge_expansion

    PROBE_LOOPLAB_SETTINGS="--set exploit_strong_node_quantile=0.75"

READ ONCE, at four complete batches. No interim look.
"""

STUB = """#!/bin/bash
# A stand-in for run_probe.sh: records what it was handed and writes the marker the driver skips on.
OUT="$PROBE_OUT_ROOT/$2"
mkdir -p "$OUT"
{
  echo "model=$1 label=$2 lane=$3 task=$4 budget=$6"
  echo "settings=${PROBE_LOOPLAB_SETTINGS:-(none)}"
  echo "pre_exists=$([ -s "$OUT/PREREGISTERED.txt" ] && echo yes || echo no)"
} > "$OUT/INSTRUMENT.txt"
echo "ИТОГ: stub" > "$OUT/probe.log"
"""


def _stand(tmp_path: Path, pre: str = PRE) -> Path:
    """A copy of the driver with a stub probe beside it, which is how `$HERE` resolves."""
    here = tmp_path / "algotune"
    here.mkdir()
    shutil.copy(DRIVER, here / "run_arm.sh")
    (here / "PREREGISTERED-T1.txt").write_text(pre, encoding="utf-8")
    (here / "run_probe.sh").write_text(STUB, encoding="utf-8")
    (here / "run_probe.sh").chmod(0o755)
    return here


def _run(here: Path, out_root: Path, **env_extra) -> subprocess.CompletedProcess:
    env = dict(os.environ, ARM="T1", LOOPLAB_LLM_MODEL="stub-model",
               ARM_OUT_ROOT=str(out_root), METER_BASE="http://127.0.0.1:1")
    env.update(env_extra)
    return subprocess.run(["bash", str(here / "run_arm.sh")], capture_output=True, text=True,
                          timeout=600, env=env)


def _manifest(out_root: Path) -> list[dict]:
    return [json.loads(l) for l in (out_root / "T1" / "arm.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]


def test_without_the_go_it_spends_nothing_and_says_what_it_would(tmp_path):
    here = _stand(tmp_path)
    out = tmp_path / "out"
    got = _run(here, out)
    assert got.returncode == 0, got.stdout + got.stderr
    assert "16 проб" in got.stdout and "$16.00" in got.stdout, got.stdout
    assert not (out / "T1").exists(), "a show wrote into the arm directory"


def test_the_size_and_the_treatment_come_from_the_registered_file(tmp_path):
    """4 batches and that one setting are in the file and nowhere else; the driver takes no
    argument that could say otherwise."""
    here = _stand(tmp_path)
    got = _run(here, tmp_path / "out")
    assert "--set exploit_strong_node_quantile=0.75" in got.stdout
    src = DRIVER.read_text(encoding="utf-8")
    assert 'BATCHES=$(sed -n' in src and "batches:" in src
    assert "ARM_BATCHES" not in src, "a size override is a knob someone will turn after the fact"


def test_a_missing_preregistration_refuses(tmp_path):
    here = _stand(tmp_path)
    (here / "PREREGISTERED-T1.txt").unlink()
    got = _run(here, tmp_path / "out", ARM_GO="1")
    assert got.returncode == 2 and "предрегистрации" in got.stderr, got.stdout + got.stderr


def test_every_probe_gets_its_condition_lane_and_its_own_preregistration(tmp_path):
    here = _stand(tmp_path)
    out = tmp_path / "out"
    got = _run(here, out, ARM_GO="1")
    assert got.returncode == 0, got.stdout + got.stderr
    rows = _manifest(out)
    assert len(rows) == 16, rows
    assert sorted(r["condition"] for r in rows).count("treat") == 8
    for r in rows:
        rec = (out / "T1" / r["label"] / "INSTRUMENT.txt").read_text(encoding="utf-8")
        assert f"lane={r['lane']}" in rec, (r, rec)
        assert "pre_exists=yes" in rec, "the probe did not get its own PREREGISTERED.txt"
        if r["condition"] == "treat":
            assert "settings=--set exploit_strong_node_quantile=0.75" in rec, rec
        else:
            assert "settings=(none)" in rec, "a control was handed the treatment:\n" + rec


def test_the_lanes_cross_so_each_condition_sees_each_lane_equally(tmp_path):
    """§266's fix, driven: over the registered size the mapping must be balanced, not merely
    'alternating' in a comment."""
    here = _stand(tmp_path)
    out = tmp_path / "out"
    _run(here, out, ARM_GO="1")
    rows = _manifest(out)
    for cond in ("treat", "control"):
        seen = {lane: 0 for lane in LANES}
        for r in rows:
            if r["condition"] == cond:
                seen[r["lane"]] += 1
        assert set(seen.values()) == {2}, (cond, seen)


def test_the_row_is_written_before_the_probe_runs(tmp_path):
    """A probe that dies must stay in the record as a dead probe of its arm. One that vanishes
    biases the arm by exactly as much as it differed."""
    src = DRIVER.read_text(encoding="utf-8")
    manifest_at = src.index('>> "$MANIFEST"')
    assert manifest_at < src.index('setsid nohup bash "$HERE/run_probe.sh"'), \
        "the manifest row is written after the launch, so a failed launch leaves no row"


def test_a_preregistration_edited_mid_arm_refuses(tmp_path):
    """Changing the conditions halfway IS the interim look, whatever the intent."""
    here = _stand(tmp_path)
    out = tmp_path / "out"
    assert _run(here, out, ARM_GO="1").returncode == 0
    (here / "PREREGISTERED-T1.txt").write_text(PRE.replace("batches: 4", "batches: 6"),
                                               encoding="utf-8")
    got = _run(here, out, ARM_GO="1")
    assert got.returncode == 2 and "ИЗМЕНИЛАСЬ" in got.stderr, got.stdout + got.stderr


def test_a_finished_probe_is_not_rerun(tmp_path):
    """$1 a probe: a resumed arm that re-runs what it already bought is a resumed arm nobody uses."""
    here = _stand(tmp_path)
    out = tmp_path / "out"
    _run(here, out, ARM_GO="1")
    before = len(_manifest(out))
    got = _run(here, out, ARM_GO="1")
    assert "уже отработала" in got.stdout, got.stdout
    assert len(_manifest(out)) == before, "a skipped probe still wrote a manifest row"


def test_the_driver_reads_no_outcome():
    """Negative pins, because what must not come back is the TEXT: no p-value, no test, no score
    arithmetic anywhere in a file that runs between batches."""
    src = DRIVER.read_text(encoding="utf-8")
    for forbidden in ("p_value", "p-value", "fisher", "compare_arms", "arm_power.py --outcome"):
        assert forbidden not in src.lower().replace("p-value", "p-value"), forbidden
    assert "не считает исходов" in src, "the file should say so, where the next reader looks"
