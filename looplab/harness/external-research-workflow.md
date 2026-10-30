# External research workflow

This is the canonical process for an external LoopLab researcher. The live run's contract, task, source and budget gates take precedence over examples here. This guide is not authority to launch, retry, publish, or change shared knowledge.

## Start and recovery

Connect using the supported harness, check `capabilities` and `connection_check`, then read current state, task, config, contract and `run_progress`. Verify run UID and generation, source health, pending questions, current operator directions and exact receipts. A handoff or notification describes a past observation, not a permission. On reconnect inspect existing command receipts, attempts and current status before choosing an action; never rerun a completed experiment merely because an older handoff mentions it. Use `phases` and `phase_info` for the action and `wait_for_next` for events. Routine monitoring rules do not authorize an abort.

The operator sets the goal, constraints and delegated authority. A single external agent may perform all delegated research and execution autonomously; a second owner or confirmation is not required unless the actual task or contract says so. The external researcher formulates questions, evaluates prior evidence and makes explicit proposals. The executor prepares and submits code through supported actions and checks actual receipts; LoopLab owns evaluation, budget and lease enforcement. A person may hold more than one role. A notification is not an admission, and no action is acceptable when evidence or authority is missing.

## Execution discipline

Use the operator's current delegated authority throughout the campaign. Do not invent a second
owner's approval step for an already authorized launch, retry, publication or budget amendment.
Missing authority is a real blocker; an optional collaborator review is not. A notification itself
still grants no authority, and the actual source, protocol, budget and resource gates still apply.

- Prepare at least one authorized next experiment while the current evaluation runs: reuse code
  and data, perform the smallest meaningful checks, register its question and comparison, and
  submit through the supported queue when dependencies permit. Prepare saved-model scoring
  before training ends; bind the genuine producer artifact after completion. Never invent a
  checkpoint, duplicate an accepted injection, or bypass a dependency to make a queue look full.
- Before an expensive fit or inference run, measure representative throughput with a bounded
  profile under the real resource gates. Consider batching, padding, precision, model heads,
  decoding and checkpoint I/O. Preserve causal inputs, target coverage and correct global loss
  normalization. A smaller update count is not a measured speedup. Disclose changes to batch,
  optimizer schedule or objective rather than calling them equivalent optimizations.
- Preserve the requested data, tasks and training scope. Do not silently substitute a subset to
  meet an estimate or budget. State an actual resource shortfall promptly; when budget changes
  are already delegated, apply a justified recorded amendment instead of waiting for permission
  again. Preserve prior spend, failed attempts and unknown costs. A large budget is not a target
  to consume; each evaluation still needs a finite resource envelope.
- Reuse completed fits, saved controls, rankings and accepted audit evidence when their identity
  matches the new question. Assign one owner to each launch, export and audit. Do not repeat a
  full payload scan or test suite after a small metadata correction. Necessary artifact reads for
  a genuinely new diagnostic remain allowed. Validate the changed boundary using the actual
  library path on a small fixture; check training, evaluation, reload and distributed weighting
  where that boundary changes, rather than rebuilding unrelated historical evidence.
- Keep preparation proportional to its decision. Optional archaeology, diagnostics, figures and
  collaborator replies must not idle a ready authorized job. Required native report cadence and
  scientific gates remain obligations. When no job is ready, name the exact remaining blocker,
  its owner and the next supported action, then continue independent preparation.
- Act on current receipts and event-driven notifications rather than waiting for a supervisor's
  periodic review. Reconcile delayed handoffs before acting. Keep a compatible campaign in one
  run with recorded amendments; use separate runs for distinct topics or incompatible protocols,
  never to reset spend or evade comparisons. Do not add seeds, repeats or speculative jobs just
  because resources are available.

These are execution instructions, not a new validation stage or a guarantee of utilization.
Report actual active work, resource use and blockers; never claim that allocating several devices
proves the implementation uses them efficiently. Keep the cumulative overview current, retain
negative results, and use the operator's requested language and readable scientific labels.

## Research cycle

