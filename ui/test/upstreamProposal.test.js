import test from 'node:test'
import assert from 'node:assert/strict'
import { upstreamProposalSummary as summary } from '../src/upstreamProposalModel.js'
import { upstreamCheckSummary } from '../src/upstreamCheckModel.js'
import { click, mountLive, until } from './_mount.js'

const claim = { type: 'upstream_proposal_started', seq: 1, action_id: 'original',
  proposal_id: `up_${'a'.repeat(24)}`, request_hash: 'b'.repeat(64) }
const proposal = { ...claim, type: 'upstream_proposed', seq: 2, source_node_id: 4 }
const check = { type: 'upstream_gate_started', seq: 3, action_id: 'check',
  proposal_id: claim.proposal_id, request_hash: 'c'.repeat(64), input_identity: 'input' }
const finish = { ...check, type: 'upstream_gate_finished', seq: 4,
  result: { input_identity: 'input', passed: true } }

test('latest proposal does not inherit the preceding passing check; late completion stays abandoned', () => {
  const next = { ...claim, seq: 5, action_id: 'next', proposal_id: `up_${'d'.repeat(24)}` }
  assert.equal(summary([claim, proposal, check, finish, next]).status, 'unfinished')
  assert.equal(summary([claim, proposal, check, finish]).status, 'check_passed')
  assert.equal(summary([claim, proposal, check]).status, 'check_unfinished')
  assert.equal(summary([claim, { type: 'upstream_gate_abandoned', seq: 2,
    claim_action_id: claim.action_id }, { ...proposal, seq: 3 }]).status, 'abandoned')
  assert.equal(summary([claim, { ...proposal, type: 'upstream_proposal_failed' }]).status, 'failed')
  assert.equal(summary([claim, proposal, check, { type: 'upstream_gate_abandoned', seq: 4,
    proposal_id: claim.proposal_id, claim_action_id: check.action_id }, { ...finish, seq: 5 }]).status, 'check_abandoned')
})

test('clipped, inconsistent, duplicate or reordered proposal evidence cannot grant a usable identity', () => {
  for (const rows of [[proposal], [claim, claim, proposal], [claim, proposal, proposal],
    [proposal, claim], [claim, null, proposal], [claim, { ...proposal, request_hash: 'changed' }],
    [{ ...claim, request_hash: 123 }], [claim, proposal, { ...proposal, seq: 5,
      action_id: 'orphan', proposal_id: `up_${'d'.repeat(24)}` }]]) {
    assert.deepEqual(summary(rows), { status: 'unknown' })
  }
  assert.equal(summary([]), null)
  assert.equal(summary([claim, proposal, finish]).status, 'check_unknown')
})

test('malformed history is unavailable before filtering, while omitted legacy history stays empty', () => {
  for (const history of [null, {}, '[]', false, 1, [null], [false],
    [{ type: 'unrelated', seq: '1' }], [{ type: 'unrelated', seq: 3 }, { type: 'unrelated', seq: 2 }]]) {
    assert.deepEqual(summary(history), { status: 'unknown' })
    assert.deepEqual(upstreamCheckSummary(history), { status: 'unknown' })
  }
  for (const history of [undefined, [], [{ type: 'unrelated', seq: 3 }]]) {
    assert.equal(summary(history), null)
    assert.equal(upstreamCheckSummary(history), null)
  }
  assert.deepEqual(summary([{ type: 'base_advanced', seq: 10 }]), { status: 'unknown' })
})

test('RU/EN damaged history offers evidence recovery and never invents an original request', async () => {
  const harness = await mountLive()
  const received = []
  const listener = event => received.push(event.detail.text)
  window.addEventListener('ll:focus-assistant', listener)
  try {
    const { default: UpstreamPanel } = await harness.load('/src/UpstreamPanel.jsx')
    for (const language of ['ru', 'en']) {
      localStorage.setItem('looplab.language', language)
      const view = await harness.mount(UpstreamPanel, {
        state: { nodes: {}, upstream_enabled: true, upstream_history: [claim] },
      })
      try {
        const button = view.container.querySelector('button')
        button.focus()
        for (const history of [null, {}, [null], [{ type: 'base_advanced', seq: 10 }]]) {
          await view.rerender({ state: { nodes: {}, upstream_enabled: true, upstream_history: history } })
          assert.match(view.container.textContent, language === 'ru'
            ? /История подготовки изменения недоступна/ : /Proposal evidence unavailable/)
          if (!Array.isArray(history) || history[0] == null) assert.match(view.container.textContent,
            language === 'ru' ? /История обновлений недоступна/ : /Base update history unavailable/)
          assert.doesNotMatch(view.container.textContent, /expected_request_hash:|proposal_id:/)
          assert.equal(document.activeElement, button)
          const count = received.length
          await click(button)
          await until(() => received.length === count + 1, 'unknown history recovery draft')
          assert.match(received.at(-1), language === 'ru'
            ? /не придумывай ключ или тело/ : /do not invent a key or body/)
          assert.ok(!received.at(-1).includes(claim.proposal_id))
        }
        await view.rerender({ state: { nodes: {}, upstream_enabled: true, upstream_history: [claim] } })
        const count = received.length
        await click(button)
        await until(() => received.length === count + 1, 'restored verified original identity')
        assert.ok(received.at(-1).includes(claim.proposal_id))
        assert.deepEqual(harness.fetch.calls, [])
      } finally { await view.unmount() }
    }
  } finally {
    window.removeEventListener('ll:focus-assistant', listener)
    localStorage.removeItem('looplab.language')
    await harness.close()
  }
})

