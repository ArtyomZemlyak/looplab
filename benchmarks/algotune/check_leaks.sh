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

# The foreign champions: not a leak into a NUMBER, but a warm start an unconfined solver could read.
# THIS BLOCK LIVED ONLY IN THE OPERATOR'S AD-HOC COPY until 2026-08-28: the two scripts had diverged,
# the tracked one covered more directories and the untracked one had this check, and each sweep got
# whichever half it happened to run. A checkout is the version that survives, so it gets both.
if bash "$ROOT/looplab/benchmarks/algotune/fence_foreign_results.sh" check >/dev/null 2>&1; then
  say "чужие чемпионы в AlgoTune/results" "закрыты"
else
  say "чужие чемпионы в AlgoTune/results" "ЧИТАЕМЫ — закрой перед стартом"
  BAD=1
fi

echo "== 6. ЖУРНАЛ МЕТРА (суммы по кампании смешаются)"
L=$(wc -l < "$ROOT/meter/meter.jsonl" 2>/dev/null || echo 0)
[ "$L" != "0" ] && { say "meter.jsonl" "$L строк — ротируй"; BAD=1; } || say "meter.jsonl" "пуст"

echo "== 7. ОБЩИЙ VENV АРЕНЫ (чужое расширение видно КАЖДОЙ идущей оценке)"
# `evaluate_results.py:266` runs `pip install .` over any candidate carrying a `setup.py`, so
# anything that lands in the arena's site-packages is importable by every concurrent evaluation.
# The bridge redirects its installs with PIP_TARGET (d439c966), but nothing was WATCHING the venv:
# on 2026-08-28 at 21:20 our own test suite dropped `_kern.cpython-311-*.so` and `kern-0.0.0.dist-info`
# there while two probe evaluations were running, and it was found by an ad-hoc sweep, not by this
# script. The baseline is the venv as built on 2026-08-20; anything NEWER than the arena's own
# `python` binary was put there after the fact.
SITE="$ROOT/AlgoTune/.venv/lib/python3.11/site-packages"
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
