#!/bin/bash
{                       # <-- ВЕСЬ СКРИПТ ОДНОЙ КОМАНДОЙ, И ЭТО НЕ СТИЛЬ.
# Bash читает файл ПО МЕРЕ ИСПОЛНЕНИЯ, по смещению. Тело уже было завёрнуто в `main()`, а внизу
# стояло `main "$@"` и `exit "$?"` — но это ДВЕ команды: между ними оболочка возвращается к файлу
# по сохранённому смещению, и если файл за это время вырос, смещение указывает в середину чужой
# строки. Замок не держал.
#
# Воспроизведено 01.09 на минимальном скрипте: правка +40 строк в середину, пока он работает, дала
#   BODY START / BODY END / "padding: command not found" / BODY START / BODY END / syntax error
# — ТЕЛО ПРОГНАЛОСЬ ВТОРОЙ РАЗ. Это и есть потеря 27.08, когда проба потратила $0.0743 сверх
# потолка после того, как движок честно упёрся в него и вышел. В тот же день это случилось СНОВА:
# я правил этот файл, пока по нему шли четыре пробы, и `remEEctl1` закончилась строкой
# `run_probe.sh: line 291: -c: command not found`.
#
# В фигурных скобках bash обязан разобрать ВСЮ команду до первого исполнения, поэтому правка на
# ходу больше ничего не меняет: та же проверка с обёрткой даёт одно исполнение тела и rc=0.
#
# И ВТОРАЯ ПОЛОВИНА, ОПЕРАЦИОННАЯ: правь этот файл только через АТОМАРНУЮ ПОДМЕНУ (запись во
# временный файл + `os.replace`). Новый inode — работающие оболочки дочитывают старый и завершаются
# чисто; измерено там же. Правка «на месте» (`write_text`) переиспользует inode, и все идущие пробы
# получают ту самую ловушку.
# ОДНА модель OpenRouter на `edge_expansion`, бюджет $1.00. Вызывается по одной на полосу.
#   run_probe.sh <модель> <короткая-метка> <полоса> [задача] [база-счётчика] [бюджет]
# (`run_model_probe.sh` — старое имя, оно теперь `exec`-ает этот файл. Строка использования
#  звала ЕГО, пока он был отдельной непочиненной копией: оператор, читающий починенный
#  скрипт, запускал сломанный.)
#
# Задача по умолчанию `edge_expansion`, счётчик по умолчанию 8802 (OpenRouter). Для контрольных
# прогонов на корпоративном шлюзе: ... <задача> http://127.0.0.1:8801
#
# ПОЧЕМУ edge_expansion: это задача, на которой плечо B дало свой лучший результат 27.1295, и на
# ней же 21 августа гоняли google/gemini-3.7-flash. То есть у неё есть и верхняя планка, и
# известный провал — обе точки отсчёта для новой модели.
#
# ЧТО ЭТО НЕ ЕСТЬ: прямое сравнение с 27.1295. То число измерено БЕЗ `--full-context`, а он теперь
# включён по умолчанию, так что разница смешивает два изменения — модель и карточку. Чистое
# сравнение потребует контрольного прогона deepseek на этой же задаче с тем же флагом; полосы под
# него сейчас нет, он идёт следующим.
#
# ДЕНЬГИ. Профиль наших запросов измерен 26.08: ~5.1M промпт-токенов и ~0.82M ответных на задачу.
# При $1 три модели получают РАЗНЫЙ объём работы, и это осознанно — сравниваем равные деньги, а не
# равное число вызовов:
#   z-ai/glm-5.3-flash   $0.075/$0.25 -> ~$0.59 за полный прогон, помещается целиком
#   qwen/qwen3.8-flash   $0.16 /$0.47 -> ~$1.20, около 83% прогона
#   openai/gpt-5.6-luna  $0.20 /$1.20 -> ~$2.00, около половины прогона
#
# GLM-5.3-flash = бывшая `stealth/ox-alpha`; шлюз на неё отвечает 404 с текстом «This model was
# ZAI's GLM-5.3 Flash». Отдельно её гонять не нужно, это одна модель.
# Она рассуждающая: при max_tokens=64 все 64 токена ушли в `reasoning`, а `content` остался пуст.
# LoopLab потолок не ставит вовсе (в коде нет `max_tokens`, наблюдаемые ответы 1222..7171 токенов),
# поэтому здесь опасности нет — но у любого клиента, который его ставит, GLM будет «молчать».
# ВСЁ ТЕЛО — В ФУНКЦИИ, И ЭТО НЕ СТИЛЬ. Bash читает скрипт ПО МЕРЕ ИСПОЛНЕНИЯ, по смещению в файле.
# 27.08 в 04:29 я отредактировал этот файл, пока три его копии работали; смещения сдвинулись, и когда
# у двух из них `looplab.cli run` завершился, оболочка прочитала «следующую команду» уже из другого
# места и ПРОГНАЛА ТЕЛО ЗАНОВО:
#   qwen38f  — перезапуск в 06:06:41, сразу после падения узла 0;
#   gpt56luna — перезапуск в 07:11:32, сразу после того, как движок ЧЕСТНО упёрся в потолок
#               («Refused: LLM spend ceiling reached: $1.0003 of the $1.0000») и вышел.
# Второй запуск попал в тот же каталог, CLI справедливо переоткрыл завершённый прогон, и модель
# потратила ещё $0.0743 сверх потолка, купив на них один невалидный узел. Потолок не виноват —
# он сработал; виновата оболочка, дочитавшая изменённый файл.
# Определение функции разбирается целиком до первого вызова, поэтому правка на ходу больше не может
# перезапустить тело.
#
# И ВТОРОЙ ЗАМОК: каталог с уже записанным терминальным событием не переиспользуется. Даже если тело
# как-то запустится дважды, второй запуск откажется, а не продолжит тратить чужой бюджет.
main() {
set -u
MODEL="$1"; LABEL="$2"; LANE="$3"; TASK="${4:-edge_expansion}"; METER="${5:-http://127.0.0.1:8802}"; BUDGET="${6:-1.00}"
ROOT=/var/tmp/looplab-bench
# КУДА ПИШЕТ ПРОБА — отдельно от того, ГДЕ СТОИТ СТЕНД. Стенд неподвижен (в нём venv,
# AlgoTune и линейка), а вот выход перенаправляем, и это не удобство: тесты этого скрипта
# гоняют его насухо и до сих пор писали ПРЯМО В КОРПУС — шесть каталогов `t_instr_*`
# лежали среди настоящих проб 01.09. Сейчас в них только INSTRUMENT.txt, и потому это
# ничего не испортило; тест, который однажды оставит там events.jsonl, попадёт в каждую
# сводку по корпусу молча и без следа в git.
OUT=${PROBE_OUT_ROOT:-$ROOT/model-probes}/$LABEL
LOG=$OUT/probe.log
mkdir -p "$OUT/ws" "$OUT/runs/$TASK/memory" "$OUT/runs/$TASK/knowledge"
say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

cd "$ROOT/looplab" && . ./benchmarks/box-jhub-l40s.sh > /dev/null

# ВТОРОЙ ЗАМОК: каталог с терминальным событием не переиспользуется. Даже если тело как-то
# запустится дважды, второй запуск откажется, а не продолжит тратить исчерпанный бюджет.
RUNDIR="$OUT/runs/$TASK/run"
if [ -f "$RUNDIR/events.jsonl" ] && grep -q '"run_finished"' "$RUNDIR/events.jsonl"; then
  say "ОТКАЗ: $RUNDIR уже несёт run_finished — второй прогон переоткрыл бы его и тратил дальше."
  say "       для нового прогона возьми другую метку."
  exit 1
fi

# СПРАШИВАЕМ У САМОГО ЗАБОРА, а не судим по именам. Первая версия этого охранника считала чужим
# всё, кроме `REC-*` и `RuleCheck-*`, и отказала бы пробе из-за `DevEvalTrain-<pid>` — каталога,
# который наша же команда `eval_train` создаёт на одну оценку и тут же убирает. Второе мнение о том,
# что чужое, — это второй способ ошибиться; правило живёт в одном месте.
if ! bash "$ROOT/looplab/benchmarks/algotune/fence_foreign_results.sh" check > /tmp/fence.$$ 2>&1; then
  say "ОТКАЗ: забор открыт"
  sed 's/^/       /' /tmp/fence.$$ | tee -a "$LOG"
  say "       закрой: bash $ROOT/looplab/benchmarks/algotune/fence_foreign_results.sh close"
  rm -f /tmp/fence.$$
  exit 1
fi
rm -f /tmp/fence.$$
say "забор закрыт"

# Полоса должна быть свободна СЕЙЧАС.
BUSY=$(python3 - "$LANE" <<'PYEOF'
import os, sys
want = set()
for part in sys.argv[1].split(","):
    a, b = part.split("-"); want.update(range(int(a), int(b) + 1))
n = 0
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        argv = [x.decode("utf-8", "replace")
                for x in open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")[:-1] if x]
        if not argv or "-c" in argv:
            continue
        if not any(k in " ".join(argv) for k in ("AlgoTuner", "algotune.sh", "looplab.cli")):
            continue
        if os.sched_getaffinity(int(pid)) & want:
            n += 1
    except Exception:
        continue
print(n)
PYEOF
)
[ "$BUSY" != "0" ] && { say "ОТКАЗ: на полосе $LANE уже $BUSY процесс(ов)"; exit 1; }

# У ЗАДАЧИ ДОЛЖЕН БЫТЬ ДАТАСЕТ. Арена держит на диске ровно двадцать задач кампании
# (`.hf_datasets/oripress__AlgoTune/data/<task>/<task>_T*ms_n*_size*_{train,test}.jsonl`), а
# `make_task.py` строит карточку для ЛЮБОГО имени — так что проба на задаче без данных стартует,
# тратит деньги и не может быть оценена ничем.
#
# ЭТО НЕ УМОЗРЕНИЕ: 28.08 в 15:58 я запустил `count_connected_components`, выбрав её по числу
# опубликованных чемпионов с `.pyx`, и снял через минуту на $0.0042 — датасета у неё нет. Ни один
# сторож этого не поймал: полоса была свободна, забор закрыт, каталог чист. Проверка стоит один
# `ls` и снимает целый класс потраченных впустую прогонов.
DATA_DIR="$ROOT/AlgoTune/.hf_datasets/oripress__AlgoTune/data/$TASK"
if ! ls "$DATA_DIR"/${TASK}_T*ms_n*_size*_train.jsonl >/dev/null 2>&1; then
  say "ОТКАЗ: у задачи '$TASK' нет датасета в $DATA_DIR — оценить её нечем."
  say "       на диске есть: $(ls "$ROOT/AlgoTune/.hf_datasets/oripress__AlgoTune/data" 2>/dev/null | tr '\n' ' ')"
  exit 1
fi

# ЗАБОР ДОЛЖЕН БЫТЬ ЗАКРЫТ. В AlgoTune/results лежат чемпионы GPT-5, Claude Opus 4.6, Gemini 3 Pro
# и ещё четырнадцати моделей — 34 каталога по одним только `edge_expansion` и `convex_hull`. Забор
# уносит их в укрытие на время прогона.
#
# ПОЧЕМУ ОТКАЗ, А НЕ АВТОЗАКРЫТИЕ: пробы идут по одной на полосу, параллельно. Если бы каждая
# закрывала забор при старте и открывала его trap'ом на выходе, первая же финишировавшая открыла
# бы его под ногами у двух ещё живых. Забор — состояние на всю машину, и управлять им должен один
# хозяин. Скрипт лишь отказывается стартовать в грязи.
#
# ЭТО НЕ УМОЗРЕНИЕ: 27.08 в 04:07 я снял забор вручную после остановки кампании (её драйвер
# открывает его своим trap'ом, но был убит), а в 04:17 запустил три пробы, не закрыв обратно. Они
# отработали 11 минут при открытом заборе. Заражения не случилось — ноль упоминаний чужих путей в
# спанах, редактируемый путь агента заперт в его рабочем каталоге, — но проверять это постфактум
# по atime бесполезно: раздел смонтирован с `relatime`, и чтение свежее суток отметку не обновляет.


# СУХОЙ ПРОГОН. Охранник, который нельзя проверить, не потратив доллар, проверяться не будет.
# Первая версия опровергателя к этому охраннику прошла проверку и поехала настоящим прогоном на
# 18 вызовов и $0.0070, прежде чем я её снял. `PROBE_DRY_RUN=1` останавливает скрипт сразу после
# всех отказов — до make_task, до модели, до денег.
# ТА ЖЕ ЛИНЕЙКА, что у двадцати измеренных чисел: общий кэш эталона и `auto` (= 22 воркера по
# одному ядру, ключ `__w22x1r3`). `1` дал бы `__lane22r3` — другой эталон, расходящийся на 24%.
export ALGOTUNE_BASELINE_CACHE_DIR="$ROOT/looplab/benchmarks/algotune/.baseline_times"
export ALGOTUNE_EVAL_WORKERS=auto
export ALGOTUNE_MIN_TIMEOUT_S=120
# И ТОТ ЖЕ КОРЕНЬ РЕПОЗИТОРИЯ, что у кампании. `make_task.py` запускается по пути, поэтому
# `sys.path[0]` — это `benchmarks/algotune`, а не рабочий каталог: `cd "$ROOT/looplab"` выше
# движок импортируемым НЕ делает. `session_budget_s()` при этом возвращает None, и карточка теряет
# долю сессии — измерено 2026-08-30 на синтетическом чекауте: кампания (PYTHONPATH выставлен)
# печатает «bounded at <N> s ... about 3 % of your session», проба — «bounded by a wall clock
# nobody shows you». Одна и та же карточка, разные предложения, и число сравнивается как одно.
export PYTHONPATH="$ROOT/looplab${PYTHONPATH:+:$PYTHONPATH}"
export LOOPLAB_LLM_BUDGET_USD="$BUDGET"

# WHICH INSTRUMENT THIS PROBE RAN ON, written into its own tree.
#
# A probe records what the run DID and nothing about the gateway it did it through. Measured
# 2026-09-01: `accEE`, `accPde` and `remEE` ran entirely UNSTREAMED, and `remEE` lost 9 calls to the
# gateway's 300 s ceiling -- 45 minutes returning nothing on a $1.00 budget. That is invisible in
# every score, card and run log; it survives only as a `stream` field on the meter's rows, and every
# comparison in `docs/56` is drawn from probe trees. §73 built a "controlled pair" on `remEE` against
# a streamed run before anyone looked, and the conclusion had to be withdrawn (§80).
#
# The meter is one file for the whole box and is not copied into a snapshot; a probe tree is. So the
# settings that decide what the ruler measures go in beside the run, at launch, before a token is
# spent. The API key is NOT among them -- only what changes the measurement.
# УЧЁТНАЯ ПАРА ЦЕЛИКОМ, И ОБЕ ПОЛОВИНЫ ОТСЮДА. Счётчик держит ключ к шлюзу сам, поэтому
# клиенту НАСТОЯЩИЙ ключ не нужен — но движок требует пару `LOOPLAB_LLM_API_KEY` +
# `LOOPLAB_LLM_API_KEY_BASE_URL` неразрывно и отказывается стартовать, если задана одна
# половина. Скрипт задавал только `_BASE_URL`, а ключ МОЛЧА наследовал из оболочки: пробы
# шли лишь потому, что оператор перед запуском делал `source .env`. Из чистого окружения
# (`setsid nohup`) тот же скрипт умирал за секунду с rc=2 — уже ПОСЛЕ проверки забора и
# записи прибора, то есть выглядел исправным ровно до момента траты. Вдобавок
# унаследованный ключ выписан на шлюз, а `_BASE_URL` здесь переставлялся на 127.0.0.1 —
# ровно та полупереопределённая пара, ради запрета которой отказ и написан.
# Значение `meter`: счётчик его игнорирует, а ключ шлюза остаётся у счётчика.
export LOOPLAB_LLM_MODEL="$MODEL"
export LOOPLAB_LLM_BASE_URL="$METER/m/$LABEL/$TASK/p1/v1"
export LOOPLAB_LLM_API_KEY_BASE_URL="$LOOPLAB_LLM_BASE_URL"
export LOOPLAB_LLM_API_KEY=meter
export OPENAI_BASE_URL="$LOOPLAB_LLM_BASE_URL"
export OPENAI_API_KEY=meter

# $PROBE_MAKE_TASK_ARGS is UNQUOTED on purpose: it carries goal VARIANTS for control arms
# (`--no-unteachable-rules` is the first) and word splitting is how a caller passes two of
# them. It is empty in every ordinary probe, and `run_probe.sh` is not run on untrusted input.
# shellcheck disable=SC2086
python3 "$ROOT/looplab/benchmarks/algotune/make_task.py" --algotune-root "$ROOT/AlgoTune" \
    --task "$TASK" --out-dir "$OUT/ws" --deliver --one-card --enforce-rules \
    ${PROBE_MAKE_TASK_ARGS:-} >> "$LOG" 2>&1 \
  || { say "make_task ПРОВАЛИЛСЯ — см. $LOG"; exit 1; }

mkdir -p "$OUT"
{
  echo "probe:          $LABEL"
  echo "task:           $TASK"
  echo "lane:           $LANE"
  echo "budget_usd:     $BUDGET"
  echo "model:          $MODEL"
  echo "started:        $(date -Iseconds)"
  echo "LOOPLAB_LLM_STREAM=${LOOPLAB_LLM_STREAM:-(unset -> engine default)}"
  echo "ALGOTUNE_EVAL_WORKERS=${ALGOTUNE_EVAL_WORKERS}"
  echo "ALGOTUNE_BASELINE_CACHE_DIR=${ALGOTUNE_BASELINE_CACHE_DIR}"
  echo "ALGOTUNE_MIN_TIMEOUT_S=${ALGOTUNE_MIN_TIMEOUT_S}"
  echo "looplab:        $(cd "$ROOT/looplab" && git log --oneline -1 2>/dev/null)"
  echo "looplab_dirty:  $(cd "$ROOT/looplab" && git status --porcelain 2>/dev/null | wc -l) file(s)"
  echo "AlgoTune:       $(cd "$ROOT/AlgoTune" && git log --oneline -1 2>/dev/null)"
  # RECORDED, not merely honoured. A probe whose card differs from its neighbours' and whose
  # tree does not say so is a run that cannot be pooled and cannot be excluded either --
  # accEE and remEE cost a dollar each and are unusable for exactly that reason, having run
  # unstreamed with nothing in the tree to say it. Printed even when empty, so "this probe
  # took the default card" is a POSITIVE statement in the record rather than a missing line.
  echo "card_args:      ${PROBE_MAKE_TASK_ARGS:-(none -- the shipped card)}"
  # ХЕШ САМОЙ КАРТОЧКИ, а не только флагов. Флаги называют вариант; хеш ловит любое
  # изменение текста -- новый пункт, поправленную формулировку, сдвинувшуюся константу.
  # 01.09 медиана обращений к reference-модулю по 28 пробам вышла 7.6 %, ровно внутри
  # доклаузульной полосы §69.1 в 4.9-8.3 %, и РАЗДЕЛИТЬ корпус по версии карточки было
  # нечем: до сегодня ни одна проба не записывала, какую карточку получила.
  echo "card_sha256:    $(python3 -c 'import hashlib,json,sys;print(hashlib.sha256(json.load(open(sys.argv[1]))["goal"].encode()).hexdigest()[:16])' "$OUT/ws/algotune_$TASK.json" 2>/dev/null || echo "(unreadable)")"
  # Полнота пары, а не ключ: имена и вердикт. Отказ движка стоит секунды и случается уже
  # после этой записи, так что прибор — единственное место, где видно, что пара собрана.
  # НИКАКИХ `${KEY:-...}` ЗДЕСЬ. Первая версия этой строки писала
  # `${LOOPLAB_LLM_API_KEY:+set}${LOOPLAB_LLM_API_KEY:-MISSING}` — когда ключ ЗАДАН,
  # вторая подстановка раскрывается в его ЗНАЧЕНИЕ, и запись прибора вынесла бы ключ в
  # файл. Тот же дефект уже был в ENVIRONMENT.txt снимка. Здесь только вердикт.
  if [ -n "${LOOPLAB_LLM_API_KEY:-}" ] && [ -n "${LOOPLAB_LLM_API_KEY_BASE_URL:-}" ]; then
    echo "llm_credential: pair complete, bound to ${LOOPLAB_LLM_API_KEY_BASE_URL}"
  else
    echo "llm_credential: INCOMPLETE PAIR -- the engine will refuse to start"
  fi
} > "$OUT/INSTRUMENT.txt"
say "прибор записан: $OUT/INSTRUMENT.txt (stream=${LOOPLAB_LLM_STREAM:-unset})"

# THE DRY-RUN GATE SITS HERE, below the environment, the card and the instrument record, and above
# everything that costs MONEY. It used to sit above all of them, which made the instrument record
# unreachable without spending a dollar -- a fix whose falsifier cannot run is a fix nobody can
# check.
#
# What is now above the gate: the credential exports, `make_task.py`, and one file write. That list
# grew today when `make_task` moved up so the record could hash the card it produced, and the
# promise had to be restated to stay true: the gate no longer means "nothing has happened yet", it
# means NO MODEL HAS BEEN CALLED AND NO MONEY SPENT. A dry run costs about four seconds of CPU and
# leaves a `ws/` directory behind. Saying "no `make_task`" here after moving `make_task` above it
# would have been a comment describing a script that no longer exists.
if [ "${PROBE_DRY_RUN:-0}" = "1" ]; then
  say "сухой прогон: все проверки пройдены, прибор записан, ничего не запущено"
  exit 0
fi

say "===== $MODEL на $TASK, полоса $LANE, бюджет \$$BUDGET ====="
S=$(date +%s)
LOOPLAB_MEMORY_DIR="$OUT/runs/$TASK/memory" LOOPLAB_KNOWLEDGE_DIR="$OUT/runs/$TASK/knowledge" \
  taskset -c "$LANE" python -m looplab.cli run "$OUT/ws/algotune_$TASK.json" \
    --out "$OUT/runs/$TASK/run" --backend llm --max-nodes 20 >> "$OUT/run.log" 2>&1
say "прогон rc=$? за $(( $(date +%s) - S ))с"

# ЧЕМПИОНА ВЫБИРАЕТ СВЁРТКА СОБЫТИЙ, А НЕ ВРЕМЯ ФАЙЛА. Здесь стояло `ls -t … | head -1` — самый
# СВЕЖИЙ solver.py, — и это не то же самое, что лучший. На пробе `convex_hull` 27.08 узел 0 имел
# train-оценку 3.7777, узел 1 — 2.7342, а `ls -t` вернул узел 1, потому что он записан позже
# (06:00:53 против 04:30:53). На тесте померили его: 2.7829. Настоящий чемпион на тесте не
# измерялся вообще, и весь день это число докладывалось как результат пробы.
# Кампания так не делает: `campaign.sh::run_one` зовёт `extract_champion.py --run-dir --all-files`, который читает
# свёртку и знает `state.best()`. Проба обязана выбирать так же, иначе она меряет не то, что цикл
# счёл лучшим, — то есть меряет не цикл.
if python "$ROOT/looplab/benchmarks/algotune/extract_champion.py" \
     --run-dir "$OUT/runs/$TASK/run" --all-files --out "$OUT/champion_solver.py" >> "$LOG" 2>&1; then
  CH="$OUT/champion_solver.py"
else
  # ВЫХОД 1 И ВЫХОД 2 — РАЗНЫЕ ОТВЕТЫ, а `if ...; then` их не различает. Извлекатель специально
  # разделил их, и оба вызывающих различие выбрасывали:
  #   1 — свёртка говорит, что чемпиона нет (нет лога, нет лучшего узла, в наборе нет solver.py).
  #       Это факт О ПРОГОНЕ и законный «НЕТ».
  #   2 — лог не удалось ПРОЧИТАТЬ или `looplab` не удалось ИМПОРТИРОВАТЬ. Это факт О МОСТЕ, и о
  #       прогоне он не говорит ничего: оценки лежат в events.jsonl, чемпион извлекается заново,
  #       без повторной траты бюджета. Измерено 31.08 на пробе `accEE`: её сводка сказала
  #       «чемпион: НЕТ», пока её же лог держал 27.466 и 221.5387.
  CHRC=$?
  CH=""
  if [ "$CHRC" = 2 ]; then
    say "ОТКАЗ: СЛОМАН МОСТ — extract_champion вышел с 2. Это НЕ «прогон без чемпиона»."
    say "       оценки целы: $OUT/runs/$TASK/run/events.jsonl"
    say "       повтори без нового прогона: python $ROOT/looplab/benchmarks/algotune/extract_champion.py \\"
    say "         --run-dir $OUT/runs/$TASK/run --all-files --out $OUT/champion_solver.py"
    exit 2
  fi
fi
say "чемпион: ${CH:-НЕТ}"
[ -n "$CH" ] && (
  cd "$OUT" && taskset -c "$LANE" "$ROOT/AlgoTune/.venv/bin/python" \
    "$ROOT/looplab/benchmarks/algotune/looplab_eval.py" --algotune-root "$ROOT/AlgoTune" \
    --task "$TASK" --model "$LABEL" --solver champion_solver.py --subset test \
    > "$OUT/final.json" 2>> "$LOG"
)
say "ИТОГ: $(head -c 300 "$OUT/final.json" 2>/dev/null)"
}

# `exit` СРАЗУ ЗА ВЫЗОВОМ — вторая половина замка. Обёртка в функцию защищает ТЕЛО, но после
# возврата из `main` оболочка продолжает читать файл с сохранённого смещения и выполнит всё, что
# там окажется после правки на ходу. Проверено: без `exit` дописанная строка исполняется.
main "$@"
exit "$?"
}
