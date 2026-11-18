# Простой при работе с внешним агентом: движок, рецепт и организация

Дата проверки: 17 ноября 2026. Проверен **настоящий remote master
`7c45d5d9f04533751e2b04a05d3c4fdbb70fe7cb`**, а не копия живого сервиса.
Изолированная серия до `5a1e6ef699b6038702e67ea9bde23e2ff2e83216` рассмотрена только
как **неопубликованная реализация на момент исходного аудита**. Последующее прямое поручение USER разрешило source-интеграцию; см. addendum ниже. Она не считается
развёрнутой. Никакой live service, ML-процесс, hook, lease или scientific protocol не изменён.

## 1. Что произошло и что числа не доказывают

В наблюдавшейся кампании от поручения до следующего большого fit прошло17ч12м.
Native attempts занимают3ч42м, включая CPU/I/O и ожидание монитора.13ч30м вне attempts
включают необходимую разработку и предотвратимые задержки: это **не измеренные13ч30м
чистого GPU-простоя**. Долларовая стоимость и remote S3 flush не установлены.

Доказательства: native event/checkpoint receipts, видимые операционные сообщения,
исходный код и конфигурации, elapsed CPU build, bounded CPU probes. В аудит не включены
credentials, hidden reasoning, персональные raw-данные, веса или повторные ML-аудиты.
Минимальное возможное время всех обязательных checks и доля provider latency не измерены.

## 2. Классификация всех двенадцати наблюдений

| Наблюдение | Ответственность и граница движка | Действие |
|---|---|---|
| F1. Агент119 мин считал себя другим исполнителем и передавал работу себе | Ошибка роли/координации. Движок не аттестует физическую Codex-сессию из её текста. | Проверить собственную session identity и actual run identity; существующий пункт `external-work-state-has-no-authored-checkpoint` покрывает recovery context. Не новый Root approval. |
| F2. «4часа сборки» при233с самой долгой parallel shard и≈15мин позднего обнаружения | Ошибка отчётности wall time против CPU time и запоздалая проверка готовности. | Хранить build start/end и отдельное ready→admit ожидание. Нового engine bug из этого не следует. |
| F3. Новая fit-нода исполнила прежнюю A0 scoring-команду | Исполнитель подал её при открытой очереди до привязки. Task evaluator глобален; ожидаемая evaluator identity кандидата не fenced. | Новый ограниченный пункт §6.1; существующие hold/stopped/CAS guards сохраняются. |
| F4. Canary PASS, настоящий initializer сразу отвергает documents_root | Прикладной canary выполнял no-op branch. Движок корректно запускает тот же chain с LOOPLAB_CANARY, но не знает смысл validation. | Настоящий config validator до CUDA. Общая semantic acceptance уже в `evaluation-faithfulness-not-attested`; новый дубликат не нужен. |
| F5. Два scoring input build107с/337с, но launch preparation73мин/65мин | Ручная сериализация wrappers/config/grants и отвлечение на helper. Inputs READY не равен полному executable package. | Готовить runner, inputs, metrics и протокол во время fit; late-bind только фактический producer/attempt. Не убирать scientific/source guards. |
| F6. Paused принят за stopped | Контракт уже различает оба состояния и правильно отказывает task/files amendment при работающем engine. | Читать execution.engine_running и pause receipt engine_stopped; optional idle-drain UX не доказанная поломка guard. |
| F7. Успешный scorer закончил команду, terminal ждал advisory36мин | Подтверждённое поведение движка плюс просроченный ответ агента. | Известный неопубликованный fix §6.3; до внедрения отвечать на actual вопросы, а не объявлять процесс освобождённым. |
| F8. Publication asymmetry → NCCL600с | В candidate-owned publisher были unbounded open/hash; исходная backend/path/syscall причина не установлена. | Finite operation deadline и точный phase receipt в прикладном publisher. Не объявлять S3 виновным, не увеличивать NCCL timeout как лечение. |
| F9. Retry потерял receipts внутри rematerialized workdir | Приложение сослалось на disposable output. Материализатор очищает workdir по задокументированному контракту. | Новый input-preflight §6.2, durable references вне disposable дерева. Не сохранять произвольный мусор автоматически. |
| F10. READY модулей принят за READY всего epoch/resume/export CLI | Ошибка организации; новый runner действительно требовал реализации. Native API ограничения повторно выясняли поздно. | Отдельные authored evidence для data/CLI/resume/publisher/profile/binding/admission; существующий work-state пункт. |
| F11. Старые notifications и цепочки мелких вызовов отвлекали от next action | Coalescing, stale-condition checks и handoff ACK **уже существуют**. Terminal facts сохраняются намеренно; принятую внешней queue строку движок отозвать не может. | Сверять current progress, продолжать незавершённое поручение после истории. Не создавать второй client/watchdog. |
| F12. Путь в каталоге control34 содержал inherited config2/g1 | Ошибка научной ссылки Root, обнаруженная до нового score. Каталог не аттестует producer. | Проверять содержимое actual native config/producer; расширяет пример existing evidence/faithfulness acceptance. Повторный control score не нужен. |

