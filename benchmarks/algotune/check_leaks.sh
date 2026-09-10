#!/bin/bash
# Everything a fresh campaign could inherit, asked as a QUESTION rather than assumed. Run before a
# start and after a reset; a non-zero exit means something would have carried over.
#
# The list is not a guess: every entry is a leak this project actually had.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=benchmarks/algotune/ours.sh
. "$HERE/ours.sh"
# THE THREE HARDCODES ARE GONE (2026-09-08), each of which defeated this tool's one job on any box
# but the original. REVIEW 2026-08-25 (correctness) measured them: (1) ROOT ignored BENCH_ROOT,
# which every other script here honours. (2) The marker and memory sweeps walked FIXED
# directory-name lists, so a campaign under the SHIPPED defaults (campaign/ + camp-runs/, per
# campaign.sh and box-jhub-l40s.sh) was invisible -- section 1 reported clean while that campaign's
# .done markers would make the next run skip every task, the precise leak the section exists for,
# and the same hardcoded-name defect snapshot.sh documents having already caused an incident one
# file over. (3) Section 3 counted files not containing "r3" as stale against a literal that one
# box used once, rather than against what the patch really writes.
#
# What replaces them: the root is a variable, the directories are DISCOVERED by glob (plus whatever
# the campaign's own CAMPAIGN_* variables name right now), and the cache generation is READ OUT of
# `patch_baseline_cache.py`. A section that finds no directory at all SAYS so instead of printing
# "чисто" about a path nobody has ever created -- an answer about a directory it never looked at is
# the failure this file exists to prevent, one level up.
ROOT="${BENCH_ROOT:-/var/tmp/looplab-bench}"
BAD=0
say() { printf "  %-52s %s\n" "$1" "$2"; }

# The directories a sweep should walk: every match of `$1`'s globs under $ROOT, plus any explicit
# paths passed after it (the live CAMPAIGN_* values, which may point outside $ROOT entirely).
# Word-split on purpose -- the globs are a list, and this whole benchmark assumes a path without
# spaces (the lane `taskset` lines and `run_final.sh` do too).
sweep_dirs() {   # $1 = space-separated globs under $ROOT; $2... = explicit paths ("" ignored)
  local globs="$1" g p
  shift
  {
    for g in $globs; do
      for p in "$ROOT"/$g; do [ -d "$p" ] && printf '%s\n' "$p"; done
    done
    for p in "$@"; do [ -n "$p" ] && [ -d "$p" ] && printf '%s\n' "$p"; done
  } | sort -u
}

# THE CURRENT CACHE GENERATION, read out of the patch that writes the names rather than typed.
# `patch_baseline_cache.py` keys a per-instance cache `<task>__<subset>__lane{N}<gen>.json` /
# `__w{W}x{C}<gen>.json`, and its own comment explains why the generation token exists ("from
# 2026-08-24 a lane is built out of WHOLE PHYSICAL CORES ... every reference taken before it is from
# another machine, effectively"). A cache file that does not carry TODAY's token was measured on a
# different ruler, which is exactly what this section is asking about -- so the token comes from the
# file that mints it, and a bump there is a bump here for free.
current_generation() {
  sed -n 's/.*__lane{\{1,2\}_ll_lane}\{1,2\}\([A-Za-z0-9]*\)".*/\1/p' \
      "$HERE/patch_baseline_cache.py" 2>/dev/null | head -1
}

