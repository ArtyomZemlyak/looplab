import { repeatChecksNotApplicable, resultMeasurement, resultSpreadText } from './resultMeasurement.js'

const token = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
const metric = value => value === null || typeof value === 'number' && Number.isFinite(value)
const integer = value => Number.isSafeInteger(value) && value >= 0

function validComparison(row) {
  const c = row.score_comparison, parents = row.parents
  if (!Array.isArray(parents) || parents.length > 8
      || parents.some(p => !p || !integer(p.node_id) || p.node_id === row.node_id
        || !integer(p.attempt) || !metric(p.score) || !['same', 'different', 'unknown'].includes(p.comparability))
      || new Set(parents.map(p => p.node_id)).size !== parents.length) return false
  if (c?.version !== 1 || !integer(c.parent_count) || c.parent_count < parents.length) return false
  if (c.status === 'no_parent') return c.parent_count === 0
  if (c.status === 'multiple_parents') return c.parent_count > 1
  if (c.parent_count !== 1) return false
  if (c.status === 'parent_unavailable') return parents.length === 0
  if (parents.length !== 1 || !['same', 'different', 'unknown', 'base_different', 'base_unknown', 'ineligible', 'retargeted'].includes(c.status)) return false
  return c.status !== 'same' || row.status === 'evaluated' && row.feasible && !row.trust_flagged
    && !row.salvaged && row.violations === 0 && Number.isFinite(row.score)
    && Number.isFinite(parents[0].score) && Number.isFinite(row.score - parents[0].score)
    && parents[0].comparability === 'same'
}

const COMPARISON_HELP = {
  no_parent: ['This experiment has no parent; improvement is not established.', 'У эксперимента нет родителя; улучшение не установлено.'],
  parent_unavailable: ['The recorded parent attempt is unavailable; its newer result cannot replace it.', 'Записанная попытка родителя недоступна; её нельзя заменить новым результатом.'],
  base_different: ['Copied code bases differ.', 'Базы кода экспериментов отличаются.'],
  base_unknown: ['Recorded code bases cannot be matched.', 'Совпадение баз кода не подтверждено.'],
  retargeted: ['The objective changed; re-evaluation is needed.', 'Цель оценки изменена; нужна повторная оценка.'],
  ineligible: ["Check both experiments' eligibility and metric source.", 'Проверьте допустимость и источник оценки обоих экспериментов.'],
}

const CAVEAT_TEXT = {
  salvaged: ['metric salvaged after a failed evaluation', 'метрика восстановлена после неудачной оценки'],
  trust_flagged: ['possible data leakage or reward hacking', 'есть сигнал утечки данных или обхода оценки'],
  params_overridden: ['executed parameters differ from the experiment record', 'фактические параметры отличаются от записи эксперимента'],
  mixed_comparability: ['evaluation conditions differ; rankings may not be comparable', 'условия оценки отличаются; порядок результатов может быть несопоставим'],
  merged_coordinates: ['weights were averaged; declared parameters were not trained as a separate configuration', 'веса усреднены; указанные параметры не обучались как отдельная конфигурация'],
  retargeted_objective: ['the target metric was changed', 'целевая метрика изменена'],
  stale_artifact: ['measured on an artifact that has been re-produced since', 'измерено на артефакте, который с тех пор пересобран'],
}
export function resultCaveatText(code, language = 'en') {
  return CAVEAT_TEXT[code]?.[language === 'ru' ? 1 : 0]
    || (language === 'ru' ? `Неизвестное ограничение: ${code}` : `Unrecognized caveat: ${code}`)
}


export const resultTrustAdvisoryText = (language = 'en') => language === 'ru'
  ? 'Есть предупреждение о надёжности этой попытки; оно не исключает результат из отбора. Проверьте раздел «Надёжность» перед продолжением.'
  : 'A Trust warning is recorded for this attempt; it does not exclude the result from selection. Review Trust before continuing.'
