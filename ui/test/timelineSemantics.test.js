import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('RunView owns one paged timeline shared by Dock and EventExplorer', async () => {
  const [runView, dock, panels] = await Promise.all([source('RunView.jsx'), source('Dock.jsx'), source('panels.jsx')])
  assert.match(runView, /const timeline = useTimeline\(runId/)
  assert.match(runView, /const timelineDeferred = view === 'report' && panel !== 'events'/)
  assert.match(runView, /enabled: !reviewMode && !routeFenceBlocked && !timelineDeferred/,
    'a terminal auto-report render must not start the owner timeline request')
  assert.match(runView, /const activateReportTimeline = \(\) => \{[\s\S]*?setTimelineActivation\(\{ runId, active: true, focusPending: true \}\)/,
    'expanding the Report timeline must activate its data source')
  assert.match(runView, /\(timelineDeferred \|\| reportTimelineFocusPending\)[\s\S]*?<div className="dock timeline-deferred-trigger"/,
    'Report renders a lightweight trigger without mounting the lazy Dock')
  assert.match(runView, /!reviewMode && !timelineDeferred && <LazyBoundary label="timeline"/)
  assert.match(runView, /focusOnMount=\{reportTimelineFocusPending\}/)
  assert.match(dock, /collapseButtonRef\.current\?\.focus\(\{ preventScroll: true \}\)/,
    'focus must move from the lazy trigger to the loaded Dock control')
  assert.match(runView, /const autoReport =[\s\S]*?const view = autoReport \? 'report' : requestedView/,
    'terminal routing must select Report before lazy route children render')
  assert.match(runView, /<Dock[\s\S]*?timeline=\{timeline\}/)
  assert.match(runView, /<EventExplorer runId=\{runId\} timeline=\{timeline\}/)
  assert.match(runView, /const returnToLive = \(\) => \{[\s\S]*?changeViewSeq\(null\)[\s\S]*?timeline\.jumpToLive\(\)/)
  // The two visible buttons now go through the focus-moving wrapper rather than `returnToLive`
  // directly. Pin the wrapper AND its refusal guard: leaving history is what earns the focus move,
  // so a refused return must not steal focus into a workspace that is still showing a snapshot.
  assert.equal((runView.match(/onClick=\{returnToLiveAndFocusWorkspace\}>Return to live/g) || []).length, 2)
  assert.match(runView, /const returnToLiveAndFocusWorkspace = \(\) => \{\s*if \(!returnToLive\(\)\) return/,
    'a refused return to live must not move focus')
  assert.match(runView, /const MIN_DOCK_HEIGHT = 200/)
  assert.match(runView, /aria-valuemin=\{MIN_DOCK_HEIGHT\}/)
  assert.doesNotMatch(dock, /\/api\/runs\/\$\{runId\}\/log[`'?]/)
  assert.doesNotMatch(panels.slice(panels.indexOf('export function EventExplorer')), /\/log[`'?]/)
})

test('historical state reloads and renders only for the displayed run generation', async () => {
  const runView = await source('RunView.jsx')
  assert.match(runView, /requestHistory\(want, generation\)/)
  assert.match(runView, /resolveHistory\(current, want, generation, p\)/)
  // The catch now distinguishes a TIMEOUT from a transport error before rejecting, so the operator
  // is told the snapshot request ran out of time rather than shown a raw failure. The `e` parameter
  // name went with it; pin the substitution and the distinction rather than the old spelling.
  assert.match(runView, /rejectHistory\(current, want, generation, failure\)/)
  assert.match(runView, /error\?\.name === 'TimeoutError'\s*\?\s*new Error\('Historical snapshot loading timed out\./,
    'a historical snapshot deadline must read as a timeout, not as an opaque transport failure')
  assert.match(runView, /\[viewSeq, runId, historyRetry, generation, routeFenceBlocked, reviewMode\]/)
  assert.match(runView, /historyMatches\(history, viewSeq, generation\)/)
})

test('generation-scoped event counts flow from run state into exact timeline lag and unread state', async () => {
  const [hooks, runView, timeline] = await Promise.all([
    source('hooks.js'), source('RunView.jsx'), source('useTimeline.js'),
  ])
  assert.match(hooks, /const eventCount = eventCountState\.runId === runId/)
  // The identity rule itself moved into ./src/runStateModel.js (doc 25 UI-09) and is now DRIVEN in
  // runStateModel.test.js — including the part this pin used to stand for: that a frame whose only
  // change is `event_count` publishes a new snapshot. What has to stay true HERE is the join: the
  // effect consults the shared rule and commits the event count that the timeline below reads.
  assert.match(hooks, /if \(!identityChanged\(last, next\)\) return next[\s\S]*?setEventCountState\(\{ runId, value: next\[3\] \}\)/)
  assert.match(hooks, /return \{ live, seq, generation, eventCount, connected/)
  assert.match(runView, /eventCount: liveEventCount/)
  assert.match(runView, /liveSeq: seq, liveEventCount, expectedGeneration: generation/)
  assert.match(timeline, /timelineBehindLive\([\s\S]*?latestLiveEventCountRef\.current\)/)
  assert.match(timeline, /const observed = lagging && merged\.windowAtTail \? \{ \.\.\.merged, windowAtTail: false \} : merged/)
  assert.match(timeline, /commit\(observed\)[\s\S]*?return observed/)
  assert.match(timeline, /const identityCurrent = runRef\.current === runId[\s\S]*?state\.generation === expectedGeneration/,
    'a run or generation replacement must hide the previous rows during the reset render')
  assert.match(timeline, /const visibleState = identityCurrent \? state : \{[\s\S]*?generationChanged: true/)
})

test('Dock uses around paging, local-only drag preview, native event controls, and retained expansion state', async () => {
  const dock = await source('Dock.jsx')
  assert.match(dock, /timeline\.ensureSeq\(viewSeq\)/)
  assert.match(dock, /dragRef\.current = v[\s\S]*?setDrag\(v\)/)
  assert.doesNotMatch(dock, /setTimeout\(\(\) => \{[^}]*commit\(v\)/)
  assert.match(dock, /<button type="button" className="fm-tw" aria-expanded=\{open\}/)
  assert.match(dock, /<button type="button" className="fm-main"/)
  assert.match(dock, /const \[eventExpansion, setEventExpansion\] = useState/)
  assert.match(dock, /expansion=\{eventExpansion\.get\(key\) \|\| CLOSED_EXPANSION\}/)
  assert.match(dock, /!showControls && \(\(\) => \{[\s\S]*?dock-agent-status/)
})

test('expanded node trace polls only its exact live lifecycle and refreshes once after settle', async () => {
  const [dock, api] = await Promise.all([source('Dock.jsx'), source('api.js')])
  assert.match(dock,
    /traceDeadlineGet\(runNodeApiPath\(runId, traceNid, '\/trace'\),[\s\S]*?expectedTraceGeneration, traceGeneration, nodeTraceLimit\)/)
  assert.match(api,
    /export const traceReadQuery = \(expectedGeneration, attempt, limit\) => \{[\s\S]*?query\.set\('attempt', attempt\)[\s\S]*?query\.set\('limit', limit\)[\s\S]*?query\.set\('expected_generation', expectedGeneration\)/)
  assert.match(dock,
    /d\?\.node_id !== traceNid \|\| d\?\.attempt !== traceGeneration[\s\S]*?!traceGenerationMatches\(d, expectedTraceGeneration\)/)
  // liveBuilding maps nodeId to generation for EVERY concurrent build (parallel_build>1), so each
  // in-flight build's row polls its own exact lifecycle — not just the singular last-appended one.
  assert.match(dock, /timeline\.generation !== expectedGeneration[\s\S]*?buildingGenerations\(live\)/)
  assert.match(dock, /const exactBuilding =[\s\S]*?liveBuilding\[traceNid\] === traceGeneration/)
  assert.match(dock, /usePoll\(\(alive\) => \{[\s\S]*?\}, exactBuilding \? 4000 : null,[\s\S]*?enabled: open && !readOnly && traceNid != null && traceGeneration != null/)
  assert.match(dock, /\[open, readOnly, runId, expectedTraceGeneration, traceNid, traceGeneration, exactBuilding,[\s\S]*?nodeTraceNonce, nodeTraceLimit\]/)
  assert.doesNotMatch(dock, /get\(`\/api\/runs\/\$\{runId\}\/trace`\)/)
  assert.match(dock, /const expectedGeneration = normalizeRunGeneration\(generation\)[\s\S]*?const scope = expectedGeneration \|\| runId[\s\S]*?tailState\.scope === scope/,
    'live trace rows must disappear immediately when the run generation changes')
})

test('virtual timeline is bounded, variable-height, generation-scoped, and politely announces unread events', async () => {
  const [virtual, css] = await Promise.all([source('VirtualTimeline.jsx'), source('styles.css')])
  assert.match(virtual, /data-event-row role="listitem"/)
  assert.match(virtual, /ResizeObserver/)
  assert.match(virtual, /`\$\{identity\}:\$\{getKey\(row, index\)\}`/)
  assert.match(virtual, /setProgrammaticScroll\(tailScrollTop\(element\.scrollHeight, element\.clientHeight\)\)/)
  assert.match(virtual, /scrollWriteTokenRef\.current === writeToken[\s\S]*?suppressScrollRef\.current = false/)
  assert.match(virtual, /identityResetRef\.current = followingRef\.current[\s\S]*?suppressScrollRef\.current = followingRef\.current/)
  assert.match(virtual, /suppressScrollRef\.current \|\| identityResetRef\.current/)
  assert.match(virtual, /requestAnimationFrame\(\(\) => verify\(aligned \? stableFrames \+ 1 : 0\)\)/)
  assert.match(virtual, /!aligned && windowAtTail[\s\S]*?onFollowingTailChange\?\.\(false\)/)
  assert.match(virtual, /timeline-unread-status" role="status"[\s\S]*?aria-live="polite"/)
  assert.match(css, /\.timeline-virtual-row \{ position: absolute;/)
  assert.match(css, /\.dock-body\.chat-body \{ overflow: hidden; \}/)
})

test('feed-hidden bookkeeping keeps the pagebar and unread badge honest about what renders', async () => {
  const [dock, narration] = await Promise.all([source('Dock.jsx'), source('narration.js')])
  // The curated feed is an ALLOW-LIST: only narrated event types render. Everything unnarrated (finalize
  // gates, per-call cost, the concept-cadence read-model sidecars, verifier scores, resume/restart
  // plumbing) is kept out — so a new event type can never regress into a raw-JSON blob in the feed.
  // The allow-list itself moved to the narration model (doc 25 UI-08); Dock still consumes it.
  assert.match(narration, /export const isCuratedType = \(type\) => Object\.hasOwn\(NARR, type\)/)
  // A run carrying any non-curated row is detected so the counts can compensate.
  assert.match(dock, /const hiddenPresent = useMemo\(\(\) => log\.some\(\(e\) => !isCuratedType\(e\.type\)\), \[log\]\)/)
  // Pagebar: whenever the feed is a strict subset of the loaded window (hidden rows or a time-scrub),
  // it names the shown count instead of silently mismatching `${log.length} loaded`.
  assert.match(dock, /feed\.length !== log\.length[\s\S]*?\$\{feed\.length\} \$\{\(filter\.trim\(\) \|\| kinds\.size\) \? 'matching' : 'shown'\}/)
  // Unread badge: an exact number only when no hidden row can inflate it; otherwise the numberless
  // "new activity" affordance rather than a count the feed can't honor.
  assert.match(dock, /unread=\{atLiveView && !hiddenPresent \? timeline\.unread : 0\}/)
  assert.match(dock, /unreadUnknown=\{atLiveView && \(timeline\.unreadUnknown \|\| \(hiddenPresent && timeline\.unread > 0\)\)\}/)
  // A window that loaded only hidden rows must not read as "no events yet".
  assert.match(dock, /only background bookkeeping in this window — page older for run events/)
})

test('bounded omissions and loaded-window search limitations are explicit on both event surfaces', async () => {
  const [dock, panels, narration] = await Promise.all([
    source('Dock.jsx'), source('panels.jsx'), source('narration.js')])
  // The omission notice is written by the narration model (doc 25 UI-08); the row that shows the same
  // receipt inline still lives in Dock. `dockNarration.test.js` drives the narrated wording for real.
  assert.match(narration, /details omitted \(\$\{omittedBytes\.toLocaleString\(\)\} source bytes exceed page limit\)/)
  assert.match(dock, /\{omittedBytes\.toLocaleString\(\)\} source bytes exceed the bounded page response/)
  assert.match(dock, /Filters search loaded events only/)
  assert.match(dock, /rawPreview = JSON\.stringify\(event\.data \?\? \{\}\)\.slice\(0, 500\)/)
  assert.match(dock, /`\$\{event\.type \|\| ''\} \$\{narration\} \$\{rawPreview\}`\.toLowerCase\(\)/)
  assert.match(panels, /details omitted · \$\{bytes\.toLocaleString\(\)\} source bytes exceed page limit/)
  assert.match(panels, /Search covers the loaded window only/)
  assert.doesNotMatch(panels.slice(panels.indexOf('export function EventExplorer')), /title=\{JSON\.stringify/)
})