1. State the goal and a decision-relevant question; distinguish scientific work from engineering recovery.
2. Review existing run evidence and relevant primary sources or knowledge. Link each source to the specific supported claim and explain differences from the current setup. State uncertainty and contradictions; do not invent citations.
3. Form a falsifiable hypothesis and feasible alternatives. Explain novelty against actual prior attempts. An operator-requested or technical check can honestly have no applicable novelty or literature review.
4. Register experiment, control, data, metric, test and comparison before its relevant result. Link an experiment to a question card where appropriate. Keep source, model producer, code reference, control and attempt as distinct relationships.
5. Prepare inputs and code, record blockers and owner, and use existing source, budget, GPU gate and receipt mechanisms. Follow the actual scope and authority; readiness does not itself grant execution.
6. Inspect genuine output, provenance and comparison, then distinguish measured observation from causal interpretation. Preserve failed, deferred and incomplete attempts. Report missing metrics as unavailable, never zero.
7. Curate evidence-backed claims and local lessons; explain a no-action decision separately for claims, concepts, lessons and skills. Shared KB changes require their own authorization.
8. Update a cumulative report covering major directions and limitations, plus the latest experiment result and references to historical versions. Proposals not tested belong in next directions. Choose the next step only from current evidence and operator priorities.

## Examples

A useful research memo: “Does a wider decoder recover more unseen items on the same weights and query cohort? Prior beam evaluation measured X on this test; the cited decoder paper uses a different candidate space. A fixed-width paired comparison tests search depth, not training quality. The effect may be negative; the next registered comparison uses the existing checkpoint and declares its metric and correction.” The paper and the prior result should be actual references, and X must be an actual observation.

A weak memo: “GPU is available; prepare beam.” It is a valid operational update but does not explain the scientific decision or evidence. Do not fabricate a literature review merely to fill a field.

A useful score-only card identifies the saved producer and attempt, test and policy, control, metric, and that no model training occurs. An engineering retry inherits its existing task context; it does not mint a fake scientific hypothesis or a result for the failed attempt.

## Installation and compatibility

Copy `skills/looplab-research/` to an agent's configured skills directory, or package it with the repository's agent integration. The skill reads this canonical guide from the connected server; the installed skill does not require a local source checkout. Restart the agent if its skill catalogue requires reload. Check the installed skill's frontmatter and the live server contract; neither a copied guide nor this skill pins a run identity, budget, model or schedule. Existing runs and event history need no migration.

## Research map

`GET /api/runs/{run_id}/research-map?expected_generation=...` and MCP `research_map` expose registered questions, arm references and report history in bounded pages. The CLI `looplab research-map RUN_DIR --generation TOKEN` uses the same projection. A registration is not a measured result or a permission; protocol and event sources are separate reads.

## Notifications and optional handoffs

`wait_for_next` and configured named agent-notify hooks deliver existing events, coalescing and suppressing stale conditions. An event contains a suggested owner and next read, but is not authority to act. When a collaboration needs a handoff receipt, `POST /api/runs/{run_id}/agent-event-ack` or MCP `acknowledge_agent_event` records an idempotent, generation-bound self-attestation. Delivery exit status is not an acknowledgement; a receipt does not resolve an underlying checkpoint or prove a person acted. The server credential is not a recipient identity, so it does not independently authenticate the named recipient. The action remains subject to the existing contract. A single external agent need not involve a second owner.

## Current action context and export

Read `GET /api/runs/{run_id}/research-briefing?expected_generation=...` or MCP `research_briefing` for the current next step, operator directions, obligations, named sources and observed GPU state. A direction is not itself an admission; a completed run does not imply a resume. `looplab research-map RUN_DIR --generation TOKEN --briefing` uses the same model. Independent source reads can change during observation; refresh when their event prefixes differ.

`GET /api/runs/{run_id}/research-export` accepts up to ten `report_seq` values and returns bounded Markdown, a SHA-256 and a report/figure reference manifest. It does not copy model weights, rankings, or publish to an external service. It does not prove durable-storage flush.

