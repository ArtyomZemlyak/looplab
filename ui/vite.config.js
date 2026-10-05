import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Build to ui/dist (served by looplab/server.py). The dev server proxies /api to the
// Python server so `npm run dev` works against a live `LoopLab ui` backend.
//
// base:'./' makes the built index.html reference its assets RELATIVELY (./assets/…) instead of
// from the domain root (/assets/…). That's what lets the app load when it's served under a path
// prefix by a proxy — e.g. JupyterHub's `/user/<name>/proxy/8765/`. API + SSE calls join the same
// served prefix at runtime (see apiUrl in src/util.js); together they make the UI proxy-agnostic.
export default defineConfig({
  base: './',
  // React 18 supports both JSX runtimes. The classic calls avoid a separate children object/array
  // wrapper for every element in this large reader UI (~4 KiB gzip on the doc 72 baseline).
  // Components must import React; productionJsxRuntime.test.js executes shipped SSR output against
  // the automatic-runtime control, including real controls, icons, concepts and result readers.
  plugins: [react({ jsxRuntime: 'classic' })],
  build: {
    // Rolldown/Oxc performs graph-aware compression first (configured below); Terser then gives the
    // emitted chunks one final cross-statement pass. This is measurably smaller over gzip than either
    // minifier alone. Size and route boundaries are checked separately by check:bundle.
    minify: 'terser',
    terserOptions: {
      ecma: 2022,
      module: true,
      toplevel: true,
      safari10: false,
      compress: {
        passes: 4, pure_getters: true, keep_fargs: false,
        hoist_props: true, unsafe: true, unsafe_arrows: true, unsafe_methods: true,
        unsafe_proto: true, unsafe_regexp: true,
        builtins_ecma: 2022,
        // NEVER re-enable `unsafe_comps` here — second verse of the same song as
        // `booleans_as_integers` below. It lets Terser NEGATE a comparison (`a <= 0` -> `a > 0`) so
        // it can swap a ternary's branches for shorter output. That is value-preserving for every
        // operand except NaN — and `undefined`, a missing field and a non-numeric string all coerce
        // to NaN, which in this UI is not an edge case: "this number is not known" is the normal
        // shape of half these payloads, and the whole house rule is that an unknown number must not
        // render as a number. So the SHIPPED build takes the OTHER branch from the source on exactly
        // the inputs the branch exists for, and which way it flips is decided by Terser's size
        // heuristic rather than by anything anyone wrote.
        //
        // It was not hypothetical. Measured 2026-08-06 by executing the REAL Rolldown+Terser SSR
        // bundle of 44 models + 6 components against 183,008 calls and diffing it against the same
        // tree built without this flag — it was the ONLY flag in this block with any behavioural
        // difference at all (unsafe, unsafe_arrows, unsafe_methods, unsafe_proto, unsafe_regexp,
        // pure_getters, keep_fargs, hoist_props: zero differences each), and it had 42, in two
        // shipped surfaces:
        //   traceProjection.conversationWindowNotice({visibleTurns: 5})
        //       source "Showing the most recent 5 of undefined steps."  built "Trace projection is partial."
        //   runMapModel.gridColumns(undefined)     source NaN            built 1
        // Both flips happen to land on the tidier text, which is luck: the source spelling decides
        // the direction, and `withhold ? … : show` and `show ? … : withhold` flip opposite ways.
        // Invisible to dev, to SSR and to all of `node --test`, because they run UNMINIFIED.
        // What it bought, measured on the same pair of builds: 74 B raw / 26 B gzip out of 305 KB
        // raw / 96 KB gzip — 0.02%. `test/minifierComparisonGuards.test.js` drives the property.
        // NEVER re-enable `booleans_as_integers` here. It rewrites `false` to `0` and `true` to `1`,
        // which is value-preserving for every JS operator and NOT value-preserving for React: `0` is
        // falsy but React RENDERS numbers, so the house guard `{open && <Menu/>}` — correct in source,
        // invisible in dev, invisible to SSR, invisible to every unit test, because they all run
        // UNMINIFIED — painted a bare `0` on screen next to every popover trigger in the shipped
        // build (Theme, Energy, LoopLab ▾, the panel hubs, …). It costs ARIA too: `aria-expanded={open}`
        // serializes as `aria-expanded="0"`, which is not a valid ARIA boolean, so the collapsed state
        // of every menu trigger was being reported as invalid rather than "false". What it bought, on a
        // controlled A/B of this tree with only this flag flipped: 3,680 B raw / 1,028 B gzip out of
        // 488 KiB gzip — 0.2%. `test/minifierBooleanGuards.test.js` drives the property, and
        // `scripts/check-bundle.mjs::findIntegerBooleanChunks` re-checks it over the emitted bytes.
      },
      mangle: true,
      format: { comments: false },
    },
    outDir: 'dist',
    emptyOutDir: true,
    // The build target is Vite's 2026 Baseline set. Browsers outside that set may ignore the
    // modulepreload hint but still load native dynamic imports, so shipping Vite's runtime preload
    // polyfill adds transfer/startup work without changing application correctness.
    modulePreload: { polyfill: false },
    // The post-build budget gate resolves route closures from Vite's graph instead of guessing from
    // hashed filenames. Keep the normal 500 kB warning as a visible early signal; the stricter raw /
    // gzip and reachability budgets live in scripts/check-bundle.mjs and fail CI.
    manifest: true,
    chunkSizeWarningLimit: 500,
    rollupOptions: {
      treeshake: {
        // Production modules do not use bare getter reads as actions. Let Rolldown discard such
        // unused reads while preserving every property value that feeds rendering or control flow.
        propertyReadSideEffects: false,
      },
      experimental: {
        // # CODEX AGENT: `module-id` exposed a Rolldown ordering bug and produced a load-time crash.
        // Keep its native topological/cycle-aware order instead: unlike the global execution shim,
        // this preserves initialization order without adding ~7.5 KiB gzip to every build.
        chunkModulesOrder: 'exec-order',
      },
      // Prefer the smaller equivalent module wrapper form. The default PIFE wrapper trades a little
      // more shipped code for startup speed; the UI's measured bundle budget favors transfer size.
      optimization: {
        pifeForModuleWrappers: false,
        inlineConst: false,
      },
      output: {
        // Import specifiers are shipped in every split chunk. Content hashes already provide cache
        // identity, so repeating long facade names in those runtime URLs only spends transfer bytes.
        entryFileNames: 'assets/[hash:8].js',
        chunkFileNames: 'assets/[hash:8].js',
        minify: {
          compress: {
            maxIterations: 10,
            treeshake: { propertyReadSideEffects: false },
          },
          mangle: true,
          codegen: true,
        },
        // Keep the only vendor split tied to the graph interaction boundary. Small application
        // groups consolidate modules used together across the same owner workspaces, avoiding many
        // tiny gzip streams without crossing the route/panel boundaries enforced by the bundle
        // checker. Never capture dependencies recursively: that would pull React/core into a group.
        // Native ESM/topological ordering avoids Rolldown's runtime execution shim.
        // check:bundle rejects static manifest cycles, so an unsafe manual-chunk topology fails CI.
        strictExecutionOrder: false,
        codeSplitting: {
          groups: [
            {
              name: 'collaboration-support',
              // # CODEX AGENT: Both collaboration entrances use the same bounded comment reader.
              // One interaction chunk lets them share vocabulary without making it route-eager.
              test: /[/\\]src[/\\](CommentsThread|commentsModel|useComments)\.(js|jsx)$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'vendor-flow',
              // The app adapter and these private graph dependencies are an exact @xyflow
              // co-closure; no non-graph source imports them. One stream shares a gzip dictionary
              // without moving graph code onto any non-graph route.
              // Portfolio MapView stays at its own lazy entrance; DAG/review do not need its run cards.
              test: /(?:[/\\]node_modules[/\\](?:@xyflow|classcat|d3-[^/\\]+|use-sync-external-store|zustand)[/\\]|[/\\]src[/\\]groupnodes\.jsx$)/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'analysis-support',
              // Reports, charts and their evidence semantics form one lazy analysis workspace.
              // Keep it separate from the run shell so concepts and report routes stay bounded.
              // scoreComparison imports Trust semantics and is consumed by report: grouping them
              // together keeps the shared comparison boundary acyclic for DAG and report readers.
              test: /[/\\]src[/\\](report|reportModel|researchMemoModel|trustSemantics|scoreComparison|charts|CodeViewer|lineDiff)\.(js|jsx)$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'run-support',
              // These pure API/live/text/timeline helpers are jointly present on every run workspace.
              // One stream gives repeated node/run/evidence vocabulary one gzip dictionary while
              // keeping charts, graph libraries, settings and owner controls independently lazy.
              // Connection rules and panel payload primitives have the same pure dependency seam;
              // keeping them here removes two tiny streams without admitting an owner UI surface.
              // Doc 72: preferences, provenance, recovery and draft models share this dependency
              // direction. Merge their small compression streams; keep all UI entrances lazy.
              // No recursive capture: model imports must not bring a panel or owner component here.
              test: /[/\\]src[/\\](?:format|urlSafety|util|hooks|runIndex|buildingModel|nodeActivity|conceptId|nodeProjection|conceptChips|conceptSearch|Highlight|markdown|dagViewport|dagProjection|grouping|timelineModel|timelineWindow|useTimeline|useRunRouteState|mergeIntent|traceProjection|traceScrollModel|crossRunPrior|runStateModel|panelPrimitives|useAssistantLanguage|useToast|baseRevision|forkProvenance|stateDelta|runCommandMachine|conceptInspect|conceptShelf|resultMeasurement|inspectorDraftStore|authoringRecoveryStorage|extraMetrics|codeSearch|capabilityRecovery|forkFromSeqModel|commentContract|commentRecoveryStorage)\.(?:js|jsx)$|[/\\]src[/\\]VirtualTimeline\.jsx$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'settings-support',
              // The Settings route and the run-local Settings panel share the same bounded schema,
              // coercion, form renderer and loss guard. One interaction-scoped stream avoids a
              // 640-byte shared wrapper and lets their repeated field vocabulary share a dictionary.
              test: /[/\\]src[/\\](Settings|SettingsForm|settingsModel|settingsSchema|navigationLossGuard)\.(js|jsx)$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'app-core',
              // Shell, transport/recovery and React/UI primitives already share the initial static
              // closure. One compression stream reduces wrappers and repeated vocabulary without
              // adding a route or owner component to that closure (doc 72 module-set comparison).
              // Keep main.jsx and its styles outside this group: capturing the bootstrap removes
              // the manifest entry facade and makes the security/route proofs unavailable.
              // Optional owner, graph, result, settings and panel entrances remain separate.
              test: /[/\\]node_modules[/\\](?:react|react-dom|scheduler)[/\\]|[/\\]src[/\\](?:App|OwnerAuth|OwnerWorkspace|LazyBoundary|DensityToggle|globalNav|resourceModel|useScopedResource|reviewRouteApi|api|apiClient|commandStorage|commandProtocol|commandModel|scopeReportActions|runStartOverRecovery|runRouteState|runMode|controlActions|conceptLensApi|crossRunLedger|eventStream|requestDeadline|EnergyToggle|PanelShell|ThemeSwitcher|accessibility|fx|icons|runMapModel|useDialogFocus)\.(?:js|jsx)$|[/\\]src[/\\]looplab-icons-v1\.svg/,
              includeDependenciesRecursively: false,
            },
          ],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8765', changeOrigin: true } },
  },
})
