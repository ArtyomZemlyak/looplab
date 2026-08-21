#!/bin/bash
# Bring a fresh AlgoTune checkout to the exact state this arm measures in. Idempotent.
#
#   ./setup_algotune.sh /path/to/AlgoTune
#
# Everything here is a DEVIATION FROM UPSTREAM that a published number depends on, so it lives in
# one script rather than in a person's shell history. Run it on any machine before a campaign; run
# it again after `git pull` in the AlgoTune checkout.
#
# What it does, and why each one is not optional:
#
#  1. The `sys.modules.items()` mutation bug. `inspect.getmembers()` inside the loop triggers lazy
#     imports and mutates the dict, raising "dictionary changed size during iteration" on EVERY
#     benchmark run, so no speedup can ever be recorded (measured: 224 occurrences in one run).
#
#  2. The cache-clearing filter. Upstream matches "AlgoTune" anywhere in a module's __file__, which
#     on a layout where the venv lives INSIDE the checkout matches the whole virtualenv -- measured
#     2132 of 2493 modules, torch/jax/scipy included -- and inflated the oracle pass 6.5x
#     (8.6s -> 56s per instance). Narrowed to exclude site-packages.
#
#  3. `disable_rlimit_as: true`. RLIMIT_AS caps VIRTUAL address space, which JAX/torch/BLAS reserve
#     in tens of GB without touching. Every evaluation died with "A process in the process pool was
#     terminated abruptly" -- at 14 GB and again at 30 GB, on a 321 MB task and on a 28 KB one,
#     with 45 GB free. Not memory, not dataset size.
#
#  4. `baseline_timeout: 10000`. A solver that times out costs 60 s per instance across ~100
#     instances: one candidate ate 87 minutes and completed zero problems. Both arms, so it is
#     parity-preserving; a solver 100x over a 100 ms target scores 0 either way.
#
#  5. The model entry, with the provider PINNED. Unpinned, one slug reaches several providers at
#     different quantizations: three calls hit two fp4 providers and returned 96/17/96 completion
#     tokens for one prompt. `allow_fallbacks: false` is required -- without it `order` is a
#     preference, not a pin.
#
#  6. The budget and run counts. See the comments written into config.yaml itself.
#
#  7. The two on-disk patches (persistent baseline cache, train/test subset). The SECOND one is
#     what makes `--subset train` mean anything: unpatched, evaluate_results.py ignores it and
#     scores every LoopLab node on the TEST split while the bridge still records subset=train.
set -eu

AT="${1:-}"
[ -n "$AT" ] || { echo "usage: $0 /path/to/AlgoTune"; exit 2; }
[ -d "$AT/AlgoTuner" ] || { echo "not an AlgoTune checkout: $AT"; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"
AT="$(cd "$AT" && pwd)"

# ---------------------------------------------------------------- the FORK is the primary path
#
# `github.com/ArtyomZemlyak/AlgoTune`, branch `looplab-bench`, is upstream dff9914 with every
# deviation below applied AS A COMMIT, so a published number can name a commit instead of "a
# checkout somebody patched". Prefer it:
#
#     git clone -b looplab-bench https://github.com/ArtyomZemlyak/AlgoTune.git /srv/AlgoTune
#
# THIS SCRIPT NO LONGER REPRODUCES THAT BRANCH, and the difference is not cosmetic. The fork also
# carries `AlgoTuner/utils/evaluator/looplab_parallel.py` plus its wiring in
# `evaluation_orchestrator.py` and `solver_executor.py` -- instance evaluation runs concurrently,
# one core per worker, measured 132 s -> 23 s for a scorer run on the reference box. There is no
# patch script for those, so a checkout prepared HERE evaluates SERIALLY and a checkout prepared
# from the fork evaluates in parallel. Both are valid harnesses; numbers from the two are not
# comparable, because the timing regime a solver is measured under is different.
#
# So the script refuses to be silent about which one you ended up with.
ON_FORK=0
if git -C "$AT" rev-parse --verify --quiet looplab-bench >/dev/null 2>&1    && [ -f "$AT/AlgoTuner/utils/evaluator/looplab_parallel.py" ]; then
    ON_FORK=1
fi
if [ "$ON_FORK" = "1" ]; then
    echo "== this checkout already carries the fork branch (looplab_parallel.py present)."
    echo "   The patches below are already committed there; re-applying them is a no-op, but the"
    echo "   parallel evaluator is NOT reproducible from this script -- do not revert it."
fi


echo "== 1/7  sys.modules iteration bug"
sed -i 's/for module_name, module in sys.modules.items():/for module_name, module in list(sys.modules.items()):/g' \
    "$AT/AlgoTuner/utils/isolated_benchmark.py"
echo "   $(grep -c 'list(sys.modules.items())' "$AT/AlgoTuner/utils/isolated_benchmark.py") site(s) patched"

echo "== 2/7  cache-clearing filter (exclude site-packages)"
python3 - "$AT" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1]) / "AlgoTuner/utils/isolated_benchmark.py"
s = p.read_text(encoding="utf-8")
if '"site-packages" not in module_file' in s:
    print("   already narrowed")
    raise SystemExit(0)
