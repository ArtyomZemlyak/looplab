# 83. Autoresearch, улучшение harness и измерение AI R&D

**Дата:** 2026-11-06. **База:** `master` `7e084afdac2a00734b96d6aeb20920ad0069e190`.
**Статус:** критический разбор первоисточников, сверка с кодом и живым production run, предложения
и OPEN-маркеры. Последующая правка исправляет наблюдаемость и внешний research workflow в ветке;
defaults, production runtime и живые jobs не меняются. Проверка описана в §7.3.

Сверка сессий, версий и всех действующих маркеров: [аудит 10 ноября](audit/session-open-triage-20261110.md).
Он различает исправление в исходниках, публикацию и deployment; старые инциденты не удаляются.

## 1. Главный вывод

LoopLab уже имеет сильный контур исполнения эксперимента: событийный журнал, replay, snapshot кода,
защищённый scorer, стадии, provenance метрики, протокол сравнений и средства остановки. Строить рядом
второй scheduler, вторую базу памяти или ещё одну иерархию из десятков агентов не нужно.

Главный недостающий контур другой: **измерять и безопасно улучшать сам research harness**. Для этого
нужны четыре связанных объекта, а не новый набор ролей:

1. **Evaluation Contract** — версионированная запись о том, какой научный вопрос и какое решение
   действительно измеряет scorer. Машинно корректная метрика ещё не доказывает, что измеряется нужная
   вещь.
2. **Harness Revision + External Work State** — воспроизводимая версия модели, инструкций, skills,
   инструментов и бюджетов агента, а также короткое авторское состояние текущей работы внешнего
   исполнителя.
3. **Harness Evaluation** — сравнение двух версий harness на одних задачах, модели, scorer и бюджете,
   сначала в shadow-режиме. Меняется только harness. Нельзя выбирать победителя по самооценке или по
   одному удобному запуску.
4. **Evidence Graph + Research Ledger** — явная связь вопроса, эксперимента, метрики, утверждения и
   отчёта, плюс учёт полезного результата, затрат и потерь по всему исследовательскому циклу.

Это позволит улучшать LoopLab комплексно: не только выбирать следующий ML-кандидат, но и понимать,
какие инструкции, память, инструменты и процессы реально ускоряют получение проверенного результата.

## 2. Что не так с упрощением «цикл + состояние + внешний вердикт»

Такая схема полезна как минимальная модель autoresearch. Но для производственного LoopLab она
пропускает шесть сущностей:

- **смысл цели и evaluator:** число может быть правильно посчитано, но отвечать на другой вопрос;
- **версию harness:** два запуска одной модели с разными инструкциями и памятью — разные системы;
- **полную стоимость:** GPU-часы обучения не включают tokens, время и повторную работу агента;
- **доказательство:** ссылка, авторская уверенность и независимо проверенный результат — разные уровни;
- **полномочия:** кто может менять задачу, промпт, память, scorer и production defaults;
- **перенос опыта:** сохранённый урок не обязан быть полезным на новой задаче и может навредить.

Поэтому предлагаемая ниже работа не сводится к улучшению выбора следующей ноды.

## 3. Разбор первоисточников

Все числа ниже относятся к авторам соответствующих работ, если не сказано иное. Независимого
воспроизведения результатов в этом обзоре не было. Читались полные PDF, первичная документация или код;
пересланные короткие пересказы использовались только как навигация.

### 3.1. Минимальный цикл, файлы и совместная работа

