import React from 'react'

// Shared chrome for the "group" visual language used by BOTH the in-run canvas (Dag.jsx) and the
// cross-run map (MapView.jsx), so the hull region and the collapsed super-node card are defined
// once. Bodies differ legitimately (experiment aggregate vs project rollup) and are passed in.

// Soft enclosing hull behind a group; `tab` is the (interactive) label the caller wires.
// Used by the cross-run Map (MapView.jsx). The in-run canvas (Dag.jsx) uses the slimmer LaneHeader.
export function RegionShell({ w, h, path, tint, tab }) {
  return (
    <div className="grp-region" style={{ width: w, height: h, '--grp-tint': tint }}>
      <svg width={w} height={h}><path d={path} className="grp-hull" /></svg>
      {tab}
    </div>
  )
}

// Slim labeled lane header drawn at the TOP edge of an EXPANDED group's cluster (round-6) — replaces
// the old full-canvas hull, which drew a giant translucent box across the screen. Group identity now
// reads from this bar + a faint per-node tint instead of an enclosing region. `w` spans the cluster;
// the whole bar is clickable to collapse the group.
export function LaneHeader({ w, label, count, tint, onToggle }) {
  return (
    <div className="grp-lane" style={{ width: w, '--grp-tint': tint }}
         onClick={(e) => { e.stopPropagation(); onToggle && onToggle(label) }} title="collapse group">
      <span className="grp-chev">▾</span>
      <span className="grp-lane-label">{label}</span>
      <span className="spacer" style={{ flex: 1 }} />
      <span className="grp-n">{count}</span>
    </div>
  )
}

// Collapsed-group card shell. Caller supplies the body (children) and the click/selected state.
export function SuperShell({ tint, selected, onClick, title, children }) {
  return (
    <div className={'grp-super' + (selected ? ' sel' : '')} style={{ '--grp-tint': tint }}
         onClick={onClick} title={title}>
      {children}
    </div>
  )
}
