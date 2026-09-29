// The concept frames the SERVER emits (tests/fixtures/concept_frame_cases.json, written by
// tests/test_concept_frame_contract.py) are frames this browser half ACCEPTS. Every other concept
// test hand-builds its frame, and none carried a derived co_occurs edge — so the receipt
// `source.edges >= included.edges` refused every run where two concepts share two experiments
// while all of them stayed green. And a refused frame must not read as an outage: a retry reads
// the same bytes.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { sharedVite } from './_mount.js'

const CASES = JSON.parse(readFileSync(
  new URL('../../tests/fixtures/concept_frame_cases.json', import.meta.url), 'utf8'))

test('every frame the server emits passes the browser validator', async () => {
  const vite = await sharedVite()
  try {
    const { validateConceptPayload } = await vite.ssrLoadModule('/src/ConceptView.jsx')
    assert.ok(CASES.length >= 5)
    assert.ok(CASES.some(({ frame }) => frame.completeness.included.derived_edges > 0
      && frame.completeness.included.edges > frame.completeness.source.edges))
    for (const { name, expected, frame } of CASES) {
      assert.doesNotThrow(() => validateConceptPayload(frame, expected), name)
    }
  } finally {
    await vite.close()
  }
})

test('the derived-edge receipt still refuses a frame whose edges no source can account for',
  async () => {
    const vite = await sharedVite()
    try {
      const { validateConceptPayload, ConceptContractError } =
        await vite.ssrLoadModule('/src/ConceptView.jsx')
      const { expected, frame } = CASES.find(({ frame: f }) =>
        f.completeness.source.edges > 0 && f.completeness.included.derived_edges > 0)
      const clone = () => JSON.parse(JSON.stringify(frame))
      const lessSource = clone()
      lessSource.completeness.source.edges -= 1
      assert.throws(() => validateConceptPayload(lessSource, expected), ConceptContractError)
      const moreDerived = clone()
      moreDerived.completeness.included.derived_edges = moreDerived.completeness.included.edges + 1
      assert.throws(() => validateConceptPayload(moreDerived, expected), ConceptContractError)
      const noDerived = clone()
      delete noDerived.completeness.included.derived_edges
      assert.throws(() => validateConceptPayload(noDerived, expected), ConceptContractError)
      const lessMembership = clone()
      lessMembership.completeness.source.membership_nodes -= 1
      assert.throws(() => validateConceptPayload(lessMembership, expected), ConceptContractError)
    } finally {
      await vite.close()
    }
  })

test('the error card names what failed instead of promising that a retry helps', async () => {
  const vite = await sharedVite()
  try {
    const { conceptFailure, conceptErrorBody, ConceptContractError } =
      await vite.ssrLoadModule('/src/ConceptView.jsx')
    const contract = conceptFailure(new ConceptContractError('Invalid concept projection'))
    assert.deepEqual(contract, { kind: 'contract' })
    assert.match(conceptErrorBody(false, contract), /refused the concept projection/)
    assert.doesNotMatch(conceptErrorBody(false, contract), /retry when/)
    const http = conceptFailure(Object.assign(new Error('run is being deleted'), { status: 409 }))
    assert.deepEqual(http, { kind: 'http', status: 409, message: 'run is being deleted' })
    assert.match(conceptErrorBody(false, http), /HTTP 409: run is being deleted/)
    // a fetch that never reached the server is a TypeError too, and is NOT a contract refusal
    const transport = conceptFailure(new TypeError('Failed to fetch'))
    assert.deepEqual(transport, { kind: 'transport' })
    assert.match(conceptErrorBody(false, transport), /unreachable/)
    assert.match(conceptErrorBody(true, contract), /timed out/)
    // a 200 whose body is not JSON reached SOMETHING: neither a contract refusal nor unreachable
    const malformed = conceptFailure(new SyntaxError('Unexpected token < in JSON'))
    assert.deepEqual(malformed, { kind: 'malformed' })
    assert.match(conceptErrorBody(false, malformed), /not JSON/)
  } finally {
    await vite.close()
  }
})
