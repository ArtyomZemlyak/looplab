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
        // Keep the only deliberate vendor split tied to the graph interaction boundary. React and
        // every other dependency remain under Rolldown's graph-driven sharing; broad vendor buckets
        // would make lightweight routes download code they never execute. Capturing dependencies
        // recursively would pull React into this graph-only chunk and make every route load it.
        strictExecutionOrder: true,
        codeSplitting: {
          groups: [{
            name: 'vendor-flow',
            test: /[/\\]node_modules[/\\]@xyflow[/\\]/,
            includeDependenciesRecursively: false,
          }],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8765', changeOrigin: true } },
  },
})
