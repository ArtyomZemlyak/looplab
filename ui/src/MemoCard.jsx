import React, { useMemo, useState } from 'react'
import Markdown from './markdown.jsx'
import { OpIcon } from './icons.jsx'
import { safeExternalHref } from './urlSafety.js'
import { normalizeResearchMemo } from './researchMemoModel.js'

function MemoVerification({ verification }) {
  if (!verification?.verdicts?.length) return null
  return <>
    <div className="section-h memo-verification-title">Verification
      {verification.unsupported > 0 && <span className="chip warn"
        title="claims whose cited evidence does not support them">
        {verification.unsupported} unsupported
      </span>}
      <span className="muted"> ({verification.method})</span>
    </div>
    <ul className="bul memo-verification-list">{verification.verdicts.map((item, index) => (
      <li key={index} className={item.verdict === 'supported' ? 'ok'
        : (item.verdict === 'unclear' || item.verdict === 'cited') ? '' : 'bad'}>
        <span className="pill">{item.verdict}</span> {item.statement || '(statement unavailable)'}
        {item.note && <span className="muted"> — {item.note}</span>}
      </li>
    ))}</ul>
  </>
}

// Report-local research memo. This must not live in panels.jsx: importing the report alone should
// never pull the optional panel hub and its owner-only dependencies into the report closure.
export default function MemoCard({ memo, idx, open, onToggle }) {
  const [think, setThink] = useState(false)
  const value = useMemo(() => normalizeResearchMemo(memo), [memo])
  const memoIndex = Number.isSafeInteger(idx) && idx >= 0 ? idx : 0
  return <div className="memo-card">
    <button type="button" className="memo-head disclosure-button" aria-expanded={open}
      onClick={() => onToggle?.(memoIndex)}>
      <span className="span-tw">{open ? '▾' : '▸'}</span>
      <b><OpIcon name="search" className="t-ic" /> memo #{memoIndex + 1}</b>
      {value.trigger && <span className="pill">{value.trigger}</span>}
      {value.at_node != null && <span className="muted"> @{value.at_node} nodes</span>}
      <span className="spacer" style={{ flex: 1 }} />
      <span className="muted">{value.sources.length} source{value.sources.length === 1 ? '' : 's'}</span>
    </button>
    {open && <div className="memo-body">
      <div className="section-h">Conclusion</div>
      <div className="v">{value.summary || '—'}</div>
      {value.findings.length > 0 && <>
        <div className="section-h">Findings</div>
        <ul className="bul">{value.findings.map((finding, index) => <li key={index}>{finding}</li>)}</ul>
      </>}
      {/* # CODEX AGENT: Render only the normalized verifier projection so unsupported claims remain
          visible in the report without trusting provider-supplied aggregate counts. */}
      <MemoVerification verification={value.verification} />
      {value.recommended_directions.length > 0 && <>
        <div className="section-h">Recommended directions (fed to the Researcher)</div>
        <ul className="bul">{value.recommended_directions.map((direction, index) => <li key={index}>{direction}</li>)}</ul>
      </>}
      {value.sources.length > 0 && <>
        <div className="section-h">Sources consulted</div>
        <ul className="bul">{value.sources.map((source, index) => {
          // Research/provider output is untrusted. Only credential-free HTTP(S) URLs become links;
          // unsafe, oversized or malformed values remain bounded inert text.
          const href = safeExternalHref(source.url)
          const label = (source.title || source.url || '—').slice(0, 300)
          const snippet = source.snippet.slice(0, 120)
          return <li key={index}>
            {href ? <a href={href} target="_blank" rel="noreferrer noopener">{label}</a> : label}
            {snippet && <span className="muted"> — {snippet}</span>}
          </li>
        })}</ul>
      </>}
      {value.reasoning && <div className="think-debug" style={{ marginTop: 8 }}>
        <button type="button" className="role-think disclosure-button" aria-expanded={think}
          onClick={() => setThink(value => !value)}
          style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.5px' }}>
          {think ? '▾' : '▸'} reasoning (debug)
        </button>
        {think && <Markdown className="think-body" text={value.reasoning} />}
      </div>}
    </div>}
  </div>
}
