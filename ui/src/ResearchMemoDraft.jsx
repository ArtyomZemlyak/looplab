import { uiMessage } from './uiLanguage.js'
import React, { useState } from 'react'
import { get, post, runApiPath, runCommand, createIdempotencyKey, COMMAND_PENDING,
  loadResearchMemoIntent, saveResearchMemoIntent, clearResearchMemoIntent } from './api.js'
import { uiText, useUILanguage } from './uiLanguage.js'

const MESSAGES = {
  missing_conclusion: 'Add a conclusion.', missing_reasoning: 'Explain the reasoning and alternatives.',
  no_open_question: 'Consider recording the open scientific question.',
  no_next_direction: 'Consider recording what the next experiment will test.',
  sources_not_linked_to_claim: 'Link the source to the claim it supports.',
  evidence_absent_or_explain: 'Add evidence or explain why an external review is unnecessary.',
  claim_has_no_evidence: 'Link each claim to an experiment or a source.',
  some_sources_unlinked: 'Some sources are not linked to a claim.',
  questions_not_on_board: 'File relevant open questions on the question board.',
  experiment_not_filed: 'Link the experiment to its research direction.',
  readability: 'Review text for missing spaces.',
  readability_hint: 'Review text readability.',
  truncated: 'Some submitted text was shortened.',
  redacted: 'Some submitted text was redacted.',
  items_omitted: 'Some submitted items were omitted.',
}

