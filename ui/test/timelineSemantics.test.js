import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('RunView owns one paged timeline shared by Dock and EventExplorer', async () => {
  const [runView, dock, panels] = await Promise.all([source('RunView.jsx'), source('Dock.jsx'), source('panels.jsx')])
  assert.match(runView, /const timeline = useTimeline\(runId/)
  assert.match(runView, /<Dock[\s\S]*?timeline=\{timeline\}/)
  assert.match(runView, /<EventExplorer runId=\{runId\} timeline=\{timeline\}/)
  assert.match(runView, /const returnToLive = \(\) => \{[\s\S]*?changeViewSeq\(null\)[\s\S]*?timeline\.jumpToLive\(\)/)
  assert.equal((runView.match(/onClick=\{returnToLive\}>Return to live/g) || []).length, 2)
  assert.match(runView, /const MIN_DOCK_HEIGHT = 200/)
  assert.match(runView, /aria-valuemin=\{MIN_DOCK_HEIGHT\}/)
  assert.doesNotMatch(dock, /\/api\/runs\/\$\{runId\}\/log[`'?]/)
  assert.doesNotMatch(panels.slice(panels.indexOf('export function EventExplorer')), /\/log[`'?]/)
})

test('historical state reloads and renders only for the displayed run generation', async () => {
  const runView = await source('RunView.jsx')
  assert.match(runView, /requestHistory\(want, generation\)/)
  assert.match(runView, /resolveHistory\(current, want, generation, p\)/)
  assert.match(runView, /rejectHistory\(current, want, generation, e\)/)
  assert.match(runView, /\[viewSeq, runId, historyRetry, generation, routeFenceBlocked, reviewMode\]/)
  assert.match(runView, /historyMatches\(history, viewSeq, generation\)/)
})

test('generation-scoped event counts flow from run state into exact timeline lag and unread state', async () => {
  const [hooks, runView, timeline] = await Promise.all([
    source('hooks.js'), source('RunView.jsx'), source('useTimeline.js'),
  ])
  assert.match(hooks, /const eventCount = eventCountState\.runId === runId/)
  assert.match(hooks, /nextEventCount === lastEventCount/)
  assert.match(hooks, /return \{ live, seq, generation, eventCount, connected/)
  assert.match(runView, /eventCount: liveEventCount/)
  assert.match(runView, /liveSeq: seq, liveEventCount, expectedGeneration: generation/)
  assert.match(timeline, /timelineBehindLive\([\s\S]*?latestLiveEventCountRef\.current\)/)
  assert.match(timeline, /const observed = lagging && merged\.windowAtTail \? \{ \.\.\.merged, windowAtTail: false \} : merged/)
  assert.match(timeline, /commit\(observed\)[\s\S]*?return observed/)
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
  const dock = await source('Dock.jsx')
  assert.match(dock, /get\(`\/api\/runs\/\$\{runId\}\/nodes\/\$\{traceNid\}\/trace`\)/)
  assert.match(dock, /timeline\.generation !== expectedGeneration[\s\S]*?live\.building\.generation[\s\S]*?return \{ nodeId, generation \}/)
  assert.match(dock, /const exactBuilding =[\s\S]*?traceGeneration === liveBuilding\.generation/)
  assert.match(dock, /usePoll\([\s\S]*?4000,[\s\S]*?enabled: open && !readOnly && traceNid != null && exactBuilding/)
  assert.match(dock, /if \(!open \|\| readOnly \|\| traceNid == null \|\| exactBuilding\) return undefined[\s\S]*?\[open, readOnly, runId, traceNid, exactBuilding, nodeTraceNonce\]/)
  assert.doesNotMatch(dock, /get\(`\/api\/runs\/\$\{runId\}\/trace`\)/)
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

test('bounded omissions and loaded-window search limitations are explicit on both event surfaces', async () => {
  const [dock, panels] = await Promise.all([source('Dock.jsx'), source('panels.jsx')])
  assert.match(dock, /details omitted \(\$\{omittedBytes\.toLocaleString\(\)\} source bytes exceed page limit\)/)
  assert.match(dock, /Filter searches this loaded window/)
  assert.match(dock, /rawPreview = JSON\.stringify\(event\.data \?\? \{\}\)\.slice\(0, 500\)/)
  assert.match(dock, /`\$\{event\.type \|\| ''\} \$\{narration\} \$\{rawPreview\}`\.toLowerCase\(\)/)
  assert.match(panels, /details omitted · \$\{bytes\.toLocaleString\(\)\} source bytes exceed page limit/)
  assert.match(panels, /Search covers the loaded window only/)
  assert.doesNotMatch(panels.slice(panels.indexOf('export function EventExplorer')), /title=\{JSON\.stringify/)
})
