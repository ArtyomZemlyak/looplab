# 82. Простой вход: Assistant в UI и внешний агент без токенов (план, 2026-10-19)

*EN summary: a re-verified simplicity plan for LoopLab's two beginner entries — the UI Assistant
(goal in words → launch card with a "who drives: LoopLab / external agent" choice) and an external
agent such as Codex that creates and drives the run itself through a new MCP `start_run`. Anonymous
mode is the main path: no tokens. Seven packages, cheapest first; every claim checked against
`6dd598083`. Nothing here is shipped yet.*

**Дата:** 2026-10-19. **База проверки:** `master` @ `6dd598083`. **Источник:** исходный план по двум
ревью Claude Code (`/home/jovyan/data/looplab-reviews/20261019-simplicity-plan/PLAN.md`). Каждый его
пункт перепроверен по коду этого дерева. Ниже записаны вердикт, поправки и новые находки.
Связанные документы: [doc 71](71-onboarding-and-entry-barriers-2026-09-30.md) (два пути новичка),
[doc 74](74-entry-barrier-inspection-2026-10-09.md), [doc 78](78-external-agent-orchestration-2026-10-14.md)
(пробуждение агента), [doc 81](81-campaign-in-one-run-2026-10-14.md) (кампания в одном ране),
руководство [external-harness](guide/external-harness.md).

Статус: план в работе. Сделано: П1 (анонимное подключение агента, экстра `ui` с MCP), П2 (профиль
`agent-minimal`, `strategist_every=0`, вывод `backend=toy` для external-запуска), П3 (MCP `start_run`), П4 (карточка «Кто ведёт» и строка для Codex).
Решения §8 приняты по рекомендациям по умолчанию (Р1–Р7).

## 1. Требования пользователя

Это контракт плана. Если пакет с ним расходится, прав контракт.

1. Максимальная простота и минимальный порог входа: меньше шагов, флагов, обязательных полей и
   понятий.
2. Токенов нет. Основной путь — анонимный режим. Токены остаются для тех, кто их уже включил, но
   ни один шаг новичка их не требует.
3. Новичок никогда не пишет `task.json` и не запускает `looplab run`. Входов ровно два:
   - **(А) Assistant в UI.** Пользователь пишет цель словами и получает карточку запуска.
   - **(Б) Внешний агент (Codex и другие).** Пользователь говорит агенту цель. Агент сам создаёт
     run и ведёт его.
4. Автоматика и разумные значения по умолчанию лучше новых флагов.

## 2. Путь новичка после доработок

**Б. Через внешнего агента**, три шага:

1. `pip install -e ".[ui]"` — после П1 экстра `ui` включает и MCP-клиент.
2. Один раз: `codex mcp add looplab -- looplab harness-mcp`. Это одна команда; вручную
   `config.toml` никто не пишет.
3. Сказать Codex: «сделай эксперимент X на данных Y». Codex вызывает MCP `start_run`. Если UI-сервера
   нет, `harness-mcp` поднимает его сам. Codex получает `run_id` и ссылку на UI для пользователя,
   затем ведёт run через `run_progress` → `submit_experiment` → `wait_for_next`.

**А. Через Assistant в UI.** Шаги «Начать новый запуск» → цель → карточка → «Проверить» → «Начать»
уже есть. Карточку строит Assistant через `propose_run` (`tools/run_launcher_tools.py`). После П4 в
карточке появится один выбор: «Кто ведёт: LoopLab / внешний агент». При втором варианте run
стартует в external-режиме, а на странице запуска появится одна строка для Codex, которую можно
скопировать. Если у LoopLab нет своей модели, Assistant работать не может. Тогда UI вместо ошибки
показывает строку пути Б с уже подставленной целью.

**Сейчас** путь Б занимает пять шагов AGENTS.md: два разных токена, ручной `looplab run` с
`--backend toy -s external_harness=true`, MCP-конфиг с токеном и `connection_check`. Без токена он
обрывается на первом же шаге.

## 3. Сверка исходного плана с кодом

