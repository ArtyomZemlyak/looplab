import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('ephemeral command results use polite atomic live regions', async () => {
  const [runView, assistant] = await Promise.all([source('RunView.jsx'), source('AssistantBar.jsx')])
  assert.match(runView, /className="toast" role="status" aria-live="polite" aria-atomic="true"/)
  assert.equal((assistant.match(/className="cmdbar-toast(?: side)?" role="status" aria-live="polite" aria-atomic="true"/g) || []).length, 3)
  assert.match(runView, /setTimeout\(\(\) => setToast\(null\), 5000\)/)
  assert.match(assistant, /setTimeout\(\(\) => mountedRef\.current && setToast\(null\), 5000\)/)
})

test('permission cards expose an accessible pending decision and safe default focus', async () => {
  const [chat, bar] = await Promise.all([source('AssistantChat.jsx'), source('AssistantBar.jsx')])
  assert.match(chat, /role="alertdialog" aria-modal="false" aria-labelledby=\{titleId\} aria-describedby=\{detailsId\}/)
  assert.match(chat, /rejectRef\.current\?\.focus\(\{ preventScroll: true \}\)/)
  assert.match(chat, /permission\.canAlways && <button/)
  assert.match(chat, /Approve once/)
  assert.match(bar, /className="asst-perm-region" role="region"/)
  assert.match(bar, /aria-live="assertive" aria-atomic="false"/)
  assert.match(bar, /pending\.length > 0 && view === 'bar'[\s\S]*?setView\('side'\)/)
  const resolve = bar.slice(bar.indexOf('const resolvePerm = async'), bar.indexOf('const onRevert = async'))
  assert.ok(resolve.indexOf('await assistantResolve') < resolve.indexOf('setPending(current =>'),
    'the card must remain visible until the resolve POST succeeds')
})

