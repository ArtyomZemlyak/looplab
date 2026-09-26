// WHAT REFUSED A RANKING, RENDERED. `comparabilityRefusalReason.test.js` drives the model; this
// file is the other half — that every surface which words a comparability refusal PRINTS the
// model's words, driven from run rows and node records through the real component, never from a
// group built by hand. Each of these was mutated back to generic text by the critic (2026-09-26)
// and no test went red: the Pareto banner, the ResearchView chip, the Registry note, a refused
// row's title and the split header. The run list's metric-sort option had no test reading its
// label at all, which is how "(tasks or directions differ)" reached a list with one task selected.
//
// One Vite server and one jsdom for the file (`_mount.js::mountLive`). The panels that read
// `/api/runs` do it in an effect, so they are MOUNTED live against a path-keyed fetch stub; the two
// surfaces that take their data as props render statically, where nothing is fetched.
import test from 'node:test'
import assert from 'node:assert/strict'

import { fetchStub, mountLive, until } from './_mount.js'

const record = (extra = {}) => ({ version: 1, authority: 'measured', keys: { measured: 'k1' },
  ...extra })
const profile = name => record({ protocol: { profile: name } })
// Grouped by the DECLARED key, refused by the stronger MEASURED one (see the model test).
const declared = measured => ({ version: 1, authority: 'declared',
  keys: { declared: 'd1', measured } })
const row = (id, metric, comparability, extra = {}) => ({
  run_id: id, label: id, task_id: 'repo_task', direction: 'max', best_metric: metric,
  best_confirmed: null, nodes: 4, finished: true, phase: 'finished', mtime: 1_700_000_000,
  best_metric_caveats: [], best_metric_comparability: comparability, ...extra,
})

let harness
let panels
let RunList
let ResearchView

test.before(async () => {
  harness = await mountLive({ visible: true })
  panels = await harness.load('/src/panels.jsx')
  ;({ default: RunList } = await harness.load('/src/RunList.jsx'))
  ;({ default: ResearchView } = await harness.load('/src/ResearchView.jsx'))
})

test.after(async () => {
  await harness?.close()
})

// What the operator reads, as whitespace-folded strings — never DOM nodes in an assertion.
const text = element => (element?.textContent ?? '').replace(/\s+/g, ' ').trim()
const staticDom = markup => {
  const host = document.createElement('div')
  host.innerHTML = markup
  return host
}

// Mount `Component` against a server whose `/api/runs` answers `rows`, and wait for `ready`.
async function mountOver(Component, props, rows, ready, what) {
  globalThis.fetch = fetchStub({
    '/api/runs': rows,
    '/api/projects': { projects: [], assignments: {} },
    '/api/supertasks': { supertasks: [], assignments: {} },
  })
  sessionStorage.clear()
  localStorage.clear()
  const view = await harness.mount(Component, props)
  await until(() => ready(view.container), what)
  return view
}

// The cross-run panel over `rows`, opened from a run of `task`, read as the operator sees it.
async function crossRun(rows, task = 'repo_task') {
  const view = await mountOver(panels.CrossRunPanel,
    { state: { task_id: task, direction: 'max' }, onClose() {} }, rows,
    container => /This server holds/.test(container.textContent), 'the cross-run coverage line')
  const c = view.container
  const groups = [...c.querySelectorAll('.notice.resource-warning')]
    .filter(notice => notice.querySelector('b'))
    .map(notice => ({
      header: text(notice.querySelector('b')),
      claim: text(notice.querySelector('b + span')),
      lines: [...notice.querySelectorAll('li')].map(text),
      marks: [...notice.querySelectorAll('b span')].map(span => ({
        text: text(span), title: span.getAttribute('title') || '' })),
    }))
  const read = {
    toolbar: text(c.querySelector('.panel-resource-toolbar')),
    groups,
    rows: [...c.querySelectorAll('tbody tr:not(.xr-group)')].map(tr => ({
      cells: [...tr.children].map(text),
      rankTitle: tr.querySelector('td:first-child span')?.getAttribute('title') || '',
    })),
    trajectory: [...c.querySelectorAll('.xr-trajectory .muted')].map(text),
    coverage: text([...c.querySelectorAll('div.muted')].find(div => /^\s*This server holds/.test(
      div.textContent))),
  }
  await view.unmount()
  return read
}