| Пункт плана | Вердикт | Что показал код (`6dd598083`) | Поправка |
|---|---|---|---|
| A6 анонимный агент закрыт | Подтверждено, но неполно | `run_stdio` отказывает без токена (`harness/mcp_server.py:1291-1295`). Кроме того, без токена отказывает и `connection_check`: при `credential_configured=false` он возвращает `harness_credential_missing` (`mcp_server.py:705-710`), а AGENTS.md велит вызывать его первым. Та же логика в тексте handoff (`harness/handoff.py:70`), в `HarnessHandoff.jsx:80-83` и в дескрипторе Codex с `env_vars=["LOOPLAB_HARNESS_TOKEN"]` (`ui/src/harnessHandoff.js`). Сам сервер по умолчанию уже анонимен: авторизация включается явно (`serve/owner_token.py:202-208`). | Снять обе проверки (П1). Отказ оставить, только если токен совпадает с UI-токеном или если в окружении MCP есть `LOOPLAB_UI_TOKEN` без harness-токена (защищённый сервер). |
| A2 нет атрибуции в открытом режиме | Подтверждено | Ветка `else` (`serve/server.py:734-757`) не вызывает `_observe_agent_request`. `submitted_by` ставится только при `request_agent_token` (`serve/run_commands.py:3086-3089`); ответы чекпоинтов идут как `human` (`routers/runs.py:1201`); `origin` загрузки — `operator_upload` (`harness/per_query.py:392-394`). | `request_agent_token` читают 19 мест в 8 роутерах, и он смешивает авторизацию с атрибуцией. П5 должен их развести, а не переиспользовать флаг вслепую. |
| A3 tmpfs не определяется | Подтверждено | Ни одного `statfs`/`/proc/mounts`. Зеркало есть только как CLI `looplab run-mirror` (`cli/mirror_cmds.py`). Модуль сам не берёт блокировок: в докстринге `events/run_mirror.py` прямо сказано «takes no lock». | Фоновый поток в UI противоречит принципу модуля: зависший FUSE-вызов должен держать только процесс зеркала. Поэтому зеркало запускать дочерним процессом под надзором UI. CLI и UI-зеркало исключают друг друга через flock (П6). |
| B1 19 из 23 обязательств | Подтверждено замером | `run_obligations(Settings(backend="toy", external_harness=True))` даёт 23 обязательства, из них 19 обязательны. С 16 значениями набора agent-minimal из §6 П2 остаются только `strategy` и `stage_check`. `strategy` нельзя выключить: `strategist_every` имеет `ge=1` (`core/config.py:1839`), а `harness/decisions.py:51` требует обзор каждые N узлов. `research` обязателен при дефолтном `deep_research_every=0` (`harness/obligations.py:270`), выключается значением `-1`. `stage_check` срабатывает лишь на проверке стадии, объявленной оператором. | Разрешить `strategist_every=0` (= выкл.). Движок 0 уже понимает (`engine/cadence.py:36-43`). `stage_check` оставить. |
| C1 `submit_experiment` без `node_kind`/`uses` | Подтверждено | Сигнатура `mcp_server.py:1193-1218`. `required_decisions` (`decisions.py:41-53`) не смотрит на вид узла, поэтому artifact-узел проходит гейты новизны. | Без изменений (П7). |
| C3 `uses` только на artifact; баг `_resolve_arm` | Подтверждено частично | Запрет — только проверка валидации (`serve/control_validation.py:942-962`); движок читает любой узел через `produced_workdir` (`engine/eval_dispatch.py:58`). Узел плеча выбирается С фильтром по `experiment_id`+`arm` (`per_query.py:486-489`). Без фильтра выбирается загрузка внутри узла (`per_query.py:492-498`). | Баг уже, чем в плане. Он проявляется, когда один узел несёт загрузки двух плеч на один тест: тогда оба плеча читают последнюю. Исправление меняет сравнения старых ранов именно в этом случае (§7). |
| A4 `reason` у pause/resume | Подтверждено, дешевле | Контракт события `pause` уже допускает `reason` (`events/types.py:2302-2315`). Отказывает только HTTP-таблица `CONTROL_DATA_FIELDS` (`serve/protocol.py:417-421`). У `resume` контракт пуст. | Добавить поле в таблицу и optional `reason` в контракт `resume` (П5). |
| B3 устаревшие доки | Подтверждено | «twelve tools» в `docs/guide/external-harness.md:478`, а `build_server` регистрирует 38 инструментов. Шаги 1-4 AGENTS.md требуют токены и `looplab run`. Строка `ui/src/locales/ru.json:5115` — машинный перевод. | Без изменений (П1). Учесть, что первый раздел AGENTS.md закреплён тестом `tests/test_entry_page_budgets.py::test_agents_md_opens_with_a_short_first_run_sequence`. |
| П1 `external_harness` ⇒ `backend=toy` | Подтверждено, важнее, чем в плане | Сегодня `Settings(external_harness=True)` без `backend` падает (`config.py:3664`). `/api/start` выводит `backend=llm` для генеративных задач (`serve/launch.py:537-542`). Карточка genesis/TUI вписывает `backend=llm` явно (`routers/genesis.py:155-172`). | Без этого правила переключатель «внешний агент» в карточке получил бы 422. Правило нужно раньше UI (П2). |
| П2 `--external` | Снять | После вывода `backend` хватает `-s external_harness=true`, а новичок CLI не запускает. | Флаг не добавлять. Блок вывода CLI и короткий `looplab harness` → BACKLOG. |
| П2 agent-minimal по умолчанию | Подтверждено с ограничением | Профили уже есть: `PROFILES` (`config.py:294`), `_apply_profile` (`config.py:3585-3608`) заполняет только незаданные поля. Но 48 тестовых файлов строят `Settings(external_harness=True)` и ждут нынешних обязательств. | Профиль по умолчанию выбирать в точках запуска (`launch.py::_resolved_settings`, `cli run`, `start_run`), а не в валидаторе `Settings`. |
| Путь Б: `start_run(goal, data)` через genesis | Переделать | Genesis вызывает модель LoopLab (`routers/genesis.py:296-303`), без модели он возвращает `ok:false`. В пути Б модель — сам агент, своей модели у LoopLab может и не быть. В открытом режиме сервер `/api/start` не запрещает: deny-list работает только в ветке токена (`server.py:668-702`). После П1 агент может запустить run и через `api_request`, но это три вызова с `validation_token` и ключом идемпотентности. | Нужен `start_run(task, …)`. Агент пишет composable-задачу по той же спецификации, что и Assistant (`propose_run`), а LoopLab проверяет её тем же preflight. Вариант «только цель через genesis» оставить как опцию, когда у LoopLab есть модель (решение Р2). |
| Карточка «сама включает agent-notify» | Нельзя как задумано | Хук — это argv, который выполняет сервер. Настраивает его только владелец (`serve/agent_events.py:38-41`), и UI не знает id сессии Codex. | Агент сам регистрирует свой thread через встроенный пресет `codex`. Запись переменных запуска агенту уже разрешена (`PUT /api/runs/{id}/agent-notify`) (П5). |
| П5 per-query «автоматически после terminal» | Частично есть | Загрузка из workdir узла уже работает (`workdir_path`, `per_query.py:406-426`). | Не хватает соглашения, откуда брать `metrics` без явной декларации (решение Р6). |
| Новое: экстра `harness` | Находка | MCP SDK живёт в отдельной экстре (`pyproject.toml:43`). С одной `pip install -e ".[ui]"` из README путь Б падает на импорте. | Включить `mcp` в `ui` или назвать один набор (П1, Р4). |