Named hooks can receive an optional addressed handoff through `POST /api/runs/{run_id}/agent-handoff` or MCP `send_agent_handoff`. The stable `action_id`, run generation and recipient are recorded in the existing event outbox; an exact retry returns the original event. The `reason` and `next_read` are authored observations, not independently verified readiness or approval. Configure the recipient hook before relying on delivery; absence of a hook does not itself block an autonomous executor. Old hook configurations with explicit kind filters must opt into `agent_handoff` and `operator_direction`.

## CPU observation

`benchmarks/research_workflow_outbox.py` measures 300 individual outbox inserts and one bounded read in a temporary directory. A single local run on 29 October 2026 measured 340 ms before this change, 543 ms with a full outbox scan, and 416 ms after a bounded-tail recovery check. These are one-run observations, not a throughput guarantee; the current variant remains slower than that baseline in this measurement. No GPU or live run is involved.

Experiment declarations may include `relationships`: each entry has `kind` (`model_producer`, `scientific_control`, or `code_reference`), `subject_arm`, `node_id`, and optional `run_id`, `node_generation`, `artifact_sha256`, and `explanation`. These are explicit authored references, not independently validated artifact identities. Existing experiments without them stay valid. They are part of the immutable registered declaration: a different reference requires a new declaration rather than silently changing a reviewed one. Mechanical late binding to an as-yet-unknown artifact is not implemented by this field; an unchanged scientific scope still requires its own verified binding workflow before execution.

## Late producer binding

Register a `model_producer` relationship with the planned producer node, without inventing a checkpoint. Once that exact attempt is evaluated, `readiness-bindings` can bind a small manifest in its own node directory to the unchanged experiment `scope_sha256` from `research-map`. The fenced reader checks run generation, producer attempt, terminal state and SHA-256 of at most 1 MiB of manifest bytes. The receipt is append-only and exact retries reuse `action_id`. A read shows whether scope and attempt remain current, while manifest freshness is explicitly not rechecked. Optionally supply `artifact_path` and `artifact_sha256` together to stream and verify the producer-node artifact itself (up to 16 GiB); the hash runs outside the command sequencer and scope/attempt are rechecked before the receipt. Both hashes describe what was read at that instant; later freshness is not rechecked. It neither queues work nor bypasses source, budget or GPU gates. After a producer completes, the normal terminal event wakes the executor, who can bind and proceed under already granted scope; no new Root confirmation is implied.

## Cumulative campaign report

A `report_generated` content may include `campaign_overview` with `summary`, `recommendation`, `recommendation_limits`, `what_worked`, `what_didnt`, `learnings`, `next_directions`, `caveats`, and `report_seqs` (event sequence references). Publish this as a current evidence-backed synthesis; the ordinary report body remains the latest local result. The research map presents the most recent published overview even when a later result omits it, with the original publication sequence and node watermark. An older overview can be stale and is not automatically updated; read its provenance and refresh it after meaningful results. Versions remain in the existing journal, and missing overview is stated explicitly. The export includes the latest overview and selected historical results.

The report's research map offers a guided memo draft and an advisory review. The draft can publish through the ordinary `research_completed` command; technical recovery is allowed to omit scientific literature. The preview checks presence and explicit links only, never the truth of a claim. A command response may be pending or unknown; inspect its receipt and reuse the same exact intent rather than inventing another result.

The research map also shows memo claims, their recorded source and experiment links, and submitted novelty/foresight/strategy reasons. Recorded verifier labels and URL links are evidence pointers, not proof of a scientific claim. The decision journal has its own health receipt; incomplete history must not be treated as an empty history.

A research memo may include `local_lessons` entries with `statement`, `limitation`, `node_ids` and an authored `status` (`open`, `limited`, `supported`, `refuted`, or `verified`). These remain in the run's event journal; writing them does not change shared knowledge. The status is the author's assertion, not an automatic verification or a causal conclusion. Use actual node evidence and limitations, and inspect source receipts before adopting the lesson.

### Mirror observations

