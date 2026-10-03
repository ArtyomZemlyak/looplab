import test from 'node:test'
import assert from 'node:assert/strict'
import { launchCostSummary } from '../src/launchCostModel.js'
import { LAUNCH_RUNTIME_FIELDS, createLaunchDraft, updateRuntimeValue, validateLaunchDraft } from '../src/launchDraft.js'
import ru from '../src/launchCardRussian.js'

test('resolved run LLM budget uses the tighter positive declaration, with zero disabling only its own field', () => {
  for (const [budget, legacy, limit] of [
    [0, 0, null], [2, 0, 2], [0, 3, 3], [2, 3, 2], [3, 2, 2], [2, 2, 2], [0.00001, 1, 0.00001],
  ]) {
    const result = launchCostSummary({ backend: 'llm', llm_budget_usd: budget, llm_cost_limit: legacy }, true)
    assert.equal(result.limit, limit)
    assert.equal(result.status, limit === null ? 'unlimited' : 'limited')
    if (limit !== null) {
      assert.equal(result.budget, `$${limit}`)
      assert.doesNotMatch(result.budget, /No run LLM limit/)
    }
  }
})

test('draft, missing and invalid budget evidence never claims an effective amount or no limit', () => {
  const draft = launchCostSummary({ backend: 'toy', llm_budget_usd: 2, llm_cost_limit: 0 }, false)
  assert.equal(draft.status, 'unchecked')
  assert.equal(draft.limit, null)
  assert.match(draft.budget, /Validate.*inherited/)
  for (const settings of [null, {}, { llm_budget_usd: 2 }, { llm_cost_limit: 0 },
    ...[null, undefined, '0', true, -1, NaN, Infinity].map(value => ({ llm_budget_usd: 2, llm_cost_limit: value }))]) {
    const result = launchCostSummary(settings, true)
    assert.equal(result.status, 'unknown')
    assert.equal(result.limit, null)
    assert.match(result.budget, /unavailable/)
    assert.doesNotMatch(result.budget, /No run LLM limit/)
  }
})

test('toy describes built-in calls and keeps external client, chat and compute costs visible in both languages', () => {
  for (const translate of [value => value, ru]) {
    const result = launchCostSummary({ backend: 'toy', external_harness: true,
      llm_budget_usd: 1, llm_cost_limit: 0 }, true, translate)
    assert.equal(result.budget, '$1')
    assert.match(result.backend, /built-in|Встроенный/)
    assert.match(result.scope, /Assistant chat|чат ассистента/)
    assert.match(result.scope, /external-client|внешнего клиента/)
    assert.match(result.scope, /compute|вычисления/)
    assert.match(result.accounting, /Unpriced|Без цен/)
    assert.match(result.accounting, /exceed the limit|перерасход/)
  }
})

test('the budget shortcut preserves advanced settings and accepts fractional USD rather than silently clearing the other cap', () => {
  const field = LAUNCH_RUNTIME_FIELDS.find(row => row.key === 'llm_budget_usd')
  const draft = createLaunchDraft({ run_id: 'budget-demo', task: { kind: 'quadratic' },
    settings: { llm_cost_limit: 3, other: { retained: true } } })
  const changed = updateRuntimeValue(draft, field, '0.25')
  assert.equal(changed.ok, true)
  assert.equal(validateLaunchDraft(changed.draft).ok, true)
  assert.deepEqual(JSON.parse(changed.draft.settings_json), {
    llm_cost_limit: 3, other: { retained: true }, llm_budget_usd: 0.25,
  })
  const invalid = updateRuntimeValue(draft, field, '-1')
  assert.match(validateLaunchDraft(invalid.draft).errors['settings.llm_budget_usd'], /0/)
})
