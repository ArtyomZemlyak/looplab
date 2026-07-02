import React from 'react'

// Monochrome, single-stroke icons (inherit currentColor). Dev/git metaphors so the node "kind"
// reads instantly without childish emoji or extra hue — type lives on FORM, status keeps color.
// Each entry is the inner <path>/<circle> set of a 16×16 viewBox.
const GLYPHS = {
  // draft / baseline = a planted flag (a starting point), clearer than a seedling
  flag: <><path d="M4 14.5V2" /><path d="M4 2.6h7.5L9.6 5l1.9 2.4H4" /></>,
  // improve = trending up
  trending: <><path d="M2.5 10.5l3.5-3.5 2.5 2.5 4.5-5" /><path d="M11 4h2.5v2.5" /></>,
  // debug = a bug
  bug: <><rect x="5.5" y="5.5" width="5" height="6.5" rx="2.5" /><path d="M8 5.5V3.4" />
    <path d="M6.4 3.6 5.4 2.6M9.6 3.6l1-1" /><path d="M5.5 7.6H3.2M10.5 7.6h2.3M5.4 10H3.2M10.6 10h2.2" /></>,
  // merge = confluence: two parents converge into one child (mirrors a DAG merge node; clearly the
  // visual opposite of fork's divergence, so the two never read alike)
  confluence: <><circle cx="3.8" cy="3.6" r="1.5" /><circle cx="12.2" cy="3.6" r="1.5" /><circle cx="8" cy="12.4" r="1.5" />
    <path d="M4.4 5q.6 4 3.6 5.4M11.6 5q-.6 4-3.6 5.4" /></>,
  // fork = git-branch (a branch splitting off the trunk)
  gitbranch: <><circle cx="5" cy="3.6" r="1.5" /><circle cx="5" cy="12.4" r="1.5" /><circle cx="11.5" cy="3.6" r="1.5" />
    <path d="M5 5.1v5.8" /><path d="M11.5 5.1c0 3.2-3 3.6-5 4.7" /></>,
  // refine_block = target / crosshair (zoom into one parameter)
  target: <><circle cx="8" cy="8" r="5.4" /><circle cx="8" cy="8" r="2.3" /><circle cx="8" cy="8" r="0.5" fill="currentColor" stroke="none" /></>,
  // generic fallback
  dot: <circle cx="8" cy="8" r="2.2" fill="currentColor" stroke="none" />,

  // ---- feed-kind + chat + transport glyphs (round-7 dock redesign) ----
  // research = magnifier
  search: <><circle cx="7" cy="7" r="4" /><path d="M10.1 10.1l3.4 3.4" /></>,
  // report = document with text lines + folded corner
  doc: <><path d="M4.4 2.2h4.9L12 4.9v8.9H4.4Z" /><path d="M9.3 2.2v2.7H12" /><path d="M6.2 8.3h3.6M6.2 10.5h3.6" /></>,
  // trust / safety = warning triangle + bang
  alert: <><path d="M8 2.6 14 13.4H2Z" /><path d="M8 6.4v3.3" /><circle cx="8" cy="11.5" r="0.6" fill="currentColor" stroke="none" /></>,
  // control / actions = gear
  gear: <><circle cx="8" cy="8" r="2.4" /><path d="M8 2.3v1.6M8 12.1v1.6M2.3 8h1.6M12.1 8h1.6M4 4l1.1 1.1M10.9 10.9l1.1 1.1M12 4l-1.1 1.1M5.1 10.9 4 12" /></>,
  // chat user = person
  user: <><circle cx="8" cy="5.3" r="2.5" /><path d="M3.6 13.4a4.4 4.4 0 0 1 8.8 0" /></>,
  // chat assistant = bot head (antenna + two eyes)
  bot: <><rect x="3.6" y="5.6" width="8.8" height="6.8" rx="2" /><path d="M8 5.6V3.4" /><circle cx="8" cy="3" r="0.6" fill="currentColor" stroke="none" /><circle cx="6.2" cy="9" r="0.7" fill="currentColor" stroke="none" /><circle cx="9.8" cy="9" r="0.7" fill="currentColor" stroke="none" /></>,
  // applied action = lightning bolt
  bolt: <path d="M8.6 2 4.4 8.7H7l-.7 5.3L11.6 7H8.4Z" />,
  // agent-highlighted = star
  star: <path d="M8 2.4l1.7 3.5 3.8.6-2.8 2.7.7 3.8L8 11.4l-3.4 1.8.7-3.8-2.8-2.7 3.8-.6Z" />,
  // transport
  pause: <><path d="M6 3.5v9" /><path d="M10 3.5v9" /></>,
  play: <path d="M5.2 3.2 12.5 8l-7.3 4.8Z" />,
  stop: <rect x="4.3" y="4.3" width="7.4" height="7.4" rx="1" />,
  replay: <><path d="M12.7 8a4.7 4.7 0 1 0-1.4 3.35" /><path d="M12.9 3.4v2.7h-2.7" /></>,
  // controls toggle = sliders
  sliders: <><path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11" /><circle cx="6" cy="4.5" r="1.5" fill="currentColor" stroke="none" /><circle cx="10.5" cy="8" r="1.5" fill="currentColor" stroke="none" /><circle cx="4.6" cy="11.5" r="1.5" fill="currentColor" stroke="none" /></>,
  // collapse chevrons
  'chevron-up': <path d="M3.5 9.5 8 5l4.5 4.5" />,
  'chevron-down': <path d="M3.5 6.5 8 11l4.5-4.5" />,
  // header = speech bubble
  chat: <path d="M3 3.8h10v6.4H6.5L4 12.4V10.2H3Z" />,

  // ---- organizational glyphs (replace the color emoji so every theme stays monochrome) ----
  // project = folder with a tab
  folder: <path d="M2.4 4.4h3.3l1.3 1.6h6.6v6.6H2.4Z" />,
  // paperclip — attach files
  clip: <path d="M11.7 5.3 6.1 10.9a1.8 1.8 0 0 1-2.6-2.6l5.9-5.9a3 3 0 0 1 4.2 4.2l-5.9 5.9a4.2 4.2 0 0 1-5.9-5.9l5.2-5.2" />,
  // super-task / cross-run = target (reuses the crosshair metaphor) — alias of `target`
  // map = location pin
  map: <><path d="M8 14s4.1-4 4.1-7A4.1 4.1 0 0 0 8 2.9 4.1 4.1 0 0 0 3.9 7c0 3 4.1 7 4.1 7Z" /><circle cx="8" cy="6.9" r="1.5" /></>,
  // strategy = compass
  compass: <><circle cx="8" cy="8" r="5.6" /><path d="M10.5 5.5 8.7 8.7 5.5 10.5 7.3 7.3Z" /></>,
  // hint = lightbulb
  bulb: <><path d="M5.7 9.5a3.4 3.4 0 1 1 4.6 0c-.5.5-.8 1-.8 1.6H6.5c0-.6-.3-1.1-.8-1.6Z" /><path d="M6.5 12.5h3M7 13.8h2" /></>,
  // status: ok / failed (color via the semantic vars at the call site)
  check: <path d="M3.5 8.4 6.4 11.3 12.5 4.7" />,
  cross: <path d="M4.6 4.6l6.8 6.8M11.4 4.6l-6.8 6.8" />,
  // ---- replacing legacy colour emoji with monochrome glyphs ----
  // rename / annotate = pencil (was ✎)
  pencil: <><path d="M11.2 2.8 13.2 4.8 5.6 12.4 3 13l0.6-2.6Z" /><path d="M10 4l2 2" /></>,
  // share link = two chain links (was 🔗)
  link: <><path d="M6.4 9.6 9.6 6.4" /><path d="M8.4 4.6 9.8 3.2a2.4 2.4 0 0 1 3.4 3.4l-1.4 1.4" /><path d="M7.6 11.4 6.2 12.8a2.4 2.4 0 0 1-3.4-3.4l1.4-1.4" /></>,
  // download / export (was ⬇)
  download: <><path d="M8 2.6v7.2" /><path d="M5 7l3 3 3-3" /><path d="M3.4 13.2h9.2" /></>,
  // print (was 🖨)
  printer: <><path d="M5 6.2V2.8h6v3.4" /><rect x="3" y="6.2" width="10" height="5" rx="1" /><path d="M5 10.4h6v2.8H5Z" /></>,
  // champion = crown (was ♚ / ★ on the champion)
  crown: <><path d="M2.6 5.4 4.8 8l3.2-4 3.2 4 2.2-2.6-1 6.2H3.6Z" /></>,
  // list view (was ☰)
  list: <><path d="M3 4.5h10M3 8h10M3 11.5h10" /></>,
}

export function OpIcon({ name, size = 14, className }) {
  const glyph = GLYPHS[name] || GLYPHS.dot
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 16 16" fill="none"
         stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {glyph}
    </svg>
  )
}