export function validResultNotices(value, generation, cursor = null, limit = 200) {
  if (value?.version !== 1 || value.generation !== generation || !token(generation)
      || !integer(value.total) || !Array.isArray(value.items) || value.items.length > limit
      || value.total < value.items.length || typeof value.has_more !== 'boolean') return false
  if (cursor ? value.total <= value.items.length
    : value.has_more !== (value.total > value.items.length)) return false
  if (!Object.hasOwn(value, 'next_cursor')) return false
  if (value.has_more) {
    const next = typeof value.next_cursor === 'string'
      && /^rn1\.([0-9a-f]{64})\.(run|node:[0-9]+:[0-9]+)\.([0-9a-f]{64})$/.exec(value.next_cursor)
    const first = value.items[0]
    if (!next || !first || next[2] !== first.id || next[3] !== first.evidence_token
        || value.total <= value.items.length || value.next_cursor === cursor) return false
  } else if (value.next_cursor !== null) return false
  if (cursor) {
    const scope = cursor.split('.')[1]
    if (value.next_cursor && value.next_cursor.split('.')[1] !== scope
        || value.items.some(row => row && cursor === `rn1.${scope}.${row.id}.${row.evidence_token}`)) return false
  }
  const ids = new Set()
  return value.items.every(row => {
    if (!row || ids.has(row.id) || !token(row.evidence_token) || !metric(row.score)
        || !metric(row.confirmed_mean) || !metric(row.confirmed_std)
        || row.confirmed_std !== null && (row.confirmed_std < 0 || row.confirmed_mean === null)
        || !['min', 'max'].includes(row.direction)
        || typeof row.objective !== 'string' || row.objective.length > 256
        || Object.hasOwn(row, 'completed_at') && row.completed_at !== null
          && !(typeof row.completed_at === 'number' && Number.isFinite(row.completed_at) && row.completed_at >= 0)
        || Object.hasOwn(row, 'commentary_source') && ![null, 'assistant', 'external'].includes(row.commentary_source)
        || Object.hasOwn(row, 'commentary_status') && !['none', 'generating', 'ready', 'published', 'failed', 'interrupted', 'superseded', 'unavailable'].includes(row.commentary_status)
        || !(row.commentary === null || typeof row.commentary === 'string' && row.commentary.length <= 700)) return false
    ids.add(row.id)
    if (row.kind === 'run') return row.id === 'run' && row.status === 'finished'
      && integer(row.evaluated) && integer(row.failed)
      && typeof row.trust_advisory === 'boolean'
      && (row.selected_node === null && row.attempt === null && row.score === null
          && row.confirmed_mean === null && !row.trust_advisory
        || integer(row.selected_node) && integer(row.attempt) && Number.isFinite(row.score))
      && Array.isArray(row.caveats) && row.caveats.every(c => typeof c === 'string')
      && (!row.caveats.includes('trust_flagged') || row.trust_advisory)
    return row.kind === 'node' && integer(row.node_id) && integer(row.attempt)
      && row.id === `node:${row.node_id}:${row.attempt}`
      && ['evaluated', 'failed', 'aborted'].includes(row.status)
      && (row.status === 'evaluated' || row.score === null && row.confirmed_mean === null && row.confirmed_std === null)
      && typeof row.feasible === 'boolean' && typeof row.trust_flagged === 'boolean' && typeof row.salvaged === 'boolean' && integer(row.violations)
      && typeof row.trust_advisory === 'boolean' && typeof row.parent_trust_advisory === 'boolean'
      && !(row.trust_flagged && row.trust_advisory) && (!row.parent_trust_advisory || row.parents?.length > 0)
      && typeof row.failure === 'string' && row.failure.length <= 160
      && validComparison(row)
  })
}

