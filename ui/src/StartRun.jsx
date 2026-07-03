import React, { useEffect, useMemo, useState } from 'react'
import { getSettings, listTasks, startRun, research } from './util.js'
import { toForm, fromForm } from './settingsSchema.js'
import SettingsForm from './SettingsForm.jsx'
import { OpIcon } from './icons.jsx'

// Compact subset shown by default in the launch dialog; "all settings" expands to the full form.
const QUICK_GROUPS = ['Search & policy', 'LLM', 'Agent loop & models', 'Budgets & confirmation']

const slug = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '').slice(0, 32)

// Modal to launch a new engine run from the UI: pick a task, name the run, optionally pre-research
// the topic, and override any engine setting — the full CLI `run` surface, in a dialog.
export default function StartRun({ onClose, onStarted }) {
  const [tasks, setTasks] = useState(null)
  const [taskPath, setTaskPath] = useState('')
  const [customPath, setCustomPath] = useState('')
  const [runId, setRunId] = useState('')
  const [form, setForm] = useState(null)
  const [showAll, setShowAll] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  // pre-research
  const [topic, setTopic] = useState('')
  const [researching, setResearching] = useState(false)
  const [brief, setBrief] = useState(null)
  const [saveNote, setSaveNote] = useState(true)

  useEffect(() => {
    listTasks().then(d => {
      setTasks(d.tasks || [])
      if (d.tasks?.length) setTaskPath(d.tasks[0].path)
    }).catch(() => setTasks([]))
    getSettings().then(d => setForm(toForm(d.settings))).catch(() => setForm(toForm({})))
  }, [])

  const selTask = useMemo(() => (tasks || []).find(t => t.path === taskPath), [tasks, taskPath])
  const effectivePath = taskPath === '__custom__' ? customPath.trim() : taskPath
  // Suggest a run id from the chosen task whenever the user hasn't typed one.
  const [touchedId, setTouchedId] = useState(false)
  useEffect(() => {
    if (touchedId) return
    const base = slug(selTask?.id || selTask?.name || (customPath && customPath.split(/[/\\]/).pop()) || 'run')
    setRunId(base ? `${base}-ui` : '')
  }, [selTask, customPath, touchedId])

  const onChange = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const doResearch = async () => {
    if (!topic.trim()) return
    setResearching(true); setBrief(null)
    try {
      const r = await research(topic.trim(), saveNote)
      setBrief(r.ok ? { text: r.text, saved: r.saved, model: r.model }
                    : { error: r.error || 'no model reachable', model: r.model, base: r.base_url })
    } catch (e) { setBrief({ error: e.message }) }
    finally { setResearching(false) }
  }

  const launch = async () => {
    setErr(null)
    if (!effectivePath) { setErr('pick or enter a task file'); return }
    if (!runId.trim()) { setErr('name the run'); return }
    setBusy(true)
    try {
      await startRun({ task_file: effectivePath, run_id: runId.trim(), settings: fromForm(form) })
      onStarted?.(runId.trim())
    } catch (e) {
      setErr(e.status === 409 ? `run "${runId}" already exists — pick another id` : 'launch failed: ' + e.message)
      setBusy(false)
    }
  }

  return <div className="overlay" onMouseDown={onClose}>
    <div className="panel" style={{ width: 'min(820px, 95%)' }} onMouseDown={e => e.stopPropagation()}>
      <div className="panel-h"><span className="ttl">New run</span>
        <span className="pill">launch an engine run</span><span className="right" />
        <button className="btn sm ghost" onClick={onClose}>✕</button></div>
      <div className="panel-b">
        {/* task + id */}
        <div className="sf-group">
          <div className="sf-group-h"><b>Task</b><span className="muted">what the engine optimizes</span></div>
          <div className="sf-grid">
            <div className="sf-field">
              <div className="sf-label">Task file</div>
              <div className="sf-input">
                <select className="text" value={taskPath} onChange={e => setTaskPath(e.target.value)}>
                  {tasks == null && <option>loading…</option>}
                  {(tasks || []).map(t => <option key={t.path} value={t.path}>{t.name} · {t.kind}</option>)}
                  <option value="__custom__">custom path…</option>
                </select>
              </div>
              {taskPath === '__custom__'
                ? <input className="text" style={{ marginTop: 6 }} placeholder="examples/regression_task.json"
                         value={customPath} onChange={e => setCustomPath(e.target.value)} />
                : selTask?.goal && <div className="sf-help">{selTask.goal}</div>}
            </div>
            <div className="sf-field">
              <div className="sf-label">Run id</div>
              <div className="sf-input"><input className="text" value={runId}
                onChange={e => { setRunId(e.target.value); setTouchedId(true) }} placeholder="my-run" /></div>
              <div className="sf-help">A new directory under the run-root. Must be unique.</div>
            </div>
          </div>
        </div>

        {/* pre-research */}
        <div className="sf-group">
          <div className="sf-group-h"><b>Pre-research</b><span className="muted">optional — prime the run with an LLM brief</span></div>
          <textarea className="text" style={{ minHeight: 60 }} placeholder="Describe the topic / dataset / what to explore…"
                    value={topic} onChange={e => setTopic(e.target.value)} />
          <div className="toolbar" style={{ marginTop: 6 }}>
            <button className="btn sm" disabled={!topic.trim() || researching} onClick={doResearch}>
              {researching ? '… researching' : <><OpIcon name="search" className="t-ic" /> Pre-research topic</>}</button>
            <label className="chk"><input type="checkbox" checked={saveNote} onChange={e => setSaveNote(e.target.checked)} /> save to knowledge dir</label>
          </div>
          {brief && (brief.error
            ? <div className="notice" style={{ marginTop: 8 }}>No brief: {brief.error}{brief.model ? ` (model ${brief.model}${brief.base ? ' @ ' + brief.base : ''})` : ''}. The endpoint may be offline — research is optional.</div>
            : <div className="brief">
                <div className="muted" style={{ marginBottom: 4 }}>brief from {brief.model}{brief.saved ? ` · saved → ${brief.saved}` : ' · not saved (no knowledge dir configured)'}</div>
                <pre className="code" style={{ maxHeight: 220 }}>{brief.text}</pre>
              </div>)}
        </div>

        {/* settings overrides */}
        <div className="sf-group-h" style={{ marginTop: 4 }}>
          <b>Settings</b><span className="muted">starts from your saved defaults — override per-run</span>
          <span className="spacer" style={{ flex: 1 }} />
          <button className="btn sm ghost" onClick={() => setShowAll(v => !v)}>{showAll ? 'quick' : 'all settings'}</button>
        </div>
        {form && <SettingsForm form={form} onChange={onChange} only={showAll ? null : QUICK_GROUPS} hideSecret />}

        {err && <div className="notice" style={{ borderColor: 'var(--fail)', color: '#ffd3d3', marginTop: 10 }}>{err}</div>}
        <div className="modal-actions" style={{ marginTop: 14 }}>
          <button className="btn sm ghost" onClick={onClose}>Cancel</button>
          <button className="btn sm primary" disabled={busy} onClick={launch}>{busy ? '… starting' : '▶ Start run'}</button>
        </div>
      </div>
    </div>
  </div>
}