test('CrossRunPanel: a group refused with NOTHING varying names its refusal everywhere it speaks',
  async () => {
    // Keys refused at a stronger authority than the partition's: before, the header read
    // "… · evaluation declared:d1 · · not ranked", the claim blamed a source tree or protocol, the
    // overlay counted two complete runs as prefix-folded, and coverage said they "provably differ".
    const screen = await crossRun([row('a', 0.5, declared('m1')), row('b', 0.7, declared('m2'))])
    assert.equal(screen.groups.length, 1)
    const [group] = screen.groups
    assert.equal(group.header, 'repo_task · maximize · 2 runs · evaluation declared:d1 · not ranked.')
    assert.equal(group.claim, 'None of these 2 runs of repo_task holds a rank: two of them were '
      + 'measured against different evaluation inputs (their recorded comparability keys differ). '
      + 'Each value is shown and is true of its own measurement; no ordering between them is.')
    assert.doesNotMatch(group.lines.join(' '), /provably differ from|source tree or protocol/)
    const notRanked = group.marks.find(mark => mark.text === 'not ranked')
    assert.match(notRanked.title, /^no rank: two runs of this group were measured against different evaluation inputs/)
    const evaluation = group.marks.find(mark => mark.text === 'evaluation declared:d1')
    assert.match(evaluation.title, /the key groups them, it does not make them one evaluation/)
    // THE REFUSED ROW'S TITLE names the refusal, on every row — both still on screen with a value.
    assert.deepEqual(screen.rows.map(r => r.cells.slice(0, 3)), [['—', 'a', '0.5'], ['—', 'b', '0.7']])
    for (const r of screen.rows) {
      assert.match(r.rankTitle,
        /^no rank: two runs of this group were measured against different evaluation inputs \(their recorded comparability keys differ\) — no ordering/)
      assert.doesNotMatch(r.rankTitle, /PREFIX/)
    }
    assert.match(screen.trajectory.join(' '), /2 runs are not drawn, for the reason they hold no rank: a pair of this group's runs provably disagrees/)
    assert.doesNotMatch(screen.trajectory.join(' '), /prefix-folded/)
    assert.match(screen.coverage, /2 runs are shown without a rank: a pair in their group provably disagrees on its evaluation, and no split by source tree or protocol separates them\./)
    assert.doesNotMatch(screen.coverage, /provably differ from|still disagree after that split/)
    assert.match(screen.toolbar, /1 group of this task, 0 comparable/)
  })

test('CrossRunPanel: task ids differing only by whitespace are one task, ranked as one', async () => {
  // Opened from the padded run itself: its own task id is trimmed as the groups are.
  const screen = await crossRun([row('a', 0.5, record()), row('b', 0.6, record(), { task_id: 'repo_task ' })],
    'repo_task ')
  assert.deepEqual(screen.groups.map(group => group.header),
    ['repo_task · maximize · 2 runs · evaluation measured:k1.'])
  assert.deepEqual(screen.rows.map(r => r.cells.slice(0, 3)), [['#1', 'b', '0.6'], ['#2', 'a', '0.5']])
  assert.match(screen.toolbar, /1 group of this task, 1 comparable/)
  assert.match(screen.coverage, /2 of them sit in 1 comparable group\./)
})

test('CrossRunPanel: the SPLIT HEADER names what split each part, and "provably" only where proven',
  async () => {
    const screen = await crossRun([row('p1', 0.5, profile('P1')), row('p2', 0.6, profile('P2')),
      row('none', 0.7, record())])
    const split = header => screen.groups.find(group => group.header.includes(header))
      .marks.find(mark => mark.title && mark.text.startsWith('eval profile'))
    const p1 = split('eval profile P1')
    assert.equal(p1.text, 'eval profile P1')
    assert.match(p1.title, /recorded a different eval profile provably differ from this run \(eval profile P1\)/)
    assert.match(p1.title, /recorded no eval profile are ranked apart from this run as well, though a missing eval profile proves no difference/)
    // THE ABSENCE PART: set apart, never said to "provably differ".
    const none = split('eval profile (none recorded)')
    assert.equal(none.text, 'eval profile (none recorded)')
    assert.match(none.title, /^This run recorded no eval profile, while other runs of this task with the same comparability key recorded conflicting ones — not recorded, so not comparable with either side\./)
    assert.doesNotMatch(none.title, /provably/)
    const noneGroup = screen.groups.find(group => group.header.includes('(none recorded)'))
    assert.doesNotMatch(noneGroup.lines.join(' '), /provably differ/)
    // COVERAGE: two runs provably differ; the third is said to be set apart without proof.
    assert.match(screen.coverage, / 2 runs share a task and comparability key with others but provably differ from some of them in source tree or evaluation protocol, so they are grouped by that as well\./)
    assert.match(screen.coverage, / 1 run recorded none of the source tree or protocol facets that split its task and key — not recorded, so not comparable with either side — and is grouped apart without a proven difference\./)
    // ONE COUNT: three groups, none of them comparable — in the toolbar and in the coverage line.
    assert.match(screen.toolbar, /3 groups of this task, 0 comparable · ranked within a group only · a group is the runs of one task and one direction that share a comparability key \(or record none\), split further by source tree and protocol where those disagree; a comparable group holds two or more runs a ranking may order/)
    assert.match(screen.coverage, /This server holds 3 runs; 0 of them sit in 0 comparable groups\./)
  })

test('CrossRunPanel: the toolbar and the coverage line count comparable groups by one rule', async () => {
  // "2 comparable groups" over "2 of them sit in 1 comparable group" was the other scenario.
  const screen = await crossRun([row('s1', 0.61, profile('aaaa')), row('s2', 0.64, profile('aaaa')),
    row('f1', 0.72, profile('bbbb'))])
  assert.match(screen.toolbar, /2 groups of this task, 1 comparable/)
  assert.match(screen.coverage, /2 of them sit in 1 comparable group\./)
  assert.deepEqual(screen.groups.map(group => group.header), [
    'repo_task · maximize · 2 runs · evaluation measured:k1 · eval profile aaaa.',
    'repo_task · maximize · 1 run · evaluation measured:k1 · eval profile bbbb.'])
})

test('ParetoPanel: the banner names the facet that refused the pair', () => {
  const node = (id, metric, comparability) => ({ id, status: 'evaluated', metric, feasible: true,
    violations: [], operator: 'mutate', metric_provenance: { comparability } })
  const banner = (a, b) => text([...staticDom(harness.render(panels.ParetoPanel, {
    state: { nodes: { 1: node(1, 0.5, a), 2: node(2, 0.6, b) }, direction: 'max', run_id: 'demo' },
    onClose() {},
  })).querySelectorAll('div.warn')].find(div => /not all measured/.test(div.textContent)))
  assert.match(banner(record({ substrate: 's1' }), record({ substrate: 's2' })),
    /not all measured against the same evaluation — two of them ran on different source trees \(a fix promoted into the editable repo/)
  assert.match(banner(profile('P1'), profile('P2')),
    /— two of them were scored under different eval profiles \(a different set of profile overrides/)
  assert.equal(banner(profile('P1'), profile('P1')), '', 'no banner over one ruler')
})

test('ResearchView: the mixed-comparability chip names the facet that refused the pair', () => {
  const cards = [
    { id: 'q1', card_kind: 'direction', statement: 'distillation raises recall',
      concept_tags: ['distill'], child_card_ids: ['e1', 'e2'],
      child_rollup: { children: 2, best_delta: 0.05, best_card_id: 'e2' } },
    { id: 'e1', card_kind: 'experiment', parent_card_id: 'q1', best_delta: 0.02, evidence: ['n1'] },
    { id: 'e2', card_kind: 'experiment', parent_card_id: 'q1', best_delta: 0.05, evidence: ['n2'] },
  ]
  const chip = (a, b) => staticDom(harness.render(ResearchView, {
    cards, state: { nodes: { n1: { metric_provenance: { comparability: a } },
      n2: { metric_provenance: { comparability: b } } } }, renderCard: () => null,
  })).querySelector('.research-row-facts .chip.warn')
  const mixed = chip(profile('P1'), profile('P2'))
  assert.match(text(mixed), /mixed comparability/)
  assert.equal(mixed.getAttribute('title'), 'two of the experiments behind these numbers were scored '
    + 'under different eval profiles (a different set of profile overrides — e.g. a smoke pass '
    + 'against a full one), so this best won a mixed field')
  assert.match(chip(record({ substrate: 's1' }), record({ substrate: 's2' })).getAttribute('title'),
    /^two of the experiments behind these numbers ran on different source trees/)
})

test('RegistryPanel: the "listed, not ranked" note says which refusal', async () => {
  const note = async rows => {
    const view = await mountOver(panels.RegistryPanel,
      { state: { run_id: 'demo', nodes: {} }, onClose() {} }, rows,
      container => /Cross-run best metric per run|Listed, not ranked/.test(container.textContent),
      'the registry table')
    const found = text([...view.container.querySelectorAll('div.muted')]
      .find(div => /^\s*Listed, not ranked/.test(div.textContent)))
    await view.unmount()
    return found
  }
  assert.equal(await note([row('a', 0.5, profile('P1')), row('b', 0.6, profile('P2'))]),
    'Listed, not ranked: these runs share one task and objective, but two of them were scored under '
    + 'different eval profiles (a different set of profile overrides — e.g. a smoke pass against a '
    + 'full one).')
  assert.equal(await note([row('a', 0.5, record()), row('b', 0.6, record(), { direction: 'min' })]),
    'Listed, not ranked: these runs share one task, but one minimizes its metric where another '
    + 'maximizes it.')
  assert.equal(await note([row('a', 0.5, record()), row('b', 0.6, record(), { task_id: 'other' })]),
    'Listed, not ranked: these runs are of different tasks.')
  assert.equal(await note([row('a', 0.5, record()), row('b', 0.6, record())]), '',
    'a comparable set is ranked, with no note')
})

test('RunList: the metric-sort option says what differs, and only that', async () => {
  const option = async (rows, navigation) => {
    const view = await mountOver(RunList,
      { onOpen() {}, onGlobalNavigate() {}, initialNavigationState: navigation }, rows,
      container => container.querySelector('select[aria-label="Sort runs by"] option[value="metric"]'),
      'the run list sort control')
    const metric = view.container.querySelector('select[aria-label="Sort runs by"] option[value="metric"]')
    const read = { label: text(metric), disabled: metric.disabled }
    await view.unmount()
    return read
  }
  // ONE task selected, its runs split only by DIRECTION: the option named a task difference.
  assert.deepEqual(await option([row('a', 0.5, record()), row('b', 0.6, record(), { direction: 'min' })],
    { task: 'repo_task' }), { label: 'best metric (directions differ)', disabled: true })
  assert.deepEqual(await option([row('a', 0.5, profile('P1')), row('b', 0.6, profile('P2'))],
    { task: 'repo_task' }), { label: 'best metric (eval profiles differ)', disabled: true })
  assert.deepEqual(await option([row('a', 0.5, record()), row('b', 0.6, record())], {}),
    { label: 'best metric (select one task)', disabled: true })
  assert.deepEqual(await option([row('a', 0.5, record()), row('b', 0.6, record())],
    { task: 'repo_task' }), { label: 'best metric', disabled: false })
})