The server briefing includes `mirror.state`, the last completed pass age, current stuck/error flags,
and the supervisor state and restart count. `degraded` takes precedence over a running supervisor;
`stale` means the last recorded completed pass is more than 180 seconds old. `unknown` means no
readable completed-pass evidence and `unconfigured` means no mirror destination. `observed_recent`
is only a source-side observation. None of these states verifies a remote storage flush. The local
CLI cannot inspect the server supervisor and reports the mirror as unavailable; use the server API
for its observation. Do not restart healthy research or create a second copier from this diagnostic.

Report `key_metrics` remain references, never authored numbers. A metric reference may carry a
`role` (`primary`, `secondary`, `transport`) and author-declared `test_id`, `cohort`, and `policy`.
A node reference may carry `node_generation` (the attempt number). These labels do not verify
identity of query arrays, ground truth, or policies. When a referenced attempt differs from the
currently folded attempt, the UI shows it as unavailable rather than substituting the newer value.
For paired inference, use the actual upload receipts and native comparison; a difference between
referenced scalars is not evidence of a matched comparison.

### Notification recovery

Successful hook delivery is not an ACK. The server keeps reaction identity metadata (not
message text, argv, or credentials) and recovers a pending reminder from retained outbox rows after
restart. A persisted explicit ACK suppresses the reminder. If the outbox rotated away or the rendered
hook changed, LoopLab keeps the pending entry and does not guess a recipient; inspect
`GET /api/agent-notify` and the outbox. A crash between executing the external hook and recording its
receipt may still produce at-least-once delivery, so consumers should deduplicate by event identity.
Neither delivery nor acknowledgement authorizes an execution or proves that the action is finished.
Before a reminder, demonstrably stale events and already acknowledged handoffs are removed from the
batch. Remaining events retain their original recipient, provided the hook can be reconstructed
exactly. Re-read each event and current progress before acting.
For an explicitly addressed `agent_handoff`, an unrelated read of the run does not count as its ACK;
the recipient records the generation-bound event acknowledgement. Routine legacy notifications may
still use observed agent activity as their reaction signal.

An exact retry of a readiness-binding action returns its original receipt even after the producer
attempt changes. That receipt is historical: inspect the current readiness snapshot and recheck
manifest/source/protocol/budget/lease before any action. Changing the same action ID's request is
rejected.

The research map lists per-query upload metadata, attempt staleness and native measured-value
consistency. A `matches_measured` result refers only to the upload metric and node metric named in
that receipt, including `extra_metrics` when selected. The shared test ID identifies query and cluster
labels; it does not independently attest ground-truth content, cohort construction or scoring policy.
Use the declared protocol and upload provenance alongside the native paired comparison.

### Project metric presentation profile

An owner may configure a project with `GET /api/projects/{project_id}/metric-profile` followed by
`PUT` of `{expected_digest, profile}`. A profile has a name, one primary metric, secondary/transport
metrics (each with key, label, unit and denominator), requested slice names and notes. The expected
digest is a compare-and-swap fence; a stale edit returns 409. For example, a retrieval project can
name ProductRecall@200 and FUR@200 with repeat/explore and popularity slices; an image segmentation
project can instead name IoU and small-object slices. Neither example is a global default.

The associated run briefing exposes the current project profile to the external agent and UI. It is
an independently read presentation preference, not a metric value, protocol amendment, permission,
or proof that a requested slice is available. Actual comparisons still use declared tests and native
per-query data. Project profile changes do not rewrite old run journals or scientific registrations.
If `reaction_pending_readable` is false, pending delivery state is unavailable, not empty. The pump
refuses new hook delivery until the owner repairs that ledger; it does not overwrite damaged bytes.

A new experiment registration may set `question_card_id` to an existing direction card. The server
checks the reference at registration; no question card is invented from prose. The map displays the
linked card alongside the registered question. Old registrations remain readable without this field;
late editorial filing does not rewrite their scientific protocol or fabricate preregistration.

