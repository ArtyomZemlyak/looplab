// task a3a32fd4: SSR the two topbar components the operator's text dump put a bare `0` next to,
// to settle empirically whether either one renders it. Throwaway probe, not a test.
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'

// minimal DOM shims: these components read localStorage / window at first render.
const store = new Map()
globalThis.window = globalThis.window || {
  addEventListener() {}, removeEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  localStorage: { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) },
  dispatchEvent() {},
}
globalThis.localStorage = globalThis.window.localStorage
globalThis.document = globalThis.document || {
  documentElement: { style: { setProperty() {} }, classList: { add() {}, remove() {}, toggle() {} }, dataset: {}, setAttribute() {}, removeAttribute() {} },
  body: { classList: { add() {}, remove() {}, toggle() {} } },
  querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {},
}
globalThis.requestAnimationFrame = globalThis.requestAnimationFrame || (fn => setTimeout(fn, 0))
globalThis.cancelAnimationFrame = globalThis.cancelAnimationFrame || clearTimeout

const vite = await createServer({
  root: fileURLToPath(new URL('.', import.meta.url)),
  configFile: false, appType: 'custom', logLevel: 'silent', server: { middlewareMode: true },
})
const text = html => html.replace(/<[^>]*>/g, '│').replace(/&#x27;/g, "'").replace(/&amp;/g, '&')
try {
  for (const spec of ['/src/GlobalMenu.jsx', '/src/EnergyToggle.jsx']) {
    const mod = await vite.ssrLoadModule(spec)
    const markup = renderToStaticMarkup(React.createElement(mod.default, {}))
    console.log(`\n=== ${spec}`)
    console.log('MARKUP :', markup)
    console.log('TEXT   :', text(markup))
    console.log('BARE 0 :', /(^|>)\s*0\s*(<|$)/.test(markup) ? 'YES' : 'no')
  }
} finally {
  await vite.close()
}
