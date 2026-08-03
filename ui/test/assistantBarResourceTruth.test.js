import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const assistantSource = () => readFile(new URL('../src/AssistantBar.jsx', import.meta.url), 'utf8')
const section = (source, start, end) => source.slice(source.indexOf(start), source.indexOf(end))

test('session selection commits only a current, bounded read and preserves the prior transcript on failure', async () => {
  const source = await assistantSource()
  const open = section(source, 'const openSession =', 'openSessionRef.current = openSession')
  const read = open.indexOf('await boundedRequest(signal => assistantGet(id, { signal }))')
  const commit = open.indexOf('sidRef.current = id; setSid(id); setMsgs([])')

  assert.ok(read >= 0 && commit > read, 'the target must be read before replacing the visible session')
  // A purely OBSERVATIONAL read of the live session must not burn a sequence number — doing so
  // would supersede the session the user is actually watching. Every other path still claims a new
  // one. Anchoring on the unconditional `++` stopped matching when that distinction was introduced.
  assert.match(open, /const seq = observingLiveSession \? openSessionSeqRef\.current : \+\+openSessionSeqRef\.current/)
  assert.match(open, /const observingLiveSession = observeOnly && id === sidRef\.current && runningRef\.current/)
  assert.match(open, /seq !== openSessionSeqRef\.current/)
  assert.match(source, /const newChat = \(\) => \{\s*\+\+openSessionSeqRef\.current/)
})

test('session creation and send are single-flight while a failed create preserves the draft', async () => {
  const source = await assistantSource()
  const run = section(source, 'const runLLM =', 'const requestNewRun =')
  const send = section(source, 'const send =', 'useEffect(() => {\n    const onNewRun')
  const normalSend = send.slice(send.indexOf('const refs ='))

  assert.ok(run.indexOf('turnCaptureRef.current = true')
    < run.indexOf('await boundedRequest(signal => assistantCreate('))
  assert.match(run, /turnCaptureRef\.current \|\| runningRef\.current/)
  assert.match(run, /sessionSeq !== openSessionSeqRef\.current/)
  assert.match(run, /sessionSeq === openSessionSeqRef\.current\) flash\('Could not start the chat — your draft is preserved'\)/)
  assert.match(run, /finally[\s\S]*?turnCaptureRef\.current = false/)
  assert.doesNotMatch(normalSend, /setInput\(''\)/)
  assert.match(normalSend, /clearComposer: true/)
})

test('AssistantBar never renders raw exception messages and keeps feedback in an accessible live region', async () => {
  const source = await assistantSource()
  const flashLines = source.split('\n').filter(line => line.includes('flash('))

  assert.equal(flashLines.some(line => /(?:e2?|error)\?*\.message/.test(line)), false)
  assert.doesNotMatch(source, /\$\{error\?\.message \|\| error\}/)
  assert.match(source, /visibleToast && <div[^>]*role="status"[^>]*aria-live="polite"/)
})

test('public-link verification fences authority while unknown truth preserves local drafting', async () => {
  const source = await assistantSource()
  const refresh = section(source, 'const refreshSessions =', '// Share privacy terms matter')
  const exact = section(source, 'const applyAssistantShareMeta =', 'const refreshSessionShareMeta =')
  const verification = section(source, 'const verifyShareStatus =', 'const retryShareStatus =')

  assert.match(refresh, /const requestedSid = sidRef\.current/)
  assert.match(refresh, /const mutationEpoch = shareMutationEpochRef\.current/)
  assert.match(refresh, /requestSeq !== sessionsRequestSeqRef\.current\s*\|\| mutationEpoch !== shareMutationEpochRef\.current/)
  assert.match(refresh, /requestedSid && sidRef\.current === requestedSid/,
    'a late list failure must not pause whichever different chat happens to be open')
  assert.match(exact, /shareMetaReadCurrent\(read\)[\s\S]*?sessionsRequestSeqRef\.current \+= 1/,
    'an exact per-session receipt must supersede older list reads')
  assert.match(source, /listRequestSeq: sessionsRequestSeqRef\.current/,
    'a list read that starts later must supersede the older exact receipt')
  assert.match(exact, /invalidatedListRequest[\s\S]*?refreshSessionsRef\.current\?\.\(\)/,
    'superseding an active list must restart it instead of leaving loading state stranded')
  assert.match(source, /activeSessionsRequestRef\.current = null[\s\S]*?setShareVerifySid\(null\)[\s\S]*?setSessions\(update\)/,
    'a local mutation must settle older list and verification ownership before publishing its receipt')
  assert.match(source, /shareMutationEpochRef\.current \+= 1[\s\S]*?setSessions\(update\)/,
    'confirmed local mutations must invalidate exact reads that started before their receipt')

  assert.match(source, /const composerPaused = sharePaused \|\| forkingCurrentSession/,
    'unknown truth still fences every Assistant mutation')
  assert.match(source, /const composerEditingPaused = shareBusy \|\| forkingCurrentSession/,
    'passive unknown truth must not disable a local draft')
  assert.match(source, /<textarea[\s\S]*?disabled=\{historical \|\| commandBusy \|\| composerEditingPaused\}/)
  assert.match(source, /className="cmdbar-in"[\s\S]*?disabled=\{historical \|\| commandBusy \|\| composerEditingPaused\}/)
  assert.match(source, /aria-label=\{shareUnknown \|\| shareVerifying \? shareVerifying[\s\S]*?'Verify public-link status before sending'/,
    'the still-focusable mutation boundary must explain why activation verifies instead of sending')
  assert.match(source, /const verifyShareStatus = \(targetSid, retrySuperseded = true\) => \{[\s\S]*?active\.sid === target && active\.promise[\s\S]*?draft is ready to send/,
    'every verification entry point must announce when a preserved draft is ready')
  assert.ok(verification.indexOf("shareVerificationRef.current = { seq, sid: null, promise: null }")
    < verification.indexOf('sidRef.current !== target'),
    'switching chats must release the matching verification before suppressing its toast')
  assert.match(source, /session === undefined[\s\S]*?status changed while checking/,
    'a superseded exact read must not be announced as a verification failure')
  assert.match(source, /if \(retrySuperseded\) return verifyShareStatus\(target, false\)/,
    'one newer authority read may be followed by one exact verification retry')
  assert.match(source, /const shareMetaOutcome = applyAssistantShareMeta[\s\S]*?shareMetaOutcome === undefined[\s\S]*?exactState = 'idle'/,
    'durable recovery must retry after newer authority supersedes its exact share read')
  assert.match(source, /if \(shareUnknown \|\| shareVerifying\) \{\s*return \(\) => verifyShareStatus/,
    'a focused transcript Retry must remain mounted as a verification boundary')
  assert.match(source, /retryBusy=\{shareVerifying\}/,
    'the mounted transcript verification boundary must expose its busy state')
  assert.match(source, /id="assistant-share-status"[\s\S]*?role="status" aria-live="polite" aria-atomic="true">\s*\{shareStatusMessage\}/,
    'all Assistant surfaces need one stable announcement when passive verification pauses sending')
  assert.match(source, /aria-describedby=\{shareUnknown \|\| shareVerifying \? 'assistant-share-status' : undefined\}/,
    'the compact draft must expose the reason Send is acting as a verification boundary')
})
