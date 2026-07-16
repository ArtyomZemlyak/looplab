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
    // The post-build budget gate resolves route closures from Vite's graph instead of guessing from
    // hashed filenames. Keep the normal 500 kB warning as a visible early signal; the stricter raw /
    // gzip and reachability budgets live in scripts/check-bundle.mjs and fail CI.
    manifest: true,
    chunkSizeWarningLimit: 500,
    rollupOptions: {
      output: {
        // Keep the only vendor split tied to the graph interaction boundary. Small application
        // groups consolidate modules used together across the same owner workspaces, avoiding many
        // tiny gzip streams without crossing the route/panel boundaries enforced by the bundle
        // checker. Never capture dependencies recursively: that would pull React/core into a group.
        strictExecutionOrder: true,
        codeSplitting: {
          groups: [
            {
              name: 'vendor-flow',
              test: /[/\\]node_modules[/\\]@xyflow[/\\]/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'analysis-support',
              test: /[/\\]src[/\\](report|reportModel|researchMemoModel|trustSemantics)\.js$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'code-support',
              test: /[/\\]src[/\\](CodeViewer\.jsx|lineDiff\.js)$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'graph-support',
              test: /[/\\]src[/\\](dagViewport|grouping)\.js$/,
              includeDependenciesRecursively: false,
            },
            {
              name: 'ui-primitives',
              test: /[/\\]src[/\\](EnergyToggle|PanelShell|icons|runMapModel|useDialogFocus)\.(js|jsx)$/,
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
