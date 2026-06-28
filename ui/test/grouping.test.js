// Unit tests for the pure layout/grouping logic behind the banded grid-pack redesign.
// Zero test-framework dependency: run with `node --test test/` from the ui/ directory.
import test from 'node:test'
import assert from 'node:assert/strict'
import { layoutWithGroups, similarityRank } from '../src/util.js'
import { computeGroups, nodeGroupMap, autoCollapseSet, isMergeEntryEdge } from '../src/grouping.js'

const NODE_W = 188, NODE_H = 78

// Build a nodes object from a compact spec. operator defaults to merge when >1 parent.
function mk(spec) {
  const nodes = {}
  for (const s of spec) {
    nodes[s.id] = {
      id: s.id,
      parent_ids: s.parents || [],
      status: s.status || 'evaluated',
      metric: s.metric,
      confirmed_mean: s.confirmed_mean,
      operator: s.operator || ((s.parents && s.parents.length > 1) ? 'merge' : 'improve'),
      idea: { theme: 'theme' in s ? s.theme : null, params: s.params || {} },
    }
  }
  return nodes
}

// AABB of a cell from its member node positions.
function cellBox(cell, pos) {
  const xs = [], ys = []
  for (const id of cell.ids) {
    const p = pos[`n:${id}`]; if (!p) continue
    xs.push(p.x, p.x + NODE_W); ys.push(p.y, p.y + NODE_H)
  }
  return { x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys) }
}
function overlaps(a, b) {
  return a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1
}
const finite = (p) => Number.isFinite(p.x) && Number.isFinite(p.y)

// A two-theme tree that spans several depths (so each theme recurs across bands) + a cross-theme merge.
function twoThemeGraph(extra = []) {
  return mk([
    { id: 1, parents: [], theme: 'A', metric: 0.10, operator: 'draft' },
    { id: 2, parents: [1], theme: 'A', metric: 0.11 },
    { id: 3, parents: [1], theme: 'B', metric: 0.90 },
    { id: 4, parents: [2], theme: 'A', metric: 0.12 },
    { id: 5, parents: [2], theme: 'A', metric: 0.13 },
    { id: 6, parents: [3], theme: 'B', metric: 0.91 },
    { id: 7, parents: [3], theme: 'B', metric: 0.92 },
    { id: 8, parents: [4], theme: 'A', metric: 0.14 },
    { id: 9, parents: [6], theme: 'B', metric: 0.93 },
    { id: 10, parents: [8, 9], theme: null },          // merge across themes (themeless)
    ...extra,
  ])
}

test('layered mode (operator): every node placed, one region cell per group, no NaN', () => {
  const ns = twoThemeGraph()
  const groups = computeGroups(ns, 'operator')
  const ng = nodeGroupMap(groups)
  const { pos, cells } = layoutWithGroups(ns, { nodeGroup: ng, groupMode: 'operator' })
  for (const id of Object.keys(ns)) assert.ok(pos[`n:${id}`] && finite(pos[`n:${id}`]), `node ${id} placed`)
  // one cell per group key present
  assert.equal(cells.length, new Set([...ng.values()]).size)
})

test('banded theme: all nodes placed with finite coords + returns per-band cells', () => {
  const ns = twoThemeGraph()
  const groups = computeGroups(ns, 'theme')
  const ng = nodeGroupMap(groups)
  const { pos, cells } = layoutWithGroups(ns, { nodeGroup: ng, groupMode: 'theme' })
  for (const id of Object.keys(ns)) assert.ok(pos[`n:${id}`] && finite(pos[`n:${id}`]), `node ${id} placed`)
  // theme A spans depths 0..3 -> >1 band -> >1 cell for A
  const aCells = cells.filter(c => c.key === 'A')
  assert.ok(aCells.length >= 2, 'theme A produces a cell in more than one band')
  assert.ok(aCells.every(c => typeof c.band === 'number'), 'banded cells carry a band index')
})

test('banded theme: no two group cells overlap (the core fix)', () => {
  const ns = twoThemeGraph()
  const groups = computeGroups(ns, 'theme')
  const ng = nodeGroupMap(groups)
  const { pos, cells } = layoutWithGroups(ns, { nodeGroup: ng, groupMode: 'theme' })
  const boxes = cells.map(c => cellBox(c, pos))
  for (let i = 0; i < boxes.length; i++)
    for (let j = i + 1; j < boxes.length; j++)
      assert.ok(!overlaps(boxes[i], boxes[j]), `cells ${i} and ${j} must not overlap`)
})

// Append-stability under the cases the first cut of this test dodged: (a) a normal append, and
// (b) an append with a HUGE metric that would have flipped a centroid-metric rank and swapped whole
// bands. Cell order keys on the immutable discovery rank now, so neither must move a placed node.
function assertStableAppend(extra, msg) {
  const before = twoThemeGraph()
  const ng1 = nodeGroupMap(computeGroups(before, 'theme'))
  const { pos: p1 } = layoutWithGroups(before, { nodeGroup: ng1, groupMode: 'theme' })
  const after = twoThemeGraph(extra)
  const ng2 = nodeGroupMap(computeGroups(after, 'theme'))
  const { pos: p2 } = layoutWithGroups(after, { nodeGroup: ng2, groupMode: 'theme' })
  for (const id of Object.keys(before))
    assert.deepEqual(p2[`n:${id}`], p1[`n:${id}`], `${msg}: node ${id} stayed put`)
}
test('banded theme: appending a node never moves already-placed nodes (append-stability)', () => {
  assertStableAppend([{ id: 11, parents: [2], theme: 'A', metric: 0.10 }], 'normal append')
  // metric 999 would push theme A's centroid far above B's — under metric-keyed ordering this flips
  // the rank and swaps the A/B columns; under discovery-order ranking nothing moves.
  assertStableAppend([{ id: 11, parents: [2], theme: 'A', metric: 999 }], 'rank-flip-immune append')
})

