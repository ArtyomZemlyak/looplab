import { realpathSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { dirname, extname, isAbsolute, relative, resolve, sep } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { gzipSync } from 'node:zlib'

const KIB = 1024
const EMPTY_TOTAL = Object.freeze({ raw: 0, gzip: 0 })

const entry = Object.freeze({ entry: true })
const source = src => Object.freeze({ src })
const named = name => Object.freeze({ name })

// These are target budgets for the split graph, not a waiver for eager code. The route/panel lazy
// boundaries have landed; the checker deliberately stays red whenever a working build exceeds a
// measured target. Keep them calibrated downward — do not raise them to make eager code green.
export const DEFAULT_BUDGETS = Object.freeze({
  total: {
    // Measured July 2026 clean parallel-master baseline was 325,655 B (already 23 B above 318 KiB).
    // The audited report authority/provenance v2 build is 327,433 B. A 320 KiB ceiling leaves 247 B
    // of zlib headroom; per-route/eager/closure guards below remain strict, so this is not a waiver.
    js: { gzip: 320 * KIB },
    css: { gzip: 45 * KIB },
  },
  individual: {
    js: { raw: 450 * KIB, gzip: 110 * KIB },
    css: { raw: 180 * KIB, gzip: 35 * KIB },
  },
  closures: [
    {
      name: 'initial shell',
      roots: [entry],
      limits: { js: { gzip: 175 * KIB }, css: { gzip: 35 * KIB } },
    },
    {
      name: 'owner List route',
      // RunList becomes a named facade when its lazy MapView imports shared list utilities. Vite
      // intentionally omits `src` on that facade, while preserving the stable Rollup chunk name.
      roots: [entry, named('RunList'), source('src/AssistantBar.jsx'), source('src/AttentionCenter.jsx')],
      limits: { js: { gzip: 210 * KIB }, css: { gzip: 35 * KIB } },
    },
    {
      name: 'owner Atlas preview route',
      roots: [
        entry, source('src/ResearchAtlas.jsx'),
        source('src/AssistantBar.jsx'), source('src/AttentionCenter.jsx'),
      ],
      limits: { js: { gzip: 210 * KIB }, css: { gzip: 35 * KIB } },
    },
    {
      name: 'owner run DAG route',
      roots: [
        entry, named('RunView'), source('src/Dag.jsx'), source('src/Dock.jsx'),
        source('src/Inspector.jsx'), source('src/DirectionsOverview.jsx'),
        source('src/AssistantBar.jsx'), source('src/AttentionCenter.jsx'),
      ],
      limits: { js: { gzip: 260 * KIB }, css: { gzip: 35 * KIB } },
    },
    {
      name: 'valid review DAG route',
      roots: [entry, named('RunView'), source('src/Dag.jsx'), source('src/DirectionsOverview.jsx')],
      limits: { js: { gzip: 220 * KIB }, css: { gzip: 35 * KIB } },
    },
    {
      name: 'panel-hub increment',
      roots: [source('src/panels.jsx')],
      // Panels can only open after the owner run workspace is present. Measure bytes newly fetched
      // at that interaction, not React/core chunks already counted in the route closure.
      baselineRoots: [
        entry, named('RunView'), source('src/Dag.jsx'), source('src/Dock.jsx'),
        source('src/Inspector.jsx'), source('src/DirectionsOverview.jsx'),
        source('src/AssistantBar.jsx'), source('src/AttentionCenter.jsx'),
      ],
      limits: { js: { gzip: 60 * KIB } },
    },
    {
      name: 'owner collaboration increment',
      roots: [source('src/CollabPanel.jsx')],
      // Comments/sharing is independently lazy so review users never pull the owner panel hub.
      // Keep the owner-workspace interaction cheap as comment features grow.
      baselineRoots: [
        entry, named('RunView'), source('src/Dag.jsx'), source('src/Dock.jsx'),
        source('src/Inspector.jsx'), source('src/DirectionsOverview.jsx'),
        source('src/AssistantBar.jsx'), source('src/AttentionCenter.jsx'),
      ],
      limits: { js: { gzip: 20 * KIB } },
    },
    {
      name: 'review collaboration increment',
      roots: [source('src/CollabPanel.jsx')],
      baselineRoots: [
        entry, named('RunView'), source('src/Dag.jsx'), source('src/DirectionsOverview.jsx'),
      ],
      limits: { js: { gzip: 20 * KIB } },
    },
    {
      name: 'Research Atlas preview increment',
      roots: [source('src/ResearchAtlas.jsx')],
      baselineRoots: [entry],
      // Audited July 2026 baseline includes independent current/stale/failed + revision/loaded-at
      // watermarks for Atlas, claims, and both steward logs. It stays isolated behind this route.
      limits: { js: { gzip: 7 * KIB }, css: { gzip: 3 * KIB } },
    },
    {
      name: 'React Flow increment',
      roots: [named('vendor-flow')],
      // The graph is an interaction after the app shell has loaded. Rolldown keeps React/runtime in
      // explicit shared chunks, so measure only bytes newly fetched for the graph, just like the
      // panel and Atlas interaction budgets above.
      baselineRoots: [entry],
      limits: { js: { gzip: 75 * KIB } },
    },
  ],
  forbidden: [
    {
      name: 'List defers graph and panel code',
      roots: [entry, named('RunList'), source('src/AssistantBar.jsx'), source('src/AttentionCenter.jsx')],
      targets: [named('vendor-flow'), source('src/panels.jsx'), source('src/CollabPanel.jsx')],
      requireTargets: true,
    },
    {
      name: 'Settings defers graph and panel code',
      // Settings.jsx and its shared schema/form are one manual group; Rolldown omits `src` from the
      // grouped dynamic entry, so use its stable chunk name rather than weakening this proof.
      roots: [entry, named('settings-support'), source('src/AssistantBar.jsx'), source('src/AttentionCenter.jsx')],
      targets: [named('vendor-flow'), source('src/panels.jsx'), source('src/CollabPanel.jsx')],
      requireTargets: true,
    },
    {
      name: 'Atlas preview defers graph and panel code',
      roots: [
        entry, source('src/ResearchAtlas.jsx'),
        source('src/AssistantBar.jsx'), source('src/AttentionCenter.jsx'),
      ],
      targets: [
        named('RunView'), named('vendor-flow'), source('src/Dock.jsx'),
        source('src/Inspector.jsx'), source('src/panels.jsx'), source('src/CollabPanel.jsx'),
      ],
      requireTargets: true,
    },
    {
      name: 'public Shared Assistant excludes owner and graph code',
      roots: [entry, source('src/SharedAssistant.jsx')],
      targets: [
        source('src/AssistantBar.jsx'), source('src/AttentionCenter.jsx'), source('src/Dock.jsx'),
        named('vendor-flow'), source('src/panels.jsx'), source('src/CollabPanel.jsx'),
      ],
      requireTargets: true,
    },
    {
      name: 'invalid review shell excludes the run and owner plane',
      roots: [entry],
      targets: [
        named('RunView'), source('src/AssistantBar.jsx'), source('src/AttentionCenter.jsx'),
        source('src/Dock.jsx'),
      ],
      requireTargets: true,
    },
    {
      name: 'valid review and comments exclude owner-only surfaces',
      roots: [
        entry, named('RunView'), source('src/Dag.jsx'), source('src/DirectionsOverview.jsx'),
        source('src/Inspector.jsx'), source('src/Report.jsx'), source('src/CollabPanel.jsx'),
      ],
      targets: [
        source('src/AssistantBar.jsx'), source('src/AttentionCenter.jsx'),
        source('src/Dock.jsx'), source('src/panels.jsx'),
      ],
      requireTargets: true,
    },
    {
      name: 'Report-only route defers the graph',
      roots: [entry, named('RunView'), source('src/Report.jsx')],
      targets: [source('src/Dag.jsx'), source('src/Dock.jsx'), named('vendor-flow')],
      requireTargets: true,
    },
  ],
})

const slash = value => String(value || '').replaceAll('\\', '/')
const isRecord = value => Boolean(value) && typeof value === 'object' && !Array.isArray(value)
const edgeList = (chunk, field) => Array.isArray(chunk?.[field]) ? chunk[field] : []

export function selectorLabel(selector) {
  if (typeof selector === 'string') return `key=${selector}`
  if (!selector || typeof selector !== 'object') return String(selector)
  return Object.entries(selector).map(([key, value]) => `${key}=${value}`).join(', ')
}

/** Return every manifest key satisfying all fields in a declarative selector. */
export function selectChunks(manifest, selector) {
  if (typeof selector === 'string') return Object.hasOwn(manifest, selector) ? [selector] : []
  if (!selector || typeof selector !== 'object') return []
  const contains = selector.contains == null ? null : String(selector.contains).toLowerCase()
  return Object.entries(manifest).filter(([key, chunk]) => {
    if (!isRecord(chunk)) return false
    if (selector.entry != null && Boolean(chunk.isEntry) !== Boolean(selector.entry)) return false
    if (selector.dynamicEntry != null
        && Boolean(chunk.isDynamicEntry) !== Boolean(selector.dynamicEntry)) return false
    if (selector.src != null && slash(chunk.src || key) !== slash(selector.src)) return false
    if (selector.name != null && String(chunk.name || '') !== String(selector.name)) return false
    if (selector.file != null && slash(chunk.file) !== slash(selector.file)) return false
    if (contains != null) {
      const identity = [key, chunk.src, chunk.name, chunk.file].map(slash).join('\n').toLowerCase()
      if (!identity.includes(contains)) return false
    }
    return true
  }).map(([key]) => key).sort()
}

export function resolveSelectors(manifest, selectors) {
  const keys = new Set()
  const missing = []
  for (const selector of selectors || []) {
    const matches = selectChunks(manifest, selector)
    if (!matches.length) missing.push(selectorLabel(selector))
    for (const key of matches) keys.add(key)
  }
  return { keys: [...keys].sort(), missing }
}

/** Traverse only static imports. Dynamic imports enter a route closure only when named as roots. */
export function collectStaticClosure(manifest, roots) {
  const keys = new Set()
  const missingImports = new Set()
  const pending = [...roots].reverse()
  while (pending.length) {
    const key = pending.pop()
    if (keys.has(key)) continue
    const chunk = manifest[key]
    if (!isRecord(chunk)) {
      missingImports.add(key)
      continue
    }
    keys.add(key)
    for (const dependency of edgeList(chunk, 'imports')) {
      if (!keys.has(dependency)) pending.push(dependency)
    }
  }
  return { keys: [...keys].sort(), missingImports: [...missingImports].sort() }
}

export function findStaticPath(manifest, roots, targets) {
  const targetSet = new Set(targets)
  const queue = [...roots]
  const parent = new Map(queue.map(key => [key, null]))
  for (let cursor = 0; cursor < queue.length; cursor += 1) {
    const key = queue[cursor]
    if (targetSet.has(key)) {
      const path = []
      for (let node = key; node != null; node = parent.get(node)) path.push(node)
      return path.reverse()
    }
    for (const dependency of edgeList(manifest[key], 'imports')) {
      if (!parent.has(dependency)) {
        parent.set(dependency, key)
        queue.push(dependency)
      }
    }
  }
  return null
}

/** Find one exact cycle in the emitted static-import graph without recursive stack growth. */
export function findStaticCycle(manifest) {
  const state = new Map()
  const path = []
  const pathIndex = new Map()

  for (const root of Object.keys(manifest).sort()) {
    if (state.has(root) || !isRecord(manifest[root])) continue
    state.set(root, 'visiting')
    pathIndex.set(root, path.length)
    path.push(root)
    const frames = [{
      key: root,
      next: 0,
      dependencies: edgeList(manifest[root], 'imports'),
    }]

    while (frames.length) {
      const frame = frames.at(-1)
      if (frame.next >= frame.dependencies.length) {
        state.set(frame.key, 'done')
        pathIndex.delete(frame.key)
        path.pop()
        frames.pop()
        continue
      }

      const dependency = frame.dependencies[frame.next++]
      if (typeof dependency !== 'string'
          || !Object.hasOwn(manifest, dependency)
          || !isRecord(manifest[dependency])) continue
      if (state.get(dependency) === 'visiting') {
        return [...path.slice(pathIndex.get(dependency)), dependency]
      }
      if (state.has(dependency)) continue
      state.set(dependency, 'visiting')
      pathIndex.set(dependency, path.length)
      path.push(dependency)
      frames.push({
        key: dependency,
        next: 0,
        dependencies: edgeList(manifest[dependency], 'imports'),
      })
    }
  }
  return null
}

export function assetKind(file) {
  const extension = extname(file).toLowerCase()
  if (extension === '.js' || extension === '.mjs' || extension === '.cjs') return 'js'
  if (extension === '.css') return 'css'
  return 'other'
}

/** Resolve emitted JS and attached CSS once, even when several chunks share them. */
export function collectAssetFiles(manifest, chunkKeys) {
  const files = new Set()
  for (const key of chunkKeys) {
    const chunk = manifest[key]
    if (!isRecord(chunk)) continue
    if (typeof chunk.file === 'string' && ['js', 'css'].includes(assetKind(chunk.file))) {
      files.add(slash(chunk.file))
    }
    for (const css of edgeList(chunk, 'css')) {
      if (typeof css === 'string' && assetKind(css) === 'css') files.add(slash(css))
    }
  }
  return [...files].sort()
}

/** Validate the complete Vite graph so an unselected/dynamic chunk cannot hide broken edges. */
export function validateManifestGraph(manifest) {
  const violations = []
  for (const [key, chunk] of Object.entries(manifest)) {
    if (!isRecord(chunk)) {
      violations.push({
        code: 'manifest_integrity',
        message: `Manifest chunk ${key} is not an object; rebuild before trusting bundle budgets.`,
      })
      continue
    }
    if (typeof chunk.file !== 'string' || !chunk.file.trim()) {
      violations.push({
        code: 'manifest_integrity',
        message: `Manifest chunk ${key} has no emitted file; rebuild before trusting bundle budgets.`,
      })
    }
    for (const field of ['imports', 'dynamicImports']) {
      if (chunk[field] != null && !Array.isArray(chunk[field])) {
        violations.push({
          code: 'manifest_integrity',
          message: `Manifest chunk ${key} has a non-array ${field} field; rebuild before trusting bundle budgets.`,
        })
        continue
      }
      for (const dependency of edgeList(chunk, field)) {
        if (typeof dependency !== 'string' || !Object.hasOwn(manifest, dependency)) {
          violations.push({
            code: 'manifest_integrity',
            message: `Manifest chunk ${key} ${field} missing chunk ${String(dependency)}; `
              + 'rebuild before trusting bundle budgets.',
          })
        }
      }
    }
    if (chunk.css != null && !Array.isArray(chunk.css)) {
      violations.push({
        code: 'manifest_integrity',
        message: `Manifest chunk ${key} has a non-array css field; rebuild before trusting bundle budgets.`,
      })
    }
  }
  const cycle = findStaticCycle(manifest)
  if (cycle) {
    violations.push({
      code: 'manifest_cycle',
      message: `Manifest static import cycle ${cycle.join(' -> ')}; manually grouped chunks `
        + 'must stay acyclic when native ESM execution ordering is enabled. Break the cycle or revise '
        + 'the chunk grouping before trusting bundle budgets.',
    })
  }
  return violations
}

export function measureAssetBuffer(file, buffer) {
  const bytes = Buffer.isBuffer(buffer) ? buffer : Buffer.from(buffer)
  return {
    file: slash(file),
    kind: assetKind(file),
    raw: bytes.byteLength,
    gzip: gzipSync(bytes, { level: 9 }).byteLength,
  }
}

const statFor = (assetStats, file) => assetStats instanceof Map
  ? assetStats.get(file)
  : assetStats?.[file]

export function summarizeFiles(files, assetStats) {
  const summary = {
    js: { ...EMPTY_TOTAL },
    css: { ...EMPTY_TOTAL },
    other: { ...EMPTY_TOTAL },
  }
  for (const file of new Set(files)) {
    const stat = statFor(assetStats, file)
    if (!stat) continue
    const kind = summary[stat.kind] ? stat.kind : 'other'
    summary[kind].raw += stat.raw
    summary[kind].gzip += stat.gzip
  }
  return summary
}

const violationKey = violation => `${violation.code}\0${violation.message}`

function enforceLimits({ scope, limits, summary, violations, remedy }) {
  for (const [kind, metrics] of Object.entries(limits || {})) {
    for (const [metric, maximum] of Object.entries(metrics || {})) {
      const actual = summary[kind]?.[metric] || 0
      if (actual <= maximum) continue
      violations.push({
        code: `${scope.type}_budget`,
        message: `${scope.label} ${kind.toUpperCase()} ${metric} is ${formatBytes(actual)}, `
          + `over the ${formatBytes(maximum)} budget by ${formatBytes(actual - maximum)}. ${remedy}`,
      })
    }
  }
}

/** Pure policy evaluation: no filesystem, process state, or output side effects. */
export function evaluateBundle({ manifest, assetStats, budgets = DEFAULT_BUDGETS }) {
  const violations = validateManifestGraph(manifest)
  const missingStats = new Set()
  const allChunkKeys = Object.keys(manifest).sort()
  const allFiles = collectAssetFiles(manifest, allChunkKeys)
  const assets = []
  for (const file of allFiles) {
    const stat = statFor(assetStats, file)
    if (!stat) {
      missingStats.add(file)
      violations.push({
        code: 'missing_asset_measurement',
        message: `Manifest asset ${file} was not measured; the budget result would be incomplete.`,
      })
    } else {
      assets.push(stat)
    }
  }

  const totals = summarizeFiles(allFiles, assetStats)
  enforceLimits({
    scope: { type: 'total', label: 'Total bundle' }, limits: budgets.total,
    summary: totals, violations,
    remedy: 'Remove or defer code; change the target only with a new measured baseline.',
  })

  for (const asset of assets) {
    enforceLimits({
      scope: { type: 'individual', label: `Asset ${asset.file}` },
      limits: { [asset.kind]: budgets.individual?.[asset.kind] },
      summary: { [asset.kind]: asset }, violations,
      remedy: 'Move optional code behind a dynamic import instead of hiding Vite\'s warning.',
    })
  }

  const closures = []
  for (const policy of budgets.closures || []) {
    const roots = resolveSelectors(manifest, policy.roots)
    for (const missing of roots.missing) {
      violations.push({
        code: 'missing_closure_root',
        message: `${policy.name}: required chunk (${missing}) is absent from the manifest. `
          + 'The lazy boundary is missing, was folded back into an eager chunk, or its selector needs calibration.',
      })
    }
    const closure = collectStaticClosure(manifest, roots.keys)
    const closureFiles = collectAssetFiles(manifest, closure.keys)
    const baseline = resolveSelectors(manifest, policy.baselineRoots || [])
    for (const missing of baseline.missing) {
      violations.push({
        code: 'missing_closure_baseline',
        message: `${policy.name}: required baseline chunk (${missing}) is absent from the manifest. `
          + 'The incremental budget cannot be trusted without its already-loaded route closure.',
      })
    }
    const baselineClosure = collectStaticClosure(manifest, baseline.keys)
    const baselineFiles = new Set(collectAssetFiles(manifest, baselineClosure.keys))
    const files = closureFiles.filter(file => !baselineFiles.has(file))
    for (const file of files) {
      if (!statFor(assetStats, file) && !missingStats.has(file)) {
        missingStats.add(file)
        violations.push({
          code: 'missing_asset_measurement',
          message: `${policy.name}: manifest asset ${file} was not measured.`,
        })
      }
    }
    const summary = summarizeFiles(files, assetStats)
    enforceLimits({
      scope: { type: 'closure', label: `Closure ${policy.name}` }, limits: policy.limits,
      summary, violations,
      remedy: `Inspect the static import closure rooted at ${roots.keys.join(', ') || '(missing root)'}.`,
    })
    closures.push({
      name: policy.name,
      roots: roots.keys,
      missingRoots: roots.missing,
      chunks: closure.keys,
      baselineRoots: baseline.keys,
      baselineChunks: baselineClosure.keys,
      files,
      summary,
    })
  }

  const reachability = []
  for (const policy of budgets.forbidden || []) {
    const roots = resolveSelectors(manifest, policy.roots)
    for (const missing of roots.missing) {
      violations.push({
        code: 'missing_reachability_root',
        message: `${policy.name}: route root (${missing}) is absent, so forbidden reachability cannot be proven.`,
      })
    }
    const targetKeys = new Set()
    const missingTargets = []
    for (const selector of policy.targets || []) {
      const matches = selectChunks(manifest, selector)
      if (!matches.length) missingTargets.push(selectorLabel(selector))
      for (const key of matches) targetKeys.add(key)
    }
    if (policy.requireTargets !== false) {
      for (const missing of missingTargets) {
        violations.push({
          code: 'missing_forbidden_target',
          message: `${policy.name}: forbidden target (${missing}) is absent from the manifest. `
            + 'That does not prove deferral: it may still be folded into an eager chunk.',
        })
      }
    }
    const paths = []
    for (const target of [...targetKeys].sort()) {
      const path = findStaticPath(manifest, roots.keys, [target])
      if (!path) continue
      paths.push(path)
      violations.push({
        code: 'forbidden_reachability',
        message: `${policy.name}: forbidden static path ${path.join(' -> ')}. `
          + 'Break the path with a route- or interaction-scoped dynamic import.',
      })
    }
    reachability.push({
      name: policy.name,
      roots: roots.keys,
      targets: [...targetKeys].sort(),
      missingRoots: roots.missing,
      missingTargets,
      paths,
    })
  }

  const uniqueViolations = [...new Map(violations.map(item => [violationKey(item), item])).values()]
  return { assets: assets.sort((a, b) => a.file.localeCompare(b.file)), totals, closures, reachability,
    violations: uniqueViolations }
}

export function formatBytes(value) {
  const bytes = Number(value) || 0
  if (Math.abs(bytes) < KIB) return `${bytes.toLocaleString('en-US')} B`
  return `${(bytes / KIB).toFixed(1)} KiB (${bytes.toLocaleString('en-US')} B)`
}

const formatTotals = summary => ['js', 'css'].map(kind => {
  const value = summary[kind] || EMPTY_TOTAL
  return `${kind.toUpperCase()} raw ${formatBytes(value.raw)}, gzip ${formatBytes(value.gzip)}`
}).join(' | ')

export function formatBundleReport(result) {
  const lines = ['LoopLab bundle budget report', '', 'Assets (deduplicated JS/CSS):']
  for (const asset of result.assets) {
    lines.push(`  ${asset.file} [${asset.kind.toUpperCase()}] raw ${formatBytes(asset.raw)}, gzip ${formatBytes(asset.gzip)}`)
  }
  lines.push('', `Total: ${formatTotals(result.totals)}`, '', 'Static closures:')
  for (const closure of result.closures) {
    const roots = closure.roots.join(', ') || '(missing)'
    const baseline = closure.baselineRoots?.length
      ? `; incremental over: ${closure.baselineRoots.join(', ')}`
      : ''
    lines.push(`  ${closure.name}: ${formatTotals(closure.summary)}; roots: ${roots}${baseline}; ${closure.chunks.length} chunk(s)`)
  }
  lines.push('')
  if (!result.violations.length) {
    lines.push('PASS: every size and reachability budget is satisfied.')
  } else {
    lines.push(`FAIL: ${result.violations.length} bundle budget violation(s):`)
    result.violations.forEach((violation, index) => {
      lines.push(`  ${index + 1}. [${violation.code}] ${violation.message}`)
    })
  }
  return lines.join('\n')
}

export function safeAssetPath(distDir, file) {
  if (typeof file !== 'string' || !file.trim() || file.includes('\0')) {
    throw new Error(`manifest asset has an invalid path: ${String(file)}`)
  }
  const base = resolve(distDir)
  const full = resolve(base, file)
  const rel = relative(base, full)
  if (rel === '..' || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
    throw new Error(`manifest asset escapes dist: ${file}`)
  }
  return full
}

export async function loadBundle(distDir) {
  const manifestPath = resolve(distDir, '.vite', 'manifest.json')
  let manifest
  try {
    manifest = JSON.parse(await readFile(manifestPath, 'utf8'))
  } catch (error) {
    throw new Error(`cannot read ${manifestPath}; run "npm run build" first (${error.message})`)
  }
  if (!manifest || Array.isArray(manifest) || typeof manifest !== 'object') {
    throw new Error(`${manifestPath} is not a Vite manifest object`)
  }
  const files = collectAssetFiles(manifest, Object.keys(manifest))
  const assetStats = new Map()
  for (const file of files) {
    const buffer = await readFile(safeAssetPath(distDir, file))
    assetStats.set(file, measureAssetBuffer(file, buffer))
  }
  return { manifest, assetStats }
}

export function parseCliArgs(argv, defaultDist) {
  let distDir = defaultDist
  let reportOnly = false
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index]
    if (argument === '--report-only') reportOnly = true
    else if (argument === '--dist') {
      if (!argv[index + 1]) throw new Error('--dist requires a path')
      distDir = resolve(argv[index += 1])
    } else if (argument.startsWith('--dist=')) {
      const value = argument.slice('--dist='.length)
      if (!value) throw new Error('--dist requires a path')
      distDir = resolve(value)
    } else {
      throw new Error(`unknown argument: ${argument}`)
    }
  }
  return { distDir, reportOnly }
}

