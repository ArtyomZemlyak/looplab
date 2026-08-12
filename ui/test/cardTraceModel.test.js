// A card's whole story: the proposal(s) that produced it, then a section per experiment.
//
// A Card is one hypothesis — the Researcher proposes it, the Developer builds one or more nodes under
// it. Those halves lived on different screens, and until the engine started stamping the card id on
// the `propose` span there was no join between them at all. The rule this model holds: a section
// appears because something DURABLE says it belongs here, never because it happened at about the
// right time.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  cardTraceNotice, cardTraceSections, researchLinkLabel,
} from '../src/cardTraceModel.js'

const payload = (overrides = {}) => ({
  card_id: 'card-1',
  research: [{ span_id: 'r1', trace_id: 't-prop', link: 'card_id', spans: 40, generations: 5,
               tools: 9, tokens: { total: 120 } }],
  nodes: [{ node_id: '0', trace_id: 't-n0', spans: 12, generations: 2, tools: 3,
            tokens: { total: 40 }, errors: 0 }],
  projection: {},
  ...overrides,
})

test('research comes first, then one section per experiment', () => {
  const sections = cardTraceSections(payload({
    nodes: [{ node_id: '0', trace_id: 't0', spans: 3 }, { node_id: '7', trace_id: 't7', spans: 3 }],
  }))
  assert.deepEqual(sections.map(s => s.kind), ['research', 'node', 'node'])
  assert.deepEqual(sections.map(s => s.title),
    ['Research', 'Experiment #0', 'Experiment #7'])
})

test('sections are NOT interleaved by time', () => {
  // A re-proposal happens after the first node. Time-sorting would drop it into the middle of the
  // experiment list where it reads as a fourth experiment rather than as reasoning.
  const sections = cardTraceSections(payload({
    research: [
      { span_id: 'a', trace_id: 'ta', link: 'card_id', start: 1, spans: 3 },
      { span_id: 'b', trace_id: 'tb', link: 'shared_trace', start: 99, spans: 3 },
    ],
  }))
  assert.equal(sections[0].kind, 'research')
  assert.equal(sections[0].rows.length, 2)
  assert.equal(sections[1].kind, 'node')
})

test('each research row says WHICH rule matched it', () => {
  const sections = cardTraceSections(payload({
    research: [
      { span_id: 'a', trace_id: 'ta', link: 'card_id', spans: 3 },
      { span_id: 'b', trace_id: 'tb', link: 'shared_trace', spans: 3 },
      { span_id: 'c', trace_id: 'tc', link: 'something_new', spans: 3 },
    ],
  }))
  assert.deepEqual(sections[0].rows.map(r => r.label), [
    'proposed this card',
    're-proposed in this experiment’s build',
    'linked to this card',          // an unknown rule renders, it does not vanish
  ])
  assert.equal(researchLinkLabel(undefined), 'linked to this card')
})

test('a row with no sub-spans is not openable — the click would land on an empty tree', () => {
  const sections = cardTraceSections(payload({
    research: [{ span_id: 'a', trace_id: 'ta', link: 'card_id', spans: 1 },
               { span_id: 'b', trace_id: 'tb', link: 'card_id', spans: 40 }],
  }))
  assert.deepEqual(sections[0].rows.map(r => r.openable), [false, true])
})

test('a card with no research says so, and says WHY, instead of implying idleness', () => {
  const notice = cardTraceNotice(payload({ research: [] }))
  assert.match(notice, /No research is linked/)
  assert.match(notice, /never inferred from timing/,
    'the reader must know an empty section is a refusal to guess, not a gap in the work')
})

test('the receipt is silent when everything is present', () => {
  assert.equal(cardTraceNotice(payload()), '')
})

test('an empty or unavailable card is reported as such', () => {
  assert.match(cardTraceNotice({ research: [], nodes: [] }), /No trace recorded/)
  assert.match(cardTraceNotice({ projection: { unavailable: true } }), /unavailable/)
  assert.deepEqual(cardTraceSections(null), [])
  assert.deepEqual(cardTraceSections({}), [])
})

test('the plural title names how many proposals there were', () => {
  const sections = cardTraceSections(payload({
    research: [{ span_id: 'a', trace_id: 'ta', link: 'card_id', spans: 3 },
               { span_id: 'b', trace_id: 'tb', link: 'shared_trace', spans: 3 }],
  }))
  assert.equal(sections[0].title, 'Research · 2 proposals')
})
