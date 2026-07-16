import React, { useState } from 'react'
import Markdown from './markdown.jsx'
import { OpIcon } from './icons.jsx'
import { safeExternalHref } from './urlSafety.js'

// Report-local research memo. This must not live in panels.jsx: importing the report alone should
// never pull the optional panel hub and its owner-only dependencies into the report closure.
export default function MemoCard({ memo, idx, open, onToggle }) {
  const [think, setThink] = useState(false)
  return <div className="memo-card">
    <button type="button" className="memo-head disclosure-button" aria-expanded={open}
      onClick={() => onToggle(idx)}>
      <span className="span-tw">{open ? '▾' : '▸'}</span>
      <b><OpIcon name="search" className="t-ic" /> memo #{idx + 1}</b>
      {memo.trigger && <span className="pill">{memo.trigger}</span>}
      {memo.at_node != null && <span className="muted"> @{memo.at_node} nodes</span>}
      <span className="spacer" style={{ flex: 1 }} />
      <span className="muted">{(memo.sources || []).length} source{(memo.sources || []).length === 1 ? '' : 's'}</span>
    </button>
    {open && <div className="memo-body">
      <div className="section-h">Conclusion</div>
      <div className="v">{memo.summary || '—'}</div>
      {(memo.findings || []).length > 0 && <>
        <div className="section-h">Findings</div>
        <ul className="bul">{memo.findings.map((finding, index) => <li key={index}>{finding}</li>)}</ul>
      </>}
      {(memo.recommended_directions || []).length > 0 && <>
        <div className="section-h">Recommended directions (fed to the Researcher)</div>
        <ul className="bul">{memo.recommended_directions.map((direction, index) => <li key={index}>{direction}</li>)}</ul>
      </>}
      {(memo.sources || []).length > 0 && <>
        <div className="section-h">Sources consulted</div>
        <ul className="bul">{memo.sources.map((source, index) => {
          const href = safeExternalHref(source?.url)
          const label = String(source?.title ?? source?.url ?? '—').slice(0, 300)
          const snippet = source?.snippet == null ? '' : String(source.snippet).slice(0, 120)
          return <li key={index}>
            {href ? <a href={href} target="_blank" rel="noreferrer noopener">{label}</a> : label}
            {snippet && <span className="muted"> — {snippet}</span>}
          </li>
        })}</ul>
      </>}
      {memo.reasoning && <div className="think-debug" style={{ marginTop: 8 }}>
        <button type="button" className="role-think disclosure-button" aria-expanded={think}
          onClick={() => setThink(value => !value)}
          style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.5px' }}>
          {think ? '▾' : '▸'} reasoning (debug)
        </button>
        {think && <Markdown className="think-body" text={memo.reasoning} />}
      </div>}
    </div>}
  </div>
}
