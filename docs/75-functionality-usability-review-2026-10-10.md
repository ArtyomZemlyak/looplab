# 75. Ревью функциональности, удобства и понятности: вторая десятиминутка (2026-10-10)

**Статус:** план исправлений; ничего из §5 ещё не отгружено. **База:** `38b7a46` (ветка
`claude/determined-dirac-22f2s4`), чистый контейнер Ubuntu 24.04, Python 3.13.16, Node 22.22, без GPU,
без модели. **Предшественники:** [doc 71](71-onboarding-and-entry-barriers-2026-09-30.md) — аудит
порога входа, [doc 74](74-entry-barrier-inspection-2026-10-09.md) — инспекция первых десяти минут и
их исправление. Этот документ начинается там, где doc 74 остановился: первые десять минут
перепроверены на дереве и держатся (§3); предмет здесь — **одиннадцатая минута**: вторая команда,
первая ошибка, завершённый прогон, справка конкретной команды, главный экран первого run.

Как читать: §1 — вывод и первый пакет; §5 — реестр находок `UX-01…UX-22`, у каждой наблюдение
(измеренное), предложение и критерий приёмки; §7 — пакеты; §8 — метрики «сейчас → цель»; §10 —
команды, которыми всё снято. Правило ведения — §11.

## 1. Вывод и порядок работ

Вход в продукт теперь правильный, и это надо сохранить: README в 899 слов с первой командой на 29-й
строке; `looplab run examples/demo.yaml` без флагов за 7,4 с (вместе со стартом интерпретатора);
`looplab --help` с панелью «Start here» и `run` второй строкой; `inspect` в девять строк, результат
первым; пустой UI с карточкой «Try the offline demo — no model needed», от которой до Report меньше
20 с; заголовок Report называет лучший результат и разницу. Все 76 тестов пяти файлов, которыми
doc 74 закрепил свою приёмку, зелёные на этой базе (§3).

Порог теперь стоит на одиннадцатой минуте, и держат его пять вещей:

1. **Две грамматики отказа.** Один и тот же факт — «модель недоступна» — печатается двумя
   способами: preflight говорит `Refused: … [unreachable] …` одним блоком с лекарствами, а путь
   `--goal` (Genesis), `harness-mcp` без токена и несуществующий файл — рамкой Typer «Usage … Try
   --help … Invalid value:», как будто пользователь ошибся в синтаксисе. В `looplab/cli` 40 мест
   `typer.BadParameter` против одного печатающего `Refused:`. Путь `looplab init` → `looplab run
   looplab.yaml` (первая команда README «Prefer the terminal?») заканчивается сырым дампом
   pydantic со ссылкой на `errors.pydantic.dev`.
2. **Глаголы над завершённым прогоном лгут или молчат.** `resume` завершённого демо дописывает
   17 строк повторной финализации и печатает тот же результат; `run` на том же `--out` — ещё 17;
   `stop` дописывает `pause` в финализированный журнал и печатает «stopped … (frozen, not
   finalized)», хотя `inspect` сразу после этого говорит `finished=True`. Ни одна из трёх команд не
   называет причину — бюджет `max_nodes` исчерпан — и лекарство (`--max-nodes`).