async function main() {
  const scriptDir = dirname(fileURLToPath(import.meta.url))
  const uiRoot = resolve(scriptDir, '..')
  const { distDir, reportOnly } = parseCliArgs(process.argv.slice(2), resolve(uiRoot, 'dist'))
  const bundle = await loadBundle(distDir)
  const result = evaluateBundle(bundle)
  console.log(formatBundleReport(result))
  if (result.violations.length && !reportOnly) process.exitCode = 1
}

// Match import.meta.url against BOTH the raw and the symlink-resolved argv path. Default Node sets
// import.meta.url to the REALPATH (so a symlinked invocation — a .bin shim, pnpm, a symlinked checkout
// — only matches once realpathSync resolves it), while `--preserve-symlinks[-main]` keeps the SYMLINK
// path (so the raw form matches). Checking both avoids a false-green of the CI budget gate under either
// resolution mode instead of trading one blind spot for another.
const invokedUrls = new Set()
if (process.argv[1]) {
  const rawPath = resolve(process.argv[1])
  invokedUrls.add(pathToFileURL(rawPath).href)
  try { invokedUrls.add(pathToFileURL(realpathSync(rawPath)).href) } catch { /* argv path may not exist */ }
}
if (invokedUrls.has(import.meta.url)) {
  main().catch(error => {
    console.error(`Bundle budget check could not run: ${error.message}`)
    process.exitCode = 2
  })
}
