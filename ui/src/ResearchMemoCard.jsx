import { uiText, uiMessage, useUILanguage } from './uiLanguage.js'
import React, { useId, useMemo, useState } from 'react'
import Markdown, { stripMd } from './markdown.jsx'
import { OpIcon } from './icons.jsx'
import { memoLead, memoLeadIsPartial, normalizeResearchMemo } from './researchMemoModel.js'
import { safeExternalHref } from './urlSafety.js'
import './research-memo.css'

const plural = (count, one, many = `${one}s`) => `${count} ${count === 1 ? one : many}`

function triggerLabel(trigger) {
  if (trigger === 'cadence') return 'scheduled'
  if (trigger === 'strategist') return 'strategist requested'
  if (trigger === 'manual') return 'manual'
  return trigger
}

export function researchMemoTrust(memo) {
  const verdicts = memo.verification?.verdicts || []
  const unsupported = verdicts.filter(row => row.verdict === 'unsupported').length
  const uncertain = verdicts.filter(row => row.verdict === 'unclear' || row.verdict === 'cited').length
  const supported = verdicts.filter(row => row.verdict === 'supported').length
  const hasEvidenceRows = verdicts.length > 0 || memo.claims.length > 0
    || (memo.claimsOmitted || 0) > 0 || (memo.verification?.omittedVerdicts || 0) > 0
  const incomplete = hasEvidenceRows && ((memo.verification?.omittedVerdicts || 0) > 0
    || memo.claimsComplete === false
    || memo.claims.some(claim => claim.evidence?.complete === false)
    || verdicts.some(row => row.evidence?.complete !== true)
    || memo.verification?.alignment?.complete === false)
  if (unsupported) return {
    tone: 'alarm', label: `${unsupported} unsupported`, detail: 'Needs review',
    unsupported, uncertain, supported, incomplete,
  }
  if (incomplete || uncertain) return {
    tone: 'warn',
    label: (incomplete ? uiText('Check incomplete') : uiMessage("{0} unclear", [uncertain])),
    detail: 'With caveats', unsupported, uncertain, supported, incomplete,
  }
  if (verdicts.length) return {
    tone: 'ok', label: `${supported}/${verdicts.length} supported`, detail: 'Checked',
    unsupported, uncertain, supported, incomplete,
  }
  return {
    tone: 'neutral', label: 'Not verified', detail: 'Advisory',
    unsupported, uncertain, supported, incomplete,
  }
}

function TrustBadge({ trust }) {
  useUILanguage()

  const icon = trust.tone === 'alarm' ? 'alert' : trust.tone === 'ok' ? 'check' : 'dot'
  return <span className={`research-trust-badge ${trust.tone}`}
    title={`${trust.detail}: ${trust.label}`}>
    <OpIcon name={icon} size={12} /> {uiText(trust.label)}
  </span>
}

function SectionHeading({ icon, children, meta }) {
  useUILanguage()

  return <div className="research-block-heading">
    <OpIcon name={icon} size={14} />
    <h4>{children}</h4>
    {meta && <span className="research-block-meta">{meta}</span>}
  </div>
}

function EvidenceChip({ nodeId, nodeGeneration, unverified = false, url, label,
  onSelectNode, onSelectEvidence }) {
  useUILanguage()

  if (nodeId != null) {
    if (Number.isSafeInteger(nodeGeneration) && nodeGeneration >= 0) {
      const exactContent = <><OpIcon name="target" size={11} /> #{nodeId}{uiText(" · attempt ")}{nodeGeneration}</>
      const exactProps = {
        className: 'research-evidence-chip node exact',
        'data-node-generation': nodeGeneration,
        title: `Exact verifier evidence for experiment #${nodeId}, attempt ${nodeGeneration}`,
      }
      return onSelectEvidence
        ? <button type="button" {...exactProps}
            aria-label={uiMessage("Inspect exact verifier evidence for experiment {0}, attempt {1}", [nodeId, nodeGeneration])}
            onClick={() => onSelectEvidence(nodeId, nodeGeneration)}>{exactContent}</button>
        : <span {...exactProps}
            aria-label={uiMessage("Exact verifier evidence: experiment {0}, attempt {1}. Historical attempt navigation is unavailable here.", [nodeId, nodeGeneration])}>
            {exactContent}</span>
    }
    const claimContent = <><OpIcon name="target" size={11} /> #{nodeId}
      {((unverified ? uiText(' · current · unverified') : ''))}</>
    return onSelectNode
      ? <button type="button" className={`research-evidence-chip node${unverified ? ' unverified' : ''}`}
          onClick={() => onSelectNode(nodeId)}
          aria-label={(unverified ? uiMessage("Open current experiment {0}; this claim reference is unverified", [nodeId]) : uiMessage("Open experiment {0}", [nodeId]))}>{claimContent}</button>
      : <span className={`research-evidence-chip node${unverified ? ' unverified' : ''}`}>
          {claimContent}</span>
  }
  const href = safeExternalHref(url)
  const content = <><OpIcon name="link" size={11} /> {uiText(label)}</>
  return href
    ? <a className="research-evidence-chip source" href={href} target="_blank"
        rel="noreferrer noopener">{content}</a>
    : <span className="research-evidence-chip source">{content}</span>
}

