import { uiMessage, uiText, useUILanguage } from './uiLanguage.js'
import React from 'react'
import Markdown from './markdown.jsx'
import { PanelResourceNotice } from './PanelResourceNotice.jsx'
import { get } from './api.js'
import { useScopedResource } from './useScopedResource.js'
import { campaignView, fmtWhen } from './campaignModel.js'
import { ContrastTable, LeaderboardTable, Registration } from './CampaignTables.jsx'

// The SUPER-TASK campaign report (doc 81), shown inside the cross-run report of a super-task scope:
// the narrative the agent (or operator) authored, then the comparisons LoopLab COMPUTED from the
// per-query uploads of every run in the super-task, then one leaderboard per shared test id. The
// narrative and the tables are rendered apart on purpose: prose is authored, numbers are measured.
export default function CampaignReport({ supertaskId }) {
  useUILanguage()
  const resource = useScopedResource(
    signal => get(`/api/supertasks/${encodeURIComponent(supertaskId)}/campaign`, { signal }),
    { scope: supertaskId, timeout: 30_000 })
  const view = resource.data ? campaignView(resource.data) : null
  const experiments = view ? view.runs.flatMap(run => run.experiments.map(exp => ({ ...exp, runId: run.runId }))) : []
  return <div className="sr-sec campaign-report">
    <div className="sr-h">{uiText('Campaign')}</div>
    <PanelResourceNotice resource={resource} label={uiText('Campaign report')} onRetry={() => resource.retry()} />
    {view && view.narrative && <div className="campaign-narrative">
      <div className="sr-advisory">{uiMessage('Narrative by {0} · revision {1} · {2} — authored text, not a measurement',
        [uiText(view.narrative.author), view.narrative.revision ?? '?', fmtWhen(view.narrative.at)])}</div>
      {view.narrative.title && <div className="sr-headline">{view.narrative.title}</div>}
      {view.narrative.findings.length > 0 && <ul>{view.narrative.findings.map((f, i) => <li key={i}>{f}</li>)}</ul>}
      <Markdown text={view.narrative.markdown} />
    </div>}
    {view && !view.narrative && <div className="muted">{uiText('No campaign narrative yet; the external agent publishes one with publish_campaign_narrative.')}</div>}
    {experiments.map(exp => <div key={`${exp.runId}:${exp.id}`} className="protocol-experiment">
      <div><b>{exp.title}</b> <code>{exp.runId} · {exp.id}</code> · <Registration experiment={exp} /></div>
      <div>{uiText('Hypothesis: ')}{exp.hypothesis}</div>
      <ContrastTable rows={exp.rows} caption={uiMessage('Paired contrasts of {0}', [exp.id])}
        csvName={`contrasts-${exp.runId}-${exp.id}`} />
      {exp.caveats.length > 0 && <ul className="muted">{exp.caveats.map((c, i) => <li key={i}>{c}</li>)}</ul>}
    </div>)}
    {view && view.leaderboards.map(board => <LeaderboardTable key={board.testId} board={board} />)}
    {view && experiments.length === 0 && view.leaderboards.length === 0 && <div className="muted">
      {uiText('No run in this super-task has registered an experiment or uploaded per-query results.')}</div>}
  </div>
}