export function resultNoticeText(row, language = 'en') {
  const ru = language === 'ru'
  const number = value => new Intl.NumberFormat(ru ? 'ru' : 'en', { maximumSignificantDigits: 6 }).format(value)
  const score = row.confirmed_mean ?? row.score
  const measurement = resultMeasurement(row.confirmed_mean !== null, row.confirmed_seeds, language,
    repeatChecksNotApplicable(row))
  const label = measurement.label
  const direction = row.direction === 'min' ? ru ? 'меньше лучше' : 'lower is better' : ru ? 'больше лучше' : 'higher is better'
  let title, outcome, next, comparison = ''
  const stateLabel = row.kind === 'run' ? ru ? 'Завершён' : 'Finished'
    : row.status === 'failed' ? ru ? 'Ошибка' : 'Failed'
    : row.status === 'aborted' ? ru ? 'Остановлен' : 'Stopped'
    : score === null ? ru ? 'Нет метрики' : 'No metric'
    : row.salvaged ? ru ? 'Восстановлено' : 'Recovered' : ru ? 'Измерено' : 'Measured'
  const actionLabel = row.kind === 'run' ? ru ? 'Открыть отчёт' : 'Open Report'
    : ['failed', 'aborted'].includes(row.status) ? ru ? 'Открыть логи' : 'Open logs'
    : ru ? 'Открыть метрики' : 'Open metrics'
  if (row.kind === 'run') {
    title = ru ? 'Запуск завершён' : 'Run finished'
    outcome = ru ? `Оценено: ${row.evaluated}; ошибок: ${row.failed}.`
      : `${row.evaluated} evaluated; ${row.failed} failed.`
    if (row.selected_node !== null && score !== null) outcome += ru
      ? ` Выбран #${row.selected_node}: ${label} ${number(score)} (${direction}).`
      : ` Selected #${row.selected_node}: ${label} ${number(score)} (${direction}).`
    else outcome += ru ? ' Допустимый результат не выбран.' : ' No eligible result selected.'
    if (row.selected_node !== null && row.confirmed_mean !== null && row.score !== null) outcome += ru
      ? ` Основная оценка: ${number(row.score)}.` : ` Evaluation score: ${number(row.score)}.`
    if (row.reason) outcome += ru ? ` Причина остановки: ${{ aborted: 'завершено вручную', done: 'задача завершена', error: 'ошибка', budget: 'лимит ресурсов' }[row.reason] || row.reason}.` : ` Stop reason: ${row.reason}.`
    if (row.trust_advisory) outcome += ` ${resultTrustAdvisoryText(language)}`
    next = ru ? 'Откройте отчёт: итог, ограничения и файлы решения.' : 'Open Report for the result, caveats and solution files.'
  } else {
    title = ru ? `Эксперимент #${row.node_id} · попытка ${row.attempt}` : `Experiment #${row.node_id} · attempt ${row.attempt}`
    const failure = ru ? ({ crash: 'ошибка выполнения команды', timeout: 'время выполнения истекло' }[row.failure] || row.failure) : row.failure
    outcome = row.status === 'aborted' ? ru ? 'Остановлен. Завершённого результата нет.' : 'Aborted. No completed result.'
      : row.status === 'failed' ? ru ? `Оценка завершилась ошибкой${failure ? ': ' + failure : '.'}`
        : `Evaluation failed${row.failure ? ': ' + row.failure : '.'}`
      : score === null ? ru ? 'Оценка завершена без пригодной метрики.' : 'Evaluation ended without a usable metric.'
      : `${ru && row.objective === 'task metric' ? 'Метрика задачи' : row.objective}: ${label} ${number(score)} (${direction}).`
    if (row.status === 'evaluated' && row.confirmed_mean !== null && row.score !== null) outcome += ru
      ? ` Основная оценка: ${number(row.score)}.` : ` Evaluation score: ${number(row.score)}.`
    if (row.status === 'evaluated' && (!row.feasible || row.violations > 0)) outcome += ru
      ? ' Нарушены ограничения; проверьте допустимость.' : 'Constraints violated; check eligibility.'
    if (row.trust_flagged) outcome += ru
      ? ' Исключён из отбора политикой проверки надёжности.' : 'Excluded from selection by the Trust policy.'
    else if (row.trust_advisory) outcome += ` ${resultTrustAdvisoryText(language)}`
    if (row.salvaged && row.score !== null) outcome += ru
      ? ' Метрика восстановлена после ошибки; проверьте источник.' : 'Metric recovered after failure; review provenance.'
    const comparisonStatus = row.score_comparison?.status
    const help = COMPARISON_HELP[comparisonStatus]?.[ru ? 1 : 0]
    const parent = row.score_comparison?.parent_count === 1 && row.parents.length === 1 ? row.parents[0] : null
    const parentLabel = parent && `#${parent.node_id} · ${ru ? 'попытка' : 'attempt'} ${parent.attempt}`
    if (row.status === 'evaluated' && parent && row.score !== null && parent.score !== null) {
      if (comparisonStatus === 'same' && validComparison(row) && ['min', 'max'].includes(row.direction)) {
        const gain = (row.score - parent.score) * (row.direction === 'min' ? -1 : 1)
        comparison = ru ? `Основная оценка ${number(row.score)} ${gain === 0 ? 'такая же, как' : gain > 0 ? 'лучше, чем' : 'хуже, чем'} у исходного эксперимента ${parentLabel} (${number(parent.score)}).`
          : `Evaluation score ${number(row.score)} ${gain === 0 ? 'ties' : gain > 0 ? 'improves on' : 'is worse than'} parent ${parentLabel} (${number(parent.score)}).`
      } else {
        comparison = ru ? `Исходный эксперимент ${parentLabel}: оценка ${number(parent.score)}; улучшение не установлено.`
        : `Parent ${parentLabel}: score ${number(parent.score)}; improvement not established.`
        const reason = help ? ` ${help}` : parent.comparability === 'different'
          ? ru ? ' Условия оценки отличаются.' : ' Evaluation conditions differ.'
          : parent.comparability === 'unknown'
            ? ru ? ' Сопоставимость условий оценки не подтверждена.' : 'Comparable evaluation conditions are not established.'
            : ru ? ' Сначала проверьте ограничения и надёжность оценки.' : 'Review eligibility and metric provenance first.'
        comparison += reason
      }
      if (row.confirmed_mean !== null) comparison += ru
        ? ' Здесь сравниваются основные оценки, а не средние повторных запусков.'
        : ' This compares evaluation scores, not confirmation means.'
    } else if (row.status === 'evaluated' && score !== null) {
      comparison = row.score === null
        ? ru ? 'Основная оценка этой попытки отсутствует; сравнение с исходным экспериментом невозможно.' : 'This attempt has no evaluation score to compare with its parent.'
        : row.score_comparison?.parent_count > 1 || row.parents.length > 1
        ? ru ? 'Несколько исходных экспериментов. Единой оценки для сравнения нет.' : 'Multiple parents; no single comparison baseline.'
        : ru ? 'Нет пригодной оценки исходного эксперимента для сравнения.' : 'No usable parent score is available for comparison.'
      if (help) comparison += ` ${help}`
    }
    if (row.parent_trust_advisory) comparison += ru
      ? ' У исходного эксперимента есть предупреждение о надёжности; числовое сравнение не подтверждает надёжность результата.'
      : 'A parent has a Trust warning; the numeric comparison does not establish result reliability.'
    next = ['failed', 'aborted'].includes(row.status) ? ru ? 'Проверьте логи и причину остановки перед повторным запуском.' : 'Review logs and the stop cause before retrying.'
      : ru ? 'Откройте разделы «Метрики» и «Надёжность»; следующий эксперимент выбирает агент.' : 'Review Metrics and Trust; the agent chooses the next experiment.'
  }
  const caution = score !== null && row.status !== 'aborted' ? measurement.reliability
    + (row.confirmed_mean !== null ? ` ${resultSpreadText(row.confirmed_std, language)}` : '') : ''
  return { title, stateLabel, actionLabel, outcome, comparison, caution, next }
}

