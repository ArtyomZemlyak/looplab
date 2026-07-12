// Pure view-model helpers for the settings UI. Keeping filtering here makes the
// progressive-disclosure rules testable without React or a browser.

export const ESSENTIAL_SETTING_KEYS = new Set([
  'profile',
  'policy',
  'max_nodes',
  'n_seeds',
  'max_parallel',
  'max_seconds',
  'max_eval_seconds',
  'timeout',
  'backend',
  'llm_model',
  'llm_base_url',
  'llm_api_key',
  'unified_agent',
  'agent_max_turns',
  'trust_mode',
  'require_approval',
  'redact_output',
])

const searchableText = (group, field) => [
  group.title,
  group.sub,
  field.key,
  field.label,
  field.help,
  field.placeholder,
  ...(field.options || []),
].filter(Boolean).join(' ').toLocaleLowerCase()

export function normalizeSettingsQuery(query) {
  return String(query || '').trim().toLocaleLowerCase()
}

// Search intentionally spans the complete catalogue even while the Essential
// view is selected. A search box that silently hides advanced matches is much
// harder to trust; clearing the query returns to the selected disclosure mode.
export function filterSettingsGroups(groups, {
  mode = 'all', query = '', only, hideSecret = false,
} = {}) {
  const needle = normalizeSettingsQuery(query)
  const allowedGroups = only ? new Set(only) : null

  return groups
    .filter(group => !allowedGroups || allowedGroups.has(group.title))
    .map(group => ({
      ...group,
      fields: group.fields.filter(field => {
        if (hideSecret && field.type === 'secret') return false
        if (needle) return searchableText(group, field).includes(needle)
        return mode !== 'essential' || ESSENTIAL_SETTING_KEYS.has(field.key)
      }),
    }))
    .filter(group => group.fields.length > 0)
}

export function settingsViewStats(groups) {
  return {
    groups: groups.length,
    fields: groups.reduce((total, group) => total + group.fields.length, 0),
    keys: new Set(groups.flatMap(group => group.fields.map(field => field.key))),
  }
}
