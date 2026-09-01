#!/bin/bash
# Failures in a pytest log that is STILL RUNNING, and how far it has got.
#
#   benchmarks/pytest_progress.sh <log>
#
# ПОЧЕМУ ЭТО ЕСТЬ. `grep -c '^FAILED' <log>` на живом прогоне ВСЕГДА возвращает 0, и это не
# редкий случай, а устройство pytest: строки `FAILED` печатаются только в итоговой сводке. В
# логе на 754 строки они стояли на 752-й, тогда как сами падения прошли точками прогресса на
# 3%, 46% и 74%. 01.09 я трижды доложил «падений пока 0» по этому счётчику, дважды — когда два
# падения уже случились. Прибор был слеп по построению, а выглядел как измерение.
#
# Считаем `F` и `E` ТОЛЬКО в строках прогресса — тех, что кончаются на `[ NN%]`. Иначе в счёт
# попадают буквы из слова FAILED в сводке и из любого текста трассировки.
set -u
LOG="${1:?usage: pytest_progress.sh <log>}"
[ -r "$LOG" ] || { echo "no such log: $LOG" >&2; exit 2; }

# Одной программой на awk: строка прогресса — это та, что кончается на `[ <число>% ]`.
awk '
  /\[ *[0-9]+%\]$/ {
    line = $0
    sub(/[ \t]*\[ *[0-9]+%\][ \t]*$/, "", line)   # отрезаем сам счётчик процентов
    n = split(line, ch, "")
    for (i = 1; i <= n; i++) {
      if (ch[i] == "F") f++
      else if (ch[i] == "E") e++
      else if (ch[i] == ".") p++
      else if (ch[i] == "s") s++
    }
    if (match($0, /[0-9]+%/)) pct = substr($0, RSTART, RLENGTH)
  }
  END {
    printf "%s  passed=%d failed=%d errors=%d skipped=%d\n",
           (pct == "" ? "0%" : pct), p+0, f+0, e+0, s+0
    if (f+e > 0) exit 1
  }
' "$LOG"