export function resultNoticeBrief(row, language = 'en') {
  const ru = language === 'ru', text = resultNoticeText(row, language)
  if (['failed', 'aborted'].includes(row.status)) return `${text.outcome} ${text.next}`
  const score = row.confirmed_mean ?? row.score
  const measurement = resultMeasurement(row.confirmed_mean !== null, row.confirmed_seeds, language,
    repeatChecksNotApplicable(row))
  const value = score === null ? ru ? 'пригодной метрики нет' : 'no usable metric'
    : `${measurement.label} ${new Intl.NumberFormat(ru ? 'ru' : 'en', { maximumSignificantDigits: 6 }).format(score)}`
  const head = row.kind === 'run'
    ? ru ? `Запуск завершён. ${row.selected_node === null ? 'Допустимый результат не выбран' : `Выбран эксперимент #${row.selected_node}: ${value}`}.`
      : `The run finished. ${row.selected_node === null ? 'No eligible result was selected' : `Experiment #${row.selected_node} was selected: ${value}`}.`
    : ru ? `Эксперимент #${row.node_id} завершён: ${value}.` : `Experiment #${row.node_id} finished: ${value}.`
  const comparison = row.kind === 'node' && score !== null
    ? row.score_comparison?.status === 'same' ? text.comparison
      : (row.score_comparison?.status === 'no_parent' ? '' : ru ? 'Улучшение пока не установлено. ' : 'Improvement is not established yet. ')
        + (COMPARISON_HELP[row.score_comparison?.status]?.[ru ? 1 : 0] || '') : ''
  const next = row.trust_flagged || row.trust_advisory || row.parent_trust_advisory || row.salvaged
      || row.feasible === false || row.violations > 0
    ? ru ? 'Перед продолжением нужно проверить ограничения и предупреждения оценки.' : 'Review evaluation caveats and warnings before continuing.'
    // A declared-deterministic objective has nothing to repeat (doc 75 UX-13): "check it with
    // repeat runs" was said of the offline demo's every experiment.
    : row.confirmed_mean === null && score !== null && !repeatChecksNotApplicable(row)
      ? ru ? 'Результат предварительный; дальше стоит проверить его повторными запусками.' : 'This is preliminary; the next step is to check it with repeat runs.'
      : row.kind === 'run' ? text.next : ru ? 'Дальше можно обсудить следующий эксперимент.' : 'We can discuss the next experiment now.'
  return [head, comparison, next].filter(Boolean).join(' ')
}