Read command receipts after authoring and address `intake_notices`: omitted or shortened content,
readability problems and questions not filed on the board are distinct observations. The read-only
receipt retains bounded notice categories and field names after reconnect; it omits authored samples
and does not restart a worker. A notice is advisory, not a universal gate.
Exports include selected result text, metric references, report content digests and figure SHA-256
references. A request whose complete result exceeds the bounded response limit returns 413; select
fewer report versions instead of accepting a silently truncated report.

A registered arm may optionally declare `work_kind` as `scientific_experiment`, `fit`, `score_only`,
or `engineering_recovery`. The map displays this as an author declaration, not an inferred model
classification. Old arms without it remain unspecified. A native retry keeps the same node and its
registered context; it does not need a new question just to recover a technical attempt.
An addressed event's acknowledgement must name its recorded recipient; the label is still
self-attested unless an external identity system binds it to a principal. It is delivery tracking,
not authentication, scientific approval, checkpoint resolution or a Root confirmation step.

A new registration can use `research_context` for `change`, `motivation`, `data_weights`,
`control`, `criterion` and `alternatives`. These are author-supplied preregistration text, included
in the scientific scope and displayed on the map. Record observations and limitations after the
result in the existing report/memo; do not invent a result in advance. A technical retry may omit
this research context when it is not applicable.

The map gives advisory clarity suggestions for generic titles, missing question/control/criterion or
motivation, and suspiciously glued text. A declared technical recovery is not asked to invent a
scientific question or a literature review. These suggestions are not admission gates, proof of
quality, or permission to modify a running experiment. Author meaningful explanations in the run's
configured output language; LoopLab does not silently translate or invent the scientific rationale.

Novelty, foresight, ranking and strategy decisions may include `prior_node_ids`,
`alternatives_summary`, `information_gain` and `not_applicable_reason`. Prior IDs must name existing
run nodes; their inclusion and the explanatory text are author assertions, not independent review.
They preserve why another run is informative or unnecessary. The native phase and admission rules
remain unchanged, and a technical recovery may explain why novelty is not applicable.

Concepts use the existing tagged membership, graph, lenses and governed alias mechanisms. Choose
stable meaningful names and explain a term in the relevant research memo or rationale. Useful axes
include data/cohort, scale, objective, architecture, input, decoding and evaluation; these are
examples, not mandatory taxonomies. Do not turn every numeric parameter into a separate concept.
Lens coverage describes the eligible recorded subset, not truth or completeness of the campaign.
A missing relationship graph is displayed as such; co-occurrence is not a causal edge. Shared
aliases or merges follow existing permissions and are never silently applied by the map.

The guided authoring form keeps technical status separate: it writes an attempt-fenced native
`comment_created` on the relevant node. A scientific memo uses `research_completed`; neither path
requires fabricated literature for technical recovery. After a lost response, the form retains only
the command key in tab storage and reads its receipt; it does not persist authored text or silently
resubmit a different draft.

#### Оценка утверждений автором

В исследовательской заметке можно указать статус утверждения (открыто, ограничено, опровергнуто, поддержано или проверено), его ограничение и ссылку на соответствующий контраст. Это оценка автора, а не независимая проверка истинности: связывайте её с конкретными источниками и результатами, указывайте дизайн и неопределённость. Проверки claims, concepts, lessons и skill-кандидатов остаются отдельными; локальный урок сам по себе не меняет общую базу знаний.

Для отдельной страницы направления передайте `question_card_id` в `research-export` или выберите «Выгрузить направление» на карточке. Экспорт включает только регистрации с явной ссылкой на эту карточку, до 100 записей, и отмечает наличие остальных. Версии отчётов добавляются только по явно выбранным `report_seq`; их связь с направлением не предполагается. Публикация во внешнюю систему не происходит автоматически.

Профиль метрик проекта задаёт ключи, подписи и заявленные знаменатели. `profile-measurements` и кнопка карты показывают фактически записанные значения `extra_metrics` по попыткам, происхождение и N/A для отсутствующих ключей; каждая таблица и график подписаны. Разрезы (например repeat/explore или popularity) выводятся лишь если их реальные ключи включены в профиль и значения записаны; сервер их не вычисляет и не подменяет нулями. Профиль не доказывает совпадение теста, GT и политики. Для статистического контраста используйте native paired comparison, его CI и оговорки.