## 4. Что нужно пути Б (агент без токена запускает и ведёт run)

1. **Подключение без секрета.** `run_stdio` и `connection_check` работают без токена (П1). В
   анонимном режиме `harness-mcp` шлёт заголовок `X-LoopLab-Client: harness-mcp/<версия>`. До П5 сервер
   его не читает.
2. **Сервер находится сам.** Если `LOOPLAB_HARNESS_URL` не задан, `harness-mcp` пробует
   `http://127.0.0.1:8765`. Если сервер не отвечает, MCP поднимает свой через
   `serve/tui_format.py::ensure_server` — TUI делает так уже сегодня (П3). Корень ранов по умолчанию
   `./runs` в рабочей папке агента.
3. **Задача без `task.json`.** `start_run(task, run_id?, settings?, key?, dry_run?)`. Схема `task` —
   composable-задача из `propose_run`: тот же текст подсказки, тот же `RunProposal`, тот же
   `preflight_start`. Ошибки валидации возвращаются полями, чтобы агент исправил их и повторил вызов.
4. **Режим включается сам.** `start_run` всегда ставит `external_harness=true`. Точка запуска выводит
   `backend=toy` и профиль `agent-minimal`, если их не задали (П2). Явное значение из этого же запуска
   побеждает.
5. **Запуск надёжен.** Составной вызов делает preflight, затем `POST /api/start` с
   `validation_token` и ключом идемпотентности из `key`, затем опрос `/api/start/{run}/status` и
   чтение `/state`. Тело сохраняется на стороне клиента до ответа (`ClientRequests`, как у остальных
   записей). Ответ: `run_id`, `generation`, `ui_url`, `next_step`.