echo "== 1. МАРКЕРЫ ГОТОВНОСТИ (кампания пропустит задачу, если они есть)"
FOUND=0
for D in $(sweep_dirs 'campaign*' "${CAMPAIGN_OUT:-}"); do
  FOUND=1
  N=$(ls "$D"/*.done 2>/dev/null | wc -l)
  [ "$N" != "0" ] && { say "${D#"$ROOT"/}" "$N маркеров — ПРОПУСТИТ"; BAD=1; } || say "${D#"$ROOT"/}" "чисто"
done
[ "$FOUND" = "0" ] && say "campaign*" "нет таких каталогов под $ROOT"

echo "== 2. ПАМЯТЬ И ЗНАНИЯ ПРОГОНОВ (межпрогонная память LoopLab)"
FOUND=0
for D in $(sweep_dirs 'runs-* camp-runs*' "${CAMPAIGN_RUNS:-}"); do
  FOUND=1
  N=$(find "$D" -maxdepth 3 \( -name 'lessons.jsonl' -o -name 'memora_cache.json' -o -name 'concept_capsules.jsonl' \) -size +0 2>/dev/null | wc -l)
  [ "$N" != "0" ] && { say "${D#"$ROOT"/}" "$N непустых файлов памяти"; BAD=1; } || say "${D#"$ROOT"/}" "пусто"
done
[ "$FOUND" = "0" ] && say "runs-*, camp-runs*" "нет таких каталогов под $ROOT"

echo "== 3. КЭШ ЭТАЛОННЫХ ВРЕМЁН (знаменатель от прошлой линейки)"
GEN="$(current_generation)"
[ -n "$GEN" ] || say "поколение кэша" "НЕ ВЫВЕДЕНО из patch_baseline_cache.py — не сужу о свежести"
FOUND=0
# `*/benchmarks/algotune/.baseline_times` and not one clone's: docs/51 §7 runs the campaign from a
# PINNED `looplab-armb` checkout beside the working one, and a cache under the clone this script is
# not in is exactly the inherited denominator the section is about.
for D in $(sweep_dirs '.baseline_times* */benchmarks/algotune/.baseline_times' \
                      "${ALGOTUNE_BASELINE_CACHE_DIR:-}"); do
  FOUND=1
  N=$(ls "$D" 2>/dev/null | wc -l)
  if [ "$N" = "0" ]; then say "${D#"$ROOT"/}" "пусто"; continue; fi
  if [ -z "$GEN" ]; then say "${D#"$ROOT"/}" "$N файлов, поколение не проверено"; continue; fi
  OLD=$(ls "$D" 2>/dev/null | grep -vc "$GEN" || true)
  say "${D#"$ROOT"/}" "$N файлов, из них не-$GEN: $OLD"
  [ "${OLD:-0}" != "0" ] && BAD=1
done
[ "$FOUND" = "0" ] && say ".baseline_times*" "нет таких каталогов под $ROOT"

echo "== 4. РАБОЧИЕ КАТАЛОГИ (старая карточка цели запустит прошлый эксперимент)"
FOUND=0
for D in $(sweep_dirs 'ws-* looplab_ws*' "${CAMPAIGN_WS:-}"); do
  FOUND=1
  N=$(ls "$D"/*.json 2>/dev/null | wc -l)
  [ "$N" != "0" ] && { say "${D#"$ROOT"/}" "$N карточек"; BAD=1; } || say "${D#"$ROOT"/}" "пусто"
done
[ "$FOUND" = "0" ] && say "ws-*, looplab_ws*" "нет таких каталогов под $ROOT"

echo "== 5. ЧУЖОЕ ДЕРЕВО (накопленные каталоги кандидатов и сводки)"
# THE ARENA THE ARMS ARE POINTED AT, which is `ALGOTUNE_ROOT` wherever a profile is sourced --
# `$ROOT/AlgoTune` is only the default. Asking the wrong checkout is the same "чисто about a
# directory it never looked at" the sections above were fixed for.
AT="${ALGOTUNE_ROOT:-$ROOT/AlgoTune}"
if [ ! -d "$AT/results" ]; then
  # NOT "чисто". `fence_foreign_results.sh::check` learned this first ("a tree that is not there is
  # not a clean one") and it is the same sentence here: an arena this script cannot find is an
  # unanswered question, and the section that exists to catch 2,831 other models' solutions may not
  # answer it with silence.
  say "AlgoTune/results" "НЕТ КАТАЛОГА $AT/results — не проверено (ALGOTUNE_ROOT?)"
  BAD=1
else
  N=0
  for _D in "$AT/results"/*/; do
    [ -d "$_D" ] || continue
    # SOURCED predicate: this line listed three of the six spellings, so a campaign that left
    # `REC-<pid>/` behind was reported clean and the next campaign inherited it.
    result_dir_is_ours "$(basename "$_D")" && N=$((N + 1))
  done
  S=$(ls "$AT/reports"/evaluate_summary.*.json 2>/dev/null | wc -l)
  [ "$N$S" != "00" ] && { say "AlgoTune/results,reports" "$N каталогов, $S сводок"; BAD=1; } || say "AlgoTune/results,reports" "чисто"
  A=$(ls "$AT/reports/agent_summary.json" 2>/dev/null | wc -l)
  [ "$A" != "0" ] && say "agent_summary.json" "есть — рука A ДОПИШЕТ в него, архивируй"
fi

# The foreign champions: not a leak into a NUMBER, but a warm start an unconfined solver could read.
# THIS BLOCK LIVED ONLY IN THE OPERATOR'S AD-HOC COPY until 2026-08-28: the two scripts had diverged,
# the tracked one covered more directories and the untracked one had this check, and each sweep got
# whichever half it happened to run. A checkout is the version that survives, so it gets both.
# `$HERE`, not `$ROOT/looplab/...`: the fence beside THIS script is the one whose rule matches this
# script's `ours.sh`, and a second checkout under the bench root is a real shape here (docs/51 §7
# pins arm B to a `looplab-armb` clone). `FENCE_ALGOTUNE_ROOT` carries the arena the same way.
if FENCE_ALGOTUNE_ROOT="$AT" bash "$HERE/fence_foreign_results.sh" check >/dev/null 2>&1; then
  say "чужие чемпионы в AlgoTune/results" "закрыты"
else
  say "чужие чемпионы в AlgoTune/results" "ЧИТАЕМЫ — закрой перед стартом"
  BAD=1
fi

echo "== 6. ЖУРНАЛ МЕТРА (суммы по кампании смешаются)"
# `METER_LOG` is what `start_meter.sh` and the box profile actually set; the path below is its
# default and not a second declaration of it.
ML="${METER_LOG:-$ROOT/meter/meter.jsonl}"
# The `[ -f ]` is what keeps the shell's own "No such file" off stderr: the `2>/dev/null` below
# silences `wc`, never the REDIRECTION, so an absent log used to print a diagnostic that reads like
# a fault in this script.
if [ -f "$ML" ]; then L=$(wc -l < "$ML" 2>/dev/null || echo 0); else L=0; fi
[ "$L" != "0" ] && { say "meter.jsonl" "$L строк — ротируй"; BAD=1; } || say "meter.jsonl" "пуст"

echo "== 7. ОБЩИЙ VENV АРЕНЫ (чужое расширение видно КАЖДОЙ идущей оценке)"
# `evaluate_results.py:266` runs `pip install .` over any candidate carrying a `setup.py`, so
# anything that lands in the arena's site-packages is importable by every concurrent evaluation.
# The bridge redirects its installs with PIP_TARGET (d439c966), but nothing was WATCHING the venv:
# on 2026-08-28 at 21:20 our own test suite dropped `_kern.cpython-311-*.so` and `kern-0.0.0.dist-info`
# there while two probe evaluations were running, and it was found by an ad-hoc sweep, not by this
# script. The baseline is the venv as built on 2026-08-20; anything NEWER than the arena's own
# `python` binary was put there after the fact.
SITE="$AT/.venv/lib/python3.11/site-packages"
if [ -d "$SITE" ]; then
  SINCE="2026-08-24 10:12"
  # `pip-*.dist-info` is the DELIBERATE repair of 2026-08-28 08:49: the uv-created venv shipped no
  # pip, so `evaluate_results.py:266` answered "No module named pip" on every candidate carrying a
  # setup.py, 363 times. Installing pip is what MAKES the arena able to score a compiled candidate,
  # so it is the one post-campaign write that belongs here. Nothing else is allowlisted.
  NEWER=$(find "$SITE" -maxdepth 1 -newermt "$SINCE" \
          \( -name '*.so' -o -name '*.dist-info' -o -name '*.egg-link' \) \
          -not -name 'pip-*.dist-info' 2>/dev/null | wc -l)
  if [ "${NEWER:-0}" -gt 0 ]; then
    say "site-packages" "$NEWER записей новее старта кампании — ЧУЖОЕ РАСШИРЕНИЕ В ЛИНЕЙКЕ:"
    find "$SITE" -maxdepth 1 -newermt "$SINCE" \
         \( -name '*.so' -o -name '*.dist-info' -o -name '*.egg-link' \) \
         -not -name 'pip-*.dist-info' \
         -printf '      %TY-%Tm-%Td %TH:%TM  %f\n' 2>/dev/null | head -20
    BAD=1
  else
    say "site-packages" "чисто (кроме умышленного ремонта pip)"
  fi
fi

echo
[ "$BAD" = "0" ] && echo "ЧИСТО: перезапуск ничего не унаследует." || echo "ЕСТЬ ЧТО УНАСЛЕДОВАТЬ — см. выше."
exit $BAD
