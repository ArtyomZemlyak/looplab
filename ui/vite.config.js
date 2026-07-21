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
        inlineConst: { mode: 'all', pass: 3 },
      },
      output: {
        minify: {
          compress: { maxIterations: 10 },
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
              test: /[/\\]src[/\\](CollabPanel|CommentsThread|commentsModel|useComments)\.(js|jsx)$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'vendor-flow',
              // The app adapter and these private graph dependencies are an exact @xyflow
              // co-closure; no non-graph source imports them. One stream shares a gzip dictionary
              // without moving graph code onto any non-graph route.
              test: /(?:[/\\]node_modules[/\\](?:@xyflow|classcat|d3-[^/\\]+|use-sync-external-store|zustand)[/\\]|[/\\]src[/\\]groupnodes\.jsx$)/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'analysis-support',
              // Charts, report projections and code-diff views share the same run analysis
              // consumers. Keeping the code viewer here prevents Markdown-only Assistant surfaces
              // from downloading it while preserving one dictionary for the analysis workspace.
              test: /[/\\]src[/\\](report|reportModel|researchMemoModel|trustSemantics|charts|CodeViewer|lineDiff)\.(js|jsx)$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'text-support',
              // Markdown is shared by owner and public Assistant/report prose; keep its stream free
              // of the heavier code/diff renderer used only by run-analysis surfaces.
              test: /[/\\]src[/\\]markdown\.jsx$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'run-visual-support',
              // Run workspaces consume both semantic grouping/canvas policy and the virtual event
              // window. The optional portfolio map reaches only the same pure grouping subset: no
              // React Flow, owner controls or route component crosses into this support stream.
              test: /[/\\]src[/\\](dagViewport|grouping|timelineModel|timelineWindow)\.js$|[/\\]src[/\\]VirtualTimeline\.jsx$/,
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
              // accessibility.jsx is an app-shell dependency and also the only dependency of this
              // small, widely shared primitive group. One chunk avoids paying two gzip wrappers;
              // no route-only surface is captured here.
              test: /[/\\]src[/\\](EnergyToggle|PanelShell|accessibility|fx|icons|runMapModel|useDialogFocus)\.(js|jsx)$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'domain-support',
              // util re-exports format, and their route consumers substantially overlap. Keep the
              // pure domain helpers together while API/layout remain independently tree-shaken.
              test: /[/\\]src[/\\](format|urlSafety|util)\.js$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'live-support',
              // Polling/run-index consumers already import concept identity and receipt gates on
              // every production list/run/Concepts closure. One pure support stream removes the
              // dependency wrapper without making any route component or graph library eager.
              test: /[/\\]src[/\\](hooks|runIndex|buildingModel|conceptId|nodeProjection|conceptChips|conceptSearch|Highlight)\.(js|jsx)$/,
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