6. **Право на запуск.** В открытом режиме ограничений нет. После П5 (заголовок даёт агенту область
   токена) нужно исключение: агенту можно `/api/start`, `/api/start/preflight`, `/api/start/{run}/status` и
   `/api/validate` (но не `resolve-claim`), только если в итоговых
   настройках `external_harness=true`. Genesis агенту по-прежнему закрыт, потому что тратит модель
   LoopLab. В токен-режиме запуск агентом остаётся запрещённым (решение Р3).
7. **Первые шаги видны.** `MCP_INSTRUCTIONS` открываются тремя строками: `start_run` →
   `run_progress` → `submit_experiment`/`wait_for_next`. При agent-minimal первый `next_step` свежего
   рана — сразу «предложи эксперимент», а не «напиши research memo».

## 5. Что нужно карточке запуска (путь А)

1. **Поле «Кто ведёт».** В `LAUNCH_RUNTIME_FIELDS` (`ui/src/launchDraft.js:5`) добавить
   «LoopLab / внешний агент». Вариант «внешний агент» пишет в settings JSON
   `external_harness: true` и `backend: "toy"`, а при переключении назад возвращает прежнее
   значение `backend`. Явный `toy` нужен потому, что карточка или сохранённые настройки могут уже
   нести `backend: "llm"`.
2. **Genesis не мешает.** `_normalize_genesis` не вписывает `backend=llm`, если в карточке есть
   `external_harness: true`.
3. **Профиль виден.** В список `profile` добавить `agent-minimal`. Preview preflight показывает
   выбранный профиль и сколько обязательств останется.
4. **Одна строка для агента после запуска.** Для external-рана `HarnessHandoff` при
   `credential_configured=false` показывает анонимный вариант: строку
   `codex -c 'mcp_servers.looplab.command="looplab"' -c 'mcp_servers.looplab.args=["harness-mcp"]' "<инструкция с run_id>"`
   и кнопку «Скопировать». Блок «2. Supply the scoped credential» не показывается. Текст инструкции
   больше не говорит «Подключение не разрешает новый запуск»: это верно для чужого рана, но не
   для пути Б.
5. **Нет модели — есть выход.** Если Assistant недоступен из-за модели, UI показывает строку пути Б с
   целью пользователя, а не только ошибку.

## 6. Пакеты (от дешёвых к дорогим)

Общие правила для всех пакетов (CLAUDE.md): точечные тесты прогоняются первыми
(`python -m pytest tests/<файл>.py --basetemp=<свой каталог>`). Изменённый дефолт или новое поле
`Settings` получает строку в `docs/guide/configuration.md` с верным дефолтом
(`tests/test_config_docs_sync.py`, бюджет строки в `tests/test_doc_surface_budgets.py`). Новый тип или
поле события получает строку `EVENT_PAYLOAD_KEYS`, после чего `docs/guide/event-reference.md`
перегенерируется. Новые строки UI добавляются с переводом в `ui/src/locales/ru.json`
(`ui/test/localeCatalogueQuality.test.js`). Схема процесса `docs/infographic/agent-architecture.html`
обновляется в той же правке, если меняется поток.

### П1 (S). Анонимное подключение агента

- **Файлы:** `looplab/harness/mcp_server.py` (`run_stdio`, `connection_check`, заголовок
  `X-LoopLab-Client`), `looplab/harness/handoff.py` (тексты политики для анонимного режима),
  `ui/src/harnessHandoff.js` и `ui/src/HarnessHandoff.jsx` (дескрипторы без `env_vars`, без блока
  про секрет), `looplab/cli/harness_cmds.py` (справка), `pyproject.toml` (экстра), `AGENTS.md`
  (первые пять шагов), `docs/guide/external-harness.md` (раздел «Codex как живой агент»: tmux,
  `codex mcp add` или `codex -c …`; `codex exec` и `codex queue` без открытого клиента сессию не
  ведут; «twelve tools» → живой список), `README.md` (одна строка про агента), `ui/src/locales/ru.json:5115`.