Это не двенадцать дефектов движка. Ответственность за роль, очередность действий,
неполную подготовку и позднее обнаружение остаётся у ведущего и исполнителя.

## 3. Что уже исправлено, но не попало в remote master

Предыдущая серия существует в отдельном checkout. Сравнение её кода/acceptance не делает
её безопасно развёрнутой и не доказывает product utility. Нельзя снова придумывать те же fixes.

| Механизм | Неопубликованный commit | Реальный остаток |
|---|---|---|
| Completed advisory monitor не держит finished evaluator | `ab76e01` | Review и unanswered evidence сохраняются;16 focused CPU tests повторно PASS. В master старый barrier. |
| Authored external checkpoint/work state | `e1ace13` | Заявленный owner не равен verified физической session identity. |
| Work intents и явные direction ACK | `622e9b4` | Доставка/ACK не доказывают понимание или выполнение. |
| Scoped direction retirement | `b6716fa` | Historical facts не стираются. |
| Bounded bootstrap/projection | `90fa02f`, `a0aaa02`, `3bc8607` | Bounded response не обещает bounded fold CPU/RAM. |
| Repair physical slot yield | `4d85007` | Относится к internal repair lane, не объясняет все внешние preparation gaps. |
| Ready consumer до cadence | `053922b` | Не разрешает строить/исполнять незарегистрированные arms. |
| Native external revision/usage telemetry | `d4773b6` | Genuine billing/provider coverage и улучшение исследований всё ещё требуют измерения. |
| Local durable worker/cluster abstraction | `5a1e6ef6` | Настоящая remote platform acceptance остаётся UNKNOWN; CPU worker не доказательство Slurm/облачного исполнения. |

Публикационный gap важен: текущий аудит не сливает крупную серию автоматически и не
меняет healthy runtime. Её отдельное ревью/интеграция и coordinated idle deployment
являются самостоятельной работой. Это не gate для текущего научного fit.

## 4. Быстрые меры без изменения живого движка

1. Сохранять один конкретный next action и actual role/run identity. Не ждать собственного ACK;
   status-only ответ не завершает незавершённое поручение.
2. Перед paid attempt запускать **настоящий** config/init contract validator на CPU. Canary
   должен проверять тот seam, который исполняется в full command; механический rc0 не означает
   scientific correctness.
3. Во время fit готовить весь следующий executable path: inputs, command, validation, resume,
   publisher, metric driver, finite budgets. Явно разделять prepared/bound/admitted/running/complete.
4. Проверять pending checkpoints перед CPU-отчётом и следующим запуском. На проверенном master
   successful evaluator может ждать ответа: не объявлять lease свободной по одному command exit.
