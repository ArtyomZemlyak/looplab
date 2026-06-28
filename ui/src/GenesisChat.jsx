import React, { useEffect, useRef, useState } from 'react'
import { genesis, startRun } from './util.js'
import StartRun from './StartRun.jsx'

const slug = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '').slice(0, 40)
// Lenient form for the live name input: keeps a TRAILING hyphen so a kebab name can be typed
// left-to-right ("my-" → "my-run"); the strict slug() is applied once at launch.
const slugLoose = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+/, '').slice(0, 40)

// Draft persistence: the planning chat lives pre-run (no run-id yet), so it can't go in a run's
// chat.jsonl until launch. To stop an accidental reload (F5) or a close-and-reopen from losing the
// in-progress planning, mirror {msgs, spec} into sessionStorage — scoped to this tab, restored on the
// next mount, and cleared once the run launches (the conversation then lives in the run's chat.jsonl).
const DRAFT_KEY = 'll.genesis.draft'
const readDraft = () => { try { return JSON.parse(sessionStorage.getItem(DRAFT_KEY) || 'null') } catch { return null } }
const clearDraft = () => { try { sessionStorage.removeItem(DRAFT_KEY) } catch { /* private mode */ } }

// A few one-tap goals to seed the empty chat — the boss turns any of these into a full spec.
const SEEDS = [
  'Run nomad2018 on minimax/minimax-m3, 100 nodes',
  'Spooky author identification with deepseek, 50 nodes',
  'A quick toy quadratic run to smoke-test',
]

