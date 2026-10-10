// Shared wording for recorded values; repeat counts never certify generalization or Trust.
export function resultSpreadText(value, language = 'en') {
  const ru = language === 'ru'
  if (!Number.isFinite(value) || value < 0)
    return ru ? 'Разброс не записан.' : 'Spread not recorded.'
  const number = new Intl.NumberFormat(ru ? 'ru' : 'en', { maximumSignificantDigits: 6 }).format(value)
  return ru ? `Разброс (std): ${number}.` : `Spread (std): ${number}.`
}

// `deterministic`: the task DECLARED that one evaluation is the score (`repeat_checks:
// "not_applicable"` on the node's comparability record, doc 75 UX-13) — the offline demo was told to
// repeat a closed-form objective with more seeds.
export const repeatChecksNotApplicable = record => record?.repeat_checks === 'not_applicable'
export const nodeRepeatChecksNotApplicable = node =>
  repeatChecksNotApplicable(node?.metric_provenance?.comparability)

export function resultMeasurement(confirmed, seeds, language = 'en', deterministic = false) {
  const ru = language === 'ru'
  const repeated = confirmed && Number.isSafeInteger(seeds) && seeds >= 2
  return {
    label: confirmed ? ru ? 'среднее повторных запусков' : 'confirmation mean'
      : ru ? 'основная оценка' : 'evaluation score',
    reliability: !confirmed && deterministic
      ? ru ? 'Детерминированная цель: одна оценка и есть результат; повторные проверки не нужны.'
        : 'Deterministic objective: one evaluation is the score; repeat checks are not needed.'
      : !confirmed ? ru ? 'Нет подтверждения повторными запусками; результат предварительный.'
      : 'No multi-seed confirmation; exploratory result.'
      : repeated ? ru ? `Повторных проверок: ${seeds}; проверьте разброс и условия.`
        : `${seeds} repeat checks; inspect spread and conditions.`
        : ru ? 'Среднее записано; несколько успешных повторов не подтверждены.'
          : 'Mean recorded; multiple successful repeats not established.',
  }
}

export function runMeasurement(run) {
  const selected = run.result_summary?.selected
  const bound = selected?.confirmed === true && selected.value === run.best_confirmed
    && Number.isSafeInteger(selected.node_id) && selected.node_id >= 0
    && Number.isSafeInteger(selected.attempt) && selected.attempt >= 0
  return resultMeasurement(run.best_confirmed != null, bound ? selected.seeds : null, 'en',
    repeatChecksNotApplicable(run.best_metric_comparability))
}