- **Суть:** без токена `harness-mcp` работает анонимно. Отказ остаётся в двух случаях: токен равен
  `LOOPLAB_UI_TOKEN` или в окружении MCP есть только `LOOPLAB_UI_TOKEN` (сервер защищён). На ответ
  401 агент получает понятное сообщение. `connection_check` в анонимном режиме выдаёт `ok` с
  `credential: "anonymous"` вместо `harness_credential_missing`.
- **Тесты:** переписать сознательно `tests/test_harness_mcp.py:55-75` и `:108-116`,
  `tests/test_harness_connection.py:159-167`, `ui/test/harnessHandoff.test.js:163-189`. Добавить:
  анонимный stdio открывает транспорт без заголовка токена; UI-токен никогда не уходит; анонимный
  `connection_check` проходит против открытого `make_app`. Проверить
  `tests/test_entry_page_budgets.py` (≤200 слов первого раздела AGENTS.md, список обязательных слов),
  `tests/test_api_key_recipes.py` и `tests/test_documentation_contracts.py`.
- **Риски:** на открытом UI агент получает права владельца. Это уже так для любого локального
  процесса, но MCP делает это удобным. Ограждения даёт П5. Удалённый клиент на общем origin
  (JupyterHub) без токена не пройдёт: это верно и не меняется.
- **Готово, когда:** на чистом окружении `pip install -e ".[ui]"`, `looplab ui`, `codex mcp add looplab
  -- looplab harness-mcp`, и `connection_check` для запущенного external-рана возвращает `ok`, без
  одной переменной окружения.
- **Автономно:** всё, кроме Р4 (состав экстры).

### П2 (S–M). Разумные значения по умолчанию для external-рана

- **Файлы:** `looplab/core/config.py` (`PROFILES["agent-minimal"]`, `strategist_every` `ge=0`),
  `looplab/harness/decisions.py:51` (0 = обзор стратегии не требуется), `looplab/serve/launch.py::
  _resolved_settings` (вывод `backend=toy` и `profile=agent-minimal`), `looplab/cli/run_cmds.py`
  (тот же вывод для `looplab run`), `looplab/serve/routers/genesis.py:155-172` (не вписывать `llm` для
  external), `looplab/harness/manifest.py` (строка `launch`), `looplab/serve/settings_ui_schema.json` (вариант
  `profile`, текст `strategist_every`), `ui/src/launchDraft.js` (список `profile`),
  `docs/guide/configuration.md`.
- **Суть:** набор `agent-minimal`, при котором обязательны только кандидат с кодом, измеренный
  результат и проверки, объявленные оператором: `novelty_mode=off`, `foresight=false`,
  `concept_pivot=false`, `concept_run_base=false`, `cross_run_concepts=false`,
  `track_hypotheses=false`, `reflection_priors=false`, `lessons_every=0`, `cross_run_curation=false`,
  `coverage_context=false`, `report_every=0`, `deep_research_every=-1`, `eval_deadline_grace_s=0`,
  `train_monitor=false`, `asha_live=false`, `stage_check_tools=false`, `strategist_every=0`. Замер на
  `6dd598083` для первых 16 значений: остаются `strategy` и `stage_check`; `strategist_every=0`
  снимает `strategy`. Правило вывода: если `external_harness=true` и `backend` не задан явно в том же
  или более высоком слое запуска, `backend=toy`; если `profile` не задан ни в одном слое,
  `profile=agent-minimal`.
- **Тесты:** новый тест на матрицу обязательств: `agent-minimal` даёт ровно `{stage_check}`, а
  явное поле из файла или запуска побеждает профиль. Preflight тесты: external-запуск генеративной
  задачи без `backend` даёт 200 и `backend=toy`, с явным `backend=llm` в том же слое — 422.
  Тест: снимок конфигурации старого external-рана (`profile=default`, полный dump) при resume
  сохраняет все обязательства. Тест на `strategist_every=0` у движка и у `decisions.py`.
  `tests/test_config_docs_sync.py`.
