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
  plugins: [react()],
  build: {
    // Rolldown/Oxc performs graph-aware compression first (configured below); Terser then gives the
    // emitted chunks one final cross-statement pass. This is measurably smaller over gzip than either
    // minifier alone and keeps the production budget green without weakening any route boundary.
    minify: 'terser',
    terserOptions: {
      ecma: 2022,
      module: true,
      toplevel: true,
      safari10: false,
      compress: {
        passes: 4, pure_getters: true, keep_fargs: false,
        hoist_props: true, unsafe: true, unsafe_arrows: true, unsafe_methods: true,
        unsafe_comps: true, unsafe_proto: true, unsafe_regexp: true,
        builtins_ecma: 2022,
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
        entryFileNames: 'assets/[hash:6].js',
        chunkFileNames: 'assets/[hash:6].js',
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
              name: 'RunList',
              // Portfolio state is used by the list and its on-demand comparison child. The child is
              // reachable only from the list, so keeping the small shared model with that parent
              // removes a request without changing either route's reachable feature set.
              test: /[/\\]src[/\\](RunList\.jsx|portfolioModel\.js)$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'collaboration-support',
              // # CODEX AGENT: Both collaboration entrances use the same bounded comment reader.
              // One interaction chunk lets them share vocabulary without making it route-eager.
              test: /[/\\]src[/\\](CollabPanel|CommentsThread|commentsModel|useComments)\.(js|jsx)$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'vendor-flow',
              // The app adapter and these private graph dependencies are an exact @xyflow
              // co-closure; no non-graph source imports them. One stream shares a gzip dictionary
              // without moving graph code onto any non-graph route.
              test: /(?:[/\\]node_modules[/\\](?:@xyflow|classcat|d3-[^/\\]+|use-sync-external-store|zustand)[/\\]|[/\\]src[/\\](?:groupnodes|MapView)\.jsx$)/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'analysis-support',
              // Reports, charts and their evidence semantics form one lazy analysis workspace.
              // Keep it separate from the run shell so concepts and report routes stay bounded.
              test: /[/\\]src[/\\](report|reportModel|researchMemoModel|trustSemantics|charts|CodeViewer|lineDiff)\.(js|jsx)$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'run-support',
              // These pure API/live/text/timeline helpers are jointly present on every run workspace.
              // One stream gives repeated node/run/evidence vocabulary one gzip dictionary while
              // keeping charts, graph libraries, settings and owner controls independently lazy.
              test: /[/\\]src[/\\](?:format|urlSafety|util|hooks|runIndex|buildingModel|conceptId|nodeProjection|conceptChips|conceptSearch|Highlight|markdown|dagViewport|dagProjection|grouping|timelineModel|timelineWindow|useTimeline|useRunRouteState|mergeIntent|traceProjection|crossRunPrior)\.(?:js|jsx)$|[/\\]src[/\\]VirtualTimeline\.jsx$/,
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
              name: 'ui-primitives',
              // App-shell controls and their shared accessibility/icon implementation are always
              // co-loaded. React's small shared runtimes are on that same universal boundary; one
              // stream removes a repeated import and lets both halves share a gzip dictionary.
              // The raw sprite stays there too instead of becoming another tiny request.
              test: /[/\\]node_modules[/\\](?:react|react-dom|scheduler)[/\\]|[/\\]src[/\\](?:EnergyToggle|PanelShell|ThemeSwitcher|accessibility|fx|icons|runMapModel|useDialogFocus)\.(?:js|jsx)$|[/\\]src[/\\]looplab-icons-v1\.svg/,
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
