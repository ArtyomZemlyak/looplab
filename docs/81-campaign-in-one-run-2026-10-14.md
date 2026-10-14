# 81. Исследовательская кампания в одном ране: протокол, поправки и парные сравнения LoopLab

Дата: 2026-10-14. База: `c4e068b98`. Источник требований — аудит Codex как внешнего агента
(`/home/jovyan/data/looplab-reviews/20261014-codex-external-audit/report.md`, §4 «Почему почти
каждый эксперимент — отдельный ран», L-03/L-04/L-13). Связанные документы:
[doc 76](76-external-agent-ux-open-items-2026-10-13.md), [doc 77](77-experiment-tracking-open-items-2026-10-13.md),
руководство [external-harness](guide/external-harness.md).

## 1. Что было не так

- За 10-12…10-14 внешний агент создал 22 рана, почти по одному эксперименту на ран; у всех
  `task_id == run_id`, ни одного super-task, ни одного `comparison_contract`.
- Причина со стороны LoopLab: задача, входы оценки и protected-файлы рана были неизменяемы,
  а руководство советовало «Start a new run to change those obligations». Исправление G1 было в
  protected `sft.py` — отсюда цепочка g → g-v2 → g-v3.
- Главный результат кампании — сетка ~12 моделей на одном замороженном тесте 40k пользователей с
  парными CI по пользователям и поправкой Холма — считался 12 копиями `compare-*.py` и был не
  виден внутри LoopLab.
- Ритуал подачи эксперимента — ~11 вызовов API на узел (24 копии `submit.py`); пропускались
  ровно те шаги, для которых не было скрипта: вопрос исследования, предрегистрация, финализация.

## 2. Ран или super-task

| Ситуация | Что делать |
|---|---|
| Новый эксперимент на той же задаче и том же коде оценки | Тот же ран: `submit_experiment` (или `amend_protocol kind=experiment` + `inject_node`). |
| Нужен новый вход оценки, data-mount или protected-файл в списке | Тот же ран: пауза → `amend_protocol kind=task` → resume. |
| Нужно исправить protected-файл (трейнер, обвязку скорера) | Тот же ран: пауза → `amend_protocol kind=files` (diff + причина) → `retry_node` или новый узел. |
| Упала подготовка/запуск узла | Тот же ран и тот же узел: `retry_node` (новая попытка, `node_reset`), при нехватке бюджета — `amend_protocol kind=budget`. |
| Несовместимая задача (другой адаптер, другая метрика-объектив, другой скорер, который нельзя менять поправкой по политике оператора) | Новый ран, но с общим super-task (`campaign_supertask`) и ссылкой на предыдущий ран в goal. |
| Сравнение моделей из разных ранов на одном тесте | Ничего дополнительно: лидерборд по `test_id` охватывает все раны корня; эксперимент может объявить плечо из другого рана (`arms.X.run_id/node_id`). |

## 3. Журнал протокола рана

Файл `<run>/protocol_amendments.jsonl` — sidecar рядом с `harness_decisions.jsonl`; `events.jsonl`
и свёртка не меняются, старые раны читаются как пустой протокол. Каждая строка: `amendment_id`,
`version` (порядковый номер в ране), `kind`, `author` (`agent` = scoped-токен агента,
`operator` = владелец), `reason`, `recorded_at`, `at_event_seq` (хвост лога событий в момент
записи), `at_node_count`, `generation`, `action_id` (идемпотентность), `payload`.

- **`experiment`** — предрегистрация: `experiment_id`, `hypothesis`, `question`, `arms`
  (метка → `{run_id?, node_id?}`; без них плечо привязывается при загрузке результатов),
  `tests` (метка → замороженный `test_id`, если уже известен), `metrics` (`mean` | `ratio`,
  направление), `contrasts` (treatment, control, metric, test, `family`), `correction`
  (`holm` | `none`), `alpha`, `ci` (`normal` | `bootstrap`). Регистрация неизменяема; исправление
  — новый `experiment_id` с `supersedes`. Признак «зарегистрирован до результата» вычисляется:
  `at_event_seq` регистрации < seq терминального события узла-treatment (для узла другого рана —
  по времени).