function EvidenceDisclosure({ value, trust, onSelectNode, onSelectEvidence }) {
  useUILanguage()

  const verdicts = value.verification?.verdicts || []
  const rows = value.claims.length
    ? value.claims.map((claim, index) => ({ claim, verdict: verdicts[index] || null }))
    : verdicts.map(verdict => ({
        claim: { statement: verdict.statement, node_ids: [], urls: [], evidence: { complete: true } },
        verdict,
      }))
  const omitted = Math.max(value.claimsOmitted || 0, value.verification?.omittedVerdicts || 0)
  if (!rows.length && !omitted) return null
  const concern = trust.tone === 'alarm' || trust.tone === 'warn'
  return <details className={`research-memo-disclosure evidence ${concern ? 'has-warning' : ''}`}
    open={concern || undefined}>
    <summary>
      <OpIcon name="chevron-down" size={12} className="research-disclosure-chevron" />
      <span className="research-disclosure-title"><OpIcon name="check" size={14} />{uiText(" Evidence & Verification")}</span>
      <span className="research-disclosure-meta">
        {plural(rows.length, 'claim')} {(omitted > 0 ? uiMessage("· {0} omitted", [omitted]) : '')}
      </span>
    </summary>
    {omitted > 0 && <p className="research-warning" role="note">
      {value.verification?.omittedVerdicts > 0
        ? <>{uiText("Verification incomplete: showing ")}{verdicts.length}{uiText(" of ")}{value.verification.totalVerdicts}{uiText(" verifier verdicts.")}</>
        : <>{uiText("Verification incomplete: ")}{omitted}{uiText(" claim row")}{((omitted === 1 ? uiText(' is') : uiText('s are')))}{uiText(" not shown.")}</>}
    </p>}
    {value.claimsComplete === false && !value.claimsOmitted && <p className="research-warning" role="note">{uiText("Claim completeness could not be verified.")}</p>}
    {value.verification?.alignment?.complete === false && <p className="research-warning" role="note">{uiText("Claim-to-verifier alignment is incomplete. Supported labels are not treated as a complete check.")}</p>}
    <ol className="research-claim-list">
      {rows.map(({ claim, verdict }, index) => {
        const status = verdict?.verdict || 'unverified'
        const sourceLabels = claim.urls.map((url, sourceIndex) => {
          const source = value.sources.find(item => item.url === url)
          return source?.title || `source ${sourceIndex + 1}`
        })
        const verifierNodeRefs = verdict?.evidence?.node_refs || []
        const verifierNodeIds = new Set(verifierNodeRefs.map(ref => ref.node_id))
        const claimOnlyNodeIds = claim.node_ids.filter(nodeId => !verifierNodeIds.has(nodeId))
        return <li key={claim.claim_id || index} className={`research-claim ${status}`}>
          <div className="research-claim-main">
            <span className={`research-verdict ${status}`}>{uiText(status)}</span>
            <span>{((claim.statement || verdict?.statement || uiText('(statement unavailable)')))}</span>
          </div>
          {(verifierNodeRefs.length > 0 || claimOnlyNodeIds.length > 0 || claim.urls.length > 0)
            && <div className="research-evidence-chips"
            aria-label={uiText("Cited evidence")}>
            {verifierNodeRefs.map((ref, nodeIndex) => <EvidenceChip
              key={`verified-node-${nodeIndex}-${ref.node_id}-${ref.generation}`}
              nodeId={ref.node_id} nodeGeneration={ref.generation} onSelectEvidence={onSelectEvidence} />)}
            {claimOnlyNodeIds.map((nodeId, nodeIndex) => <EvidenceChip
              key={`claim-node-${nodeIndex}-${nodeId}`} nodeId={nodeId} unverified onSelectNode={onSelectNode} />)}
            {claim.urls.map((url, sourceIndex) => <EvidenceChip key={`source-${sourceIndex}-${url}`}
              url={url} label={sourceLabels[sourceIndex]} />)}
          </div>}
          {claim.evidence?.complete === false && <div className="research-warning compact">{uiText("Evidence references are incomplete.")}</div>}
          {verdict?.evidence?.complete !== true && <div className="research-warning compact">{uiText("Verification evidence identity is incomplete.")}</div>}
          {verdict && verdict.alignment?.statementMatches === false && <div className="research-warning compact">{uiText("Verifier checked a different statement: ")}{((verdict.statement || uiText('(statement unavailable)')))}</div>}
          {verdict && (verdict.alignment?.nodeIdsMatch === false
            || verdict.alignment?.sourceIdentitiesMatch === false) && <div className="research-warning compact">{uiText("Verifier evidence identities do not match this claim.")}</div>}
          {verdict?.alignment?.supportedEvidencePresent === false && <div className="research-warning compact">{uiText("Supported verdict has no verifiable evidence identity.")}</div>}
          {verdict?.note && <div className="research-verifier-note">{verdict.note}</div>}
        </li>
      })}
    </ol>
  </details>
}

