import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import {
  traceDetailState, tracePartial, traceUnavailable, unavailableTraceDetail,
} from '../src/traceProjection.js'

test('trace omission truth reconciles malformed server counters and local caps', () => {
  assert.equal(tracePartial({ total_spans: 20, visible_spans: 7, omitted_spans: -9 }), true)
  assert.equal(tracePartial({ total_spans: 1, visible_spans: 8 }), false)
  assert.equal(tracePartial({ truncated: true }), true)
})

test('trace detail transport failure stays distinct from a successful empty projection', () => {
  assert.deepEqual(traceDetailState({ attributes: {}, projection: {} }), {
    status: 'ready', attributes: {}, partial: false,
  })
  assert.deepEqual(traceDetailState({ attributes: [], projection: { detail_truncated: true } }), {
    status: 'ready', attributes: {}, partial: true,
  })
  assert.deepEqual(unavailableTraceDetail(), {
    status: 'unavailable', attributes: {}, partial: false,
  })
})

test('trace detail distinguishes its own truncation from elided siblings', () => {
  assert.deepEqual(traceDetailState({
    attributes: { output: 'complete' },
    projection: {
      truncated: true, detail_truncated: false, siblings_elided: true, omitted_trace_spans: 1,
    },
  }), { status: 'ready', attributes: { output: 'complete' }, partial: false })
  assert.deepEqual(traceDetailState({
    attributes: { output: 'bounded' },
    projection: { truncated: true, detail_truncated: true, siblings_elided: false },
  }), { status: 'ready', attributes: { output: 'bounded' }, partial: true })
  assert.equal(traceDetailState({ projection: { truncated: true } }).partial, false,
    'aggregate or legacy truncation must not claim that selected detail was cut')
})

test('HTTP-200 unavailable receipt takes precedence over empty or partial detail', () => {
  assert.equal(traceUnavailable({ unavailable: true }), true)
  for (const malformed of [{ unavailable: 'true' }, { unavailable: 1 }, null, []]) {
    assert.equal(traceUnavailable(malformed), false)
  }
  assert.deepEqual(traceDetailState({
    attributes: { output: 'must not masquerade as authoritative detail' },
    projection: { unavailable: true, truncated: true },
  }), { status: 'unavailable', attributes: {}, partial: false })
})

test('Inspector and Dock preserve projection truth through every trace surface', async () => {
  const [inspector, dock] = await Promise.all([
    readFile(new URL('../src/Inspector.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../src/Dock.jsx', import.meta.url), 'utf8'),
  ])
  assert.match(inspector, /const unavailable = traceUnavailable\(conv\.projection\)[\s\S]*?if \(unavailable\) return <TraceUnavailable[\s\S]*?if \(!stages\.length\)/,
    'conversation unavailable must win over the ordinary empty state')
  assert.match(inspector, /catch\(\(\) => alive\(\) && setConv\(\{ stages: \[\], projection: \{ unavailable: true \} \}\)\)/,
    'conversation transport failures need an explicit unavailable receipt')
  assert.match(inspector, /if \(!spans\.length && !agent\)[\s\S]*?if \(unavailable\)[\s\S]*?<TraceUnavailable[\s\S]*?if \(partial\)[\s\S]*?no observations were included[\s\S]*?No execution spans/,
    'an empty node trace must check unavailable and partial before successful empty')
  assert.ok(inspector.indexOf("if (view === 'conversation')") < inspector.indexOf('if (!spans.length && !agent)'),
    'the selected conversation view must load before raw-tree empty/unavailable branches')
  assert.match(inspector, /\[open, io, runId, s\.span_id, kind\][\s\S]*?const retryIo = \(\) => setIo\(null\)[\s\S]*?<TraceUnavailable label="Trace detail unavailable\." onRetry=\{retryIo\}/,
    'span detail unavailable must be retryable even after the first expansion')
  assert.match(inspector, /function Conversation\(\{[\s\S]*?onRetry[\s\S]*?\[runId, n\.id, working, reloadNonce\][\s\S]*?<TraceUnavailable onRetry=\{onRetry\}/,
    'a finished conversation one-shot must expose an explicit retry')
  assert.match(inspector, /export function NodeTrace\(\{ spans, runId, projection = \{\}, onRetry \}\)[\s\S]*?<TraceUnavailable onRetry=\{onRetry\}/)
  assert.match(inspector, />span tree<\/button>/)
  assert.doesNotMatch(inspector, />raw spans<\/button>|every span's full I\/O|nothing is lost|WHOLE re-sent/)

  assert.match(dock, /setTrace\(\{[\s\S]*?spans:[\s\S]*?projection: d\?\.projection \|\| \{\}/,
    'operation trace must retain the server projection envelope')
  assert.match(dock, /<NodeTrace spans=\{nodeSpans\}[\s\S]*?projection=\{nodeTrace\.projection\}/,
    'node trace must pass its projection metadata to the shared renderer')
  assert.match(dock, /const unavailable = traceUnavailable\(current\.projection\)[\s\S]*?<TraceUnavailable label="Trace unavailable; retrying automatically\." \/>/,
    'live tail transport/unavailable state must not look like an empty waiting feed')
  assert.match(dock, /3000, \[runId, generation, active, open\], \{ enabled: active && open \}/,
    'the open live tail must automatically recover from a transient unavailable state')
  assert.match(dock, /!current\.loaded[\s\S]*?loading trace…[\s\S]*?partial && tail\.length === 0[\s\S]*?no observations were included[\s\S]*?waiting for the next agent step/,
    'an empty partial live tail must not look like a complete waiting feed')
  assert.match(dock, /function OpTrace[\s\S]*?\[runId, traceId, retryNonce\][\s\S]*?onRetry=\{retry\}/,
    'operation trace HTTP-200 unavailable must be retryable')
  assert.match(dock, /const retryNodeTrace = \(\) => \{[\s\S]*?setNodeTrace\(null\)[\s\S]*?setNodeTraceError\(false\)[\s\S]*?setNodeTraceNonce/,
    'node trace retry must enter a real loading state before issuing another request')
  assert.match(dock, /projection=\{nodeTrace\.projection\} runId=\{runId\} onRetry=\{retryNodeTrace\}/,
    'HTTP-200 unavailable node traces must share the same retry path as transport failures')
  assert.doesNotMatch(dock, /raw <think>|full I\/O of any observation|full, UNtruncated text|FULL content/)
})