- **`task`** — `inputs_add/remove`, `protect_add/remove`, `data_add/remove` в
  `task.snapshot.json`. Новая задача проходит ту же валидацию (`validate_task(existing_run=True)`),
  в записи — JSON-diff и `task_identity` до/после. Движок прочтёт её при следующем resume и сам
  запишет `task_changed`; строка журнала объясняет это изменение.
- **`files`** — файлы базового репозитория (`editable_path`), в том числе protected, с CAS по
  `expected_sha256`; в записи — unified diff и SHA-256 до/после. Файлы edit-surface так менять
  нельзя (они принадлежат узлам). Следующий материализованный жизненный цикл получит новые байты;
  resume запишет `workspace_changed`.
- **`budget`** — `add_nodes` с причиной; исполняется обычной durable-командой `budget_extend`,
  id которой записан в строке.

`task` и `files` требуют остановленного движка (иначе 409 `engine_running`) и отсутствия
активной команды. Изменение и его запись атомарны: если запись в журнал не удалась, файл/снимок
возвращается к прежним байтам.

**Политика оператора** — `<run>/harness_policy.json`, пишется только токеном владельца
(`PUT /api/runs/{id}/harness-policy`; агенту 403): `amendments` = `off` < `experiments` <
`inputs` < `all` (по умолчанию `all`) и `max_agent_added_nodes` (по умолчанию без лимита).
Лимит действует и на «сырую» команду `budget_extend` от агента, и на поправку `budget`;
счётчик — durable-записи команд агента.

## 4. Повтор упавшего узла — новая попытка того же узла

`retry_node` (MCP) = `node_reset` с текущим `generation` узла, по умолчанию `from_stage=eval`.
Узел остаётся тем же, `attempt` растёт, `node_failed` прежней попытки остаётся в логе, логи
прежней попытки архивируются (`attempt_logs/node_<id>/`, doc 78). Если исправление требует
protected-файла или нового входа — сначала поправка `files`/`task` при остановленном движке.
Не хватает места в бюджете — поправка `budget`.

## 5. Сравнение на общем тесте

**Загрузка.** `POST /api/runs/{id}/per-query` (MCP `upload_per_query`) для терминального узла:
`.npz` (читается без numpy и без pickle, `core/npyio.py`) или JSON с именованными массивами;
`query_key` (id запросов), `cluster_key` (пользователи; `null` — каждый запрос свой кластер),
`metrics`: `{имя: {kind: mean, array, columns?}}` или `{kind: ratio, num, den, columns?}`;
`columns` выбирает столбцы 2-D массива перед суммой по строке (например корзины популярности).
LoopLab сам:

- вычисляет `test_id = "qs1-" + sha256(отсортированные строки "query_id\tcluster")[:32]`;
- приводит векторы к каноническому порядку запросов и хранит их в `<run>/per_query/`;
- по `measured_check` сравнивает среднее загрузки с метрикой, которую он сам измерил для узла
  (`matches_measured` / `differs_from_measured`, допуск 1e-6 отн.);
- помечает загрузку `stale`, если узел потом сброшен.

**Расчёт** (`trust/paired.py`, на чтении, из сохранённых векторов):

- `mean`: Δ = среднее(t_i − c_i); SE = sqrt(G/(G−1) · Σ_g (Σ_{i∈g}(d_i − Δ))²) / n;
- `ratio`: Δ = Σn_t/Σd_t − Σn_c/Σd_c, SE дельта-методом с той же кластеризацией;
- 95% CI нормальный (z = 1.96), p двусторонний; Холм — внутри каждой объявленной `family`;
- опционально кластерный bootstrap (процентильный), с numpy — быстро, без numpy — с ограничением
  на число выборок.

