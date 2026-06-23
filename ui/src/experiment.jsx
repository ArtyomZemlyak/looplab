import React, { useEffect, useRef, useState } from 'react'
import { fmt, CONTROL, chat, suggestIdea, resumeRun } from './util.js'

// A lightweight modal shell (matches the panel overlay styling) for the experiment dialogs.
function Modal({ title, sub, onClose, children, width = 560 }) {
  return (
    <div className="overlay" onClick={onClose}>
      <div className="panel" style={{ width: `min(${width}px, 95%)` }} onClick={e => e.stopPropagation()}>
        <div className="panel-h"><span className="ttl">{title}</span>{sub && <span className="pill">{sub}</span>}<span className="right" /><button className="btn sm ghost" onClick={onClose}>✕</button></div>
        <div className="panel-b">{children}</div>
      </div>
    </div>
  )
}

// Parse a "k=v, k2=v2" or JSON params string into a {k: number} object. Tolerant: accepts both.
function parseParams(s) {
  const t = (s || '').trim()
  if (!t) return {}
  try { if (t.startsWith('{')) return JSON.parse(t) } catch { /* fall through */ }
  const out = {}
  t.split(/[,\n]/).forEach(pair => {
    const m = pair.split(/[=:]/)
    if (m.length >= 2) { const k = m[0].trim(); const v = Number(m[1].trim()); if (k && !Number.isNaN(v)) out[k] = v }
  })
  return out
}

