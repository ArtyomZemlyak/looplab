import test from 'node:test'
import assert from 'node:assert/strict'
import {
  SETTINGS_SCHEMA_VERSION, createSettingsSchemaLoader, validateSettingsSchema,
} from '../src/settingsSchema.js'
import { RAW_SETTINGS_SCHEMA } from './settingsSchemaFixture.js'

const payload = () => ({ ...structuredClone(RAW_SETTINGS_SCHEMA), revision: 'a'.repeat(64) })

test('packaged settings metadata validates as one bounded versioned contract without losing copy', () => {
  const schema = validateSettingsSchema(payload())
  assert.equal(schema.schema, SETTINGS_SCHEMA_VERSION)
  assert.equal(schema.groups.length, RAW_SETTINGS_SCHEMA.groups.length)
  assert.equal(Object.keys(schema.fieldByKey).length,
    RAW_SETTINGS_SCHEMA.groups.reduce((total, group) => total + group.fields.length, 0))
  assert.equal(Object.keys(schema.fieldByKey).length, 141)
  assert.equal(schema.fieldByKey.concept_pivot.type, 'bool')
  assert.equal(schema.fieldByKey.graded_novelty.type, 'bool')
  assert.equal(schema.fieldByKey.capability_expansion.type, 'bool')
  assert.match(schema.fieldByKey.cross_run_concepts.help, /D8.*persist independently/i)
  assert.match(schema.fieldByKey.cross_run_curation_auto.warning, /never applies/i)
  assert.equal(schema.agentRolePills.researcher.title,
    RAW_SETTINGS_SCHEMA.agent_role_pills.researcher.title)
  assert.ok(Object.isFrozen(schema) && Object.isFrozen(schema.groups)
    && Object.isFrozen(schema.fieldByKey.max_nodes))
})

test('settings metadata fails closed on version, revision, identity and role drift', () => {
  for (const mutate of [
    value => { value.schema += 1 },
    value => { value.revision = 'not-a-digest' },
    value => { value.groups[1].fields[0].key = value.groups[0].fields[0].key },
    value => { value.groups[0].fields[0].agents = ['unknown-role'] },
    value => { value.groups[0].fields[2].options = ['not-an-enum'] },
  ]) {
    const value = payload()
    mutate(value)
    assert.throws(() => validateSettingsSchema(value), /Invalid settings schema/)
  }
})

test('settings schema loader is single-flight and keeps one immutable validated revision', async () => {
  let finish
  let reads = 0
  const seen = []
  const loader = createSettingsSchemaLoader(options => {
    reads += 1
    seen.push(options)
    return new Promise(resolve => { finish = resolve })
  })
  const first = loader.load()
  const joined = loader.load({ reload: true })
  assert.equal(first, joined)
  assert.equal(reads, 0, 'the read starts in a microtask so same-tick callers can join')
  await Promise.resolve()
  assert.equal(reads, 1)
  finish(payload())
  const schema = await first
  assert.equal(await loader.load(), schema)
  assert.equal(loader.peek(), schema)
  assert.equal(reads, 1)
  assert.deepEqual(seen, [{ cache: 'default' }])
})

test('failed or malformed schema reads never populate cache and retry requests a reload', async () => {
  const seen = []
  let attempt = 0
  const loader = createSettingsSchemaLoader(options => {
    seen.push(options)
    attempt += 1
    if (attempt === 1) return Promise.reject(new Error('offline'))
    if (attempt === 2) return { ...payload(), revision: 'broken' }
    return payload()
  })
  await assert.rejects(loader.load(), /offline/)
  assert.equal(loader.peek(), null)
  await assert.rejects(loader.load({ reload: true }), /Invalid settings schema/)
  assert.equal(loader.peek(), null)
  const schema = await loader.load({ reload: true })
  assert.equal(schema.revision, 'a'.repeat(64))
  assert.deepEqual(seen.map(item => item.cache), ['default', 'reload', 'reload'])
})
