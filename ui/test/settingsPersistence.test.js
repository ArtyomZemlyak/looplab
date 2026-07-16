import test from 'node:test'
import assert from 'node:assert/strict'
import {
  FIELD_BY_KEY,
  settingsSavePayload,
  toForm,
} from '../src/settingsSchema.js'

const CROSS_RUN_FLAGS = [
  'fingerprint_universal',
  'cross_run_concepts',
  'cross_run_read_tools',
  'cross_run_advisory',
  'cross_run_structured_claims',
  'cross_run_curation',
  'cross_run_curation_auto',
]

test('global Settings sends only controlled fields and never replays stale hidden values or secrets', () => {
  const loaded = {
    profile: 'default',
    future_engine_override: { mode: 'newer-server-value', budget: 7 },
    llm_api_key: '***',
    agent_control: { timeout: ['strategist'] },
  }
  const payload = settingsSavePayload(toForm(loaded), loaded.agent_control)

  assert.equal(Object.hasOwn(payload, 'future_engine_override'), false)
  assert.deepEqual(payload.agent_control, loaded.agent_control)
  assert.equal(Object.hasOwn(payload, 'llm_api_key'), false)
})

test('every Part IV/V flag is visible and round-trips its boolean value', () => {
  const baseline = Object.fromEntries(CROSS_RUN_FLAGS.map(key => [key, false]))
  baseline.future_engine_override = 'still-here'
  const form = toForm(baseline)
  for (const key of CROSS_RUN_FLAGS) form[key] = true

  const payload = settingsSavePayload(form, {})
  for (const key of CROSS_RUN_FLAGS) {
    assert.equal(FIELD_BY_KEY[key]?.type, 'bool', `${key} must be a visible boolean control`)
    assert.equal(payload[key], true, `${key} must round-trip through the form`)
  }
  assert.equal(Object.hasOwn(payload, 'future_engine_override'), false)
  assert.match(FIELD_BY_KEY.cross_run_concepts.help, /D8.*persist independently/i)
  assert.match(FIELD_BY_KEY.cross_run_curation_auto.warning, /never applies/i)
  assert.match(FIELD_BY_KEY.cross_run_curation_auto.help, /explicit operator/i)
})
