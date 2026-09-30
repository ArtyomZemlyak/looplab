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

export function modelConnectionView(snapshot, check) {
  if (snapshot === null) return { tone: '', text: 'Loading saved model settings…' }
  const settings = snapshot?.settings
  if (!settings) return { tone: 'warn', text: 'Could not read saved model settings.' }
  const model = typeof settings.llm_model === 'string' ? settings.llm_model.trim() : ''
  const endpoint = typeof settings.llm_base_url === 'string' ? settings.llm_base_url.trim() : ''
  if (!model || !endpoint) return { tone: 'warn', text: 'Set a model and endpoint before chatting.' }
  const name = model.slice(0, 80)
  const current = revision(snapshot.settings_revision) && revision(snapshot.secret_revision)
    && check?.settingsRevision === snapshot.settings_revision
    && check?.secretRevision === snapshot.secret_revision
  if (current && check.outcome === 'passed') return { tone: 'ok', text: `${name} · Last explicit test passed.` }
  if (current && check.outcome === 'failed') return { tone: 'warn', text: `${name} · Last test failed. Review Settings.` }
  if (current && check.outcome === 'unknown') return { tone: 'warn', text: `${name} · Test outcome unknown. Review Settings.` }
  if (['incomplete', 'unbound', 'endpoint_mismatch'].includes(snapshot.credential?.status)) {
    return { tone: 'warn', text: `${name} · Shared key needs attention.` }
  }
  return { tone: '', text: `${name} · Connection unverified.` }
}