- **Риски:** выключенные `train_monitor` и `asha_live` означают, что никто не остановит
  расходящееся обучение. Цена — GPU-часы, а не корректность (Р1). Старый LoopLab не прочитает снимок
  с `profile=agent-minimal`: совместимость только вперёд.
- **Готово, когда:** свежий external-ран без явных настроек показывает в `harness-contract` одно
  условное обязательство, и первый `next_step` — «предложи эксперимент».
- **Автономно:** механика. Состав набора и отключение мониторов — Р1.

### П3 (M). `start_run`: внешний агент создаёт run сам (путь Б)

- **Файлы:** `looplab/harness/mcp_server.py` (инструмент, `MCP_INSTRUCTIONS`),
  `looplab/harness/composite.py` (составной запуск, как `finish_run`), `looplab/harness/client_requests.py`
  (сохранение тела запуска), `looplab/tools/run_launcher_tools.py` (вынести текст composable-задачи в
  общую константу, которую читают и `propose_run`, и `start_run`), `looplab/serve/tui_format.py`
  (`ensure_server`: переиспользовать; порт 8765, если свободен), `docs/guide/external-harness.md`,
  `AGENTS.md`.
- **Суть:** см. §4, п. 2–5 и 7. Сервер не меняется: в открытом режиме `/api/start` агенту доступен.
- **Тесты:** composite-тест на `MockTransport`: preflight → start → status → state, повтор с тем же
  `key` возвращает тот же run, потерянный ответ восстанавливается из клиентского журнала, 422
  возвращается с полями. Интеграционный тест против `make_app` в открытом режиме: `start_run` на
  toy-задаче создаёт external-ран с `backend=toy` и `profile=agent-minimal`. Тест `ensure_server`:
  второй вызов находит уже поднятый сервер. Обновить тест первого раздела AGENTS.md: шаг «launch»
  становится `start_run`.
- **Риски:** сервер, поднятый MCP, останавливается вместе с сессией агента. Движок при этом живёт
  (`REAP_ON_EXIT` снимается, как в TUI), но outbox и notify стоят, пока сервера нет. Две сессии
  агента могут поднять два сервера над одним корнем. Outbox к этому устойчив
  (`agent_events.py`, ключ дедупликации), но UI будет на разных портах. Задача, которую написал
  агент, может указать любой путь на диске сервера. На локальной машине это права самого агента,
  на общем сервере — решение Р3.
- **Готово, когда:** в чистой папке Codex по фразе «оптимизируй (x-3)^2» без единой подготовки
  создаёт run, отправляет кандидата и получает измеренный результат. Пользователь открывает UI по
  ссылке из ответа.
- **Автономно:** всё, кроме Р2 (genesis-вариант) и Р3 (токен-режим, общий сервер).

### П4 (M). Карточка «Кто ведёт» и строка для агента (путь А)

- **Файлы:** `ui/src/launchDraft.js`, `ui/src/LaunchCard.jsx`, `ui/src/HarnessHandoff.jsx`,
  `ui/src/harnessHandoff.js`, `ui/src/AssistantBar.jsx` (вариант «нет модели»), `ui/src/locales/ru.json`,
  `docs/guide/ui.md`.
- **Суть:** см. §5. Значение «Кто ведёт» по умолчанию: «LoopLab», если модель настроена, иначе
  «внешний агент».
- **Тесты:** `ui/test/launchDraft.test.js` (переключение пишет и откатывает `external_harness` и
  `backend`), `ui/test/launchCardSemantics.test.js`, `ui/test/harnessHandoff.test.js` (анонимная
  строка без токена, экранирование `run_id` в однострочнике), серверный preflight-тест из П2 для
  карточки с `backend=llm` от genesis.
- **Риски:** однострочник с кавычками в разных оболочках. Строка собирается из whitelisted-полей, как
  `harnessAgentInstruction`; для Windows PowerShell — отдельный вариант или только `codex mcp add`.
- **Готово, когда:** из Assistant без правки JSON запускается external-ран, и после копирования
  одной строки Codex его ведёт.
- **Автономно:** да; тексты на русском и английском проверить человеком.

### П5 (M). Атрибуция агента, пробуждение, `reason` у паузы

