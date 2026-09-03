"""§115's confound needs the OLD `check` running beside the new one, on the same box, at the same time.

§114 measured the first evaluated node moving after §99 and §103 repaired the pre-flight command --
12 of 18 above 150 against the corpus's 5 of 27. §115 named the confound: different days, a shared
endpoint. §134/§135 then made it live rather than hypothetical, with the SAME harness and the SAME
card fingerprints producing 11-of-14 and then 1-of-4 four hours apart.

Nothing separates "our repairs" from "the endpoint" except running both checkers concurrently.
`--checker` is what makes that buildable; `looplab_check_pre99.py` is the file as of 103c4b1e^,
extracted rather than reconstructed.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "benchmarks" / "algotune" / "make_task.py"
LEGACY = REPO / "benchmarks" / "algotune" / "looplab_check_pre99.py"
CURRENT = REPO / "benchmarks" / "algotune" / "looplab_check.py"
ALGOTUNE = Path("/var/tmp/looplab-bench/AlgoTune")

needs_algotune = pytest.mark.skipif(
    not (ALGOTUNE / ".hf_datasets").is_dir(), reason="needs the AlgoTune checkout")


def _card(tmp_path: Path, *flags: str) -> tuple[dict, str]:
    """Returns the card AND its out-dir, because `editable_path` embeds the out-dir.

    The first version of the comparison below built the two cards into two directories and then
    asserted they differed only in the checker path -- they also differed in `editable_path`, which
    is an artefact of the fixture and not of the flag. The out-dir is normalised away instead.
    """
    out = tmp_path / ("ws" + str(abs(hash(flags))))
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--algotune-root", str(ALGOTUNE), "--task", "edge_expansion",
         "--out-dir", str(out), "--deliver", "--one-card", "--enforce-rules", *flags],
        capture_output=True, text=True, timeout=900)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads((out / "algotune_edge_expansion.json").read_text(encoding="utf-8")), str(out)


def test_the_legacy_checker_predates_the_repairs():
    """It has to be the file BEFORE §99, not a copy of today's with the gate deleted by hand."""
    old = LEGACY.read_text(encoding="utf-8")
    new = CURRENT.read_text(encoding="utf-8")
    assert "build_gate" not in old and "build_gate" in new
    assert "sys.path.insert(0, str(solver.resolve().parent))" not in old
    assert "sys.path.insert(0, str(solver.resolve().parent))" in new


@needs_algotune
def test_the_shipped_card_still_names_the_shipped_checker(tmp_path):
    argv = json.dumps(_card(tmp_path)[0])
    assert "looplab_check.py" in argv and "looplab_check_pre99.py" not in argv


@needs_algotune
def test_the_flag_swaps_only_that_path(tmp_path):
    """MUTATION GUARD: if `--checker` changed anything else, the arm would differ in two places and
    §92's whole readability argument would be gone."""
    ca, oa = _card(tmp_path)
    cb, ob = _card(tmp_path, "--checker", str(LEGACY))
    a = json.dumps(ca, sort_keys=True).replace(oa, "<OUT>")
    b = json.dumps(cb, sort_keys=True).replace(ob, "<OUT>")
    assert a != b
    assert a.replace(str(CURRENT), str(LEGACY)) == b, (
        "the two cards differ somewhere other than the checker path")
