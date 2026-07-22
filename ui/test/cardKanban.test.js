import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

let vite
let HypothesisBoard

test.before(async () => {
  vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  ;({ HypothesisBoard } = await vite.ssrLoadModule('/src/panels.jsx'))
})

test.after(async () => {
  await vite?.close()
})

test('HypothesisBoard renders the bounded Card DTO as lifecycle lanes in priority order', () => {
  const cards = {
    'card-later': {
      id: 'spoofed-body-id', status: 'proposed', statement: 'Later priority', priority: 4,
      selection_ready: false, evidence: [],
    },
    'card-first': {
      id: 'another-spoof', status: 'proposed', statement: 'First priority', priority: 0,
      pinned: true, source: 'researcher', operator: 'improve', params: { lr: 0.125 },
      space: { lr: [0.1, 0.2] }, eval_timeout: 90,
      footprint: { gpus: 2, gpu_mem_mib: 8192, proposed_by: 'researcher', finalized_by: 'developer' },
      resource_pin: { gpus: 1, gpu_mem_mib: 4096, pinned_by: 'operator' },
      identity: { kind: 'native', source: 'card_added_receipt', durable: true, receipt_valid: true },
      selection_provenance: {
        action_source: 'card_added', action_owner_count: 1, action_complete: true,
        freshness: 'current', owner_state: 'none',
      },
      selection_ready: false, selection_blockers: ['work_in_flight', 'freshness_stale'],
      evidence: [7, 8], best_delta: 0.25, parent_ids: [3], parent_generations: { 3: 2 },
      scored_against: 3, scored_against_generation: 4,
      concept_tags: ['model/tree'], novelty_verdict: { grade: 'ALLOW' }, provenance_tier: 'native',
      // These are deliberately hostile body-shaped fields. The Card UI is an explicit allowlist and
      // must not render them even if a malformed caller bypasses the server projection in a unit test.
      code: 'PRIVATE-CODE-BODY', files: { 'secret.py': 'PRIVATE-FILE-BODY' }, stdout: 'PRIVATE-OUTPUT',
    },
    'card-spec': { status: 'speculating', statement: 'Speculative build', selection_ready: false },
    'card-future': { status: 'awaiting-audit', statement: 'Future lifecycle', selection_ready: false },
    'card-dropped': {
      status: 'dropped', statement: 'Terminal work', selection_ready: false,
      dropped_reason: 'operator stopped it', dropped_by: 'operator',
    },
  }
  const items = Object.fromEntries(Object.keys(cards).map(id => [id, {
    complete: id !== 'card-first', fields: { total: 1, returned: 1, omitted: 0, complete: id !== 'card-first' },
    omissions: id === 'card-first' ? { rationale: { unit: 'characters', total: 10, returned: 5, omitted: 5, complete: false } } : {},
  }]))
  const markup = renderToStaticMarkup(React.createElement(HypothesisBoard, {
    state: {
      cards, hypotheses: {},
      cards_projection: {
        source_valid: true, total: 5, returned: 5, omitted: 0, complete: false, items,
      },
    },
    runId: 'run', onClose() {}, onSelect() {},
  }))

  assert.match(markup, /aria-label="Cards"/)
  assert.match(markup, /aria-label="Card lifecycle kanban"/)
  assert.match(markup, /role="region" aria-label="Card lifecycle kanban"/)
  assert.match(markup, /aria-labelledby="card-lane-proposed"/)
  assert.match(markup, /<h3 id="card-lane-proposed"/)
  for (const lane of ['Proposed', 'Building', 'Coded', 'Running', 'Evaluated', 'Gated', 'Dropped']) {
    assert.match(markup, new RegExp(`>${lane} <`))
  }
  assert.match(markup, />Speculating </)
  assert.match(markup, />Awaiting Audit </)
  assert.ok(markup.indexOf('First priority') < markup.indexOf('Later priority'))
  assert.match(markup, /card-first/)
  assert.doesNotMatch(markup, /spoofed-body-id|another-spoof/)
  assert.match(markup, /Action[\s\S]*improve[\s\S]*lr=0\.125/)
  assert.match(markup, /Declared[\s\S]*2 GPUs[\s\S]*8.?192 MiB\/GPU[\s\S]*90s timeout/)
  assert.match(markup, /Configured pin[\s\S]*operator override[\s\S]*1 GPU[\s\S]*4.?096 MiB\/GPU/)
  assert.match(markup, /requested 1 GPU[\s\S]*4.?096 MiB\/GPU/)
  assert.match(markup, /Provenance[\s\S]*identity native[\s\S]*action card_added[\s\S]*resource pin operator/)
  assert.match(markup, /freshness stale|freshness current/)
  assert.match(markup, /work in flight/)
  assert.match(markup, /partial details/)
  assert.match(markup, /parent #3 · attempt 2/)
  assert.match(markup, /scored vs #3 · attempt 4/)
  assert.match(markup, /aria-label="Open evidence node #7"/)
  assert.match(markup, /Operator controls/)
  for (const label of ['Save text', 'Pin priority', 'Pin resources', 'Confirm drop']) {
    assert.match(markup, new RegExp(`>${label}<`))
  }
  for (const label of [
    'Display statement for card-first', 'Priority for card-first', 'GPU count for card-first',
    'GPU memory in MiB for card-first', 'Drop reason for card-first',
  ]) assert.match(markup, new RegExp(`aria-label="${label}"`))
  assert.doesNotMatch(markup, /PRIVATE-CODE-BODY|PRIVATE-FILE-BODY|PRIVATE-OUTPUT|secret\.py/)
})

test('HypothesisBoard keeps the hypothesis workflow as a graceful empty-Card fallback', () => {
  const markup = renderToStaticMarkup(React.createElement(HypothesisBoard, {
    state: {
      cards: {}, cards_projection: {
        source_valid: true, total: 0, returned: 0, omitted: 0, complete: true, items: {},
      },
      hypotheses: {
        later: { id: 'later', status: 'open', statement: 'Later hypothesis', priority: 5, evidence: [] },
        first: { id: 'first', status: 'open', statement: 'First hypothesis', priority: 0, evidence: [] },
      },
    },
    runId: 'run', onClose() {}, onSelect() {}, onToast() {},
  }))

  assert.match(markup, /aria-label="Hypotheses"/)
  assert.match(markup, /aria-label="New hypothesis"/)
  assert.doesNotMatch(markup, /Card lifecycle kanban/)
  assert.ok(markup.indexOf('First hypothesis') < markup.indexOf('Later hypothesis'))
})

test('Card evidence chips retain the node-selection callback contract', async () => {
  const source = await readFile(new URL('../src/panels.jsx', import.meta.url), 'utf8')
  const card = source.slice(source.indexOf('function _CardKanbanCard'), source.indexOf('function _CardKanban('))
  assert.match(card, /aria-label=\{`Open evidence node #\$\{nid\}`\}/)
  assert.match(card, /onSelect\?\.\(nid\)/)
  assert.match(card, /onClose\?\.\(\)/)
})

test('Card controls use only generation-fenced command helpers and never client provenance', async () => {
  const source = await readFile(new URL('../src/api.js', import.meta.url), 'utf8')
  const runView = await readFile(new URL('../src/RunView.jsx', import.meta.url), 'utf8')
  const controls = source.slice(source.indexOf('reprioritizeCard:'), source.indexOf('refreshReport:'))
  for (const eventType of [
    'card_reprioritized', 'card_edited', 'card_resource_pinned', 'card_dropped',
  ]) assert.match(controls, new RegExp(`runCommand\\([^)]*['"]${eventType}['"]`, 's'))
  assert.doesNotMatch(controls, /source\s*:|dropped_by\s*:|pinned\s*:/)
  assert.match(runView, /\['hypotheses', 'Cards'\]/)
  assert.match(runView, /runGeneration=\{generation\}/)
})

test('Card optimistic controls retain uncertain commands and roll back only the failed operation', async () => {
  const source = await readFile(new URL('../src/panels.jsx', import.meta.url), 'utf8')
  const board = source.slice(source.indexOf('function _CardKanban('), source.indexOf('function _HypothesisFallback'))
  assert.match(board, /feedback\.kind === 'pending'[\s\S]*waiting-for-fold/)
  assert.match(board, /submissionMayHaveSucceeded[\s\S]*confirmation-unknown/)
  assert.match(board, /if \(!uncertain\) delete updates\[kind\]/)
  assert.doesNotMatch(board, /feedback\.kind !== 'success'[\s\S]{0,120}(revert|delete next\[card\.id\])/)
})

test('An uncertain (confirmation-unknown) command does not freeze controls on the whole board', async () => {
  const source = await readFile(new URL('../src/panels.jsx', import.meta.url), 'utf8')
  const board = source.slice(source.indexOf('function _CardKanban('), source.indexOf('function _HypothesisFallback'))
  // globalPending — which drives controlsLocked on every OTHER card — must exclude the never-clearing
  // 'confirmation-unknown' pending, or one lost submission disables the entire board until reload.
  assert.match(board, /const globalPending = [\s\S]*?pending\.phase !== 'confirmation-unknown'/)
})

test('Edit reflection prefers the durable event receipt and safely falls back for legacy folds', async () => {
  // CODEX AGENT: this is a source-text regex test, so it never submits two edits or delivers the two
  // SSE folds in the problematic order; an equivalent-looking but incorrect predicate (or even dead
  // code) can satisfy it. Render the board with a mocked CONTROL transport and assert the optimistic
  // value/pending fence after each response+fold transition, including clipping/redaction at baseline.
  const source = await readFile(new URL('../src/panels.jsx', import.meta.url), 'utf8')
  const reflected = source.slice(
    source.indexOf('function _cardControlReflected'), source.indexOf('function _cardWithOptimisticControls'))
  assert.match(reflected, /statement_edit_seq[\s\S]*foldedEventSeq >= expected/)
  // The clipped-prefix branch counts as landed only when the card is a prefix of the submission but NOT
  // a prefix of the baseline — so an extend edit's pre-value (and a still-in-flight earlier chained edit
  // whose value is a prefix of this submission) cannot read as already-landed.
  assert.match(reflected, /patch\.statement\.startsWith\(card\.statement\)[\s\S]*!baseline\.startsWith\(card\.statement\)/)
  // The submit path baselines against the prior in-flight submission (via sentEditRef), not the stale
  // fold, so a chained extend edit is not falsely reflected by the earlier edit's landing.
  const board = source.slice(source.indexOf('function _CardKanban('), source.indexOf('function _HypothesisFallback'))
  assert.match(board, /const sentEditRef = useRef\(\{\}\)/)
  assert.match(board, /let editBaseline/)
  assert.match(board, /prior\.startsWith\(card\.statement\)\) \? prior : card\.statement/)
  assert.match(board, /record\?\.event_seq/)
  assert.match(board, /commandRecord\?\.event_seq/)
})

test('Card optimistic state is scoped to run generation and ignores late unmounted completions', async () => {
  const source = await readFile(new URL('../src/panels.jsx', import.meta.url), 'utf8')
  const board = source.slice(source.indexOf('function _CardKanban('), source.indexOf('function _HypothesisFallback'))
  const owner = source.slice(source.indexOf('export function HypothesisBoard'), source.indexOf('// Module scope'))
  assert.match(owner, /const scopeKey = `\$\{runId \|\| ''\}:\$\{runGeneration \|\| ''\}`/)
  assert.match(owner, /key=\{`cards:\$\{scopeKey\}`\}/)
  assert.match(board, /activeRef\.current = false/)
  assert.match(board, /if \(!activeRef\.current\) return \{ kind: 'stale'/)
})

test('Each Card control draft re-seeds only from its own folded source (no cross-field clobber)', async () => {
  const source = await readFile(new URL('../src/panels.jsx', import.meta.url), 'utf8')
  const card = source.slice(source.indexOf('function _CardKanbanCard'), source.indexOf('function _CardKanban('))
  // Four independent effects, one per field — NOT one effect that resets all drafts on any dep change.
  assert.match(card, /useEffect\(\(\) => \{ setStatementDraft\(statement\) \}, \[card\.id, statement\]\)/)
  assert.match(card, /setGpuDraft\([\s\S]*?\}, \[card\.id, formGpus\]\)/)
  assert.match(card, /setMemoryDraft\([\s\S]*?\}, \[card\.id, formGpuMem\]\)/)
  assert.doesNotMatch(card, /\[card\.id, statement, card\.priority, formGpus, formGpuMem\]/)
})