function SourceDisclosure({ sources }) {
  useUILanguage()

  if (!sources.length) return null
  return <details className="research-memo-disclosure activity">
    <summary>
      <OpIcon name="chevron-down" size={12} className="research-disclosure-chevron" />
      <span className="research-disclosure-title"><OpIcon name="gear" size={14} />{uiText(" Research activity & sources")}</span>
      <span className="research-disclosure-meta">{plural(sources.length, 'step')}</span>
    </summary>
    <ol className="research-source-list">
      {sources.map((source, index) => {
        const href = safeExternalHref(source.url)
        const label = source.title || source.url || `Research step ${index + 1}`
        const looksLikeTool = /^[\w.-]+\([^)]*\)$/.test(label)
        return <li key={`${source.url}-${index}`}>
          <span className="research-source-index">{index + 1}</span>
          <div>
            <div className="research-source-title">
              {looksLikeTool && <span className="pill">{uiText("tool")}</span>}
              {href ? <a href={href} target="_blank" rel="noreferrer noopener">{uiText(label)}</a> : uiText(label)}
            </div>
            {source.snippet && <div className="research-source-snippet">{source.snippet}</div>}
          </div>
        </li>
      })}
    </ol>
  </details>
}

export function ResearchMemoBody({ memo, onSteer, steeringDirection = '', onSelectNode,
  onSelectEvidence, showSummary = false, compact = false, normalized = false, steeringNote }) {
  useUILanguage()

  // Collection owners already applied the bounded projection. Re-projecting that derived shape
  // would discard its omission receipts (`claimsTotal` / `claim.evidence`) because raw payloads use
  // different receipt field names.
  const value = useMemo(() => normalized ? memo : normalizeResearchMemo(memo), [memo, normalized])
  const trust = researchMemoTrust(value)
  return <div className={`research-memo-body${compact ? ' compact' : ''}`}>
    {showSummary && <section className="research-memo-block takeaway">
      <SectionHeading icon="search">{uiText("Research takeaway")}</SectionHeading>
      <div className="research-takeaway"><Markdown text={value.summary || uiText('No conclusion was recorded.')} externalOnly /></div>
    </section>}
    {value.findings.length > 0 && <section className="research-memo-block findings">
      <SectionHeading icon="bulb" meta={plural(value.findings.length, 'finding')}>{uiText("Key findings")}</SectionHeading>
      <ul>{value.findings.map((finding, index) => <li key={index}><Markdown text={finding} externalOnly /></li>)}</ul>
    </section>}
    <EvidenceDisclosure value={value} trust={trust} onSelectNode={onSelectNode}
      onSelectEvidence={onSelectEvidence} />
    {value.recommended_directions.length > 0 && <section className="research-memo-block actions">
      <SectionHeading icon="compass" meta={plural(value.recommended_directions.length, 'action')}>{uiText("Next actions")}</SectionHeading>
      {(steeringNote || onSteer) && <p className="research-action-note">{((steeringNote || uiText('Using a direction queues a hint for the next proposal. It does not start an experiment.')))}</p>}
      <ol className="research-direction-list">{value.recommended_directions.map((direction, index) => {
        const busy = steeringDirection === direction
        return <li key={index}>
          <span>{stripMd(direction)}</span>
          {onSteer && <button type="button" className="btn sm ghost"
            disabled={!!steeringDirection} aria-busy={busy || undefined}
            aria-label={uiMessage("Use for next proposal: {0}", [stripMd(direction)])}
            onClick={() => onSteer(direction)}>{((busy ? uiText('Queueing…') : uiText('Use direction')))}</button>}
        </li>
      })}</ol>
    </section>}
    <SourceDisclosure sources={value.sources} />
    {value.reasoning && <details className="research-memo-disclosure reasoning">
      <summary>
        <OpIcon name="chevron-down" size={12} className="research-disclosure-chevron" />
        <span className="research-disclosure-title"><OpIcon name="bug" size={14} />{uiText(" Technical reasoning")}</span>
        <span className="research-disclosure-meta">{uiText("debug")}</span>
      </summary>
      <Markdown className="think-body research-reasoning" text={value.reasoning} />
    </details>}
  </div>
}