Исторический или текущий отчёт с `key_metrics.metrics[].test_id` можно проверить через `GET /api/runs/{id}/report/versions/{seq}/metric-bindings?expected_generation=…`. Сопоставляются node, указанный test и ключ метрики; явно записанный `measured_check` может связывать другое имя массива с ключом node. При старой/неизвестной попытке проверка объявляется `unchecked`. Совпадение числа не доказывает одинаковую разметку, когорту или политику; отсутствие загрузки не означает нулевое значение. Доступна только метаинформация, массивы повторно не читаются.

Покрытие вопросами исключает только явно отмеченные `engineering_recovery`; неклассифицированные ноды остаются в знаменателе как неизвестные. Число ссылок на источники не является проверкой содержания. Исторический статус устранения intake notices автоматически не выводится: проверяйте исходную квитанцию и текущие данные; ни одно предупреждение не создаёт gate.

Поиск понятий учитывает действующие записи переименования из существующего `concept_consolidation`: старое имя находит каноническое понятие и связанные с ним опыты. Неверные или циклические ссылки не используются. Это не создаёт новую таксономию и не меняет общую базу знаний; определения остаются в обосновании опыта или исследовательской заметке.

В проектном профиле `authoring_language` задаёт предпочтительный язык нового авторского текста (`ru`, `en`, `auto`); briefing сообщает действующее значение. Явный `output_language` конфигурации run имеет приоритет, а при `auto` применяется предпочтение проекта. Изменение профиля не переводит старые записи и не изменяет конфигурацию движка; сохраняйте исходные имена, значения метрик и provenance.

Для уже записанных суммируемых вкладов можно задать одинаковый `stack_group` у ключей профиля, например у repeat и explore. Составной график строится только для неотрицательных присутствующих значений с одинаковыми явно заданными знаменателем и единицами; пропущенные строки исключаются, а таблица показывает N/A. Аддитивность заявляет автор профиля: LoopLab не доказывает её и не вычисляет сами метрики.

Briefing показывает зарегистрированные зависимости от producer: ожидает producer/привязку, актуальная привязка или устаревшая. Межзапусковая ссылка показана как непроверенная, а не как локальный producer с совпавшим номером. Эти состояния относятся только к наблюдаемой привязке, не доказывают готовность к GPU и не заменяют обязательные source, protocol, budget и lease проверки.

`research-notices` постранично показывает категории предупреждений из сохранённых квитанций команд, включая повтор без изменений; поле `unreviewed` не останавливает работу. Опциональный POST фиксирует `reviewed`, `resolved` или `unreviewed` с `notice_id`, исходным `command_id`, `expected_generation`, уникальным `action_id` и причиной. Это утверждение автора, а не автоматическая проверка исправления. Повтор того же action возвращает исходную квитанцию. Старые недоступные и только временно показанные предупреждения невозможно восстановить: отсутствие в этом списке не доказывает полную историю.

Проект может явно настроить единственный встроенный publication adapter `manual_markdown` и описательное имя назначения в профиле. Экспорт включает это указание и контрольную сумму содержимого; скачивание и последующая ручная публикация выполняются пользователем с обычными разрешениями целевого сервиса. LoopLab не принимает URL или credentials в этом поле и не отправляет данные в произвольные внешние сервисы. Автоматического Confluence adapter здесь нет.

Briefing также включает последнюю опубликованную общую сводку и до пяти последних нод с записанными ключами проектного профиля, если профиль настроен. Каждое наблюдение связано с поколением и номером события; `same_profile_as_briefing` и `same_event_prefix` показывают расхождение независимых чтений. Для полного контекста используйте страницы карты и отчётов, для действий — свежие квитанции и native gates.

## Рубрика накопленного научного обзора

