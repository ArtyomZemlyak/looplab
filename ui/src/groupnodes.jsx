import React from 'react'

// Shared chrome for the "group" visual language used by BOTH the in-run canvas (Dag.jsx) and the
// cross-run map (MapView.jsx), so the hull region and the collapsed super-node card are defined
// once. Bodies differ legitimately (experiment aggregate vs project rollup) and are passed in.

// Soft enclosing hull behind a group; `tab` is the (interactive) label the caller wires. Used by the
// cross-run Map (MapView.jsx); the in-run canvas (Dag.jsx) uses the slimmer GroupRegion below.
export function RegionShell({ w, h, path, tint, tab }) {
  return (
    <div className="grp-region" style={{ width: w, height: h, '--grp-tint': tint }}>
      <svg width={w} height={h}><path d={path} className="grp-hull" /></svg>
      {tab}
    </div>
  )
}

// Group region for an EXPANDED cluster (round-8): a WHISPER-faint rounded band behind the members
// (the "objединяющий бокс" without the heavy old hull) + a compact label PILL hugging its text at the
// top-left (replaces the full-width bar that stretched across the cluster). Group identity reads from
// the members' shared muted fill; the band + pill just frame and name it. The band is click-through
// (pointer-events:none) so nodes on top stay clickable; only the pill collapses the group.
export function GroupRegion({ w, h, label, count, tint, onToggle }) {
  return (
    <div className="grp-band" style={{ width: w, height: h, '--grp-tint': tint }}>
      <button type="button" className="grp-pill"
        onClick={(e) => { e.stopPropagation(); onToggle && onToggle(label) }}
        aria-label={`Collapse group ${label}`} title="collapse group">
        <span className="grp-chev">▾</span>
        <span className="grp-pill-label">{label}</span>
        <span className="grp-n">{count}</span>
      </button>
    </div>
  )
}

// Collapsed-group card shell. Caller supplies the body (children) and the click/selected state.
export function SuperShell({ tint, selected, onClick, title, children }) {
  const onKeyDown = onClick ? (event) => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onClick() }
  } : undefined
  return (
    <div className={'grp-super' + (selected ? ' sel' : '')} style={{ '--grp-tint': tint }}
         onClick={onClick} onKeyDown={onKeyDown} role={onClick ? 'button' : undefined}
         tabIndex={onClick ? 0 : undefined} title={title}>
      {children}
    </div>
  )
}
