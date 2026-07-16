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
  assert.deepEqual(traceDetailState({ attributes: [], projection: { truncated: true } }), {
    status: 'ready', attributes: {}, partial: true,
  })
  assert.deepEqual(unavailableTraceDetail(), {
    status: 'unavailable', attributes: {}, partial: false,
  })
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
  assert.match(inspector, /const unavailable = traceUnavailable\(conv\.projection\)[\s\S]*?if \(unavailable\)[\s\S]*?Trace unavailable\.[\s\S]*?if \(!stages\.length\)/,
    'conversation unavailable must win over the ordinary empty state')
  assert.match(inspector, /catch\(\(\) => alive\(\) && setConv\(\{ stages: \[\], projection: \{ unavailable: true \} \}\)\)/,
    'conversation transport failures need an explicit unavailable receipt')
  assert.match(inspector, /if \(unavailable\)[\s\S]*?Trace unavailable\.[\s\S]*?if \(partial\)[\s\S]*?no observations were included[\s\S]*?No execution spans/,
    'an empty node trace must check unavailable and partial before successful empty')
  assert.match(inspector, />span tree<\/button>/)
  assert.doesNotMatch(inspector, />raw spans<\/button>|every span's full I\/O|nothing is lost|WHOLE re-sent/)

  assert.match(dock, /setTrace\(\{[\s\S]*?spans:[\s\S]*?projection: d\?\.projection \|\| \{\}/,
    'operation trace must retain the server projection envelope')
  assert.match(dock, /<NodeTrace spans=\{nodeSpans\}[\s\S]*?projection=\{nodeTrace\.projection\}/,
    'node trace must pass its projection metadata to the shared renderer')
  assert.match(dock, /const unavailable = traceUnavailable\(current\.projection\)[\s\S]*?Trace unavailable\./,
    'live tail transport/unavailable state must not look like an empty waiting feed')
  assert.match(dock, /partial && tail\.length === 0[\s\S]*?no observations were included[\s\S]*?waiting for the next agent step/,
    'an empty partial live tail must not look like a complete waiting feed')
  assert.doesNotMatch(dock, /raw <think>|full I\/O of any observation|full, UNtruncated text|FULL content/)
})
