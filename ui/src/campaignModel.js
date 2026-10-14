// The CAMPAIGN model — the pure half of `ProtocolPanel.jsx` and `CampaignReport.jsx` (doc 81; the
// house pattern: decisions in a plain module `node --test` drives, the components keep the fetch and
// the markup).
//
// What it reads is the server's own computation: `GET /api/runs/{id}/protocol` (pre-registered
// experiments + the amendment audit trail), `GET /api/runs/{id}/experiments/{eid}/comparison` (the
// paired contrasts LoopLab computed) and `GET /api/supertasks/{sid}/campaign` (the cross-run report).
// Nothing here computes a statistic: a cell is the server's number or an explicit dash, and every
// row carries the AUTHOR of what it shows — `agent` (the scoped harness token), `operator` (the
// owner) or `LoopLab` (a computation) — because a reader must be able to tell prose an agent wrote
// from a number LoopLab measured.

const text = (value, max = 4000) => (typeof value === 'string' ? value.slice(0, max) : '')
const finite = value => (typeof value === 'number' && Number.isFinite(value) ? value : null)
const list = value => (Array.isArray(value) ? value : [])
const record = value => (value && typeof value === 'object' && !Array.isArray(value) ? value : {})

export const AUTHOR_LABELS = { agent: 'agent', operator: 'operator', rule: 'rule', looplab: 'LoopLab' }

export function authorLabel(author) {
  const kind = typeof author === 'string' ? author : record(author).kind
  return AUTHOR_LABELS[kind] || 'unknown'
}

// Significant digits that keep a small paired delta readable (0.000475, not 0.0005) without
// printing 17 digits of float noise.
export function fmtStat(value, digits = 4) {
  const v = finite(value)
  if (v === null) return '—'
  if (v === 0) return '0'
  const abs = Math.abs(v)
  if (abs < 1e-4 || abs >= 1e6) return v.toExponential(2)
  return Number(v.toPrecision(digits)).toString()
}

export function fmtP(value) {
  const v = finite(value)
  if (v === null) return '—'
  if (v < 1e-4) return v.toExponential(1)
  return v.toFixed(4)
}

export function fmtCI(low, high) {
  const lo = finite(low), hi = finite(high)
  return lo === null || hi === null ? '—' : `[${fmtStat(lo)}; ${fmtStat(hi)}]`
}

export function fmtWhen(ts) {
  const v = finite(ts)
  if (v === null) return '—'
  const date = new Date(v * 1000)
  return Number.isNaN(date.getTime()) ? '—' : date.toISOString().replace('T', ' ').slice(0, 16) + 'Z'
}

export function protocolView(payload) {
  const body = record(payload)
  const policy = record(body.policy)
  return {
    version: Number.isInteger(body.protocol_version) ? body.protocol_version : 0,
    experiments: list(body.experiments).map(experimentView).filter(Boolean),
    amendments: list(body.amendments).map(amendmentView).filter(Boolean),
    policy: {
      amendments: text(policy.amendments, 32) || 'all',
      cap: Number.isInteger(policy.max_agent_added_nodes) ? policy.max_agent_added_nodes : null,
      source: text(policy.source, 64) || 'default',
    },
  }
}

export function experimentView(raw) {
  const exp = record(raw)
  const id = text(exp.experiment_id, 64)
  if (!id) return null
  const reg = record(exp.registration)
  return {
    id,
    title: text(exp.title, 300) || id,
    question: text(exp.question),
    hypothesis: text(exp.hypothesis),
    superseded: exp.superseded === true,
    supersedes: text(exp.supersedes, 64) || null,
    correction: text(exp.correction, 16) || 'holm',
    arms: Object.entries(record(exp.arms)).map(([label, arm]) => ({
      label, runId: text(record(arm).run_id, 255) || null,
      nodeId: Number.isInteger(record(arm).node_id) ? record(arm).node_id : null,
      description: text(record(arm).description, 500),
    })),
    contrasts: list(exp.contrasts).length,
    registeredAt: finite(reg.recorded_at),
    registeredSeq: Number.isInteger(reg.at_event_seq) ? reg.at_event_seq : null,
    author: authorLabel(reg.author),
    version: Number.isInteger(reg.version) ? reg.version : null,
  }
}