test('timeline reveal exposes its state and a mobile touch-sized target', async () => {
  const [dock, css] = await Promise.all([source('Dock.jsx'), source('styles.css')])
  assert.match(dock, /aria-label=\{collapsed \? 'Expand events and timeline' : 'Collapse events and timeline'\}/)
  assert.match(dock, /aria-expanded=\{!collapsed\} aria-controls="run-events-timeline"/)
  assert.match(dock, /id="run-events-timeline" className="dock-body chat-body"/)
  assert.match(css, /@media \(max-width: 900px\)[\s\S]*?\.dock-tabs \.dock-collapse \{ min-width: 44px; min-height: 44px;/)
})

test('compact Inspector dialog has an explicit close or collapse accessible name', async () => {
  const runView = await source('RunView.jsx')
  assert.match(runView, /role=\{compactWorkspace \? 'dialog' : 'complementary'\}/)
  assert.match(runView,
    /aria-label=\{`\$\{compactWorkspace \? 'Close' : 'Collapse'\} \$\{selectedGroup != null \? 'group details' : 'experiment inspector'\}`\}/)
})

test('run view modes are truthful toggles and merge has a native keyboard destination', async () => {
  const runView = await source('RunView.jsx')
  assert.match(runView, /className="view-toggle" role="group" aria-label="Run workspace views"/)
  assert.match(runView, /aria-pressed=\{view === 'dag'\}/)
  assert.match(runView, /aria-pressed=\{view === 'report'\}/)
  assert.match(runView, /<form className="merge-destination-bar"/)
  assert.match(runView, /<label htmlFor="merge-destination-select">/)
  assert.match(runView, /<select id="merge-destination-select"[\s\S]*?autoFocus>/)
  assert.match(runView, /onSubmit=\{submitMergeTarget\}/)
  assert.match(runView, /You can also choose the destination directly on the graph/)
})

test('reset and merge workflows restore focus after temporary controls close', async () => {
  const [inspector, runView, dag] = await Promise.all([
    source('Inspector.jsx'), source('RunView.jsx'), source('Dag.jsx'),
  ])
  assert.match(inspector, /requestAnimationFrame\(\(\) => triggerRef\.current\?\.focus\(\{ preventScroll: true \}\)\)/)
  assert.match(inspector, /document\.addEventListener\('pointerdown', dismiss, true\)/)
  assert.match(inspector, /if \(event\.key === 'Tab'\) \{ setOpen\(false\); return \}/)
  assert.match(inspector, /event\.relatedTarget !== triggerRef\.current && !event\.currentTarget\.contains\(event\.relatedTarget\)/)
  assert.match(dag, /closeMenu\(action !== 'merge'\)[\s\S]*?onNodeAction\(action, id, \{ returnFocus \}\)/)
  assert.match(runView, /const closeMergeChooser = \(restoreFocus = true\) => \{[\s\S]*?data-node-action-id/)
  assert.match(runView, /onClick=\{\(\) => closeMergeChooser\(true\)\}>Cancel<\/button>/)
})

test('popup menus use one roving tab stop and restore the invoking control', async () => {
  const [runView, runList, dag] = await Promise.all([
    source('RunView.jsx'), source('RunList.jsx'), source('Dag.jsx'),
  ])
  assert.match(runView, /aria-haspopup="menu" aria-expanded=\{openHub === label\} aria-controls=\{hubMenuId\(label\)\}/)
  assert.match(runView, /role="menu"[\s\S]*?aria-label=\{`\$\{label\} panels`\}/)
  assert.match(runView, /type="button" role="menuitem" tabIndex=\{-1\}/)
  assert.match(runView, /event\.key === 'Escape'[\s\S]*?closeHub\(true\)/)
  assert.match(runList, /const restoreRunModalFocus = \(\) => requestAnimationFrame/)
  assert.match(runList, /onRename=\{openRunRename\}/)
  assert.match(runList, /onClose=\{closeRunRename\}/)
  assert.equal((dag.match(/type="button" role="menuitem" tabIndex=\{-1\}/g) || []).length, 9)
})

test('compact drawers and nested popups expose only the active modal layer', async () => {
  const [runList, dialogFocus] = await Promise.all([source('RunList.jsx'), source('useDialogFocus.js')])
  assert.match(runList, /role=\{compactNav && projectsOpen && !projModal \? 'dialog' : undefined\}/)
  assert.match(runList, /aria-modal=\{compactNav && projectsOpen && !projModal \? 'true' : undefined\}/)
  assert.match(runList, /aria-hidden=\{compactNav && \(!projectsOpen \|\| !!projModal\) \? 'true' : undefined\}/)
  assert.match(runList, /inert=\{compactNav && \(!projectsOpen \|\| !!projModal\) \? '' : undefined\}/)
  assert.match(runList, /useDialogFocus\(projectsDialogRef, \(\) => setProjectsOpen\(false\), compactNav && projectsOpen\)/)
  assert.match(dialogFocus, /if \(event\.defaultPrevented\) return/)
})

test('route changes update title and move focus to a named main landmark', async () => {
  const [app, auth, list, runView, settings, shared] = await Promise.all([
    source('App.jsx'), source('OwnerAuth.jsx'), source('RunList.jsx'),
    source('RunView.jsx'), source('Settings.jsx'), source('SharedAssistant.jsx'),
  ])
  assert.match(app, /document\.title = `\$\{label\} · LoopLab`/)
  assert.match(app, /document\.querySelector\('\[data-route-main\]'\)\?\.focus\(\)/)
  assert.match(auth, /data-route-main tabIndex=\{-1\}/)
  assert.match(auth, /gateRef\.current\?\.focus\(\{ preventScroll: true \}\)/)
  for (const [name, text] of [['RunList', list], ['RunView', runView], ['Settings', settings], ['SharedAssistant', shared]]) {
    assert.match(text, /data-route-main[^>]*tabIndex=\{-1\}/, `${name} needs a focusable main landmark`)
  }
})

test('compact Assistant blocks background pointers and traps focus in the side drawer', async () => {
  const [assistant, css] = await Promise.all([source('AssistantBar.jsx'), source('styles.css')])
  assert.match(assistant, /useMediaQuery\('\(max-width: 1279px\)'\)/)
  assert.match(assistant, /useDialogFocus\(sideDialogRef, collapseToBar, view === 'side' && compactAssistant && !hidden\)/)
  assert.match(assistant, /className="asst-side-backdrop" aria-hidden="true"[\s\S]*?onPointerDown=\{collapseToBar\}/)
  assert.match(assistant, /role=\{compactAssistant \? 'dialog' : undefined\} aria-modal=\{compactAssistant \? 'true' : undefined\}/)
  assert.match(assistant, /if \(next === 'side'\) requestAnimationFrame\(\(\) => inputRef\.current\?\.focus/)
  assert.match(css, /\.asst-side-backdrop \{ position: fixed; inset: 0; z-index: 57;/)
})

test('temporary create and delete workflows retain a connected focus destination', async () => {
  const runList = await source('RunList.jsx')
  assert.match(runList, /const projectModalReturnRef = useRef\(null\)/)
  assert.match(runList, /const closeProjectModal = \(\) => \{ setProjModal\(null\); restoreProjectModalFocus\(\) \}/)
  assert.match(runList, /fallbackFocus\?\.isConnected \? fallbackFocus : runsMainRef\.current/)
  assert.match(runList, /requestAnimationFrame\(\(\) => projectsAllRef\.current\?\.focus/)
  assert.match(runList, /if \(restoreFocus\) requestAnimationFrame/)
})

test('theme and energy menus restore focus and synchronize across tabs', async () => {
  const [theme, energy] = await Promise.all([source('ThemeSwitcher.jsx'), source('EnergyToggle.jsx')])
  for (const text of [theme, energy]) {
    assert.match(text, /aria-haspopup="menu"/)
    assert.match(text, /role="menuitemradio" aria-checked=/)
    assert.match(text, /className="th-backdrop" aria-hidden="true" onClick=\{\(\) => close\(true\)\}/)
    assert.match(text, /window\.addEventListener\('storage'/)
  }
})

test('splitters support visible keyboard focus and cancelled pointer gestures', async () => {
  const [runView, css] = await Promise.all([source('RunView.jsx'), source('styles.css')])
  assert.match(runView, /window\.addEventListener\('pointercancel', onUp\)/)
  assert.match(runView, /target\.setPointerCapture\?\.\(pointerId\)/)
  assert.match(css, /\.splitter \{[^}]*touch-action: none;/)
  assert.match(css, /\.splitter:focus-visible \{[^}]*outline: 2px solid var\(--accent\);/)
})

test('run header drill-down metrics are native buttons', async () => {
  const runView = await source('RunView.jsx')
  assert.equal((runView.match(/<button type="button" className="chip(?: alarm)? run-metric-chip"/g) || []).length, 3)
  assert.doesNotMatch(runView, /<span className="chip(?: alarm)?"[^>]*onClick=/)
})

test('reduced-motion disables every live CTRL activity indicator', async () => {
  const css = await source('styles.css')
  const reduced = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'))
  for (const selector of ['.cmdbar-pip', '.agent-status .as-dot', '.trace-live-status .tls-dot', '.live.on .led']) {
    assert.ok(reduced.includes(selector), `${selector} must be static under reduced motion`)
  }
})

test('Dock exposes accepted command records immediately instead of waiting in submitting copy', async () => {
  const dock = await source('Dock.jsx')
  assert.match(dock, /onRecord: next => \{[\s\S]*?persistTransport\(visible\)[\s\S]*?setTransportPending/)
  assert.match(dock, /idempotencyKey, expectedGeneration: generation, waitMs: 0/)
})

test('displayed run generation participates in state dedupe and is published only after commit', async () => {
  const hooks = await source('hooks.js')
  assert.match(hooks, /p\.seq === lastSeq && alive === lastAlive && nextGeneration === lastGeneration/)
  assert.match(hooks, /useLayoutEffect\(\(\) => \{ observeRunGeneration\(runId, generation\) \}/)
})

test('Config restart tracks both accepted/executing and terminates authoritative read failures', async () => {
  const panels = await source('panels.jsx')
  assert.match(panels, /!\['accepted', 'executing'\]\.includes\(pending\.record\.status\)/)
  assert.match(panels, /\[401, 403, 404\]\.includes\(error\?\.status\)[\s\S]*?setRestartPending\(null\)/)
})

test('both command surfaces fail before POST when durable intent persistence fails', async () => {
  const [assistant, dock] = await Promise.all([source('AssistantBar.jsx'), source('Dock.jsx')])
  assert.match(assistant, /if \(!persistDirect\(submitting\)\) \{ localStorageFailure\(bound\); return \}[\s\S]*?submitAssistantDirect/)
  assert.match(dock, /if \(!persistTransport\(start\)\) \{ storageTransportFailure\(action, idempotencyKey, generation\); return \}[\s\S]*?runCommand/)
})

test('Assistant renders durable Retry-same-command and actionable Dismiss states', async () => {
  const assistant = await source('AssistantBar.jsx')
  assert.match(assistant, /retryRunCommand\(failure\.runId, failure\.record\.id/)
  assert.match(assistant, />Retry same command<\/button>/)
  assert.match(assistant, /dismissDirectFailure/)
  assert.match(assistant, /role=\{directNeedsAlert \? 'alert' : 'status'\}/)
})

test('collapsed recovery actions are not clipped and retain a 44px touch target', async () => {
  const css = await source('assistant-polish.css')
  assert.match(css, /\.cmdbar-status\.recovery \{[\s\S]*?overflow: visible;[\s\S]*?white-space: normal;/)
  assert.match(css, /\.cmdbar-status\.recovery \.btn \{ min-height: 44px;/)
})

test('Assistant unmount cleanup clears its toast timer and every command poll clears its timer', async () => {
  const assistant = await source('AssistantBar.jsx')
  assert.match(assistant, /if \(flashTimerRef\.current\) clearTimeout\(flashTimerRef\.current\)/)
  assert.match(assistant, /return \(\) => \{ active = false; clearTimeout\(timer\) \}/)
})

test('direct command presentation is guarded by currentRunId while durable cleanup remains unconditional', async () => {
  const assistant = await source('AssistantBar.jsx')
  assert.match(assistant, /currentRunIdRef\.current = runId/)
  assert.match(assistant, /presentAssistantCommandResult\([\s\S]*?currentRunIdRef\.current, entry\.runId/)
  assert.match(assistant, /clearAssistantRunTransport\(entry\.runId[\s\S]*?setCurrentFailure\(entry/)
})

test('Assistant clears stale run-scoped toast immediately when the route run changes', async () => {
  const assistant = await source('AssistantBar.jsx')
  assert.match(assistant, /assistantRunChanged\(toastRunIdRef\.current, runId\)[\s\S]*?clearTimeout\(flashTimerRef\.current\)[\s\S]*?setToast\(null\)[\s\S]*?toastRunIdRef\.current = runId/)
  assert.match(assistant, /const visibleToast = assistantRunChanged\(toastRunIdRef\.current, runId\) \? null : toast/)
  assert.equal((assistant.match(/\{visibleToast && <div className="cmdbar-toast(?: side)?"/g) || []).length, 3)
})

test('moving pending or failed command UI to side/full re-arms status focus', async () => {
  const assistant = await source('AssistantBar.jsx')
  assert.match(assistant, /const openCommandView = \(next\) => \{[\s\S]*?if \(commandBusy \|\| directFailure\) commandFocusRequestedRef\.current = true[\s\S]*?setView\(next\)/)
  assert.match(assistant, /const openSide = \(\) => openCommandView\('side'\)/)
  assert.match(assistant, /const openFull = \(\) => openCommandView\('full'\)/)
  assert.match(assistant, /const collapseToBar = \(\) => \{[\s\S]*?if \(commandBusy \|\| directFailure\) commandFocusRequestedRef\.current = true[\s\S]*?setView\('bar'\)/)
  assert.match(assistant, /\[directPending\?\.record\?\.id,[\s\S]*?busy, view\]/)
})

test('an unsent Assistant storage quarantine keeps failure and Dismiss visible in the same tab', async () => {
  const assistant = await source('AssistantBar.jsx')
  assert.match(assistant, /const ownStorageFailureLock = assistantStorageFailureOwnsLock\(directFailure, runCommandLock\)/)
  assert.match(assistant, /const commandBusy = directPending != null \|\| \(runCommandLock != null && !ownStorageFailureLock\)/)
  assert.match(assistant, /const clearUnsentDirectRecovery = entry => \{[\s\S]*?clearAssistantRunTransport\(entry\.runId[\s\S]*?clearRunCommandLock\(entry\.runId, expected\)[\s\S]*?remainingTransport[\s\S]*?remainingLock/)
  assert.match(assistant, /const localStorageFailure = entry => \{[\s\S]*?clearUnsentDirectRecovery\(failure\)[\s\S]*?setCurrentFailure\(entry, failure\)/)
  assert.match(assistant, /failure\.record\?\.error\?\.code === 'command_storage_unavailable'[\s\S]*?!clearUnsentDirectRecovery\(failure\)[\s\S]*?remains quarantined/)
})

test('live node-detail and per-node building-trace polls gate their setState on usePoll alive()', async () => {
  // R5-UI: a poll whose callback ignores the alive() predicate lets a slow in-flight response land
  // after the user selected a different node (Inspector) or after building ended (Dock), overwriting
  // fresher state with a stale snapshot. Both must take (alive) and guard the setState with alive().
  const [inspector, dock] = await Promise.all([source('Inspector.jsx'), source('Dock.jsx')])
  // Inspector node-detail poll: callback receives (alive), rejects a STALER attempt (accepts fresher),
  // and only then writes. (R7: `>= nodeAttempt`, not exact — the detail endpoint often leads the poll.)
  assert.match(inspector, /const detailMatchesAttempt = value => !Number\.isSafeInteger\(nodeAttempt\)[\s\S]*?value\.attempt >= nodeAttempt/)
  assert.match(inspector, /usePoll\(\(alive\) => \{[\s\S]*?nodes\/\$\{nodeId\}`\)\.then\(d => \{\s*if \(alive\(\) && detailMatchesAttempt\(d\)\)/)
  // Dock building-trace poll: the O(node) callback receives alive, and every state write is gated.
  assert.match(dock, /const loadNodeTrace = \(alive\) => get\(`\/api\/runs\/\$\{runId\}\/nodes\/\$\{traceNid\}\/trace`\)[\s\S]*?\.then\(d => \{ if \(alive\(\)\) setNodeTrace\(d\) \}\)/)
  assert.match(dock, /usePoll\(\(alive\) => \{ setNodeTraceError\(false\); loadNodeTrace\(alive\) \}[\s\S]*?enabled: open && !readOnly && traceNid != null && exactBuilding/)
  assert.doesNotMatch(dock, /get\(`\/api\/runs\/\$\{runId\}\/trace`\)/,
    'the timeline must not regress to a whole-run trace fold/poll')
})

test('R6: attention delivery re-fires on eligibility flip and gates enable on a committed write', async () => {
  const ac = await source('AttentionCenter.jsx')
  // deliveryKey must include notifyEligible so a stale→fresh flip (same id+created) re-runs delivery.
  assert.match(ac, /deliveryItems[\s\S]*?\.map\(item => `\$\{item\.id\}:\$\{item\.created\}:\$\{item\.notifyEligible \? 1 : 0\}`\)/)
  // enableNotifications must gate valid:true on result.ok (mirroring disableNotifications).
  assert.match(ac, /if \(result\.state && result\.ok\) setPreferences\(\{ state: result\.state, available: true, valid: true \}\)/)
})

test('R6: Dock timeline-window note uses a boolean guard so no stray 0 renders', async () => {
  const dock = await source('Dock.jsx')
  // The && chain must start from a boolean, not `filter.trim() || kinds.size` (which is 0 when empty).
  assert.match(dock, /\{\(filter\.trim\(\) !== '' \|\| kinds\.size > 0\) && timeline\.totalEvents != null/)
})

test('R7: Inspector accepts a fresher node-detail payload (no spurious attempt-changed error)', async () => {
  const inspector = await source('Inspector.jsx')
  // detailMatchesAttempt must accept attempt >= summary (fresher after inline repair), not exact-only.
  assert.match(inspector, /Number\.isSafeInteger\(value\?\.attempt\) && value\.attempt >= nodeAttempt/)
})
