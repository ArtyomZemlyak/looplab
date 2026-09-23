// The Assistant composer's per-chat drafts, driven LIVE through the real hook (review 2026-09-22,
// UI-06). While this choreography lived inside the 4,000-line `AssistantBar.jsx`, nothing reached it
// short of mounting the whole Assistant and typing: which chat a draft belongs to, that switching
// chats never carries text across, that "+ New" gets a fresh slot once its draft is bound to a
// created chat, and that a draft keeps the run it was written against. A probe component calls
// `useAssistantComposer` under the shared live harness, so React's own state machinery runs it.
import test, { after, before } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'

import { mountLive } from './_mount.js'

let harness
let useAssistantComposer

before(async () => {
  harness = await mountLive()
  ;({ useAssistantComposer } = await harness.load('/src/useAssistantComposer.js'))
})

after(async () => { await harness?.close() })

async function probe() {
  const box = { runId: 'run-a', activations: 0, api: null }
  const Probe = () => {
    box.api = useAssistantComposer({
      currentRunId: () => box.runId,
      onActivate: () => { box.activations += 1 },
    })
    return null
  }
  const handle = await harness.mount(Probe)
  const act = fn => React.act(async () => { fn(box.api) })
  return { box, handle, act }
}

test('a draft is written against the open run and keeps it when the run changes', async () => {
  const { box, handle, act } = await probe()
  await act(api => api.setInput('why did #3 fail?'))
  assert.equal(box.api.input, 'why did #3 fail?')
  assert.equal(box.api.draftRunScope, 'run-a')
  box.runId = 'run-b'
  await act(api => api.setInput(prev => `${prev} and #4`))
  assert.equal(box.api.input, 'why did #3 fail? and #4')
  assert.equal(box.api.draftRunScope, 'run-a', 'switching runs mid-draft does not re-target it')
  await act(api => api.setInput('/new tune the learning rate'))
  assert.equal(box.api.draftRunScope, null, 'a new-run command belongs to no run')
  await handle.unmount()
})

test('each chat keeps its own draft, and binding frees the new-chat slot', async () => {
  const { box, handle, act } = await probe()
  await act(api => api.setInput('draft for the new chat'))
  await act(api => { api.activateComposer('sess-1') })
  assert.equal(box.api.input, '', 'another chat does not inherit the draft')
  assert.equal(box.activations, 1, 'activating a composer resets what the component asked it to')
  await act(api => api.setInput('draft for sess-1'))
  await act(api => { api.activateComposer('__new__') })
  assert.equal(box.api.input, 'draft for the new chat', 'the new-chat draft was kept')
  await act(api => { api.bindComposerToSession('sess-2') })
  await act(api => { api.activateComposer('__new__') })
  assert.equal(box.api.input, '', 'the bound draft left the new-chat slot, which starts fresh')
  await act(api => { api.activateComposer('sess-2') })
  assert.equal(box.api.input, 'draft for the new chat', 'the draft moved with its chat')
  await act(api => { api.activateComposer('sess-1') })
  assert.equal(box.api.input, 'draft for sess-1')
  await act(api => { api.activateComposer('sess-1', { clear: true }) })
  assert.equal(box.api.input, '', 'clear starts that chat over')
  await handle.unmount()
})

test('attachments, pending reads and the mode belong to the draft too', async () => {
  const { box, handle, act } = await probe()
  const file = { name: 'notes.md', size: 3, content: 'abc', truncated: false }
  await act(api => api.setFiles([file]))
  assert.deepEqual(box.api.files, [file])
  assert.equal(box.api.draftRunScope, 'run-a', 'an attachment ties the draft to the open run')
  let returned
  await act(api => { returned = api.updatePendingFileReads(api.composerDraftRef.current, 2) })
  assert.equal(returned, 2)
  assert.equal(box.api.pendingFileReads, 2)
  await act(api => { api.updatePendingFileReads(api.composerDraftRef.current, n => n - 5) })
  assert.equal(box.api.pendingFileReads, 0, 'pending reads never go negative')
  await act(api => api.setComposerMode('auto'))
  assert.equal(box.api.mode, 'auto')
  await act(api => api.setComposerMode('bogus'))
  assert.equal(box.api.mode, 'plan', 'an unknown mode falls back to plan')
  await act(api => { api.activateComposer('sess-9', { seedMode: 'acceptEdits' }) })
  assert.equal(box.api.mode, 'acceptEdits', 'a fresh chat takes the mode it was opened with')
  assert.deepEqual(box.api.files, [])
  await handle.unmount()
})
