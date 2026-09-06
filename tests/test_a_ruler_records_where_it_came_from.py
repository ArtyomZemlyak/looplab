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

# BOTH PATHS, AND NOT BY ACCIDENT. The sidecar tests below import `ruler_check`, which lives one
# directory up. Run beside another file that had already inserted that path they passed; run alone
# they raised `ModuleNotFoundError`, and three mutations "failed" on the import rather than on the
# behaviour -- a green-looking mutation run that measured nothing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "algotune"))

import patch_baseline_cache as pbc  # noqa: E402
import ruler_check  # noqa: E402

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


def _cache(tmp_path, name, times, prov=None):
    import json as _json
    (tmp_path / name).write_text(_json.dumps({str(i): t for i, t in enumerate(times)}),
                                 encoding="utf-8")
    if prov is not None:
        (tmp_path / (name + ".provenance.json")).write_text(_json.dumps(prov), encoding="utf-8")


def test_a_sidecar_is_not_reported_as_a_malformed_entry(tmp_path):
    """The moment §297's sidecars landed, `ruler_check` called both of them malformed cache files --
    a false alarm I created myself, in the tool whose job is telling a real problem from a
    memorised number."""
    _cache(tmp_path, "pagerank__test__w22x1r3.json", [1.0] * 100,
           prov={"eval_workers": "auto", "loadavg": [8.4, 5.9, 7.5]})
    rows = ruler_check.entries(tmp_path)
    assert [r["file"] for r in rows] == ["pagerank__test__w22x1r3.json"], [r["file"] for r in rows]
    assert ruler_check.problems(rows, "w22x1r3", 100) == []


def test_the_conditions_are_read_and_shown(tmp_path, capsys):
    """Skipping the sidecar would have been enough to silence the alarm. Reading it is the point:
    pagerank's 46 % error was undiagnosable for three sweeps because no entry carried this."""
    _cache(tmp_path, "pagerank__test__w22x1r3.json", [1.0] * 100,
           prov={"eval_workers": "auto", "loadavg": [8.4, 5.9, 7.5]})
    rows = ruler_check.entries(tmp_path)
    assert rows[0]["provenance"]["eval_workers"] == "auto"
    ruler_check.main([str(tmp_path)])
    out = capsys.readouterr().out
    assert "auto workers" in out and "load 8.4" in out, out


def test_an_entry_without_a_sidecar_still_prints(tmp_path, capsys):
    """Nine of the eleven entries here predate the sidecar and will never have one; a tool that
    needed it would report the whole cache broken."""
    _cache(tmp_path, "pde_heat1d__test__w22x1r3.json", [1.0] * 100)
    rows = ruler_check.entries(tmp_path)
    assert rows[0]["provenance"] == {}
    assert ruler_check.main([str(tmp_path)]) == 0
    assert "pde_heat1d" in capsys.readouterr().out


def test_a_torn_sidecar_does_not_take_the_entry_down(tmp_path):
    _cache(tmp_path, "pagerank__test__w22x1r3.json", [1.0] * 100)
    (tmp_path / "pagerank__test__w22x1r3.json.provenance.json").write_text("{not json",
                                                                          encoding="utf-8")
    rows = ruler_check.entries(tmp_path)
    assert len(rows) == 1 and rows[0]["provenance"] == {}


def test_the_sidecar_names_the_interpreter():
    """The one field §297 did not record, and the only one that mattered.

    The 46 % "drift" the sidecar was written to explain was an interpreter difference (§299) --
    109.999 ms under the bench venv against 74.6 ms under conda on the same task, same lane, same
    cache. None of workers, threads, affinity, load or quota can show that."""
    emitted = pbc.WRITE_PATCH
    assert '"interpreter": _ll_sys2.executable' in emitted, "the sidecar cannot name its Python"
    names = [n for n, _ in pbc._REQUIRED_FRAGMENTS]
    assert "provenance names the interpreter" in names, names


def test_the_deployed_sidecar_names_it_too():
    if not DEPLOYED.is_file():
        import pytest
        pytest.skip("no AlgoTune deployment on this box")
    src = DEPLOYED.read_text(encoding="utf-8", errors="replace")
    assert '"interpreter": _ll_sys2.executable' in src


def test_the_sidecar_counts_busy_cpus_outside_the_lane():
    """Whole-box load is a weak proxy when the work is pinned, and it produced a false alarm on its
    first real use: thirty baselines written at load 350-1817 looked ruined, and `min_dominating_set`
    re-timed alone came back x0.996.

    What bites is other LANES. Measured on `max_common_subgraph`: 105-113 ms alone against 130 ms
    with three sibling lanes busy (x1.196). So the number worth keeping is how many CPUs outside our
    own affinity are held by other pinned processes -- 0 alone, 44 with two 22-cpu lanes busy.

    Counting distinct affinity SETS instead read 46 on an idle box, because an evaluator pins each
    worker to a single core and every core is its own set."""
    emitted = pbc.WRITE_PATCH
    assert '"busy_cpus_outside_lane": _ll_lanes' in emitted
    assert "_ll_seen |= _ll_o" in emitted, "counting sets again, not cpus"
    assert "provenance counts busy cpus" in [n for n, _ in pbc._REQUIRED_FRAGMENTS]


def test_the_deployed_file_counts_cpus_too():
    if not DEPLOYED.is_file():
        import pytest
        pytest.skip("no AlgoTune deployment on this box")
    src = DEPLOYED.read_text(encoding="utf-8", errors="replace")
    assert '"busy_cpus_outside_lane": _ll_lanes' in src and "_ll_seen |= _ll_o" in src


def test_an_unpinned_writer_reports_zero_not_the_whole_box():
    """A process with the run of the machine has no lane to be crowded out of, and reporting 96
    there would read as maximum contention when it means the opposite."""
    emitted = pbc.WRITE_PATCH
    i = emitted.index("_ll_mine = _ll_os2.sched_getaffinity(0)")
    j = emitted.index("busy_cpus_outside_lane")
    block = emitted[i:j]
    assert "len(_ll_mine) < _ll_tot" in block, block[:300]
    assert "_ll_lanes = 0" in block, block[-300:]
