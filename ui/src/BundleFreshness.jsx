import React, { useEffect, useState } from 'react'
import RunMirrorBanner from './RunMirrorBanner.jsx'
import { uiText, useUILanguage } from './uiLanguage.js'
import { bundleIsStale, buildIdFromHtml, currentBuildId, indexUrl } from './bundleFreshness.js'

const CHECK_EVERY_MS = 10 * 60_000

// The "interface was updated" banner (`bundleFreshness.js`). Checks on tab focus and every ten
// minutes; nothing is fetched when this bundle carries no build id (dev server, tests).
export default function BundleFreshness() {
  useUILanguage()
  const [stale, setStale] = useState(false)
  useEffect(() => {
    const current = currentBuildId()
    if (!current || typeof window === 'undefined' || typeof fetch !== 'function') return undefined
    let alive = true, busy = false
    const check = async () => {
      if (busy || document.visibilityState === 'hidden') return
      busy = true
      try {
        const response = await fetch(indexUrl(window.location), { cache: 'no-store', credentials: 'same-origin' })
        if (response.ok && alive) {
          const served = buildIdFromHtml(await response.text())
          if (alive && bundleIsStale(current, served)) setStale(true)
        }
      } catch { /* offline or restarting: say nothing */ } finally { busy = false }
    }
    const timer = window.setInterval(check, CHECK_EVERY_MS)
    window.addEventListener('focus', check)
    document.addEventListener('visibilitychange', check)
    return () => {
      alive = false; window.clearInterval(timer)
      window.removeEventListener('focus', check); document.removeEventListener('visibilitychange', check)
    }
  }, [])
  return <><RunMirrorBanner />{stale && <div className="notice warn bundle-stale" role="status">
    {uiText('The interface was updated. Reload the page to use the new version; save any draft first.')}{' '}
    <button type="button" className="btn sm" onClick={() => window.location.reload()}>{uiText('Reload')}</button>
  </div>}</>
}