export default function ResearchMemoCard({ memo, memoNumber = 1, open, onToggle,
  defaultOpen = false, latest = false, variant = 'panel', keepMounted = false,
  staticOpen = false, onSteer, steeringDirection = '', steeringNote,
  onSelectNode, onSelectEvidence, normalized = false }) {
  useUILanguage()

  const value = useMemo(() => normalized ? memo : normalizeResearchMemo(memo), [memo, normalized])
  const trust = researchMemoTrust(value)
  const [internalOpen, setInternalOpen] = useState(defaultOpen)
  const controlled = typeof open === 'boolean'
  const expanded = staticOpen || (controlled ? open : internalOpen)
  const reactId = useId().replace(/:/g, '')
  const headingId = `research-memo-${reactId}-heading`
  const bodyId = `research-memo-${reactId}-body`
  const toggle = () => {
    if (controlled) onToggle?.(memoNumber - 1)
    else setInternalOpen(current => !current)
  }
  const classes = [
    'research-memo-card', variant === 'report' ? 'memo-card' : 'rsch-memo',
    `tone-${trust.tone}`, expanded ? 'open' : 'closed',
  ].join(' ')
  const HeaderTag = staticOpen ? 'span' : 'button'
  const headerProps = staticOpen
    ? { className: 'research-memo-toggle static' }
    : { type: 'button', className: 'research-memo-toggle disclosure-button',
        'aria-expanded': expanded, 'aria-controls': bodyId, onClick: toggle }
  return <article className={classes} aria-labelledby={headingId}>
    <h3 className="research-memo-heading" id={headingId}>
      <HeaderTag {...headerProps}>
        <span className="research-memo-chevron">
          <OpIcon name={expanded ? 'chevron-up' : 'chevron-down'} size={13} />
        </span>
        <span className="research-memo-title-group">
          <span className="research-memo-kicker">
            <OpIcon name="search" size={13} />{uiText(" Research memo #")}{memoNumber}
            {latest && <span className="pill">{uiText("latest")}</span>}
            {value.trigger && <span>{uiText(triggerLabel(value.trigger))}</span>}
            {value.at_node != null && <span>{uiMessage(value.at_node === 1 ? "After {0} experiment" : "After {0} experiments", [value.at_node])}</span>}
          </span>
          {/* Accordion headers show a lead; the static reading header goes straight to the research takeaway. */}
          {!staticOpen && <span className="research-memo-summary" title={value.summary || undefined}>
            {((memoLead(value.summary) || uiText('No conclusion was recorded.')))}
            {memoLeadIsPartial(value.summary)
              && <span className="research-memo-more">{uiText(" — full conclusion below")}</span>}
          </span>}
        </span>
        <span className="research-memo-overview">
          <TrustBadge trust={trust} />
          {(value.claimsTotal || value.verification?.totalVerdicts) > 0
            && <span>{plural(value.claimsTotal || value.verification?.totalVerdicts, 'claim')}</span>}
          {value.sources.length > 0 && <span>{plural(value.sources.length, 'research step')}</span>}
        </span>
      </HeaderTag>
    </h3>
    {(expanded || keepMounted) && <div id={bodyId} role="region" aria-labelledby={headingId}
      className="research-memo-region" hidden={!expanded}>
      {/* `showSummary` because the header now shows only a lead: without it the full conclusion
          would exist nowhere on this card. */}
      <ResearchMemoBody memo={value} normalized showSummary onSteer={onSteer}
        steeringDirection={steeringDirection} steeringNote={steeringNote} onSelectNode={onSelectNode}
        onSelectEvidence={onSelectEvidence} />
    </div>}
  </article>
}
