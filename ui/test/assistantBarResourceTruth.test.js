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
  assert.match(open, /id === sidRef\.current && !observeOnly && \(!recover \|\| runningRef\.current\)/,
    'an ordinary reselect of current A must supersede a pending B read without launching a late A GET')
  assert.match(open, /const currentNeedsRecovery = id === sidRef\.current && !!danglingAssistantTurn\(msgs\)[\s\S]*?&& !currentNeedsRecovery/,
    'reselecting an observe-only dangling chat must be able to promote it to exact recovery')
  assert.match(open, /seq !== openSessionSeqRef\.current/)
  assert.match(open, /sessionRead = observingLiveSession \? null\s*: \{ seq, id, allowsRecovery: !observeOnly, promise: null \}[\s\S]*?openSessionPendingRef\.current = sessionRead/)
  assert.match(open, /requestedAllowsRecovery = options\.observeOnly !== true[\s\S]*?matchingRead\.allowsRecovery \|\| !requestedAllowsRecovery[\s\S]*?return matchingRead\.promise/,
    'same-target reads dedupe only when the pending operation is at least as authoritative')
  assert.match(open, /sessionDeleteTombstonesRef\.current\.has\(String\(id\)\)/,
    'a transcript read must not commit a chat whose deletion already started')
  assert.match(open, /turnCaptureRef\.current && !sidRef\.current[\s\S]*?reason: 'turn-starting'/,
    'session navigation must not orphan a newly-created chat before its first turn is bound')
  assert.match(open, /deferredRecoverySessionRef\.current = dangling && !observeOnly && historicalRef\.current[\s\S]*?const inFlight = !!dangling && !historicalRef\.current/,
    'read-only routes may hydrate a dangling turn but must not reattach or replay it')
  assert.match(open, /const startExactRecovery = async \(\) => \{\s*if \(historicalRef\.current[\s\S]*?await boundedRequest[\s\S]*?if \(historicalRef\.current \|\| !replyAttemptCurrent/,
    'exact recovery must recheck live access across its final transcript read')
  assert.match(source, /if \(historical\) return\s*const deferred = deferredRecoverySessionRef\.current[\s\S]*?openSession\(deferred, \{ recover: true \}\)[\s\S]*?\[historical, sessionOpening\]/,
    'returning live must resume only the exact session whose recovery was deferred')
  assert.match(source, /aria-busy=\{String\(openingSid \|\| ''\) === String\(s\.id\) \|\| undefined\}/)
  assert.match(source, /disabled=\{s\.cleanup_required === true \|\| turnStarting \|\| retryChecking[\s\S]*?String\(openingSid \|\| ''\) === String\(s\.id\)\}/,
    'the target row must expose and gate its pending selection')
  assert.match(source, /rememberBoundedMapValue\(sessionDeleteTombstonesRef\.current, String\(id\), 'pending'\)[\s\S]*?pendingOpen && String\(pendingOpen\.id\) === String\(id\)[\s\S]*?\+\+openSessionSeqRef\.current/,
    'deleting a selected target must synchronously supersede its pending transcript read')
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
  assert.match(run, /runningRef\.current \|\| directCaptureRef\.current/,
    'an Assistant turn must not overlap a direct-command preflight')
  assert.match(run, /const sessionSeq = \+\+openSessionSeqRef\.current/,
    'Send must claim a newer choice epoch before session creation can race an older session read')
  assert.match(run, /sessionSeq !== openSessionSeqRef\.current/)
  assert.match(run, /sessionSeq === openSessionSeqRef\.current\) flash\('Could not start the chat — your draft is preserved'\)/)
  assert.match(run, /id = m\.id[\s\S]*?sidRef\.current = id[\s\S]*?if \(historicalRef\.current\)[\s\S]*?your draft is preserved/,
    'a session-create receipt must be bound before a read-only transition stops the unsent first turn')
  assert.match(run, /finally[\s\S]*?turnCaptureRef\.current = false/)
  assert.ok(run.indexOf('await cancelReqRef.current') < run.indexOf("setInput('')"),
    'Stop handoff must settle before consuming the draft')
  assert.match(run, /await cancelReqRef\.current[\s\S]*?const accessBecameReadOnly = historicalRef\.current[\s\S]*?\|\| accessBecameReadOnly/,
    'a route that becomes read-only during Stop handoff must preserve the draft and prevent the POST')
  assert.match(send, /turnCaptureRef\.current \|\| directCaptureRef\.current[\s\S]*?Another action is already starting/,
    'the shared Send surface must gate both mutation preflights synchronously')
  assert.match(source, /disabled=\{turnStarting \|\| retryChecking\} onClick=\{newChat\}/,
    'a first-turn session create must not be orphaned by starting another chat')
  assert.doesNotMatch(normalSend, /setInput\(''\)/)
  assert.match(normalSend, /clearComposer: true/)
})