// Chat-first run creation: describe a goal, the BOSS proposes a run name + task + key settings as an
// editable card; tweak and launch (or drop to the manual form). The graph/run only exists AFTER launch,
// so this is a pre-run surface — no run-id yet, the boss invents one.
export default function GenesisChat({ onClose, onStarted, seed }) {
  // A fresh goal typed in the global chat bar (a non-empty `seed`) starts clean; otherwise restore any
  // saved draft so reopening "New run" (or recovering after a reload) brings the planning chat back.
  const fresh = !!(seed && seed.trim())
  const [msgs, setMsgs] = useState(() => fresh ? [] : (readDraft()?.msgs || []))   // {role:'user'|'assistant', content}
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [spec, setSpec] = useState(() => fresh ? null : (readDraft()?.spec || null))  // {run_id, task, task_file, settings, rationale}
  const [err, setErr] = useState(null)
  const [advanced, setAdvanced] = useState(false)
  const feedRef = useRef(null)

  useEffect(() => {
    const f = feedRef.current
    if (f) requestAnimationFrame(() => { f.scrollTop = f.scrollHeight })
  }, [msgs, busy])

  // Mirror the in-progress draft to sessionStorage on every change (cleared when nothing's drafted).
  useEffect(() => {
    try {
      if (msgs.length || spec) sessionStorage.setItem(DRAFT_KEY, JSON.stringify({ msgs, spec }))
      else sessionStorage.removeItem(DRAFT_KEY)
    } catch { /* private mode / quota — best-effort */ }
  }, [msgs, spec])

  const ask = async (text) => {
    const goal = (text ?? input).trim()
    if (!goal || busy) return
    setErr(null); setInput('')
    const next = [...msgs, { role: 'user', content: goal }]
    setMsgs(next); setBusy(true)
    try {
      const r = await genesis({ messages: next, instruction: goal, draft: spec })
      setMsgs(m => [...m, { role: 'assistant', content: r.reply || '(planned — see the card)' }])
      // Only adopt a REAL spec — the offline soft-fail returns ok:false with an all-blank spec, which
      // must NOT wipe a good draft the user already tuned.
      if (r.ok !== false && r.spec && (r.spec.run_id || r.spec.task_file || r.spec.task?.kind)) setSpec(r.spec)
      if (r.ok === false && r.error) setErr(r.error)
    } catch (e) {
      setMsgs(m => [...m, { role: 'assistant', content: 'Could not reach the planner — use the manual form.' }])
      setErr(e.message)
    } finally { setBusy(false) }
  }

  // Seeded from the main-menu global chat bar: auto-send the typed message once on mount so the boss
  // starts planning immediately (the bar and the New-run button converge on this same flow).
  const seededRef = useRef(false)
  useEffect(() => {
    if (seed && seed.trim() && !seededRef.current) { seededRef.current = true; ask(seed.trim()) }
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  const setSetting = (k, v) => setSpec(s => ({ ...s, settings: { ...(s?.settings || {}), [k]: v } }))
  const setTaskField = (k, v) => setSpec(s => ({ ...s, task: { ...(s?.task || {}), [k]: v } }))

  // launchable when there's a name + a task; an mlebench_real task also needs a competition (else the
  // engine would crash on an empty slug — the backend now 400s too, but gate it here for a clean UX).
  const taskReady = spec?.task_file || (spec?.task?.kind &&
    (spec.task.kind !== 'mlebench_real' || spec.task.competition?.trim()))
  const ready = spec && spec.run_id?.trim() && taskReady
  const launch = async () => {
    if (!ready) { setErr('describe a goal first so the boss can pick a task (and a competition for MLE-bench)'); return }
    const rid = slug(spec.run_id)
    if (!rid) { setErr('give the run a name'); return }
    setBusy(true); setErr(null)
    try {
      const body = { run_id: rid, settings: spec.settings || {} }
      if (spec.task_file) body.task_file = spec.task_file; else body.task = spec.task
      // Carry the planning conversation into the new run so it opens with its own creation story
      // (the boss's chat becomes the first turns of the run's saved chat) instead of being lost.
      if (msgs.length) body.chat = msgs.map(m => ({ role: m.role, content: m.content }))
      await startRun(body)
      clearDraft()                 // the conversation now lives in the run's chat.jsonl
      onStarted?.(rid)
    } catch (e) {
      setErr(/409/.test(e.message) ? `run "${spec.run_id}" already exists — rename it` : 'launch failed: ' + e.message)
      setBusy(false)
    }
  }

  if (advanced) return <StartRun onClose={() => setAdvanced(false)} onStarted={onStarted} />

  const sk = spec?.settings || {}
  const taskLabel = spec?.task_file
    ? spec.task_file.split(/[\\/]/).pop() + ' · catalogue'
    : (spec?.task?.kind || '—')

  return <div className="overlay" onMouseDown={onClose}>
    <div className="panel gen-panel" onMouseDown={e => e.stopPropagation()}>
      <div className="panel-h"><span className="ttl">New run</span>
        <span className="pill">describe it — the boss plans the rest</span><span className="right" />
        <button className="btn sm ghost" onClick={() => setAdvanced(true)} title="precise task file + full settings form">manual form</button>
        <button className="btn sm ghost" onClick={onClose}>✕</button></div>

      <div className="panel-b gen-wrap">
        {/* chat column */}
        <div className="gen-chat">
          <div className="gen-feed" ref={feedRef}>
            {msgs.length === 0 && <div className="gen-empty">
              <div className="muted" style={{ marginBottom: 8 }}>Tell me what to run — I’ll name it, pick the task, and set the knobs.</div>
              {SEEDS.map(s => <button key={s} className="gen-seed" onClick={() => ask(s)}>{s}</button>)}
            </div>}
            {msgs.map((m, i) => <div key={i} className={'feed-msg chat ' + m.role}>
              <div className="fm-body">
                <div className="chat-who">{m.role === 'user' ? 'you' : 'boss'}</div>
                <div className="chat-bubble"><div className="chat-text">{m.content}</div></div>
              </div>
            </div>)}
            {busy && <div className="feed-msg chat assistant"><div className="fm-body">
              <div className="chat-who">boss</div>
              <div className="chat-bubble"><div className="chat-text muted">… planning</div></div>
            </div></div>}
          </div>
          <div className="chat-in">
            <textarea className="text" placeholder="e.g. run titanic baseline on deepseek, 30 nodes…"
              value={input} onChange={e => setInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask() } }} />
            <div className="toolbar" style={{ marginTop: 6 }}>
              <span className="muted" style={{ flex: 1, fontSize: 11 }}>Enter to send · Shift+Enter for a newline</span>
              <button className="btn sm" disabled={!input.trim() || busy} onClick={() => ask()}>{busy ? '…' : 'Send'}</button>
            </div>
          </div>
        </div>

        {/* editable spec card */}
        <div className="gen-card">
          <div className="gen-card-h"><b>Proposed run</b>{spec?.rationale && <span className="muted" title={spec.rationale}>· why</span>}</div>
          {!spec && <div className="gen-card-empty muted">The plan shows up here once you describe a goal. You can edit every field before launching.</div>}
          {spec && <>
            <div className="gen-field"><div className="gen-lab">Run name</div>
              <input className="text" value={spec.run_id || ''} onChange={e => setSpec(s => ({ ...s, run_id: slugLoose(e.target.value) }))} placeholder="run-name" /></div>
            <div className="gen-field"><div className="gen-lab">Task</div>
              {spec.task?.kind === 'mlebench_real' && !spec.task_file
                ? <input className="text" value={spec.task.competition || ''} onChange={e => setTaskField('competition', e.target.value)} placeholder="kaggle-competition-id" />
                : <div className="gen-ro">{taskLabel}</div>}
              <div className="gen-help">{spec.task?.kind === 'mlebench_real' ? 'MLE-bench / Kaggle competition' : (spec.task_file ? 'from the task catalogue' : (spec.task?.kind || ''))}</div>
            </div>
            <div className="gen-grid">
              <div className="gen-field"><div className="gen-lab">Model</div>
                <input className="text" value={sk.llm_model || ''} onChange={e => setSetting('llm_model', e.target.value || undefined)} placeholder="default" /></div>
              <div className="gen-field"><div className="gen-lab">Max nodes</div>
                <input className="text" type="number" value={sk.max_nodes ?? ''} onChange={e => setSetting('max_nodes', e.target.value === '' ? undefined : Number(e.target.value))} placeholder="default" /></div>
              <div className="gen-field"><div className="gen-lab">Seeds</div>
                <input className="text" type="number" value={sk.n_seeds ?? ''} onChange={e => setSetting('n_seeds', e.target.value === '' ? undefined : Number(e.target.value))} placeholder="default" /></div>
              <div className="gen-field"><div className="gen-lab">Policy</div>
                <select className="text" value={sk.policy || ''} onChange={e => setSetting('policy', e.target.value || undefined)}>
                  <option value="">default</option>
                  {['greedy', 'evolutionary', 'mcts', 'asha'].map(p => <option key={p} value={p}>{p}</option>)}
                </select></div>
            </div>
          </>}
          {err && <div className="notice" style={{ borderColor: 'var(--fail)', color: '#ffd3d3', marginTop: 8 }}>{err}</div>}
          <div className="modal-actions" style={{ marginTop: 12 }}>
            <button className="btn sm ghost" onClick={onClose}>Cancel</button>
            <button className="btn sm primary" disabled={!ready || busy} onClick={launch}>{busy ? '… starting' : '▶ Start run'}</button>
          </div>
        </div>
      </div>
    </div>
  </div>
}
