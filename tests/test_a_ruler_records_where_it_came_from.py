"""A baseline that cannot be diagnosed is a baseline nobody can trust for long.

On 2026-09-06 the cached `pagerank` baseline was found to sit 1.45x above three independent fresh
timings, uniformly across all 100 instances (ratio cv 0.10, the tightest of the four tasks). Six
mechanisms were tested and none reproduced it:

    lane            three bench lanes: 1.4131 / 1.3581 / 1.4099
    threads         OMP/OPENBLAS/MKL=1 gives 75.3 ms, default 75.1 ms
    load            three concurrent readings give 76.1 ms against 74.6 idle
    reference       AlgoTuneTasks/pagerank untouched since init, byte-identical to the staged copy
    re-timing date  the TRAIN twin, untouched since 08-31, disagrees by MORE (1.4908)
    cgroup quota    cpu.max identical across all 257 recorded snapshots

It stayed a mystery because the cache records the times and nothing else, while every other artefact
on this bench says where it came from: a probe's INSTRUMENT.txt, a snapshot's PROVENANCE.txt, the
regime key in the cache's own filename. So a write now leaves a sidecar. It cannot fix the entries
already on disk; it stops the next one being undiagnosable.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "algotune"))

import patch_baseline_cache as pbc  # noqa: E402

DEPLOYED = Path("/var/tmp/looplab-bench/AlgoTune/AlgoTuner/utils/evaluator/baseline_manager.py")


def test_provenance_is_part_of_what_a_current_deployment_must_contain():
    """Without this the fragment list would call a provenance-less deployment current, which is how
    the patch quietly rotted the last time."""
    names = [name for name, _ in pbc._REQUIRED_FRAGMENTS]
    assert "write provenance" in names, names


def test_the_template_records_the_conditions_that_were_tested_and_failed():
    """Each field is one of the six hypotheses above: recording anything less would leave the next
    occurrence exactly as undiagnosable as this one.

    Checked against `WRITE_PATCH`, the string that is actually emitted, and NOT against the module
    text. The first version searched the whole file -- where every required fragment also appears
    inside `_REQUIRED_FRAGMENTS` itself, so "is the fragment present" was trivially true and a
    mutation deleting the field from the emitted patch sailed through."""
    emitted = pbc.WRITE_PATCH
    for field in ("eval_workers", "omp_num_threads", "openblas_num_threads", "cpu_affinity",
                  "loadavg", "cpu_max", "written_at", "entries"):
        assert f'"{field}"' in emitted, field
    frag = dict(pbc._REQUIRED_FRAGMENTS)["write provenance"]
    assert frag in emitted, frag


def test_a_failed_provenance_write_never_costs_the_ruler():
    """The measurement is the expensive thing and the sidecar is a courtesy; an exception writing
    the courtesy must not lose the times. The template catches around the sidecar only."""
    emitted = pbc.WRITE_PATCH
    i = emitted.index("A RULER WITH NO PROVENANCE")
    j = emitted.index("LOOPLAB baseline cache WRITE", i)
    block = emitted[i:j]
    assert "except Exception as _ll_pexc" in block, block[-400:]
    assert "never lose the ruler" in block


def test_the_deployed_file_actually_carries_it():
    """The template is not the deployment. The last time these diverged, `patch_baseline_cache`
    reported STALE and could not upgrade in place -- the fragment list is what caught it."""
    if not DEPLOYED.is_file():
        import pytest
        pytest.skip("no AlgoTune deployment on this box")
    src = DEPLOYED.read_text(encoding="utf-8", errors="replace")
    missing = [name for name, frag in pbc._REQUIRED_FRAGMENTS if frag not in src]
    assert not missing, missing
