// Shared wording for recorded values; repeat counts never certify generalization or Trust.
export function resultMeasurement(confirmed, seeds, language = 'en') {
  const ru = language === 'ru'
  const repeated = confirmed && Number.isSafeInteger(seeds) && seeds >= 2
  return {
    label: confirmed ? ru ? 'среднее повторных запусков' : 'confirmation mean'
      : ru ? 'основная оценка' : 'evaluation score',
    reliability: !confirmed ? ru ? 'Нет подтверждения повторными запусками; результат предварительный.'
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
  return resultMeasurement(run.best_confirmed != null, bound ? selected.seeds : null)
}