После существенного результата fit или score и перед итоговым отчётом прочитайте свежий
`research-briefing.report_context`, предыдущий обзор целиком по `previous_overview_api`, историю
отчётов и подходящие сравнения. Самостоятельно обновите `campaign_overview`; свежий отдельный
`body_markdown` может подробно описывать последний эксперимент. Телеметрия шага, скорости и потерь
нужна для состояния обучения, но не заменяет научный обзор и измерения качества. Не переписывайте
историю и не опускайте значимые отрицательные или неоднозначные результаты.

Для каждого важного направления расскажите связно: **какой вопрос**, **что служило контролем**,
**какой результат измерен** (тест, метрика, значение, единица и знаменатель), **что он означает**,
**каковы ограничения** и **какое решение следует сейчас**. Дайте ссылку на конкретные node,
реальное сравнение/contrast или report version рядом с соответствующим утверждением; не связывайте
автоматически доказательства, если их соответствие не установлено. `campaign_overview.report_seqs`
содержит event sequence событий `report_generated`, а не номера нод. Если результатов нескольких
важных направлений нет в отдельной истории отчётов, укажите их настоящие node/contrast ссылки в
обзоре и не выдумывайте report sequence. `same_protocol`, consistency и интервал доверия описывайте
по фактическому evidence; одинаковый query ID не доказывает одинаковые GT или policy.

Рекомендация должна сказать, **какой измеренный вариант выбрать или почему выбор пока невозможен**,
для какой задачи и при каких ограничениях. Если изменений несколько, не приписывайте эффект одной
причине. Для следующего опыта сформулируйте вопрос, контроль, ожидаемые различающие исходы и то,
какое решение каждый исход изменит. Если ресурсов или полномочий нет, опишите предложение и не
запускайте его. Если данных недостаточно, честно обозначьте неизвестное, а не заполняйте шаблон.

Пример хорошего текста (условные числа для объяснения, не факт о каком-либо run): «На одном тесте
контекстный вариант улучшил Recall с 40% до 42% по 100 пользователям; paired Δ +2 п.п., 95% ДИ
[−1; +5] п.п. В пользу внедрения пока недостаточно доказательств: интервал включает ноль, один
seed. Следующий вопрос: сохраняется ли эффект на редких товарах? Контроль — те же пользователи и
правила; положительный эффект поддержит проверку этого направления, нулевой — приоритет более
простого варианта. Ссылки: node 3, node 4 и зарегистрированный contrast C1». Плохой текст:
«Использовать paired results; дальше проверить occupancy». Он не сообщает результат, область
применения, ограничение или решение, которое изменится от следующего измерения.

Подписи графиков пишите на языке отчёта и объясняйте модель/сравнение, метрику, тест и знаменатель;
избегайте имён файлов и внутренних ID как единственной подписи. Пример: «Recall@20 для редких
товаров, среднее по пользователям с непустой группой; контроль и вариант». Не переводите имена
метрик, команды, идентификаторы и исходные свидетельства. Перед публикацией самостоятельно
проверьте, что выводы согласованы с таблицами и отрицательными эффектами, числа и единицы не
перепутаны, ссылки действительно ведут к соответствующим записям, а непроверенные утверждения
помечены. Детерминированные предупреждения проверяют только наличие разделов, события после обзора
и существование report event refs; они не оценивают истинность, полноту научного рассуждения или
качество рекомендаций и не создают нового GPU gate.

При доставке framework может не будить агента исторической паузой, если полный native journal
доказывает последующий `resume`/`run_reopened`, а точная command receipt связывает эту паузу
с тем же run generation и записанной атрибуцией внешнему агенту. На anonymous UI такая атрибуция не является криптографической аутентификацией отправителя. Запись остаётся в outbox и event history;
паузы без агентской атрибуции, указания пользователя (`operator_direction`), открытые checkpoints, результаты,
ошибки и адресованные handoffs сохраняются. При неясной идентичности или неполном источнике
уведомление сохраняется. Эта проверка не может отозвать сообщение, уже переданное внешней очереди:
даже старое уведомление сначала сверяйте с текущими `run_progress` и receipt, а не выполняйте его
как актуальную команду. `delivered` и наблюдаемая активность сами по себе не доказывают чтение
конкретного сообщения.
