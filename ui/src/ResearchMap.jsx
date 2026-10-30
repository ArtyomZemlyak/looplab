import React, { useEffect, useState } from 'react'
import { get, runApiPath } from './api.js'
import { uiText, uiMessage, useUILanguage, effectiveUILanguage } from './uiLanguage.js'
import ResearchComparison from './ResearchComparison.jsx'
import ResearchMemoDraft from './ResearchMemoDraft.jsx'
import Markdown from './markdown.jsx'
import ProjectMetricProfileEditor from './ProjectMetricProfileEditor.jsx'
import { downloadBlob } from './accessibility.jsx'
import ProfileMeasurements from './ProfileMeasurements.jsx'
import ResearchNotices from './ResearchNotices.jsx'

// This is a read-only projection. It cannot schedule an experiment or grant permission.
export default function ResearchMap({ runId, generation, onPickNode, onOpenConcepts, onOpenCards, onViewReport, readOnly = false }) {
  const [preference] = useUILanguage()
  const language = effectiveUILanguage(preference)
  const [open, setOpen] = useState(false), [offset, setOffset] = useState(0)
  const [page, setPage] = useState(null), [error, setError] = useState(false)
  const [briefing, setBriefing] = useState(null)
  useEffect(() => {
    if (!open || !runId || !/^[a-f0-9]{64}$/.test(generation || '')) return undefined
    let live = true
    const controller = new AbortController()
    setPage(null); setError(false)
    get(`${runApiPath(runId, '/research-map')}?expected_generation=${generation}&offset=${offset}&limit=20`,
      { signal: controller.signal, cache: 'no-store' })
      .then(data => { if (live) setPage(data) }).catch(() => { if (live) setError(true) })
    return () => { live = false; controller.abort() }
  }, [open, runId, generation, offset])
  useEffect(() => {
    if (!open || !runId || !/^[a-f0-9]{64}$/.test(generation || '')) return undefined
    let live = true
    const controller = new AbortController()
    setBriefing(null)
    get(`${runApiPath(runId, '/research-briefing')}?expected_generation=${generation}&language=${language}`,
      { signal: controller.signal, cache: 'no-store' })
      .then(data => { if (live) setBriefing(data) }).catch(() => { if (live) setBriefing(null) })
    return () => { live = false; controller.abort() }
  }, [open, runId, generation, language])
  const [exportError, setExportError] = useState(false)
  const exportDirection = async cardId => {
    setExportError(false)
    try {
      const params = new URLSearchParams({ expected_generation: generation, question_card_id: cardId })
      const result = await get(`${runApiPath(runId, '/research-export')}?${params}`, { cache: 'no-store' })
      if (typeof result.markdown !== 'string' || !downloadBlob(`${runId}-direction.md`, [result.markdown], 'text/markdown')) throw new Error()
    } catch { setExportError(true) }
  }
  return <details onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{uiText('Research map')}</summary>
    {open && <>
      <p>{uiText('Recorded questions and comparisons; declarations do not grant permission or prove a result.')}</p>
      {!readOnly && <ResearchMemoDraft key={`${runId}:${generation}`} runId={runId} generation={generation} />}
      {briefing && <section>
        <h3>{uiText('Current action context')}</h3>
        <p>{briefing.next_step?.title}: {briefing.next_step?.detail}</p>
        {briefing.next_step && <details><summary>{uiText('Action context and source reads')}</summary>
          <p>{uiText('Responsible actor')}: {briefing.next_step.owner === 'external_agent' ? uiText('External executor') : briefing.next_step.owner}</p>
          {briefing.next_step.phase_id && <p>{uiText('Phase')}: {briefing.next_step.phase_id}</p>}
          {briefing.next_step.action && <p>{uiText('Supported action')}: <code>{briefing.next_step.action.replaceAll('{run_id}', encodeURIComponent(runId))}</code></p>}
          <ul>{(briefing.next_step.reads || []).map((read, i) => <li key={i}><code>{read.replaceAll('{run_id}', encodeURIComponent(runId)).replaceAll('TOKEN', generation)}</code></li>)}</ul>
          <p>{uiText('These observations do not themselves grant execution authority.')}</p>
        </details>}
        {briefing.metric_profile?.state === 'configured' && <section>
          <h4>{uiText('Project metric profile')}: {briefing.metric_profile.profile.name}</h4>
          <ul>{briefing.metric_profile.profile.metrics.map(metric => <li key={metric.key}>{metric.label || metric.key} · {uiText(({primary: 'Primary metric', secondary: 'Secondary metric', transport: 'Transport metric'})[metric.role] || 'unknown')}
            {metric.denominator && ` · ${metric.denominator}`}{metric.unit && ` · ${metric.unit}`}</li>)}</ul>
          <p>{uiText('Requested slices')}: {briefing.metric_profile.profile.slices.join(', ') || uiText('not recorded')}</p>
          <p>{uiText('Project authoring language')}: {(briefing.authoring_language?.language || uiText('auto'))} · {uiText('Explicit run language takes precedence.')}</p>
          <p>{uiText('The profile describes requested presentation, not verified measurements.')}</p>
          <ProfileMeasurements runId={runId} generation={generation} />
        </section>}
        {!readOnly && briefing.metric_profile?.project_id && briefing.metric_profile?.digest && <ProjectMetricProfileEditor
          key={`${briefing.metric_profile.project_id}:${briefing.metric_profile.digest}`} context={briefing.metric_profile}
          onSaved={result => setBriefing(current => ({ ...current, metric_profile: { ...current.metric_profile, state: 'configured', ...result } }))} />}
        {briefing.mirror && <p role="status">{uiText('Mirror status')}: {briefing.mirror.state} · {uiText('Last successful pass age (seconds)')}: {briefing.mirror.last_pass_age_seconds == null ? uiText('unknown') : Math.round(briefing.mirror.last_pass_age_seconds)} · {uiText('Supervisor restarts')}: {briefing.mirror.supervisor_restarts ?? uiText('unknown')}</p>}
        {(briefing.operator_directions?.items || []).map((item, index) => <p key={index}>{item}</p>)}
        {(briefing.readiness?.plans || []).map((plan, index) => <p key={`plan-${index}`}>{uiText('Prepared dependency')}: {plan.experiment_id} / {plan.subject_arm} · {plan.producer_run_id} / #{plan.producer_node_id} · {uiText(({ binding_current: 'Binding current', binding_stale: 'Binding stale', awaiting_producer_or_binding: 'Awaiting producer or binding', external_producer_unverified: 'External producer not verified' })[plan.state] || plan.state)} · {uiText('Supported action')}: <code>{plan.next_action.replace('{run_id}', encodeURIComponent(runId))}</code></p>)}
        {(briefing.readiness?.items || []).map(binding => <p key={binding.action_id}>
          {uiText('Producer manifest binding')}: {binding.experiment_id} · #{binding.node_id} · {binding.scope_and_attempt_current ? uiText('scope and attempt current') : uiText('stale scope or attempt')} · {uiText('manifest freshness not rechecked')}{binding.status === 'artifact_and_manifest_verified' && <> · {uiText('artifact matched at read time; freshness not rechecked')}</>}
        </p>)}
        {!briefing.observation?.same_event_prefix && <p role="status">{uiText('Sources changed during this read. Refresh before acting.')}</p>}
      </section>}
      {error && <p role="status">{uiText('Research map is unavailable. Refresh the run before acting.')}</p>}
      {!page && !error && <p>{uiText('Loading…')}</p>}
      {page && <>
        {page.goal && <section><h3>{uiText('Campaign goal')}</h3><p>{page.goal}</p></section>}
        {!page.source_health?.protocol?.read_complete && <p role="status">{uiText('Protocol history is incomplete; missing entries are unknown.')}</p>}
        {page.quality && <section>
          <h3>{uiText('Research coverage')}</h3>
          <p>{uiMessage('Questions in registrations: {0}; experiments linked to a question card: {1} of {2}.',
            [page.quality.registrations_with_questions, page.quality.nodes_with_parent_card, page.quality.node_count])}</p>
          <p>{uiMessage('Registrations linked to question cards: {0} of {1}.', [page.quality.registrations_with_question_card, page.quality.registered_experiments])}</p>
          <p>{uiMessage('Research notes with sources: {0} of {1}; structured claims: {2}; direct source links: {3}.',
            [page.quality.memos_with_sources, page.quality.memo_count, page.quality.structured_claims, page.quality.claim_source_links])}</p>
          <p>{uiText('Counts do not verify claims. Technical operations may not need a separate question or literature review.')}</p>
          <p>{uiMessage('Without linked question: {0} of {1} applicable or unclassified nodes; {2} explicitly technical nodes excluded, {3} unclassified.', [page.quality.question_applicable_or_unknown_without_card, page.quality.question_applicable_or_unknown_nodes, page.quality.engineering_nodes_excluded, page.quality.unclassified_nodes])}</p>
          <p>{uiText('Intake notice resolution is unknown; inspect the original command receipt.')}</p>
          <ResearchNotices runId={runId} generation={generation} />
          {!!page.quality.report_fields_empty?.length && <p>{uiText('Unfilled report sections')}: {page.quality.report_fields_empty.join(', ')}</p>}
        </section>}
        <section><h3>{uiText('Campaign overview')}</h3>
          {page.campaign_overview ? <>
            <p>{uiMessage('Published in report #{0}', [page.campaign_overview.published_seq])}</p>
            {page.campaign_overview.detail_truncated && <p>{uiText('Details shortened; inspect the original report.')}</p>}
            {Number.isInteger(page.campaign_overview.at_node) && page.campaign_overview.at_node < page.nodes.total && <p role="status">{uiText('This overview predates some experiments; refresh its conclusions.')}</p>}
            <Markdown text={page.campaign_overview.content?.summary || ''} />
            {page.campaign_overview.content?.recommendation && <section><h4>{uiText('Current recommendation')}</h4><Markdown text={page.campaign_overview.content.recommendation} /></section>}
            {page.campaign_overview.content?.recommendation_limits && <section><h4>{uiText('Recommendation limits')}</h4><Markdown text={page.campaign_overview.content.recommendation_limits} /></section>}
            {!!page.campaign_overview.content?.report_seqs?.length && <p>{uiText('Related report versions')}: {page.campaign_overview.content.report_seqs.map(seq => onViewReport ? <button type="button" key={seq} onClick={() => onViewReport(seq)}>#{seq}</button> : `#${seq} `)}</p>}
            {['what_worked', 'what_didnt', 'learnings', 'next_directions', 'caveats'].map(key =>
              page.campaign_overview.content?.[key]?.length ? <section key={key}>
                <h4>{uiText(({ what_worked: 'What worked', what_didnt: "What didn't work", learnings: 'Learnings', next_directions: 'Next directions', caveats: 'Caveats' })[key])}</h4>
                <ul>{page.campaign_overview.content[key].map((item, i) => <li key={i}>{item}</li>)}</ul>
              </section> : null)}
          </> : <p>{uiText('No cumulative campaign overview has been published.')}</p>}
        </section>
        <section><h3>{uiText('Research questions and directions')}</h3>
          <p>{uiText('Card verdicts are derived from recorded evidence and do not establish causality.')}</p>
          {onOpenCards && <button type="button" onClick={onOpenCards}>{uiText('Open question board')}</button>}
          {exportError && <p role="status">{uiText('Direction export failed. Refresh and try again.')}</p>}
          {(page.cards?.items || []).map(card => <article key={card.card_id}>
            <h4>{card.statement || card.card_id}</h4><button type="button" onClick={() => exportDirection(card.card_id)}>{uiText('Export direction')}</button><p>{card.kind} · {uiText(card.status)} · {card.verdict}</p>
            {card.parent_card_id && <p>{uiText('Parent direction')}: {card.parent_card_id}</p>}
            <ul>{(card.evidence || []).map(n => <li key={n}>{onPickNode ? <button type="button" onClick={() => onPickNode(n)}>#{n}</button> : `#${n}`}{card.discarded_nodes?.includes(n) && ` · ${uiText('discarded')}`}</li>)}</ul>
            {card.detail_truncated && <p>{uiText('Details shortened; inspect the original card.')}</p>}
          </article>)}
        </section>
        <h3>{uiText('Registered experiments')}</h3>
        {!page.experiments.items.length && <p>{uiText('No registered experiments in this page.')}</p>}
        {page.experiments.items.map(exp => <article key={exp.experiment_id}>
          <h4>{exp.title || exp.experiment_id}</h4>
          {exp.detail_truncated && <p>{uiText('Details shortened; inspect the original protocol.')}</p>}
          {!!exp.quality_issues?.length && <section><h5>{uiText('Suggestions for clarity')}</h5><ul>{exp.quality_issues.map(code => <li key={code}>{uiText(({ generic_title: 'Use a specific title.', hard_to_read: 'Review text for missing spaces.', question_not_recorded: 'Record the scientific question if applicable.', control_not_recorded: 'Describe the control if applicable.', criterion_not_explained: 'Explain the success criterion if applicable.', motivation_not_explained: 'Explain why this experiment is informative.' })[code] || code)}</li>)}</ul>
            <p>{uiText('Suggestions are advisory and do not block work.')}</p></section>}
          {exp.question && <p>{exp.question}</p>}
          {exp.question_card_id && <p>{uiText('Linked question')}: {exp.question_card_statement || exp.question_card_id}
            {onOpenCards && <> · <button type="button" onClick={onOpenCards}>{uiText('Open question board')}</button></>}</p>}
          {exp.hypothesis && <p>{exp.hypothesis}</p>}
          <p>{uiText('Primary metric')}: {exp.primary_metric || uiText('not recorded')}</p>
          {exp.research_context && Object.entries({ change: 'What changes', motivation: 'Why', data_weights: 'Data and weights',
            control: 'Control', criterion: 'Success criterion', alternatives: 'Alternatives' }).map(([key, label]) =>
            exp.research_context[key] ? <p key={key}><strong>{uiText(label)}:</strong> {exp.research_context[key]}</p> : null)}
          {!!exp.tests?.length && <p>{uiText('Declared evaluation sets')}: {exp.tests.map(test => [test.label, test.test_id, test.description].filter(Boolean).join(' · ')).join('; ')}</p>}
          {!!exp.metrics?.length && <p>{uiText('Declared metrics')}: {exp.metrics.map(metric => [metric.name, metric.direction, metric.description].filter(Boolean).join(' · ')).join('; ')}</p>}
          {!!exp.contrasts?.length && <p>{uiText('Declared comparisons')}: {exp.contrasts.map(c => `${c.control} → ${c.treatment} · ${c.metric} · ${c.test}`).join('; ')}</p>}
          <p>{uiText('Declarations alone do not verify that cohorts or evaluation policies match.')}</p>
          <ul>{(exp.arms || []).map(arm => <li key={arm.name}>{arm.name}: {arm.node_id == null ? uiText('not recorded') :
            onPickNode ? <button type="button" onClick={() => onPickNode(arm.node_id)}>#{arm.node_id}</button> : `#${arm.node_id}`}
            {' · '}{arm.node_status || uiText('not measured')}{arm.work_kind && ` · ${uiText(({ scientific_experiment: 'Scientific experiment', fit: 'Training', score_only: 'Scoring only', engineering_recovery: 'Technical recovery' })[arm.work_kind] || arm.work_kind)}`}</li>)}</ul>
          {!!exp.relationships?.length && <section><h5>{uiText('Declared relationships')}</h5>
            <ul>{exp.relationships.map((rel, i) => <li key={i}>{rel.subject_arm} · {uiText(({ model_producer: 'Model producer', scientific_control: 'Scientific control', code_reference: 'Code reference' })[rel.kind] || rel.kind)} · {rel.run_id && rel.run_id !== runId
              ? <><a href={`#/run/${encodeURIComponent(rel.run_id)}`}>{rel.run_id}</a> #{rel.node_id} · {uiText('Run identity not verified')}</>
              : onPickNode ? <button type="button" onClick={() => onPickNode(rel.node_id)}>#{rel.node_id}</button> : `#${rel.node_id}`}
              {rel.node_generation == null ? '' : ` · ${uiMessage('Attempt {0}', [rel.node_generation])}`} · {rel.explanation}</li>)}</ul>
            <p>{uiText('Declared links do not verify artifact identity.')}</p>
          </section>}
          <ResearchComparison runId={runId} experimentId={exp.experiment_id} />
        </article>)}
        <details><summary>{uiText('Research evidence and decisions')}</summary>
          <p>{uiText('Source links and recorded verdicts do not establish the truth of a claim.')}</p>
          {(page.research_memos?.items || []).map((memo, i) => <article key={memo.memo_id || i}>
            <h4>{memo.summary}</h4>
            {memo.detail_truncated && <p>{uiText('Details shortened; inspect the original research memo.')}</p>}
            {memo.reasoning && <p>{memo.reasoning}</p>}
            {(memo.local_lessons || []).map((lesson, j) => <section key={`lesson-${j}`}>
              <p>{uiText('Run-local lesson')} · {uiText('author status')}: {uiText(lesson.status)} · {lesson.statement}</p>
              {lesson.limitation && <p>{uiText('Limitation')}: {lesson.limitation}</p>}
              <ul>{(lesson.node_ids || []).map(n => <li key={n}>{onPickNode
                ? <button type="button" onClick={() => onPickNode(n)}>#{n}</button> : `#${n}`}</li>)}</ul>
            </section>)}
            {(memo.sources || []).map((source, j) => <p key={j}>{source.title}: {/^https?:\/\//i.test(source.url)
              ? <a href={source.url} target="_blank" rel="noopener noreferrer">{source.url}</a> : source.url}</p>)}
            {(memo.claims || []).map((claim, j) => <section key={j}><p>{claim.statement} · {claim.verdict || uiText('not verified')}</p>
              {claim.author_status && <p>{uiText('Author assessment')}: {uiText(claim.author_status)}</p>}
              {claim.limitation && <p>{uiText('Limitation')}: {claim.limitation}</p>}
              {claim.contrast_ref && <p>{uiText('Declared contrast')}: {claim.contrast_ref}</p>}
              <ul>{claim.node_ids.map(n => <li key={`node-${n}`}>{onPickNode
                ? <button type="button" onClick={() => onPickNode(n)}>#{n}</button> : `#${n}`}</li>)}
                {claim.urls.map((url, k) => <li key={`source-${k}`}>{/^https?:\/\//i.test(url)
                  ? <a href={url} target="_blank" rel="noopener noreferrer">{url}</a> : url}</li>)}</ul>
            </section>)}
          </article>)}
          <h4>{uiText('Review decisions by type')}</h4>
          <p>{uiText('A review receipt is an author decision, not proof that a scientific claim is true.')}</p>
          {!page.source_health?.reviews?.read_complete && <p role="status">{uiText('Review history is incomplete.')}</p>}
          {(page.reviews?.items || []).map((review, i) => <article key={review.action_id || i}>
            <p>{uiText(review.phase)} · {review.decision} · {review.reason}</p>
            {review.action_ref && <p>{uiText('Recorded action')}: {review.action_ref}</p>}
            <ul>{(review.evidence || []).map(n => <li key={n}>{onPickNode ? <button type="button" onClick={() => onPickNode(n)}>#{n}</button> : `#${n}`}</li>)}</ul>
          </article>)}
          {!page.source_health?.decisions?.read_complete && <p role="status">{uiText('Decision history is incomplete.')}</p>}
          {(page.decisions?.items || []).map((decision, i) => <article key={decision.action_id || i}>
            <p>{uiText(decision.phase)} · {decision.decision} · {decision.reason}</p>
            {decision.alternatives_summary && <p>{uiText('Alternatives')}: {decision.alternatives_summary}</p>}
            {decision.information_gain && <p>{uiText('Expected information')}: {decision.information_gain}</p>}
            {decision.not_applicable_reason && <p>{uiText('Why not applicable')}: {decision.not_applicable_reason}</p>}
            <ul>{(decision.prior_node_ids || []).map(n => <li key={n}>{onPickNode ? <button type="button" onClick={() => onPickNode(n)}>#{n}</button> : `#${n}`}</li>)}</ul>
          </article>)}
        </details>
        <section><h3>{uiText('Per-query evidence')}</h3>
          <p>{uiText('The test ID covers query and cluster identity; it does not by itself verify ground truth or evaluation policy.')}</p>
          {!page.source_health?.uploads?.read_complete && <p role="status">{uiText('Upload history is incomplete.')}</p>}
          {(page.uploads?.items || []).map(upload => <article key={upload.upload_id}>
            <p>{onPickNode ? <button type="button" onClick={() => onPickNode(upload.node_id)}>#{upload.node_id}</button> : `#${upload.node_id}`}
              {' · '}{upload.test_id} · {upload.experiment_id} · {upload.arm} · {upload.n_queries} {uiText('queries')}</p>
            <p>{uiText('Recorded metrics')}: {(upload.metrics || []).join(', ')} · {uiText('Consistency')}: {uiText(({ matches_measured: 'matches recorded value', differs_from_measured: 'differs from recorded value', no_measured_value: 'recorded value unavailable', unchecked: 'not checked' })[upload.consistency?.status] || 'unknown')}
              {upload.stale && ` · ${uiText('stale attempt')}`}</p>
          </article>)}
        </section>
        <section><h3>{uiText('Experiment cards and concepts')}</h3>
          <p>{uiText('Concept labels and questions are authored links. Missing links do not establish that an experiment lacks scientific value.')}</p>
          {onOpenConcepts && <button type="button" onClick={onOpenConcepts}>{uiText('Explore concepts')}</button>}
          {(page.nodes.items || []).map(node => <article key={node.node_id}>
            <h4>{onPickNode ? <button type="button" onClick={() => onPickNode(node.node_id)}>#{node.node_id}</button> : `#${node.node_id}`} · {node.title}</h4>
            {node.parent_card_id && <p>{uiText('Parent direction')}: {node.parent_card_statement || node.parent_card_id}
              {onOpenCards && <> · <button type="button" onClick={onOpenCards}>{uiText('Open question board')}</button></>}</p>}
            {node.hypothesis && <p>{uiText('Hypothesis')}: {node.hypothesis}</p>}
            {node.rationale && <p>{uiText('Rationale')}: {node.rationale}</p>}
            {node.status && <p>{uiText('Recorded status')}: {uiText(node.status)}{Number.isInteger(node.attempt) && <> · {uiMessage('Attempt {0}', [node.attempt])}</>}</p>}
            {!!node.declared_work_kinds?.length && <p>{uiText('Declared work type')}: {node.declared_work_kinds.map(kind => uiText(({ scientific_experiment: 'Scientific experiment', fit: 'Training', score_only: 'Scoring only', engineering_recovery: 'Technical recovery' })[kind] || kind)).join(', ')}</p>}
            {!!node.extra_metric_keys?.length && <p>{uiText('Recorded metric keys')}: {node.extra_metric_keys.join(', ')}</p>}
            {!!node.parent_ids?.length && <p>{uiText('Recorded node parents')}: {node.parent_ids.map(n => `#${n}`).join(', ')} · {uiText('Parent links do not by themselves identify a scientific control.')}</p>}
            {!!node.questions?.length && <ul>{node.questions.map((q, i) => <li key={i}>{q}</li>)}</ul>}
            <p>{uiText('Concepts')}: {(node.concepts || []).join(', ') || uiText('not recorded')}</p>
            {node.detail_truncated && <p>{uiText('Details shortened; inspect the original experiment card.')}</p>}
          </article>)}
        </section>
        <h3>{uiText('Report history')}</h3>
        <ul>{page.reports.items.map(report => <li key={report.seq}>#{report.seq} · {report.headline}</li>)}</ul>
        <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>{uiText('Previous')}</button>{' '}
        <button type="button" disabled={!page.experiments.has_more && !page.cards?.has_more && !page.uploads?.has_more && !page.nodes.has_more && !page.research_memos?.has_more && !page.decisions?.has_more && !page.reviews?.has_more} onClick={() => setOffset(offset + 20)}>{uiText('Next')}</button>
        <p>{uiMessage('{0} experiments', [page.experiments.total])}</p>
      </>}
    </>}
  </details>
}