- **Файлы:** `looplab/serve/server.py` (ветка `else`, общий предикат deny-list),
  `looplab/serve/principal.py` (разделить «кто» и «что можно»), 8 роутеров с `request_agent_token`,
  `looplab/serve/agent_events.py` и `looplab/cli/harness_cmds.py::agent_notify` (пресет `codex`,
  `--codex [THREAD]`), `ui/src` (подпись в ledger), `looplab/serve/protocol.py:417-421`,
  `looplab/events/types.py` (`resume.reason`).
- **Суть:** в открытом режиме запрос с `X-LoopLab-Client: harness-mcp` атрибутируется агенту:
  активность, `submitted_by`, `answerer=agent`, `origin=agent_upload`. Вариант Р5: к такому запросу
  применяется та же область, что к токену (без запуска чужих ранов, правки настроек, удаления), с
  исключением для external-запуска из §4 п. 6. Это ограждение для честного агента, а не защита:
  заголовок может прислать кто угодно. В ledger статус `delivered` переименовать в «передано
  (exit 0)». `reaction` — новое событие от агента в течение 10 минут, одна повторная отправка.
  Пресет `codex` — фиксированный argv `codex queue --thread {thread} --message {message}`, который
  включает владелец; агент только сообщает свой `thread`. `reason` для pause/resume.
- **Тесты:** открытый `make_app` с заголовком и без: атрибуция есть только с заголовком; запросы
  браузера без заголовка ведут себя как раньше. Тест каждого из 19 мест: авторизация не стала шире
  или уже, кроме решённого в Р5. Тест на `reason` команд pause/resume и fold старых событий без
  него. Регенерация `docs/guide/event-reference.md` (`tests/test_event_payload_contract.py`).
- **Риски:** смешение авторизации и атрибуции в 19 местах — главный риск регрессии, поэтому пакет не
  S. Автопоиск сессии Codex зависит от формата `~/.codex/sessions` конкретной версии Codex CLI
  (проверено наличие `codex queue --thread` в 0.162.1).
- **Готово, когда:** в открытом режиме журнал команд, чекпоинты и загрузки подписаны агентом, а
  уснувший Codex просыпается от события рана без ручной настройки хука.
- **Автономно:** атрибуция, `reason`, подписи. Выбор пользователя: Р5 (ограждения) и включение
  пресета `codex` по умолчанию.

### П6 (M). tmpfs и зеркало ранов

- **Файлы:** `looplab/events/run_mirror.py` (flock на `src_root/.looplab-mirror/`),
  `looplab/cli/mirror_cmds.py`, `looplab/cli/ui_cmds.py` и `looplab/serve/server.py` (запуск дочернего
  зеркала при `LOOPLAB_RUN_MIRROR_DST`, `GET /api/run-mirror`), `ui/src` (баннер), помощник
  определения tmpfs в `looplab/core` (`statfs`/`/proc/mounts`), `docs/guide/deployment.md`.
- **Суть:** если корень ранов лежит на tmpfs, `looplab ui` и `looplab run` предупреждают об этом, а UI
  показывает баннер. При `LOOPLAB_RUN_MIRROR_DST` UI поднимает `looplab run-mirror --loop` дочерним
  процессом и перезапускает его, если тот завис (`exit_on_stuck`). Если CLI-зеркало уже держит
  блокировку, второе не стартует.
- **Тесты:** flock-тест на два `mirror_pass`; тест супервизора на фейковом зависании; тест на баннер
  в `GET /api/run-mirror`; детектор tmpfs на подменённом `/proc/mounts`.
- **Риски:** запуск процесса из UI-сервера, у которого уже есть свой reaper. Зеркало не должно
  останавливаться вместе с UI посреди копирования: публикация атомарна, поэтому достаточно
  корректного SIGTERM.
- **Готово, когда:** на tmpfs-корне без отдельного терминала журнал рана переживает перезапуск
  пода, а UI показывает, когда был последний проход зеркала.
- **Автономно:** да. Путь назначения по умолчанию задаёт пользователь (Р7).

### П7 (M–L). Кампания: виды узлов и per-query

- **Файлы:** `looplab/harness/mcp_server.py` и `looplab/harness/composite.py` (`submit_experiment`:
  `node_kind`, `uses`), `looplab/serve/control_validation.py:942-962` (`uses` на любые evaluated-узлы),
  `looplab/harness/decisions.py` (artifact-узлы без гейтов новизны), `looplab/harness/per_query.py`
  (фикс выбора загрузки внутри узла; автозагрузка `per_query/*.npz` и `per_query/<arm>.json` после
  terminal), `docs/81-campaign-in-one-run-2026-10-14.md` и `docs/guide/external-harness.md`
  (рецепт архивного плеча через `inputs_add`).
