# 75. Ревью функциональности, удобства и понятности: вторая десятиминутка (2026-10-10)

*EN summary: a usability review of the minutes after the first ten (2026-10-10) — refusal messages, verbs on a
finished run, command help, the first run's screen, vocabulary, docs; 35 findings, revised by four critics (§12).*

**Статус:** план, пересмотренный критикой (§12), и реализованный 2026-10-10; статус пунктов — только в §7. **База:** `38b7a46` (ветка
`claude/determined-dirac-22f2s4`), чистый контейнер Ubuntu 24.04, Python 3.13.16, Node 22.22, без GPU,
без модели. **Предшественники:** [doc 71](71-onboarding-and-entry-barriers-2026-09-30.md) — аудит
порога входа, [doc 74](74-entry-barrier-inspection-2026-10-09.md) — инспекция первых десяти минут и
их исправление. Этот документ начинается там, где doc 74 остановился: первые десять минут
перепроверены на дереве и держатся (§3); предмет здесь — **одиннадцатая минута**: вторая команда,
первая ошибка, завершённый прогон, справка конкретной команды, главный экран первого run.

Как читать: §1 — вывод и первый пакет; §5 — реестр находок `UX-01…UX-35`, у каждой наблюдение
(измеренное), предложение и критерий приёмки; §7 — пакеты; §8 — метрики «сейчас → цель»; §10 —
команды, которыми всё снято. Правило ведения — §11; критика плана и выбор вариантов — §12.

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
   --help … Invalid value:», как будто пользователь ошибся в синтаксисе. В `looplab/cli` 34 вызова
   `raise typer.BadParameter(` против одного печатающего `Refused:`. Путь `looplab init` → `looplab run
   looplab.yaml` (первая команда README «Prefer the terminal?») заканчивается сырым дампом
   pydantic со ссылкой на `errors.pydantic.dev`.
2. **Глаголы над завершённым прогоном лгут или молчат.** `resume` завершённого демо дописывает
   17 строк повторной финализации и печатает тот же результат; `run` на том же `--out` — ещё 17;
   `stop` дописывает `pause` в финализированный журнал и печатает «stopped … (frozen, not
   finalized)», хотя `inspect` сразу после этого говорит `finished=True`. Ни одна из трёх команд не
   называет причину — бюджет `max_nodes` исчерпан — и лекарство (`--max-nodes`).
3. **Язык движка там, где doc 74 его не искал.** Панели `looplab --help` вычищены, но 37 из 74
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

Критика плана (§12) добавила шестую вещь, самую дорогую: **документированный рецепт ключа
хостинговой модели отклоняется** самим CLI (UX-23) — каждый пользователь OpenAI-совместимого
сервиса падает на первом настоящем шаге.

**Первый пакет:** UX-01, UX-02, UX-23, UX-05, UX-07, UX-13, UX-16 — правки сообщений, рецептов и
кнопок и одно аддитивное поле контракта; ни одно не трогает фолд, типы событий или промпты. Затем
UX-10, UX-11 (справка команд), UX-14, UX-15 (главный экран), UX-19 (глоссарий). Остальное — §7.