export function resultNoticeQuestion(row, language = 'en') {
  const ru = language === 'ru'
  const target = row.kind === 'run' ? ru ? 'итог этого запуска' : 'this run’s result'
    : ru ? `эксперимент #${row.node_id}, попытку ${row.attempt}` : `experiment #${row.node_id}, attempt ${row.attempt}`
  if (row.kind === 'run') return ru
    ? `Разбери ${target}: прочитай отчёт, объясни выбранный результат, ограничения и причину завершения. Сравни с первым пригодным результатом только при сопоставимых условиях; не смешивай основные оценки со средними повторных запусков. Предложи следующий шаг. Не запускай новые эксперименты и не меняй настройки.`
    : `Explain ${target}: read Report, explain the selected result, caveats and stop reason. Compare with the first eligible result only under comparable conditions; keep evaluation scores separate from confirmation means. Propose a next step. Do not start experiments or change settings.`
  if (row.status === 'failed') return ru
    ? `Разбери ${target}: прочитай историю выполнения и логи этой попытки, объясни причину ошибки и предложи минимальное исправление. Отдели подтверждённые факты от предположений. Не запускай новые эксперименты и не меняй настройки.`
    : `Explain ${target}: read this attempt’s Trace and logs, identify the failure cause, and propose a minimal repair. Separate recorded facts from assumptions. Do not start experiments or change settings.`
  if (row.status === 'aborted') return ru
    ? `Разбери ${target}: прочитай историю выполнения и логи этой попытки, объясни причину остановки и что нужно для повторного запуска. Завершённой метрики у этой попытки нет. Не запускай новые эксперименты и не меняй настройки.`
    : `Explain ${target}: read this attempt’s Trace and logs, explain why it stopped and what is needed to retry. This attempt has no completed metric. Do not start experiments or change settings.`
  return ru
    ? `Разбери ${target}: что измерено, что изменилось относительно родителя, насколько надёжен результат и что делать дальше. Сначала прочитай фактические данные этой попытки и ограничения. Не запускай новые эксперименты и не меняй настройки.`
    : `Explain ${target}: what was measured, what changed relative to the parent, how reliable the result is, and what to do next. Read the recorded evidence for this attempt and its caveats first. Do not start experiments or change settings.`
}
