#!/bin/bash
# Everything a fresh campaign could inherit, asked as a QUESTION rather than assumed. Run before a
# start and after a reset; a non-zero exit means something would have carried over.
#
# The list is not a guess: every entry is a leak this project actually had.
set -u
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
N=$(ls -d "$ROOT/AlgoTune/results"/LoopLab* "$ROOT/AlgoTune/results"/diag* "$ROOT/AlgoTune/results"/recheck* 2>/dev/null | wc -l)
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
