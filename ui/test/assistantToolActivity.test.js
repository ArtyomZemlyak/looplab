// The tool-activity strip's window, truncation and NUMBERING — reachable now that they live in a
// model beside the component. Nothing in the suite mounts `AssistantChat`, so as long as these
// decisions were inside it, none of them could be driven.
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  TOOL_ACTIVITY_ITEM_MAX, TOOL_ACTIVITY_LABEL_MAX,
  boundedToolText, toolActivityLabel, toolActivityProjection,
} from '../src/assistantToolActivity.js'

test('an unlabelled item in the window does not shift every ordinal', () => {
  // THE DEFECT. Items whose label resolves to '' are skipped from the shown list but still counted
  // in `total`, and the render derived its first ordinal as `total - shown + 1` — correct only when
  // every windowed item happened to carry a label. One unlabelled payload and the numbers under
  // "Showing the latest N of M steps" disagreed with the real positions.
  const items = ['read a', {}, 'read c', { tool: 'grep' }]
  const { steps, total, limited } = toolActivityProjection(items)
  assert.equal(total, 4, 'an unlabelled item is still a step that happened')
  assert.equal(limited, false)
  assert.deepEqual(steps.map(s => s.position), [1, 3, 4],
    'each shown step carries its TRUE position, so the list can set an explicit value per item')
  assert.deepEqual(steps.map(s => s.label), ['read a', 'read c', 'grep'])
  // The arithmetic the old render used would have started this list at 2.
  assert.notEqual(total - steps.length + 1, steps[0].position)
})

test('the window keeps the newest items and says it is limited', () => {
  const items = Array.from({ length: TOOL_ACTIVITY_ITEM_MAX + 5 }, (_, i) => `step ${i + 1}`)
  const { steps, total, limited } = toolActivityProjection(items)
  assert.equal(total, TOOL_ACTIVITY_ITEM_MAX + 5)
  assert.equal(steps.length, TOOL_ACTIVITY_ITEM_MAX)
  assert.equal(limited, true)
  assert.equal(steps[0].label, 'step 6', 'the OLDEST items are the ones dropped')
  assert.equal(steps[0].position, 6, 'and the ordinals are absolute, not window-relative')
  assert.equal(steps[steps.length - 1].position, total)
})

test('a label is bounded and never ends on half a character', () => {
  const long = 'x'.repeat(TOOL_ACTIVITY_LABEL_MAX + 50)
  assert.equal(boundedToolText(long).length, TOOL_ACTIVITY_LABEL_MAX)
  // A fixed-length slice can cut an astral character in half; a lone high surrogate is not text.
  const astral = 'a'.repeat(TOOL_ACTIVITY_LABEL_MAX - 1) + '\u{1F600}'
  const clipped = boundedToolText(astral)
  assert.ok(!/[\uD800-\uDBFF]$/.test(clipped), 'a trailing lone surrogate must be trimmed')
  assert.equal(boundedToolText('  a\n\tb  '), 'a b', 'whitespace is collapsed and trimmed')
})

test('a label falls back to the tool name, and shapes that carry neither are skipped', () => {
  assert.equal(toolActivityLabel({ label: 'read the log' }), 'read the log')
  assert.equal(toolActivityLabel({ tool: 'grep' }), 'grep')
  assert.equal(toolActivityLabel({ label: '', tool: 'grep' }), 'grep')
  for (const shape of [null, undefined, 42, [], {}, { label: 7 }])
    assert.equal(toolActivityLabel(shape), '', `${JSON.stringify(shape)} carries no label`)
})

test('an empty or non-array input is an empty projection, not a crash', () => {
  for (const shape of [null, undefined, [], 'nope', {}])
    assert.deepEqual(toolActivityProjection(shape), { steps: [], total: 0, limited: false })
})