export default function ResearchMemoDraft({ runId, generation }) {
  useUILanguage()
  const [purpose, setPurpose] = useState('scientific'), [summary, setSummary] = useState('')
  const [reasoning, setReasoning] = useState(''), [question, setQuestion] = useState('')
  const [source, setSource] = useState(''), [sourceTitle, setSourceTitle] = useState('')
  const [claim, setClaim] = useState(''), [next, setNext] = useState('')
  const [claimStatus, setClaimStatus] = useState('open'), [claimLimit, setClaimLimit] = useState(''), [contrastRef, setContrastRef] = useState('')
  const [known, setKnown] = useState(''), [difference, setDifference] = useState('')
  const [alternatives, setAlternatives] = useState(''), [uncertainty, setUncertainty] = useState('')
  const [decision, setDecision] = useState(''), [nodeIds, setNodeIds] = useState('')
  const [technicalNode, setTechnicalNode] = useState(''), [technicalAttempt, setTechnicalAttempt] = useState('')
  const [lesson, setLesson] = useState(''), [lessonLimit, setLessonLimit] = useState(''), [lessonStatus, setLessonStatus] = useState('open')
  const [feedback, setFeedback] = useState(null), [status, setStatus] = useState('idle')
  const [intent, setIntent] = useState(() => loadResearchMemoIntent(runId, generation)), [receipt, setReceipt] = useState(null)
  const memo = () => ({ summary, reasoning: [reasoning, known && uiMessage('Known: {0}', [known]),
    difference && uiMessage('Methodological difference: {0}', [difference]), alternatives && uiMessage('Alternatives: {0}', [alternatives]),
    uncertainty && uiMessage('Uncertainty: {0}', [uncertainty]), decision && uiMessage('Decision: {0}', [decision])].filter(Boolean).join('\n\n'), open_questions: question ? [question] : [],
    next_experiments: next ? [next] : [], sources: source ? [{ title: sourceTitle, url: source }] : [],
    claims: claim ? [{ statement: claim, author_status: claimStatus, limitation: claimLimit, contrast_ref: contrastRef, node_ids: nodeIds.split(',').map(s => s.trim()).filter(s => /^\d+$/.test(s)).map(Number).filter(Number.isSafeInteger), urls: source ? [source] : [] }] : [],
    local_lessons: lesson ? [{ statement: lesson, limitation: lessonLimit, status: lessonStatus,
      node_ids: nodeIds.split(',').map(s => s.trim()).filter(s => /^\d+$/.test(s)).map(Number).filter(Number.isSafeInteger) }] : [] })
  const invalidNodeIds = nodeIds.trim() !== '' && nodeIds.split(',').some(s => !/^\d+$/.test(s.trim()) || !Number.isSafeInteger(Number(s.trim())))
  const technicalText = [summary, reasoning].filter(Boolean).join('\n\n')
  const technicalInvalid = purpose === 'technical' && (![technicalNode, technicalAttempt].every(v => /^\d+$/.test(v) && Number.isSafeInteger(Number(v)))
    || new TextEncoder().encode(technicalText).length > 8192)
  const preview = async () => {
    setStatus('previewing')
    try {
      const result = await post(runApiPath(runId, '/research-memo-review'),
        { expected_generation: generation, purpose, memo: memo() })
      setFeedback(result); setStatus('idle')
    } catch { setStatus('preview_error') }
  }
  const publish = async () => {
    const saved = intent || { generation, key: createIdempotencyKey(),
      type: purpose === 'technical' ? 'comment_created' : 'research_completed',
      data: purpose === 'technical' ? { node_id: Number(technicalNode), node_generation: Number(technicalAttempt), text: technicalText } : { memo: memo() } }
    if (!intent) {
      if (!saveResearchMemoIntent(runId, saved)) { setStatus('storage_error'); return }
      setIntent(saved)
    }
    setStatus('publishing')
    try {
      const record = await runCommand(runId, saved.type, saved.data,
        { expectedGeneration: saved.generation, idempotencyKey: saved.key })
      setReceipt(record); setStatus(COMMAND_PENDING.has(record.status) ? 'unknown' : 'receipt')
    } catch { setStatus('unknown') }
  }
  const checkReceipt = async () => {
    setStatus('checking')
    try {
      const found = await get(`${runApiPath(runId, '/command-receipt')}?expected_generation=${generation}`,
        { headers: { 'Idempotency-Key': intent.key }, cache: 'no-store' })
      setReceipt({ ...found.command, intake_notices: found.intake_notices }); setStatus(found.terminal ? 'receipt' : 'unknown')
    } catch { setStatus('unknown') }
  }
  const reset = () => { clearResearchMemoIntent(runId); setIntent(null); setReceipt(null); setStatus('idle') }
  const locked = !!intent
  return <details><summary>{uiText('Draft a research memo')}</summary>
    <p>{uiText('Write the decision and evidence. Technical recovery can omit a literature review. Feedback is advisory.')}</p>
    <fieldset disabled={locked}>
      <label>{uiText('Purpose')} <select value={purpose} onChange={e => { setPurpose(e.target.value); setFeedback(null) }}>
        <option value="scientific">{uiText('Scientific')}</option><option value="technical">{uiText('Technical')}</option></select></label>
      <label>{uiText('Conclusion')}<textarea maxLength={4000} value={summary} onChange={e => setSummary(e.target.value)} /></label>
      <label>{uiText('Reasoning, alternatives and limitations')}<textarea maxLength={12000} value={reasoning} onChange={e => setReasoning(e.target.value)} /></label>
      {purpose === 'technical' ? <>
        <p>{uiText('Technical status is stored as a comment on this exact experiment attempt, separately from research.')}</p>
        <label>{uiText('Experiment number')}<input value={technicalNode} inputMode="numeric" onChange={e => setTechnicalNode(e.target.value)} /></label>
        <label>{uiText('Attempt number')}<input value={technicalAttempt} inputMode="numeric" onChange={e => setTechnicalAttempt(e.target.value)} /></label>
      </> : <>
      <label>{uiText('Open question')}<input maxLength={1200} value={question} onChange={e => setQuestion(e.target.value)} /></label>
      <label>{uiText('What is already known?')}<textarea maxLength={1200} value={known} onChange={e => setKnown(e.target.value)} /></label>
      <label>{uiText('How does the source differ from this task?')}<textarea maxLength={1200} value={difference} onChange={e => setDifference(e.target.value)} /></label>
      <label>{uiText('Alternatives considered')}<textarea maxLength={1200} value={alternatives} onChange={e => setAlternatives(e.target.value)} /></label>
      <label>{uiText('Uncertainty or contradictions')}<textarea maxLength={1200} value={uncertainty} onChange={e => setUncertainty(e.target.value)} /></label>
      <label>{uiText('Decision and why')}<textarea maxLength={1200} value={decision} onChange={e => setDecision(e.target.value)} /></label>
      <label>{uiText('Evidence node numbers, separated by commas')}<input maxLength={255} value={nodeIds} onChange={e => setNodeIds(e.target.value)} /></label>
      <label>{uiText('Source title')}<input maxLength={500} value={sourceTitle} onChange={e => setSourceTitle(e.target.value)} /></label>
      <label>{uiText('Source URL')}<input maxLength={2000} value={source} onChange={e => setSource(e.target.value)} /></label>
      <label>{uiText('Claim supported by this source')}<textarea maxLength={1200} value={claim} onChange={e => setClaim(e.target.value)} /></label>
      <label>{uiText('Author-assessed claim status')}<select value={claimStatus} onChange={e => setClaimStatus(e.target.value)}>
        {['open', 'limited', 'supported', 'refuted', 'verified'].map(status => <option key={status} value={status}>{uiText(status)}</option>)}</select></label>
      <label>{uiText('Claim limitation')}<textarea maxLength={1200} value={claimLimit} onChange={e => setClaimLimit(e.target.value)} /></label>
      <label>{uiText('Contrast reference')}<input maxLength={200} value={contrastRef} onChange={e => setContrastRef(e.target.value)} /></label>
      <p>{uiText('Claim status is the author assessment, not automatic scientific verification.')}</p>
      <label>{uiText('Run-local lesson')}<textarea maxLength={1200} value={lesson} onChange={e => setLesson(e.target.value)} /></label>
      <label>{uiText('Lesson limitation')}<textarea maxLength={1200} value={lessonLimit} onChange={e => setLessonLimit(e.target.value)} /></label>
      <label>{uiText('Author-assessed lesson status')}<select value={lessonStatus} onChange={e => setLessonStatus(e.target.value)}>
        {['open', 'limited', 'supported', 'refuted', 'verified'].map(status => <option key={status} value={status}>{uiText(status)}</option>)}</select></label>
      <p>{uiText('The lesson uses the evidence node numbers above. Its status is an author assessment, not automatic verification.')}</p>
      <label>{uiText('Next experiment')}<input maxLength={1200} value={next} onChange={e => setNext(e.target.value)} /></label>
      </>}
    </fieldset>
    {purpose === 'technical' && technicalInvalid && <p role="status">{uiText('Enter a valid experiment and attempt; technical comments must fit within 8192 UTF-8 bytes.')}</p>}
    {purpose === 'scientific' && invalidNodeIds && <p role="status">{uiText('Use non-negative whole node numbers separated by commas.')}</p>}
    {!locked && <button type="button" onClick={preview} disabled={(purpose === 'scientific' && invalidNodeIds) || status === 'previewing'}>{uiText('Review draft')}</button>}
    {feedback && <ul>{feedback.issues.map((issue, i) => <li key={i}>{uiText(MESSAGES[issue.code] || issue.code)}</li>)}</ul>}
    {status === 'preview_error' && <p role="status">{uiText('Draft review is unavailable.')}</p>}
    {!locked && <button type="button" onClick={publish} disabled={!summary || (purpose === 'scientific' && invalidNodeIds) || technicalInvalid || status === 'publishing'}>{uiText(purpose === 'technical' ? 'Publish technical comment' : 'Publish memo')}</button>}
    {intent?.data && status === 'unknown' && <button type="button" onClick={publish}>{uiText('Retry the same draft')}</button>}
    {locked && status !== 'receipt' && <button type="button" onClick={checkReceipt} disabled={status === 'checking'}>{uiText('Check command receipt')}</button>}
    {status === 'receipt' && <button type="button" onClick={reset}>{uiText('Start another memo')}</button>}
    {status === 'storage_error' && <p role="status">{uiText('Cannot safely save the command key in this tab. Publishing is unavailable.')}</p>}
    {locked && !intent.data && <p role="status">{uiText('The command key was recovered; the draft text was not stored. Check the receipt before writing another memo.')}</p>}
    {status === 'unknown' && <p role="status">{uiText('Submission outcome is unknown. Retry the same saved draft and key, or inspect the command receipt.')}</p>}
    {receipt && <p role="status">{uiText('Command status')}: {uiText(receipt.status)} · {receipt.id}</p>}
    {!!receipt?.intake_notices?.length && <section><h4>{uiText('Publication notices')}</h4>
      <p>{uiText('Review these notices and correct the authored content or file the relevant questions as needed.')}</p>
      <ul>{receipt.intake_notices.map((notice, i) => <li key={i}>{uiText(MESSAGES[notice.kind] || notice.kind || '')}{notice.field ? ` · ${notice.field}` : ''}</li>)}</ul>
    </section>}
  </details>
}
