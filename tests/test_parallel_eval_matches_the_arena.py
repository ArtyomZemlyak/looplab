"""What a rebuilt stand would install must be what the corpus was measured with.

`patch_parallel_eval.py` copies `benchmarks/algotune/parallel_eval.py` into the arena as
`AlgoTuner/utils/evaluator/looplab_parallel.py`. On 2026-09-01 the two had diverged by 49 lines:

  * `apply_thread_budget` existed ONLY in the deployed copy. It holds BLAS/OpenMP threads down to a
    worker's core count, and its own docstring measures the alternative on this box over 24 rbf
    instances -- 893.5 ms serial, **5708.6 ms** through the pinned pool, 872.2 ms with the budget.
  * `_pool_init`, `prefetch_oracle` and `prefetch_results` differed; the pool itself had changed
    from `ctx.Pool` to `ProcessPoolExecutor`.
  * `resolve_workers` was identical -- so the baseline cache KEY was never at risk. What was at risk
    is the number under it: a stand rebuilt from the stale copy would time solvers with a 6.5x
    oversubscription distortion under the same regime name, and nothing checked.

This is the second time a generator's source and its deployment drifted unnoticed on this box, after
`patch_baseline_cache.py`. The shape is the same and so is the repair: the deployed file is the
authority, because it is what every number was measured with.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "benchmarks" / "algotune" / "parallel_eval.py"
DEPLOYED = Path("/var/tmp/looplab-bench/AlgoTune/AlgoTuner/utils/evaluator/looplab_parallel.py")

pytestmark = pytest.mark.skipif(not DEPLOYED.is_file(), reason="no patched arena on this box")


def _functions(src: str) -> set[str]:
    return {m.group(1) for m in re.finditer(r"^def (\w+)", src, re.M)}


def _body(src: str, name: str) -> str:
    m = re.search(rf"^def {name}\b.*?(?=^def |\Z)", src, re.M | re.S)
    return m.group(0) if m else ""


def test_every_function_the_arena_runs_exists_in_the_source():
    src, dep = SOURCE.read_text(), DEPLOYED.read_text()
    missing = _functions(dep) - _functions(src)
    assert not missing, (
        f"the arena runs {sorted(missing)}, which a rebuilt stand would not install. "
        "apply_thread_budget went missing this way and its absence inflates timings 6.5x."
    )


def test_no_function_in_the_source_is_absent_from_the_arena():
    src, dep = SOURCE.read_text(), DEPLOYED.read_text()
    extra = _functions(src) - _functions(dep)
    assert not extra, (
        f"{sorted(extra)} would be installed by a rebuild but is not what the corpus ran on"
    )


def test_the_bodies_agree():
    src, dep = SOURCE.read_text(), DEPLOYED.read_text()
    differing = [f for f in sorted(_functions(src) & _functions(dep))
                 if _body(src, f) != _body(dep, f)]
    assert not differing, (
        f"{differing} differ between the source and the arena. A rebuilt stand would time solvers "
        "differently from every number already in the corpus, under the same regime key."
    )


def test_resolve_workers_is_the_one_that_decides_the_ruler_name():
    """Named explicitly: this is the function whose drift would rename the cache key.

    It was NOT the one that drifted, and that is worth pinning separately -- the others change the
    measurement silently while this one changes the file name, which is loud.
    """
    src, dep = SOURCE.read_text(), DEPLOYED.read_text()
    assert _body(src, "resolve_workers"), "resolve_workers vanished from the source"
    assert _body(src, "resolve_workers") == _body(dep, "resolve_workers")


def test_the_thread_budget_is_actually_applied_not_merely_defined():
    """A defined-but-uncalled budget is the same as no budget."""
    src = SOURCE.read_text()
    assert "def apply_thread_budget" in src, "the budget function is gone"
    calls = [l for l in src.splitlines()
             if "apply_thread_budget(" in l and not l.strip().startswith("def ")]
    assert calls, "apply_thread_budget is defined and never called"
