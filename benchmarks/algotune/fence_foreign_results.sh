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

# ЧУЖОЕ — ЭТО ТО, ЧТО ОТСЛЕЖИВАЕТ GIT, а не то, чьё имя не попало в список.
#
# Раньше здесь стоял перечень наших префиксов (`LoopLab*|diag*|recheck*|REC-*|RuleCheck-*|CTL*`), и
# он устарел ровно так, как устаревают все такие перечни: арена называет каталог по значению
# `--model`, а `make_task.py:787` передаёт ей `DevEvalTrain` для команды `eval_train` разработчика.
# 27.08 в 05:56 на живой машине возник `results/DevEvalTrain-2668122/convex_hull/solver.py` и исчез
# к 05:57 — он живёт ровно одну оценку. Внутри этого окна `close` унёс бы артефакт работающей пробы
# в укрытие, а последующий `open` вернул бы его в `results/` как чужого чемпиона.
#
# Семнадцать опубликованных каталогов лежат в форке под контролем версий, всё, что оба плеча
# порождают на ходу, — нет. Проверено на живом дереве: `GPT-5`, `Claude Opus 4.6` и `R1`
# отслеживаются, `REC-90409` и три `RuleCheck-*` — нет. Новое имя, которое мы придумаем завтра,
# правки здесь не потребует.
is_foreign() {  # is_foreign <имя каталога>
  git -C "$AT" ls-files --error-unmatch -- "results/$1" >/dev/null 2>&1
}

# БЕЗ GIT ОТЛИЧИТЬ НЕКОГО ОТ КОГО НЕЛЬЗЯ: `ls-files` ответит «не отслеживается» про всё подряд, и
# забор молча станет пустышкой, отрапортовав `closed 0` с нулевым кодом. Лучше громкий отказ.
require_git() {
  git -C "$AT" rev-parse --git-dir >/dev/null 2>&1 || {
    echo "ОТКАЗ: $AT не git-дерево — чужое от нашего не отличить"; exit 3; }
}

case "${1:-}" in
  close)
    require_git
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
    for D in "$AT/results"/*/; do
      B="$(basename "$D")"
      is_foreign "$B" || continue
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
    require_git
    bad=0
    for D in "$AT/results"/*/; do
      B="$(basename "$D")"
      is_foreign "$B" || continue
      echo "  STILL PRESENT: $B"; bad=1
    done
    [ "$bad" = "0" ] && echo "all foreign result directories are closed" || echo "SOME ARE READABLE"
    exit $bad
    ;;
  *) echo "usage: $0 close|open|check"; exit 2 ;;
esac
