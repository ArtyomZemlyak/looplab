import React from 'react'
import ResearchMemoCard from './ResearchMemoCard.jsx'

// Report-local research memo. This must not live in panels.jsx: importing the report alone should
// never pull the optional panel hub and its owner-only dependencies into the report closure.
export default function MemoCard({ memo, idx, open, onToggle, latest = false, onSelectNode,
  onSelectEvidence, normalized = false }) {
  const memoIndex = Number.isSafeInteger(idx) && idx >= 0 ? idx : 0
  return <ResearchMemoCard memo={memo} memoNumber={memoIndex + 1} open={open}
    onToggle={() => onToggle?.(memoIndex)} variant="report" keepMounted latest={latest}
    onSelectNode={onSelectNode} onSelectEvidence={onSelectEvidence} normalized={normalized} />
}
