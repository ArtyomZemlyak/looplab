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
