import { readFileSync } from 'node:fs'
import { SETTINGS_SCHEMA_VERSION, validateSettingsSchema } from '../src/settingsSchema.js'

const PACKAGED_SETTINGS_CATALOGUE = JSON.parse(readFileSync(
  new URL('../../looplab/serve/settings_ui_schema.json', import.meta.url), 'utf8'))

// Production adds these values from Settings.model_fields before serving v2. Keep representative
// bounds in this JS fixture so browser coercion is tested against the same HTTP contract shape.
const TEST_MODEL_BOUNDS = {
  max_nodes: { minimum: 1, maximum: 1_000_000 },
  n_seeds: { minimum: 1, maximum: 1024 },
  eval_parallel: { minimum: 0, maximum: 1024 },
  llm_parallel: { minimum: 0, maximum: 64 },
  speculation_depth: { minimum: 0, maximum: 64 },
  timeout: { exclusiveMinimum: 0 },
  max_eval_timeout: { exclusiveMinimum: 0, maximum: 86_400 },
  holdout_fraction: { minimum: 0, maximum: 0.9 },
  select_verifier_samples: { minimum: 1, maximum: 32 },
  concept_retag_every: { minimum: 1 },
}
const TEST_NULLABLE = new Set([
  'max_seconds', 'max_eval_seconds', 'memory_dir', 'agent_cmd', 'researcher_model',
  'developer_model', 'researcher_base_url', 'developer_base_url', 'researcher_temperature',
  'developer_temperature', 'strategist_temperature', 'compressor_model', 'compressor_base_url',
  'knowledge_dir', 'embed_model', 'embed_base_url', 'memora_cache', 'skills_dir', 'prompt_dir',
  'llm_api_key',
])

export const RAW_SETTINGS_SCHEMA = {
  ...PACKAGED_SETTINGS_CATALOGUE,
  schema: SETTINGS_SCHEMA_VERSION,
  groups: PACKAGED_SETTINGS_CATALOGUE.groups.map(group => ({
    ...group,
    fields: group.fields.map(field => ({
      ...field, nullable: TEST_NULLABLE.has(field.key), ...(TEST_MODEL_BOUNDS[field.key] || {}),
    })),
  })),
}

export const SETTINGS_SCHEMA = validateSettingsSchema({
  ...RAW_SETTINGS_SCHEMA,
  revision: '0'.repeat(64),
})

export const SETTINGS_GROUPS = SETTINGS_SCHEMA.groups
export const FIELD_BY_KEY = SETTINGS_SCHEMA.fieldByKey