5. До retry проверять declared resume inputs и их durable references **вне** дерева,
   которое materializer пересоздаёт. Соблюдать integrity, selected/pinned exports и exact attempt.
6. Собирать независимые reads в один пакет, сохранять exact command/action IDs и readback.
   Timeout POST не означает отказ: не повторять blind POST и не рестартовать сервис.

У этих мер нет заявленного количества сэкономленных часов: effect следует измерять
по ready→admit, command-exit→terminal и GPU occupancy отдельно, с сохранением затрат и failures.

## 5. Какие инструкции проверены

`AGENTS.md` различает driver и contributor, предупреждает, что pause не доказывает остановку
lease, и требует ответы monitor. Последнее соответствует **этому master**, хотя именно этот
старый completion barrier предлагается изменить. Нельзя исправить его одной фразой инструкции:
до source change terminal/release по-прежнему надо проверять по actual native состоянию.
`CLAUDE.md` задаёт source invariants и OPEN falsifiers; новый Root approval там не требуется.
Краткая ссылка из driver guide ведёт сюда, подробная история не добавлена к каждому запуску.

## 6. Ограниченные дополнительные OPEN

### 6.1. Ожидаемая evaluator identity кандидата

*Закрыто 2026-11-18, ветка `ang_review_2026_11_17`: канонический `evaluator_digest` (`looplab/core/evaluator_identity.py`, `looplab/engine/shared.py::run_dir_evaluator_digest`) хэширует эффективный `eval_spec`, защищённые базовые файлы в `editable_path`, маунты `data`/`references`, `data_content` из `protocol_amendments.jsonl` и task `assets`. Он публикуется как `current_evaluator_digest` в `harness-progress` (включая `brief=true`), принимается как опциональный `expected_evaluator_digest` в `submit_experiment` и `inject_node` (отсутствие поля явно маркируется `evaluator_binding="unpinned_legacy"`), проверяется fail-closed (`409 stale_evaluator_digest`) на admission с сохранением идемпотентного реплея `key:inject` и повторно проверяется перед резервированием GPU и в `_eval_admit`/`_eval_run_attempt` (`EvaluationAdmissionRefusal("stale_evaluator_digest")` при `eval_seconds=0.0` + `pause`). Тесты: `tests/test_evaluator_identity_and_canary_reuse.py`.*

Независимое ревью 18 ноября: namespace каждого editable, glob- и directory-защита учитываются при хэшировании. Нечитаемый, слишком большой (>16 MiB) защищённый файл или неполный обход (>4096 members) не получают переиспользуемую identity. Проверки: `tests/test_antigravity_review_regressions.py`.

Решение: authored expected resolved command/protocol digest с exact source и attempt. Проверять
на admission и повторно перед dispatch **до** lease/model work, при несовпадении fail closed.
Это не право кандидату менять protected scorer. Existing hold, stopped amendment, source,
budget, role и authority checks не ослабляются; legacy отсутствие поля явно маркируется.
Приёмка: matching success, stale task refusal, race amendment между admission/dispatch,
exact idempotent retry, source/env/protocol identity coverage, никакого автоматического launch.
Absence выбранного символа — временный structural falsifier, не behavioral proof: закрывать
по настоящим negative/positive cases, а не добавлением имени в комментарий.

### 6.2. Resume input внутри пересоздаваемого workspace

*Закрыто 2026-11-18, ветка `ang_review_2026_11_17`: явная декларация `resume_inputs` (`looplab/core/retry_inputs.py`, `looplab/engine/workspace.py::retry_input_preflight`) проверяется на приёме `retry_node`/`node_reset` и повторно в `WorkspaceSeeder.materialize` **до** архивации логов и `rmtree_readonly_aware(wd)`. Ссылки внутрь пересоздаваемого `nodes/node_<id>` или `canary/` (включая внешние симлинки и вложенные симлинки директорий) отвергаются с `ephemeral_resume_input` до удаления старой попытки; отсутствующие объявленные пути дают `missing_resume_input` (в отличие от штатной очистки необъявленного output), расхождение попытки/квитанции — `resume_input_attempt_drift`, несовпадение `sha256` — `resume_input_integrity_mismatch`, при сохранении legacy поведения и stage reuse. Тесты: `tests/test_retry_input_preflight.py`.*

