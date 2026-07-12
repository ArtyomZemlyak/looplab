import test from 'node:test'
import assert from 'node:assert/strict'
import { assistantErrorInfo, assistantPreview } from '../src/assistantErrors.js'

test('raw provider payload is normalized without leaking routing or user identifiers', () => {
  const raw = `(assistant error: LLM request to https://openrouter.ai/api/v1 failed: Error code: 429 - {'metadata': {'raw': 'openai/model:free is temporarily rate-limited', 'provider_name': 'Vendor'}, 'user_id': 'secret-user'})`
  const info = assistantErrorInfo(raw)
  assert.equal(info.kind, 'rate_limit')
  assert.equal(info.status, 429)
  const rendered = JSON.stringify(info)
  assert.doesNotMatch(rendered, /openrouter|openai\/model|Vendor|secret-user/)
  assert.equal(assistantPreview(raw), 'Assistant is temporarily rate-limited')
})

test('ordinary assistant prose is not treated as an error', () => {
  assert.equal(assistantErrorInfo('The experiment improved by 12%.'), null)
})

test('legacy persisted provider exceptions are normalized', () => {
  const cases = [
    ["Couldn't reach the model (AuthenticationError: secret-key for user-77)", 'credentials'],
    ['429 Client Error: https://provider.example/model/private-user', 'rate_limit'],
    ['Assistant error: connection timeout at https://provider.example', 'unavailable'],
  ]
  for (const [raw, kind] of cases) {
    const info = assistantErrorInfo(raw)
    assert.equal(info.kind, kind)
    assert.doesNotMatch(JSON.stringify(info), /secret-key|user-77|provider\.example|private-user/)
  }
})

test('structured error kind is authoritative and raw text stays out of render data', () => {
  const info = assistantErrorInfo('arbitrary sensitive exception body', 'provider_error')
  assert.equal(info.kind, 'provider_error')
  assert.doesNotMatch(JSON.stringify(info), /sensitive exception/)
})
