import { upstreamCheckSummary } from './upstreamCheckModel.js'

// State retains only 200 upstream events. This is a recorded hint, not live
// authority, writer liveness, or proof that an original request file still exists.
export function upstreamProposalSummary(history) {
  const rows = Array.isArray(history) ? history : []
  const types = new Set(['upstream_proposal_started', 'upstream_proposed', 'upstream_proposal_failed'])
  const relevant = rows.filter(row => types.has(row?.type))
  if (!relevant.length) return null
  const unknown = { status: 'unknown' }
  if (rows.some((row, index) => !row || !Number.isSafeInteger(row.seq) || row.seq < 0
      || index > 0 && row.seq <= rows[index - 1].seq)) return unknown
  const claim = relevant.filter(row => row.type === 'upstream_proposal_started').at(-1)
  if (!claim || typeof claim.proposal_id !== 'string' || !/^up_[0-9a-f]{24}$/.test(claim.proposal_id)
      || typeof claim.request_hash !== 'string' || !/^[0-9a-f]{64}$/.test(claim.request_hash)
      || typeof claim.action_id !== 'string' || !claim.action_id
      || relevant.filter(row => row.type === 'upstream_proposal_started'
        && (row.action_id === claim.action_id || row.proposal_id === claim.proposal_id)).length !== 1
      || relevant.some(row => row.seq > claim.seq && row.type !== 'upstream_proposal_started'
        && !relevant.some(start => start.type === 'upstream_proposal_started'
          && start.seq < row.seq && start.action_id === row.action_id
          && start.proposal_id === row.proposal_id && start.request_hash === row.request_hash))) return unknown
  const result = { proposal_id: claim.proposal_id, request_hash: claim.request_hash }
  const abandoned = rows.some(row => row.type === 'upstream_gate_abandoned'
    && row.seq > claim.seq && row.claim_action_id === claim.action_id)
  if (abandoned) return { ...result, status: 'abandoned' }
  const settlements = relevant.filter(row => row.type !== 'upstream_proposal_started'
    && row.action_id === claim.action_id)
  if (settlements.length > 1 || settlements.some(row => row.seq <= claim.seq
      || row.proposal_id !== claim.proposal_id || row.request_hash !== claim.request_hash)) return unknown
  if (!settlements.length) return { ...result, status: 'unfinished' }
  const proposal = settlements[0]
  if (proposal.type === 'upstream_proposal_failed') return { ...result, status: 'failed' }
  const following = rows.filter(row => row.seq > proposal.seq && row.proposal_id === claim.proposal_id)
  // Advancement records require a matching passing gate even in this hint. A
  // clipped gate/start must not become an optimistic "ready" state.
  const check = upstreamCheckSummary(following)
  const advance = following.find(row => row.type === 'base_advanced')
  // The latest verified check must be the one named by the advancement, and must
  // precede it. An orphan/abandoned older pass cannot borrow a newer check's proof.
  const status = advance ? (check?.status === 'passed' && check.seq === advance.gate_seq
      && check.seq < advance.seq ? 'advanced' : 'unknown')
    : check ? `check_${check.status}` : 'proposed'
  return { ...result, status, check, source_node_id: Number.isSafeInteger(proposal.source_node_id)
    && proposal.source_node_id >= 0 ? proposal.source_node_id : null }
}

export function upstreamRecoveryDraft(proposal, language) {
  const identity = proposal.proposal_id
    ? JSON.stringify({ proposal_id: proposal.proposal_id, expected_request_hash: proposal.request_hash }) : null
  return language === 'ru'
    ? `Помоги разобраться с последним переносом кода. Прочитай актуальные state generation и upstream_status; история UI ограничена и не разрешает действий.${identity ? ` Исходный запрос: ${identity}. Прочитай upstream_request со всеми страницами и проверь hashes, сохрани исходную generation внутри тела.` : ' Найди исходную identity в полной истории; не придумывай ключ или тело.'} Объясни состояние proposal, причину остановки, необходимость восстановления оператором и следующее действие. Предложи план; не повторяй записи, не запускай проверки, не меняй базу и не возобновляй запуск.`
    : `Help inspect the latest code reuse proposal. Read current state generation and upstream_status; bounded UI history authorizes no action.${identity ? ` Original request: ${identity}. Read all upstream_request pages and verify hashes, preserving the original generation inside its body.` : ' Find the original identity in complete history; do not invent a key or body.'} Explain proposal status, why work stopped, whether operator recovery is required and the next action. Propose a plan; do not retry writes, execute checks, advance the base or resume.`
}
