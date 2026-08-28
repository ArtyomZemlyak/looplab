#!/bin/bash
# WHICH `AlgoTune/results/` DIRECTORIES ARE OURS. Sourced, never copied.
#
# `results/` holds two populations that must never be confused: twenty-two OTHER models' finished
# solutions to the tasks we score (the contamination `fence_foreign_results.sh` exists to close) and
# the per-invocation directories our own bridge writes. Telling them apart is one predicate with
# three readers -- the fence's `close`, the fence's `check`, and `check_leaks.sh` -- and it was
# written out three times.
#
# That is not hypothetical drift: `check_leaks.sh`'s copy listed only `LoopLab*`, `diag*` and
# `recheck*`, missing `REC-*`, `RuleCheck-*` and `CTL*`, so a campaign that left `REC-90409/` behind
# was reported clean while the next campaign inherited it. Worse, the fence spelled its own rule
# twice, so its VERIFIER ran a second copy of the thing it verifies: drift there means `check` says
# "all foreign result directories are closed" about a directory `close` skipped.
#
# The spellings come from what the bridge actually WRITES -- `looplab_eval.py::main` names each
# invocation `--model LoopLab-<pid>`, and `REC-`/`RuleCheck-`/`CTL` are the diagnostic and
# rule-check passes whose names appear in that file's own recorded output.
OURS_GLOBS='LoopLab*|diag*|recheck*|REC-*|RuleCheck-*|CTL*'

result_dir_is_ours() {   # $1 = basename of a directory under AlgoTune/results
  # shellcheck disable=SC2254  # the glob alternation is the point
  case "$1" in
    LoopLab*|diag*|recheck*|REC-*|RuleCheck-*|CTL*) return 0 ;;
  esac
  return 1
}
