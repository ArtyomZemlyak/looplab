export const MODEL_CHECK_EVENT = 'll:model-check'

const revision = value => typeof value === 'string' && value.length > 0 && value.length <= 256
const outcomes = new Set(['passed', 'failed', 'unknown'])
let latestCheck = null

export const readModelCheck = () => latestCheck

export function publishModelCheck(settingsRevision, secretRevision, outcome) {
  if (!revision(settingsRevision) || !revision(secretRevision) || !outcomes.has(outcome)) return
  latestCheck = { settingsRevision, secretRevision, outcome }
  window.dispatchEvent(new CustomEvent(MODEL_CHECK_EVENT, {
    detail: latestCheck,
  }))
}

// Settings saved new revisions: a check made against OTHER settings no longer describes them.
// `modelConnectionView` already compares revisions; `paidActionModelNote` is handed the bare check,
// so a failed check survived a fixed key ("the last connection check failed") and a passed one a
// cleared key (code review). Dropping it here keeps both readers on the current settings.
export function noteSettingsRevisions(settingsRevision, secretRevision) {
  if (!latestCheck || !revision(settingsRevision) || !revision(secretRevision)) return
  if (latestCheck.settingsRevision === settingsRevision
      && latestCheck.secretRevision === secretRevision) return
  latestCheck = null
  window.dispatchEvent(new CustomEvent(MODEL_CHECK_EVENT, { detail: null }))
}

// What a PAID action must say first when a model is not known to be there (doc 75 UX-16), or ''.
// The offline demo's Report offered "Refresh report · paid" and "provider charges may apply" with no
// model anywhere, and every such click ended `provider_unavailable`. "Connection unverified" alone is
// NOT the condition — it is also the ordinary state of a user with a working model who never pressed
// "Check connection" — so the note fires on a failed check, or on a run that used no model
// (backend=toy) with no passed check in this tab.
export function paidActionModelNote(runBackend, check) {
  if (check?.outcome === 'failed') return 'Needs a model: the last connection check failed. Review Settings → Model.'
  if (runBackend === 'toy' && check?.outcome !== 'passed')
    return 'Needs a model: this run used none (backend=toy). Check the connection in Settings → Model first.'
  return ''
}

export function modelConnectionView(snapshot, check, language = 'auto') {
  const text = (en, ru) => language === 'ru' ? ru : en
  if (snapshot === null) return { tone: '', text: text('Loading saved model settings…', 'Читаем настройки модели…') }
  const settings = snapshot?.settings
  if (!settings) return { tone: 'warn', text: text('Could not read saved model settings.', 'Не удалось прочитать настройки модели.') }
  const model = typeof settings.llm_model === 'string' ? settings.llm_model.trim() : ''
  const endpoint = typeof settings.llm_base_url === 'string' ? settings.llm_base_url.trim() : ''
  if (!model || !endpoint) return { tone: 'warn', text: text('Set a model and endpoint before chatting.', 'Перед общением укажите модель и адрес сервера модели.') }
  const name = model.slice(0, 80)
  const current = revision(snapshot.settings_revision) && revision(snapshot.secret_revision)
    && check?.settingsRevision === snapshot.settings_revision
    && check?.secretRevision === snapshot.secret_revision
  if (current && check.outcome === 'passed') return { tone: 'ok', text: `${name} · ${text('Last explicit test passed.', 'Последняя явная проверка связи пройдена.')}` }
  if (current && check.outcome === 'failed') return { tone: 'warn', text: `${name} · ${text('Last test failed. Review Settings.', 'Проверка связи не прошла. Откройте настройки.')}` }
  if (current && check.outcome === 'unknown') return { tone: 'warn', text: `${name} · ${text('Test outcome unknown. Review Settings.', 'Результат проверки неизвестен. Откройте настройки.')}` }
  if (['incomplete', 'unbound', 'endpoint_mismatch'].includes(snapshot.credential?.status)) {
    return { tone: 'warn', text: `${name} · ${text('Shared key needs attention.', 'Проверьте общий API-ключ в настройках.')}` }
  }
  return { tone: '', text: `${name} · ${text('Connection unverified.', 'Связь с моделью ещё не проверена.')}` }
}