# Anchored on the UPSTREAM single-line form. An earlier version of this script guessed a
# multi-line shape, did not match, and printed nothing an operator would read as a failure --
# leaving the 6.5x oracle slowdown in place on a fresh machine. Hence the verify-or-fail below.
old = ('                if module_file and any(\n'
       '                    part in module_file for part in ["llm_src", "AlgoTune", "/tmp/", "solver"]')
new = ('                if (\n'
       '                    module_file\n'
       '                    and "site-packages" not in module_file\n'
       '                    and "dist-packages" not in module_file\n'
       '                    and any(\n'
       '                        part in module_file\n'
       '                        for part in ["llm_src", "AlgoTune", "/tmp/", "solver"]\n'
       '                    )')
if old not in s:
    raise SystemExit("   FAILED: upstream shape changed; narrow the filter by hand "
                     "(see README, 'Known upstream bug')")
s = s.replace(old, new, 1)
# close the `if (` that the replacement opened: the original line ended with `):` two lines down
s = s.replace(new + '\n                ):', new + '\n                ):', 1)
p.write_text(s, encoding="utf-8")
if '"site-packages" not in module_file' not in p.read_text(encoding="utf-8"):
    raise SystemExit("   FAILED: narrowing did not take")
print("   narrowed")
PY
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" \
    "$AT/AlgoTuner/utils/isolated_benchmark.py" \
    || { echo "   FAILED: patched isolated_benchmark.py does not parse"; exit 1; }

echo "== 3-6/7  config.yaml"
python3 - "$AT" <<'PY'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1]) / "AlgoTuner/config/config.yaml"
s = p.read_text(encoding="utf-8")

def setkv(text, key, value, indent="  "):
    pat = re.compile(rf"^{re.escape(indent)}{re.escape(key)}:[^\n]*$", re.M)
    line = f"{indent}{key}: {value}"
    return pat.sub(line, text, count=1) if pat.search(text) else text

s = setkv(s, "spend_limit", "0.02 # in USD")
s = setkv(s, "dev_runs", "3")
s = setkv(s, "eval_runs", "3")
s = setkv(s, "baseline_timeout", "10000 # in milliseconds")
if not re.search(r"^  runs:", s, re.M):
    s = s.replace("  dev_runs:", "  runs: 3\n  dev_runs:", 1)
else:
    s = setkv(s, "runs", "3")
s = setkv(s, "num_workers", "4", indent="    ")
s = setkv(s, "memory_limit_gb_per_worker", "30", indent="    ")
s = setkv(s, "disable_rlimit_as", "true", indent="    ")

MODEL = """  openrouter/deepseek/deepseek-v4-flash-0731:
    api_key_env: "OPENROUTER_API_KEY"
    temperature: 0.0
    drop_params: true
    usage:
      include: true
    extra_body:
      provider:
        order: ["siliconflow/fp8"]
        allow_fallbacks: false
      reasoning:
        effort: medium
"""
if "openrouter/deepseek/deepseek-v4-flash-0731:" not in s:
    s = re.sub(r"^models:\s*$", "models:\n" + MODEL.rstrip(), s, count=1, flags=re.M)
p.write_text(s, encoding="utf-8")
print("   spend_limit / runs / dev_runs / eval_runs / baseline_timeout / pool / model entry set")
PY

echo "== 7/7  on-disk patches"
python3 "$HERE/patch_eval_subset.py"    --algotune-root "$AT"
python3 "$HERE/patch_baseline_cache.py" --algotune-root "$AT"

echo
echo "done. verify:"
echo "  grep -E '^  (runs|dev_runs|eval_runs|spend_limit|total_messages)|disable_rlimit_as' $AT/AlgoTuner/config/config.yaml"
echo "  echo OPENROUTER_API_KEY=sk-or-... > $AT/.env"
echo "  ARM=A $HERE/campaign.sh"
