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
  const copy = between(text, 'const mutationMessage', 'function useMutation')
  const hook = between(text, 'function useMutation', 'const focusSoon')

  assert.match(copy, /error\?\.status === 409[\s\S]*error\?\.status === 503/)
  assert.match(copy, /current input or selection kept/)
  assert.match(copy, /outcome is unknown/)
  assert.doesNotMatch(copy, /Refresh before retrying/,
    'the UI must not instruct a retry before it has actually reconciled the store')
  assert.doesNotMatch(copy, /\.message|\.detail|String\(/,
    'provider text must not be reflected into a mutation alert')
  assert.match(hook, /if \(lock\.current\) return false/)
  assert.match(hook, /lock\.current = true; setState\(true\)/)
  assert.equal((hook.match(/settleWithin\(action, LIST_WRITE_TIMEOUT_MS\)/g) || []).length, 1,
    'a mutation intent is sent once through the bounded settlement guard')
  assert.match(hook, /if \(!settlement\.ok\)[\s\S]*typeof reconcile === 'function'[\s\S]*settleWithin\(reconcile, LIST_RECONCILE_TIMEOUT_MS\)[\s\S]*Current data was refreshed[\s\S]*return false/,
    'a stalled write reconciles the affected store before the explicit retry path is re-armed')
  assert.match(hook, /follow-up refresh timed out[\s\S]*follow-up refresh failed/)
  assert.match(hook, /const outcome = settlement\.value[\s\S]*if \(outcome === false\)[\s\S]*return false/,
    'a stronger caller-owned reconciliation result cannot be relabeled as success')
  assert.match(hook, /catch \(error\)[\s\S]*settleWithin\(reconcile, LIST_RECONCILE_TIMEOUT_MS\)[\s\S]*setState\(message\); return false/)
  assert.match(hook, /finally \{ lock\.current = false \}/)
  assert.doesNotMatch(hook, /setTimeout|setInterval|while\s*\(/,
    'RunList must never schedule or loop an automatic mutation replay')
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
  assert.match(prompt, /await mutate\(\(\) => onSubmit\(v\.trim\(\)\), onReconcile\)[\s\S]*onClose\(\)/,
    'close happens only after the authoritative request resolves')
  assert.match(prompt, /readOnly=\{busy\}/)
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
