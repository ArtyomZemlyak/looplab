import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

import React, { act, useState } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

const source = () => readFile(new URL('../src/RunList.jsx', import.meta.url), 'utf8')
const between = (text, start, end) => text.slice(text.indexOf(start), text.indexOf(end))

test('the shared RunList mutation guard is single-flight, bounded, and honest about timeouts', async () => {
  const text = await source()
  const copy = between(text, 'const FENCE_CONFLICT_CODES', 'function useMutation')
  const branches = between(text, 'const mutationMessage', 'function useMutation')
  const reflection = between(text, 'const fenceConflictMessage', 'const mutationMessage')
  const hook = between(text, 'function useMutation', 'const focusSoon')

  assert.ok(copy && branches && reflection && hook, 'a source anchor moved; this test reads nothing')
  assert.match(branches, /error\?\.status === 409[\s\S]*error\?\.status === 503/)
  assert.match(branches, /current input or selection kept/)
  assert.match(branches, /outcome is unknown/)
  assert.doesNotMatch(copy, /Refresh before retrying/,
    'the UI must not instruct a retry before it has actually reconciled the store')

  // Reflection is allowed in exactly one branch and nowhere else. This used to be a blanket ban on
  // `.message`, which stopped holding when the fence-conflict codes were added — those describe
  // WHICH identity moved under the request, which only the server knows. The rule is now the
  // narrower true one: every OTHER branch is client-owned copy.
  assert.doesNotMatch(branches, /\.message|\.detail|String\(/,
    'only the fence-conflict helper may reflect server text; every other branch is client-owned')
  assert.match(branches, /FENCE_CONFLICT_CODES\.has\(error\?\.code\)[\s\S]*fenceConflictMessage\(error\)/)
  assert.doesNotMatch(reflection, /\.detail/,
    'a raw provider `detail` is never the app-owned explanation these codes carry')
  // ...and what it reflects is coerced, bounded, and has a fallback. A malformed body can put a
  // non-string in either field, and joining those raw renders `[object Object]` into an alert;
  // an envelope carrying neither used to produce an EMPTY alert precisely when the operator most
  // needed to be told their view had moved.
  assert.match(reflection, /typeof part === 'string'/)
  assert.match(reflection, /\.slice\(0, 500\)/)
  assert.match(reflection, /parts\.length\s*\?[\s\S]*:\s*'[^']+'/,
    'the reflection helper must fall back to client-owned copy, never to the empty string')
  assert.match(hook, /if \(lock\.current\) return false/)
  assert.match(hook, /lock\.current = true; setState\(true\)/)
  assert.equal((hook.match(/settleWithin\(action, LIST_WRITE_TIMEOUT_MS\)/g) || []).length, 1,
    'a mutation intent is sent once through the bounded settlement guard')
  assert.match(hook, /if \(!settlement\.ok\)[\s\S]*typeof reconcile === 'function'[\s\S]*settleWithin\(reconcile, LIST_RECONCILE_TIMEOUT_MS\)[\s\S]*mutationReconcileSuffix\(reconciliationStatus\(check\)\)[\s\S]*return false/,
    'a stalled write reconciles the affected store before the explicit retry path is re-armed')
  // The per-status copy moved out of the hook into `mutationReconcileSuffix`; assert it where it
  // lives now rather than where it used to, so the anchor tracks the code instead of decaying.
  assert.match(copy, /follow-up refresh timed out[\s\S]*follow-up refresh failed/)
  assert.match(hook, /const outcome = settlement\.value[\s\S]*if \(outcome === false\)[\s\S]*return false/,
    'a stronger caller-owned reconciliation result cannot be relabeled as success')
  // The failure state grew a `cause`, so a `setState(message)`-exact anchor stopped seeing this
  // branch. The properties are what matter: a thrown mutation reconciles the store, and EVERY exit
  // from the failure path reports failure — a `return true` here would relabel a failed write as a
  // success and let the caller close its dialog over it.
  const caught = hook.slice(hook.indexOf('catch (error)'), hook.indexOf('finally {'))
  assert.match(caught, /settleWithin\(reconcile, LIST_RECONCILE_TIMEOUT_MS\)/)
  assert.equal((caught.match(/return false/g) || []).length,
    (caught.match(/\breturn\b/g) || []).length,
    'every exit from the failure path must report failure')
  // A purely LOCAL failure (saved-view storage/naming/capacity) has nothing on the server to
  // reconcile against, so it short-circuits before paying for a refresh that could not tell it
  // anything. Pinned because the ordering is the whole point of the branch.
  assert.match(caught, /if \(localMutationError\(error\)\)[\s\S]*return false/)
  assert.ok(caught.indexOf('localMutationError(error)') < caught.indexOf('settleWithin(reconcile'),
    'a local failure must short-circuit before the reconcile, not after it')
  assert.match(hook, /finally \{ lock\.current = false \}/)
  assert.doesNotMatch(hook, /setTimeout|setInterval|while\s*\(/,
    'RunList must never schedule or loop an automatic mutation replay')

  // Both alert paths — the shared mutation hook and the list-mutation copy — must reach the fence
  // conflicts through the same set and the same reflection helper. They were two hand-maintained
  // copies of an identical six-code list plus an identical raw join, which is how the hardening
  // applied to one would have silently missed the other.
  assert.doesNotMatch(text, /\[error\.message, error\.remediation\]/,
    'the raw unbounded join is back; reflect through fenceConflictMessage instead')
  assert.equal((text.match(/'invalid_expected_organization'/g) || []).length, 1,
    'the fence-conflict codes are listed twice again; a fix to one list will miss the other')
  assert.equal((text.match(/FENCE_CONFLICT_CODES\.has\(/g) || []).length, 2,
    'both mutation-alert paths must consult the one fence-conflict set')
  assert.equal((text.match(/fenceConflictMessage\(error\)/g) || []).length, 2,
    'both mutation-alert paths must reflect through the one bounded helper')
})

test('reflected fence-conflict text is coerced, bounded, and never empty', async () => {
  const vite = await createServer({ root: UI_ROOT, configFile: false, appType: 'custom',
    logLevel: 'silent', server: { middlewareMode: true } })
  try {
    const { __testFenceConflictMessage: message } = await vite.ssrLoadModule('/src/RunList.jsx')
    assert.ok(typeof message === 'function', 'RunList no longer exports the reflection helper')

    assert.equal(message({ message: 'The run moved.', remediation: 'Refresh.' }),
      'The run moved. Refresh.')
    assert.equal(message({ remediation: 'Refresh.' }), 'Refresh.')
    // A malformed body can put anything in either field. Joining raw rendered `[object Object]`
    // straight into the alert; a body with neither field rendered nothing at all, precisely when
    // the operator needed to be told their view had moved under them.
    assert.equal(message({ message: { detail: 'x' }, remediation: ['y'] }).includes('object Object'),
      false)
    assert.match(message({}), /changed under this request/)
    assert.match(message({ message: '   ' }), /changed under this request/)
    assert.match(message(null), /changed under this request/)
    assert.equal(message({ message: 'x'.repeat(4000) }).length, 500)
  } finally {
    await vite.close()
  }
})

test('prompt dialogs retain their draft, focus layer, and controls until success', async () => {
  const text = await source()
  const modal = between(text, 'function Modal', 'function PromptModal')
  const prompt = between(text, 'function PromptModal', '// Per-run')
  const submitProject = between(text, 'const submitProject', 'const startProjectRename')
  const submitRename = between(text, 'const submitRunRename', 'const removeRun')

  assert.match(modal, /useDialogFocus\(dialogRef, busy \? null : onClose\)/)
  assert.match(modal, /!busy && event\.target === event\.currentTarget/)
  assert.match(modal, /aria-busy=\{busy\}/)
  assert.match(modal, /disabled=\{busy\} onClick=\{onClose\}/)
  // `onSubmit` gained a second argument (the typed confirmation), which an argument-exact anchor
  // could not see. The property is the GUARD: the dialog closes only on a truthy awaited result, and
  // there is exactly one `onClose()` in the submit path — so every failure keeps the draft.
  const go = prompt.slice(prompt.indexOf('const go = async'), prompt.indexOf('return <Modal'))
  assert.match(go, /await mutate\(\(\) => onSubmit\([\s\S]*?\), onReconcile\)\) onClose\(\)/,
    'close happens only after the authoritative request resolves')
  assert.equal((go.match(/onClose\(\)/g) || []).length, 1,
    'a second close path would discard the draft on some failure')
  assert.match(prompt, /readOnly=\{busy \|\| blocked\}/,
    'a blocked row must not accept a new value either, not only a busy one')
  assert.match(prompt, /disabled=\{!ok \|\| busy\}/)
  assert.match(prompt, /role="alert"/)
  assert.doesNotMatch(submitProject.slice(0, submitProject.indexOf('await createProject')), /setProjModal|restoreProjectModalFocus/)
  assert.doesNotMatch(submitRename.slice(0, submitRename.indexOf('await renameRun')), /setRunRename|restoreRunModalFocus/)
})

test('move, project, and super-task drafts remain recoverable through failures', async () => {
  const text = await source()
  const menu = between(text, 'function RunMenu', '// Manage super-tasks')
  const superModal = between(text, 'function SuperTaskModal', 'export default function RunList')
  const tree = between(text, 'function TreeNode', '// Small centered popup')
  const projectRename = between(text, 'const finishProjectRename', 'const removeProject')

  assert.match(menu, /const act = async action => \{ if \(await mutate\(action, onReconcile\)\) onClose\(true\) \}/)
  assert.match(menu, /aria-busy=\{busy\}/)
  assert.match(menu, /aria-disabled=\{busy\}/)
  assert.match(menu, /onClickCapture=\{e => \{ if \(busy\)/,
    'the open menu blocks every competing action while its intent is pending')
  assert.match(menu, /act\(\(\) => onMove\(/)
  assert.match(menu, /act\(\(\) => onSetSuper\(/)
  assert.match(menu, /role="status">Saving…/)
  assert.match(menu, /role="alert"/)

  assert.match(superModal, /await mutate\(\(\) => onCreate\(v\), onReconcile\)[\s\S]*setName\(''\)/,
    'create clears the controlled draft only after success')
  assert.match(superModal, /mutate\(\(\) => onRename\(task\.id, v\), onReconcile\)/)
  assert.match(superModal, /await mutate\(async \(\) => \{ removed = await onDelete\(task\) \}, onReconcile\)/)
  assert.match(superModal, /readOnly=\{busy\}/)
  assert.match(superModal, /disabled=\{busy \|\| !name\.trim\(\)\}/)
  assert.match(superModal, /role="alert"/)

  assert.match(projectRename, /if \(value && !await saveProjectRename\([\s\S]*patchProject[\s\S]*loadProjects\)\) return false/)
  assert.match(projectRename, /setRenaming\(null\)[\s\S]*return true/,
    'the inline project editor remains mounted with its exact draft on rejection')
  assert.match(tree, /input\.dataset\.pending = 'true'[\s\S]*await finishProjectRename[\s\S]*delete input\.dataset\.pending/,
    'a failed Enter save must re-arm blur and explicit retry')
  assert.doesNotMatch(tree, /dataset\.finished/)
  assert.match(text, /readOnly=\{projectBusy\}/)
  assert.match(tree, /disabled=\{projectBusy\}/)
  assert.match(tree, /projectBusy && <div className="muted" role="status">Saving project name/)
  assert.match(text, /renaming === p\.id && projectError[\s\S]*role="alert"/)
})

test('project rename blocks competing controls and re-arms after an authoritative failure', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, HTMLElement: dom.window.HTMLElement,
    requestAnimationFrame: callback => setTimeout(callback, 0),
    cancelAnimationFrame: handle => clearTimeout(handle), IS_REACT_ACT_ENVIRONMENT: true,
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  const pending = []
  const calls = []
  let root
  let vite
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, { TreeNode }] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/RunList.jsx'),
    ])
    root = createRoot(document.getElementById('root'))

    function Harness() {
      const [busy, setBusy] = useState(false)
      const [renaming, setRenaming] = useState('project-1')
      const finishProjectRename = async (...args) => {
        calls.push(args)
        setBusy(true)
        const finished = await new Promise(resolve => pending.push(resolve))
        setBusy(false)
        if (finished) setRenaming(null)
        return finished
      }
      return React.createElement(TreeNode, {
        p: { id: 'project-1', name: 'Baseline sweep' }, depth: 0,
        ctx: {
          byParent: {}, expanded: new Set(), sel: null, setSel() {}, onDrop() {}, toggle() {},
          renaming, finishProjectRename, startProjectRename() {}, projectBusy: busy, projectError: '',
          count: () => 0, addProject() {}, removeProject() {},
        },
      })
    }
    await act(async () => root.render(React.createElement(Harness)))
    let input = document.querySelector('.ptree-rename')
    await act(async () => {
      input.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
      await Promise.resolve()
    })
    assert.equal(calls.length, 1)
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /Saving project name/)
    assert.ok([...document.querySelectorAll('.ptree-node button')].every(button => button.disabled))

    input = document.querySelector('.ptree-rename')
    await act(async () => {
      input.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
      await Promise.resolve()
    })
    assert.equal(calls.length, 1, 'Escape cannot bypass the pending mutation lock')

    await act(async () => { pending.shift()(false); await Promise.resolve() })
    input = document.querySelector('.ptree-rename')
    assert.ok(input)
    assert.equal(input.dataset.pending, undefined)
    await act(async () => {
      input.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
      await Promise.resolve()
    })
    assert.equal(calls.length, 2, 'the exact draft can be retried after rejection')
    await act(async () => { pending.shift()(true); await Promise.resolve() })
    assert.equal(document.querySelector('.ptree-rename'), null)
  } finally {
    if (root) await act(async () => root.unmount())
    if (vite) await vite.close()
    dom.window.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  }
})
