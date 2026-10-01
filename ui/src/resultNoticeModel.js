const token = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
const metric = value => value === null || typeof value === 'number' && Number.isFinite(value)
const integer = value => Number.isSafeInteger(value) && value >= 0

export function validResultNotices(value, generation) {
  if (value?.version !== 1 || value.generation !== generation || !token(generation)
      || !integer(value.total) || !Array.isArray(value.items) || value.items.length > 200
      || value.total < value.items.length || typeof value.has_more !== 'boolean') return false
  const ids = new Set()
  return value.items.every(row => {
    if (!row || ids.has(row.id) || !token(row.evidence_token) || !metric(row.score)
        || !metric(row.confirmed_mean) || !['min', 'max'].includes(row.direction)
        || typeof row.objective !== 'string' || row.objective.length > 256
        || !(row.commentary === null || typeof row.commentary === 'string' && row.commentary.length <= 700)) return false
    ids.add(row.id)
    if (row.kind === 'run') return row.id === 'run' && row.status === 'finished'
      && integer(row.evaluated) && integer(row.failed)
      && (row.selected_node === null || integer(row.selected_node) && integer(row.attempt))
      && Array.isArray(row.caveats) && row.caveats.every(c => typeof c === 'string')
    return row.kind === 'node' && integer(row.node_id) && integer(row.attempt)
      && row.id === `node:${row.node_id}:${row.attempt}`
      && ['evaluated', 'failed', 'aborted'].includes(row.status)
      && typeof row.feasible === 'boolean' && typeof row.trust_flagged === 'boolean' && typeof row.salvaged === 'boolean' && integer(row.violations)
      && typeof row.failure === 'string' && row.failure.length <= 160
      && Array.isArray(row.parents) && row.parents.length <= 8
      && row.parents.every(p => integer(p.node_id) && integer(p.attempt) && metric(p.score)
        && ['same', 'different', 'unknown'].includes(p.comparability))
  })
}