Реестр: 35 находок, из них P0 — 4, P1 — 14, P2 — 17; снятых — 0.

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
ширине 80: 213 986 байт; 37 команд цитируют документ или модуль (на любой ширине) (`doc N`, `§`, `*.py`, `::`,
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

В `looplab/cli/*.py` — 34 вызова `raise typer.BadParameter(` (40 совпадений grep вместе с комментариями); печать `Refused:` — одно место
(`cli/__init__.py`, обработчик `OperatorRefusal`).

### 4.3. Завершённый прогон

Журнал демо после `run`: 110 строк, последний `seq` 112 (`finalize_step`). `looplab resume runs/demo` →
«run was finished — resuming to continue with the current settings», тот же BEST, +17 строк
(`resume`, `prior_injected` ×2, `run_finished`, `budget`, `diversity_archive`, `finalize_step` ×10,
`finalization_finished`). `looplab stop runs/demo` → «stopped … (frozen, not finalized) — `looplab
resume` to continue», +1 строка `pause` с `reason: operator stop`; `inspect` сразу после:
`finished=True`. `looplab run examples/demo.yaml --out runs/demo` → «already finished — reopening
to continue with the current task/settings (use a new --out for a fresh run)», +17 строк
(`run_reopened` и повторная финализация). Итого 145 строк и три повторные финализации без одного нового узла. `looplab resume runs/demo --max-nodes 8`
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
**Приёмка**. Размер: S — часы, M — день-два, L — больше. Ссылки на код — `модуль::символ`. Пункт,
который изменила критика плана, несёт фразу «Пересмотрено (§12)» и решение; UX-23…UX-35 добавлены
ею.

### A. Отказы и сообщения об ошибках

#### UX-01 · P0 · S — Один факт, две грамматики отказа

**Наблюдение.** §4.2: preflight печатает `Refused: …` с причиной и лекарствами (это образец), а
Genesis (`cli/run_cmds.py::run`, сообщение «Genesis couldn't reach the model…»), `harness-mcp`
без токена (`harness/mcp_server.py::run_stdio` → `ValueError` → `BadParameter`) и отсутствующий
файл печатаются рамкой Typer с «Usage … Try --help … Invalid value:». Для пользователя это
выглядит как ошибка в аргументах. Genesis-отказ называет `--no-genesis`, но при `backend=llm` по
умолчанию этот совет ведёт прямо во второй отказ — preflight.
**Предложение.** Пересмотрено (§12): не счётчик, а классификация. (1) Перед Genesis вызывается тот
же `agents/preflight.py::preflight_role_endpoints`, что перед run: недоступная модель даёт тот же
`LLMError` и тот же принтер `Refused:` с теми же лекарствами — нового текста нет. (2)
`run_stdio` бросает `core/errors.py::ConfigRefusal` (он `OperatorRefusal` и `ValueError` сразу, так
что существующие `except ValueError` не меняются); `cli/harness_cmds.py` пропускает отказ
наверх. (3) Остальные отказы по вводу в `looplab/cli` — `CliRefusal(OperatorRefusal,
typer.BadParameter)`: `pytest.raises(typer.BadParameter)` в существующих тестах остаётся
зелёным, а `_RefusalBoundaryGroup.invoke` печатает `Refused:` одной строкой. Это раздел Click:
`UsageError` — ошибка вызова с «Try --help», `ClickException` — простая ошибка; git (`usage:` /
`fatal:`) и cargo (`error:` + `help:`) делят так же. Код выхода 2 не меняется.
**Приёмка.** Тест-таблица классифицирует каждый `raise` в `looplab/cli` как синтаксис или отказ и
краснеет на неклассифицированном; три пути §4.2 (Genesis без модели, `harness-mcp` без токена,
несуществующий файл) через `CliRunner` дают `Refused:` без «Usage»/«Try».

#### UX-02 · P0 · S — `init` → `run` падает дампом pydantic

**Наблюдение.** `looplab init` без флагов пишет `kind: dataset`, `data_path: data.csv`; README
советует «edit the task and run `looplab run looplab.yaml`». Запуск без правки — §4.2: дамп
pydantic с `input_value={…}`, `[type=value_error, …]` и ссылкой на `errors.pydantic.dev`; полезная
фраза «data path(s) not found … (use an absolute path; ~ and $VARS are expanded)» стоит в середине,
и совет ложен: путь в сообщении уже абсолютный, относительные пути работают. Источник —
`cli/run_cmds.py::run`, `raise typer.BadParameter(f"invalid task: {e}")`. После того как
`data.csv` появился, шаблон с активным `backend: llm` ведёт пользователя без модели во второй
отказ — preflight.
**Предложение.** Пересмотрено (§12): `ConfigRefusal` с одной строкой на запись `e.errors()`
(`loc`: `msg`); подсказка в `adapters/dataset_task.py::DatasetTask._resolve_and_require_data`
называет, от чего считали, и не советует абсолютный путь; `init` печатает после записи файла одну
строку «needs a model; to try offline: `looplab init --kind quadratic`», а `backend: llm` в
шаблоне закомментирован (это и есть значение по умолчанию; активная строка перекрывала
`LOOPLAB_BACKEND`, doc 74 EB-17).
**Приёмка.** В выводе ни одного `pydantic.dev`, `input_value`, `type=value_error`; первая строка
называет поле и файл; `init` называет офлайн-вариант; тест через `CliRunner`.

#### UX-03 · P1 · S — Текст предупреждения о `CAP_DAC_OVERRIDE`

**Наблюдение.** Первое, что печатает каждый `looplab run` под root или на Windows, — одна строка в
388 символов (пять строк терминала): «the read fence's KERNEL self-protection rung is ADVISORY
here: this process holds CAP_DAC_OVERRIDE … A NATIVE writer a node's eval code starts … can
overwrite the generated fence … the audit-hook rung (`_SELF`) refuses the Python spellings and sees
none of those». Doc 74 EB-05 оставил предупреждение и назвал его в Installation — верно; текст
написан для автора фенса и стоит раньше результата.
**Предложение.** Пересмотрено (§12): `runtime/read_fence.py::harden_guarantee` не трогать — его
фраза и есть продукт безопасности, и `tests/test_read_fence.py` её держит. Меняется только вызов
`_LOG.warning` в `engine/resources.py`: короткая строка, которая всё ещё называет остаточный риск
(«running as root/Windows: native programs started by eval code can overwrite the sandbox read
fence — see Installation»), полный текст — на DEBUG. Цитата в Installation и тест обновляются в
том же изменении.
**Приёмка.** Строка WARNING ≤ 160 символов, без идентификаторов в обратных кавычках, называет
риск; полный текст на DEBUG; `test_installation_names_the_read_fence_warning…` зелёный с новым
текстом.

#### UX-04 · P1 · M — Относительные пути task-файла считаются от CWD, и от него же зависит resume

**Наблюдение.** §4.6: `examples/dataset_task.json` держит `examples/dataset_example/data.csv`; из
любого каталога, кроме корня репозитория, запуск отказывает
(`adapters/dataset_task.py::DatasetTask._resolve_and_require_data`). Критика добавила худшее:
`task.snapshot.json` хранит путь так, как он написан (`"data_path":
"examples/dataset_example/data.csv"`), хотя докстринг `_resolve` говорит «resolved once at load
time (recorded in the snapshot)». Поэтому `cd /tmp && looplab resume <run> --max-nodes 3` на
dataset-прогоне падает тем же дампом, а сервер порождает `resume <rd> --task-file <snapshot>` из
своего каталога.
**Предложение.** Пересмотрено (§12; размер M). Правило «сначала каталог task-файла, затем CWD»
неоднозначно (два разных `data.csv` — тихо разные данные) и для снимка выбирает каталог прогона.
Вместо него: (1) относительный путь в файле, названном в командной строке `run`, считается от
каталога этого файла; флаговые и UI-задачи — от CWD, как сейчас; (2) если по обоим основаниям
существуют РАЗНЫЕ файлы — отказ, который называет оба; (3) в `task.snapshot.json` пишутся
разрешённые абсолютные пути, и resume перестаёт зависеть от каталога; старые снимки читаются как
раньше; (4) правило записано в `tasks.md` и `examples/README.md`.
**Приёмка.** `cd /tmp && looplab run <repo>/examples/dataset_task.json --backend toy --max-nodes 2`
проходит; `resume` из другого каталога проходит; `task_identity` на resume не меняется; два
разных кандидата — отказ, а не выбор.

### B. Глаголы над завершённым прогоном

#### UX-05 · P1 · S — `resume`, `stop`, повторный `run` не называют исчерпанный бюджет

**Наблюдение.** §4.3: три команды над завершённым прогоном, 35 новых строк журнала, три
повторные финализации, ни одного нового узла; `stop` печатает «frozen, not finalized» о
финализированном прогоне и дописывает `pause`. Каждое повторное открытие заново пишет
`finalize_scope` и прогоняет `reflection`, `concept_curation`, `claim_curation`, `llm_cost` — на
прогоне с моделью это деньги, а не только шум. Код: `cli/run_cmds.py::_open_and_drive` (ветка
`prior_kind == "finished"`), `::resume`, `::stop` (строка печатается безусловно; проверка
`already.halted` стоит только под `--drain-builds`).
**Предложение.** Пересмотрено (§12): тест `len(nodes) >= max_nodes` неверен — потолок движка
`max_nodes + add_nodes + refunded_node_reservations` (`engine/orchestrator.py::_node_id_ceiling`),
а `extend_budget` Ассистента дописывает `budget_extend` и открывает прогон через `resume`. Правило
выносится в чистую функцию «есть ли запас при НОВЫХ настройках» (потолок узлов и `max_seconds`
против прошедшего времени) от `(state, settings)`, общую для CLI. На `finished` без запаса:
`resume` — `already finished: 6/6 experiments; nothing to continue. To continue: looplab resume
RUN --max-nodes 12`, 0 строк; `run` на том же `--out` — те же слова плюс «or a fresh run: --out
runs/demo-2»; `stop` — `already finished; nothing to stop`, на остановленном — `already stopped`.
Образец — `finalize` («already finalized … nothing to do», 0 строк, выход 0). `pending_finalize`
и `error` классифицирует `classify_prior_run`, как сейчас.
**Приёмка.** 0 дописанных строк в трёх случаях без запаса; сообщение содержит `N/N` и
`--max-nodes`; `budget_extend` + `resume` продолжает; `run` с поднятым `max_nodes` продолжает;
тест на каждое по числу строк журнала до/после.

#### UX-06 · P2 · S — `--crash-after` скрыт, но описан как шаг пользователя

**Наблюдение.** CLI walkthrough §5 («Crash & resume») предлагает `--crash-after 3`; флаг объявлен
`hidden=True` и в `run --help` отсутствует; `cli-reference.md` называет его «hidden test hook». Сам
сценарий работает: после `--crash-after 3` `inspect` пишет «STOPPED WITHOUT A BOUNDARY …
`looplab resume` picks it up», `resume` доводит до 12 узлов.
**Предложение.** Пересмотрено (§12): флаг остаётся скрытым; walkthrough называет его тестовым
(«a test flag that simulates `kill -9`»).
**Приёмка.** Walkthrough называет флаг тестовым; тест по тексту.

### C. Команды чтения

#### UX-07 · P1 · S — `inspect` завершённого прогона печатает диагностику застрявшего

**Наблюдение.** Девять строк `inspect` демо: 2-я — «stop: finished — the search ran out of work
with a champion standing. That natural completion is the one finish that names no reason.»
(`events/stop_account.py::stop_account`); 5–6-я — «stop evidence: last record: `finalize_step`
seq=112 … no phase beacon was left open …» (`cli/inspect_cmds.py::inspect`); 8-я —
«comparability: declared declared=d811c07496298294». Критика: фраза из `stop_account` печатается
и в конце каждого `run`/`resume` (`cli/__init__.py::_print_result`), а демо остановилось на
`max_nodes` — «ran out of work» здесь фактически неверно.
**Предложение.** Пересмотрено (§12): чинится сама `stop_account` — при исчерпанном потолке узлов
`finished — node budget spent (6/6 experiments)`, и `run`/`resume` выигрывают вместе с
`inspect`; «stop evidence» скрыта только на полном современном финише (есть
`finalization_finished`); `comparability: declared (scores comparable within contract
d811c074…)`.
**Приёмка.** `inspect` завершённого демо ≤ 6 строк, без «names no reason» и «phase beacon»; на
прерванном прогоне evidence остаётся; `run` демо печатает «node budget spent»; тест на оба.

#### UX-08 · P2 · S — `timings` в минутах с одним знаком, `tokens` без слова «офлайн»

**Наблюдение.** На офлайн-прогоне в 3 с `looplab timings` печатает «run wall clock 0.0 min» и
«0.0 min» в 66 из 79 строк (`cli/inspect_cmds.py::timings`); `tokens` — «ledger total: n/a (the
log carries no llm_usage or llm_cost row) / no generation spans found; nothing to attribute.»
**Предложение.** Пересмотрено (§12): одна единица на весь отчёт, выбранная по длительности прогона
(секунды до 2 мин, минуты до 2 ч, часы), — столбцы остаются сравнимыми, а пины `test_timings.py`
(«10.0 min» на 600 с) держатся; `tokens` на `backend=toy` — «offline run (backend=toy): no model
calls to attribute».
**Приёмка.** На демо ни одной строки `0.0 min`; `tokens` на toy называет причину; тест.

#### UX-09 · P2 · S — `replay` без вердикта

**Наблюдение.** `looplab replay runs/demo` — 59 740 байт JSON, `--help` без единой опции.
Walkthrough: «`replay` proves the run is reproducible — it folds the append-only log into the
same state», но вывод не содержит ни сравнения, ни итога.
**Предложение.** Пересмотрено (§12): вывод по умолчанию НЕ меняется — `cli-reference.md` обещает,
что `looplab replay … | jq` не затронут, и `test_seq_gap_visibility.py` читает его как JSON.
Добавляется `--summary`: `folded 110 events → 6 experiments, best #4 = 1.34289 (min)`. Слова
«matches» нет: журнал не хранит дайджест состояния, сравнивать не с чем.
**Приёмка.** `replay --summary` ≤ 5 строк; вывод без флага байт-в-байт прежний; walkthrough
называет `--summary`; тест.

### D. Справка команд

#### UX-10 · P1 · S — `run --help` нечитаем при 80 колонках, «Genesis» и «legacy» не объяснены

**Наблюдение.** §4.1: 23 опции, столбец текста с 56-й позиции, ширину столбца флагов задают пары
`--validate-agent --no-validate-agent`, `--agent-patch-gate --no-agent-patch-gate`,
`--require-approval --no-require-approval`. «Genesis» встречается три раза без определения.
Форма «a bare task file + flags (legacy)» (`cli/run_cmds.py::run`; `cli-reference.md` дважды)
противоречит `examples/README.md`, который рекомендует её для всех примеров.
**Предложение.** Пересмотрено (§12): Rich раскладывает каждую панель отдельной таблицей, поэтому
панели «Task», «Budget», «Model», «External coding agent», «Approval» сужают столбцы; три пары
`--x/--no-x` — поля `Settings`, доступные через `-s key=value`, — скрываются (`hidden=True`, флаги
работают); одна фраза «Genesis: a model writes the task from your --goal»; «legacy» убрать в обоих
местах.
**Приёмка.** При 80 колонках столбец текста в каждой панели ≥ 40 символов; `legacy` нет в `run
--help` и `cli-reference.md`; `Genesis` определён; тест в `tests/test_cli_help_panels.py`.

#### UX-11 · P1 · M — 37 из 74 `<команда> --help` цитируют внутренние документы и модули

**Наблюдение.** §4.1 (исправлено критикой: 37, не 43, на любой ширине). Примеры: `resume --help`
— «--drain-only (doc 68 68.3a) finishes the OWED evaluations (engine/run_boundary.py:: drain_owed)
… (doc 68 68.3e)»; `stop --help` — 45 строк, «eval CANARY», «Card build», «measured on MiniOneRec
inf13»; `ui --help` — три абзаца о починке бандла. Из 37 в первые минуты встречаются `resume`,
`ui`, `comparability`, `export-git`, `export-bundle`; остальные (`cross-run-*`,
`concept-steward`, `claim-decide`, `landlock-check` …) — инструменты сопровождающего.
**Предложение.** Пересмотрено (§12): переписать справку команд, которые встречает пользователь
(`resume`, `stop`, `ui`, `inspect`, `export-*`, `comparability`): 1–3 предложения + опции;
вынутый текст — дословно в докстринг модуля. Для остальных — фильтр цитат в тесте и shrink-only
список (house pattern), который только уменьшается.
**Приёмка.** Ни одна команда пользователя не цитирует документ или модуль и не длиннее 60 строк при
80 колонках; остальные — в shrink-only списке `tests/data/`; новая команда с цитатой — красная;
`test_a_command_still_prints_its_full_docstring` перенацелен.

#### UX-12 · P2 · S — Мелкая непоследовательность CLI

**Наблюдение.** (а) `bench` стоит в панели «Export», `build-ui` — в «Run control», `tui` — в
«Run control», тогда как `ui` — в «Start here». (б) Шаблон `init`: `developer_backend: default |
opencode | aider | goose | continue` (`core/appconfig.py::render_template`) — без `codex` и
`claude` из `core/config.py::DEVELOPER_BACKENDS`. (в) `export-git RUN OUT` берёт выход
позиционно, `export-notebook`/`export-bundle`/`export-sft` — флагом `--out`. (г) `tui --help`
трижды говорит «the boss». Критика: «boss» есть в руководстве (17 мест, определён в
`configuration.md`) и это значение `core/config.py::AGENT_ROLES` — переименовать нельзя.
**Предложение.** `bench` → «Research instruments», `build-ui` → «Maintenance», `tui` → «Start
here»; строку шаблона собирать из `DEVELOPER_BACKENDS`; `export-git` принимает `--out`
(позиционный остаётся; оба сразу или ни одного — отказ); в `tui --help` «boss» →
«the run-chat Assistant (role `boss`)», и слово — в глоссарий (UX-19).
**Приёмка.** Тест сравнивает строку шаблона с реестром; `export-git RUN --out X` работает; `tui
--help` определяет слово.

### E. UI первого прогона

#### UX-13 · P0 · S — Детерминированное демо просят повторить с несколькими seed

**Наблюдение.** `examples/demo.yaml` объявляет `uncertainty_protocol: "none: deterministic
objective"`. Report демо: «Next step: Repeat the selected experiment with multiple seeds …»
(`ui/src/report.js::verdict`), ярлык `UNCONFIRMED`, «repeat evidence: No multi-seed confirmation;
exploratory result.»; итоги в чате — «This is preliminary; the next step is to check it with repeat
runs.» (`ui/src/resultNoticeModel.js::resultNoticeBrief`), в том числе для худших узлов, где
doc 74 EB-21 обещал «#4: 17.18 — worse than the current best». Текст контракта в журнал не
попадает — только его дайджест в `metric_provenance.comparability`.
**Предложение.** Пересмотрено (§12): префикс «none» значит «протокол не объявлен», а не
«детерминировано», и сервер не должен читать сайдкар `task.snapshot.json`. Вместо этого —
явное поле контракта `deterministic: true` (аддитивно; `exclude_none` сохраняет id старых
контрактов; с `measurement_phase: confirmed` — отказ). Движок пишет его следствие туда же, где
решает ключ контракта: `engine/comparability.py::comparability_record` кладёт рядом с `keys`
`repeat_checks: "not_applicable"`. UI на этом значении: без `UNCONFIRMED`, «Next step: Open the
selected experiment's solution», в итогах чата — без «repeat runs»; итог худшего узла говорит
«worse than the selected #N». Демо объявляет поле.
**Приёмка.** Report и чат демо без «Repeat»/«repeat runs»/`UNCONFIRMED`; без поля (и при
«none: not measured») — как сегодня; тест движка на запись поля, UI-тест на обе ветки.

#### UX-14 · P1 · S — Язык движка на главном экране первого run

**Наблюдение.** §4.4, Lineage во время и после демо: «exploring breadth: race candidates with
ASHA (smoke rung -> promote survivors to full)», «exploit best (rungs collapsed) -> #3», «endgame
(inside the plan's reserve, 100% of node budget spent): …»; бейдж «Concepts · PARTIAL ·
Membership withheld for all 1 tagged experiment; not empty.» (`ui/src/ConceptChipBar.jsx`).
**Предложение.** Пересмотрено (§12): текст стратегии — свободный `rationale` стратега
(`agents/strategist.py` или модель), его не переписывать. Видимая строка строится из
структурных полей (`policy`, `fidelity`, фаза плана): «Trying several directions, then refining
the best» / «Budget spent; combining the best results»; `rationale` — в `title` и под
раскрытием. Бейдж Concepts не прятать (`ConceptChipBar.jsx` запрещает выдавать «withheld» за
пустой run) — сказать простыми словами («Concept tags hidden for 1 experiment — see Concepts»).
**Приёмка.** Видимый текст полосы на демо без `ASHA`, `rung`, `endgame`, `reserve`, `sweep`;
полный текст доступен; бейдж без «Membership withheld»; UI-тест.

#### UX-15 · P1 · S — Переключатель анимации — первая кнопка полосы видов

**Наблюдение.** В рабочем пространстве полоса начинается с «Energy», затем Lineage · Cards ·
Concepts · Report · Overview; «Energy» — переключатель визуальных эффектов (`ui/src/EnergyToggle.jsx`).
В `ui.md` не упомянут.
**Предложение.** Перенести в конец полосы; одна строка в `ui.md`.
**Приёмка.** Первая кнопка полосы — вид; `ui.md` называет переключатель; UI-тест порядка.

#### UX-16 · P1 · S — Report офлайн-прогона предлагает платные действия без модели

**Наблюдение.** На демо без настроенной модели: «Refresh report · paid» (`ui/src/Report.jsx`),
«Paid AI action: provider charges may apply. One request identity will be saved…», раздел
«Hypothesis search · Deep Research», три «Discuss next step» в чате — каждое заканчивается
`provider_unavailable`.
**Предложение.** Пересмотрено (§12): «Connection unverified» — обычное состояние и у пользователя
с рабочей моделью, не нажимавшего «Check connection»; условие — «модель не настроена» или
«последняя проверка провалилась». Тогда платные кнопки показывают условие («Needs a model —
Settings → Model»); раздел Deep Research не прятать (это вход в исследование), а свернуть, пока
мемо нет.
**Приёмка.** На демо без модели ни одной кнопки `paid` без «Needs a model»; Deep Research
свёрнут без мемо; с проверенной моделью — как сегодня; UI-тест на обе ветки.

#### UX-17 · P2 · S — Статусы и бейджи без легенды

**Наблюдение.** Cards: `tested` / `supported`, «Proposed 0 · Building 0 · Running 0 · Gated 0 ·
Dropped 0» на завершённом toy-прогоне; «Base unknown» (`ui/src/BaseRevision.jsx`; у `quadratic`
базы кода нет по определению); заголовок Report: «Detector coverage is not fully verified.»
**Предложение.** `title` на каждый статус, легенда в `ui.md`; «Base unknown» не показывать, если
вид задачи не может иметь базы кода (по виду задачи, а не по имени `quadratic`); фразу про
детекторы — из заголовка в раскрытие Trust.
**Приёмка.** Каждое слово-статус Cards имеет `title`; «Base unknown» нет на задаче без базы;
заголовок Report без фразы про детекторы; UI-тест.

#### UX-18 · P2 · M — Три слова для одной сущности

**Наблюдение.** §4.4: `node` / `experiment` / `candidate` — 403 / 505 / 36 строк UI; CLI говорит
только `node` («BEST node 4», «nodes=6»), README — `experiment` и `candidate`. Критика:
«experiment» не равно узлу один к одному (артефактные узлы не ранжируются, есть попытки и
поколения), `ru.json` ключуется английскими строками, и семь Python-тестов держат «BEST node».
**Предложение.** Пересмотрено (§12): малый срез. Строки итога `run`/`inspect` («best experiment
#4»), заголовок Report и список run говорят «experiment»; `node` остаётся в журнале, API, коде и
технических панелях; глоссарий называет соответствие. Счётчик «node ≤ 50» снят.
**Приёмка.** Итог `run`/`inspect`, заголовок Report и строки списка — одно слово; глоссарий
называет `node`; тесты обновлены.

### F. Документация и понятность

#### UX-19 · P1 · M — Нет глоссария

**Наблюдение.** §4.5: слова `glossary` нет; слова первых двадцати минут без определения на
страницах входа: node/experiment, card, lifecycle, generation, champion/selected, eligible,
comparable, confirmed, caveat, lane, rung, endgame, concept, claim, lesson, Deep Research,
Genesis, Strategist, boss, finished; `docs/index.md` открывается «Card → agent → policy authority».
**Предложение.** `docs/guide/glossary.md`: ≤ 60 терминов, по строке, со ссылкой на подробную
страницу; строка в «Start here» на `guide/index.md` и в nav «Get started»; добавить «boss»,
«finished» и слова, которые печатает `inspect`.
**Приёмка.** Страница ≤ 1 500 слов; каждое слово из ярлыков Report и строк `inspect` есть в ней
(тест по списку); `mkdocs build --strict` зелёный.

#### UX-20 · P2 · S — Язык проектных записей не виден до открытия

**Наблюдение.** 14 из 74 нумерованных документов на русском (55, 59, 60, 65–75), 60 на
английском; в таблице индекса строки без пометки языка.
**Предложение.** Пометка `[RU]`/`[EN]` в строке индекса; тест выводит язык из текста (доля
кириллицы в первых 4 КБ; `00-INDEX.md` вне эвристики) и сверяет с пометкой; у 71–75 —
трёхстрочная аннотация на другом языке.
**Приёмка.** У каждой строки индекса пометка совпадает с языком файла; у 71–75 есть аннотация.

#### UX-21 · P2 · S — У справочных страниц нет потолка

**Наблюдение.** Размер руководства не уменьшился: 246 207 → 246 629 слов за день; бюджет есть
только у страниц входа.
**Предложение.** Пересмотрено (§12): жёсткий храповик противоречит правилу «каждое новое поле
`Settings` — строка в `configuration.md` в том же изменении», а `event-reference.md` и
`api-reference.md` генерируются. Потолок — по прозе ВНЕ таблицы настроек и для рукописных страниц
> 10 000 слов; сгенерированные страницы вне потолка.
**Приёмка.** `tests/test_entry_page_budgets.py` держит потолок по странице; новое поле Settings
его не краснит; +1 слово прозы — красный.

#### UX-22 · P2 · S — Детали walkthrough и каталога прогона

**Наблюдение.** (а) CLI walkthrough §2, абзац про `inspect --config` о семи `run_started`-полях —
на втором шаге знакомства. (б) «What's in a run directory» не называет `AGENTS.md`, который движок
пишет в каждый каталог прогона, и служебные `*.lock`, `.looplab-fence/`. (в) README: «`looplab
stop RUN_DIR` # stop without the wrap-up; resumable» — после UX-05 дополнить «unless finished».
**Предложение.** Абзац (а) — в `cli-reference.md` к `inspect`; (б) — строка про `AGENTS.md` и
фраза «plus lock and fence files you can ignore»; (в) — правка README.
**Приёмка.** Текстовые проверки в `tests/test_entry_page_budgets.py`.

### G. Добавлено критикой (§12)

#### UX-23 · P0 · S — Документированный рецепт ключа хостинговой модели отклоняется

**Наблюдение.** `cli-walkthrough.md` (шаг 4), `llm-and-agents.md` («Configure from the CLI») и
`jupyterhub-onboarding.md` (A4) задают только `LOOPLAB_LLM_API_KEY`. С ним и базовым URL `smoke` и
`run` печатают «Refused: LOOPLAB_LLM_API_KEY was set without LOOPLAB_LLM_API_KEY_BASE_URL» и
восемь строк про атомарную пару. Каждый пользователь хостинговой модели падает на первом
настоящем шаге.
**Предложение.** Правило пары — решение безопасности (ключ привязан к адресу, которому его
отдают), его не ослаблять. Чинить рецепты: в трёх местах строка `export
LOOPLAB_LLM_API_KEY_BASE_URL=<тот же URL>` рядом с ключом; тест находит в `docs/` и README каждое
присваивание `LOOPLAB_LLM_API_KEY=` и требует пару в том же блоке.
**Приёмка.** Тест зелёный и красный на блоке без пары; три страницы исправлены.

#### UX-24 · P1 · S — `looplab smoke` без лекарства

**Наблюдение.** Без модели `smoke` печатает `text FAILED: LLM request to http://localhost:11434/v1
failed: Connection error.` и выходит с 1; ни классификации, ни переменных, ни `--backend toy`, хотя
README называет его «тем же чеком», что preflight.
**Предложение.** `smoke` при отказе соединения печатает те же строки лекарств, что preflight
(общая функция), и объясняет, что такое «text» (проверка текстового ответа).
**Приёмка.** Вывод `smoke` без модели содержит `LOOPLAB_LLM_BASE_URL` и `--backend toy`; тест.

#### UX-25 · P1 · S — `--backend toy` с `--goal` всё равно зовёт модель

**Наблюдение.** `looplab run --goal "minimize (x-3)^2" --kind quadratic --direction min --backend
toy` отказывает «Genesis couldn't reach the model … use --no-genesis». README обещает «`--backend
toy` … to stay offline».
**Предложение.** По умолчанию Genesis включён, только если бэкенд — модель: `--genesis` получает
значение «не задано», и при `backend=toy` оно значит `--no-genesis`; явный `--genesis` по-прежнему
просит модель.
**Приёмка.** Команда выше проходит офлайн; явный `--genesis --backend toy` без модели — отказ
`Refused:` (UX-01); тест.

#### UX-26 · P1 · S — Прогон под внешним агентом молчит

**Наблюдение.** `looplab run task.json --out runs/my-run --backend toy -s external_harness=true` 25 с
не печатает ничего; `inspect` после — «STOPPED WITHOUT A BOUNDARY … it is still running, or
whatever was writing it is gone». Оператор не отличает ожидание от зависания.
**Предложение.** При `external_harness=true` `run` печатает одну строку: «waiting for an external
agent: connect `looplab harness-mcp` to this run (run dir runs/my-run); see `looplab harness`».
**Приёмка.** Строка печатается до первого ожидания; тест.

#### UX-27 · P1 · S — Итог `run`/`inspect` без имени метрики и без следующего шага

**Наблюдение.** `BEST node 4: metric=1.34289 params={…}` — без имени метрики и направления;
следующий шаг («`looplab ui` или `runs/demo/tree.html`») есть только в комментарии YAML.
**Предложение.** Строка итога называет метрику, если контракт её объявил (`metric_uid`), и
направление: `BEST experiment #4: quadratic_loss = 1.34289 (lower is better)`; последней строкой
`run` — `next: looplab ui   (or open runs/demo/tree.html)`.
**Приёмка.** Итог демо содержит `quadratic_loss` и `lower is better`; `run` печатает `next:`; тест.

#### UX-28 · P2 · S — Settings свежего сервера: «3 customized values»

**Наблюдение.** `/api/settings` возвращает `overrides {}`, а экран говорит «3 customized values»,
«Seed from a prior run» несёт точку «differs from the engine default», «Reset all» активна.
Причина — сравнение пустой строки с `null` в проверке изменений `ui/src/Settings.jsx`.
**Предложение.** Сравнивать после нормализации пустого значения (`''` ≡ `null` для необязательных
полей).
**Приёмка.** Свежий сервер — «0 customized values», «Reset all» неактивна; UI-тест.

#### UX-29 · P2 · S — Walkthrough обещает «true degree is 2», прогон находит 3

**Наблюдение.** CLI walkthrough шаг 3: цикл «discovers … the true degree is 2». Прогон печатает
`BEST node 11: … params={'degree': 2.9883, …}`; адаптер применяет `int(round(degree))`, то есть
победитель — степень 3; степень 2 оценена четыре раза с одинаковыми параметрами.
**Предложение.** Переписать утверждение под то, что прогон показывает (поиск находит модель с
наименьшей ошибкой на данных; степень — параметр, который он подбирает), без обещания конкретного
числа.
**Приёмка.** Walkthrough не обещает степень; тест по тексту.

#### UX-30 · P2 · S — Вход в демо исчезает после первого run

**Наблюдение.** Когда первый run существует, `/` показывает только список; «Try the offline demo»
пропадает, а четыре подсказки Ассистента требуют модели.
**Предложение.** Пункт «Run the offline demo» в меню «New run» — та же карточка, что на пустом
корне.
**Приёмка.** При непустом списке демо доступно из «New run»; UI-тест.

#### UX-31 · P2 · S — Карточка демо: «Metric not explicitly stated»

**Наблюдение.** Карточка запуска демо пишет `Score: Metric not explicitly stated`, хотя спек
объявляет `metric_uid: quadratic_loss`.
**Предложение.** Карточка читает `comparison_contract.metric_uid` (и `unit`), когда задача их
объявила.
**Приёмка.** Карточка демо показывает `quadratic_loss`; UI-тест.

#### UX-32 · P2 · S — Офлайн-демо пишет в общую память `~/.looplab`

**Наблюдение.** Демо и toy-прогоны молча пишут уроки в `~/.looplab/memory`; следующий прогон той
же задачи получил `prior_injected` из toy-прогона. Ни одна страница входа не называет `~/.looplab`.
**Предложение.** `examples/demo.yaml` и спек демо в UI ставят `memory_dir: null` — демо ничего не
пишет вне своего каталога; Installation называет `~/.looplab` и `LOOPLAB_MEMORY_DIR`.
**Приёмка.** После демо `~/.looplab/memory` не создан; Installation называет каталог; тест.

#### UX-33 · P2 · S — Отказанный run оставляет пустой `engine.lock`

**Наблюдение.** Run, отказанный preflight, оставляет пустой каталог `runs/<name>/` с
`engine.lock`; UI его скрывает, а CLI потом видит «занятый» `--out`.
**Предложение.** Отказ до первого события убирает то, что создал (каталог, если он был создан этим
запуском и пуст, кроме замка).
**Приёмка.** После отказанного run каталога нет, если его не было до запуска; тест.

#### UX-34 · P2 · S — Первый `looplab ui` печатает ~100 строк Vite

**Наблюдение.** При отсутствии сборки `looplab ui` ~20 с собирает UI и печатает около 100 строк
вывода npm/Vite.
**Предложение.** Одна строка «building the UI once (about 20 s)…», вывод сборки — только при
ошибке.
**Приёмка.** Успешная автосборка печатает ≤ 2 строк; ошибка печатает хвост лога; тест.

#### UX-35 · P2 · S — В CLI нет списка прогонов

**Наблюдение.** Ни одна из 74 команд не перечисляет прогоны; `looplab inspect` без аргумента —
«Missing argument 'run_dir'».
**Предложение.** `looplab inspect` без аргумента перечисляет прогоны под `./runs` (новые первыми:
имя, задача, узлы, лучший результат, состояние) и называет `looplab inspect RUN`.
**Приёмка.** `inspect` без аргумента в каталоге с `runs/` печатает список; без `runs/` — одна
строка, как найти прогон; тест.

## 6. Целевые сценарии после исправлений

1. **Терминал, второй шаг.** `looplab init` → `looplab run looplab.yaml` без `data.csv`: одна строка
   `Refused: task.data_path: … not found (relative to …)`, выход 2, следующий шаг назван; `init`
   уже сказал, как попробовать офлайн (UX-01, UX-02, UX-04).
2. **Терминал, завершённый прогон.** `looplab resume runs/demo` → `already finished: 6/6
   experiments … looplab resume runs/demo --max-nodes 12`; журнал не растёт. `looplab inspect
   runs/demo` — шесть строк, первая — `best experiment #4: quadratic_loss = 1.34289 (lower is
   better)` (UX-05, UX-07, UX-18, UX-27).
3. **UI, первый run.** После демо Report говорит «Selected #4 … better by 77.01 …»; под ним «Next
   step: Open the selected experiment's solution»; ярлыка UNCONFIRMED нет; полоса стратегии —
   «Budget spent; combining the best results»; платные кнопки предупреждают «Needs a model» (UX-13,
   UX-14, UX-16).
4. **Справка.** `looplab stop --help` — несколько строк и опции; `looplab run --help` в терминале 80
   колонок читается без переносов по два слова; ни одна команда пользователя не цитирует документ
   (UX-10, UX-11).
5. **Хостинговая модель.** Рецепт из walkthrough (ключ + его базовый URL) проходит `smoke` и
   `run`; без модели `smoke` называет те же лекарства, что preflight (UX-23, UX-24).

## 7. Пакеты поставки

| Пакет | Пункты | Поверхности | Статус |
|---|---|---|---|
| 1. Сообщения CLI | UX-01, UX-02, UX-05, UX-07, UX-08, UX-24, UX-25, UX-26, UX-27, UX-33, UX-35 | `looplab/cli/*`, `core/errors.py`, `events/stop_account.py`, `adapters/dataset_task.py` | сделано 2026-10-10: `tests/test_cli_refusal_classes.py`, `test_cli_finished_run.py`, `test_cli_first_minutes.py`; отказ preflight кончает причину одной точкой, а не «Connection error..» |
| 2. UI первого run | UX-13, UX-14, UX-15, UX-16, UX-17, UX-28, UX-30, UX-31 | `core/comparison.py`, `engine/comparability.py`, `ui/src/report.js`, `resultNoticeModel.js`, `RunView.jsx`, `Report.jsx`, `Settings.jsx`, `RunList.jsx` | сделано 2026-10-10: `tests/test_deterministic_contract.py`, `ui/test/deterministicObjective.test.js`, `whyStripPlain.test.js`, `settingsFreshDefaults.test.js`; у детерминированного результата нет и чипа «repeat checks are not established» рядом с «repeat checks are not needed» (`deterministicObjective.test.js`); полоса стратегии завершённого run не называет следующий шаг и говорит «Budget spent» (`whyStripPlain.test.js`) |
| 3. Справка команд | UX-10, UX-11, UX-12, UX-34 | докстринги команд, `cli/help_panels.py`, `core/appconfig.py`, `tests/test_cli_help_panels.py` | сделано 2026-10-10: `tests/test_cli_help_panels.py`; у всех 74 команд `--help` без цитат — 37 команд сопровождающего показывают одну строку, а прежний текст хранят после `\f`, который `--help` не печатает; shrink-only список пуст |
| 4. Пути и примеры | UX-04, UX-06, UX-22, UX-23, UX-29, UX-32 | `adapters/*_task.py`, снимок задачи, `tasks.md`, `cli-walkthrough.md`, `llm-and-agents.md`, `examples/` | сделано 2026-10-10: `tests/test_task_paths.py`, `test_api_key_recipes.py`, `test_entry_page_budgets.py` |
| 5. Словарь | UX-18, UX-19 | вывод `run`/`inspect`, заголовок Report, список run, `docs/guide/glossary.md` | сделано 2026-10-10: `docs/guide/glossary.md`, `tests/test_glossary.py`; панель концептов не пишет «(the run is tagged)» про run без тегов, где merge-эксперимент не унаследовал теги от неотмеченных родителей (`ui/test/conceptChipBar.test.js`); `timings` озаглавливает блоки `experiment #N` и `run setup (node -1)`, столбец меток выровнен (`tests/test_timings.py`); список команд и справка `timings`/`tensorboard`/`export-git` говорят «experiment», буквальные имена (`--node`, `nodes/`, тег `node-<id>`) остаются (`tests/test_cli_help_panels.py`) |
| 6. Документация как система | UX-20, UX-21, UX-09, UX-03 | `00-INDEX.md`, `tests/test_entry_page_budgets.py`, `replay --summary`, предупреждение фенса | сделано 2026-10-10: `tests/test_index_language_tags.py`, потолки прозы в `test_entry_page_budgets.py` |

Порядок (§12): 1 (сначала UX-01/UX-02 — один тип отказа, затем функция запаса для UX-05/UX-07) →
4 (UX-04 зависит от формата отказа UX-02) → 2 → 3 → 5 (глоссарий раньше переименования) → 6.
Ни один пакет не меняет фолд, типы событий или промпты. Новые данные — два аддитивных поля:
`ComparisonContract.deterministic` и `repeat_checks` в записи сопоставимости узла (UX-13), с
отсутствием для старых журналов.

## 8. Метрики приёмки

| Метрика | Сейчас (`38b7a46`) | Цель |
|---|---|---|
| Отказов в рамке Typer среди путей §4.2 | 4 из 6 | 0 (только синтаксис аргументов) |
| `raise` в `looplab/cli` без класса «синтаксис/отказ» | 34 | 0 (тест-таблица) |
| Строк, дописанных `resume`/`stop`/`run` на завершённом прогоне без запаса | 17 / 1 / 17 | 0 / 0 / 0 |
| Строк `inspect` на завершённом демо; фраз «names no reason», «phase beacon» | 9; 2 | ≤ 6; 0 |
| Команд с цитатами в `--help` | 37 из 74 (5 — пользовательские) | 0 из 74 |
| Строк `run --help` / `ui --help` / `stop --help` при 80 колонках | 135 / 59 / 45 | ≤ 60 у команд пользователя |
| Ширина столбца текста в каждой панели `run --help` при 80 колонках | ~20 | ≥ 40 |
| Report демо: «Repeat … seeds», `UNCONFIRMED`, кнопок `paid` без условия | есть, есть, 1 | нет, нет, 0 |
| Слов движка на видимой полосе стратегии демо (`ASHA`, `rung`, `endgame`, `reserve`, `sweep`) | 5 | 0 |
| Первая кнопка полосы видов | Energy (анимация) | вид |
| Рецептов ключа без базового URL в документации | 3 | 0 (тест) |
| `smoke` без модели называет лекарство | нет | да |
| Страница глоссария | нет | ≤ 1 500 слов, в nav |
| Пометка языка в строках индекса | 0 | у каждой строки |
| Потолок прозы у рукописных страниц руководства > 10 000 слов | нет | у каждой |
| Относительный `data_path` из другого каталога; `resume` из другого каталога | отказ; отказ | работает; работает |

Структура этого реестра закреплена `tests/test_doc75_registry.py`: у каждого `UX-NN` есть
наблюдение, предложение и приёмка, каждый пункт назначен ровно в один пакет §7, и счёт в §1
получен парсером.

## 9. Отвергнутые альтернативы

- **Переписать справочники на Use/Reference** — отвергнуто doc 74 (цена против измеренной
  пользы); здесь вместо этого потолок прозы (UX-21) и глоссарий (UX-19).
- **Убрать предупреждение `CAP_DAC_OVERRIDE` или переписать `harden_guarantee`** — нет: факт и его
  фраза — продукт безопасности; меняется только строка WARNING (UX-03).
- **Снять ярлыки неопределённости вообще** или выводить детерминизм из префикса «none» — нет:
  `UNCONFIRMED` верен для любой задачи с шумом, а «none» значит «не объявлено»; детерминизм
  объявляется явным полем контракта (UX-13).
- **Поднять `BadParameter` в `OperatorRefusal` глобальным обработчиком** — одна обёртка над Typer
  спрятала бы и настоящие синтаксические ошибки; классифицируется каждое место (UX-01).
- **Счётчик «`BadParameter` ≤ 20»** — снят: базу считали grep с комментариями (40 против 34), и
  число не отличает отказ от синтаксиса; вместо него тест-таблица (UX-01).
- **Ослабить правило пары ключ/URL** («ключ из того же окружения принадлежит `LLM_BASE_URL`») —
  нет: пара — защита ключа от отправки не тому адресу; чинятся рецепты (UX-23).
- **Запрещать повторный `run` на завершённом `--out`** — нет: ветка «reopen» нужна после `error`
  и для большего бюджета; меняется только случай «нет запаса» (UX-05).
- **Сменить вывод `replay` по умолчанию** — нет: `| jq` обещан и используется; `--summary` (UX-09).
- **Переименовать роль `boss`** — нет: это значение конфигурации (`AGENT_ROLES`); слово получает
  определение (UX-12, UX-19).
- **Спрятать Deep Research и бейдж Concepts** — нет: первое — вход в исследование, второе
  запрещено выдавать за пустой run; свернуть и переформулировать (UX-14, UX-16).
- **Сделать `node` единственным словом или переименовать всё в `experiment`** — нет: «experiment»
  не равно узлу один к одному, а переименование переключевало бы переводы; малый срез (UX-18).
- **Переписать `AGENTS.md`** — отложено: сокращение плотной прозы после хорошего начала — мнение
  одного критика, нужна проверка на пользователе (doc 74 §12.9). Сделана измеримая половина: Codex
  читает `AGENTS.md` как инструкции контрибьютора репозитория, поэтому файл теперь начинается с
  того, для кого он, и отправляет агента, меняющего код LoopLab, в `CLAUDE.md` (тест в
  `tests/test_entry_page_budgets.py`).

## 10. Воспроизводимость

```bash
# CLI
time looplab run examples/demo.yaml --out /tmp/ll/demo
looplab inspect /tmp/ll/demo | wc -l; looplab replay /tmp/ll/demo | wc -c
looplab resume /tmp/ll/demo; looplab stop /tmp/ll/demo; looplab run examples/demo.yaml --out /tmp/ll/demo
wc -l /tmp/ll/demo/events.jsonl                      # 110 -> 145 (последний seq 112 -> 147)
looplab run --goal "minimize (x-3)^2" --out /tmp/ll/g; echo $?          # рамка Typer, 2
looplab run examples/toy_task.json --out /tmp/ll/t; echo $?             # Refused: … [unreachable], 2
(mkdir -p /tmp/ll/i && cd /tmp/ll/i && looplab init && looplab run looplab.yaml); echo $?   # дамп pydantic, 2
grep -c "raise typer.BadParameter(" looplab/cli/*.py | awk -F: '{s+=$2} END {print s}'   # 34
# перепись --help: размер, цитаты, жаргон (37 цитирующих на любой ширине)
for c in $(looplab --help | grep -oP '^│ \K[a-z][a-z0-9-]+'); do
  COLUMNS=80 looplab $c --help | awk -v c=$c '{b+=length($0)+1} /doc ?[0-9]|§|[a-z_]+\.py\b|::|ADR-/ {n++} END {print c, b, NR, n+0}'
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
при необходимости строка в §8; новый раздел не добавляется, журнал реализации — коммиты. Пересмотр
§12 сделан ДО начала реализации и поэтому правит этот номер, а не открывает новый; следующий
пересмотр по существу — новый номер. Единственный открытый пункт doc 74 — проверка на новых
пользователях по протоколу doc 74 §12.9 — не закрыт и этим документом: он требует людей, а не
сессии.

## 12. Критика плана и выбор

Четыре независимых критика (субагенты, только чтение) прошли план и дерево на `2daeb56`: (1) аудит
doc 74 — что из его «сделано» держится; (2) новичок — те же десять минут с нуля, с хостинговой
моделью и без; (3) ревью кода диапазона doc 74; (4) аудит этого плана — каждое наблюдение заново
и лучший вариант каждого предложения. Каждое утверждение критиков перепроверено командой или
чтением кода до того, как попасть сюда.

**Исправленные наблюдения.** Цитирующих справок 37, не 43 (на любой ширине; 213 986 байт при 80
колонках, не 168 311); `raise typer.BadParameter(` — 34 (40 — совпадения grep с комментариями);
журнал демо — 110 строк (112 — последний `seq`), после трёх команд 145; «boss» в руководстве есть
(17 мест) и это значение конфигурации; `timings` на демо — 66 из 79 строк «0.0 min»; снимок задачи
хранит относительный путь, и `resume` зависит от каталога (в UX-04); фраза `stop_account`
печатается и в `run`/`resume`, а демо остановилось на `max_nodes` (в UX-07); повторная
финализация на модели стоит денег (в UX-05).

**Выбранные варианты** (у каждого пункта — фраза «Пересмотрено (§12)»): единый тип отказа с
тест-таблицей вместо счётчика и preflight перед Genesis (UX-01); функция запаса по потолку движка
вместо `len(nodes)` (UX-05); правка в `stop_account`, а не в `inspect` (UX-07); абсолютные пути в
снимке и отказ при двух кандидатах вместо порядка поиска (UX-04); явное поле контракта и запись
движка вместо префикса «none» и чтения сайдкара (UX-13); структурная строка стратегии вместо
переписывания `rationale` (UX-14); условие «модель не настроена или проверка провалилась» вместо
«не проверена» (UX-16); `replay --summary` вместо смены вывода (UX-09); потолок прозы вместо
храповика всей страницы (UX-21); малый срез словаря (UX-18).

**Добавлено:** UX-23…UX-35 — рецепт ключа (P0), `smoke`, `--backend toy` с `--goal`, молчание под
внешним агентом, строка итога, Settings «3 customized values», степень в walkthrough, вход в демо
после первого run, метрика на карточке демо, память `~/.looplab`, пустой `engine.lock`, шум
первой сборки UI, список прогонов.

**Doc 74.** Аудит итогов doc 74 (критик 1; doc 74 §11 запрещает ему расти после §12.9, поэтому
запись здесь, а у его пунктов — строка статуса со ссылкой сюда). Все 59 тестов приёмки были
зелёными, но часть проверяла не критерий. Найдено и исправлено:

- **Указатели на файлы вместо проверок.** UI-строки `tests/test_doc74_acceptance.py` называли файл
  теста; теперь каждая называет тест, который проверяет критерий (EB-18 — видимый текст экрана
  Model, EB-19 — счёт кнопок, EB-21 — порядок «фраза, затем ярлыки», EB-22 — полноэкранный
  Ассистент, EB-24 — карточка демо), а проверка EB-23 вырезает и блочные комментарии.
- **Завышенные статусы.** EB-10, EB-11, EB-17, EB-21, EB-22, EB-24 получили строку статуса у пункта
  и в doc 74 §12.8; остаток передан сюда (EB-10 → UX-09, EB-21 → UX-13, EB-17 → UX-02). Пометки
  пунктов — только «уточнено», «пересмотрено», «снято», и doc 74 §12.2 п. 6 выводит их список
  тестом.
- **Числа.** Один метод счёта — `split()`, как в тестах; бюджет Quickstart в тесте — 700, как в
  приёмке; «804», а не «810», в докстринге теста бюджетов; устаревшие счётчики (CLAUDE.md, число
  тестов) заменены ссылкой на тест или помечены.
- **Ревью кода** (критик 3) нашло три дефекта UI, исправленных тестом, который краснеет на старом
  коде: экран «Starting the run…» мигал и уводил фокус каждую секунду; полноэкранный Ассистент
  остался без выбора языка; компактный список прятал фильтр, когда портфель уменьшался. Плюс
  `inspect` читал журнал дважды.