**Karpathy [autoresearch](https://github.com/karpathy/autoresearch).** Агент меняет только `train.py`,
запускает фиксированные пять минут обучения и сравнивает `val_bpb`; `prepare.py` и evaluator неизменны.
`program.md` задаёт commit/run/keep-or-revert и журнал `results.tsv`. Это отличный эталон дисциплины
единой edit surface и одинакового бюджета. Но фиксированное wall-clock время делает результаты
зависимыми от платформы, а один scalar не превращает инженерную оптимизацию в полноценную науку.

**[OpenResearch](https://github.com/alphaXiv/OpenResearch) и его [agent guide](https://openresearch.sh/docs/agents).**
Локальная SQL-база хранит эксперименты, а код, логи и артефакты остаются на машине; внешние coding
agents подключаются через skills. Полезны local-first наблюдаемость и выбор compute. Сам по себе список
SSH/Slurm/Kubernetes/Ray не доказывает более качественный research loop. Нам distributed execution сейчас
не нужен, а git identity, event journal и previews уже есть.

**[AiScientist](https://github.com/AweAI-Team/AiScientist), [paper](https://arxiv.org/abs/2604.13018).**
File-as-Bus — это не просто общая папка: у артефактов есть назначение, update mode, writers/readers и
компактная workspace map. Авторы сообщают, что удаление File-as-Bus снижает PaperBench на 6.41 пункта и
MLE-Bench Lite Any Medal на 31.82 п.п.; MLE-Bench Lite имеет три seed, а полный 20-task PaperBench оценён
ими примерно в $832.
Практический вывод: сохранять «толстое» состояние на диске, но передавать агенту компактную карту,
а не всю историю. LoopLab уже хранит много состояния; проблема — его актуальная проекция и связность.

**[CORAL](https://github.com/Human-Agent-Society/CORAL), [paper](https://arxiv.org/abs/2604.01658), [eval loop](https://coral.compounding-intelligence.ai/docs/concepts/eval-loop).**
Attempts, notes и skills разделяются между агентами; grader работает отдельно над зафиксированным commit,
heartbeat вызывает reflection/consolidation/pivot. Ablation отключения notes/skills ухудшает результаты,
а co-evolution сравнивается с best-of-4 independent runs. Это хорошие доказательства ценности памяти,
но не аргумент за обязательный skill после каждого eval, одинаковых агентов или доступ ко всему общему
состоянию. Авторы сами ограничивают вывод необходимостью хорошо заданного evaluator.

**[Sibyl](https://github.com/Sibyl-Research-Team/AutoResearch-SibylSystem).** Имеет 19 стадий, более
20 агентов, prompt overlays и автоматические исправления с тестами. Полезна идея отслеживать эффективность
уроков. Однако README не даёт сопоставимого независимого benchmark, который доказывал бы выигрыш именно
автоматического переписывания системы после каждой итерации. Переносить в production нужно не
«self-evolution ради self-evolution», а версионированные изменения с исполнением тестов и возможностью
отката. Здоровый живой run автоматически менять нельзя.

### 3.2. Полный исследовательский процесс

**[AI Scientist v1](https://github.com/SakanaAI/AI-Scientist).** Архив идей проверяется на новизну через
литературу, эксперименты строятся вокруг человеческих templates, а manuscript review использует ансамбль
LLM-рецензий. Сильный template делает систему надёжнее, но сужает область поиска; открытая автономность не
является безусловным преимуществом. Случай самопроизвольного продления time limits из последующего разбора
Agentic Scientist — аргумент за неизменяемый execution/evaluation contract, а не только за сильную модель.

**[AI-Scientist-v2](https://github.com/SakanaAI/AI-Scientist-v2).** Progressive agentic tree search проходит
preliminary investigation → tuning → agenda → ablations. Сами авторы предупреждают: v2 не обязательно
лучше v1 при сильном начальном шаблоне и имеет меньшую надёжность на открытых задачах. У нас tree search
и staged evaluation уже есть; копировать четыре стадии как обязательную бюрократию не нужно.

**[Agent Laboratory](https://github.com/SamuelSchmidgall/AgentLaboratory), [paper](https://arxiv.org/abs/2501.04227).**
Линейные фазы literature → experimentation → report поддерживают co-pilot checkpoints. Работа подчёркивает
расхождение human и automated evaluation и пользу человеческих поправок. Для нас ценна небольшая явная
точка проверки научного замысла, а не обязательное подтверждение каждого technical retry.

**[AI-Researcher](https://github.com/HKUDS/AI-Researcher), [paper](https://arxiv.org/abs/2505.18705).**
Различает подробную идею пользователя и ideation по референсам, использует divergent/convergent design и
LLM-оценщиков. Разброс оценок между LLM и разбор слабых теоретических результатов важнее рекламного
«approach human-level». Независимый LLM review полезен как второе мнение, но не как доказательство
научной истины.

**[Kosmos](https://arxiv.org/abs/2511.02824).** Structured world model разделяет состояние между analysis
и literature agents; каждое утверждение снабжается кодом или первичной литературой. При этом эксперты
признали точными 79.4% выбранных утверждений; для interpretation точность была 57%, для data analysis —
85%. Это прямое доказательство, что citation coverage не равен истинности или значимости. Отбор важных
результатов остаётся трудоёмкой человеческой работой. Нужны проверяемые evidence links и отдельная оценка
смысла, а не больше текста с источниками.

**[The Agentic Scientist](https://pub.sakana.ai/agentic-scientist/), [paper](https://arxiv.org/abs/2610.18933).**
Сильные элементы: independent design review, alignment judge над evaluator, typed idea contracts,
раздельные rubric-pass и best-score decisions, shared memory с context resets, retrospective baselines и
evidence map. Сравнение включает 70 campaigns, 5 задач и 14 model–harness сочетаний; выбор harness менее
заметен, чем выбор базовой модели. В двух из трёх рассмотренных Pi runs агент изменил test-time protocol,
и rubric это пропустила. Поэтому scorer и его семантика должны быть защищены и рецензируемы, а не только
метрики кандидатов. 52%→98% в Doc-to-LoRA относится к synthetic multi-needle retrieval, а не к любому
длинному документу. Только одна работа прошла внешнее peer review после человеческой проверки.

### 3.3. Улучшение harness, инструменты и разнообразие

**[Harness²](https://github.com/google-research/harness2), [paper](https://arxiv.org/abs/2610.14224).**
Propose–Probe–Compose выполняет кандидатов на той же задаче и сопоставляет поведение, а improvement
library сохраняет adopted harness и уроки с evidence. Принципиальные оговорки: behavioral contrast не
доказывает correctness, adoption не означает optimality, компоненты не обязаны быть аддитивными, повторный
offline reuse не измеряет online gain. Дополнительные executions стоят денег, а открытый web доступ может
раскрыть benchmark answers. Для LoopLab это лучший референс именно paired harness evaluation, но не
автоматического принятия новых промптов.

**[Hyra 2.0](https://hy.tencent.ai/research/hyra2).** Overall/Mission/Worker разделяют приоритеты,
долгосрочное владение вопросом и исполнение. Research Board, evidence-first Review Worker и разделение
project state, agent memory и Books особенно близки нашему случаю. Books различает Observations, Verified
и Conventions; последние требуют подтверждения человеком. Результаты обучения новых моделей не являются
контролируемой ablation самого harness. Переносим типизацию знаний и независимый просмотр доказательств,
а не утверждение, что новые модели доказали превосходство системы.

**[QD-Rec](https://pub.sakana.ai/qd-rec/), [paper](https://arxiv.org/abs/2610.23470), [code](https://github.com/SakanaAI/QD-Rec).**
Архив состоит из item–hypothesis пар; diversity-aware contextual bandit выбирает объект, mutate/crossover/
explore используют прошлые гипотезы, а novel или лучшая в локальном сравнении запись сохраняется. Важно,
что слабая гипотеза может стать stepping stone для другой задачи. Но качество в основном оценивает LLM,
некоторые сравнения используют нескорректированные t-tests, а в примерах авторы сами нашли неточности.
Для нас это повод сохранять разнообразные неудачные направления и сравнивать полезность ниш, а не
бесконтрольно увеличивать KB. В LoopLab это прежде всего усиливает уже открытые board rotation, novelty
и work-claims, а не требует отдельного QD-движка.

**[Prove2Me](https://prove2.me/about), [paper](https://arxiv.org/html/2608.28433v2).** Immutable theorem
statements отделены от proofs, proof-sketches создают переиспользуемый граф, Lean kernel проверяет тип.
Люди аудируют только mission core: цель, определения и milestone statements. Kernel не доказывает, что
statement верно отражает намерение. Сравнение стоимости проектов не контролирует модель и harness, что
авторы прямо признают. Для LoopLab это сильнейший аналог Evaluation Contract: проверять смысловое ядро
один раз, не рецензируя каждую техническую операцию, и сохранять результаты как независимо адресуемые
объекты.

### 3.4. Что METR позволяет — и не позволяет — сказать об ускорении

[METR: Anthropic AI R&D acceleration assessment](https://metr.org/blog/2026-11-05-anthropic-ai-rd-acceleration-assessment/)
оценивает рост AECI примерно в 1.9 раза быстрее прежнего тренда [95% CI 1.5–2.8], но подчёркивает
неопределённость, redactions и возможный диапазон robustness 1.1–2.9. Основной вклад пока даёт AI
assistance, а не полностью автономные исследования. Self-reported индивидуальный speedup нельзя напрямую
считать ускорением всей программы. Well-specified benchmarks могут не отражать messy real-world work,
а модельные результаты запаздывают относительно исследовательских улучшений.

Для LoopLab это означает: нельзя писать «новый prompt ускорил research в 2 раза» по длительности одного
run. Нужны сопоставимые кампании, качество результата, полные затраты и анализ контрфактуала.

## 4. Что уже есть в master — и не должно дублироваться новой системой

- Event-sourced `RunState`, replay, code snapshots, code worktrees и export; operator-owned scorer,
  staged `train → score`, stage reuse с отказом при недоказанной идентичности, lifecycle и budget gates.
- `question_card_id` у experiment registrations, research memos с claims/sources, local lessons,
  report metric bindings и сравнение по per-query arrays.
- Versioned reports, cumulative overview, research notices и `research_notice_dispositions.jsonl`.
- Lesson hygiene, promotion/demotion skills, prior-injection receipts и `events/prior_citations.py`.
  Citation utility уже существует, но измеряет использование в rationale, а не пользу для результата.
- Internal `agent_checkpointed` и phase spans. Они относятся к внутренним ролям, не сохраняют состояние
  отдельного Codex/Claude процесса, его модель, prompt/skills, затраты и незавершённые CPU-наработки.
- Delegated `cli_agent` уже имеет usage accounting, но независимо подключённый `external_harness` — другой
  режим, за который LoopLab не получает модель и usage внешнего CLI. Не дублировать существующий ledger.
- `hint` с `replace: true` умеет заменять standing directive. Нужен не новый базовый механизм замены, а
  scoped lifecycle по темам и завершение конкретных директив.
- Trust, comparison и contract machinery. Их нужно расширять на семантику evaluation и harness, а не
  создавать параллельный trust store.

## 5. Факты из живого production run

Снят read-only срез одного production external query/SID run на 2026-11-06, event seq 1232. Run был на
паузе, engine не работал, pending jobs не было. Точные UID/generation для архитектурного вывода не нужны и
здесь не приводятся. Никаких научных действий, restarts или изменений данных обзор не производил.
Наблюдения не следует распространять на все исторические runs.

1. **`connection_check` не проходит на длинном run.** `/state?observe_only=true` вернул 321 761 байт;
  MCP отклонил ответ как `response_incomplete`. При этом узкий `run_progress` читается. Это не outage UI,
  а дефект выбранного bootstrap read.
2. **Pagination не ограничивает общий research map по объёму.** При `limit=10` ответ занял около 93 КБ
  текста. Один `offset/limit` применяется к каждой коллекции отдельно, а directions, overview и quality
  добавляются сверху. Транспортный cap ещё не обязан быть превышен, но контекстный бюджет агента уже
  расходуется непропорционально.
3. **Формальная связность evidence graph не заполнена, несмотря на существующий API.** Research map
  показывает 31 memo, только 9 с sources, 0 structured claims и 0 claim-source links; 9 registrations имеют
  question, но 0 — question card; из 25 nodes 17 имеют parent card, 19 не классифицированы по work kind. У
  текущего отчёта нет `key_metrics`, metric profile не настроен, `inspect` сообщает `comparability: UNKNOWN`.
  Это факты заполнения, а не оценка научной ценности: technical work может не требовать claims или sources.
4. **Часть проблемы — использование и приёмка, а не отсутствие функций.** Поддержка question cards,
  claims, notices и bindings уже есть. Нужна сквозная приёмка и guided remediation, а не ещё одно поле,
  которое снова останется пустым.
5. **Есть и противоречие в проекции.** `research_quality.py` пишет, что universal acknowledgement ledger
  отсутствует, хотя `serve/research_notices.py` уже реализует disposition journal. Это отдельный подтверждённый
  случай устаревшей модели состояния.
6. **Директивы перегружены историей.** Возвращаются 6 из 14 standing directions, каждая до 2000 знаков.
  `hint` уже поддерживает `replace: true`, заменяющий прежнюю standing directive. Но нет scoped retirement
  по id/topic и статуса completed: глобальная замена и «последняя выигрывает при конфликте» не различают
  независимые задачи, поэтому автоматически выбрасывать всё, кроме последней, нельзя.
7. **Стоимость независимого внешнего исследования невидима.** `looplab tokens` сообщает отсутствие LLM
  ledger для external/toy run. В `timings` 36% wall clock не покрыто spans, а 30% времени после первой
  оценки нет evaluation; сами по себе эти паузы не доказывают потерю GPU или вину scheduler. Нужна атрибуция
  ожидания, человеческих решений, подготовки и внешней модели, а не ещё один aggregate speedup.
8. **Объявленный `job_role` теряется при материализации.** `_emit_node_created` не принимает и не пишет
  `job_role`. Это самостоятельный дефект проверенного master, а не объяснение всех legacy unclassified:
  у старых записей причина может быть иной.

## 6. Целевая архитектура

### 6.1. Evaluation Contract

Версия scientific contract должна содержать вопрос, ожидаемое решение, target/label, cohort и split,
основную метрику и её роль, scoring policy, доступность фичей на момент inference, scorer/source digests
и фасеты сопоставимости. Отдельно фиксируются: машинная валидация, independent semantic review и
operator attestation. Технический recovery может честно пометить большинство полей неприменимыми.

Это не новый store: используются существующие task/protocol snapshots, amendment history, report bindings
и version references. Независимый reviewer читает исходный вопрос и evaluator до вывода кандидата, чтобы не
принять его объяснение за истину. Изменение contract после просмотра результата — новая версия, а не
ретроспективное «мы это и имели в виду». Для уже работающего healthy fit новые правила вводятся только на
безопасной границе; нет требования останавливать обучение ради оформления.

### 6.2. Harness Revision и External Work State

`HarnessRevision` — immutable identity: provider/model и redacted endpoint identity, generation parameters,
CLI version, prompt/skill/tool versions, policy scopes, resource budgets и язык. Он связывается с каждой
попыткой, usage и результатом. External usage может быть `provider_observed`, `agent_declared` или `unknown`;
самодекларация не превращается в независимый факт. Revision digest вычисляется из существующих настроек и
дополнительной внешней metadata, а не из второй независимо редактируемой копии конфигурации.

`ExternalWorkState` — короткая авторская запись: текущая цель, выполненный шаг, следующий supported
action, blocker/owner, активные work claims и ссылки на незавершённые артефакты. Сырые chain-of-thought,
полные transcripts и секреты не нужны. Это состояние не даёт права запуска: recovery снова читает live
progress, receipts и authority.

### 6.3. Paired Harness Evaluation

Базовая и новая версии выполняются на одних задачах, модели, scorer и бюджете. Сначала shadow, затем
ограниченный canary, затем явное принятие на безопасной границе. Измеряются внешняя correctness,
поведенческие различия, tool errors, retries, стоимость и время. Сохраняются отрицательные и нулевые исходы
при их наличии; `keep_current` всегда допустимо.

Протокол: [Harness Revision Benchmark](audit/harness-revision-benchmark.md). Это отдельный контур от
`no-offline-policy-replay`: там переигрывается выбор policy на записанном дереве, здесь реально выполняются
разные harness revisions. Arms переиспользуют существующий experiment protocol и receipts, а не новый
scheduler. Контур также помогает `research-grade-profile-is-not-the-default`,
`smoke-full-rank-fidelity-unmeasured` и `no-research-lifecycle-benchmark-number`, но не подменяет их.

### 6.4. Evidence Graph и Research Ledger

Связи question → card → experiment/arm → attempt/metric → claim → report собираются из существующих
реестров. Для scientific работ отсутствие критичной связи получает конкретный supported action и owner;
для engineering recovery связь может быть неприменимой. Не вводится единый «research quality score»:
каждое предупреждение объясняет, какого именно доказательства недостаёт.

Research Ledger — проекция существующих events, spans и usage records, а не ещё один независимый ledger.
Он добавляет model/agent spend, время фаз, time-to-first-valid-candidate, время до проверенного улучшения,
долю повторной работы и причины ожидания. Никакой единой формулы «ускорение = старое время / новое время»
без согласованного качества и полных затрат.

### 6.5. Жизненный цикл знаний

Observation — авторская гипотеза или единичное наблюдение. Verified — заявленный аспект подтверждён
независимым или повторным исполнением в согласованном scope; это не универсальное доказательство истины.
Convention — правило, влияющее на поведение системы; для production adoption нужна отдельная авторизация.
Исходное evidence сохраняется во всех случаях, а переход между уровнями не является автоматическим.

Citation rate остаётся диагностикой использования, но не доказательством пользы. Протокол оценки
[Memory Injection Outcomes](audit/memory-injection-outcomes.md) сравнивает одинаковый harness с заданной
памятью и без неё, включая negative transfer и цену лишнего контекста.

## 7. Новые OPEN-пункты

Каждый пункт имеет машинный falsifier. Пункты про измерения ссылаются на протокол без результата;
документ не объявляет их выполненными.

### 7.1. P0/P1 — корректность и наблюдаемость

- **Исправлено: потеря `job_role` при создании ноды.** Единый emitter теперь сохраняет optional
  `job_role` в фактическом `node_created`, replay возвращает его в `Node`, а rebuild наследует роль.
  Старые события без этого поля остаются `unknown`, а не получают ретроспективную классификацию.
  Отдельный `node_kind=artifact` по-прежнему не является синонимом job role и не обещает primary scalar.
  Регрессионная проверка: `tests/test_job_role_materialization.py`.

- **Bounded external reads — isolated implementation, 11 November.** Bootstrap now reads a small
  versioned identity; API/MCP/CLI/UI research maps share a 64 KiB whole-response budget, explicit
  section omissions and independent collection cursors. Historical reports past 200 remain reachable.
  Synthetic HTTP/MCP/CLI/UI acceptance is recorded in [bounded reads](audit/external-read-bounds.md).
  Production incidents in §5 remain historical evidence; this is not a live deployment claim.

- **OPEN[evaluation-faithfulness-not-attested].** Защита scorer доказывает целостность исполнения, но не
  соответствие target и теста исходному научному намерению.
  proof:`absent:RESULT 20@docs/audit/evaluation-faithfulness.md`
  **Исправление:** Evaluation Contract из §6.1, независимый semantic review и явный статус comparability.
  Приёмка по [протоколу](audit/evaluation-faithfulness.md): намеренно изменённые target/label, cohort,
  availability или scoring policy выявляются до научного вывода; technical retry может обойтись без
  literature review; число с неизвестной сопоставимостью не попадает в общий ranking как сравнимое.

- **OPEN[external-harness-version-and-spend-unrecorded].** Existing diagnostic checkpoints now record
  bounded, non-secret revision fingerprints and delta usage with exact run/node/attempt identity;
  API/UI/export preserve provider-client observations versus agent declarations, missing counters,
  conflicting duplicates and original revisions after restart while keeping native LLM accounting and
  GPU hours separate ([source acceptance](audit/external-harness-telemetry.md)). Live campaign client
  coverage and independent billing attestation remain unmeasured.
  proof:`absent:RESULT 20@docs/audit/external-harness-telemetry.md`

- **Authored external recovery state — isolated implementation, 11 November.** The existing native
  `agent_checkpointed` event now carries bounded external work notes through saved command receipts.
  Progress/briefing recovers goal, last completed step, blocker/owner and unfinished references;
  changed direction/lifecycle/budget context withholds the old next-step suggestion. The concrete
  recovery action is a pure receipt/progress read. [MCP/native/cold-process acceptance](audit/external-work-state-recovery.md)
  preserves completed nodes and original payloads without automatic retry. Artifact/harness/tool
  identities remain declared or unverified until independently checked; no live rollout is claimed.

- **Operator direction lifecycle — isolated implementation, 11 November.** Scoped hint commands
  now carry immutable IDs/topics, exact same-topic supersession and explicit completed/cancelled
  retirement. Retired IDs stay retired across duplicates, accepted-command replay, legacy global
  replace and service restart. Current context omits retired instructions; history remains durable.
  [Public command/replay acceptance](audit/operator-direction-lifecycle.md) includes append races
  and preserves existing cold-start quarantine and active-command admission guards. No live rollout.

### 7.2. P1/P2 — управляемое улучшение и научная ценность

- **OPEN[harness-revisions-lack-matched-eval].** Нет стандартного сравнения версий harness при
  неизменных модели, задачах, scorer и бюджете.
  proof:`absent:RESULT 20@docs/audit/harness-revision-benchmark.md`
  **Исправление:** реализовать протокол §6.3 как shadow experiment над существующими runs и receipts,
  без автоматического изменения production prompts/defaults. Приёмка: paired results содержат quality,
  behavior, cost, CI/ограничения и keep-current; adopted revision воспроизводима, rollback не стирает историю.

- **OPEN[research-evidence-acceptance-missing].** Существующие связи и journals не образуют обязательную
  сквозную приёмку внешнего scientific workflow; production-факты в §5.
  proof:`absent:RESULT 20@docs/audit/external-research-evidence.md`
  **Исправление:** проекция evidence graph с конкретными remediation actions, единое чтение notice
  dispositions и непротиворечивые статусы; не создавать обязательную литературу для технических jobs.
  Приёмка: question/card/arm/metric/claim/report проходят по ссылкам в обе стороны; отсутствующие связи
  видны как N/A или blocker с причиной; текст не утверждает отсутствие уже существующего journal.

- **OPEN[memory-injection-outcome-unmeasured].** Citation utility показывает использование prior в
  rationale, но не улучшение/ухудшение результата и не цену контекста.
  proof:`absent:RESULT 20@docs/audit/memory-injection-outcomes.md`
  **Исправление:** controlled on/off и leave-one-out сравнения с зафиксированным harness, классификация
  memory по scope и evidence tier, регистрация negative transfer. Приёмка: решение о retrieval/retirement
  опирается на outcome по задачам, а citation не используется как суррогат истины; shared KB не меняется
  автоматически.

- **OPEN[research-acceleration-attribution-missing].** Протокол уже записан, но нет принятого измерения
  ускорения исследовательской программы с полными затратами и атрибуцией ожидания по причинам.
  proof:`absent:RESULT 20@docs/audit/research-acceleration.md`
  **Исправление:** Research Ledger и сопоставимые кампании по [протоколу](audit/research-acceleration.md).
  Приёмка: verified improvement, quality, полные agent/LLM/GPU costs, wall time, повторная работа и
  waiting causes видимы вместе; self-reported временные оценки не выдаются за контрфактуал; нет заявления
  об ускорении при несопоставимом результате.

### 7.3. Исправления после живого разбора

В рабочей ветке закрыт `job-role-lost-at-node-creation` и исправлена наблюдаемость внешнего workflow.
Единый `node_created` emitter сохраняет `job_role`; replay и rebuild сохраняют старые записи как
`unknown`. `created_at`, `eval_started_at`, `completed_at` вычисляются из событий, но не попадают во
внутренний `model_dump`: public `/state`, `/nodes/{id}` и research map добавляют их отдельной
проекцией, поэтому golden replay остаётся прежним.

`uses` отображается пунктиром отдельно от научных parents; Concepts показывает operator эксперимента.
Artifact completion без scalar объясняется как не-ranked работа, а не измеренное улучшение или ноль.
Blocker’ы получили человеческие пояснения; `work_terminal` не выдаётся за ошибку. Research map
показывает timestamps, типы работ, связи, notice dispositions и различает `bound`, `missing` и
`explained_unavailable` у report metrics. Повтор overview/body — только advisory, не truth verdict.

External research skill и новый `looplab-deep-research` требуют реальные primary sources, claims с
ссылками, сопоставление протоколов и следующий различающий эксперимент; technical retry не требует
фальшивой литературы. Задокументированы один staged fit→score, различия `job_role`/`node_kind`,
`question_card_id`/`parent_card_id`/`parent_ids`/`uses`, разные shared/run-local lessons и отчёты
без механических дублей. При `auto` в run и project briefing консервативно выбирает язык по goal;
явные настройки важнее, исторические записи не переводятся.

Проверка: 390 backend-тестов, 44 целевых UI-теста, production UI build и strict MkDocs прошли;
полный UI-набор — 2237 passed / 0 failed с `--test-concurrency=4`.
Production runtime, healthy fit,
исходные scorer/settings и живые jobs не изменялись. Сохранённый live journal использовался только
для read-only проверки: timestamps читаются, старые `job_role` остаются unknown, текущий отчёт
получает `explained_unavailable` из собственного объяснения отсутствия native scalar.

### 7.4. Инциденты исполнения и загрузки: сверка 10 ноября

10 ноября обнаружены три отдельных остатка в исходниках `afbc48d` и инцидентах на `7e084af`.
Ошибки пользовательских publisher, API-вызовов и толкования поручения сюда не включены.

- **Exact pending-inject cancellation:** локальная реализация 11 ноября отменяет только точный
  unmaterialized head по original command, queue index/digest и run UID/generation, при доказанно
  остановленном владельце. [Приёмка](audit/pending-inject-cancellation.md) проверяет public API/MCP,
  atomic journal batch, потерянный ACK, torn publication и fencing старого worker до respawn. Это
  не обновление live runtime и не разрешение запускать или останавливать neural work.

- **Completed evaluation и advisory review:** локальная реализация 11 ноября сохраняет исходное
  наблюдение с `ended_before_decision`/`unreviewed` и `answer=null`, затем фиксирует execution terminal.
  [Приёмка](audit/completed-eval-advisory-monitor.md) проверяет реальные CPU команды, неизвестный result,
  гонки публикации и lifecycle fences. Stage/deadline gates сохраняются; live deployment не выполнялся.

- **Per-query metadata preflight:** реализован и проверен в локальной ветке реализации 11 ноября.
  [Приёмка](audit/per-query-upload-preflight.md) содержит HTTP/MCP regressions, exact replay и attempt
  race; `no_measured_value` сохранён. Это не утверждение о публикации или обновлении live runtime.

## 8. Что делать с уже существующими OPEN

Новые пункты не отменяют старый индекс:

- **Evaluation и рецензирование:** `metric-behavior-thresholds-unmeasured` сохраняет calibration нового numerical observer,
  `report-review-nominates-no-experiment` — за недостающие confirm/ablation/comparison. Evaluation
  Faithfulness связывает их с исходным научным вопросом, а не дублирует.
- **Разнообразие и память:** QD-Rec полезен для `board-rotation-by-attempt-only`,
  `embedding-novelty-gate-declined-on-one-incident`, `belief-identity-text-keyed` и
  `concurrent-runs-share-no-work-claims`. Сначала измерить последствия, не включать semantic novelty по
  одной красивой статье.
- **Переиспользуемые инструменты:** Hyra подкрепляет `skills-have-no-external-source`, но любой новый
  skill проходит execution, contamination и scope checks. Existing upstream lane остаётся местом переноса
  проверенных возможностей.
- **Язык и отчёты:** production подтверждает важность `external-russian-readability` и
  `external-report-presentation`. В их приёмку должны входить не только экраны UI, но и authored next
  actions, directions и claims. Слитые слова исправляет автор, не механический перевод измерений.
- **Benchmarks:** harness evaluation снабжает данными `research-grade-profile-is-not-the-default`,
  `smoke-full-rank-fidelity-unmeasured` и `no-research-lifecycle-benchmark-number`; defaults не
  переключаются до собственных измерений.
- **Distributed:** `no-cluster-executor` и `eval-parallelism-is-in-process-only` не входят в ближайший
  приоритет. Улучшение контекста, evidence и harness не требует новой распределённой инфраструктуры.

## 9. Порядок реализации

1. **Корректность без смены поведения:** роль ноды, connection summary, проекция notice disposition.
   Тесты replay, HTTP/MCP и production-sized fixtures; живой runtime не переключать.
2. **Contracts и пассивные receipts:** Evaluation Contract, Harness Revision, external usage/work state.
   Сначала чтение и заполнение на новых runs, без обратного переписывания истории.
3. **Evidence и текущий контекст:** remediation graph, structured directions, bounded projections.
   Проверить recovery после compaction и ручную смену приоритета.
4. **Shadow harness benchmark:** одна базовая и одна изменённая revision, paired tasks, keep-current.
   Никакого автоматического adoption.
5. **Knowledge и acceleration:** outcome-измерения памяти и продольный Research Ledger. Только после
   появления телеметрии и сопоставимых кампаний принимать решения о defaults.

## 10. Чего не делать

- Не добавлять агентов и стадии ради количества; проблему не решает «20 ролей вместо 3».
- Не переписывать prompt или shared KB после каждого run без paired evidence и отдельной авторизации.
- Не считать LLM judge, citation coverage или заполненные поля доказательством научной истины.
- Не требовать sources, novelty или новую research question для technical retry.
- Не выбрасывать отрицательные и разнообразные направления только потому, что они не стали champion.
- Не выдавать ack за применение, quiet за работу, ускорение inference за ускорение исследования и
  совпадение чисел за совпадение протоколов.
- Не хранить сырые chain-of-thought, credentials и полные transcripts ради observability.
- Не строить параллельный scheduler, GPU dispatcher, report store или research-memory spine поверх
  существующих механизмов.