export function amendmentView(raw) {
  const row = record(raw)
  const id = text(row.amendment_id, 64)
  if (!id) return null
  const payload = record(row.payload)
  const diffs = []
  if (typeof payload.diff === 'string') diffs.push({ path: 'task.snapshot.json', diff: payload.diff })
  for (const file of list(payload.files)) {
    const f = record(file)
    if (typeof f.diff === 'string') diffs.push({ path: text(f.path, 1024), diff: f.diff,
      protected: f.protected === true })
  }
  return {
    id, version: Number.isInteger(row.version) ? row.version : null,
    kind: text(row.kind, 16), author: authorLabel(row.author), reason: text(row.reason, 2000),
    summary: text(row.summary, 500), at: finite(row.recorded_at),
    atSeq: Number.isInteger(row.at_event_seq) ? row.at_event_seq : null, diffs,
  }
}

// One contrast row of a comparison, ready to render. `flags` are the provenance caveats a reader
// must see beside the number, in the order of their weight.
export function contrastRow(raw) {
  const c = record(raw)
  const t = record(c.treatment_source), k = record(c.control_source)
  const flags = []
  if (c.status !== 'computed') flags.push({ tone: 'warn', label: 'not computed', title: text(c.reason, 500) })
  if (c.registered_before_result === false) flags.push({ tone: 'warn', label: 'post hoc',
    title: 'registered after the treatment result existed' })
  if (c.registered_before_result === true) flags.push({ tone: 'ok', label: 'pre-registered',
    title: 'registered before the treatment result existed' })
  for (const [side, src] of [['treatment', t], ['control', k]]) {
    if (src.stale === true) flags.push({ tone: 'warn', label: `${side} stale`,
      title: 'the node was reset after these arrays were uploaded' })
    if (src.consistency === 'differs_from_measured') flags.push({ tone: 'warn',
      label: `${side} ≠ measured`, title: 'the upload does not reproduce the metric LoopLab measured' })
  }
  const where = src => (src.run_id ? `${src.run_id}#${src.node_id ?? '?'}` : '—')
  return {
    id: text(c.id, 200), status: text(c.status, 32), family: text(c.family, 64),
    metric: text(c.metric, 64), test: text(c.test, 64),
    treatment: text(c.treatment, 64), control: text(c.control, 64),
    delta: finite(c.delta), low: finite(c.ci95_low), high: finite(c.ci95_high),
    p: finite(c.p_two_sided), pAdj: finite(c.p_adjusted),
    significant: c.significant === true, familySize: Number.isInteger(c.family_size) ? c.family_size : null,
    clusters: Number.isInteger(c.clusters) ? c.clusters : null,
    treatmentWhere: where(t), controlWhere: where(k),
    bootstrap: record(c.bootstrap).ci95_low != null ? record(c.bootstrap) : null,
    flags, reason: text(c.reason, 500),
  }
}

export function comparisonView(payload) {
  const body = record(payload)
  const by = record(body.computed_by)
  return {
    rows: list(body.contrasts).map(contrastRow),
    caveats: list(body.caveats).map(x => text(x, 500)).filter(Boolean),
    method: text(by.method, 64), ci: text(by.ci, 200), correction: text(by.correction, 16),
    computedAt: finite(by.computed_at),
  }
}

export function leaderboardView(board) {
  const b = record(board)
  return {
    testId: text(b.test_id, 64), label: text(b.test_label, 64), metric: text(b.metric, 64),
    rows: list(b.entries).map(raw => {
      const e = record(raw)
      return {
        runId: text(e.run_id, 255), nodeId: Number.isInteger(e.node_id) ? e.node_id : null,
        label: text(e.label, 200), value: finite(e.value),
        low: finite(e.ci95_low), high: finite(e.ci95_high),
        consistency: text(e.consistency, 32), stale: e.stale === true,
        author: authorLabel(e.author),
      }
    }),
  }
}

export function campaignView(payload) {
  const body = record(payload)
  const narrative = record(body.narrative)
  return {
    name: text(record(body.supertask).name, 300),
    runs: list(body.runs).map(raw => {
      const r = record(raw)
      return {
        runId: text(r.run_id, 255),
        amendments: list(r.amendments).length,
        experiments: list(r.experiments).map(e => ({
          ...experimentView(e) || { id: '?', title: '?' },
          rows: list(record(e).contrasts).map(contrastRow),
          caveats: list(record(e).caveats).map(x => text(x, 500)).filter(Boolean),
        })),
      }
    }),
    leaderboards: list(body.leaderboards).map(leaderboardView),
    narrative: body.narrative ? {
      title: text(narrative.title, 300), markdown: text(narrative.body_markdown, 65536),
      findings: list(narrative.key_findings).map(x => text(x, 500)).filter(Boolean),
      revision: Number.isInteger(narrative.revision) ? narrative.revision : null,
      author: authorLabel(narrative.author), at: finite(narrative.recorded_at),
    } : null,
  }
}
