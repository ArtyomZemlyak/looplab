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
import json, sys, time, collections, os
root, hours = sys.argv[1], float(sys.argv[2])
T = time.time() - hours * 3600
for name, path in (("8801 шлюз", "meter/meter.jsonl"), ("8802 openrouter", "meter/meter-gemini.jsonl")):
    p = os.path.join(root, path)
    if not os.path.exists(p):
        continue
    rows = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
    new = [x for x in rows if float(x["ts"]) >= T]
    # ПРАВИЛО ПРОКСИ, дословно: proxy.py:416
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
        c = collections.Counter(
            "%s %s" % (x.get("arm"), (str(x.get("error")).split(":")[0] if x.get("error") else x.get("status")))
            for x in bad)
        for k, v in c.most_common(8):
            print("      %-42s %d" % (k, v))
    # то, что видел бы наивный фильтр
    naive = [x for x in new if str(x.get("status")) != "200"]
    if len(naive) != len(bad):
        print("      (фильтр по коду ответа показал бы %d — на %d меньше)" % (len(naive), len(bad) - len(naive)))
PY