// Hand-add an experiment to the live search tree. The operator authors an idea (operator, params,
// rationale, optional theme), optionally branches from a parent node, and optionally ships ready-made
// solution.py code; otherwise the Developer implements the idea. Posts an `inject_node` control event.
export function InjectModal({ runId, state, initial, onClose, onToast }) {
  const nodes = Object.values(state.nodes || {}).sort((a, b) => a.id - b.id)
  const init = initial || {}
  const initIdea = init.idea || {}
  const [operator, setOperator] = useState(initIdea.operator || 'manual')
  const [parent, setParent] = useState(init.parent_id ?? '')
  const [params, setParams] = useState(initIdea.params ? JSON.stringify(initIdea.params) : '')
  const [rationale, setRationale] = useState(initIdea.rationale || '')
  const [theme, setTheme] = useState(initIdea.theme || '')
  const [code, setCode] = useState(init.code || '')
  const [busy, setBusy] = useState(false)
  const finished = state.finished
  const submit = async () => {
    setBusy(true)
    try {
      // Finished run? Reopen it FIRST (clears the terminal flag), then inject, then re-enter the
      // loop so the engine actually evaluates the new node and re-finishes.
      if (finished) await CONTROL.reopen(runId)
      await CONTROL.inject(runId, {
        idea: { operator, params: parseParams(params), rationale, theme: theme || null },
        parent_id: parent === '' ? null : Number(parent),
        code: code.trim() ? code : null,
      })
      if (finished) { await resumeRun(runId); onToast('experiment added — continuing the run') }
      else onToast('experiment queued — the engine will evaluate it next')
      onClose()
    } catch (e) { onToast('inject failed: ' + e.message); setBusy(false) }
  }
  return (
    <Modal title="Add experiment to the tree" sub="manual node" onClose={onClose} width={640}>
      {finished && <div className="notice" style={{ marginBottom: 10 }}>This run has finished — adding an experiment will <b>reopen and continue</b> it: the engine re-enters, evaluates your node, then re-finishes.</div>}
      <div className="sf-field"><div className="sf-label">operator</div>
        <select className="text" value={operator} onChange={e => setOperator(e.target.value)}>
          {['manual', 'improve', 'draft', 'debug'].map(o => <option key={o} value={o}>{o}</option>)}
        </select></div>
      <div className="sf-field"><div className="sf-label">branch from</div>
        <select className="text" value={parent} onChange={e => setParent(e.target.value)}>
          <option value="">— no parent (root) —</option>
          {nodes.map(n => <option key={n.id} value={n.id}>#{n.id} · {n.operator} · {fmt(n.confirmed_mean ?? n.metric)}</option>)}
        </select></div>
      <div className="sf-field"><div className="sf-label">params</div>
        <input className="text" placeholder='degree=3, lam=0.01  (or JSON)' value={params} onChange={e => setParams(e.target.value)} /></div>
      <div className="sf-field"><div className="sf-label">theme</div>
        <input className="text" placeholder="optional group, e.g. regularization" value={theme} onChange={e => setTheme(e.target.value)} /></div>
      <div className="sf-field"><div className="sf-label">rationale</div>
        <input className="text" placeholder="why try this?" value={rationale} onChange={e => setRationale(e.target.value)} /></div>
      <div className="sf-field"><div className="sf-label">code <span className="muted">(optional — blank = let the Developer implement the idea)</span></div>
        <textarea className="text" style={{ minHeight: 120 }} placeholder="# solution.py — paste ready-made code, or leave blank" value={code} onChange={e => setCode(e.target.value)} /></div>
      <div className="toolbar" style={{ marginTop: 12 }}>
        <button className="btn primary" disabled={busy} onClick={submit}>{busy ? 'queuing…' : '✚ Add experiment'}</button>
        <button className="btn ghost" onClick={onClose}>Cancel</button>
      </div>
    </Modal>
  )
}

// Per-experiment chat: a grounded conversation about a run (and the focused node) with the same LLM
// the engine uses. Read-only/advisory; "Propose experiment" turns the discussion into a concrete
// idea (via /suggest) and opens the inject dialog pre-filled — closing the loop from idea to node.
export function ChatTab({ runId, nodeId, state, onInject, onToast }) {
  const [msgs, setMsgs] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const endRef = useRef(null)
  // Reset the thread when the focused node changes (the grounding context changes with it).
  useEffect(() => { setMsgs([]) }, [nodeId])
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [msgs, busy])
  const send = async () => {
    const text = input.trim()
    if (!text || busy) return
    const next = [...msgs, { role: 'user', content: text }]
    setMsgs(next); setInput(''); setBusy(true)
    try {
      const r = await chat(runId, next, nodeId)
      setMsgs([...next, { role: 'assistant', content: r.ok ? r.text : `⚠ ${r.error || 'no model reachable'}` }])
    } catch (e) { setMsgs([...next, { role: 'assistant', content: '⚠ ' + e.message }]) }
    setBusy(false)
  }
  const propose = async () => {
    setBusy(true)
    try {
      const r = await suggestIdea(runId, { node_id: nodeId, messages: msgs })
      if (r.ok && r.idea) onInject({ idea: r.idea, parent_id: nodeId ?? null })
      else onToast('suggest failed: ' + (r.error || 'no idea'))
    } catch (e) { onToast('suggest failed: ' + e.message) }
    setBusy(false)
  }
  return (
    <div className="chat">
      <div className="chat-log">
        {!msgs.length && <div className="muted" style={{ padding: 8 }}>
          Ask about {nodeId != null ? `experiment #${nodeId}` : 'this run'} — interpret the metric, debate what to try
          next, or ask for a concrete next experiment. Grounded on the run's goal{nodeId != null ? ', code, and result' : ' and best-so-far'}.
        </div>}
        {msgs.map((m, i) => <div key={i} className={'chat-msg ' + m.role}>
          <div className="chat-role">{m.role === 'user' ? 'you' : 'researcher'}</div>
          <div className="chat-text">{m.content}</div>
        </div>)}
        {busy && <div className="muted" style={{ padding: 8 }}>…thinking</div>}
        <div ref={endRef} />
      </div>
      <div className="chat-in">
        <textarea className="text" rows={2} placeholder="message… (Enter to send, Shift+Enter for newline)"
          value={input} onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }} />
        <div className="toolbar" style={{ marginTop: 6 }}>
          <button className="btn sm primary" disabled={busy || !input.trim()} onClick={send}>Send</button>
          <button className="btn sm" disabled={busy || state.finished} title="turn this discussion into a concrete experiment node" onClick={propose}>↪ Propose experiment</button>
        </div>
      </div>
    </div>
  )
}
