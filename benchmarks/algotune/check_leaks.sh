#!/bin/bash
# Everything a fresh campaign could inherit, asked as a QUESTION rather than assumed. Run before a
# start and after a reset; a non-zero exit means something would have carried over.
#
# The list is not a guess: every entry is a leak this project actually had.
set -u
# shellcheck source=benchmarks/algotune/ours.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ours.sh"
# OPEN[check-leaks-pinned-to-one-box] the leak checker answers "чисто" about directories it never
# looked at, and flags the repo's own baseline cache as stale.
# proof:`present:ROOT=/var/tmp/looplab-bench@benchmarks/algotune/check_leaks.sh`
# REVIEW 2026-08-25 (correctness): three hardcodes, each defeating the tool's one job on any box
# but the original. (1) ROOT ignores BENCH_ROOT, which every other script here honours. (2) The
# marker and memory sweeps walk FIXED directory-name lists, so a campaign under the shipped
# defaults (campaign/ + camp-runs/, per campaign.sh and box-jhub-l40s.sh) is invisible: section 1
# reports clean while that campaign's .done markers will make the next run skip every task -- the
# precise leak the section exists for, and the same hardcoded-name defect snapshot.sh documents
# having already caused an incident one file over. (3) Section 3 counts files not containing "r3"
# as stale, but the repo's own patch_baseline_cache.py writes names with no such generation token
# at all (`<task>__<subset>[__wNxC].json`), so every CURRENT cache file it produces is reported as
# a leak. Fix: `ROOT="${BENCH_ROOT:-...}"`, discover campaign*/runs-*/camp-runs by glob, and derive
# the current-generation test from the patch's real naming rather than a literal one box used once.
ROOT=/var/tmp/looplab-bench
BAD=0
say() { printf "  %-52s %s\n" "$1" "$2"; }

echo "== 1. МАРКЕРЫ ГОТОВНОСТИ (кампания пропустит задачу, если они есть)"
for D in campaign-paired campaign-armb campaign-armA-1usd campaign-deep; do
  N=$(ls "$ROOT/$D"/*.done 2>/dev/null | wc -l)
  [ "$N" != "0" ] && { say "$D" "$N маркеров — ПРОПУСТИТ"; BAD=1; } || say "$D" "чисто"
done

echo "== 2. ПАМЯТЬ И ЗНАНИЯ ПРОГОНОВ (межпрогонная память LoopLab)"
for D in runs-A runs-B runs-armb runs-deep; do
  N=$(find "$ROOT/$D" -maxdepth 3 \( -name 'lessons.jsonl' -o -name 'memora_cache.json' -o -name 'concept_capsules.jsonl' \) -size +0 2>/dev/null | wc -l)
  [ "$N" != "0" ] && { say "$D" "$N непустых файлов памяти"; BAD=1; } || say "$D" "пусто"
done

echo "== 3. КЭШ ЭТАЛОННЫХ ВРЕМЁН (знаменатель от прошлой линейки)"
for D in "$ROOT/looplab/benchmarks/algotune/.baseline_times" "$ROOT/.baseline_times_deep"; do
  N=$(ls "$D" 2>/dev/null | wc -l)
  OLD=$(ls "$D" 2>/dev/null | grep -vc "r3" || true)
  [ "$N" != "0" ] && { say "$(basename "$D")" "$N файлов, из них не-r3: $OLD"; [ "${OLD:-0}" != "0" ] && BAD=1; } || say "$(basename "$D")" "пусто"
done

echo "== 4. РАБОЧИЕ КАТАЛОГИ (старая карточка цели запустит прошлый эксперимент)"
for D in ws-A ws-B ws-armb ws-deep; do
  N=$(ls "$ROOT/$D"/*.json 2>/dev/null | wc -l)
  [ "$N" != "0" ] && { say "$D" "$N карточек"; BAD=1; } || say "$D" "пусто"
done

echo "== 5. ЧУЖОЕ ДЕРЕВО (накопленные каталоги кандидатов и сводки)"
N=0
for _D in "$ROOT/AlgoTune/results"/*/; do
  [ -d "$_D" ] || continue
  # SOURCED predicate: this line listed three of the six spellings, so a campaign that left
  # `REC-<pid>/` behind was reported clean and the next campaign inherited it.
  result_dir_is_ours "$(basename "$_D")" && N=$((N + 1))
done
S=$(ls "$ROOT/AlgoTune/reports"/evaluate_summary.*.json 2>/dev/null | wc -l)
[ "$N$S" != "00" ] && { say "AlgoTune/results,reports" "$N каталогов, $S сводок"; BAD=1; } || say "AlgoTune/results,reports" "чисто"
A=$(ls "$ROOT/AlgoTune/reports/agent_summary.json" 2>/dev/null | wc -l)
[ "$A" != "0" ] && say "agent_summary.json" "есть — рука A ДОПИШЕТ в него, архивируй"

echo "== 6. ЖУРНАЛ МЕТРА (суммы по кампании смешаются)"
L=$(wc -l < "$ROOT/meter/meter.jsonl" 2>/dev/null || echo 0)
[ "$L" != "0" ] && { say "meter.jsonl" "$L строк — ротируй"; BAD=1; } || say "meter.jsonl" "пуст"

echo
[ "$BAD" = "0" ] && echo "ЧИСТО: перезапуск ничего не унаследует." || echo "ЕСТЬ ЧТО УНАСЛЕДОВАТЬ — см. выше."
exit $BAD
