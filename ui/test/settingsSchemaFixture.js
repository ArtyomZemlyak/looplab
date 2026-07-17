import { readFileSync } from 'node:fs'
import { validateSettingsSchema } from '../src/settingsSchema.js'

export const RAW_SETTINGS_SCHEMA = JSON.parse(readFileSync(
  new URL('../../looplab/serve/settings_ui_schema.json', import.meta.url), 'utf8'))

export const SETTINGS_SCHEMA = validateSettingsSchema({
  ...RAW_SETTINGS_SCHEMA,
  revision: '0'.repeat(64),
})

export const SETTINGS_GROUPS = SETTINGS_SCHEMA.groups
export const FIELD_BY_KEY = SETTINGS_SCHEMA.fieldByKey
