// The board's "Group the board by" bar. Two views, not three: the Directions tab was retired on
// 2026-08-26 once the Research ladder became a strict superset of it.
//
// WHY THIS IS A SOURCE PIN AND NOT A RENDER TEST. Nothing in this suite MOUNTS CardBoard — it is a
// ~2,000-line component whose existing coverage is source-shaped for that reason — so what is
// checkable here is the vocabulary, and CLAUDE.md's rule is that NEGATIVE pins stay substrings on
// purpose: what must not come back is the TEXT. The behavioural half is covered elsewhere and
// deliberately: `researchView.test.js` SSR-renders the ladder that replaced the tab, and the whole
// tree is proven to still COMPILE by a staging `vite build` (a dropped brace once left the build
// refusing the tree while every test passed).
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const SRC = readFileSync(new URL('../src/CardBoard.jsx', import.meta.url), 'utf8')
const MODEL = readFileSync(new URL('../src/cardLineageModel.js', import.meta.url), 'utf8')

test('the grouping bar offers exactly Lanes and Research', () => {
  assert.ok(SRC.includes("['lanes', 'Lanes'"), 'the kanban is still a view')
  assert.ok(SRC.includes("['research', 'Research'"), 'the question ladder is still a view')
  assert.ok(!SRC.includes("['directions', 'Directions'"),
    'the Directions tab was retired — the ladder reads the same parent_card_id edge and adds the ' +
    'concept nesting, so keeping both offered two answers to one question')
})

test('no render branch can still select the retired view', () => {
  // The tab going while a branch stayed would leave a view reachable by nothing but a stale state
  // value — dead UI that no operator can open and no test would notice.
  assert.ok(!SRC.includes('directionsBoard'),
    'the directions board element is gone, not merely unreachable from the bar')
  assert.ok(!SRC.includes("grouping === 'directions'"),
    'and no branch still tests for it')
})

test('the retired board does not linger as an unused import', () => {
  // An import surviving its only consumer is how a "removed" surface stays half-alive.
  assert.ok(!SRC.includes('directionGroups'), 'CardBoard no longer imports the grouping helper')
  assert.ok(!SRC.includes('UNFILED_GROUP_ID'), 'nor the unfiled sentinel it needed')
})

test('the symbols the kanban still needs were NOT removed with it', () => {
  // The counter-assertion, and the one that would catch an over-eager deletion: three symbols the
  // directions board used are ALSO used by the lane cards, so a wholesale import purge breaks the
  // remaining view. This is the case a "remove the unused imports" pass gets wrong.
  for (const kept of ['cardIsDirection', 'rollupChips', 'CARD_KIND_DIRECTION']) {
    assert.ok(SRC.includes(kept), `${kept} is still used by the kanban card body`)
  }
})

test('every control kind has a labels row — the gate that shipped `reopen` dead', () => {
  // DERIVED from both literals, never pinned on either. `cardControl` refuses any kind with no
  // `labels` row, and it refuses with the CONCURRENCY message — so a missing row does not read as a
  // missing feature, it reads to the operator as "another Card command is still being submitted",
  // forever, with no request ever leaving the browser. That is exactly how `reopen` shipped
  // unreachable: EV_CARD_REOPENED, the five control_validation rows, `_on_card_reopened`, the
  // supersede clause in `_apply_card_drops` and the form all landed, and one absent row here meant
  // `CONTROL.reopenCard` was never called.
  const stripped = SRC.replace(/^\s*\/\/.*$/gm, '')   // a comment may satisfy NEITHER side
  const vocab = stripped.match(/const _CARD_CONTROL_KINDS = \[([^\]]*)\]/)
  assert.ok(vocab, 'the control vocabulary is still a literal this test can read')
  const kinds = [...vocab[1].matchAll(/'([a-z_]+)'/g)].map(match => match[1])
  assert.ok(kinds.length >= 6, 'and it is not empty — an empty read would pass vacuously')

  const table = stripped.match(/const labels = \{([\s\S]*?)\}\[kind\]/)
  assert.ok(table, 'cardControl still selects its labels from an object literal keyed by `kind`')
  const rows = [...table[1].matchAll(/^\s*([a-z_]+):\s*\{/gm)].map(match => match[1])

  assert.deepEqual(rows.slice().sort(), kinds.slice().sort(),
    'every kind in _CARD_CONTROL_KINDS needs a labels row and every row needs a kind — a kind ' +
    'missing here is refused before its request is built, and a row with no kind is dead weight')
})

test('the reopen kind reaches its own transport call', () => {
  // The other half: a labels row makes the command REACHABLE, and this is what makes it the right
  // command. Without it, adding the row above would satisfy the derivation while the ladder still
  // fell through to `CONTROL.dropCard` — i.e. the Reopen button would re-drop the card.
  assert.ok(SRC.includes("kind === 'reopen'"), 'the dispatch ladder still branches on it')
  assert.ok(SRC.includes('CONTROL.reopenCard('), 'and reaches the reopen transport')
})

test('the model keeps the direction grouping as a pure function', () => {
  // Deliberate: `directionGroups` lost its UI consumer, not its correctness, and its own tests still
  // drive it. It is left in place rather than deleted in the same change that removes the tab,
  // because deleting a tested pure function and its tests is a separate decision from retiring a
  // view — and a half-done deletion is worse than an explicit one.
  assert.ok(MODEL.includes('export function directionGroups'),
    'still exported and still tested; its removal is its own change')
})
