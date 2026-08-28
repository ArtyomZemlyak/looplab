#!/bin/bash
# Деньги и неудачи по обоим счётчикам, по правилу САМОГО прокси, а не по коду ответа.
#
# ПОЧЕМУ НЕ `status != 200`: строка, где шлюз честно ответил 200 и открыл поток, а клиент отвалился
# на середине, несёт `status: 200` — и это правда, так ответил ШЛЮЗ. Неудача лежит в отдельном поле
# `error`, а `metered` остаётся false. Прокси в healthz (proxy.py:416) считает ошибкой
# `status >= 400 ИЛИ есть error`, и любая сводка обязана считать так же.
#
# 27.08 я весь день докладывал «не-200 нет», фильтруя по коду ответа. В этот момент в леджере 8802
# лежала строка qwen38f: status 200, attempts 6, queued_s 60.0, BrokenPipeError, ноль дельт,
# латентность 95 с. Мой фильтр её не видел, healthz видел.
set -u
ROOT=${ROOT:-/var/tmp/looplab-bench}
HOURS=${1:-3}
python3 - "$ROOT" "$HOURS" <<'PY'
import json, re, sys, time, collections, os
root, hours = sys.argv[1], float(sys.argv[2])
T = time.time() - hours * 3600
# THREE LEDGERS, NOT TWO, since 2026-08-28 08:04. The 8801 process has been alive since 2026-08-24
# 10:11 and is FIVE commits behind the file it was started from -- 2afb287c (the synthetic usage
# frame in the shape a client actually reads), 5f253594 (an aborted stream is not an error),
# 0bdc1866, 10a79c3e (a call retried five times recorded attempts=1, queued_s=0.0) and 903759be.
# Restarting it under four live probes would drop their in-flight calls into the provider circuit
# breaker, so instead a proxy on the CURRENT code runs beside it on 8803 with its own ledger and
# new probes are pointed there. A ledger this monitor cannot see is money it cannot reconcile,
# which is the whole reason this list is not hard-coded to the old pair.
for name, path in (("8801 шлюз (код от 24.08)", "meter/meter.jsonl"),
                   ("8803 шлюз (текущий код)", "meter/meter-8803.jsonl"),
                   ("8802 openrouter", "meter/meter-gemini.jsonl")):
    p = os.path.join(root, path)
    if not os.path.exists(p):
        continue
    rows = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
    new = [x for x in rows if float(x["ts"]) >= T]
    # ПРАВИЛО ПРОКСИ, дословно: proxy.py:416
    def cause(x):
        """Почему упало. Поле зависит от ВЕТКИ прокси, а не от вида отказа, и это стоило мне
        ложного вывода: 27.08 два 502 у ctlEdge я назвал «записаны без причины», потому что
        печатал `error` и получал None. Причина лежала рядом, в `upstream_error` — целиком
        страница nginx «502 Bad Gateway». `_fail` пишет `error`, ветка HTTPError пишет
        `upstream_error`; сводка обязана знать обе."""
        for field in ("error", "upstream_error"):
            v = x.get(field)
            if not v:
                continue
            text = " ".join(str(v).split())
            # Ответ шлюза бывает страницей, а не строкой: nginx отдаёт целый html. Заголовок
            # <title> — это ровно одна фраза, которая нужна («502 Bad Gateway»), а вся разметка
            # в метке счётчика делает её нечитаемой.
            m = re.search(r"<title>(.*?)</title>", text, re.I)
            if m:
                return "%s (%s)" % (m.group(1).strip(), x.get("status"))
            return text[:60]
        return str(x.get("status"))

    bad = [x for x in new if int(x.get("status") or 0) >= 400 or x.get("error")]
    spent = sum(float(x.get("cost") or 0) for x in new)
    # КОГДА БЫЛ ПОСЛЕДНИЙ ВЫЗОВ — часть ответа, а не украшение. Окно в час включает и то, что
    # кончилось пятьдесят минут назад, и дважды подряд (27.08, два обхода) я принимал хвост
    # завершившейся пробы за неопознанную активность на шлюзе. Сводка обязана сказать читателю,
    # что окно уже остыло.
    last = max((float(x["ts"]) for x in new), default=0.0)
    when = ("последний вызов %s, %.0f мин назад" % (
        time.strftime("%H:%M:%S", time.localtime(last)), (time.time() - last) / 60)) if new else "тишина"
    print("  %-16s за %.0f ч: вызовов %d, $%.4f, НЕУДАЧ %d | %s" % (
        name, hours, len(new), spent, len(bad), when))
    if bad:
        # КОГДА БЫЛА ПОСЛЕДНЯЯ НЕУДАЧА — вторая половина того же урока. Окно в час держит и то, что
        # кончилось сорок минут назад: 27.08 в 09:56 эта сводка показала 12 отказов 403 у glm53f, и
        # я пошёл их расследовать, а все они лежали в 09:00-09:14, до перезапуска счётчика, после
        # которого их ноль. Возраст последнего ВЫЗОВА тут уже печатается с утра; возраст последней
        # НЕУДАЧИ отсутствовал, и именно он отвечает на вопрос «это сейчас или это уже прошло».
        newest = max(float(x["ts"]) for x in bad)
        print("      последняя неудача %s, %.0f мин назад" % (
            time.strftime("%H:%M:%S", time.localtime(newest)), (time.time() - newest) / 60))
        c = collections.Counter("%s %s" % (x.get("arm"), cause(x)) for x in bad)
        for k, v in c.most_common(8):
            g = [x for x in bad if ("%s %s" % (x.get("arm"), cause(x))) == k]
            print("      %-42s %3d  последняя %s" % (
                k, v, time.strftime("%H:%M:%S", time.localtime(max(float(x["ts"]) for x in g)))))
    # то, что видел бы наивный фильтр
    naive = [x for x in new if str(x.get("status")) != "200"]
    if len(naive) != len(bad):
        print("      (фильтр по коду ответа показал бы %d — на %d меньше)" % (len(naive), len(bad) - len(naive)))
