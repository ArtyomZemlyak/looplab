#!/bin/bash
# Close the one contamination path both arms share: SEVENTEEN other models' finished solutions to
# these exact tasks live inside the checkout both arms are pointed at.
#
#   AlgoTune/results/{Claude Opus 4.6, GPT-5.4, Gemini 3.1 Pro, o4-mini, …}/<task>/solver.py
#   22 model directories, ~150 tasks each, 2,831 files, 70 MB — and `Claude Opus 4.6/convex_hull`
#   is a numba-jitted monotone chain, i.e. a finished answer to a task we are scoring.
#
# Neither arm's TOOLS can reach them: LoopLab's probe runs under a Landlock allow-list that admits
# nothing under `AlgoTune/` except `.venv/`, and AlgoTuner's `_make_absolute` takes only
# `Path(x).name` and forces it into CODE_DIR, so traversal is impossible by construction. Verified
# on the live corpus: across 41 probe calls, every AlgoTune path touched was under `.venv/`.
#
# But a SUBMITTED SOLVER is executable code, and during evaluation it runs unconfined for BOTH arms
# (`Settings.landlock` is off by default and AlgoTuner has no equivalent). Nothing stops
# `open("/var/tmp/looplab-bench/AlgoTune/results/Claude Opus 4.6/convex_hull/solver.py").read()`
# inside a candidate. No agent has done it and nothing suggests either would — but a benchmark that
# relies on nobody thinking of something is not measuring what it claims to.
#
# So: mode 000 for the duration, restored afterwards. Not moved, because the fork tracks all 2,831
# files and a moved tree would no longer be the commit the campaign names. `CapEff` is 0 in this
# container, so the owner bits are enforced against us too — checked, not assumed.
set -u
AT=${FENCE_ALGOTUNE_ROOT:-/var/tmp/looplab-bench/AlgoTune}
STATE=${FENCE_STATE:-/var/tmp/looplab-bench/.foreign_results_moved}
HOLD=${FENCE_HOLD:-/var/tmp/looplab-bench/.foreign_results_held}

case "${1:-}" in
  close)
    : > "$STATE"
    n=0
    for D in "$AT/results"/*/; do
      B="$(basename "$D")"
      case "$B" in LoopLab*|diag*|recheck*|REC-*|RuleCheck-*|CTL*) continue ;; esac
      printf '%s\n' "$B" >> "$STATE"
      mkdir -p "$HOLD"
      mv "$D" "$HOLD/$B"; n=$((n+1))
    done
    echo "closed $n foreign result directories"
    ;;
  open)
    [ -s "$STATE" ] || { echo "no state file — nothing to restore"; exit 0; }
    while IFS= read -r B; do
      [ -n "$B" ] || continue
      [ -d "$HOLD/$B" ] && mv "$HOLD/$B" "$AT/results/$B"
    done < "$STATE"
    rmdir "$HOLD" 2>/dev/null || true
    echo "restored $(wc -l < "$STATE") directories"
    rm -f "$STATE"
    ;;
  check)
    bad=0
    for D in "$AT/results"/*/; do
      B="$(basename "$D")"
      case "$B" in LoopLab*|diag*|recheck*|REC-*|RuleCheck-*|CTL*) continue ;; esac
      echo "  STILL PRESENT: $B"; bad=1
    done
    [ "$bad" = "0" ] && echo "all foreign result directories are closed" || echo "SOME ARE READABLE"
    exit $bad
    ;;
  *) echo "usage: $0 close|open|check"; exit 2 ;;
esac
