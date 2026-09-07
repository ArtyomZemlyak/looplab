"""A campaign that names no baseline regime measures against a denominator nobody chose.

THE FAILURE. `looplab_eval.py::_regime_mismatch` opens with

    if not (os.environ.get("ALGOTUNE_BASELINE_CACHE_DIR")
            or "--baseline-times-dir" in sys.argv):
        return None

— deliberately, so a unit test cannot be refused because of a data directory it never asked for.
`run_probe.sh` sets the environment variable; `campaign.sh` set neither it nor the flag (measured on
af13b4dd: `grep -c ALGOTUNE_BASELINE_CACHE_DIR campaign.sh` = 0, `grep -c baseline-times-dir
campaign.sh` = 0). So in the campaign — the one population the guard was written for — it returned
None on its FIRST LINE and no regime was ever checked.

WHY THAT IS EXPENSIVE RATHER THAN UNTIDY. A cold per-instance reference is not a slow measurement,
it is a different one: AlgoTune times the reference in the same pass and reports it against itself
at ~1.0 whatever was submitted (demonstrated 2026-08-25 with a solver returning `[]` for every
instance: 1.0009, 100/100 valid). Eight of the campaign's twenty final numbers are that shape. And
what decides whether the cache is cold is the WIDTH: with nothing set `resolve_workers` answers one
worker and the arena keys `__lane<N>r3`, while `run_probe.sh` declares `auto` and keys `__w<N>x1r3`
— which is what the live reference cache under `/var/tmp/looplab-bench` actually holds. The two
references sum to 3898 ms and 2976 ms over the same hundred instances, so the campaign and the
probes were reporting numbers off two instruments about 24 % apart with nothing in either record
naming which one it used.

WHAT IS PINNED HERE. Not the text of an export line: the campaign's own `declare_baseline_ruler` is
EXTRACTED and RUN, and the environment it produces is then handed to the REAL bridge, which must
refuse a foreign regime before timing anything. The last test is the mutation in permanent form —
the same bridge invocation under the pre-fix environment (neither name set) does NOT refuse.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = ROOT / "benchmarks" / "algotune" / "campaign.sh"
BRIDGE = ROOT / "benchmarks" / "algotune" / "looplab_eval.py"

_FUNCTIONS = ("baseline_cache_dir", "declare_baseline_ruler")


def _harness() -> str:
    """The ruler declaration, verbatim, with the two variables it reads from the preamble.

    Extracted by NAME and asserted to have a body, so deleting or renaming either function is a red
    test rather than a silently vacuous one — the rest of `campaign.sh` cannot run here (`cd "$AT"`,
    `source .venv/bin/activate`, a live endpoint).
    """
    src = CAMPAIGN.read_text(encoding="utf-8")
    parts = ["set -u"]
    for name in _FUNCTIONS:
        found = re.search(rf"^{name}\(\) \{{.*?^\}}$", src, re.M | re.S)
        assert found, f"campaign.sh no longer defines {name}()"
        body = found.group(0)
        assert len(body.splitlines()) > 2, f"{name}() extracted as an empty body"
        parts.append(body)
    return "\n".join(parts) + "\n"


def _declare(repo: Path, at: Path, **overrides: str) -> dict[str, str]:
    """Run the real `declare_baseline_ruler` and report the environment it leaves behind."""
    script = (_harness() + f'REPO={repo!s}\nAT={at!s}\n'
              'declare_baseline_ruler\n'
              'for V in ALGOTUNE_BASELINE_CACHE_DIR ALGOTUNE_EVAL_WORKERS '
              'ALGOTUNE_EVAL_CORES_PER_WORKER; do echo "$V=${!V-}"; done\n')
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ALGOTUNE_BASELINE", "ALGOTUNE_EVAL"))}
    env.update(overrides)
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120,
                         env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    return dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)


def _patched_checkout(tmp: Path, cache_dir: str | None) -> Path:
    """An AlgoTune root whose `BaselineManager` carries (or does not carry) the cache patch."""
    at = tmp / "AlgoTune"
    target = at / "AlgoTuner" / "utils" / "evaluator"
    target.mkdir(parents=True, exist_ok=True)
    if cache_dir is None:
        body = "class BaselineManager:\n    pass\n"
    else:
        # The shape the patch wears on the live box: overridable, with the baked path as default.
        body = ("class BaselineManager:\n"
                "    def get_baseline_times(self, subset):\n"
                "        _ll_cache_dir = os.environ.get(\n"
                "            'ALGOTUNE_BASELINE_CACHE_DIR',\n"
                f"            {cache_dir!r})\n"
                "        _ll_key = None\n")
    (target / "baseline_manager.py").write_text(body, encoding="utf-8")
    return at


# ------------------------------------------------------------------------------------------------
# the declaration itself
# ------------------------------------------------------------------------------------------------
def test_the_campaign_names_the_cache_dir_the_patch_really_writes(tmp_path):
    """`patch_baseline_cache.py` bakes the path at PATCH time, out of whichever clone ran it.

    The campaign may be a different clone (docs/51 SS7 runs it from the pinned `looplab-armb`), so a
    `$REPO`-derived guess reproduces the very defect being armed against — fingerprinting a
    directory nothing writes.
    """
    baked = str(tmp_path / "elsewhere" / ".baseline_times")
    got = _declare(tmp_path / "repo", _patched_checkout(tmp_path, baked))
    assert got["ALGOTUNE_BASELINE_CACHE_DIR"] == baked, got
    assert Path(baked).is_dir(), "the guard globs this directory; it has to exist to be watched"


def test_an_unpatched_checkout_falls_back_to_this_repos_own(tmp_path):
    repo = tmp_path / "repo"
    got = _declare(repo, _patched_checkout(tmp_path, None))
    assert got["ALGOTUNE_BASELINE_CACHE_DIR"] == str(repo / "benchmarks" / "algotune"
                                                     / ".baseline_times"), got


def test_the_width_is_declared_and_it_is_the_probes_width(tmp_path):
    """`auto` keys `__w<N>x1r3`, which is what `run_probe.sh` declares and what the live cache holds.

    A campaign that says nothing gets one worker and `__lane<N>r3` — a different instrument, not a
    quieter setting of the same one.
    """
    got = _declare(tmp_path / "repo", _patched_checkout(tmp_path, str(tmp_path / "t")))
    assert got["ALGOTUNE_EVAL_WORKERS"] == "auto", got
    assert got["ALGOTUNE_EVAL_CORES_PER_WORKER"] == "1", got


def test_a_side_experiment_may_still_name_its_own_ruler(tmp_path):
    """The falsifier for a declaration that overrides the operator instead of defaulting for them."""
    got = _declare(tmp_path / "repo", _patched_checkout(tmp_path, str(tmp_path / "t")),
                   ALGOTUNE_BASELINE_CACHE_DIR=str(tmp_path / "mine"),
                   ALGOTUNE_EVAL_WORKERS="1")
    assert got["ALGOTUNE_BASELINE_CACHE_DIR"] == str(tmp_path / "mine"), got
    assert got["ALGOTUNE_EVAL_WORKERS"] == "1", got


# ------------------------------------------------------------------------------------------------
# and the guard it arms, driven through the real bridge
# ------------------------------------------------------------------------------------------------
def _bridge_row(tmp: Path, cache: Path, env: dict[str, str]) -> dict:
    """Run the REAL bridge exactly the way `campaign.sh` runs it — no `--baseline-times-dir`."""
    src = BRIDGE.read_text(encoding="utf-8")
    marker = re.search(r'_SUBSET_PATCH_MARKER = "([^"]+)"', src)
    assert marker, "the bridge no longer defines _SUBSET_PATCH_MARKER"
    root = tmp / "AlgoTune"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    # A stub evaluator that leaves a FOOTPRINT: the claim is that the guard answers without running
    # it, so "it ran and said nothing" has to be distinguishable from "it never ran".
    (root / "scripts" / "evaluate_results.py").write_text(
        f"{marker.group(1)}\n"
        "import pathlib, sys\n"
        "print(\"LOOPLAB scoring on the 'train' split (1 problems)\", file=sys.stderr)\n"
        "pathlib.Path(sys.argv[0]).parent.parent.joinpath('EVALUATOR_RAN').write_text('yes')\n",
        encoding="utf-8")
    (tmp / "solver.py").write_text("class Solver:\n    def solve(self, p):\n        return []\n",
                                   encoding="utf-8")
    full = {k: v for k, v in os.environ.items()
            if not k.startswith(("ALGOTUNE_BASELINE", "ALGOTUNE_EVAL"))}
    full.update(env)
    out = subprocess.run(
        [sys.executable, str(BRIDGE), "--algotune-root", str(root), "--task", "demo",
         "--model", "T", "--solver", str(tmp / "solver.py"), "--subset", "train", "--timeout", "30"],
        capture_output=True, text=True, timeout=180, env=full, cwd=str(tmp))
    for line in reversed((out.stdout or "").splitlines()):
        if line.strip().startswith("{"):
            row = json.loads(line)
            row["_evaluator_ran"] = (root / "EVALUATOR_RAN").exists()
            return row
    raise AssertionError(f"the bridge printed no JSON line:\n{out.stdout}\n{out.stderr}")


def _foreign_cache(tmp: Path) -> Path:
    """A reference cache keyed for a regime `auto` will not key on this box."""
    cache = tmp / "times"
    cache.mkdir(exist_ok=True)
    # `auto` keys `__w<width>x1r3` -- EXCEPT on a one-CPU lane, where `width // 1` is 1, the
    # `workers <= 1` branch fires and `auto` keys `__lane1r3`. So "a one-worker lane key is always
    # the foreign one" is false exactly when the lane is one core wide, and this whole end-to-end
    # falsifier then went red in a single-CPU container against production code that is fine --
    # with a message ("the campaign would have re-timed the reference in this pass") pointing the
    # reader at a defect that is not there. The foreign key is now a width this lane CANNOT have,
    # so it is foreign at any affinity.
    _width = len(os.sched_getaffinity(0))
    (cache / f"demo__train__lane{_width + 1}r3.json").write_text("{}", encoding="utf-8")
    return cache


def test_the_declared_ruler_arms_the_regime_guard(tmp_path):
    """END TO END: the campaign's own environment, the real bridge, a foreign regime on disk."""
    cache = _foreign_cache(tmp_path)
    declared = _declare(tmp_path / "repo", _patched_checkout(tmp_path, str(cache)))
    row = _bridge_row(tmp_path, cache, declared)
    assert row["_evaluator_ran"] is False, (
        "the evaluator RAN — the campaign would have re-timed the reference in this pass")
    assert row.get("speedup") is None, row
    assert (row.get("no_speedup") or {}).get("reason") == "baseline_regime_mismatch", row


def test_without_the_declaration_the_guard_is_dead(tmp_path):
    """THE MUTATION, kept: the identical invocation with neither name set measures anyway.

    This is what every campaign run to date did. If this test ever goes red because the bridge
    started refusing without being pointed at a cache, the guard has grown a second entry point and
    the reasoning above needs re-deriving — it is not a licence to delete the declaration.
    """
    cache = _foreign_cache(tmp_path)
    row = _bridge_row(tmp_path, cache, {})
    assert (row.get("no_speedup") or {}).get("reason") != "baseline_regime_mismatch", (
        "the pre-fix environment already refused; the falsifier above proves nothing", row)
