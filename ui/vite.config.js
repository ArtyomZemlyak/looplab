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
        // CODEX AGENT: Stable module-id ordering lets related literals share each gzip stream's
        // dictionary. Rolldown applies it only where dependency order stays valid; the manifest
        // cycle gate below still fails closed if a chunk topology becomes unsafe.
        chunkModulesOrder: 'module-id',
      },
      // Prefer the smaller equivalent module wrapper form. The default PIFE wrapper trades a little
      // more shipped code for startup speed; the UI's measured bundle budget favors transfer size.
      optimization: {
        pifeForModuleWrappers: false,
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
        // Native ESM ordering avoids Rolldown's runtime ordering shim (about 4.2 KiB gzip here).
        // check:bundle rejects static manifest cycles, so an unsafe manual-chunk topology fails CI.
        strictExecutionOrder: false,
        codeSplitting: {
          groups: [
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
              // Charts and analysis projections have the same run/report/panel consumers; the only
              // narrower consumer is DirectionsOverview, whose app closures already include Dag.
              // One stream shares their dictionary and removes a redundant gzip/chunk boundary.
              test: /[/\\]src[/\\](report|reportModel|researchMemoModel|trustSemantics|charts)\.(js|jsx)$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'text-support',
              // Markdown is already present wherever the narrower code viewer is opened. Sharing
              // their parser/rendering dictionary removes another tiny transfer boundary without
              // pulling either feature into the app shell.
              test: /[/\\]src[/\\](CodeViewer\.jsx|lineDiff\.js|markdown\.jsx)$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'graph-support',
              test: /[/\\]src[/\\](dagViewport|grouping)\.js$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'settings-support',
              // The Settings route and the run-local Settings panel share the same bounded schema,
              // coercion and form renderer. One interaction-scoped stream avoids a second wrapper
              // and lets their repeated field vocabulary share a gzip dictionary.
              test: /[/\\]src[/\\](Settings|SettingsForm|settingsModel|settingsSchema)\.(js|jsx)$/,
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
              // Polling hooks and the run index travel together on every owner list/run surface.
              test: /[/\\]src[/\\](hooks|runIndex)\.js$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'timeline-support',
              // The virtualized feed is the primary consumer of its canonical event model.
              test: /[/\\]src[/\\](timelineModel\.js|timelineWindow\.js|VirtualTimeline\.jsx)$/,
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
