#!/bin/bash
# Фоновые ожидалки вида `until ! pgrep …; do sleep …; done`, оставшиеся от прошлых проверок.
#
# ДВА УРОКА ВНУТРИ, оба стоили ложных срабатываний:
#  * НИКОГДА не `pkill -f` — шаблон матчит командную строку самого убийцы. Только обход /proc.
#  * И сам ДЕТЕКТОР обязан исключать себя. Версия 27.08 02:5x искала подстроку 'pgrep' в argv и
#    нашла двоих: собственный `python3 -c` (в исходнике которого лежала эта подстрока) и обёртку
#    `bash -c`, через которую он был запущен. Ноль настоящих зомби, два обвинения.
#    Поэтому: исключаем свой pid, своего родителя и всю цепочку предков до init.
python3 - "$$" <<'PY'
import os, sys
me = int(sys.argv[1])
mine = set()
p = me
for _ in range(32):                      # своя цепочка предков — под подозрение не попадает
    if p <= 1: break
    mine.add(p)
    try:
        p = int(open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[1])
    except OSError:
        break
mine.add(os.getpid())

found = 0
for pid in os.listdir("/proc"):
    if not pid.isdigit() or int(pid) in mine:
        continue
    try:
        argv = open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "replace")
    except OSError:
        continue
    # Ожидалка — это цикл, который И опрашивает, И спит. Одного слова мало.
    if "pgrep" in argv and "sleep" in argv and ("until" in argv or "while" in argv):
        print("  ЗОМБИ pid=%s  %s" % (pid, argv.replace("\0", " ")[:100]))
        found += 1
print("  зомби-оболочек: %d" % found)
PY