Независимое ревью 18 ноября: также отклоняются ancestor-каталоги и цепочки directory symlinks. Неполный обход (>512 entries или unreadable directory) возвращает `resume_input_scan_incomplete`; нельзя считать непроверенный хвост безопасным. После архивирования, непосредственно перед удалением, входы проверяются снова. Проверки: `tests/test_antigravity_review_regressions.py`.

Решение: отдельная явная декларация resume inputs с identity/durability, containment проверкой
и validation **до** удаления. Refuse ephemeral/self-dependent references, назвать конкретную
операцию и путь; сохранять verified external refs. Не угадывать зависимости по любым JSON-строкам,
не удерживать все outputs, не обходить stage-reuse или восстановление source.
Приёмка: внутренняя ссылка отвергнута до cleanup и старая попытка сохранена; внешняя verified
ссылка проходит; symlink/attempt drift/refused integrity; declared missing input отличён от
необъявленного произвольного output. Legacy поведение и stage reuse проверяются отдельно.

### 6.3. Известный completion barrier, реализация ещё не опубликована

*Source integration 2026-11-17:* `completed-eval-waits-for-advisory-monitor` is implemented by the existing `ab76e01` source change; integrated acceptance is recorded in the integration receipt. This is not a statement about live runtime adoption or physical GPU release.

Существующий `ab76e01` сохраняет unresolved advisory review отдельно от execution terminal.
Нужна интеграция с meaningful acceptance, не новый бот auto-answer и не invented continue.
Приёмка: один terminal и release, restart сохраняет unanswered review; actual stage/deadline
решения не bypass; reset/reclaim/unknown result/corrupt evidence fail safely.16 focused CPU cases
повторно PASS на isolated implementation. Физический GPU release следует проверить в настоящем
разрешённом idle integration window; этот CPU probe его не измерял.

## 7. Воспроизведения и пределы проверки

- На исходном master replay+OPEN index: **206 PASS**.
- Настоящие CAS evaluator/stopped guard/paused submit tests: **4 PASS**.
- Сценарий developer_stuck → return → own rebuild → real freshness: **8 PASS** с семью
  прежними cases; новая регрессия добавлена в существующий test module. Старый пункт69.1
  опровергнут уже имеющимся production fold; code fix для него не нужен.
- Bounded successful artifact/неотвеченный advisory: master не возвращается до отмены0.75с,
  три опроса. Это доказывает barrier, но не измеряет GPU lease occupancy.
- Реальные `effective_eval_spec`/`build_command` выбирают shared evaluator; настоящий materializer
  удаляет receipt внутри node workdir и сохраняет внешний. Это воспроизводит документированные
  hazards, а не утверждает, что движок обещал сохранить произвольный output.
- Notification source review опроверг гипотезу о потере attempt identity: native generation
  сохраняется в outbox details. На момент исходного аудита human headline его не выводил
  (в ветке `ang_review_2026_11_17` номер попытки `(gen <N>)` добавлен в human summaries
  `node_evaluated`, `node_failed`, `stage_failed` и `monitor_question`); отдельный OPEN не создан.

Синтетические CPU evidence snapshots сохранены в
[audit evidence](audit/external-engine-idle-probes-20261117.json). Пробы не запускали модель,
не выполняли raw score, не меняли live API и не измеряли причинный вклад смены агентной модели.

## 8. Повторная проверка всех исходных OPEN

Исходный remote census содержит **58** пунктов. Все собственные predicates проходят. Это структурная проверка, а не доказательство результата; семантика и остаточная приёмка разобраны для каждого пункта в [машинном census](audit/external-engine-open-census-20261117.json). Из них один диагноз 69.1 опровергнут настоящей регрессией. Все остальные исходные пункты сохраняются; новые пункты §6 имеют отдельные slugs.

