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
#
# OPEN[fence-header-describes-abandoned-design] the paragraph above is the ABANDONED design: the
# code below MOVES the directories.
# proof:`present:So: mode 000 for the duration@benchmarks/algotune/fence_foreign_results.sh`
# REVIEW 2026-08-25 (correctness): the chmod design was replaced because mode 000 killed the
# evaluator's own results/ walk (commit "the fence must hide foreign work without blinding the
# ruler"), and `close` now does `mv` into $HOLD -- so the header asserts the exact opposite of the
# behaviour, INCLUDING the rationale ("Not moved, because ...") for the provenance property the
# current code deliberately gives up (a closed fence leaves the fork's `git status` showing 2,831
# deletions -- worth stating, since the header currently promises it away). `check`'s "SOME ARE
# READABLE" wording is chmod-era too. Rewrite the header to the move design; the commit message's
# text is accurate and can be lifted.
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
    # OPEN[fence-close-unmatched-glob-fabricates-a-close] with results/ holding no directories, the
    # unmatched glob is processed as a literal `*` entry.
    # proof:`absent:-d "$D"@benchmarks/algotune/fence_foreign_results.sh`
    # REVIEW 2026-08-25 (correctness): `set -u` does not set nullglob, so an empty results/ leaves
    # `$D` as the literal pattern; `basename` yields `*`, which no skip pattern matches, so the
    # loop records a `*` row in $STATE, `mv` errors on stderr, and it prints "closed 1 foreign
    # result directories" about nothing. Driven 2026-08-25 against an empty results/. The realistic
    # trigger is the exact double-close the idempotence comment above documents the driver doing
    # (everything already held, no LoopLab* dirs yet); `check` has the same unguarded glob and then
    # reports "STILL PRESENT: *" and exits 1 on a fresh checkout. Fix: guard both loops with a
    # directory-exists test on `$D` (the held-restore loop above already does exactly that for its
    # own glob), and the fabricated close and false alarm both disappear.
    for D in "$AT/results"/*/; do
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
      B="$(basename "$D")"
      case "$B" in LoopLab*|diag*|recheck*|REC-*|RuleCheck-*|CTL*) continue ;; esac
      echo "  STILL PRESENT: $B"; bad=1
    done
    [ "$bad" = "0" ] && echo "all foreign result directories are closed" || echo "SOME ARE READABLE"
    exit $bad
    ;;
  *) echo "usage: $0 close|open|check"; exit 2 ;;
esac
