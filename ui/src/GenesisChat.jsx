import React, { useEffect, useRef, useState } from 'react'
import { genesis, genesisAwait, startRun } from './util.js'
import StartRun from './StartRun.jsx'

// 48 > the server's 40-char normalize cap, so a server-deduped name ("…-2", 42 chars) survives the
// launch-time re-slug instead of being chopped back to the colliding 40-char base (guaranteed 409).
const slug = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '').slice(0, 48)
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
  const [progress, setProgress] = useState(null)   // live scout step while the boss inspects the repo
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
    setMsgs(next); setBusy(true); setProgress(null)
    try {
      // The boss may scout the repo across several turns server-side; a slow model hands back a job we
      // poll (genesisAwait) so it can't 504. A fast model returns the plan inline — no extra latency.
      // onProgress surfaces each scout step ("reading README.md…") so a long plan isn't an opaque wait.
      const r = await genesisAwait(await genesis({ messages: next, instruction: goal, draft: spec }),
        { onProgress: p => setProgress(p) })
      // On failure/timeout (ok:false, no reply) don't claim a plan exists — the transcript would lie
      // (and later be carried into the launched run's chat.jsonl).
      setMsgs(m => [...m, { role: 'assistant', content: r.reply
        || (r.ok === false ? `(planning failed: ${r.error || 'timed out'})` : '(planned — see the card)') }])
      // Only adopt a REAL spec — the offline soft-fail returns ok:false with an all-blank spec, which
      // must NOT wipe a good draft the user already tuned.
      if (r.ok !== false && r.spec && (r.spec.run_id || r.spec.task_file || r.spec.task?.kind)) setSpec(r.spec)
      if (r.ok === false && r.error) setErr(r.error)
    } catch (e) {
      setMsgs(m => [...m, { role: 'assistant', content: 'Could not reach the planner — use the manual form.' }])
      setErr(e.message)
    } finally { setBusy(false); setProgress(null) }
  }

  // Seeded from the main-menu global chat bar: auto-send the typed message once on mount so the boss
  // starts planning immediately (the bar and the New-run button converge on this same flow).
  const seededRef = useRef(false)
  useEffect(() => {
    if (seed && seed.trim() && !seededRef.current) { seededRef.current = true; ask(seed.trim()) }
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  const setSetting = (k, v) => setSpec(s => ({ ...s, settings: { ...(s?.settings || {}), [k]: v } }))
  const setTaskField = (k, v) => setSpec(s => ({ ...s, task: { ...(s?.task || {}), [k]: v } }))
  // COMPOSABLE schema: the boss authors `repo`/`dataset`/`cmd` (no `kind`). The form reads either the
  // composable name or its legacy alias, and WRITES the composable one. `cmd` is the run+score spec
  // (an object {command, metric:{reader,key}, timeout}); the legacy alias is `eval`.
  const _cmdObj = (task => (task.cmd && typeof task.cmd === 'object' && !Array.isArray(task.cmd)) ? task.cmd
    : (Array.isArray(task.cmd) ? { command: task.cmd } : (task.eval || {})))(spec?.task || {})
  const setCmdField = (k, v) => setSpec(s => { const t = s?.task || {}; const cur = (t.cmd && typeof t.cmd === 'object' && !Array.isArray(t.cmd)) ? t.cmd : (Array.isArray(t.cmd) ? { command: t.cmd } : (t.eval || {})); const { eval: _e, ...rest } = t; return { ...s, task: { ...rest, cmd: { ...cur, [k]: v } } } })
  // Quote-aware tokenize so an argument containing spaces survives (e.g. --run-name "my model"): the
  // engine runs this argv with NO shell, so a plain whitespace split would tear quoted args apart.
  const setCmdCommand = (str) => setCmdField('command',
    (String(str).match(/"[^"]*"|'[^']*'|\S+/g) || []).map(t => t.replace(/^(['"])([\s\S]*)\1$/, '$2')))
  const setMetricKey = (v) => setCmdField('metric', { reader: 'stdout_json', ..._cmdObj.metric, key: v })
  const setRepoPath = (v) => setSpec(s => { const { editable_path: _e, ...rest } = (s?.task || {}); return { ...s, task: { ...rest, repo: v } } })
  // Data mounts textarea: one "name /abs/path" per line -> {name: path}. Writes the composable `dataset`.
  const setDataset = (str) => setSpec(s => { const { data: _d, ...rest } = (s?.task || {}); const mounts = {}; for (const ln of String(str).split('\n')) { const m = ln.trim().match(/^(\S+)\s+(.+)$/); if (m) mounts[m[1]] = m[2].trim() } return { ...s, task: { ...rest, dataset: mounts } } })

  const task = spec?.task || {}
  const repoPath = task.repo || task.editable_path || ''
  const cmdCommand = _cmdObj.command || []
  const dataMounts = task.dataset || task.data || {}
  const dataText = Object.entries(dataMounts).map(([n, p]) => `${n} ${p}`).join('\n')
  // Infer the task type from which fields are present (composable), falling back to an explicit kind.
  const inferredKind = task.kind || (repoPath ? 'repo'
    : ((task.dataset || task.data || task.data_path) ? 'dataset'
    : ((task.kaggle || task.competition) ? 'mlebench_real' : '')))
  const isRepo = inferredKind === 'repo'
  const isDataset = inferredKind === 'dataset'
  // launchable when there's a name + a task; a competition needs its slug, a repo task needs a `repo`
  // path + a way to score it (a cmd command, metric reader "auto", or onboard). The backend validates
  // too, but gate here for a clean UX (no doomed launch).
  const repoReady = !isRepo || (repoPath.trim() &&
    ((Array.isArray(cmdCommand) && cmdCommand.length) || task.onboard || _cmdObj.metric?.reader === 'auto'))
  const datasetReady = !isDataset || !!task.data_path?.trim()
  const taskReady = spec?.task_file || (inferredKind &&
    (inferredKind !== 'mlebench_real' || (task.kaggle || task.competition)?.trim()) && repoReady && datasetReady)
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
      setErr(e.status === 409 ? `run "${spec.run_id}" already exists — rename it` : 'launch failed: ' + e.message)
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
              <div className="chat-bubble"><div className="chat-text muted">
                {progress?.label ? `… ${progress.label}` : '… planning'}
                {progress?.step ? ` (step ${progress.step})` : ''}
              </div></div>
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
              {/* Catalogue tasks are fixed; an INLINE task is fully editable here — so even when the
                  boss returns an incomplete/empty task (no kind), the user can pick the type and fill
                  it in rather than being stuck on a read-only "—". */}
              {spec.task_file
                ? <div className="gen-ro">{taskLabel}</div>
                : <select className="text" value={inferredKind} onChange={e => setTaskField('kind', e.target.value || undefined)}>
                    <option value="">— choose a task type —</option>
                    <option value="repo">repo — optimize an existing code project</option>
                    <option value="dataset">dataset — here's my data, get the best metric</option>
                    <option value="mlebench_real">kaggle — a Kaggle / MLE-bench competition</option>
                    {inferredKind && !['repo', 'dataset', 'mlebench_real'].includes(inferredKind) &&
                      <option value={inferredKind}>{inferredKind}</option>}
                  </select>}
              <div className="gen-help">{spec.task_file ? 'from the task catalogue' : (inferredKind ? '' : 'pick a type, then fill the fields below')}</div>
            </div>
            {!spec.task_file && inferredKind === 'mlebench_real' && <div className="gen-field"><div className="gen-lab">Kaggle competition</div>
              <input className="text" value={task.kaggle || task.competition || ''} onChange={e => setTaskField('kaggle', e.target.value)} placeholder="kaggle-competition-id" />
              <div className="gen-help">MLE-bench / Kaggle competition id (the full slug).</div></div>}
            {isDataset && !spec.task_file && <div className="gen-repo">
              <div className="gen-field"><div className="gen-lab">Goal</div>
                <input className="text" value={task.goal || ''} onChange={e => setTaskField('goal', e.target.value)} placeholder="what to predict, e.g. the target column" /></div>
              <div className="gen-field"><div className="gen-lab">Data path</div>
                <input className="text" value={task.data_path || ''} onChange={e => setTaskField('data_path', e.target.value)} placeholder="/abs/path/to/data.csv or a folder" />
                <div className="gen-help">The data the agent reads — an absolute path (a file or a folder).</div></div>
              <div className="gen-field"><div className="gen-lab">Direction</div>
                <select className="text" value={task.direction || 'max'} onChange={e => setTaskField('direction', e.target.value)}>
                  <option value="max">maximize</option><option value="min">minimize</option></select></div>
            </div>}
            {isRepo && !spec.task_file && <div className="gen-repo">
              <div className="gen-field"><div className="gen-lab">Goal</div>
                <input className="text" value={task.goal || ''} onChange={e => setTaskField('goal', e.target.value)} placeholder="what to optimize, e.g. validation accuracy" /></div>
              <div className="gen-field"><div className="gen-lab">Repo path (editable)</div>
                <input className="text" value={repoPath} onChange={e => setRepoPath(e.target.value)} placeholder="/abs/path/to/your/repo" />
                <div className="gen-help">The codebase the agent may edit — an absolute path on this machine.</div></div>
              <div className="gen-field"><div className="gen-lab">Data mounts (optional)</div>
                <textarea className="text" rows={2} value={dataText} onChange={e => setDataset(e.target.value)} placeholder={"dataset /abs/path/to/data\nmodel /abs/path/to/weights"} />
                <div className="gen-help">Read-only datasets / model weights outside the repo — one <code>name /abs/path</code> per line (appear at ./name).</div></div>
              <div className="gen-field"><div className="gen-lab">Run + score command (cmd)</div>
                <input className="text" value={(cmdCommand || []).join(' ')} onChange={e => setCmdCommand(e.target.value)} placeholder="python test.py" />
                <div className="gen-help">{_cmdObj.metric?.reader === 'auto' ? 'Auto reader: the agent writes the metric reader for this command.' : 'How LoopLab runs + scores the repo. It must print the metric (see steps below).'}</div></div>
              <div className="gen-grid">
                <div className="gen-field"><div className="gen-lab">Metric key</div>
                  <input className="text" value={_cmdObj.metric?.key || ''} onChange={e => setMetricKey(e.target.value)} placeholder="metric" />
                  <div className="gen-help">JSON key the command prints, e.g. {'{'}&quot;metric&quot;: 0.93{'}'}.</div></div>
                <div className="gen-field"><div className="gen-lab">Direction</div>
                  <select className="text" value={task.direction || 'max'} onChange={e => setTaskField('direction', e.target.value)}>
                    <option value="max">maximize</option><option value="min">minimize</option></select></div>
                <div className="gen-field"><div className="gen-lab">Edit surface</div>
                  <input className="text" value={Array.isArray(task.edit_surface) ? task.edit_surface.join(', ') : (task.edit_surface || '')}
                    onChange={e => setTaskField('edit_surface', e.target.value.split(',').map(x => x.trim()).filter(Boolean))} placeholder="**/*.py" />
                  <div className="gen-help">Comma-separated globs the agent may change.</div></div>
              </div>
            </div>}
            <div className="gen-grid">
              <div className="gen-field"><div className="gen-lab">Model</div>
                <input className="text" value={sk.llm_model || ''} onChange={e => setSetting('llm_model', e.target.value || undefined)} placeholder="default" /></div>
              <div className="gen-field"><div className="gen-lab">Max nodes</div>
                <input className="text" type="number" value={sk.max_nodes ?? ''} onChange={e => { const n = Math.round(Number(e.target.value)); setSetting('max_nodes', e.target.value === '' || !Number.isFinite(n) ? undefined : n) }} placeholder="default" /></div>
              <div className="gen-field"><div className="gen-lab">Seeds</div>
                <input className="text" type="number" value={sk.n_seeds ?? ''} onChange={e => { const n = Math.round(Number(e.target.value)); setSetting('n_seeds', e.target.value === '' || !Number.isFinite(n) ? undefined : n) }} placeholder="default" /></div>
              <div className="gen-field"><div className="gen-lab">Policy</div>
                <select className="text" value={sk.policy || ''} onChange={e => setSetting('policy', e.target.value || undefined)}>
                  <option value="">default</option>
                  {['greedy', 'evolutionary', 'mcts', 'asha'].map(p => <option key={p} value={p}>{p}</option>)}
                </select></div>
            </div>
            {spec.setup_steps?.length > 0 && <div className="gen-steps">
              <div className="gen-lab">To make this LoopLab-ready{isRepo ? ' (adapt your repo)' : ''}</div>
              <ol className="gen-steplist">{spec.setup_steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
            </div>}
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