- **Тесты:** узел с загрузками двух плеч на один тест — каждое плечо читает своё; artifact-узел без
  receipt новизны принимается, эксперимент без него — нет; автозагрузка срабатывает один раз на
  lifecycle и не дублирует ручную; replay рана с `uses` на experiment-узел.
- **Риски:** фикс `_resolve_arm` меняет опубликованные сравнения старых ранов, у которых был такой
  узел; перед мержем пересчитать их и перечислить. Автозагрузке нужна схема `metrics` (Р6).
- **Готово, когда:** кампания из doc 81 проходит без ручных `upload_per_query`, а сравнение
  архивного плеча не требует отдельного рана.
- **Автономно:** фикс и `node_kind`/`uses`. Конвенция автозагрузки — Р6.

## 7. Совместимость (старые раны, replay, снапшоты)

| Изменение | Старые раны и replay | Снимки конфигурации |
|---|---|---|
| Анонимный MCP (П1) | События не меняются | Не затронуты |
| `backend=toy` и `agent-minimal` при запуске (П2) | Только новые запуски; fold не меняется | Снимок хранит полный dump с явным `profile`, поэтому resume ничего не перезаполняет. Новое значение `profile` старый LoopLab не прочитает |
| `strategist_every=0` (П2) | Старые снимки ≥1 | `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` не нужен: дефолт прежний |
| `start_run` (П3) | Обычный `/api/start`, тот же `task.input.json` → `task.snapshot.json` | Как у UI-запуска |
| Атрибуция (П5) | Новые записи команд и загрузок; старые остаются как есть. Пометка старых `operator_upload` — только в проекции, историю не переписывать | Не затронуты |
| `reason` у pause/resume (П5) | Optional-поле, fold без него прежний | Не затронуты |
| Зеркало (П6) | Формат манифеста прежний | Не затронуты |
| `uses`, artifact-гейты, фикс `_resolve_arm` (П7) | Admission — серверная проверка, replay её не повторяет. Фикс меняет пересчитанные сравнения (§6 П7) | Не затронуты |

## 8. Решения пользователя

| # | Вопрос | Рекомендация по умолчанию |
|---|---|---|
| Р1 | Выключать ли в `agent-minimal` живые мониторы обучения (`train_monitor`, `asha_live`) и `eval_deadline_grace_s` | Выключать. Они держат узел, пока агент спит. Тому, кто хочет мониторинг, достаточно правил `harness-monitor-rules` или явного поля |
| Р2 | Нужен ли `start_run(goal=…)` через genesis, когда у LoopLab есть своя модель | Нет на первом шаге. Задачу пишет агент, genesis остаётся пути А |
| Р3 | Разрешать ли агенту запуск в токен-режиме и на общем сервере | Нет. Токен-режим не трогать |
| Р4 | Одна экстра для UI и MCP | Добавить `mcp` в `ui` |
| Р5 | Заголовок `X-LoopLab-Client` в открытом режиме: только атрибуция или атрибуция + ограждения токен-области | Атрибуция + ограждения, с исключением для external-запуска |
| Р6 | Конвенция автозагрузки per-query (`metrics` без декларации) | Файл `per_query/<arm>.json` с полем `metrics` в самом файле; без него автозагрузки нет |
| Р7 | Путь зеркала по умолчанию | Без значения по умолчанию; баннер предлагает задать `LOOPLAB_RUN_MIRROR_DST` |

## 9. Вне пакетов (BACKLOG)

Release-wheel со собранным `ui_dist`, чтобы не нужен был Node: упаковка уже готова
(`pyproject.toml` `serve/ui_dist/*`, `setup.py`), не опубликован артефакт. `eval.tracks` в живом ране
и per-query от треков. Автостарт Codex-сессии из UI. Профиль `agent-standard`. Пометка старых
`operator_upload` в проекции. Блок вывода `looplab run` для external-рана и короткий
`looplab harness` с `--full`. MCP по HTTP прямо из UI-сервера (`codex mcp add --url`), чтобы на машине
агента не нужен был LoopLab.