export function resultNoticeText(row, language = 'en') {
  const ru = language === 'ru'
  const number = value => new Intl.NumberFormat(ru ? 'ru' : 'en', { maximumSignificantDigits: 6 }).format(value)
  const score = row.confirmed_mean ?? row.score
  const label = row.confirmed_mean !== null ? ru ? 'среднее повторных запусков' : 'confirmation mean' : ru ? 'оценка' : 'score'
  const direction = row.direction === 'min' ? ru ? 'меньше лучше' : 'lower is better' : ru ? 'больше лучше' : 'higher is better'
  let title, outcome, next
  if (row.kind === 'run') {
    title = ru ? 'Запуск завершён' : 'Run finished'
    outcome = ru ? `Оценено: ${row.evaluated}; ошибок: ${row.failed}.`
      : `${row.evaluated} evaluated; ${row.failed} failed.`
    if (row.selected_node !== null && score !== null) outcome += ru
      ? ` Выбран #${row.selected_node}: ${label} ${number(score)} (${direction}).`
      : ` Selected #${row.selected_node}: ${label} ${number(score)} (${direction}).`
    else outcome += ru ? ' Допустимый результат не выбран.' : 'No eligible result selected.'
    if (row.reason) outcome += ru ? ` Причина остановки: ${{ aborted: 'завершено вручную', done: 'задача завершена', error: 'ошибка', budget: 'лимит ресурсов' }[row.reason] || row.reason}.` : ` Stop reason: ${row.reason}.`
    next = ru ? 'Откройте Report: итог, ограничения и файлы решения.' : 'Open Report for the result, caveats and solution files.'
  } else {
    title = ru ? `Эксперимент #${row.node_id} · попытка ${row.attempt}` : `Experiment #${row.node_id} · attempt ${row.attempt}`
    const failure = ru ? ({ crash: 'ошибка выполнения команды', timeout: 'время выполнения истекло' }[row.failure] || row.failure) : row.failure
    outcome = row.status === 'aborted' ? ru ? 'Остановлен. Завершённого результата нет.' : 'Aborted. No completed result.'
      : row.status === 'failed' ? ru ? `Оценка завершилась ошибкой${failure ? ': ' + failure : '.'}`
        : `Evaluation failed${row.failure ? ': ' + row.failure : '.'}`
      : score === null ? ru ? 'Оценка завершена без пригодной метрики.' : 'Evaluation ended without a usable metric.'
      : `${ru && row.objective === 'task metric' ? 'Метрика задачи' : row.objective}: ${label} ${number(score)} (${direction}).`
    if (row.status === 'evaluated' && (!row.feasible || row.violations > 0 || row.trust_flagged)) outcome += ru
      ? ' Есть ограничения или сигналы Trust; проверьте допустимость.' : 'Constraints or Trust signals recorded; check eligibility.'
    if (row.salvaged && row.score !== null) outcome += ru
      ? ' Метрика восстановлена после ошибки; проверьте источник.' : 'Metric recovered after failure; review provenance.'
    const parent = row.parents.length === 1 ? row.parents[0] : null
    if (row.status === 'evaluated' && parent && row.score !== null && parent.score !== null) {
      if (parent.comparability === 'same' && row.feasible && !row.trust_flagged && !row.salvaged && row.violations === 0) {
        const gain = (row.score - parent.score) * (row.direction === 'min' ? -1 : 1)
        outcome += ru ? ` Оценка ${gain === 0 ? 'такая же, как' : gain > 0 ? 'лучше, чем' : 'хуже, чем'} у родителя #${parent.node_id} (${number(parent.score)}).`
          : `Score ${gain === 0 ? 'ties' : gain > 0 ? 'improves on' : 'is worse than'} parent #${parent.node_id} (${number(parent.score)}).`
      } else outcome += ru ? ` Родитель #${parent.node_id}: оценка ${number(parent.score)}; улучшение не установлено.`
        : `Parent #${parent.node_id}: score ${number(parent.score)}; improvement not established.`
    }
    next = row.status === 'failed' ? ru ? 'Откройте Trace и логи перед исправлением.' : 'Open Trace and logs before repairing.'
      : ru ? 'Откройте Metrics и Trust; следующий эксперимент выбирает агент.' : 'Review Metrics and Trust; the agent chooses the next experiment.'
  }
  const caution = score !== null && row.status !== 'aborted' ? row.confirmed_mean === null
    ? ru ? 'Нет подтверждения повторными запусками с разной случайной инициализацией; результат предварительный.' : 'No multi-seed confirmation; exploratory result.'
    : Number.isSafeInteger(row.confirmed_seeds) && row.confirmed_seeds >= 2
      ? ru ? `Успешных повторных запусков: ${row.confirmed_seeds}; проверьте разброс оценок.` : `${row.confirmed_seeds} confirmation seeds; check spread.`
      : ru ? 'Среднее записано; несколько успешных повторных запусков не подтверждены.' : 'Mean recorded; multiple successful seeds not established.' : ''
  return { title, outcome, caution, next }
}

export function resultNoticeQuestion(row, language = 'en') {
  const ru = language === 'ru'
  const target = row.kind === 'run' ? ru ? 'итог этого запуска' : 'this run’s result'
    : ru ? `эксперимент #${row.node_id}, попытку ${row.attempt}` : `experiment #${row.node_id}, attempt ${row.attempt}`
  if (row.status === 'failed') return ru
    ? `Разбери ${target}: прочитай Trace и логи этой попытки, объясни причину ошибки и предложи минимальное исправление. Отдели подтверждённые факты от предположений. Не запускай новые эксперименты и не меняй настройки.`
    : `Explain ${target}: read this attempt’s Trace and logs, identify the failure cause, and propose a minimal repair. Separate recorded facts from assumptions. Do not start experiments or change settings.`
  return ru
    ? `Разбери ${target}: что измерено, что изменилось относительно родителя, насколько надёжен результат и что делать дальше. Сначала прочитай фактические данные этой попытки и ограничения. Не запускай новые эксперименты и не меняй настройки.`
    : `Explain ${target}: what was measured, what changed relative to the parent, how reliable the result is, and what to do next. Read the recorded evidence for this attempt and its caveats first. Do not start experiments or change settings.`
}
