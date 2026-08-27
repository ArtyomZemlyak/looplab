#!/bin/bash
# Состояние живых проб: узлы, деньги, ПАДЕНИЯ УЗЛОВ и отказы шлюза, дошедшие до цикла.
#
# ДВА УРОКА, оба стоили ложных докладов 27.08:
#
#  1. ЖУРНАЛ СОБЫТИЙ — НЕ «СТРОКА = СОБЫТИЕ». `eventstore.py` пишет крах-атомарные ПАКЕТЫ: один
#     конверт с `type` в виде ОДНОЭЛЕМЕНТНОГО СПИСКА `["__looplab_event_batch_v1__"]` и настоящими
#     событиями внутри `data.events`. Форма списком выбрана нарочно — она невалидна для старого
#     контракта `Event.type: str`, поэтому пре-пакетный читатель упирается в свой забор целостности
#     и отказывается дописывать журнал, вместо того чтобы принять один непонятный event и потерять
#     вложенные. Мой разбор группировал по `e['type']` и (а) падал с `unhashable type: 'list'`,
#     (б) три обхода подряд не видел `node_failed` у qwen38f, из-за чего я докладывал «ноль узлов»
#     вместо «узел был и упал». Десять таких конвертов лежат в корпусе, девять — в измеренных
#     прогонах плеча B.
#
#  2. ОТКАЗ ШЛЮЗА, ДОШЕДШИЙ ДО ЦИКЛА, НЕ ИМЕЕТ ТИПА СО СЛОВОМ error. 403 приходит внутри memo
#     обычного `research_completed`: «deep research unavailable: ... Error code: 403». Я дважды
#     докладывал «до цикла не дошла ни одна неудача», потому что искал по имени типа.
set -u
ROOT=${ROOT:-/var/tmp/looplab-bench}
python3 - "$ROOT" <<'PY'
import json, sys, time, glob, os, collections
root = sys.argv[1]

# ЖИВОСТЬ — ПО КАТАЛОГУ ПРОГОНА, И НЕ ПРО СЕБЯ. Проверка «подстрока в /proc/*/cmdline» находит СВОЙ
# ЖЕ процесс: исходник этого скрипта содержит и "looplab.cli run", и "fullctx", поэтому мёртвая
# проба выглядела живой. Это третий случай самосовпадения за 27.08 (детектор зомби, список на
# снятие, и вот этот), поэтому: исключаем себя и всю цепочку предков, и сверяем по --out, а не по
# вольной подстроке.
_ME = set()
_p = os.getpid()
for _ in range(32):
    if _p <= 1:
        break
    _ME.add(_p)
    try:
        _p = int(open(f"/proc/{_p}/stat").read().rsplit(")", 1)[1].split()[1])
    except OSError:
        break


def _alive(run_dir: str) -> bool:
    want = os.path.realpath(run_dir)
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) in _ME:
            continue
        try:
            argv = open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        if "looplab.cli" not in " ".join(argv):
            continue
        # ЛЮБОЙ аргумент, а не только тот, что идёт за `--out`: `looplab.cli run` называет каталог
        # флагом, а `looplab.cli resume` — ПОЗИЦИОННО. Привязка к `--out` показывала возобновлённую
        # пробу остановленной, пока она делала девять успешных вызовов в минуту.
        for a in argv:
            if a and os.path.realpath(a) == want:
                return True
    return False


def expand(path):
    """Физические строки -> настоящие события, с разворотом пакетных конвертов."""
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("type")
        if not isinstance(t, str) or t == "__looplab_event_batch_v1__":
            out.extend((e.get("data") or {}).get("events") or [])
        else:
            out.append(e)
    return out

for run in sorted(glob.glob(os.path.join(root, "model-probes/*/runs/*/run"))
                  + glob.glob(os.path.join(root, "fullctx-probe/runs/*/run"))):
    log = os.path.join(run, "events.jsonl")
    if not os.path.exists(log):
        continue
    label = run.split("/")[-4] if "model-probes" in run else "fullctx"
    ev = expand(log)
    usage = [e for e in ev if e.get("type") == "llm_usage"]
    spend = sum(float((e.get("data") or {}).get("cost") or 0) for e in usage)
    scores = []
    for f in sorted(glob.glob(os.path.join(run, "nodes/*/score.log"))):
        for line in open(f, encoding="utf-8", errors="replace"):
            if line.strip().startswith("{"):
                d = json.loads(line)
                scores.append((os.path.basename(os.path.dirname(f)), d.get("speedup"), d.get("eval_seconds")))
    good = [s for _, s, _ in scores if s is not None]
    alive = _alive(run)
    print("  %-10s %s $%.3f  %2d узл  лучший %-9s  тишина %.0fс" % (
        label, "жив " if alive else "стоп", spend, len(scores),
        max(good) if good else None, time.time() - ev[-1]["ts"] if ev else -1))
    if scores:
        print("       " + "  ".join("%s=%s" % (n.replace("node_", "n"), s) for n, s, _ in scores))
    for n, s, e in scores:
        if s in (0, 0.0) and (e or 99) < 1:
            print("       ОТКАЗ ЛИНЕЙКИ %s eval_s=%s" % (n, e))
    # то, что прячется в пакетах
    for e in ev:
        if e.get("type") in ("node_failed", "node_abandoned", "stage_failed"):
            d = e.get("data") or {}
            print("       ПАДЕНИЕ УЗЛА %s node=%s: %s" % (
                time.strftime("%H:%M:%S", time.localtime(e["ts"])), d.get("node_id"),
                str(d.get("error") or d.get("reason"))[:150]))
    # то, что прячется в тексте
    degraded = [e for e in ev if "unavailable" in json.dumps(e.get("data") or {})]
    if degraded:
        print("       ОТКАЗ ШЛЮЗА ДОШЁЛ ДО ЦИКЛА: %d раз (напр. %s)" % (
            len(degraded), time.strftime("%H:%M:%S", time.localtime(degraded[-1]["ts"]))))
PY
