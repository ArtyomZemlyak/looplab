// Monochrome dev/git glyph geometry lives in a versioned SVG sprite. It is bundled once (as raw markup)
// and injected a single time into the document, so OpIcon stays a tiny `<use>` (paths out of the per-icon
// render path, currentColor preserved) — but the `<use>` now references an IN-DOCUMENT symbol.
//
// WebKit/Safari does NOT resolve an EXTERNAL-document `<use href="sprite.svg#id">` (a long-standing
// security restriction), so the previous external reference rendered every icon BLANK on Safari/iOS.
// Injecting the sprite into the page and referencing `<use href="#id">` (same-document) works in every
// engine, including WebKit.
import spriteMarkup from './looplab-icons-v1.svg?raw'

const OP_ICON_NAMES = new Set(
  'flag trending bug confluence gitbranch target dot search doc alert gear user bot bolt star pause play stop replay sliders chevron-up chevron-down chat bell folder clip map compass bulb check cross pencil link download printer crown list'.split(' '),
)
const SPRITE_DOM_ID = 'looplab-icon-sprite'

// Inject the symbol defs exactly once. Guarded for SSR/tests (no document) and idempotent against a
// hot-reload or a second import. The host is visually hidden but must stay in the accessibility tree's
// blind spot (aria-hidden) and out of layout.
function ensureSprite() {
  if (typeof document === 'undefined' || !document.body) return
  if (document.getElementById(SPRITE_DOM_ID)) return
  const host = document.createElement('div')
  host.id = SPRITE_DOM_ID
  host.setAttribute('aria-hidden', 'true')
  host.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden'
  host.innerHTML = spriteMarkup   // trusted, bundled, same-origin asset — our own sprite, never user input
  document.body.prepend(host)
}

if (typeof document !== 'undefined') {
  if (document.body) ensureSprite()
  // Module may evaluate before <body> exists (non-deferred include); catch up on DOM ready.
  else document.addEventListener('DOMContentLoaded', ensureSprite, { once: true })
}

export function OpIcon({ name, size = 14, className }) {
  ensureSprite()   // idempotent safety net: guarantees the defs exist before the first icon paints
  const glyph = OP_ICON_NAMES.has(name) ? name : 'dot'
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 16 16" fill="none"
         stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"
         aria-hidden="true" focusable="false">
      <use href={`#${glyph}`} />
    </svg>
  )
}