PY

# ВОЗРАСТ ПРОЦЕССА ПРОТИВ ВОЗРАСТА ЕГО КОДА.
#
# Это прожило четыре дня незамеченным. Прокси 8801 стартовал 2026-08-24 10:11:59, а
# `benchmarks/meter/proxy.py` менялся пять раз после этого — включая правку, после которой
# оборванный поток перестал считаться ошибкой, и правку счётчика повторов. Все эти дни монитор
# бодро печатал суммы и неудачи, посчитанные кодом, которого в дереве уже нет. docs/53 §9 записал
# это 26.08 и оно осталось верным, потому что НИКТО НЕ СВЕРЯЛ. Теперь сверяет.
#
# По /proc, а не через pkill: шаблон pkill матчит собственную командную строку.
python3 - "$ROOT" <<'PY'
import os, sys, time
root = sys.argv[1]
src = os.environ.get("PROXY_SRC_OVERRIDE") or os.path.join(root, "looplab", "benchmarks", "meter", "proxy.py")
if not os.path.exists(src):
    src = os.path.join(root, "benchmarks", "meter", "proxy.py")
if os.path.exists(src):
    mtime = os.path.getmtime(src)
    boot = time.time() - float(open("/proc/uptime").read().split()[0])
    hz = os.sysconf("SC_CLK_TCK")
    stale = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            parts = [a for a in open(f"/proc/{pid}/cmdline", "rb").read().decode(
                "utf8", "replace").split("\0") if a]
            started = boot + int(open(f"/proc/{pid}/stat").read().rsplit(") ", 1)[1].split()[19]) / hz
        except (OSError, IndexError):
            continue
        # BY ARGV ELEMENTS, NOT BY SUBSTRING. A substring search over the whole command line matches
        # THIS monitor's own shell, whose `bash -c` argument contains the heredoc that mentions
        # proxy.py -- it reported five stale proxies where three exist. The same self-match that
        # `pkill -f` is banned for. So: the interpreter must be python and some argument must BE
        # proxy.py, as a path element rather than as text.
        if not parts or not os.path.basename(parts[0]).startswith("python"):
            continue
        if not any(os.path.basename(a) == "proxy.py" for a in parts):
            continue
        port = "?"
        if "--port" in parts:
            i = parts.index("--port")
            if i + 1 < len(parts):
                port = parts[i + 1]
        if started < mtime:
            stale.append((pid, port, (mtime - started) / 3600))
    for pid, port, hours in sorted(stale, key=lambda r: -r[2]):
        print(f"  УСТАРЕВШИЙ ПРОКСИ: pid={pid} порт={port} стартовал на {hours:.1f} ч РАНЬШЕ "
              f"последней правки proxy.py — его числа считает код, которого в дереве нет")
    if not stale:
        print("  прокси: все процессы новее своего кода")
PY
