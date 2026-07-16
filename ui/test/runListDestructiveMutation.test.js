import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const source = () => readFile(new URL('../src/RunList.jsx', import.meta.url), 'utf8')
const between = (text, start, end) => text.slice(text.indexOf(start), text.indexOf(end))

test('destructive and drag/drop writes stay authoritative, bounded, and recoverable', async () => {
  const text = await source()
  const deadline = between(text, 'const settleWithin', '// Destructive list actions')
  const hook = between(text, 'export function useListMutation', '// Module-scope')
  const projectDelete = between(text, 'const removeProject', 'const moveRun')
  const menuMove = between(text, 'const moveRun', 'const onDrop')
  const drop = between(text, 'const onDrop', 'const submitRunRename')
  const runDelete = between(text, 'const removeRun', '// super-task CRUD')
  const menu = between(text, 'function RunMenu', '// Manage super-tasks')

  assert.match(hook, /if \(lock\.current\) return false/)
  assert.equal((hook.match(/settleWithin\(action, actionTimeout\)/g) || []).length, 1)
  assert.equal((hook.match(/settleWithin\(reconcile, reconcileTimeout\)/g) || []).length, 1)
  assert.match(deadline, /if \(settled\) return[\s\S]*clearTimeout\(timer\)/)
  assert.match(deadline, /setTimeout\(\(\) => finish\(\{ timeout: true \}\), timeout\)/)
  assert.match(deadline, /Promise\.resolve\(\)\.then\(work\)\.then/)
  assert.match(hook, /const token = \+\+version\.current[\s\S]*version\.current === token/,
    'only the current mutation may update presentation state')
  assert.doesNotMatch(hook, /setInterval|while\s*\(|retry\s*\(/i,
    'an ambiguous write must never be replayed automatically')

  assert.match(projectDelete, /confirm\([\s\S]*await mutateList\('delete-project'/)
  assert.match(projectDelete, /await deleteProject\(id\); await refresh\(\)/)
  assert.match(projectDelete, /if \(removed && sel === id\) setSel\(ALL\)/,
    'the selected project changes only after authoritative deletion')
  assert.match(drop, /if \(!runId \|\| listBusy\) return/)
  assert.match(menuMove, /return mutateList\('move-run'/)
  assert.match(menuMove, /await assignRun\([\s\S]*await refresh\(\)/)
  assert.match(drop, /setDragRun\(null\)[\s\S]*await moveRun\(runId, project_id\)/,
    'menu and drag/drop must share one authoritative move contract')
  assert.match(runDelete, /confirm\([\s\S]*setRunMenu\(null\)[\s\S]*await mutateList\('delete-run'/)
  assert.doesNotMatch(runDelete.slice(0, runDelete.indexOf('if (!confirm')), /setRunMenu\(null\)/)
  assert.match(menu, /className="mi danger" onClick=\{\(\) => onDelete\(r\)\}/)
  assert.doesNotMatch(menu, /className="mi danger"[^\n]*close\(/,
    'cancelling the native confirmation leaves the action menu and its focus target mounted')
  assert.doesNotMatch(runDelete, /alert\(|\.message|\.detail|String\(/,
    'delete errors must remain inline and must not reflect backend text')

  assert.match(text, /listMutation\?\.busy[\s\S]*role="status"/)
  assert.match(text, /listMutation\?\.error[\s\S]*role="alert"/)
  assert.match(text, /compactNav && projectsOpen && mutationNotice/)
  assert.match(text, /\(!compactNav \|\| !projectsOpen\) && mutationNotice/,
    'pending and error feedback has exactly one live region in the active visual layer')
  assert.match(text, /aria-disabled=\{navigationBusy \|\| undefined\}/)
  assert.match(text, /if \(navigationBusy\) \{ event\.preventDefault\(\); return \}/)
  assert.match(text, /className="crumb" disabled=\{navigationBusy\}/,
    'scope navigation cannot move the operator away from a pending outcome')
  assert.match(text, /busy && event\.key === 'Tab'/,
    'keyboard focus cannot navigate out while a menu write is pending')
  assert.match(text, /useDialogFocus\(projectsDialogRef, navigationBusy \? null/,
    'Escape cannot close the drawer during any list, project, or menu mutation')
  assert.match(text, /className="project-backdrop" disabled=\{projectBusy\} aria-disabled=\{navigationBusy \|\| undefined\}/)
  assert.match(text, /onClick=\{\(\) => \{ if \(!navigationBusy\) setProjectsOpen\(false\) \}\}/,
    'Escape and backdrop dismissal are both disabled while a drawer mutation is pending')
  assert.match(text, /<div inert=\{listBusy \? '' : undefined\}>/,
    'the drawer controls themselves leave the interaction and accessibility trees while pending')
})

test('the list mutation guard bounds hung writes and reconciliation without late overwrite', async () => {
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
  const writes = []
  const reads = []
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
    const [{ createRoot }, { useListMutation }] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/RunList.jsx'),
    ])
    root = createRoot(document.getElementById('root'))

    const write = () => new Promise((resolve, reject) => writes.push({ resolve, reject }))
    const reconcile = () => {
      if (!reads.length) { reads.push(null); return Promise.resolve() }
      return new Promise(resolve => reads.push(resolve))
    }
    function Harness() {
      const [state, mutate, clear] = useListMutation({ actionTimeout: 25, reconcileTimeout: 25 })
      return React.createElement(React.Fragment, null,
        React.createElement('button', { onClick: () => mutate('delete-run', 'Deleting run…', write, reconcile) }, 'Delete'),
        state?.busy && React.createElement('div', { role: 'status' }, state.label),
        state?.error && React.createElement('div', { role: 'alert' }, state.error,
          React.createElement('button', { onClick: clear }, 'Dismiss')))
    }

    await act(async () => root.render(React.createElement(Harness)))
    const button = document.querySelector('button')
    await act(async () => {
      button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
      button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    assert.equal(writes.length, 1, 'double activation sends one destructive intent')
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /Deleting run/)

    await act(async () => { await new Promise(resolve => setTimeout(resolve, 70)) })
    assert.equal(reads.length, 1)
    let alert = document.querySelector('[role="alert"]')?.textContent || ''
    assert.match(alert, /not confirmed/i, 'a hung write becomes an explicit unknown outcome')
    await act(async () => {
      button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    assert.equal(writes.length, 2, 'the lock re-arms only after the bounded list check')
    await act(async () => { writes[1].resolve(); await Promise.resolve() })
    await act(async () => { writes[0].resolve(); await Promise.resolve() })
    assert.equal(document.querySelector('[role="alert"]'), null,
      'the old write settling late cannot overwrite a newer success')

    await act(async () => {
      button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    assert.equal(writes.length, 3)
    await act(async () => { writes[2].reject({ status: 500, message: 'private provider detail' }); await Promise.resolve() })
    assert.equal(reads.length, 2)
    assert.match(document.querySelector('[role="status"]')?.textContent || '', /Checking the current list/)
    await act(async () => {
      button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    assert.equal(writes.length, 3, 'the lock stays armed while reconciliation is still pending')

    await act(async () => { await new Promise(resolve => setTimeout(resolve, 70)) })
    alert = document.querySelector('[role="alert"]')?.textContent || ''
    assert.match(alert, /follow-up list check timed out/i)
    assert.doesNotMatch(alert, /private provider detail/)
    await act(async () => {
      button.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
      await Promise.resolve()
    })
    assert.equal(writes.length, 4, 'a hung read cannot hold the mutation lock forever')
    await act(async () => { writes[3].resolve(); await Promise.resolve() })
    await act(async () => { reads[1](); await Promise.resolve() })
    assert.equal(document.querySelector('[role="status"]'), null)
    assert.equal(document.querySelector('[role="alert"]'), null)
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
