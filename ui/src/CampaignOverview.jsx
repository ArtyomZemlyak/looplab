import React, { useEffect, useState } from 'react'
import { get, runApiPath } from './api.js'
import { uiMessage, uiText, useUILanguage } from './uiLanguage.js'
import Markdown from './markdown.jsx'
import { campaignOverviewView } from './campaignOverviewModel.js'

const GENERATION = /^[0-9a-f]{64}$/
const GROUPS = [
  ['what_worked', 'What worked'], ['what_didnt', "What didn't work"],
  ['learnings', 'Learnings'], ['next_directions', 'Next directions'], ['caveats', 'Caveats'],
]

export function CampaignOverview({ overview, visibleNodeCount, mapEventSeq, snapshotSeq,
  onViewReport, headingLevel = 2 }) {
  useUILanguage()
  const view = campaignOverviewView(overview, { visibleNodeCount, mapEventSeq, snapshotSeq })
  const Heading = headingLevel === 3 ? 'h3' : 'h2'
  const Subheading = headingLevel === 3 ? 'h4' : 'h3'
  if (view.status === 'inconsistent') return <section className={'campaign-overview ' + view.status} role="status">
    <Heading>{uiText('Campaign overview')}</Heading>
    <p>{uiText('The research map is newer than this report snapshot. Reload the run instead of combining two points in time.')}</p>
  </section>
  return <section className={`campaign-overview ${view.status}`} aria-labelledby={`campaign-overview-heading-${headingLevel}`}>
    <div className="agent-report-head">
      <Heading id={`campaign-overview-heading-${headingLevel}`}>{uiText('Campaign overview')}</Heading>
      <span className="pill">{uiText('cumulative · not one experiment')}</span>
    </div>
    {!overview ? <p>{uiText('No cumulative campaign overview has been published. The latest local report does not replace it.')}</p> : <>
      <p className="report-section-intro">
        {view.publishedSeq !== null && <span>{uiText('Published in report ')}{onViewReport
          ? <button type="button" onClick={() => onViewReport(view.publishedSeq)}>#{view.publishedSeq}</button>
          : <b>#{view.publishedSeq}</b>}{' · '}</span>}
        {view.atNode !== null ? uiMessage('node watermark {0}', [view.atNode]) : uiText('node watermark not recorded')}
        {overview.detail_truncated && <> · {uiText('details shortened; inspect the original report')}</>}
      </p>
      {view.reasons.map((row, index) => <p className="report-coverage-warning" role="status" key={`${row.code}-${index}`}>
        {row.code === 'newer_terminal' ? uiMessage('A terminal result was recorded after this overview (event #{0}). Review its recommendation before relying on it.', [row.event_seq])
          : row.code === 'newer_operator_direction' ? uiMessage('An operator direction was recorded after this overview (event #{0}). The old recommendation may no longer match the current task.', [row.event_seq])
            : row.code === 'newer_nodes' ? uiMessage('This overview covers {0} of {1} visible nodes. Review the later results before relying on its recommendation.', [row.at_node, row.current_nodes])
              : uiText('Newer evidence was recorded after this overview. Review it before relying on the recommendation.')}
      </p>)}
      {view.status === 'unknown' && <p className="muted">{uiText('Coverage cannot be judged from a node count. Check the original report and later evidence.')}</p>}
      <Markdown text={overview.content?.summary || ''} />
      {overview.content?.recommendation && <section><Subheading>{uiText('Current recommendation')}</Subheading><Markdown text={overview.content.recommendation} /></section>}
      {overview.content?.recommendation_limits && <section><Subheading>{uiText('Recommendation limits')}</Subheading><Markdown text={overview.content.recommendation_limits} /></section>}
      {GROUPS.map(([key, label]) => overview.content?.[key]?.length ? <section key={key}>
        <Subheading>{uiText(label)}</Subheading><ul>{overview.content[key].map((item, index) => <li key={index}><Markdown text={item} externalOnly /></li>)}</ul>
      </section> : null)}
      {!!overview.content?.report_seqs?.length && <p>{uiText('Related report versions')}: {overview.content.report_seqs.map(seq => onViewReport
        ? <button type="button" key={seq} onClick={() => onViewReport(seq)}>#{seq}</button> : `#${seq} `)}</p>}
    </>}
  </section>
}

export default function CampaignOverviewResource({ runId, generation, publicationSeq = null,
  snapshotSeq = null, visibleNodeCount = 0, enabled = true, onViewReport = null }) {
  const scope = JSON.stringify({ runId, generation, publicationSeq, snapshotSeq, enabled })
  const [resource, setResource] = useState({ scope: '', status: 'idle', page: null })
  useEffect(() => {
    if (!enabled || !GENERATION.test(generation || '')) {
      setResource({ scope, status: 'idle', page: null })
      return undefined
    }
    let live = true
    const controller = new AbortController()
    const query = new URLSearchParams({ expected_generation: generation, offset: '0', limit: '1' })
    setResource({ scope, status: 'loading', page: null })
    get(`${runApiPath(runId, '/research-map')}?${query}`, { signal: controller.signal, cache: 'no-store' })
      .then(page => {
        if (!live) return
        if (page?.generation !== generation || page?.run_id !== runId
            || !Number.isSafeInteger(page?.event_seq)) throw new Error()
        setResource({ scope, status: 'ready', page })
      })
      .catch(error => {
        if (live && error?.name !== 'AbortError') setResource({ scope, status: 'error', page: null })
      })
    return () => { live = false; controller.abort() }
  }, [runId, generation, publicationSeq, snapshotSeq, enabled, scope])
  if (!enabled || !GENERATION.test(generation || '')) return null
  if (resource.scope !== scope || resource.status === 'loading') {
    return <p className="muted">{uiText('Loading campaign overview…')}</p>
  }
  if (resource.status === 'error') {
    return <p role="status">{uiText('Campaign overview is unavailable. Refresh the run; the latest local report is still shown separately.')}</p>
  }
  return <CampaignOverview overview={resource.page?.campaign_overview} visibleNodeCount={visibleNodeCount}
    mapEventSeq={resource.page?.event_seq} snapshotSeq={snapshotSeq} onViewReport={onViewReport} />
}