test('autoCollapseSet: folds settled groups but keeps champion / selected / working / tiny / pending open', () => {
  const ns = mk([
    // theme S: 3 settled nodes -> collapsible
    { id: 1, theme: 'S' }, { id: 2, parents: [1], theme: 'S' }, { id: 3, parents: [2], theme: 'S' },
    // theme C (champion): settled but must stay open
    { id: 4, theme: 'C' }, { id: 5, parents: [4], theme: 'C' }, { id: 6, parents: [5], theme: 'C' },
    // theme P: has a pending node -> not settled
    { id: 7, theme: 'P' }, { id: 8, parents: [7], theme: 'P' }, { id: 9, parents: [8], theme: 'P', status: 'pending' },
    // theme T: too small (2) -> skipped
    { id: 10, theme: 'T' }, { id: 11, parents: [10], theme: 'T' },
  ])
  const groups = computeGroups(ns, 'theme')
  const set = autoCollapseSet(ns, groups, { mode: 'theme', bestId: 6 })
  assert.ok(set.has('S'), 'settled non-special group folds')
  assert.ok(!set.has('C'), 'champion group stays open')
  assert.ok(!set.has('P'), 'group with a pending node stays open')
  assert.ok(!set.has('T'), 'tiny group is left alone')
  // selected / working overrides
  const set2 = autoCollapseSet(ns, groups, { mode: 'theme', bestId: 6, selectedId: 1 })
  assert.ok(!set2.has('S'), 'selected group stays open')
  const set3 = autoCollapseSet(ns, groups, { mode: 'theme', bestId: 6, workId: 2 })
  assert.ok(!set3.has('S'), 'working-node group stays open')
  // disabled outside theme/niche
  assert.equal(autoCollapseSet(ns, computeGroups(ns, 'operator'), { mode: 'operator' }).size, 0)
})

test('isMergeEntryEdge: true only when the child has ≥2 parents', () => {
  assert.equal(isMergeEntryEdge({ parent_ids: [1, 2] }), true)
  assert.equal(isMergeEntryEdge({ parent_ids: [1] }), false)
  assert.equal(isMergeEntryEdge({ parent_ids: [] }), false)
  assert.equal(isMergeEntryEdge(null), false)
})

test('similarityRank: theme orders by stable discovery (NOT live metric); niche by param value', () => {
  const ns = twoThemeGraph()
  const ng = nodeGroupMap(computeGroups(ns, 'theme'))
  const rank = similarityRank(ns, ng, 'theme')
  assert.ok(rank.get('A') < rank.get('B'), 'earlier-discovered theme A ranks before B')
  // ordering must NOT depend on metrics: blow up A's metrics and the rank is unchanged (append-stable).
  const churned = twoThemeGraph()
  for (const id of [1, 2, 4, 5, 8]) churned[id].metric = 999
  const rank2 = similarityRank(churned, nodeGroupMap(computeGroups(churned, 'theme')), 'theme')
  assert.equal(rank2.get('A'), rank.get('A'))
  assert.equal(rank2.get('B'), rank.get('B'))

  const niche = mk([
    { id: 1, params: { lr: 1 } }, { id: 2, params: { lr: 3 } }, { id: 3, params: { lr: 2 } },
  ])
  // niche keys look like "lr=1" etc.
  const gn = computeGroups(niche, 'niche'); const ngn = nodeGroupMap(gn)
  const rn = similarityRank(niche, ngn, 'niche')
  const keyFor = (lr) => [...gn.keys()].find(k => k.includes(`lr=${lr}`))
  assert.ok(rn.get(keyFor(1)) < rn.get(keyFor(2)) && rn.get(keyFor(2)) < rn.get(keyFor(3)),
    'niche groups order by ascending param value')

  // non-numeric niche params must not crash or produce NaN ranks (deterministic total order)
  const strn = mk([{ id: 1, params: { act: 'relu' } }, { id: 2, params: { act: 'gelu' } }, { id: 3, params: { opt: 'adam' } }])
  const gs = computeGroups(strn, 'niche'); const rs = similarityRank(strn, nodeGroupMap(gs), 'niche')
  const ranks = [...gs.keys()].map(k => rs.get(k))
  assert.equal(new Set(ranks).size, gs.size, 'every niche group gets a distinct, non-NaN rank')
  assert.ok(ranks.every(r => Number.isInteger(r)), 'ranks are integers, never NaN')
})

test('collapsed group: its members vanish into a super-node, no region cell emitted for it', () => {
  const ns = twoThemeGraph()
  const groups = computeGroups(ns, 'theme')
  const ng = nodeGroupMap(groups)
  const collapsed = new Set(['A'])
  const { pos, cells } = layoutWithGroups(ns, { nodeGroup: ng, groupMode: 'theme', collapsed })
  assert.ok(pos['super:A'] && finite(pos['super:A']), 'collapsed group A has a super-node position')
  assert.ok(!cells.some(c => c.key === 'A'), 'no region cell for the collapsed group')
  assert.ok(cells.some(c => c.key === 'B'), 'expanded group B still has cells')
})
