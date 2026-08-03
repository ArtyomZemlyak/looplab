import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('editable launch card gates Start on the exact validated fingerprint', async () => {
  const card = await source('LaunchCard.jsx')
  assert.match(card, /const fingerprint = launchFingerprint\(draft, chat\)/)
  assert.match(card, /validation\.fingerprint === fingerprint/)
  assert.match(card, /await preflightRunStart\(built\.body\)/)
  assert.match(card, /validation_token: validation\.token/)
  assert.match(card, /idempotency_key: operation\.idempotencyKey/,
    'paid Start must carry the identity from the durable operation record, not a fresh key')
  assert.match(card, /disabled=\{locked \|\| storageBlocked \|\| settingsLaunchBlocked \|\| !validatedCurrent\}/,
    'Start stays gated on a current validation, durable storage, AND a settled settings fence')
  assert.match(card, /className="btn xs"\n\s*disabled=\{locked \|\| settingsLaunchBlocked\} onClick=\{validate\}/,
    'invalid drafts must keep Validate actionable so it can render and focus their errors')
  assert.match(card, /onSubmit=\{event => \{ event\.preventDefault\(\); validate\(\) \}\}/)
  assert.doesNotMatch(card, /onSubmit=\{[^\n]*start\(\)/,
    'Enter/form submit must never turn a current validation into a paid launch')
  assert.match(card, />Reset proposal<\/button>/)
  assert.ok(card.indexOf('saveLaunchTransport(transportIdentity') < card.indexOf('await startRun('),
    'the recovery key must be durable before paid Start leaves the browser')
  assert.match(card, /loadLaunchTransport\(transportIdentity\)/)
  assert.match(card, /Durable tab storage is unavailable; paid Start was not sent\./)
  assert.match(card, /Recovered unfinished startup “\$\{saved\.runId\}”/)
  assert.match(card, /<strong>Startup being observed<\/strong><code>\{unknownStart\.runId\}<\/code>/,
    'reload recovery must show the saved run identity even when the proposal originally had another name')
  assert.match(card, /<strong>Validate is free:<\/strong>/)
  assert.match(card, /Start may incur provider cost\. No monetary cap is configured\./,
    'an unvalidated draft must warn that Start can cost money and that nothing caps it')
  assert.doesNotMatch(card, /did not reach Popen/)
})

test('launch card is a labelled busy form with actionable errors and status', async () => {
  const card = await source('LaunchCard.jsx')
  assert.match(card, /<form className="asst-launch" aria-labelledby=\{titleId\} aria-busy=/)
  assert.match(card, /aria-busy=\{operationBusy \? 'true' : 'false'\}/,
    'idle startup recovery is a user decision state, not an indefinitely busy form')
  assert.match(card, /<label htmlFor=\{`launch-\$\{reactId\}-run_id`\}>Run name<\/label>/)
  assert.match(card, /<fieldset className="asst-launch-source"/)
  assert.match(card, /<legend>Task source<\/legend>/)
  assert.match(card, /<div ref=\{errorRef\} id=\{errorId\} className="asst-launch-errors" tabIndex=\{-1\}>/,
    'the error summary must be programmatically focusable for the field-path fallback below')
  assert.match(card, /<div role="alert" aria-label="Cannot start yet">/,
    'errors that map to no control must still be ANNOUNCED, not only rendered')
  assert.match(card, /role="status" aria-live="polite" aria-atomic="true"/)
  assert.match(card, /runIdRef\.current\?\.focus\(\); runIdRef\.current\?\.select\(\)/)
  assert.match(card, /const focusTarget = field && !fieldDisabled \? field : errorRef\.current/,
    'a server field path with no matching (or a disabled) control must focus the alert summary')
  assert.match(card, /if \(path === 'task' \|\| path\.startsWith\('task\.'\)\) return 'task'/)
  assert.match(card, /if \(path === 'settings'\) return 'advanced-settings'/,
    'a settings-scoped server error must route focus to the collapsed advanced panel')
  assert.match(card, /<code>\{path\}<\/code>/,
    'nested server errors must retain their exact field path in the alert')
  assert.match(card, /\{hasHelp && <span className="asst-launch-help" id=\{helpId\}>/,
    'runtime AUTO semantics must be visible and programmatically associated with their controls')
  assert.match(card, /const hasHelp = !!field\.help \|\| field\.type === 'bool'/,
    'an inherit-able boolean explains itself even with no authored help text')
  assert.match(card, /aria-describedby=\{describedBy\(`\$\{titleId\}-task-help`, taskInvalid && inlineErrorId\('task'\)\)\}/,
    'help text and the live error must BOTH stay associated with the control, not replace each other')
  assert.match(card, /aria-describedby=\{fieldDescribedBy\}/)
  assert.match(card, /Operator checklist only — these notes are not commands and are not executed automatically\./)
})

test('ambiguous startup is observed and never blind-retried', async () => {
  const [card, api] = await Promise.all([source('LaunchCard.jsx'), source('api.js')])
  assert.match(card, /setUnknownStart\(\{ \.\.\.operation, paidEffectUnknown: true \}\)/,
    'an ambiguous startup must be observed under the identity that was already sent')
  assert.match(card, /getStartStatus\(operation\.runId, operation\.idempotencyKey, \{/)
  assert.match(card, /'Check startup'/)
  const start = card.indexOf('} else if (launchAmbiguous(error))')
  const ambiguousBranch = card.slice(start, card.indexOf('} else {', start))
  assert.doesNotMatch(ambiguousBranch, /startRun\(/)
  assert.match(card, /\['start_in_progress', 'start_uncertain', 'spawn_claim_unknown', 'engine_start_uncertain'\]/)
  assert.match(card, /\[408, 425, 429\]\.includes\(Number\(error\.status\)\)/,
    'proxy timeout, Too Early, and rate-limit responses must retain the paid-start observation key')
  assert.match(card, /'external_start_in_progress', 'external_start_uncertain'/,
    'an unowned legacy engine must be presented as a run-name conflict, not this card’s startup')
  const recovery = await source('launchRecovery.js')
  assert.match(recovery, /const STARTED_STATUSES = new Set\(\['executing', 'succeeded'\]\)/,
    'only a started status may end the observation; accepted alone has not proven Popen')
  // The proof moved into `launchStatusOutcome`, which cross-checks the server's own booleans
  // against its status word — a stronger gate than the old single predicate.
  assert.match(card, /const outcome = launchStatusOutcome\(result, operation\.runId\)/)
  assert.ok(card.indexOf("if (outcome.kind === 'started')") < card.indexOf('location.hash = `#/run/'),
    'a 2xx launch response must prove startup before navigation')
  assert.match(recovery, /\(expectedStarted && result\.paid_effect_unknown\)/,
    'a "started" claim carrying an unresolved paid effect is a protocol violation, not a start')
  assert.doesNotMatch(card, /\['not_started', 'failed', 'rejected'[^\]]*executing/)
  assert.doesNotMatch(card, /\['not_started', 'failed', 'rejected'[^\]]*uncertain/)
  assert.match(card, /provider work or cost cannot be ruled out/)
  assert.match(api, /headers: \{ 'Idempotency-Key': String\(idempotencyKey \|\| ''\) \}/)
  assert.doesNotMatch(api, /status\?idempotency_key=/,
    'startup identity must not enter browser history or ordinary access-log URLs')
  assert.match(card, /error\?\.status === 404 && errorCode\(error\) === 'start_not_found'/)
  assert.match(card, /Release after inspection/)
  assert.match(card, /missingStart \|\| unknownStart\.paidEffectUnknown/)
  // `clearRecovery` is now passed the exact operation it is releasing, so a stale key cannot be
  // cleared by a later one. The property — never proceed on a failed release — is unchanged.
  assert.match(card, /if \(!clearRecovery\(unknownStart\)\) return/)
  const missing = card.indexOf("error?.status === 404 && errorCode(error) === 'start_not_found'")
  const missingBranch = card.slice(missing, card.indexOf('} else {', missing))
  assert.doesNotMatch(missingBranch, /clearLaunchTransport|clearRecovery/,
    'a first missing status must retain the key while the original POST may still be preflighting')
  assert.match(card, /const cleared = clearRecovery\(operation\)[\s\S]*?Startup is proven for/,
    'a proven status response must surface recovery cleanup failure before navigation')
  assert.match(card, /but tab recovery storage could not be cleared/,
    'a failed release is told to the operator rather than swallowed by the success path')
})

test('New Run CTA opens and prefills the composer without auto-submitting or replacing a draft', async () => {
  const assistant = await source('AssistantBar.jsx')
  const effect = assistant.slice(assistant.indexOf('const onNewRun = (event)'), assistant.indexOf("window.addEventListener('ll:new-run'"))
  assert.match(effect, /const command = goal \? `\/new \$\{goal\}` : '\/new '/)
  assert.match(effect, /if \(!existing \|\| existing === command\.trim\(\)\) setInput\(command\)/)
  // The wording gained the second escape hatch (Chat) the composer actually offers; the property —
  // an existing draft is never silently overwritten, and the user is told why — is unchanged.
  assert.match(effect, /Draft preserved — choose Chat or clear the composer before drafting a new run/)
  assert.match(effect, /setView\(current => current === 'bar' \? 'side' : current\)/)
  assert.match(effect, /inputRef\.current\?\.focus\(\)/)
  assert.doesNotMatch(effect, /requestNewRun\(/)
  assert.doesNotMatch(effect, /runLLM\(/)
})

test('proposal chat and in-memory draft ownership are passed into the card', async () => {
  const [assistant, chat] = await Promise.all([source('AssistantBar.jsx'), source('AssistantChat.jsx')])
  assert.match(assistant, /const launchChatThrough = index => proposalLaunchChat\(msgs, index\)/)
  // Same JSX element, now spread over two lines by the `onRunOpen` prop that landed between them —
  // the property is that the card receives BOTH, not that they share a source line.
  assert.match(assistant, /onOpenSettings=\{openAssistantSettings\}[\s\S]{0,160}?launchChat=\{launchChatThrough\(i\)\}/)
  assert.match(assistant, /const \[launchDrafts, setLaunchDrafts\] = useState\(\{\}\)/)
  assert.match(assistant, /const \[launchRecoveries, setLaunchRecoveries\] = useState\(\(\) => listLaunchTransports\(\)\)/)
  assert.match(assistant, /const launchRecoveryButton = launchRecoveries\.length > 0/,
    'unresolved startup recovery must remain visible after chat/surface changes')
  assert.match(assistant, /nextLaunchRecovery\?\.invalid \? 'Review startup recovery' : 'Check startup'/,
    'a damaged record must be named as one, not offered as an ordinary check')
  assert.match(assistant, /launchRecoveries\.length > 1 && <span className="asst-launch-global-count">/,
    'several unresolved startups must not collapse into one silent affordance')
  assert.match(chat, /retainedDraft=\{launchDrafts\?\.\[draftKey\]\}/)
  assert.match(chat, /<LaunchCard key=\{draftKey\}/,
    'a fork/session switch must remount the card instead of leaking local draft state')
  assert.match(chat, /launchIdentity=\{draftKey\}/,
    'startup recovery must be session-scoped even when a fork copies proposal_id')
  assert.match(chat, /onDraftChange=\{draft => onLaunchDraft\?\.\(draftKey, draft\)\}/)
  assert.match(assistant, /sidRef\.current = id; setSid\(id\); setMsgs\(\[\]\); setPreview\(''\)/)
  assert.match(assistant, /if \(!mountedRef\.current \|\| sidRef\.current !== id\) return/,
    'a delayed session read must never render the previous session under a new draft key')
  assert.doesNotMatch(assistant, /setMsgs\(\[\]\); setLaunchDrafts\(\{\}\)/,
    'New chat must not erase drafts belonging to still-existing sessions')
  // The guard became a named predicate consulted BEFORE the request rather than inline ordering.
  assert.match(assistant, /const deleteSessionBlock = \(id\) => \{\n\s*const unresolved = listLaunchTransports\(\)/,
    'a chat that owns unresolved startup recovery must not be deletable')
  assert.match(assistant, /Check or release startup recovery/)
  const confirmDelete = assistant.slice(assistant.indexOf('const confirmDeleteSession = async'),
    assistant.indexOf('const acceptDeletedSession'))
  assert.ok(confirmDelete.indexOf('const block = deleteSessionBlock(id)') >= 0
    && confirmDelete.indexOf('deleteSessionBlock(id)') < confirmDelete.indexOf('deleteConfirmBusyRef.current = true'),
    'the block must be decided before any delete work starts')
  // The draft cleanup moved into `acceptDeletedSession`, which only the SUCCESS paths call, so the
  // ordering property is now structural: no cleanup exists outside that accept step.
  const accept = assistant.slice(assistant.indexOf('const acceptDeletedSession'),
    assistant.indexOf('try {', assistant.indexOf('const acceptDeletedSession')))
  assert.match(accept, /clearLaunchDraftSession\(current, id\)/,
    'a failed session delete must leave its in-memory draft intact')
  assert.match(accept, /if \(id === sidRef\.current\) newChat\(\)/,
    'a delayed delete for session A must not clear session B')
})

test('editing invalidates every old server validation presentation', async () => {
  const card = await source('LaunchCard.jsx')
  const update = card.slice(card.indexOf('const update = patch =>'), card.indexOf('const reset ='))
  const runtime = card.slice(card.indexOf('const changeRuntime ='), card.indexOf('const allErrors ='))
  for (const branch of [update, runtime]) {
    assert.match(branch, /setValidation\(null\)/)
    assert.match(branch, /setWarnings\(\[\]\)/)
    assert.match(branch, /setPreview\(null\)/)
  }
  assert.match(card, /fingerprintRef\.current !== requestFingerprint/,
    'a late validation response must not relabel an edited proposal preview as current')
})