| Исходный slug | Фактический статус на проверенном master |
|---|---|
| `meter-ceiling-blind-to-tool-call-runaways` | Нужен настоящий результат измерения; source/unit tests не заменяют его. |
| `structured-claims-setting-retirement` | На master gap сохраняется; реализация есть только в неопубликованной isolated серии. |
| `unconverted-stat-signature-ledger` | Ограничение контракта/консервативный выбор сохранён; обещанием не закрывать. |
| `research-grade-profile-is-not-the-default` | Нужен настоящий результат измерения; source/unit tests не заменяют его. |
| `embedding-novelty-gate-declined-on-one-incident` | Нужен настоящий результат измерения; source/unit tests не заменяют его. |
| `eval-noise-floor-is-never-measured` | Нужен настоящий результат измерения; source/unit tests не заменяют его. |
| `no-external-benchmark-number-exists` | Нужен настоящий результат измерения; source/unit tests не заменяют его. |
| `developer-hack-rate-unmeasured` | Нужен настоящий результат измерения; source/unit tests не заменяют его. |
| `foresight-selective-accuracy-unmeasured` | Нужен настоящий результат измерения; source/unit tests не заменяют его. |
| `smoke-full-rank-fidelity-unmeasured` | Нужен настоящий результат измерения; source/unit tests не заменяют его. |
| `no-research-lifecycle-benchmark-number` | Нужен настоящий результат измерения; source/unit tests не заменяют его. |
| `parallel-build-is-a-bulk-synchronous-barrier` | Нужен настоящий результат измерения; source/unit tests не заменяют его. |
| `report-review-nominates-no-experiment` | На master gap сохраняется; реализация есть только в неопубликованной isolated серии. |
| `discarded-card-rebuild-cost-unmeasured` | Нужен настоящий результат измерения; source/unit tests не заменяют его. |
| `best-of-n-ranks-without-executing` | На master gap сохраняется; реализация есть только в неопубликованной isolated серии. |
| `skills-have-no-external-source` | На master gap сохраняется; реализация есть только в неопубликованной isolated серии. |
| `concurrent-runs-share-no-work-claims` | На master gap сохраняется; реализация есть только в неопубликованной isolated серии. |
| `stuck-rebuild-always-superseded` | Диагноз опровергнут; новый behavioral test проходит, marker удалён. |
| `repair-holds-the-only-eval-slot` | still present; internal LLM repair lane; do not attribute this Nov16 external preparation delay to this mechanism |
| `no-metric-behaviour-detector` | still absent; advisory change detection does not prove scorer correctness or relevance |
| `board-rotation-by-attempt-only` | still present; internal prompt context, not external Codex task queue |
| `card-plan-counts-hidden-nodes` | still present; f381eee eligible population shared by plan/gates, unshipped |
| `lesson-from-pre-repair-prose` | still present; c2c5154 uses current repair ledger rationale, unshipped |
| `ready-dispatch-waits-for-cadence` | still present; internal speculative session cadence; not all time external agent spends preparing nodes |
| `canary-never-reused-across-nodes` | still present intentionally conservative; cross-node reuse speed feature, distinct from no-op canary validation error; require full source/env/protocol equivalence to relax |
| `dotenv-files-unreadable` | still present with documented unsafe attempted relaxation; readability limitation, not authority to print secrets; robust all-channel parsing/redaction acceptance needed |
| `no-offline-policy-replay` | still absent; must not claim counterfactual outcomes for unevaluated candidates |
| `no-cluster-executor` | still absent; single-machine implementation; do not close platform capability with local CPU tests or literal sbatch token; local docs narrow and retain platform item |
| `external-russian-readability` | Часть реализации есть; end-to-end/production приёмка остаётся открытой. |
| `external-report-presentation` | Часть реализации есть; end-to-end/production приёмка остаётся открытой. |
| `experiment-runtime-parameters` | Часть реализации есть; end-to-end/production приёмка остаётся открытой. |
| `experiment-artifact-previews` | Часть реализации есть; end-to-end/production приёмка остаётся открытой. |
| `experiment-resource-snapshot` | Часть реализации есть; end-to-end/production приёмка остаётся открытой. |
| `job-role-lost-at-node-creation` | implementation-unpublished; unrelated to client session identity proof |
| `external-reads-lack-end-to-end-bounds` | implementation-unpublished, live adoption unmeasured |
| `evaluation-faithfulness-not-attested` | measurement/semantic review OPEN; do not close by deterministic integrity tests |
| `external-harness-version-and-spend-unrecorded` | source implementation unpublished; live billing measurement remains UNKNOWN |
| `external-work-state-has-no-authored-checkpoint` | implementation-unpublished; not a proof that a client is the sole physical session |
| `operator-directions-lack-explicit-retirement` | implementation-unpublished; delivery is not comprehension/execution |
| `harness-revisions-lack-matched-eval` | measurement OPEN |
| `research-evidence-acceptance-missing` | acceptance OPEN, do not call all evidence graphs absent |
| `memory-injection-outcome-unmeasured` | measurement OPEN |
| `research-acceleration-attribution-missing` | measurement OPEN; 13.5h outsideattempt is not a proved avoidable idle total |
| `landlock-is-opt-in-by-default` | Нужна приёмка на целевой платформе; Linux CPU не доказывает Windows/remote поведение. |
| `eval-parallelism-is-in-process-only` | Один host/in-process; пересекается с no-cluster-executor. Не потолок в два GPU. |
| `asha-inert-on-this-task-family` | Нужен настоящий результат измерения; source/unit tests не заменяют его. |
| `first-propose-runs-with-every-gpu-idle` | Нужен настоящий результат измерения; source/unit tests не заменяют его. |
| `claim-refutation-undercut` | На master gap сохраняется; реализация есть только в неопубликованной isolated серии. |
| `belief-identity-text-keyed` | Ограничение контракта/консервативный выбор сохранён; обещанием не закрывать. |
| `windows-tree-kill-is-not-atomic` | Нужна приёмка на целевой платформе; Linux CPU не доказывает Windows/remote поведение. |
| `categorical-leak-rung-never-measured` | Нужен настоящий результат измерения; source/unit tests не заменяют его. |
| `atomicio-windows-parent-publication` | Нужна приёмка на целевой платформе; Linux CPU не доказывает Windows/remote поведение. |
| `stream-idle-kill-unproven-on-windows` | Нужна приёмка на целевой платформе; Linux CPU не доказывает Windows/remote поведение. |
| `crash-predictability-unmeasured` | Нужен настоящий результат измерения; source/unit tests не заменяют его. |
| `raw-stage-card-and-audit-are-separate-appends` | На master gap сохраняется; реализация есть только в неопубликованной isolated серии. |
| `read-fence-inputs-are-writable-by-the-fenced-process` | Ограничение контракта/консервативный выбор сохранён; обещанием не закрывать. |
| `read-fence-self-protection-ends-at-an-unfenced-child` | Ограничение контракта/консервативный выбор сохранён; обещанием не закрывать. |
| `punched-dir-is-not-listable` | Нужна приёмка на целевой платформе; Linux CPU не доказывает Windows/remote поведение. |

## 9. Source integration addendum, 17 November 2026

After the original audit publication, the user explicitly authorized integration of the complete accepted source series through `5a1e6ef6` into actual remote master, preserving this audit and its two new integration items. The original58-item census remains a historical baseline, not the post-integration OPEN inventory. Source closures require implementation plus meaningful integrated tests; measurement/platform/end-to-end UNKNOWNs remain open. Live services, client, mirror, hooks, ML and leases remain unchanged. No deployment is part of this source publication.