test('direct-command recovery shares the Assistant mutation gate', async () => {
  const source = await assistantSource()
  const launch = section(source, 'const runDirect =', 'const checkDirect =')
  const check = section(source, 'const checkDirect =', 'const retryDirect =')
  const retry = section(source, 'const retryDirect =', 'const dismissDirectFailure =')

  assert.match(launch, /const directRunId = runId[\s\S]*?await get\(runApiPath\(directRunId, '\/state'\)\)[\s\S]*?currentRunIdRef\.current[\s\S]*?runId: directRunId/,
    'an async direct-command preflight must remain bound to the run that the user acted on')
  for (const handler of [check, retry]) {
    assert.match(handler, /historicalRef\.current[\s\S]*?flash\(readOnlyAction\)/,
      'direct-command recovery must not start from a read-only run view')
    assert.match(handler, /directCaptureRef\.current \|\| turnCaptureRef\.current \|\| runningRef\.current[\s\S]*?openSessionPendingRef\.current/)
    assert.match(handler, /directCaptureRef\.current = true[\s\S]*?finally[\s\S]*?directCaptureRef\.current = false/,
      'every recovery request must synchronously own and release the shared direct-command gate')
  }
  assert.match(source, /const directRecoveryPaused = historical \|\| sessionOpening \|\| turnStarting \|\| retryChecking \|\| busy/)
  assert.match(source, /canCheckDirect && <button[^>]*disabled=\{directRecoveryPaused\}/)
  assert.match(source, /canRetryDirect && <button[^>]*disabled=\{directRecoveryPaused\}/)
  assert.match(source, /const dismissProtocolDirect = \(\) => \{[\s\S]*?directCaptureRef\.current[\s\S]*?Wait for the current command check/)
  assert.equal((source.match(/directPending\?\.protocolInvalid && <button[\s\S]*?disabled=\{!!directPending\.checking\}/g) || []).length, 2,
    'protocol recovery cannot be dismissed while its late read can still restore a hidden transport lock')
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

  assert.match(source, /const composerPaused = sessionOpening \|\| turnStarting \|\| retryChecking \|\| sharePaused \|\| forkingCurrentSession/,
    'unknown truth still fences every Assistant mutation')
  assert.match(source, /const composerEditingPaused = sessionOpening \|\| shareBusy \|\| forkingCurrentSession/,
    'session switching freezes the old composer while passive share uncertainty remains editable')
  assert.match(source, /<textarea[\s\S]*?disabled=\{historical \|\| commandBusy \|\| composerEditingPaused\}/)
  assert.match(source, /className="cmdbar-in"[\s\S]*?disabled=\{historical \|\| commandBusy \|\| composerEditingPaused\}/)
  assert.match(source, /const onFocusAssistant = event => \{\s*if \(openSessionPendingRef\.current\)/)
  assert.match(source, /const onNewRun = \(event\) => \{\s*event\.preventDefault\(\)\s*if \(openSessionPendingRef\.current\)/)
  assert.match(source, /const chooseSuggestion = [\s\S]*?\{\s*if \(openSessionPendingRef\.current\) return/)
  assert.match(source, /className="asst-hint"\s*disabled=\{composerEditingPaused\}[\s\S]*?if \(openSessionPendingRef\.current\) return/)
  assert.match(source, /aria-label=\{`Detach experiment \$\{id\}`\} disabled=\{composerEditingPaused\}[\s\S]*?if \(openSessionPendingRef\.current\) return/,
    'every draft shortcut must honor the same synchronous session-opening fence as the inputs')
  assert.match(source, /aria-label=\{sessionOpening \? 'Opening selected Assistant chat'[\s\S]*?turnStarting \? 'Starting Assistant response'[\s\S]*?'Verify public-link status before sending'/,
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

test('Assistant reply completion is owned by one exact attempt and one exact user turn', async () => {
  const source = await assistantSource()
  const open = section(source, 'const openSession =', 'openSessionRef.current = openSession')
  const completion = section(source, 'const completedAssistantReply =', 'const recoverReply =')
  const recovery = section(source, 'const recoverReply =', '// Stream one instruction')
  const run = section(source, 'const runLLM =', 'const requestNewRun =')
  const retry = section(source, 'const retryTurn =', 'const openAssistantSettings =')
  const stop = section(source, 'const stop =', 'const activeMode =')

  assert.match(source, /const activeReplyAttemptRef = useRef\(null\)/)
  assert.match(source, /const replyAttemptCurrent = \(attempt, id\) => replyAttemptOwned\(attempt, id\) && runningRef\.current/)
  assert.match(open, /const reattachAttempt = \{\}[\s\S]*?activeReplyAttemptRef\.current = reattachAttempt/)
  assert.match(open, /seq !== openSessionSeqRef\.current \|\| sidRef\.current !== id[\s\S]*?openSessionPendingRef\.current !== sessionRead/,
    'the progress await must retain the session-selection sequence fence')
  assert.match(open, /sessionRead = observingLiveSession \? null\s*: \{ seq, id, allowsRecovery: !observeOnly, promise: null \}[\s\S]*?finally \{[\s\S]*?openSessionPendingRef\.current === sessionRead/,
    'the selected transcript must own the progress-read window so Send cannot replace its token')
  assert.match(open, /recoverReply\(id, arr\.length \+ 1, dangling, reattachAttempt/)
  assert.match(open, /activeReplyAttemptRef\.current === reattachAttempt[\s\S]*?runningRef\.current = false/,
    'an old reattach finally must not release a newer same-session attempt')

  assert.match(recovery, /const recoverReply = async \(id, priorLen, prior, attempt, authoritativeFailure = null\)/)
  assert.match(completion, /assistantReplyCompletesTurn\(messages, prior\)/)
  assert.match(completion, /assistantTurnIndex\(messages, prior\)/)
  const recoveryRead = recovery.indexOf('await boundedRequest(signal => assistantGet(id, { signal }), 8000)')
  const recoveryFence = recovery.indexOf('if (!replyAttemptCurrent(attempt, id)) return false', recoveryRead)
  const recoveryCommit = recovery.indexOf('setMsgs(arr)', recoveryFence)
  assert.ok(recoveryRead >= 0 && recoveryFence > recoveryRead && recoveryCommit > recoveryFence,
    'a late recovery GET must re-check attempt ownership before replacing the transcript')

  assert.ok(run.indexOf('activeReplyAttemptRef.current = localAttempt')
    < run.indexOf('await boundedRequest(signal => assistantCreate('),
  'session creation itself must belong to the attempt that captured the composer')
  assert.match(run, /const safeAttempt = \(fn\)[\s\S]*?replyAttemptCurrent\(localAttempt, id\)/)
  assert.match(run, /completedAssistantReply\(durableMessages, localUserTurn\)/,
    'terminal reconciliation must bind the reply to the optimistic user identity')
  assert.match(run, /exactTurnIndex >= 0 && exactTurnIndex === durableMessages\.length - 1/,
    'terminal failure recovery must not attach itself to another tab\'s trailing user turn')
  assert.match(run, /activeReplyAttemptRef\.current === localAttempt[\s\S]*?turnCaptureRef\.current = false/,
    'only the owning attempt may release the shared send capture')
  assert.match(retry, /const retrySessionSeq = \+\+openSessionSeqRef\.current[\s\S]*?replyAttemptOwned\(retryCheckAttempt, id\)[\s\S]*?retrySessionSeq !== openSessionSeqRef\.current/,
    'saved-turn preflight must not race a new Send or a session round trip')
  assert.match(retry, /await boundedRequest\(signal => assistantGet\(id, \{ signal \}\)\)[\s\S]*?historicalRef\.current \|\| !replyAttemptOwned/,
    'a saved-turn check must revalidate live run access after its network boundary')
  assert.match(retry, /clearReplyAnnouncement\(\)\s*setRetryChecking\(true\)[\s\S]*?finally[\s\S]*?setRetryChecking\(false\)/,
    'saved-turn preflight must own a visible, one-shot checking state')
  assert.match(retry, /exactTurnIndex === durableMessages\.length - 1 && danglingAssistantTurn\(durableMessages\)/,
    'Retry may recover only the exact user turn that owns the clicked failure')
  assert.match(stop, /activeReplyAttemptRef\.current = null\s*turnCaptureRef\.current = false\s*setTurnStarting\(false\)\s*setRetryChecking\(false\)\s*runningRef\.current = false/,
    'Stop must synchronously invalidate callbacks and make the composer reusable')
  assert.match(source, /if \(runningRef\.current\) \{ stop\(\); return \}[\s\S]*?\+\+openSessionSeqRef\.current[\s\S]*?setRetryChecking\(false\)/,
    'entering history must invalidate a response or retry preflight before it can mutate')
  assert.match(source, /turnCaptureRef\.current && !sidRef\.current && !retryChecking\) return/,
    'history must retain ownership long enough to bind an in-flight first-session creation receipt')
  assert.match(source, /\(composerPaused && !retryChecking\)[\s\S]*?retryBusy=\{shareVerifying \|\| retryChecking\}/,
    'Retry must remain mounted and aria-disabled while its saved-turn check owns focus')
})

test('Assistant response announcements describe a new completion, never hydrated presentation state', async () => {
  const source = await assistantSource()
  const collapse = section(source, 'const collapseToBar =', 'collapseForNavigationRef.current =')

  assert.match(source, /const \[replyAnnouncement, setReplyAnnouncement\] = useState\(''\)/)
  assert.match(source, /const announceReplyReady = React\.useCallback\(\(content, \{ sessionId = null, turn = null,/)
  assert.match(source, /announcedReplyTurnsRef\.current\.has\(turnKey\)[\s\S]*?announcedReplyAttemptsRef\.current\.has\(attempt\)/,
    'one exact turn completion must not be announced twice')
  assert.match(source, /\{sessionOpening \? 'Opening Assistant chat\.'[\s\S]*?retryChecking \? 'Checking saved Assistant turn\.'[\s\S]*?turnStarting \? 'Starting Assistant response\.'[\s\S]*?busy \? 'Assistant is responding\.' : replyAnnouncement\}/)
  assert.doesNotMatch(source, /<span className="cmdbar-status thinking" role="status" aria-live="polite" aria-atomic="true">\s*<span className="cmdbar-pip" \/> opening selected chat/,
    'the visible opening indicator must not duplicate the stable live output')
  assert.doesNotMatch(source, /busy \? 'Assistant is responding\.' : preview/)
  assert.match(collapse, /setPreview\(previewText\(la\.content\)\)[\s\S]*?setHasNew\(false\)/,
    'folding a reply already being read must not manufacture unread state')
  assert.match(source, /const ready = announceReplyReady\(reply,[\s\S]*?if \(ready\) setHasNew\(viewRef\.current === 'bar'\)/,
    'only a successful completion that finishes while collapsed is visually unread')
})