3. **Язык движка там, где doc 74 его не искал.** Панели `looplab --help` вычищены, но 43 из 74
   `<команда> --help` цитируют внутренние документы и модули (`doc 68 68.3a`,
   `engine/run_boundary.py::drain_owed`); `stop --help` — 45 строк про канарейки и MiniOneRec,
   `ui --help` — три абзаца о починке публикации бандла. На главном экране первого run полоса
   стратегии говорит «race candidates with ASHA (smoke rung -> promote survivors to full)» и
   «endgame (inside the plan's reserve, 100% of node budget spent)», а бейдж Concepts —
   «Membership withheld for all 1 tagged experiment; not empty». Глоссария нет: слово `glossary`
   не встречается ни в README, ни в руководстве.
4. **Непоследовательность.** Одна сущность зовётся `node` (CLI, 403 строки UI), `experiment`
   (Report, 505 строк UI) и `candidate` (README); список `developer_backend` в шаблоне `init` не
   знает `codex` и `claude`, которые знают реестр и `run --help`; форма «bare task file» названа
   «legacy» в `run --help` и справочнике CLI, при этом карта примеров рекомендует именно её для
   каждого примера; команды `export-*` берут выход то флагом `--out`, то позиционным аргументом.
5. **Детерминированное демо просят повторить.** Контракт `examples/demo.yaml` объявляет
   `uncertainty_protocol: "none: deterministic objective"`, но Report пишет «Next step: Repeat the
   selected experiment with multiple seeds», ярлык `UNCONFIRMED` стоит, и три итога в чате
   советуют «check it with repeat runs». UI не читает `uncertainty_protocol` ни в одном модуле.
   Рядом на отчёте офлайн-прогона без модели — «Refresh report · paid» и «Paid AI action:
   provider charges may apply».

**Первый пакет:** UX-01, UX-02, UX-05, UX-07, UX-13, UX-16 — шесть пунктов: пять правок сообщений
и кнопок и одно правило в проекции результата; ни одно не трогает фолд, событийный лог или промпты. Затем UX-10,
UX-11 (справка команд), UX-14, UX-15 (главный экран), UX-19 (глоссарий). Остальное — §7.

Реестр: 22 находки, из них P0 — 3, P1 — 10, P2 — 9; снятых — 0.

## 2. Метод и границы

**Сделано** на чистом контейнере, без модели:

- Прочитаны README, `docs/index.md`, `docs/guide/*` (20 страниц), `00-INDEX.md`, `mkdocs.yml`,
  `AGENTS.md`, `examples/README.md`, `tests/README.md`, doc 71 (§1, §13) и doc 74 целиком.
- Прогнаны: `looplab run examples/demo.yaml`; офлайн `dataset` и `repo` с `--backend toy`; `run`
  без модели на `--goal` (Genesis) и на голом task-файле (preflight); `smoke`; `init` (`dataset` и
  `--kind quadratic`) и запуск написанного файла; `inspect`, `inspect --config`, `replay`,
  `timings`, `tokens`, `comparability`, `export-notebook`, `export-git`; `resume`, `stop` и
  повторный `run` на завершённом прогоне; `--crash-after 3` и `resume` после него; `resume
  --max-nodes 8`; `harness`, `harness-mcp` без токена; `--help` всех 74 команд (перепись §4.1).
- Доказательные тесты doc 74 прогнаны на этой базе: `test_documentation_contracts.py`,
  `test_entry_page_budgets.py`, `test_doc74_acceptance.py`, `test_config_docs_sync.py`,
  `test_cli_help_panels.py` — 76 зелёных. `mkdocs build --strict` — зелёный, 18,6 с.
- UI: `looplab ui --no-build` на корне с тремя прогонами и на пустом корне; Playwright/Chromium,
  1600 × 1000, тема по умолчанию. Пройдено: пустой корень, меню «LoopLab», «Start a new run»,
  сообщение в чат без модели, «Check connection…», карточка демо → Validate → Start run → Report
  (конец в конец), список с тремя run, рабочее пространство демо (Report, Lineage, Cards, Energy,
  четыре меню, Run settings), Settings → Essential, статический `tree.html`. Снимки —
  `docs/assets/75-usability-review/`.

**Не доказано:** ни одного вызова платной модели; ни одного нового пользователя (протокол —
doc 74 §12.9, он остаётся открытым); Windows, JupyterHub, Docker Compose, TUI и внешний MCP-цикл
не запускались; экраны иной ширины и мобильная компоновка вне плана. Счёт кнопок здесь — `button:visible`
в Playwright; он отличается от счётчика doc 74 (там 19 на пустом корне, здесь 15), поэтому числа §4.4
сравниваются только между собой.

Ниже **наблюдение** — измеренное число, выполненная команда или снятый экран; **предложение** —
продуктовая гипотеза с критерием приёмки, по которому её можно принять или отвергнуть.

## 3. Сверка с doc 71 и doc 74

| Утверждение doc 74 (§12.8) | На `38b7a46` | Держится |
|---|---|---|
| README ≤ 1 000 слов, первая команда в первых 40 строках | 899 слов (`wc -w`); `looplab run examples/demo.yaml` на 29-й строке | да |
| Quickstart ≤ 700, Installation ≤ 500 | 651 / 461 | да |
| Демо без флагов, без сети | `examples/demo.yaml` ставит `backend: toy`; 7,4 с по `time`, 6 узлов | да |
| `--help`: «Start here» первой, `run` в первых трёх, 0 ссылок на документы | 8 панелей, `run` 2-я, 14 938 байт, 0 ссылок | да (но см. UX-11: ссылки ушли из панелей, не из `<команда> --help`) |
| `inspect` ≤ 20 строк, результат первым | 9 строк | да (но см. UX-07: 4 из 9 строк — диагностика для застрявшего run) |
| `init` ≤ 12 активных строк | 9 | да (но см. UX-02: написанный по умолчанию файл не запускается без правки `data_path`) |
| `note: backend=toy …` под BEST на офлайн-`dataset` | есть на `dataset` и `repo`, нет на `quadratic` | да |
| Пустой UI: «Try the offline demo — no model needed» → Validate → Start → Report ≤ 2 мин | карточка есть, Validate бесплатен, run открылся сам, Report через < 20 с | да |
| Первая фраза Report — лучший результат и разница | «Selected #4: evaluation score 1.343. Its evaluation score is better by 77.01 than the first eligible experiment #0 …» | да (но см. UX-13: следом «Next step: Repeat … with multiple seeds») |
| Один селектор языка | один, в шапке | да |
| Руководство 246 207 слов, 20 страниц | 246 629 слов (`wc -w`), 20 страниц; `ui.md` +1 191, `configuration.md` +105 | размер не уменьшился (UX-21) |
| Открыт только тест на новых пользователях | по-прежнему открыт; протокол doc 74 §12.9 | — |

Из doc 71 §13 проверены OB-03 (готовность модели рядом с composer: «qwen3:8b · Connection
unverified», «Check connection…»), OB-05 (подсказка «Start with three things»), OB-07 («Permissions ·
Plan» с одной строкой пояснения) и OB-13 (ошибка чата без модели: «Assistant could not reach the
model provider … Retry · Open Settings») — держатся.

Один пункт doc 74 пересматривается по форме, не по решению: EB-05 (предупреждение
`CAP_DAC_OVERRIDE` остаётся) — здесь UX-03 про его **текст**, а не про его наличие.

## 4. Измерения

### 4.1. Справка команд

`looplab --help`: 74 команды, 8 панелей, 14 938 байт, 148 строк. Сумма `<команда> --help` при
ширине 80: 168 311 байт; 43 команды цитируют документ или модуль (`doc N`, `§`, `*.py`, `::`,
`ADR-N`), 25 используют слова движка (`Card`, `Genesis`, `boss`, `champion`, `fold`, `CAS`,
`lane`, `rung`, `canary`, `fence`, `seq`, `splice`). Самые длинные: `run` 11 919 байт / 135 строк /
23 опции; `ui` 5 147 / 59; `stop` 4 243 / 45; `resume` 2 921 / 29 с пятью цитатами. В `run --help`
при 80 колонках столбец текста начинается на 56-й позиции: на описание опции остаётся около 20
символов, и «Task kind. With --goal it PINS the kind and Genesis fills the rest» переносится в
шесть строк по два-три слова.

### 4.2. Отказы

| Путь | Что печатается | Код |
|---|---|---|
| `looplab run examples/toy_task.json` (backend `llm` по умолчанию, модели нет) | `Refused: LLM endpoint preflight failed: [unreachable] …` + две строки лекарств (`--backend toy`, `looplab smoke`) | 2 |
| `looplab run --goal "…"` без модели | рамка Typer: `Usage … Try 'looplab run --help' … Invalid value: Genesis couldn't reach the model …` | 2 |
| `looplab init` → `looplab run looplab.yaml` (нет `data.csv`) | рамка Typer с дампом pydantic: `1 validation error for DatasetTask … [type=value_error, input_value={'kind': 'dataset', 'goal...}] … https://errors.pydantic.dev/2.13/v/value_error` | 2 |
| `looplab run nope.yaml` | рамка Typer: `Invalid value: config file not found: …` | 2 |
| `looplab inspect runs/none` | одна строка: `no run found at … (no config.snapshot.json or events.jsonl).` | 2 |
| `looplab harness-mcp` без `LOOPLAB_HARNESS_TOKEN` | рамка Typer: `Invalid value: Set LOOPLAB_HARNESS_TOKEN …` | 2 |
| `looplab smoke` без модели | `text FAILED: LLM request to http://localhost:11434/v1 failed: Connection error.` — без лекарства | 1 |

В `looplab/cli/*.py` — 40 вызовов `typer.BadParameter`; печать `Refused:` — одно место
(`cli/__init__.py`, обработчик `OperatorRefusal`).

### 4.3. Завершённый прогон

Журнал демо после `run`: 112 строк (`finalize_step` последней). `looplab resume runs/demo` →
«run was finished — resuming to continue with the current settings», тот же BEST, +17 строк
(`resume`, `prior_injected` ×2, `run_finished`, `budget`, `diversity_archive`, `finalize_step` ×10,
`finalization_finished`). `looplab stop runs/demo` → «stopped … (frozen, not finalized) — `looplab
resume` to continue», +1 строка `pause` с `reason: operator stop`; `inspect` сразу после:
`finished=True`. `looplab run examples/demo.yaml --out runs/demo` → «already finished — reopening
to continue with the current task/settings (use a new --out for a fresh run)», +17 строк
(`run_reopened` и повторная финализация). Итого 147 строк и три повторные финализации без одного нового узла. `looplab resume runs/demo --max-nodes 8`
работает как задумано: 8 узлов, новый BEST.

### 4.4. UI

| Экран | Кнопок | Слов | Что бросается в глаза |
|---|---|---|---|
| Пустой корень | 15 | 124 | «Start with a goal», «Try the offline demo — no model needed», «qwen3:8b · Connection unverified.» — хорошо |
| Список, 3 run | 18 | 144 | строки «selected 1.343 · evaluation score · No multi-seed confirmation; exploratory result. · 6 nodes · min» |
| Run → Report (демо) | 54 | 987 | «Next step: Repeat the selected experiment with multiple seeds …», `UNCONFIRMED`, «Refresh report · paid», «Paid AI action …», «Hypothesis search · Deep Research», «Base unknown», «Detector coverage is not fully verified.» |
| Run → Lineage (демо, живой) | 45 | 202 | «strategy: exploring breadth: race candidates with ASHA (smoke rung -> promote survivors to full)», «policy: exploit best (rungs collapsed) -> #3» |
| Run → Lineage (демо, завершён) | 48 | 344 | «endgame (inside the plan's reserve, 100% of node budget spent): reserve for a final ensemble of the top solutions and a champion sweep, no new breadth»; бейдж «Concepts · PARTIAL · Membership withheld for all 1 tagged experiment; not empty.» |
| Run → Cards (демо) | 34 | 315 | «6 work items · Lanes · Research · + Add · Proposed 0 · Building 0 · Running 0 · Gated 0 · Dropped 0 · EVALUATED 6»; статусы `tested` / `supported` без легенды |
| Run → Energy | — | — | первая кнопка полосы видов — переключатель анимации: «Off · Subtle (glow, light motion) · Full (energy flows + living nodes)»; в `ui.md` слово `Energy` не встречается |
| Run → Run settings | 71 | 2 188 | полный редактор настроек внутри рабочего пространства |
| Settings → Essential | 21 | 339 | 14 настроек; «Experiment runner: toy / llm»; «3 customized values» |

Чат без модели: «Assistant could not reach the model provider. Check the connection and retry. Your
message is still available. provider_unavailable · Retry · Open Settings» — хорошо. Итоги в чате
на демо (три): «This is preliminary; the next step is to check it with repeat runs.»

Словарь UI (`ui/src/locales/ru.json`, 9 149 исходных строк): `experiment` 505, `node` 403,
`concept` 253, `card` 174, `attempt` 142, `generation` 140, `claim` 114, `champion` 55, `lesson`
53, `candidate` 36, `lane` 24, `endgame` 13, `lifecycle` 11, `rung` 9. На одном экране Report:
«6 nodes (6 evaluated, 0 failed)» и «Selected #4 … experiment #0». README: `experiment` 3,
`candidate` 4, `node` 1; `run --help` и `inspect --help`: `node` 3, `experiment` 0.

### 4.5. Документация

Руководство: 246 629 слов (`wc -w`), 20 страниц; больше 10 000 слов — `configuration.md` 49 585,
`cli-reference.md` 33 574, `concepts.md` 32 452, `tasks.md` 24 854, `ui.md` 22 606,
`external-harness.md` 17 971, `llm-and-agents.md` 15 242, `memory.md` 12 904. Глоссария нет
(`grep -il glossary README.md docs/guide/*.md` — пусто). Нумерованных документов, включая этот, 74 (два с
номером 18): 14 на русском (55, 59, 60, 65–75), 60 на английском; в таблице индекса 13 строк из 75
на русском, остальные на английском, без пометки языка. Руководство пользователя — на английском; UI двуязычен.

### 4.6. Конфигурация и примеры

300 настроек, 135 булевых, 99 включены по умолчанию (как в doc 74). `looplab init` по умолчанию
пишет `kind: dataset` с `data_path: data.csv`; `looplab init --kind quadratic` пишет файл, который
запускается офлайн сразу (`backend: toy`, 8 узлов). Шаблон `init` перечисляет `developer_backend:
default | opencode | aider | goose | continue`; `core/config.py::DEVELOPER_BACKENDS` и `run --help` —
семь имён, включая `codex` и `claude`. Относительные пути в task-файле считаются от текущего
каталога: `looplab run <repo>/examples/dataset_task.json --backend toy` из другого каталога
отказывает «data path(s) not found: <cwd>/examples/dataset_example/data.csv».

## 5. Реестр находок

Формат пункта: `UX-NN · приоритет · размер — название`; **Наблюдение**, **Предложение**,
**Приёмка**. Размер: S — часы, M — день-два, L — больше. Ссылки на код — `модуль::символ`.

### A. Отказы и сообщения об ошибках

#### UX-01 · P0 · S — Один факт, две грамматики отказа

**Наблюдение.** §4.2: preflight печатает `Refused: …` с причиной и лекарствами (это образец), а
Genesis (`cli/run_cmds.py::run`, сообщение «Genesis couldn't reach the model…»), `harness-mcp`
без токена (`harness/mcp_server.py::run_stdio` → `ValueError` → `BadParameter`) и отсутствующий
файл печатаются рамкой Typer с «Usage … Try --help … Invalid value:». Для пользователя это
выглядит как ошибка в аргументах. Genesis-отказ не называет ни `looplab smoke`, ни `--backend toy`.
**Предложение.** Всё, что является отказом по вводу оператора, а не по синтаксису команды, идёт
через `core/errors.py::OperatorRefusal` (тип уже есть; `LLMError` — его подтип) и печатается
единственным принтером `Refused:`. Genesis-отказ получает те же три строки лекарств, что
preflight. `BadParameter` остаётся только там, где неверен сам аргумент (`--timeout` без `--wait`).
**Приёмка.** `looplab run --goal x` без модели → `Refused:` одним блоком, в нём `smoke` и
`--backend toy`, без «Usage»/«Try»; `harness-mcp` без токена — то же; тест на оба; счётчик
`BadParameter` в `looplab/cli` ≤ 20 (сейчас 40), остаток — только синтаксис аргументов.

#### UX-02 · P0 · S — `init` → `run` падает дампом pydantic

**Наблюдение.** `looplab init` без флагов пишет `kind: dataset`, `data_path: data.csv`; README
советует «edit the task and run `looplab run looplab.yaml`». Запуск без правки — §4.2: дамп
pydantic с `input_value={…}`, `[type=value_error, …]` и ссылкой на `errors.pydantic.dev`; полезная
фраза «data path(s) not found … (use an absolute path; ~ and $VARS are expanded)» стоит в середине.
Источник — `cli/run_cmds.py::run`, `raise typer.BadParameter(f"invalid task: {e}")` со строковым
`ValidationError`. **Предложение.** Печатать только `msg` каждой ошибки из `e.errors()`, с именем
поля: `task.data_path: data.csv not found (resolved against <cwd>; edit data_path in looplab.yaml or
pass an absolute path)`; через `OperatorRefusal` (UX-01). Отдельно: шаблон `dataset` пишет под
`data_path` строку-подсказку «put your CSV here; the run refuses to start until it exists».
**Приёмка.** В выводе ни одного `pydantic.dev`, `input_value`, `type=value_error`; первая строка
называет поле и файл; тест через `CliRunner`.

#### UX-03 · P1 · S — Текст предупреждения о `CAP_DAC_OVERRIDE`

**Наблюдение.** Первое, что печатает каждый `looplab run` под root или на Windows, — одна строка в 388 символов
(пять строк терминала):
«the read fence's KERNEL self-protection rung is ADVISORY here: this process holds
CAP_DAC_OVERRIDE … A NATIVE writer a node's eval code starts (a `cp`, a `ctypes` `fopen`, any
non-Python child) can overwrite the generated fence … the audit-hook rung (`_SELF`) refuses the
Python spellings and sees none of those». Doc 74 EB-05 оставил предупреждение и назвал его в
Installation — верно; но его текст написан для автора фенса, не для пользователя, и стоит раньше
результата. **Предложение.** Одна строка: `warning: running as root/Windows — the sandbox's
file-mode protection does not bind here (see Installation → Check the install)`; сегодняшний текст
— на уровне DEBUG. Цитату в Installation и тест, который держит её равной сообщению, обновить в том
же изменении. **Приёмка.** ≤ 1 строка, ≤ 160 символов, без идентификаторов в обратных кавычках;
`test_installation_names_the_read_fence_warning…` зелёный с новым текстом.

#### UX-04 · P1 · S — Относительные пути task-файла считаются от CWD

**Наблюдение.** §4.6: `examples/dataset_task.json` и `repo_task.json` держат пути вида
`examples/dataset_example/data.csv`; из любого каталога, кроме корня репозитория, запуск отказывает
(`adapters/dataset_task.py::DatasetTask._resolve_and_require_data`). Карта примеров и CLI
walkthrough молчат о правиле. **Предложение.** Разрешать относительный путь сначала от каталога
task-файла, затем от CWD (как делают `pytest.ini`, `docker compose`), и записать правило в
`tasks.md` и `examples/README.md`; в отказе называть, от чего считали. **Приёмка.** `cd /tmp &&
looplab run <repo>/examples/dataset_task.json --backend toy --max-nodes 2` проходит; тест на оба
порядка; строка правила в `tasks.md`.

### B. Глаголы над завершённым прогоном

#### UX-05 · P1 · S — `resume`, `stop`, повторный `run` не называют исчерпанный бюджет

**Наблюдение.** §4.3: три команды над завершённым прогоном, 35 новых строк журнала, три
повторные финализации, ни одного нового узла; `stop` печатает «frozen, not finalized» о
финализированном прогоне и дописывает `pause`. Код: `cli/run_cmds.py::_open_and_drive` (ветка
`prior_kind == "finished"` — «reopening to continue», честно, но без причины), `::resume` («resuming
to continue with the current settings»), `::stop` (строка печатается безусловно; проверка
`already.halted` стоит только под `--drain-builds`). **Предложение.** Все три команды сначала
читают фолд: если `finished` и бюджет узлов спет (`len(nodes) >= max_nodes`) — печатать `already
finished: 6/6 experiments; nothing to continue. To continue, raise the budget: looplab resume RUN
--max-nodes 12` и ничего не дописывать; `stop` на завершённом — `already finished; nothing to
stop`, без `pause`. Реальные причины повторного открытия (ошибка, новый `--max-nodes`, `error`
как `stop_reason`) сохраняются как есть — это именно то, ради чего ветка написана. **Приёмка.**
`resume`/`stop`/`run` на завершённом демо без нового бюджета дописывают 0 строк; сообщение
содержит `N/N` и `--max-nodes`; `resume --max-nodes 8` продолжает как сейчас; тест на три команды
по числу строк журнала до/после.

#### UX-06 · P2 · S — `--crash-after` скрыт, но описан как шаг пользователя

**Наблюдение.** CLI walkthrough §5 («Crash & resume») предлагает `--crash-after 3`; флаг объявлен
`hidden=True` и в `run --help` отсутствует; `cli-reference.md` называет его «hidden test hook». Сам
сценарий работает: после `--crash-after 3` `inspect` честно пишет «STOPPED WITHOUT A BOUNDARY …
`looplab resume` picks it up», `resume` доводит до 12 узлов. **Предложение.** В walkthrough —
одна фраза «тестовый флаг, имитирует `kill -9`», или снять `hidden`. **Приёмка.** Либо флаг в `run
--help`, либо walkthrough называет его тестовым.

### C. Команды чтения

#### UX-07 · P1 · S — `inspect` завершённого прогона печатает диагностику застрявшего

**Наблюдение.** Девять строк `inspect` демо: 2-я — «stop: finished — the search ran out of work
with a champion standing. That natural completion is the one finish that names no reason.»
(`events/stop_account.py::stop_account`); 5–6-я — «stop evidence: last record: `finalize_step`
seq=112 … no phase beacon was left open — the run was between phases, so its own log does not say
what it was working on» (`cli/inspect_cmds.py::inspect`, печатается «for every disposition»);
8-я — «comparability: declared declared=d811c07496298294» (`cli/run_report.py::echo_inspect_tail`).
На `--crash-after` те же строки на месте и уместны. **Предложение.** Для `finished`: `stop:
finished — budget spent (6/6 experiments)`; строки «stop evidence» только при `finished=False`;
`comparability: declared (contract d811c074…)`. **Приёмка.** `inspect` завершённого демо ≤ 6 строк,
без «names no reason» и «phase beacon»; на прерванном прогоне строки evidence остаются; тест на
оба.

#### UX-08 · P2 · S — `timings` в минутах с одним знаком, `tokens` без слова «офлайн»

**Наблюдение.** На офлайн-прогоне в 3 с (`ds-toy`) `looplab timings` печатает «run wall clock 0.0 min» и «0.0 min»
в 54 из 65 строк (`cli/inspect_cmds.py::timings`, формат `minutes(...)`); `tokens` — «ledger
total: n/a (the log carries no llm_usage or llm_cost row) / no generation spans found; nothing to
attribute.» **Предложение.** Единицы по длительности (секунды до 2 мин, минуты до 2 ч, часы);
`tokens` на `backend=toy` — «offline run (backend=toy): no model calls to attribute». **Приёмка.**
На демо ни одной строки `0.0 min`; `tokens` на toy называет причину одним словом.

#### UX-09 · P2 · S — `replay` без вердикта и без опций

**Наблюдение.** `looplab replay runs/demo` — 59 740 байт JSON, `--help` без единой опции.
Walkthrough: «`replay` proves the run is reproducible — it folds the append-only log into the
same state», но вывод не содержит ни сравнения, ни слова «matches». **Предложение.** По умолчанию
— сводка в пять строк (`147 events → 6 experiments, best #4 1.34289; fold matches the recorded
summary (digest …)`), `--json` возвращает сегодняшний дамп. **Приёмка.** Вывод по умолчанию ≤ 10
строк и содержит слово `matches`/`differs`; `--json` байт-в-байт равен сегодняшнему.

### D. Справка команд

#### UX-10 · P1 · S — `run --help` нечитаем при 80 колонках, «Genesis» и «legacy» не объяснены

**Наблюдение.** §4.1: 23 опции, столбец текста с 56-й позиции, ширину столбца флагов задают пары
`--validate-agent --no-validate-agent`, `--agent-patch-gate --no-agent-patch-gate`,
`--require-approval --no-require-approval`. «Genesis» встречается три раза без определения.
Форма «`looplab run task.json --max-nodes 20` # a bare task file + flags (legacy)»
(`cli/run_cmds.py::run`, docstring; `cli-reference.md` дважды) противоречит `examples/README.md`,
который рекомендует эту форму для всех примеров. **Предложение.** Панели `rich_help_panel`:
«Task» (`--goal --kind --direction --data --genesis`), «Budget» (`--max-nodes --max-seconds
--out`), «Model» (`--backend --model --set`), «External coding agent» (пять `--agent*` и
`--developer-backend`), «Confirmation» (остальные); одна фраза «Genesis — the model writes the task
file from `--goal`»; слово «legacy» убрать в обоих местах (форма поддерживается и рекомендована).
**Приёмка.** При 80 колонках столбец текста ≥ 40 символов (измерить позицией `<str>` в первой
строке опций); `legacy` отсутствует в `run --help` и `cli-reference.md`; `Genesis` определён в
первых 20 строках; `tests/test_cli_help_panels.py` расширен.

#### UX-11 · P1 · M — 43 из 74 `<команда> --help` цитируют внутренние документы и модули

**Наблюдение.** §4.1. Примеры: `resume --help` — «--drain-only (doc 68 68.3a) finishes the OWED
evaluations (engine/run_boundary.py:: drain_owed) … (doc 68 68.3e) … (doc 68 68.3b)»; `stop --help`
— 45 строк, в них «eval CANARY», «Card build», «run_is_stopping», «measured on MiniOneRec inf13»;
`ui --help` — три абзаца о том, как чинится прерванная публикация бандла; `inspect --help` —
«the seven run_started-pinned settings can differ from it (the owner config API overlays the
effective folded values)». Doc 74 EB-08/EB-09 вычистили панели и `run`, но правило «ссылки на
документы — не в справке» закреплено тестом только для списка команд
(`test_cli_help_panels.py::_DOC_CITATION`). **Предложение.** Правило CLAUDE.md для самого CLAUDE.md
(«правило — здесь, история — в докстринге модуля или doc 64») применить к докстрингам команд: 1–3
предложения для пользователя + опции; измерения, инциденты и цитаты переезжают в докстринг модуля
или doc 64. **Приёмка.** Перепись §10 даёт 0 команд с цитатами (сейчас 43) и ≤ 60 строк у любой
команды (сейчас `run` 135, `ui` 59, `stop` 45); `_DOC_CITATION` проверяется на каждом `<команда>
--help`; `test_a_command_still_prints_its_full_docstring` перенацелен на фразу, которая останется.

#### UX-12 · P2 · S — Мелкая непоследовательность CLI

**Наблюдение.** (а) `bench` стоит в панели «Export», `build-ui` — в «Run control», `tui`
(терминальный близнец `ui`) — в «Run control», тогда как `ui` — в «Start here». (б) Шаблон `init`:
`developer_backend: default | opencode | aider | goose | continue`
(`core/appconfig.py::render_template`) — без `codex` и `claude`, которые есть в
`core/config.py::DEVELOPER_BACKENDS` и в `run --help`. (в) `export-git RUN OUT` берёт выход
позиционно, `export-notebook`/`export-bundle`/`export-sft` — флагом `--out`; `looplab export-git
RUN --out X` отвечает «No such option». (г) `tui --help` трижды говорит «the boss» — слово, которого
нет ни в README, ни в руководстве (там Assistant). **Предложение.** `bench` → «Research
instruments», `build-ui` → «Maintenance», `tui` → «Start here» рядом с `ui`; строку шаблона
собирать из `DEVELOPER_BACKENDS`; `export-git` принимает `--out` (позиционный оставить как
алиас); «boss» → «Assistant». **Приёмка.** Тест сравнивает строку шаблона с реестром; `export-git
RUN --out X` работает; `grep -c boss` по `--help` = 0.

### E. UI первого прогона

#### UX-13 · P0 · S — Детерминированное демо просят повторить с несколькими seed

**Наблюдение.** `examples/demo.yaml` объявляет `uncertainty_protocol: "none: deterministic
objective"` и `aggregation: "single deterministic evaluation"`. Report демо: «Next step: Repeat the
selected experiment with multiple seeds and inspect Trust before relying on the result.»
(`ui/src/report.js::verdict`, решение по одному флагу `repeated`), ярлык `UNCONFIRMED`, в карточке
«repeat evidence: No multi-seed confirmation; exploratory result.»; три итога в чате — «This is
preliminary; the next step is to check it with repeat runs.»
(`ui/src/resultNoticeModel.js::resultNoticeBrief`); в списке — «No multi-seed confirmation;
exploratory result.» на каждой строке. `uncertainty_protocol` в UI читает только `offlineDemo.json`
(сам спек демо), на сервере — только `serve/scope_report.py`; в `/api/runs` и `result_summary`
признака детерминизма нет. Это та же находка, что doc 74 §1 («короткие итоги в чате советуют
«repeat runs» трёхсекундной игрушке»), оставшаяся после EB-21. **Предложение.** Сервер выводит на
строку run и на `result_summary` поле `repeat_checks: "not_applicable"` (из
`comparison_contract.uncertainty_protocol`, начинающегося с `none`), UI на этом значении: без
`UNCONFIRMED`, «repeat evidence: deterministic objective — repeat checks are not needed», «Next
step: open the solution» и соответствующая фраза в итогах чата. Прогоны без контракта (например
`ds-toy`) ведут себя как сегодня. **Приёмка.** Report и чат демо не содержат «Repeat»/«repeat
runs»/`UNCONFIRMED`; `ds-toy` содержит; серверный тест на поле, UI-тест на обе ветки.

#### UX-14 · P1 · S — Язык движка на главном экране первого run

**Наблюдение.** §4.4, Lineage во время и после демо: «exploring breadth: race candidates with
ASHA (smoke rung -> promote survivors to full)», «exploit best (rungs collapsed) -> #3», «endgame
(inside the plan's reserve, 100% of node budget spent): reserve for a final ensemble of the top
solutions and a champion sweep, no new breadth»; бейдж «Concepts · PARTIAL · Membership withheld
for all 1 tagged experiment; not empty.» (`ui/src/ConceptChipBar.jsx`). Ни «ASHA», ни «rung», ни
«endgame» в `ui.md` не объяснены на первом экране. **Предложение.** Полоса стратегии в два слоя:
видимая строка для пользователя («Trying several directions first, then refining the best» /
«Budget spent; combining the best results»), сегодняшняя формулировка — под раскрытием или в
`title`; бейдж Concepts не показывать, пока ни один концепт не отображён. **Приёмка.** Видимый
текст полосы на демо не содержит `ASHA`, `rung`, `endgame`, `reserve`, `sweep`; полный текст
доступен по раскрытию; UI-тест.

#### UX-15 · P1 · S — Переключатель анимации — первая кнопка полосы видов

**Наблюдение.** В рабочем пространстве полоса начинается с «Energy», затем Lineage · Cards ·
Concepts · Report · Overview; «Energy» — переключатель визуальных эффектов (Off / Subtle / Full,
«energy flows + living nodes»; `ui/src/EnergyToggle.jsx`, монтируется в `RunView.jsx` и
`RunList.jsx`). В `ui.md` не упомянут. **Предложение.** Перенести в меню «Theme» (рядом есть
кнопка Theme) или в конец полосы; одна строка в `ui.md`. **Приёмка.** Первая кнопка полосы — вид;
`ui.md` называет переключатель; снимок.

#### UX-16 · P1 · S — Report офлайн-прогона предлагает платные действия без модели

**Наблюдение.** На демо без настроенной модели: «Refresh report · paid» (`ui/src/Report.jsx`),
«Paid AI action: provider charges may apply. One request identity will be saved when you start…»,
раздел «Hypothesis search · Deep Research» с «Open Deep Research», три «Discuss next step» в чате —
каждое заканчивается `provider_unavailable`. **Предложение.** Пока сохранённое соединение не
проверено (то же состояние, что «Connection unverified» на пустом экране), платные кнопки
показывают сначала условие («Needs a model — Settings → Model») и не обещают «identity will be
saved»; раздел Deep Research скрыт, если run не делал ни одного исследовательского вызова.
**Приёмка.** На демо без модели ни одной кнопки со словом `paid` без строки «Needs a model»;
раздела Deep Research нет; UI-тест на оба состояния.

#### UX-17 · P2 · S — Статусы и бейджи без легенды

**Наблюдение.** Cards: `tested` / `supported`, «Lanes · Research · + Add», «Proposed 0 · Building
0 · Running 0 · Gated 0 · Dropped 0» на завершённом toy-прогоне; карточка «Selected experiment»:
«Base unknown» (`ui/src/BaseRevision.jsx`, у `quadratic` базы кода нет по определению); заголовок
Report: «Detector coverage is not fully verified.» (`ui/src/report.js`). **Предложение.** `title`
на каждый статус, легенда в `ui.md`; «Base unknown» не показывать для видов задач без базы кода;
фразу про детекторы — в раскрытие Trust. **Приёмка.** Каждое слово-статус Cards имеет `title`;
«Base unknown» отсутствует на `quadratic`; заголовок Report без фразы про детекторы.

#### UX-18 · P2 · M — Три слова для одной сущности

**Наблюдение.** §4.4: `node` / `experiment` / `candidate` — 403 / 505 / 36 строк UI, один экран
Report использует первые два; CLI говорит только `node` («BEST node 4», «nodes=6 evaluated=6»),
README — `experiment` и `candidate`; Cards добавляют «work items» и `card-N`. **Предложение.**
Одно слово для пользователя — «experiment» (в UI, README, выводе `run`/`inspect`: «best
experiment #4»); `node` остаётся в журнале, API и коде. **Приёмка.** Список run, Report и `inspect`
употребляют одно слово; счётчик §10 по строкам UI: `node` ≤ 50 (только технические панели).

### F. Документация и понятность

#### UX-19 · P1 · M — Нет глоссария

**Наблюдение.** §4.5: слова `glossary` нет; слова, которые пользователь встречает в первые
двадцать минут и которые ни одна страница входа не определяет: node/experiment, card, lifecycle,
generation, champion/selected, eligible, comparable, confirmed, caveat, lane, rung, endgame,
concept, claim, lesson, Deep Research, Genesis, Strategist, boss. Их определения разбросаны по
`concepts.md` (32 452 слова) и `ui.md`. **Предложение.** `docs/guide/glossary.md`: ≤ 60 терминов,
по одной строке, каждая ссылается на якорь в `concepts.md`/`ui.md`; строка в «Start here» на
`guide/index.md` и в nav «Get started»; тот же список — в `title` ярлыков Report. **Приёмка.**
Страница ≤ 1 500 слов; каждое слово из ярлыков и заголовка Report есть в ней (тест по списку);
`mkdocs build --strict` зелёный.

#### UX-20 · P2 · S — Язык проектных записей не виден до открытия

**Наблюдение.** 14 из 74 нумерованных документов на русском (55, 59, 60, 65–75, включая этот), 60
на английском; таблица индекса — 13 русских строк среди 62 английских без пометки; руководство на
английском; у русских документов нет английского резюме. Читатель на любом языке узнаёт язык
документа только открыв его. **Предложение.** Пометка `[RU]`/`[EN]` в строке индекса и
трёхстрочная аннотация на другом языке в начале каждого нумерованного документа с 71-го; тест
выводит пометку из текста (доля кириллицы в первых 4 КБ) и сверяет со строкой. **Приёмка.** У
каждой строки индекса пометка совпадает с языком файла; у 71–75 есть аннотация.

#### UX-21 · P2 · S — У справочных страниц нет потолка

**Наблюдение.** Doc 74 отверг переписывание справочников на Use/Reference и дал каждому вводный
абзац «Start here» (`test_every_large_guide_page_opens_with_where_to_start`). Размер страниц не
уменьшился: 246 207 → 246 629 слов за день; `ui.md` 21 415 → 22 606, `configuration.md` 49 480 → 49 585. Ничто не мешает
`configuration.md` (49 585) расти дальше: бюджет есть только у страниц входа. **Предложение.**
Храповик, как у `CLAUDE.md` (`test_claude_md_does_not_grow`): для каждой страницы руководства >
10 000 слов — потолок, равный сегодняшнему размеру; уменьшать можно, расти — нет; новая страница
> 10 000 слов должна начинаться с «Start here» (уже есть). **Приёмка.**
`tests/test_entry_page_budgets.py` держит потолок по странице; красный на +1 слово к любой из
восьми.

#### UX-22 · P2 · S — Детали walkthrough и каталога прогона

**Наблюдение.** (а) CLI walkthrough §2, абзац про `inspect --config`: «It does NOT overlay the
event-effective values, so on a resumed or live-retuned run the seven `run_started`-pinned fields
and an event-sourced `trust_gate` can differ» — на втором шаге знакомства. (б) «What's in a run
directory» не называет `AGENTS.md`, который движок пишет в каждый каталог прогона
(`engine/setup_phase.py`), и семь служебных файлов (`.looplab-fence/`, `*.lock`,
`.spans-append.jsonl`); пользователь, открыв каталог, видит на 8 записей больше, чем в схеме.
(в) README: «`looplab stop RUN_DIR` # stop without the wrap-up; resumable» — верно, но после UX-05
слово «resumable» должно дополниться «unless finished». **Предложение.** Абзац (а) — в
`cli-reference.md` к `inspect`; (б) — одна строка про `AGENTS.md` («что движок говорит кандидату
о задаче») и фраза «plus lock and fence files you can ignore». **Приёмка.** Текстовые проверки в
`tests/test_entry_page_budgets.py`.

## 6. Целевые сценарии после исправлений

1. **Терминал, второй шаг.** `looplab init` → `looplab run looplab.yaml` без `data.csv`: одна строка
   `Refused: task.data_path: data.csv not found (resolved against /work; …)`, выход 2, следующий шаг
   назван. Пользователь правит путь и запускает снова (UX-01, UX-02, UX-04).
2. **Терминал, завершённый прогон.** `looplab resume runs/demo` → `already finished: 6/6 experiments
   … looplab resume runs/demo --max-nodes 12`; журнал не растёт. `looplab inspect runs/demo` — шесть
   строк, первая содержит `best experiment #4` (UX-05, UX-07, UX-18).
3. **UI, первый run.** После демо Report говорит «Selected #4 … better by 77.01 …»; под ним «Next
   step: open the solution»; ярлыка UNCONFIRMED нет; полоса стратегии — «Budget spent; combining the
   best results»; платные кнопки предупреждают «Needs a model» (UX-13, UX-14, UX-16).
4. **Справка.** `looplab stop --help` — пять строк и опции; `looplab run --help` в терминале 80
   колонок читается без переносов по два слова; ни одна команда не цитирует документ (UX-10, UX-11).

## 7. Пакеты поставки

| Пакет | Пункты | Поверхности | Статус |
|---|---|---|---|
| 1. Сообщения CLI | UX-01, UX-02, UX-05, UX-07, UX-08 | `looplab/cli/*`, `core/errors.py`, `events/stop_account.py` | план |
| 2. UI первого run | UX-13, UX-16, UX-14, UX-15, UX-17 | `serve/run_projections.py`, `ui/src/report.js`, `resultNoticeModel.js`, `RunView.jsx`, `Report.jsx` | план |
| 3. Справка команд | UX-10, UX-11, UX-12 | докстринги команд, `cli/help_panels.py`, `tests/test_cli_help_panels.py` | план |
| 4. Пути и примеры | UX-04, UX-06, UX-22 | `adapters/dataset_task.py`, `repo_task.py`, `tasks.md`, `cli-walkthrough.md`, `examples/README.md` | план |
| 5. Словарь | UX-18, UX-19 | UI-строки, вывод `run`/`inspect`, `docs/guide/glossary.md` | план |
| 6. Документация как система | UX-20, UX-21, UX-09, UX-03 | `00-INDEX.md`, `tests/test_entry_page_budgets.py`, `replay`, предупреждение фенса | план |

Порядок: 1 → 2 → 3; 4–6 независимы. Ни один пакет не меняет фолд, типы событий или промпты;
единственное новое поле — выведенное серверной проекцией (`repeat_checks`, UX-13), с дефолтом для
старых журналов.

## 8. Метрики приёмки

| Метрика | Сейчас (`38b7a46`) | Цель |
|---|---|---|
| Отказов в рамке Typer среди шести путей §4.2 | 4 из 6 | 0 (только синтаксис аргументов) |
| `BadParameter` в `looplab/cli` | 40 | ≤ 20 |
| Строк, дописанных `resume`/`stop`/`run` на завершённом прогоне без нового бюджета | 17 / 1 / 17 | 0 / 0 / 0 |
| Строк `inspect` на завершённом демо; фраз «names no reason», «phase beacon» | 9; 2 | ≤ 6; 0 |
| `<команда> --help` с цитатами документов/модулей | 43 из 74 | 0 |
| Строк `run --help` / `ui --help` / `stop --help` при 80 колонках | 135 / 59 / 45 | ≤ 60 каждая |
| Столбец текста в `run --help` при 80 колонках (позиция) | 56-я | ≤ 40-я |
| Report демо: «Repeat … seeds», `UNCONFIRMED`, кнопок `paid` без условия | есть, есть, 1 | нет, нет, 0 |
| Слов движка на видимой полосе стратегии демо (`ASHA`, `rung`, `endgame`, `reserve`, `sweep`) | 5 | 0 |
| Первая кнопка полосы видов | Energy (анимация) | вид |
| Слов для сущности «эксперимент» на экране Report | 2 (`node`, `experiment`) | 1 |
| Страница глоссария | нет | ≤ 1 500 слов, в nav |
| Пометка языка в строках индекса | 0 из 74 | 74 из 74 |
| Потолок размера у страниц руководства > 10 000 слов | нет | 8 из 8 |
| Относительный `data_path` из другого каталога | отказ | работает или правило названо |

Структура этого реестра закреплена `tests/test_doc75_registry.py`: у каждого `UX-NN` есть
наблюдение, предложение и приёмка, каждый пункт назначен ровно в один пакет §7, и счёт в §1
получен парсером.

## 9. Отвергнутые альтернативы

- **Переписать справочники на Use/Reference** — отвергнуто doc 74 (цена против измеренной
  пользы); здесь вместо этого храповик размера (UX-21) и глоссарий (UX-19), которые стоят часы.
- **Убрать предупреждение `CAP_DAC_OVERRIDE`** — решение doc 74 EB-05 стоит: факт важен для
  безопасности песочницы; меняется только текст (UX-03).
- **Снять ярлыки неопределённости вообще** — нет: `UNCONFIRMED` верен для `ds-toy` и любой задачи
  с шумом; UX-13 снимает его только там, где контракт объявил детерминизм.
- **Поднять `BadParameter` в `OperatorRefusal` глобальным обработчиком** — одна обёртка над Typer
  спрятала бы и настоящие синтаксические ошибки; переносятся только отказы по вводу (UX-01).
- **Запрещать повторный `run` на завершённом `--out`** — нет: ветка «reopen» написана ради
  повтора после `error` и ради большего бюджета; меняется только случай «бюджет спет» (UX-05).
- **Сделать `node` единственным словом** — нет: README, Report и чат уже говорят «experiment», и
  это слово понятно без объяснения; `node` остаётся техническим (UX-18).

## 10. Воспроизводимость

```bash
# CLI
time looplab run examples/demo.yaml --out /tmp/ll/demo
looplab inspect /tmp/ll/demo | wc -l; looplab replay /tmp/ll/demo | wc -c
looplab resume /tmp/ll/demo; looplab stop /tmp/ll/demo; looplab run examples/demo.yaml --out /tmp/ll/demo
wc -l /tmp/ll/demo/events.jsonl                      # 112 -> 147
looplab run --goal "minimize (x-3)^2" --out /tmp/ll/g; echo $?          # рамка Typer, 2
looplab run examples/toy_task.json --out /tmp/ll/t; echo $?             # Refused: … [unreachable], 2
(mkdir -p /tmp/ll/i && cd /tmp/ll/i && looplab init && looplab run looplab.yaml); echo $?   # дамп pydantic, 2
grep -c BadParameter looplab/cli/*.py | awk -F: '{s+=$2} END {print s}'   # 40
# перепись --help: размер, цитаты, жаргон
for c in $(looplab --help | grep -oP '^│ \K[a-z][a-z0-9-]+'); do
  looplab $c --help | awk -v c=$c '{b+=length($0)+1} /doc ?[0-9]|§|[a-z_]+\.py\b|::|ADR-/ {n++} END {print c, b, NR, n+0}'
done | sort -k2 -n -r | head
COLUMNS=80 looplab run --help | grep -m1 '^│ --goal' | awk -F'<str>' '{print length($1)+5}'   # 56
# документы
wc -w docs/guide/*.md | tail -1                      # 246 629
grep -il glossary README.md docs/guide/*.md          # пусто
python - <<'PY'
import re, pathlib
for d in sorted(pathlib.Path("docs").glob("[0-9][0-9]-*.md")):
    t = d.read_text(errors="ignore")[:4000]
    print("RU" if len(re.findall(r"[А-Яа-яЁё]", t)) > 0.3*len(re.findall(r"[A-Za-z]", t)) else "EN", d.name)
PY
# UI: looplab ui --run-root /tmp/ll --port 8765 --no-build; Playwright/Chromium 1600x1000 считает
# button:visible и inner_text('body') на '/', '/#/run/demo', '/#/settings' и ведёт карточку демо
# Validate -> Start run; снимки в docs/assets/75-usability-review/
```

Снимки: [пустой корень](assets/75-usability-review/01-empty-root.png) ·
[карточка демо](assets/75-usability-review/02-demo-launch-card.png) ·
[полоса стратегии во время демо](assets/75-usability-review/03-lineage-strategy-strip-live.png) ·
[Report: «Next step: Repeat…»](assets/75-usability-review/04-report-next-step.png) ·
[Cards](assets/75-usability-review/05-cards-view.png) ·
[Energy первой кнопкой](assets/75-usability-review/06-energy-first-tab.png) ·
[Settings → Essential](assets/75-usability-review/07-settings-essential.png).

## 11. Как вести этот документ

Этот документ — план (правило doc 74 §11). Когда пункт отгружен, меняется ячейка «Статус» в §7 и
при необходимости строка в §8; новый раздел не добавляется, журнал реализации — коммиты. Если план
меняется по существу, пишется новый номер, а здесь появляется одна строка ссылки вверху. Единственный
открытый пункт doc 74 — проверка на новых пользователях по протоколу doc 74 §12.9 — не закрыт и этим
документом: он требует людей, а не сессии.
