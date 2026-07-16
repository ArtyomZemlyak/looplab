import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const assistantSource = () => readFile(new URL('../src/AssistantBar.jsx', import.meta.url), 'utf8')
const section = (source, start, end) => source.slice(source.indexOf(start), source.indexOf(end))

test('session selection commits only a current, bounded read and preserves the prior transcript on failure', async () => {
  const source = await assistantSource()
  const open = section(source, 'const openSession =', 'openSessionRef.current = openSession')
  const read = open.indexOf('await boundedRead(assistantGet(id))')
  const commit = open.indexOf('sidRef.current = id; setSid(id); setMsgs([])')

  assert.ok(read >= 0 && commit > read, 'the target must be read before replacing the visible session')
  assert.match(open, /const seq = \+\+openSessionSeqRef\.current/)
  assert.match(open, /seq !== openSessionSeqRef\.current/)
  assert.match(source, /const newChat = \(\) => \{\s*\+\+openSessionSeqRef\.current/)
})

test('session creation and send are single-flight while a failed create preserves the draft', async () => {
  const source = await assistantSource()
  const run = section(source, 'const runLLM =', 'const requestNewRun =')
  const send = section(source, 'const send =', 'useEffect(() => {\n    const onNewRun')
  const normalSend = send.slice(send.indexOf('const refs ='))

  assert.ok(run.indexOf('turnCaptureRef.current = true') < run.indexOf('await assistantCreate('))
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
