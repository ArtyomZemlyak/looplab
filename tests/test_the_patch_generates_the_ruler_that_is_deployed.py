"""The generator drifted from the patch it generated, and the write side outran its own guard.

Both found 2026-08-31 by diffing `benchmarks/algotune/patch_baseline_cache.py` against the file it
had produced in `AlgoTune/AlgoTuner/utils/evaluator/baseline_manager.py`.

  1. THE DEPLOYED PATCH READS `ALGOTUNE_BASELINE_CACHE_DIR`; the generator baked the path in. And the
     deployed regime key is `__lane{N}r3` / `__w{W}x{C}r3`, while the generator emitted `""` /
     `__w{W}x{C}` -- no lane form, no `r3` generation marker. Re-running the generator (the obvious
     move when rebuilding the stand, which happened on 2026-08-29) would have renamed every key on
     disk, made the entire existing ruler unreachable, and silently measured a new one under the old
     names' place. Nothing said so.

  2. THE WRITE SIDE MINTED RULERS NOBODY ASKED FOR. `looplab_eval.py::_regime_mismatch` deliberately
     switches itself OFF when neither ALGOTUNE_BASELINE_CACHE_DIR nor --baseline-times-dir is given:
     it must not police a directory nobody pointed at. The write path had no matching restraint and
     defaulted straight into the repo's live `.baseline_times`. So an invocation without the
     variable escaped the guard AND added a second regime beside the campaign's. It happened: a
     reference-against-itself diagnostic wrote `edge_expansion__train__lane22r3.json` and
     `pde_heat1d__train__lane22r3.json` next to the `__w22x1r3` set -- 28.2 ms against 44.6 ms on the
     same hundred instances, waiting for the next run at workers <= 1 to pick up.
"""
import ast
import sys
import pathlib
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "benchmarks" / "algotune"))
import patch_baseline_cache as pbc  # noqa: E402

DEPLOYED = Path("/var/tmp/looplab-bench/AlgoTune/AlgoTuner/utils/evaluator/baseline_manager.py")


def _rendered() -> str:
    return pbc.PATCH.format(marker=pbc.MARKER, cache_dir="/somewhere/.baseline_times")


def test_the_cache_directory_is_read_from_the_environment_not_baked_in():
    body = _rendered()
    assert "os.environ.get(" in body and "ALGOTUNE_BASELINE_CACHE_DIR" in body, (
        "the generator hardcodes the cache directory again -- re-running it would undo the "
        "deployed patch's ability to be pointed at another ruler"
    )
    assert "/somewhere/.baseline_times" in body, "the pointed-at path must remain the DEFAULT"


def test_the_regime_key_keeps_the_lane_form_and_the_generation_marker():
    body = _rendered()
    assert '__lane{_ll_lane}r3' in body, (
        "the lane form is gone. At workers <= 1 the pool is bypassed and the whole cpuset is the "
        "instrument, so a 22-core lane's reference is not an 8-core lane's."
    )
    assert '__w{_ll_w}x{_ll_c}r3' in body, "the worker-pool form lost its r3 generation marker"
    assert "sched_getaffinity" in body, "nothing measures the lane width any more"


@pytest.mark.skipif(not DEPLOYED.exists(), reason="no patched arena on this box")
def test_it_generates_the_same_ruler_the_arena_is_running():
    """Byte equality is too strong -- but the ruler-DEFINING lines must match, or the names drift."""
    dep = DEPLOYED.read_text(errors="replace")
    if "_ll_cache_dir" not in dep:
        pytest.skip("arena is not patched")
    body = _rendered()
    for defining in ('__lane{_ll_lane}r3', '__w{_ll_w}x{_ll_c}r3'):
        assert defining in body and defining in dep, (
            f"{defining!r} is in one of generator/deployed and not the other -- the next re-patch "
            "renames the ruler on disk"
        )
    assert "ALGOTUNE_BASELINE_CACHE_DIR" in dep, "premise: the deployed patch reads the variable"


# --------------------------------------------------------------- the write gate, run not read
#
# Executed with the REAL os and logging against a REAL directory. The first version of this harness
# faked both, and both tests went red for reasons that had nothing to do with the gate -- a fake
# `makedirs` that did not make the directory, so `open` raised and the fragment's own except
# swallowed it. A fake that can fail in its own right cannot testify about the code it wraps.


def _run_write_fragment(tmp_path, env_has_dir, monkeypatch, caplog):
    """Compile the WRITE fragment into a function and run it for real."""
    frag = textwrap.dedent(pbc.WRITE_PATCH)
    # `_ll_cache_dir` is bound unconditionally at the top of the real method (it is what the
    # env lookup assigns), so the fragment may reference it; the harness supplies it.
    src = "def _w(self, subset, baseline_times, _ll_key, actual_count, _ll_cache_dir):\n"
    src += "\n".join("    " + ln if ln.strip() else ln for ln in frag.splitlines())
    src += "\n    return _ll_key\n"
    ast.parse(src)

    cache = tmp_path / "cache"
    cache.mkdir()
    if env_has_dir:
        monkeypatch.setenv("ALGOTUNE_BASELINE_CACHE_DIR", str(cache))
    else:
        monkeypatch.delenv("ALGOTUNE_BASELINE_CACHE_DIR", raising=False)

    import logging as real_logging
    import os as real_os
    ns = {"os": real_os, "logging": real_logging}
    exec(src, ns)                                   # noqa: S102 - the fragment IS the subject

    class _Self:
        _cache = {}

    key = str(cache / "task__train__w22x1r3.json")
    with caplog.at_level(real_logging.WARNING):
        ns["_w"](_Self(), "train", {"1": 2.0, "2": 3.0}, key, 2, str(cache))
    return pathlib.Path(key), caplog.text


def test_no_pointed_at_cache_means_no_new_ruler_is_minted(tmp_path, monkeypatch, caplog):
    written, log = _run_write_fragment(tmp_path, False, monkeypatch, caplog)
    assert not written.exists(), (
        "a run with no ALGOTUNE_BASELINE_CACHE_DIR still wrote a baseline file -- that is how a "
        "second regime appeared beside the campaign's set"
    )
    assert "NOT WRITTEN" in log, (
        f"it declined silently, which is how it went unnoticed for a day. log: {log!r}"
    )


def test_a_pointed_at_cache_is_still_written(tmp_path, monkeypatch, caplog):
    written, log = _run_write_fragment(tmp_path, True, monkeypatch, caplog)
    assert written.is_file(), (
        f"the gate now blocks the campaign and the probes too, and both name the cache. log: {log!r}"
    )
    import json
    assert json.loads(written.read_text()) == {"1": 2.0, "2": 3.0}