Ответ `GET /api/runs/{id}/experiments/{eid}/comparison` несёт `computed_by` (метод
`looplab-paired-v1`), для каждой стороны — run/node/generation, `source_sha256`, автора загрузки,
согласие с измеренной метрикой, `stale`, а также `registered_before_result` и предупреждения
(post hoc, stale, расхождение с измеренным, «интервалы не одновременные; дисперсия по seed
обучения не учтена»).

**Лидерборд.** `GET /api/shared-tests/{test_id}?metric=&reference=run:node[&supertask_id=]` —
все узлы всех ранов корня с загрузкой на этот тест: значения с кластерным CI и, при `reference`,
парные Δ.

**Проверка на реальных данных.** Копии ранов D3, D4, D5 MiniOneRec и их `original_ic.npz`:
загрузка через API дала `matches_measured` (расхождение ~1e-15 с метрикой LoopLab), D4−D3 =
+0.000475 (SE 0.000987, p 0.630), D4−D5 = +0.01260 (SE 0.00114, p 1.5e-28, Холм 3.0e-28),
rare-micro D4−D3 = +0.00315 — побитово совпадает с `D4-co-primary-Holm2.json` и
`all-budgets-through-D4-40k.json` Codex.

## 6. Отчёт кампании (super-task)

- Агент может создать super-task и назначить раны (`campaign_supertask`); удалять — нет.
- `GET /api/supertasks/{sid}/campaign`: раны super-task, их эксперименты с вычисленными
  контрастами и поправками, лидерборды по всем `test_id`, текущий нарратив.
- `POST /api/supertasks/{sid}/campaign/narrative`: `body_markdown` (GFM), `title`,
  `key_findings`, CAS по `expected_revision`, автор фиксируется. Хранение —
  `<run-root>/.campaign-reports/<sid>.jsonl`, все ревизии.
- UI: в отчёте super-task (Runs → фильтр super-task → «Report») блок «Campaign» — нарратив
  отдельно с пометкой «authored text, not a measurement», затем таблицы LoopLab. В ране —
  панель «Protocol & comparisons» (Analysis): эксперименты, кнопка расчёта контрастов, журнал
  поправок с diff.

## 7. Составные операции MCP

| Инструмент | Что делает за один вызов |
|---|---|
| `submit_experiment` | progress → (опц.) предрегистрация → (опц.) вопрос исследования (`hypothesis_added`, `source=external_agent`) и `idea.parent_card_id` → (опц.) memo → требуемые решения (`harness-decisions`) → `inject_node`. `dry_run` перечисляет недостающее. |
| `finish_run` | progress (гейты финиша) → (опц.) `report_generated` → (опц.) `harness-reviews` → `run_abort` с причиной. |
| `retry_node` | чтение узла → `node_reset` его текущего поколения. |
| `amend_protocol`, `run_protocol` | запись/чтение журнала протокола. |
| `upload_per_query`, `compare_experiment`, `shared_test_leaderboard` | загрузка массивов и чтение расчётов LoopLab. |
| `campaign_supertask`, `campaign_report`, `publish_campaign_narrative` | группировка ранов и отчёт кампании. |

Каждый шаг идёт через существующие маршруты (та же валидация и авторство), ключи шагов выводятся
из `key`; повтор с тем же `key` воспроизводит выполненные шаги их исходными квитанциями.

## 8. Чего здесь нет

- Привязки узла к версии протокола в свёртке (`Node.protocol_version`) и запрета сравнивать узлы
  разных версий: версия видна по `at_event_seq`, решение — за читателем.
- Поправки содержимого data-mount каталогов (только их объявление в задаче).
- Отдельного события в `events.jsonl` для поправки: журнал — sidecar; движок фиксирует
  изменения задачи и рабочего дерева своими `task_changed` / `workspace_changed`.
- Межрановой очереди GPU и пробуждения агента — это doc 78 (оркестрация).
