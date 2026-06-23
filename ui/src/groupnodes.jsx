import React from 'react'

// Shared chrome for the "group" visual language used by BOTH the in-run canvas (Dag.jsx) and the
// cross-run map (MapView.jsx), so the hull region and the collapsed super-node card are defined
// once. Bodies differ legitimately (experiment aggregate vs project rollup) and are passed in.

// Soft enclosing hull behind a group; `tab` is the (interactive) label the caller wires.
export function RegionShell({ w, h, path, tint, tab }) {
  return (
    <div className="grp-region" style={{ width: w, height: h, '--grp-tint': tint }}>
      <svg width={w} height={h}><path d={path} className="grp-hull" /></svg>
      {tab}
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