test('recorded advancement requires its own passing gate, never a clipped or unrelated check', () => {
  const advance = { type: 'base_advanced', seq: 5, proposal_id: claim.proposal_id, gate_seq: 4 }
  assert.equal(summary([claim, proposal, check, finish, advance]).status, 'advanced')
  assert.equal(summary([claim, proposal, advance]).status, 'unknown')
  assert.equal(summary([claim, proposal, check, finish, { ...advance, gate_seq: 123 }]).status, 'unknown')
  assert.equal(summary([claim, proposal, check, { ...finish, result: { input_identity: 'input', passed: false } }, advance]).status, 'unknown')
})

test('advancement cannot borrow a different passing check or point to a future gate', () => {
  const next = { ...check, seq: 5, action_id: 'check-two', request_hash: 'd'.repeat(64) }
  const nextFinish = { ...finish, ...next, type: 'upstream_gate_finished', seq: 6 }
  const advance = { type: 'base_advanced', seq: 7, proposal_id: claim.proposal_id, gate_seq: 4 }
  for (const history of [
    [claim, proposal, { ...finish, seq: 3 }, { ...check, seq: 4 },
      { ...finish, seq: 5 }, { ...advance, seq: 6, gate_seq: 3 }],
    [claim, proposal, check, finish, next, nextFinish, advance],
    [claim, proposal, check, { ...advance, seq: 4, gate_seq: 5 }, { ...finish, seq: 5 }],
    [claim, proposal, check, finish, { type: 'upstream_gate_abandoned', seq: 5,
      proposal_id: claim.proposal_id, claim_action_id: check.action_id },
      { ...next, seq: 6 }, { ...nextFinish, seq: 7 }, { ...advance, seq: 8 }],
  ]) assert.equal(summary(history).status, 'unknown')
  assert.equal(summary([claim, proposal, check, finish, next, nextFinish,
    { ...advance, gate_seq: 6 }]).status, 'advanced')
})

test('a new proposal never shows the preceding proposal check as its own in RU or EN', async () => {
  const harness = await mountLive()
  const next = { ...claim, seq: 5, action_id: 'next', proposal_id: `up_${'d'.repeat(24)}` }
  const nextProposal = { ...next, type: 'upstream_proposed', seq: 6, source_node_id: 5 }
  const nextCheck = { ...check, seq: 7, action_id: 'check-two', proposal_id: next.proposal_id }
  const nextFinish = { ...finish, ...nextCheck, type: 'upstream_gate_finished', seq: 8,
    result: { ...finish.result, executions: [{ seconds: 1 }], eval_seconds: 1 } }
  try {
    const { default: UpstreamPanel } = await harness.load('/src/UpstreamPanel.jsx')
    for (const language of ['ru', 'en']) {
      localStorage.setItem('looplab.language', language)
      const old = [claim, proposal, check, finish]
      const state = history => ({ nodes: {}, upstream_enabled: true, upstream_history: history })
      const view = await harness.mount(UpstreamPanel, { state: state(old) })
      try {
        assert.match(view.container.textContent, language === 'ru' ? /Записанная завершённая проверка/ : /Recorded completed check/)
        const button = view.container.querySelector('button')
        button.focus()
        for (const tail of [[next], [next, { ...nextProposal, type: 'upstream_proposal_failed' }],
          [next, { type: 'upstream_gate_abandoned', seq: 6,
            proposal_id: next.proposal_id, claim_action_id: next.action_id }],
          [next, nextProposal], [next, nextProposal, nextCheck],
          [next, { ...nextProposal, request_hash: 'invalid' }]]) {
          await view.rerender({ state: state([...old, ...tail]) })
          assert.doesNotMatch(view.container.textContent, /Записанная завершённая проверка|Recorded completed check/)
          assert.equal(document.activeElement, button)
        }
        await view.rerender({ state: state([...old, next, nextProposal, nextCheck, nextFinish]) })
        assert.match(view.container.textContent, language === 'ru' ? /Выполнений: 1 · 1.0 с/ : /1 explicit executions · 1.0 s/)
        assert.deepEqual(harness.fetch.calls, [])
      } finally { await view.unmount() }
    }
  } finally {
    localStorage.removeItem('looplab.language')
    await harness.close()
  }
})

test('recovery handoff preserves original identity in a Russian draft and never calls the server', async () => {
  const harness = await mountLive()
  const received = []
  const listener = event => received.push(event.detail.text)
  window.addEventListener('ll:focus-assistant', listener)
  try {
    localStorage.setItem('looplab.language', 'ru')
    const { default: UpstreamPanel } = await harness.load('/src/UpstreamPanel.jsx')
    const view = await harness.mount(UpstreamPanel, {
      state: { nodes: {}, upstream_enabled: true, upstream_history: [claim] },
    })
    assert.match(view.container.textContent, /Подготовка изменения не завершена/)
    assert.match(view.container.textContent, /200 событий/)
    await click(view.container.querySelector('button'))
    await until(() => received.length === 1, 'recovery draft')
    assert.match(received[0], /upstream_request/)
    assert.match(received[0], new RegExp(claim.proposal_id))
    assert.match(received[0], new RegExp(claim.request_hash))
    assert.match(received[0], /не повторяй записи, не запускай проверки/)
    await view.rerender({ state: { nodes: {}, upstream_enabled: true, upstream_history: [proposal] } })
    await click(view.container.querySelector('button'))
    await until(() => received.length === 2, 'unknown recovery draft')
    assert.match(received[1], /не придумывай ключ или тело/)
    assert.ok(!received[1].includes(claim.proposal_id))
    assert.deepEqual(harness.fetch.calls, [])
  } finally {
    window.removeEventListener('ll:focus-assistant', listener)
    localStorage.removeItem('looplab.language')
    await harness.close()
  }
})
