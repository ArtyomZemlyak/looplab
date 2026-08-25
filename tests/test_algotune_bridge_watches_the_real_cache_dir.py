"""The bridge must watch the directory the patch actually writes to.

`patch_baseline_cache.py` patches AlgoTune's `BaselineManager` to keep per-instance reference
timings in `ALGOTUNE_BASELINE_CACHE_DIR`. `looplab_eval.py` fingerprints that directory before and
after the evaluator runs, and refuses the number when it changed — that is how
`baseline_measured_in_pass` is detected at all.

THE DEFECT, found by review 2026-08-25 and confirmed by measurement: the bridge derived the
directory from its own `__file__`, while the campaign runs it out of the PINNED clone
(`looplab-armb`) and the patch writes into the working clone's. Measured on the live box: the
`__file__`-derived path did not exist and held 0 files; the real one held 44. The fingerprint was
therefore `{}` before AND after every campaign evaluation, the two compared equal, and the refusal
could never fire. It survived its live check only because that check passed `--baseline-times-dir`
by hand — which the campaign does not.

A guard that watches the wrong directory is worse than no guard: it reports green.
"""
import importlib.util
import os
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "looplab_eval.py"


def _load(env_value):
    """Import the bridge with the environment the campaign actually gives it."""
    old = os.environ.get("ALGOTUNE_BASELINE_CACHE_DIR")
    if env_value is None:
        os.environ.pop("ALGOTUNE_BASELINE_CACHE_DIR", None)
    else:
        os.environ["ALGOTUNE_BASELINE_CACHE_DIR"] = str(env_value)
    try:
        spec = importlib.util.spec_from_file_location("looplab_eval_dirtest", BRIDGE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if old is None:
            os.environ.pop("ALGOTUNE_BASELINE_CACHE_DIR", None)
        else:
            os.environ["ALGOTUNE_BASELINE_CACHE_DIR"] = old


def test_the_default_follows_the_environment_the_patch_uses(tmp_path):
    """This is the campaign's own configuration: the env names the dir, the bridge lives elsewhere."""
    d = tmp_path / "elsewhere" / ".baseline_times"
    d.mkdir(parents=True)
    mod = _load(d)
    assert mod.DEFAULT_TIMES_DIR == d, (
        f"bridge watches {mod.DEFAULT_TIMES_DIR}, patch writes to {d}")


def test_without_the_variable_it_falls_back_beside_itself(tmp_path):
    """The falsifier for the test above: a default that IGNORED the environment would still have to
    fail this one, and a default hard-wired to any single path would fail both."""
    mod = _load(None)
    assert mod.DEFAULT_TIMES_DIR == BRIDGE.parent / ".baseline_times"


def test_the_fingerprint_reads_that_same_directory():
    """And the guard must use it, not a second path of its own."""
    src = BRIDGE.read_text(encoding="utf-8")
    fp = src.index("def _baseline_fingerprint")
    body = src[fp:fp + 700]
    assert "args.baseline_times_dir" in body, \
        "the fingerprint no longer globs the configured timings dir"
