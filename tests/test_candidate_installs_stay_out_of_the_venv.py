"""A candidate's compiled extension must not be installed into the shared arena venv.

`AlgoTune/scripts/evaluate_results.py:266` runs `pip install . --no-deps --force-reinstall
--no-cache-dir` over the candidate directory whenever a `setup.py` is present. While the venv had
no pip that branch just failed. Once pip was added (2026-08-28, `box-jhub-l40s.sh`) it SUCCEEDS,
and every compiled candidate lands in `site-packages` and outlives its run.

MEASURED hours after that repair: six candidate modules were installed there -- `cutcounter`,
`edge_cut`, `edge_flatten`, `edgecut`, `_fast_cut`, `solver_ext` -- five within ninety minutes.
It changed a score: ds3's champion scored 156.4328 train and 0.0 test with `evaluator_error`,
because a `cutcounter` installed on 2026-08-27 19:56 SHADOWED the champion's own build and the
import resolved to a stale binary whose `count_cut` takes three arguments where the caller passes
two. Removing that one file makes the same champion validate on every instance.

So the bridge sets `PIP_TARGET` to a per-invocation directory and puts it first on `PYTHONPATH`.
The arena is not patched; pip's own redirect does the work.
"""
from __future__ import annotations

import re
from pathlib import Path

_BRIDGE = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "looplab_eval.py"


def _env_block() -> str:
    body = _BRIDGE.read_text(encoding="utf-8")
    start = body.index("env = dict(os.environ, ALGOTUNE_EVAL_SUBSET=args.subset)")
    return body[start:body.index("proc = subprocess.run(argv", start)]


def test_the_bridge_redirects_pip_away_from_the_shared_venv():
    block = _env_block()
    assert 'env["PIP_TARGET"]' in block, "a candidate install would land in the shared site-packages"
    assert "mkdtemp" in block, "the target must be per-invocation, or two evaluations shadow each other"


def test_the_target_is_first_on_pythonpath_so_the_module_is_importable():
    block = _env_block()
    assert 'env["PYTHONPATH"]' in block
    m = re.search(r'env\["PYTHONPATH"\]\s*=\s*os\.pathsep\.join\(\[(\w+)\]', block)
    assert m and m.group(1) == "_pip_target", \
        "the fresh target must lead PYTHONPATH; behind an inherited one it can still be shadowed"


def test_an_existing_pythonpath_is_kept_rather_than_dropped():
    """The operator's own PYTHONPATH is not ours to discard."""
    block = _env_block()
    assert 'env.get("PYTHONPATH")' in block, "an inherited PYTHONPATH must survive"


def test_the_reason_is_recorded_where_the_next_reader_will_look():
    block = _env_block()
    for token in ("evaluate_results.py:266", "cutcounter", "156.4328", "PIP_TARGET"):
        assert token in block, f"the comment lost {token!r} -- this defect cost three wrong diagnoses"
