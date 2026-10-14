// Is the JavaScript in THIS tab the bundle the server now serves? (review 2026-10-14 section 3.4, section 10.4)
//
// A rebuilt `ui/dist` reaches only tabs opened after it: an open SPA tab keeps its old bundle, and the
// operator saw fixed problems ("Needs a model… backend=toy") in a tab opened before the rebuild. The
// build stamps one id into the bundle (`__LOOPLAB_BUILD_ID__`, vite.config.js) and into index.html
// (`<meta name="looplab-build">`); the tab re-reads the served index.html now and then and compares.
// A NOTICE, never a reload: a reload would discard a draft the operator is typing.
export const BUILD_META = 'looplab-build'
const ID_RE = /^[0-9a-f]{12,64}$/

export function currentBuildId() {
  // eslint-disable-next-line no-undef
  const value = typeof __LOOPLAB_BUILD_ID__ === 'string' ? __LOOPLAB_BUILD_ID__ : ''
  return ID_RE.test(value) ? value : ''
}

// The build id the served index.html declares, or '' (no meta / malformed / not HTML).
export function buildIdFromHtml(html) {
  const text = typeof html === 'string' ? html.slice(0, 20_000) : ''
  const match = /<meta\s+name=["']looplab-build["']\s+content=["']([0-9a-f]{12,64})["']/i.exec(text)
  return match ? match[1] : ''
}

// Stale only when BOTH ids are known and differ: a dev server, an old server or a failed read
// never claims an update it cannot prove.
export function bundleIsStale(current, served) {
  return Boolean(current && served && current !== served)
}

// Where index.html lives for this tab: the directory of the page (proxy prefixes included).
export function indexUrl(locationLike) {
  const path = String(locationLike?.pathname || '/')
  return path.endsWith('/') ? path : path.replace(/[^/]*$/, '')
}
