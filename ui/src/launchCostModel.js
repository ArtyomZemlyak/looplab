// Review only the authoritative preflight settings. Draft overrides cannot tell
// us the inherited USD limit; an older/partial preview cannot prove there is none.
// Keep the tightest-positive rule aligned with core/llm_budget.py::run_usd_ceiling.
export function launchCostSummary(settings, validated, t = value => value) {
  const keys = ['llm_budget_usd', 'llm_cost_limit']
  const complete = validated && keys.every(key => Object.hasOwn(settings || {}, key)
    && typeof settings[key] === 'number' && Number.isFinite(settings[key]) && settings[key] >= 0)
  const caps = complete ? keys.map(key => settings[key]).filter(value => value > 0) : []
  const limit = caps.length ? Math.min(...caps) : null
  const status = !validated ? 'unchecked' : !complete ? 'unknown' : limit !== null ? 'limited' : 'unlimited'
  const budget = status === 'limited' ? `$${limit}`
    : status === 'unlimited' ? t('No run LLM limit configured.')
      : status === 'unknown' ? t('Run LLM limit unavailable; validate with the current server.')
        : t('Validate to resolve the inherited run LLM limit.')
  const backend = validated && settings?.backend === 'toy'
    ? t('Toy makes no built-in model/provider calls.')
    : t('LLM calls may incur provider cost.')
  return { status, limit, budget, backend,
    scope: t('This limit excludes Assistant chat, external-client models and experiment/upstream compute.'),
    accounting: t('Unpriced calls leave spend unknown; costs can exceed the limit before a call settles. This is not a provider bill or compute cap.') }
}
