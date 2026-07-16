import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import { layoutWithGroups } from '../src/layout.js'
import { computeGroups, nodeGroupMap } from '../src/grouping.js'
import { createDagCanvasRefitScheduler } from '../src/dagViewport.js'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')
const NODE_W = 188
const NODE_H = 84

function desktopDemoNodes() {
  const rows = [
    [0, []], [1, []], [2, []], [3, [2]], [4, [3]],
    ...Array.from({ length: 9 }, (_, i) => [5 + i, [4]]),
    [14, [], 'manual-probe'],
    ...Array.from({ length: 4 }, (_, i) => [15 + i, [0]]),
  ]
  return Object.fromEntries(rows.map(([id, parent_ids, theme = null]) => [id, {
    id, parent_ids, status: 'evaluated', idea: { theme, params: {} },
  }]))
}

test('desktop theme layout spends width to keep a 19-node graph readable above the docks', () => {
  const nodes = desktopDemoNodes()
  const nodeGroup = nodeGroupMap(computeGroups(nodes, 'theme'))
  const { pos } = layoutWithGroups(nodes, { nodeGroup, groupMode: 'theme' })
  const points = Object.values(pos)
  const minX = Math.min(...points.map(point => point.x))
  const maxX = Math.max(...points.map(point => point.x + NODE_W))
  const minY = Math.min(...points.map(point => point.y))
  const maxY = Math.max(...points.map(point => point.y + NODE_H))
  const width = maxX - minX
  const height = maxY - minY

  assert.ok(width >= 1_250, `desktop graph should use horizontal room, got ${width}px`)
  assert.ok(height <= 450, `desktop graph should stay above the timeline, got ${height}px`)
  // At 1280x720, the fixed run chrome leaves about 430px for the canvas. The graph-owned pixel
  // insets reserve 80px horizontally and 92px vertically for controls without forcing tiny labels.
  const fittedZoom = Math.min((1_280 - 80) / width, (430 - 92) / height)
  assert.ok(fittedZoom >= 0.75, `desktop fit should keep detailed cards readable, got ${fittedZoom}`)
})

test('desktop canvas shrink schedules a second fit, then manual camera ownership stops refits', () => {
  const callbacks = new Map()
  let nextFrame = 0
  let fits = 0
  let touched = false
  const scheduler = createDagCanvasRefitScheduler({
    fit: () => { fits += 1 },
    cameraTouched: () => touched,
    requestFrame: callback => { const id = ++nextFrame; callbacks.set(id, callback); return id },
    cancelFrame: id => callbacks.delete(id),
  })
  const flushFrame = () => {
    const current = [...callbacks.values()]
    callbacks.clear()
    current.forEach(callback => callback())
  }

  assert.equal(scheduler.resize(1_280, 487), true)
  flushFrame(); flushFrame()
  assert.equal(fits, 1, 'the initial settled canvas is fitted')
  assert.equal(scheduler.resize(1_280, 251), true)
  flushFrame(); flushFrame()
  assert.equal(fits, 2, 'expanding the timeline causes a second fit against the smaller canvas')
  touched = true
  assert.equal(scheduler.resize(1_280, 487), true)
  flushFrame(); flushFrame()
  assert.equal(fits, 2, 'manual camera ownership suppresses later resize fits')
  scheduler.cancel()
})

test('late desktop canvas settling is wired to real user camera intent', async () => {
  const [dag, css] = await Promise.all([source('Dag.jsx'), source('styles.css')])
  assert.match(dag, /padding: \{ top: '50px', right: '40px', bottom: '42px', left: '40px' \}/)
  assert.match(dag, /const observer = new ResizeObserver\(entries =>/)
  assert.doesNotMatch(dag, /if \(!autoFit \|\| !nodesInitialized/,
    'virtualized offscreen nodes must not prevent the canvas observer from attaching')
  assert.match(dag, /if \(box\) refitter\.resize\(box\.width, box\.height\)/)
  assert.match(dag, /onPointerDownCapture=\{claimCamera\}/)
  assert.match(dag, /onWheelCapture=\{\(\) => \{ cameraTouchedRef\.current = true \}\}/)
  assert.match(dag, /return \(\) => \{ refitter\.cancel\(\); observer\.disconnect\(\) \}/)
  assert.match(css, /\.dag-wrap \{[^}]*min-width: 0; min-height: 0;/)
  assert.match(css, /\.dag-wrap > \.react-flow \{ overflow: hidden; \}/)
})
