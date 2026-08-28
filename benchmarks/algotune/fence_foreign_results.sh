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
# So: MOVED ASIDE into `$HOLD` for the duration, and moved back afterwards.
#
# `chmod 000` was the first design and it took a live campaign down within half an hour: the arena's
# own `scripts/evaluate_results.py` discovers work by iterating EVERY directory under `results/`, so
# the first unreadable one raises `PermissionError` and the evaluator dies before it writes
# `evaluate_summary.json` — two task-arms scored 0.0 in 0.1 s and it read as a solver fault. A fence
# has to hide the foreign work WITHOUT blinding the ruler, and only the move does both
# (`tests/test_algotune_fence_keeps_results_walkable.py` asserts each obligation separately).
#
# WHAT THE MOVE COSTS, stated rather than promised away: while the fence is closed the fork's
# `git status` shows all 2,831 tracked files as deletions, so a `git` operation taken mid-campaign
# sees a tree that is not the commit the campaign names. `open` restores them exactly, and it
# restores from the HOLDING DIRECTORY rather than from `$STATE`, so losing the record never means
# losing the data.
set -u
AT=${FENCE_ALGOTUNE_ROOT:-/var/tmp/looplab-bench/AlgoTune}
STATE=${FENCE_STATE:-/var/tmp/looplab-bench/.foreign_results_moved}
HOLD=${FENCE_HOLD:-/var/tmp/looplab-bench/.foreign_results_held}

case "${1:-}" in
  close)
    # THE HOLD DIRECTORY IS THE STATE, and the state file is only a record of it. `close` used to
    # truncate `$STATE` on entry, so calling it twice — which the driver does, because
    # `check_leaks.sh` fences before `run_final.sh` fences again — erased the list of what had been
    # moved and stranded all seventeen directories where `open` would never look for them. The fork
    # would have been left permanently short of 2,831 tracked files.
    #
    # So `close` is idempotent: anything already held stays held and is re-recorded, never dropped.
    mkdir -p "$HOLD"
    : > "$STATE"
    for H in "$HOLD"/*/; do
      [ -d "$H" ] && printf '%s\n' "$(basename "$H")" >> "$STATE"
    done
    held=$(wc -l < "$STATE")
    n=0
    # `[ -d "$D" ] || continue` FIRST, exactly as the held-restore loop above does: `set -u` does
    # not set nullglob, so an empty `results/` leaves `$D` as the literal pattern, `basename` yields
    # `*`, no skip pattern matches it, and the loop recorded a `*` row in $STATE, errored on `mv`
    # and then printed "closed 1 foreign result directories" about nothing. The realistic trigger is
    # the double-close the idempotence note above documents the driver doing.
    for D in "$AT/results"/*/; do
      [ -d "$D" ] || continue
      B="$(basename "$D")"
      case "$B" in LoopLab*|diag*|recheck*|REC-*|RuleCheck-*|CTL*) continue ;; esac
      printf '%s\n' "$B" >> "$STATE"
      mkdir -p "$HOLD"
      mv "$D" "$HOLD/$B"; n=$((n+1))
    done
    echo "closed $n foreign result directories${held:+ (${held} already held)}"
    ;;
  open)
    # Restores from the HOLD DIRECTORY, not from the state file — everything held goes back, whether
    # or not the record of it survived. Losing the record must never mean losing the data.
    [ -d "$HOLD" ] || { echo "nothing held — nothing to restore"; exit 0; }
    n=0
    for H in "$HOLD"/*/; do
      [ -d "$H" ] || continue
      B="$(basename "$H")"
      if [ -e "$AT/results/$B" ]; then echo "  REFUSING to overwrite existing $B"; continue; fi
      mv "$H" "$AT/results/$B"; n=$((n+1))
    done
    rmdir "$HOLD" 2>/dev/null || true
    echo "restored $n directories"
    rm -f "$STATE"
    ;;
  check)
    bad=0
    for D in "$AT/results"/*/; do
      [ -d "$D" ] || continue          # unmatched glob is a literal `*`, not a foreign directory
      B="$(basename "$D")"
      case "$B" in LoopLab*|diag*|recheck*|REC-*|RuleCheck-*|CTL*) continue ;; esac
      echo "  STILL PRESENT: $B"; bad=1
    done
    # "STILL PRESENT", not "READABLE": the shipped design MOVES the directories aside, so what a
    # failure means here is that they are still in `results/` — not that their mode bits are open.
    [ "$bad" = "0" ] && echo "all foreign result directories are closed" || echo "SOME ARE STILL PRESENT"
    exit $bad
    ;;
  *) echo "usage: $0 close|open|check"; exit 2 ;;
esac
